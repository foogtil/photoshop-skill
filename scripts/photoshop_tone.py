#!/usr/bin/env python3
"""Adaptive photo colour grading in real Adobe Photoshop, with export review.

Uses Adobe Photoshop's Windows COM automation and ExtendScript.

Pipeline
--------
1. Preview and analyse tone, saturation and clipping with Pillow.
2. Combine adaptive exposure with per-photo look and explicit colour choices.
3. Build a JSX script and execute it through Photoshop's COM `DoJavaScript`.
4. Each image is opened (Photoshop auto-applies EXIF orientation), converted to
   sRGB, given one Curves adjustment layer per channel, then saved as a JPEG
   copy, then write before/after comparisons and a technical-risk report.

Usage
-----
    python photoshop_tone.py INPUT_DIR [--out DIR] [--prefix P_] [--suffix ""]
                                  [--quality 12] [--mode MODE] [--strength 0..1.5]
                                  [--jobs jobs.json] [--recursive] [--limit N]
                                  [--dry-run|--preview-only] [--review-dir DIR]
                                  [--strict-review] [--overwrite] [--quit|--keep-open]

Requires: pywin32, Pillow  (pip install pywin32 Pillow)
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from io import BytesIO
from typing import Optional
from urllib.parse import unquote

try:
    from PIL import Image, ImageCms, ImageOps, ImageStat
except ImportError:  # pragma: no cover
    sys.stderr.write("Pillow is required: pip install Pillow\n")
    raise

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PHOTOSHOP_PROGID = "Photoshop.Application"

# HResult raised when Photoshop is busy (a modal dialog / long task). Retry.
RPC_E_SERVERCALL_RETRYLATER = -2147417846

IMAGE_EXTS = {".jpg", ".jpeg"}

# Creative looks are separate from exposure diagnosis. Neutral is the default.
MODES = {
    "auto": {},
    "natural": {"contrast": 1.10, "vibrance": 18, "saturation": 0, "temperature": 0, "fade": 0},
    "clean": {"contrast": 1.05, "vibrance": 10, "saturation": 0, "temperature": 0, "fade": 0},
    "vivid": {"contrast": 1.22, "vibrance": 32, "saturation": 3, "temperature": 0, "fade": 0},
    "warm": {"contrast": 1.10, "vibrance": 16, "saturation": 0, "temperature": 12, "fade": 0},
    "film": {"contrast": 1.07, "vibrance": 6, "saturation": -7, "temperature": 5, "fade": 7},
}


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


# ---------------------------------------------------------------------------
# Histogram analysis (Pillow)
# ---------------------------------------------------------------------------

def load_rgb(path: str, sample: int = 512):
    """ICC-aware, orientation-correct thumbnail detached from its source file."""
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im)
        if im.info.get("icc_profile"):
            try:
                im = ImageCms.profileToProfile(
                    im, ImageCms.ImageCmsProfile(BytesIO(im.info["icc_profile"])),
                    ImageCms.createProfile("sRGB"), renderingIntent=1, outputMode="RGB")
            except ImageCms.PyCMSError as exc:
                raise ValueError("Cannot convert embedded ICC profile: {0}".format(exc)) from exc
        else:
            im = im.convert("RGB")
        im.thumbnail((sample, sample), Image.Resampling.LANCZOS)
        return im.copy()


def analyze_image(path: str, sample: int = 512) -> dict:
    """Measure tone/colour risk; these statistics are not an aesthetic score."""
    im = load_rgb(path, sample)
    hist = im.convert("L").histogram()
    mean_r, mean_g, mean_b = ImageStat.Stat(im).mean

    n = sum(hist)
    targets = {"p01": 0.01, "p05": 0.05, "p25": 0.25, "p50": 0.50,
               "p75": 0.75, "p95": 0.95, "p99": 0.99}
    ordered = sorted(targets.items(), key=lambda kv: kv[1])

    out = {}
    cum = 0
    ti = 0
    for i in range(256):
        cum += hist[i]
        while ti < len(ordered) and (cum / n) >= ordered[ti][1]:
            out[ordered[ti][0]] = i
            ti += 1

    out["meanR"] = round(mean_r, 1)
    out["meanG"] = round(mean_g, 1)
    out["meanB"] = round(mean_b, 1)
    out["mean"] = round((mean_r + mean_g + mean_b) / 3.0, 1)
    out["meanL"] = round(sum(i * count for i, count in enumerate(hist)) / n, 2)
    sat = im.convert("HSV").getchannel("S").histogram()
    out["meanSaturation"] = round(sum(i * count for i, count in enumerate(sat)) / (n * 255), 4)
    out["saturatedFraction"] = round(sum(sat[245:]) / n, 5)
    out["shadowClip"] = round(sum(hist[:4]) / n, 5)
    out["highlightClip"] = round(sum(hist[252:]) / n, 5)
    out["channelClip"] = {
        label: round((sum(ch.histogram()[:2]) + sum(ch.histogram()[254:])) / n, 5)
        for label, ch in zip(("red", "green", "blue"), im.split())
    }
    with Image.open(path) as original:
        out["size"] = list(ImageOps.exif_transpose(original).size)
        out["hasICC"] = bool(original.info.get("icc_profile"))
    return out


# ---------------------------------------------------------------------------
# Curve derivation
# ---------------------------------------------------------------------------

def tone_curve_points(black: float, white: float, gamma: float, contrast: float):
    """Emulate a Levels/Curves correction: clamp to [black,white], apply gamma,
    then contrast around mid-grey. Returns ActionManager curve points in 0..255."""
    if not (0 <= black < white <= 255 and white - black >= 8):
        raise ValueError("tone requires 0 <= black < white <= 255 and a range of at least 8")
    if not (math.isfinite(gamma) and gamma > 0 and math.isfinite(contrast) and contrast > 0):
        raise ValueError("gamma and contrast must be finite positive numbers")
    black, white = int(round(black)), int(round(white))
    pts = [[0, 0]]
    if black > 0:
        pts.append([black, 0])
    for k in range(1, 7):
        x = black + (white - black) * k / 7.0
        t = (x - black) / (white - black)
        y = 255.0 * (t ** (1.0 / gamma))
        y = 128 + (y - 128) * contrast
        xi = int(round(x))
        if pts[-1][0] < xi < white:
            pts.append([xi, int(round(clamp(y, 0, 255)))])
    pts.append([white, 255])
    if white < 255:
        pts.append([255, 255])
    return pts


def validate_adjustments(settings: dict) -> dict:
    """Vibrance/saturation use Photoshop units; temperature/tint are bounded RGB shifts."""
    if not isinstance(settings, dict):
        raise ValueError("adjustments must be an object")
    limits = {"vibrance": 100, "saturation": 100, "temperature": 30, "tint": 30}
    if set(settings) - set(limits):
        raise ValueError("adjustments support vibrance, saturation, temperature and tint")
    out = {}
    for key, value in settings.items():
        if (isinstance(value, bool) or not isinstance(value, (float, int))
                or not math.isfinite(value) or abs(value) > limits[key]):
            raise ValueError("{0} must be finite and within +/-{1}".format(key, limits[key]))
        out[key] = round(value)
    return out


def colour_curves(settings: dict):
    """Optional warmth/tint. Endpoints stay neutral; no unconditional blue whites."""
    temperature = settings.get("temperature", 0)
    tint = settings.get("tint", 0)
    shifts = (temperature * 0.65 + tint * 0.25, -tint * 0.5,
              -temperature * 0.65 + tint * 0.25)
    curves = []
    for ch, shift in enumerate(shifts, 1):
        if abs(shift) < 0.25:
            continue
        pts = [[x, int(round(clamp(x + shift * math.sin(math.pi * x / 255), 0, 255)))]
               for x in (0, 48, 96, 160, 208, 255)]
        curves.append({"ch": ch, "pts": pts})
    return curves


def derive_curves(stats: dict, mode: str = "auto", strength: float = 1.0):
    """Adaptive exposure plus a selectable look; automatic tone never clips percentile tails."""
    if not isinstance(mode, str) or mode not in MODES:
        raise ValueError("unknown mode: " + str(mode))
    if (isinstance(strength, bool) or not isinstance(strength, (int, float))
            or not math.isfinite(strength) or not 0 <= strength <= 1.5):
        raise ValueError("strength must be finite and within 0..1.5")
    active = "natural" if mode == "auto" else mode
    preset = MODES[active]
    median = stats["p50"]
    spread = stats["p75"] - stats["p25"]
    gamma = 1.0
    if median < 105:
        gamma += clamp((110 - median) / 240, 0, 0.28)
    elif median > 180:
        gamma -= clamp((median - 170) / 360, 0, 0.20)
    if active == "clean":
        gamma = max(gamma, 1.0) + 0.05 if median < 180 else gamma
    contrast = preset["contrast"]
    if spread < 70:
        contrast += 0.04
    elif spread > 145:
        contrast = 1 + (contrast - 1) * 0.65
    # Sparse highlights and bright/high-key scenes get a soft shoulder, not a hard black point.
    shadow_lift = clamp((75 - stats["p25"]) * 0.18, 0, 12)
    highlight_compression = clamp((stats["p75"] - 190) * 0.32, 0, 20)
    if active == "film":
        highlight_compression = max(8, highlight_compression)
    pts = []
    for x in (0, 16, 32, 64, 96, 128, 160, 192, 224, 240, 255):
        u = x / 255
        y = 255 * u ** (1 / gamma)
        y += (contrast - 1) * 255 * (u - 0.5) * u * (1 - u) * 4
        y += shadow_lift * 9.5 * u * (1 - u) ** 3
        y -= highlight_compression * 9.5 * u ** 3 * (1 - u)
        y += preset["fade"] * (1 - u) ** 2
        y = int(round(clamp(x + strength * (y - x), 0, 255)))
        if pts:
            y = max(pts[-1][1], y)
        pts.append([x, y])
    adjustments = {key: int(round(preset[key] * strength))
                   for key in ("vibrance", "saturation", "temperature")}
    # Already colourful photographs need less additional colour, regardless of the look.
    if stats.get("meanSaturation", 0) > 0.45 or stats.get("saturatedFraction", 0) > 0.15:
        adjustments["vibrance"] = int(round(adjustments["vibrance"] * 0.35))
        adjustments["saturation"] = min(0, adjustments["saturation"])
    # A true monochrome input remains monochrome unless warmth is intentionally selected.
    if stats.get("meanSaturation", 1) < 0.015 and active not in ("warm", "film"):
        adjustments["vibrance"] = adjustments["saturation"] = 0
    summary = {
        "mode": active, "strength": strength, "black": pts[0][1], "white": pts[-1][1],
        "gamma": round(gamma, 3), "contrast": round(contrast, 3),
        "shadow_lift": round(shadow_lift, 2),
        "highlight_compression": round(highlight_compression, 2),
        "adjustments": adjustments,
        "diagnosis": {
            "brightness": "dark" if median < 105 else "bright" if median > 180 else "balanced",
            "contrast": "flat" if spread < 70 else "wide" if spread > 145 else "moderate",
            "highlights_already_clipped": stats.get("highlightClip", 0) > 0.01,
        },
    }
    return [{"ch": 0, "pts": pts}], summary


def normalize_manual(entry: dict):
    """Build curve specs from a jobs.json entry (tone tuple or explicit points)."""
    if not isinstance(entry, dict):
        raise ValueError("manual job must be an object")
    if set(entry) - {"tone", "channels", "curves"}:
        raise ValueError("unknown job fields; use tone/channels or curves")
    if "curves" in entry:
        if "tone" in entry or "channels" in entry:
            raise ValueError("use either curves or tone/channels, not both")
        curves = entry["curves"]
        if not isinstance(curves, list):
            raise ValueError("curves must be a list")
        return validate_curves(curves)
    tone = entry.get("tone")
    channels = entry.get("channels", {})
    if not isinstance(channels, dict):
        raise ValueError("channels must be an object")
    curves = []
    if tone is not None:
        if not isinstance(tone, list) or len(tone) != 4 or any(
            isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
            for v in tone
        ):
            raise ValueError("tone must be four finite numbers [black, white, gamma, contrast]")
        curves.append({"ch": 0, "pts": tone_curve_points(*tone)})
    for ch, pts in channels.items():
        if str(ch) not in ("0", "1", "2", "3"):
            raise ValueError("channel must be 0, 1, 2, or 3")
        curves.append({"ch": int(ch), "pts": pts})
    return validate_curves(curves)


def normalize_job(entry: dict, stats: dict, mode: str, strength: float):
    """Keep legacy manual curves exact; allow independent per-photo creative decisions."""
    if not isinstance(entry, dict):
        raise ValueError("job must be an object")
    allowed = {"tone", "channels", "curves", "mode", "strength", "adjustments", "reason"}
    if set(entry) - allowed:
        raise ValueError("unknown job fields: " + ", ".join(sorted(set(entry) - allowed)))
    if "reason" in entry and not isinstance(entry["reason"], str):
        raise ValueError("reason must be text")
    manual = {key: entry[key] for key in ("tone", "channels", "curves") if key in entry}
    if manual:
        if "mode" in entry or "strength" in entry:
            raise ValueError("manual curves cannot be combined with mode/strength")
        curves = normalize_manual(manual)
        summary = {"mode": "manual", "adjustments": {}}
    else:
        curves, summary = derive_curves(stats, entry.get("mode", mode), entry.get("strength", strength))
    summary["adjustments"].update(validate_adjustments(entry.get("adjustments", {})))
    if entry.get("reason"):
        summary["reason"] = entry["reason"]
    return curves, summary


def validate_curves(curves: list) -> list:
    """Reject malformed manual curves before sending any work to Photoshop."""
    if not curves:
        raise ValueError("at least one curve is required")
    seen = set()
    validated = []
    for spec in curves:
        if not isinstance(spec, dict) or set(spec) != {"ch", "pts"}:
            raise ValueError("each curve needs ch and pts")
        ch, pts = spec["ch"], spec["pts"]
        if type(ch) is not int or ch not in range(4) or ch in seen:
            raise ValueError("channels must be unique integers from 0 to 3")
        if not isinstance(pts, list) or not 2 <= len(pts) <= 16:
            raise ValueError("each curve needs 2 to 16 points")
        clean = []
        last_x = -1
        for point in pts:
            if not isinstance(point, list) or len(point) != 2 or any(
                isinstance(v, bool) or not isinstance(v, (int, float)) or
                not math.isfinite(v) or not 0 <= v <= 255 for v in point
            ):
                raise ValueError("curve points must be [x, y] numbers in 0..255")
            x, y = point
            if x <= last_x:
                raise ValueError("curve input x values must increase strictly")
            clean.append([x, y])
            last_x = x
        seen.add(ch)
        validated.append({"ch": ch, "pts": clean})
    return validated


# ---------------------------------------------------------------------------
# Photoshop COM + JSX
# ---------------------------------------------------------------------------

JSX_HEADER = r"""
function sT(s){ return app.stringIDToTypeID(s); }
function _curves(chIdx, pts){
  var desc = new ActionDescriptor();
  var ref = new ActionReference();
  ref.putClass(sT("adjustmentLayer"));
  desc.putReference(sT("null"), ref);
  var layer = new ActionDescriptor();
  var adj = new ActionDescriptor();
  adj.putEnumerated(sT("presetKind"), sT("presetKindType"), sT("presetKindCustom"));
  var list = new ActionList();
  var chan = new ActionDescriptor();
  var channelRef = new ActionReference();
  channelRef.putEnumerated(sT("channel"), sT("channel"),
    sT(["composite", "red", "green", "blue"][chIdx]));
  chan.putReference(sT("channel"), channelRef);
  var pl = new ActionList();
  for (var i=0;i<pts.length;i++){
    var p = new ActionDescriptor();
    p.putDouble(sT("horizontal"), pts[i][0]);
    p.putDouble(sT("vertical"), pts[i][1]);
    pl.putObject(sT("point"), p);
  }
  chan.putList(sT("curve"), pl);
  list.putObject(sT("curvesAdjustment"), chan);
  adj.putList(sT("adjustment"), list);
  layer.putObject(sT("type"), sT("curves"), adj);
  desc.putObject(sT("using"), sT("adjustmentLayer"), layer);
  executeAction(sT("make"), desc, DialogModes.NO);
}
function _vibrance(amount, saturation){
  var desc = new ActionDescriptor(), ref = new ActionReference();
  ref.putClass(sT("adjustmentLayer"));
  desc.putReference(sT("null"), ref);
  var layer = new ActionDescriptor(), adj = new ActionDescriptor();
  adj.putInteger(sT("vibrance"), amount);
  adj.putInteger(sT("saturation"), saturation);
  layer.putObject(sT("type"), sT("vibrance"), adj);
  desc.putObject(sT("using"), sT("adjustmentLayer"), layer);
  executeAction(sT("make"), desc, DialogModes.NO);
  app.activeDocument.activeLayer.name = "Colour - Vibrance";
}
function _process(srcPath, outPath, quality, curveSpecs, colour, luminanceOnly, overwrite, log){
  var doc = null;
  try {
    var srcFile = new File(srcPath), outFile = new File(outPath);
    if (!overwrite && outFile.exists) throw new Error("Output already exists");
    for (var d=0; d<app.documents.length; d++){
      var openPath = null;
      try { openPath = app.documents[d].fullName.fsName; } catch(noPath){}
      if (openPath && openPath.toLowerCase() == srcFile.fsName.toLowerCase())
        throw new Error("Source is already open in Photoshop; close it before batching");
    }
    doc = app.open(srcFile);
    if (doc.mode != DocumentMode.RGB) doc.changeMode(ChangeMode.RGB);
    doc.bitsPerChannel = BitsPerChannelType.EIGHT;
    doc.convertProfile("sRGB IEC61966-2.1", Intent.RELATIVECOLORIMETRIC, true, true);
    for (var c=0;c<curveSpecs.length;c++){
      _curves(curveSpecs[c][0], curveSpecs[c][1]);
      if (curveSpecs[c][0] == 0){
        doc.activeLayer.name = "Tone - Adaptive Curves";
        if (luminanceOnly) doc.activeLayer.blendMode = BlendMode.LUMINOSITY;
      } else { doc.activeLayer.name = "Colour - Channel " + curveSpecs[c][0]; }
    }
    if (colour.vibrance || colour.saturation) _vibrance(colour.vibrance || 0, colour.saturation || 0);
    var opts = new JPEGSaveOptions();
    opts.quality = quality;
    opts.embedColorProfile = true;
    doc.saveAs(outFile, opts, true, Extension.LOWERCASE);
    doc.close(SaveOptions.DONOTSAVECHANGES);
    doc = null;
    log.push("OK");
  } catch(err){
    log.push("ERR:" + encodeURIComponent(String(err.message || err)));
    try { if (doc) doc.close(SaveOptions.DONOTSAVECHANGES); } catch(e2){}
  }
}
var _log = [];
"""


def js_lit(value) -> str:
    """Escape Unicode line separators for the older ExtendScript parser too."""
    return json.dumps(value, ensure_ascii=True, allow_nan=False)


def build_jsx(jobs, quality: int, overwrite: bool = False) -> str:
    parts = ["(function(){", JSX_HEADER, r"""
