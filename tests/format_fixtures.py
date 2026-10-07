"""Small genuine photo-format fixtures; no Photoshop or camera downloads needed."""
from pathlib import Path


def write_cfa_dng(path, *, width=128, height=96, orientation=1):
    """Write a 12-bit RGGB sensor mosaic in a valid DNG/TIFF container.

    The sensor samples vary by position and filter colour, so LibRaw must perform
    real RAW demosaicing. This is deliberately not a renamed RGB TIFF file.
    """
    import numpy as np
    import tifffile

    if width < 64 or height < 64 or width % 2 or height % 2:
        raise ValueError("DNG fixture needs even dimensions of at least 64 pixels")
    y, x = np.mgrid[0:height, 0:width]
    signal = 400 + 1600 * x / (width - 1) + 500 * y / (height - 1)
    gains = np.ones((height, width), dtype=np.float64)
    gains[0::2, 0::2] = 1.15  # red photosites
    gains[1::2, 1::2] = 0.80  # blue photosites
    mosaic = np.clip(64 + signal * gains, 64, 4095).astype(np.uint16)
    # Rational values are supplied as flattened numerator/denominator pairs.
    matrix = (1, 1, 0, 1, 0, 1, 0, 1, 1, 1, 0, 1, 0, 1, 0, 1, 1, 1)
    tags = [
        (274, "H", 1, orientation, False),              # Orientation
        (33421, "H", 2, (2, 2), False),                # CFARepeatPatternDim
        (33422, "B", 4, (0, 1, 1, 2), False),          # CFAPattern: RGGB
        (50706, "B", 4, (1, 4, 0, 0), False),          # DNGVersion
        (50707, "B", 4, (1, 1, 0, 0), False),          # DNGBackwardVersion
        (50708, "s", 0, "Codex synthetic RGGB camera", False),
        (50710, "B", 3, (0, 1, 2), False),             # CFAPlaneColor
        (50711, "H", 1, 1, False),                     # CFALayout
        (50714, "H", 1, 64, False),                    # BlackLevel
        (50717, "I", 1, 4095, False),                  # WhiteLevel
        (50719, "I", 2, (0, 0), False),                # DefaultCropOrigin
        (50720, "I", 2, (width, height), False),        # DefaultCropSize
        (50721, "2i", 9, matrix, False),               # ColorMatrix1
        (50728, "2I", 3, (1, 1, 1, 1, 1, 1), False), # AsShotNeutral
        (50778, "H", 1, 21, False),                    # D65 calibration
        (50829, "I", 4, (0, 0, height, width), False),  # ActiveArea
    ]
    target = Path(path)
    tifffile.imwrite(target, mosaic, photometric=32803, metadata=None,
                     compression=None, extratags=tags)
    return target


def write_16bit_gray(path):
    """Three repeatable gray bands: black, midpoint, and full-scale white."""
    import numpy as np
    import tifffile

    pixels = np.tile(np.repeat(np.array([0, 32768, 65535], dtype=np.uint16), 16), (32, 1))
    tifffile.imwrite(path, pixels, photometric="minisblack", metadata=None)
    return Path(path)


def write_16bit_rgb(path):
    import numpy as np
    import tifffile

    y, x = np.mgrid[0:48, 0:64]
    pixels = np.stack((4096 + x * 700, 8192 + y * 800, 16384 + x * 300), axis=2)
    tifffile.imwrite(path, pixels.astype(np.uint16), photometric="rgb", metadata=None)
    return Path(path)
