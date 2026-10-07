"""Format regressions for PNG, high-bit-depth TIFF, and genuine camera RAW.

These tests decode images and exercise planning without starting Photoshop.
"""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
import zlib

from PIL import Image, ImageCms

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import photo_formats as formats
import photoshop_tone as tone

from format_fixtures import write_16bit_gray, write_16bit_rgb, write_cfa_dng


RAW_AVAILABLE = all(importlib.util.find_spec(name) is not None
                    for name in ("rawpy", "numpy", "tifffile"))
TIFF_AVAILABLE = all(importlib.util.find_spec(name) is not None
                     for name in ("numpy", "tifffile"))


def _pixel_list(image):
    return list(image.get_flattened_data() if hasattr(image, "get_flattened_data") else image.getdata())


class _FormatCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="photo-formats-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "sources"
        self.source.mkdir()
        self.output = self.root / "exports"
        self.icc = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()

    def run_cli(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = tone.main([str(self.source), "--out", str(self.output), *map(str, args)])
        return code, stdout.getvalue(), stderr.getvalue()


class PhotoFormatTests(_FormatCase):
    def test_common_input_extensions_include_photos_and_camera_raw(self):
        for extension in (".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp",
                          ".raw", ".dng", ".nef", ".cr2", ".cr3", ".arw", ".raf", ".rw2"):
            with self.subTest(extension=extension):
                self.assertIn(extension, formats.IMAGE_EXTS)
        self.assertIn(".dng", formats.RAW_EXTS)

    def test_auto_output_matches_preservation_requirements(self):
        expected = {
            "photo.JPG": "jpg", "photo.jpeg": "jpg", "photo.PNG": "png",
            "photo.tif": "tiff", "photo.TIFF": "tiff", "photo.DNG": "tiff",
            "photo.nef": "tiff", "photo.cr3": "tiff", "photo.arw": "tiff",
            "photo.bmp": "png", "photo.webp": "png",
        }
        for name, output in expected.items():
            with self.subTest(name=name):
                self.assertEqual(formats.choose_output_format(name), output)
        for requested in ("jpg", "png", "tiff"):
            with self.subTest(requested=requested):
                self.assertEqual(formats.choose_output_format("camera.dng", requested), requested)
        self.assertEqual(formats.output_extension("jpg"), ".jpg")
        self.assertEqual(formats.output_extension("png"), ".png")
        self.assertEqual(formats.output_extension("tiff"), ".tif")

    def test_png_preview_preserves_alpha_and_metadata(self):
        path = self.source / "transparent.png"
        pixels = Image.new("RGBA", (3, 1))
        pixels.putdata([(255, 0, 0, 0), (90, 140, 180, 128), (128, 128, 128, 255)])
        pixels.save(path, icc_profile=self.icc)
        rgb, alpha, metadata = formats.read_preview(str(path))
        self.assertEqual(rgb.mode, "RGB")
        self.assertEqual(alpha.mode, "L")
        self.assertEqual(_pixel_list(alpha), [0, 128, 255])
        self.assertEqual(metadata["size"], [3, 1])
        self.assertTrue(metadata["hasAlpha"])
        self.assertTrue(metadata["hasICC"])
        self.assertEqual(metadata["bitDepth"], 8)

    def test_indexed_png_with_rgb_icc_can_be_analyzed(self):
        path = self.source / "indexed.png"
        image = Image.new("P", (3, 1))
        image.putpalette([255, 0, 0, 0, 255, 0, 0, 0, 255] + [0] * 759)
        image.putdata([0, 1, 2])
        image.save(path, icc_profile=self.icc)
        rgb, alpha, metadata = formats.read_preview(path)
        self.assertEqual(_pixel_list(rgb), [(255, 0, 0), (0, 255, 0), (0, 0, 255)])
        self.assertIsNone(alpha)
        self.assertTrue(metadata["hasICC"])

    def test_bilevel_png_with_rgb_icc_keeps_black_and_white(self):
        path = self.source / "bilevel.png"
        image = Image.new("1", (2, 1))
        image.putdata([0, 255])
        image.save(path, icc_profile=self.icc)
        rgb, alpha, metadata = formats.read_preview(path)
        self.assertEqual(_pixel_list(rgb), [(0, 0, 0), (255, 255, 255)])
        self.assertIsNone(alpha)
        self.assertTrue(metadata["hasICC"])

    def test_gray16_png_transparency_is_compared_before_reducing_precision(self):
        path = self.source / "gray16-key.png"
        image = Image.frombytes("I;16", (4, 1), struct.pack("<HHHH", 0, 32768, 32769, 65535))
        image.save(path, transparency=32768)
        rgb, alpha, metadata = formats.read_preview(path)
        self.assertEqual(_pixel_list(rgb), [(0, 0, 0), (128, 128, 128),
                                               (128, 128, 128), (255, 255, 255)])
        self.assertEqual(_pixel_list(alpha), [255, 0, 255, 255])
        self.assertEqual(metadata["bitDepth"], 16)
        self.assertTrue(metadata["hasAlpha"])

    def test_rgb16_png_colour_key_is_rejected_instead_of_hiding_the_wrong_pixel(self):
        path = self.source / "rgb16-key.png"

        def chunk(kind, payload):
            return (struct.pack(">I", len(payload)) + kind + payload
                    + struct.pack(">I", zlib.crc32(kind + payload)))

        scanline = b"\x00" + b"".join(struct.pack(">HHH", v, v, v) for v in (0, 32768, 65535))
        path.write_bytes(b"\x89PNG\r\n\x1a\n"
                         + chunk(b"IHDR", struct.pack(">IIBBBBB", 3, 1, 16, 2, 0, 0, 0))
                         + chunk(b"tRNS", struct.pack(">HHH", 32768, 32768, 32768))
                         + chunk(b"IDAT", zlib.compress(scanline)) + chunk(b"IEND", b""))
        with self.assertRaisesRegex(ValueError, "tRNS"):
            formats.read_preview(path)

    def test_ordinary_images_and_gray16_do_not_require_numpy(self):
        jpeg = self.source / "plain.jpg"
        gray = self.source / "gray16.png"
        gray_tiff = self.source / "gray16.tif"
        Image.new("RGB", (4, 2), (100, 130, 160)).save(jpeg)
        grayscale = Image.frombytes("I;16", (3, 1), struct.pack("<HHH", 0, 32768, 65535))
        grayscale.save(gray)
        grayscale.save(gray_tiff)
        original_import = __import__

        def without_numpy(name, *args, **kwargs):
            if name == "numpy" or name.startswith("numpy."):
                raise ImportError("NumPy deliberately unavailable")
            return original_import(name, *args, **kwargs)

        spec = importlib.util.spec_from_file_location("photo_formats_without_numpy", formats.__file__)
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {spec.name: module}), \
             patch("builtins.__import__", side_effect=without_numpy):
            spec.loader.exec_module(module)
            jpeg_rgb, _, _ = module.read_preview(jpeg)
            gray_rgb, _, metadata = module.read_preview(gray)
            tiff_rgb, _, tiff_metadata = module.read_preview(gray_tiff)
        self.assertEqual(jpeg_rgb.size, (4, 2))
        self.assertEqual(_pixel_list(gray_rgb), [(0, 0, 0), (128, 128, 128), (255, 255, 255)])
        self.assertEqual(tiff_rgb.tobytes(), gray_rgb.tobytes())
        self.assertEqual(metadata["bitDepth"], 16)
        self.assertEqual(tiff_metadata["bitDepth"], 16)

    def test_hidden_png_rgb_does_not_pollute_exposure_or_colour_statistics(self):
        path = self.source / "hidden-red.png"
        pixels = Image.new("RGBA", (64, 48), (255, 0, 0, 0))
        pixels.paste((128, 128, 128, 255), (16, 12, 48, 36))
        pixels.save(path)
        stats = tone.analyze_image(str(path))
        for name in ("meanR", "meanG", "meanB", "meanL", "p50"):
            with self.subTest(statistic=name):
                self.assertAlmostEqual(stats[name], 128, delta=1)
        self.assertEqual(stats["meanSaturation"], 0)
        self.assertEqual(stats["shadowClip"], 0)
        self.assertEqual(stats["highlightClip"], 0)
        self.assertTrue(stats["hasAlpha"])

    def test_fully_transparent_image_has_no_tone_statistics(self):
        path = self.source / "empty.png"
        Image.new("RGBA", (24, 16), (255, 0, 0, 0)).save(path)
        with self.assertRaises(ValueError):
            tone.analyze_image(str(path))

    def test_thumbnail_does_not_mix_hidden_rgb_into_visible_edges(self):
        path = self.source / "large-transparent.png"
        image = Image.new("RGBA", (1536, 1024), (255, 0, 0, 0))
        image.paste((128, 128, 128, 255), (384, 256, 1152, 768))
        image.save(path)
        stats = tone.analyze_image(str(path), sample=512)
        for channel in ("meanR", "meanG", "meanB"):
            with self.subTest(channel=channel):
                self.assertAlmostEqual(stats[channel], 128, delta=1)
        self.assertLess(stats["meanSaturation"], 0.01)
        self.assertEqual(stats["size"], [1536, 1024])

    def test_bmp_and_webp_pixels_are_decoded_and_planned_as_png(self):
        for extension in ("bmp", "webp"):
            with self.subTest(extension=extension):
                path = self.source / ("photo." + extension)
                options = {"lossless": True} if extension == "webp" else {}
                Image.new("RGB", (24, 16), (90, 130, 170)).save(path, **options)
                rgb, alpha, metadata = formats.read_preview(str(path))
                self.assertEqual(rgb.getpixel((0, 0)), (90, 130, 170))
                self.assertIsNone(alpha)
                self.assertEqual(metadata["size"], [24, 16])
                self.assertIn(str(path), tone.collect_images(str(self.source), False, "P_", ""))
                path.unlink()

    def test_webp_working_png_keeps_transparency_and_is_cleaned_up(self):
        path = self.source / "transparent.webp"
        image = Image.new("RGBA", (24, 16), (120, 150, 180, 128))
        image.paste((20, 30, 40, 0), (0, 0, 8, 16))
        image.save(path, lossless=True, icc_profile=self.icc)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        _, source_alpha, _ = formats.read_preview(str(path))
        with formats.PhotoSources() as sources:
            prepared = sources.prepare(str(path))
            working = Path(prepared.path)
            self.assertEqual(working.suffix.lower(), ".png")
            self.assertNotEqual(working, path)
            _, prepared_alpha, metadata = formats.read_preview(str(working))
            self.assertEqual(prepared_alpha.tobytes(), source_alpha.tobytes())
            self.assertTrue(metadata["hasICC"])
        self.assertFalse(working.exists())
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)

    @unittest.skipUnless(TIFF_AVAILABLE, "16-bit TIFF tests require NumPy and tifffile")
    def test_16bit_gray_preview_scales_midgray_instead_of_clipping(self):
        path = write_16bit_gray(self.source / "gray.tif")
        rgb, alpha, metadata = formats.read_preview(str(path))
        self.assertIsNone(alpha)
        self.assertEqual(metadata["bitDepth"], 16)
        self.assertEqual(rgb.getpixel((4, 4)), (0, 0, 0))
        middle = rgb.getpixel((20, 4))
        self.assertTrue(all(127 <= value <= 128 for value in middle), middle)
        self.assertEqual(rgb.getpixel((40, 4)), (255, 255, 255))
        self.assertEqual(tone.analyze_image(str(path))["p50"], middle[0])

    @unittest.skipUnless(TIFF_AVAILABLE, "16-bit TIFF tests require NumPy and tifffile")
    def test_16bit_rgb_preview_reports_original_depth_and_dimensions(self):
        path = write_16bit_rgb(self.source / "rgb.tif")
        rgb, alpha, metadata = formats.read_preview(str(path))
        self.assertEqual(rgb.size, (64, 48))
        self.assertIsNone(alpha)
        self.assertEqual(metadata["bitDepth"], 16)
        self.assertEqual(metadata["size"], [64, 48])
        self.assertTrue(10 < rgb.getpixel((0, 0))[0] < 30)

    def test_animated_photo_is_rejected_instead_of_losing_frames(self):
        path = self.source / "animated.png"
        Image.new("RGB", (16, 12), "red").save(
            path, save_all=True, append_images=[Image.new("RGB", (16, 12), "blue")],
            duration=100, loop=0)
        with self.assertRaises(ValueError):
            formats.read_preview(str(path))

    def test_multipage_tiff_is_rejected_instead_of_discarding_pages(self):
        path = self.source / "pages.tif"
        Image.new("RGB", (16, 12), "red").save(
            path, save_all=True, append_images=[Image.new("RGB", (16, 12), "blue")])
        with self.assertRaises(ValueError):
            formats.read_preview(str(path))

    def test_explicit_cli_output_formats_plan_correct_extensions_without_writes(self):
        Image.new("RGB", (24, 16), (100, 130, 160)).save(self.source / "sample.png")
        for requested, extension in (("auto", ".png"), ("jpg", ".jpg"),
                                     ("png", ".png"), ("tiff", ".tif")):
            with self.subTest(requested=requested), patch.object(tone, "connect_photoshop") as connect:
                code, output, errors = self.run_cli("--dry-run", "--format", requested)
                self.assertEqual(code, 0, errors)
                self.assertIn("P_sample" + extension, output)
                connect.assert_not_called()
                self.assertFalse(self.output.exists())

    def test_preview_cannot_replace_a_png_source_outside_current_limit(self):
        Image.new("RGB", (24, 16), (100, 130, 160)).save(self.source / "a.jpg")
        omitted_source = self.source / "sources_001.png"
        Image.new("RGB", (24, 16), (120, 140, 160)).save(omitted_source)
        digest = hashlib.sha256(omitted_source.read_bytes()).hexdigest()
        with patch.object(tone, "connect_photoshop") as connect:
            code, _, errors = self.run_cli("--limit", "1", "--preview-only",
                                           "--review-dir", self.source)
        self.assertEqual(code, 2, errors)
        self.assertIn("review", errors.lower())
        connect.assert_not_called()
        self.assertEqual(hashlib.sha256(omitted_source.read_bytes()).hexdigest(), digest)
        self.assertFalse((self.source / "review.json").exists())

    def test_review_report_cannot_replace_an_omitted_source_via_hardlink(self):
        Image.new("RGB", (24, 16), (100, 130, 160)).save(self.source / "a.jpg")
        omitted_source = self.source / "z.png"
        Image.new("RGB", (24, 16), (120, 140, 160)).save(omitted_source)
        digest = hashlib.sha256(omitted_source.read_bytes()).hexdigest()
        review_folder = self.root / "review"
        review_folder.mkdir()
        report = review_folder / "review.json"
        os.link(omitted_source, report)
        with patch.object(tone, "connect_photoshop") as connect:
            code, _, errors = self.run_cli("--limit", "1", "--preview-only",
                                           "--review-dir", review_folder)
        self.assertEqual(code, 2, errors)
        self.assertIn("hard link", errors.lower())
        connect.assert_not_called()
        self.assertEqual(hashlib.sha256(omitted_source.read_bytes()).hexdigest(), digest)
        self.assertEqual(hashlib.sha256(report.read_bytes()).hexdigest(), digest)

    def test_intentional_white_backdrop_does_not_fail_strict_jpeg_review(self):
        source = self.source / "transparent.png"
        image = Image.new("RGBA", (64, 48), (255, 0, 0, 0))
        image.paste((128, 128, 128, 255), (16, 12, 48, 36))
        image.save(source, icc_profile=self.icc)

        def export(app, jsx):
            backdrop = Image.new("RGBA", image.size, (255, 255, 255, 255))
            Image.alpha_composite(backdrop, image).convert("RGB").save(
                self.output / "P_transparent.jpg", quality=100, subsampling=0,
                icc_profile=self.icc)
            return "OK"

        with patch.object(tone, "photoshop_is_running", return_value=True), \
             patch.object(tone, "connect_photoshop", return_value=object()), \
             patch.object(tone, "run_jsx", side_effect=export), \
             patch.dict(sys.modules, {"pythoncom": types.SimpleNamespace(CoUninitialize=lambda: None)}):
            code, _, errors = self.run_cli("--format", "jpg", "--strength", "0", "--strict-review")
        self.assertEqual(code, 0, errors)
        record = json.loads((self.output / "_ps_review/review.json").read_text(encoding="utf-8"))["images"][0]
        self.assertEqual(record["warnings"], [])
        self.assertEqual(record["status"], "pass")


