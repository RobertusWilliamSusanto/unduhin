"""Minimal localhost Gradio UI for unduhin.

All Gradio telemetry is disabled prior to importing ``gradio``. The server
always binds exclusively to ``127.0.0.1`` with sharing, auto-browser launch,
run history, file previews, and external font/asset requests disabled.
"""

from __future__ import annotations

import importlib
import os
import queue
import re
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from unduhin.config import (
    DEFAULT_UI_PORT,
    MAX_UI_URL_LENGTH,
    ConflictPolicy,
    DownloadRequest,
    ExitCode,
    ItemResult,
    ItemStatus,
    MediaMode,
)
from unduhin.errors import (
    CancelledError,
    DependencyError,
    DownloadFailedError,
    PlaylistDisabledError,
    UnduhinError,
    ValidationError,
    classify_exception,
)
from unduhin.i18n import SupportedLang, detect_language, safe_console_text, t
from unduhin.pipeline import PipelineHooks, run_download
from unduhin.validators import (
    cleanup_stale_staging_dirs,
    ensure_safe_output_dir,
    resolve_output_dir,
    validate_output_dir_path,
    validate_url,
)
from unduhin.ytdlp_adapter import MediaAdapter, ProgressEvent, YtDlpAdapter

if TYPE_CHECKING:
    from types import ModuleType

LOCALHOST_BIND_HOST = "127.0.0.1"
_STAGING_TOKEN_RE = re.compile(r"\.unduhin-tmp-[A-Za-z0-9_.-]+")
_WINDOWS_PATH_FRAGMENT_RE = re.compile(r"[A-Za-z]:\\[^\s'\"]*\.unduhin-tmp-[^\s'\"]*")
_POSIX_PATH_FRAGMENT_RE = re.compile(r"/[^\s'\"]*\.unduhin-tmp-[^\s'\"]*")

UI_FONT_STACK: tuple[str, ...] = (
    "system-ui",
    "-apple-system",
    "Segoe UI",
    "Roboto",
    "sans-serif",
)
UI_MONO_STACK: tuple[str, ...] = (
    "ui-monospace",
    "Cascadia Mono",
    "Consolas",
    "monospace",
)

# Solid hex color tokens verified for WCAG 2.1 AA contrast (>= 4.5:1 text,
# >= 3.0:1 borders & focus outlines across page, surface, and secondary fills).
LIGHT_TOKENS: dict[str, str] = {
    "page": "#F6F6F4",
    "surface": "#FFFFFF",
    "secondary": "#EAEAE6",
    "secondary_hover": "#DFDFD9",
    "border": "#80807A",
    "border_hover": "#686862",
    "text": "#1A1A18",
    "muted": "#55554F",
    "accent": "#0F6B5C",
    "accent_hover": "#0B5347",
    "accent_soft": "#E4F0ED",
    "on_accent": "#FFFFFF",
    "danger": "#B42318",
    "danger_hover": "#911C13",
    "danger_bg": "#FDF2F2",
    "success": "#1B7F4B",
    "warn": "#8A5A00",
}

DARK_TOKENS: dict[str, str] = {
    "page": "#151514",
    "surface": "#1E1E1C",
    "secondary": "#262623",
    "secondary_hover": "#32322E",
    "border": "#72726C",
    "border_hover": "#8B8B84",
    "text": "#ECECE8",
    "muted": "#A3A39B",
    "accent": "#4FB3A0",
    "accent_hover": "#66C4B2",
    "accent_soft": "#1B2E2A",
    "on_accent": "#0B1F1B",
    "danger": "#F0867B",
    "danger_hover": "#F49D94",
    "danger_bg": "#2A1918",
    "success": "#6FCF97",
    "warn": "#E3B341",
}

# Pure teal + neutral stone palettes to avoid Gradio's default blue/indigo hues.
_TEAL_PALETTE: tuple[str, ...] = (
    "#E4F0ED",
    "#CBE2DC",
    "#A6CCC3",
    "#7EB6AA",
    "#4FB3A0",
    "#0F6B5C",
    "#0B5347",
    "#084137",
    "#063028",
    "#04231E",
    "#031814",
)
_STONE_PALETTE: tuple[str, ...] = (
    "#F6F6F4",
    "#EAEAE6",
    "#DFDFD9",
    "#C2C2BA",
    "#A3A39B",
    "#80807A",
    "#55554F",
    "#3B3B37",
    "#262623",
    "#1E1E1C",
    "#151514",
)

