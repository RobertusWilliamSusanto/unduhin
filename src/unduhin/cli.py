"""Typer CLI frontend for unduhin."""

from __future__ import annotations

import signal
import sys
import threading
import traceback
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer
import typer.core

from unduhin import __version__
from unduhin.config import (
    DEFAULT_AUDIO_BITRATE,
    DEFAULT_FILENAME_TEMPLATE,
    DEFAULT_MAX_HEIGHT,
    DEFAULT_MAX_PLAYLIST_ITEMS,
    DEFAULT_UI_PORT,
    LEGAL_NOTICE_EN,
    LEGAL_NOTICE_ID,
    ConflictPolicy,
    DownloadRequest,
    ExitCode,
    ItemStatus,
    MediaMode,
)
from unduhin.i18n import detect_language, safe_console_text, t

if TYPE_CHECKING:
    from rich.console import Console

    from unduhin.ytdlp_adapter import MediaAdapter, ProgressEvent

_LEGAL_EPILOG = f"{LEGAL_NOTICE_EN}\n\n{LEGAL_NOTICE_ID}"


class _ShortcutGroup(typer.core.TyperGroup):
    """Typer command group directing a non-option URL first argument to ``download``."""

    def resolve_command(
        self, ctx: Any, args: list[str]
    ) -> tuple[str | None, Any, list[str]]:
        if args:
            first = args[0]
            if not first.startswith("-") and first not in self.commands:
                args = ["download", *args]
        return super().resolve_command(ctx, args)


app = typer.Typer(
    name="unduhin",
    cls=_ShortcutGroup,
    invoke_without_command=True,
    no_args_is_help=False,
    add_completion=False,
    pretty_exceptions_enable=False,
    pretty_exceptions_show_locals=False,
    rich_markup_mode=None,
    help=(
        "Local-first media downloader and converter (MP3 / MP4).\n\n"
        "Shortcut: `unduhin <URL> [OPTIONS]` routes directly to `download` "
        "(place options after the URL).\n\n"
        f"{_LEGAL_EPILOG}"
    ),
)


def _get_consoles() -> tuple[Console, Console]:
    """Lazily import Rich and return ``(stdout_console, stderr_console)``."""
    from rich.console import Console

    return Console(soft_wrap=True), Console(stderr=True, soft_wrap=True)


def _safe(value: object, stream: Any) -> str:
    """Convert ``value`` to a string safe for ``stream`` encoding without crashing."""
    return safe_console_text(str(value), encoding=getattr(stream, "encoding", None))


def _print_err(err_console: Console, text: str, *, style: str = "red") -> None:
    """Print error or warning text safely across non-UTF-8 or redirected consoles."""
    err_console.print(_safe(text, err_console.file), style=style, markup=False)


def _create_adapter(verbose: bool, console: Console) -> MediaAdapter:
    """Lazily instantiate the default yt-dlp adapter (monkeypatchable in tests)."""
    from unduhin.ytdlp_adapter import YtDlpAdapter

    return YtDlpAdapter(
        log_callback=(lambda msg: console.print(_safe(msg, console.file), style="dim", markup=False))
        if verbose
        else None
    )


def _is_interactive_tty() -> bool:
    """Return True when standard input is attached to an interactive terminal."""
    try:
        return bool(sys.stdin is not None and sys.stdin.isatty())
    except (OSError, ValueError):
        return False


def _resolve_media_mode(
    mode: MediaMode | None,
    *,
    lang: str,
    console: Console,
    err_console: Console,
) -> MediaMode:
    """Return explicit mode or prompt interactively on a TTY; fail cleanly when non-TTY."""
    if mode is not None:
        return mode

    from unduhin.errors import ValidationError

    if not _is_interactive_tty():
        raise ValidationError("cli.missing_mode")

    prompt_text = _safe(t("cli.mode_prompt", lang=lang), console.file)
    invalid_text = _safe(t("cli.mode_prompt_invalid", lang=lang), err_console.file)
    while True:
        console.print(f"{prompt_text} [1/2]: ", end="", markup=False)
        if hasattr(console.file, "flush"):
            console.file.flush()
        raw_line = sys.stdin.readline()
        if raw_line == "":
            raise ValidationError("cli.missing_mode")
        choice = raw_line.strip().lower()
        if choice in {"1", "mp3", "audio"}:
            return MediaMode.AUDIO
        if choice in {"2", "mp4", "video"}:
            return MediaMode.VIDEO
        _print_err(err_console, invalid_text, style="yellow")


