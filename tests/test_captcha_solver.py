"""
Unit tests for captcha_solver.py.

RapidOCR (ONNX Runtime) is mocked to keep tests fast and avoid loading
the ~130MB model. Image I/O uses real PIL operations on small synthetic images.

RapidOCR result format:  (result_list, elapse)
  result_list: [ [bbox, text, confidence], ... ]  or  None
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


def _mock_engine(texts: list[str]):
    """
    Return a mock RapidOCR instance whose __call__ returns a result list
    in the format: ([ [bbox, text, conf], ... ], elapse)
    """
    mock = MagicMock()
    result_list = [[None, t, 0.99] for t in texts] if texts else None
    mock.return_value = (result_list, 0.01)
    return mock


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

    def test_solve_returns_string(self):
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as src:
            _make_test_image(src.name)
            src_path = src.name

        try:
            with patch("captcha_solver._get_engine", return_value=_mock_engine(["abc123"])):
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
            with patch("captcha_solver._get_engine", return_value=_mock_engine(["ab", "cd"])):
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
            with patch("captcha_solver._get_engine", return_value=_mock_engine(["ab cd"])):
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
            with patch("captcha_solver._get_engine", return_value=_mock_engine([])):
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
            with patch("captcha_solver._get_engine", return_value=_mock_engine(["xyz"])):
                from captcha_solver import solve
                solve(src_path)
            assert not os.path.exists(processed_path)
        finally:
            os.unlink(src_path)
            if os.path.exists(processed_path):
                os.unlink(processed_path)

    def test_solve_strips_non_alphanumeric(self):
        """Punctuation and symbols stripped; only alphanumeric kept."""
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as src:
            _make_test_image(src.name)
            src_path = src.name

        try:
            with patch("captcha_solver._get_engine", return_value=_mock_engine(["A3!X#9"])):
                from captcha_solver import solve
                result = solve(src_path)
            assert result == "A3X9"
        finally:
            os.unlink(src_path)


# ── _get_engine (singleton) ───────────────────────────────────────────────────

class TestGetEngine:

    def test_engine_is_cached(self):
        import captcha_solver
        # Reset singleton
        captcha_solver._engine = None

        mock_engine = MagicMock()
        with patch("rapidocr_onnxruntime.RapidOCR", return_value=mock_engine) as mock_cls:
            e1 = captcha_solver._get_engine()
            e2 = captcha_solver._get_engine()
            # RapidOCR() should only be called once
            assert mock_cls.call_count == 1
            assert e1 is e2

    def test_engine_returns_same_instance_on_repeat_calls(self):
        import captcha_solver
        captcha_solver._engine = None

        with patch("rapidocr_onnxruntime.RapidOCR", return_value=MagicMock()):
            e1 = captcha_solver._get_engine()
            e2 = captcha_solver._get_engine()
        assert e1 is e2