# Theme variable mapping: every light visual variable has an explicit _dark pair,
# zero shadows, zero gradients, and solid hex colors only.
UI_THEME_VARIABLES: dict[str, str] = {
    "body_background_fill": LIGHT_TOKENS["page"],
    "body_background_fill_dark": DARK_TOKENS["page"],
    "background_fill_primary": LIGHT_TOKENS["page"],
    "background_fill_primary_dark": DARK_TOKENS["page"],
    "background_fill_secondary": LIGHT_TOKENS["secondary"],
    "background_fill_secondary_dark": DARK_TOKENS["secondary"],
    "block_background_fill": LIGHT_TOKENS["surface"],
    "block_background_fill_dark": DARK_TOKENS["surface"],
    "block_border_color": LIGHT_TOKENS["border"],
    "block_border_color_dark": DARK_TOKENS["border"],
    "block_border_width": "1px",
    "block_border_width_dark": "1px",
    "block_info_text_color": LIGHT_TOKENS["muted"],
    "block_info_text_color_dark": DARK_TOKENS["muted"],
    "block_label_background_fill": LIGHT_TOKENS["surface"],
    "block_label_background_fill_dark": DARK_TOKENS["surface"],
    "block_label_border_color": LIGHT_TOKENS["border"],
    "block_label_border_color_dark": DARK_TOKENS["border"],
    "block_label_border_width": "1px",
    "block_label_border_width_dark": "1px",
    "block_label_text_color": LIGHT_TOKENS["muted"],
    "block_label_text_color_dark": DARK_TOKENS["muted"],
    "block_title_text_color": LIGHT_TOKENS["text"],
    "block_title_text_color_dark": DARK_TOKENS["text"],
    "panel_background_fill": LIGHT_TOKENS["surface"],
    "panel_background_fill_dark": DARK_TOKENS["surface"],
    "panel_border_color": LIGHT_TOKENS["border"],
    "panel_border_color_dark": DARK_TOKENS["border"],
    "panel_border_width": "1px",
    "panel_border_width_dark": "1px",
    "body_text_color": LIGHT_TOKENS["text"],
    "body_text_color_dark": DARK_TOKENS["text"],
    "body_text_color_subdued": LIGHT_TOKENS["muted"],
    "body_text_color_subdued_dark": DARK_TOKENS["muted"],
    "border_color_primary": LIGHT_TOKENS["border"],
    "border_color_primary_dark": DARK_TOKENS["border"],
    "border_color_accent": LIGHT_TOKENS["accent"],
    "border_color_accent_dark": DARK_TOKENS["accent"],
    "border_color_accent_subdued": LIGHT_TOKENS["accent"],
    "border_color_accent_subdued_dark": DARK_TOKENS["accent"],
    "color_accent": LIGHT_TOKENS["accent"],
    "color_accent_soft": LIGHT_TOKENS["accent_soft"],
    "color_accent_soft_dark": DARK_TOKENS["accent_soft"],
    "loader_color": LIGHT_TOKENS["accent"],
    "loader_color_dark": DARK_TOKENS["accent"],
    "slider_color": LIGHT_TOKENS["accent"],
    "slider_color_dark": DARK_TOKENS["accent"],
    "stat_background_fill": LIGHT_TOKENS["accent"],
    "stat_background_fill_dark": DARK_TOKENS["accent"],
    "code_background_fill": LIGHT_TOKENS["secondary"],
    "code_background_fill_dark": DARK_TOKENS["secondary"],
    "input_background_fill": LIGHT_TOKENS["surface"],
    "input_background_fill_dark": DARK_TOKENS["surface"],
    "input_background_fill_focus": LIGHT_TOKENS["surface"],
    "input_background_fill_focus_dark": DARK_TOKENS["surface"],
    "input_background_fill_hover": LIGHT_TOKENS["surface"],
    "input_background_fill_hover_dark": DARK_TOKENS["surface"],
    "input_border_color": LIGHT_TOKENS["border"],
    "input_border_color_dark": DARK_TOKENS["border"],
    "input_border_color_focus": LIGHT_TOKENS["accent"],
    "input_border_color_focus_dark": DARK_TOKENS["accent"],
    "input_border_color_hover": LIGHT_TOKENS["border_hover"],
    "input_border_color_hover_dark": DARK_TOKENS["border_hover"],
    "input_border_width": "1px",
    "input_border_width_dark": "1px",
    "input_placeholder_color": LIGHT_TOKENS["muted"],
    "input_placeholder_color_dark": DARK_TOKENS["muted"],
    "checkbox_background_color": LIGHT_TOKENS["surface"],
    "checkbox_background_color_dark": DARK_TOKENS["surface"],
    "checkbox_background_color_focus": LIGHT_TOKENS["surface"],
    "checkbox_background_color_focus_dark": DARK_TOKENS["surface"],
    "checkbox_background_color_hover": LIGHT_TOKENS["secondary"],
    "checkbox_background_color_hover_dark": DARK_TOKENS["secondary"],
    "checkbox_background_color_selected": LIGHT_TOKENS["accent"],
    "checkbox_background_color_selected_dark": DARK_TOKENS["accent"],
    "checkbox_border_color": LIGHT_TOKENS["border"],
    "checkbox_border_color_dark": DARK_TOKENS["border"],
    "checkbox_border_color_focus": LIGHT_TOKENS["accent"],
    "checkbox_border_color_focus_dark": DARK_TOKENS["accent"],
    "checkbox_border_color_hover": LIGHT_TOKENS["border_hover"],
    "checkbox_border_color_hover_dark": DARK_TOKENS["border_hover"],
    "checkbox_border_color_selected": LIGHT_TOKENS["accent"],
    "checkbox_border_color_selected_dark": DARK_TOKENS["accent"],
    "checkbox_border_width": "1px",
    "checkbox_border_width_dark": "1px",
    "checkbox_label_background_fill": LIGHT_TOKENS["secondary"],
    "checkbox_label_background_fill_dark": DARK_TOKENS["secondary"],
    "checkbox_label_background_fill_hover": LIGHT_TOKENS["secondary_hover"],
    "checkbox_label_background_fill_hover_dark": DARK_TOKENS["secondary_hover"],
    "checkbox_label_background_fill_selected": LIGHT_TOKENS["accent_soft"],
    "checkbox_label_background_fill_selected_dark": DARK_TOKENS["accent_soft"],
    "checkbox_label_border_color": LIGHT_TOKENS["border"],
    "checkbox_label_border_color_dark": DARK_TOKENS["border"],
    "checkbox_label_border_color_hover": LIGHT_TOKENS["border_hover"],
    "checkbox_label_border_color_hover_dark": DARK_TOKENS["border_hover"],
    "checkbox_label_border_color_selected": LIGHT_TOKENS["accent"],
    "checkbox_label_border_color_selected_dark": DARK_TOKENS["accent"],
    "checkbox_label_border_width": "1px",
    "checkbox_label_border_width_dark": "1px",
    "checkbox_label_text_color": LIGHT_TOKENS["text"],
    "checkbox_label_text_color_dark": DARK_TOKENS["text"],
    "checkbox_label_text_color_selected": LIGHT_TOKENS["text"],
    "checkbox_label_text_color_selected_dark": DARK_TOKENS["text"],
    "error_background_fill": LIGHT_TOKENS["danger_bg"],
    "error_background_fill_dark": DARK_TOKENS["danger_bg"],
    "error_border_color": LIGHT_TOKENS["danger"],
    "error_border_color_dark": DARK_TOKENS["danger"],
    "error_border_width": "1px",
    "error_border_width_dark": "1px",
    "error_text_color": LIGHT_TOKENS["danger"],
    "error_text_color_dark": DARK_TOKENS["danger"],
    "error_icon_color": LIGHT_TOKENS["danger"],
    "error_icon_color_dark": DARK_TOKENS["danger"],
    "button_border_width": "1px",
    "button_border_width_dark": "1px",
    "button_transform_hover": "none",
    "button_transform_active": "none",
    "button_transition": "background-color 0.15s ease, border-color 0.15s ease",
    "button_primary_background_fill": LIGHT_TOKENS["accent"],
    "button_primary_background_fill_dark": DARK_TOKENS["accent"],
    "button_primary_background_fill_hover": LIGHT_TOKENS["accent_hover"],
    "button_primary_background_fill_hover_dark": DARK_TOKENS["accent_hover"],
    "button_primary_border_color": LIGHT_TOKENS["accent"],
    "button_primary_border_color_dark": DARK_TOKENS["accent"],
    "button_primary_border_color_hover": LIGHT_TOKENS["accent_hover"],
    "button_primary_border_color_hover_dark": DARK_TOKENS["accent_hover"],
    "button_primary_text_color": LIGHT_TOKENS["on_accent"],
    "button_primary_text_color_dark": DARK_TOKENS["on_accent"],
    "button_primary_text_color_hover": LIGHT_TOKENS["on_accent"],
    "button_primary_text_color_hover_dark": DARK_TOKENS["on_accent"],
    "button_secondary_background_fill": LIGHT_TOKENS["secondary"],
    "button_secondary_background_fill_dark": DARK_TOKENS["secondary"],
    "button_secondary_background_fill_hover": LIGHT_TOKENS["secondary_hover"],
    "button_secondary_background_fill_hover_dark": DARK_TOKENS["secondary_hover"],
    "button_secondary_border_color": LIGHT_TOKENS["border"],
    "button_secondary_border_color_dark": DARK_TOKENS["border"],
    "button_secondary_border_color_hover": LIGHT_TOKENS["border_hover"],
    "button_secondary_border_color_hover_dark": DARK_TOKENS["border_hover"],
    "button_secondary_text_color": LIGHT_TOKENS["text"],
    "button_secondary_text_color_dark": DARK_TOKENS["text"],
    "button_secondary_text_color_hover": LIGHT_TOKENS["text"],
    "button_secondary_text_color_hover_dark": DARK_TOKENS["text"],
    "button_cancel_background_fill": LIGHT_TOKENS["surface"],
    "button_cancel_background_fill_dark": DARK_TOKENS["surface"],
    "button_cancel_background_fill_hover": LIGHT_TOKENS["danger_bg"],
    "button_cancel_background_fill_hover_dark": DARK_TOKENS["danger_bg"],
    "button_cancel_border_color": LIGHT_TOKENS["danger"],
    "button_cancel_border_color_dark": DARK_TOKENS["danger"],
    "button_cancel_border_color_hover": LIGHT_TOKENS["danger_hover"],
    "button_cancel_border_color_hover_dark": DARK_TOKENS["danger_hover"],
    "button_cancel_text_color": LIGHT_TOKENS["danger"],
    "button_cancel_text_color_dark": DARK_TOKENS["danger"],
    "button_cancel_text_color_hover": LIGHT_TOKENS["danger_hover"],
    "button_cancel_text_color_hover_dark": DARK_TOKENS["danger_hover"],
    "shadow_drop": "none",
    "shadow_drop_lg": "none",
    "shadow_inset": "none",
    "shadow_spread": "0px",
    "shadow_spread_dark": "0px",
    "block_label_shadow": "none",
    "block_shadow": "none",
    "block_shadow_dark": "none",
    "checkbox_shadow": "none",
    "checkbox_label_shadow": "none",
    "checkbox_label_shadow_dark": "none",
    "checkbox_label_shadow_hover": "none",
    "checkbox_label_shadow_hover_dark": "none",
    "checkbox_label_shadow_active": "none",
    "checkbox_label_shadow_active_dark": "none",
    "input_shadow": "none",
    "input_shadow_dark": "none",
    "input_shadow_focus": "none",
    "input_shadow_focus_dark": "none",
    "button_primary_shadow": "none",
    "button_primary_shadow_hover": "none",
    "button_primary_shadow_active": "none",
    "button_primary_shadow_dark": "none",
    "button_primary_shadow_hover_dark": "none",
    "button_primary_shadow_active_dark": "none",
    "button_secondary_shadow": "none",
    "button_secondary_shadow_hover": "none",
    "button_secondary_shadow_active": "none",
    "button_secondary_shadow_dark": "none",
    "button_secondary_shadow_hover_dark": "none",
    "button_secondary_shadow_active_dark": "none",
    "button_cancel_shadow": "none",
    "button_cancel_shadow_hover": "none",
    "button_cancel_shadow_active": "none",
    "button_cancel_shadow_dark": "none",
    "button_cancel_shadow_hover_dark": "none",
    "button_cancel_shadow_active_dark": "none",
}

