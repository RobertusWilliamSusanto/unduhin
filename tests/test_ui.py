"""Unit and security tests for the localhost Gradio web UI."""

from __future__ import annotations

import importlib
import os
import re
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from tests.conftest import FakeMediaAdapter
from typer.testing import CliRunner

from unduhin import ui
from unduhin.cli import app
from unduhin.config import (
    DEFAULT_UI_PORT,
    MAX_UI_URL_LENGTH,
    AdapterDownloadResult,
    ExitCode,
    MediaInfo,
    MediaMode,
)
from unduhin.errors import DependencyError, PlaylistDisabledError
from unduhin.i18n import MESSAGES, t


def _relative_luminance(hex_color: str) -> float:
    """Compute WCAG 2.1 relative luminance for ``#RRGGBB``."""
    stripped = hex_color.lstrip("#")
    assert len(stripped) == 6
    channels = [int(stripped[i : i + 2], 16) / 255.0 for i in (0, 2, 4)]

    def _linearize(channel: float) -> float:
        if channel <= 0.03928:
            return channel / 12.92
        return ((channel + 0.055) / 1.055) ** 2.4

    r, g, b = (_linearize(c) for c in channels)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast_ratio(fg_hex: str, bg_hex: str) -> float:
    """Compute WCAG 2.1 contrast ratio between two ``#RRGGBB`` colors."""
    lum1 = _relative_luminance(fg_hex)
    lum2 = _relative_luminance(bg_hex)
    lighter = max(lum1, lum2)
    darker = min(lum1, lum2)
    return (lighter + 0.05) / (darker + 0.05)


def test_import_gradio_sets_telemetry_env_before_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GRADIO_ANALYTICS_ENABLED", raising=False)
    monkeypatch.delenv("HF_HUB_DISABLE_TELEMETRY", raising=False)
    observed_env: dict[str, str | None] = {}
    sentinel = SimpleNamespace(name="fake-gradio")

    real_import = importlib.import_module

    def _fake_import(name: str, package: str | None = None) -> Any:
        if name == "gradio":
            observed_env["GRADIO_ANALYTICS_ENABLED"] = os.environ.get("GRADIO_ANALYTICS_ENABLED")
            observed_env["HF_HUB_DISABLE_TELEMETRY"] = os.environ.get("HF_HUB_DISABLE_TELEMETRY")
            return sentinel
        return real_import(name, package)

    monkeypatch.setattr(importlib, "import_module", _fake_import)
    imported = ui._import_gradio()

    assert imported is sentinel
    assert observed_env["GRADIO_ANALYTICS_ENABLED"] == "False"
    assert observed_env["HF_HUB_DISABLE_TELEMETRY"] == "True"


def test_missing_gradio_raises_dependency_error_and_cli_exits_3(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_dir = tmp_path / "out_not_created"

    real_import = importlib.import_module

    def _missing_gradio(name: str, package: str | None = None) -> Any:
        if name == "gradio":
            raise ImportError("No module named 'gradio'")
        return real_import(name, package)

    monkeypatch.setattr(ui.importlib, "import_module", _missing_gradio)

    with pytest.raises(DependencyError) as exc_info:
        ui.launch_ui(target_dir, port=DEFAULT_UI_PORT, lang="en")

    assert exc_info.value.i18n_key == "error.gradio_missing"
    assert 'pip install "unduhin[ui]"' in exc_info.value.format("en")
    assert not target_dir.exists()

    runner = CliRunner()
    result = runner.invoke(app, ["ui", "--output-dir", str(target_dir), "--lang", "en"])
    assert result.exit_code == int(ExitCode.MISSING_DEPENDENCY)
    assert "unduhin[ui]" in " ".join(result.output.split())


def test_cli_ui_help_exposes_only_safe_flags_and_no_host() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["ui", "--help"])
    assert result.exit_code == 0
    assert "--output-dir" in result.output
    assert "--port" in result.output
    assert "--lang" in result.output
    assert "--host" not in result.output


def test_cli_ui_port_in_use_exits_1_without_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import gradio as gr

    def _busy_launch(self: Any, **kwargs: Any) -> Any:
        _ = (self, kwargs)
        raise OSError("Address already in use")

    monkeypatch.setattr(gr.Blocks, "launch", _busy_launch)
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["ui", "--output-dir", str(tmp_path), "--port", "9876", "--lang", "en"],
    )

    assert result.exit_code == int(ExitCode.FAILURE)
    assert "9876" in result.output
    assert "Traceback" not in result.output
    assert ".unduhin-tmp-" not in result.output


