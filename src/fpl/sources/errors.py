"""The failure taxonomy from spec §10, expressed as types.

Call sites branch on class rather than on status codes, because the important
distinction is not *what* went wrong but *what to do about it*. A 5xx means try
again later. A 403 means we have been blocked and trying again makes it worse.
Those need to be impossible to confuse.
"""

from __future__ import annotations

from collections.abc import Mapping

__all__ = [
    "BlockedError",
    "RateLimitedError",
    "SchemaError",
    "SourceError",
    "TransientError",
]


class SourceError(Exception):
    """Base class for every failure originating in an external source."""

    def __init__(self, message: str, *, url: str | None = None) -> None:
        super().__init__(message)
        self.url = url


class TransientError(SourceError):
    """A 5xx, a timeout, or a dropped connection. Retryable.

    Snapshots are append-only, so abandoning a run leaves a gap rather than a
    corruption, and tomorrow's run recovers it.
    """

    def __init__(self, message: str, *, url: str | None = None, status: int | None = None) -> None:
        super().__init__(message, url=url)
        self.status = status


class RateLimitedError(SourceError):
    """HTTP 429. Retryable, but only after backing off properly.

    Politeness matters more than throughput on an undocumented API we depend on
    entirely and have no contract with.
    """

    def __init__(
        self,
        message: str,
        *,
        url: str | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message, url=url)
        self.retry_after = retry_after


class BlockedError(SourceError):
    """HTTP 403. **Never retryable.**

    Spec §13: the FPL site sits behind Cloudflare, which can classify datacenter
    traffic as automated and refuse it outright. That is a standing condition,
    not a transient one, so retrying wastes requests and — worse — delays the
    moment we find out. Connectivity from GitHub runners was verified clear on
    2026-07-30, but that is a point-in-time result and policy can change without
    notice.

    The response headers are carried along because ``cf-ray`` and ``server`` are
    what will distinguish "Cloudflare blocked us" from "this endpoint genuinely
    needs authentication" at 03:30 on some future morning.
    """

    def __init__(
        self,
        message: str,
        *,
        url: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(message, url=url)
        self.headers = dict(headers or {})

    @property
    def looks_like_cloudflare(self) -> bool:
        lowered = {key.lower(): value.lower() for key, value in self.headers.items()}
        return "cf-ray" in lowered or "cloudflare" in lowered.get("server", "")


class SchemaError(SourceError):
    """The response parsed but did not look like what we expect.

    Hard fail, never a warning. FPL adds and removes fields between seasons, and
    guessing at a changed schema is how a season of subtly wrong data gets
    built without anyone noticing.
    """
