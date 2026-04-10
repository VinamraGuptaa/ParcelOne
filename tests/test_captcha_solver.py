"""
Unit tests for captcha_solver.py.

EasyOCR (which loads a neural network) is mocked to keep tests fast.
Image I/O uses real PIL operations on small synthetic images.
"""

import os
import tempfile
import pytest
from unittest.mock import patch, MagicMock
from PIL import Image


def _make_test_image(path: str, width: int = 120, height: int = 40) -> None:
    """Create a minimal RGB PNG at the given path."""
    img = Image.new("RGB", (width, height), color=(200, 200, 200))
    img.save(path)


# ── preprocess_image ──────────────────────────────────────────────────────────

class TestPreprocessImage:

    def test_creates_output_file(self):
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as src:
            _make_test_image(src.name)
            src_path = src.name

        out_path = src_path.replace(".png", "_out.png")
        try:
            from captcha_solver import preprocess_image
            result = preprocess_image(src_path, out_path)
            assert os.path.exists(result)
        finally:
            os.unlink(src_path)
            if os.path.exists(out_path):
                os.unlink(out_path)

    def test_output_is_3x_width(self):
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as src:
            _make_test_image(src.name, width=100, height=40)
            src_path = src.name

        out_path = src_path.replace(".png", "_out.png")
        try:
            from captcha_solver import preprocess_image
            result = preprocess_image(src_path, out_path)
            img = Image.open(result)
            assert img.size[0] == 300   # 100 * 3
        finally:
            os.unlink(src_path)
            if os.path.exists(out_path):
                os.unlink(out_path)

    def test_output_is_3x_height(self):
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as src:
            _make_test_image(src.name, width=100, height=40)
            src_path = src.name

        out_path = src_path.replace(".png", "_out.png")
        try:
            from captcha_solver import preprocess_image
            result = preprocess_image(src_path, out_path)
            img = Image.open(result)
            assert img.size[1] == 120   # 40 * 3
        finally:
            os.unlink(src_path)
            if os.path.exists(out_path):
                os.unlink(out_path)

    def test_output_is_rgb(self):
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as src:
            _make_test_image(src.name)
            src_path = src.name

        out_path = src_path.replace(".png", "_out.png")
        try:
            from captcha_solver import preprocess_image
            result = preprocess_image(src_path, out_path)
            img = Image.open(result)
            assert img.mode == "RGB"
        finally:
            os.unlink(src_path)
            if os.path.exists(out_path):
                os.unlink(out_path)

    def test_default_output_path_uses_processed_suffix(self):
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as src:
            _make_test_image(src.name)
            src_path = src.name

        expected_out = src_path.replace(".png", "_processed.png")
        try:
            from captcha_solver import preprocess_image
            result = preprocess_image(src_path)
            assert result == expected_out
            assert os.path.exists(result)
        finally:
            os.unlink(src_path)
            if os.path.exists(expected_out):
                os.unlink(expected_out)


# ── solve ─────────────────────────────────────────────────────────────────────

class TestSolve:

    def _mock_reader(self, return_text: list[str]):
        mock_reader = MagicMock()
        mock_reader.readtext.return_value = return_text
        return mock_reader

    def test_solve_returns_string(self):
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as src:
            _make_test_image(src.name)
            src_path = src.name

        try:
            with patch("captcha_solver._get_reader", return_value=self._mock_reader(["abc123"])):
                from captcha_solver import solve
                result = solve(src_path)
            assert isinstance(result, str)
        finally:
            os.unlink(src_path)

    def test_solve_joins_multiple_tokens(self):
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as src:
            _make_test_image(src.name)
            src_path = src.name

        try:
            with patch("captcha_solver._get_reader", return_value=self._mock_reader(["ab", "cd"])):
                from captcha_solver import solve
                result = solve(src_path)
            assert result == "abcd"
        finally:
            os.unlink(src_path)

    def test_solve_strips_internal_whitespace(self):
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as src:
            _make_test_image(src.name)
            src_path = src.name

        try:
            with patch("captcha_solver._get_reader", return_value=self._mock_reader(["ab cd"])):
                from captcha_solver import solve
                result = solve(src_path)
            assert " " not in result
        finally:
            os.unlink(src_path)

    def test_solve_empty_ocr_returns_empty_string(self):
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as src:
            _make_test_image(src.name)
            src_path = src.name

        try:
            with patch("captcha_solver._get_reader", return_value=self._mock_reader([])):
                from captcha_solver import solve
                result = solve(src_path)
            assert result == ""
        finally:
            os.unlink(src_path)

    def test_solve_removes_processed_temp_file(self):
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as src:
            _make_test_image(src.name)
            src_path = src.name

        processed_path = src_path.replace(".png", "_processed.png")
        try:
            with patch("captcha_solver._get_reader", return_value=self._mock_reader(["xyz"])):
                from captcha_solver import solve
                solve(src_path)
            # Preprocessed file should be cleaned up
            assert not os.path.exists(processed_path)
        finally:
            os.unlink(src_path)
            if os.path.exists(processed_path):
                os.unlink(processed_path)


# ── _get_reader (singleton) ───────────────────────────────────────────────────

class TestGetReader:

    def test_reader_is_cached(self):
        import captcha_solver
        # Reset singleton
        captcha_solver._reader = None

        mock_reader = MagicMock()
        with patch("easyocr.Reader", return_value=mock_reader) as mock_cls:
            r1 = captcha_solver._get_reader()
            r2 = captcha_solver._get_reader()
            # easyocr.Reader() should only be called once
            assert mock_cls.call_count == 1
            assert r1 is r2
