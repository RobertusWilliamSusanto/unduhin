"""Core media download orchestration, inspection, staging, retry, and cleanup pipeline."""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from unduhin.config import (
    CONSECUTIVE_RATE_LIMIT_BREAKER,
    CONSERVATIVE_UNKNOWN_SIZE_BYTES,
    DEFAULT_MAX_PLAYLIST_ITEMS,
    MAX_AUDIO_BITRATE,
    MAX_PLAYLIST_ITEMS_CAP,
    MAX_PLAYLIST_NESTING_DEPTH,
    MAX_TOTAL_SLEEP_PER_ITEM_SECONDS,
    MAX_VIDEO_HEIGHT,
    MIN_AUDIO_BITRATE,
    MIN_VIDEO_HEIGHT,
    AdapterDownloadResult,
    BatchSummary,
    DownloadRequest,
    FormatOption,
    ItemResult,
    ItemStatus,
    MediaInfo,
    MediaMode,
)
from unduhin.doctor import find_deno, find_ffmpeg
from unduhin.errors import (
    CancelledError,
    DependencyError,
    LiveStreamError,
    OutdatedExtractorError,
    PlaylistDisabledError,
    RateLimitError,
    TransientHttp403Error,
    TransientNetworkError,
    UnduhinError,
    ValidationError,
    classify_exception,
)
from unduhin.i18n import t
from unduhin.validators import (
    assert_allowed_extension,
    assert_within_directory,
    cleanup_path_with_retry,
    cleanup_stale_staging_dirs,
    create_staging_dir,
    ensure_safe_output_dir,
    fit_filename_to_path_limit,
    move_staged_file,
    render_allowlisted_filename,
    reserve_destination_detailed,
    template_fields_available,
    validate_output_dir_path,
    validate_template,
    validate_url,
    verify_disk_space,
)
from unduhin.ytdlp_adapter import (
    MediaAdapter,
    ProgressCallback,
    ProgressEvent,
    is_h264_aac_compatible,
)

_YOUTUBE_HOSTS: frozenset[str] = frozenset(
    {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtu.be",
        "www.youtu.be",
        "youtube-nocookie.com",
        "www.youtube-nocookie.com",
    }
)


@dataclass(slots=True)
class PipelineHooks:
    """Callbacks connecting pipeline execution to a CLI or UI frontend."""

    on_progress: ProgressCallback | None = None
    on_warning: Callable[[str], None] | None = None
    on_retry: Callable[[int, int, float, str], None] | None = None
    is_cancelled: Callable[[], bool] = field(default=lambda: False)
    sleep_fn: Callable[[float], None] = field(default=time.sleep)

    def emit_progress(self, event: ProgressEvent) -> None:
        """Forward a progress event if a listener is attached."""
        if self.on_progress is not None:
            self.on_progress(event)

    def emit_warning(self, message: str) -> None:
        """Forward a warning message if a listener is attached."""
        if self.on_warning is not None:
            self.on_warning(message)

    def emit_retry(self, attempt: int, max_retries: int, delay: float, reason: str) -> None:
        """Forward a retry notification if a listener is attached."""
        if self.on_retry is not None:
            self.on_retry(attempt, max_retries, delay, reason)


@dataclass(frozen=True, slots=True)
class InspectionResult:
    """Normalized metadata result returned for ``unduhin info``."""

    info: MediaInfo
    items: tuple[MediaInfo, ...]
    warnings: tuple[str, ...]
    selected_format_summary: str


def looks_like_youtube_url(url: str) -> bool:
    """Return True if ``url`` points to a known YouTube hostname."""
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    return host in _YOUTUBE_HOSTS or host.endswith(".youtube.com")


