"""Image operations (Phase 1 of the Master Specification).

Everything here is pure local computation with Pillow + NumPy - no paid APIs, no ML.
The operations exist to create realistic CPU workloads for the Kubernetes demo.
"""
from __future__ import annotations

import random
from pathlib import Path
from typing import Callable, Optional

import numpy as np
from PIL import Image, ImageEnhance, ImageFilter, ImageOps

Progress = Callable[[int, str], None]

FORMATS = {"jpeg": "JPEG", "jpg": "JPEG", "png": "PNG", "webp": "WEBP"}
EXT_FOR = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}
ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".webp"}

OPERATIONS = [
    "resize", "crop", "rotate", "flip_h", "flip_v",
    "convert", "compress", "thumbnail",
    "grayscale", "brightness", "contrast", "sharpen", "blur",
    "augment",
]
AUGMENT_OPS = ["rotate", "flip_h", "flip_v", "crop", "brightness", "contrast", "noise"]


def _f(params: dict, key: str, default: float) -> float:
    try:
        return float(params.get(key, default))
    except (TypeError, ValueError):
        return float(default)


def _i(params: dict, key: str, default: int) -> int:
    return int(_f(params, key, default))


# --------------------------------------------------------------------- primitives
def resize(img: Image.Image, params: dict) -> Image.Image:
    w, h = img.size
    if params.get("width") or params.get("height"):
        tw, th = _i(params, "width", 0), _i(params, "height", 0)
        if tw and not th:
            th = round(h * tw / w)
        elif th and not tw:
            tw = round(w * th / h)
    else:
        scale = _f(params, "scale", 50) / 100.0
        tw, th = round(w * scale), round(h * scale)
    tw, th = max(1, tw), max(1, th)
    return img.resize((tw, th), Image.LANCZOS)


def crop(img: Image.Image, params: dict, rng: Optional[random.Random] = None) -> Image.Image:
    w, h = img.size
    pct = max(10.0, min(100.0, _f(params, "percent", 80))) / 100.0
    cw, ch = max(1, round(w * pct)), max(1, round(h * pct))
    if rng is not None:  # random crop (augmentation)
        x, y = rng.randint(0, w - cw), rng.randint(0, h - ch)
    elif "x" in params or "y" in params:
        x = max(0, min(w - cw, _i(params, "x", 0)))
        y = max(0, min(h - ch, _i(params, "y", 0)))
    else:  # centre crop
        x, y = (w - cw) // 2, (h - ch) // 2
    return img.crop((x, y, x + cw, y + ch))


def rotate(img: Image.Image, angle: float) -> Image.Image:
    return img.rotate(angle, resample=Image.BICUBIC, expand=True)


def flip_h(img: Image.Image) -> Image.Image:
    return ImageOps.mirror(img)


def flip_v(img: Image.Image) -> Image.Image:
    return ImageOps.flip(img)


def grayscale(img: Image.Image) -> Image.Image:
    return ImageOps.grayscale(img).convert("RGB")


def brightness(img: Image.Image, factor: float) -> Image.Image:
    return ImageEnhance.Brightness(img).enhance(factor)


def contrast(img: Image.Image, factor: float) -> Image.Image:
    return ImageEnhance.Contrast(img).enhance(factor)


def sharpen(img: Image.Image, amount: float = 2.0) -> Image.Image:
    return ImageEnhance.Sharpness(img).enhance(amount)


def blur(img: Image.Image, radius: float = 4.0) -> Image.Image:
    return img.filter(ImageFilter.GaussianBlur(radius))


def noise(img: Image.Image, sigma: float, rng: random.Random) -> Image.Image:
    arr = np.asarray(img, dtype=np.int16)
    gen = np.random.default_rng(rng.randint(0, 2**31 - 1))
    arr = arr + gen.normal(0, sigma, arr.shape).astype(np.int16)
    return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))


# --------------------------------------------------------------------- augmentation
def augment(img: Image.Image, params: dict, progress: Optional[Progress] = None) -> Image.Image:
    """Apply a random (but reproducible) chain of the selected augmentations.

    params: augment_ops (list), seed (int), max_angle, noise_sigma
    """
    rng = random.Random(_i(params, "seed", 0))
    ops = [o for o in params.get("augment_ops") or AUGMENT_OPS if o in AUGMENT_OPS]
    chosen = [o for o in ops if rng.random() < 0.7] or [rng.choice(ops or AUGMENT_OPS)]
    max_angle = _f(params, "max_angle", 20)
    for n, op in enumerate(chosen, 1):
        if op == "rotate":
            img = rotate(img, rng.uniform(-max_angle, max_angle))
        elif op == "flip_h":
            img = flip_h(img)
        elif op == "flip_v":
            img = flip_v(img)
        elif op == "crop":
            img = crop(img, {"percent": rng.uniform(70, 95)}, rng)
        elif op == "brightness":
            img = brightness(img, rng.uniform(0.6, 1.4))
        elif op == "contrast":
            img = contrast(img, rng.uniform(0.7, 1.4))
        elif op == "noise":
            img = noise(img, _f(params, "noise_sigma", 12), rng)
        if progress:
            progress(35 + int(40 * n / len(chosen)), f"augment:{op}")
    return img


