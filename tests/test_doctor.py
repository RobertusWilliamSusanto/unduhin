"""Tests for environment diagnostics (doctor) without real external tool dependencies."""

from __future__ import annotations

import datetime as dt
import importlib.metadata
from pathlib import Path
from types import SimpleNamespace

import pytest

from unduhin.doctor import (
    CheckStatus,
    CheckTier,
    check_deno,
    check_ffmpeg,
    check_python,
    check_ytdlp,
    check_ytdlp_ejs,
    detect_os_family,
    parse_ytdlp_release_date,
    run_doctor,
)


def test_detect_os_family() -> None:
    assert detect_os_family("Windows") == "windows"
    assert detect_os_family("Darwin") == "macos"
    assert detect_os_family("Linux") == "linux"


def test_parse_ytdlp_release_date() -> None:
    assert parse_ytdlp_release_date("2026.10.26") == dt.date(2026, 10, 26)
    assert parse_ytdlp_release_date("2026.10.26.post1") == dt.date(2026, 10, 26)
    assert parse_ytdlp_release_date("invalid") is None


def test_check_python_ok_and_old(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("unduhin.doctor.sys.version_info", SimpleNamespace(major=3, minor=13, micro=2))
    ok = check_python(os_family="linux", lang="en")
    assert ok.status is CheckStatus.OK

    monkeypatch.setattr("unduhin.doctor.sys.version_info", SimpleNamespace(major=3, minor=9, micro=18))
    old = check_python(os_family="windows", lang="id")
    assert old.status is CheckStatus.MISSING
    assert "winget" in old.fix_hint


def test_check_ytdlp_and_outdated_offline_check(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("unduhin.doctor.importlib.metadata.version", lambda _pkg: "2026.10.26")
    fresh = check_ytdlp(os_family="linux", lang="en", today=dt.date(2026, 11, 11))
    assert fresh.status is CheckStatus.OK

    stale = check_ytdlp(os_family="macos", lang="en", today=dt.date(2027, 4, 1))
    assert stale.status is CheckStatus.WARN
    assert stale.tier is CheckTier.REQUIRED
    assert "uv tool upgrade unduhin" in stale.fix_hint
    assert "pipx runpip unduhin" in stale.fix_hint
    assert 'pip install -U "yt-dlp[default]"' in stale.fix_hint

    def _not_found(_pkg: str) -> str:
        raise importlib.metadata.PackageNotFoundError("yt-dlp")

    monkeypatch.setattr("unduhin.doctor.importlib.metadata.version", _not_found)
    missing = check_ytdlp(os_family="linux", lang="id")
    assert missing.status is CheckStatus.MISSING


def test_check_ytdlp_ejs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("unduhin.doctor.importlib.metadata.version", lambda _pkg: "0.8.0")
    ok = check_ytdlp_ejs(os_family="linux", lang="en")
    assert ok.status is CheckStatus.OK
    assert ok.tier is CheckTier.RECOMMENDED

    def _not_found(_pkg: str) -> str:
        raise importlib.metadata.PackageNotFoundError("yt-dlp-ejs")

    monkeypatch.setattr("unduhin.doctor.importlib.metadata.version", _not_found)
    missing = check_ytdlp_ejs(os_family="windows", lang="en")
    assert missing.status is CheckStatus.WARN
    assert missing.tier is CheckTier.RECOMMENDED
    assert "YouTube" in missing.detail
    assert "uv tool upgrade unduhin" in missing.fix_hint


def test_check_ffmpeg_and_deno(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("unduhin.doctor.find_ffmpeg", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr(
        "unduhin.doctor._run_version_probe",
        lambda exe, _args: "ffmpeg version 7.1 Copyright" if "ffmpeg" in exe else "deno 2.3.5",
    )
    ff_ok = check_ffmpeg(os_family="linux", lang="en")
    assert ff_ok.status is CheckStatus.OK
    assert "7.1" in ff_ok.detail

    monkeypatch.setattr("unduhin.doctor.find_deno", lambda: "/usr/bin/deno")
    deno_ok = check_deno(os_family="linux", lang="en")
    assert deno_ok.status is CheckStatus.OK
    assert "2.3.5" in deno_ok.detail

    # Outdated Deno (< 2.3.0) produces warning with OS-specific fix hint.
    monkeypatch.setattr(
        "unduhin.doctor._run_version_probe",
        lambda _exe, _args: "deno 2.1.4 (release)",
    )
    deno_old = check_deno(os_family="macos", lang="en")
    assert deno_old.status is CheckStatus.WARN
    assert "brew install deno" in deno_old.fix_hint

    # Missing required vs recommended binaries show per-OS hints and correct tiers.
    monkeypatch.setattr("unduhin.doctor.find_ffmpeg", lambda: None)
    monkeypatch.setattr("unduhin.doctor.find_deno", lambda: None)
    ff_missing = check_ffmpeg(os_family="windows", lang="id")
    deno_missing = check_deno(os_family="linux", lang="id")
    assert ff_missing.status is CheckStatus.MISSING
    assert ff_missing.tier is CheckTier.REQUIRED
    assert "winget" in ff_missing.fix_hint
    assert deno_missing.status is CheckStatus.WARN
    assert deno_missing.tier is CheckTier.RECOMMENDED
    assert "YouTube" in deno_missing.detail
    assert "deno.land" in deno_missing.fix_hint


def test_run_doctor_aggregates_report_and_tiers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("unduhin.doctor.importlib.metadata.version", lambda _pkg: "2026.10.26")
    monkeypatch.setattr("unduhin.doctor.find_ffmpeg", lambda: None)
    monkeypatch.setattr("unduhin.doctor.find_deno", lambda: None)

    report_missing_ff = run_doctor(lang="en", os_family="windows")
    assert report_missing_ff.os_family == "windows"
    assert report_missing_ff.is_ready is False
    assert len(report_missing_ff.checks) == 5

    # Core tools OK + missing Deno/yt-dlp-ejs -> is_ready=True, has_warnings=True
    monkeypatch.setattr("unduhin.doctor.find_ffmpeg", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr(
        "unduhin.doctor._run_version_probe",
        lambda _exe, _args: "ffmpeg version 7.1 Copyright",
    )

    def _only_ytdlp(pkg: str) -> str:
        if pkg == "yt-dlp":
            return "2026.10.26"
        raise importlib.metadata.PackageNotFoundError(pkg)

    monkeypatch.setattr("unduhin.doctor.importlib.metadata.version", _only_ytdlp)
    report_recommended_warn = run_doctor(lang="en", os_family="linux")
    assert report_recommended_warn.is_ready is True
    assert report_recommended_warn.has_warnings is True
    assert report_recommended_warn.has_recommended_warnings is True


def test_windows_fallback_binary_discovery_and_doctor_details(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unduhin.doctor import _find_windows_fallback_binary

    fake_localappdata = tmp_path / "LocalAppData"
    winget_pkg_dir = fake_localappdata / "Microsoft" / "WinGet" / "Packages"
    old_ver = winget_pkg_dir / "Gyan.FFmpeg_Source" / "ffmpeg-7.0" / "bin"
    new_ver = winget_pkg_dir / "Gyan.FFmpeg_Source" / "ffmpeg-9.0" / "bin"
    old_ver.mkdir(parents=True)
    new_ver.mkdir(parents=True)

    fake_old = old_ver / "ffmpeg.exe"
    fake_new = new_ver / "ffmpeg.exe"
    fake_old.write_bytes(b"old")
    fake_new.write_bytes(b"new")

    monkeypatch.setattr("unduhin.doctor.os.name", "nt")
    monkeypatch.setenv("LOCALAPPDATA", str(fake_localappdata))

    found = _find_windows_fallback_binary("ffmpeg")
    assert found is not None
    assert str(fake_new) in found or str(fake_old) in found

    # Verify check_ffmpeg emits 'not in PATH' note when found off-path
    monkeypatch.setattr("unduhin.doctor.shutil.which", lambda _cmd: None)
    monkeypatch.setattr("unduhin.doctor._run_version_probe", lambda _exe, _args: "ffmpeg version 9.0.2")
    monkeypatch.setattr("unduhin.doctor.find_ffmpeg", lambda: str(fake_new))

    check_res = check_ffmpeg(os_family="windows", lang="en")
    assert check_res.status is CheckStatus.OK
    assert "not in PATH" in check_res.detail