def _validate_common_options(
    *,
    audio_bitrate: int,
    max_height: int,
    max_retries: int,
    max_items: int,
) -> None:
    if not (MIN_AUDIO_BITRATE <= audio_bitrate <= MAX_AUDIO_BITRATE):
        raise ValidationError(
            "cli.invalid_option",
            detail=f"audio bitrate must be between {MIN_AUDIO_BITRATE} and {MAX_AUDIO_BITRATE} kbps",
        )
    if not (MIN_VIDEO_HEIGHT <= max_height <= MAX_VIDEO_HEIGHT):
        raise ValidationError(
            "cli.invalid_option",
            detail=f"max height must be between {MIN_VIDEO_HEIGHT} and {MAX_VIDEO_HEIGHT} px",
        )
    if max_retries < 0:
        raise ValidationError(
            "cli.invalid_option",
            detail="max retries cannot be negative",
        )
    if not (1 <= max_items <= MAX_PLAYLIST_ITEMS_CAP):
        raise ValidationError(
            "cli.invalid_option",
            detail=f"max-items must be between 1 and {MAX_PLAYLIST_ITEMS_CAP}",
        )


def _validate_request_inputs(request: DownloadRequest) -> tuple[Path, tuple[str, ...]]:
    """Validate all request parameters, template, output path, and URLs before touching disk."""
    if not request.urls:
        raise ValidationError("cli.missing_url")
    _validate_common_options(
        audio_bitrate=request.audio_bitrate,
        max_height=request.max_height,
        max_retries=request.max_retries,
        max_items=request.max_items,
    )
    if request.max_total_items < 1:
        raise ValidationError(
            "cli.invalid_option",
            detail="max_total_items must be at least 1",
        )
    validate_template(request.filename_template)
    resolved_output_dir = validate_output_dir_path(request.output_dir)
    validated_urls = tuple(validate_url(raw_url) for raw_url in request.urls)
    return resolved_output_dir, validated_urls


validate_request_inputs = _validate_request_inputs


def _sleep_interruptible(
    seconds: float,
    *,
    is_cancelled: Callable[[], bool],
    sleep_fn: Callable[[float], None],
) -> None:
    """Sleep in short slices so Ctrl+C cancels backoff promptly."""
    remaining = max(0.0, seconds)
    while remaining > 0.0:
        if is_cancelled():
            raise CancelledError("error.cancelled")
        step = min(0.1, remaining)
        sleep_fn(step)
        remaining -= step


def _download_with_retry(
    *,
    adapter: MediaAdapter,
    url: str,
    request: DownloadRequest,
    staging_dir: Path,
    output_dir: Path,
    hooks: PipelineHooks,
) -> AdapterDownloadResult:
    """Execute download with bounded exponential backoff on transient errors."""
    attempt = 0
    total_slept = 0.0
    while True:
        if hooks.is_cancelled():
            raise CancelledError("error.cancelled")
        try:
            return adapter.download(
                url=url,
                mode=request.mode,
                audio_bitrate=request.audio_bitrate,
                max_height=request.max_height,
                staging_dir=staging_dir,
                allow_playlist=False,
                verbose=request.verbose,
                progress_callback=hooks.on_progress,
                cancel_check=hooks.is_cancelled,
            )
        except (KeyboardInterrupt, CancelledError):
            raise
        except Exception as raw_exc:
            mapped = classify_exception(raw_exc, output_dir=output_dir)
            if isinstance(mapped, CancelledError):
                raise mapped from raw_exc
            if isinstance(mapped, TransientNetworkError) and attempt < request.max_retries:
                base = (
                    request.rate_limit_backoff_seconds
                    if isinstance(mapped, RateLimitError)
                    else request.backoff_base_seconds
                )
                jitter = random.uniform(0.0, 0.5)  # noqa: S311
                delay = base * (2**attempt) + jitter
                if total_slept + delay <= MAX_TOTAL_SLEEP_PER_ITEM_SECONDS:
                    hooks.emit_retry(
                        attempt + 1,
                        request.max_retries,
                        delay,
                        mapped.format(request.lang),
                    )
                    _sleep_interruptible(
                        delay,
                        is_cancelled=hooks.is_cancelled,
                        sleep_fn=hooks.sleep_fn,
                    )
                    total_slept += delay
                    attempt += 1
                    continue
            if isinstance(mapped, TransientHttp403Error):
                raise OutdatedExtractorError("error.ytdlp_outdated") from raw_exc
            raise mapped from raw_exc


