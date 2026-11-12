"""Input validation, filename sanitization, template rendering, and path containment."""

from __future__ import annotations

import ctypes
import errno
import os
import re
import shutil
import sys
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from unduhin.config import (
    ALLOWED_TEMPLATE_FIELDS,
    CONSERVATIVE_UNKNOWN_SIZE_BYTES,
    MAX_BATCH_FILE_BYTES,
    MAX_BATCH_URLS,
    MAX_FINAL_PATH_LENGTH,
    MIN_FILENAME_ROOM,
    MIN_FREE_DISK_BYTES,
    MISSING_TEMPLATE_FIELD_PLACEHOLDER,
    MODE_EXTENSION_MAP,
    STAGING_CLEANUP_RETRY_DELAYS,
    STAGING_DIR_PREFIX,
    STALE_STAGING_AGE_SECONDS,
    ConflictPolicy,
    MediaInfo,
    MediaMode,
    get_default_output_dir,
)
from unduhin.errors import DiskSpaceError, PathSecurityError, ValidationError
from unduhin.i18n import t

_WINDOWS_RESERVED_NAMES: frozenset[str] = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
)

_FORBIDDEN_FILENAME_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_WHITESPACE_RE = re.compile(r"\s+")
_CURLY_TOKEN_RE = re.compile(r"\{([^{}]*)\}")
_PERCENT_TOKEN_RE = re.compile(r"%\(([^()]*)\)([a-zA-Z]?)")
_STAGING_NAME_RE = re.compile(r"^\.unduhin-tmp-(\d+)-[0-9a-f]+$")


def validate_url(raw_url: str) -> str:
    """Validate that ``raw_url`` uses http/https and has a valid host.

    Rejects unsafe schemes (``file://``, ``javascript:``, ``data:``, etc.),
    whitespace/control characters, backslashes in authority, and missing hosts.
    """
    if not isinstance(raw_url, str):
        raise ValidationError("error.invalid_url", url=str(raw_url))

    cleaned = raw_url.strip()
    if not cleaned:
        raise ValidationError("error.invalid_url", url=raw_url)

    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in cleaned):
        raise ValidationError("error.invalid_url", url=cleaned)

    try:
        parsed = urlsplit(cleaned)
    except ValueError as exc:
        raise ValidationError("error.invalid_url", url=cleaned) from exc

    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise ValidationError("error.invalid_url", url=cleaned)

    if not parsed.netloc or "\\" in parsed.netloc:
        raise ValidationError("error.invalid_url", url=cleaned)

    try:
        hostname = parsed.hostname
        _ = parsed.port
    except ValueError as exc:
        raise ValidationError("error.invalid_url", url=cleaned) from exc

    if not hostname:
        raise ValidationError("error.invalid_url", url=cleaned)

    # Normalize scheme casing while preserving path/query intact.
    if not cleaned.startswith(f"{scheme}://"):
        return f"{scheme}://{cleaned[len(scheme) + 3:]}"
    return cleaned


def sanitize_filename(
    name: str,
    *,
    fallback: str = "media",
    max_length: int = 150,
) -> str:
    """Sanitize a filename or metadata component safely across Windows/macOS/Linux."""
    cleaned = _FORBIDDEN_FILENAME_CHARS_RE.sub("_", name)
    cleaned = _WHITESPACE_RE.sub(" ", cleaned).strip(" .")

    if not cleaned or cleaned in {".", ".."}:
        cleaned = fallback

    if len(cleaned) > max_length:
        cleaned = cleaned[:max_length].strip(" .")
        if not cleaned or cleaned in {".", ".."}:
            cleaned = fallback

    stem_part = cleaned.split(".", 1)[0].upper()
    if stem_part in _WINDOWS_RESERVED_NAMES:
        cleaned = f"_{cleaned}"

    return cleaned


