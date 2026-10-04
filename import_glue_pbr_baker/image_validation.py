"""Real raster container validation for the texture precheck.

``precheck.file_looks_valid`` only checks a 64 byte size floor plus a trailing
``IEND``/``EOI`` needle, so a header-only or mid-stream truncated texture sails
through the precheck and then fails much later inside the bake, after the user
has already paid for the setup.  This module parses the containers for real --
the PNG chunk stream, the JPEG marker stream, and the TGA/BMP/TIFF headers --
and asks Blender to decode anything it cannot decide on its own.

Only the fallback needs ``bpy``.  The parsers are deliberately pure stdlib so
this module imports (and can be unit tested) outside Blender as well as inside
it, which is also why nothing here uses a relative import.

Every read is bounded: headers come out of one small sniff buffer, chunk and
marker walking seeks rather than reads, and both walks are capped, so a hostile
or garbage file can never make this loop or allocate without limit.
"""
from __future__ import annotations

import os
import struct
import zlib
from dataclasses import dataclass
from typing import Any, BinaryIO, Callable, Dict, Optional, Tuple

try:  # only the fallback needs Blender; the parsers must import without it
    import bpy as _bpy
except Exception:  # pragma: no cover - the pure-stdlib unit run takes this
    _bpy = None


# --- bounds -----------------------------------------------------------------
# One sniff read has to cover the largest header we parse (PNG signature plus a
# complete IHDR chunk is 33 bytes; BMP needs 26, TGA 18, TIFF 8).
_SNIFF_BYTES = 512
_TAIL_BYTES = 4096          # window searched for a JPEG EOI
_MAX_PNG_CHUNKS = 8192
_MAX_JPEG_SEGMENTS = 1024
_MAX_JPEG_FILL = 64         # 0xFF padding tolerated in front of one marker
_MAX_TIFF_ENTRIES = 512
_MAX_DIMENSION = 1 << 20    # generous, but rejects garbage read as a size

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# colour type -> the bit depths PNG actually permits for it
_PNG_DEPTHS: Dict[int, Tuple[int, ...]] = {
    0: (1, 2, 4, 8, 16),
    2: (8, 16),
    3: (1, 2, 4, 8),
    4: (8, 16),
    6: (8, 16),
}

_JPEG_SOF = frozenset(
    (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF)
)
# TEM and the restart markers carry no length field
_JPEG_STANDALONE = frozenset((0x01,)) | frozenset(range(0xD0, 0xD8))

_TGA_IMAGE_TYPES = frozenset((0, 1, 2, 3, 9, 10, 11))
_TGA_DEPTHS = frozenset((8, 15, 16, 24, 32))
_TGA_UNCOMPRESSED = frozenset((1, 2, 3))
_BMP_DIB_SIZES = frozenset((12, 40, 52, 56, 64, 108, 124))

# Extensions we can name a format for, used only for the non-fatal mismatch
# note -- dispatch is by content, exactly like Blender's own loader.
_EXTENSION_FORMATS: Dict[str, str] = {
    ".png": "PNG",
    ".jpg": "JPEG", ".jpeg": "JPEG", ".jpe": "JPEG",
    ".tga": "TGA", ".targa": "TGA", ".icb": "TGA", ".vda": "TGA", ".vst": "TGA",
    ".bmp": "BMP", ".dib": "BMP",
    ".tif": "TIFF", ".tiff": "TIFF",
}


@dataclass
class RasterInfo:
    """What the container claims about itself once it has been parsed."""

    format: str
    width: int
    height: int


# (is_valid, reason, info).  ``info`` is None whenever is_valid is False, so a
# caller never has to guess whether the dimensions can be trusted; failure
# detail, dimensions included, goes into ``reason``.  A non-empty reason with
# is_valid True is a non-fatal note (currently only an extension mismatch).
Result = Tuple[bool, str, Optional[RasterInfo]]
_Parser = Callable[[BinaryIO, int, bytes], Optional[Result]]


def inspect_raster(path: str) -> Result:
    """Validate one image file and report its container format and size.

    Returns ``(is_valid, reason, info)``.  The header parsers decide on their
    own for PNG/JPEG/TGA/BMP/TIFF; anything else, or a header that parses but
    stays inconclusive, is handed to Blender to actually decode.
    """
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        return False, "unreadable: %s" % exc, None
    if size == 0:
        return False, "file is empty", None

    head = b""
    result: Optional[Result] = None
    try:
        with open(path, "rb") as handle:
            head = handle.read(_SNIFF_BYTES)
            parser = _dispatch(head, path)
            if parser is not None:
                result = parser(handle, size, head)
    except (OSError, struct.error, ValueError) as exc:
        return False, "unreadable: %s" % exc, None

    if result is None:
        # Unknown container, or a header we could not conclude on: let Blender
        # be the judge rather than guessing.  Done outside the ``with`` so the
        # file is closed before Blender opens it itself.
        result = _inspect_with_blender(path, size, head)
    return _note_extension_mismatch(path, result)


