# Reef Scanner Cron & Dashboard

The scanner runs via **system cron** (primary) and Hermes MCP cron (backup).
The dashboard serves at **http://0.0.0.0:8891**.

---

## System Cron — Primary

**Installed at:** `/var/spool/cron/crontabs/rob`

```
# Reef Scanner Cron — owned by reef profile
*/5 * * * * cd /home/rob/reef-workspace && venv/bin/python scanner.py >> /home/rob/reef-workspace/cron/scanner.log 2>&1
```

**To view/edit:**
```bash
crontab -e        # edit
crontab -l        # list
crontab -r        # remove
```

**Scanner Logs:**
```bash
tail -f /home/rob/reef-workspace/cron/scanner.log
```

---

## Dashboard

**URL:** `http://<host>:8891` (internal network only)

**Start manually:**
```bash
cd /home/rob/reef-workspace && venv/bin/python dashboard.py
# or
/home/rob/reef-workspace/cron/start-dashboard.sh
```

**Restart if not running:**
```bash
pkill -f "reef-workspace/dashboard.py" && cd /home/rob/reef-workspace && nohup venv/bin/python dashboard.py >> cron/dashboard.log 2>&1 &
```

**Dashboard logs:**
```bash
tail -f /home/rob/reef-workspace/cron/dashboard.log
```

**Features:**
- Real-time stats (total swaps, wallets, buys/sells)
- Top wallets by score
- Recent swap activity
- DEX breakdown
- Cron log viewer
- Auto-refreshes every 30s

---

## Hermes Cron — Backup

- **Every 5 minutes** — `ec5dfb8ad7f6`
- **Job name:** Reef DEX Scanner

```python
mcp_cronjob(action="list")
mcp_cronjob(action="run", job_id="ec5dfb8ad7f6")
mcp_cronjob(action="pause", job_id="ec5dfb8ad7f6")
mcp_cronjob(action="resume", job_id="ec5dfb8ad7f6")
mcp_cronjob(action="remove", job_id="ec5dfb8ad7f6")
```

---

## Cron ID

```
ec5dfb8ad7f6
```

---

## Ownership

This cron is named and owned by the **reef** profile. Other profiles should check before modifying.

Naming convention: `reef-*` for all reef-owned cron jobs and processes.

---

## Data Outputs

| File | Description |
|------|-------------|
| `data/wallets.csv` | Ranked wallet list with scores, win rates, ROI |
| `data/swaps.csv` | Raw swap history |
| `cron/scanner.log` | Scanner execution log |
| `cron/dashboard.log` | Dashboard server log |
