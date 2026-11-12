"""Tests for Typer CLI subcommands, flags, legal notice, and exit codes."""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest
from tests.conftest import FakeMediaAdapter
from typer.testing import CliRunner

from unduhin import __version__
from unduhin.cli import app, main
from unduhin.config import AdapterDownloadResult, ExitCode, FormatOption, MediaInfo
from unduhin.doctor import CheckStatus, CheckTier, DoctorCheck, DoctorReport

runner = CliRunner()


def test_cli_help_includes_bilingual_legal_notice() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "Creative Commons" in result.stdout
    assert "Catatan Legal" in result.stdout


def test_cli_version_flag() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert f"unduhin {__version__}" in result.stdout


def test_cli_doctor_exit_codes(monkeypatch: pytest.MonkeyPatch) -> None:
    ready_report = DoctorReport(
        os_family="linux",
        checks=(DoctorCheck("Python", CheckStatus.OK, "Python 3.13.0"),),
    )
    monkeypatch.setattr("unduhin.doctor.run_doctor", lambda lang=None: ready_report)
    res_ok = runner.invoke(app, ["doctor", "--lang", "en"], env={"COLUMNS": "140"})
    assert res_ok.exit_code == int(ExitCode.SUCCESS)
    assert "Environment looks good" in res_ok.stdout

    # Recommended-only warnings (e.g. Deno/yt-dlp-ejs missing) -> exit 0 + YouTube-only notice
    warn_report = DoctorReport(
        os_family="linux",
        checks=(
            DoctorCheck("Python", CheckStatus.OK, "Python 3.13.0"),
            DoctorCheck("FFmpeg", CheckStatus.OK, "FFmpeg 7.1 at /usr/bin/ffmpeg"),
            DoctorCheck(
                "Deno",
                CheckStatus.WARN,
                "Deno not found (optional: only required for YouTube)",
                "curl -fsSL https://deno.land/install.sh | sh",
                tier=CheckTier.RECOMMENDED,
            ),
        ),
    )
    monkeypatch.setattr("unduhin.doctor.run_doctor", lambda lang=None: warn_report)
    res_warn = runner.invoke(app, ["doctor", "--lang", "en"], env={"COLUMNS": "140"})
    assert res_warn.exit_code == int(ExitCode.SUCCESS)
    assert "YouTube" in res_warn.stdout

    missing_report = DoctorReport(
        os_family="windows",
        checks=(
            DoctorCheck("FFmpeg", CheckStatus.MISSING, "FFmpeg missing", "winget install Gyan.FFmpeg"),
        ),
    )
    monkeypatch.setattr("unduhin.doctor.run_doctor", lambda lang=None: missing_report)
    res_missing = runner.invoke(app, ["doctor", "--lang", "en"], env={"COLUMNS": "140"})
    assert res_missing.exit_code == int(ExitCode.MISSING_DEPENDENCY)
    assert "winget install Gyan.FFmpeg" in res_missing.stdout