def _extract_cli_flag_value(argv: list[str], flag_name: str) -> str | None:
    """Extract a CLI flag value like ``--lang id`` or ``--lang=id`` from ``argv``."""
    prefix = f"{flag_name}="
    for idx, token in enumerate(argv):
        if token == flag_name and idx + 1 < len(argv):
            return argv[idx + 1]
        if token.startswith(prefix):
            return token[len(prefix) :]
    return None


def _report_unexpected_error(
    exc: BaseException,
    *,
    verbose: bool,
    lang: str | None,
    err_console: Console | None = None,
) -> int:
    """Emit a localized unexpected-error notice, hiding tracebacks unless verbose."""
    resolved_lang = detect_language(lang)
    if verbose:
        traceback.print_exc(file=sys.stderr)
    msg = safe_console_text(
        t("error.unexpected_cli", lang=resolved_lang, detail=type(exc).__name__),
        encoding=getattr(err_console.file if err_console else sys.stderr, "encoding", None),
    )
    if err_console is not None:
        _print_err(err_console, msg, style="red")
    else:
        print(msg, file=sys.stderr)
    return int(ExitCode.FAILURE)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"unduhin {__version__}")
        raise typer.Exit(code=int(ExitCode.SUCCESS))


def _format_duration(seconds: float | None, *, lang: str | None) -> str:
    if seconds is None or seconds < 0:
        return t("cli.info_unknown", lang=lang)
    total = int(seconds)
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def _format_bytes(size_bytes: int | None, *, lang: str | None) -> str:
    if size_bytes is None or size_bytes <= 0:
        return t("cli.info_unknown", lang=lang)
    value = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024.0 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024.0
    return f"{value:.1f} GB"


def _merge_ctx_options(
    ctx: typer.Context, *, lang: str | None, verbose: bool
) -> tuple[str | None, bool]:
    """Combine root-level ``--lang``/``--verbose`` with subcommand-level flags."""
    obj = ctx.obj if isinstance(ctx.obj, dict) else {}
    resolved_lang_arg = lang if lang is not None else obj.get("lang")
    resolved_verbose = bool(verbose or obj.get("verbose", False))
    return resolved_lang_arg, resolved_verbose


@app.callback(invoke_without_command=True)
def root_callback(
    ctx: typer.Context,
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            "-V",
            callback=_version_callback,
            is_eager=True,
            help="Show version and exit.",
        ),
    ] = False,
    lang: Annotated[
        str | None,
        typer.Option("--lang", help="Language override: en or id."),
    ] = None,
    verbose: Annotated[
        bool,
        typer.Option("--verbose", "-v", help="Show detailed diagnostics or tracebacks on error."),
    ] = False,
) -> None:
    """unduhin CLI root command."""
    _ = version
    ctx.obj = {"lang": lang, "verbose": verbose}
    if ctx.invoked_subcommand is None:
        resolved_lang = detect_language(lang)
        enc = getattr(sys.stdout, "encoding", None)
        for line in (
            t("cli.welcome_title", lang=resolved_lang),
            "",
            t("cli.welcome_examples", lang=resolved_lang),
            "",
            t("cli.welcome_hint", lang=resolved_lang),
        ):
            typer.echo(safe_console_text(line, encoding=enc))
        raise typer.Exit(code=int(ExitCode.SUCCESS))


