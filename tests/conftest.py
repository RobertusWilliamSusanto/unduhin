"""Shared test fixtures for unduhin."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from unduhin.config import AdapterDownloadResult, MediaInfo, MediaMode
from unduhin.ytdlp_adapter import CancelCheck, ProgressCallback, ProgressEvent


class FakeMediaAdapter:
    """In-memory mock adapter implementing ``MediaAdapter`` with zero network access."""

    def __init__(
        self,
        *,
        info_by_url: dict[str, MediaInfo] | None = None,
        extract_error: Exception | None = None,
        download_handler: Callable[..., AdapterDownloadResult] | None = None,
    ) -> None:
        self.info_by_url: dict[str, MediaInfo] = info_by_url or {}
        self.extract_error = extract_error
        self.download_handler = download_handler
        self.extract_calls: list[tuple[str, bool, bool]] = []
        self.download_calls: list[tuple[str, MediaMode, int, int, Path]] = []

    def extract_info(
        self,
        url: str,
        *,
        allow_playlist: bool = False,
        max_items: int = 50,
        verbose: bool = False,
    ) -> MediaInfo:
        _ = max_items
        self.extract_calls.append((url, allow_playlist, verbose))
        if self.extract_error is not None:
            raise self.extract_error
        if url in self.info_by_url:
            return self.info_by_url[url]
        return MediaInfo(
            id="vid123",
            title="Sample Creative Commons Track",
            url=url,
            webpage_url=url,
            uploader="CC Creator",
            upload_date="20261001",
            duration_seconds=42.0,
            filesize_approx=1024 * 1024,
        )

    def download(
        self,
        url: str,
        *,
        mode: MediaMode,
        audio_bitrate: int,
        max_height: int,
        staging_dir: Path,
        allow_playlist: bool = False,
        verbose: bool = False,
        progress_callback: ProgressCallback | None = None,
        cancel_check: CancelCheck | None = None,
    ) -> AdapterDownloadResult:
        _ = (allow_playlist, verbose)
        self.download_calls.append((url, mode, audio_bitrate, max_height, staging_dir))
        if self.download_handler is not None:
            return self.download_handler(
                url=url,
                mode=mode,
                audio_bitrate=audio_bitrate,
                max_height=max_height,
                staging_dir=staging_dir,
                progress_callback=progress_callback,
                cancel_check=cancel_check,
            )

        if progress_callback is not None:
            progress_callback(ProgressEvent(status="downloading", downloaded_bytes=512, total_bytes=1024))
            progress_callback(ProgressEvent(status="finished", downloaded_bytes=1024, total_bytes=1024))

        ext = ".mp3" if mode is MediaMode.AUDIO else ".mp4"
        staged = staging_dir / f"vid123{ext}"
        staged.write_bytes(b"fake-media-bytes")
        info = self.info_by_url.get(url)
        if info is None:
            info = self._find_nested_info(url)
        if info is None:
            info = MediaInfo(id="vid123", title="Sample Creative Commons Track", url=url)
        return AdapterDownloadResult(
            filepath=staged,
            info=info,
            codec_fallback_used=False,
            vcodec="avc1.640028" if mode is MediaMode.VIDEO else "none",
            acodec="mp4a.40.2",
        )

    def _find_nested_info(self, url: str) -> MediaInfo | None:
        stack = list(self.info_by_url.values())
        while stack:
            node = stack.pop()
            if node.url == url or node.webpage_url == url:
                return node
            stack.extend(node.entries)
        return None


@pytest.fixture()
def clean_locale_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear locale environment variables for deterministic i18n tests."""
    for var in ("UNDUHIN_LANG", "LC_ALL", "LC_MESSAGES", "LANG", "LANGUAGE"):
        monkeypatch.delenv(var, raising=False)