var previousDialogs = app.displayDialogs;
var previousDoc = app.documents.length ? app.activeDocument : null;
try {
app.displayDialogs = DialogModes.NO;
"""]
    for job in jobs:
        settings = validate_adjustments(job.get("adjustments", {}))
        specs = [[c["ch"], c["pts"]] for c in job["curves"] + colour_curves(settings)]
        parts.append(
            "_process({0}, {1}, {2}, {3}, {4}, {5}, {6}, _log);\n".format(
                js_lit(job["src"]), js_lit(job["out"]), int(quality), js_lit(specs),
                js_lit(settings), js_lit(job.get("luminance_only", False)), js_lit(overwrite)
            )
        )
    parts.append(r"""
return _log.join(String.fromCharCode(10));
} finally {
  app.displayDialogs = previousDialogs;
  if (previousDoc) { try { app.activeDocument = previousDoc; } catch(restoreError){} }
}
})();
""")
    return "".join(parts)


def photoshop_is_running() -> bool:
    """Check Windows processes without starting Photoshop or relying on COM registration."""
    import ctypes
    from ctypes import wintypes

    class ProcessEntry(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    for name in ("Process32FirstW", "Process32NextW"):
        fn = getattr(kernel, name)
        fn.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
        fn.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)  # TH32CS_SNAPPROCESS
    if snapshot == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        entry = ProcessEntry()
        entry.dwSize = ctypes.sizeof(entry)
        available = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while available:
            if entry.szExeFile.casefold() == "photoshop.exe":
                return True
            available = kernel.Process32NextW(snapshot, ctypes.byref(entry))
        error = ctypes.get_last_error()
        if error != 18:  # ERROR_NO_MORE_FILES
            raise ctypes.WinError(error)
        return False
    finally:
        kernel.CloseHandle(snapshot)


def quit_photoshop_if_idle(app) -> bool:
    """Request normal application exit, preserving any documents still open."""
    try:
        if int(run_jsx(app, "app.documents.length;")):
            sys.stderr.write("Photoshop left open because documents are still open\n")
            return True
        app.Quit()
        return True
    except Exception as exc:
        sys.stderr.write("Could not quit Photoshop: {0}\n".format(exc))
        return False


def connect_photoshop():
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    try:
        return win32com.client.Dispatch(PHOTOSHOP_PROGID)
    except Exception:
        pythoncom.CoUninitialize()
        raise


def run_jsx(app, jsx: str, retries: int = 8, delay: float = 2.0):
    """Execute JSX, retrying while Photoshop reports it is busy."""
    import pythoncom

    if retries < 1 or delay < 0:
        raise ValueError("retries must be positive and delay nonnegative")
    last = None
    for attempt in range(retries):
        try:
            return app.DoJavaScript(jsx)
        except pythoncom.com_error as exc:  # type: ignore[attr-defined]
            hresult = getattr(exc, "hresult", None)
            if hresult is None and exc.args:
                hresult = exc.args[0]
            if hresult == RPC_E_SERVERCALL_RETRYLATER:
                last = exc
                if attempt + 1 < retries:
                    time.sleep(delay)
                continue
            raise
    raise last  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Job assembly + CLI
# ---------------------------------------------------------------------------

@dataclass
class Job:
    src: str
    out: str
    curves: list = field(default_factory=list)
    summary: dict = field(default_factory=dict)
    stats: dict = field(default_factory=dict)
    status: str = "planned"


def is_within(path: str, folder: str) -> bool:
    path, folder = path_key(path), path_key(folder)
    try:
        return os.path.commonpath((path, folder)) == folder
    except ValueError:  # Different Windows drives.
        return False


def path_key(path: str) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def collect_images(folder: str, recursive: bool, prefix: str, suffix: str,
                   excluded_dir: Optional[str] = None):
    found = []
    if recursive:
        for root, dirs, files in os.walk(folder):
            if excluded_dir:
                dirs[:] = [d for d in dirs if not is_within(os.path.join(root, d), excluded_dir)]
            for name in files:
                if os.path.splitext(name)[1].lower() in IMAGE_EXTS:
                    found.append(os.path.join(root, name))
    else:
        for name in os.listdir(folder):
            p = os.path.join(folder, name)
            if os.path.isfile(p) and os.path.splitext(name)[1].lower() in IMAGE_EXTS:
                found.append(p)
    result = []
    for p in sorted(found):
        base = os.path.basename(p)
        stem = os.path.splitext(base)[0]
        if prefix and stem.startswith(prefix):
            continue
        if suffix and stem.endswith(suffix):
            continue
        result.append(p)
    return result


def out_name(base: str, prefix: str, suffix: str) -> str:
    stem = os.path.splitext(base)[0]
    return "{0}{1}{2}.jpg".format(prefix, stem, suffix)


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="Photo colour grading via Photoshop, with before/after review.")
    ap.add_argument("input", help="folder containing source images")
    ap.add_argument("--out", default=None, help="output folder (default: same as input)")
    ap.add_argument("--prefix", default="P_", help="filename prefix for outputs (default: P_)")
    ap.add_argument("--suffix", default="", help="filename suffix for outputs (default: none)")
    ap.add_argument("--quality", type=int, default=12, help="JPEG quality 1-12 (default 12)")
    ap.add_argument("--mode", default="auto", choices=sorted(MODES.keys()))
    ap.add_argument("--strength", type=float, default=1.0, help="automatic look intensity 0..1.5 (default 1)")
    ap.add_argument("--jobs", default=None, help="JSON file of per-image parameters")
    ap.add_argument("--recursive", action="store_true", help="recurse into subfolders")
    ap.add_argument("--limit", type=int, default=0, help="process at most N images (0 = all)")
    ap.add_argument("--dry-run", action="store_true", help="print derived params, do not run Photoshop")
    ap.add_argument("--preview-only", action="store_true", help="write source contact sheets and plan; do not start Photoshop")
    ap.add_argument("--review-dir", help="write source/comparison PNGs and review JSON here (default output/_ps_review)")
    ap.add_argument("--strict-review", action="store_true", help="return 1 for objective review warnings; still visually inspect results")
    ap.add_argument("--overwrite", action="store_true", help="replace existing output images")
    lifetime = ap.add_mutually_exclusive_group()
    lifetime.add_argument("--quit", action="store_true", help="quit even a pre-existing Photoshop session if no documents remain")
    lifetime.add_argument("--keep-open", action="store_true", help="leave Photoshop open even if this script started it")
    return ap.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    if not 1 <= args.quality <= 12:
        sys.stderr.write("--quality must be between 1 and 12\n")
        return 2
    if args.limit < 0:
        sys.stderr.write("--limit must be nonnegative\n")
        return 2
    if not math.isfinite(args.strength) or not 0 <= args.strength <= 1.5:
        sys.stderr.write("--strength must be finite and between 0 and 1.5\n")
        return 2
    if args.dry_run and args.preview_only:
        sys.stderr.write("Use --dry-run or --preview-only, not both\n")
        return 2
    if any(c in '<>:"/\\|?*' or ord(c) < 32 for c in args.prefix + args.suffix):
        sys.stderr.write("--prefix and --suffix must be filename text, without path separators or reserved characters\n")
        return 2
    if not args.prefix and not args.suffix and (not args.out or
        os.path.normcase(os.path.abspath(args.out)) == os.path.normcase(os.path.abspath(args.input))):
        sys.stderr.write("Use --prefix, --suffix, or a different --out to protect source images\n")
        return 2
    if not os.path.isdir(args.input):
        sys.stderr.write("Input folder not found: {0}\n".format(args.input))
        return 2

    out_dir = os.path.abspath(args.out or args.input)
    input_dir = os.path.abspath(args.input)
    excluded_dir = out_dir if path_key(out_dir) != path_key(input_dir) and is_within(out_dir, input_dir) else None
    files = collect_images(input_dir, args.recursive, args.prefix, args.suffix, excluded_dir)
    # Protect every scanned original, including those outside the current sample limit.
    source_keys = {path_key(src) for src in files}
    try:
        source_ids = {(st.st_dev, st.st_ino) for st in (os.stat(src) for src in files)}
    except OSError as exc:
        sys.stderr.write("Cannot inspect source files: {0}\n".format(exc))
        return 2
    if args.limit:
        files = files[: args.limit]
    if not files:
        sys.stderr.write("No source images found in {0}\n".format(args.input))
        return 1

    manual = {}
    if args.jobs:
        try:
            with open(args.jobs, "r", encoding="utf-8-sig") as fh:
                manual = json.load(fh)
            if not isinstance(manual, dict):
                raise ValueError("jobs JSON must be an object")
        except (OSError, ValueError) as exc:
            sys.stderr.write("Invalid --jobs: {0}\n".format(exc))
            return 2

    jobs = []
    basenames = Counter(os.path.basename(src) for src in files)
    destinations = set()
    skipped = 0
    for src in files:
        base = os.path.basename(src)
        relative = os.path.relpath(os.path.dirname(src), input_dir)
        dst_dir = os.path.join(out_dir, relative)
        dst = os.path.join(dst_dir, out_name(base, args.prefix, args.suffix))
        dst_key = path_key(dst)
        dst_stat = os.stat(dst) if os.path.exists(dst) else None
        if (dst_key in source_keys or (dst_stat is not None
                and (dst_stat.st_dev, dst_stat.st_ino) in source_ids)):
            sys.stderr.write("Output would replace an original source: {0}\n".format(dst))
            return 2
        if dst_key in destinations:
            sys.stderr.write("Two inputs have the same output: {0}\n".format(dst))
            return 2
        destinations.add(dst_key)
        if os.path.exists(dst) and not args.overwrite and not args.preview_only:
            print("Skipping existing output: {0}".format(dst))
            skipped += 1
            continue
        relative_key = os.path.relpath(src, input_dir).replace(os.sep, "/")
        key = relative_key if relative_key in manual else base
        try:
            if key == base and key in manual and basenames[base] > 1:
                raise ValueError("ambiguous jobs key; use input-relative paths such as subfolder/name.jpg")
            stats = analyze_image(src)
            curves, summary = normalize_job(manual.get(key, {}), stats, args.mode, args.strength)
        except (OSError, ValueError) as exc:
            sys.stderr.write("Cannot prepare {0}: {1}\n".format(src, exc))
            return 2
        jobs.append(Job(src=src, out=dst, curves=curves, summary=summary, stats=stats))

    if not jobs:
        print("No new images to process ({0} existing output(s) skipped).".format(skipped))
        return 0

    for job in jobs:
        s = job.summary
        print("{0:<14} mode={1:<7} black={2} white={3} gamma={4} contrast={5} -> {6}".format(
            os.path.basename(job.src), s.get("mode", "-"), s.get("black", "-"),
            s.get("white", "-"), s.get("gamma", "-"), s.get("contrast", "-"),
            job.out))
        print("  colour={0} strength={1} diagnosis={2}".format(
            json.dumps(s.get("adjustments", {}), ensure_ascii=False), s.get("strength", "manual"),
            json.dumps(s.get("diagnosis", {}), ensure_ascii=False)))

    if args.dry_run:
        print("\n[dry-run] {0} image(s) would be processed.".format(len(jobs)))
        return 0

    review_dir = os.path.abspath(args.review_dir or os.path.join(out_dir, "_ps_review"))
    # Diagnostics must never overwrite a source or any intended export.
    from photo_review import validate_review_paths, write_review
    try:
        validate_review_paths(review_dir, jobs)
        if args.preview_only:
            report, _ = write_review(jobs, review_dir, preview=True)
            print("Source previews and plan: " + report)
            return 0
    except (OSError, ValueError) as exc:
        sys.stderr.write("Cannot prepare review: {0}\n".format(exc))
        return 2

    app = None
    started_here = False
    exit_code = 1
    try:
        for job in jobs:
            os.makedirs(os.path.dirname(job.out), exist_ok=True)
        started_here = not photoshop_is_running()
        app = connect_photoshop()
        jsx = build_jsx([{"src": j.src, "out": j.out, "curves": j.curves,
                          "adjustments": j.summary.get("adjustments", {}),
                          "luminance_only": j.summary.get("mode") != "manual"} for j in jobs],
                        args.quality, args.overwrite)
        result = run_jsx(app, jsx)
        lines = str(result).splitlines() if result is not None else []
        if len(lines) != len(jobs):
            raise RuntimeError("Photoshop returned {0} statuses for {1} images".format(len(lines), len(jobs)))
        failures = 0
        for job, line in zip(jobs, lines):
            if line == "OK" and os.path.isfile(job.out) and os.path.getsize(job.out) > 0:
                job.status = "exported"
                print("OK  {0} -> {1}".format(job.src, job.out))
            elif line == "OK":
                job.status = "failed"
                failures += 1
                print("ERR Output missing or empty: {0}".format(job.out), file=sys.stderr)
            elif line.startswith("ERR:"):
                job.status = "failed"
                failures += 1
                print("ERR {0}: {1}".format(job.src, unquote(line[4:])), file=sys.stderr)
            else:
                raise RuntimeError("Unexpected Photoshop status: {0}".format(line))
        exit_code = 1 if failures else 0
        report, flagged = write_review(jobs, review_dir)
        print("Review: {0} ({1} flagged image(s); visual comparison still required)".format(report, flagged))
        if args.strict_review and flagged:
            exit_code = 1
    except Exception as exc:
        sys.stderr.write("Photoshop automation failed: {0}\n".format(exc))
        exit_code = 1
    finally:
        if app is not None:
            if args.quit or (started_here and not args.keep_open):
                if not quit_photoshop_if_idle(app):
                    exit_code = 1
            import pythoncom
            app = None
            pythoncom.CoUninitialize()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
