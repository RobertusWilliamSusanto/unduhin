"""Domain exception hierarchy and exception classification for unduhin."""

from __future__ import annotations

import errno
import re
from pathlib import Path

from unduhin.config import ExitCode
from unduhin.i18n import t


class UnduhinError(Exception):
    """Base localized exception for all unduhin domain errors."""

    exit_code: ExitCode = ExitCode.FAILURE

    def __init__(self, i18n_key: str, **params: object) -> None:
        self.i18n_key = i18n_key
        self.params: dict[str, object] = params
        super().__init__(t(i18n_key, lang="en", **params))

    def format(self, lang: str | None = None) -> str:
        """Render this error in the requested or detected language."""
        return t(self.i18n_key, lang=lang, **self.params)


class CancelledError(UnduhinError):
    """Raised when the user cancels execution (e.g. Ctrl+C)."""

    exit_code = ExitCode.CANCELLED


class ValidationError(UnduhinError):
    """Raised on invalid URLs, templates, options, or batch input."""

    exit_code = ExitCode.INVALID_INPUT


class PathSecurityError(ValidationError):
    """Raised when path containment or extension whitelist validation fails."""


class DependencyError(UnduhinError):
    """Raised when a required external binary such as FFmpeg is unavailable."""

    exit_code = ExitCode.MISSING_DEPENDENCY


class MediaUnavailableError(UnduhinError):
    """Raised for private, deleted, age-restricted, or geo-blocked media."""


class UnsupportedMediaError(UnduhinError):
    """Raised when a URL or output format is not supported."""


class DrmProtectedError(UnduhinError):
    """Raised when media is DRM-protected."""


class LiveStreamError(UnduhinError):
    """Raised for live or upcoming streams that are not supported."""


class DiskSpaceError(UnduhinError):
    """Raised when disk space is insufficient or ENOSPC occurs."""


class OutdatedExtractorError(UnduhinError):
    """Raised when extractor failures likely indicate an outdated yt-dlp."""


class TransientNetworkError(UnduhinError):
    """Raised for retryable network timeouts, drops, 403s, or 5xx responses."""


class NetworkTimeoutError(TransientNetworkError):
    """Raised when a network connection or read times out."""


class TransientHttp403Error(TransientNetworkError):
    """Raised for retryable HTTP 403 responses before falling back to OutdatedExtractorError."""


class RateLimitError(TransientNetworkError):
    """Raised when HTTP 429 rate limiting is encountered."""


class PlaylistDisabledError(ValidationError):
    """Raised when a playlist URL is passed without ``--playlist``."""

    exit_code = ExitCode.INVALID_INPUT


class DownloadFailedError(UnduhinError):
    """Fallback exception for unclassified download failures."""


_HTTP_429_RE = re.compile(
    r"\bhttp error 429\b|\btoo many requests\b|\brate[- ]limit",
    re.IGNORECASE,
)
_HTTP_5XX_RE = re.compile(r"\bhttp error 5\d\d\b", re.IGNORECASE)


def classify_exception(
    exc: BaseException,
    *,
    output_dir: Path | None = None,
) -> UnduhinError:
    """Map raw yt-dlp, OSError, or runtime exceptions into typed ``UnduhinError`` instances."""
    if isinstance(exc, UnduhinError):
        return exc

    exc_type_name = type(exc).__name__
    msg = str(exc).strip()
    lowered = msg.lower()

    if isinstance(exc, KeyboardInterrupt) or exc_type_name == "DownloadCancelled":
        return CancelledError("error.cancelled")

    if isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
        return DiskSpaceError("error.disk_full", path=str(output_dir or "."))

    if any(tok in lowered for tok in ("no space left on device", "disk full", "not enough space")):
        return DiskSpaceError("error.disk_full", path=str(output_dir or "."))

    if any(tok in lowered for tok in ("drm", "widevine", "fairplay", "playready")):
        return DrmProtectedError("error.drm_protected")

    # Anti-bot challenge must be detected before generic age/sign-in checks.
    if any(
        tok in lowered
        for tok in ("confirm you're not a bot", "confirm you are not a bot", "not a bot")
    ):
        return MediaUnavailableError("error.bot_check")

    if any(
        tok in lowered
        for tok in (
            "sign in to confirm your age",
            "confirm your age",
            "age-restricted",
            "inappropriate for some users",
        )
    ):
        return MediaUnavailableError("error.age_restricted")

    if any(
        tok in lowered
        for tok in (
            "not available in your country",
            "made this video available in your country",
            "not available from your location due to geo restriction",
            "geo-restricted",
            "georestricted",
            "blocked it in your country",
        )
    ):
        return MediaUnavailableError("error.geo_blocked")

    if any(
        tok in lowered
        for tok in (
            "private video",
            "video unavailable",
            "has been removed",
            "been deleted",
            "does not exist",
            "terminated",
            "members-only",
            "members only",
            "only available for registered users",
        )
    ):
        return MediaUnavailableError("error.video_unavailable")

    if any(
        tok in lowered
        for tok in ("live event will begin", "premieres in", "is live", "live stream")
    ):
        return LiveStreamError("error.live_stream_unsupported")

    if ("ffmpeg" in lowered or "ffprobe" in lowered) and (
        "not found" in lowered or "not installed" in lowered or "unable to obtain" in lowered
    ):
        return DependencyError("error.ffmpeg_missing")

    if "javascript runtime" in lowered or ("deno" in lowered and "not found" in lowered):
        return DependencyError("error.deno_missing")

    if any(
        tok in lowered
        for tok in ("requested format is not available", "no video formats found")
    ):
        return UnsupportedMediaError("error.format_unavailable")

    if exc_type_name == "UnsupportedError" or "unsupported url" in lowered:
        return UnsupportedMediaError("error.unsupported_url")

    if _HTTP_429_RE.search(msg) or "content isn't available, try again later" in lowered:
        return RateLimitError("error.rate_limited")

    if (
        isinstance(exc, TimeoutError)
        or any(tok in lowered for tok in ("timed out", "timeout"))
    ):
        return NetworkTimeoutError("error.network_timeout", detail=msg or exc_type_name)

    if (
        isinstance(exc, ConnectionError)
        or _HTTP_5XX_RE.search(msg)
        or any(
            tok in lowered
            for tok in (
                "connection reset",
                "connection aborted",
                "incomplete read",
                "temporary failure in name resolution",
                "name or service not known",
                "temporarily unavailable",
                "remote end closed connection",
                "eof occurred",
            )
        )
    ):
        return TransientNetworkError("error.network_transient", detail=msg or exc_type_name)

    if "http error 403" in lowered:
        return TransientHttp403Error("error.http_403_transient")

    if any(
        tok in lowered
        for tok in (
            "unable to extract",
            "signature extraction failed",
            "nsig extraction failed",
        )
    ):
        return OutdatedExtractorError("error.ytdlp_outdated")

    return DownloadFailedError("error.unexpected", detail=msg or exc_type_name)
