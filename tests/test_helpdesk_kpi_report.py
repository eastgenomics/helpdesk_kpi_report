"""Tests for helpdesk_kpi_report.py. Run from the repo root with: pytest"""
from datetime import date, datetime, timezone

from collections import Counter

import pytest

from helpdesk_kpi_report import (LONDON, kpi_rows, months_back, oldest_open, parse_period, percentile, sla_outcome,
                                 summarise, ticket_metrics, write_html,
                                 working_seconds)

H = 3600


def london(*args):
    return datetime(*args, tzinfo=LONDON)


# ---------------------------------------------------------------- working_seconds

def test_same_day_inside_hours():
    assert working_seconds(london(2026, 11, 18, 10), london(2026, 11, 18, 12, 30)) == 2.5 * H


def test_same_day_clipped_to_hours():
    assert working_seconds(london(2026, 11, 18, 7), london(2026, 11, 18, 20)) == 8 * H


def test_overnight():
    assert working_seconds(london(2026, 11, 18, 16), london(2026, 11, 19, 10)) == 2 * H


def test_weekend_skipped():
    # Fri 16:00 -> Mon 10:00
    assert working_seconds(london(2026, 11, 20, 16), london(2026, 11, 23, 10)) == 2 * H


def test_raised_at_weekend():
    assert working_seconds(london(2026, 11, 21, 12), london(2026, 11, 22, 18)) == 0


def test_bank_holidays_skipped():
    # Thu 24 Dec 16:00 -> Tue 29 Dec 10:00 with Christmas and Boxing Day (substitute) off
    holidays = {date(2026, 12, 25), date(2026, 12, 28)}
    start, end = london(2026, 12, 24, 16), london(2026, 12, 29, 10)
    assert working_seconds(start, end, holidays) == 2 * H
    assert working_seconds(start, end) == 18 * H


def test_across_bst_change():
    # Clocks go forward on Sun 29 Mar 2026; working hours stay 09:00-17:00 local
    start, end = london(2026, 3, 27, 16), london(2026, 3, 30, 10)
    assert working_seconds(start, end) == 2 * H
    assert working_seconds(start.astimezone(timezone.utc), end.astimezone(timezone.utc)) == 2 * H


def test_reversed_or_missing():
    assert working_seconds(london(2026, 11, 18, 12), london(2026, 11, 18, 10)) == 0
    assert working_seconds(None, london(2026, 11, 18, 10)) == 0


def test_percentile():
    assert percentile([5], 90) == 5
    assert percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 50) == 5.5
    assert round(percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 90), 2) == 9.1


# ---------------------------------------------------------------- per-ticket metrics

CATEGORIES = {"10001": "new", "3": "indeterminate", "10100": "indeterminate", "10002": "done"}
WAITING = {"Waiting for response", "On hold"}
NOW = datetime(2026, 11, 18, 14, 0, tzinfo=timezone.utc)


def sla(elapsed_ms, breached=False, completed=True, goal_h=336):
    cycle = {"elapsedTime": {"millis": elapsed_ms}, "breached": breached, "goalDuration": {"millis": goal_h * 3600000}}
    return {"completedCycles": [cycle]} if completed else {"completedCycles": [], "ongoingCycle": cycle}


def issue(key="EBH-1", status=("Done", "done"), assignee="Joe Bloggs", created="2026-11-13T13:28:49.689+0000",
          resolved="2026-11-18T11:53:42.037+0000", ttfr=None, ttr=None):
    return {"id": "1", "key": key, "fields": {
        "issuetype": {"name": "Submit a request or incident"},
        "status": {"name": status[0], "statusCategory": {"key": status[1]}},
        "assignee": {"displayName": assignee} if assignee else None,
        "created": created, "resolutiondate": resolved,
        "customfield_10033": ttfr, "customfield_10032": ttr,
    }}


def change(ts, field, frm=None, to=None, from_string=None, to_string=None, hid=1):
    return {"id": str(hid), "created": ts,
            "items": [{"field": field, "from": frm, "to": to, "fromString": from_string, "toString": to_string}]}


# A typical ticket: assigned after ~1.6h, started after ~3.25h, resolved 5 days later
SAMPLE_HISTORY = [
    change("2026-11-13T15:03:20.662+0000", "assignee", to="acc", to_string="Joe Bloggs", hid=1),
    change("2026-11-13T16:43:54.039+0000", "status", "10001", "3", "New", "Development", hid=2),
    change("2026-11-18T11:53:42.037+0000", "status", "3", "10002", "Development", "Done", hid=3),
]