def validate_template(template: str) -> None:
    """Validate that a filename template uses only allowlisted fields and no path escapes."""
    if not isinstance(template, str) or not template.strip():
        raise ValidationError(
            "error.invalid_template",
            template=str(template),
            reason="empty template",
        )

    if "\x00" in template or "/" in template or "\\" in template or ".." in template:
        raise ValidationError(
            "error.invalid_template",
            template=template,
            reason="path separators and '..' are not allowed",
        )

    if ":" in template or template.lstrip().startswith("-"):
        raise ValidationError(
            "error.invalid_template",
            template=template,
            reason="drive letters, colons, or leading '-' are not allowed",
        )

    # Strip valid %(field)s tokens first, then reject stray '%' format strings.
    pos = 0
    for match in _PERCENT_TOKEN_RE.finditer(template):
        field_name = match.group(1)
        conv = match.group(2)
        if field_name not in ALLOWED_TEMPLATE_FIELDS or conv != "s":
            raise ValidationError(
                "error.invalid_template",
                template=template,
                reason=f"unsupported field '%({field_name}){conv}'",
            )
        pos = match.end()

    without_valid_percent = _PERCENT_TOKEN_RE.sub("", template)
    if "%(" in without_valid_percent:
        raise ValidationError(
            "error.invalid_template",
            template=template,
            reason="malformed placeholder syntax",
        )

    # Validate brace placeholders strictly without str.format evaluation.
    without_valid_curly = _CURLY_TOKEN_RE.sub("", without_valid_percent)
    if "{" in without_valid_curly or "}" in without_valid_curly:
        raise ValidationError(
            "error.invalid_template",
            template=template,
            reason="unbalanced braces",
        )

    for match in _CURLY_TOKEN_RE.finditer(without_valid_percent):
        token = match.group(1)
        if token not in ALLOWED_TEMPLATE_FIELDS:
            raise ValidationError(
                "error.invalid_template",
                template=template,
                reason=f"field '{token}' is not in allowlist {sorted(ALLOWED_TEMPLATE_FIELDS)}",
            )
    _ = pos


def extract_template_fields(template: str) -> set[str]:
    """Return all allowlisted placeholder field names referenced by ``template``."""
    validate_template(template)
    fields: set[str] = {m.group(1) for m in _PERCENT_TOKEN_RE.finditer(template)}
    stripped = _PERCENT_TOKEN_RE.sub("", template)
    fields.update(m.group(1) for m in _CURLY_TOKEN_RE.finditer(stripped))
    return fields


def template_fields_available(info: MediaInfo, template: str) -> bool:
    """Return True if every non-``ext`` placeholder in ``template`` has a non-empty value on ``info``."""
    fields = extract_template_fields(template) - {"ext"}
    field_values: dict[str, str | None] = {
        "title": info.title,
        "id": info.id,
        "uploader": info.uploader,
        "upload_date": info.upload_date,
    }
    for name in fields:
        raw_val = field_values.get(name)
        if not raw_val or not raw_val.strip():
            return False
    return True


def _sanitize_template_value(raw_value: str | None) -> str:
    """Sanitize a template metadata value using the unified missing-field placeholder."""
    if not raw_value or not raw_value.strip():
        return MISSING_TEMPLATE_FIELD_PLACEHOLDER
    return sanitize_filename(raw_value, fallback=MISSING_TEMPLATE_FIELD_PLACEHOLDER)


def render_allowlisted_filename(
    info: MediaInfo,
    template: str,
    mode: MediaMode,
) -> str:
    """Render a safe final filename locally from ``info`` using allowlisted template fields.

    Any missing or non-sanitizable field (``title``, ``id``, ``uploader``, ``upload_date``)
    uniformly renders as ``MISSING_TEMPLATE_FIELD_PLACEHOLDER`` (``"unknown"``).
    """
    validate_template(template)
    target_ext = MODE_EXTENSION_MAP[mode]
    ext_no_dot = target_ext.lstrip(".")

    values: dict[str, str] = {
        "title": _sanitize_template_value(info.title),
        "id": _sanitize_template_value(info.id),
        "uploader": _sanitize_template_value(info.uploader),
        "upload_date": _sanitize_template_value(info.upload_date),
        "ext": ext_no_dot,
    }

    rendered = _PERCENT_TOKEN_RE.sub(lambda m: values[m.group(1)], template)
    rendered = _CURLY_TOKEN_RE.sub(lambda m: values[m.group(1)], rendered)

    # Remove trailing target extension if already present before sanitizing stem.
    lowered = rendered.lower()
    stem_raw = rendered[: -len(target_ext)] if lowered.endswith(target_ext) else rendered

    safe_stem = sanitize_filename(stem_raw, fallback=MISSING_TEMPLATE_FIELD_PLACEHOLDER)
    return f"{safe_stem}{target_ext}"