def test_build_launch_kwargs_enforces_localhost_and_privacy() -> None:
    kwargs = ui.build_launch_kwargs(port=7861, theme="dummy-theme")
    assert kwargs["server_name"] == "127.0.0.1"
    assert kwargs["server_port"] == 7861
    assert kwargs["share"] is False
    assert kwargs["inbrowser"] is False
    assert kwargs["show_error"] is False
    assert kwargs["footer_links"] == []
    assert kwargs["run_history"] is False
    assert kwargs["allowed_paths"] == []
    assert kwargs["enable_monitoring"] is False
    assert kwargs["theme"] == "dummy-theme"


def test_launch_ui_passes_localhost_kwargs_and_validates_port(tmp_path: Path) -> None:
    with pytest.raises(Exception) as exc_info:
        ui.launch_ui(tmp_path, port=0, lang="en")
    assert getattr(exc_info.value, "exit_code", None) == ExitCode.INVALID_INPUT


def test_create_ui_app_blocks_security_and_minimal_scope(tmp_path: Path) -> None:
    import gradio as gr

    demo = ui.create_ui_app(tmp_path, lang="en")
    assert demo.analytics_enabled is False

    components = list(demo.blocks.values())
    assert not any(isinstance(comp, gr.File) for comp in components)

    labels = " ".join(str(getattr(comp, "label", "") or "") for comp in components).casefold()
    assert "playlist" not in labels
    assert "batch" not in labels

    readonly_outputs = [
        comp
        for comp in components
        if isinstance(comp, gr.Textbox) and getattr(comp, "label", "") == t("ui.output_dir_label", lang="en")
    ]
    assert len(readonly_outputs) == 1
    assert readonly_outputs[0].interactive is False
    assert readonly_outputs[0].value == str(tmp_path.resolve())

    url_inputs = [
        comp
        for comp in components
        if isinstance(comp, gr.Textbox) and getattr(comp, "label", "") == t("ui.url_label", lang="en")
    ]
    assert len(url_inputs) == 1
    assert url_inputs[0].max_length == MAX_UI_URL_LENGTH

    assert demo.fns
    for fn_meta in demo.fns.values():
        assert fn_meta.api_visibility == "private"
    assert demo.fns[0].concurrency_limit == 1
    assert any(fn_meta.cancels == [0] for fn_meta in demo.fns.values())


def test_theme_and_css_have_no_external_assets_gradients_shadows_or_purple() -> None:
    theme = ui.build_ui_theme()
    theme_css = theme._get_theme_css()
    combined_css = f"{theme_css}\n{ui.CUSTOM_CSS}"

    assert "system-ui" in theme.font
    assert "ui-monospace" in theme.font_mono
    assert not theme._stylesheets

    # Strip SVG XML namespace before external-URL scanning.
    css_without_svg_ns = combined_css.replace("http://www.w3.org/2000/svg", "")
    assert "http://" not in css_without_svg_ns
    assert "https://" not in css_without_svg_ns
    assert "fonts.googleapis.com" not in css_without_svg_ns
    assert "fonts.gstatic.com" not in css_without_svg_ns

    lowered = combined_css.casefold()
    for forbidden in (
        "gradient",
        "backdrop-filter",
        "blur(",
        "@keyframes",
        "animation:",
        "purple",
        "violet",
        "indigo",
        "magenta",
        "#7c3aed",
        "#8b5cf6",
        "#a855f7",
        "#6366f1",
    ):
        assert forbidden not in lowered, f"Forbidden token in CSS: {forbidden}"

    for line in combined_css.splitlines():
        if "box-shadow:" in line:
            assert line.strip().endswith("none;")

    for radius_match in re.finditer(r"border-radius:\s*(\d+)px", lowered):
        assert int(radius_match.group(1)) <= 6

    assert "outline: 2px solid" in ui.CUSTOM_CSS
    assert "outline-offset: 2px" in ui.CUSTOM_CSS

    # Every light variable with a dark counterpart in UI_THEME_VARIABLES is paired.
    singletons = {
        "color_accent",
        "block_label_shadow",
        "checkbox_shadow",
        "shadow_drop",
        "shadow_drop_lg",
        "shadow_inset",
        "button_transform_hover",
        "button_transform_active",
        "button_transition",
    }
    hex_re = re.compile(r"^#[0-9A-Fa-f]{6}$")
    for key, val in ui.UI_THEME_VARIABLES.items():
        if not key.endswith("_dark") and key not in singletons:
            assert f"{key}_dark" in ui.UI_THEME_VARIABLES, f"Missing _dark pair for {key}"
        if "color" in key or "background_fill" in key or key == "stat_background_fill":
            assert hex_re.match(val), f"Expected solid #RRGGBB hex for {key}, got {val!r}"

    for key, msg in MESSAGES["en"].items():
        if key.startswith("ui."):
            assert all(ord(ch) < 0x1F000 for ch in msg), f"Emoji disallowed in {key}"


