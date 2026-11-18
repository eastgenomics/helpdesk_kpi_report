#!/usr/bin/env python3
"""Helpdesk KPI report for Jira Service Management tickets (DI-379).

Covers every ticket type except those excluded (Sequencing Run has its own
KPI tracking). For each ticket it works out time to assign, time to start
work, time to resolve, time spent waiting on the requester, reopens and the
JSM SLA outcomes, then writes CSVs and a self-contained HTML report.

Durations are given in working hours (Mon-Fri 09:00-17:00 Europe/London,
excluding England and Wales bank holidays) and in calendar hours.

Only keys, types, statuses, dates and assignee names are requested from Jira:
summaries and descriptions can contain patient identifiers and are never fetched.

Auth via environment variables (see jira_client.py):
    JIRA_BASE_URL   e.g. https://yourorg.atlassian.net
    JIRA_EMAIL      your Atlassian account email
    JIRA_API_TOKEN  API token from id.atlassian.com
"""
import argparse
import base64
import csv
import html
import io
import os
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta, timezone
from statistics import median
from zoneinfo import ZoneInfo

from jira_client import fetch_changelog_single, fetch_changelogs_bulk, make_session, request

LONDON = ZoneInfo("Europe/London")
DAY_START = time(9, 0)
DAY_END = time(17, 0)
BANK_HOLIDAYS_URL = "https://www.gov.uk/bank-holidays.json"

SLA_RESOLUTION_FIELD = "customfield_10032"
SLA_FIRST_RESPONSE_FIELD = "customfield_10033"
SEARCH_FIELDS = ["issuetype", "status", "assignee", "created", "resolutiondate", "resolution",
                 SLA_RESOLUTION_FIELD, SLA_FIRST_RESPONSE_FIELD]

# Duration metrics: (column prefix, label shown in the report)
DURATIONS = [
    ("assign", "Time to assign"),
    ("start", "Time to start work"),
    ("resolve", "Time to resolve"),
    ("waiting", "Time waiting on requester"),
]
# Metrics left out of the all-types totals for --exclude-from-totals types
TOTALS_EXCLUDED_METRICS = {"start", "resolve", "waiting"}


# ---------------------------------------------------------------- time helpers

def parse_ts(value):
    """Parse a Jira timestamp such as 2026-11-13T13:28:49.689+0000, or epoch millis (bulk changelog)."""
    if not value:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, timezone.utc)
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            pass
    return datetime.fromisoformat(value)


def load_bank_holidays(session):
    """Return the set of England and Wales bank holiday dates from gov.uk."""
    resp = session.get(BANK_HOLIDAYS_URL, timeout=30)
    resp.raise_for_status()
    events = resp.json()["england-and-wales"]["events"]
    return {date.fromisoformat(e["date"]) for e in events}


def working_seconds(start, end, holidays=frozenset()):
    """Seconds between start and end that fall within Mon-Fri 09:00-17:00 London time."""
    if start is None or end is None or end <= start:
        return 0.0
    start, end = start.astimezone(LONDON), end.astimezone(LONDON)
    total = 0.0
    day = start.date()
    while day <= end.date():
        if day.weekday() < 5 and day not in holidays:
            window_start = datetime.combine(day, DAY_START, LONDON)
            window_end = datetime.combine(day, DAY_END, LONDON)
            lo, hi = max(start, window_start), min(end, window_end)
            if hi > lo:
                total += (hi - lo).total_seconds()
        day += timedelta(days=1)
    return total


def calendar_seconds(start, end):
    if start is None or end is None or end <= start:
        return 0.0
    return (end - start).total_seconds()


def hours(seconds):
    return None if seconds is None else round(seconds / 3600, 2)