CUSTOM_CSS = """
.unduhin-root {
    max-width: 720px;
    margin: 0 auto;
    padding: 20px 16px;
}
.unduhin-header h1 {
    font-size: 1.5rem;
    font-weight: 700;
    margin: 0 0 4px 0;
}
.unduhin-header p {
    margin: 0;
    color: var(--body-text-color-subdued);
}
.unduhin-root .block,
.unduhin-root input,
.unduhin-root textarea,
.unduhin-root select,
.unduhin-root button {
    border-radius: 6px;
    box-shadow: none;
}
.unduhin-mono textarea,
.unduhin-mono input {
    font-family: ui-monospace, "Cascadia Mono", Consolas, monospace;
}
.unduhin-root button:focus-visible,
.unduhin-root input:focus-visible,
.unduhin-root textarea:focus-visible,
.unduhin-root select:focus-visible,
.unduhin-root label:focus-within {
    outline: 2px solid var(--border-color-accent);
    outline-offset: 2px;
}
.unduhin-legal {
    font-size: 0.875rem;
    color: var(--body-text-color-subdued);
}
.dark .unduhin-root {
    --color-accent: #4FB3A0;
}
"""

THEME_HEAD_SCRIPT = """
<script>
(function () {
    const STORAGE_KEY = "unduhin-theme";
    function readPref() {
        try {
            const raw = window.localStorage.getItem(STORAGE_KEY);
            if (raw === "light" || raw === "dark" || raw === "system") {
                return raw;
            }
        } catch (_err) {
            return "system";
        }
        return "system";
    }
    function applyPref(pref) {
        const prefersDark =
            typeof window.matchMedia === "function" &&
            window.matchMedia("(prefers-color-scheme: dark)").matches;
        const useDark = pref === "dark" || (pref === "system" && prefersDark);
        document.documentElement.classList.toggle("dark", useDark);
    }
    applyPref(readPref());
    if (typeof window.matchMedia === "function") {
        const mq = window.matchMedia("(prefers-color-scheme: dark)");
        if (typeof mq.addEventListener === "function") {
            mq.addEventListener("change", function () {
                if (readPref() === "system") {
                    applyPref("system");
                }
            });
        }
    }
})();
</script>
""".strip()