def looks_valid(path: str) -> Tuple[bool, str]:
    """Drop-in shape for ``precheck.file_looks_valid``: ``(ok, reason)``."""
    ok, reason, _info = inspect_raster(path)
    return ok, ("" if ok else reason)


def _dispatch(head: bytes, path: str) -> Optional[_Parser]:
    """Pick a parser by magic bytes, falling back to the extension for TGA."""
    if head.startswith(_PNG_SIGNATURE):
        return _parse_png
    if head[:2] == b"\xff\xd8":
        return _parse_jpeg
    if head[:2] == b"BM":
        return _parse_bmp
    if head[:4] in (b"II\x2a\x00", b"MM\x00\x2a"):
        return _parse_tiff
    if _EXTENSION_FORMATS.get(os.path.splitext(path)[1].lower()) == "TGA":
        return _parse_tga  # TGA has no magic number; only the extension names it
    return None


def _note_extension_mismatch(path: str, result: Result) -> Result:
    """Flag a mislabelled file without failing it -- Blender loads by content."""
    ok, reason, info = result
    if not ok or info is None or reason:
        return result
    declared = _EXTENSION_FORMATS.get(os.path.splitext(path)[1].lower())
    if declared is not None and declared != info.format:
        return True, "extension claims %s but the content is %s" % (declared, info.format), info
    return result


# --- PNG --------------------------------------------------------------------

def _parse_png(handle: BinaryIO, size: int, head: bytes) -> Optional[Result]:
    if len(head) < 33:  # 8 signature + 8 chunk header + 13 IHDR body + 4 CRC
        return False, "PNG shorter than a complete IHDR chunk (%d bytes)" % size, None
    length, kind = struct.unpack(">I4s", head[8:16])
    if kind != b"IHDR" or length != 13:
        return False, "PNG first chunk is %r of %d bytes, not a 13 byte IHDR" % (
            kind, length), None
    if zlib.crc32(head[12:29]) & 0xFFFFFFFF != struct.unpack(">I", head[29:33])[0]:
        return False, "PNG IHDR fails its own CRC (header corrupt)", None

    width, height, depth, colour, compression, filtering, interlace = struct.unpack(
        ">IIBBBBB", head[16:29]
    )
    if width == 0 or height == 0:
        return False, "PNG declares zero dimensions %dx%d" % (width, height), None
    if width > _MAX_DIMENSION or height > _MAX_DIMENSION:
        return False, "PNG declares implausible dimensions %dx%d" % (width, height), None
    if depth not in _PNG_DEPTHS.get(colour, ()):
        return False, "PNG colour type %d with bit depth %d is not a legal combination" % (
            colour, depth), None
    if compression != 0 or filtering != 0 or interlace > 1:
        return False, "PNG declares unknown compression/filter/interlace %d/%d/%d" % (
            compression, filtering, interlace), None

    ok, reason = _walk_png_chunks(handle, size, width, height)
    if not ok:
        return False, reason, None
    return True, "", RasterInfo("PNG", int(width), int(height))


def _walk_png_chunks(handle: BinaryIO, size: int, width: int, height: int) -> Tuple[bool, str]:
    """Seek through the chunk stream demanding IDAT and a terminating IEND."""
    handle.seek(len(_PNG_SIGNATURE))
    seen_idat = False
    for _ in range(_MAX_PNG_CHUNKS):
        offset = handle.tell()
        header = handle.read(8)
        if not header:
            return False, "PNG %dx%d ends after %d bytes without an IEND chunk" % (
                width, height, size)
        if len(header) < 8:
            return False, "PNG %dx%d chunk header truncated at byte %d" % (
                width, height, offset)
        length, kind = struct.unpack(">I4s", header)
        if not kind.isalpha():
            return False, "PNG chunk stream desynchronised at byte %d (type %r)" % (
                offset, kind)
        end = offset + 8 + length + 4  # header + payload + CRC
        if length > size or end > size:
            return False, "PNG %dx%d %s chunk at byte %d claims %d bytes but only %d remain" % (
                width, height, kind.decode("ascii"), offset, length, max(0, size - offset - 12))
        if kind == b"IDAT":
            seen_idat = True
        elif kind == b"IEND":
            if not seen_idat:
                return False, "PNG %dx%d reaches IEND without any IDAT (no pixel data)" % (
                    width, height)
            return True, ""
        handle.seek(end)
    return False, "PNG has more than %d chunks" % _MAX_PNG_CHUNKS


