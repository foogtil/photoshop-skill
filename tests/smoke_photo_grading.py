"""Live Photoshop tests for actual colour layers and neutral adaptive tone.

Run explicitly: python -X utf8 tests/smoke_photo_grading.py
Creates synthetic JPEGs only; preserves existing Photoshop documents/session.
"""
import hashlib
import json
from pathlib import Path
import sys
import subprocess
import tempfile

from PIL import Image, ImageCms, ImageDraw, ImageStat

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import photoshop_tone as tone
from photo_review import write_review


def mean(path):
    return ImageStat.Stat(tone.load_rgb(str(path))).mean


def main():
    import pythoncom
    started = not tone.photoshop_is_running()
    app = tone.connect_photoshop()
    try:
        initial_docs = int(app.Documents.Count)
        print("Photoshop version:", app.Version, flush=True)
        with tempfile.TemporaryDirectory(prefix="photoshop-grading-smoke-") as directory:
            root = Path(directory)
            icc = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
            grey = root / "grey.jpg"
            dark = root / "dark.jpg"
            colour = root / "colour.jpg"
            Image.new("RGB", (120, 80), (150, 150, 150)).save(grey, quality=100, icc_profile=icc)
            Image.new("RGB", (120, 80), (50, 50, 50)).save(dark, quality=100, icc_profile=icc)
            image = Image.new("RGB", (180, 80))
            draw = ImageDraw.Draw(image)
            for i, rgb in enumerate(((140, 160, 180), (160, 140, 120), (130, 160, 135))):
                draw.rectangle((i * 60, 0, i * 60 + 59, 79), fill=rgb)
            image.save(colour, quality=100, icc_profile=icc)
            hashes = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (grey, dark, colour)}
            identity = [{"ch": 0, "pts": [[0, 0], [255, 255]]}]
            specifications = [
                ("identity", grey, identity, {}, False),
                ("warm", grey, identity, {"temperature": 18}, False),
                ("cool", grey, identity, {"temperature": -18}, False),
                ("tint", grey, identity, {"tint": 18}, False),
                ("colour_baseline", colour, identity, {}, False),
                ("colour_vibrance", colour, identity, {"vibrance": 60}, False),
                ("colour_less_saturation", colour, identity, {"saturation": -40}, False),
            ]
            for name, src, mode in (("neutral_natural", grey, "natural"),
                                    ("dark_vivid", dark, "vivid"),
                                    ("film", grey, "film")):
                curves, summary = tone.derive_curves(tone.analyze_image(str(src)), mode)
                specifications.append((name, src, curves, summary["adjustments"], True))
            jobs = [{"src": str(src), "out": str(root / (name + "_out.jpg")),
                     "curves": curves, "adjustments": settings, "luminance_only": lum}
                    for name, src, curves, settings, lum in specifications]
            result = tone.run_jsx(app, tone.build_jsx(jobs, 12))
            assert str(result).splitlines() == ["OK"] * len(jobs), result
            for src, expected in hashes.items():
                assert hashlib.sha256(src.read_bytes()).hexdigest() == expected, "source changed"
            neutral = mean(root / "neutral_natural_out.jpg")
            assert max(neutral) - min(neutral) < 3, ("neutral cast", neutral)
            warm, cool, tint = [mean(root / (name + "_out.jpg")) for name in ("warm", "cool", "tint")]
            assert warm[0] - warm[2] > 8, ("warm curve had no effect", warm)
            assert cool[2] - cool[0] > 8, ("cool curve had no effect", cool)
            assert min(tint[0], tint[2]) - tint[1] > 8, ("tint curve had no effect", tint)
            assert mean(root / "dark_vivid_out.jpg")[0] > 56, "vivid darkened a dark image"
            baseline = tone.analyze_image(str(root / "colour_baseline_out.jpg"))["meanSaturation"]
            boosted = tone.analyze_image(str(root / "colour_vibrance_out.jpg"))["meanSaturation"]
            reduced = tone.analyze_image(str(root / "colour_less_saturation_out.jpg"))["meanSaturation"]
            assert boosted > baseline + 0.02, ("vibrance had no effect", baseline, boosted)
            assert reduced < baseline - 0.02, ("saturation had no effect", baseline, reduced)
            print("PASS actual warm/cool/tint, vibrance/saturation, neutral tone and dark-photo lift", flush=True)
            for job in jobs:
                with Image.open(job["out"]) as output:
                    assert output.info.get("icc_profile")
            assert int(app.Documents.Count) == initial_docs
            # Exercise the real CLI's preview, export and review integration in this session.
            inputs = root / "inputs"
            inputs.mkdir()
            Image.new("RGB", (100, 80), (90, 90, 90)).save(inputs / "sample.jpg", icc_profile=icc)
            # Separate interpreter: nested CoUninitialize must not invalidate this test's COM proxy.
            result = subprocess.run([sys.executable, "-X", "utf8", str(Path(tone.__file__)),
                                     str(inputs), "--out", str(root / "exports"), "--mode", "clean", "--keep-open"],
                                    capture_output=True, text=True, encoding="utf-8", timeout=120,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
            assert result.returncode == 0, result.stdout + result.stderr
            report = json.loads((root / "exports/_ps_review/review.json").read_text(encoding="utf-8"))
            assert report["visual_review_required"]
            assert report["images"][0]["settings"]["mode"] == "clean"
            assert Path(report["contact_sheets"][0]).is_file()
            assert int(app.Documents.Count) == initial_docs
            print("PASS CLI export, comparison/report, source hashes, ICC and session safety", flush=True)
    finally:
        try:
            if started and tone.photoshop_is_running() and not tone.quit_photoshop_if_idle(app):
                raise RuntimeError("Could not quit test Photoshop")
        finally:
            app = None
            pythoncom.CoUninitialize()


if __name__ == "__main__":
    main()