THEME_TOGGLE_JS = """
(mode) => {
    const next = mode === "light" || mode === "dark" || mode === "system" ? mode : "system";
    try {
        window.localStorage.setItem("unduhin-theme", next);
    } catch (_err) {
        // Ignore storage restrictions in private browsing.
    }
    const prefersDark =
        typeof window.matchMedia === "function" &&
        window.matchMedia("(prefers-color-scheme: dark)").matches;
    const useDark = next === "dark" || (next === "system" && prefersDark);
    document.documentElement.classList.toggle("dark", useDark);
}
""".strip()

THEME_LOAD_JS = """
() => {
    try {
        const raw = window.localStorage.getItem("unduhin-theme");
        if (raw === "light" || raw === "dark" || raw === "system") {
            return raw;
        }
    } catch (_err) {
        return "system";
    }
    return "system";
}
""".strip()


@dataclass(frozen=True)
class UiDownloadOutcome:
    """Structured result emitted by pure UI download execution."""

    status_text: str
    summary_text: str
    exit_code: ExitCode


class UiSessionController:
    """Coordinate single-flight cooperative cancellation across Download & Cancel."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active_event: threading.Event | None = None

    def start_run(self) -> threading.Event:
        """Register a fresh cancellation event for an active download."""
        event = threading.Event()
        with self._lock:
            self._active_event = event
        return event

    def request_cancel(self) -> bool:
        """Signal the currently active download to stop, returning True if active."""
        with self._lock:
            event = self._active_event
        if event is None:
            return False
        event.set()
        return True

    def finish_run(self, event: threading.Event) -> None:
        """Clear the active event only if it matches the completed run."""
        with self._lock:
            if self._active_event is event:
                self._active_event = None


def disable_gradio_telemetry_env() -> None:
    """Disable Gradio and Hugging Face Hub network telemetry via env vars."""
    os.environ["GRADIO_ANALYTICS_ENABLED"] = "False"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "True"


def _import_gradio() -> ModuleType:
    """Set telemetry opt-out flags first, then import and return ``gradio``."""
    disable_gradio_telemetry_env()
    try:
        return importlib.import_module("gradio")
    except ImportError as exc:
        raise DependencyError("error.gradio_missing") from exc


def validate_ui_url(raw_url: str) -> str:
    """Validate URL length and scheme/host safety for UI input."""
    trimmed = raw_url.strip()
    if len(trimmed) > MAX_UI_URL_LENGTH:
        raise ValidationError("error.ui_url_too_long", max_len=MAX_UI_URL_LENGTH)
    return validate_url(trimmed)


def sanitize_ui_error_text(exc: BaseException, *, lang: SupportedLang) -> str:
    """Format an error into human-safe localized copy without staging paths or tracebacks."""
    if isinstance(exc, PlaylistDisabledError):
        raw = t("error.ui_playlist_rejected", lang=lang)
    elif isinstance(exc, UnduhinError):
        raw = exc.format(lang=lang)
    else:
        mapped = classify_exception(exc)
        raw = mapped.format(lang=lang)

    cleaned = _WINDOWS_PATH_FRAGMENT_RE.sub("[staging]", raw)
    cleaned = _POSIX_PATH_FRAGMENT_RE.sub("[staging]", cleaned)
    cleaned = _STAGING_TOKEN_RE.sub("[staging]", cleaned)
    return cleaned.splitlines()[0].strip() if cleaned else t("error.unexpected", lang=lang, detail="-")


def format_item_outcome(
    item: ItemResult,
    *,
    lang: SupportedLang,
) -> UiDownloadOutcome:
    """Map one pipeline ``ItemResult`` to UI status and summary strings."""
    filename = item.output_path.name if item.output_path is not None else "-"
    if item.status is ItemStatus.SUCCEEDED:
        status_text = t("ui.status_succeeded", lang=lang, filename=filename)
        return UiDownloadOutcome(
            status_text=status_text,
            summary_text=status_text,
            exit_code=ExitCode.SUCCESS,
        )
    if item.status is ItemStatus.SKIPPED_EXISTS:
        status_text = t("ui.status_skipped_exists", lang=lang, filename=filename)
        return UiDownloadOutcome(
            status_text=status_text,
            summary_text=status_text,
            exit_code=ExitCode.SUCCESS,
        )
    if item.status is ItemStatus.SKIPPED_UNSUPPORTED:
        reason = item.message or item.title or "-"
        status_text = t("ui.status_skipped_unsupported", lang=lang, reason=reason)
        return UiDownloadOutcome(
            status_text=status_text,
            summary_text=status_text,
            exit_code=ExitCode.FAILURE,
        )

    reason = item.message or "-"
    if reason == t("error.playlist_disabled", lang=lang):
        reason = t("error.ui_playlist_rejected", lang=lang)
    cleaned_reason = _STAGING_TOKEN_RE.sub("[staging]", reason)
    cancelled_msg = t("error.cancelled", lang=lang)
    if cleaned_reason == cancelled_msg or "cancelled" in cleaned_reason.casefold():
        status_text = t("ui.status_cancelled", lang=lang)
        return UiDownloadOutcome(
            status_text=status_text,
            summary_text=status_text,
            exit_code=ExitCode.CANCELLED,
        )
    status_text = t("ui.status_failed", lang=lang, reason=cleaned_reason)
    return UiDownloadOutcome(
        status_text=status_text,
        summary_text=status_text,
        exit_code=ExitCode.FAILURE,
    )


def _format_progress_status(event: ProgressEvent, *, lang: SupportedLang) -> str:
    """Translate adapter progress stages into localized single-line UI status."""
    if event.status in {"finished", "postprocessing"}:
        return t("ui.status_postprocessing", lang=lang)
    if event.downloaded_bytes > 0 and event.total_bytes and event.total_bytes > 0:
        pct = min(100.0, (event.downloaded_bytes / event.total_bytes) * 100.0)
        percent_text = f"{pct:.1f}%"
    else:
        percent_text = "0%"
    return t("ui.status_downloading", lang=lang, percent=percent_text)


def execute_ui_download(
    raw_url: str,
    raw_mode: str,
    output_dir: Path,
    *,
    lang: SupportedLang,
    adapter_factory: Callable[[], MediaAdapter] = YtDlpAdapter,
    session: UiSessionController | None = None,
    cancel_event: threading.Event | None = None,
) -> Iterator[tuple[str, str]]:
    """Yield ``(status_text, summary_text)`` updates for a single UI download.

    Playlists are strictly rejected in UI mode, ``output_dir`` is always fixed
    by the caller (never UI input), and ``cancel_event`` cooperates with
    ``PipelineHooks.is_cancelled`` so staging directories are cleaned promptly.
    """
    resolved_lang: SupportedLang = "id" if lang == "id" else "en"
    yield t("ui.status_validating", lang=resolved_lang), ""

    try:
        validated_url = validate_ui_url(raw_url)
        validated_output = validate_output_dir_path(output_dir)
        mode = MediaMode(raw_mode)
    except (ValidationError, ValueError) as exc:
        err: BaseException = (
            exc
            if isinstance(exc, UnduhinError)
            else ValidationError("error.invalid_url", url=raw_url)
        )
        msg = sanitize_ui_error_text(err, lang=resolved_lang)
        yield t("ui.status_failed", lang=resolved_lang, reason=msg), msg
        return

    owned_event = cancel_event
    if owned_event is None:
        owned_event = session.start_run() if session is not None else threading.Event()

    updates: queue.Queue[tuple[str, tuple[str, str] | None]] = queue.Queue()
    completed = False

    def _on_progress(event: ProgressEvent) -> None:
        if owned_event.is_set():
            return
        updates.put(("status", (_format_progress_status(event, lang=resolved_lang), "")))

    def _on_retry(attempt: int, max_retries: int, _delay: float, _reason: str) -> None:
        if owned_event.is_set():
            return
        updates.put(
            (
                "status",
                (
                    t(
                        "ui.status_retrying",
                        lang=resolved_lang,
                        attempt=attempt,
                        max_retries=max_retries,
                    ),
                    "",
                ),
            )
        )

    def _worker() -> None:
        try:
            adapter = adapter_factory()
            request = DownloadRequest(
                urls=(validated_url,),
                mode=mode,
                output_dir=validated_output,
                allow_playlist=False,
                conflict_policy=ConflictPolicy.RENAME,
                lang=resolved_lang,
            )
            hooks = PipelineHooks(
                on_progress=_on_progress,
                on_retry=_on_retry,
                is_cancelled=owned_event.is_set,
            )
            summary = run_download(request, adapter=adapter, hooks=hooks)
            if owned_event.is_set() or summary.interrupted:
                cancelled_text = t("ui.status_cancelled", lang=resolved_lang)
                updates.put(("done", (cancelled_text, cancelled_text)))
                return
            if summary.results:
                outcome = format_item_outcome(summary.results[0], lang=resolved_lang)
                updates.put(("done", (outcome.status_text, outcome.summary_text)))
            else:
                idle_text = t("ui.status_idle", lang=resolved_lang)
                updates.put(("done", (idle_text, "")))
        except CancelledError:
            cancelled_text = t("ui.status_cancelled", lang=resolved_lang)
            updates.put(("done", (cancelled_text, cancelled_text)))
        except BaseException as exc:
            msg = sanitize_ui_error_text(exc, lang=resolved_lang)
            updates.put(("done", (t("ui.status_failed", lang=resolved_lang, reason=msg), msg)))

    worker = threading.Thread(target=_worker, name="unduhin-ui-worker", daemon=True)
    worker.start()

    try:
        while worker.is_alive() or not updates.empty():
            try:
                kind, payload = updates.get(timeout=0.1)
            except queue.Empty:
                if owned_event.is_set():
                    break
                continue
            if payload is not None:
                yield payload
            if kind == "done":
                completed = True
                break

        if owned_event.is_set() and not completed:
            cancelled_text = t("ui.status_cancelled", lang=resolved_lang)
            yield cancelled_text, cancelled_text
    finally:
        if not completed:
            owned_event.set()
        worker.join(timeout=2.0)
        if session is not None:
            session.finish_run(owned_event)


def build_ui_theme(gr_mod: ModuleType | None = None) -> Any:
    """Create a flat, shadow-free, gradient-free Gradio 6 ``Base`` theme."""
    gr = gr_mod if gr_mod is not None else _import_gradio()
    colors_mod = importlib.import_module("gradio.themes.utils.colors")
    fonts_mod = importlib.import_module("gradio.themes.utils.fonts")
    sizes_mod = importlib.import_module("gradio.themes.utils.sizes")

    primary_palette = colors_mod.Color(*_TEAL_PALETTE, name="unduhin_teal")
    neutral_palette = colors_mod.Color(*_STONE_PALETTE, name="unduhin_stone")
    compact_radius = sizes_mod.Size(
        name="unduhin_radius",
        xxs="2px",
        xs="4px",
        sm="4px",
        md="6px",
        lg="6px",
        xl="6px",
        xxl="6px",
    )

    theme = gr.themes.Base(
        primary_hue=primary_palette,
        secondary_hue=primary_palette,
        neutral_hue=neutral_palette,
        radius_size=compact_radius,
        font=[fonts_mod.Font(name) for name in UI_FONT_STACK],
        font_mono=[fonts_mod.Font(name) for name in UI_MONO_STACK],
    )
    theme.set(**UI_THEME_VARIABLES)
    return theme


def build_launch_kwargs(
    *,
    port: int = DEFAULT_UI_PORT,
    theme: Any = None,
) -> dict[str, Any]:
    """Return enforced localhost-only, telemetry-free ``Blocks.launch`` kwargs."""
    kwargs: dict[str, Any] = {
        "server_name": LOCALHOST_BIND_HOST,
        "server_port": port,
        "share": False,
        "inbrowser": False,
        "show_error": False,
        "footer_links": [],
        "run_history": False,
        "allowed_paths": [],
        "enable_monitoring": False,
        "css": CUSTOM_CSS,
        "head": THEME_HEAD_SCRIPT,
    }
    if theme is not None:
        kwargs["theme"] = theme
    return kwargs


def _language_update_payload(lang_code: str, *, gr_mod: ModuleType) -> tuple[Any, ...]:
    """Return Gradio component ``update(...)`` payloads for switching UI copy."""
    resolved_lang: SupportedLang = "id" if lang_code == "id" else "en"
    header_md = (
        f"# {t('ui.app_title', lang=resolved_lang)}\n\n"
        f"{t('ui.app_subtitle', lang=resolved_lang)}"
    )
    return (
        gr_mod.update(value=header_md),
        gr_mod.update(label=t("ui.output_dir_label", lang=resolved_lang)),
        gr_mod.update(
            label=t("ui.url_label", lang=resolved_lang),
            placeholder=t("ui.url_placeholder", lang=resolved_lang),
        ),
        gr_mod.update(
            label=t("ui.mode_label", lang=resolved_lang),
            choices=[
                (t("ui.mode_audio", lang=resolved_lang), MediaMode.AUDIO.value),
                (t("ui.mode_video", lang=resolved_lang), MediaMode.VIDEO.value),
            ],
        ),
        gr_mod.update(label=t("ui.language_label", lang=resolved_lang)),
        gr_mod.update(
            label=t("ui.theme_label", lang=resolved_lang),
            choices=[
                (t("ui.theme_system", lang=resolved_lang), "system"),
                (t("ui.theme_light", lang=resolved_lang), "light"),
                (t("ui.theme_dark", lang=resolved_lang), "dark"),
            ],
        ),
        gr_mod.update(value=t("ui.download_button", lang=resolved_lang)),
        gr_mod.update(value=t("ui.cancel_button", lang=resolved_lang)),
        gr_mod.update(
            label=t("ui.status_label", lang=resolved_lang),
            value=t("ui.status_idle", lang=resolved_lang),
        ),
        gr_mod.update(label=t("ui.summary_label", lang=resolved_lang)),
        gr_mod.update(value=t("ui.legal_note", lang=resolved_lang)),
    )


def create_ui_app(
    output_dir: Path | None = None,
    *,
    lang: str | None = None,
    adapter_factory: Callable[[], MediaAdapter] = YtDlpAdapter,
    gr_mod: ModuleType | None = None,
) -> Any:
    """Build and wire the localhost Gradio ``Blocks`` interface."""
    gr = gr_mod if gr_mod is not None else _import_gradio()
    resolved_lang: SupportedLang = detect_language(lang)
    fixed_output_dir = resolve_output_dir(output_dir)
    session = UiSessionController()

    with gr.Blocks(
        analytics_enabled=False,
        title=t("ui.page_title", lang=resolved_lang),
        elem_classes=["unduhin-root"],
    ) as demo:
        header_md = gr.Markdown(
            value=(
                f"# {t('ui.app_title', lang=resolved_lang)}\n\n"
                f"{t('ui.app_subtitle', lang=resolved_lang)}"
            ),
            elem_classes=["unduhin-header"],
        )

        with gr.Row():
            lang_radio = gr.Radio(
                label=t("ui.language_label", lang=resolved_lang),
                choices=[("English", "en"), ("Bahasa Indonesia", "id")],
                value=resolved_lang,
            )
            theme_radio = gr.Radio(
                label=t("ui.theme_label", lang=resolved_lang),
                choices=[
                    (t("ui.theme_system", lang=resolved_lang), "system"),
                    (t("ui.theme_light", lang=resolved_lang), "light"),
                    (t("ui.theme_dark", lang=resolved_lang), "dark"),
                ],
                value="system",
            )

        output_display = gr.Textbox(
            label=t("ui.output_dir_label", lang=resolved_lang),
            value=str(fixed_output_dir),
            interactive=False,
            elem_classes=["unduhin-mono"],
        )
        url_input = gr.Textbox(
            label=t("ui.url_label", lang=resolved_lang),
            placeholder=t("ui.url_placeholder", lang=resolved_lang),
            lines=1,
            max_lines=1,
            max_length=MAX_UI_URL_LENGTH,
        )
        mode_radio = gr.Radio(
            label=t("ui.mode_label", lang=resolved_lang),
            choices=[
                (t("ui.mode_audio", lang=resolved_lang), MediaMode.AUDIO.value),
                (t("ui.mode_video", lang=resolved_lang), MediaMode.VIDEO.value),
            ],
            value=MediaMode.AUDIO.value,
        )

        with gr.Row():
            download_btn = gr.Button(
                value=t("ui.download_button", lang=resolved_lang),
                variant="primary",
            )
            cancel_btn = gr.Button(
                value=t("ui.cancel_button", lang=resolved_lang),
                variant="stop",
            )

        status_box = gr.Textbox(
            label=t("ui.status_label", lang=resolved_lang),
            value=t("ui.status_idle", lang=resolved_lang),
            interactive=False,
            lines=1,
        )
        summary_box = gr.Textbox(
            label=t("ui.summary_label", lang=resolved_lang),
            value="",
            interactive=False,
            lines=2,
        )
        legal_md = gr.Markdown(
            value=t("ui.legal_note", lang=resolved_lang),
            elem_classes=["unduhin-legal"],
        )

        def _handle_download(
            url_val: str,
            mode_val: str,
            selected_lang: str,
        ) -> Iterator[tuple[str, str]]:
            lang_code: SupportedLang = "id" if selected_lang == "id" else "en"
            yield from execute_ui_download(
                url_val,
                mode_val,
                fixed_output_dir,
                lang=lang_code,
                adapter_factory=adapter_factory,
                session=session,
            )

        def _handle_cancel(selected_lang: str) -> tuple[str, str]:
            lang_code: SupportedLang = "id" if selected_lang == "id" else "en"
            if session.request_cancel():
                msg = t("ui.status_cancelled", lang=lang_code)
                return msg, msg
            return t("ui.status_idle", lang=lang_code), ""

        def _handle_language_change(selected_lang: str) -> tuple[Any, ...]:
            return _language_update_payload(selected_lang, gr_mod=gr)

        download_event = download_btn.click(
            fn=_handle_download,
            inputs=[url_input, mode_radio, lang_radio],
            outputs=[status_box, summary_box],
            concurrency_limit=1,
            api_visibility="private",
        )
        cancel_btn.click(
            fn=_handle_cancel,
            inputs=[lang_radio],
            outputs=[status_box, summary_box],
            cancels=[download_event],
            api_visibility="private",
        )
        lang_radio.change(
            fn=_handle_language_change,
            inputs=[lang_radio],
            outputs=[
                header_md,
                output_display,
                url_input,
                mode_radio,
                lang_radio,
                theme_radio,
                download_btn,
                cancel_btn,
                status_box,
                summary_box,
                legal_md,
            ],
            api_visibility="private",
        )
        theme_radio.change(
            fn=None,
            inputs=[theme_radio],
            outputs=None,
            js=THEME_TOGGLE_JS,
            api_visibility="private",
        )
        demo.load(
            fn=None,
            inputs=None,
            outputs=[theme_radio],
            js=THEME_LOAD_JS,
            api_visibility="private",
        )

    return demo


def launch_ui(
    output_dir: Path | None = None,
    *,
    port: int = DEFAULT_UI_PORT,
    lang: str | None = None,
    adapter_factory: Callable[[], MediaAdapter] = YtDlpAdapter,
) -> None:
    """Validate output path, build UI, and run Gradio exclusively on 127.0.0.1."""
    if port < 1 or port > 65535:
        raise ValidationError("cli.invalid_option", detail=f"--port {port} (1-65535)")

    validated_output = resolve_output_dir(output_dir)
    gr = _import_gradio()
    ensure_safe_output_dir(validated_output)
    cleanup_stale_staging_dirs(validated_output)
    resolved_lang = detect_language(lang)
    print(
        safe_console_text(
            t("cli.summary_output_dir", lang=resolved_lang, path=validated_output)
        )
    )

    theme = build_ui_theme(gr)
    demo = create_ui_app(
        validated_output,
        lang=lang,
        adapter_factory=adapter_factory,
        gr_mod=gr,
    )
    launch_kwargs = build_launch_kwargs(port=port, theme=theme)
    try:
        demo.launch(**launch_kwargs)
    except OSError as exc:
        raise DownloadFailedError("error.ui_port_in_use", port=port) from exc
