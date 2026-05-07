"""Build a static dropdown catalog from Mahabhulekh.

Walks district -> taluka -> village via Playwright using BhulekhScraper and
serializes the result to a JSON file consumed by the frontend at
``static/data/bhulekh_catalog.json``.

Run once after a fresh checkout (or whenever the upstream Bhulekh dropdown
data needs refreshing):

    uv run python -m scripts.build_bhulekh_catalog --districts "Pune,Satara"

Survey-number options are intentionally NOT pre-built: they depend on the
chosen village + ``survey_part1`` and there are typically hundreds per
village, which would balloon the catalog and require an extra Playwright
postback per village. The frontend keeps survey-number entry as plain text,
matching the live Bhulekh form.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from bhulekh_scraper import BhulekhScraper, _LABEL_ALIASES

logger = logging.getLogger("build_bhulekh_catalog")

DEFAULT_OUTPUT = Path("static/data/bhulekh_catalog.json")

# Bhulekh dropdowns include a "--Select--" prompt that has a non-empty
# ``value`` attribute, so ``list_*`` filters do not drop them. Recognise them
# by label prefix / substring (Marathi: निवडा).
_PLACEHOLDER_LABEL_RE = re.compile(r"^--|निवडा|select|--$", re.IGNORECASE)


def _is_placeholder_label(label: str) -> bool:
    lab = (label or "").strip()
    if not lab:
        return True
    return bool(_PLACEHOLDER_LABEL_RE.search(lab))


def _english_alias_for_label(label: str) -> str | None:
    """Best-effort English alias for a Marathi-only Bhulekh option label.

    1. If the label already embeds English in parentheses (e.g. ``पुणे(Pune)``),
       returns the inner English text.
    2. Otherwise looks the label up in ``_LABEL_ALIASES`` (defined in
       :mod:`bhulekh_scraper`) and returns the English key if any variant
       matches as a substring (in either direction).
    """
    raw = (label or "").strip()
    if not raw:
        return None

    paren = re.search(r"\(([^)]+)\)", raw)
    if paren:
        inner = paren.group(1).strip()
        if inner and re.search(r"[A-Za-z]", inner):
            return inner

    lab_lower = raw.lower()
    for english_key, variants in _LABEL_ALIASES.items():
        for variant in variants:
            v = (variant or "").strip().lower()
            if not v:
                continue
            if v in lab_lower or lab_lower in v:
                # Title-case for display; preserve multi-word forms like
                # "karve nagar" -> "Karve Nagar".
                return english_key.title()
    return None


def _annotate(option: dict[str, str]) -> dict[str, str]:
    """Return a {value, label, english} dict; english may be None."""
    out = {
        "value": (option.get("value") or "").strip(),
        "label": (option.get("label") or "").strip(),
    }
    english = _english_alias_for_label(out["label"])
    if english:
        out["english"] = english
    return out


def _filter_districts(
    all_districts: list[dict[str, str]],
    requested: list[str] | None,
) -> list[dict[str, str]]:
    """Match ``requested`` against district labels (English or Marathi)."""
    if not requested:
        return all_districts
    wanted: list[dict[str, str]] = []
    for needle in requested:
        n = (needle or "").strip().lower()
        if not n:
            continue
        match = None
        for d in all_districts:
            label = (d.get("label") or "").strip()
            english = _english_alias_for_label(label) or ""
            if (
                n in label.lower()
                or label.lower() in n
                or n in english.lower()
                or english.lower() == n
            ):
                match = d
                break
        if match is None:
            logger.warning("District %r not found in Bhulekh dropdown; skipping.", needle)
            continue
        if match not in wanted:
            wanted.append(match)
    return wanted


async def _walk_district(
    scraper: BhulekhScraper,
    district: dict[str, str],
) -> dict[str, Any]:
    """Reload portal, select district, walk all talukas/villages.

    A fresh ``load_portal`` ensures the postback chain is in a known good
    state for each district. Within a district we re-select the district
    after every taluka so the village dropdown re-populates reliably.
    """
    district_value = district["value"]
    district_label = district["label"]

    talukas_out: list[dict[str, Any]] = []
    await scraper.load_portal()
    await scraper.select_district(district_value, label_hint=district_label)
    raw_talukas = await scraper.list_taluka_options()
    talukas = [t for t in raw_talukas if not _is_placeholder_label(t.get("label", ""))]
    logger.info(
        "District %r (value=%s): %s talukas (filtered %s placeholders)",
        district_label,
        district_value,
        len(talukas),
        len(raw_talukas) - len(talukas),
    )

    for idx, taluka in enumerate(talukas, start=1):
        try:
            await scraper.select_taluka(taluka["value"], label_hint=taluka["label"])
            raw_villages = await scraper.list_village_options()
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "  taluka %r failed (%s); continuing with empty village list.",
                taluka.get("label"),
                type(exc).__name__,
            )
            raw_villages = []

        villages = [
            v for v in raw_villages if not _is_placeholder_label(v.get("label", ""))
        ]
        annotated_taluka = _annotate(taluka)
        annotated_taluka["villages"] = [_annotate(v) for v in villages]
        talukas_out.append(annotated_taluka)
        logger.info(
            "  [%s/%s] taluka %r -> %s villages",
            idx,
            len(talukas),
            taluka.get("label"),
            len(villages),
        )

        # Re-select the district so the next taluka_value triggers a fresh
        # village postback. Cheaper than reloading the portal.
        if idx < len(talukas):
            try:
                await scraper.select_district(
                    district_value, label_hint=district_label
                )
            except Exception:
                # Fallback: full reload if re-select misbehaves.
                await scraper.load_portal()
                await scraper.select_district(
                    district_value, label_hint=district_label
                )

    annotated_district = _annotate(district)
    annotated_district["talukas"] = talukas_out
    return annotated_district


async def build_catalog(
    requested_districts: list[str] | None,
    output_path: Path,
    *,
    headless: bool = True,
) -> dict[str, Any]:
    scraper = BhulekhScraper(headless=headless)
    try:
        await scraper.setup_driver()
        await scraper.load_portal()
        raw_districts = await scraper.list_district_options()
        all_districts = [
            d for d in raw_districts if not _is_placeholder_label(d.get("label", ""))
        ]
        logger.info(
            "Loaded %s districts from Bhulekh (filtered %s placeholders).",
            len(all_districts),
            len(raw_districts) - len(all_districts),
        )

        target_districts = _filter_districts(all_districts, requested_districts)
        logger.info(
            "Walking %s district(s): %s",
            len(target_districts),
            [d.get("label") for d in target_districts],
        )

        out_districts: list[dict[str, Any]] = []
        for i, district in enumerate(target_districts, start=1):
            logger.info(
                "[%s/%s] Walking district %r",
                i,
                len(target_districts),
                district.get("label"),
            )
            district_payload = await _walk_district(scraper, district)
            out_districts.append(district_payload)

    finally:
        await scraper.close()

    catalog = {
        "generated_at": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
        "source": "bhulekh.mahabhumi.gov.in/NewBhulekh.aspx",
        "districts": out_districts,
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info(
        "Catalog written: %s (%s districts)",
        output_path,
        len(out_districts),
    )
    return catalog


def _parse_districts(raw: str | None) -> list[str] | None:
    if raw is None:
        return None
    parts = [p.strip() for p in re.split(r"[,;]", raw) if p.strip()]
    return parts or None


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--districts",
        type=str,
        default=None,
        help="Comma-separated district names (English or Marathi). "
        "Default: walk every district.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Output JSON path (default: {DEFAULT_OUTPUT}).",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Launch Playwright in headed mode (debugging).",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Verbose logging.",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    requested = _parse_districts(args.districts)
    asyncio.run(
        build_catalog(
            requested_districts=requested,
            output_path=args.out,
            headless=not args.headed,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
