"""Offline tests for ``src.origin`` (strict Origin parsing, exact matching)."""

from __future__ import annotations

import pytest

from src.origin import OriginSyntaxError, normalize_origin, origin_is_allowed


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("http://localhost:3000", "http://localhost:3000"),
        ("https://attendance.test.invalid", "https://attendance.test.invalid"),
        ("HTTPS://Attendance.Test.Invalid", "https://attendance.test.invalid"),
        ("https://example.com:443", "https://example.com"),
        ("http://example.com:80", "http://example.com"),
        ("http://example.com", "http://example.com"),
        ("http://example.com:8080", "http://example.com:8080"),
        ("http://127.0.0.1:8000", "http://127.0.0.1:8000"),
        ("http://[::1]:3000", "http://[::1]:3000"),
        ("http://[::1]", "http://[::1]"),
        ("http://[::1]:80", "http://[::1]"),
        ("https://example.com/", "https://example.com"),
    ],
)
def test_valid_origins_normalize_to_a_canonical_form(raw: str, expected: str) -> None:
    assert normalize_origin(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        " ",
        "  https://example.com",
        "https://example.com ",
        "null",
        "NULL",
        "example.com",
        "//example.com",
        "ftp://example.com",
        "ws://example.com",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "https://user:pass@example.com",
        "https://user@example.com",
        "https://example.com/login",
        "https://example.com/.",
        "https://example.com/?q=1",
        "https://example.com/#frag",
        # A trailing delimiter parses as an empty query/fragment, so it must be
        # refused before parsing rather than accepted as a bare origin.
        "https://example.com?",
        "https://example.com#",
        "https://example.com?#",
        "https://example.com/?",
        "https://example.com/#",
        "https://example.com?#x",
        # IPv6 zone identifiers are scoped addresses, not origins.
        "http://[fe80::1%25eth0]",
        "http://[fe80::1%eth0]",
        "http://[fe80::1%25eth0]:8080",
        "https://example.com:0",
        "https://example.com:65536",
        "https://example.com:",
        "https://example.com:abc",
        "https://example.com:-1",
        "https://example.com:8 0",
        "http://::1",
        "http://[::1",
        "http://exa mple.com",
        "http://ex_ample.com",
        "http://example.com.",
        "http://example..com",
        "http://-example.com",
        "https://example.com,https://evil.test",
        "https://例子.测试",
    ],
)
def test_invalid_origins_are_rejected(raw: str) -> None:
    with pytest.raises(OriginSyntaxError):
        normalize_origin(raw)


@pytest.mark.parametrize("raw", [None, 3000, b"https://example.com", ["x"]])
def test_non_string_values_are_rejected(raw: object) -> None:
    with pytest.raises(OriginSyntaxError):
        normalize_origin(raw)


def test_allowlist_matching_is_exact() -> None:
    trusted = ("https://attendance.test.invalid", "http://localhost:3000")
    assert origin_is_allowed(normalize_origin("https://attendance.test.invalid"), trusted)
    assert origin_is_allowed(normalize_origin("https://ATTENDANCE.test.invalid/"), trusted)
    assert not origin_is_allowed(normalize_origin("https://attendance.test.invalid.evil"), trusted)
    assert not origin_is_allowed(normalize_origin("https://evil.attendance.test.invalid"), trusted)
    assert not origin_is_allowed(normalize_origin("http://attendance.test.invalid"), trusted)
    assert not origin_is_allowed(normalize_origin("https://attendance.test.invalid:444"), trusted)


def test_an_empty_allowlist_matches_nothing() -> None:
    assert not origin_is_allowed(normalize_origin("http://localhost:3000"), ())


def test_matching_never_uses_wildcards_or_suffixes() -> None:
    trusted = ("https://attendance.test.invalid",)
    for value in (
        "https://attendance.test.invalid.evil.test",
        "https://evil.attendance.test.invalid",
        "https://attendance.test.invalid:444",
        "http://attendance.test.invalid",
        "https://attendance.test.invalid.evil.test/",
    ):
        assert not origin_is_allowed(normalize_origin(value), trusted)


def test_allowlist_entries_are_canonicalized_before_comparison() -> None:
    trusted = ("HTTPS://Attendance.Test.Invalid:443/",)
    assert origin_is_allowed(normalize_origin("https://attendance.test.invalid"), trusted)
    assert origin_is_allowed(
        normalize_origin("https://ATTENDANCE.test.invalid"),
        ("https://attendance.test.invalid",),
    )
