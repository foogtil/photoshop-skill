"""Offline colour-grading and export-review regressions; never start Photoshop."""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from PIL import Image, ImageCms

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import photoshop_tone as tone
import photo_review as review


def statistics(**updates):
    result = {
        "p01": 12, "p05": 28, "p25": 70, "p50": 128,
        "p75": 180, "p95": 225, "p99": 246,
        "mean": 128, "meanL": 128, "meanR": 128, "meanG": 128, "meanB": 128,
        "meanSaturation": 0.2, "saturatedFraction": 0.02,
        "shadowClip": 0.002, "highlightClip": 0.002,
        "channelClip": {"red": 0.002, "green": 0.002, "blue": 0.002},
        "size": [64, 48], "hasICC": True,
    }
    result.update(updates)
    return result


class GradingTests(unittest.TestCase):
    def test_natural_and_auto_keep_neutral_colour_endpoints(self):
        for mode in ("auto", "natural", "clean", "vivid"):
            with self.subTest(mode=mode):
                curves, summary = tone.derive_curves(statistics(meanSaturation=0), mode)
                self.assertEqual([curve["ch"] for curve in curves], [0])
                self.assertEqual(curves[0]["pts"][0], [0, 0])
                self.assertEqual(curves[0]["pts"][-1], [255, 255])
                self.assertEqual(tone.colour_curves(summary["adjustments"]), [])
                self.assertEqual(summary["adjustments"]["vibrance"], 0)
                self.assertEqual(summary["adjustments"]["saturation"], 0)

    def test_dark_vivid_lifts_subject_midtones(self):
        dark = statistics(p01=2, p05=8, p25=25, p50=55, p75=95, p95=150, p99=200)
        curves, summary = tone.derive_curves(dark, "vivid")
        lookup = dict(curves[0]["pts"])
        self.assertGreater(lookup[64], 64)
        self.assertGreater(lookup[96], 96)
        self.assertGreater(summary["gamma"], 1)
        self.assertEqual(summary["diagnosis"]["brightness"], "dark")

    def test_high_key_does_not_turn_first_percentile_into_hard_black(self):
        bright = statistics(p01=145, p05=160, p25=185, p50=215, p75=238,
                            p95=250, p99=255, mean=216, meanL=216)
        curves, summary = tone.derive_curves(bright, "auto")
        self.assertEqual(summary["mode"], "natural")
        self.assertEqual(curves[0]["pts"][0], [0, 0])
        self.assertGreater(dict(curves[0]["pts"])[128], 64)
        self.assertTrue(all(y > 0 for x, y in curves[0]["pts"] if x >= bright["p01"]))
        self.assertEqual(curves[0]["pts"][-1], [255, 255])

    def test_zero_strength_is_identity_for_every_look(self):
        for mode in tone.MODES:
            with self.subTest(mode=mode):
                curves, summary = tone.derive_curves(statistics(p50=55, p25=25), mode, 0)
                self.assertTrue(all(x == y for x, y in curves[0]["pts"]))
                self.assertTrue(all(value == 0 for value in summary["adjustments"].values()))
                self.assertEqual(tone.colour_curves(summary["adjustments"]), [])

    def test_looks_have_distinct_results_and_warmth_is_explicit(self):
        signatures = set()
        for mode in ("natural", "clean", "vivid", "warm", "film"):
            curves, summary = tone.derive_curves(statistics(), mode)
            signatures.add(json.dumps([curves, summary["adjustments"]], sort_keys=True))
            if mode in ("warm", "film"):
                self.assertGreater(summary["adjustments"]["temperature"], 0)
                for colour_curve in tone.colour_curves(summary["adjustments"]):
                    self.assertEqual(colour_curve["pts"][0], [0, 0])
                    self.assertEqual(colour_curve["pts"][-1], [255, 255])
            else:
                self.assertEqual(summary["adjustments"]["temperature"], 0)
        self.assertEqual(len(signatures), 5)

    def test_already_colourful_input_gets_less_colour_boost(self):
        _, muted = tone.derive_curves(statistics(), "vivid")
        for colour_stats in ({"meanSaturation": 0.7}, {"saturatedFraction": 0.3}):
            with self.subTest(colour_stats=colour_stats):
                _, colourful = tone.derive_curves(statistics(**colour_stats), "vivid")
                self.assertLess(colourful["adjustments"]["vibrance"], muted["adjustments"]["vibrance"])
                self.assertLessEqual(colourful["adjustments"]["saturation"], 0)

    def test_extreme_strength_curves_remain_bounded_and_monotonic(self):
        scenes = [statistics(p50=20, p25=8, p75=70), statistics(),
                  statistics(p01=190, p25=220, p50=240, p75=250, p99=255)]
        for scene in scenes:
            for mode in tone.MODES:
                with self.subTest(median=scene["p50"], mode=mode):
                    curves, _ = tone.derive_curves(scene, mode, 1.5)
                    tone.validate_curves(curves)
                    ys = [y for _, y in curves[0]["pts"]]
                    self.assertEqual(ys, sorted(ys))
                    self.assertTrue(all(0 <= y <= 255 for y in ys))

    def test_per_photo_mode_strength_and_colour_overrides(self):
        curves, summary = tone.normalize_job({
            "mode": "warm", "strength": 0.5,
            "adjustments": {"vibrance": 12, "tint": -3},
            "reason": "Lift the subject and warm the room lighting",
        }, statistics(), "vivid", 1)
        self.assertEqual(summary["mode"], "warm")
        self.assertEqual(summary["strength"], 0.5)
        self.assertEqual(summary["adjustments"]["vibrance"], 12)
        self.assertEqual(summary["adjustments"]["tint"], -3)
        self.assertIn("subject", summary["reason"])
        self.assertEqual([c["ch"] for c in curves], [0])

    def test_invalid_per_photo_choices_are_rejected(self):
        entries = [
            {"mode": "unknown"}, {"mode": []},
            {"strength": -0.1}, {"strength": 1.6}, {"strength": True},
            {"strength": float("nan")}, {"strength": "1"},
            {"adjustments": []}, {"adjustments": {"vibrance": 101}},
            {"adjustments": {"saturation": -101}},
            {"adjustments": {"temperature": 31}}, {"adjustments": {"tint": -31}},
            {"adjustments": {"vibrance": True}}, {"adjustments": {"tint": float("inf")}},
            {"adjustments": {"exposure": 1}}, {"reason": 123}, {"unexpected": 1},
            {"tone": [0, 255, 1, 1], "mode": "natural"},
            {"curves": [{"ch": 0, "pts": [[0, 0], [255, 255]]}], "strength": 1},
        ]
        for entry in entries:
            with self.subTest(entry=entry), self.assertRaises(ValueError):
                tone.normalize_job(entry, statistics(), "auto", 1)

    def test_legacy_manual_curves_remain_exact_with_independent_colour(self):
        points = [[0, 0], [128, 115], [255, 255]]
        curves, summary = tone.normalize_job({
            "curves": [{"ch": 0, "pts": points}], "adjustments": {"vibrance": 8},
        }, statistics(), "vivid", 1.5)
        self.assertEqual(curves, [{"ch": 0, "pts": points}])
        self.assertEqual(summary["mode"], "manual")
        self.assertEqual(summary["adjustments"], {"vibrance": 8})


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="photo-grading-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.icc = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()

    def image(self, name, colour, *, size=(64, 48)):
        path = self.root / name
        Image.new("RGB", size, colour).save(path, quality=100, subsampling=0, icc_profile=self.icc)
        return path

    def codes(self, before, after, **kwargs):
        return {item["code"] for item in review.assess_pair(before, after, change=8, **kwargs)}

    def test_review_identifies_new_clipping_and_saturation_jump(self):
        source = statistics()
        result = statistics(shadowClip=0.08, highlightClip=0.07,
                            channelClip={"red": 0.07, "green": 0.002, "blue": 0.002},
                            meanSaturation=0.5, saturatedFraction=0.15)
        codes = self.codes(source, result)
        self.assertIn("increased_shadowClip", codes)
        self.assertIn("increased_highlightClip", codes)
        self.assertIn("channel_clipping_red", codes)
        self.assertIn("saturation_jump", codes)
        self.assertNotIn("channel_clipping_green", codes)

    def test_existing_blown_highlights_are_not_a_new_export_risk(self):
        source = statistics(highlightClip=0.35,
                            channelClip={"red": 0.35, "green": 0.35, "blue": 0.35})
        for clipped in (0.35, 0.30):
            with self.subTest(clipped=clipped):
                result = statistics(highlightClip=clipped,
                                    channelClip={"red": clipped, "green": clipped, "blue": clipped})
                self.assertEqual(self.codes(source, result), set())

    def test_actual_neutral_pixels_reveal_an_unintentional_cast(self):
        source = self.image("neutral.jpg", (140, 140, 140))
        output = self.image("cast.jpg", (150, 140, 115))
        job = tone.Job(str(source), str(output), summary={"strength": 1, "adjustments": {}},
                       stats=tone.analyze_image(str(source)), status="exported")
        result = review.review_pair(job)
        self.assertGreater(result["neutral_shift"], 5)
        self.assertIn("neutral_colour_shift", {w["code"] for w in result["warnings"]})
        self.assertTrue(result["visual_review_required"])
        job.summary["adjustments"] = {"temperature": 12}
        intentional = review.review_pair(job)
        self.assertNotIn("neutral_colour_shift", {w["code"] for w in intentional["warnings"]})

    def test_colourful_scene_is_not_assumed_to_contain_neutral_pixels(self):
        before = Image.new("RGB", (64, 48), (160, 70, 25))
        after = Image.new("RGB", (64, 48), (185, 85, 35))
        self.assertIsNone(review._neutral_shift(before, after))

    def test_opposite_local_casts_cannot_cancel_in_neutral_check(self):
        before = Image.new("RGB", (64, 48))
        after = Image.new("RGB", (64, 48))
        before.paste((90, 90, 90), (0, 0, 32, 48))
        before.paste((200, 200, 200), (32, 0, 64, 48))
        after.paste((97, 90, 83), (0, 0, 32, 48))
        after.paste((193, 200, 207), (32, 0, 64, 48))
        shift = review._neutral_shift(before, after)
        self.assertGreater(shift, 5)
        self.assertIn("neutral_colour_shift", self.codes(statistics(), statistics(), neutral_shift=shift))

    def test_failed_export_does_not_review_an_existing_old_image(self):
        source = self.image("source.jpg", (130, 130, 130))
        old = self.image("old.jpg", (180, 180, 180))
        job = tone.Job(str(source), str(old), stats=tone.analyze_image(str(source)), status="failed")
        report_path, flags = review.write_review([job], str(self.root / "review"))
        record = json.loads(Path(report_path).read_text(encoding="utf-8"))["images"][0]
        self.assertEqual(record["status"], "failed")
        self.assertNotIn("after", record)
        self.assertEqual(flags, 1)

    def test_strict_review_makes_real_new_clipping_fail_the_cli(self):
        source_folder = self.root / "sources"
        source_folder.mkdir()
        self.image("sample.jpg", (130, 130, 130)).replace(source_folder / "sample.jpg")
        output_folder = self.root / "exports"

        def export(app, jsx):
            Image.new("RGB", (64, 48), "white").save(output_folder / "P_sample.jpg", icc_profile=self.icc)
            return "OK"

        with patch.object(tone, "photoshop_is_running", return_value=True), \
             patch.object(tone, "connect_photoshop", return_value=object()), \
             patch.object(tone, "run_jsx", side_effect=export), \
             patch.dict(sys.modules, {"pythoncom": types.SimpleNamespace(CoUninitialize=lambda: None)}), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = tone.main([str(source_folder), "--out", str(output_folder), "--strict-review"])
        self.assertEqual(code, 1)
        record = json.loads((output_folder / "_ps_review/review.json").read_text(encoding="utf-8"))["images"][0]
        self.assertIn("increased_highlightClip", {w["code"] for w in record["warnings"]})

    def test_review_detects_dark_subject_worsening_and_export_metadata(self):
        before = statistics(p50=60, meanL=70)
        after = statistics(meanL=45, size=[48, 64], hasICC=False)
        codes = self.codes(before, after)
        self.assertIn("dark_photo_darkened", codes)
        self.assertIn("dimensions_changed", codes)
        self.assertIn("missing_icc", codes)

    def test_zero_strength_does_not_warn_about_an_intentional_identity_export(self):
        warnings = review.assess_pair(statistics(), statistics(), change=0, strength=0)
        self.assertEqual(warnings, [])

    def test_preview_writes_inspectable_sources_and_plan_without_photoshop(self):
        source_folder = self.root / "sources"
        source_folder.mkdir()
        source = source_folder / "sample.jpg"
        Image.new("RGB", (64, 48), (130, 145, 160)).save(source, icc_profile=self.icc)
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        output_folder = self.root / "exports"
        review_folder = self.root / "review"
        with patch.object(tone, "connect_photoshop", side_effect=AssertionError("preview started PS")) as connect, \
             patch.object(tone, "photoshop_is_running", side_effect=AssertionError("preview touched PS")) as running, \
             patch.object(tone, "run_jsx", side_effect=AssertionError("preview executed JSX")) as execute, \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = tone.main([str(source_folder), "--out", str(output_folder),
                              "--preview-only", "--review-dir", str(review_folder)])
        self.assertEqual(code, 0)
        connect.assert_not_called()
        running.assert_not_called()
        execute.assert_not_called()
        self.assertFalse(output_folder.exists())
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), digest)
        report = json.loads((review_folder / "review.json").read_text(encoding="utf-8"))
        self.assertEqual(report["phase"], "source_preview")
        self.assertTrue(report["visual_review_required"])
        self.assertIn("not an aesthetic score", report["purpose"])
        self.assertEqual(report["images"][0]["status"], "preview_only")
        self.assertEqual(report["images"][0]["planned_settings"]["mode"], "natural")
        self.assertNotIn("aesthetic_score", report)
        with Image.open(report["contact_sheets"][0]) as sheet:
            sheet.verify()


if __name__ == "__main__":
    unittest.main()