# --- JPEG -------------------------------------------------------------------

def _parse_jpeg(handle: BinaryIO, size: int, head: bytes) -> Optional[Result]:
    handle.seek(2)  # past SOI
    dimensions: Optional[Tuple[int, int]] = None
    saw_scan = False

    for _ in range(_MAX_JPEG_SEGMENTS):
        offset = handle.tell()
        prefix = handle.read(2)
        if len(prefix) < 2:
            return False, "JPEG marker stream truncated at byte %d" % offset, None
        if prefix[0] != 0xFF:
            return False, "JPEG marker stream desynchronised at byte %d (0x%02X)" % (
                offset, prefix[0]), None
        marker = prefix[1]
        for _fill in range(_MAX_JPEG_FILL):  # 0xFF padding may precede a marker
            if marker != 0xFF:
                break
            nxt = handle.read(1)
            if not nxt:
                return False, "JPEG ends inside marker padding at byte %d" % offset, None
            marker = nxt[0]
        else:
            return False, "JPEG has more than %d fill bytes at byte %d" % (
                _MAX_JPEG_FILL, offset), None

        if marker in _JPEG_STANDALONE:
            continue
        if marker == 0xD8:
            return False, "JPEG has a second SOI at byte %d" % offset, None
        if marker == 0xD9:
            return False, "JPEG reaches EOI at byte %d without any scan data" % offset, None

        raw_length = handle.read(2)
        if len(raw_length) < 2:
            return False, "JPEG segment 0x%02X at byte %d has no length" % (marker, offset), None
        length = struct.unpack(">H", raw_length)[0]
        if length < 2:
            return False, "JPEG segment 0x%02X at byte %d declares %d bytes" % (
                marker, offset, length), None
        body_end = handle.tell() + length - 2
        if body_end > size:
            return False, "JPEG segment 0x%02X at byte %d runs %d bytes past end of file" % (
                marker, offset, body_end - size), None

        if marker in _JPEG_SOF:
            if length < 8:
                return False, "JPEG SOF at byte %d is too short (%d bytes)" % (
                    offset, length), None
            body = handle.read(5)
            _precision, frame_h, frame_w = struct.unpack(">BHH", body)
            dimensions = (int(frame_w), int(frame_h))
        elif marker == 0xDA:  # SOS: entropy coded data follows, no length to walk
            saw_scan = True
            break
        handle.seek(body_end)
    else:
        return False, "JPEG has more than %d marker segments" % _MAX_JPEG_SEGMENTS, None

    if not saw_scan:
        return False, "JPEG has no SOS marker (no scan data)", None
    if dimensions is None:
        return False, "JPEG has no SOF marker (dimensions unknown)", None
    width, height = dimensions
    if width == 0 or height == 0:
        return False, "JPEG declares zero dimensions %dx%d" % (width, height), None
    if width > _MAX_DIMENSION or height > _MAX_DIMENSION:
        return False, "JPEG declares implausible dimensions %dx%d" % (width, height), None

    # The scan is entropy coded, so the only bounded way to prove it finished
    # is to look for EOI in a tail window rather than walking to it.
    handle.seek(max(0, size - _TAIL_BYTES))
    if b"\xff\xd9" not in handle.read(_TAIL_BYTES):
        return False, "JPEG %dx%d has no EOI marker in its last %d bytes (truncated)" % (
            width, height, min(size, _TAIL_BYTES)), None
    return True, "", RasterInfo("JPEG", width, height)


# --- TGA / BMP / TIFF -------------------------------------------------------

