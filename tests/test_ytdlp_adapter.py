"""Unit tests for the yt-dlp option builder and adapter boundary."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from unduhin.config import MediaMode
from unduhin.doctor import find_deno, find_ffmpeg
from unduhin.errors import CancelledError
from unduhin.ytdlp_adapter import (
    YtDlpAdapter,
    build_ydl_opts,
    is_h264_aac_compatible,
    normalize_info_dict,
)


def test_build_ydl_opts_info_mode_security_and_defaults() -> None:
    opts = build_ydl_opts(
        download=False,
        allow_playlist=False,
        verbose=False,
        deno_path="/usr/local/bin/deno",
        ffmpeg_path="/usr/bin/ffmpeg",
    )
    assert opts["skip_download"] is True
    assert opts["extract_flat"] == "in_playlist"
    assert opts["noplaylist"] is True
    assert opts["enable_file_urls"] is False
    assert opts["remote_components"] == []
    assert opts["js_runtimes"] == {"deno": {"path": "/usr/local/bin/deno"}}
    assert "ffmpeg_location" not in opts
    assert opts["retries"] == 0
    assert opts["fragment_retries"] == 1
    assert opts["extractor_retries"] == 0
    assert opts["quiet"] is True
    assert opts["verbose"] is False
    for forbidden in ("cookiefile", "cookiesfrombrowser", "username", "password", "usenetrc"):
        assert forbidden not in opts


def test_build_ydl_opts_audio_mode_uses_id_staging_and_mp3_postprocessor(tmp_path: Path) -> None:
    opts = build_ydl_opts(
        download=True,
        mode=MediaMode.AUDIO,
        audio_bitrate=256,
        staging_dir=tmp_path,
        verbose=True,
        deno_path=None,
    )
    assert opts["paths"] == {"home": str(tmp_path), "temp": str(tmp_path)}
    # Must NEVER forward user templates to yt-dlp; staging relies on ID-based naming.
    assert opts["outtmpl"] == {"default": "%(id)s.%(ext)s"}
    assert opts["restrictfilenames"] is True
    assert opts["windowsfilenames"] is True
    assert opts["js_runtimes"] == {"deno": {}}
    assert opts["format"] == "bestaudio/best"
    assert opts["postprocessors"] == [
        {
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "256",
        }
    ]
    assert opts["quiet"] is False
    assert opts["verbose"] is True


def test_build_ydl_opts_video_mode_prioritizes_h264_aac_and_remux(tmp_path: Path) -> None:
    opts = build_ydl_opts(
        download=True,
        mode=MediaMode.VIDEO,
        max_height=720,
        staging_dir=tmp_path,
    )
    assert opts["format"] == "bestvideo[height<=720]+bestaudio/best[height<=720]/best"
    assert opts["format_sort"] == [
        "res:720",
        "vcodec:h264",
        "acodec:aac",
        "vext:mp4",
        "aext:m4a",
    ]
    assert opts["merge_output_format"] == "mp4"
    assert opts["postprocessors"] == [
        {"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"}
    ]


def test_build_ydl_opts_requires_staging_dir_for_download() -> None:
    with pytest.raises(ValueError, match="staging_dir"):
        build_ydl_opts(download=True, staging_dir=None)


def test_is_h264_aac_compatible() -> None:
    assert is_h264_aac_compatible("avc1.640028", "mp4a.40.2") is True
    assert is_h264_aac_compatible("h264", "aac") is True
    assert is_h264_aac_compatible("vp9", "mp4a.40.2") is False
    assert is_h264_aac_compatible("avc1.640028", "opus") is False
    assert is_h264_aac_compatible("av01.0.08M.08", "opus") is False


def test_ytdlp_adapter_reads_requested_downloads_filepath_and_detects_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staged = tmp_path / "abc123.mp4"
    staged.write_bytes(b"video-data")

    class _StubYDL:
        def __init__(self, opts: dict[str, Any]) -> None:
            self.opts = opts

        def __enter__(self) -> _StubYDL:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def extract_info(self, url: str, download: bool = True) -> dict[str, Any]:
            _ = download
            return {
                "id": "abc123",
                "title": "Sample Video",
                "webpage_url": url,
                "requested_downloads": [
                    {
                        "filepath": str(staged),
                        "vcodec": "vp9",
                        "acodec": "opus",
                    }
                ],
            }

    import yt_dlp

    monkeypatch.setattr(yt_dlp, "YoutubeDL", _StubYDL)
    monkeypatch.setattr("unduhin.ytdlp_adapter.find_deno", lambda: None)
    monkeypatch.setattr("unduhin.ytdlp_adapter.find_ffmpeg", lambda: "/usr/bin/ffmpeg")

    adapter = YtDlpAdapter()
    result = adapter.download(
        "https://example.org/watch?v=abc123",
        mode=MediaMode.VIDEO,
        audio_bitrate=192,
        max_height=720,
        staging_dir=tmp_path,
    )
    assert result.filepath == staged
    assert result.codec_fallback_used is True
    assert result.vcodec == "vp9"
    assert result.acodec == "opus"


def test_ytdlp_adapter_progress_hook_raises_cancelled_when_flagged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from yt_dlp.utils import DownloadCancelled

    class _CancelYDL:
        def __init__(self, opts: dict[str, Any]) -> None:
            self.opts = opts

        def __enter__(self) -> _CancelYDL:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def extract_info(self, _url: str, download: bool = True) -> dict[str, Any]:
            _ = download
            for hook in self.opts.get("progress_hooks", []):
                hook({"status": "downloading", "downloaded_bytes": 100})
            return {}

    import yt_dlp

    monkeypatch.setattr(yt_dlp, "YoutubeDL", _CancelYDL)
    monkeypatch.setattr("unduhin.ytdlp_adapter.find_deno", lambda: None)
    monkeypatch.setattr("unduhin.ytdlp_adapter.find_ffmpeg", lambda: "/usr/bin/ffmpeg")

    adapter = YtDlpAdapter()
    with pytest.raises((CancelledError, DownloadCancelled)):
        adapter.download(
            "https://example.org/watch?v=abc123",
            mode=MediaMode.AUDIO,
            audio_bitrate=192,
            max_height=1080,
            staging_dir=tmp_path,
            cancel_check=lambda: True,
        )


@pytest.mark.network
def test_live_youtube_metadata_smoke_skipped_by_default() -> None:
    """Opt-in network integration test using a public Creative Commons sample."""
    adapter = YtDlpAdapter()
    info = adapter.extract_info("https://www.youtube.com/watch?v=aqz-KE-bpKQ")
    assert info.id == "aqz-KE-bpKQ"
    assert info.title


def test_build_ydl_opts_playlist_bounds() -> None:
    opts = build_ydl_opts(
        download=False,
        allow_playlist=True,
        max_items=25,
        ffmpeg_path="/usr/bin/ffmpeg",
    )
    assert opts["noplaylist"] is False
    assert opts["extract_flat"] == "in_playlist"
    assert opts["playlist_items"] == "1:26"
    assert "ffmpeg_location" not in opts


def test_normalize_info_dict_handles_none_and_nested_playlist_entries() -> None:
    raw: dict[str, Any] = {
        "_type": "playlist",
        "id": "pl1",
        "title": "Outer Playlist",
        "playlist_count": 4,
        "entries": [
            {"id": "vid1", "title": "First", "webpage_url": "https://example.org/1"},
            None,
            {"title": "Empty stub"},
            {
                "_type": "playlist",
                "id": "pl2",
                "title": "Inner Playlist",
                "entries": [
                    {"id": "vid2", "title": "Nested", "url": "https://example.org/2"},
                ],
            },
        ],
    }
    info = normalize_info_dict(raw, fallback_url="https://example.org/playlist")
    assert info.is_playlist is True
    assert info.playlist_count == 4
    assert len(info.entries) == 4
    assert info.entries[0].is_unavailable is False
    assert info.entries[0].url == "https://example.org/1"
    assert info.entries[1].is_unavailable is True
    assert info.entries[2].is_unavailable is True
    assert info.entries[3].is_playlist is True
    assert info.entries[3].entries[0].url == "https://example.org/2"


@pytest.mark.network
@pytest.mark.integration
@pytest.mark.skipif(not find_ffmpeg() or not find_deno(), reason="Requires ffmpeg and deno")
def test_live_end_to_end_audio_download_integration(tmp_path: Path) -> None:
    """Opt-in real end-to-end integration test requiring ffmpeg, deno, and network."""
    adapter = YtDlpAdapter()
    info = adapter.extract_info("https://www.youtube.com/watch?v=aqz-KE-bpKQ")
    assert info.id == "aqz-KE-bpKQ"
    result = adapter.download(
        info.url,
        mode=MediaMode.AUDIO,
        audio_bitrate=128,
        max_height=720,
        staging_dir=tmp_path,
    )
    assert result.filepath.exists()
    assert result.filepath.suffix.lower() == ".mp3"
