"""Tests for domain exception classification and localization."""

from __future__ import annotations

import errno
from pathlib import Path

import pytest

from unduhin.errors import (
    CancelledError,
    DependencyError,
    DiskSpaceError,
    DownloadFailedError,
    DrmProtectedError,
    LiveStreamError,
    MediaUnavailableError,
    NetworkTimeoutError,
    OutdatedExtractorError,
    RateLimitError,
    TransientHttp403Error,
    TransientNetworkError,
    UnsupportedMediaError,
    classify_exception,
)
from unduhin.i18n import t


# Error strings captured directly from installed yt-dlp 2026.10.26 source files:
# - extractor/youtube/_video.py:198,818,902,2901,3420,4062,4071-4076
# - extractor/youtube/_base.py:715-717,1157
# - extractor/youtube/_tab.py:2209
# - extractor/common.py:1227,1246,1256
# - YoutubeDL.py:1191,3093
# - networking/exceptions.py:63
@pytest.mark.parametrize(
    ("raw_msg", "expected_cls", "expected_key"),
    [
        ("ERROR: Private video. Sign in if you've been granted access", MediaUnavailableError, "error.video_unavailable"),
        ("ERROR: This video has been removed for violating YouTube's policy on hate speech.", MediaUnavailableError, "error.video_unavailable"),
        ("ERROR: Video unavailable", MediaUnavailableError, "error.video_unavailable"),
        ("ERROR: Video unavailable. This video has been removed by the uploader", MediaUnavailableError, "error.video_unavailable"),
        ("ERROR: Members-only videos are only available to channel members", MediaUnavailableError, "error.video_unavailable"),
        ("ERROR: Join this channel to access members only content", MediaUnavailableError, "error.video_unavailable"),
        ("ERROR: This video is only available for registered users", MediaUnavailableError, "error.video_unavailable"),
        ("ERROR: Sign in to confirm your age", MediaUnavailableError, "error.age_restricted"),
        ("ERROR: This video may be inappropriate for some users", MediaUnavailableError, "error.age_restricted"),
        ("ERROR: The uploader has not made this video available in your country", MediaUnavailableError, "error.geo_blocked"),
        ("ERROR: This video is not available from your location due to geo restriction", MediaUnavailableError, "error.geo_blocked"),
        ("ERROR: Sign in to confirm you're not a bot", MediaUnavailableError, "error.bot_check"),
        ("ERROR: This live event will begin in a few moments", LiveStreamError, "error.live_stream_unsupported"),
        ("ERROR: This video is DRM protected", DrmProtectedError, "error.drm_protected"),
        ("ERROR: Some formats have been skipped as they are DRM protected.", DrmProtectedError, "error.drm_protected"),
        ("ERROR: Requested format is not available. Use --list-formats for a list of available formats", UnsupportedMediaError, "error.format_unavailable"),
        ("ERROR: No video formats found!", UnsupportedMediaError, "error.format_unavailable"),
        ("ERROR: Unsupported URL: https://example.com", UnsupportedMediaError, "error.unsupported_url"),
        ("ERROR: ffmpeg not found. Please install ffmpeg", DependencyError, "error.ffmpeg_missing"),
        ("ERROR: No supported JavaScript runtime could be found", DependencyError, "error.deno_missing"),
        ("ERROR: HTTP Error 403: Forbidden", TransientHttp403Error, "error.http_403_transient"),
        ("ERROR: HTTP Error 429: Too Many Requests", RateLimitError, "error.rate_limited"),
        ("ERROR: This content isn't available, try again later", RateLimitError, "error.rate_limited"),
        ("ERROR: HTTP Error 500: Internal Server Error", TransientNetworkError, "error.network_transient"),
        ("ERROR: HTTP Error 502: Bad Gateway", TransientNetworkError, "error.network_transient"),
        ("ERROR: HTTP Error 503: Service Unavailable", TransientNetworkError, "error.network_transient"),
        ("ERROR: HTTP Error 504: Gateway Timeout", NetworkTimeoutError, "error.network_timeout"),
        ("ERROR: Connection reset by peer", TransientNetworkError, "error.network_transient"),
        ("ERROR: Unable to extract signature extraction failed", OutdatedExtractorError, "error.ytdlp_outdated"),
        ("ERROR: Something completely weird happened", DownloadFailedError, "error.unexpected"),
    ],
)
def test_classify_exception_maps_known_messages(
    raw_msg: str,
    expected_cls: type[Exception],
    expected_key: str,
) -> None:
    mapped = classify_exception(RuntimeError(raw_msg))
    assert isinstance(mapped, expected_cls)
    assert mapped.i18n_key == expected_key
    assert mapped.format("en")
    assert mapped.format("id")


def test_bot_check_guidance_avoids_cookie_export_and_is_not_retried() -> None:
    mapped = classify_exception(RuntimeError("Sign in to confirm you're not a bot"))
    assert isinstance(mapped, MediaUnavailableError)
    assert not isinstance(mapped, TransientNetworkError)
    assert mapped.i18n_key == "error.bot_check"
    for lang in ("en", "id"):
        msg = mapped.format(lang).lower()
        assert "export" not in msg
        assert "ekspor" not in msg
        assert "--cookies" not in msg
        assert "yt-dlp" in msg


def test_unknown_error_mentions_verbose_and_is_not_transient() -> None:
    mapped = classify_exception(RuntimeError("Mystery platform failure xyz-987"))
    assert isinstance(mapped, DownloadFailedError)
    assert not isinstance(mapped, TransientNetworkError)
    for lang in ("en", "id"):
        msg = t(mapped.i18n_key, lang=lang, **mapped.params)
        assert "--verbose" in msg


def test_classify_exception_handles_enospc_and_cancellation(tmp_path: Path) -> None:
    enospc = OSError(errno.ENOSPC, "No space left on device")
    mapped_disk = classify_exception(enospc, output_dir=tmp_path)
    assert isinstance(mapped_disk, DiskSpaceError)
    assert str(tmp_path) in mapped_disk.format("en")

    mapped_kbd = classify_exception(KeyboardInterrupt())
    assert isinstance(mapped_kbd, CancelledError)

    class DownloadCancelled(Exception):
        pass

    mapped_dl_cancel = classify_exception(DownloadCancelled("stopped"))
    assert isinstance(mapped_dl_cancel, CancelledError)


def test_outdated_ytdlp_and_bot_check_show_uv_pipx_pip_upgrades() -> None:
    for err in (
        OutdatedExtractorError("error.ytdlp_outdated"),
        MediaUnavailableError("error.bot_check"),
    ):
        for lang in ("en", "id"):
            formatted = err.format(lang)
            assert "uv tool upgrade unduhin" in formatted
            assert 'pipx runpip unduhin install -U "yt-dlp[default]"' in formatted
            assert 'pip install -U "yt-dlp[default]"' in formatted
