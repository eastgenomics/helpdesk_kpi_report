"""Minimal Jira Cloud REST client: authenticated session, rate-limit back-off and changelog retrieval.

Auth via environment variables:
    JIRA_BASE_URL   e.g. https://yourorg.atlassian.net
    JIRA_EMAIL      your Atlassian account email
    JIRA_API_TOKEN  API token from id.atlassian.com
"""
import os
import sys
import time
from collections import defaultdict

import requests


def make_session():
    missing = [v for v in ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN") if not os.environ.get(v)]
    if missing:
        sys.exit(f"Missing environment variables: {', '.join(missing)}")
    session = requests.Session()
    session.auth = (os.environ["JIRA_EMAIL"], os.environ["JIRA_API_TOKEN"])
    session.headers["Accept"] = "application/json"
    return session, os.environ["JIRA_BASE_URL"].rstrip("/")


def request(session, method, url, **kwargs):
    """Send a request, backing off on rate limits."""
    for attempt in range(6):
        resp = session.request(method, url, timeout=60, **kwargs)
        if resp.status_code == 429 or resp.status_code >= 500:
            wait = float(resp.headers.get("Retry-After", 2 ** attempt))
            time.sleep(wait)
            continue
        return resp
    resp.raise_for_status()
    return resp


def fetch_changelogs_bulk(session, base, issue_ids, field_ids=("status",)):
    """Return {issue_id: [history, ...]} via the bulk changelog endpoint, or None if unavailable."""
    histories = defaultdict(list)
    for i in range(0, len(issue_ids), 1000):
        body = {"issueIdsOrKeys": issue_ids[i:i + 1000], "fieldIds": list(field_ids), "maxResults": 1000}
        while True:
            resp = request(session, "POST", f"{base}/rest/api/3/changelog/bulkfetch", json=body)
            if resp.status_code in (404, 405):
                return None
            resp.raise_for_status()
            data = resp.json()
            for log in data.get("issueChangeLogs", []):
                histories[str(log["issueId"])].extend(log.get("changeHistories", []))
            token = data.get("nextPageToken")
            if not token:
                break
            body["nextPageToken"] = token
    return histories


def fetch_changelog_single(session, base, key):
    histories = []
    start = 0
    while True:
        resp = request(session, "GET", f"{base}/rest/api/3/issue/{key}/changelog",
                       params={"startAt": start, "maxResults": 100})
        resp.raise_for_status()
        data = resp.json()
        values = data.get("values", [])
        histories.extend(values)
        start += len(values)
        if data.get("isLast", True) or not values:
            return histories
