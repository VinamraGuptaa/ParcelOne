#!/usr/bin/env python3
"""
eCourts India Case Scraper - CLI Entry Point

Scrapes case data by petitioner name from the eCourts India portal.
Pre-configured for: Maharashtra → Pune → Pune, District and Sessions Court.

Usage:
    python main.py "Rajesh Gupta" --year 2017
    python main.py "Rajesh Gupta"                   # last 10 years
    python main.py "Rajesh Gupta" --output my_results.csv

Maharashtra Bhulekh (7/12 by survey) — separate subcommand:
    python main.py bhulekh --list-districts
    python main.py bhulekh --snapshot --district-value <v> --taluka-value <v>
    python main.py bhulekh --district-value ... --taluka-value ... --village-value ...
        --survey-part1 "1" --survey-number-value "12" --output bhulekh_out.html
"""

import argparse
import asyncio
import json
import sys

from scraper import ECourtsScraper, HybridECourtsScraper


async def _run(args):
    if args.dump_network:
        scraper = ECourtsScraper(headless=args.headless)
    else:
        scraper = HybridECourtsScraper(headless=args.headless)
    try:
        print("\n[1/4] Setting up scraper...")
        await scraper.setup_driver()

        if args.dump_network:
            print("[2/4] Navigating to eCourts and selecting court...")
            await scraper.navigate_and_select()

        print(f"[3/4] Searching for '{args.petitioner_name}'...")
        if args.dump_network:
            year = args.year or str(__import__("datetime").datetime.now().year)
            print(f"[Phase0] Running network dump for year {year}...")
            await scraper._dump_network(args.petitioner_name, year)
            print("[Phase0] Done. See /tmp/ecourts_net.json")
            return
        elif args.year:
            results = await scraper.scrape_single_year(args.petitioner_name, args.year)
        else:
            results = await scraper.scrape_all_years(args.petitioner_name)

        print("[4/4] Exporting results...")
        if results:
            scraper.export_to_csv(results, args.output)
            print(f"\n Successfully scraped {len(results)} records.")
            print(f"   Results saved to: {args.output}")

            print("\n--- Preview (first 5 records) ---")
            import pandas as pd
            df = pd.DataFrame(results)
            preview_cols = [c for c in [
                "Sr No", "Case Type/Case Number/Case Year",
                "Petitioner Name versus Respondent Name",
                "CNR Number", "Case_Type", "Filing_Number", "Filing_Date",
                "Registration_Number", "Registration_Date",
                "First_Hearing_Date", "Decision_Date", "Case_Status",
                "Nature_of_Disposal", "Court_Number_Judge",
                "Petitioner_and_Advocate", "Respondent_and_Advocate",
                "Search_Year",
            ] if c in df.columns]
            print(df[preview_cols].head().to_string() if preview_cols else df.head().to_string())
        else:
            print("\n  No records found.")

    except KeyboardInterrupt:
        print("\n\n  Scraping interrupted by user.")
    except Exception as e:
        error_text = str(e).strip() or f"{type(e).__name__}: {e!r}"
        print(f"\n Error: {error_text}")
        import traceback
        traceback.print_exc()
    finally:
        await scraper.close()
        print("\nDone.")


async def _run_bhulekh(args):
    from bhulekh_scraper import BhulekhScraper, BhulekhSearchParams, save_document_html

    scraper = BhulekhScraper(headless=args.headless)
    should_close = True
    try:
        await scraper.setup_driver()

        if args.list_districts:
            await scraper.load_portal()
            districts = await scraper.list_district_options()
            print(json.dumps(districts, indent=2, ensure_ascii=False))
            return

        if args.snapshot:
            snap = await scraper.collect_dropdown_snapshot(
                args.district_value,
                args.taluka_value,
            )
            print(json.dumps(snap, indent=2, ensure_ascii=False))
            return

        if args.district_label:
            html = await scraper.run_search_with_labels(
                args.district_label,
                args.taluka_label,
                args.village_label,
                args.survey_part1,
                args.survey_option_label,
                mobile=args.mobile,
            )
        else:
            params = BhulekhSearchParams(
                district_value=args.district_value,
                taluka_value=args.taluka_value,
                village_value=args.village_value,
                survey_part1=args.survey_part1,
                survey_number_value=args.survey_number_value,
                mobile=args.mobile,
            )
            html = await scraper.run_search(params)
        pdf_path = await scraper.save_verification_pdf(args.output)
        print(f"\nBhulekh verification PDF saved to: {pdf_path}")
        if args.save_html:
            html_path = save_document_html(html, args.html_output)
            print(f"Bhulekh HTML saved to: {html_path}")
        if args.save_submit_assets:
            assets = await scraper.save_submit_artifacts(
                pdf_path,
                include_pdf=args.save_submit_pdf,
                max_downloads=args.max_submit_downloads,
            )
            if assets:
                print("Saved submit artifacts:")
                for p in assets:
                    print(f"  - {p}")
            else:
                print("No submit artifacts were detected/saved.")
        if args.keep_browser_open and not args.headless:
            should_close = False
            print("\nBrowser left open for manual verification. Press Ctrl+C here to finish.")
            try:
                while True:
                    await asyncio.sleep(3600)
            except KeyboardInterrupt:
                print("\nClosing browser...")
    finally:
        if should_close:
            await scraper.close()
        print("\nDone.")