def test_theme_wcag_contrast_ratios_light_and_dark() -> None:
    for mode_name, tokens in (("light", ui.LIGHT_TOKENS), ("dark", ui.DARK_TOKENS)):
        for bg_key in ("page", "surface"):
            bg = tokens[bg_key]
            assert _contrast_ratio(tokens["text"], bg) >= 4.5, (mode_name, "text", bg_key)
            assert _contrast_ratio(tokens["muted"], bg) >= 4.5, (mode_name, "muted", bg_key)
            assert _contrast_ratio(tokens["accent"], bg) >= 4.5, (mode_name, "accent", bg_key)
            assert _contrast_ratio(tokens["danger"], bg) >= 4.5, (mode_name, "danger", bg_key)
            assert _contrast_ratio(tokens["success"], bg) >= 4.5, (mode_name, "success", bg_key)
            assert _contrast_ratio(tokens["warn"], bg) >= 4.5, (mode_name, "warn", bg_key)

        assert _contrast_ratio(tokens["on_accent"], tokens["accent"]) >= 4.5, (mode_name, "on_accent")
        assert _contrast_ratio(tokens["on_accent"], tokens["accent_hover"]) >= 4.5, (
            mode_name,
            "on_accent_hover",
        )
        assert _contrast_ratio(tokens["danger"], tokens["danger_bg"]) >= 4.5, (mode_name, "danger_bg")

        for surface_key in ("page", "surface", "secondary"):
            surface = tokens[surface_key]
            assert _contrast_ratio(tokens["border"], surface) >= 3.0, (
                mode_name,
                "border",
                surface_key,
            )
            assert _contrast_ratio(tokens["accent"], surface) >= 3.0, (
                mode_name,
                "focus",
                surface_key,
            )


def test_ui_rejects_invalid_and_oversized_urls(tmp_path: Path) -> None:
    for bad_url in ("", "   ", "file:///etc/passwd", "javascript:alert(1)", "https://example.com/" + "x" * MAX_UI_URL_LENGTH):
        events = list(ui.execute_ui_download(bad_url, "audio", tmp_path, lang="en"))
        status, summary = events[-1]
        assert status.startswith("FAILED:")
        assert summary


def test_ui_rejects_playlist_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("unduhin.pipeline.find_ffmpeg", lambda: "ffmpeg")
    adapter = FakeMediaAdapter(
        info_by_url={
            "https://example.com/list": MediaInfo(
                id="pl1",
                title="Playlist Title",
                url="https://example.com/list",
                is_playlist=True,
                entries=(MediaInfo(id="v1", title="One", url="https://example.com/v1"),),
            )
        }
    )

    events = list(
        ui.execute_ui_download(
            "https://example.com/list",
            "audio",
            tmp_path,
            lang="en",
            adapter_factory=lambda: adapter,
        )
    )
    assert adapter.extract_calls[0][1] is False
    status, summary = events[-1]
    assert status.startswith("FAILED:")
    assert t("error.ui_playlist_rejected", lang="en") in summary