def validate_output_dir_path(
    output_dir: Path,
    *,
    max_total_path_len: int = MAX_FINAL_PATH_LENGTH,
) -> Path:
    """Resolve and validate ``output_dir`` without creating directories on disk."""
    resolved = output_dir.expanduser().resolve()
    if resolved.exists() and not resolved.is_dir():
        raise ValidationError(
            "error.invalid_template",
            template=str(output_dir),
            reason="not a directory",
        )
    max_dir_len = max_total_path_len - 1 - MIN_FILENAME_ROOM
    dir_len = len(str(resolved))
    if dir_len > max_dir_len:
        raise ValidationError(
            "error.output_path_too_long",
            path=str(resolved),
            length=dir_len,
            max_len=max_dir_len,
        )
    return resolved


def resolve_output_dir(
    explicit_dir: Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    max_total_path_len: int = MAX_FINAL_PATH_LENGTH,
) -> Path:
    """Resolve output directory using CLI > UNDUHIN_OUTPUT_DIR > ~/Downloads/unduhin."""
    env = os.environ if environ is None else environ
    if explicit_dir is not None:
        raw_dir = explicit_dir
    else:
        env_val = env.get("UNDUHIN_OUTPUT_DIR", "").strip()
        raw_dir = Path(env_val) if env_val else get_default_output_dir()
    return validate_output_dir_path(raw_dir, max_total_path_len=max_total_path_len)


def ensure_safe_output_dir(
    output_dir: Path,
    *,
    max_total_path_len: int = MAX_FINAL_PATH_LENGTH,
) -> Path:
    """Resolve and create ``output_dir``, rejecting files or over-long paths."""
    resolved = validate_output_dir_path(output_dir, max_total_path_len=max_total_path_len)
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def parse_batch_file(batch_path: Path) -> tuple[tuple[str, ...], int]:
    """Parse a UTF-8 (with optional BOM) URL batch file cleanly and fail-fast.

    Ignores blank lines and ``#`` comments, strips whitespace, deduplicates URLs
    preserving first-seen order, and enforces 1 MB / 500 URL limits.
    Returns ``(urls, duplicates_removed)``.
    """
    resolved = batch_path.expanduser()
    if not resolved.exists() or not resolved.is_file():
        raise ValidationError(
            "error.batch_file_unreadable",
            path=str(batch_path),
            reason="file does not exist or is not a regular file",
        )

    try:
        file_size = resolved.stat().st_size
    except OSError as exc:
        raise ValidationError(
            "error.batch_file_unreadable",
            path=str(batch_path),
            reason=str(exc),
        ) from exc

    if file_size > MAX_BATCH_FILE_BYTES:
        raise ValidationError(
            "error.batch_too_large",
            path=str(batch_path),
            max_mb=MAX_BATCH_FILE_BYTES // (1024 * 1024),
        )

    try:
        raw_text = resolved.read_text(encoding="utf-8-sig")
    except (UnicodeDecodeError, OSError) as exc:
        raise ValidationError(
            "error.batch_file_unreadable",
            path=str(batch_path),
            reason=str(exc),
        ) from exc

    urls: list[str] = []
    seen: set[str] = set()
    duplicates_removed = 0

    for line_no, raw_line in enumerate(raw_text.splitlines(), start=1):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            normalized = validate_url(stripped)
        except ValidationError as exc:
            raise ValidationError(
                "error.batch_invalid_line",
                line=line_no,
                url=stripped,
            ) from exc

        if normalized in seen:
            duplicates_removed += 1
            continue
        seen.add(normalized)
        urls.append(normalized)
        if len(urls) > MAX_BATCH_URLS:
            raise ValidationError("error.batch_too_many_urls", max_urls=MAX_BATCH_URLS)

    if not urls:
        raise ValidationError("error.batch_empty", path=str(batch_path))

    return tuple(urls), duplicates_removed


