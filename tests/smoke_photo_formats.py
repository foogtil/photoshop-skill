"""Real Photoshop export of common photo formats, including a genuine CFA DNG.

Run explicitly: python -X utf8 tests/smoke_photo_formats.py
Requires rawpy, NumPy and tifffile. Uses synthetic photos, preserves existing PS
documents, and normally exits Photoshop if this test started it.
"""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile

import numpy as np
import tifffile
from PIL import Image, ImageCms, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import photoshop_tone as tone
from format_fixtures import write_cfa_dng


def cli(inputs, outputs, *extra):
    result = subprocess.run(
        [sys.executable, "-X", "utf8", str(Path(tone.__file__)), str(inputs),
         "--out", str(outputs), "--keep-open", *extra],
        capture_output=True, text=True, encoding="utf-8", timeout=180,
        creationflags=subprocess.CREATE_NO_WINDOW)
    assert result.returncode == 0, result.stdout + result.stderr
    print(result.stdout, end="", flush=True)
    return json.loads((outputs / "_ps_review/review.json").read_text(encoding="utf-8"))


def main():
    import pythoncom
    started = not tone.photoshop_is_running()
    app = tone.connect_photoshop()
    try:
        initial_docs = int(app.Documents.Count)
        print("Photoshop version:", app.Version, flush=True)
        with tempfile.TemporaryDirectory(prefix="photoshop-formats-smoke-") as directory:
            root = Path(directory)
            inputs = root / "inputs"
            inputs.mkdir()
            icc = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
            rgba = Image.new("RGBA", (128, 96), (255, 0, 0, 0))
            draw = ImageDraw.Draw(rgba)
            draw.rectangle((32, 0, 63, 95), fill=(125, 140, 150, 128))
            draw.rectangle((64, 0, 127, 95), fill=(100, 125, 145, 255))
            rgba.save(inputs / "transparent.png", icc_profile=icc)
            values = np.tile(np.linspace(1024, 60000, 128, dtype=np.uint16), (96, 1))
            rgb16 = np.stack((values, values, values), axis=2)
            tifffile.imwrite(inputs / "rgb16.tif", rgb16, photometric="rgb", metadata=None,
                             extratags=[(34675, "B", len(icc), icc, False)])
            Image.fromarray(values).save(inputs / "grey16.png")
            Image.fromarray(values).save(inputs / "grey16.tif")
            rgb = Image.new("RGB", (128, 96), (100, 130, 150))
            for ext in ("jpg", "bmp", "webp"):
                rgb.save(inputs / ("ordinary." + ext), icc_profile=icc)
            # Avoid same-stem BMP/WebP exports: both correctly map to PNG.
            (inputs / "ordinary.bmp").rename(inputs / "bitmap.bmp")
            (inputs / "ordinary.webp").rename(inputs / "webphoto.webp")
            write_cfa_dng(inputs / "camera.dng", orientation=6)
            (inputs / "camera.xmp").write_text("untouched sidecar", encoding="utf-8")
            hashes = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in inputs.iterdir()}
            preview = root / "preview"
            preview_run = subprocess.run(
                [sys.executable, "-X", "utf8", str(Path(tone.__file__)), str(inputs),
                 "--preview-only", "--review-dir", str(preview)], capture_output=True,
                text=True, encoding="utf-8", timeout=120, creationflags=subprocess.CREATE_NO_WINDOW)
            assert preview_run.returncode == 0, preview_run.stdout + preview_run.stderr
            preview_report = json.loads((preview / "review.json").read_text(encoding="utf-8"))
            assert len(preview_report["images"]) == 8
            assert int(app.Documents.Count) == initial_docs
            auto = root / "auto"
            report = cli(inputs, auto, "--strength", "0")
            expected = {"P_transparent.png", "P_rgb16.tif", "P_grey16.png", "P_grey16.tif",
                        "P_ordinary.jpg", "P_bitmap.png", "P_webphoto.png", "P_camera.tif"}
            assert {p.name for p in auto.iterdir() if p.is_file()} == expected
            with Image.open(auto / "P_transparent.png") as image:
                assert image.convert("RGBA").getchannel("A").tobytes() == rgba.getchannel("A").tobytes(), "alpha changed"
            for name in ("P_rgb16.tif", "P_grey16.tif", "P_camera.tif", "P_grey16.png"):
                assert tone.analyze_image(str(auto / name))["bitDepth"] == 16, ("lost 16-bit precision", name)
            camera = next(r for r in report["images"] if r["source"].endswith("camera.dng"))
            assert camera["before"]["size"] == camera["after"]["size"] == [96, 128], "RAW orientation changed"
            assert camera["source_preparation"], "RAW decode not recorded"
            for record in report["images"]:
                assert record["after"]["hasICC"], ("missing ICC", record["output"])
                assert not any(w["code"] in ("transparency_changed", "bit_depth_reduced", "dimensions_changed")
                               for w in record["warnings"]), record
            # Explicit JPEG request: grade first, composite transparent regions on white.
            transparent_only = root / "transparent_only"
            transparent_only.mkdir()
            rgba.save(transparent_only / "sample.png", icc_profile=icc)
            jpeg_out = root / "jpeg"
            cli(transparent_only, jpeg_out, "--format", "jpg", "--strength", "0")
            with Image.open(jpeg_out / "P_sample.jpg") as image:
                assert min(image.convert("RGB").getpixel((8, 40))) >= 252, "JPEG transparent background is not white"
            tiff_out = root / "transparent_tiff"
            cli(transparent_only, tiff_out, "--format", "tiff", "--strength", "0")
            with Image.open(tiff_out / "P_sample.tif") as image:
                assert image.convert("RGBA").getchannel("A").tobytes() == rgba.getchannel("A").tobytes(), "TIFF alpha changed"
            # The same colour grading must act on PNG, not just convert its container.
            coloured = root / "colour"
            cli(transparent_only, coloured, "--mode", "vivid", "--strength", "1.2")
            assert tone.analyze_image(str(coloured / "P_sample.png"))["meanL"] != tone.analyze_image(str(transparent_only / "sample.png"))["meanL"]
            # Confirm adjustment layers change a 16-bit RAW render, not just decode/save it.
            raw_only = root / "raw_only"
            raw_only.mkdir()
            (raw_only / "camera.dng").write_bytes((inputs / "camera.dng").read_bytes())
            raw_coloured = cli(raw_only, root / "raw_coloured", "--mode", "warm")
            changed = raw_coloured["images"][0]
            baseline_colour = camera["after"]["meanR"] - camera["after"]["meanB"]
            result_colour = changed["after"]["meanR"] - changed["after"]["meanB"]
            assert result_colour > baseline_colour + 3, ("RAW warm curves had no effect", baseline_colour, result_colour)
            assert changed["after"]["bitDepth"] == 16
            for path, digest in hashes.items():
                assert hashlib.sha256(path.read_bytes()).hexdigest() == digest, "source or sidecar changed"
            assert int(app.Documents.Count) == initial_docs, "test left documents open"
            print("PASS PNG alpha, PNG/TIFF 16-bit, RAW render/orientation, BMP/WebP/JPEG, ICC, colour grading and original/XMP protection", flush=True)
    finally:
        try:
            if started and tone.photoshop_is_running() and not tone.quit_photoshop_if_idle(app):
                raise RuntimeError("Could not quit test Photoshop")
        finally:
            app = None
            pythoncom.CoUninitialize()


if __name__ == "__main__":
    main()
