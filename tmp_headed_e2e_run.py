import asyncio
import datetime as dt
import json
import time
from pathlib import Path

from api.land_case_flow import (
    build_name_variants,
    dedupe_case_key,
    extract_land_entity,
    extract_survey_option_labels,
    rank_case_hits,
    write_html_artifact,
)
from api.land_case_worker import _extract_igr_party_row_for_target_survey
from bhulekh_scraper import BhulekhScraper
from igr_freesearch_scraper import IGRFreeSearchScraper
from scraper import HybridECourtsScraper


def last_15_years() -> list[str]:
    y = dt.datetime.now().year
    return [str(v) for v in range(y, y - 15, -1)]


def igr_years_from_2002() -> list[str]:
    y = dt.datetime.now().year
    return [str(v) for v in range(y, 2001, -1)]


async def main() -> None:
    started_at = time.monotonic()
    district = "Pune"
    taluka = "Haveli"
    village = "बाणेर"
    survey_option = "70/6"
    survey_part1 = survey_option.split("/")[0]

    artifacts_dir = Path("artifacts/workflows")
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    bhulekh = BhulekhScraper(headless=False)
    igr = IGRFreeSearchScraper(headless=False)
    ecourts = HybridECourtsScraper(headless=False)

    summary: dict = {
        "input": {
            "district": district,
            "taluka": taluka,
            "village": village,
            "survey_option": survey_option,
            "survey_part1": survey_part1,
            "years": last_15_years(),
            "igr_years": igr_years_from_2002(),
        }
    }

    try:
        await bhulekh.setup_driver()
        html = await bhulekh.run_search_with_labels(
            district_label=district,
            taluka_label=taluka,
            village_label=village,
            survey_part1=survey_part1,
            survey_option_label=survey_option,
        )
        pdf_path = await bhulekh.save_verification_pdf(artifacts_dir / "headed_e2e_70_6_land_record.pdf")
        html_path = write_html_artifact(artifacts_dir, "headed_e2e_70_6", html)
        print("BHULEKH_DONE", flush=True)

        survey_options = extract_survey_option_labels(html, survey_part1)
        entity = extract_land_entity(html=html, pdf_path=str(pdf_path))
        variants = build_name_variants(entity.occupant_primary_name or "")
        summary["bhulekh"] = {
            "pdf_path": str(pdf_path),
            "html_path": str(html_path),
            "survey_options": survey_options,
            "occupant_primary_name": entity.occupant_primary_name,
            "mutation_numbers": entity.mutation_numbers,
            "variant_count": len(variants),
        }

        await igr.setup_driver()
        igr_all = []
        for year in igr_years_from_2002():
            rows = await igr.search_rest_maharashtra(
                district_label=district,
                taluka_label=taluka,
                village_label=village,
                survey_number=survey_part1,
                year=year,
            )
            matched = [
                r2 for r in rows if (r2 := _extract_igr_party_row_for_target_survey(r, survey_option)) is not None
            ]
            igr_all.extend(matched)
            print(f"IGR_YEAR {year} matched={len(matched)} raw={len(rows)}", flush=True)

        summary["igr"] = {
            "matched_rows": len(igr_all),
            "sample": igr_all[:5],
        }
        print("IGR_DONE", flush=True)

        await ecourts.setup_driver()
        await ecourts.navigate_and_select()
        dedupe: set[str] = set()
        collected: list[dict] = []

        for year in last_15_years():
            before = len(collected)
            for variant in variants:
                records = await ecourts.search_petitioner(variant.variant_text, year)
                for rec in records:
                    rec["Search_Year"] = year
                    key = dedupe_case_key(rec)
                    if key in dedupe:
                        continue
                    dedupe.add(key)
                    collected.append(rec)
            print(f"ECOURTS_YEAR {year} added={len(collected)-before} cumulative={len(collected)}", flush=True)

        ranked = rank_case_hits(collected, variants, min_score=0.0)
        summary["ecourts"] = {
            "deduped_records": len(collected),
            "ranked_hits": len(ranked),
            "top10": [
                {
                    "rank": i + 1,
                    "case_id": h.case_id,
                    "cnr_number": h.cnr_number,
                    "search_year": h.search_year,
                    "is_civil": h.is_civil,
                    "name_match_score": h.name_match_score,
                    "matched_variant": h.matched_variant,
                }
                for i, h in enumerate(ranked[:10])
            ],
        }

        out_path = artifacts_dir / "headed_e2e_70_6_summary.json"
        summary["timing"] = {"total_elapsed_seconds": round(time.monotonic() - started_at, 2)}
        out_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print("E2E_SUMMARY_PATH=" + str(out_path), flush=True)
        print("E2E_SUMMARY=" + json.dumps(summary, ensure_ascii=False), flush=True)
    finally:
        try:
            await bhulekh.close()
        except Exception:
            pass
        try:
            await igr.close()
        except Exception:
            pass
        try:
            await ecourts.close()
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())
