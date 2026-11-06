"""Adversarial tests for RSC parsing: unescaping and brace matching (#16).

These cover the failure modes that a hand-rolled JS-escape decoder and a
bracket matcher are prone to: unicode, surrogate pairs, malformed escapes,
truncation, nested braces, and oversized input.
"""

from __future__ import annotations

import pytest

from suno_dl.parser import (
    _brace_match,
    _extract_next_f_blob,
    _unescape_js,
)

# --- _unescape_js ------------------------------------------------------------


def test_unescape_plain_ascii() -> None:
    assert _unescape_js("hello world") == "hello world"


def test_unescape_common_escapes() -> None:
    assert _unescape_js(r"a\nb\tc") == "a\nb\tc"
    assert _unescape_js(r"quote\"end") == 'quote"end'
    assert _unescape_js(r"back\\slash") == "back\\slash"
    assert _unescape_js(r"slash\/") == "slash/"


def test_unescape_unicode_escape() -> None:
    assert _unescape_js(r"\u00e9") == "é"
    assert _unescape_js(r"\u0026") == "&"


def test_unescape_cjk_preserved() -> None:
    """Escaped CJK must decode correctly (regression guard)."""
    # '日' is \u65e5, '本' is \u672c
    assert _unescape_js(r"\u65e5\u672c") == "日本"


def test_unescape_emoji_surrogate_pair() -> None:
    # U+1F3B5 MUSICAL NOTE = surrogate pair \uD83C\uDFB5
    assert _unescape_js(r"\uD83C\uDFB5") == "🎵"


def test_unescape_lone_high_surrogate_replaced() -> None:
    assert _unescape_js(r"\uD83C") == "\ufffd"


def test_unescape_hex_escape() -> None:
    assert _unescape_js(r"\x41") == "A"


def test_unescape_malformed_escape_preserved() -> None:
    # A trailing backslash and invalid hex must not raise.
    assert _unescape_js("trailing\\") == "trailing\\"
    assert _unescape_js(r"\uZZZZ") == r"\uZZZZ"
    assert _unescape_js(r"\xZZ") == r"\xZZ"


def test_unescape_no_backslash_fast_path() -> None:
    assert _unescape_js("no escapes here") == "no escapes here"


def test_unescape_does_not_mojibake_raw_utf8() -> None:
    """Raw UTF-8 already in the string must survive untouched."""
    assert _unescape_js("日本語のうた") == "日本語のうた"


# --- _brace_match ------------------------------------------------------------


def test_brace_match_simple() -> None:
    s = '{"a": 1}'
    assert _brace_match(s, 0) == s


def test_brace_match_nested() -> None:
    s = '{"a": {"b": {"c": 1}}}'
    assert _brace_match(s, 0) == s


def test_brace_match_ignores_braces_in_strings() -> None:
    s = '{"a": "}{", "b": 1}'
    assert _brace_match(s, 0) == s


def test_brace_match_handles_escaped_quote() -> None:
    s = '{"a": "he said \\"}\\""}'
    assert _brace_match(s, 0) == s


def test_brace_match_returns_none_when_unbalanced() -> None:
    assert _brace_match('{"a": 1', 0) is None


def test_brace_match_requires_leading_brace() -> None:
    assert _brace_match("not a brace", 0) is None
    assert _brace_match("", 0) is None


def test_brace_match_from_offset() -> None:
    s = 'xx{"a":1}yy'
    assert _brace_match(s, 2) == '{"a":1}'


def test_brace_match_truncated_deep() -> None:
    s = '{"a":{"b":{"c":'
    assert _brace_match(s, 0) is None


# --- _extract_next_f_blob ----------------------------------------------------


def test_extract_blob_from_html() -> None:
    html = (
        "<html><body><script>"
        'self.__next_f=self.__next_f||[];self.__next_f.push([1,"{\\"a\\":1}"])'
        "</script></body></html>"
    )
    blob = _extract_next_f_blob(html)
    assert '"a"' in blob


def test_extract_blob_empty_on_no_chunks() -> None:
    assert _extract_next_f_blob("<html>nothing</html>") == ""


def test_extract_blob_rejects_oversized(monkeypatch) -> None:
    import suno_dl.parser as parser_mod

    monkeypatch.setattr(parser_mod, "_MAX_BLOB_BYTES", 10)
    big = "x" * 50
    html = f'<script>self.__next_f.push([1,"{big}"])</script>'
    assert _extract_next_f_blob(html) == ""


@pytest.mark.parametrize("s", ["", "{", "}", '"', "\\", '{"a":', "}{", "\\u", "\\x"])
def test_brace_match_never_raises(s: str) -> None:
    for start in range(len(s) + 1):
        _brace_match(s, start)


@pytest.mark.parametrize(
    "s",
    ["", "\\", "\\\\", "\\u", "\\u12", "\\x", "\\uD83C", "\\\\u0026", "a\\"],
)
def test_unescape_never_raises(s: str) -> None:
    _unescape_js(s)
