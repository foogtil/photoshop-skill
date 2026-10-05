"""Explicit live test: python tests/smoke_photoshop.py (starts Photoshop).

Uses generated JPEGs in a temporary folder. Exits Photoshop if this test started
it; otherwise preserves the existing session. Closes only test documents.
"""
import hashlib
from pathlib import Path
import sys
import tempfile

from PIL import Image, ImageCms, ImageStat

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import photoshop_tone as tone


def main():
    import pythoncom
    started_here = not tone.photoshop_is_running()
    app = tone.connect_photoshop()
    opened = None
    try:
        baseline = int(app.Documents.Count)
        dialogs = int(app.DisplayDialogs)
        print("Photoshop version:", app.Version)
        with tempfile.TemporaryDirectory(prefix="photoshop-tone-smoke-") as directory:
            root = Path(directory)
            source = root / "中文样图"
            source.mkdir()
            output = root / "export"
            icc = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
            # Neutral patches make separate RGB channel effects measurable.
            for name in ("identity", "red", "green", "blue", "dark"):
                Image.new("RGB", (80, 48), (180, 180, 180)).save(
                    source / (name + ".jpg"), quality=100, icc_profile=icc)
            exif = Image.Exif()
            exif[274] = 6
            Image.new("RGB", (80, 48), (180, 180, 180)).save(
                source / "portrait.jpeg", exif=exif, quality=100, icc_profile=icc)
            jobs = {"identity.jpg": {"tone": [0, 255, 1, 1]},
                    "portrait.jpeg": {"tone": [0, 255, 1, 1]},
                    "dark.jpg": {"tone": [0, 255, 0.5, 1]}}
            for ch, name in enumerate(("red", "green", "blue"), 1):
                jobs[name + ".jpg"] = {"channels": {str(ch): [[0, 0], [255, 128]]}}
            hashes = {p.name: hashlib.sha256(p.read_bytes()).digest() for p in source.iterdir()}
            output.mkdir()
            plan = [{"src": str(source / name),
                     "out": str(output / ("P_" + Path(name).stem + ".jpg")),
                     "curves": tone.normalize_manual(entry)} for name, entry in jobs.items()]
            # Keep a single COM connection, as the production CLI does.
            result = tone.run_jsx(app, tone.build_jsx(plan, 12))
            assert str(result).splitlines() == ["OK"] * len(plan), result
            for path in source.iterdir():
                assert hashlib.sha256(path.read_bytes()).digest() == hashes[path.name], "source changed"
            for name, ch in (("red", 0), ("green", 1), ("blue", 2)):
                with Image.open(output / ("P_" + name + ".jpg")) as image:
                    means = ImageStat.Stat(image).mean
                    assert means[ch] < 115, (name, means)
                    assert all(abs(means[i] - 180) < 6 for i in range(3) if i != ch), (name, means)
            with Image.open(output / "P_identity.jpg") as image:
                assert all(abs(v - 180) < 3 for v in ImageStat.Stat(image).mean)
                assert image.info.get("icc_profile"), "missing output profile"
            with Image.open(output / "P_dark.jpg") as image:
                assert ImageStat.Stat(image).mean[0] < 145, "composite curve had no effect"
            with Image.open(output / "P_portrait.jpg") as image:
                assert image.size == (48, 80), image.size
                assert image.getexif().get(274, 1) == 1, "orientation would be applied twice"
            assert int(app.Documents.Count) == baseline
            assert int(app.DisplayDialogs) == dialogs
            # A user-open source must survive with its original layer count.
            opened = app.Open(str(source / "identity.jpg"))
            layers = int(opened.Layers.Count)
            result = tone.run_jsx(app, tone.build_jsx([{
                "src": str(source / "identity.jpg"), "out": str(output / "blocked.jpg"),
                "curves": tone.normalize_manual({"tone": [0, 255, 0.5, 1]})}], 12))
            assert str(result).startswith("ERR:"), result
            assert int(app.Documents.Count) == baseline + 1
            assert int(opened.Layers.Count) == layers
            assert not (output / "blocked.jpg").exists()
            opened.Close(2)  # psDoNotSaveChanges
            opened = None
            print("PASS: six JPEG exports, composite/R/G/B effects, ICC, EXIF, Unicode, source hashes, session safety")
    finally:
        try:
            if opened is not None:
                opened.Close(2)
            if started_here:
                if not tone.quit_photoshop_if_idle(app):
                    raise RuntimeError("Photoshop cleanup failed")
        finally:
            app = None
            pythoncom.CoUninitialize()


if __name__ == "__main__":
    main()
