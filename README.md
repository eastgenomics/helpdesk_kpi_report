# helpdesk_kpi_report

KPI report for the Bioinformatics Helpdesk (Jira Service Management project `EBH`). For every helpdesk ticket type except Sequencing Run, which has its own KPI tracking, it measures how quickly tickets are assigned, started and resolved, and how often they meet the helpdesk SLAs. It writes a self-contained HTML report with charts, plus CSVs for further analysis.

Jira: [DI-379](https://cuhbioinformatics.atlassian.net/browse/DI-379)

## KPIs

The KPIs are based on the SLAs set up in the helpdesk:
- time to first response: 72h
- time to resolution: 72h for Reanalysis and Urgent Reanalysis, 336h for everything else

Every KPI is reported by ticket type and by the month tickets were created. Durations are medians, with the 90th percentile where it helps, in both **working hours** and **calendar hours**.

| KPI | Definition |
|---|---|
| Volume | Tickets created and resolved |
| Time to assign | From creation until an assignee is first set (0 if assigned on creation) |
| Time to first response | Elapsed time from the helpdesk SLA timer, and % within target |
| Time to start work | From creation until the first move into an in-progress status (waiting statuses don't count) |
| Time to resolve | From creation until the resolution date, and % within the SLA target |
| Time waiting on requester | Time spent in *Waiting for response* or *On hold* (resolved tickets only) |
| Outcome mix | Final status of resolved tickets (Done, Resolved, Closed, Acknowledged, Cancelled) |
| Reopen rate | % of resolved tickets that moved from a done status back to an open one |
| Open backlog | All tickets open now, by type and age, how many are unassigned, and the oldest open tickets |

How some of these are worked out:
- **Working hours:** Mon–Fri 09:00–17:00 UK time, excluding England and Wales bank holidays. The bank holidays come from the [gov.uk feed](https://www.gov.uk/bank-holidays.json).
- **SLA % met:** counts only tickets whose SLA timer has finished or breached. The SLA timers themselves run on calendar time, 24 hours a day, 7 days a week.
- **"Inform us" tickets:** these only need acknowledging. They appear in the per-type tables but are left out of the all-types start, resolve and waiting figures (`--exclude-from-totals`).
- **Provisional months:** recent months where more than 10% of tickets are still open are marked with `*`, because their figures only cover the tickets resolved so far.

## Setup

Python 3.10 or later.

```bash
git clone https://github.com/eastgenomics/helpdesk_kpi_report.git
cd helpdesk_kpi_report
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

matplotlib is only needed for the charts. Without it, the report is still written, just with no charts.

### Jira API token

1. Create a token at <https://id.atlassian.com/manage-profile/security/api-tokens>.
2. Save it to a file that only you can read, for example `~/jira_token.txt`. Paste the token, press Enter, then Ctrl-D:
   ```bash
   (umask 077; cat > ~/jira_token.txt)
   ```
3. Set these environment variables when you run the script:

| Variable | Value |
|---|---|
| `JIRA_BASE_URL` | `https://cuhbioinformatics.atlassian.net` |
| `JIRA_EMAIL` | Your Atlassian account email |
| `JIRA_API_TOKEN` | The token, e.g. `$(cat ~/jira_token.txt)` |

Never commit a token. The `.gitignore` excludes any file with `token` in its name.

## Usage

```bash
JIRA_BASE_URL=https://cuhbioinformatics.atlassian.net \
JIRA_EMAIL=your.name@nhs.net \
JIRA_API_TOKEN=$(cat ~/jira_token.txt) \
.venv/bin/python helpdesk_kpi_report.py --period 2026-Q3 --out-dir kpi_report
```

Then open `kpi_report/helpdesk_kpi_report.html` in a browser. It's a single file, so it can be emailed or attached.

### Choosing the period

| `--period` | Tickets created in |
|---|---|
| `3m`, `6m`, `12m` | The last N months up to today. `12m` is the default. |
| `2026` | That calendar year |
| `2026-Q3` | That quarter |
| `2026-07` | That month |

For exact dates, use `--since YYYY-MM-DD` and/or `--until YYYY-MM-DD` instead. Both dates are included.

The open-ticket tables always show everything open at the moment you run the report, whatever the period.

### All options

| Option | Default | Meaning |
|---|---|---|
| `--project` | `EBH` | Jira project key |
| `--period` | `12m` | Report period (see above) |
| `--since` / `--until` | | Exact date range, instead of `--period` |
| `--exclude-types` | `Sequencing Run,Task,Sub-task` | Ticket types left out entirely |
| `--exclude-from-totals` | `Inform us` | Types shown per type but left out of the all-types start, resolve and waiting figures |
| `--waiting-statuses` | `Waiting for response,On hold` | Statuses that count as waiting on the requester |
| `--stale-days` | `14` | Age threshold for flagging open tickets |
| `--provisional-pct` | `10` | Mark months where more than this % of tickets are still open |
| `--oldest` | `20` | How many of the oldest open tickets to list |
| `--by-assignee` | off | Add per-person tables (see Privacy) |
| `--no-bank-holidays` | off | Don't fetch bank holidays, e.g. when offline |
| `--out-dir` | `.` | Where to write the outputs |

## Outputs

| File | Contents |
|---|---|
| `helpdesk_kpi_report.html` | Headline figures, charts, then tables by type, by month, open backlog, oldest open tickets and outcomes |
| `tickets.csv` | One row per ticket, with every computed duration and SLA result |
| `kpi_by_type.csv` | KPIs per ticket type, plus an all-types row |
| `kpi_by_month.csv` | KPIs per month created |
| `open_backlog.csv` | Open tickets by type and age band |
| `oldest_open.csv` | The longest-open tickets |
| `kpi_by_assignee.csv` | Per-person figures, only with `--by-assignee` |

## Privacy

- **No patient data.** Ticket summaries and descriptions can contain patient and sample identifiers, so the script never requests them from Jira. The outputs only contain ticket keys, types, statuses, dates and assignee names.
- **Per-person figures are opt-in.** They're only produced with `--by-assignee`. A run without the flag deletes any `kpi_by_assignee.csv` left in the output folder by an earlier run.
- **Don't commit reports.** Report outputs (`*.csv`, `*.html`, `kpi_report/`) are git-ignored.

## Tests

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest
```

The tests use made-up tickets and need no Jira access. GitHub Actions runs them on every push.
