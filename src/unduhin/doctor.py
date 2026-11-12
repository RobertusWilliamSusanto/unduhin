"""Environment diagnostics and per-OS remediation instructions."""

from __future__ import annotations

import datetime as dt
import importlib.metadata
import os
import platform
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from unduhin.i18n import t

MIN_PYTHON_VERSION: tuple[int, int] = (3, 11)
MIN_DENO_VERSION: tuple[int, int, int] = (2, 3, 0)
YTDLP_OUTDATED_DAYS: int = 90

_DENO_VERSION_RE = re.compile(r"deno\s+(\d+)\.(\d+)\.(\d+)")
_FFMPEG_VERSION_RE = re.compile(r"ffmpeg version\s+([^\s]+)")
_YTDLP_CALVER_RE = re.compile(r"^(\d{4})\.(\d{1,2})\.(\d{1,2})")


class CheckStatus(StrEnum):
    """Health status for an environment check item."""

    OK = "ok"
    WARN = "warn"
    MISSING = "missing"


class CheckTier(StrEnum):
    """Severity tier distinguishing mandatory runtime tools from optional extras."""

    REQUIRED = "required"
    RECOMMENDED = "recommended"


@dataclass(frozen=True, slots=True)
class DoctorCheck:
    """Result of a single environment check."""

    component: str
    status: CheckStatus
    detail: str
    fix_hint: str = ""
    tier: CheckTier = CheckTier.REQUIRED


@dataclass(frozen=True, slots=True)
class DoctorReport:
    """Aggregated environment diagnostics report."""

    os_family: str
    checks: tuple[DoctorCheck, ...]

    @property
    def is_ready(self) -> bool:
        """Return True when all required checks are available."""
        return all(
            c.status is not CheckStatus.MISSING
            for c in self.checks
            if c.tier is CheckTier.REQUIRED
        )

    @property
    def has_warnings(self) -> bool:
        """Return True when any check reported a warning."""
        return any(c.status is CheckStatus.WARN for c in self.checks)

    @property
    def has_recommended_warnings(self) -> bool:
        """Return True when any recommended extra is not OK."""
        return any(
            c.tier is CheckTier.RECOMMENDED and c.status is not CheckStatus.OK
            for c in self.checks
        )


def detect_os_family(system_name: str | None = None) -> str:
    """Normalize platform name into ``'windows'``, ``'macos'``, or ``'linux'``."""
    raw = (system_name or platform.system()).strip().lower()
    if raw.startswith("win"):
        return "windows"
    if raw == "darwin" or raw.startswith("mac"):
        return "macos"
    return "linux"


def _find_windows_fallback_binary(
    binary_base: str,
    *,
    subdirs: tuple[str, ...] = ("bin", ""),
) -> str | None:
    """Find a candidate Windows binary in standard per-user or system package directories.

    Scans WinGet packages, Scoop shims/apps, and Chocolatey bin roots without altering PATH.
    When multiple version folders exist, picks the latest by sorting directory paths.
    """
    if os.name != "nt":
        return None

    exe_name = f"{binary_base}.exe"
    roots: list[Path] = []

    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        winget_pkgs = Path(local_app_data) / "Microsoft" / "WinGet" / "Packages"
        if winget_pkgs.is_dir():
            roots.append(winget_pkgs)

    user_profile = os.environ.get("USERPROFILE")
    if user_profile:
        scoop_dir = Path(user_profile) / "scoop"
        if scoop_dir.is_dir():
            roots.append(scoop_dir / "shims")
            roots.append(scoop_dir / "apps")

    prog_data = os.environ.get("PROGRAMDATA")
    if prog_data:
        choco_bin = Path(prog_data) / "chocolatey" / "bin"
        if choco_bin.is_dir():
            roots.append(choco_bin)

    candidates: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        direct = root / exe_name
        if direct.is_file():
            candidates.append(direct)
            continue
        try:
            for matched in root.glob(f"**/{exe_name}"):
                if matched.is_file():
                    candidates.append(matched)
        except OSError:
            continue

    if not candidates:
        return None

    # Sort descending by directory modification time / path string to pick newest
    candidates.sort(key=lambda p: (p.stat().st_mtime if p.exists() else 0.0, str(p)), reverse=True)
    return str(candidates[0])


def find_ffmpeg() -> str | None:
    """Locate the FFmpeg binary on PATH or standard Windows package locations."""
    on_path = shutil.which("ffmpeg")
    if on_path:
        return on_path
    return _find_windows_fallback_binary("ffmpeg")


