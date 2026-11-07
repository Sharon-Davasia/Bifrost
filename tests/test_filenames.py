"""Filename sanitisation tests (Windows-safe, traversal-proof, Unicode-safe)."""

from __future__ import annotations

import pytest

from file_manager import guess_mime, safe_extension, sanitize_filename


@pytest.mark.parametrize(
    "raw, expected",
    [
        # Path traversal attempts are reduced to a bare basename.
        ("../../evil.txt", "evil.txt"),
        ("..\\..\\evil.txt", "evil.txt"),
        ("/etc/passwd", "passwd"),
        ("C:\\Windows\\System32\\evil.dll", "evil.dll"),
        ("..%2fnot-decoded.txt", "..%2fnot-decoded.txt"),
        # Windows reserved device names, with and without extensions.
        ("CON", "_CON"),
        ("NUL.txt", "_NUL.txt"),
        ("COM1.jpg", "_COM1.jpg"),
        ("LPT9.tar.gz", "_LPT9.tar.gz"),
        ("con", "_con"),
        # Ordinary names survive untouched.
        ("photo.jpg", "photo.jpg"),
        ("photo (1).jpg", "photo (1).jpg"),
        ("report 2026 final.pdf", "report 2026 final.pdf"),
        # Invalid Windows characters are replaced.
        ('a<b>c:d"e|f?g*h.txt', "a_b_c_d_e_f_g_h.txt"),
        # Trailing dots and spaces (forbidden on Windows) are removed.
        ("trailing.  ", "trailing"),
        ("spaces   ", "spaces"),
        # Control characters are dropped.
        ("bad\x00name\x1f.txt", "bad name .txt"),
        # Unicode and emoji are preserved.
        ("नमस्ते.txt", "नमस्ते.txt"),
        ("🎉 party 🎉.png", "🎉 party 🎉.png"),
        ("clip 👨‍👩‍👧.jpg", "clip 👨‍👩‍👧.jpg"),
        # Empty / unusable names fall back.
        ("", "file"),
        ("   ", "file"),
        ("..", "file"),
        ("/", "file"),
        (None, "file"),
        # Nested paths keep only the final segment.
        ("folder/subfolder/photo.png", "photo.png"),
    ],
)
def test_sanitize_filename(raw, expected):
    assert sanitize_filename(raw, max_chars=150) == expected


def test_sanitize_replaces_backslashes_before_basename():
    assert sanitize_filename("one\\two\\three.txt") == "three.txt"


def test_sanitize_limits_length_but_keeps_extension():
    raw = "a" * 300 + ".jpg"
    result = sanitize_filename(raw, max_chars=150)
    assert len(result) <= 150
    assert result.endswith(".jpg")


def test_sanitize_limits_length_for_unicode_names():
    raw = "फ़ाइल" * 80 + ".txt"
    result = sanitize_filename(raw, max_chars=150)
    assert len(result) <= 150
    assert result.endswith(".txt")


def test_sanitize_never_returns_a_path():
    for raw in ("../../etc/passwd", "..\\..\\windows\\win.ini", "a/../../b"):
        result = sanitize_filename(raw)
        assert "/" not in result
        assert "\\" not in result
        assert result not in {".", ".."}


@pytest.mark.parametrize(
    "name, extension",
    [
        ("photo.JPG", ".jpg"),
        ("archive.tar.gz", ".gz"),
        ("no-extension", ""),
        ("weird.name.with.many.dots.pdf", ".pdf"),
        ("dangerous.sh", ".sh"),
        ("file.", ""),
        ("file.verylongextensionthatisnotreal", ""),
        ("emoji🎉.png", ".png"),
        ("CON.txt", ".txt"),
    ],
)
def test_safe_extension(name, extension):
    assert safe_extension(name) == extension


def test_safe_extension_rejects_path_characters():
    assert safe_extension("evil.p/hp") == ""


def test_guess_mime_prefers_extension():
    assert guess_mime("photo.jpg", "text/plain") == "image/jpeg"
    assert guess_mime("notes.txt") == "text/plain"
    assert guess_mime("किसी-फ़ाइल.unknownext") == "application/octet-stream"
    assert guess_mime("unknown.zzz", "image/png") == "image/png"
    assert guess_mime("unknown.zzz", "not a mime") == "application/octet-stream"
