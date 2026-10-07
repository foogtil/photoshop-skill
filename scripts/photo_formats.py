"""Shared image decoding and private raster inputs for Photoshop photo grading."""
from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
import tempfile

from PIL import Image, ImageCms, ImageOps


RAW_EXTS = {".dng", ".nef", ".nrw", ".cr2", ".cr3", ".crw", ".arw", ".srf", ".sr2",
            ".raf", ".orf", ".pef", ".ptx", ".rw2", ".rwl", ".raw", ".srw", ".3fr",
            ".fff", ".iiq", ".kdc", ".dcr", ".mos", ".mef", ".erf", ".mrw"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp"} | RAW_EXTS


def choose_output_format(path, requested="auto"):
    aliases = {"jpeg": "jpg", "tif": "tiff"}
    requested = aliases.get(str(requested).lower(), str(requested).lower())
    if requested != "auto":
        if requested not in ("jpg", "png", "tiff"):
            raise ValueError("output format must be auto, jpg, png or tiff")
        return requested
    ext = Path(path).suffix.lower()
    if ext in (".jpg", ".jpeg"):
        return "jpg"
    if ext in (".tif", ".tiff") or ext in RAW_EXTS:
        return "tiff"
    if ext in (".png", ".bmp", ".webp"):
        return "png"
    raise ValueError("unsupported source format: " + ext)


def output_extension(fmt):
    try:
        return {"jpg": ".jpg", "png": ".png", "tiff": ".tif"}[fmt]
    except (KeyError, TypeError) as exc:
        raise ValueError("output format must be jpg, png or tiff") from exc


def _srgb_profile():
    return ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()


def _raw_decode(path):
    try:
        import rawpy
    except ImportError as exc:
        raise ValueError("RAW processing requires rawpy: python -m pip install rawpy") from exc
    params = dict(use_camera_wb=True, use_auto_wb=False, no_auto_bright=True,
                  output_color=rawpy.ColorSpace.sRGB, output_bps=16, gamma=(2.4, 12.92))
    try:
        with rawpy.imread(str(path)) as raw:
            pixels = raw.postprocess(**params)
    except Exception as exc:
        raise ValueError("Cannot decode RAW {0}: {1}".format(path, exc)) from exc
    if pixels.dtype.name != "uint16" or pixels.ndim != 3 or pixels.shape[2] != 3:
        raise ValueError("RAW decoder did not return an unsigned 16-bit RGB image")
    meta = {"size": [int(pixels.shape[1]), int(pixels.shape[0])], "hasICC": True,
            "hasAlpha": False, "bitDepth": 16, "inputFormat": Path(path).suffix[1:].upper(),
            "rawOriginalExtension": Path(path).suffix.lower(), "decoder": "rawpy",
            "decoderVersion": str(rawpy.__version__),
            "librawVersion": list(rawpy.libraw_version),
            "rawParameters": {"use_camera_wb": True, "use_auto_wb": False,
                              "no_auto_bright": True, "output_color": "sRGB",
                              "output_bps": 16, "gamma": [2.4, 12.92]}}
    return pixels, meta


def _preview(rgb, alpha, metadata, sample):
    if isinstance(sample, bool) or not isinstance(sample, int) or sample < 1:
        raise ValueError("preview sample must be a positive integer")
    # Resample RGBA together so invisible RGB values cannot bleed into visible edges.
    if alpha is not None:
        rgba = rgb.convert("RGBA")
        rgba.putalpha(alpha)
        rgba.thumbnail((sample, sample), Image.Resampling.LANCZOS)
        alpha = rgba.getchannel("A")
        rgb = rgba.convert("RGB")
    else:
        rgb.thumbnail((sample, sample), Image.Resampling.LANCZOS)
    return rgb, alpha, metadata


def read_preview(path, sample=512):
    """Return an oriented sRGB 8-bit view, separate alpha and original source metadata."""
    ext = Path(path).suffix.lower()
    if ext not in IMAGE_EXTS:
        raise ValueError("unsupported source format: " + ext)
    if ext in RAW_EXTS:
        pixels, metadata = _raw_decode(path)
        # Pillow's RGB16 TIFF decoder selects high bytes; use the same conversion here.
        rgb = Image.fromarray((pixels >> 8).astype("uint8"))
        return _preview(rgb, None, metadata, sample)
    try:
        with Image.open(path) as original:
            if getattr(original, "n_frames", 1) != 1:
                raise ValueError("multi-frame/multi-page images are not supported")
            input_format = original.format
            bit_depth = 8
            if ext == ".png":
                with open(path, "rb") as stream:
                    header = stream.read(26)
                if header[:8] == b"\x89PNG\r\n\x1a\n" and len(header) >= 26:
                    bit_depth = int(header[24])
            elif ext in (".tif", ".tiff"):
                bits = original.tag_v2.get(258, (8,))
                bits = bits if isinstance(bits, (tuple, list)) else (bits,)
                bit_depth = max(int(v) for v in bits)
                sample_format = original.tag_v2.get(339, (1,))
                sample_format = sample_format if isinstance(sample_format, (tuple, list)) else (sample_format,)
                if any(int(v) != 1 for v in sample_format):
                    raise ValueError("signed or floating-point TIFF pixels are not supported")
            if bit_depth > 16 or original.mode == "F":
                raise ValueError("32-bit/floating-point images are not supported")
            if original.mode == "I" and bit_depth != 16:
                raise ValueError("32-bit integer images are not supported")
            profile = original.info.get("icc_profile")
            im = ImageOps.exif_transpose(original)
            alpha = None
            if bit_depth == 16 and im.mode in ("I", "I;16", "I;16L", "I;16B", "I;16N"):
                values = im.convert("I")
                low, high = values.getextrema()
                if low < 0 or high > 65535:
                    raise ValueError("16-bit grayscale source is outside the unsigned 16-bit range")
                if "transparency" in im.info:
                    # Compare the original 16-bit value before reducing colour precision.
                    transparent_value = int(im.info["transparency"])
                    data = values.get_flattened_data() if hasattr(values, "get_flattened_data") else values.getdata()
                    mask = bytes(0 if value == transparent_value else 255
                                 for value in data)
                    alpha = Image.frombytes("L", im.size, mask)
                colour = values.point(lambda value: value * (1.0 / 257) + 0.5).convert("L")
            elif "A" in im.getbands() or "transparency" in im.info:
                if ext == ".png" and bit_depth == 16 and im.mode == "RGB" and "transparency" in im.info:
                    # Pillow discards low colour bytes before applying a 16-bit RGB key.
                    raise ValueError("16-bit RGB PNG with a tRNS transparency key is not supported; use RGBA PNG")
                rgba = im.convert("RGBA")
                alpha = rgba.getchannel("A")
                colour = im.convert("L") if im.mode in ("1", "L", "LA") else rgba.convert("RGB")
            else:
                colour = im
            if profile:
                try:
                    source_profile = ImageCms.ImageCmsProfile(BytesIO(profile))
                    if colour.mode == "P":
                        colour = colour.convert("RGB")
                    elif colour.mode == "1":
                        colour = colour.convert("L")
                    # Some grayscale exports carry an RGB ICC; expand equal channels
                    # before using that profile, while retaining native gray profiles.
                    if colour.mode == "L" and source_profile.profile.xcolor_space.strip() == "RGB":
                        colour = colour.convert("RGB")
                    rgb = ImageCms.profileToProfile(
                        colour, source_profile,
                        ImageCms.createProfile("sRGB"), renderingIntent=1, outputMode="RGB")
                except (ImageCms.PyCMSError, OSError, ValueError) as exc:
                    raise ValueError("Cannot convert embedded ICC profile: " + str(exc)) from exc
            else:
                rgb = colour.convert("RGB")
            metadata = {"size": list(im.size), "hasICC": bool(profile),
                        "hasAlpha": alpha is not None, "bitDepth": bit_depth,
                        "inputFormat": input_format or ext[1:].upper()}
            rgb = rgb.copy()
            if alpha is not None:
                alpha = alpha.copy()
    except (OSError, SyntaxError) as exc:
        raise ValueError("Cannot read image {0}: {1}".format(path, exc)) from exc
    return _preview(rgb, alpha, metadata, sample)


@dataclass
class PreparedSource:
    path: str
    metadata: dict


class PhotoSources:
    """Own only this run's delayed private temporary directory; never modify sources/XMP."""
    def __init__(self):
        self._temp = None
        self._counter = 0

    def __enter__(self):
        return self

    def _directory(self):
        if self._temp is None:
            self._temp = tempfile.TemporaryDirectory(prefix="photoshop-sources-")
        return Path(self._temp.name)

    def prepare(self, path, dry_run=False):
        source = str(Path(path).resolve())
        ext = Path(source).suffix.lower()
        if ext in RAW_EXTS:
            pixels, metadata = _raw_decode(source)
            if dry_run:
                return PreparedSource(source, metadata)
            try:
                import tifffile
            except ImportError as exc:
                raise ValueError("RAW preparation requires tifffile: python -m pip install tifffile") from exc
            self._counter += 1
            target = self._directory() / ("raw_{0:05}.tif".format(self._counter))
            profile = _srgb_profile()
            tifffile.imwrite(str(target), pixels, photometric="rgb", metadata=None,
                             extratags=[(34675, "B", len(profile), profile, False)])
            return PreparedSource(str(target), metadata)
        rgb, alpha, metadata = read_preview(source)
        if ext != ".webp" or dry_run:
            return PreparedSource(source, metadata)
        rgb, alpha, _ = read_preview(source, sample=max(metadata["size"]))
        if alpha is not None:
            rgb = rgb.convert("RGBA")
            rgb.putalpha(alpha)
        self._counter += 1
        target = self._directory() / ("webp_{0:05}.png".format(self._counter))
        rgb.save(target, format="PNG", icc_profile=_srgb_profile())
        metadata = dict(metadata, hasICC=True, decoder="Pillow", preparedFormat="PNG")
        return PreparedSource(str(target), metadata)

    def __exit__(self, exc_type, exc_value, traceback):
        if self._temp is not None:
            self._temp.cleanup()
            self._temp = None
        return False
