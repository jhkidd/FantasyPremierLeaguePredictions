"""Shared HTTP machinery for every source connector.

Connectors fetch bytes and nothing else. They do not parse, interpret or
reshape — that is staging's job. Keeping the boundary that sharp is what lets
the whole pipeline be replayed from ``raw/`` alone.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx

from fpl.config import SourceConfig
from fpl.log import get_logger
from fpl.sources.errors import BlockedError, RateLimitedError, SourceError, TransientError

__all__ = ["FetchResponse", "HttpFetcher", "RateLimiter"]

logger = get_logger(__name__)

_BACKOFF_BASE_SECONDS = 2.0
_MAX_BACKOFF_SECONDS = 60.0


@dataclass
class RateLimiter:
    """Enforces a minimum interval between consecutive requests.

    The clock and sleep functions are injectable so tests can prove the spacing
    without actually waiting for it.
    """

    min_interval: float
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    _last_call: float | None = field(default=None, init=False, repr=False)

    def wait(self) -> None:
        now = self.clock()
        if self._last_call is not None:
            elapsed = now - self._last_call
            remaining = self.min_interval - elapsed
            if remaining > 0:
                self.sleep(remaining)
                now = self.clock()
        self._last_call = now


@dataclass(frozen=True)
class FetchResponse:
    url: str
    status: int
    body: bytes
    headers: Mapping[str, str]

    def json(self) -> Any:
        import json

        return json.loads(self.body)


class HttpFetcher:
    """A polite, retrying HTTP client scoped to one source."""

    def __init__(
        self,
        config: SourceConfig,
        user_agent: str,
        *,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[float], float] | None = None,
    ) -> None:
        self.config = config
        self.user_agent = user_agent
        self._sleep = sleep
        self._jitter = jitter if jitter is not None else (lambda upper: random.uniform(0, upper))
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=config.timeout,
            follow_redirects=True,
        )
        self.limiter = RateLimiter(config.min_request_interval, sleep=sleep)

    def __enter__(self) -> HttpFetcher:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def get(
        self,
        url: str,
        params: Mapping[str, Any] | None = None,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> FetchResponse:
        """Fetch a URL, retrying only the failures worth retrying.

        The URL is used exactly as given. In particular the scheme is never
        rewritten: Club Elo answers on HTTP and does not respond on HTTPS at
        all (spec §13), so a helpful upgrade would silently break it.

        ``headers`` lets one call add to the default header set - e.g.
        football-data.org's ``X-Auth-Token`` (plan §7.1/§7.8), the one
        credentialed source in this project. Applied additively: a caller can
        never override ``User-Agent`` this way, since the shared fetcher is
        what makes every source's traffic legible, not the individual caller.
        """
        last_error: SourceError | None = None
        request_headers = self._headers()
        default_user_agent = request_headers["User-Agent"]
        if headers:
            request_headers.update(headers)
            request_headers["User-Agent"] = default_user_agent

        for attempt in range(1, self.config.max_attempts + 1):
            self.limiter.wait()
            try:
                response = self._client.get(url, params=params, headers=request_headers)
            except httpx.TimeoutException as exc:
                last_error = TransientError(f"timeout: {exc}", url=url)
            except httpx.TransportError as exc:
                last_error = TransientError(f"transport error: {exc}", url=url)
            else:
                outcome = self._classify(response, url)
                if outcome is None:
                    return FetchResponse(
                        url=str(response.url),
                        status=response.status_code,
                        body=response.content,
                        headers=dict(response.headers),
                    )
                last_error = outcome

            if not isinstance(last_error, TransientError | RateLimitedError):
                # Blocked, or an unambiguous 4xx refusal. Neither improves by
                # asking again, and retrying a block actively makes it worse.
                raise last_error

            if attempt < self.config.max_attempts:
                delay = self._backoff(attempt, last_error)
                logger.warning(
                    "retrying url=%s attempt=%d/%d delay=%.1fs reason=%s",
                    url,
                    attempt,
                    self.config.max_attempts,
                    delay,
                    last_error,
                )
                self._sleep(delay)

        assert last_error is not None
        raise last_error

    def _headers(self) -> dict[str, str]:
        return {"User-Agent": self.user_agent, "Accept": "application/json, text/plain, */*"}

    def _classify(self, response: httpx.Response, url: str) -> SourceError | None:
        """Return None if the response is good, else the error it represents."""
        status = response.status_code

        if status == 403:
            # Deliberately raised before any retry logic can see it.
            return BlockedError(
                f"403 Forbidden for {url}; treating as blocked, not transient",
                url=url,
                headers=dict(response.headers),
            )
        if status == 429:
            return RateLimitedError(
                f"429 Too Many Requests for {url}",
                url=url,
                retry_after=_parse_retry_after(response.headers.get("retry-after")),
            )
        if status >= 500:
            return TransientError(f"{status} from {url}", url=url, status=status)
        if status >= 400:
            # 404 and friends: the request was understood and refused. Retrying
            # an unambiguous "no" just wastes the source's patience.
            return SourceError(f"{status} from {url}", url=url)
        return None

    def _backoff(self, attempt: int, error: SourceError | None) -> float:
        if isinstance(error, RateLimitedError) and error.retry_after is not None:
            return min(error.retry_after, _MAX_BACKOFF_SECONDS)
        ceiling = min(_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)), _MAX_BACKOFF_SECONDS)
        # Full jitter: spreads retries out instead of synchronising them, which
        # matters if several endpoints fail together.
        return self._jitter(ceiling)


def _parse_retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        # The header also permits an HTTP date. Falling back to normal backoff
        # is better than parsing dates badly.
        return None