def _cleanup_staging_dir(
    staging_dir: Path,
    *,
    lang: str | None,
    summary: BatchSummary,
    hooks: PipelineHooks,
) -> None:
    """Remove an item staging directory (with retries) and surface persistent failures as warnings."""
    warning = cleanup_path_with_retry(
        staging_dir,
        lang=lang,
        sleep_fn=hooks.sleep_fn,
    )
    if warning is not None:
        summary.add_warning(warning)
        hooks.emit_warning(warning)


def _merge_media_info(primary: MediaInfo, fallback: MediaInfo) -> MediaInfo:
    """Prefer complete post-download metadata while falling back to pre-download values."""
    return MediaInfo(
        id=primary.id or fallback.id,
        title=primary.title or fallback.title,
        url=primary.url or fallback.url,
        webpage_url=primary.webpage_url or fallback.webpage_url,
        uploader=primary.uploader or fallback.uploader,
        upload_date=primary.upload_date or fallback.upload_date,
        duration_seconds=(
            primary.duration_seconds
            if primary.duration_seconds is not None
            else fallback.duration_seconds
        ),
        is_live=primary.is_live,
        live_status=primary.live_status or fallback.live_status,
        is_drm=primary.is_drm,
        is_unavailable=primary.is_unavailable,
        filesize_approx=(
            primary.filesize_approx
            if primary.filesize_approx is not None
            else fallback.filesize_approx
        ),
        is_playlist=primary.is_playlist,
        playlist_count=(
            primary.playlist_count if primary.playlist_count is not None else fallback.playlist_count
        ),
        entries=primary.entries or fallback.entries,
        formats=primary.formats or fallback.formats,
    )


def _record_rate_limit_breaker(
    summary: BatchSummary,
    *,
    remaining_items: list[MediaInfo],
    remaining_urls: tuple[str, ...],
    reason: str,
    fallback_url: str,
) -> None:
    """Record leftover batch entries as FAILED after consecutive HTTP 429 limit breaks."""
    summary.had_runtime_failure = True
    for rem in remaining_items:
        summary.add(
            ItemResult(
                url=rem.webpage_url or rem.url or fallback_url,
                status=ItemStatus.FAILED,
                title=rem.title,
                message=reason,
            )
        )
    for rem_url in remaining_urls:
        summary.add(
            ItemResult(
                url=rem_url,
                status=ItemStatus.FAILED,
                message=reason,
            )
        )


def _record_item_outcome_from_exception(
    summary: BatchSummary,
    *,
    url: str,
    exc: UnduhinError,
    lang: str | None,
    title: str = "",
) -> None:
    """Record a non-cancelled ``UnduhinError`` as SKIPPED_UNSUPPORTED or FAILED."""
    if isinstance(exc, LiveStreamError):
        summary.add(
            ItemResult(
                url=url,
                status=ItemStatus.SKIPPED_UNSUPPORTED,
                title=title,
                message=exc.format(lang),
            )
        )
        return
    if isinstance(exc, ValidationError):
        summary.had_validation_error = True
    else:
        summary.had_runtime_failure = True
    if isinstance(exc, DependencyError):
        summary.had_dependency_error = True
    summary.add(
        ItemResult(
            url=url,
            status=ItemStatus.FAILED,
            title=title,
            message=exc.format(lang),
        )
    )


def expand_playlist_items(
    info: MediaInfo,
    *,
    allow_playlist: bool,
    max_items: int,
    lang: str | None = None,
) -> tuple[list[MediaInfo], list[str]]:
    """Expand and flatten playlist items (max depth 2) and enforce ``max_items`` cap."""
    if not info.is_playlist:
        return [info], []

    if not allow_playlist:
        raise PlaylistDisabledError("error.playlist_disabled")

    flattened: list[MediaInfo] = []
    depth_exceeded = False

    def _walk(node: MediaInfo, depth: int) -> None:
        nonlocal depth_exceeded
        for child in node.entries:
            if child.is_playlist:
                if depth >= MAX_PLAYLIST_NESTING_DEPTH:
                    depth_exceeded = True
                    continue
                if child.entries:
                    _walk(child, depth + 1)
                else:
                    # Empty sub-playlist leaf cannot yield playable items without N+1 calls.
                    depth_exceeded = True
                continue
            flattened.append(child)

    _walk(info, depth=1)

    warnings: list[str] = []
    if depth_exceeded:
        warnings.append(
            t("warn.playlist_depth_exceeded", lang=lang, max_depth=MAX_PLAYLIST_NESTING_DEPTH)
        )

    reported_total = info.playlist_count or len(flattened)
    if reported_total < len(flattened):
        reported_total = len(flattened)

    if len(flattened) > max_items or (
        info.playlist_count is not None and info.playlist_count > max_items
    ):
        warnings.append(
            t("warn.playlist_truncated", lang=lang, max_items=max_items, total=reported_total)
        )

    return flattened[:max_items], warnings


