"""Tests for EN/ID localization and OS/Windows locale detection."""

from __future__ import annotations

import io

import pytest

from unduhin.i18n import (
    MESSAGES,
    detect_language,
    normalize_locale,
    safe_console_text,
    t,
)


def test_en_and_id_catalogs_have_identical_keys() -> None:
    en_keys = set(MESSAGES["en"].keys())
    id_keys = set(MESSAGES["id"].keys())
    assert en_keys == id_keys


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("id", "id"),
        ("id_ID", "id"),
        ("id-ID", "id"),
        ("id_ID.UTF-8", "id"),
        ("in_ID", "id"),
        ("Indonesian_Indonesia.1252", "id"),
        ("Indonesian_Indonesia", "id"),
        ("Bahasa Indonesia", "id"),
        ("en", "en"),
        ("en_US.UTF-8", "en"),
        ("en-GB", "en"),
        ("English_United States.1252", "en"),
        ("C", None),
        ("POSIX", None),
        ("fr_FR.UTF-8", None),
        ("", None),
        (None, None),
    ],
)
def test_normalize_locale(raw: str | None, expected: str | None) -> None:
    assert normalize_locale(raw) == expected


def test_detect_language_explicit_override_takes_priority(
    clean_locale_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ = clean_locale_env
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    assert detect_language("id") == "id"
    assert detect_language("Indonesian_Indonesia.1252") == "id"


def test_detect_language_from_env_and_fallback(
    clean_locale_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ = clean_locale_env
    monkeypatch.setenv("LANG", "id_ID.UTF-8")
    assert detect_language() == "id"

    monkeypatch.setenv("LANG", "ja_JP.UTF-8")
    monkeypatch.setattr("unduhin.i18n.locale.getlocale", lambda: ("ja_JP", "UTF-8"))
    monkeypatch.setattr("unduhin.i18n._detect_windows_ui_locale", lambda: None)
    assert detect_language() == "en"


def test_detect_language_windows_locale_fallback(
    clean_locale_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ = clean_locale_env
    monkeypatch.setattr("unduhin.i18n.locale.getlocale", lambda: ("Indonesian_Indonesia", "1252"))
    assert detect_language() == "id"

    monkeypatch.setattr("unduhin.i18n.locale.getlocale", lambda: (None, None))
    monkeypatch.setattr("unduhin.i18n._detect_windows_ui_locale", lambda: "id")
    assert detect_language() == "id"


def test_translate_formats_safely_and_never_raises_on_missing_kwarg() -> None:
    msg_id = t("doctor.python_ok", lang="id", version="3.13.1")
    assert "3.13.1" in msg_id

    # Missing formatting parameter falls back cleanly without KeyError
    raw_msg = t("doctor.python_ok", lang="en")
    assert "{version}" in raw_msg


def test_unuduhin_lang_env_and_explicit_override(
    clean_locale_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ = clean_locale_env
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    monkeypatch.setenv("UNDUHIN_LANG", "id")
    assert detect_language() == "id"
    assert detect_language("en") == "en"


def test_safe_console_text_handles_cp1252_strict_stream() -> None:
    sample = "日本語 🎵 test"
    cleaned = safe_console_text(sample, encoding="cp1252")
    assert "test" in cleaned
    buf = io.BytesIO()
    wrapper = io.TextIOWrapper(buf, encoding="cp1252", errors="strict")
    wrapper.write(cleaned)
    wrapper.flush()
    assert b"test" in buf.getvalue()
