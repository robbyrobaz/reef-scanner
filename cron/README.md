# Reef Scanner Cron

The scanner runs automatically via Hermes MCP cron, not system crontab.

## Schedule

- **Every 5 minutes** — `ec5dfb8ad7f6`
- **Job name:** Reef DEX Scanner
- **Repeat:** forever

## Cron ID

```
ec5dfb8ad7f6
```

## To Manage

```python
# List all cron jobs
mcp_cronjob(action="list")

# Run manually now
mcp_cronjob(action="run", job_id="ec5dfb8ad7f6")

# Pause
mcp_cronjob(action="pause", job_id="ec5dfb8ad7f6")

# Resume
mcp_cronjob(action="resume", job_id="ec5dfb8ad7f6")

# Remove
mcp_cronjob(action="remove", job_id="ec5dfb8ad7f6")
```

## Note

This cron is managed by Hermes (the agent framework). If Hermes restarts or the session clears, the cron schedule is preserved but execution depends on the agent being active.

For **reliable execution without Hermes**, use system crontab:

```bash
*/5 * * * * cd /home/rob/reef-workspace && venv/bin/python scanner.py >> /home/rob/reef-workspace/cron/scanner.log 2>&1
```

## What the Cron Runs

```bash
cd /home/rob/reef-workspace && venv/bin/python scanner.py
```

Output:
- `data/wallets.csv` — ranked profitable wallets
- `data/swaps.csv` — raw swap history
