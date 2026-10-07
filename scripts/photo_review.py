"""Source previews and export risk checks, separate from visual aesthetic judgement."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageStat

import photoshop_tone as tone


PAGE_SIZE = 4


def artifact_paths(folder, count, preview=False):
    root = Path(folder)
    prefix = "sources" if preview else "comparison"
    return [root / "review.json"] + [root / (prefix + "_{0:03}.png".format(i + 1))
                                     for i in range(math.ceil(count / PAGE_SIZE))]


def validate_review_paths(folder, jobs, *, source_keys=None, source_ids=None):
    reserved = ({tone.path_key(j.src) for j in jobs} | {tone.path_key(j.out) for j in jobs}
                | set(source_keys or ()))
    protected_ids = set(source_ids or ())
    for job in jobs:
        for original in (job.src, job.out):
            if os.path.isfile(original):
                stat = os.stat(original)
                protected_ids.add((stat.st_dev, stat.st_ino))
    for path in artifact_paths(folder, len(jobs), False) + artifact_paths(folder, len(jobs), True):
        if tone.path_key(str(path)) in reserved:
            raise ValueError("review artifacts would replace a source/export: " + str(path))
        if path.exists() and protected_ids:
            stat = path.stat()
            if (stat.st_dev, stat.st_ino) in protected_ids:
                raise ValueError("review artifact is a hard link to an original source: " + str(path))
    if os.path.isfile(folder):
        raise ValueError("review directory is a file: " + folder)


def _neutral_shift(before, after, mask=None):
    """Compare the same neutral source pixels; do not infer white balance from whole-image means."""
    if before.size != after.size:
        return None
    source, result = [], []
    data_a = before.get_flattened_data() if hasattr(before, "get_flattened_data") else before.getdata()
    data_b = after.get_flattened_data() if hasattr(after, "get_flattened_data") else after.getdata()
    visible = iter(mask.get_flattened_data() if hasattr(mask, "get_flattened_data") else mask.getdata()) if mask else None
    for a, b in zip(data_a, data_b):
        if visible is not None and not next(visible):
            continue
        if max(a) - min(a) <= 10 and 48 <= sum(a) / 3 <= 235:
            source.append(a)
            result.append(b)
    if len(source) < max(32, before.width * before.height * 0.01):
        return None
    # Opposite local casts must not cancel when averaging R/G/B over the whole mask.
    gains = sorted(max(0, (max(b) - min(b)) - (max(a) - min(a)))
                   for a, b in zip(source, result))
    return round(gains[int(0.9 * (len(gains) - 1))], 2)


def assess_pair(source_stats, result_stats, *, change=0.0, neutral_shift=None,
                intentional_tint=False, strength=1.0):
    """Conservative review triggers. A warning requires inspection, not automatic rejection."""
    warnings = []
    if source_stats.get("size") != result_stats.get("size"):
        warnings.append({"code": "dimensions_changed", "action": "检查方向与尺寸；重新导出"})
    if not result_stats.get("hasICC", False):
        warnings.append({"code": "missing_icc", "action": "检查 sRGB 转换和嵌入配置"})
    for key, action in (("shadowClip", "减弱对比度或黑点；提亮主体暗部"),
                        ("highlightClip", "减弱提亮；增加高光压缩，检查白色背景是否合理")):
        if result_stats.get(key, 0) - source_stats.get(key, 0) > 0.01:
            warnings.append({"code": "increased_" + key, "action": action})
    for channel in ("red", "green", "blue"):
        if (result_stats.get("channelClip", {}).get(channel, 0)
                - source_stats.get("channelClip", {}).get(channel, 0) > 0.02):
            warnings.append({"code": "channel_clipping_" + channel,
                             "action": "检查该通道细节；降低自然饱和度、饱和度或色调强度"})
    if (result_stats.get("saturatedFraction", 0) - source_stats.get("saturatedFraction", 0) > 0.05
            or result_stats.get("meanSaturation", 0) - source_stats.get("meanSaturation", 0) > 0.15):
        warnings.append({"code": "saturation_jump", "action": "检查肤色、绿叶与天空；降低 vibrance/saturation"})
    if neutral_shift is not None and neutral_shift > 5 and not intentional_tint:
        warnings.append({"code": "neutral_colour_shift", "action": "复查灰白区域；取消非预期 temperature/tint"})
    if source_stats.get("p50", 128) < 105 and result_stats.get("meanL", 128) < source_stats.get("meanL", 128) - 5:
        warnings.append({"code": "dark_photo_darkened", "action": "检查主体；提亮中间调或改用 clean"})
    if strength > 0 and change < 1.2:
        warnings.append({"code": "minimal_visible_change", "action": "对比视觉效果；若问题仍存在，再调整曝光/对比度/色彩，勿只为增加差异加重效果"})
    return warnings


def review_pair(job):
    baseline = job.input_src or job.src
    before_stats = job.stats or tone.analyze_image(baseline)
    after_stats = tone.analyze_image(job.out)
    before = tone.load_rgb(baseline, background="#ffffff" if job.format == "jpg" else "#eeeeee")
    after = tone.load_rgb(job.out)
    _, source_alpha, source_info = tone.read_preview(baseline)
    _, result_alpha, _ = tone.read_preview(job.out)
    if source_alpha is not None and job.format == "jpg":
        # White is an intentional format conversion, not newly clipped photo detail.
        source_info = dict(source_info, hasAlpha=False)
        before_stats = tone.analyze_pixels(before, source_info)
    visible = source_alpha.point(lambda a: 255 if a else 0) if source_alpha is not None else None
    shift = _neutral_shift(before, after, visible)
    # Mean pixel change only detects a near-identity export; it is not a quality rating.
    resized = after.resize(before.size, Image.Resampling.LANCZOS)
    from PIL import ImageChops
    change = round(sum(ImageStat.Stat(ImageChops.difference(before, resized), mask=visible).mean) / 3, 3)
    colour = job.summary.get("adjustments", {})
    warnings = assess_pair(before_stats, after_stats, change=change, neutral_shift=shift,
                           intentional_tint=bool(colour.get("temperature") or colour.get("tint")),
                           strength=job.summary.get("strength", 1))
    expected_bits = job.summary.get("export", {}).get("bitDepth", before_stats.get("bitDepth", 8))
    if after_stats.get("bitDepth", 8) < expected_bits:
        warnings.append({"code": "bit_depth_reduced", "action": "检查导出格式和位深；PNG/TIFF 应保留输入精度"})
    alpha_change = None
    if source_alpha is not None and job.format != "jpg" and before_stats.get("size") == after_stats.get("size"):
        if result_alpha is None:
            result_alpha = Image.new("L", source_alpha.size, 255)
        result_alpha = result_alpha.resize(source_alpha.size, Image.Resampling.LANCZOS)
        alpha_diff = ImageChops.difference(source_alpha, result_alpha)
        alpha_change = round(ImageStat.Stat(alpha_diff).mean[0], 3)
        if alpha_diff.getextrema()[1] > 1:
            warnings.append({"code": "transparency_changed", "action": "检查透明边缘与背景；重新导出 PNG/TIFF"})
    return {"source": job.src, "output": job.out, "settings": job.summary,
            "source_preparation": job.source_metadata, "mean_alpha_change": alpha_change,
            "original_source_stats": job.stats,
            "curves": job.curves, "before": before_stats, "after": after_stats,
            "mean_pixel_change": change, "neutral_shift": shift,
            "neutral_shift_method": "90th percentile of same-pixel chroma-range increase",
            "status": "warn" if warnings else "pass", "warnings": warnings,
            "visual_review_required": True}


def _font(size):
    for name in (r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\arial.ttf"):
        if os.path.isfile(name):
            return ImageFont.truetype(name, size)
    return ImageFont.load_default()


def _tile(canvas, path, box, background="#eeeeee"):
    rgb = tone.load_rgb(path, 1100, background=background)
    left, top, width, height = box
    rgb.thumbnail((width, height), Image.Resampling.LANCZOS)
    canvas.paste(rgb, (left + (width - rgb.width) // 2, top + (height - rgb.height) // 2))


def contact_sheet(jobs, path, preview=False):
    """Same-scale before/after rows, four photographs per readable PNG page."""
    columns = 1 if preview else 2
    cell_w, cell_h, label_h = 620, 390, 62
    canvas = Image.new("RGB", (cell_w * columns, (cell_h + label_h) * len(jobs)), "#eeeeee")
    draw = ImageDraw.Draw(canvas)
    font = _font(17)
    for row, job in enumerate(jobs):
        y = row * (cell_h + label_h)
        label = "RAW render: " if Path(job.src).suffix.lower() in tone.RAW_EXTS else "Original: "
        draw.text((12, y + 8), label + Path(job.src).name[:58], fill="#111111", font=font)
        _tile(canvas, job.input_src or job.src, (8, y + label_h, cell_w - 16, cell_h - 8),
              background="#ffffff" if not preview and job.format == "jpg" else "#eeeeee")
        if not preview:
            colour = job.summary.get("adjustments", {})
            label = "Edited: {0} | strength {1} | vibrance {2}".format(
                job.summary.get("mode"), job.summary.get("strength", "manual"), colour.get("vibrance", 0))
            draw.text((cell_w + 12, y + 8), label, fill="#111111", font=font)
            draw.text((cell_w + 12, y + 32), "temperature {0} | tint {1}".format(
                colour.get("temperature", 0), colour.get("tint", 0)), fill="#444444", font=font)
            if os.path.isfile(job.out) and job.status != "failed":
                _tile(canvas, job.out, (cell_w + 8, y + label_h, cell_w - 16, cell_h - 8))
            else:
                draw.text((cell_w + 20, y + 150), "Export failed / unavailable", fill="#aa2222", font=font)
    canvas.save(path)


def write_review(jobs, folder, preview=False):
    validate_review_paths(folder, jobs)
    root = Path(folder)
    root.mkdir(parents=True, exist_ok=True)
    records = []
    for job in jobs:
        if preview:
            records.append({"source": job.src, "planned_output": job.out, "before": job.stats,
                            "source_preparation": job.source_metadata,
                            "planned_settings": job.summary, "curves": job.curves,
                            "status": "preview_only", "visual_review_required": True})
        elif job.status == "failed" or not os.path.isfile(job.out):
            records.append({"source": job.src, "output": job.out, "status": "failed",
                            "warnings": [{"code": "export_failed", "action": "修复导出错误；不可用旧输出代替本次结果"}]})
        else:
            records.append(review_pair(job))
    paths = artifact_paths(folder, len(jobs), preview)
    for i, sheet in enumerate(paths[1:]):
        contact_sheet(jobs[i * PAGE_SIZE:(i + 1) * PAGE_SIZE], sheet, preview)
    payload = {"purpose": "Technical risk checks and visual comparison; not an aesthetic score",
               "phase": "source_preview" if preview else "after_export",
               "visual_review_required": True, "contact_sheets": [str(p) for p in paths[1:]], "images": records}
    paths[0].write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    flagged = sum(r["status"] in ("warn", "failed") for r in records)
    return str(paths[0]), flagged
