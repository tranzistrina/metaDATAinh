"""Integration smoke test with the real ExifTool binary, run in GitHub Actions."""
import shutil
import struct
import zlib
from pathlib import Path

import pytest

import app as editor


def _chunk(tag: bytes, contents: bytes) -> bytes:
    payload = tag + contents
    return struct.pack(">I", len(contents)) + payload + struct.pack(">I", zlib.crc32(payload))


def _png() -> bytes:
    # Valid 1x1 RGB PNG with one black pixel, created without third-party packages.
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + _chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00"))
        + _chunk(b"IEND", b"")
    )


@pytest.mark.skipif(shutil.which("exiftool") is None, reason="ExifTool not installed")
def test_actual_png_xmp_round_trip(tmp_path: Path):
    image = tmp_path / "image.png"
    image.write_bytes(_png())
    editor.writable_tag_names.cache_clear()

    initial = editor.read_metadata(image)
    assert any(tag["name"] == "FileType" for tag in initial)

    editor._write_changes(image, {"XMP-dc:Title": "Sample metadata"}, [])
    new_metadata = editor.read_metadata(image)
    matches = [tag for tag in new_metadata if tag["name"] == "Title"]
    assert matches
    assert any("Sample metadata" in str(tag["value"]) for tag in matches)