# --------------------------------------------------------------------- pipeline
def _normalise_mode(img: Image.Image, fmt: str) -> Image.Image:
    if fmt == "JPEG" and img.mode != "RGB":
        if img.mode in ("RGBA", "LA", "P"):
            rgba = img.convert("RGBA")
            bg = Image.new("RGB", rgba.size, (255, 255, 255))
            bg.paste(rgba, mask=rgba.split()[-1])
            return bg
        return img.convert("RGB")
    if img.mode not in ("RGB", "RGBA", "L"):
        return img.convert("RGB")
    return img


def run_operation(src: Path, dest_base: Path, operation: str, params: dict,
                  progress: Optional[Progress] = None) -> Path:
    """Load `src`, apply `operation`, save to dest_base + extension. Returns final path."""
    cb = progress or (lambda p, s: None)
    if operation not in OPERATIONS:
        raise ValueError(f"unsupported operation: {operation}")

    cb(10, "loading")
    with Image.open(src) as im:
        src_fmt = im.format or "JPEG"
        img = ImageOps.exif_transpose(im)
        img.load()

    cb(30, "processing")
    fmt = src_fmt if src_fmt in EXT_FOR else "PNG"
    save_kwargs: dict = {}

    if operation == "resize":
        img = resize(img, params)
    elif operation == "crop":
        img = crop(img, params)
    elif operation == "rotate":
        img = rotate(img, _f(params, "angle", 90))
    elif operation == "flip_h":
        img = flip_h(img)
    elif operation == "flip_v":
        img = flip_v(img)
    elif operation == "grayscale":
        img = grayscale(img)
    elif operation == "brightness":
        img = brightness(img, _f(params, "factor", 1.3))
    elif operation == "contrast":
        img = contrast(img, _f(params, "factor", 1.3))
    elif operation in ("sharpen", "blur"):
        passes = max(1, min(_i(params, "passes", 1), 60))
        for n in range(passes):
            img = sharpen(img, _f(params, "amount", 2.0)) if operation == "sharpen" else blur(img, _f(params, "radius", 4.0))
            cb(30 + int(48 * (n + 1) / passes), f"{operation} pass {n + 1}/{passes}")
    elif operation == "thumbnail":
        size = _i(params, "size", 256)
        img = img.copy()
        img.thumbnail((size, size), Image.LANCZOS)
    elif operation == "convert":
        fmt = FORMATS.get(str(params.get("format", "webp")).lower(), "WEBP")
        save_kwargs["quality"] = _i(params, "quality", 90)
    elif operation == "compress":
        if fmt == "PNG":
            img = img.convert("RGB").quantize(colors=max(2, min(256, _i(params, "colors", 128))))
            save_kwargs["optimize"] = True
        else:
            save_kwargs.update(quality=_i(params, "quality", 55), optimize=True)
    elif operation == "augment":
        img = augment(img, params, cb)

    cb(80, "saving")
    img = _normalise_mode(img, fmt)
    if fmt in ("JPEG", "WEBP"):
        save_kwargs.setdefault("quality", 88)
    dest = dest_base.with_name(dest_base.name + EXT_FOR[fmt])
    dest.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest, format=fmt, **save_kwargs)
    cb(95, "saved")
    return dest


def make_thumbnail(src: Path, dest: Path, size: int = 256) -> Path:
    with Image.open(src) as im:
        im = im.convert("RGB")
        im.thumbnail((size, size))
        dest.parent.mkdir(parents=True, exist_ok=True)
        im.save(dest, "JPEG", quality=70)
    return dest


def synthetic_image(seed: int, width: int = 1600, height: int = 1200) -> Image.Image:
    """Colourful gradient + shapes + noise; used by the demo workload generator."""
    gen = np.random.default_rng(seed)
    x = np.linspace(0, 1, width, dtype=np.float32)[None, :]
    y = np.linspace(0, 1, height, dtype=np.float32)[:, None]
    c = gen.random((3, 4)).astype(np.float32)
    chans = [(c[i, 0] * x + c[i, 1] * y + c[i, 2] * np.sin(6 * x * c[i, 3] + 5 * y)) for i in range(3)]
    arr = np.stack(chans, axis=-1)
    arr = (arr - arr.min()) / (np.ptp(arr) + 1e-6) * 255
    arr += gen.normal(0, 6, arr.shape)
    img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))
    from PIL import ImageDraw
    d = ImageDraw.Draw(img)
    for _ in range(8):
        cx, cy = int(gen.integers(0, width)), int(gen.integers(0, height))
        r = int(gen.integers(40, 220))
        col = tuple(int(v) for v in gen.integers(0, 255, 3))
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=col, outline=(255, 255, 255), width=4)
    return img