def collect_input_urls(
    positional_url: str | None,
    batch_file: Path | None,
) -> tuple[tuple[str, ...], int]:
    """Collect and cross-deduplicate URLs from positional arg (first) and ``--batch`` file.

    Returns ``(deduped_urls, total_duplicates_removed)``.
    """
    if positional_url is None and batch_file is None:
        raise ValidationError("cli.missing_url")

    combined: list[str] = []
    seen: set[str] = set()
    duplicates_removed = 0

    if positional_url is not None:
        normalized = validate_url(positional_url)
        seen.add(normalized)
        combined.append(normalized)

    if batch_file is not None:
        batch_urls, batch_dups = parse_batch_file(batch_file)
        duplicates_removed += batch_dups
        for url in batch_urls:
            if url in seen:
                duplicates_removed += 1
                continue
            seen.add(url)
            combined.append(url)
            if len(combined) > MAX_BATCH_URLS:
                raise ValidationError("error.batch_too_many_urls", max_urls=MAX_BATCH_URLS)

    return tuple(combined), duplicates_removed


def assert_within_directory(target_path: Path, base_dir: Path) -> Path:
    """Verify that ``target_path`` resolves strictly inside ``base_dir``."""
    resolved_base = base_dir.resolve()
    resolved_target = target_path.resolve()
    if resolved_target == resolved_base or not resolved_target.is_relative_to(resolved_base):
        raise PathSecurityError("error.path_traversal", path=str(target_path))
    return resolved_target


def assert_allowed_extension(filepath: Path, mode: MediaMode) -> None:
    """Verify the staged file extension matches the expected extension for ``mode``."""
    expected = MODE_EXTENSION_MAP[mode]
    actual = filepath.suffix.lower()
    if actual != expected:
        raise PathSecurityError(
            "error.disallowed_extension",
            ext=actual or "<none>",
            expected=expected,
        )


def _fit_stem_with_suffix(
    output_dir: Path,
    stem: str,
    full_suffix: str,
    *,
    max_total_path_len: int = MAX_FINAL_PATH_LENGTH,
) -> str:
    """Fit ``stem + full_suffix`` so ``len(str(output_dir / result)) <= max_total_path_len``."""
    overhead = len(str(output_dir / "x")) - 1
    max_stem_len = max_total_path_len - overhead - len(full_suffix)
    if max_stem_len < 1:
        max_dir_len = max(0, max_total_path_len - 1 - MIN_FILENAME_ROOM)
        raise ValidationError(
            "error.output_path_too_long",
            path=str(output_dir),
            length=len(str(output_dir)),
            max_len=max_dir_len,
        )
    if len(stem) <= max_stem_len:
        return f"{stem}{full_suffix}"
    trimmed = sanitize_filename(
        stem[:max_stem_len],
        max_length=max_stem_len,
        fallback=MISSING_TEMPLATE_FIELD_PLACEHOLDER,
    )
    if len(trimmed) > max_stem_len:
        trimmed = trimmed[:max_stem_len].rstrip(" .") or "x"
    return f"{trimmed}{full_suffix}"


def fit_filename_to_path_limit(
    output_dir: Path,
    filename: str,
    *,
    max_total_path_len: int = MAX_FINAL_PATH_LENGTH,
) -> str:
    """Shorten ``filename`` stem cleanly if ``output_dir / filename`` exceeds ``max_total_path_len``."""
    path_obj = Path(filename)
    suffix = path_obj.suffix
    stem = path_obj.stem or MISSING_TEMPLATE_FIELD_PLACEHOLDER
    return _fit_stem_with_suffix(
        output_dir,
        stem,
        suffix,
        max_total_path_len=max_total_path_len,
    )