@app.command("download", epilog=_LEGAL_EPILOG)
def download_command(
    ctx: typer.Context,
    url: Annotated[
        str | None,
        typer.Argument(help="Media URL (http:// or https://). Optional when --batch is used."),
    ] = None,
    batch: Annotated[
        Path | None,
        typer.Option(
            "--batch",
            "-B",
            help="Path to UTF-8 text file of URLs (positional URL runs first; cross-deduplicated).",
        ),
    ] = None,
    mode: Annotated[
        MediaMode | None,
        typer.Option(
            "--mode",
            "-m",
            case_sensitive=False,
            help="Output format: audio (MP3) or video (MP4). Prompts interactively if omitted on a TTY.",
        ),
    ] = None,
    bitrate: Annotated[
        int,
        typer.Option("--bitrate", "-b", help="MP3 audio bitrate in kbps (64-320)."),
    ] = DEFAULT_AUDIO_BITRATE,
    max_height: Annotated[
        int,
        typer.Option("--max-height", "-H", help="Maximum MP4 video height in pixels (e.g. 720, 1080)."),
    ] = DEFAULT_MAX_HEIGHT,
    output_dir: Annotated[
        Path | None,
        typer.Option(
            "--output-dir",
            "-o",
            help="Destination directory (default: UNDUHIN_OUTPUT_DIR or ~/Downloads/unduhin).",
        ),
    ] = None,
    template: Annotated[
        str,
        typer.Option(
            "--template",
            "-t",
            help="Filename template using allowlisted fields: {title}, {id}, {uploader}, {upload_date}, {ext}.",
        ),
    ] = DEFAULT_FILENAME_TEMPLATE,
    on_conflict: Annotated[
        ConflictPolicy,
        typer.Option(
            "--on-conflict",
            case_sensitive=False,
            help=(
                "Action if destination file exists: 'rename' appends (N); 'skip' keeps existing files; "
                "'overwrite' replaces pre-existing files on disk but suffixes intra-batch duplicate "
                "titles so earlier downloads in the same batch are preserved."
            ),
        ),
    ] = ConflictPolicy.RENAME,
    playlist: Annotated[
        bool,
        typer.Option("--playlist", help="Allow downloading playlists (disabled by default)."),
    ] = False,
    max_items: Annotated[
        int,
        typer.Option("--max-items", help="Maximum playlist items to process (1-200)."),
    ] = DEFAULT_MAX_PLAYLIST_ITEMS,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Plan output paths without downloading or writing to disk."),
    ] = False,
    lang: Annotated[
        str | None,
        typer.Option("--lang", help="UI language override: en or id."),
    ] = None,
    verbose: Annotated[
        bool,
        typer.Option("--verbose", "-v", help="Show detailed yt-dlp diagnostics."),
    ] = False,
) -> None:
    """Download media from a URL or batch file into MP3 (audio) or MP4 (video)."""
    lang_arg, is_verbose = _merge_ctx_options(ctx, lang=lang, verbose=verbose)
    resolved_lang = detect_language(lang_arg)
    console, err_console = _get_consoles()

    try:
        from unduhin.errors import UnduhinError
        from unduhin.pipeline import PipelineHooks, run_download, validate_request_inputs
        from unduhin.validators import collect_input_urls, resolve_output_dir

        urls, duplicates_removed = collect_input_urls(url, batch)
        resolved_output_dir = resolve_output_dir(output_dir)
        precheck_request = DownloadRequest(
            urls=urls,
            mode=mode or MediaMode.AUDIO,
            output_dir=resolved_output_dir,
            audio_bitrate=bitrate,
            max_height=max_height,
            filename_template=template,
            conflict_policy=on_conflict,
            allow_playlist=playlist,
            max_items=max_items,
            dry_run=dry_run,
            lang=resolved_lang,
            verbose=is_verbose,
        )
        validate_request_inputs(precheck_request)

        resolved_mode = _resolve_media_mode(
            mode,
            lang=resolved_lang,
            console=console,
            err_console=err_console,
        )
        request = DownloadRequest(
            urls=urls,
            mode=resolved_mode,
            output_dir=resolved_output_dir,
            audio_bitrate=bitrate,
            max_height=max_height,
            filename_template=template,
            conflict_policy=on_conflict,
            allow_playlist=playlist,
            max_items=max_items,
            dry_run=dry_run,
            lang=resolved_lang,
            verbose=is_verbose,
        )

        if duplicates_removed > 0:
            console.print(
                _safe(
                    t("cli.duplicates_removed", lang=resolved_lang, count=duplicates_removed),
                    console.file,
                ),
                style="dim",
                markup=False,
            )

        cancel_event = threading.Event()
        prev_sigint: Any = None
        prev_sigbreak: Any = None
        registered_sigint = False
        registered_sigbreak = False
        sigbreak_num: int | None = getattr(signal, "SIGBREAK", None)

        def _cancel_signal_handler(_signum: int, _frame: Any) -> None:
            if cancel_event.is_set():
                raise KeyboardInterrupt
            cancel_event.set()

        if threading.current_thread() is threading.main_thread():
            prev_sigint = signal.getsignal(signal.SIGINT)
            signal.signal(signal.SIGINT, _cancel_signal_handler)
            registered_sigint = True
            if sigbreak_num is not None:
                prev_sigbreak = signal.getsignal(sigbreak_num)
                signal.signal(sigbreak_num, _cancel_signal_handler)
                registered_sigbreak = True

        adapter = _create_adapter(verbose=is_verbose, console=console)

        def _on_warning(msg: str) -> None:
            _print_err(err_console, msg, style="yellow")

        def _on_retry(attempt: int, max_retries: int, delay: float, reason: str) -> None:
            _print_err(
                err_console,
                t(
                    "cli.retrying",
                    lang=resolved_lang,
                    attempt=attempt,
                    max_retries=max_retries,
                    delay=delay,
                    reason=reason,
                ),
                style="yellow",
            )

        try:
            if dry_run:
                hooks = PipelineHooks(
                    on_warning=_on_warning,
                    on_retry=_on_retry,
                    is_cancelled=cancel_event.is_set,
                )
                summary = run_download(request, adapter, hooks=hooks)
            else:
                from rich.progress import (
                    BarColumn,
                    DownloadColumn,
                    Progress,
                    TextColumn,
                    TimeRemainingColumn,
                    TransferSpeedColumn,
                )

                with Progress(
                    TextColumn("[bold blue]{task.description}"),
                    BarColumn(),
                    DownloadColumn(),
                    TransferSpeedColumn(),
                    TimeRemainingColumn(),
                    console=console,
                    transient=True,
                ) as progress:
                    task_id = progress.add_task(
                        _safe(t("cli.downloading", lang=resolved_lang), console.file),
                        total=None,
                    )

                    def _on_progress(event: ProgressEvent) -> None:
                        if event.total_bytes is not None and event.total_bytes > 0:
                            progress.update(
                                task_id,
                                total=event.total_bytes,
                                completed=event.downloaded_bytes,
                            )
                        else:
                            progress.update(task_id, completed=event.downloaded_bytes)

                    hooks = PipelineHooks(
                        on_progress=_on_progress,
                        on_warning=_on_warning,
                        on_retry=_on_retry,
                        is_cancelled=cancel_event.is_set,
                    )
                    summary = run_download(request, adapter, hooks=hooks)
        finally:
            if registered_sigint and prev_sigint is not None:
                signal.signal(signal.SIGINT, prev_sigint)
            if registered_sigbreak and sigbreak_num is not None and prev_sigbreak is not None:
                signal.signal(sigbreak_num, prev_sigbreak)

        if summary.results:
            from rich.table import Table

            summary_table = Table(
                title=_safe(t("cli.summary_header", lang=resolved_lang), console.file)
            )
            summary_table.add_column(
                _safe(t("cli.summary_col_status", lang=resolved_lang), console.file),
                style="bold",
            )
            summary_table.add_column(
                _safe(t("cli.summary_col_target", lang=resolved_lang), console.file)
            )
            summary_table.add_column(
                _safe(t("cli.summary_col_detail", lang=resolved_lang), console.file)
            )

            for item in summary.results:
                batch_badge = (
                    f" {t('cli.item_batch_renamed_suffix', lang=resolved_lang)}"
                    if item.is_batch_renamed
                    else ""
                )
                if item.status is ItemStatus.SUCCEEDED:
                    status_str = "[green]OK[/green]"
                    target_str = str(item.output_path or item.url)
                    warn_notes = f" ({'; '.join(item.warnings)})" if item.warnings else ""
                    detail_str = f"{item.title or '-'}{batch_badge}{warn_notes}"
                elif item.status is ItemStatus.PLANNED:
                    status_str = "[cyan]DRY-RUN[/cyan]"
                    target_str = str(item.output_path or item.url)
                    detail_str = f"{item.title or '-'}{batch_badge}"
                elif item.status is ItemStatus.SKIPPED_EXISTS:
                    status_str = "[yellow]SKIPPED (EXISTS)[/yellow]"
                    target_str = str(item.output_path or item.title or item.url)
                    detail_str = item.message or "-"
                elif item.status is ItemStatus.SKIPPED_UNSUPPORTED:
                    status_str = "[yellow]SKIPPED (UNSUPPORTED)[/yellow]"
                    target_str = item.title or item.url
                    detail_str = item.message or "-"
                else:
                    status_str = "[red]FAILED[/red]"
                    target_str = item.url
                    detail_str = item.message or "-"
                summary_table.add_row(
                    status_str,
                    _safe(target_str, console.file),
                    _safe(detail_str, console.file),
                )

            console.print(summary_table)

        if summary.interrupted:
            _print_err(err_console, t("cli.interrupted", lang=resolved_lang), style="yellow")

        if dry_run:
            counts_line = t(
                "cli.summary_dry_run",
                lang=resolved_lang,
                planned=summary.planned_count,
                failed=summary.failed_count,
                skipped_exists=summary.skipped_exists_count,
                skipped_unsupported=summary.skipped_unsupported_count,
            )
        else:
            counts_line = t(
                "cli.summary_counts",
                lang=resolved_lang,
                succeeded=summary.succeeded_count,
                failed=summary.failed_count,
                skipped_exists=summary.skipped_exists_count,
                skipped_unsupported=summary.skipped_unsupported_count,
            )

        summary_label = _safe(t("cli.summary_header", lang=resolved_lang), console.file)
        console.print(
            f"[bold]{summary_label}:[/bold] {_safe(counts_line, console.file)}"
        )
        console.print(
            _safe(
                t("cli.summary_output_dir", lang=resolved_lang, path=str(resolved_output_dir)),
                console.file,
            ),
            style="dim",
            markup=False,
        )

        if (
            not dry_run
            and not summary.interrupted
            and summary.succeeded_count == 0
            and summary.failed_count == 0
            and summary.skipped_unsupported_count == 0
            and summary.skipped_exists_count > 0
        ):
            console.print(
                _safe(t("cli.summary_all_skipped", lang=resolved_lang), console.file),
                style="yellow",
                markup=False,
            )

        raise typer.Exit(code=int(summary.exit_code))
    except (typer.Exit, typer.Abort):
        raise
    except KeyboardInterrupt as exc:
        _print_err(err_console, t("cli.interrupted", lang=resolved_lang), style="yellow")
        raise typer.Exit(code=int(ExitCode.CANCELLED)) from exc
    except UnduhinError as exc:
        _print_err(err_console, exc.format(resolved_lang), style="red")
        raise typer.Exit(code=int(exc.exit_code)) from exc
    except Exception as exc:
        code = _report_unexpected_error(
            exc,
            verbose=is_verbose,
            lang=resolved_lang,
            err_console=err_console,
        )
        raise typer.Exit(code=code) from exc


