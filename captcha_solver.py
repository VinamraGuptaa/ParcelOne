"""
Captcha solver — RapidOCR (ONNX Runtime) only.

Runs five image-preprocessing variants, scores each candidate by length and
character diversity, and returns the best plausible result.
"""

import json
import logging
import os
import re
import shutil
import tempfile
import time
from PIL import Image, ImageEnhance, ImageFilter, ImageOps

_engine = None

logger = logging.getLogger(__name__)
_DEBUG_LOG_PATH = "/Users/vinamragupta/.gemini/antigravity/playground/icy-disk/.cursor/debug-eb113b.log"

CAPTCHA_MIN_LEN = int(os.getenv("CAPTCHA_MIN_LEN", "4"))
CAPTCHA_MAX_LEN = int(os.getenv("CAPTCHA_MAX_LEN", "7"))


def _debug_log(hypothesis_id: str, message: str, data: dict, run_id: str = "captcha") -> None:
    # #region agent log
    payload = {
        "sessionId": "eb113b",
        "runId": run_id,
        "hypothesisId": hypothesis_id,
        "location": "captcha_solver.py",
        "message": message,
        "data": data,
        "timestamp": int(time.time() * 1000),
    }
    try:
        with open(_DEBUG_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except OSError:
        pass
    # #endregion


def _get_engine():
    global _engine
    if _engine is None:
        from rapidocr_onnxruntime import RapidOCR
        _engine = RapidOCR()
    return _engine


def _clean_text(text: str) -> str:
    text = re.sub(r"\s+", "", text or "")
    return re.sub(r"[^A-Za-z0-9]", "", text)


def is_plausible_captcha(text: str) -> bool:
    """Reject obvious OCR junk before submitting to the form."""
    cleaned = _clean_text(text)
    if not cleaned:
        return False
    n = len(cleaned)
    if n < CAPTCHA_MIN_LEN or n > CAPTCHA_MAX_LEN:
        return False
    # Reject degenerate single-character repetitions (e.g. "hhhhh").
    if len(set(cleaned.lower())) <= 1:
        return False
    return True


def _captcha_score(text: str) -> int:
    """Higher is better. Plausible strings with length near 6 and rich diversity win."""
    cleaned = _clean_text(text)
    if not cleaned:
        return -10_000
    if not is_plausible_captcha(cleaned):
        return -1_000 + len(cleaned)
    return 1_000 - abs(len(cleaned) - 6) * 10 + len(set(cleaned.lower()))


def _otsu_threshold(gray: Image.Image) -> int:
    """Pure-PIL Otsu threshold for an L-mode image."""
    if gray.mode != "L":
        gray = gray.convert("L")
    hist = gray.histogram()
    total = gray.width * gray.height
    sum_total = sum(i * hist[i] for i in range(256))
    sum_b = 0.0
    w_b = 0
    best_t = 0
    best_var = 0.0
    for t in range(256):
        w_b += hist[t]
        if w_b == 0:
            continue
        w_f = total - w_b
        if w_f == 0:
            break
        sum_b += t * hist[t]
        m_b = sum_b / w_b
        m_f = (sum_total - sum_b) / w_f
        var_between = w_b * w_f * (m_b - m_f) ** 2
        if var_between > best_var:
            best_var = var_between
            best_t = t
    return best_t


def _pad(im: Image.Image, ratio: float = 0.14) -> Image.Image:
    w, h = im.size
    px = max(2, int(min(w, h) * ratio))
    return ImageOps.expand(im, border=px, fill=(255, 255, 255))


# ── preprocessing variants ────────────────────────────────────────────────────

def _variant_padded_stroke_4x(im: Image.Image) -> Image.Image:
    im = _pad(im, ratio=0.18)
    w, h = im.size
    im = im.resize((w * 4, h * 4), Image.LANCZOS)
    g = im.convert("L").filter(ImageFilter.MaxFilter(3))
    im = g.convert("RGB")
    return ImageEnhance.Contrast(im).enhance(1.7)


def _variant_median_3x_c185(im: Image.Image) -> Image.Image:
    im = im.filter(ImageFilter.MedianFilter(3))
    w, h = im.size
    im = im.resize((w * 3, h * 3), Image.LANCZOS)
    return ImageEnhance.Contrast(im).enhance(1.85)


def _variant_otsu_rgb_3x(im: Image.Image) -> Image.Image:
    g = im.convert("L")
    thr = _otsu_threshold(g)
    bw = g.point(lambda p, t=thr: 255 if p > t else 0)
    rgb = bw.convert("RGB")
    w, h = rgb.size
    return rgb.resize((w * 3, h * 3), Image.LANCZOS)


def _variant_soft_blur_4x(im: Image.Image) -> Image.Image:
    w, h = im.size
    im = im.resize((w * 2, h * 2), Image.LANCZOS)
    im = im.filter(ImageFilter.MedianFilter(3))
    im = im.resize((w * 4, h * 4), Image.LANCZOS)
    im = im.filter(ImageFilter.GaussianBlur(radius=0.45))
    return ImageEnhance.Contrast(im).enhance(1.6)


def _variant_default_3x_c20(im: Image.Image) -> Image.Image:
    w, h = im.size
    im = im.resize((w * 3, h * 3), Image.LANCZOS)
    return ImageEnhance.Contrast(im).enhance(2.0)


# ── public API ────────────────────────────────────────────────────────────────

def preprocess_image(image_path: str, output_path: str = None) -> str:
    """Preprocess captcha image (3x upscale + contrast boost)."""
    if output_path is None:
        base, ext = os.path.splitext(image_path)
        output_path = f"{base}_processed.png"
    img = Image.open(image_path).convert("RGB")
    img = _variant_default_3x_c20(img)
    img.save(output_path)
    return output_path


def solve(image_path: str, mode: str = "auto") -> str:
    """
    Solve a captcha image using RapidOCR.

    Runs multiple preprocessing variants, scores each result, and returns
    the best plausible candidate.  Returns empty string if no plausible
    result is found (caller should refresh captcha and retry).
    """
    logger.debug("captcha solve start: image=%s", os.path.basename(image_path))
    _debug_log("H1", "solve_start", {"image": os.path.basename(image_path)})

    variant_specs = [
        ("padded_stroke_4x", _variant_padded_stroke_4x),
        ("median_3x_c185",   _variant_median_3x_c185),
        ("otsu_rgb_3x",      _variant_otsu_rgb_3x),
        ("soft_blur_4x",     _variant_soft_blur_4x),
        ("default_3x_c20",   _variant_default_3x_c20),
    ]

    base = Image.open(image_path).convert("RGB")
    engine = _get_engine()
    best_candidate = ""
    best_score = -10_000
    temp_paths: list[str] = []

    try:
        for name, fn in variant_specs:
            im = fn(base.copy())
            tf = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
            tf.close()
            p = tf.name
            temp_paths.append(p)
            im.save(p)
            try:
                result, _ = engine(p)
                if not result:
                    continue
                text = "".join(item[1] for item in result)
                cleaned = _clean_text(text)
                score = _captcha_score(cleaned)
                logger.debug(
                    "captcha variant=%s text=%r score=%s", name, cleaned, score
                )
                _debug_log(
                    "H2",
                    "variant_scored",
                    {"variant": name, "text_len": len(cleaned), "score": score},
                )
                if score > best_score:
                    best_score = score
                    best_candidate = cleaned
                    shutil.copy(p, "/tmp/ecourts_captcha_debug.png")
            except Exception as exc:
                logger.debug("captcha variant=%s failed: %s", name, exc)

        if not best_candidate:
            logger.warning("captcha OCR: all variants returned empty")
            _debug_log("H2", "all_variants_empty", {})
            return ""

        if not is_plausible_captcha(best_candidate):
            logger.warning(
                "captcha OCR: best candidate implausible best=%r len=%s",
                best_candidate,
                len(best_candidate),
            )
            _debug_log(
                "H2",
                "best_implausible",
                {"text_len": len(best_candidate), "score": best_score},
            )
            return ""

        logger.info("captcha solved text=%r score=%s", best_candidate, best_score)
        _debug_log(
            "H2",
            "solved",
            {"text_len": len(best_candidate), "score": best_score},
        )
        return best_candidate

    finally:
        for p in temp_paths:
            try:
                os.remove(p)
            except OSError:
                pass
        processed_default = image_path.replace(".png", "_processed.png")
        if processed_default != image_path:
            try:
                os.remove(processed_default)
            except OSError:
                pass
