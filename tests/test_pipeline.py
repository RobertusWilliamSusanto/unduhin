"""Tests for core download pipeline orchestration, staging, retries, and cleanup."""

from __future__ import annotations

import errno
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

import pytest
from tests.conftest import FakeMediaAdapter

from unduhin.config import (
    CONSECUTIVE_RATE_LIMIT_BREAKER,
    CONSERVATIVE_UNKNOWN_SIZE_BYTES,
    MAX_PLAYLIST_ITEMS_CAP,
    MAX_PLAYLIST_NESTING_DEPTH,
    MAX_TOTAL_ITEMS_GLOBAL,
    MAX_TOTAL_SLEEP_PER_ITEM_SECONDS,
    STAGING_DIR_PREFIX,
    AdapterDownloadResult,
    ConflictPolicy,
    DownloadRequest,
    ExitCode,
    FormatOption,
    ItemStatus,
    MediaInfo,
    MediaMode,
)
from unduhin.errors import DependencyError, PlaylistDisabledError, ValidationError
from unduhin.i18n import t
from unduhin.pipeline import PipelineHooks, inspect_url, run_download


def test_pipeline_http_403_transient_retries_and_recovers_or_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("unduhin.pipeline.find_ffmpeg", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr("unduhin.pipeline.find_deno", lambda: "/usr/bin/deno")

    url_recover = "https://example.org/retry-403-ok"
    recover_attempts = 0

    def _dl_recover(*, staging_dir: Path, **_kw: object) -> AdapterDownloadResult:
        nonlocal recover_attempts
        recover_attempts += 1
        if recover_attempts == 1:
            raise RuntimeError("HTTP Error 403: Forbidden")
        target = staging_dir / "vid.mp3"
        target.write_bytes(b"recovered")
        return AdapterDownloadResult(filepath=target, info=MediaInfo(id="r1", title="Rec", url=url_recover))

    retry_events: list[tuple[int, int, str]] = []
    hooks = PipelineHooks(
        on_retry=lambda att, mx, _dly, rsn: retry_events.append((att, mx, rsn)),
        sleep_fn=lambda _d: None,
    )
    adapter = FakeMediaAdapter(
        info_by_url={url_recover: MediaInfo(id="r1", title="Rec", url=url_recover)},
        download_handler=_dl_recover,
    )
    summary = run_download(
        DownloadRequest(urls=(url_recover,), output_dir=tmp_path / "rec_out", max_retries=2, lang="en"),
        adapter,
        hooks=hooks,
    )
    assert summary.exit_code == ExitCode.SUCCESS
    assert recover_attempts == 2
    assert len(retry_events) == 1
    assert "403" in retry_events[0][2]

    # Persistent 403 fails and reports OutdatedExtractorError (ytdlp_outdated)
    url_fail = "https://example.org/retry-403-fail"
    fail_attempts = 0

    def _dl_fail(*args: object, **_kw: object) -> AdapterDownloadResult:
        nonlocal fail_attempts
        fail_attempts += 1
        raise RuntimeError("HTTP Error 403: Forbidden")

    retry_fail_events: list[tuple[int, int, str]] = []
    hooks_fail = PipelineHooks(
        on_retry=lambda att, mx, _dly, rsn: retry_fail_events.append((att, mx, rsn)),
        sleep_fn=lambda _d: None,
    )
    adapter_fail = FakeMediaAdapter(
        info_by_url={url_fail: MediaInfo(id="f1", title="Fail", url=url_fail)},
        download_handler=_dl_fail,
    )
    summary_fail = run_download(
        DownloadRequest(urls=(url_fail,), output_dir=tmp_path / "fail_out", max_retries=2, lang="en"),
        adapter_fail,
        hooks=hooks_fail,
    )
    assert summary_fail.exit_code == ExitCode.FAILURE
    assert fail_attempts == 3  # initial + 2 retries
    assert len(retry_fail_events) == 2
    assert "pip install" in summary_fail.results[0].message
    assert "uv tool upgrade" in summary_fail.results[0].message


def test_pipeline_audio_only_video_warns_in_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("unduhin.pipeline.find_ffmpeg", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr("unduhin.pipeline.find_deno", lambda: "/usr/bin/deno")

    url = "https://example.org/audio-only-source"
    info = MediaInfo(id="ao1", title="Audio Only Source", url=url)

    def _dl_ao(*, staging_dir: Path, **_kw: object) -> AdapterDownloadResult:
        target = staging_dir / "vid.mp4"
        target.write_bytes(b"fake-mp4")
        return AdapterDownloadResult(
            filepath=target,
            info=info,
            codec_fallback_used=True,
            vcodec="none",
            acodec="mp3",
            is_audio_only_video=True,
        )

    adapter = FakeMediaAdapter(info_by_url={url: info}, download_handler=_dl_ao)
    summary = run_download(
        DownloadRequest(urls=(url,), output_dir=tmp_path / "ao_out", mode=MediaMode.VIDEO, lang="en"),
        adapter,
    )
    assert summary.exit_code == ExitCode.SUCCESS
    res = summary.results[0]
    assert any("audio-only" in w.lower() for w in res.warnings)
    assert any("audio-only" in w.lower() for w in summary.warnings)


def test_pipeline_audio_download_moves_sanitized_file_and_cleans_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("unduhin.pipeline.find_ffmpeg", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr("unduhin.pipeline.find_deno", lambda: "/usr/bin/deno")

    out_dir = tmp_path / "out"
    adapter = FakeMediaAdapter(
        info_by_url={
            "https://example.org/v1": MediaInfo(
                id="v1",
                title="Track: One? <Demo>",
                url="https://example.org/v1",
                uploader="Artist/A",
            )
        }
    )

    progress_events: list[str] = []
    req = DownloadRequest(
        urls=("https://example.org/v1",),
        mode=MediaMode.AUDIO,
        output_dir=out_dir,
        filename_template="{uploader} - {title}.{ext}",
        lang="en",
    )
    hooks = PipelineHooks(on_progress=lambda e: progress_events.append(e.status))
    summary = run_download(req, adapter, hooks=hooks)

    assert summary.exit_code == ExitCode.SUCCESS
    assert summary.succeeded_count == 1
    expected_file = out_dir / "Artist_A - Track_ One_ _Demo_.mp3"
    assert expected_file.exists()
    assert expected_file.read_bytes() == b"fake-media-bytes"
    assert not any(p.name.startswith(STAGING_DIR_PREFIX) for p in out_dir.iterdir())
    assert progress_events == ["downloading", "finished"]


def test_pipeline_rejects_unexpected_extension_from_generic_extractor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("unduhin.pipeline.find_ffmpeg", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr("unduhin.pipeline.find_deno", lambda: "/usr/bin/deno")

    out_dir = tmp_path / "out"

    def _bad_ext_download(*, staging_dir: Path, url: str, **_kwargs: object) -> AdapterDownloadResult:
        evil = staging_dir / "index.html"
        evil.write_text("<html>not media</html>", encoding="utf-8")
        return AdapterDownloadResult(
            filepath=evil,
            info=MediaInfo(id="x1", title="Page", url=url),
        )

    adapter = FakeMediaAdapter(download_handler=_bad_ext_download)
    req = DownloadRequest(urls=("https://example.org/page",), mode=MediaMode.VIDEO, output_dir=out_dir, lang="en")
    summary = run_download(req, adapter)

    assert summary.exit_code == ExitCode.INVALID_INPUT
    assert summary.failed_count == 1
    assert not list(out_dir.glob("*"))


def test_pipeline_skips_ffmpeg_check_on_dry_run_and_requires_for_real_download(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("unduhin.pipeline.find_ffmpeg", lambda: None)
    monkeypatch.setattr("unduhin.pipeline.find_deno", lambda: None)

    out_dir = tmp_path / "out"
    adapter = FakeMediaAdapter()

    dry_req = DownloadRequest(
        urls=("https://www.youtube.com/watch?v=vid123",),
        mode=MediaMode.AUDIO,
        output_dir=out_dir,
        dry_run=True,
        lang="en",
    )
    dry_summary = run_download(dry_req, adapter)
    assert dry_summary.exit_code == ExitCode.SUCCESS
    assert dry_summary.planned_count == 1
    assert len(adapter.download_calls) == 0
    assert any("Deno" in w for w in dry_summary.warnings)

    real_req = DownloadRequest(
        urls=("https://www.youtube.com/watch?v=vid123",),
        mode=MediaMode.AUDIO,
        output_dir=out_dir,
        dry_run=False,
        lang="en",
    )
    with pytest.raises(DependencyError):
        run_download(real_req, adapter)


def test_pipeline_emits_codec_fallback_warning_for_non_h264_aac_mp4(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("unduhin.pipeline.find_ffmpeg", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr("unduhin.pipeline.find_deno", lambda: "/usr/bin/deno")

    out_dir = tmp_path / "out"

    def _vp9_download(*, staging_dir: Path, url: str, **_kwargs: object) -> AdapterDownloadResult:
        staged = staging_dir / "vid123.mp4"
        staged.write_bytes(b"vp9-opus-mp4")
        return AdapterDownloadResult(
            filepath=staged,
            info=MediaInfo(id="vid123", title="Fallback Video", url=url),
            codec_fallback_used=True,
            vcodec="vp9",
            acodec="opus",
        )

    warnings: list[str] = []
    adapter = FakeMediaAdapter(download_handler=_vp9_download)
    req = DownloadRequest(urls=("https://example.org/v",), mode=MediaMode.VIDEO, output_dir=out_dir, lang="id")
    summary = run_download(req, adapter, hooks=PipelineHooks(on_warning=warnings.append))

    assert summary.exit_code == ExitCode.SUCCESS
    assert len(warnings) == 1
    assert "vp9/opus" in warnings[0]


def test_pipeline_cancellation_halts_entire_batch_and_cleans_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("unduhin.pipeline.find_ffmpeg", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr("unduhin.pipeline.find_deno", lambda: "/usr/bin/deno")

    out_dir = tmp_path / "out"
    cancelled = False

    def _cancel_mid_download(*, staging_dir: Path, **_kwargs: object) -> AdapterDownloadResult:
        nonlocal cancelled
        (staging_dir / "vid123.mp4.part").write_bytes(b"partial")
        cancelled = True
        raise KeyboardInterrupt()

    adapter = FakeMediaAdapter(download_handler=_cancel_mid_download)
    req = DownloadRequest(
        urls=("https://example.org/first", "https://example.org/second"),
        mode=MediaMode.VIDEO,
        output_dir=out_dir,
        lang="en",
    )
    summary = run_download(
        req,
        adapter,
        hooks=PipelineHooks(is_cancelled=lambda: cancelled),
    )

    assert summary.interrupted is True
    assert summary.exit_code == ExitCode.CANCELLED
    # Second URL must never even be inspected/downloaded once cancelled.
    assert len(adapter.extract_calls) == 1
    assert len(adapter.download_calls) == 1
    assert not any(p.name.startswith(STAGING_DIR_PREFIX) for p in out_dir.iterdir())


def test_pipeline_retries_transient_and_applies_longer_backoff_for_429(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("unduhin.pipeline.find_ffmpeg", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr("unduhin.pipeline.find_deno", lambda: "/usr/bin/deno")
    monkeypatch.setattr("unduhin.pipeline.random.uniform", lambda _a, _b: 0.0)

    out_dir = tmp_path / "out"
    attempts = 0
    sleep_durations: list[float] = []

    def _flaky_download(*, staging_dir: Path, url: str, **_kwargs: object) -> AdapterDownloadResult:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("HTTP Error 429: Too Many Requests")
        if attempts == 2:
            raise TimeoutError("timed out")
        staged = staging_dir / "vid123.mp3"
        staged.write_bytes(b"ok-after-retries")
        return AdapterDownloadResult(
            filepath=staged,
            info=MediaInfo(id="vid123", title="Recovered", url=url),
        )

    adapter = FakeMediaAdapter(download_handler=_flaky_download)
    req = DownloadRequest(
        urls=("https://example.org/flaky",),
        mode=MediaMode.AUDIO,
        output_dir=out_dir,
        max_retries=3,
        backoff_base_seconds=1.0,
        rate_limit_backoff_seconds=8.0,
        lang="en",
    )
    hooks = PipelineHooks(sleep_fn=sleep_durations.append)
    summary = run_download(req, adapter, hooks=hooks)

    assert summary.exit_code == ExitCode.SUCCESS
    assert attempts == 3
    # Attempt 1 was HTTP 429 (8.0 * 2^0 = 8.0s total in 0.1s slices), Attempt 2 was timeout (1.0 * 2^1 = 2.0s).
    total_slept = sum(sleep_durations)
    assert pytest.approx(total_slept, abs=0.05) == 10.0


def test_pipeline_conflict_policies_end_to_end(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("unduhin.pipeline.find_ffmpeg", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr("unduhin.pipeline.find_deno", lambda: "/usr/bin/deno")

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    existing = out_dir / "Sample Creative Commons Track.mp3"
    existing.write_bytes(b"old-content")

    adapter = FakeMediaAdapter()

    # Default RENAME keeps old file and writes " (1).mp3"
    summary_rename = run_download(
        DownloadRequest(
            urls=("https://example.org/v",),
            output_dir=out_dir,
            conflict_policy=ConflictPolicy.RENAME,
        ),
        adapter,
    )
    assert summary_rename.succeeded_count == 1
    assert (out_dir / "Sample Creative Commons Track (1).mp3").exists()
    assert existing.read_bytes() == b"old-content"

    # SKIP avoids downloading completely
    summary_skip = run_download(
        DownloadRequest(
            urls=("https://example.org/v",),
            output_dir=out_dir,
            conflict_policy=ConflictPolicy.SKIP,
        ),
        adapter,
    )
    assert summary_skip.skipped_count == 1
    assert summary_skip.skipped_exists_count == 1
    assert summary_skip.results[0].status is ItemStatus.SKIPPED_EXISTS


class _FakeDiskUsage(NamedTuple):
    total: int
    used: int
    free: int


@pytest.mark.parametrize(
    ("case_id", "stage", "info_override", "error_factory", "expected_key", "expected_status", "expected_exit"),
    [
        (
            "private_video",
            "extract",
            None,
            lambda: RuntimeError("Private video. Sign in if you've been granted access"),
            "error.video_unavailable",
            ItemStatus.FAILED,
            ExitCode.FAILURE,
        ),
        (
            "deleted_video",
            "download",
            None,
            lambda: RuntimeError("Video unavailable. This video has been removed by the uploader"),
            "error.video_unavailable",
            ItemStatus.FAILED,
            ExitCode.FAILURE,
        ),
        (
            "age_restricted",
            "extract",
            None,
            lambda: RuntimeError("Sign in to confirm your age"),
            "error.age_restricted",
            ItemStatus.FAILED,
            ExitCode.FAILURE,
        ),
        (
            "geo_blocked",
            "extract",
            None,
            lambda: RuntimeError("This video is not available in your country"),
            "error.geo_blocked",
            ItemStatus.FAILED,
            ExitCode.FAILURE,
        ),
        (
            "live_stream_metadata",
            "info",
            MediaInfo(id="live1", title="Live Now", url="https://example.org/live", is_live=True),
            None,
            "error.live_stream_unsupported",
            ItemStatus.SKIPPED_UNSUPPORTED,
            ExitCode.FAILURE,
        ),
        (
            "upcoming_premiere_metadata",
            "info",
            MediaInfo(
                id="up1",
                title="Upcoming Premiere",
                url="https://example.org/upcoming",
                live_status="is_upcoming",
            ),
            None,
            "error.live_stream_unsupported",
            ItemStatus.SKIPPED_UNSUPPORTED,
            ExitCode.FAILURE,
        ),
        (
            "live_stream_error",
            "download",
            None,
            lambda: RuntimeError("This live event will begin in a few moments"),
            "error.live_stream_unsupported",
            ItemStatus.SKIPPED_UNSUPPORTED,
            ExitCode.FAILURE,
        ),
        (
            "drm_metadata",
            "info",
            MediaInfo(id="drm1", title="DRM Track", url="https://example.org/drm", is_drm=True),
            None,
            "error.drm_protected",
            ItemStatus.FAILED,
            ExitCode.FAILURE,
        ),
        (
            "drm_error",
            "download",
            None,
            lambda: RuntimeError("This video is protected by Widevine DRM"),
            "error.drm_protected",
            ItemStatus.FAILED,
            ExitCode.FAILURE,
        ),
        (
            "unsupported_url",
            "extract",
            None,
            lambda: RuntimeError("Unsupported URL: https://example.org/page"),
            "error.unsupported_url",
            ItemStatus.FAILED,
            ExitCode.FAILURE,
        ),
        (
            "outdated_ytdlp",
            "extract",
            None,
            lambda: RuntimeError("Unable to extract video data; nsig extraction failed"),
            "error.ytdlp_outdated",
            ItemStatus.FAILED,
            ExitCode.FAILURE,
        ),
        (
            "disk_full_enospc",
            "download",
            None,
            lambda: OSError(errno.ENOSPC, "No space left on device"),
            "error.disk_full",
            ItemStatus.FAILED,
            ExitCode.FAILURE,
        ),
    ],
)
def test_pipeline_edge_case_matrix(
    case_id: str,
    stage: str,
    info_override: MediaInfo | None,
    error_factory: Callable[[], Exception] | None,
    expected_key: str,
    expected_status: ItemStatus,
    expected_exit: ExitCode,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ = case_id
    monkeypatch.setattr("unduhin.pipeline.find_ffmpeg", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr("unduhin.pipeline.find_deno", lambda: "/usr/bin/deno")
    out_dir = tmp_path / "out"
    target_url = info_override.url if info_override is not None else "https://example.org/media"

    def _failing_download(*, staging_dir: Path, **_kwargs: object) -> AdapterDownloadResult:
        (staging_dir / "partial.mp3.part").write_bytes(b"incomplete")
        assert error_factory is not None
        raise error_factory()

    adapter = FakeMediaAdapter(
        info_by_url={target_url: info_override} if info_override is not None else None,
        extract_error=error_factory() if stage == "extract" and error_factory is not None else None,
        download_handler=_failing_download if stage == "download" else None,
    )
    req = DownloadRequest(
        urls=(target_url,),
        mode=MediaMode.AUDIO,
        output_dir=out_dir,
        max_retries=0,
        lang="en",
    )
    summary = run_download(req, adapter)

    assert len(summary.results) == 1
    result = summary.results[0]
    assert result.status is expected_status
    assert summary.exit_code == expected_exit
    expected_msg = t(expected_key, lang="en", path=str(out_dir))
    assert result.message == expected_msg
    if expected_key == "error.age_restricted":
        assert "sign-in" in (result.message or "")
    leftovers = list(out_dir.rglob("*")) if out_dir.exists() else []
    assert not any(
        p.name.startswith(STAGING_DIR_PREFIX) or p.name.endswith(".part") for p in leftovers
    )


def test_pipeline_enospc_during_final_move_cleans_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("unduhin.pipeline.find_ffmpeg", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr("unduhin.pipeline.find_deno", lambda: "/usr/bin/deno")
    monkeypatch.setattr(
        "unduhin.pipeline.move_staged_file",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError(errno.ENOSPC, "No space left on device")),
    )

    out_dir = tmp_path / "out"
    adapter = FakeMediaAdapter()
    summary = run_download(
        DownloadRequest(urls=("https://example.org/v",), output_dir=out_dir, lang="en"),
        adapter,
    )

    assert summary.exit_code == ExitCode.FAILURE
    assert summary.failed_count == 1
    assert summary.succeeded_count == 0
    assert summary.results[0].message == t("error.disk_full", lang="en", path=str(out_dir))
    assert list(out_dir.rglob("*")) == []


def test_pipeline_unknown_filesize_warns_and_enforces_200mb_floor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("unduhin.pipeline.find_ffmpeg", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr("unduhin.pipeline.find_deno", lambda: "/usr/bin/deno")

    url = "https://example.org/unknown-size"
    info = MediaInfo(id="unk1", title="No Size Track", url=url, filesize_approx=None)
    adapter = FakeMediaAdapter(info_by_url={url: info})

    monkeypatch.setattr(
        "unduhin.validators.shutil.disk_usage",
        lambda _p: _FakeDiskUsage(
            total=10_000_000_000,
            used=1_000_000_000,
            free=CONSERVATIVE_UNKNOWN_SIZE_BYTES + 10_000,
        ),
    )
    ok_summary = run_download(
        DownloadRequest(urls=(url,), output_dir=tmp_path / "ok", lang="en"),
        adapter,
    )
    assert ok_summary.exit_code == ExitCode.SUCCESS
    assert ok_summary.succeeded_count == 1
    assert any("200 MB" in w for w in ok_summary.warnings)

    monkeypatch.setattr(
        "unduhin.validators.shutil.disk_usage",
        lambda _p: _FakeDiskUsage(
            total=10_000_000_000,
            used=9_900_000_000,
            free=CONSERVATIVE_UNKNOWN_SIZE_BYTES - 1,
        ),
    )
    low_dir = tmp_path / "low"
    fail_summary = run_download(
        DownloadRequest(urls=(url,), output_dir=low_dir, lang="en"),
        adapter,
    )
    assert fail_summary.exit_code == ExitCode.FAILURE
    assert fail_summary.failed_count == 1
    assert fail_summary.results[0].message == t("error.disk_full", lang="en", path=str(low_dir))


def test_pipeline_retry_exhaustion_fails_cleanly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("unduhin.pipeline.find_ffmpeg", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr("unduhin.pipeline.find_deno", lambda: "/usr/bin/deno")
    monkeypatch.setattr("unduhin.pipeline.random.uniform", lambda _a, _b: 0.0)

    out_dir = tmp_path / "out"
    attempts = 0
    sleep_durations: list[float] = []

    def _always_timeout(*, staging_dir: Path, **_kwargs: object) -> AdapterDownloadResult:
        nonlocal attempts
        attempts += 1
        (staging_dir / "vid.mp3.part").write_bytes(b"partial")
        raise TimeoutError("timed out")

    adapter = FakeMediaAdapter(download_handler=_always_timeout)
    summary = run_download(
        DownloadRequest(
            urls=("https://example.org/drop",),
            output_dir=out_dir,
            max_retries=2,
            backoff_base_seconds=1.0,
            lang="en",
        ),
        adapter,
        hooks=PipelineHooks(sleep_fn=sleep_durations.append),
    )

    assert attempts == 3
    assert pytest.approx(sum(sleep_durations), abs=0.05) == 3.0
    assert summary.exit_code == ExitCode.FAILURE
    assert summary.failed_count == 1
    assert summary.results[0].status is ItemStatus.FAILED
    assert "timed out" in (summary.results[0].message or "")
    assert list(out_dir.rglob("*")) == []


def test_pipeline_ctrl_c_overrides_prior_failure_with_exit_130(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("unduhin.pipeline.find_ffmpeg", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr("unduhin.pipeline.find_deno", lambda: "/usr/bin/deno")

    out_dir = tmp_path / "out"
    cancelled = False

    def _handler(*, url: str, staging_dir: Path, **_kwargs: object) -> AdapterDownloadResult:
        nonlocal cancelled
        if url.endswith("/1"):
            raise RuntimeError("Private video")
        (staging_dir / "partial.mp3.part").write_bytes(b"partial")
        cancelled = True
        raise KeyboardInterrupt()

    adapter = FakeMediaAdapter(download_handler=_handler)
    summary = run_download(
        DownloadRequest(
            urls=(
                "https://example.org/1",
                "https://example.org/2",
                "https://example.org/3",
            ),
            output_dir=out_dir,
            max_retries=0,
            lang="en",
        ),
        adapter,
        hooks=PipelineHooks(is_cancelled=lambda: cancelled),
    )

    assert summary.interrupted is True
    assert summary.exit_code == ExitCode.CANCELLED
    assert summary.failed_count == 1
    assert len(adapter.extract_calls) == 2
    assert list(out_dir.rglob("*")) == []


def test_pipeline_playlist_disabled_expansion_nesting_and_truncation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("unduhin.pipeline.find_ffmpeg", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr("unduhin.pipeline.find_deno", lambda: "/usr/bin/deno")

    pl_url = "https://example.org/playlist"
    nested_deep = MediaInfo(
        id="deep",
        title="Too Deep Playlist",
        url="https://example.org/deep",
        is_playlist=True,
        entries=(MediaInfo(id="d1", title="Depth 3 Song", url="https://example.org/d1"),),
    )
    nested_ok = MediaInfo(
        id="sub",
        title="Sub Playlist",
        url="https://example.org/sub",
        is_playlist=True,
        entries=(
            MediaInfo(id="s1", title="Depth 2 Song", url="https://example.org/s1"),
            nested_deep,
        ),
    )
    playlist_info = MediaInfo(
        id="pl1",
        title="Main Playlist",
        url=pl_url,
        is_playlist=True,
        playlist_count=4,
        entries=(
            MediaInfo(id="a1", title="First Song", url="https://example.org/a1"),
            MediaInfo(id="unavail", title="unavailable", url="", is_unavailable=True),
            nested_ok,
        ),
    )
    adapter = FakeMediaAdapter(info_by_url={pl_url: playlist_info})

    # Playlist without --playlist -> exit 2 (INVALID_INPUT)
    rejected = run_download(
        DownloadRequest(urls=(pl_url,), output_dir=tmp_path / "no_pl", allow_playlist=False, lang="en"),
        adapter,
    )
    assert rejected.exit_code == ExitCode.INVALID_INPUT
    assert rejected.results[0].message == t("error.playlist_disabled", lang="en")

    # Playlist with --playlist flattens depth 2, skips depth > 2 + unavailable entry, downloads rest
    out_pl = tmp_path / "with_pl"
    accepted = run_download(
        DownloadRequest(
            urls=(pl_url,),
            output_dir=out_pl,
            allow_playlist=True,
            max_items=50,
            lang="en",
        ),
        adapter,
    )
    assert accepted.exit_code == ExitCode.FAILURE
    assert accepted.succeeded_count == 2
    assert accepted.skipped_count == 1
    assert accepted.skipped_unsupported_count == 1
    assert (out_pl / "First Song.mp3").exists()
    assert (out_pl / "Depth 2 Song.mp3").exists()
    assert any(
        str(MAX_PLAYLIST_NESTING_DEPTH) in w and "depth" in w.lower() for w in accepted.warnings
    )
    # Truncation check with small max_items
    big_pl = MediaInfo(
        id="pl_big",
        title="Big Playlist",
        url=pl_url,
        is_playlist=True,
        playlist_count=4,
        entries=tuple(
            MediaInfo(id=f"i{i}", title=f"Track {i}", url=f"https://example.org/t{i}")
            for i in range(1, 5)
        ),
    )
    trunc_summary = run_download(
        DownloadRequest(
            urls=(pl_url,),
            output_dir=tmp_path / "trunc",
            allow_playlist=True,
            max_items=2,
            lang="en",
        ),
        FakeMediaAdapter(info_by_url={pl_url: big_pl}),
    )
    assert trunc_summary.exit_code == ExitCode.SUCCESS
    assert trunc_summary.succeeded_count == 2
    assert any("truncated to 2" in w for w in trunc_summary.warnings)

    # Exceeding hard cap MAX_PLAYLIST_ITEMS_CAP rejected without creating dir
    over_cap_dir = tmp_path / "over_cap"
    with pytest.raises(ValidationError):
        run_download(
            DownloadRequest(
                urls=(pl_url,),
                output_dir=over_cap_dir,
                allow_playlist=True,
                max_items=MAX_PLAYLIST_ITEMS_CAP + 1,
                lang="en",
            ),
            adapter,
        )
    assert not over_cap_dir.exists()


def test_pipeline_dry_run_has_zero_disk_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("unduhin.pipeline.find_ffmpeg", lambda: None)
    monkeypatch.setattr("unduhin.pipeline.find_deno", lambda: None)

    never_created_dir = tmp_path / "never_created"
    pl_url = "https://example.org/pl"
    pl_info = MediaInfo(
        id="pl",
        title="Playlist",
        url=pl_url,
        is_playlist=True,
        entries=(
            MediaInfo(id="v1", title="Same Title", url="https://example.org/v1"),
            MediaInfo(id="v2", title="Same Title", url="https://example.org/v2"),
        ),
    )
    summary = run_download(
        DownloadRequest(
            urls=(pl_url,),
            output_dir=never_created_dir,
            allow_playlist=True,
            dry_run=True,
            lang="en",
        ),
        FakeMediaAdapter(info_by_url={pl_url: pl_info}),
    )
    assert summary.exit_code == ExitCode.SUCCESS
    assert summary.planned_count == 2
    assert summary.results[0].output_path == never_created_dir.resolve() / "Same Title.mp3"
    assert summary.results[1].output_path == never_created_dir.resolve() / "Same Title (1).mp3"
    assert not never_created_dir.exists()

    # Even in an existing directory, dry-run does not touch or delete stale dirs.
    existing_dir = tmp_path / "existing"
    existing_dir.mkdir()
    stale = existing_dir / ".unduhin-tmp-999999-abcdef1234"
    stale.mkdir()
    run_download(
        DownloadRequest(
            urls=(pl_url,),
            output_dir=existing_dir,
            allow_playlist=True,
            dry_run=True,
            lang="en",
        ),
        FakeMediaAdapter(info_by_url={pl_url: pl_info}),
    )
    assert stale.exists()


def test_pipeline_invalid_inputs_fail_fast_without_touching_disk(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("unduhin.pipeline.find_ffmpeg", lambda: "/usr/bin/ffmpeg")
    adapter = FakeMediaAdapter()

    stale_parent = tmp_path / "existing_with_stale"
    stale_parent.mkdir()
    stale_dir = stale_parent / ".unduhin-tmp-999999-abcdef1234"
    stale_dir.mkdir()

    with pytest.raises(ValidationError):
        run_download(
            DownloadRequest(
                urls=("file:///etc/passwd",),
                output_dir=stale_parent,
                lang="en",
            ),
            adapter,
        )
    assert stale_dir.exists()

    never_dir = tmp_path / "never_created_bad_tpl"
    with pytest.raises(ValidationError):
        run_download(
            DownloadRequest(
                urls=("https://example.org/ok",),
                output_dir=never_dir,
                filename_template="{bad_field}.{ext}",
                lang="en",
            ),
            adapter,
        )
    assert not never_dir.exists()
    assert len(adapter.extract_calls) == 0


def test_pipeline_intra_batch_conflicts_skip_race_and_all_skipped_exit_0(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("unduhin.pipeline.find_ffmpeg", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr("unduhin.pipeline.find_deno", lambda: "/usr/bin/deno")

    u1, u2 = "https://example.org/one", "https://example.org/two"
    same_infos = {
        u1: MediaInfo(id="id1", title="Duplicate Title", url=u1),
        u2: MediaInfo(id="id2", title="Duplicate Title", url=u2),
    }

    # RENAME: both succeed with unique names
    dir_rename = tmp_path / "rename"
    res_rename = run_download(
        DownloadRequest(urls=(u1, u2), output_dir=dir_rename, conflict_policy=ConflictPolicy.RENAME),
        FakeMediaAdapter(info_by_url=same_infos),
    )
    assert res_rename.exit_code == ExitCode.SUCCESS
    assert res_rename.succeeded_count == 2
    assert (dir_rename / "Duplicate Title.mp3").exists()
    assert (dir_rename / "Duplicate Title (1).mp3").exists()

    # SKIP: second intra-batch duplicate is skipped without calling download a second time
    dir_skip = tmp_path / "skip"
    adapter_skip = FakeMediaAdapter(info_by_url=same_infos)
    res_skip = run_download(
        DownloadRequest(urls=(u1, u2), output_dir=dir_skip, conflict_policy=ConflictPolicy.SKIP),
        adapter_skip,
    )
    assert res_skip.exit_code == ExitCode.SUCCESS
    assert res_skip.succeeded_count == 1
    assert res_skip.skipped_count == 1
    assert len(adapter_skip.download_calls) == 1

    # Concurrent race under SKIP: target file appears mid-download -> recorded SKIPPED, not SUCCEEDED
    dir_race = tmp_path / "race"

    def _race_download(*, url: str, staging_dir: Path, **_kwargs: object) -> AdapterDownloadResult:
        (dir_race / "Race Title.mp3").write_bytes(b"written-by-concurrent-process")
        staged = staging_dir / "r1.mp3"
        staged.write_bytes(b"our-download")
        return AdapterDownloadResult(
            filepath=staged,
            info=MediaInfo(id="r1", title="Race Title", url=url),
        )

    res_race = run_download(
        DownloadRequest(
            urls=(u1,),
            output_dir=dir_race,
            conflict_policy=ConflictPolicy.SKIP,
            lang="en",
        ),
        FakeMediaAdapter(
            info_by_url={u1: MediaInfo(id="r1", title="Race Title", url=u1)},
            download_handler=_race_download,
        ),
    )
    assert res_race.exit_code == ExitCode.SUCCESS
    assert res_race.succeeded_count == 0
    assert res_race.skipped_count == 1
    assert res_race.skipped_exists_count == 1
    assert res_race.results[0].status is ItemStatus.SKIPPED_EXISTS
    assert (dir_race / "Race Title.mp3").read_bytes() == b"written-by-concurrent-process"

    # All-skipped unsupported (live/upcoming) -> exit 1
    live1 = MediaInfo(id="l1", title="Stream 1", url=u1, is_live=True)
    live2 = MediaInfo(id="l2", title="Stream 2", url=u2, live_status="is_upcoming")
    res_all_unsupported = run_download(
        DownloadRequest(urls=(u1, u2), output_dir=tmp_path / "all_unsupported", lang="en"),
        FakeMediaAdapter(info_by_url={u1: live1, u2: live2}),
    )
    assert res_all_unsupported.exit_code == ExitCode.FAILURE
    assert res_all_unsupported.skipped_unsupported_count == 2
    assert res_all_unsupported.skipped_exists_count == 0

    # Benign all-skipped exists -> exit 0
    exist_dir = tmp_path / "all_exists"
    exist_dir.mkdir()
    (exist_dir / "Existing 1.mp3").write_bytes(b"e1")
    (exist_dir / "Existing 2.mp3").write_bytes(b"e2")
    res_all_exists = run_download(
        DownloadRequest(
            urls=(u1, u2),
            output_dir=exist_dir,
            conflict_policy=ConflictPolicy.SKIP,
            lang="en",
        ),
        FakeMediaAdapter(
            info_by_url={
                u1: MediaInfo(id="e1", title="Existing 1", url=u1),
                u2: MediaInfo(id="e2", title="Existing 2", url=u2),
            }
        ),
    )
    assert res_all_exists.exit_code == ExitCode.SUCCESS
    assert res_all_exists.succeeded_count == 0
    assert res_all_exists.skipped_exists_count == 2


def test_inspect_url_single_and_playlist_without_ffmpeg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("unduhin.pipeline.find_ffmpeg", lambda: None)
    monkeypatch.setattr("unduhin.pipeline.find_deno", lambda: None)

    yt_url = "https://www.youtube.com/watch?v=aqz-KE-bpKQ"
    info = MediaInfo(
        id="aqz-KE-bpKQ",
        title="Big Buck Bunny",
        url=yt_url,
        uploader="Blender",
        duration_seconds=596.0,
        formats=(
            FormatOption(
                format_id="137",
                ext="mp4",
                height=1080,
                vcodec="avc1.640028",
                acodec="none",
                filesize_approx=50_000_000,
            ),
            FormatOption(
                format_id="140",
                ext="m4a",
                height=None,
                vcodec="none",
                acodec="mp4a.40.2",
                abr=128.0,
                filesize_approx=5_000_000,
            ),
        ),
    )
    adapter = FakeMediaAdapter(info_by_url={yt_url: info})
    result = inspect_url(yt_url, adapter, mode=MediaMode.VIDEO, max_height=1080, lang="en")
    assert result.info.title == "Big Buck Bunny"
    assert len(result.items) == 1
    assert any("Deno" in w for w in result.warnings)
    assert "1080p" in result.selected_format_summary or "137" in result.selected_format_summary

    pl_url = "https://example.org/list"
    pl_info = MediaInfo(
        id="pl",
        title="Mix",
        url=pl_url,
        is_playlist=True,
        playlist_count=2,
        entries=(
            MediaInfo(id="1", title="One", url="https://example.org/1"),
            MediaInfo(id="2", title="Two", url="https://example.org/2"),
        ),
    )
    pl_adapter = FakeMediaAdapter(info_by_url={pl_url: pl_info})
    with pytest.raises(PlaylistDisabledError):
        inspect_url(pl_url, pl_adapter, allow_playlist=False, lang="en")

    pl_result = inspect_url(pl_url, pl_adapter, allow_playlist=True, max_items=10, lang="en")
    assert len(pl_result.items) == 2


def test_pipeline_a1_mixed_status_exit_code_precedence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("unduhin.pipeline.find_ffmpeg", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr("unduhin.pipeline.find_deno", lambda: "/usr/bin/deno")

    u_ok = "https://example.org/ok"
    u_exists = "https://example.org/exists"
    u_live = "https://example.org/live"

    out_ok = tmp_path / "ok_mix"
    out_ok.mkdir()
    (out_ok / "Already There.mp3").write_bytes(b"old")

    adapter = FakeMediaAdapter(
        info_by_url={
            u_ok: MediaInfo(id="1", title="Fresh Track", url=u_ok, filesize_approx=50_000),
            u_exists: MediaInfo(id="2", title="Already There", url=u_exists, filesize_approx=50_000),
            u_live: MediaInfo(id="3", title="Live Now", url=u_live, is_live=True),
        }
    )

    benign_summary = run_download(
        DownloadRequest(
            urls=(u_ok, u_exists),
            output_dir=out_ok,
            conflict_policy=ConflictPolicy.SKIP,
            lang="en",
        ),
        adapter,
    )
    assert benign_summary.succeeded_count == 1
    assert benign_summary.skipped_exists_count == 1
    assert benign_summary.skipped_unsupported_count == 0
    assert benign_summary.exit_code == ExitCode.SUCCESS

    out_unsup = tmp_path / "unsupported_mix"
    out_unsup.mkdir()
    (out_unsup / "Already There.mp3").write_bytes(b"old")
    mixed_unsupported = run_download(
        DownloadRequest(
            urls=(u_ok, u_exists, u_live),
            output_dir=out_unsup,
            conflict_policy=ConflictPolicy.SKIP,
            lang="en",
        ),
        adapter,
    )
    assert mixed_unsupported.succeeded_count == 1
    assert mixed_unsupported.skipped_exists_count == 1
    assert mixed_unsupported.skipped_unsupported_count == 1
    assert mixed_unsupported.exit_code == ExitCode.FAILURE

    cancel_state = False

    def _cancel_after_live(*, url: str, **_kwargs: object) -> AdapterDownloadResult:
        nonlocal cancel_state
        cancel_state = True
        raise KeyboardInterrupt

    interrupted = run_download(
        DownloadRequest(urls=(u_live, u_ok), output_dir=tmp_path / "cancel_mix", lang="en"),
        FakeMediaAdapter(
            info_by_url={
                u_live: MediaInfo(id="3", title="Live Now", url=u_live, is_live=True),
                u_ok: MediaInfo(id="1", title="Fresh Track", url=u_ok, filesize_approx=50_000),
            },
            download_handler=_cancel_after_live,
        ),
        hooks=PipelineHooks(is_cancelled=lambda: cancel_state),
    )
    assert interrupted.skipped_unsupported_count == 1
    assert interrupted.exit_code == ExitCode.CANCELLED


def test_pipeline_a2_deferred_naming_from_complete_metadata_and_release_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("unduhin.pipeline.find_ffmpeg", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr("unduhin.pipeline.find_deno", lambda: "/usr/bin/deno")

    u_flat = "https://example.org/flat-entry"
    flat_info = MediaInfo(
        id="v1",
        title="Flat Song",
        url=u_flat,
        uploader=None,
        upload_date=None,
        filesize_approx=50_000,
    )
    complete_info = MediaInfo(
        id="v1",
        title="Flat Song",
        url=u_flat,
        uploader="Complete Artist",
        upload_date="20260501",
        filesize_approx=50_000,
    )

    out1 = tmp_path / "deferred_ok"
    out1.mkdir()
    # Missing-field placeholder path exists on disk, so an eager check would wrongly SKIP it!
    (out1 / "unknown - unknown - Flat Song.mp3").write_bytes(b"placeholder-trap")

    def _complete_dl(*, staging_dir: Path, **_kwargs: object) -> AdapterDownloadResult:
        staged = staging_dir / "v1.mp3"
        staged.write_bytes(b"complete-audio")
        return AdapterDownloadResult(filepath=staged, info=complete_info)

    res1 = run_download(
        DownloadRequest(
            urls=(u_flat,),
            output_dir=out1,
            filename_template="{upload_date} - {uploader} - {title}.{ext}",
            conflict_policy=ConflictPolicy.SKIP,
            lang="en",
        ),
        FakeMediaAdapter(info_by_url={u_flat: flat_info}, download_handler=_complete_dl),
    )
    assert res1.exit_code == ExitCode.SUCCESS
    assert res1.succeeded_count == 1
    expected_final = out1 / "20260501 - Complete Artist - Flat Song.mp3"
    assert res1.results[0].output_path == expected_final
    assert expected_final.read_bytes() == b"complete-audio"

    # Now re-run with exact resolved file already on disk -> deferred SKIPPED_EXISTS!
    res2 = run_download(
        DownloadRequest(
            urls=(u_flat,),
            output_dir=out1,
            filename_template="{upload_date} - {uploader} - {title}.{ext}",
            conflict_policy=ConflictPolicy.SKIP,
            lang="en",
        ),
        FakeMediaAdapter(info_by_url={u_flat: flat_info}, download_handler=_complete_dl),
    )
    assert res2.exit_code == ExitCode.SUCCESS
    assert res2.skipped_exists_count == 1
    assert expected_final.read_bytes() == b"complete-audio"
    assert not any(p.name.startswith(".unduhin-tmp-") for p in out1.iterdir())

    # Reservation release on failure: item 1 fails -> item 2 gets un-suffixed name.
    u_fail = "https://example.org/rel-fail"
    u_ok = "https://example.org/rel-ok"
    out3 = tmp_path / "release_fail"

    def _fail_first(*, url: str, staging_dir: Path, **_kwargs: object) -> AdapterDownloadResult:
        if url == u_fail:
            raise RuntimeError("Private video")
        staged = staging_dir / "rel.mp3"
        staged.write_bytes(b"second-ok")
        return AdapterDownloadResult(
            filepath=staged,
            info=MediaInfo(id="rel2", title="Release Name", url=url),
        )

    res3 = run_download(
        DownloadRequest(urls=(u_fail, u_ok), output_dir=out3, lang="en"),
        FakeMediaAdapter(
            info_by_url={
                u_fail: MediaInfo(id="rel1", title="Release Name", url=u_fail, filesize_approx=10_000),
                u_ok: MediaInfo(id="rel2", title="Release Name", url=u_ok, filesize_approx=10_000),
            },
            download_handler=_fail_first,
        ),
    )
    assert res3.failed_count == 1
    assert res3.succeeded_count == 1
    assert res3.results[1].output_path == out3 / "Release Name.mp3"


def test_pipeline_a3_unknown_error_never_retried(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("unduhin.pipeline.find_ffmpeg", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr("unduhin.pipeline.find_deno", lambda: "/usr/bin/deno")

    url = "https://example.org/unknown-err"
    sleep_calls: list[float] = []

    def _unknown_fail(**_kwargs: object) -> AdapterDownloadResult:
        raise RuntimeError("Mysterious unmapped backend glitch #999")

    adapter = FakeMediaAdapter(
        info_by_url={url: MediaInfo(id="u1", title="Test", url=url, filesize_approx=10_000)},
        download_handler=_unknown_fail,
    )
    summary = run_download(
        DownloadRequest(urls=(url,), output_dir=tmp_path / "out", max_retries=3, lang="en"),
        adapter,
        hooks=PipelineHooks(sleep_fn=sleep_calls.append),
    )
    assert len(adapter.download_calls) == 1
    assert sleep_calls == []
    assert summary.exit_code == ExitCode.FAILURE
    assert summary.results[0].message is not None
    assert "--verbose" in summary.results[0].message


def test_pipeline_a4_per_item_sleep_cap_120s(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("unduhin.pipeline.find_ffmpeg", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr("unduhin.pipeline.find_deno", lambda: "/usr/bin/deno")
    monkeypatch.setattr("unduhin.pipeline.random.uniform", lambda _a, _b: 0.0)

    url = "https://example.org/slow-backoff"
    sleep_calls: list[float] = []

    def _always_timeout(**_kwargs: object) -> AdapterDownloadResult:
        raise TimeoutError("timed out")

    adapter = FakeMediaAdapter(
        info_by_url={url: MediaInfo(id="t1", title="Timeout Track", url=url, filesize_approx=10_000)},
        download_handler=_always_timeout,
    )
    summary = run_download(
        DownloadRequest(
            urls=(url,),
            output_dir=tmp_path / "cap120",
            max_retries=6,
            backoff_base_seconds=45.0,
            lang="en",
        ),
        adapter,
        hooks=PipelineHooks(sleep_fn=sleep_calls.append),
    )
    # 45s fits (45 <= 120), next attempt wants 90s (45 + 90 = 135 > 120), so retries stop early.
    assert sum(sleep_calls) <= MAX_TOTAL_SLEEP_PER_ITEM_SECONDS
    assert len(adapter.download_calls) == 2
    assert summary.failed_count == 1
    assert summary.exit_code == ExitCode.FAILURE


def test_pipeline_a4_rate_limit_circuit_breaker_and_reset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("unduhin.pipeline.find_ffmpeg", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr("unduhin.pipeline.find_deno", lambda: "/usr/bin/deno")

    pl_url = "https://example.org/rl-playlist"
    entries = tuple(
        MediaInfo(
            id=f"item{i}",
            title=f"Item {i}",
            url=f"https://example.org/v{i}",
            filesize_approx=10_000,
        )
        for i in range(1, 6)
    )
    pl_info = MediaInfo(
        id="rl_pl",
        title="RL List",
        url=pl_url,
        is_playlist=True,
        playlist_count=5,
        entries=entries,
    )
    extra_url = "https://example.org/after-breaker"

    def _always_429(**_kwargs: object) -> AdapterDownloadResult:
        raise RuntimeError("HTTP Error 429: Too Many Requests")

    adapter = FakeMediaAdapter(
        info_by_url={
            pl_url: pl_info,
            extra_url: MediaInfo(id="ex", title="Extra", url=extra_url, filesize_approx=10_000),
        },
        download_handler=_always_429,
    )
    summary = run_download(
        DownloadRequest(
            urls=(pl_url, extra_url),
            output_dir=tmp_path / "breaker",
            allow_playlist=True,
            max_retries=0,
            lang="en",
        ),
        adapter,
    )
    assert summary.exit_code == ExitCode.FAILURE
    assert summary.failed_count == 6
    assert len(adapter.download_calls) == CONSECUTIVE_RATE_LIMIT_BREAKER
    assert any("rate-limit" in w.lower() for w in summary.warnings)
    assert summary.results[-1].url == extra_url

    # Non-429 outcome resets consecutive breaker count.
    urls_reset = tuple(f"https://example.org/r{i}" for i in range(1, 7))
    infos_reset = {
        u: MediaInfo(id=f"r{i}", title=f"Reset {i}", url=u, filesize_approx=10_000)
        for i, u in enumerate(urls_reset, 1)
    }

    def _two_429_then_ok(*, url: str, staging_dir: Path, **_kwargs: object) -> AdapterDownloadResult:
        if url.endswith(("r1", "r2", "r4", "r5")):
            raise RuntimeError("HTTP Error 429: Too Many Requests")
        staged = staging_dir / "ok.mp3"
        staged.write_bytes(b"ok")
        return AdapterDownloadResult(filepath=staged, info=infos_reset[url])

    reset_adapter = FakeMediaAdapter(info_by_url=infos_reset, download_handler=_two_429_then_ok)
    reset_summary = run_download(
        DownloadRequest(
            urls=urls_reset,
            output_dir=tmp_path / "reset",
            max_retries=0,
            lang="en",
        ),
        reset_adapter,
    )
    assert reset_summary.succeeded_count == 2
    assert reset_summary.failed_count == 4
    assert len(reset_adapter.download_calls) == 6


def test_pipeline_a5_global_total_items_cap_500(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("unduhin.pipeline.find_deno", lambda: None)

    default_req = DownloadRequest(urls=("https://example.org/one",))
    assert default_req.max_total_items == MAX_TOTAL_ITEMS_GLOBAL == 500

    pl_urls = tuple(f"https://example.org/pl{p}" for p in range(1, 5))
    infos: dict[str, MediaInfo] = {}
    for p_idx, pl_url in enumerate(pl_urls, 1):
        entries = tuple(
            MediaInfo(
                id=f"p{p_idx}i{i}",
                title=f"Track P{p_idx} #{i}",
                url=f"https://example.org/p{p_idx}/v{i}",
            )
            for i in range(1, 201)
        )
        infos[pl_url] = MediaInfo(
            id=f"pl{p_idx}",
            title=f"Playlist {p_idx}",
            url=pl_url,
            is_playlist=True,
            playlist_count=200,
            entries=entries,
        )

    adapter = FakeMediaAdapter(info_by_url=infos)
    summary = run_download(
        DownloadRequest(
            urls=pl_urls,
            output_dir=tmp_path / "cap500",
            allow_playlist=True,
            max_items=MAX_PLAYLIST_ITEMS_CAP,
            dry_run=True,
            lang="en",
        ),
        adapter,
    )
    assert summary.planned_count == 500
    assert any("500" in w for w in summary.warnings)
    # Playlists 1..3 produce 200+200+100 = 500; playlist 4 is never extracted!
    assert len(adapter.extract_calls) == 3


def test_pipeline_a8_single_unknown_filesize_warning_and_duplicate_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("unduhin.pipeline.find_ffmpeg", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr("unduhin.pipeline.find_deno", lambda: "/usr/bin/deno")
    monkeypatch.setattr(
        "unduhin.validators.shutil.disk_usage",
        lambda _p: _FakeDiskUsage(
            total=10_000_000_000,
            used=1_000_000_000,
            free=CONSERVATIVE_UNKNOWN_SIZE_BYTES + 10_000,
        ),
    )

    out_dir = tmp_path / "a8_out"
    out_dir.mkdir()
    (out_dir / "OnDisk.mp3").write_bytes(b"preexisting")

    u1 = "https://example.org/t1"
    u2 = "https://example.org/t2"
    u3 = "https://example.org/disk"
    adapter = FakeMediaAdapter(
        info_by_url={
            u1: MediaInfo(id="1", title="Twin", url=u1, filesize_approx=None),
            u2: MediaInfo(id="2", title="Twin", url=u2, filesize_approx=None),
            u3: MediaInfo(id="3", title="OnDisk", url=u3, filesize_approx=None),
        }
    )
    summary = run_download(
        DownloadRequest(urls=(u1, u2, u3), output_dir=out_dir, lang="en"),
        adapter,
    )
    assert summary.exit_code == ExitCode.SUCCESS
    assert summary.succeeded_count == 3
    size_warnings = [w for w in summary.warnings if "200 MB" in w]
    assert len(size_warnings) == 1
    assert "3 item" in size_warnings[0]

    assert summary.results[0].is_batch_renamed is False
    assert summary.results[1].is_batch_renamed is True
    assert summary.results[2].is_batch_renamed is False
