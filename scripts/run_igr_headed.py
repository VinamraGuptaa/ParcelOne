#!/usr/bin/env python3
"""Run IGR FreeSearch locally in headed mode for debugging."""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.land_case_worker import _extract_igr_party_row_for_target_survey
from igr_freesearch_scraper import IGRFreeSearchScraper


def _parse_years(raw: str | None, *, all_years: bool) -> list[str]:
    if all_years:
        y = dt.datetime.now().year
        return [str(v) for v in range(y, 2001, -1)]
    if raw:
        return [y.strip() for y in raw.split(",") if y.strip()]
    y = dt.datetime.now().year
    return [str(v) for v in range(y, y - 5, -1)]


async def main() -> None:
    parser = argparse.ArgumentParser(description="Headed IGR search for one survey/village.")
    parser.add_argument("--district", default="Pune")
    parser.add_argument("--taluka", default="Shirur")
    parser.add_argument("--village", default="Talegaon Dhamdhere")
    parser.add_argument("--survey", default="3954", help="Base survey number for IGR search")
    parser.add_argument(
        "--target-survey",
        default="3954",
        help="Full survey token to match in Property Description (e.g. 3954/1)",
    )
    parser.add_argument("--years", help="Comma-separated years (default: last 5)")
    parser.add_argument("--all-years", action="store_true", help="Search 2002 → current year")
    parser.add_argument("--headless", action="store_true", help="Run without visible browser")
    args = parser.parse_args()

    years = _parse_years(args.years, all_years=args.all_years)
    os.environ.setdefault("IGR_SAVE_RAW_HTML", "1")
    os.environ.setdefault("IGR_RAW_HTML_DIR", str(ROOT / "artifacts" / "igr_debug"))

    print(
        json.dumps(
            {
                "district": args.district,
                "taluka": args.taluka,
                "village": args.village,
                "survey": args.survey,
                "target_survey": args.target_survey,
                "years": years,
                "headless": args.headless,
            },
            ensure_ascii=False,
            indent=2,
        ),
        flush=True,
    )

    igr = IGRFreeSearchScraper(headless=args.headless)
    matched_all: list[dict] = []
    try:
        await igr.setup_driver()
        for year in years:
            print(f"\n=== IGR year {year} ===", flush=True)
            rows = await igr.search_rest_maharashtra(
                district_label=args.district,
                taluka_label=args.taluka,
                village_label=args.village,
                survey_number=args.survey,
                year=year,
            )
            matched = [
                r2
                for r in rows
                if (r2 := _extract_igr_party_row_for_target_survey(r, args.target_survey))
                is not None
            ]
            matched_all.extend(matched)
            print(
                f"IGR_YEAR {year} raw_rows={len(rows)} matched_target={len(matched)}",
                flush=True,
            )
            if matched:
                for row in matched[:3]:
                    print(
                        json.dumps(
                            {
                                "seller": row.get("seller_name"),
                                "purchaser": row.get("purchaser_name"),
                                "property": (row.get("property_description") or "")[:120],
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
    finally:
        await igr.close()

    out = ROOT / "artifacts" / "igr_debug" / f"headed_{args.survey}_summary.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "input": vars(args),
                "years_searched": years,
                "matched_rows": len(matched_all),
                "sample": matched_all[:10],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nDONE matched_total={len(matched_all)} summary={out}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
