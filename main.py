#!/usr/bin/env python3
"""
eCourts India Case Scraper - CLI Entry Point

Scrapes case data by petitioner name from the eCourts India portal.
Pre-configured for: Maharashtra → Pune → Pune, District and Sessions Court.

Usage:
    python main.py "Rajesh Gupta" --year 2017
    python main.py "Rajesh Gupta"                   # last 10 years
    python main.py "Rajesh Gupta" --output my_results.csv
"""

import argparse
import asyncio
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


def main():
    parser = argparse.ArgumentParser(
        description="eCourts India Case Scraper - Search by Petitioner Name",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            '  python main.py "Rajesh Gupta" --year 2017\n'
            '  python main.py "Rajesh Gupta" --output results.csv\n'
            '  python main.py "Rajesh Gupta"  # scrape last 10 years'
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