def _next_available_path(
    output_dir: Path,
    stem: str,
    suffix: str,
    reserved_names: set[str],
    *,
    max_total_path_len: int = MAX_FINAL_PATH_LENGTH,
) -> Path:
    """Return the next free ``stem (N)suffix`` path avoiding disk and reserved collisions."""
    for index in range(1, 10_000):
        candidate_name = _fit_stem_with_suffix(
            output_dir,
            stem,
            f" ({index}){suffix}",
            max_total_path_len=max_total_path_len,
        )
        renamed = output_dir / candidate_name
        assert_within_directory(renamed, output_dir)
        if not renamed.exists() and renamed.name.casefold() not in reserved_names:
            reserved_names.add(renamed.name.casefold())
            return renamed

    fallback_name = _fit_stem_with_suffix(
        output_dir,
        stem,
        f" ({uuid.uuid4().hex[:8]}){suffix}",
        max_total_path_len=max_total_path_len,
    )
    unique = output_dir / fallback_name
    assert_within_directory(unique, output_dir)
    reserved_names.add(unique.name.casefold())
    return unique


@dataclass(frozen=True, slots=True)
class ReservationDecision:
    """In-memory destination reservation result prior to moving a staged file."""

    path: Path
    should_skip: bool
    reserved_key: str | None
    is_batch_duplicate: bool = False


def reserve_destination_detailed(
    output_dir: Path,
    filename: str,
    policy: ConflictPolicy,
    reserved_names: set[str],
    *,
    max_total_path_len: int = MAX_FINAL_PATH_LENGTH,
) -> ReservationDecision:
    """Reserve a final destination path once per batch and report batch-rename state."""
    fitted_name = fit_filename_to_path_limit(
        output_dir,
        filename,
        max_total_path_len=max_total_path_len,
    )
    candidate = output_dir / fitted_name
    assert_within_directory(candidate, output_dir)
    key = candidate.name.casefold()
    in_batch = key in reserved_names
    on_disk = candidate.exists()

    if policy is ConflictPolicy.SKIP:
        if in_batch or on_disk:
            return ReservationDecision(
                path=candidate,
                should_skip=True,
                reserved_key=None,
                is_batch_duplicate=False,
            )
        reserved_names.add(key)
        return ReservationDecision(
            path=candidate,
            should_skip=False,
            reserved_key=key,
            is_batch_duplicate=False,
        )

    if policy is ConflictPolicy.OVERWRITE:
        if not in_batch:
            reserved_names.add(key)
            return ReservationDecision(
                path=candidate,
                should_skip=False,
                reserved_key=key,
                is_batch_duplicate=False,
            )
        # Intra-batch duplicate title: keep earlier batch download intact via suffix.
        renamed = _next_available_path(
            output_dir,
            candidate.stem,
            candidate.suffix,
            reserved_names,
            max_total_path_len=max_total_path_len,
        )
        return ReservationDecision(
            path=renamed,
            should_skip=False,
            reserved_key=renamed.name.casefold(),
            is_batch_duplicate=True,
        )

    # ConflictPolicy.RENAME
    if not in_batch and not on_disk:
        reserved_names.add(key)
        return ReservationDecision(
            path=candidate,
            should_skip=False,
            reserved_key=key,
            is_batch_duplicate=False,
        )
    renamed = _next_available_path(
        output_dir,
        candidate.stem,
        candidate.suffix,
        reserved_names,
        max_total_path_len=max_total_path_len,
    )
    return ReservationDecision(
        path=renamed,
        should_skip=False,
        reserved_key=renamed.name.casefold(),
        is_batch_duplicate=in_batch,
    )


