"""Typed adapter wrapping ``yt_dlp.YoutubeDL``."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from unduhin.config import (
    DEFAULT_MAX_PLAYLIST_ITEMS,
    AdapterDownloadResult,
    FormatOption,
    MediaInfo,
    MediaMode,
)
from unduhin.doctor import find_deno, find_ffmpeg
from unduhin.errors import CancelledError, DownloadFailedError


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    """Normalized progress update emitted during media download."""

    status: str
    downloaded_bytes: int = 0
    total_bytes: int | None = None
    speed_bytes_per_sec: float | None = None
    eta_seconds: float | None = None


ProgressCallback = Callable[[ProgressEvent], None]
CancelCheck = Callable[[], bool]
LogCallback = Callable[[str], None]


class MediaAdapter(Protocol):
    """Protocol abstracting ``yt_dlp`` operations for testability without network."""

    def extract_info(
        self,
        url: str,
        *,
        allow_playlist: bool = False,
        max_items: int = DEFAULT_MAX_PLAYLIST_ITEMS,
        verbose: bool = False,
    ) -> MediaInfo:
        """Fetch media metadata without downloading."""
        ...

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
        """Download and convert media into ``staging_dir`` and return the staged filepath."""
        ...


class _YdlLogger:
    """Logger bridge passed to ``yt_dlp.YoutubeDL``."""

    def __init__(self, *, verbose: bool, on_log: LogCallback | None = None) -> None:
        self._verbose = verbose
        self._on_log = on_log

    def debug(self, msg: str) -> None:
        if self._verbose and self._on_log is not None:
            self._on_log(msg)

    def warning(self, msg: str) -> None:
        if self._verbose and self._on_log is not None:
            self._on_log(f"[warning] {msg}")

    def error(self, msg: str) -> None:
        if self._on_log is not None:
            self._on_log(f"[error] {msg}")


def _extract_codecs(first_dl: dict[str, Any], raw: dict[str, Any]) -> tuple[str | None, str | None]:
    vcodec = str(first_dl.get("vcodec") or raw.get("vcodec") or "") or None
    acodec = str(first_dl.get("acodec") or raw.get("acodec") or "") or None

    req_formats = first_dl.get("requested_formats")
    if isinstance(req_formats, list):
        for fmt in req_formats:
            if not isinstance(fmt, dict):
                continue
            f_v = str(fmt.get("vcodec") or "")
            f_a = str(fmt.get("acodec") or "")
            if (not vcodec or vcodec == "none") and f_v and f_v != "none":
                vcodec = f_v
            if (not acodec or acodec == "none") and f_a and f_a != "none":
                acodec = f_a

    return vcodec, acodec


def is_h264_aac_compatible(vcodec: str | None, acodec: str | None) -> bool:
    """Return True when codecs are H.264/AVC video and AAC audio (or absent)."""
    v_norm = (vcodec or "").strip().lower()
    a_norm = (acodec or "").strip().lower()

    v_ok = (not v_norm or v_norm == "none") or any(
        tok in v_norm for tok in ("avc", "h264", "x264")
    )
    a_ok = (not a_norm or a_norm == "none") or any(
        tok in a_norm for tok in ("mp4a", "aac")
    )
    return v_ok and a_ok


def build_ydl_opts(
    *,
    download: bool,
    mode: MediaMode = MediaMode.AUDIO,
    audio_bitrate: int = 192,
    max_height: int = 1080,
    staging_dir: Path | None = None,
    allow_playlist: bool = False,
    max_items: int = DEFAULT_MAX_PLAYLIST_ITEMS,
    verbose: bool = False,
    deno_path: str | None = None,
    ffmpeg_path: str | None = None,
) -> dict[str, Any]:
    """Construct verified ``yt_dlp.YoutubeDL`` options.

    Security & determinism guarantees:
    - Never supplies cookies, browser cookie extraction, or credentials.
    - Disables ``file://`` URLs and runtime ``remote_components`` fetching.
    - Configures Deno via ``js_runtimes`` dict (``{'deno': {'path': ...}}`` or ``{'deno': {}}``).
    - Keeps user filename templates out of yt-dlp; writes staging files by ``%(id)s.%(ext)s``.
    - Bounds internal retries explicitly so the outer pipeline controls backoff and HTTP 429 handling.
    """
    js_runtimes: dict[str, dict[str, str]] = (
        {"deno": {"path": deno_path}} if deno_path else {"deno": {}}
    )

    opts: dict[str, Any] = {
        "quiet": not verbose,
        "no_warnings": not verbose,
        "verbose": verbose,
        "noprogress": True,
        "color": "never",
        "noplaylist": not allow_playlist,
        "enable_file_urls": False,
        "remote_components": [],
        "js_runtimes": js_runtimes,
        # Explicit bounded internal retries; outer pipeline handles exponential backoff & HTTP 429.
        "retries": 0,
        "fragment_retries": 1,
        "extractor_retries": 0,
        "socket_timeout": 30,
    }

    if ffmpeg_path and download:
        opts["ffmpeg_location"] = ffmpeg_path

    if not download:
        opts["skip_download"] = True
        opts["extract_flat"] = "in_playlist"
        if allow_playlist and max_items > 0:
            opts["playlist_items"] = f"1:{max_items + 1}"
        return opts

    if staging_dir is None:
        raise ValueError("staging_dir must be provided when download=True")

    opts["paths"] = {
        "home": str(staging_dir),
        "temp": str(staging_dir),
    }
    opts["outtmpl"] = {"default": "%(id)s.%(ext)s"}
    opts["restrictfilenames"] = True
    opts["windowsfilenames"] = True
    opts["overwrites"] = True

    if mode is MediaMode.AUDIO:
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": str(audio_bitrate),
            }
        ]
    else:
        opts["format"] = (
            f"bestvideo[height<={max_height}]+bestaudio/best[height<={max_height}]/best"
        )
        opts["format_sort"] = [
            f"res:{max_height}",
            "vcodec:h264",
            "acodec:aac",
            "vext:mp4",
            "aext:m4a",
        ]
        opts["merge_output_format"] = "mp4"
        opts["postprocessors"] = [
            {
                "key": "FFmpegVideoRemuxer",
                "preferedformat": "mp4",
            }
        ]

    return opts


def _parse_formats(raw_formats: object) -> tuple[FormatOption, ...]:
    if not isinstance(raw_formats, list):
        return ()
    parsed: list[FormatOption] = []
    for fmt in raw_formats:
        if not isinstance(fmt, dict):
            continue
        parsed.append(
            FormatOption(
                format_id=str(fmt.get("format_id") or ""),
                ext=str(fmt.get("ext") or ""),
                height=int(fmt["height"]) if isinstance(fmt.get("height"), int) else None,
                vcodec=str(fmt["vcodec"]) if fmt.get("vcodec") else None,
                acodec=str(fmt["acodec"]) if fmt.get("acodec") else None,
                abr=float(fmt["abr"]) if isinstance(fmt.get("abr"), (int, float)) else None,
                filesize_approx=(
                    int(fmt["filesize_approx"])
                    if isinstance(fmt.get("filesize_approx"), int)
                    else (
                        int(fmt["filesize"])
                        if isinstance(fmt.get("filesize"), int)
                        else None
                    )
                ),
            )
        )
    return tuple(parsed)


def normalize_info_dict(raw: dict[str, Any], *, fallback_url: str = "") -> MediaInfo:
    """Convert a raw ``yt_dlp`` info dictionary into a typed ``MediaInfo``."""
    raw_entries = raw.get("entries")
    is_playlist = raw.get("_type") in {"playlist", "multi_video"} or isinstance(raw_entries, list)
    entries: list[MediaInfo] = []
    if isinstance(raw_entries, list):
        for entry in raw_entries:
            if entry is None:
                entries.append(
                    MediaInfo(
                        id="unavailable",
                        title="unavailable",
                        url="",
                        is_unavailable=True,
                    )
                )
                continue
            if isinstance(entry, dict):
                entry_url = str(entry.get("webpage_url") or entry.get("url") or "")
                child = normalize_info_dict(entry, fallback_url=entry_url)
                if not (entry.get("url") or entry.get("webpage_url") or entry.get("id") or entry.get("entries")):
                    child = MediaInfo(
                        id=child.id,
                        title=child.title,
                        url=child.url,
                        is_unavailable=True,
                    )
                entries.append(child)

    filesize_raw = raw.get("filesize_approx") or raw.get("filesize")
    duration_raw = raw.get("duration")
    playlist_count_raw = raw.get("playlist_count")

    return MediaInfo(
        id=str(raw.get("id") or ""),
        title=str(raw.get("title") or ""),
        url=str(raw.get("webpage_url") or raw.get("original_url") or raw.get("url") or fallback_url),
        webpage_url=str(raw["webpage_url"]) if raw.get("webpage_url") else None,
        uploader=str(raw["uploader"]) if raw.get("uploader") else None,
        upload_date=str(raw["upload_date"]) if raw.get("upload_date") else None,
        duration_seconds=(
            float(duration_raw) if isinstance(duration_raw, (int, float)) else None
        ),
        is_live=bool(raw.get("is_live", False)),
        live_status=str(raw["live_status"]) if raw.get("live_status") else None,
        is_drm=bool(raw.get("drm", False) or raw.get("_has_drm", False)),
        is_unavailable=bool(raw.get("is_unavailable", False)),
        filesize_approx=int(filesize_raw) if isinstance(filesize_raw, (int, float)) else None,
        is_playlist=is_playlist,
        playlist_count=(
            int(playlist_count_raw) if isinstance(playlist_count_raw, int) else None
        ),
        entries=tuple(entries),
        formats=_parse_formats(raw.get("formats")),
    )


class YtDlpAdapter:
    """Concrete ``MediaAdapter`` implementation backed by ``yt_dlp.YoutubeDL``."""

    def __init__(self, *, log_callback: LogCallback | None = None) -> None:
        self._log_callback = log_callback

    def extract_info(
        self,
        url: str,
        *,
        allow_playlist: bool = False,
        max_items: int = DEFAULT_MAX_PLAYLIST_ITEMS,
        verbose: bool = False,
    ) -> MediaInfo:
        import yt_dlp

        opts = build_ydl_opts(
            download=False,
            allow_playlist=allow_playlist,
            max_items=max_items,
            verbose=verbose,
            deno_path=find_deno(),
        )
        opts["logger"] = _YdlLogger(verbose=verbose, on_log=self._log_callback)

        with yt_dlp.YoutubeDL(opts) as ydl:
            raw = ydl.extract_info(url, download=False)
        if not isinstance(raw, dict):
            raise DownloadFailedError("error.unexpected", detail="empty metadata response")
        return normalize_info_dict(raw, fallback_url=url)

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
        import yt_dlp
        from yt_dlp.utils import DownloadCancelled

        opts = build_ydl_opts(
            download=True,
            mode=mode,
            audio_bitrate=audio_bitrate,
            max_height=max_height,
            staging_dir=staging_dir,
            allow_playlist=allow_playlist,
            verbose=verbose,
            deno_path=find_deno(),
            ffmpeg_path=find_ffmpeg(),
        )
        opts["logger"] = _YdlLogger(verbose=verbose, on_log=self._log_callback)

        def _progress_hook(data: dict[str, Any]) -> None:
            if cancel_check is not None and cancel_check():
                raise DownloadCancelled("Download cancelled by user")
            if progress_callback is None:
                return
            status = str(data.get("status") or "")
            downloaded = data.get("downloaded_bytes")
            total = data.get("total_bytes") or data.get("total_bytes_estimate")
            speed = data.get("speed")
            eta = data.get("eta")
            progress_callback(
                ProgressEvent(
                    status=status,
                    downloaded_bytes=int(downloaded) if isinstance(downloaded, (int, float)) else 0,
                    total_bytes=int(total) if isinstance(total, (int, float)) else None,
                    speed_bytes_per_sec=float(speed) if isinstance(speed, (int, float)) else None,
                    eta_seconds=float(eta) if isinstance(eta, (int, float)) else None,
                )
            )

        opts["progress_hooks"] = [_progress_hook]

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                raw = ydl.extract_info(url, download=True)
        except DownloadCancelled as exc:
            raise CancelledError("error.cancelled") from exc

        if not isinstance(raw, dict):
            raise DownloadFailedError("error.unexpected", detail="empty download response")

        requested = raw.get("requested_downloads")
        if not isinstance(requested, list) or not requested or not isinstance(requested[0], dict):
            raise DownloadFailedError(
                "error.unexpected",
                detail="missing requested_downloads entry from yt-dlp",
            )

        first_dl = requested[0]
        raw_filepath = first_dl.get("filepath")
        if not raw_filepath:
            raise DownloadFailedError(
                "error.unexpected",
                detail="missing requested_downloads[0]['filepath'] from yt-dlp",
            )

        filepath = Path(str(raw_filepath))
        vcodec, acodec = _extract_codecs(first_dl, raw)
        codec_fallback = (
            mode is MediaMode.VIDEO and not is_h264_aac_compatible(vcodec, acodec)
        )
        is_audio_only = mode is MediaMode.VIDEO and (not vcodec or vcodec.strip().lower() == "none")

        return AdapterDownloadResult(
            filepath=filepath,
            info=normalize_info_dict(raw, fallback_url=url),
            codec_fallback_used=codec_fallback,
            vcodec=vcodec,
            acodec=acodec,
            is_audio_only_video=is_audio_only,
        )
