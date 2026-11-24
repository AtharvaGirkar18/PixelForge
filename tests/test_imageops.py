"""Phase 1 tests: python -m pytest tests  (needs: pip install pillow numpy pytest)"""
import sys
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from shared.imageops import AUGMENT_OPS, OPERATIONS, run_operation, synthetic_image  # noqa: E402


@pytest.fixture()
def src(tmp_path):
    p = tmp_path / "a.b.jpg"  # dotted stem on purpose
    synthetic_image(1, 400, 300).save(p, "JPEG")
    return p


PARAMS = {"resize": {"scale": 50}, "crop": {"percent": 50}, "rotate": {"angle": 45}, "convert": {"format": "png"},
          "compress": {"quality": 30}, "thumbnail": {"size": 64}, "brightness": {"factor": 1.5},
          "augment": {"augment_ops": AUGMENT_OPS, "seed": 7}}


@pytest.mark.parametrize("op", OPERATIONS)
def test_every_operation_produces_a_valid_image(op, src, tmp_path):
    out = run_operation(src, tmp_path / "out" / "a.b_x", op, PARAMS.get(op, {}))
    assert out.exists()
    with Image.open(out) as im:
        im.verify()


def test_resize_halves_dimensions(src, tmp_path):
    out = run_operation(src, tmp_path / "o", "resize", {"scale": 50})
    assert Image.open(out).size == (200, 150)


@pytest.mark.parametrize("fmt,ext", [("png", ".png"), ("webp", ".webp"), ("jpeg", ".jpg")])
def test_format_conversion(fmt, ext, src, tmp_path):
    out = run_operation(src, tmp_path / "o", "convert", {"format": fmt})
    assert out.suffix == ext


def test_augmentation_is_reproducible(src, tmp_path):
    a = run_operation(src, tmp_path / "a", "augment", {"seed": 3, "augment_ops": AUGMENT_OPS})
    b = run_operation(src, tmp_path / "b", "augment", {"seed": 3, "augment_ops": AUGMENT_OPS})
    assert Image.open(a).size == Image.open(b).size


def test_unknown_operation_rejected(src, tmp_path):
    with pytest.raises(ValueError):
        run_operation(src, tmp_path / "o", "teleport", {})
