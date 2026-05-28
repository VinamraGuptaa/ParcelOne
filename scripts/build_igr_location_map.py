#!/usr/bin/env python3
"""Build Bhulekh → IGR location label map by walking both dropdown trees.

Reads ``static/data/bhulekh_catalog.json`` (or ``--catalog``) and opens the IGR
Rest-of-Maharashtra form to list district/taluka/village options at each level.
Writes ``static/data/igr_location_map.json`` used at runtime by
:func:`api.location_labels.resolve_igr_labels`.

Examples::

    # Single district (fast smoke test)
    uv run python -m scripts.build_igr_location_map --districts Pune --headed

    # All districts in catalog (~35 districts, may take 1–3 hours)
    uv run python -m scripts.build_igr_location_map
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
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.location_labels import (
    DEFAULT_MAP_PATH,
    best_option_match,
    build_lookup_from_district_tree,
    is_placeholder_label,
    sanitize_label,
)
from igr_freesearch_scraper import (
    IGRFreeSearchScraper,
    SEL_DISTRICT,
    SEL_TALUKA,
    SEL_VILLAGE,
)

logger = logging.getLogger("build_igr_location_map")

DEFAULT_CATALOG = Path("static/data/bhulekh_catalog.json")


def _filter_catalog_districts(catalog: dict, requested: list[str] | None) -> list[dict]:
    districts = catalog.get("districts") or []
    if not requested:
        return districts
    wanted: list[dict] = []
    for needle in requested:
        n = sanitize_label(needle).lower()
        if not n:
            continue
        for d in districts:
            label = sanitize_label(d.get("label", "")).lower()
            english = sanitize_label(d.get("english", "")).lower()
            if n in label or label in n or n == english or n in english:
                if d not in wanted:
                    wanted.append(d)
                break
        else:
            logger.warning("District %r not found in Bhulekh catalog.", needle)
    return wanted


def _match_payload(
    bhulekh_label: str,
    options: list[dict[str, str]],
    *,
    level: str,
) -> dict[str, Any]:
    match = best_option_match(bhulekh_label, options)
    if match is None:
        logger.warning("  UNMATCHED %s: bhulekh=%r options=%s", level, bhulekh_label, len(options))
        return {
            "bhulekh": {"label": bhulekh_label},
            "igr": None,
            "match_method": "unmatched",
        }
    logger.info(
        "  matched %s bhulekh=%r -> igr=%r (%s score=%.2f)",
        level,
        bhulekh_label,
        match.label,
        match.method,
        match.score,
    )
    return {
        "bhulekh": {"label": bhulekh_label},
        "igr": {"label": match.label, "value": match.value},
        "match_method": match.method,
        "score": match.score,
    }


async def _walk_district(
    igr: IGRFreeSearchScraper,
    bhulekh_district: dict,
) -> dict[str, Any]:
    district_label = bhulekh_district.get("label", "")
    await igr._reload_portal_search_tab()
    await igr._ensure_rest_maharashtra_form_ready()

    district_options = await igr.list_location_options("district")
    district_node = _match_payload(district_label, district_options, level="district")
    if district_node.get("igr") is None:
        district_node["talukas"] = []
        return district_node

    igr_district_label = district_node["igr"]["label"]
    ok = await igr._select_by_label_alias(SEL_DISTRICT, igr_district_label)
    if not ok:
        logger.warning("Failed to select IGR district %r", igr_district_label)
        district_node["talukas"] = []
        return district_node
    await igr._wait_for_postback_settle(timeout_s=12.0)
    await igr._wait_for_option_growth(SEL_TALUKA, min_count=2)
    await igr._wait_for_select_populated(SEL_TALUKA, timeout_s=12.0)

    taluka_nodes: list[dict] = []
    for taluka in bhulekh_district.get("talukas") or []:
        taluka_label = taluka.get("label", "")
        taluka_options = await igr.list_location_options("taluka")
        taluka_node = _match_payload(taluka_label, taluka_options, level="taluka")
        if taluka_node.get("igr") is None:
            taluka_node["villages"] = []
            taluka_nodes.append(taluka_node)
            continue

        igr_taluka_label = taluka_node["igr"]["label"]
        tok = await igr._select_by_label_alias(SEL_TALUKA, igr_taluka_label)
        if not tok:
            logger.warning("Failed to select IGR taluka %r", igr_taluka_label)
            taluka_node["villages"] = []
            taluka_nodes.append(taluka_node)
            continue
        await igr._wait_for_postback_settle(timeout_s=12.0)
        await igr._wait_for_option_growth(SEL_VILLAGE, min_count=2)
        await igr._wait_for_select_populated(SEL_VILLAGE, timeout_s=15.0)

        village_options = await igr.list_location_options("village")

        village_nodes: list[dict] = []
        for village in taluka.get("villages") or []:
            village_label = village.get("label", "")
            village_node = _match_payload(village_label, village_options, level="village")
            village_nodes.append(village_node)

        taluka_node["villages"] = village_nodes
        taluka_nodes.append(taluka_node)

        # Re-select district for next taluka village dropdown refresh.
        await igr._select_by_label_alias(SEL_DISTRICT, igr_district_label)
        await igr._wait_for_postback_settle(timeout_s=10.0)

    district_node["talukas"] = taluka_nodes
    return district_node


async def build_map(
    *,
    catalog_path: Path,
    output_path: Path,
    requested_districts: list[str] | None,
    headless: bool,
) -> dict[str, Any]:
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    target_districts = _filter_catalog_districts(catalog, requested_districts)
    logger.info(
        "Building IGR map for %s district(s) from %s",
        len(target_districts),
        catalog_path,
    )

    igr = IGRFreeSearchScraper(headless=headless)
    out_districts: list[dict] = []
    try:
        await igr.setup_driver()
        for idx, district in enumerate(target_districts, start=1):
            logger.info(
                "[%s/%s] District %r",
                idx,
                len(target_districts),
                district.get("label"),
            )
            out_districts.append(await _walk_district(igr, district))
    finally:
        await igr.close()

    payload: dict[str, Any] = {
        "generated_at": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
        "source_bhulekh_catalog": str(catalog_path),
        "source_igr_portal": "igr.maharashtra.gov.in",
        "version": 1,
        "districts": out_districts,
    }
    payload["lookup"] = build_lookup_from_district_tree(out_districts)

    unmatched = sum(
        1
        for d in out_districts
        for t in d.get("talukas") or []
        for v in t.get("villages") or []
        if v.get("match_method") == "unmatched"
    )
    payload["stats"] = {
        "districts": len(out_districts),
        "lookup_entries": len(payload["lookup"]),
        "unmatched_villages": unmatched,
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("Wrote %s (lookup=%s unmatched_villages=%s)", output_path, len(payload["lookup"]), unmatched)
    return payload


def _parse_districts(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    parts = [p.strip() for p in re.split(r"[,;]", raw) if p.strip()]
    return parts or None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--out", type=Path, default=DEFAULT_MAP_PATH)
    parser.add_argument("--districts", type=str, default=None, help="Comma-separated district filter")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if not args.catalog.is_file():
        logger.error("Bhulekh catalog not found: %s", args.catalog)
        return 1

    asyncio.run(
        build_map(
            catalog_path=args.catalog,
            output_path=args.out,
            requested_districts=_parse_districts(args.districts),
            headless=not args.headed,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
