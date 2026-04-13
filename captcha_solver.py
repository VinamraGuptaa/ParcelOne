"""
Captcha solver module for eCourts Securimage captcha.
Uses RapidOCR (ONNX Runtime) — ~130MB RAM, 0.09s init vs EasyOCR's ~400MB / 35s.
"""

import re
from PIL import Image, ImageEnhance

# Lazy singleton — loaded on first solve() call
_engine = None


def _get_engine():
    global _engine
    if _engine is None:
        from rapidocr_onnxruntime import RapidOCR
        _engine = RapidOCR()
    return _engine


def preprocess_image(image_path: str, output_path: str = None) -> str:
    """
    Preprocess captcha image for OCR.
    Upscales 3x and boosts contrast so characters stand out.
    """
    import os
    if output_path is None:
        base, ext = os.path.splitext(image_path)
        output_path = f"{base}_processed.png"

    img = Image.open(image_path).convert('RGB')
    width, height = img.size
    img = img.resize((width * 3, height * 3), Image.LANCZOS)
    img = ImageEnhance.Contrast(img).enhance(2.0)
    img.save(output_path)
    return output_path


def solve(image_path: str) -> str:
    """
    Solve a captcha image using RapidOCR.

    Returns:
        Recognized text (alphanumeric only, whitespace stripped).
    """
    import os, shutil

    processed_path = preprocess_image(image_path)

    debug_path = "/tmp/ecourts_captcha_debug.png"
    shutil.copy(processed_path, debug_path)

    engine = _get_engine()
    result, _ = engine(processed_path)

    try:
        os.remove(processed_path)
    except OSError:
        pass

    if not result:
        return ""

    # result is a list of [bbox, text, confidence] — join all text blocks
    text = "".join(item[1] for item in result)
    text = re.sub(r'\s+', '', text)
    # Keep only alphanumeric characters (captcha format)
    text = re.sub(r'[^A-Za-z0-9]', '', text)
    return text
