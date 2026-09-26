"""R4 — GitHub rate-limit retry-with-backoff (plan #9).

`request(client, method, url, *, sleep, now, max_attempts=6)` must survive a 403 with
`x-ratelimit-remaining: 0` (wait until the reset time) followed by a 429 with
`Retry-After` (wait that many seconds), and return the eventual 200 — never crash on a
transient rate limit, and never actually sleep in a test (sleep/now are injected seams).
"""

from __future__ import annotations

import httpx


def test_rate_limited_then_ok() -> None:
    now_value = 1_700_000_000.0
    reset_at = int(now_value) + 5
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(
                403,
                headers={
                    "x-ratelimit-remaining": "0",
                    "x-ratelimit-reset": str(reset_at),
                },
            )
        if len(calls) == 2:
            return httpx.Response(429, headers={"Retry-After": "2"})
        return httpx.Response(200, json=[{"number": 1}])

    client = httpx.Client(transport=httpx.MockTransport(handler))
    waits: list[float] = []

    from ecosystem_statistics.github import request

    response = request(
        client,
        "GET",
        "https://api.github.com/repos/acme/demo/pulls",
        sleep=waits.append,
        now=lambda: now_value,
    )

    assert response.status_code == 200
    assert response.json() == [{"number": 1}]
    assert len(calls) == 3
    # First wait: reset(now+5) - now() = 5s. Second wait: Retry-After = 2s.
    assert waits == [5, 2]
