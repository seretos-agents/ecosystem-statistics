"""R4 — GitHub rate-limit retry-with-backoff (plan #9).

`request(client, method, url, *, sleep, now, max_attempts=6)` must survive a 403 with
`x-ratelimit-remaining: 0` (wait until the reset time) followed by a 429 with
`Retry-After` (wait that many seconds), and return the eventual 200 — never crash on a
transient rate limit, and never actually sleep in a test (sleep/now are injected seams).
"""

from __future__ import annotations

import httpx
import pytest


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
            # Deliberately NOT 2 (== 2**1, the exponential-backoff fallback for this
            # attempt): if a wrong implementation ignored Retry-After and used the
            # fallback instead, it would coincidentally wait the same 2s and this test
            # would not catch it. 9 has no such collision.
            return httpx.Response(429, headers={"Retry-After": "9"})
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
    # First wait: reset(now+5) - now() = 5s. Second wait: honored Retry-After = 9s (not
    # the 2**attempt=2s fallback -- see the handler comment above).
    assert waits == [5, 9]


def test_403_without_remaining_zero_raises_immediately() -> None:
    """A plain 403 (no `x-ratelimit-remaining: 0`) is a real error, not a rate limit --
    it must not be retried."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(403, headers={"x-ratelimit-remaining": "42"})

    client = httpx.Client(transport=httpx.MockTransport(handler))

    from ecosystem_statistics.github import request

    with pytest.raises(httpx.HTTPStatusError):
        request(
            client,
            "GET",
            "https://api.github.com/repos/acme/demo/pulls",
            sleep=lambda _seconds: None,
            now=lambda: 1_700_000_000.0,
        )
    assert len(calls) == 1


def test_persistent_rate_limit_raises_rate_limit_exhausted() -> None:
    """A rate limit that never clears must give up after `max_attempts`, not hang or
    crash with a generic error."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "1"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    waits: list[float] = []

    from ecosystem_statistics.github import RateLimitExhausted, request

    with pytest.raises(RateLimitExhausted):
        request(
            client,
            "GET",
            "https://api.github.com/repos/acme/demo/pulls",
            sleep=waits.append,
            now=lambda: 1_700_000_000.0,
            max_attempts=3,
        )
    assert len(calls) == 3
    assert waits == [1, 1, 1]
