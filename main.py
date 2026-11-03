import argparse
import calendar
import json
import logging
import os
import re
import tomllib
import urllib.parse
import urllib.request
import uuid
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import cast
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from todoist_api_python.api import TodoistAPI
from todoist_api_python.models import Task

LABEL_TO_MATCH_START_TASKS = "start"
LABEL_TO_MATCH_RECURRING_TASKS = "recurring"
REPEAT_PATTERN = re.compile(r"^\s*repeat:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
INTERVAL_PATTERN = re.compile(
    r"\bevery!?\s+(other\s+|\d+\s+)?(day|week|month|year)s?\b", re.IGNORECASE
)
COMPLETED_LOOKBACK = timedelta(days=7)
SYNC_API_URL = "https://api.todoist.com/api/v1/sync"
CONTEXTS_CONFIG_PATH = Path(__file__).parent / "contexts.toml"
MANAGED_FILTER_DESCRIPTION = (
    "Managed by todoist-manager (contexts.toml). Edit the base filter instead."
)
MAX_FILTER_QUERY_LENGTH = 1024
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


def sync_request(api_token: str, **fields: object) -> dict:
    """
    POST to the Todoist Sync API, JSON-encoding any non-string fields.
    Raises if any command in the request was not applied.
    """
    data = urllib.parse.urlencode(
        {k: v if isinstance(v, str) else json.dumps(v) for k, v in fields.items()}
    ).encode()
    request = urllib.request.Request(
        SYNC_API_URL, data=data, headers={"Authorization": f"Bearer {api_token}"}
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        body = json.load(response)

    failed = {
        command_uuid: status
        for command_uuid, status in body.get("sync_status", {}).items()
        if status != "ok"
    }
    if failed:
        raise RuntimeError(f"Sync commands failed: {failed}")

    return body


def resolve_timezone(user: dict, override: str | None) -> ZoneInfo:
    """
    Use the TIMEZONE override if set, otherwise the Todoist account's timezone,
    so time windows line up with how Todoist itself decides "today".
    """
    if override:
        logger.info(f"Using timezone from TIMEZONE override: {override}")
        return ZoneInfo(override)

    timezone = user["tz_info"]["timezone"]
    logger.info(f"Using Todoist account timezone: {timezone}")
    return ZoneInfo(timezone)


def process_start_tasks(api: TodoistAPI, tz: ZoneInfo, dry_run: bool) -> None:
    """
    Process tasks with @start label and due date.
    If the due date is today, remove the @start label and the due date.
    """
    try:
        tasks_iterator = api.get_tasks(label=LABEL_TO_MATCH_START_TASKS)

        tasks: list[Task] = []

        for page in tasks_iterator:
            tasks.extend(page)

        initial_task_count = len(tasks)

        # Make sure all tasks are unique in the list based on their ID
        unique_tasks = {task.id: task for task in tasks}.values()
        tasks = list(unique_tasks)

        logger.info(f"Fetched {len(tasks)} tasks from Todoist ({initial_task_count - len(tasks)} duplicates removed)")

        today = datetime.now(tz).date()
        logger.info(f"Checking for tasks due today: {today}")

        processed_count = 0

        logger.info(f"{'\n'.join(str(task) for task in tasks)}")

        for task in tasks:
            if task.labels and LABEL_TO_MATCH_START_TASKS in task.labels and task.due:
                due: date = cast(date, task.due.date)

                if due <= today:
                    logger.info(f"Processing task: {task.content} (ID: {task.id})")

                    if dry_run:
                        logger.info(f"[dry run] Would remove @start label and due date from: {task.content}")
                        continue

                    updated_labels = [
                        label
                        for label in task.labels
                        if label != LABEL_TO_MATCH_START_TASKS
                    ]

                    api.update_task(
                        task_id=task.id,
                        labels=updated_labels,
                        due_string="no date",
                    )

                    processed_count += 1
                    logger.info(
                        f"Removed @start label and due date from: {task.content}"
                    )

        logger.info(f"Processed {processed_count} tasks")

    except Exception as e:
        logger.error(f"Error processing tasks: {e}")
        raise


def parse_repeat_interval(text: str) -> tuple[int, str] | None:
    """
    Read the interval from a recurrence like "every! 2 months starting 1 dec".
    Returns (count, unit) or None if there is no simple interval.
    """
    match = INTERVAL_PATTERN.search(text)
    if not match:
        return None

    amount = (match.group(1) or "").strip().lower()
    count = 2 if amount == "other" else int(amount or 1)
    return count, match.group(2).lower()


def add_interval(start: date, count: int, unit: str) -> date:
    """Add count units to a date, clamping to the end of the month (31 Dec + 2 months is 28 Feb)."""
    if unit == "day":
        return start + timedelta(days=count)
    if unit == "week":
        return start + timedelta(weeks=count)

    months = count * 12 if unit == "year" else count
    year, month = divmod(start.month - 1 + months, 12)
    year += start.year
    month += 1
    day = min(start.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def process_recurring_tasks(api: TodoistAPI, tz: ZoneInfo, dry_run: bool) -> None:
    """
    Keep @recurring tasks undated once they're due, so they can't go overdue,
    and bring them back one interval after they're completed.

    The interval lives in a "Repeat: every ..." description line, taken from
    the task's own recurrence (e.g. "every! 2 months") when its date is removed.
    """
    try:
        now = datetime.now(tz)
        today = now.date()

        # Reopen completed tasks first, so any whose next start has already
        # passed are made undated below in the same run
        completed: list[Task] = []

        for page in api.get_completed_tasks_by_completion_date(
            since=now - COMPLETED_LOOKBACK, until=now
        ):
            completed.extend(page)

        completed_ids = {
            task.id
            for task in completed
            if task.labels and LABEL_TO_MATCH_RECURRING_TASKS in task.labels
        }
        logger.info(f"Found {len(completed_ids)} completed @recurring tasks in the last {COMPLETED_LOOKBACK.days} days")

        reopened_count = 0

        for task_id in completed_ids:
            # Re-fetch so tasks reopened on an earlier run are skipped
            task = api.get_task(task_id)
            if not task.is_completed:
                continue

            repeat = REPEAT_PATTERN.search(task.description or "")
            interval = parse_repeat_interval(repeat.group(1)) if repeat else None
            if interval is None:
                logger.warning(
                    f"Not reopening {task.content}: no 'Repeat: every ...' line in its description"
                )
                continue

            completed_at = cast(datetime, task.completed_at)
            if completed_at.tzinfo is None:
                completed_at = completed_at.replace(tzinfo=ZoneInfo("UTC"))
            next_start = add_interval(completed_at.astimezone(tz).date(), *interval)

            if dry_run:
                logger.info(f"[dry run] Would reopen {task.content} starting {next_start}")
                continue

            api.uncomplete_task(task.id)
            api.update_task(task_id=task.id, due_date=next_start)

            reopened_count += 1
            logger.info(f"Reopened {task.content} starting {next_start}")

        logger.info(f"Reopened {reopened_count} @recurring tasks")

        # Remove the date from tasks whose start date has arrived
        tasks: list[Task] = []

        for page in api.get_tasks(label=LABEL_TO_MATCH_RECURRING_TASKS):
            tasks.extend(page)

        tasks = list({task.id: task for task in tasks}.values())
        floated_count = 0

        for task in tasks:
            if not task.due or cast(date, task.due.date) > today:
                continue

            # Removing the date drops its recurrence, so keep it as a Repeat: line
            description = task.description or ""
            if not REPEAT_PATTERN.search(description):
                if not task.due.is_recurring or parse_repeat_interval(task.due.string) is None:
                    logger.warning(
                        f"Skipping {task.content}: give it a recurrence like 'every! 2 months' "
                        "or a 'Repeat: every 2 months' description line"
                    )
                    continue
                description = f"{description}\nRepeat: {task.due.string}".strip()

            if dry_run:
                logger.info(f"[dry run] Would remove due date from: {task.content}")
                if description != task.description:
                    logger.info(f"[dry run] Would add 'Repeat: {task.due.string}' to: {task.content}")
                continue

            api.update_task(
                task_id=task.id,
                due_string="no date",
                description=description if description != task.description else None,
            )

            floated_count += 1
            logger.info(f"Removed due date from: {task.content}")

        logger.info(f"Removed due dates from {floated_count} @recurring tasks")

    except Exception as e:
        logger.error(f"Error processing @recurring tasks: {e}")
        raise


def parse_time_range(value: str) -> tuple[time, time]:
    """Parse "HH:MM-HH:MM" into a (start, end) pair."""
    start, end = value.split("-")
    return time.fromisoformat(start.strip()), time.fromisoformat(end.strip())


def load_contexts_config(path: Path) -> dict:
    """
    Load contexts.toml and check it up front, so a typo fails loudly
    rather than silently never matching.
    """
    with path.open("rb") as f:
        config = tomllib.load(f)

    if not config.get("bases") or not config.get("contexts"):
        raise ValueError(f"{path.name} needs at least one base filter and one context")

    for context_name, context in config["contexts"].items():
        unknown_keys = set(context) - {"hide"}
        if unknown_keys:
            raise ValueError(f"Context '{context_name}': unknown keys {sorted(unknown_keys)}")

    for number, rule in enumerate(config.get("rules", []), start=1):
        unknown_keys = set(rule) - {"hide", "when", "unless", "contexts"}
        if unknown_keys:
            raise ValueError(f"Rule {number}: unknown keys {sorted(unknown_keys)}")
        if not rule.get("hide"):
            raise ValueError(f"Rule {number}: 'hide' must list at least one term")

        for context_name in rule.get("contexts", []):
            if context_name not in config["contexts"]:
                raise ValueError(f"Rule {number}: unknown context '{context_name}'")

        for condition in (rule.get("when", {}), rule.get("unless", {})):
            for key, value in condition.items():
                if key == "time":
                    parse_time_range(value)
                elif key == "days":
                    unknown_days = set(value) - set(WEEKDAYS)
                    if unknown_days:
                        raise ValueError(f"Rule {number}: unknown days {sorted(unknown_days)} (use {WEEKDAYS})")
                else:
                    raise ValueError(f"Rule {number}: unknown condition '{key}' (supported: time, days)")

    return config


def condition_holds(condition: dict, now: datetime) -> bool:
    """True when every check in the condition matches now. An empty condition always holds."""
    for key, value in condition.items():
        if key == "time":
            start, end = parse_time_range(value)
            current = now.time()
            if start < end:
                matched = start <= current < end
            else:
                # Window wraps past midnight, e.g. 18:00-00:00 or 22:00-02:00
                matched = current >= start or current < end
        elif key == "days":
            matched = WEEKDAYS[now.weekday()] in value
        else:
            raise ValueError(f"Unknown condition '{key}'")

        if not matched:
            return False

    return True


def hidden_terms_for_context(context_name: str, config: dict, now: datetime) -> list[str]:
    """Collect the context's own hidden terms plus those from every rule active right now."""
    terms = list(config["contexts"][context_name].get("hide", []))

    for rule in config.get("rules", []):
        if "contexts" in rule and context_name not in rule["contexts"]:
            continue

        active = condition_holds(rule.get("when", {}), now) and not (
            rule.get("unless") and condition_holds(rule["unless"], now)
        )
        if active:
            terms.extend(term for term in rule["hide"] if term not in terms)

    return terms


def split_filter_sections(query: str) -> list[str]:
    """
    Split a filter query on top-level commas. Each comma-separated section
    is shown as its own list in the filter view.
    """
    sections: list[str] = []
    current: list[str] = []
    depth = 0
    escaped = False

    for char in query:
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "," and depth == 0:
            sections.append("".join(current).strip())
            current = []
            continue
        current.append(char)

    sections.append("".join(current).strip())
    return [section for section in sections if section]


def build_view_query(base_query: str, hidden_terms: list[str]) -> str:
    """Append an exclusion for each hidden term to every section of the base query."""
    exclusions = "".join(f" & !({term})" for term in hidden_terms)
    return ", ".join(
        f"({section}){exclusions}" for section in split_filter_sections(base_query)
    )


def count_matching_tasks(api: TodoistAPI, query: str) -> int:
    """Count tasks matching a multi-section query, querying each section separately."""
    task_ids = {
        task.id
        for section in split_filter_sections(query)
        for page in api.filter_tasks(query=section)
        for task in page
    }
    return len(task_ids)


def process_context_filters(
    api: TodoistAPI, api_token: str, filters: list[dict], tz: ZoneInfo, dry_run: bool
) -> None:
    """
    Generate a "<base> - <context>" view for every base filter and context in
    contexts.toml, hiding terms according to the context and the rules active
    right now. Only filters whose query has changed are written.
    """
    config = load_contexts_config(CONTEXTS_CONFIG_PATH)
    now = datetime.now(tz)
    live_filters = {f["name"]: f for f in filters if not f.get("is_deleted")}

    missing_bases = [name for name in config["bases"] if name not in live_filters]
    if missing_bases:
        raise ValueError(f"Base filters not found in Todoist: {missing_bases}")

    logger.info(f"Building context views at {now:%a %H:%M}")

    commands = []
    views = [
        (live_filters[base_name], context_name, f"{base_name} - {context_name}")
        for base_name in config["bases"]
        for context_name in config["contexts"]
    ]

    for base, context_name, view_name in views:
        hidden_terms = hidden_terms_for_context(context_name, config, now)
        query = build_view_query(base["query"], hidden_terms)
        logger.info(f"{view_name}: hiding {', '.join(hidden_terms) or 'nothing'}")

        if len(query) > MAX_FILTER_QUERY_LENGTH:
            logger.warning(f"{view_name}: query is {len(query)} characters, Todoist may reject it")

        existing = live_filters.get(view_name)

        if existing is None:
            action = "create"
            commands.append(
                {
                    "type": "filter_add",
                    "temp_id": str(uuid.uuid4()),
                    "uuid": str(uuid.uuid4()),
                    "args": {
                        "name": view_name,
                        "query": query,
                        "description": MANAGED_FILTER_DESCRIPTION,
                        "color": base["color"],
                        "is_favorite": True,
                    },
                }
            )
        elif existing["query"] != query or existing.get("description") != MANAGED_FILTER_DESCRIPTION:
            action = "update"
            commands.append(
                {
                    "type": "filter_update",
                    "uuid": str(uuid.uuid4()),
                    "args": {
                        "id": existing["id"],
                        "query": query,
                        "description": MANAGED_FILTER_DESCRIPTION,
                    },
                }
            )
        else:
            action = "unchanged"

        if dry_run:
            logger.info(f"[dry run] {view_name} ({action}): {query}")
            logger.info(f"[dry run] {view_name} currently matches {count_matching_tasks(api, query)} tasks")

    if dry_run:
        logger.info(f"[dry run] Would write {len(commands)} context filters")
        return

    if commands:
        sync_request(api_token, commands=commands)

    logger.info(f"Wrote {len(commands)} context filters")


def main():
    """Main entry point for the Todoist manager service."""
    parser = argparse.ArgumentParser(description="Housekeeping for Todoist")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="log what would change without writing anything to Todoist",
    )
    args = parser.parse_args()

    env_path = Path(__file__).parent / ".env"
    load_dotenv(env_path)

    logger.info("Starting Todoist Manager")

    api_token = os.getenv("TODOIST_API_TOKEN")
    if not api_token:
        logger.error("TODOIST_API_TOKEN not set in .env file or environment")
        raise ValueError("TODOIST_API_TOKEN is required")

    api = TodoistAPI(api_token)
    logger.info("Connected to Todoist API")

    state = sync_request(api_token, sync_token="*", resource_types=["user", "filters"])
    tz = resolve_timezone(state["user"], os.getenv("TIMEZONE"))

    # Process @recurring tasks
    process_recurring_tasks(api, tz, args.dry_run)

    # Process @start tasks
    process_start_tasks(api, tz, args.dry_run)

    # Regenerate context views (Top - Home, Top - Office, ...)
    process_context_filters(api, api_token, state["filters"], tz, args.dry_run)

    logger.info("Todoist Manager completed successfully")


if __name__ == "__main__":
    main()
