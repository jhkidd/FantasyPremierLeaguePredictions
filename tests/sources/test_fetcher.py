from __future__ import annotations

import httpx
import pytest
import respx

from fpl.config import SourceConfig
from fpl.sources.errors import BlockedError, RateLimitedError, SourceError, TransientError
from fpl.sources.fetcher import HttpFetcher, RateLimiter

URL = "https://example.test/api/thing/"


def fetcher(**overrides) -> HttpFetcher:
    """A fetcher that never actually sleeps, so tests stay fast and exact."""
    config = SourceConfig(
        name="test",
        min_request_interval=overrides.pop("min_request_interval", 0.0),
        timeout=1.0,
        max_attempts=overrides.pop("max_attempts", 4),
    )
    return HttpFetcher(
        config,
        user_agent="test-agent/1.0 (+https://example.test)",
        sleep=overrides.pop("sleep", lambda _seconds: None),
        # Deterministic "jitter" so backoff assertions are exact.
        jitter=overrides.pop("jitter", lambda upper: upper),
        **overrides,
    )


class TestRateLimiter:
    def test_first_call_does_not_wait(self) -> None:
        slept: list[float] = []
        limiter = RateLimiter(2.0, clock=lambda: 100.0, sleep=slept.append)
        limiter.wait()
        assert slept == []

    def test_waits_out_the_remaining_interval(self) -> None:
        slept: list[float] = []
        now = [100.0]
        limiter = RateLimiter(2.0, clock=lambda: now[0], sleep=slept.append)
        limiter.wait()
        now[0] = 100.5
        limiter.wait()
        assert slept == [1.5]

    def test_does_not_wait_when_enough_time_has_passed(self) -> None:
        slept: list[float] = []
        now = [100.0]
        limiter = RateLimiter(2.0, clock=lambda: now[0], sleep=slept.append)
        limiter.wait()
        now[0] = 105.0
        limiter.wait()
        assert slept == []

    def test_zero_interval_never_waits(self) -> None:
        slept: list[float] = []
        limiter = RateLimiter(0.0, clock=lambda: 100.0, sleep=slept.append)
        limiter.wait()
        limiter.wait()
        assert slept == []


class TestSuccess:
    @respx.mock
    def test_returns_body_and_status(self) -> None:
        respx.get(URL).mock(return_value=httpx.Response(200, json={"ok": True}))
        response = fetcher().get(URL)
        assert response.status == 200
        assert response.json() == {"ok": True}

    @respx.mock
    def test_returns_raw_bytes_unmodified(self) -> None:
        """The raw layer stores exactly what the source returned."""
        payload = b'{"a": 1,  "b":  2}'
        respx.get(URL).mock(return_value=httpx.Response(200, content=payload))
        assert fetcher().get(URL).body == payload

    @respx.mock
    def test_sends_the_descriptive_user_agent(self) -> None:
        route = respx.get(URL).mock(return_value=httpx.Response(200, json={}))
        fetcher().get(URL)
        assert "example.test" in route.calls[0].request.headers["user-agent"]

    @respx.mock
    def test_passes_query_parameters(self) -> None:
        route = respx.get(URL).mock(return_value=httpx.Response(200, json={}))
        fetcher().get(URL, params={"event": 7})
        assert route.calls[0].request.url.params["event"] == "7"


class TestBlocked:
    @respx.mock
    def test_403_raises_blocked(self) -> None:
        respx.get(URL).mock(return_value=httpx.Response(403))
        with pytest.raises(BlockedError):
            fetcher().get(URL)

    @respx.mock
    def test_403_is_never_retried(self) -> None:
        """Spec §10: a 403 means blocked, not mistimed. Retrying into a block
        wastes requests and delays the moment we find out."""
        route = respx.get(URL).mock(return_value=httpx.Response(403))
        with pytest.raises(BlockedError):
            fetcher(max_attempts=4).get(URL)
        assert route.call_count == 1

    @respx.mock
    def test_403_carries_response_headers(self) -> None:
        """cf-ray and server are what distinguish a Cloudflare block from an
        endpoint that genuinely needs authentication."""
        respx.get(URL).mock(
            return_value=httpx.Response(403, headers={"cf-ray": "abc123", "server": "cloudflare"})
        )
        with pytest.raises(BlockedError) as caught:
            fetcher().get(URL)
        assert caught.value.headers["cf-ray"] == "abc123"
        assert caught.value.looks_like_cloudflare

    @respx.mock
    def test_403_without_cloudflare_markers_is_not_misattributed(self) -> None:
        respx.get(URL).mock(return_value=httpx.Response(403, headers={"server": "nginx"}))
        with pytest.raises(BlockedError) as caught:
            fetcher().get(URL)
        assert not caught.value.looks_like_cloudflare


