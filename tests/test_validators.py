"""Security and validation tests for URLs, templates, filenames, and path containment."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import NamedTuple

import pytest

from unduhin.config import (
    CONSERVATIVE_UNKNOWN_SIZE_BYTES,
    MAX_BATCH_URLS,
    MAX_FINAL_PATH_LENGTH,
    MIN_FILENAME_ROOM,
    MISSING_TEMPLATE_FIELD_PLACEHOLDER,
    ConflictPolicy,
    MediaInfo,
    MediaMode,
    get_default_output_dir,
)
from unduhin.errors import DiskSpaceError, PathSecurityError, ValidationError
from unduhin.validators import (
    assert_allowed_extension,
    assert_within_directory,
    check_free_disk_space,
    cleanup_path_with_retry,
    cleanup_stale_staging_dirs,
    collect_input_urls,
    create_staging_dir,
    fit_filename_to_path_limit,
    move_staged_file,
    parse_batch_file,
    render_allowlisted_filename,
    reserve_destination,
    reserve_destination_detailed,
    resolve_conflict,
    resolve_output_dir,
    sanitize_filename,
    template_fields_available,
    validate_output_dir_path,
    validate_template,
    validate_url,
)


@pytest.mark.parametrize(
    "valid_url",
    [
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "http://example.org/media/sample.mp4?download=1",
        "HTTPS://Youtu.Be/abc123",
    ],
)
def test_validate_url_accepts_http_and_https(valid_url: str) -> None:
    normalized = validate_url(valid_url)
    assert normalized.startswith(("http://", "https://"))


@pytest.mark.parametrize(
    "unsafe_url",
    [
        "",
        "   ",
        "file:///etc/passwd",
        "file://C:/Windows/System32/cmd.exe",
        "javascript:alert(1)",
        "data:text/plain;base64,SGVsbG8=",
        "ftp://example.com/file.mp3",
        "smb://server/share/video.mp4",
        "ytsearch:creative commons music",
        "https://",
        "https://example.com/bad url with spaces",
        "https://example.com/\ninjected",
        "https://example.com\\path",
        "https://example.com:notaport/video",
    ],
)
def test_validate_url_rejects_unsafe_schemes_and_malformed_urls(unsafe_url: str) -> None:
    with pytest.raises(ValidationError):
        validate_url(unsafe_url)


def test_sanitize_filename_removes_dangerous_chars_and_reserved_names() -> None:
    sanitized = sanitize_filename('../evil:title*?"<>|')
    assert sanitized == "_evil_title______"
    assert ".." not in sanitized
    assert sanitize_filename("CON") == "_CON"
    assert sanitize_filename("nul.txt") == "_nul.txt"
    assert sanitize_filename("COM1") == "_COM1"
    assert sanitize_filename("...") == "media"
    assert sanitize_filename("   ") == "media"
    assert len(sanitize_filename("a" * 300, max_length=100)) == 100


def test_template_validation_and_allowlisted_rendering() -> None:
    validate_template("{title}.{ext}")
    validate_template("{uploader} - {title} [{id}].{ext}")
    validate_template("%(title)s.%(ext)s")

    info = MediaInfo(
        id="abc123",
        title="My <Cool> Track / Remix",
        url="https://example.org/watch?v=abc123",
        uploader="Alice",
        upload_date="20261111",
    )
    rendered_mp3 = render_allowlisted_filename(info, "{uploader} - {title}.{ext}", MediaMode.AUDIO)
    assert rendered_mp3 == "Alice - My _Cool_ Track _ Remix.mp3"

    rendered_mp4 = render_allowlisted_filename(info, "{title}", MediaMode.VIDEO)
    assert rendered_mp4.endswith(".mp4")


@pytest.mark.parametrize(
    "bad_template",
    [
        "",
        "../{title}.{ext}",
        "subdir/{title}.{ext}",
        "sub\\{title}.{ext}",
        "C:{title}.{ext}",
        "-{title}.{ext}",
        "{title.__class__}.{ext}",
        "{extractor_key}.{ext}",
        "%(filepath)s.%(ext)s",
        "%(title)d",
        "{unclosed",
    ],
)
def test_validate_template_rejects_traversal_and_non_allowlisted_fields(bad_template: str) -> None:
    with pytest.raises(ValidationError):
        validate_template(bad_template)


def test_assert_within_directory_prevents_path_traversal(tmp_path: Path) -> None:
    out_dir = tmp_path / "downloads"
    out_dir.mkdir()
    inside = out_dir / "track.mp3"
    assert assert_within_directory(inside, out_dir) == inside.resolve()

    with pytest.raises(PathSecurityError):
        assert_within_directory(out_dir / ".." / "escape.mp3", out_dir)

    with pytest.raises(PathSecurityError):
        assert_within_directory(out_dir, out_dir)


def test_assert_within_directory_blocks_symlink_escape(tmp_path: Path) -> None:
    out_dir = tmp_path / "downloads"
    outside = tmp_path / "outside"
    out_dir.mkdir()
    outside.mkdir()
    link_path = out_dir / "escape_link.mp3"
    try:
        link_path.symlink_to(outside / "secret.mp3")
    except (OSError, NotImplementedError):
        pytest.skip("Symlink creation requires elevated privileges on this OS configuration")

    with pytest.raises(PathSecurityError):
        assert_within_directory(link_path, out_dir)


def test_assert_within_directory_blocks_escaped_resolve(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out_dir = tmp_path / "downloads"
    outside = tmp_path / "outside"
    out_dir.mkdir()
    outside.mkdir()
    tricky = out_dir / "alias.mp3"

    orig_resolve = Path.resolve

    def _fake_resolve(self: Path, strict: bool = False) -> Path:
        if self == tricky:
            return orig_resolve(outside / "secret.mp3", strict=strict)
        return orig_resolve(self, strict=strict)

    monkeypatch.setattr(Path, "resolve", _fake_resolve)
    with pytest.raises(PathSecurityError):
        assert_within_directory(tricky, out_dir)


def test_assert_allowed_extension_enforces_mode_whitelist(tmp_path: Path) -> None:
    assert_allowed_extension(tmp_path / "audio.mp3", MediaMode.AUDIO)
    assert_allowed_extension(tmp_path / "video.MP4", MediaMode.VIDEO)

    for bad_name, mode in [
        ("page.html", MediaMode.AUDIO),
        ("payload.exe", MediaMode.VIDEO),
        ("raw.webm", MediaMode.VIDEO),
        ("song.mp4", MediaMode.AUDIO),
        ("noext", MediaMode.AUDIO),
    ]:
        with pytest.raises(PathSecurityError):
            assert_allowed_extension(tmp_path / bad_name, mode)


def test_resolve_conflict_policies(tmp_path: Path) -> None:
    existing = tmp_path / "song.mp3"
    existing.write_bytes(b"original")

    renamed, skip_rename = resolve_conflict(tmp_path, "song.mp3", ConflictPolicy.RENAME)
    assert skip_rename is False
    assert renamed == tmp_path / "song (1).mp3"

    renamed.write_bytes(b"copy1")
    renamed2, _ = resolve_conflict(tmp_path, "song.mp3", ConflictPolicy.RENAME)
    assert renamed2 == tmp_path / "song (2).mp3"

    skipped, skip_flag = resolve_conflict(tmp_path, "song.mp3", ConflictPolicy.SKIP)
    assert skip_flag is True
    assert skipped == existing

    overwritten, skip_overwrite = resolve_conflict(tmp_path, "song.mp3", ConflictPolicy.OVERWRITE)
    assert skip_overwrite is False
    assert overwritten == existing


def test_cleanup_stale_staging_dirs_removes_dead_and_reports_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dead_dir = tmp_path / ".unduhin-tmp-999999-abcdef1234"
    live_dir = tmp_path / ".unduhin-tmp-424242-1122334455"
    old_legacy = tmp_path / ".unduhin-tmp-legacy"
    active_mine = create_staging_dir(tmp_path)

    dead_dir.mkdir()
    live_dir.mkdir()
    old_legacy.mkdir()
    old_time = time.time() - 7200
    os.utime(old_legacy, (old_time, old_time))

    warnings = cleanup_stale_staging_dirs(
        tmp_path,
        lang="en",
        is_pid_alive=lambda pid: pid == 424242,
    )
    assert warnings == []
    assert not dead_dir.exists()
    assert not old_legacy.exists()
    assert live_dir.exists()
    assert active_mine.exists()

    # Verify deletion failure notifies the user cleanly instead of crashing.
    locked_dir = tmp_path / ".unduhin-tmp-888888-deadbeef00"
    locked_dir.mkdir()

    def _fail_rmtree(_path: Path) -> None:
        raise PermissionError("file in use")

    monkeypatch.setattr("unduhin.validators.shutil.rmtree", _fail_rmtree)
    err_warnings = cleanup_stale_staging_dirs(
        tmp_path,
        lang="id",
        is_pid_alive=lambda pid: pid == 424242,
    )
    assert len(err_warnings) == 1
    assert "Gagal menghapus folder sementara basi" in err_warnings[0]


class _FakeUsage(NamedTuple):
    total: int
    used: int
    free: int


def test_validate_output_dir_path_no_disk_write_and_rejects_file(tmp_path: Path) -> None:
    nonexistent = tmp_path / "not_created_yet"
    resolved = validate_output_dir_path(nonexistent)
    assert resolved == nonexistent.resolve()
    assert not nonexistent.exists()

    existing_file = tmp_path / "occupied.txt"
    existing_file.write_text("hi", encoding="utf-8")
    with pytest.raises(ValidationError) as exc_info:
        validate_output_dir_path(existing_file)
    assert exc_info.value.i18n_key == "error.invalid_template"


def test_parse_batch_file_handles_bom_comments_whitespace_and_duplicates(tmp_path: Path) -> None:
    batch = tmp_path / "urls.txt"
    content = (
        "\ufeff# Leading BOM and comment\n"
        "\n"
        "   https://example.org/one   \n"
        "# another comment\n"
        "https://example.org/two\n"
        "https://example.org/one\n"
        "  \t  \n"
        "https://example.org/three\n"
        "https://example.org/two\n"
    )
    batch.write_text(content, encoding="utf-8")

    urls, dup_count = parse_batch_file(batch)
    assert urls == (
        "https://example.org/one",
        "https://example.org/two",
        "https://example.org/three",
    )
    assert dup_count == 2


def test_parse_batch_file_validation_errors(tmp_path: Path) -> None:
    missing = tmp_path / "does_not_exist.txt"
    with pytest.raises(ValidationError) as missing_exc:
        parse_batch_file(missing)
    assert missing_exc.value.i18n_key == "error.batch_file_unreadable"

    empty_file = tmp_path / "empty.txt"
    empty_file.write_text("# only comments\n\n  # nothing else\n", encoding="utf-8")
    with pytest.raises(ValidationError) as empty_exc:
        parse_batch_file(empty_file)
    assert empty_exc.value.i18n_key == "error.batch_empty"

    invalid_utf8 = tmp_path / "binary.txt"
    invalid_utf8.write_bytes(b"\xff\xfe\xfd\x80\x81")
    with pytest.raises(ValidationError) as utf8_exc:
        parse_batch_file(invalid_utf8)
    assert utf8_exc.value.i18n_key == "error.batch_file_unreadable"

    oversized = tmp_path / "huge.txt"
    oversized.write_bytes(b"https://example.org/a\n" * 60_000)  # > 1 MB
    with pytest.raises(ValidationError) as size_exc:
        parse_batch_file(oversized)
    assert size_exc.value.i18n_key == "error.batch_too_large"

    too_many = tmp_path / "many.txt"
    too_many.write_text(
        "\n".join(f"https://example.org/item-{i}" for i in range(MAX_BATCH_URLS + 1)),
        encoding="utf-8",
    )
    with pytest.raises(ValidationError) as count_exc:
        parse_batch_file(too_many)
    assert count_exc.value.i18n_key == "error.batch_too_many_urls"

    bad_line = tmp_path / "bad_line.txt"
    bad_line.write_text(
        "# comment line 1\nhttps://example.org/ok\nfile:///etc/passwd\nhttps://example.org/two\n",
        encoding="utf-8",
    )
    with pytest.raises(ValidationError) as line_exc:
        parse_batch_file(bad_line)
    assert line_exc.value.i18n_key == "error.batch_invalid_line"
    assert line_exc.value.params["line"] == 3
    assert line_exc.value.params["url"] == "file:///etc/passwd"


def test_collect_input_urls_orders_positional_first_and_cross_dedups(tmp_path: Path) -> None:
    batch = tmp_path / "urls.txt"
    batch.write_text(
        "https://example.org/b\nhttps://example.org/a\nhttps://example.org/b\n",
        encoding="utf-8",
    )
    urls, dups = collect_input_urls("https://example.org/a", batch)
    assert urls == ("https://example.org/a", "https://example.org/b")
    assert dups == 2

    with pytest.raises(ValidationError) as missing_exc:
        collect_input_urls(None, None)
    assert missing_exc.value.i18n_key == "cli.missing_url"


def test_check_free_disk_space_unknown_filesize_fallback_200mb(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Plenty of space (> 200 MB): succeeds and emits warning.
    monkeypatch.setattr(
        "unduhin.validators.shutil.disk_usage",
        lambda _p: _FakeUsage(total=10_000_000_000, used=1_000_000_000, free=CONSERVATIVE_UNKNOWN_SIZE_BYTES + 1024),
    )
    warning = check_free_disk_space(tmp_path, estimated_bytes=None, lang="en")
    assert warning is not None
    assert "200 MB" in warning

    # Less than 200 MB free: raises DiskSpaceError.
    monkeypatch.setattr(
        "unduhin.validators.shutil.disk_usage",
        lambda _p: _FakeUsage(total=10_000_000_000, used=9_900_000_000, free=CONSERVATIVE_UNKNOWN_SIZE_BYTES - 1),
    )
    with pytest.raises(DiskSpaceError) as exc_info:
        check_free_disk_space(tmp_path, estimated_bytes=None, lang="en")
    assert exc_info.value.i18n_key == "error.disk_full"


def test_reserve_destination_and_move_staged_file_race_handling(tmp_path: Path) -> None:
    # Intra-batch duplicate names under RENAME
    reserved: set[str] = set()
    p1, skip1 = reserve_destination(tmp_path, "Track.mp3", ConflictPolicy.RENAME, reserved)
    p2, skip2 = reserve_destination(tmp_path, "Track.mp3", ConflictPolicy.RENAME, reserved)
    assert (skip1, skip2) == (False, False)
    assert p1 == tmp_path / "Track.mp3"
    assert p2 == tmp_path / "Track (1).mp3"

    # Intra-batch duplicate names under SKIP
    reserved_skip: set[str] = set()
    s1, skip_s1 = reserve_destination(tmp_path, "Same.mp3", ConflictPolicy.SKIP, reserved_skip)
    s2, skip_s2 = reserve_destination(tmp_path, "Same.mp3", ConflictPolicy.SKIP, reserved_skip)
    assert s1 == tmp_path / "Same.mp3"
    assert skip_s1 is False
    assert s2 == tmp_path / "Same.mp3"
    assert skip_s2 is True

    # Race during move under SKIP: file created after reservation -> staged file discarded, was_moved=False
    staged_skip = tmp_path / "staged_skip.mp3"
    staged_skip.write_bytes(b"new-bytes")
    s1.write_bytes(b"concurrent-writer")
    moved_path, was_moved = move_staged_file(staged_skip, s1, tmp_path, ConflictPolicy.SKIP, reserved_skip)
    assert was_moved is False
    assert moved_path == s1
    assert not staged_skip.exists()
    assert s1.read_bytes() == b"concurrent-writer"

    # Race during move under RENAME: file appeared mid-download -> moves to next free name
    staged_rename = tmp_path / "staged_rename.mp3"
    staged_rename.write_bytes(b"second-bytes")
    p1.write_bytes(b"race-winner")
    final_rename, moved_rename = move_staged_file(
        staged_rename, p1, tmp_path, ConflictPolicy.RENAME, reserved
    )
    assert moved_rename is True
    assert final_rename == tmp_path / "Track (2).mp3"
    assert final_rename.read_bytes() == b"second-bytes"
    assert p1.read_bytes() == b"race-winner"

    # Race/overwrite during move under OVERWRITE: replaces destination
    staged_over = tmp_path / "staged_over.mp3"
    staged_over.write_bytes(b"fresh")
    reserved_over: set[str] = set()
    o1, _ = reserve_destination(tmp_path, "Over.mp3", ConflictPolicy.OVERWRITE, reserved_over)
    o1.write_bytes(b"stale")
    final_over, moved_over = move_staged_file(
        staged_over, o1, tmp_path, ConflictPolicy.OVERWRITE, reserved_over
    )
    assert moved_over is True
    assert final_over == o1
    assert o1.read_bytes() == b"fresh"


def test_render_filename_consistent_unknown_placeholder_and_flat_field_check() -> None:
    empty_info = MediaInfo(
        id="",
        title="",
        url="https://example.org/watch?v=1",
        uploader=None,
        upload_date=None,
    )
    assert (
        render_allowlisted_filename(empty_info, "{title}.{ext}", MediaMode.AUDIO)
        == f"{MISSING_TEMPLATE_FIELD_PLACEHOLDER}.mp3"
    )
    assert (
        render_allowlisted_filename(
            empty_info,
            "{upload_date} - {uploader} - {title} [{id}].{ext}",
            MediaMode.VIDEO,
        )
        == "unknown - unknown - unknown [unknown].mp4"
    )

    flat_entry = MediaInfo(
        id="flat1",
        title="Flat Entry Title",
        url="https://example.org/watch?v=flat1",
        uploader=None,
        upload_date=None,
    )
    assert template_fields_available(flat_entry, "{title}.{ext}") is True
    assert template_fields_available(flat_entry, "{title} [{id}].{ext}") is True
    assert template_fields_available(flat_entry, "{uploader} - {title}.{ext}") is False
    assert template_fields_available(flat_entry, "{upload_date} - {title}.{ext}") is False


@pytest.mark.parametrize(
    "link_exc",
    [
        OSError("cross-device link not permitted"),
        NotImplementedError("hard links unsupported"),
        PermissionError("access denied for hard link"),
    ],
)
def test_move_staged_file_fallback_without_hardlink_all_policies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    link_exc: BaseException,
) -> None:
    def _failing_link(_src: Path, _dst: Path) -> None:
        raise link_exc

    monkeypatch.setattr("unduhin.validators.os.link", _failing_link)

    # 1. RENAME: clean move + race collision
    s1 = tmp_path / "staged1.mp3"
    s1.write_bytes(b"first")
    final1, ok1 = move_staged_file(
        s1, tmp_path / "Song.mp3", tmp_path, ConflictPolicy.RENAME, {"song.mp3"}
    )
    assert ok1 is True
    assert final1 == tmp_path / "Song.mp3"
    assert final1.read_bytes() == b"first"

    s1_race = tmp_path / "staged1_race.mp3"
    s1_race.write_bytes(b"second")
    final1_race, ok1_race = move_staged_file(
        s1_race, tmp_path / "Song.mp3", tmp_path, ConflictPolicy.RENAME, {"song.mp3"}
    )
    assert ok1_race is True
    assert final1_race == tmp_path / "Song (1).mp3"
    assert final1_race.read_bytes() == b"second"

    # 2. SKIP: clean move when absent, discards staged when destination exists
    s2 = tmp_path / "staged2.mp3"
    s2.write_bytes(b"fresh-skip")
    final2, ok2 = move_staged_file(
        s2, tmp_path / "Unique.mp3", tmp_path, ConflictPolicy.SKIP, {"unique.mp3"}
    )
    assert ok2 is True
    assert final2.read_bytes() == b"fresh-skip"

    s2_race = tmp_path / "staged2_race.mp3"
    s2_race.write_bytes(b"dup-skip")
    final2_race, ok2_race = move_staged_file(
        s2_race, tmp_path / "Unique.mp3", tmp_path, ConflictPolicy.SKIP, {"unique.mp3"}
    )
    assert ok2_race is False
    assert not s2_race.exists()
    assert final2_race.read_bytes() == b"fresh-skip"

    # 3. OVERWRITE: replaces destination
    s3 = tmp_path / "staged3.mp3"
    s3.write_bytes(b"new-overwrite")
    target_over = tmp_path / "ReplaceMe.mp3"
    target_over.write_bytes(b"old-content")
    final3, ok3 = move_staged_file(
        s3, target_over, tmp_path, ConflictPolicy.OVERWRITE, {"replaceme.mp3"}
    )
    assert ok3 is True
    assert final3 == target_over
    assert target_over.read_bytes() == b"new-overwrite"


def test_cleanup_path_with_retry_delays_recovery_and_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import shutil as real_shutil

    staging = tmp_path / ".unduhin-tmp-999999-abc"
    staging.mkdir()
    (staging / "part.tmp").write_bytes(b"locked")

    attempts = 0
    real_rmtree = real_shutil.rmtree

    def _flaky_rmtree(path: Path) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise PermissionError("locked 1")
        if attempts == 2:
            raise OSError("locked 2")
        real_rmtree(path)

    sleeps_ok: list[float] = []
    monkeypatch.setattr("unduhin.validators.shutil.rmtree", _flaky_rmtree)
    warn_ok = cleanup_path_with_retry(staging, lang="en", sleep_fn=sleeps_ok.append)
    assert warn_ok is None
    assert not staging.exists()
    assert sleeps_ok == [0.2, 0.5]

    # Permanent PermissionError exhausts 3 retries (0.2s, 0.5s, 1.0s) and warns
    stale = tmp_path / ".unduhin-tmp-999999-cafe"
    stale.mkdir()

    def _always_locked(_path: Path) -> None:
        raise PermissionError("still open")

    sleeps_fail: list[float] = []
    monkeypatch.setattr("unduhin.validators.shutil.rmtree", _always_locked)
    warnings = cleanup_stale_staging_dirs(
        tmp_path,
        lang="en",
        is_pid_alive=lambda _pid: False,
        sleep_fn=sleeps_fail.append,
    )
    assert sleeps_fail == [0.2, 0.5, 1.0]
    assert len(warnings) == 1
    assert str(stale) in warnings[0]


def test_resolve_output_dir_precedence_and_validation(tmp_path: Path) -> None:
    env_dir = tmp_path / "from_env"
    cli_dir = tmp_path / "from_cli"

    # Explicit CLI overrides env var
    assert resolve_output_dir(cli_dir, environ={"UNDUHIN_OUTPUT_DIR": str(env_dir)}) == cli_dir.resolve()
    # Env var overrides default
    assert resolve_output_dir(None, environ={"UNDUHIN_OUTPUT_DIR": str(env_dir)}) == env_dir.resolve()
    # Blank env var falls back to ~/Downloads/unduhin
    assert resolve_output_dir(None, environ={"UNDUHIN_OUTPUT_DIR": "   "}) == get_default_output_dir().resolve()

    # Invalid file path rejected without creating directory
    blocker = tmp_path / "existing_file"
    blocker.write_text("not a dir", encoding="utf-8")
    with pytest.raises(ValidationError) as exc_info:
        resolve_output_dir(None, environ={"UNDUHIN_OUTPUT_DIR": str(blocker)})
    assert exc_info.value.i18n_key == "error.invalid_template"


def test_path_length_limit_truncates_title_and_rejects_overlong_dir(tmp_path: Path) -> None:
    # Build a long directory leaving ~45 chars below MAX_FINAL_PATH_LENGTH (240)
    deep = tmp_path
    while len(str(deep)) < MAX_FINAL_PATH_LENGTH - 55:
        deep = deep / ("nested_dir_segment_12345")
    resolved_dir = validate_output_dir_path(deep)
    assert len(str(resolved_dir)) <= MAX_FINAL_PATH_LENGTH - 1 - MIN_FILENAME_ROOM

    long_title = "Very Long Track Title With Trailing Spaces And Dots " * 3
    rendered = fit_filename_to_path_limit(resolved_dir, f"{long_title}.mp3")
    assert rendered.endswith(".mp3")
    assert len(str(resolved_dir / rendered)) <= MAX_FINAL_PATH_LENGTH

    # Duplicate rename (1) also fits cleanly within MAX_FINAL_PATH_LENGTH
    reserved: set[str] = set()
    first = reserve_destination_detailed(resolved_dir, f"{long_title}.mp3", ConflictPolicy.RENAME, reserved)
    second = reserve_destination_detailed(resolved_dir, f"{long_title}.mp3", ConflictPolicy.RENAME, reserved)
    assert len(str(first.path)) <= MAX_FINAL_PATH_LENGTH
    assert len(str(second.path)) <= MAX_FINAL_PATH_LENGTH
    assert "(1).mp3" in second.path.name

    # Too-long directory leaves fewer than MIN_FILENAME_ROOM chars -> ValidationError
    overlong_dir = deep / ("x" * 60)
    with pytest.raises(ValidationError) as too_long_exc:
        validate_output_dir_path(overlong_dir)
    assert too_long_exc.value.i18n_key == "error.output_path_too_long"
    assert not overlong_dir.exists()
