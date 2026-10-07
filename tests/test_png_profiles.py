"""PNG metadata edits must never re-encode RGB16 or transparency."""
from pathlib import Path
from io import BytesIO
import struct
import sys
import tempfile
import unittest
import zlib

from PIL import Image, ImageCms

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from png_profiles import embed_srgb_profile, SIGNATURE, _chunk


def chunks(blob):
    offset = 8
    result = []
    while offset < len(blob):
        length, kind = struct.unpack(">I4s", blob[offset:offset + 8])
        result.append((kind, blob[offset:offset + length + 12]))
        offset += length + 12
    return result


class PNGProfileTests(unittest.TestCase):
    def test_16bit_pixels_and_alpha_chunks_are_identical_after_profile_insertion(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rgba16.png"
            ihdr = struct.pack(">IIBBBBB", 2, 1, 16, 6, 0, 0, 0)
            samples = struct.pack(">8H", 1000, 32768, 65535, 12800, 2000, 45000, 32000, 65535)
            blob = (SIGNATURE + _chunk(b"IHDR", ihdr) + _chunk(b"sRGB", b"\x00")
                    + _chunk(b"IDAT", zlib.compress(b"\x00" + samples)) + _chunk(b"IEND", b""))
            path.write_bytes(blob)
            embed_srgb_profile(path)
            updated = path.read_bytes()
            self.assertEqual([c for c in chunks(blob) if c[0] in (b"IHDR", b"IDAT", b"IEND")],
                             [c for c in chunks(updated) if c[0] in (b"IHDR", b"IDAT", b"IEND")])
            self.assertNotIn(b"sRGB", [c[0] for c in chunks(updated)])
            with Image.open(path) as image:
                self.assertTrue(image.info.get("icc_profile"))
                profile = ImageCms.ImageCmsProfile(BytesIO(image.info["icc_profile"]))
                self.assertEqual(profile.profile.xcolor_space.strip(), "RGB")
            first_chunks = [c for c in chunks(updated) if c[0] != b"iCCP"]
            embed_srgb_profile(path)
            self.assertEqual(first_chunks, [c for c in chunks(path.read_bytes()) if c[0] != b"iCCP"])
            self.assertEqual(sum(k == b"iCCP" for k, _ in chunks(path.read_bytes())), 1)

    def test_invalid_or_truncated_export_is_preserved_without_temporary_debris(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "broken.png"
            valid_start = SIGNATURE + _chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            for blob in (b"not png", valid_start + struct.pack(">I4s", 9, b"IDAT") + b"cut"):
                path.write_bytes(blob)
                with self.assertRaises(ValueError):
                    embed_srgb_profile(path)
                self.assertEqual(path.read_bytes(), blob)
                self.assertEqual(list(Path(directory).iterdir()), [path])


if __name__ == "__main__":
    unittest.main()
