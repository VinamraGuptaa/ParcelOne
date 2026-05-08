"""
Captcha solver module for eCourts Securimage captcha.
Uses ddddocr when available, then RapidOCR (ONNX Runtime) as fallback.
"""

import json
import logging
import os
import re
import time
from PIL import Image, ImageEnhance, ImageFilter, ImageOps

# Lazy singleton — loaded on first solve() call
_engine = None
_dddd_engine = None

logger = logging.getLogger(__name__)
_DEBUG_LOG_PATH = "/Users/vinamragupta/.gemini/antigravity/playground/icy-disk/.cursor/debug-eb113b.log"


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


def _get_dddd_engine():
    global _dddd_engine
    if _dddd_engine is None:
        import ddddocr

        # Keep defaults; this model is usually better for distorted captcha glyphs.
        _dddd_engine = ddddocr.DdddOcr(show_ad=False)
    return _dddd_engine


def _clean_text(text: str) -> str:
    text = re.sub(r"\s+", "", text or "")
    return re.sub(r"[^A-Za-z0-9]", "", text)


CAPTCHA_MIN_LEN = int(os.getenv("CAPTCHA_MIN_LEN", "4"))
CAPTCHA_MAX_LEN = int(os.getenv("CAPTCHA_MAX_LEN", "7"))


def is_plausible_captcha(text: str) -> bool:
    """Heuristic filter to reject obvious OCR junk before form submit."""
    cleaned = _clean_text(text)
    if not cleaned:
        return False
    n = len(cleaned)
    if n < CAPTCHA_MIN_LEN or n > CAPTCHA_MAX_LEN:
        return False
    # Reject degenerate outputs like "hhhhh" that often come from blurred glyphs.
    uniq = len(set(cleaned.lower()))
    if uniq <= 1:
        return False
    return True


def _captcha_score(text: str) -> int:
    """
    Rank candidate quality; higher is better.
    Heuristic only: strong length fit + diversity.
    """
    cleaned = _clean_text(text)
    if not cleaned:
        return -10_000
    if not is_plausible_captcha(cleaned):
        return -1_000 + len(cleaned)
    uniq = len(set(cleaned.lower()))
    # Prefer lengths near 6 and richer character diversity.
    return 1_000 - abs(len(cleaned) - 6) * 10 + uniq


def _otsu_threshold(gray: Image.Image) -> int:
    """Otsu threshold for L-mode image (no numpy)."""
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


def _variant_median_3x_c185(im: Image.Image) -> Image.Image:
    """H1: median reduces salt-and-pepper before upscale."""
    im = im.filter(ImageFilter.MedianFilter(3))
    w, h = im.size
    im = im.resize((w * 3, h * 3), Image.LANCZOS)
    return ImageEnhance.Contrast(im).enhance(1.85)


def _pad(im: Image.Image, ratio: float = 0.14) -> Image.Image:
    """
    Add symmetric white border before any denoise/resize operation.
    Helps OCR when glyphs touch image edges and get visually clipped.
    """
    w, h = im.size
    px = max(2, int(min(w, h) * ratio))
    return ImageOps.expand(im, border=px, fill=(255, 255, 255))


def _variant_padded_stroke_4x(im: Image.Image) -> Image.Image:
    """
    H5: edge-safe path for cut alphabets.
    - pad to protect edge glyphs
    - upscale 4x for better OCR character context
    - mild max-filter to thicken thin/cut strokes
    """
    im = _pad(im, ratio=0.18)
    w, h = im.size
    im = im.resize((w * 4, h * 4), Image.LANCZOS)
    # MaxFilter on grayscale slightly reconnects broken edge strokes.
    g = im.convert("L").filter(ImageFilter.MaxFilter(3))
    im = g.convert("RGB")
    return ImageEnhance.Contrast(im).enhance(1.7)


def _variant_otsu_rgb_3x(im: Image.Image) -> Image.Image:
    """H2: binarize noisy background, then upscale for ddddocr."""
    g = im.convert("L")
    thr = _otsu_threshold(g)
    bw = g.point(lambda p, t=thr: 255 if p > t else 0)
    rgb = bw.convert("RGB")
    w, h = rgb.size
    return rgb.resize((w * 3, h * 3), Image.LANCZOS)


def _variant_soft_blur_4x(im: Image.Image) -> Image.Image:
    """H3: mild denoise + soft blur + moderate contrast (less harsh than 2.0)."""
    w, h = im.size
    im = im.resize((w * 2, h * 2), Image.LANCZOS)
    im = im.filter(ImageFilter.MedianFilter(3))
    im = im.resize((w * 4, h * 4), Image.LANCZOS)
    im = im.filter(ImageFilter.GaussianBlur(radius=0.45))
    return ImageEnhance.Contrast(im).enhance(1.6)


def _variant_default_3x_c20(im: Image.Image) -> Image.Image:
    """H4: original pipeline (upscale 3x + strong contrast)."""
    w, h = im.size
    im = im.resize((w * 3, h * 3), Image.LANCZOS)
    return ImageEnhance.Contrast(im).enhance(2.0)


