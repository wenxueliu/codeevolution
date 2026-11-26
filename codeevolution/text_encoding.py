"""Decode repository text that is not guaranteed to be UTF-8.

Windows checkouts mix encodings: Markdown written by Notepad can be GB18030 or
UTF-16 with a BOM, and ``git log`` echoes the bytes a commit actually carries,
so a Chinese commit authored in a cp936 console arrives as GBK.  Decoding those
bytes as strict UTF-8 either raises or replaces every Chinese character with
U+FFFD, which reaches the ADR workbench as mojibake.

The policy is deliberately simple and zh-CN first:

1. honour a byte order mark (UTF-32/UTF-8/UTF-16);
2. strict UTF-8;
3. strict GB18030, which covers GBK/GB2312 legacy Chinese content;
4. UTF-8 with replacement only when nothing else decodes cleanly.
"""

from __future__ import annotations

import codecs

_UTF32_BOMS = (codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)
_UTF16_BOMS = (codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)


def decode_text_bytes(data: bytes) -> tuple[str, str]:
    """Best-effort decode of one file or blob.

    Returns ``(text, encoding)`` where ``encoding`` names the decoder that won,
    so callers can log or surface why a document was not read as UTF-8.
    """
    data = data or b""
    if data.startswith(codecs.BOM_UTF8):
        return data.decode("utf-8-sig"), "utf-8-sig"
    if data.startswith(_UTF32_BOMS):
        return data.decode("utf-32"), "utf-32"
    if data.startswith(_UTF16_BOMS):
        return data.decode("utf-16"), "utf-16"
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    try:
        return data.decode("gb18030"), "gb18030"
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="replace"), "utf-8(replace)"


def decode_text_lines(data: bytes) -> str:
    """Decode newline-separated output (``git log``/``git show``) line by line.

    A single non-UTF-8 byte in one hunk must not re-decode the whole diff as
    GBK, so each line picks its own decoder and the line structure is kept.
    """
    return "\n".join(decode_text_bytes(line)[0] for line in (data or b"").split(b"\n"))


def read_text_file(path) -> str:
    """Read a repository file with the same tolerance as :func:`decode_text_bytes`."""
    return decode_text_bytes(path.read_bytes())[0]


__all__ = ["decode_text_bytes", "decode_text_lines", "read_text_file"]
