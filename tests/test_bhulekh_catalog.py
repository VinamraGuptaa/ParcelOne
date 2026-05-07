"""Tests for ``scripts/build_bhulekh_catalog.py``.

These tests exercise the pure-Python pieces of the catalog builder
(English alias resolution, district filtering, JSON shape) without
launching Playwright. The Bhulekh interaction surface is mocked by a
fake scraper that returns canned dropdown options.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from scripts.build_bhulekh_catalog import (
    _english_alias_for_label,
    _filter_districts,
    build_catalog,
)


class _FakeBhulekhScraper:
    """Minimal stand-in for :class:`bhulekh_scraper.BhulekhScraper`.

    Drives the same API surface used by ``build_catalog`` -
    ``setup_driver``, ``load_portal``, ``list_district_options``,
    ``select_district``, ``list_taluka_options``, ``select_taluka``,
    ``list_village_options`` and ``close``.
    """

    def __init__(
        self,
        layout: dict[str, dict[str, list[dict[str, str]]]],
    ) -> None:
        self._layout = layout
        self._current_district: str | None = None

    @classmethod
    def factory(cls, layout):
        def _build(*, headless=True):  # noqa: ARG001 - mimics real ctor signature
            return cls(layout)

        return _build

    async def setup_driver(self) -> None:
        pass

    async def load_portal(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def list_district_options(self):
        return [
            {"value": v, "label": data["label"]}
            for v, data in self._layout.items()
        ]

    async def select_district(self, value, *, label_hint=""):
        self._current_district = value

    async def list_taluka_options(self):
        d = self._layout[self._current_district]
        return [
            {"value": k, "label": tdata["label"]}
            for k, tdata in d["talukas"].items()
        ]

    async def select_taluka(self, value, *, label_hint=""):
        self._current_taluka = value

    async def list_village_options(self):
        d = self._layout[self._current_district]
        return d["talukas"][self._current_taluka]["villages"]


def _sample_layout() -> dict[str, Any]:
    return {
        "25": {
            "label": "पुणे",
            "talukas": {
                "7": {
                    "label": "हवेली",
                    "villages": [
                        {"value": "v1", "label": "म .कर्वेनगर"},
                        {"value": "v2", "label": "वाघोली"},
                    ],
                }
            },
        },
        "30": {
            "label": "सातारा",
            "talukas": {
                "1": {
                    # Parenthesised English is a common Bhulekh pattern -
                    # exercise that fallback in `_english_alias_for_label`.
                    "label": "कोरेगाव(Koregaon)",
                    "villages": [
                        {"value": "v3", "label": "धामणे"},
                    ],
                }
            },
        },
    }


class TestEnglishAlias:
    def test_returns_inner_english_when_label_has_parens(self):
        assert _english_alias_for_label("पुणे(Pune)") == "Pune"

    def test_returns_label_aliased_value(self):
        assert _english_alias_for_label("पुणे") == "Pune"
        assert _english_alias_for_label("कर्वेनगर") == "Karve Nagar"

    def test_returns_none_for_unknown_label(self):
        assert _english_alias_for_label("कुठेच नाही") is None


class TestFilterDistricts:
    def test_filters_by_english_alias(self):
        all_d = [
            {"value": "25", "label": "पुणे"},
            {"value": "30", "label": "सातारा"},
        ]
        out = _filter_districts(all_d, ["Pune"])
        assert [d["value"] for d in out] == ["25"]

    def test_returns_all_when_no_filter(self):
        all_d = [{"value": "25", "label": "पुणे"}]
        assert _filter_districts(all_d, None) == all_d

    def test_skips_unknown_district(self):
        all_d = [{"value": "25", "label": "पुणे"}]
        out = _filter_districts(all_d, ["NoSuchPlace"])
        assert out == []


@pytest.mark.asyncio
async def test_build_catalog_produces_expected_shape(tmp_path: Path):
    layout = _sample_layout()
    out_file = tmp_path / "catalog.json"

    with patch(
        "scripts.build_bhulekh_catalog.BhulekhScraper",
        _FakeBhulekhScraper.factory(layout),
    ):
        catalog = await build_catalog(
            requested_districts=["Pune", "Satara"],
            output_path=out_file,
        )

    assert out_file.exists()
    on_disk = json.loads(out_file.read_text(encoding="utf-8"))
    assert on_disk == catalog

    assert "generated_at" in catalog
    assert "districts" in catalog
    assert len(catalog["districts"]) == 2

    pune = next(d for d in catalog["districts"] if d["value"] == "25")
    assert pune["label"] == "पुणे"
    assert pune["english"] == "Pune"
    assert len(pune["talukas"]) == 1
    haveli = pune["talukas"][0]
    assert haveli["english"] == "Haveli"
    assert {v["value"] for v in haveli["villages"]} == {"v1", "v2"}
    karve = next(v for v in haveli["villages"] if v["value"] == "v1")
    assert karve["english"] == "Karve Nagar"

    satara = next(d for d in catalog["districts"] if d["value"] == "30")
    assert satara["english"] == "Satara"
    assert satara["talukas"][0]["english"] == "Koregaon"


@pytest.mark.asyncio
async def test_build_catalog_filters_by_district(tmp_path: Path):
    layout = _sample_layout()
    out_file = tmp_path / "catalog.json"

    with patch(
        "scripts.build_bhulekh_catalog.BhulekhScraper",
        _FakeBhulekhScraper.factory(layout),
    ):
        catalog = await build_catalog(
            requested_districts=["Pune"],
            output_path=out_file,
        )

    assert [d["value"] for d in catalog["districts"]] == ["25"]