def test_cli_download_success_and_verbose(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("unduhin.pipeline.find_ffmpeg", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr("unduhin.pipeline.find_deno", lambda: "/usr/bin/deno")

    fake_adapter = FakeMediaAdapter()
    captured_verbose: list[bool] = []

    def _fake_factory(*, verbose: bool, console: object) -> FakeMediaAdapter:
        _ = console
        captured_verbose.append(verbose)
        return fake_adapter

    monkeypatch.setattr("unduhin.cli._create_adapter", _fake_factory)

    out_dir = tmp_path / "dl"
    res = runner.invoke(
        app,
        [
            "download",
            "https://example.org/cc-video",
            "--mode",
            "audio",
            "--bitrate",
            "256",
            "--output-dir",
            str(out_dir),
            "--lang",
            "id",
            "--verbose",
        ],
    )
    assert res.exit_code == int(ExitCode.SUCCESS)
    assert captured_verbose == [True]
    assert "Berhasil: 1" in res.stdout
    assert (out_dir / "Sample Creative Commons Track.mp3").exists()


def test_cli_download_invalid_url_returns_exit_2(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("unduhin.pipeline.find_ffmpeg", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr("unduhin.cli._create_adapter", lambda **_kw: FakeMediaAdapter())

    untouched = tmp_path / "should_not_exist"
    res = runner.invoke(
        app,
        ["download", "file:///etc/passwd", "--output-dir", str(untouched), "--lang", "en"],
    )
    assert res.exit_code == int(ExitCode.INVALID_INPUT)
    assert "Invalid URL" in res.output
    assert not untouched.exists()


def test_cli_download_missing_ffmpeg_returns_exit_3(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("unduhin.pipeline.find_ffmpeg", lambda: None)
    monkeypatch.setattr("unduhin.cli._create_adapter", lambda **_kw: FakeMediaAdapter())

    res = runner.invoke(
        app,
        [
            "download",
            "https://example.org/video",
            "--mode",
            "audio",
            "--output-dir",
            str(tmp_path),
            "--lang",
            "en",
        ],
    )
    assert res.exit_code == int(ExitCode.MISSING_DEPENDENCY)
    assert "FFmpeg is required" in res.output


def test_cli_subcommand_help_shows_phase2_options_and_legal_notice() -> None:
    dl_help = runner.invoke(app, ["download", "--help"], env={"COLUMNS": "140"})
    assert dl_help.exit_code == 0
    for token in ("--batch", "--playlist", "--max-items", "--dry-run", "Creative Commons", "Catatan Legal"):
        assert token in dl_help.output

    info_help = runner.invoke(app, ["info", "--help"], env={"COLUMNS": "140"})
    assert info_help.exit_code == 0
    for token in ("--mode", "--playlist", "--max-items", "Creative Commons", "Catatan Legal"):
        assert token in info_help.output


def test_cli_info_single_and_playlist_without_ffmpeg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("unduhin.pipeline.find_ffmpeg", lambda: None)
    monkeypatch.setattr("unduhin.pipeline.find_deno", lambda: None)

    video_url = "https://example.org/sample"
    pl_url = "https://example.org/pl"
    infos = {
        video_url: MediaInfo(
            id="s1",
            title="CC Acoustic Track",
            url=video_url,
            uploader="Indie Musician",
            duration_seconds=125.0,
            filesize_approx=2_500_000,
            formats=(
                FormatOption(
                    format_id="22",
                    ext="mp4",
                    height=720,
                    vcodec="avc1.64001F",
                    acodec="mp4a.40.2",
                    filesize_approx=2_500_000,
                ),
            ),
        ),
        pl_url: MediaInfo(
            id="pl1",
            title="Study Tunes",
            url=pl_url,
            is_playlist=True,
            playlist_count=3,
            entries=(
                MediaInfo(id="e1", title="One", url="https://example.org/e1", duration_seconds=60.0),
                MediaInfo(id="e2", title="Two", url="https://example.org/e2", duration_seconds=90.0),
                MediaInfo(id="e3", title="Three", url="https://example.org/e3", duration_seconds=120.0),
            ),
        ),
    }
    adapter = FakeMediaAdapter(info_by_url=infos)
    monkeypatch.setattr("unduhin.cli._create_adapter", lambda **_kw: adapter)

    res_single = runner.invoke(
        app,
        ["info", video_url, "--mode", "video", "--max-height", "720", "--lang", "en"],
        env={"COLUMNS": "140"},
    )
    assert res_single.exit_code == int(ExitCode.SUCCESS)
    assert "CC Acoustic Track" in res_single.stdout
    assert "Indie Musician" in res_single.stdout
    assert "No" in res_single.stdout

    res_pl_blocked = runner.invoke(app, ["info", pl_url, "--lang", "en"])
    assert res_pl_blocked.exit_code == int(ExitCode.INVALID_INPUT)
    assert "--playlist" in res_pl_blocked.output

    res_pl_ok = runner.invoke(
        app,
        ["info", pl_url, "--playlist", "--max-items", "2", "--lang", "en"],
        env={"COLUMNS": "140"},
    )
    assert res_pl_ok.exit_code == int(ExitCode.SUCCESS)
    assert "Study Tunes" in res_pl_ok.stdout
    assert "truncated to 2" in res_pl_ok.output
    assert "One" in res_pl_ok.stdout
    assert "Two" in res_pl_ok.stdout

    res_bad = runner.invoke(app, ["info", "file:///etc/passwd", "--lang", "en"])
    assert res_bad.exit_code == int(ExitCode.INVALID_INPUT)


def test_cli_download_batch_with_bom_comments_positional_and_dedup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("unduhin.pipeline.find_ffmpeg", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr("unduhin.pipeline.find_deno", lambda: "/usr/bin/deno")

    u1 = "https://example.org/first"
    u2 = "https://example.org/second"
    adapter = FakeMediaAdapter(
        info_by_url={
            u1: MediaInfo(id="id1", title="First Song", url=u1, filesize_approx=100_000),
            u2: MediaInfo(id="id2", title="Second Song", url=u2, filesize_approx=100_000),
        }
    )
    monkeypatch.setattr("unduhin.cli._create_adapter", lambda **_kw: adapter)

    batch_file = tmp_path / "batch.txt"
    batch_file.write_text(
        f"\ufeff# comment\n\n{u2}\n{u1}\n{u2}\n",
        encoding="utf-8",
    )
    out_dir = tmp_path / "out"
    res = runner.invoke(
        app,
        [
            "download",
            u1,
            "--batch",
            str(batch_file),
            "--mode",
            "audio",
            "--output-dir",
            str(out_dir),
            "--lang",
            "en",
        ],
        env={"COLUMNS": "140"},
    )
    assert res.exit_code == int(ExitCode.SUCCESS)
    assert "Removed 2 duplicate URL(s)" in res.stdout
    assert "Succeeded: 2 | Failed: 0 | Skipped (exists): 0 | Skipped (unsupported): 0" in res.stdout
    assert (out_dir / "First Song.mp3").exists()
    assert (out_dir / "Second Song.mp3").exists()


def test_cli_download_batch_invalid_line_fails_fast_exit_2(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("unduhin.pipeline.find_ffmpeg", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr("unduhin.cli._create_adapter", lambda **_kw: FakeMediaAdapter())

    batch_file = tmp_path / "bad_batch.txt"
    batch_file.write_text(
        "# header\nhttps://example.org/ok\njavascript:alert(1)\n",
        encoding="utf-8",
    )
    untouched = tmp_path / "never_created"
    res = runner.invoke(
        app,
        ["download", "--batch", str(batch_file), "--output-dir", str(untouched), "--lang", "en"],
    )
    assert res.exit_code == int(ExitCode.INVALID_INPUT)
    assert "line 3" in res.output
    assert not untouched.exists()


def test_cli_download_dry_run_all_skipped_and_conflict_docs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("unduhin.pipeline.find_ffmpeg", lambda: None)
    monkeypatch.setattr("unduhin.pipeline.find_deno", lambda: None)

    dl_help = runner.invoke(app, ["download", "--help"], env={"COLUMNS": "140"})
    assert dl_help.exit_code == 0
    assert "intra-batch" in dl_help.output

    url = "https://example.org/track"
    adapter = FakeMediaAdapter(
        info_by_url={url: MediaInfo(id="t1", title="Dry Track", url=url, filesize_approx=100_000)}
    )
    monkeypatch.setattr("unduhin.cli._create_adapter", lambda **_kw: adapter)

    untouched = tmp_path / "dry_out"
    res_dry = runner.invoke(
        app,
        [
            "download",
            url,
            "--mode",
            "audio",
            "--dry-run",
            "--output-dir",
            str(untouched),
            "--lang",
            "en",
        ],
        env={"COLUMNS": "140"},
    )
    assert res_dry.exit_code == int(ExitCode.SUCCESS)
    assert "DRY-RUN" in res_dry.stdout
    assert "Planned: 1 | Failed: 0 | Skipped (exists): 0 | Skipped (unsupported): 0" in res_dry.stdout
    assert not untouched.exists()

    # Live-stream skip is unsupported -> exit 1 (SKIPPED UNSUPPORTED)
    monkeypatch.setattr("unduhin.pipeline.find_ffmpeg", lambda: "/usr/bin/ffmpeg")
    live_url = "https://example.org/live"
    live_adapter = FakeMediaAdapter(
        info_by_url={
            live_url: MediaInfo(id="lv", title="Stream", url=live_url, is_live=True)
        }
    )
    monkeypatch.setattr("unduhin.cli._create_adapter", lambda **_kw: live_adapter)

    res_live = runner.invoke(
        app,
        [
            "download",
            live_url,
            "--mode",
            "audio",
            "--output-dir",
            str(tmp_path / "live_out"),
            "--lang",
            "en",
        ],
        env={"COLUMNS": "140"},
    )
    assert res_live.exit_code == int(ExitCode.FAILURE)
    assert "SKIPPED (UNSUPPORTED)" in res_live.stdout

    # Benign all-skipped batch (existing file under --on-conflict skip) -> exit 0 + notice
    skip_out = tmp_path / "skip_out"
    skip_out.mkdir()
    (skip_out / "Existing.mp3").write_bytes(b"already-here")
    exist_url = "https://example.org/exist"
    monkeypatch.setattr(
        "unduhin.cli._create_adapter",
        lambda **_kw: FakeMediaAdapter(
            info_by_url={exist_url: MediaInfo(id="e1", title="Existing", url=exist_url)}
        ),
    )
    res_skipped = runner.invoke(
        app,
        [
            "download",
            exist_url,
            "--mode",
            "audio",
            "--on-conflict",
            "skip",
            "--output-dir",
            str(skip_out),
            "--lang",
            "id",
        ],
        env={"COLUMNS": "140"},
    )
    assert res_skipped.exit_code == int(ExitCode.SUCCESS)
    assert "Semua item dilewati" in res_skipped.stdout


def test_cli_summary_marks_intra_batch_duplicate_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("unduhin.pipeline.find_ffmpeg", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr("unduhin.pipeline.find_deno", lambda: "/usr/bin/deno")

    u1 = "https://example.org/dup-a"
    u2 = "https://example.org/dup-b"
    adapter = FakeMediaAdapter(
        info_by_url={
            u1: MediaInfo(id="da", title="Same Title", url=u1, filesize_approx=100_000),
            u2: MediaInfo(id="db", title="Same Title", url=u2, filesize_approx=100_000),
        }
    )
    monkeypatch.setattr("unduhin.cli._create_adapter", lambda **_kw: adapter)
    batch_file = tmp_path / "dups.txt"
    batch_file.write_text(f"{u1}\n{u2}\n", encoding="utf-8")

    out_dir = tmp_path / "dup_out"
    res = runner.invoke(
        app,
        [
            "download",
            "--batch",
            str(batch_file),
            "--mode",
            "audio",
            "--output-dir",
            str(out_dir),
            "--lang",
            "en",
        ],
        env={"COLUMNS": "140"},
    )
    assert res.exit_code == int(ExitCode.SUCCESS)
    normalized_stdout = " ".join(res.stdout.split())
    assert "(intra-batch rename)" in normalized_stdout
    assert str(out_dir.resolve()) in res.stdout
    assert (out_dir / "Same Title.mp3").exists()
    assert (out_dir / "Same Title (1).mp3").exists()


@pytest.mark.parametrize(
    ("cli_args", "expected_rc"),
    [
        (["--version"], 0),
        (["--help"], 0),
        ([], 0),
        (["doctor", "--lang", "en"], None),
        (["ui", "--help"], 0),
    ],
)
def test_cli_lazy_imports_never_load_ytdlp_or_gradio(
    cli_args: list[str],
    expected_rc: int | None,
) -> None:
    probe = (
        "import json, sys; "
        "from unduhin.cli import main; "
        "sys.argv = ['unduhin', *sys.argv[1:]]; "
        "rc = main(); "
        "print('__PROBE__:' + json.dumps({"
        "'rc': rc, "
        "'yt_dlp': 'yt_dlp' in sys.modules, "
        "'gradio': 'gradio' in sys.modules"
        "}))"
    )
    proc = subprocess.run(  # noqa: S603 - hardcoded probe script with internal sys.executable
        [sys.executable, "-c", probe, *cli_args],
        capture_output=True,
        text=True,
        check=False,
    )
    marker_lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("__PROBE__:")]
    assert marker_lines, f"Probe output missing:\n{proc.stdout}\n{proc.stderr}"
    payload = json.loads(marker_lines[-1].removeprefix("__PROBE__:"))
    if expected_rc is not None:
        assert payload["rc"] == expected_rc
    assert payload["yt_dlp"] is False
    assert payload["gradio"] is False


def test_cli_no_args_welcome_help_and_env_lang(
    clean_locale_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ = clean_locale_env
    res_en = runner.invoke(app, [])
    assert res_en.exit_code == int(ExitCode.SUCCESS)
    assert "unduhin doctor" in res_en.stdout
    assert "--help" in res_en.stdout
    assert "--mode audio" in res_en.stdout
    assert "--mode video" in res_en.stdout
    assert "unduhin ui" in res_en.stdout
    assert "Usage:" not in res_en.stdout

    monkeypatch.setenv("UNDUHIN_LANG", "id")
    res_id = runner.invoke(app, [])
    assert res_id.exit_code == int(ExitCode.SUCCESS)
    assert "Contoh:" in res_id.stdout
    assert "unduhin doctor" in res_id.stdout

    # Explicit --lang overrides UNDUHIN_LANG
    res_override = runner.invoke(app, ["--lang", "en"])
    assert res_override.exit_code == int(ExitCode.SUCCESS)
    assert "Examples:" in res_override.stdout


def test_cli_url_shortcut_and_invalid_token_routing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("unduhin.pipeline.find_ffmpeg", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr("unduhin.pipeline.find_deno", lambda: "/usr/bin/deno")
    monkeypatch.setattr("unduhin.cli._create_adapter", lambda **_kw: FakeMediaAdapter())

    out_dir = tmp_path / "shortcut_out"
    res_shortcut = runner.invoke(
        app,
        [
            "https://example.org/cc-shortcut",
            "--mode",
            "audio",
            "--output-dir",
            str(out_dir),
            "--lang",
            "en",
        ],
        env={"COLUMNS": "140"},
    )
    assert res_shortcut.exit_code == int(ExitCode.SUCCESS)
    assert (out_dir / "Sample Creative Commons Track.mp3").exists()

    # Unknown non-option token is treated as an invalid URL (exit 2), NOT "No such command".
    res_bad = runner.invoke(app, ["sembarang", "--lang", "en"])
    assert res_bad.exit_code == int(ExitCode.INVALID_INPUT)
    assert "Invalid URL" in res_bad.output
    assert "No such command" not in res_bad.output


def test_cli_omitted_mode_non_tty_fails_and_tty_prompts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("unduhin.pipeline.find_ffmpeg", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr("unduhin.pipeline.find_deno", lambda: "/usr/bin/deno")
    monkeypatch.setattr("unduhin.cli._create_adapter", lambda **_kw: FakeMediaAdapter())

    out_dir = tmp_path / "mode_out"
    monkeypatch.setattr("unduhin.cli._is_interactive_tty", lambda: False)
    res_non_tty = runner.invoke(
        app,
        ["download", "https://example.org/cc", "--output-dir", str(out_dir), "--lang", "en"],
    )
    assert res_non_tty.exit_code == int(ExitCode.INVALID_INPUT)
    assert "--mode audio or --mode video" in res_non_tty.output
    assert not out_dir.exists()

    # TTY interactive selection retries invalid entry cleanly then chooses MP3 (1) or MP4 (2).
    monkeypatch.setattr("unduhin.cli._is_interactive_tty", lambda: True)
    res_tty_mp3 = runner.invoke(
        app,
        ["download", "https://example.org/cc", "--output-dir", str(out_dir), "--lang", "en"],
        input="9\n1\n",
        env={"COLUMNS": "140"},
    )
    assert res_tty_mp3.exit_code == int(ExitCode.SUCCESS)
    assert (out_dir / "Sample Creative Commons Track.mp3").exists()

    res_tty_mp4 = runner.invoke(
        app,
        ["https://example.org/cc-vid", "--output-dir", str(out_dir), "--lang", "en"],
        input="2\n",
        env={"COLUMNS": "140"},
    )
    assert res_tty_mp4.exit_code == int(ExitCode.SUCCESS)
    assert (out_dir / "Sample Creative Commons Track.mp4").exists()


def test_cli_output_dir_env_precedence_and_invalid_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("unduhin.pipeline.find_ffmpeg", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr("unduhin.pipeline.find_deno", lambda: "/usr/bin/deno")
    monkeypatch.setattr("unduhin.cli._create_adapter", lambda **_kw: FakeMediaAdapter())

    env_dir = tmp_path / "env_dl"
    monkeypatch.setenv("UNDUHIN_OUTPUT_DIR", str(env_dir))
    res_env = runner.invoke(
        app,
        ["https://example.org/env", "--mode", "audio", "--lang", "en"],
        env={"COLUMNS": "160"},
    )
    assert res_env.exit_code == int(ExitCode.SUCCESS)
    assert (env_dir / "Sample Creative Commons Track.mp3").exists()
    assert str(env_dir.resolve()) in res_env.stdout

    blocker = tmp_path / "blocker_file"
    blocker.write_text("file", encoding="utf-8")
    monkeypatch.setenv("UNDUHIN_OUTPUT_DIR", str(blocker))
    res_bad_env = runner.invoke(
        app,
        ["https://example.org/env", "--mode", "audio", "--lang", "en"],
    )
    assert res_bad_env.exit_code == int(ExitCode.INVALID_INPUT)


def test_cli_global_error_handling_completion_disabled_and_ctrl_c(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Shell completion options are disabled.
    res_comp = runner.invoke(app, ["--install-completion"])
    assert res_comp.exit_code != 0

    def _boom(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("unexpected internal crash")

    monkeypatch.setattr("unduhin.doctor.run_doctor", _boom)

    # Without --verbose: exit 1, friendly i18n message, NO raw Python traceback.
    monkeypatch.setattr(sys, "argv", ["unduhin", "doctor", "--lang", "en"])
    rc_quiet = main()
    captured_quiet = capsys.readouterr()
    assert rc_quiet == int(ExitCode.FAILURE)
    assert "--verbose" in (captured_quiet.out + captured_quiet.err)
    assert "Traceback (most recent call last)" not in (captured_quiet.out + captured_quiet.err)

    # With --verbose: exit 1 AND traceback is printed to stderr.
    monkeypatch.setattr(sys, "argv", ["unduhin", "doctor", "--lang", "en", "--verbose"])
    rc_verbose = main()
    captured_verbose = capsys.readouterr()
    assert rc_verbose == int(ExitCode.FAILURE)
    assert "Traceback (most recent call last)" in captured_verbose.err

    # KeyboardInterrupt on any command exits with 130.
    def _interrupt(*_args: object, **_kwargs: object) -> object:
        raise KeyboardInterrupt

    monkeypatch.setattr("unduhin.doctor.run_doctor", _interrupt)
    monkeypatch.setattr(sys, "argv", ["unduhin", "doctor", "--lang", "en"])
    assert main() == int(ExitCode.CANCELLED)


def test_cli_non_utf8_stdout_does_not_crash_with_cjk_or_emoji(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("unduhin.pipeline.find_ffmpeg", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr("unduhin.pipeline.find_deno", lambda: "/usr/bin/deno")

    url = "https://example.org/cjk"
    adapter = FakeMediaAdapter(
        info_by_url={url: MediaInfo(id="cjk1", title="日本語 🎵 test", url=url, filesize_approx=50_000)}
    )
    monkeypatch.setattr("unduhin.cli._create_adapter", lambda **_kw: adapter)

    raw_out = io.BytesIO()
    raw_err = io.BytesIO()
    strict_out = io.TextIOWrapper(raw_out, encoding="cp1252", errors="strict")
    strict_err = io.TextIOWrapper(raw_err, encoding="cp1252", errors="strict")

    from rich.console import Console

    monkeypatch.setattr(
        "unduhin.cli._get_consoles",
        lambda: (
            Console(file=strict_out, force_terminal=False, no_color=True, soft_wrap=True),
            Console(file=strict_err, force_terminal=False, no_color=True, soft_wrap=True),
        ),
    )

    out_dir = tmp_path / "cjk_out"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "unduhin",
            "download",
            url,
            "--mode",
            "audio",
            "--output-dir",
            str(out_dir),
            "--lang",
            "en",
        ],
    )
    rc = main()
    strict_out.flush()
    strict_err.flush()
    assert rc == int(ExitCode.SUCCESS)
    assert b"test" in raw_out.getvalue()


def test_cli_download_cancel_signals_cleanup_staging_and_restore_handlers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import signal

    monkeypatch.setattr("unduhin.pipeline.find_ffmpeg", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr("unduhin.pipeline.find_deno", lambda: "/usr/bin/deno")

    target_sig = getattr(signal, "SIGBREAK", signal.SIGINT)
    orig_sigint = signal.getsignal(signal.SIGINT)
    orig_target = signal.getsignal(target_sig)
    url = "https://example.org/cancel-sig"

    def _download_until_signal(
        *,
        staging_dir: Path,
        cancel_check: object,
        **_kwargs: object,
    ) -> AdapterDownloadResult:
        (staging_dir / "part.mp4.part").write_bytes(b"partial")
        handler = signal.getsignal(target_sig)
        assert callable(handler)
        handler(int(target_sig), None)
        assert callable(cancel_check) and cancel_check() is True
        from unduhin.errors import CancelledError

        raise CancelledError("error.cancelled")

    adapter = FakeMediaAdapter(
        info_by_url={url: MediaInfo(id="c1", title="Cancel Me", url=url, filesize_approx=10_000)},
        download_handler=_download_until_signal,
    )
    monkeypatch.setattr("unduhin.cli._create_adapter", lambda **_kw: adapter)

    out_dir = tmp_path / "cancel_out"
    res = runner.invoke(
        app,
        ["download", url, "--mode", "audio", "--output-dir", str(out_dir), "--lang", "en"],
    )
    assert res.exit_code == int(ExitCode.CANCELLED)
    assert signal.getsignal(signal.SIGINT) == orig_sigint
    assert signal.getsignal(target_sig) == orig_target
    assert list(out_dir.iterdir()) == []
