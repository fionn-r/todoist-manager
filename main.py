import logging
import os
from datetime import date, datetime
from pathlib import Path
from typing import cast
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from todoist_api_python.api import TodoistAPI
from todoist_api_python.models import Task

LABEL_TO_MATCH_START_TASKS = "start"

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


def process_start_tasks(api: TodoistAPI, timezone: str | None) -> None:
    """
    Process tasks with @start label and due date.
    If the due date is today, remove the @start label and the due date.
    """
    try:
        tasks_iterator = api.get_tasks(label=LABEL_TO_MATCH_START_TASKS)

        tasks: list[Task] = []

        for tasks in tasks_iterator:
            tasks.extend(tasks)

        # Make sure all tasks are unique in the list based on their ID
        unique_tasks = {task.id: task for task in tasks}.values()
        tasks = list(unique_tasks)

        logger.info(f"Fetched {len(tasks)} tasks from Todoist")

        # Get today's date in configured timezone or system timezone
        if timezone:
            tz = ZoneInfo(timezone)
            today = datetime.now(tz).date()
            logger.info(f"Checking for tasks due today ({timezone}): {today}")
        else:
            today = datetime.now().astimezone().date()
            tz_name = datetime.now().astimezone().tzname()
            logger.info(
                f"Checking for tasks due today (system timezone: {tz_name}): {today}"
            )

        processed_count = 0

        logger.info(f"{'\n'.join(str(task) for task in tasks)}")

        for task in tasks:
            if task.labels and LABEL_TO_MATCH_START_TASKS in task.labels and task.due:
                due: date = cast(date, task.due.date)

                if due == today:
                    logger.info(f"Processing task: {task.content} (ID: {task.id})")

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


def main():
    """Main entry point for the Todoist manager service."""
    env_path = Path(__file__).parent / ".env"
    load_dotenv(env_path)

    logger.info("Starting Todoist Manager")

    api_token = os.getenv("TODOIST_API_TOKEN")
    if not api_token:
        logger.error("TODOIST_API_TOKEN not set in .env file or environment")
        raise ValueError("TODOIST_API_TOKEN is required")

    timezone = os.getenv("TIMEZONE")
    if timezone:
        logger.info(f"Using configured timezone: {timezone}")
    else:
        logger.info("Using system timezone")

    api = TodoistAPI(api_token)
    logger.info("Connected to Todoist API")

    # Process @start tasks
    process_start_tasks(api, timezone)

    logger.info("Todoist Manager completed successfully")


if __name__ == "__main__":
    main()