def reserve_destination(
    output_dir: Path,
    filename: str,
    policy: ConflictPolicy,
    reserved_names: set[str],
    *,
    max_total_path_len: int = MAX_FINAL_PATH_LENGTH,
) -> tuple[Path, bool]:
    """Reserve a final destination path once per batch before downloading.

    Returns ``(target_path, should_skip)``.
    """
    decision = reserve_destination_detailed(
        output_dir,
        filename,
        policy,
        reserved_names,
        max_total_path_len=max_total_path_len,
    )
    return decision.path, decision.should_skip


def resolve_conflict(
    output_dir: Path,
    filename: str,
    policy: ConflictPolicy,
    *,
    max_total_path_len: int = MAX_FINAL_PATH_LENGTH,
) -> tuple[Path, bool]:
    """Resolve destination path against ``policy``; returns ``(path, should_skip)``."""
    return reserve_destination(
        output_dir,
        filename,
        policy,
        set(),
        max_total_path_len=max_total_path_len,
    )


def _link_without_overwrite(staged_file: Path, dest: Path) -> bool:
    """Atomically move ``staged_file`` to ``dest`` unless ``dest`` already exists."""
    try:
        os.link(staged_file, dest)
    except FileExistsError:
        return False
    except (OSError, NotImplementedError) as exc:
        if isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
            raise
        if dest.exists():
            return False
        staged_file.replace(dest)
        return True
    staged_file.unlink()
    return True


def move_staged_file(
    staged_file: Path,
    planned_path: Path,
    output_dir: Path,
    policy: ConflictPolicy,
    reserved_names: set[str],
    *,
    max_total_path_len: int = MAX_FINAL_PATH_LENGTH,
) -> tuple[Path, bool]:
    """Move ``staged_file`` to pre-reserved ``planned_path`` handling concurrent races.

    Does not re-evaluate ``skip`` unless a concurrent race actually created ``planned_path``.
    Returns ``(final_path, was_moved)``; when ``was_moved`` is False, ``staged_file`` is removed.
    """
    assert_within_directory(planned_path, output_dir)

    if policy is ConflictPolicy.OVERWRITE:
        staged_file.replace(planned_path)
        return planned_path, True

    if _link_without_overwrite(staged_file, planned_path):
        return planned_path, True

    # Filesystem race: destination appeared while download was running.
    if policy is ConflictPolicy.SKIP:
        staged_file.unlink(missing_ok=True)
        return planned_path, False

    stem = planned_path.stem
    suffix = planned_path.suffix
    for _ in range(25):
        candidate = _next_available_path(
            output_dir,
            stem,
            suffix,
            reserved_names,
            max_total_path_len=max_total_path_len,
        )
        assert_within_directory(candidate, output_dir)
        if _link_without_overwrite(staged_file, candidate):
            return candidate, True

    fallback_name = _fit_stem_with_suffix(
        output_dir,
        stem,
        f" ({uuid.uuid4().hex[:8]}){suffix}",
        max_total_path_len=max_total_path_len,
    )
    fallback = output_dir / fallback_name
    assert_within_directory(fallback, output_dir)
    reserved_names.add(fallback.name.casefold())
    staged_file.replace(fallback)
    return fallback, True


def create_staging_dir(output_dir: Path) -> Path:
    """Create a per-process isolated staging folder inside ``output_dir``."""
    folder_name = f"{STAGING_DIR_PREFIX}{os.getpid()}-{uuid.uuid4().hex[:10]}"
    staging = output_dir / folder_name
    assert_within_directory(staging, output_dir)
    staging.mkdir(parents=True, exist_ok=False)
    return staging


def _default_is_pid_alive(pid: int) -> bool:
    """Check whether ``pid`` corresponds to a currently running process."""
    if pid <= 0:
        return False

    if sys.platform == "win32":
        windll = getattr(ctypes, "windll", None)
        if windll is None:
            return False
        process_query_limited = 0x1000
        still_active = 259
        handle = windll.kernel32.OpenProcess(process_query_limited, False, pid)
        if not handle:
            last_err = int(windll.kernel32.GetLastError())
            # ERROR_ACCESS_DENIED (5) means process exists; ERROR_INVALID_PARAMETER (87) means gone.
            return last_err == 5
        try:
            exit_code = ctypes.c_ulong(0)
            ok = windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
            return bool(ok and exit_code.value == still_active)
        finally:
            windll.kernel32.CloseHandle(handle)
    else:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True