@app.command("info", epilog=_LEGAL_EPILOG)
def info_command(
    ctx: typer.Context,
    url: Annotated[str, typer.Argument(help="Media URL (http:// or https://).")],
    mode: Annotated[
        MediaMode,
        typer.Option("--mode", "-m", case_sensitive=False, help="Target format preview: audio or video."),
    ] = MediaMode.AUDIO,
    bitrate: Annotated[
        int,
        typer.Option("--bitrate", "-b", help="Preview MP3 bitrate in kbps (64-320)."),
    ] = DEFAULT_AUDIO_BITRATE,
    max_height: Annotated[
        int,
        typer.Option("--max-height", "-H", help="Preview maximum MP4 video height (e.g. 720, 1080)."),
    ] = DEFAULT_MAX_HEIGHT,
    playlist: Annotated[
        bool,
        typer.Option("--playlist", help="Allow inspecting playlist entries (disabled by default)."),
    ] = False,
    max_items: Annotated[
        int,
        typer.Option("--max-items", help="Maximum playlist entries to list (1-200)."),
    ] = DEFAULT_MAX_PLAYLIST_ITEMS,
    lang: Annotated[
        str | None,
        typer.Option("--lang", help="UI language override: en or id."),
    ] = None,
    verbose: Annotated[
        bool,
        typer.Option("--verbose", "-v", help="Show detailed yt-dlp diagnostics."),
    ] = False,
) -> None:
    """Inspect media metadata and available formats without downloading."""
    lang_arg, is_verbose = _merge_ctx_options(ctx, lang=lang, verbose=verbose)
    resolved_lang = detect_language(lang_arg)
    console, err_console = _get_consoles()

    try:
        from rich.table import Table

        from unduhin.errors import UnduhinError
        from unduhin.pipeline import inspect_url

        adapter = _create_adapter(verbose=is_verbose, console=console)
        result = inspect_url(
            url,
            adapter,
            mode=mode,
            audio_bitrate=bitrate,
            max_height=max_height,
            allow_playlist=playlist,
            max_items=max_items,
            lang=resolved_lang,
            verbose=is_verbose,
        )

        for warning in result.warnings:
            _print_err(err_console, warning, style="yellow")

        meta = result.info
        unknown = t("cli.info_unknown", lang=resolved_lang)
        live_label = (
            meta.live_status
            if meta.live_status
            else ("live" if meta.is_live else t("cli.info_not_live", lang=resolved_lang))
        )
        playlist_label = (
            t("cli.info_yes", lang=resolved_lang)
            if meta.is_playlist
            else t("cli.info_no", lang=resolved_lang)
        )
        entry_count_label = (
            str(meta.playlist_count or len(result.items)) if meta.is_playlist else "1"
        )

        table = Table(title=_safe(t("cli.info_title", lang=resolved_lang), console.file))
        table.add_column(_safe(t("cli.info_field", lang=resolved_lang), console.file), style="bold")
        table.add_column(_safe(t("cli.info_value", lang=resolved_lang), console.file))

        table.add_row(
            _safe(t("cli.info_media_title", lang=resolved_lang), console.file),
            _safe(meta.title or unknown, console.file),
        )
        table.add_row(
            _safe(t("cli.info_uploader", lang=resolved_lang), console.file),
            _safe(meta.uploader or unknown, console.file),
        )
        table.add_row(
            _safe(t("cli.info_duration", lang=resolved_lang), console.file),
            _safe(_format_duration(meta.duration_seconds, lang=resolved_lang), console.file),
        )
        table.add_row(
            _safe(t("cli.info_live_status", lang=resolved_lang), console.file),
            _safe(live_label, console.file),
        )
        table.add_row(
            _safe(t("cli.info_est_size", lang=resolved_lang), console.file),
            _safe(_format_bytes(meta.filesize_approx, lang=resolved_lang), console.file),
        )
        table.add_row(
            _safe(t("cli.info_selected_format", lang=resolved_lang), console.file),
            _safe(result.selected_format_summary, console.file),
        )
        table.add_row(
            _safe(t("cli.info_playlist", lang=resolved_lang), console.file),
            _safe(playlist_label, console.file),
        )
        table.add_row(
            _safe(t("cli.info_item_count", lang=resolved_lang), console.file),
            _safe(entry_count_label, console.file),
        )
        console.print(table)

        if meta.is_playlist and result.items:
            entries_table = Table(
                title=_safe(
                    t("cli.info_playlist_entries", lang=resolved_lang, shown=len(result.items)),
                    console.file,
                )
            )
            entries_table.add_column("#", style="dim")
            entries_table.add_column(
                _safe(t("cli.info_media_title", lang=resolved_lang), console.file)
            )
            entries_table.add_column(
                _safe(t("cli.info_duration", lang=resolved_lang), console.file)
            )
            for idx, entry in enumerate(result.items, start=1):
                title_text = (
                    t("error.playlist_entry_unavailable", lang=resolved_lang)
                    if entry.is_unavailable
                    else entry.title
                )
                entries_table.add_row(
                    str(idx),
                    _safe(title_text, console.file),
                    _safe(_format_duration(entry.duration_seconds, lang=resolved_lang), console.file),
                )
            console.print(entries_table)

        raise typer.Exit(code=int(ExitCode.SUCCESS))
    except (typer.Exit, typer.Abort):
        raise
    except KeyboardInterrupt as exc:
        _print_err(err_console, t("cli.interrupted", lang=resolved_lang), style="yellow")
        raise typer.Exit(code=int(ExitCode.CANCELLED)) from exc
    except UnduhinError as exc:
        _print_err(err_console, exc.format(resolved_lang), style="red")
        raise typer.Exit(code=int(exc.exit_code)) from exc
    except Exception as exc:
        code = _report_unexpected_error(
            exc,
            verbose=is_verbose,
            lang=resolved_lang,
            err_console=err_console,
        )
        raise typer.Exit(code=code) from exc


