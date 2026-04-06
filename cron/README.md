# Reef Scanner Cron

The scanner runs via **system cron** (primary) and Hermes MCP cron (backup).

## System Cron — Primary

**Installed at:** `/var/spool/cron/crontabs/rob`

```
# Reef Scanner Cron — owned by reef profile
# Managed via Hermes MCP cron job_id: ec5dfb8ad7f6
*/5 * * * * cd /home/rob/reef-workspace && /home/rob/reef-workspace/venv/bin/python scanner.py >> /home/rob/reef-workspace/cron/scanner.log 2>&1
```

**To view/edit:**
```bash
crontab -e        # edit
crontab -l        # list
crontab -r        # remove
```

**Logs:**
```bash
tail -f /home/rob/reef-workspace/cron/scanner.log
```

## Hermes Cron — Backup

- **Every 5 minutes** — `ec5dfb8ad7f6`
- **Job name:** Reef DEX Scanner
- **Repeat:** forever

```python
# Manage Hermes cron
mcp_cronjob(action="list")
mcp_cronjob(action="run", job_id="ec5dfb8ad7f6")
mcp_cronjob(action="pause", job_id="ec5dfb8ad7f6")
mcp_cronjob(action="resume", job_id="ec5dfb8ad7f6")
mcp_cronjob(action="remove", job_id="ec5dfb8ad7f6")
```

## Cron ID

```
ec5dfb8ad7f6
```

## Ownership

This cron is named and owned by the **reef** profile. Other profiles should check before modifying.

Naming convention: `reef-*` for all reef-owned cron jobs.

## What the Cron Runs

```bash
cd /home/rob/reef-workspace && venv/bin/python scanner.py
```

Output:
- `data/wallets.csv` — ranked profitable wallets
- `data/swaps.csv` — raw swap history
- `cron/scanner.log` — execution log

## Note

System cron runs independently of Hermes chat sessions. Hermes cron is a backup/notification mechanism and depends on the agent being active.