def _parse_bhulekh_args(argv):
    p = argparse.ArgumentParser(
        description="Maharashtra Bhulekh — NewBhulekh.aspx (7/12 by survey)",
        prog="python main.py bhulekh",
    )
    p.add_argument(
        "--headless",
        action="store_true",
        help="Run Chromium headless",
    )
    p.add_argument(
        "--keep-browser-open",
        action="store_true",
        help="Keep headed browser open after submit for manual verification",
    )
    p.add_argument(
        "--list-districts",
        action="store_true",
        help="Print all district <option> values and labels, then exit",
    )
    p.add_argument(
        "--snapshot",
        action="store_true",
        help="Print districts + talukas + villages for --district-value and --taluka-value",
    )
    p.add_argument("--district-value", default="", help="District <select> value code")
    p.add_argument("--taluka-value", default="", help="Taluka <select> value code")
    p.add_argument("--village-value", default="", help="Village <select> value code")
    p.add_argument(
        "--survey-part1",
        default="",
        help="Survey number (part 1) — numeric key before choosing survey list",
    )
    p.add_argument(
        "--survey-number-value",
        default="",
        help="Survey number dropdown value after part-1 search (when using coded path)",
    )
    p.add_argument(
        "--district-label",
        default="",
        help="Partial district label match, e.g. Pune — alternative to --district-value",
    )
    p.add_argument(
        "--taluka-label",
        default="",
        help="Taluka label match, e.g. Haveli — use with --district-label",
    )
    p.add_argument(
        "--village-label",
        default="",
        help="Village label match, e.g. Wagholi",
    )
    p.add_argument(
        "--survey-option-label",
        default="",
        help='Survey line in dropdown after part-1 search, e.g. "1530/3"',
    )
    p.add_argument(
        "--mobile",
        default="9999999999",
        help="10-digit Indian mobile (default test number)",
    )
    p.add_argument(
        "--output",
        default="artifacts/bhulekh_document.pdf",
        help="Where to save verification PDF after submit",
    )
    p.add_argument(
        "--save-html",
        action="store_true",
        help="Also save raw HTML response (optional debug artifact)",
    )
    p.add_argument(
        "--html-output",
        default="artifacts/bhulekh_document.html",
        help="Where to save HTML when --save-html is set",
    )
    p.add_argument(
        "--save-submit-assets",
        action="store_true",
        help="Save screenshot + detected doc/image resources after submit",
    )
    p.add_argument(
        "--save-submit-pdf",
        action="store_true",
        help="Also save submitted_page.pdf under the *_assets folder (in addition to the default verification PDF)",
    )
    p.add_argument(
        "--max-submit-downloads",
        type=int,
        default=10,
        help="Maximum linked document/image resources to download (default: 10)",
    )
    args = p.parse_args(argv)

    if args.snapshot:
        if not args.district_value or not args.taluka_value:
            p.error("--snapshot requires --district-value and --taluka-value")

    need_full = not args.list_districts and not args.snapshot
    if need_full:
        if args.district_label:
            missing = [
                n
                for n, v in (
                    ("district_label", args.district_label),
                    ("taluka_label", args.taluka_label),
                    ("village_label", args.village_label),
                    ("survey_part1", args.survey_part1),
                    ("survey_option_label", args.survey_option_label),
                )
                if not v
            ]
            if missing:
                p.error(
                    "Label-based search needs: --district-label, --taluka-label, "
                    "--village-label, --survey-part1, --survey-option-label "
                    f"(missing: {', '.join(missing)})"
                )
        else:
            missing = [
                n
                for n, v in (
                    ("district_value", args.district_value),
                    ("taluka_value", args.taluka_value),
                    ("village_value", args.village_value),
                    ("survey_part1", args.survey_part1),
                    ("survey_number_value", args.survey_number_value),
                )
                if not v
            ]
            if missing:
                p.error(
                    "Coded search requires: --district-value, --taluka-value, "
                    "--village-value, --survey-part1, --survey-number-value "
                    f"(missing: {', '.join(missing)}) "
                    "or use --district-label … --survey-option-label for named places."
                )

    return args


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "bhulekh":
        args = _parse_bhulekh_args(sys.argv[2:])
        asyncio.run(_run_bhulekh(args))
        return

    parser = argparse.ArgumentParser(
        description="eCourts India Case Scraper - Search by Petitioner Name",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            '  python main.py "Rajesh Gupta" --year 2017\n'
            '  python main.py "Rajesh Gupta" --output results.csv\n'
            '  python main.py "Rajesh Gupta"  # scrape last 10 years\n'
            "  python main.py bhulekh --list-districts\n"
            "  python main.py bhulekh --district-value X --taluka-value Y ...\n"
        ),
    )
    parser.add_argument(
        "petitioner_name",
        help="Name of the petitioner/respondent to search for (min 3 characters)",
    )
    parser.add_argument(
        "--year",
        default="",
        help="Registration year to search (e.g., 2017). If omitted, scrapes last 10 years.",
    )
    parser.add_argument(
        "--output",
        default="results.csv",
        help="Output CSV filename (default: results.csv)",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run browser in headless mode (no visible window)",
    )
    parser.add_argument(
        "--dump-network",
        action="store_true",
        help="Phase 0: intercept POST requests and dump to /tmp/ecourts_net.json (dev only)",
    )

    args = parser.parse_args()

    if len(args.petitioner_name) < 3:
        print("Error: Petitioner name must be at least 3 characters.")
        sys.exit(1)

    print("=" * 60)
    print("eCourts India Case Scraper")
    print("=" * 60)
    print(f"  Petitioner Name : {args.petitioner_name}")
    print(f"  Year            : {args.year or 'Last 15 years'}")
    print(f"  Output File     : {args.output}")
    print(f"  Headless        : {args.headless}")
    print("  State           : Maharashtra")
    print("  District        : Pune")
    print("  Court Complex   : Pune, District and Sessions Court")
    print("  Case Status     : Both (Pending + Disposed)")
    print("=" * 60)

    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