@unittest.skipUnless(RAW_AVAILABLE, "RAW tests require rawpy, NumPy, and tifffile")
class CameraRawTests(_FormatCase):
    def test_fixture_is_an_actual_bayer_raw_that_libraw_demosaics(self):
        import numpy as np
        import rawpy

        path = write_cfa_dng(self.source / "camera.dng")
        with rawpy.imread(str(path)) as camera:
            self.assertEqual(camera.raw_image.shape, (96, 128))
            self.assertIsNotNone(camera.raw_pattern)
            pixels = camera.postprocess(use_camera_wb=True, use_auto_wb=False,
                                        no_auto_bright=True, output_color=rawpy.ColorSpace.sRGB,
                                        output_bps=16, gamma=(2.4, 12.92))
        self.assertEqual(pixels.dtype, np.uint16)
        self.assertEqual(pixels.shape, (96, 128, 3))
        self.assertGreater(int(pixels.max()) - int(pixels.min()), 20000)

    def test_raw_preparation_is_16bit_tagged_tiff_with_camera_orientation(self):
        import tifffile

        path = write_cfa_dng(self.source / "camera.dng", orientation=6)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        with formats.PhotoSources() as sources:
            prepared = sources.prepare(str(path))
            rendered_path = Path(prepared.path)
            self.assertTrue(rendered_path.is_file())
            self.assertNotEqual(rendered_path, path)
            with tifffile.TiffFile(rendered_path) as image:
                self.assertEqual(image.asarray().dtype.name, "uint16")
                self.assertEqual(image.asarray().shape, (128, 96, 3))
                self.assertTrue(image.pages[0].tags[34675].value)
            rgb, _, metadata = formats.read_preview(str(rendered_path))
            self.assertEqual(rgb.size, (96, 128))
            self.assertEqual(metadata["bitDepth"], 16)
            self.assertTrue(metadata["hasICC"])
            self.assertEqual(prepared.metadata["size"], [96, 128])
        self.assertFalse(rendered_path.exists())
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)
        self.assertEqual(sorted(p.name for p in self.source.iterdir()), ["camera.dng"])

    def test_raw_preview_and_photoshop_working_tiff_share_one_decoder_result(self):
        path = write_cfa_dng(self.source / "camera.dng", orientation=6)
        raw_rgb, raw_alpha, raw_metadata = formats.read_preview(str(path))
        with formats.PhotoSources() as sources:
            prepared = sources.prepare(str(path))
            working_rgb, working_alpha, working_metadata = formats.read_preview(str(prepared.path))
            self.assertEqual(raw_rgb.tobytes(), working_rgb.tobytes())
            self.assertEqual(raw_rgb.size, working_rgb.size)
            self.assertIsNone(raw_alpha)
            self.assertIsNone(working_alpha)
            self.assertEqual(raw_metadata["size"], working_metadata["size"])

    def test_raw_dry_run_creates_no_workspace_or_temporary_files(self):
        write_cfa_dng(self.source / "camera.dng")
        before = {p.relative_to(self.root) for p in self.root.rglob("*")}
        with patch.object(tone, "connect_photoshop") as connect, \
             patch.object(tempfile, "TemporaryDirectory", side_effect=AssertionError("dry-run wrote a temp directory")), \
             patch.object(tempfile, "mkdtemp", side_effect=AssertionError("dry-run wrote a temp directory")):
            code, output, errors = self.run_cli("--dry-run")
        self.assertEqual(code, 0, errors)
        self.assertIn("P_camera.tif", output)
        connect.assert_not_called()
        self.assertEqual({p.relative_to(self.root) for p in self.root.rglob("*")}, before)

    def test_jpeg_and_raw_with_same_stem_have_different_auto_outputs(self):
        Image.new("RGB", (24, 16), (130, 140, 150)).save(self.source / "camera.jpg")
        write_cfa_dng(self.source / "camera.dng")
        with patch.object(tone, "connect_photoshop") as connect:
            code, output, errors = self.run_cli("--dry-run")
        self.assertEqual(code, 0, errors)
        self.assertIn("P_camera.jpg", output)
        self.assertIn("P_camera.tif", output)
        connect.assert_not_called()

    def test_forcing_same_stem_jpeg_and_raw_to_jpeg_rejects_collision(self):
        Image.new("RGB", (24, 16)).save(self.source / "camera.jpg")
        write_cfa_dng(self.source / "camera.dng")
        with patch.object(tone, "connect_photoshop") as connect:
            code, _, errors = self.run_cli("--dry-run", "--format", "jpg")
        self.assertEqual(code, 2)
        self.assertIn("same output", errors)
        connect.assert_not_called()

    def test_corrupt_raw_returns_preparation_error_before_connecting(self):
        (self.source / "broken.dng").write_bytes(b"this is not a camera RAW image")
        with patch.object(tone, "connect_photoshop") as connect:
            code, _, errors = self.run_cli("--dry-run")
        self.assertEqual(code, 2)
        self.assertIn("Cannot prepare", errors)
        connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