@app.command("doctor")
def doctor_command(
    ctx: typer.Context,
    lang: Annotated[
        str | None,
        typer.Option("--lang", help="UI language override: en or id."),
    ] = None,
    verbose: Annotated[
        bool,
        typer.Option("--verbose", "-v", help="Show detailed tracebacks on unexpected errors."),
    ] = False,
) -> None:
    """Check Python, yt-dlp, FFmpeg (required) and yt-dlp-ejs, Deno (recommended)."""
    lang_arg, is_verbose = _merge_ctx_options(ctx, lang=lang, verbose=verbose)
    resolved_lang = detect_language(lang_arg)
    console, err_console = _get_consoles()

    try:
        from rich.table import Table

        from unduhin.doctor import CheckStatus, CheckTier, run_doctor

        report = run_doctor(lang=resolved_lang)

        table = Table(
            title=_safe(f"{t('doctor.title', lang=resolved_lang)} ({report.os_family})", console.file)
        )
        table.add_column(
            _safe(t("doctor.col_component", lang=resolved_lang), console.file),
            style="bold",
        )
        table.add_column(_safe(t("doctor.col_tier", lang=resolved_lang), console.file))
        table.add_column(_safe(t("doctor.col_status", lang=resolved_lang), console.file))
        table.add_column(_safe(t("doctor.col_details", lang=resolved_lang), console.file))
        table.add_column(_safe(t("doctor.col_fix", lang=resolved_lang), console.file))

        for check in report.checks:
            tier_text = (
                t("doctor.tier_required", lang=resolved_lang)
                if check.tier is CheckTier.REQUIRED
                else t("doctor.tier_recommended", lang=resolved_lang)
            )
            if check.status is CheckStatus.OK:
                status_cell = f"[green]{t('doctor.status_ok', lang=resolved_lang)}[/green]"
            elif check.status is CheckStatus.WARN:
                status_cell = f"[yellow]{t('doctor.status_warn', lang=resolved_lang)}[/yellow]"
            else:
                status_cell = f"[red]{t('doctor.status_missing', lang=resolved_lang)}[/red]"
            table.add_row(
                _safe(check.component, console.file),
                _safe(tier_text, console.file),
                status_cell,
                _safe(check.detail, console.file),
                _safe(check.fix_hint or "-", console.file),
            )

        console.print(table)
        if report.is_ready and not report.has_warnings:
            console.print(
                _safe(t("doctor.summary_ready", lang=resolved_lang), console.file),
                style="green",
                markup=False,
            )
            raise typer.Exit(code=int(ExitCode.SUCCESS))

        if report.is_ready:
            console.print(
                _safe(t("doctor.summary_ready_warn", lang=resolved_lang), console.file),
                style="yellow",
                markup=False,
            )
            raise typer.Exit(code=int(ExitCode.SUCCESS))

        console.print(
            _safe(t("doctor.summary_issues", lang=resolved_lang), console.file),
            style="red",
            markup=False,
        )
        raise typer.Exit(code=int(ExitCode.MISSING_DEPENDENCY))
    except (typer.Exit, typer.Abort):
        raise
    except KeyboardInterrupt as exc:
        _print_err(err_console, t("cli.interrupted", lang=resolved_lang), style="yellow")
        raise typer.Exit(code=int(ExitCode.CANCELLED)) from exc
    except Exception as exc:
        code = _report_unexpected_error(
            exc,
            verbose=is_verbose,
            lang=resolved_lang,
            err_console=err_console,
        )
        raise typer.Exit(code=code) from exc