def summarize_selected_format(
    info: MediaInfo,
    *,
    mode: MediaMode,
    max_height: int,
    audio_bitrate: int = 192,
    lang: str | None = None,
) -> str:
    """Summarize the likely target format from extracted metadata."""
    if mode is MediaMode.AUDIO:
        audio_fmts = [f for f in info.formats if f.acodec and f.acodec != "none"]
        if audio_fmts:
            best_audio = max(audio_fmts, key=lambda f: f.abr or 0.0)
            abr_text = f" (~{int(best_audio.abr)} kbps source)" if best_audio.abr else ""
            return f"MP3 {audio_bitrate} kbps [{best_audio.acodec}{abr_text}]"
        return f"MP3 {audio_bitrate} kbps"

    video_fmts: list[FormatOption] = [
        f
        for f in info.formats
        if f.vcodec and f.vcodec != "none" and (f.height is None or f.height <= max_height)
    ]
    if not video_fmts:
        return f"MP4 <= {max_height}p"

    # Prefer H.264 streams if present, matching download format_sort behavior.
    h264_fmts = [f for f in video_fmts if is_h264_aac_compatible(f.vcodec, "aac")]
    pool = h264_fmts or video_fmts
    best_video = max(pool, key=lambda f: f.height or 0)
    height_label = f"{best_video.height}p" if best_video.height else f"<={max_height}p"
    codec_label = best_video.vcodec or t("cli.info_unknown", lang=lang)
    return f"MP4 {height_label} ({codec_label})"


def inspect_url(
    raw_url: str,
    adapter: MediaAdapter,
    *,
    mode: MediaMode = MediaMode.AUDIO,
    audio_bitrate: int = 192,
    max_height: int = 1080,
    allow_playlist: bool = False,
    max_items: int = DEFAULT_MAX_PLAYLIST_ITEMS,
    lang: str | None = None,
    verbose: bool = False,
) -> InspectionResult:
    """Inspect metadata and available formats without downloading or requiring FFmpeg."""
    _validate_common_options(
        audio_bitrate=audio_bitrate,
        max_height=max_height,
        max_retries=0,
        max_items=max_items,
    )
    validated = validate_url(raw_url)
    warnings: list[str] = []
    if looks_like_youtube_url(validated) and not find_deno():
        warnings.append(t("warn.deno_missing", lang=lang))

    try:
        info = adapter.extract_info(
            validated,
            allow_playlist=allow_playlist,
            max_items=max_items,
            verbose=verbose,
        )
    except (KeyboardInterrupt, CancelledError) as exc:
        raise CancelledError("error.cancelled") from exc
    except Exception as raw_exc:
        raise classify_exception(raw_exc) from raw_exc

    items, pl_warnings = expand_playlist_items(
        info,
        allow_playlist=allow_playlist,
        max_items=max_items,
        lang=lang,
    )
    warnings.extend(pl_warnings)

    fmt_summary = summarize_selected_format(
        info,
        mode=mode,
        max_height=max_height,
        audio_bitrate=audio_bitrate,
        lang=lang,
    )
    return InspectionResult(
        info=info,
        items=tuple(items),
        warnings=tuple(warnings),
        selected_format_summary=fmt_summary,
    )


