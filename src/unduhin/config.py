"""Configuration models, enumerations, and defaults for unduhin."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
from pathlib import Path


class MediaMode(StrEnum):
    """Target output media conversion mode."""

    AUDIO = "audio"
    VIDEO = "video"


class ConflictPolicy(StrEnum):
    """Policy when the destination file already exists."""

    RENAME = "rename"
    SKIP = "skip"
    OVERWRITE = "overwrite"


class ExitCode(IntEnum):
    """Process exit codes emitted by the CLI."""

    SUCCESS = 0
    FAILURE = 1
    INVALID_INPUT = 2
    MISSING_DEPENDENCY = 3
    CANCELLED = 130


class ItemStatus(StrEnum):
    """Outcome status for an individual media item."""

    SUCCEEDED = "succeeded"
    SKIPPED_EXISTS = "skipped_exists"
    SKIPPED_UNSUPPORTED = "skipped_unsupported"
    FAILED = "failed"
    PLANNED = "planned"


DEFAULT_AUDIO_BITRATE: int = 192
MIN_AUDIO_BITRATE: int = 64
MAX_AUDIO_BITRATE: int = 320

DEFAULT_MAX_HEIGHT: int = 1080
MIN_VIDEO_HEIGHT: int = 144
MAX_VIDEO_HEIGHT: int = 4320

DEFAULT_OUTPUT_DIR: Path = Path.home() / "Downloads" / "unduhin"
DEFAULT_FILENAME_TEMPLATE: str = "{title}.{ext}"
MISSING_TEMPLATE_FIELD_PLACEHOLDER: str = "unknown"
MAX_FINAL_PATH_LENGTH: int = 240
MIN_FILENAME_ROOM: int = 16


def get_default_output_dir() -> Path:
    """Return the default download folder inside the user's Downloads directory."""
    return Path.home() / "Downloads" / "unduhin"

DEFAULT_MAX_RETRIES: int = 3
DEFAULT_BACKOFF_BASE_SECONDS: float = 1.5
DEFAULT_RATE_LIMIT_BACKOFF_SECONDS: float = 10.0
MAX_TOTAL_SLEEP_PER_ITEM_SECONDS: float = 120.0
CONSECUTIVE_RATE_LIMIT_BREAKER: int = 3

MIN_FREE_DISK_BYTES: int = 50 * 1024 * 1024  # 50 MiB safety floor
CONSERVATIVE_UNKNOWN_SIZE_BYTES: int = 200 * 1024 * 1024  # 200 MiB when filesize unknown
STAGING_DIR_PREFIX: str = ".unduhin-tmp-"
STALE_STAGING_AGE_SECONDS: float = 3600.0  # 1 hour fallback threshold
STAGING_CLEANUP_RETRY_DELAYS: tuple[float, ...] = (0.2, 0.5, 1.0)

DEFAULT_MAX_PLAYLIST_ITEMS: int = 50
MAX_PLAYLIST_ITEMS_CAP: int = 200
MAX_PLAYLIST_NESTING_DEPTH: int = 2
MAX_TOTAL_ITEMS_GLOBAL: int = 500

MAX_BATCH_FILE_BYTES: int = 1024 * 1024  # 1 MiB
MAX_BATCH_URLS: int = 500

MAX_UI_URL_LENGTH: int = 2048
DEFAULT_UI_PORT: int = 7860

ALLOWED_TEMPLATE_FIELDS: frozenset[str] = frozenset(
    {"title", "id", "uploader", "upload_date", "ext"}
)

MODE_EXTENSION_MAP: dict[MediaMode, str] = {
    MediaMode.AUDIO: ".mp3",
    MediaMode.VIDEO: ".mp4",
}

LEGAL_NOTICE_EN: str = (
    "Legal Notice: Use this tool only for content you own, content under open/Creative "
    "Commons licenses, public domain works, or with explicit permission from the rights "
    "holder. Users are responsible for complying with platform terms and local copyright law."
)

LEGAL_NOTICE_ID: str = (
    "Catatan Legal: Gunakan alat ini hanya untuk konten milik sendiri, berlisensi "
    "terbuka/Creative Commons, domain publik, atau dengan izin pemilik hak cipta. "
    "Pengguna bertanggung jawab penuh atas kepatuhan terhadap syarat layanan platform dan "
    "hukum hak cipta setempat."
)


@dataclass(frozen=True, slots=True)
class FormatOption:
    """Summary of an available stream format from media metadata."""

    format_id: str
    ext: str
    height: int | None = None
    vcodec: str | None = None
    acodec: str | None = None
    abr: float | None = None
    filesize_approx: int | None = None