@app.command("ui", epilog=_LEGAL_EPILOG)
def ui_command(
    ctx: typer.Context,
    output_dir: Annotated[
        Path | None,
        typer.Option(
            "--output-dir",
            "-o",
            help="Fixed destination directory (default: UNDUHIN_OUTPUT_DIR or ~/Downloads/unduhin).",
        ),
    ] = None,
    port: Annotated[
        int,
        typer.Option(
            "--port",
            "-p",
            help="Local TCP port on 127.0.0.1.",
        ),
    ] = DEFAULT_UI_PORT,
    lang: Annotated[
        str | None,
        typer.Option("--lang", "-l", help="UI language override: en or id."),
    ] = None,
    verbose: Annotated[
        bool,
        typer.Option("--verbose", "-v", help="Show detailed tracebacks on unexpected errors."),
    ] = False,
) -> None:
    """Launch the local web UI bound strictly to 127.0.0.1."""
    lang_arg, is_verbose = _merge_ctx_options(ctx, lang=lang, verbose=verbose)
    resolved_lang = detect_language(lang_arg)
    _console, err_console = _get_consoles()

    try:
        from unduhin.errors import UnduhinError
        from unduhin.ui import launch_ui

        launch_ui(output_dir, port=port, lang=lang_arg)
    except (typer.Exit, typer.Abort):
        raise
    except KeyboardInterrupt as exc:
        _print_err(err_console, t("cli.interrupted", lang=resolved_lang), style="yellow")
        raise typer.Exit(code=int(ExitCode.CANCELLED)) from exc
    except UnduhinError as exc:
        _print_err(err_console, exc.format(resolved_lang), style="red")
        raise typer.Exit(code=int(exc.exit_code)) from exc
    except Exception as exc:
        code = _report_unexpected_error(
            exc,
            verbose=is_verbose,
            lang=resolved_lang,
            err_console=err_console,
        )
        raise typer.Exit(code=code) from exc


def main() -> int:
    """Run the Typer CLI application and return a POSIX-friendly exit code."""
    argv = sys.argv[1:]
    verbose_requested = "--verbose" in argv or "-v" in argv
    lang_override = _extract_cli_flag_value(argv, "--lang")
    try:
        app()
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 1
    except KeyboardInterrupt:
        resolved_lang = detect_language(lang_override)
        msg = safe_console_text(
            t("cli.interrupted", lang=resolved_lang),
            encoding=getattr(sys.stderr, "encoding", None),
        )
        print(msg, file=sys.stderr)
        return int(ExitCode.CANCELLED)
    except Exception as exc:
        return _report_unexpected_error(exc, verbose=verbose_requested, lang=lang_override)
    return int(ExitCode.SUCCESS)
