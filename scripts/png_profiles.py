"""Embed sRGB in Photoshop RGB PNG exports without decoding their image data.

PNGSaveOptions can omit ICC. Re-saving RGB16 through Pillow would lose precision;
replace colour metadata chunks only, retaining IHDR, IDAT and alpha unchanged.
Chunk definition: https://www.w3.org/TR/png-3/#11iCCP
"""
import os
from pathlib import Path
import struct
import tempfile
import zlib

from PIL import ImageCms


SIGNATURE = b"\x89PNG\r\n\x1a\n"
COLOUR_CHUNKS = {b"iCCP", b"sRGB", b"gAMA", b"cHRM", b"cICP", b"mDCV", b"cLLI"}


def _chunk(kind, data):
    return (struct.pack(">I", len(data)) + kind + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff))


def _copy_exact(source, output, count):
    while count:
        data = source.read(min(count, 1024 * 1024))
        if not data:
            raise ValueError("Truncated Photoshop PNG export")
        if output is not None:
            output.write(data)
        count -= len(data)


def embed_srgb_profile(path):
    """Atomically tag a PS export already converted to sRGB; preserve pixel bytes."""
    target = Path(path)
    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    iccp = _chunk(b"iCCP", b"sRGB IEC61966-2.1\x00\x00" + zlib.compress(profile))
    temporary = None
    try:
        with target.open("rb") as source:
            if source.read(8) != SIGNATURE:
                raise ValueError("Photoshop export is not a PNG")
            header = source.read(8)
            if len(header) != 8 or header[4:] != b"IHDR" or struct.unpack(">I", header[:4])[0] != 13:
                raise ValueError("Invalid PNG header")
            ihdr = source.read(17)  # 13 bytes of image information, plus CRC.
            if len(ihdr) != 17 or ihdr[9] not in (2, 6):
                raise ValueError("Expected an RGB/RGBA Photoshop PNG export")
            with tempfile.NamedTemporaryFile(mode="wb", prefix=".ps-profile-", suffix=".png",
                                             dir=target.parent, delete=False) as output:
                temporary = Path(output.name)
                output.write(SIGNATURE + header + ihdr + iccp)
                while True:
                    header = source.read(8)
                    if len(header) != 8:
                        raise ValueError("PNG export has no complete IEND chunk")
                    length, kind = struct.unpack(">I4s", header)
                    if kind in COLOUR_CHUNKS:
                        _copy_exact(source, None, length + 4)
                    else:
                        output.write(header)
                        _copy_exact(source, output, length + 4)
                    if kind == b"IEND":
                        if length:
                            raise ValueError("Invalid PNG IEND chunk")
                        break
        os.replace(temporary, target)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