def preprocess_image(image_path: str, output_path: str = None) -> str:
    """
    Preprocess captcha image for OCR.
    Upscales 3x and boosts contrast so characters stand out.
    """
    import os

    if output_path is None:
        base, ext = os.path.splitext(image_path)
        output_path = f"{base}_processed.png"

    img = Image.open(image_path).convert("RGB")
    img = _variant_default_3x_c20(img)
    img.save(output_path)
    return output_path


def _solve_with_rapidocr_only(image_path: str) -> str:
    import os
    import shutil

    processed_path = preprocess_image(image_path)
    debug_path = "/tmp/ecourts_captcha_debug.png"
    shutil.copy(processed_path, debug_path)

    engine = _get_engine()
    result, _ = engine(processed_path)
    if not result:
        logger.warning("captcha OCR returned empty text in RapidOCR-only mode")
        _debug_log("H2", "rapidocr_only_empty", {})
        return ""

    text = "".join(item[1] for item in result)
    cleaned = _clean_text(text)
    logger.info("captcha solved by RapidOCR-only mode text=%r", cleaned)
    _debug_log("H2", "rapidocr_only_selected", {"text_len": len(cleaned)})
    try:
        os.remove(processed_path)
    except OSError:
        pass
    return cleaned


def solve(image_path: str, mode: str = "auto") -> str:
    """
    Solve a captcha image.

    Modes:
      - auto (default): ddddocr variants first, then RapidOCR fallback
      - rapidocr_only: skip ddddocr and use RapidOCR pipeline only

    Returns:
        Recognized text (alphanumeric only, whitespace stripped).
    """
    import os
    import shutil
    import tempfile

    logger.debug("captcha solve start: image=%s mode=%s", os.path.basename(image_path), mode)
    _debug_log("H1", "solve_start", {"image": os.path.basename(image_path)})

    if mode == "rapidocr_only":
        return _solve_with_rapidocr_only(image_path)

    base = Image.open(image_path).convert("RGB")
    variant_specs = [
        ("padded_stroke_4x", "H5", _variant_padded_stroke_4x),
        ("median_3x_c185", "H1", _variant_median_3x_c185),
        ("otsu_rgb_3x", "H2", _variant_otsu_rgb_3x),
        ("soft_blur_4x", "H3", _variant_soft_blur_4x),
        ("default_3x_c20", "H4", _variant_default_3x_c20),
    ]

    temp_paths: list[str] = []
    debug_path = "/tmp/ecourts_captcha_debug.png"

    best_candidate = ""
    best_score = -10_000
    try:
        for name, hid, fn in variant_specs:
            im = fn(base.copy())
            tf = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
            tf.close()
            p = tf.name
            temp_paths.append(p)
            im.save(p)
            try:
                dddd = _get_dddd_engine()
                with open(p, "rb") as f:
                    blob = f.read()
                dddd_text = _clean_text(dddd.classification(blob))
                logger.debug(
                    "ddddocr attempt: variant=%s hypothesis=%s bytes=%s text_len=%s",
                    name,
                    hid,
                    len(blob),
                    len(dddd_text),
                )
                _debug_log(hid, "dddd_variant_result", {"variant": name, "blob_len": len(blob), "text_len": len(dddd_text)})
                if dddd_text:
                    score = _captcha_score(dddd_text)
                    _debug_log(
                        hid,
                        "dddd_candidate_scored",
                        {"variant": name, "text_len": len(dddd_text), "score": score},
                    )
                    if score > best_score:
                        best_score = score
                        best_candidate = dddd_text
                        shutil.copy(p, debug_path)
            except Exception as e:
                logger.debug("ddddocr variant failed: variant=%s err=%s", name, type(e).__name__)
                _debug_log(hid, "dddd_variant_error", {"variant": name, "error": type(e).__name__})

        processed_path = preprocess_image(image_path)
        shutil.copy(processed_path, debug_path)

        engine = _get_engine()
        result, _ = engine(processed_path)

        if result:
            text = "".join(item[1] for item in result)
            cleaned = _clean_text(text)
            score = _captcha_score(cleaned)
            _debug_log("H2", "rapidocr_candidate_scored", {"text_len": len(cleaned), "score": score})
            if score > best_score:
                best_score = score
                best_candidate = cleaned
        else:
            logger.warning("captcha OCR returned empty text after ddddocr + RapidOCR fallback")
            _debug_log("H2", "rapidocr_empty", {})

        if not best_candidate:
            return ""
        if not is_plausible_captcha(best_candidate):
            logger.warning(
                "captcha OCR produced only implausible candidates; best=%r len=%s",
                best_candidate,
                len(best_candidate),
            )
            _debug_log("H2", "ocr_implausible_best", {"text_len": len(best_candidate), "score": best_score})
            return ""

        logger.info("captcha solved (best-candidate) text=%r score=%s", best_candidate, best_score)
        _debug_log("H2", "ocr_best_selected", {"text_len": len(best_candidate), "score": best_score})
        return best_candidate
    finally:
        import os as _os

        for p in temp_paths:
            try:
                _os.remove(p)
            except OSError:
                pass
        processed_default = image_path.replace(".png", "_processed.png")
        if processed_default != image_path:
            try:
                _os.remove(processed_default)
            except OSError:
                pass