class TestRetries:
    @respx.mock
    def test_recovers_from_a_transient_500(self) -> None:
        respx.get(URL).mock(
            side_effect=[httpx.Response(500), httpx.Response(200, json={"ok": True})]
        )
        assert fetcher().get(URL).json() == {"ok": True}

    @respx.mock
    def test_gives_up_after_max_attempts(self) -> None:
        route = respx.get(URL).mock(return_value=httpx.Response(503))
        with pytest.raises(TransientError):
            fetcher(max_attempts=3).get(URL)
        assert route.call_count == 3

    @respx.mock
    def test_retries_timeouts(self) -> None:
        respx.get(URL).mock(
            side_effect=[httpx.TimeoutException("slow"), httpx.Response(200, json={})]
        )
        assert fetcher().get(URL).status == 200

    @respx.mock
    def test_retries_connection_errors(self) -> None:
        respx.get(URL).mock(
            side_effect=[httpx.ConnectError("refused"), httpx.Response(200, json={})]
        )
        assert fetcher().get(URL).status == 200

    @respx.mock
    def test_backoff_grows_exponentially(self) -> None:
        slept: list[float] = []
        respx.get(URL).mock(return_value=httpx.Response(500))
        with pytest.raises(TransientError):
            fetcher(max_attempts=4, sleep=slept.append).get(URL)
        assert slept == [2.0, 4.0, 8.0]

    @respx.mock
    def test_backoff_is_capped(self) -> None:
        slept: list[float] = []
        respx.get(URL).mock(return_value=httpx.Response(500))
        with pytest.raises(TransientError):
            fetcher(max_attempts=10, sleep=slept.append).get(URL)
        assert max(slept) <= 60.0

    @respx.mock
    def test_no_sleep_after_the_final_attempt(self) -> None:
        """Sleeping after the last try just delays the failure report."""
        slept: list[float] = []
        respx.get(URL).mock(return_value=httpx.Response(500))
        with pytest.raises(TransientError):
            fetcher(max_attempts=2, sleep=slept.append).get(URL)
        assert len(slept) == 1

    def test_jitter_is_applied_to_the_backoff(self) -> None:
        """Full jitter spreads retries out instead of synchronising them."""
        seen: list[float] = []

        def recording_jitter(upper: float) -> float:
            seen.append(upper)
            return 0.0

        with respx.mock:
            respx.get(URL).mock(return_value=httpx.Response(500))
            with pytest.raises(TransientError):
                fetcher(max_attempts=3, jitter=recording_jitter).get(URL)
        assert seen == [2.0, 4.0]


class TestRateLimitResponses:
    @respx.mock
    def test_429_is_retried(self) -> None:
        respx.get(URL).mock(side_effect=[httpx.Response(429), httpx.Response(200, json={})])
        assert fetcher().get(URL).status == 200

    @respx.mock
    def test_429_exhausted_raises_rate_limited(self) -> None:
        respx.get(URL).mock(return_value=httpx.Response(429))
        with pytest.raises(RateLimitedError):
            fetcher(max_attempts=2).get(URL)

    @respx.mock
    def test_retry_after_header_is_honoured(self) -> None:
        slept: list[float] = []
        respx.get(URL).mock(return_value=httpx.Response(429, headers={"retry-after": "17"}))
        with pytest.raises(RateLimitedError):
            fetcher(max_attempts=2, sleep=slept.append).get(URL)
        assert slept == [17.0]

    @respx.mock
    def test_unparseable_retry_after_falls_back_to_normal_backoff(self) -> None:
        slept: list[float] = []
        respx.get(URL).mock(
            return_value=httpx.Response(
                429, headers={"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}
            )
        )
        with pytest.raises(RateLimitedError):
            fetcher(max_attempts=2, sleep=slept.append).get(URL)
        assert slept == [2.0]


class TestOtherClientErrors:
    @respx.mock
    def test_404_is_not_retried(self) -> None:
        """The request was understood and refused. Retrying an unambiguous 'no'
        just wastes the source's patience."""
        route = respx.get(URL).mock(return_value=httpx.Response(404))
        with pytest.raises(SourceError):
            fetcher(max_attempts=4).get(URL)
        assert route.call_count == 1

    @respx.mock
    def test_404_is_not_reported_as_blocked(self) -> None:
        respx.get(URL).mock(return_value=httpx.Response(404))
        with pytest.raises(SourceError) as caught:
            fetcher().get(URL)
        assert not isinstance(caught.value, BlockedError)


class TestSchemePreservation:
    @respx.mock
    def test_http_urls_are_not_upgraded_to_https(self) -> None:
        """Club Elo answers on HTTP and does not respond on HTTPS at all
        (spec §13), so a helpful upgrade would silently break it."""
        insecure = "http://api.clubelo.test/Fixtures"
        route = respx.get(insecure).mock(return_value=httpx.Response(200, content=b"x"))
        fetcher().get(insecure)
        assert route.calls[0].request.url.scheme == "http"

    @respx.mock
    def test_https_urls_stay_https(self) -> None:
        route = respx.get(URL).mock(return_value=httpx.Response(200, json={}))
        fetcher().get(URL)
        assert route.calls[0].request.url.scheme == "https"


class TestPoliteness:
    @respx.mock
    def test_spacing_is_enforced_between_requests(self) -> None:
        slept: list[float] = []
        respx.get(URL).mock(return_value=httpx.Response(200, json={}))
        client = fetcher(min_request_interval=2.0, sleep=slept.append)
        now = [0.0]
        client.limiter.clock = lambda: now[0]
        client.get(URL)
        now[0] = 0.5
        client.get(URL)
        assert slept == [1.5]