@dataclass(frozen=True, slots=True)
class MediaInfo:
    """Normalized metadata for a media item or playlist."""

    id: str
    title: str
    url: str
    webpage_url: str | None = None
    uploader: str | None = None
    upload_date: str | None = None
    duration_seconds: float | None = None
    is_live: bool = False
    live_status: str | None = None
    is_drm: bool = False
    is_unavailable: bool = False
    filesize_approx: int | None = None
    is_playlist: bool = False
    playlist_count: int | None = None
    entries: tuple[MediaInfo, ...] = ()
    formats: tuple[FormatOption, ...] = ()


@dataclass(frozen=True, slots=True)
class DownloadRequest:
    """Validated parameters for a download, dry-run, or batch execution."""

    urls: tuple[str, ...]
    mode: MediaMode = MediaMode.AUDIO
    output_dir: Path = field(default_factory=get_default_output_dir)
    audio_bitrate: int = DEFAULT_AUDIO_BITRATE
    max_height: int = DEFAULT_MAX_HEIGHT
    filename_template: str = DEFAULT_FILENAME_TEMPLATE
    conflict_policy: ConflictPolicy = ConflictPolicy.RENAME
    allow_playlist: bool = False
    max_items: int = DEFAULT_MAX_PLAYLIST_ITEMS
    dry_run: bool = False
    max_retries: int = DEFAULT_MAX_RETRIES
    backoff_base_seconds: float = DEFAULT_BACKOFF_BASE_SECONDS
    rate_limit_backoff_seconds: float = DEFAULT_RATE_LIMIT_BACKOFF_SECONDS
    max_total_items: int = MAX_TOTAL_ITEMS_GLOBAL
    lang: str | None = None
    verbose: bool = False


@dataclass(frozen=True, slots=True)
class AdapterDownloadResult:
    """Result returned by the media adapter after downloading into staging."""

    filepath: Path
    info: MediaInfo
    codec_fallback_used: bool = False
    vcodec: str | None = None
    acodec: str | None = None
    is_audio_only_video: bool = False


@dataclass(frozen=True, slots=True)
class ItemResult:
    """Execution outcome for a single requested or expanded media item."""

    url: str
    status: ItemStatus
    title: str = ""
    output_path: Path | None = None
    message: str | None = None
    warnings: tuple[str, ...] = ()
    is_batch_renamed: bool = False


@dataclass(slots=True)
class BatchSummary:
    """Aggregated outcome across all items processed in a run."""

    results: list[ItemResult] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    interrupted: bool = False
    had_validation_error: bool = False
    had_dependency_error: bool = False
    had_runtime_failure: bool = False

    def add(self, result: ItemResult) -> None:
        """Append an item result to the summary."""
        self.results.append(result)

    def add_warning(self, warning: str) -> None:
        """Record a user-facing warning message."""
        if warning not in self.warnings:
            self.warnings.append(warning)

    @property
    def succeeded_count(self) -> int:
        """Return count of successfully downloaded items."""
        return sum(1 for r in self.results if r.status is ItemStatus.SUCCEEDED)

    @property
    def failed_count(self) -> int:
        """Return count of failed items."""
        return sum(1 for r in self.results if r.status is ItemStatus.FAILED)

    @property
    def skipped_exists_count(self) -> int:
        """Return count of items benignly skipped because the destination exists."""
        return sum(1 for r in self.results if r.status is ItemStatus.SKIPPED_EXISTS)

    @property
    def skipped_unsupported_count(self) -> int:
        """Return count of unsupported or unavailable skipped items (live/upcoming/unavailable)."""
        return sum(1 for r in self.results if r.status is ItemStatus.SKIPPED_UNSUPPORTED)

    @property
    def skipped_count(self) -> int:
        """Return total count of skipped items across exists and unsupported categories."""
        return self.skipped_exists_count + self.skipped_unsupported_count

    @property
    def planned_count(self) -> int:
        """Return count of dry-run planned items."""
        return sum(1 for r in self.results if r.status is ItemStatus.PLANNED)

    @property
    def exit_code(self) -> ExitCode:
        """Compute the meaningful CLI exit code with strict precedence 130 > 3 > 2 > 1 > 0."""
        if self.interrupted:
            return ExitCode.CANCELLED
        if self.had_dependency_error:
            return ExitCode.MISSING_DEPENDENCY
        if (
            self.had_validation_error
            and self.succeeded_count == 0
            and self.skipped_count == 0
            and not self.had_runtime_failure
        ):
            return ExitCode.INVALID_INPUT
        if self.failed_count > 0 or self.skipped_unsupported_count > 0:
            return ExitCode.FAILURE
        return ExitCode.SUCCESS