def find_deno() -> str | None:
    """Locate the Deno binary on PATH, standard ``~/.deno/bin``, or Windows package locations."""
    on_path = shutil.which("deno")
    if on_path:
        return on_path

    exe_name = "deno.exe" if os.name == "nt" else "deno"
    candidate = Path.home() / ".deno" / "bin" / exe_name
    if candidate.is_file():
        return str(candidate)

    return _find_windows_fallback_binary("deno")


def _run_version_probe(executable: str, args: list[str]) -> str | None:
    """Execute a safe ``shell=False`` version check with a strict timeout."""
    try:
        proc = subprocess.run(  # noqa: S603
            [executable, *args],
            shell=False,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    output = (proc.stdout or "") + "\n" + (proc.stderr or "")
    return output.strip() or None


def parse_ytdlp_release_date(version: str) -> dt.date | None:
    """Parse ``YYYY.MM.DD`` release date from a yt-dlp calendar version string."""
    match = _YTDLP_CALVER_RE.match(version.strip())
    if not match:
        return None
    try:
        year, month, day = int(match.group(1)), int(match.group(2)), int(match.group(3))
        return dt.date(year, month, day)
    except ValueError:
        return None


def _fix_instructions(component: str, os_family: str) -> str:
    """Return official OS-specific installation/upgrade instructions."""
    instructions: dict[str, dict[str, str]] = {
        "python": {
            "windows": "winget install Python.Python.3.13",
            "macos": "brew install python@3.13",
            "linux": "Install Python >= 3.11 via pyenv, uv (`uv python install 3.13`), or distro packages",
        },
        "yt-dlp": {
            "windows": (
                'uv sync -U  |  uv tool install . --force  |  pip install -U "yt-dlp[default]"'
                '  |  (post-release: uv tool upgrade unduhin)'
            ),
            "macos": (
                'uv sync -U  |  uv tool install . --force  |  pip install -U "yt-dlp[default]"'
                '  |  (post-release: uv tool upgrade unduhin)'
            ),
            "linux": (
                'uv sync -U  |  uv tool install . --force  |  pip install -U "yt-dlp[default]"'
                '  |  (post-release: uv tool upgrade unduhin)'
            ),
        },
        "yt-dlp-ejs": {
            "windows": (
                'uv sync -U  |  uv tool install . --force  |  pip install -U "yt-dlp[default]"'
                '  |  (post-release: uv tool upgrade unduhin)'
            ),
            "macos": (
                'uv sync -U  |  uv tool install . --force  |  pip install -U "yt-dlp[default]"'
                '  |  (post-release: uv tool upgrade unduhin)'
            ),
            "linux": (
                'uv sync -U  |  uv tool install . --force  |  pip install -U "yt-dlp[default]"'
                '  |  (post-release: uv tool upgrade unduhin)'
            ),
        },
        "ffmpeg": {
            "windows": "winget install Gyan.FFmpeg (then restart terminal)",
            "macos": "brew install ffmpeg",
            "linux": "sudo apt install ffmpeg  # or: sudo dnf install ffmpeg / sudo pacman -S ffmpeg",
        },
        "deno": {
            "windows": "winget install DenoLand.Deno (or: irm https://deno.land/install.ps1 | iex)",
            "macos": "brew install deno",
            "linux": "curl -fsSL https://deno.land/install.sh | sh  (ensure ~/.deno/bin is in PATH)",
        },
    }
    return instructions.get(component, {}).get(os_family, "")


def check_python(*, os_family: str, lang: str | None = None) -> DoctorCheck:
    """Check whether the running Python interpreter satisfies ``>= 3.11``."""
    ver_info = sys.version_info
    version_str = f"{ver_info.major}.{ver_info.minor}.{ver_info.micro}"
    if (ver_info.major, ver_info.minor) >= MIN_PYTHON_VERSION:
        return DoctorCheck(
            component="Python",
            status=CheckStatus.OK,
            detail=t("doctor.python_ok", lang=lang, version=version_str),
        )
    return DoctorCheck(
        component="Python",
        status=CheckStatus.MISSING,
        detail=t("doctor.python_old", lang=lang, version=version_str),
        fix_hint=_fix_instructions("python", os_family),
    )


def check_ytdlp(
    *,
    os_family: str,
    lang: str | None = None,
    today: dt.date | None = None,
) -> DoctorCheck:
    """Check installed ``yt-dlp`` version and offline calendar-age threshold."""
    try:
        version = importlib.metadata.version("yt-dlp")
    except importlib.metadata.PackageNotFoundError:
        return DoctorCheck(
            component="yt-dlp",
            status=CheckStatus.MISSING,
            detail=t("doctor.ytdlp_missing", lang=lang),
            fix_hint=_fix_instructions("yt-dlp", os_family),
        )

    rel_date = parse_ytdlp_release_date(version)
    ref_today = today or dt.date.today()
    if rel_date is not None:
        age_days = (ref_today - rel_date).days
        if age_days > YTDLP_OUTDATED_DAYS:
            return DoctorCheck(
                component="yt-dlp",
                status=CheckStatus.WARN,
                detail=t("doctor.ytdlp_outdated", lang=lang, version=version, days=age_days),
                fix_hint=_fix_instructions("yt-dlp", os_family),
            )

    return DoctorCheck(
        component="yt-dlp",
        status=CheckStatus.OK,
        detail=t("doctor.ytdlp_ok", lang=lang, version=version),
    )


def check_ytdlp_ejs(*, os_family: str, lang: str | None = None) -> DoctorCheck:
    """Check whether ``yt-dlp-ejs`` is installed (recommended for YouTube only)."""
    try:
        version = importlib.metadata.version("yt-dlp-ejs")
    except importlib.metadata.PackageNotFoundError:
        return DoctorCheck(
            component="yt-dlp-ejs",
            status=CheckStatus.WARN,
            detail=t("doctor.ejs_missing", lang=lang),
            fix_hint=_fix_instructions("yt-dlp-ejs", os_family),
            tier=CheckTier.RECOMMENDED,
        )
    return DoctorCheck(
        component="yt-dlp-ejs",
        status=CheckStatus.OK,
        detail=t("doctor.ejs_ok", lang=lang, version=version),
        tier=CheckTier.RECOMMENDED,
    )


def check_ffmpeg(*, os_family: str, lang: str | None = None) -> DoctorCheck:
    """Check FFmpeg availability and reported version."""
    ffmpeg_path = find_ffmpeg()
    if not ffmpeg_path:
        return DoctorCheck(
            component="FFmpeg",
            status=CheckStatus.MISSING,
            detail=t("doctor.ffmpeg_missing", lang=lang),
            fix_hint=_fix_instructions("ffmpeg", os_family),
        )

    output = _run_version_probe(ffmpeg_path, ["-version"]) or ""
    match = _FFMPEG_VERSION_RE.search(output)
    version = match.group(1) if match else "unknown"
    on_path = shutil.which("ffmpeg") is not None
    detail_key = "doctor.ffmpeg_ok" if on_path else "doctor.ffmpeg_ok_not_on_path"
    return DoctorCheck(
        component="FFmpeg",
        status=CheckStatus.OK,
        detail=t(detail_key, lang=lang, version=version, path=ffmpeg_path),
    )


def check_deno(*, os_family: str, lang: str | None = None) -> DoctorCheck:
    """Check Deno availability and verify version ``>= 2.3.0`` (recommended for YouTube)."""
    deno_path = find_deno()
    if not deno_path:
        return DoctorCheck(
            component="Deno",
            status=CheckStatus.WARN,
            detail=t("doctor.deno_missing", lang=lang),
            fix_hint=_fix_instructions("deno", os_family),
            tier=CheckTier.RECOMMENDED,
        )

    output = _run_version_probe(deno_path, ["--version"]) or ""
    match = _DENO_VERSION_RE.search(output)
    on_path = shutil.which("deno") is not None
    detail_key = "doctor.deno_ok" if on_path else "doctor.deno_ok_not_on_path"
    if not match:
        return DoctorCheck(
            component="Deno",
            status=CheckStatus.WARN,
            detail=t(detail_key, lang=lang, version="unknown", path=deno_path),
            fix_hint=_fix_instructions("deno", os_family),
            tier=CheckTier.RECOMMENDED,
        )

    major, minor, patch = int(match.group(1)), int(match.group(2)), int(match.group(3))
    version_str = f"{major}.{minor}.{patch}"
    if (major, minor, patch) < MIN_DENO_VERSION:
        return DoctorCheck(
            component="Deno",
            status=CheckStatus.WARN,
            detail=t("doctor.deno_old", lang=lang, version=version_str, path=deno_path),
            fix_hint=_fix_instructions("deno", os_family),
            tier=CheckTier.RECOMMENDED,
        )

    return DoctorCheck(
        component="Deno",
        status=CheckStatus.OK,
        detail=t(detail_key, lang=lang, version=version_str, path=deno_path),
        tier=CheckTier.RECOMMENDED,
    )


def run_doctor(*, lang: str | None = None, os_family: str | None = None) -> DoctorReport:
    """Run all environment health checks and return a structured report."""
    resolved_os = detect_os_family(os_family)
    checks = (
        check_python(os_family=resolved_os, lang=lang),
        check_ytdlp(os_family=resolved_os, lang=lang),
        check_ytdlp_ejs(os_family=resolved_os, lang=lang),
        check_ffmpeg(os_family=resolved_os, lang=lang),
        check_deno(os_family=resolved_os, lang=lang),
    )
    return DoctorReport(os_family=resolved_os, checks=checks)