def run_download(
    request: DownloadRequest,
    adapter: MediaAdapter,
    hooks: PipelineHooks | None = None,
) -> BatchSummary:
    """Run the validated download (or dry-run) pipeline for one or more URLs."""
    active_hooks = hooks or PipelineHooks()
    summary = BatchSummary()

    # 1. Fail-fast input validation BEFORE any directory creation or disk cleanup.
    resolved_output_dir, validated_urls = _validate_request_inputs(request)

    # 2. Check FFmpeg and perform disk side-effects ONLY for real downloads (not --dry-run).
    if not request.dry_run:
        if not find_ffmpeg():
            raise DependencyError("error.ffmpeg_missing")
        output_dir = ensure_safe_output_dir(resolved_output_dir)
        for stale_warning in cleanup_stale_staging_dirs(
            output_dir,
            lang=request.lang,
            sleep_fn=active_hooks.sleep_fn,
        ):
            summary.add_warning(stale_warning)
            active_hooks.emit_warning(stale_warning)
    else:
        output_dir = resolved_output_dir

    if any(looks_like_youtube_url(u) for u in validated_urls) and not find_deno():
        deno_warning = t("warn.deno_missing", lang=request.lang)
        summary.add_warning(deno_warning)
        active_hooks.emit_warning(deno_warning)

    reserved_names: set[str] = set()
    total_emitted = 0
    unknown_filesize_count = 0
    consecutive_rate_limits = 0
    breaker_tripped = False
    global_cap_warned = False

    def _emit_global_cap_warning() -> None:
        nonlocal global_cap_warned
        if not global_cap_warned:
            global_cap_warned = True
            cap_warn = t(
                "warn.global_item_limit_reached",
                lang=request.lang,
                max_items=request.max_total_items,
            )
            summary.add_warning(cap_warn)
            active_hooks.emit_warning(cap_warn)

    for url_idx, url in enumerate(validated_urls):
        if active_hooks.is_cancelled():
            summary.interrupted = True
            break
        if total_emitted >= request.max_total_items:
            _emit_global_cap_warning()
            break

        try:
            extracted = adapter.extract_info(
                url,
                allow_playlist=request.allow_playlist,
                max_items=request.max_items,
                verbose=request.verbose,
            )
            items, playlist_warnings = expand_playlist_items(
                extracted,
                allow_playlist=request.allow_playlist,
                max_items=request.max_items,
                lang=request.lang,
            )
            for pl_warn in playlist_warnings:
                summary.add_warning(pl_warn)
                active_hooks.emit_warning(pl_warn)

            remaining_global = request.max_total_items - total_emitted
            if len(items) > remaining_global:
                items = items[:remaining_global]
                _emit_global_cap_warning()
            total_emitted += len(items)

            for item_idx, item in enumerate(items):
                if active_hooks.is_cancelled():
                    raise CancelledError("error.cancelled")

                if item.is_unavailable:
                    consecutive_rate_limits = 0
                    summary.add(
                        ItemResult(
                            url=item.webpage_url or item.url or url,
                            status=ItemStatus.SKIPPED_UNSUPPORTED,
                            title=item.title or "unavailable",
                            message=t("error.playlist_entry_unavailable", lang=request.lang),
                        )
                    )
                    continue

                if item.is_live or item.live_status in {"is_live", "is_upcoming"}:
                    consecutive_rate_limits = 0
                    summary.add(
                        ItemResult(
                            url=item.webpage_url or item.url or url,
                            status=ItemStatus.SKIPPED_UNSUPPORTED,
                            title=item.title,
                            message=t("error.live_stream_unsupported", lang=request.lang),
                        )
                    )
                    continue

                if item.is_drm:
                    consecutive_rate_limits = 0
                    summary.had_runtime_failure = True
                    summary.add(
                        ItemResult(
                            url=item.webpage_url or item.url or url,
                            status=ItemStatus.FAILED,
                            title=item.title,
                            message=t("error.drm_protected", lang=request.lang),
                        )
                    )
                    continue

                fields_complete = template_fields_available(item, request.filename_template)
                early_decision = None
                if fields_complete:
                    early_name = fit_filename_to_path_limit(
                        output_dir,
                        render_allowlisted_filename(
                            item,
                            request.filename_template,
                            request.mode,
                        ),
                    )
                    early_decision = reserve_destination_detailed(
                        output_dir,
                        early_name,
                        request.conflict_policy,
                        reserved_names,
                    )
                    assert_within_directory(early_decision.path, output_dir)
                    if early_decision.should_skip:
                        consecutive_rate_limits = 0
                        summary.add(
                            ItemResult(
                                url=item.webpage_url or item.url or url,
                                status=ItemStatus.SKIPPED_EXISTS,
                                title=item.title,
                                output_path=early_decision.path,
                                message=t(
                                    "status.skipped_exists",
                                    lang=request.lang,
                                    path=early_decision.path,
                                ),
                            )
                        )
                        continue

                if request.dry_run:
                    consecutive_rate_limits = 0
                    if early_decision is None:
                        preview_name = fit_filename_to_path_limit(
                            output_dir,
                            render_allowlisted_filename(
                                item,
                                request.filename_template,
                                request.mode,
                            ),
                        )
                        preview_decision = reserve_destination_detailed(
                            output_dir,
                            preview_name,
                            request.conflict_policy,
                            reserved_names,
                        )
                    else:
                        preview_decision = early_decision
                    assert_within_directory(preview_decision.path, output_dir)
                    summary.add(
                        ItemResult(
                            url=item.webpage_url or item.url or url,
                            status=ItemStatus.PLANNED,
                            title=item.title,
                            output_path=preview_decision.path,
                            is_batch_renamed=preview_decision.is_batch_duplicate,
                        )
                    )
                    continue

                item_url = validate_url(item.webpage_url or item.url or url)
                active_reserved_key = early_decision.reserved_key if early_decision else None

                try:
                    if verify_disk_space(output_dir, estimated_bytes=item.filesize_approx):
                        unknown_filesize_count += 1

                    staging_dir = create_staging_dir(output_dir)
                    try:
                        dl_result = _download_with_retry(
                            adapter=adapter,
                            url=item_url,
                            request=request,
                            staging_dir=staging_dir,
                            output_dir=output_dir,
                            hooks=active_hooks,
                        )

                        staged_file = dl_result.filepath
                        assert_within_directory(staged_file, staging_dir)
                        assert_allowed_extension(staged_file, request.mode)

                        complete_info = _merge_media_info(dl_result.info, fallback=item)
                        final_name = fit_filename_to_path_limit(
                            output_dir,
                            render_allowlisted_filename(
                                complete_info,
                                request.filename_template,
                                request.mode,
                            ),
                        )
                        if (
                            early_decision is not None
                            and final_name.casefold() == early_decision.path.name.casefold()
                        ):
                            planned_path = early_decision.path
                            is_batch_renamed = early_decision.is_batch_duplicate
                        else:
                            if active_reserved_key is not None:
                                reserved_names.discard(active_reserved_key)
                                active_reserved_key = None
                            final_decision = reserve_destination_detailed(
                                output_dir,
                                final_name,
                                request.conflict_policy,
                                reserved_names,
                            )
                            assert_within_directory(final_decision.path, output_dir)
                            if final_decision.should_skip:
                                staged_file.unlink(missing_ok=True)
                                consecutive_rate_limits = 0
                                summary.add(
                                    ItemResult(
                                        url=item_url,
                                        status=ItemStatus.SKIPPED_EXISTS,
                                        title=complete_info.title or item.title,
                                        output_path=final_decision.path,
                                        message=t(
                                            "status.skipped_exists",
                                            lang=request.lang,
                                            path=final_decision.path,
                                        ),
                                    )
                                )
                                continue
                            active_reserved_key = final_decision.reserved_key
                            planned_path = final_decision.path
                            is_batch_renamed = final_decision.is_batch_duplicate

                        item_warnings: list[str] = []
                        if dl_result.is_audio_only_video:
                            audio_only_warn = t("warn.mp4_audio_only", lang=request.lang)
                            item_warnings.append(audio_only_warn)
                            summary.add_warning(audio_only_warn)
                            active_hooks.emit_warning(audio_only_warn)
                        if dl_result.codec_fallback_used:
                            codec_warn = t(
                                "warn.mp4_codec_fallback",
                                lang=request.lang,
                                vcodec=dl_result.vcodec or "unknown",
                                acodec=dl_result.acodec or "unknown",
                            )
                            item_warnings.append(codec_warn)
                            summary.add_warning(codec_warn)
                            active_hooks.emit_warning(codec_warn)

                        final_path, was_moved = move_staged_file(
                            staged_file,
                            planned_path,
                            output_dir,
                            request.conflict_policy,
                            reserved_names,
                        )
                        if not was_moved:
                            if active_reserved_key is not None:
                                reserved_names.discard(active_reserved_key)
                            consecutive_rate_limits = 0
                            summary.add(
                                ItemResult(
                                    url=item_url,
                                    status=ItemStatus.SKIPPED_EXISTS,
                                    title=complete_info.title or item.title,
                                    output_path=final_path,
                                    message=t(
                                        "status.skipped_exists",
                                        lang=request.lang,
                                        path=final_path,
                                    ),
                                    warnings=tuple(item_warnings),
                                )
                            )
                        else:
                            consecutive_rate_limits = 0
                            summary.add(
                                ItemResult(
                                    url=item_url,
                                    status=ItemStatus.SUCCEEDED,
                                    title=complete_info.title or item.title,
                                    output_path=final_path,
                                    warnings=tuple(item_warnings),
                                    is_batch_renamed=is_batch_renamed,
                                )
                            )
                    finally:
                        _cleanup_staging_dir(
                            staging_dir,
                            lang=request.lang,
                            summary=summary,
                            hooks=active_hooks,
                        )
                except (KeyboardInterrupt, CancelledError):
                    if active_reserved_key is not None:
                        reserved_names.discard(active_reserved_key)
                    raise
                except Exception as raw_exc:
                    if active_reserved_key is not None:
                        reserved_names.discard(active_reserved_key)
                    mapped = classify_exception(raw_exc, output_dir=output_dir)
                    if isinstance(mapped, CancelledError):
                        raise mapped from raw_exc
                    _record_item_outcome_from_exception(
                        summary,
                        url=item_url,
                        exc=mapped,
                        lang=request.lang,
                        title=item.title,
                    )
                    if isinstance(mapped, RateLimitError):
                        consecutive_rate_limits += 1
                        if consecutive_rate_limits >= CONSECUTIVE_RATE_LIMIT_BREAKER:
                            breaker_reason = t(
                                "error.rate_limit_circuit_breaker",
                                lang=request.lang,
                                count=CONSECUTIVE_RATE_LIMIT_BREAKER,
                            )
                            summary.add_warning(breaker_reason)
                            active_hooks.emit_warning(breaker_reason)
                            _record_rate_limit_breaker(
                                summary,
                                remaining_items=items[item_idx + 1 :],
                                remaining_urls=validated_urls[url_idx + 1 :],
                                reason=breaker_reason,
                                fallback_url=url,
                            )
                            breaker_tripped = True
                            break
                    else:
                        consecutive_rate_limits = 0

            if breaker_tripped:
                break

        except (KeyboardInterrupt, CancelledError):
            # Must precede UnduhinError so user cancellation aborts the entire batch (exit 130).
            summary.interrupted = True
            break
        except Exception as raw_exc:
            mapped = classify_exception(raw_exc, output_dir=output_dir)
            if isinstance(mapped, CancelledError):
                summary.interrupted = True
                break
            _record_item_outcome_from_exception(
                summary,
                url=url,
                exc=mapped,
                lang=request.lang,
            )
            if isinstance(mapped, RateLimitError):
                consecutive_rate_limits += 1
                if consecutive_rate_limits >= CONSECUTIVE_RATE_LIMIT_BREAKER:
                    breaker_reason = t(
                        "error.rate_limit_circuit_breaker",
                        lang=request.lang,
                        count=CONSECUTIVE_RATE_LIMIT_BREAKER,
                    )
                    summary.add_warning(breaker_reason)
                    active_hooks.emit_warning(breaker_reason)
                    _record_rate_limit_breaker(
                        summary,
                        remaining_items=[],
                        remaining_urls=validated_urls[url_idx + 1 :],
                        reason=breaker_reason,
                        fallback_url=url,
                    )
                    break
            else:
                consecutive_rate_limits = 0

    if unknown_filesize_count > 0:
        unknown_warn = t(
            "warn.unknown_filesize_batch",
            lang=request.lang,
            count=unknown_filesize_count,
            min_mb=CONSERVATIVE_UNKNOWN_SIZE_BYTES // (1024 * 1024),
        )
        summary.add_warning(unknown_warn)
        active_hooks.emit_warning(unknown_warn)

    return summary
