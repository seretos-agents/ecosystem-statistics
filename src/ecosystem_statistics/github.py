"""GitHub REST client: rate-limit retry-with-backoff (R4) and merged-PR listing (used by
R3's `collect`).

`request()` is the one place every GitHub call in this package goes through. On a
transient rate limit (403 with `x-ratelimit-remaining: 0`, or any 429) it waits and
retries instead of crashing; on any other error status it raises immediately. `sleep`
and `now` are injectable seams so tests never actually sleep and never depend on the
wall clock.
"""

from __future__ import annotations

import sys
import time as _time
from dataclasses import dataclass
from datetime import date, datetime, timezone

import httpx

_MAX_WAIT_SECONDS = 3600.0
_MIN_WAIT_SECONDS = 1.0


class RateLimitExhausted(RuntimeError):
    """Raised when a GitHub request is still rate-limited after `max_attempts` tries."""


def make_client(token: str | None = None) -> httpx.Client:
    """An `httpx.Client` for the GitHub REST API. `token` (ECOSYSTEM_TOKEN or
    GITHUB_TOKEN) is optional -- without one, requests run against the public,
    lower-rate-limited API."""
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return httpx.Client(base_url="https://api.github.com", headers=headers, timeout=30.0)


def _is_rate_limited(response: httpx.Response) -> bool:
    if response.status_code == 429:
        return True
    return response.status_code == 403 and response.headers.get("x-ratelimit-remaining") == "0"


def _wait_seconds(response: httpx.Response, attempt: int, *, now) -> float:
    retry_after = response.headers.get("Retry-After")
    if retry_after is not None:
        wait = float(retry_after)
    elif response.status_code == 403:
        reset = response.headers.get("x-ratelimit-reset")
        wait = float(reset) - now() if reset is not None else float(2**attempt)
    else:
        wait = float(2**attempt)
    return max(_MIN_WAIT_SECONDS, min(wait, _MAX_WAIT_SECONDS))


def request(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    sleep=_time.sleep,
    now=_time.time,
    max_attempts: int = 6,
    **kwargs,
) -> httpx.Response:
    attempt = 0
    while attempt < max_attempts:
        response = client.request(method, url, **kwargs)
        if not _is_rate_limited(response):
            response.raise_for_status()
            return response
        sleep(_wait_seconds(response, attempt, now=now))
        attempt += 1
    raise RateLimitExhausted(
        f"GitHub rate limit exhausted after {attempt} attempts for {method} {url}"
    )


# ---------------------------------------------------------------------------
# Merged-PR listing (R3)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MergedPullRequest:
    number: int
    merged_at: datetime
    merge_commit_sha: str


def _parse_github_datetime(raw: str) -> datetime:
    return datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _next_link(response: httpx.Response) -> str | None:
    link = response.links.get("next")
    return link["url"] if link else None


def list_merged_prs(
    client: httpx.Client,
    owner: str,
    repo: str,
    base: str,
    since: date,
    until: date,
    *,
    sleep=_time.sleep,
    now=_time.time,
) -> list[MergedPullRequest]:
    """Merged PRs into `base` with `merged_at` inside `[since, until]` (UTC calendar
    days). Pages newest-updated-first and stops as soon as a page's `updated_at` falls
    before `since` -- any PR merged in-window must have been updated at least that
    recently, so nothing in-window is missed by stopping there."""
    results: list[MergedPullRequest] = []
    since_start = datetime.combine(since, datetime.min.time(), tzinfo=timezone.utc)
    # A full absolute URL, not a base_url-relative path: `make_client` sets a base_url,
    # but a test double standing in for it (e.g. R3's MockTransport client) may not, and
    # a bare path is not a valid request target on its own.
    url: str | None = f"https://api.github.com/repos/{owner}/{repo}/pulls"
    params: dict[str, str] | None = {
        "state": "closed",
        "base": base,
        "sort": "updated",
        "direction": "desc",
        "per_page": "100",
    }

    while url:
        response = request(client, "GET", url, params=params, sleep=sleep, now=now)
        params = None  # only the first request needs query params; Link URLs carry them
        items = response.json()

        stop = False
        for item in items:
            if _parse_github_datetime(item["updated_at"]) < since_start:
                stop = True
                break
            merged_at_raw = item.get("merged_at")
            if not merged_at_raw:
                continue
            merged_at = _parse_github_datetime(merged_at_raw)
            if not (since <= merged_at.date() <= until):
                continue
            merge_sha = item.get("merge_commit_sha")
            if not merge_sha:
                print(
                    f"skip PR #{item['number']} in {owner}/{repo}: no merge_commit_sha",
                    file=sys.stderr,
                )
                continue
            results.append(
                MergedPullRequest(
                    number=item["number"], merged_at=merged_at, merge_commit_sha=merge_sha
                )
            )

        if stop:
            break
        url = _next_link(response)

    return results
