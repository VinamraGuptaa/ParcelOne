"""Regression: PropertySearchForm must not crash when catalog entries omit english."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FORM_SRC = ROOT / "frontend" / "src" / "pages" / "search" / "PropertySearchForm.tsx"


def test_formatLabel_handles_missing_english_field():
    """Bhulekh catalog often has label-only entries; .english is optional."""
    src = FORM_SRC.read_text(encoding="utf-8")
    assert "item.english ?? ''" in src or "(item.english ?? '')" in src
    assert "item.english.trim()" not in src