def cleanup_path_with_retry(
    target_path: Path,
    *,
    lang: str | None = None,
    delays: tuple[float, ...] = STAGING_CLEANUP_RETRY_DELAYS,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> str | None:
    """Remove directory ``target_path`` with bounded retries on ``PermissionError``/``OSError``.

    Tries once and then retries up to ``len(delays)`` times (default 0.2s, 0.5s, 1.0s).
    Returns ``None`` on success, or a localized warning message if removal still fails.
    """
    if not target_path.exists():
        return None

    last_exc: OSError | None = None
    for attempt in range(1 + len(delays)):
        try:
            shutil.rmtree(target_path)
            return None
        except FileNotFoundError:
            return None
        except OSError as exc:
            last_exc = exc
            if attempt < len(delays):
                sleep_fn(delays[attempt])

    return t(
        "warn.stale_tmp_cleanup_failed",
        lang=lang,
        path=str(target_path),
        reason=str(last_exc) if last_exc is not None else "unknown",
    )


def cleanup_stale_staging_dirs(
    output_dir: Path,
    *,
    lang: str | None = None,
    max_age_seconds: float = STALE_STAGING_AGE_SECONDS,
    is_pid_alive: Callable[[int], bool] | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> list[str]:
    """Remove stale ``.unduhin-tmp-*`` directories at startup and report any deletion failures."""
    if not output_dir.exists() or not output_dir.is_dir():
        return []

    pid_checker = is_pid_alive or _default_is_pid_alive
    now = time.time()
    warnings: list[str] = []

    try:
        entries = list(output_dir.iterdir())
    except OSError as exc:
        warnings.append(
            t("warn.stale_tmp_cleanup_failed", lang=lang, path=str(output_dir), reason=str(exc))
        )
        return warnings

    for entry in entries:
        if entry.is_symlink() or not entry.is_dir() or not entry.name.startswith(STAGING_DIR_PREFIX):
            continue

        match = _STAGING_NAME_RE.match(entry.name)
        is_stale = False
        if match is not None:
            owner_pid = int(match.group(1))
            if owner_pid != os.getpid() and not pid_checker(owner_pid):
                is_stale = True
        else:
            try:
                age = now - entry.stat().st_mtime
                if age >= max_age_seconds:
                    is_stale = True
            except OSError:
                is_stale = True

        if not is_stale:
            continue

        assert_within_directory(entry, output_dir)
        cleanup_warning = cleanup_path_with_retry(entry, lang=lang, sleep_fn=sleep_fn)
        if cleanup_warning is not None:
            warnings.append(cleanup_warning)

    return warnings


def verify_disk_space(
    output_dir: Path,
    *,
    estimated_bytes: int | None = None,
) -> bool:
    """Check free space in ``output_dir``; returns True when estimated size was unknown."""
    probe = output_dir
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent

    usage = shutil.disk_usage(probe)
    if estimated_bytes is None or estimated_bytes <= 0:
        size_unknown = True
        required = CONSERVATIVE_UNKNOWN_SIZE_BYTES
    else:
        size_unknown = False
        required = max(MIN_FREE_DISK_BYTES, int(estimated_bytes * 1.15))

    if usage.free < required:
        raise DiskSpaceError("error.disk_full", path=str(output_dir))
    return size_unknown


def check_free_disk_space(
    output_dir: Path,
    *,
    estimated_bytes: int | None = None,
    lang: str | None = None,
) -> str | None:
    """Verify sufficient free space exists in ``output_dir`` and return warning if unknown size."""
    if verify_disk_space(output_dir, estimated_bytes=estimated_bytes):
        return t(
            "warn.unknown_filesize_batch",
            lang=lang,
            count=1,
            min_mb=CONSERVATIVE_UNKNOWN_SIZE_BYTES // (1024 * 1024),
        )
    return None