def test_sample_ticket_timeline():
    row = ticket_metrics(issue(ttfr=sla(11704350), ttr=sla(426292348)), SAMPLE_HISTORY,
                         CATEGORIES, WAITING, NOW, frozenset())
    assert row["assign_cal_h"] == 1.58 and row["assign_work_h"] == 1.58
    assert row["start_cal_h"] == 3.25
    assert row["resolve_cal_h"] == 118.41
    # Fri 13:28:49 -> Wed 11:53:42: 3.52h + 8h + 8h + 2.895h
    assert row["resolve_work_h"] == 22.41
    assert row["waiting_work_h"] == 0
    assert row["sla_first_response"] == "met" and row["sla_first_response_h"] == 3.25
    assert row["sla_resolution"] == "met" and row["sla_resolution_h"] == 118.41
    assert row["reopened"] == 0
    assert row["outcome"] == "Done" and row["resolved_month"] == "2026-11"


def test_bulk_changelog_epoch_millis():
    # The bulk changelog endpoint returns "created" as epoch millis rather than a string
    history = [dict(h, created=int(datetime.strptime(h["created"], "%Y-%m-%dT%H:%M:%S.%f%z").timestamp() * 1000))
               for h in reversed(SAMPLE_HISTORY)]
    row = ticket_metrics(issue(), history, CATEGORIES, WAITING, NOW, frozenset())
    assert row["assign_cal_h"] == 1.58 and row["start_cal_h"] == 3.25 and row["resolve_work_h"] == 22.41


def test_assigned_on_creation():
    history = [change("2026-11-14T10:00:00.000+0000", "assignee", frm="a", to="b",
                      from_string="Jane Doe", to_string="Joe Bloggs")]
    row = ticket_metrics(issue(), history, CATEGORIES, WAITING, NOW, frozenset())
    assert row["assign_cal_h"] == 0


def test_assigned_on_creation_no_history():
    row = ticket_metrics(issue(), [], CATEGORIES, WAITING, NOW, frozenset())
    assert row["assign_cal_h"] == 0


def test_never_assigned_open_ticket():
    row = ticket_metrics(issue(status=("New", "new"), assignee=None, resolved=None), [],
                         CATEGORIES, WAITING, NOW, frozenset())
    assert row["assign_cal_h"] is None
    assert row["resolve_cal_h"] is None and row["waiting_cal_h"] is None
    assert row["outcome"] == "" and row["age_days"] == 5.0


def test_waiting_time_and_reopen():
    history = [
        change("2026-11-16T10:00:00.000+0000", "status", "10001", "3", "New", "Development", hid=1),
        change("2026-11-16T12:00:00.000+0000", "status", "3", "10100", "Development", "Waiting for response", hid=2),
        change("2026-11-17T12:00:00.000+0000", "status", "10100", "10002", "Waiting for response", "Done", hid=3),
        change("2026-11-17T13:00:00.000+0000", "status", "10002", "3", "Done", "Development", hid=4),
        change("2026-11-18T10:00:00.000+0000", "status", "3", "10002", "Development", "Done", hid=5),
    ]
    row = ticket_metrics(issue(created="2026-11-16T09:00:00.000+0000", resolved="2026-11-18T10:00:00.000+0000"),
                         history, CATEGORIES, WAITING, NOW, frozenset())
    assert row["start_work_h"] == 1
    assert row["waiting_work_h"] == 8 and row["waiting_cal_h"] == 24
    assert row["reopened"] == 1


def test_waiting_status_does_not_count_as_start():
    history = [change("2026-11-16T10:00:00.000+0000", "status", "10001", "10100", "New", "On hold", hid=1),
               change("2026-11-16T15:00:00.000+0000", "status", "10100", "3", "On hold", "Development", hid=2)]
    row = ticket_metrics(issue(status=("Development", "indeterminate"), created="2026-11-16T09:00:00.000+0000",
                               resolved=None), history, CATEGORIES, WAITING, NOW, frozenset())
    assert row["start_work_h"] == 6


def test_sla_outcomes():
    assert sla_outcome(None) == (None, None, None)
    assert sla_outcome(sla(3600000)) == (3600, "met", 336 * H)
    assert sla_outcome(sla(3600000, breached=True, goal_h=72)) == (3600, "breached", 72 * H)
    assert sla_outcome(sla(3600000, completed=False)) == (3600, "pending", 336 * H)