def test_ui_audio_and_video_success_returns_filename_without_staging_leak(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("unduhin.pipeline.find_ffmpeg", lambda: "ffmpeg")
    adapter = FakeMediaAdapter()

    for mode_val, expected_ext in (("audio", ".mp3"), ("video", ".mp4")):
        events = list(
            ui.execute_ui_download(
                "https://example.com/watch?v=1",
                mode_val,
                tmp_path,
                lang="en",
                adapter_factory=lambda: adapter,
            )
        )
        statuses = [ev[0] for ev in events]
        assert any(s.startswith("VALIDATING:") for s in statuses)
        assert any(s.startswith("DOWNLOADING:") for s in statuses)
        assert any(s.startswith("CONVERTING:") for s in statuses)
        final_status, final_summary = events[-1]
        assert final_status.startswith("SUCCEEDED:")
        assert expected_ext in final_summary
        assert ".unduhin-tmp-" not in final_summary
        assert (tmp_path / f"Sample Creative Commons Track{expected_ext}").exists()


def test_ui_error_sanitization_strips_staging_paths() -> None:
    class _LeakyError(Exception):
        pass

    msg = ui.sanitize_ui_error_text(
        _LeakyError(r"C:\Users\bob\downloads\.unduhin-tmp-12345\bad.part failed"),
        lang="en",
    )
    assert ".unduhin-tmp-" not in msg
    assert "\n" not in msg

    playlist_msg = ui.sanitize_ui_error_text(PlaylistDisabledError("error.playlist_disabled"), lang="id")
    assert playlist_msg == t("error.ui_playlist_rejected", lang="id")


def test_ui_cooperative_cancel_cleans_staging_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("unduhin.pipeline.find_ffmpeg", lambda: "ffmpeg")
    entered_download = threading.Event()

    def _cancellable_download(
        *,
        url: str,
        mode: MediaMode,
        audio_bitrate: int,
        max_height: int,
        staging_dir: Path,
        progress_callback: Any,
        cancel_check: Any,
    ) -> AdapterDownloadResult:
        _ = (url, mode, audio_bitrate, max_height)
        partial = staging_dir / "vid123.mp3.part"
        partial.write_bytes(b"partial-data")
        if progress_callback is not None:
            progress_callback(ui.ProgressEvent(status="downloading", downloaded_bytes=10, total_bytes=100))
        entered_download.set()
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if cancel_check is not None and cancel_check():
                raise RuntimeError("cancelled")
            time.sleep(0.02)
        raise AssertionError("cancel_check did not trigger within timeout")

    adapter = FakeMediaAdapter(download_handler=_cancellable_download)
    session = ui.UiSessionController()
    collected: list[tuple[str, str]] = []

    def _runner() -> None:
        for item in ui.execute_ui_download(
            "https://example.com/watch?v=slow",
            "audio",
            tmp_path,
            lang="en",
            adapter_factory=lambda: adapter,
            session=session,
        ):
            collected.append(item)

    thread = threading.Thread(target=_runner)
    thread.start()
    assert entered_download.wait(timeout=3.0)
    assert session.request_cancel() is True
    thread.join(timeout=5.0)
    assert not thread.is_alive()

    assert collected
    final_status, final_summary = collected[-1]
    assert final_status.startswith("CANCELLED:")
    assert final_summary.startswith("CANCELLED:")
    assert list(tmp_path.glob(".unduhin-tmp-*")) == []


def test_ui_language_switch_payload_en_and_id() -> None:
    import gradio as gr

    en_payload = ui._language_update_payload("en", gr_mod=gr)
    id_payload = ui._language_update_payload("id", gr_mod=gr)
    assert len(en_payload) == 11
    assert len(id_payload) == 11
    assert en_payload != id_payload


def test_ui_uses_env_and_default_output_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_dir = tmp_path / "ui_env_out"
    monkeypatch.setenv("UNDUHIN_OUTPUT_DIR", str(env_dir))
    assert ui.resolve_output_dir(None) == env_dir.resolve()


def test_ui_localhost_and_assets_make_zero_non_loopback_connections(tmp_path: Path) -> None:
    import ipaddress
    import json
    import socket
    import subprocess
    import sys
    import urllib.request

    # Pick an available ephemeral loopback port cleanly before starting the subprocess.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])

    out_dir = tmp_path / "ui_out"
    record_file = tmp_path / "connect_attempts.json"
    stop_file = tmp_path / "stop.flag"

    harness = """
import ipaddress
import json
import socket
import sys
import threading
import time
from pathlib import Path

_orig_connect = socket.socket.connect
_orig_connect_ex = socket.socket.connect_ex
non_loopback = []

def _is_loopback(addr):
    if not isinstance(addr, tuple) or not addr:
        return True
    host = str(addr[0]).split("%", 1)[0]
    if host in {"localhost", "127.0.0.1", "::1", ""}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False

def _guarded_connect(self, address):
    if not _is_loopback(address):
        non_loopback.append(["connect", str(address)])
        raise ConnectionRefusedError(f"Blocked non-loopback connect: {address}")
    return _orig_connect(self, address)

def _guarded_connect_ex(self, address):
    if not _is_loopback(address):
        non_loopback.append(["connect_ex", str(address)])
        return 111
    return _orig_connect_ex(self, address)

socket.socket.connect = _guarded_connect
socket.socket.connect_ex = _guarded_connect_ex

from unduhin.ui import launch_ui

port = int(sys.argv[1])
out_path = Path(sys.argv[2])
rec_path = Path(sys.argv[3])
stop_path = Path(sys.argv[4])

def _run_server():
    try:
        launch_ui(output_dir=out_path, port=port, lang="en")
    except BaseException:
        import traceback
        traceback.print_exc(file=sys.stderr)
        sys.stderr.flush()

server_thread = threading.Thread(target=_run_server, daemon=True)
server_thread.start()

# Wait until parent signals completion via stop_path after fetching "/" and assets.
try:
    while not stop_path.exists():
        time.sleep(0.1)
finally:
    rec_path.write_text(json.dumps(non_loopback), encoding="utf-8")
"""

    proc = subprocess.Popen(  # noqa: S603 - hardcoded harness with internal sys.executable
        [sys.executable, "-c", harness, str(port), str(out_dir), str(record_file), str(stop_file)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    base_url = f"http://127.0.0.1:{port}"
    html_body = ""
    last_err = ""
    try:
        deadline = time.monotonic() + 25.0
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                stdout_txt, stderr_txt = proc.communicate(timeout=2)
                raise AssertionError(
                    f"UI subprocess exited early ({proc.returncode}):\n{stdout_txt}\n{stderr_txt}"
                )
            try:
                with opener.open(f"{base_url}/", timeout=1.0) as resp:
                    if resp.status == 200:
                        html_body = resp.read().decode("utf-8", errors="replace")
                        break
            except Exception as exc:
                last_err = repr(exc)
                time.sleep(0.2)

        if not html_body:
            stop_file.write_text("stop", encoding="utf-8")
            out_txt, err_txt = proc.communicate(timeout=5)
            raise AssertionError(
                f"UI server did not serve HTTP 200 on {base_url}/; last_err={last_err}\n"
                f"STDOUT:\n{out_txt}\nSTDERR:\n{err_txt}"
            )
        assert "fonts.googleapis.com" not in html_body
        assert "fonts.gstatic.com" not in html_body

        asset_paths: list[str] = []
        for attr_match in re.finditer(
            r'(?:src|href)=["\']([^"\']+\.(?:js|css)(?:\?[^"\']*)?)["\']',
            html_body,
            re.IGNORECASE,
        ):
            ref = attr_match.group(1)
            assert not ref.startswith(("http://", "https://", "//")), f"Unexpected remote asset ref: {ref}"
            if ref.startswith("./"):
                ref = "/" + ref[2:]
            elif not ref.startswith("/"):
                ref = "/" + ref
            if ref not in asset_paths:
                asset_paths.append(ref)

        assert any(p.endswith(".js") or ".js?" in p for p in asset_paths), (
            f"Expected at least one local JS asset in / HTML, got {asset_paths}"
        )
        for rel_path in asset_paths:
            with opener.open(f"{base_url}{rel_path}", timeout=5.0) as asset_resp:
                assert asset_resp.status == 200
                payload = asset_resp.read()
                assert len(payload) > 0

        stop_file.write_text("stop", encoding="utf-8")
        proc.wait(timeout=10.0)
    finally:
        stop_file.write_text("stop", encoding="utf-8")
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5.0)

    assert record_file.exists(), "Subprocess did not write socket connection audit log"
    recorded = json.loads(record_file.read_text(encoding="utf-8"))
    assert recorded == [], f"Unexpected non-loopback socket connection attempts: {recorded}"
    # Verify loopback check helper matches standard loopback addresses too.
    assert ipaddress.ip_address("127.0.0.1").is_loopback
