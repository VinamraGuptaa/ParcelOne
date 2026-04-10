"""
Captcha solver module for eCourts Securimage captcha.
Uses EasyOCR for text recognition.
"""

import re
from PIL import Image, ImageFilter
import easyocr

# Initialize reader once (downloading models on first run)
_reader = None


def _get_reader() -> easyocr.Reader:
    global _reader
    if _reader is None:
        _reader = easyocr.Reader(['en'], gpu=False, verbose=False)
    return _reader


def preprocess_image(image_path: str, output_path: str = None) -> str:
    """
    Preprocess captcha image for EasyOCR.

    Keeps the image as RGB with contrast enhancement and upscaling.
    Avoids heavy binarization — EasyOCR handles its own internal preprocessing.

    Args:
        image_path: Path to the captcha image
        output_path: Optional path to save preprocessed image

    Returns:
        Path to the preprocessed image
    """
    import os
    from PIL import ImageEnhance
    if output_path is None:
        base, ext = os.path.splitext(image_path)
        output_path = f"{base}_processed.png"

    img = Image.open(image_path).convert('RGB')

    # Upscale 3x so characters are larger and easier to read
    width, height = img.size
    img = img.resize((width * 3, height * 3), Image.LANCZOS)

    # Boost contrast so text stands out from noisy background
    img = ImageEnhance.Contrast(img).enhance(2.0)

    img.save(output_path)
    return output_path


def solve(image_path: str) -> str:
    """
    Solve a captcha image using EasyOCR.

    Args:
        image_path: Path to the captcha image

    Returns:
        Recognized text from the captcha
    """
    import os
    import shutil

    processed_path = preprocess_image(image_path)

    # Save a debug copy so you can inspect what EasyOCR is seeing
    debug_path = "/tmp/ecourts_captcha_debug.png"
    shutil.copy(processed_path, debug_path)

    reader = _get_reader()
    results = reader.readtext(
        processed_path,
        detail=0,
        allowlist='ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789',
        paragraph=False,
        width_ths=0.5,
    )

    try:
        os.remove(processed_path)
    except OSError:
        pass

    text = ''.join(results)
    text = re.sub(r'\s+', '', text)

    return text
