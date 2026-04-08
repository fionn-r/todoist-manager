# Todoist Manager

A housekeeping service for Todoist that automatically manages tasks based on specific rules.

## Features

### @start Label Processing
- Checks all tasks for the `@start` label and a due date
- When the due date matches today's date:
  - Removes the `@start` label
  - Removes the due date
- Useful for tasks you want to start working on when they become due

## Setup

### 1. Install Dependencies

```bash
uv sync
```

### 2. Configure the Service

1. Copy the example environment file:
```bash
cp .env.example .env
```

2. Edit `.env` and add your Todoist API token:
   - Go to Todoist Settings → Integrations → Developer
   - Copy your API token
   - Paste it into the `.env` file

3. (Optional) Customize the timezone in `.env`:
   - By default, uses your system's local timezone
   - To override, uncomment and set the `TIMEZONE` variable
   - Use IANA timezone names (e.g., `America/New_York`, `Europe/London`)
   - See [list of timezones](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones)

## Usage

### Run Manually

```bash
uv run python main.py
```

### Run as Systemd Service with Timer

1. Copy the service files:
```bash
sudo cp todoist-manager.service /etc/systemd/system/
sudo cp todoist-manager.timer /etc/systemd/system/
```

2. Ensure your `.env` file is configured in the project directory

3. Enable and start the timer:

```bash
sudo systemctl daemon-reload
sudo systemctl enable todoist-manager.timer
sudo systemctl start todoist-manager.timer
```

4. Check status:

```bash
sudo systemctl status todoist-manager.timer
sudo systemctl list-timers todoist-manager.timer
```

## Logging

The service logs all operations with timestamps. When running via cron, logs are written to the configured log file. When running via systemd, logs are available through journalctl:

```bash
journalctl -u todoist-manager.service -f
```

## Future Features

Additional housekeeping rules can be added to the service as needed.