def _parse_tga(handle: BinaryIO, size: int, head: bytes) -> Optional[Result]:
    if len(head) < 18:
        return False, "TGA shorter than its 18 byte header (%d bytes)" % size, None
    id_length, cmap_type, image_type = head[0], head[1], head[2]
    cmap_length = struct.unpack("<H", head[5:7])[0]
    cmap_entry_bits = head[7]
    width, height = struct.unpack("<HH", head[12:16])
    depth = head[16]
    if cmap_type > 1 or image_type not in _TGA_IMAGE_TYPES or depth not in _TGA_DEPTHS:
        return None  # not a TGA after all despite the extension; let Blender try
    if width == 0 or height == 0:
        return False, "TGA declares zero dimensions %dx%d" % (width, height), None

    if image_type in _TGA_UNCOMPRESSED:
        cmap_bytes = cmap_length * ((cmap_entry_bits + 7) // 8) if cmap_type == 1 else 0
        needed = 18 + id_length + cmap_bytes + width * height * ((depth + 7) // 8)
        if size < needed:
            return False, "TGA %dx%d needs %d bytes but the file holds %d (truncated)" % (
                width, height, needed, size), None
    return True, "", RasterInfo("TGA", int(width), int(height))


def _parse_bmp(handle: BinaryIO, size: int, head: bytes) -> Optional[Result]:
    if len(head) < 26:
        return False, "BMP shorter than its header (%d bytes)" % size, None
    declared_size, dib_size = struct.unpack("<I", head[2:6])[0], struct.unpack("<I", head[14:18])[0]
    if dib_size not in _BMP_DIB_SIZES:
        return None  # OS/2 variant or garbage; Blender knows more than we do
    if dib_size == 12:  # BITMAPCOREHEADER: unsigned 16 bit extents
        width, height = struct.unpack("<HH", head[18:22])
        width, height = int(width), int(height)
    else:  # BITMAPINFOHEADER and later: signed 32 bit, negative height = top-down
        width, height = struct.unpack("<ii", head[18:26])
        height = abs(height)
    if width <= 0 or height == 0:
        return False, "BMP declares zero dimensions %dx%d" % (width, height), None
    if width > _MAX_DIMENSION or height > _MAX_DIMENSION:
        return False, "BMP declares implausible dimensions %dx%d" % (width, height), None
    if declared_size > size:
        return False, "BMP %dx%d declares %d bytes but the file holds %d (truncated)" % (
            width, height, declared_size, size), None
    return True, "", RasterInfo("BMP", width, height)


def _parse_tiff(handle: BinaryIO, size: int, head: bytes) -> Optional[Result]:
    if len(head) < 8:
        return False, "TIFF shorter than its 8 byte header (%d bytes)" % size, None
    endian = "<" if head[:2] == b"II" else ">"
    if struct.unpack(endian + "H", head[2:4])[0] != 42:
        return None  # BigTIFF or a variant we do not speak
    ifd_offset = struct.unpack(endian + "I", head[4:8])[0]
    if ifd_offset < 8 or ifd_offset + 2 > size:
        return False, "TIFF IFD offset %d lies outside the %d byte file" % (ifd_offset, size), None

    handle.seek(ifd_offset)
    raw_count = handle.read(2)
    if len(raw_count) < 2:
        return False, "TIFF IFD truncated at byte %d" % ifd_offset, None
    count = struct.unpack(endian + "H", raw_count)[0]
    if count == 0 or count > _MAX_TIFF_ENTRIES:
        return None  # implausible directory; hand it to Blender rather than guess
    entries = handle.read(12 * count)
    if len(entries) < 12 * count:
        return False, "TIFF IFD claims %d entries but the file ends early" % count, None

    width = height = None
    for start in range(0, 12 * count, 12):
        entry = entries[start:start + 12]
        tag, field_type, values = struct.unpack(endian + "HHI", entry[:8])
        if values != 1 or tag not in (256, 257):
            continue
        # Values of 4 bytes or fewer sit left justified in the value field.
        if field_type == 3:
            value = struct.unpack(endian + "H", entry[8:10])[0]
        elif field_type == 4:
            value = struct.unpack(endian + "I", entry[8:12])[0]
        else:
            continue
        if tag == 256:
            width = int(value)
        else:
            height = int(value)

    if width is None or height is None:
        return None  # dimensions live in a tag layout we did not follow
    if width == 0 or height == 0:
        return False, "TIFF declares zero dimensions %dx%d" % (width, height), None
    if width > _MAX_DIMENSION or height > _MAX_DIMENSION:
        return False, "TIFF declares implausible dimensions %dx%d" % (width, height), None
    return True, "", RasterInfo("TIFF", width, height)


# --- Blender fallback -------------------------------------------------------

def _inspect_with_blender(path: str, size: int, head: bytes) -> Result:
    """Last resort: make Blender decode the file and report what it got."""
    if _bpy is None:
        return False, "unrecognised image container (%d bytes, starts %r)" % (
            size, head[:8]), None
    image: Any = None
    try:
        image = _bpy.data.images.load(path, check_existing=False)
        width, height = int(image.size[0]), int(image.size[1])
        if width <= 0 or height <= 0:
            # Blender happily creates the datablock for a file it cannot decode;
            # a zero extent is how that failure actually surfaces.
            return False, "Blender loaded the file but decoded no pixels", None
        image_format = str(getattr(image, "file_format", "") or "UNKNOWN").upper()
        return True, "", RasterInfo(image_format, width, height)
    except Exception as exc:
        return False, "Blender refused to load the image: %s" % exc, None
    finally:
        if image is not None:
            try:
                _bpy.data.images.remove(image)
            except Exception:
                pass  # a leaked datablock must never mask the real verdict