def percentile(values, pct):
    """Linear-interpolated percentile of a non-empty list."""
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * pct / 100
    lo = int(pos)
    hi = min(lo + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


# ---------------------------------------------------------------- Jira access

def build_jql(project, exclude_types, extra):
    clauses = [f'project = "{project}"']
    if exclude_types:
        quoted = ", ".join(f'"{t}"' for t in exclude_types)
        clauses.append(f"issuetype not in ({quoted})")
    clauses.extend(extra)
    return " AND ".join(clauses) + " ORDER BY key"


def search_issues(session, base, jql):
    """Return the raw issue dicts matching the JQL, with SEARCH_FIELDS only."""
    issues = []
    params = {"jql": jql, "fields": ",".join(SEARCH_FIELDS), "maxResults": 100}
    while True:
        resp = request(session, "GET", f"{base}/rest/api/3/search/jql", params=params)
        resp.raise_for_status()
        data = resp.json()
        issues.extend(data.get("issues", []))
        token = data.get("nextPageToken")
        if not token or data.get("isLast", False):
            return issues
        params["nextPageToken"] = token


def fetch_status_categories(session, base):
    """Return {status_id: category_key} where key is 'new', 'indeterminate' or 'done'."""
    resp = request(session, "GET", f"{base}/rest/api/3/status")
    resp.raise_for_status()
    return {str(s["id"]): s["statusCategory"]["key"] for s in resp.json()}


def fetch_histories(session, base, issues):
    """Return {issue_id: [history, ...]} for status, assignee and resolution changes."""
    ids = [issue["id"] for issue in issues]
    bulk = fetch_changelogs_bulk(session, base, ids, field_ids=("status", "assignee", "resolution"))
    if bulk is not None:
        return {issue_id: bulk.get(issue_id, []) for issue_id in ids}
    return {issue["id"]: fetch_changelog_single(session, base, issue["key"]) for issue in issues}


# ---------------------------------------------------------------- per-ticket metrics

def sla_outcome(value):
    """Return (elapsed_seconds, outcome, goal_seconds) from a JSM SLA field; outcome is met/breached/pending."""
    if not value:
        return None, None, None
    cycles = value.get("completedCycles") or []
    if cycles:
        cycle = cycles[-1]
        outcome = "breached" if cycle.get("breached") else "met"
    elif value.get("ongoingCycle"):
        cycle = value["ongoingCycle"]
        outcome = "breached" if cycle.get("breached") else "pending"
    else:
        return None, None, None
    elapsed = (cycle.get("elapsedTime") or {}).get("millis")
    goal = (cycle.get("goalDuration") or {}).get("millis")
    return (elapsed / 1000 if elapsed is not None else None), outcome, (goal / 1000 if goal is not None else None)


def field_changes(histories, field):
    """Return [(timestamp, item), ...] for one field, oldest first."""
    changes = []
    for history in histories:
        for item in history.get("items", []):
            if item.get("field") == field or item.get("fieldId") == field:
                changes.append((parse_ts(history["created"]), int(history.get("id", 0)), item))
    return [(at, item) for at, _, item in sorted(changes, key=lambda c: (c[0], c[1]))]


def ticket_metrics(issue, histories, categories, waiting_statuses, now, holidays):
    """Work out every per-ticket figure the report needs."""
    fields = issue["fields"]
    created = parse_ts(fields["created"])
    status = fields["status"]
    category = status["statusCategory"]["key"]
    assignee = (fields.get("assignee") or {}).get("displayName")

    # Time to assign: an assignee change with a "from" value means it was assigned on creation.
    assign_changes = field_changes(histories, "assignee")
    if assign_changes:
        first_at, first_item = assign_changes[0]
        assigned_at = created if first_item.get("from") else first_at
    else:
        assigned_at = created if assignee else None

    status_changes = field_changes(histories, "status")
    started_at = None
    reopened = 0
    for at, item in status_changes:
        to_cat = categories.get(str(item.get("to")))
        from_cat = categories.get(str(item.get("from")))
        if started_at is None and to_cat == "indeterminate" and item.get("toString") not in waiting_statuses:
            started_at = at
        if from_cat == "done" and to_cat != "done":
            reopened += 1

    resolved_at = parse_ts(fields.get("resolutiondate"))
    if resolved_at is None and category == "done":
        done_entries = [at for at, item in status_changes if categories.get(str(item.get("to"))) == "done"]
        resolved_at = done_entries[-1] if done_entries else None
    if category != "done":
        resolved_at = None

    # Time in waiting statuses, walking each interval between status changes.
    initial = status_changes[0][1].get("fromString") if status_changes else status["name"]
    segments = [(created, initial)] + [(at, item.get("toString")) for at, item in status_changes]
    window_end = resolved_at or now
    waiting_work = waiting_cal = 0.0
    for (seg_start, name), nxt in zip(segments, segments[1:] + [(window_end, None)]):
        if name in waiting_statuses:
            seg_end = min(nxt[0], window_end)
            waiting_work += working_seconds(seg_start, seg_end, holidays)
            waiting_cal += calendar_seconds(seg_start, seg_end)

    frt_elapsed, frt_outcome, frt_goal = sla_outcome(fields.get(SLA_FIRST_RESPONSE_FIELD))
    ttr_elapsed, ttr_outcome, ttr_goal = sla_outcome(fields.get(SLA_RESOLUTION_FIELD))

    def both(end):
        if end is None:
            return None, None
        return hours(working_seconds(created, end, holidays)), hours(calendar_seconds(created, end))

    row = {
        "key": issue["key"],
        "type": fields["issuetype"]["name"],
        "status": status["name"],
        "status_category": category,
        "outcome": status["name"] if category == "done" else "",
        "assignee": assignee or "",
        "created": created.astimezone(LONDON).isoformat(timespec="minutes"),
        "created_month": created.astimezone(LONDON).strftime("%Y-%m"),
        "resolved": resolved_at.astimezone(LONDON).isoformat(timespec="minutes") if resolved_at else "",
        "resolved_month": resolved_at.astimezone(LONDON).strftime("%Y-%m") if resolved_at else "",
        "age_days": round(calendar_seconds(created, now) / 86400, 1) if category != "done" else None,
        "reopened": reopened,
        "sla_first_response_h": hours(frt_elapsed),
        "sla_first_response": frt_outcome or "",
        "sla_first_response_goal_h": hours(frt_goal),
        "sla_resolution_h": hours(ttr_elapsed),
        "sla_resolution": ttr_outcome or "",
        "sla_resolution_goal_h": hours(ttr_goal),
    }
    row["assign_work_h"], row["assign_cal_h"] = both(assigned_at)
    row["start_work_h"], row["start_cal_h"] = both(started_at)
    row["resolve_work_h"], row["resolve_cal_h"] = both(resolved_at)
    row["waiting_work_h"], row["waiting_cal_h"] = hours(waiting_work), hours(waiting_cal)
    if resolved_at is None:
        row["waiting_work_h"] = row["waiting_cal_h"] = None
    return row


# ---------------------------------------------------------------- aggregation

def sla_counted(rows, column):
    """Tickets whose SLA has finished or breached, the denominator of SLA met %."""
    return sum(r[column] in ("met", "breached") for r in rows)


def sla_pct(rows, column):
    met = sum(r[column] == "met" for r in rows)
    counted = sla_counted(rows, column)
    return round(100 * met / counted, 1) if counted else None


def sla_targets(rows, column):
    """The SLA goal(s) seen in a group, e.g. "72h" or "72h/336h" when types differ."""
    goals = sorted({r[column] for r in rows if r[column] is not None})
    return "/".join(f"{g:g}h" for g in goals)


def summarise(rows, exclude_types=()):
    """Return one dict of KPIs for a group of tickets."""
    resolved = [r for r in rows if r["resolved"]]
    out = {
        "created": len(rows),
        "resolved": len(resolved),
        "still_open": len(rows) - len(resolved),
        "still_open_pct": round(100 * (len(rows) - len(resolved)) / len(rows), 1) if rows else None,
        "never_assigned": sum(r["assign_work_h"] is None for r in rows),
        "reopen_pct": round(100 * sum(r["reopened"] > 0 for r in resolved) / len(resolved), 1) if resolved else None,
        "sla_first_response_met_pct": sla_pct(rows, "sla_first_response"),
        "sla_resolution_met_pct": sla_pct(rows, "sla_resolution"),
        "sla_resolution_n": sla_counted(rows, "sla_resolution"),
        "sla_first_response_target": sla_targets(rows, "sla_first_response_goal_h"),
        "sla_resolution_target": sla_targets(rows, "sla_resolution_goal_h"),
    }
    for prefix, _ in DURATIONS:
        pool = rows
        if prefix in TOTALS_EXCLUDED_METRICS and exclude_types:
            pool = [r for r in rows if r["type"] not in exclude_types]
        for clock in ("work", "cal"):
            values = [r[f"{prefix}_{clock}_h"] for r in pool if r[f"{prefix}_{clock}_h"] is not None]
            out[f"{prefix}_{clock}_median_h"] = round(median(values), 1) if values else None
            out[f"{prefix}_{clock}_p90_h"] = round(percentile(values, 90), 1) if values else None
            out[f"{prefix}_n"] = len(values)
    return out


def group_by(rows, key):
    groups = defaultdict(list)
    for r in rows:
        if r[key]:
            groups[r[key]].append(r)
    return dict(sorted(groups.items()))


def assignee_summary(rows, open_rows):
    open_counts = Counter(r["assignee"] for r in open_rows)
    out = []
    for name, group in group_by(rows, "assignee").items():
        resolve = [r["resolve_work_h"] for r in group if r["resolve_work_h"] is not None]
        out.append({
            "assignee": name,
            "assigned": len(group),
            "resolved": sum(bool(r["resolved"]) for r in group),
            "resolve_work_median_h": round(median(resolve), 1) if resolve else None,
            "open_now": open_counts.get(name, 0),
        })
    return sorted(out, key=lambda d: -d["assigned"])


def oldest_open(open_rows, limit, include_assignee):
    """The longest-open tickets: key, type, status and age only (never summaries)."""
    out = []
    for r in sorted(open_rows, key=lambda r: -r["age_days"])[:limit]:
        row = {"key": r["key"], "type": r["type"], "status": r["status"], "age_days": r["age_days"],
               "created": r["created"][:10]}
        if include_assignee:
            row["assignee"] = r["assignee"] or "(unassigned)"
        out.append(row)
    return out


def backlog_summary(open_rows, stale_days):
    bands = [(0, 2, "0-2 d"), (2, 7, "2-7 d"), (7, 14, "7-14 d"), (14, 30, "14-30 d"), (30, 90, "30-90 d"),
             (90, float("inf"), "90+ d")]
    out = []
    for ticket_type, group in group_by(open_rows, "type").items():
        row = {"type": ticket_type, "open": len(group),
               "unassigned": sum(not r["assignee"] for r in group),
               f"older_than_{stale_days}d": sum(r["age_days"] > stale_days for r in group)}
        for lo, hi, label in bands:
            row[label] = sum(lo <= r["age_days"] < hi for r in group)
        out.append(row)
    return out, [label for _, _, label in bands]


# ---------------------------------------------------------------- outputs

def write_csv(path, rows, columns=None):
    columns = columns or (list(rows[0].keys()) if rows else [])
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return path


def make_charts(rows, by_type, by_month, backlog, bands):
    """Return {name: base64 PNG}, or {} if matplotlib isn't installed."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib not installed: the report will be written without charts.", file=sys.stderr)
        return {}

    blue, orange, grey = "#2a6fdb", "#e8833a", "#8a8f98"

    def encode(fig):
        buf = io.BytesIO()
        fig.tight_layout()
        fig.savefig(buf, format="png", dpi=130)
        plt.close(fig)
        return base64.b64encode(buf.getvalue()).decode()

    def style(ax):
        ax.spines[["top", "right"]].set_visible(False)
        ax.grid(axis="y" if ax.name == "rectilinear" else "x", alpha=0.25)

    charts = {}
    months = list(by_month)
    resolved_by_month = Counter(r["resolved_month"] for r in rows if r["resolved_month"])

    fig, ax = plt.subplots(figsize=(8, 3.4))
    x = range(len(months))
    ax.bar([i - 0.2 for i in x], [by_month[m]["created"] for m in months], 0.4, label="Created", color=blue)
    ax.bar([i + 0.2 for i in x], [resolved_by_month.get(m, 0) for m in months], 0.4, label="Resolved", color=orange)
    ax.set_xticks(list(x), months, rotation=45, ha="right", fontsize=8)
    ax.set_ylabel("Tickets")
    ax.legend(frameon=False)
    style(ax)
    charts["volume"] = encode(fig)

    types = [t for t in by_type if by_type[t]["resolve_work_median_h"] is not None]
    if types:
        types.sort(key=lambda t: by_type[t]["resolve_work_median_h"])
        fig, ax = plt.subplots(figsize=(8, 0.4 * len(types) + 1.2))
        bars = ax.barh(types, [by_type[t]["resolve_work_median_h"] for t in types], color=blue)
        ax.bar_label(bars, fmt="%.1f h", padding=3, fontsize=8)
        ax.set_xlabel("Median working hours to resolve")
        ax.margins(x=0.12)
        ax.spines[["top", "right"]].set_visible(False)
        charts["resolve_by_type"] = encode(fig)

    types = [t for t in by_type if by_type[t]["sla_resolution_met_pct"] is not None]
    if types:
        types.sort(key=lambda t: by_type[t]["sla_resolution_met_pct"])
        fig, ax = plt.subplots(figsize=(8, 0.4 * len(types) + 1.2))
        bars = ax.barh(types, [by_type[t]["sla_resolution_met_pct"] for t in types], color=blue)
        labels = [f'{by_type[t]["sla_resolution_met_pct"]:.0f}%  ({by_type[t]["sla_resolution_target"]} target, '
                  f'n={by_type[t]["sla_resolution_n"]})' for t in types]
        ax.bar_label(bars, labels=labels, padding=3, fontsize=8)
        ax.set_xlim(0, 135)
        ax.set_xticks(range(0, 101, 20))
        ax.set_xlabel("% resolved within SLA")
        ax.spines[["top", "right"]].set_visible(False)
        charts["sla_by_type"] = encode(fig)

    fig, ax = plt.subplots(figsize=(8, 3.2))
    for column, label, colour in (("sla_first_response_met_pct", "First response", blue),
                                  ("sla_resolution_met_pct", "Resolution", orange)):
        points = [(i, by_month[m][column]) for i, m in enumerate(months) if by_month[m][column] is not None]
        if points:
            ax.plot([p[0] for p in points], [p[1] for p in points], marker="o", label=label, color=colour)
    ax.set_xticks(range(len(months)), months, rotation=45, ha="right", fontsize=8)
    ax.set_ylim(0, 105)
    ax.set_ylabel("% within SLA")
    if ax.lines:
        ax.legend(frameon=False)
    style(ax)
    charts["sla"] = encode(fig)

    if backlog:
        fig, ax = plt.subplots(figsize=(8, 3))
        totals = [sum(row[b] for row in backlog) for b in bands]
        ax.bar(bands, totals, color=[grey] * 3 + [orange] * 3)
        ax.set_ylabel("Open tickets")
        ax.set_xlabel("Age")
        style(ax)
        charts["backlog"] = encode(fig)
    return charts


def fmt(value, suffix=""):
    if value is None or value == "":
        return "–"
    return f"{value}{suffix}"


def html_table(headers, rows):
    head = "".join(f"<th>{html.escape(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{html.escape(str(c))}</td>" for c in row) + "</tr>" for row in rows)
    return f'<div class="scroll"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def sla_cell(pct, target):
    if pct is None:
        return "–"
    return f"{pct}% of {target}" if target else f"{pct}%"


def kpi_rows(groups, provisional_pct=None):
    """Table rows of the main duration KPIs for {label: summary}; flags provisional groups with *."""
    out = []
    for label, s in groups.items():
        if provisional_pct is not None and (s["still_open_pct"] or 0) > provisional_pct:
            label = f"{label} *"
        out.append([
            label, s["created"], s["resolved"], s["still_open"],
            fmt(s["assign_work_median_h"]), fmt(s["assign_cal_median_h"]),
            fmt(s["start_work_median_h"]),
            fmt(s["resolve_work_median_h"]), fmt(s["resolve_work_p90_h"]), fmt(s["resolve_cal_median_h"]),
            fmt(s["waiting_work_median_h"]),
            sla_cell(s["sla_first_response_met_pct"], s["sla_first_response_target"]),
            sla_cell(s["sla_resolution_met_pct"], s["sla_resolution_target"]),
            fmt(s["reopen_pct"], "%"),
        ])
    return out


KPI_HEADERS = ["", "Created", "Resolved", "Still open", "Assign (work h)", "Assign (cal h)", "Start (work h)",
               "Resolve (work h)", "Resolve p90 (work h)", "Resolve (cal h)", "Waiting (work h)",
               "1st response SLA met", "Resolution SLA met", "Reopened"]

CSS = """
:root{--bg:#fbfbfa;--fg:#1d1f23;--muted:#5d636d;--line:#e2e4e8;--card:#fff;--accent:#2a6fdb}
@media (prefers-color-scheme:dark){:root{--bg:#16181c;--fg:#e8eaed;--muted:#a1a7b0;--line:#2e3238;--card:#1f2227;--accent:#7aa7ff}
img{background:#fff;border-radius:6px}}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1100px;margin:0 auto;padding:24px 16px 64px}
h1{font-size:1.6rem;margin:0 0 4px}h2{font-size:1.15rem;margin:36px 0 8px}
.muted{color:var(--muted)}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin:20px 0}
.tile{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px 14px}
.tile b{display:block;font-size:1.5rem;font-variant-numeric:tabular-nums}
.tile span{color:var(--muted);font-size:.85rem}
.scroll{overflow-x:auto}
table{border-collapse:collapse;width:100%;background:var(--card);font-size:.85rem;font-variant-numeric:tabular-nums}
th,td{border-bottom:1px solid var(--line);padding:6px 8px;text-align:right;white-space:nowrap}
th:first-child,td:first-child{text-align:left}
th{color:var(--muted);font-weight:600}
img{max-width:100%;height:auto}
.charts{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,480px),1fr));gap:16px}
figure{margin:0;background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px}
h3{font-size:.95rem;margin:0 0 8px}
ul{padding-left:20px}
"""


def write_html(path, meta, overall, by_type, by_month, backlog, bands, stale_days, outcomes, assignees, charts,
               provisional_pct, oldest):
    tiles = [
        (overall["created"], "tickets created"),
        (fmt(overall["assign_work_median_h"], " h"), "median time to assign (working)"),
        (fmt(overall["sla_first_response_met_pct"], "%"), "first responses within SLA"),
        (fmt(overall["resolve_work_median_h"], " h"), "median time to resolve (working)"),
        (fmt(overall["sla_resolution_met_pct"], "%"), "resolved within SLA"),
        (sum(r["open"] for r in backlog), "open now"),
    ]
    parts = [f"<h1>Helpdesk KPI report</h1><p class='muted'>{html.escape(meta)}</p>",
             '<div class="tiles">' + "".join(f'<div class="tile"><b>{html.escape(str(v))}</b><span>{l}</span></div>'
                                              for v, l in tiles) + "</div>"]

    chart_titles = [
        ("volume", "Tickets created and resolved per month"),
        ("sla", "% within SLA per month"),
        ("sla_by_type", "% resolved within SLA, by ticket type"),
        ("resolve_by_type", "Median working hours to resolve, by ticket type"),
        ("backlog", "Open tickets now, by age"),
    ]
    figures = [f'<figure><h3>{title}</h3><img alt="{title}" src="data:image/png;base64,{charts[name]}"></figure>'
               for name, title in chart_titles if name in charts]
    if figures:
        parts.append('<h2>Charts</h2><div class="charts">' + "".join(figures) + "</div>")
        if any(by_month[m]["still_open_pct"] and by_month[m]["still_open_pct"] > provisional_pct for m in by_month):
            parts.append(f"<p class='muted'>Recent months marked * in the month table are provisional, "
                         "so their points on the monthly SLA chart will change.</p>")

    parts.append("<h2>By ticket type</h2>")
    parts.append(html_table(KPI_HEADERS, kpi_rows({"All types": overall, **by_type})))

    parts.append("<h2>By month created</h2>")
    parts.append(html_table(KPI_HEADERS, kpi_rows(by_month, provisional_pct)))
    parts.append(f"<p class='muted'>* Provisional: more than {provisional_pct:g}% of this month's tickets are still "
                 "open, so its times and SLA figures only reflect the tickets resolved so far and will look "
                 "better than the final figures.</p>")

    parts.append("<h2>Open tickets now</h2><p class='muted'>All currently open tickets, whatever the report period.</p>")
    stale_col = f"older_than_{stale_days}d"
    parts.append(html_table(["Type", "Open", "Unassigned", f"Older than {stale_days} d"] + bands,
                            [[r["type"], r["open"], r["unassigned"], r[stale_col]] + [r[b] for b in bands]
                             for r in backlog]))
    if oldest:
        parts.append(f"<h2>Oldest open tickets</h2><p class='muted'>The {len(oldest)} longest-open tickets.</p>")
        headers = ["Ticket", "Type", "Status", "Age (days)", "Created"] + (["Assignee"] if "assignee" in oldest[0] else [])
        parts.append(html_table(headers, [list(r.values()) for r in oldest]))

    parts.append("<h2>Outcomes of resolved tickets</h2>")
    parts.append(html_table(["Final status", "Tickets"], outcomes.most_common()))

    if assignees is not None:
        parts.append("<h2>By assignee</h2><p class='muted'>Current assignee of each ticket created in the period.</p>")
        parts.append(html_table(["Assignee", "Assigned", "Resolved", "Median resolve (work h)", "Open now"],
                                [[a["assignee"], a["assigned"], a["resolved"], fmt(a["resolve_work_median_h"]),
                                  a["open_now"]] for a in assignees]))

    parts.append("""<h2>How these are measured</h2><ul class="muted">
<li><b>Working hours</b>: Mon–Fri 09:00–17:00 UK time, excluding England and Wales bank holidays. <b>Calendar hours</b>: raw elapsed time.</li>
<li>Durations are medians (p90 where shown), measured from ticket creation.</li>
<li><b>Assign</b>: until an assignee was first set (0 if assigned on creation). <b>Start</b>: until the first move into an in-progress status, excluding waiting statuses.
<b>Resolve</b>: until the resolution date. <b>Waiting</b>: time in waiting statuses, resolved tickets only.</li>
<li><b>SLA met %</b>: from the Jira Service Management SLA timers, shown with the SLA target (calendar hours, 24/7); counts only tickets whose SLA has finished or breached. Targets differ by ticket type, so a group can show more than one.</li>
<li><b>Reopened</b>: resolved tickets that moved from a done status back to an open one at least once.</li>
<li>Ticket types excluded from the all-types start/resolve/waiting figures are listed at the top of the report.</li>
</ul>""")

    doc = (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
           f'<meta name="viewport" content="width=device-width,initial-scale=1"><title>Helpdesk KPI report</title>'
           f"<style>{CSS}</style></head><body><main>{''.join(parts)}</main></body></html>")
    with open(path, "w") as f:
        f.write(doc)
    return path



# ---------------------------------------------------------------- main

def months_back(day, n):
    """The same day n months earlier, clamped to the end of shorter months."""
    year, month = divmod(day.year * 12 + day.month - 1 - n, 12)
    month += 1
    next_month = date(year + month // 12, month % 12 + 1, 1)
    return date(year, month, min(day.day, (next_month - timedelta(days=1)).day))


def parse_period(value, today):
    """Turn a --period value into (since, until) dates, both inclusive.

    Accepts: 3m / 12m (the last N months up to today), 2026 (a calendar year),
    2026-Q3 (a quarter) or 2026-07 (a month).
    """
    original, value = value, value.strip().upper()
    if value.endswith("M") and value[:-1].isdigit():
        return months_back(today, int(value[:-1])), today
    if value.isdigit() and len(value) == 4:
        return date(int(value), 1, 1), date(int(value), 12, 31)
    year, _, part = value.partition("-")
    if year.isdigit() and part.startswith("Q") and part[1:] in ("1", "2", "3", "4"):
        first = 3 * int(part[1:]) - 2
        start = date(int(year), first, 1)
        return start, months_back(start, -3) - timedelta(days=1)
    if year.isdigit() and part.isdigit() and 1 <= int(part) <= 12:
        start = date(int(year), int(part), 1)
        return start, months_back(start, -1) - timedelta(days=1)
    raise ValueError(f"Unrecognised period {original!r}: use e.g. 3m, 12m, 2026, 2026-Q3 or 2026-07")


def split_list(value):
    return [v.strip() for v in value.split(",") if v.strip()]


def main():
    today = datetime.now(LONDON).date()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--project", default="EBH")
    parser.add_argument("--period", help="Tickets created in this period: 3m or 12m (last N months), "
                                         "2026 (a year), 2026-Q3 (a quarter) or 2026-07 (a month). Default: 12m")
    parser.add_argument("--since", help="Only tickets created on/after YYYY-MM-DD (instead of --period)")
    parser.add_argument("--until", help="Only tickets created on/before YYYY-MM-DD (instead of --period)")
    parser.add_argument("--exclude-types", default="Sequencing Run,Task,Sub-task",
                        help="Comma-separated ticket types to leave out entirely")
    parser.add_argument("--exclude-from-totals", default="Inform us",
                        help="Types reported separately but left out of all-types start/resolve/waiting figures")
    parser.add_argument("--waiting-statuses", default="Waiting for response,On hold",
                        help="Statuses that count as waiting on the requester")
    parser.add_argument("--stale-days", type=int, default=14, help="Age threshold for open tickets")
    parser.add_argument("--provisional-pct", type=float, default=10,
                        help="Flag months where more than this %% of tickets are still open")
    parser.add_argument("--oldest", type=int, default=20, help="How many of the oldest open tickets to list")
    parser.add_argument("--by-assignee", action="store_true", help="Add per-assignee tables")
    parser.add_argument("--no-bank-holidays", action="store_true", help="Don't fetch bank holidays from gov.uk")
    parser.add_argument("--out-dir", default=".")
    args = parser.parse_args()
    if args.period and (args.since or args.until):
        parser.error("use either --period or --since/--until, not both")
    try:
        since, until = parse_period(args.period or "12m", today)
    except ValueError as exc:
        parser.error(str(exc))
    if args.since or args.until:
        since = date.fromisoformat(args.since) if args.since else months_back(today, 12)
        until = date.fromisoformat(args.until) if args.until else today

    exclude_types = split_list(args.exclude_types)
    totals_excluded = set(split_list(args.exclude_from_totals))
    waiting = set(split_list(args.waiting_statuses))

    session, base = make_session()
    holidays = frozenset() if args.no_bank_holidays else load_bank_holidays(session)
    categories = fetch_status_categories(session, base)
    now = datetime.now(timezone.utc)

    # created < the day after `until`, because Jira reads a bare date as midnight at the start of that day
    period = [f'created >= "{since}"', f'created < "{until + timedelta(days=1)}"']
    period_jql = build_jql(args.project, exclude_types, period)
    open_jql = build_jql(args.project, exclude_types, ["statusCategory != Done"])
    print(f"JQL: {period_jql}")

    issues = search_issues(session, base, period_jql)
    open_issues = search_issues(session, base, open_jql)
    print(f"Found {len(issues)} tickets in the period and {len(open_issues)} open now; fetching history...")

    histories = fetch_histories(session, base, issues)
    rows = [ticket_metrics(i, histories[i["id"]], categories, waiting, now, holidays) for i in issues]
    open_rows = [ticket_metrics(i, [], categories, waiting, now, holidays) for i in open_issues]

    outcomes = Counter(r["outcome"] for r in rows if r["outcome"])
    overall = summarise(rows, totals_excluded)
    by_type = {t: summarise(g) for t, g in group_by(rows, "type").items()}
    by_month = {m: summarise(g, totals_excluded) for m, g in group_by(rows, "created_month").items()}
    backlog, bands = backlog_summary(open_rows, args.stale_days)
    assignees = assignee_summary(rows, open_rows) if args.by_assignee else None
    oldest = oldest_open(open_rows, args.oldest, args.by_assignee)

    os.makedirs(args.out_dir, exist_ok=True)
    out = lambda name: os.path.join(args.out_dir, name)
    written = [
        write_csv(out("tickets.csv"), rows),
        write_csv(out("kpi_by_type.csv"), [{"type": t, **s} for t, s in {"All types": overall, **by_type}.items()]),
        write_csv(out("kpi_by_month.csv"), [{"month": m, **s} for m, s in by_month.items()]),
        write_csv(out("open_backlog.csv"), backlog),
        write_csv(out("oldest_open.csv"), oldest),
    ]
    if assignees is not None:
        written.append(write_csv(out("kpi_by_assignee.csv"), assignees))
    elif os.path.exists(out("kpi_by_assignee.csv")):
        # Don't leave per-person figures from an earlier --by-assignee run next to this report
        os.remove(out("kpi_by_assignee.csv"))
        print(f"Removed old {out('kpi_by_assignee.csv')} (run with --by-assignee to regenerate it)")

    span = f"created {since:%d %b %Y} to {until:%d %b %Y}"
    meta = (f"{args.project} tickets {span}, generated {now.astimezone(LONDON):%d %b %Y %H:%M}. "
            f"Excluded types: {', '.join(exclude_types) or 'none'}. "
            f"Left out of all-types start/resolve/waiting figures: {', '.join(sorted(totals_excluded)) or 'none'}.")
    charts = make_charts(rows, by_type, by_month, backlog, bands)
    written.append(write_html(out("helpdesk_kpi_report.html"), meta, overall, by_type, by_month,
                              backlog, bands, args.stale_days, outcomes, assignees, charts,
                              args.provisional_pct, oldest))
    print("Wrote:\n  " + "\n  ".join(written))


if __name__ == "__main__":
    main()
