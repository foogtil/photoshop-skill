"""Regression tests that do not launch Photoshop: python -m unittest discover -s tests -v."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import photoshop_tone as tone


class ToneTests(unittest.TestCase):
    def setUp(self):
        running = patch.object(tone, "photoshop_is_running", return_value=True)
        self.running = running.start()
        self.addCleanup(running.stop)
        self.temp = tempfile.TemporaryDirectory(prefix="photoshop-tone-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.output = self.root / "output"

    def jpeg(self, relative="sample.JPG", color=(130, 150, 170)):
        path = self.source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (24, 16), color).save(path)
        return path

    def run_main(self, *args):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return tone.main([str(self.source), *map(str, args)])

    def test_dry_run_does_not_create_output_or_connect(self):
        self.jpeg()
        with patch.object(tone, "connect_photoshop") as connect:
            self.assertEqual(self.run_main("--out", self.output, "--dry-run"), 0)
            connect.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_suffix_filter_checks_stem(self):
        original = self.jpeg("scene.jpg")
        self.jpeg("scene_done.JPG")
        self.assertEqual(tone.collect_images(str(self.source), False, "", "_done"), [str(original)])

    def test_recursive_output_preserves_subfolders_and_excludes_output_tree(self):
        self.jpeg("one/same.jpg")
        self.jpeg("two/same.jpg")
        nested_output = self.source / "export"
        nested_output.mkdir()
        Image.new("RGB", (8, 8)).save(nested_output / "unprefixed.jpg")
        captured = []

        def build(jobs, quality, overwrite=False):
            captured.extend(jobs)
            return "jsx"

        with patch.object(tone, "connect_photoshop", return_value=object()), \
             patch.object(tone, "build_jsx", side_effect=build), \
             patch.object(tone, "run_jsx", return_value="ERR:test\nERR:test"), \
             patch.dict(sys.modules, {"pythoncom": types.SimpleNamespace(CoUninitialize=lambda: None)}):
            self.assertEqual(self.run_main("--recursive", "--out", nested_output), 1)
        self.assertEqual(len(captured), 2)
        self.assertEqual({Path(j["out"]).relative_to(nested_output).as_posix() for j in captured},
                         {"one/P_same.jpg", "two/P_same.jpg"})

    def test_existing_output_is_preserved(self):
        self.jpeg()
        original = b"existing export"
        (self.source / "P_sample.jpg").write_bytes(original)
        with patch.object(tone, "connect_photoshop") as connect:
            self.assertEqual(self.run_main(), 0)
            connect.assert_not_called()
        self.assertEqual((self.source / "P_sample.jpg").read_bytes(), original)

    def test_jpg_jpeg_output_collision_rejected_before_connect(self):
        self.jpeg("same.jpg")
        self.jpeg("same.jpeg")
        with patch.object(tone, "connect_photoshop") as connect:
            self.assertEqual(self.run_main("--dry-run"), 2)
            connect.assert_not_called()

    def test_output_cannot_overwrite_a_different_source_in_the_batch(self):
        self.jpeg("sample.jpg")
        self.jpeg("source/sample.jpg")
        with patch.object(tone, "connect_photoshop") as connect:
            self.assertEqual(self.run_main("--recursive", "--out", self.root,
                                           "--prefix", "", "--overwrite", "--dry-run"), 2)
            connect.assert_not_called()

    def test_existing_hardlink_to_other_original_is_not_overwritten(self):
        self.jpeg("a.jpg")
        other = self.jpeg("b.jpg")
        self.output.mkdir()
        os.link(other, self.output / "P_a.jpg")
        with patch.object(tone, "connect_photoshop") as connect:
            self.assertEqual(self.run_main("--out", self.output, "--overwrite", "--dry-run"), 2)
            connect.assert_not_called()

    def test_invalid_cli_values_and_path_fragments(self):
        self.jpeg()
        for args in [("--quality", "13"), ("--quality", "0"), ("--limit", "-1"),
                     ("--prefix", "../"), ("--suffix", ":bad"), ("--prefix", "")]:
            with self.subTest(args=args):
                self.assertEqual(self.run_main(*args, "--dry-run"), 2)

    def test_invalid_manual_curves(self):
        for entry in [{}, {"points": [[0, 0], [255, 255]]}, {"tone": [10, 10, 1, 1]},
                      {"tone": [0, 255, 0, 1]}, {"tone": [0, 255, float("nan"), 1]},
                      {"tone": [0, 255, 1, 1], "channels": {"0": [[0, 0], [255, 255]]}},
                      {"channels": {"4": [[0, 0], [255, 255]]}},
                      {"curves": [{"ch": 0, "pts": [[128, 0], [128, 255]]}]},
                      {"curves": [{"ch": 0, "pts": [[0, -1], [255, 255]]}]}]:
            with self.subTest(entry=entry), self.assertRaises(ValueError):
                tone.normalize_manual(entry)

    def test_example_jobs_are_valid(self):
        example = Path(__file__).resolve().parents[1] / "examples" / "jobs.example.json"
        entries = json.loads(example.read_text(encoding="utf-8-sig"))
        for key, entry in entries.items():
            if not key.startswith("_"):
                with self.subTest(key=key):
                    stats = tone.analyze_image(str(self.jpeg()))
                    curves, summary = tone.normalize_job(entry, stats, "auto", 1)
                    self.assertTrue(curves)
                    self.assertIn("mode", summary)

    def test_identity_and_extreme_tones_have_valid_points(self):
        identity = tone.tone_curve_points(0, 255, 1, 1)
        self.assertTrue(all(x == y for x, y in identity))
        for black, white in [(0, 8), (247, 255), (0.4, 8.4), (10, 240)]:
            tone.validate_curves([{"ch": 0, "pts": tone.tone_curve_points(black, white, 1, 1)}])

    def test_bom_jobs_and_relative_keys(self):
        self.jpeg("one/same.jpg")
        self.jpeg("two/same.jpg")
        jobs = self.root / "jobs.json"
        jobs.write_text(json.dumps({"one/same.jpg": {"tone": [0, 255, 1, 1]}}), encoding="utf-8-sig")
        self.assertEqual(self.run_main("--recursive", "--jobs", jobs, "--dry-run"), 0)
        jobs.write_text(json.dumps({"same.jpg": {"tone": [0, 255, 1, 1]}}), encoding="utf-8")
        self.assertEqual(self.run_main("--recursive", "--jobs", jobs, "--dry-run"), 2)

    def test_corrupt_image_and_json_are_reported(self):
        (self.source / "bad.jpg").write_bytes(b"not an image")
        self.assertEqual(self.run_main("--dry-run"), 2)
        jobs = self.root / "bad.json"
        jobs.write_text("[]", encoding="utf-8")
        self.assertEqual(self.run_main("--jobs", jobs, "--dry-run"), 2)

    def test_photoshop_failure_and_incomplete_log_are_nonzero(self):
        self.jpeg()
        for result in ["ERR:failed%0Awith%20detail", "", None, "OK\nOK", "unexpected", "OK"]:
            with self.subTest(result=result), \
                 patch.object(tone, "connect_photoshop", return_value=object()), \
                 patch.object(tone, "run_jsx", return_value=result), \
                 patch.dict(sys.modules, {"pythoncom": types.SimpleNamespace(CoUninitialize=lambda: None)}):
                self.assertEqual(self.run_main("--out", self.output), 1)

    def test_verified_output_returns_success(self):
        self.jpeg()

        def run(app, jsx):
            Image.new("RGB", (24, 16)).save(self.output / "P_sample.jpg")
            return "OK"

        with patch.object(tone, "connect_photoshop", return_value=object()), \
             patch.object(tone, "run_jsx", side_effect=run), \
             patch.dict(sys.modules, {"pythoncom": types.SimpleNamespace(CoUninitialize=lambda: None)}):
            self.assertEqual(self.run_main("--out", self.output), 0)

    def test_application_lifetime_flags(self):
        self.jpeg()
        cases = [(False, [], True), (True, [], False),
                 (False, ["--keep-open"], False), (True, ["--quit"], True)]
        for was_running, flags, expect_quit in cases:
            with self.subTest(was_running=was_running, flags=flags):
                self.running.return_value = was_running
                app = Mock()
                def run(app, jsx):
                    if jsx == "app.documents.length;":
                        return 0
                    Image.new("RGB", (24, 16)).save(self.output / "P_sample.jpg")
                    return "OK"
                with patch.object(tone, "connect_photoshop", return_value=app), \
                     patch.object(tone, "run_jsx", side_effect=run), \
                     patch.dict(sys.modules, {"pythoncom": types.SimpleNamespace(CoUninitialize=lambda: None)}):
                    self.assertEqual(self.run_main("--out", self.output, "--overwrite", *flags), 0)
                self.assertEqual(app.Quit.call_count, int(expect_quit))

    def test_error_still_quits_script_started_photoshop(self):
        self.jpeg()
        self.running.return_value = False
        app = Mock()
        with patch.object(tone, "connect_photoshop", return_value=app), \
             patch.object(tone, "run_jsx", side_effect=[RuntimeError("batch failed"), 0]), \
             patch.dict(sys.modules, {"pythoncom": types.SimpleNamespace(CoUninitialize=lambda: None)}):
            self.assertEqual(self.run_main(), 1)
        app.Quit.assert_called_once()

    def test_open_documents_prevent_quit(self):
        app = Mock()
        with patch.object(tone, "run_jsx", return_value=1), contextlib.redirect_stderr(io.StringIO()):
            self.assertTrue(tone.quit_photoshop_if_idle(app))
        app.Quit.assert_not_called()

    def test_quit_failure_is_nonzero(self):
        self.jpeg()
        self.running.return_value = False
        app = Mock()
        app.Quit.side_effect = RuntimeError("quit failed")
        def run(app, jsx):
            if jsx == "app.documents.length;":
                return 0
            Image.new("RGB", (24, 16)).save(self.output / "P_sample.jpg")
            return "OK"
        with patch.object(tone, "connect_photoshop", return_value=app), \
             patch.object(tone, "run_jsx", side_effect=run), \
             patch.dict(sys.modules, {"pythoncom": types.SimpleNamespace(CoUninitialize=lambda: None)}):
            self.assertEqual(self.run_main("--out", self.output), 1)

    def test_busy_retry_is_bounded_and_other_errors_are_not_retried(self):
        class ComError(Exception):
            def __init__(self, code):
                self.hresult = code
        app = types.SimpleNamespace()
        from unittest.mock import Mock
        app.DoJavaScript = Mock(side_effect=[ComError(tone.RPC_E_SERVERCALL_RETRYLATER), "OK"])
        with patch.dict(sys.modules, {"pythoncom": types.SimpleNamespace(com_error=ComError)}), \
             patch.object(tone.time, "sleep"):
            self.assertEqual(tone.run_jsx(app, "jsx", retries=2), "OK")
            app.DoJavaScript = Mock(side_effect=ComError(123))
            with self.assertRaises(ComError):
                tone.run_jsx(app, "jsx")
            self.assertEqual(app.DoJavaScript.call_count, 1)

    @unittest.skipUnless(os.name == "nt", "Windows drive semantics")
    def test_separate_windows_drive_is_not_nested(self):
        self.assertFalse(tone.is_within("D:\\exports", "C:\\photos"))


if __name__ == "__main__":
    unittest.main()