def test_sla_targets_in_summary_and_table():
    a = ticket_metrics(issue(ttfr=sla(1, goal_h=72), ttr=sla(1, goal_h=72)), [], CATEGORIES, WAITING, NOW, frozenset())
    b = ticket_metrics(issue(ttfr=sla(1, goal_h=72), ttr=sla(1, breached=True)), [], CATEGORIES, WAITING, NOW, frozenset())
    assert a["sla_resolution_goal_h"] == 72 and b["sla_resolution_goal_h"] == 336
    s = summarise([a, b])
    assert s["sla_first_response_target"] == "72h"
    assert s["sla_resolution_target"] == "72h/336h"
    row = kpi_rows({"X": s})[0]
    assert "100.0% of 72h" in row and "50.0% of 72h/336h" in row


def test_provisional_month_flag():
    done = ticket_metrics(issue(), [], CATEGORIES, WAITING, NOW, frozenset())
    still_open = ticket_metrics(issue(status=("New", "new"), resolved=None), [], CATEGORIES, WAITING, NOW, frozenset())
    s = summarise([done] * 8 + [still_open] * 2)
    assert s["still_open"] == 2 and s["still_open_pct"] == 20
    assert kpi_rows({"2026-11": s}, provisional_pct=10)[0][0] == "2026-11 *"
    assert kpi_rows({"2026-11": s}, provisional_pct=25)[0][0] == "2026-11"
    assert kpi_rows({"2026-11": s})[0][0] == "2026-11"


def test_oldest_open():
    rows = [ticket_metrics(issue(key=f"EBH-{n}", status=("New", "new"), resolved=None, assignee=None if n == 2 else "Joe Bloggs",
                                 created=f"2026-11-0{n}T09:00:00.000+0000"), [], CATEGORIES, WAITING, NOW, frozenset())
            for n in (5, 2, 8)]
    oldest = oldest_open(rows, 2, include_assignee=False)
    assert [r["key"] for r in oldest] == ["EBH-2", "EBH-5"]
    assert set(oldest[0]) == {"key", "type", "status", "age_days", "created"}
    assert oldest_open(rows, 2, include_assignee=True)[0]["assignee"] == "(unassigned)"


def test_summarise_excludes_types_from_totals():
    a = ticket_metrics(issue(), SAMPLE_HISTORY, CATEGORIES, WAITING, NOW, frozenset())
    b = dict(a, type="Inform us", resolve_work_h=1000.0)
    s = summarise([a, b], {"Inform us"})
    assert s["created"] == 2 and s["resolved"] == 2
    assert s["resolve_work_median_h"] == 22.4
    assert summarise([a, b])["resolve_work_median_h"] == 511.2



# ---------------------------------------------------------------- period and layout

def test_months_back():
    assert months_back(date(2026, 11, 18), 3) == date(2026, 8, 18)
    assert months_back(date(2026, 2, 10), 3) == date(2025, 11, 10)
    assert months_back(date(2026, 5, 31), 3) == date(2026, 2, 28)
    assert months_back(date(2026, 11, 1), -2) == date(2027, 1, 1)


def test_parse_period():
    today = date(2026, 11, 18)
    assert parse_period("3m", today) == (date(2026, 8, 18), today)
    assert parse_period("12M", today) == (date(2025, 11, 18), today)
    assert parse_period("2026", today) == (date(2026, 1, 1), date(2026, 12, 31))
    assert parse_period("2026-Q3", today) == (date(2026, 7, 1), date(2026, 9, 30))
    assert parse_period("2025-q4", today) == (date(2025, 10, 1), date(2025, 12, 31))
    assert parse_period("2026-02", today) == (date(2026, 2, 1), date(2026, 2, 28))
    for bad in ("last quarter", "2026-Q5", "2026-13", "m"):
        with pytest.raises(ValueError):
            parse_period(bad, today)


def test_charts_come_before_tables(tmp_path):
    row = ticket_metrics(issue(ttr=sla(1)), SAMPLE_HISTORY, CATEGORIES, WAITING, NOW, frozenset())
    s = summarise([row])
    charts = {"volume": "AAAA", "sla_by_type": "AAAA"}
    path = write_html(tmp_path / "r.html", "meta", s, {"Submit a request or incident": s}, {"2026-11": s}, [], [],
                      14, Counter(), None, charts, 10, [])
    page = path.read_text()
    assert page.index("<h2>Charts</h2>") < page.index("<h2>By ticket type</h2>") < page.index("<h2>By month created</h2>")
    assert page.count("<figure>") == 2
