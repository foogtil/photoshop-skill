"""Live CLI lifetime regression. Requires Photoshop to be closed before starting."""
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import photoshop_tone as tone


def wait_for_exit():
    deadline = time.monotonic() + 15
    while tone.photoshop_is_running() and time.monotonic() < deadline:
        time.sleep(0.25)
    assert not tone.photoshop_is_running(), "Photoshop process remains after normal exit"


def main():
    if tone.photoshop_is_running():
        raise RuntimeError("Close Photoshop before this live test; an existing user session will not be closed")
    script = Path(tone.__file__).resolve()
    try:
        with tempfile.TemporaryDirectory(prefix="photoshop-exit-test-") as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            Image.new("RGB", (40, 24), (120, 140, 160)).save(source / "sample.jpg")
            for label, flags, remains_open in [
                ("default startup exits", [], False),
                ("keep-open retains application", ["--keep-open"], True),
                ("existing session is preserved", [], True),
                ("explicit quit exits", ["--quit"], False),
            ]:
                result = subprocess.run(
                    [sys.executable, "-X", "utf8", str(script), str(source),
                     "--out", str(root / label), *flags],
                    capture_output=True, text=True, encoding="utf-8", timeout=120,
                    creationflags=subprocess.CREATE_NO_WINDOW)
                assert result.returncode == 0, result.stdout + result.stderr
                assert (root / label / "P_sample.jpg").is_file()
                if remains_open:
                    assert tone.photoshop_is_running(), label
                else:
                    wait_for_exit()
                print("PASS:", label, flush=True)
    finally:
        if tone.photoshop_is_running():
            import pythoncom
            app = tone.connect_photoshop()
            try:
                if not tone.quit_photoshop_if_idle(app):
                    raise RuntimeError("Test application cleanup failed")
            finally:
                app = None
                pythoncom.CoUninitialize()


if __name__ == "__main__":
    main()
