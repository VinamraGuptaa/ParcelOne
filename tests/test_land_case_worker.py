"""Worker lifecycle tests for land-to-cases workflow."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy import select

from api.database import Base
from api.models import EcourtsApiCall, EcourtsApiCase, EcourtsRankCache, LandCaseWorkflow, WorkflowCaseHit
from api.land_case_worker import (
    _contains_exact_survey_token,
    _extract_igr_party_row_for_target_survey,
    _igr_dedupe_key,
    parse_igr_hit_raw,
)


TEST_DB_URL = "sqlite+aiosqlite:///:memory:"


async def _make_test_session_factory():
    eng = create_async_engine(
        TEST_DB_URL,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False), eng


def test_contains_exact_survey_token_latin_and_devanagari_suffix_equivalent():
    assert _contains_exact_survey_token("Survey listed as 204/6अ in document", "204/6A") is True
    assert _contains_exact_survey_token("Survey listed as 204/6A in document", "204/6अ") is True


def test_rejects_557_when_it_is_area_from_calculation():
    text = "घराची लांबी रुंदी 21.6 x 25.8 = 557 चौ.फुट पक्के बांधकाम"
    assert _contains_exact_survey_token(text, "557") is False


def test_rejects_557_in_prior_agreement_document_reference():
    text = "पूर्वगामी करारानामा क्र.557/ 2019 दि. 28/02/2019"
    assert _contains_exact_survey_token(text, "557") is False


def test_rejects_bare_557_without_survey_label():
    text = "property number 913/302 flat 302 building ramashray"
    assert _contains_exact_survey_token(text, "557") is False


def test_accepts_city_survey_number_with_label():
    text = "मौजे खांदाड सि.स.नं. 1873 क्षेत्रफळ 75.8 चौ.मी."
    assert _contains_exact_survey_token(text, "1873") is True


def test_accepts_abbreviated_gat_number_label():
    assert _contains_exact_survey_token("मिळकत ग.नं 970 बाणेर", "970") is True
    assert _contains_exact_survey_token("ग. नं. 970", "970") is True
    assert _contains_exact_survey_token("ग.नं970", "970") is True
    assert _contains_exact_survey_token("ग.नं 970 हिस्सा 3", "970/3") is True
    assert _contains_exact_survey_token("area 970 sqft", "970") is False


def test_accepts_short_gat_na_label():
    assert _contains_exact_survey_token("मिळकत गट न. 3954 तालेगाव", "3954") is True
    assert _contains_exact_survey_token("गट न.3954", "3954") is True
    assert _contains_exact_survey_token("गट न 3954", "3954") is True
    assert _contains_exact_survey_token("area 3954 sqft", "3954") is False


def test_accepts_gat_kramank_label():
    assert _contains_exact_survey_token("मिळकत गट क्रमांक 970 बाणेर", "970") is True
    assert _contains_exact_survey_token("गट क्र. 3954", "3954") is True
    assert _contains_exact_survey_token("गट क्र.3954", "3954") is True
    assert _contains_exact_survey_token("गट क्रमांक 1530 हिस्सा क्रमांक 3", "1530/3") is True
    assert _contains_exact_survey_token("पूर्वगामी करारानामा क्र.557/ 2019", "557") is False


def test_accepts_gat_comma_separated_list():
    assert _contains_exact_survey_token("गट नं 3952,3954,3927,3137/1 बाबत", "3954") is True
    assert _contains_exact_survey_token("गट 3137/1,3927,3952,3954 बाबत", "3954") is True
    assert _contains_exact_survey_token("गट नं 3952,39540,3927 बाबत", "3954") is False
    assert _contains_exact_survey_token("मिळकती 3952,3954,3927 बाबत", "3954") is False


def test_accepts_old_survey_number_with_label():
    text = "जुना सर्वे नंबर 70/2ब नवीन गट 576/ ब"
    assert _contains_exact_survey_token(text, "70/2") is False
    assert _contains_exact_survey_token(text, "70/2ब") is True


def test_accepts_gat_hissa_with_devanagari_suffix_for_latin_target():
    text = "गट नंबर 204 हिस्सा नंबर 6अ"
    assert _contains_exact_survey_token(text, "204/6A") is True


def test_accepts_devanagari_digits_in_slash_notation():
    text = "गट नंबर २०४/६अ"
    assert _contains_exact_survey_token(text, "204/6A") is True


def test_accepts_comma_separated_hissa_notation():
    text = "204,हिस्सा नं. 6अ,यांस"
    assert _contains_exact_survey_token(text, "204/6A") is True
    assert _contains_exact_survey_token("204, हिस्सा नं. 6A", "204/6A") is True
    assert _contains_exact_survey_token("204,हिस्सा 7/1", "204/6A") is False


def test_accepts_comma_hi_na_with_kshetra():
    assert _contains_exact_survey_token("28,हि.नं.1,क्षेत्र", "28/1") is True
    assert _contains_exact_survey_token("28,हि.नं1,क्षेत्र", "28/1") is True
    assert _contains_exact_survey_token("204,हि.नं.6अ,क्षेत्र", "204/6A") is True
    assert _contains_exact_survey_token("1530,हि.नं.3,क्षेत्रफळ", "1530/3") is True
    assert _contains_exact_survey_token("28,हि.नं.2,क्षेत्र", "28/1") is False


def test_accepts_slash_notation_with_kshetra():
    assert _contains_exact_survey_token("28/1 क्षेत्र", "28/1") is True
    assert _contains_exact_survey_token("28/1,क्षेत्र", "28/1") is True
    assert _contains_exact_survey_token("28/1क्षेत्र", "28/1") is True
    assert _contains_exact_survey_token("204/6A क्षेत्रफळ 100", "204/6A") is True
    assert _contains_exact_survey_token("28/2 क्षेत्र", "28/1") is False


def test_filters_full_target_survey_not_base_only():
    """IGR portal search uses base 204; result filter must require full 204/6A."""
    target = "204/6A"
    assert _contains_exact_survey_token("गट नंबर 204 हिस्सा 6अ", target) is True
    assert _contains_exact_survey_token("204/6A near village road", target) is True
    assert _contains_exact_survey_token("गट नंबर 204 हिस्सा 3", target) is False
    assert _contains_exact_survey_token("204/3", target) is False
    assert _contains_exact_survey_token("गट 204 only", target) is False


def test_rejects_subsurvey_prefix_when_target_has_shorter_hissa():
    assert _contains_exact_survey_token("मिळकत 1530/30", "1530/3") is False
    assert _contains_exact_survey_token("मिळकत 2040", "204") is False


def test_still_matches_gat_hissa_notation():
    text = "गट नंबर 1530 हिस्सा नंबर 3 जमीन"
    assert _contains_exact_survey_token(text, "1530/3") is True


def test_extract_igr_party_row_requires_target_survey_and_parties():
    row = {
        "Property Description": "गट नंबर 1530 हिस्सा नंबर 3 जमीन",
        "Seller Name": "Alice Seller",
        "Purchaser Name": "Bob Buyer",
    }
    matched = _extract_igr_party_row_for_target_survey(row, "1530/3")
    assert matched is not None
    assert matched["seller_name"] == "Alice Seller"
    assert matched["purchaser_name"] == "Bob Buyer"
    assert matched["property_description"] == row["Property Description"]
    assert matched["matched_target_survey"] == "1530/3"


def test_extract_igr_party_row_rejects_wrong_hissa_or_missing_parties():
    assert (
        _extract_igr_party_row_for_target_survey(
            {
                "Property Description": "मिळकत 1530/30",
                "Seller Name": "Alice Seller",
            },
            "1530/3",
        )
        is None
    )
    assert _extract_igr_party_row_for_target_survey({"Property Description": "1530/3"}, "1530/3") is None


def test_extract_igr_party_row_accepts_alternate_column_keys():
    matched = _extract_igr_party_row_for_target_survey(
        {
            "PropertyDescription": "204,हि.नं.6अ,क्षेत्र",
            "SellerName": "Alice Seller",
            "PurchaserName": "Bob Buyer",
        },
        "204/6A",
    )
    assert matched is not None
    assert matched["seller_name"] == "Alice Seller"


def test_igr_dedupe_key_collapses_parallel_slice_duplicates():
    row1 = {
        "search_year": "2025",
        "matched_target_survey": "1530/3",
        "seller_name": " Alice Seller ",
        "purchaser_name": "Bob Buyer",
        "property_description": "गट नंबर 1530 हिस्सा नंबर 3",
    }
    row2 = {
        "search_year": "2025",
        "survey_number": "1530/3",
        "Seller Name": "Alice  Seller",
        "Purchaser Name": "Bob Buyer",
        "Property Description": "गट नंबर 1530 हिस्सा नंबर 3",
    }
    assert _igr_dedupe_key(row1) == _igr_dedupe_key(row2)


def test_parse_igr_hit_raw_splits_braced_parties():
    parsed = parse_igr_hit_raw(
        {
            "DocNo": "1001",
            "DName": "खरेदीखत",
            "RDate": "01/01/2025",
            "SROName": "Haveli",
            "Seller Name": "{Alice Seller}{Carol Seller}",
            "Purchaser Name": "{Bob Buyer}",
        }
    )
    assert parsed["doc_type"] == "Sale deed"
    assert parsed["seller"] == "Alice Seller"
    assert parsed["buyer"] == "Bob Buyer"
    assert parsed["sellers"] == ["Alice Seller", "Carol Seller"]


def _mock_bhulekh():
    mock = MagicMock()
    mock.setup_driver = AsyncMock()
    mock.run_search_with_labels = AsyncMock(
        return_value="""
        <html><body>
        <select id="ContentPlaceHolder1_ddlsurveyno">
          <option value="">--निवडा--</option>
          <option value="1530/1">1530/1</option>
          <option value="1530/2">1530/2</option>
          <option value="1530/3">1530/3</option>
        </select>
        <table>
          <tr><td>Name of the occupant</td><td>Snehal Bhushan Dhut</td></tr>
          <tr><td>Mutation number</td><td>(20133)</td></tr>
        </table>
        </body></html>
        """
    )
    mock.save_verification_pdf = AsyncMock(return_value="artifacts/workflows/test.pdf")
    mock.close = AsyncMock()
    return mock


def _mock_ecourts(records):
    mock = MagicMock()
    mock.setup_driver = AsyncMock()
    mock.navigate_and_select = AsyncMock()
    mock.search_petitioner = AsyncMock(return_value=records)
    mock.close = AsyncMock()
    return mock


def _mock_igr(records):
    mock = MagicMock()
    mock.setup_driver = AsyncMock()
    mock.search_rest_maharashtra = AsyncMock(return_value=records)
    mock.close = AsyncMock()
    return mock


async def _create_workflow(session_factory):
    async with session_factory() as session:
        wf = LandCaseWorkflow(
            district_label="Pune",
            taluka_label="Haveli",
            village_label="Wagholi",
            survey_part1="1530",
            survey_option_label="1530/3",
            status="pending_input",
        )
        session.add(wf)
        await session.commit()
        await session.refresh(wf)
        return wf


class TestLandCaseWorker:
    async def test_marks_ranked_done_and_persists_hits(self, monkeypatch):
        session_factory, eng = await _make_test_session_factory()
        wf = await _create_workflow(session_factory)
        monkeypatch.setenv("ECOURTS_ALLOW_SCRAPER_FALLBACK", "1")

        recs = [
            {
                "Case Type/Case Number/Case Year": "RCA/10/2024",
                "Petitioner Name versus Respondent Name": "Snehal Bhushan Dhut vs State",
                "CNR_Number": "CNRX",
                "Case_Type": "Regular Civil Appeal",
            }
        ]
        bh = _mock_bhulekh()
        ec = _mock_ecourts(recs)
        igr = _mock_igr(
            [{"survey_number": "1530/1", "search_year": "2024", "village_label": "Wagholi", "doc": "x"}]
        )

        with patch("api.land_case_worker.AsyncSessionLocal", session_factory), patch(
            "bhulekh_scraper.BhulekhScraper", return_value=bh
        ), patch("scraper.HybridECourtsScraper", return_value=ec), patch(
            "igr_freesearch_scraper.IGRFreeSearchScraper", return_value=igr
        ):
            from api.land_case_worker import run_land_case_workflow

            await run_land_case_workflow(wf.id)

        async with session_factory() as session:
            result = await session.execute(select(LandCaseWorkflow).where(LandCaseWorkflow.id == wf.id))
            final = result.scalar_one()
            assert final.status == "ranked_done"
            assert final.total_hits >= 1
            hit_rows = (
                await session.execute(select(WorkflowCaseHit).where(WorkflowCaseHit.workflow_id == wf.id))
            ).scalars().all()
            assert len(hit_rows) >= 1
            assert hit_rows[0].is_civil is True
        survey_options_path = Path("artifacts/workflows") / f"{wf.id}_survey_options.json"
        assert survey_options_path.exists()
        payload = json.loads(survey_options_path.read_text(encoding="utf-8"))
        assert payload["survey_options"] == ["1530/1", "1530/2", "1530/3"]
        ranked_csv_path = Path("artifacts/workflows") / f"{wf.id}_ranked_hits.csv"
        assert ranked_csv_path.exists()
        ranked_csv = ranked_csv_path.read_text(encoding="utf-8")
        header = ranked_csv.split("\n", 1)[0]
        assert "final_rank" in header and "search_year" in header and "is_civil" not in header

        await eng.dispose()

    async def test_parallel_igr_slices_merge_and_dedupe(self, monkeypatch):
        session_factory, eng = await _make_test_session_factory()
        wf = await _create_workflow(session_factory)
        monkeypatch.setenv("ECOURTS_ALLOW_SCRAPER_FALLBACK", "1")
        monkeypatch.setenv("IGR_PARALLEL_CONTEXTS", "2")

        recs = [
            {
                "Case Type/Case Number/Case Year": "RCA/10/2024",
                "Petitioner Name versus Respondent Name": "Snehal Bhushan Dhut vs State",
                "CNR_Number": "CNRX",
                "Case_Type": "Regular Civil Appeal",
            }
        ]
        bh = _mock_bhulekh()
        ec = _mock_ecourts(recs)
        igr_row = {
            "Property Description": "Land parcel includes survey 1530/3 near main road",
            "Seller Name": "Seller A",
            "Purchaser Name": "Purchaser A",
            "search_year": "2024",
        }
        igr1 = _mock_igr([igr_row])
        igr2 = _mock_igr([igr_row])

        with patch("api.land_case_worker.AsyncSessionLocal", session_factory), patch(
            "bhulekh_scraper.BhulekhScraper", return_value=bh
        ), patch("scraper.HybridECourtsScraper", return_value=ec), patch(
            "igr_freesearch_scraper.IGRFreeSearchScraper", side_effect=[igr1, igr2]
        ):
            from api.land_case_worker import run_land_case_workflow

            await run_land_case_workflow(wf.id)

        async with session_factory() as session:
            from api.models import WorkflowIgrHit

            rows = (
                await session.execute(select(WorkflowIgrHit).where(WorkflowIgrHit.workflow_id == wf.id))
            ).scalars().all()
            assert len(rows) == 1
            result = await session.execute(select(LandCaseWorkflow).where(LandCaseWorkflow.id == wf.id))
            final = result.scalar_one()
            assert final.status == "ranked_done"

        assert igr1.setup_driver.await_count == 1
        assert igr2.setup_driver.await_count == 1
        await eng.dispose()

    async def test_parallel_igr_slice_failure_marks_workflow_failed(self, monkeypatch):
        session_factory, eng = await _make_test_session_factory()
        wf = await _create_workflow(session_factory)
        monkeypatch.setenv("ECOURTS_ALLOW_SCRAPER_FALLBACK", "1")
        monkeypatch.setenv("IGR_PARALLEL_CONTEXTS", "2")

        bh = _mock_bhulekh()
        ec = _mock_ecourts([])
        igr1 = _mock_igr([])
        igr2 = _mock_igr([])

        async def _slice1_search(**kwargs):
            year = str(kwargs.get("year") or "")
            if year == "2015":
                raise RuntimeError("igr slice error")
            return []

        igr1.search_rest_maharashtra = AsyncMock(side_effect=_slice1_search)
        igr2.search_rest_maharashtra = AsyncMock(return_value=[])

        with patch("api.land_case_worker.AsyncSessionLocal", session_factory), patch(
            "bhulekh_scraper.BhulekhScraper", return_value=bh
        ), patch("scraper.HybridECourtsScraper", return_value=ec), patch(
            "igr_freesearch_scraper.IGRFreeSearchScraper", side_effect=[igr1, igr2]
        ):
            from api.land_case_worker import run_land_case_workflow

            await run_land_case_workflow(wf.id)

        async with session_factory() as session:
            result = await session.execute(select(LandCaseWorkflow).where(LandCaseWorkflow.id == wf.id))
            final = result.scalar_one()
            assert final.status == "failed"
            assert "igr_running failed" in (final.error_message or "")

        await eng.dispose()

    async def test_igr_uses_base_survey_and_filters_target_survey_option_in_property_description(self, monkeypatch):
        session_factory, eng = await _make_test_session_factory()
        wf = await _create_workflow(session_factory)
        monkeypatch.setenv("ECOURTS_ALLOW_SCRAPER_FALLBACK", "1")

        recs = [
            {
                "Case Type/Case Number/Case Year": "RCA/10/2024",
                "Petitioner Name versus Respondent Name": "Snehal Bhushan Dhut vs State",
                "CNR_Number": "CNRX",
                "Case_Type": "Regular Civil Appeal",
            }
        ]
        bh = _mock_bhulekh()
        ec = _mock_ecourts(recs)
        igr = _mock_igr(
            [
                {
                    "Property Description": "Land parcel includes survey 1530/3 near main road",
                    "Seller Name": "Seller A",
                    "Purchaser Name": "Purchaser A",
                    "search_year": "2024",
                },
                {
                    "Property Description": "Land parcel includes survey 1530/1 nearby",
                    "Seller Name": "Seller B",
                    "Purchaser Name": "Purchaser B",
                    "search_year": "2024",
                },
                {
                    "Property Description": "Land parcel includes survey 1530/30 nearby",
                    "Seller Name": "Seller C",
                    "Purchaser Name": "Purchaser C",
                    "search_year": "2024",
                },
            ]
        )

        with patch("api.land_case_worker.AsyncSessionLocal", session_factory), patch(
            "bhulekh_scraper.BhulekhScraper", return_value=bh
        ), patch("scraper.HybridECourtsScraper", return_value=ec), patch(
            "igr_freesearch_scraper.IGRFreeSearchScraper", return_value=igr
        ):
            from api.land_case_worker import run_land_case_workflow

            await run_land_case_workflow(wf.id)

        # Ensure IGR search uses base survey part1, not each sibling as direct input.
        called_kwargs = igr.search_rest_maharashtra.await_args_list[0].kwargs
        assert called_kwargs["survey_number"] == "1530"

        # Persisted records should only include rows matching selected survey option 1530/3.
        async with session_factory() as session:
            from api.models import WorkflowIgrHit

            rows = (
                await session.execute(select(WorkflowIgrHit).where(WorkflowIgrHit.workflow_id == wf.id))
            ).scalars().all()
            assert len(rows) > 0
            raws = [json.loads(r.raw_json) for r in rows]
            assert all(raw.get("matched_target_survey") == "1530/3" for raw in raws)
            assert all(raw.get("seller_name") == "Seller A" for raw in raws)
            assert all(raw.get("purchaser_name") == "Purchaser A" for raw in raws)

        await eng.dispose()

    async def test_marks_failed_on_exception(self, monkeypatch):
        session_factory, eng = await _make_test_session_factory()
        wf = await _create_workflow(session_factory)
        monkeypatch.setenv("ECOURTS_ALLOW_SCRAPER_FALLBACK", "1")
        bh = _mock_bhulekh()
        bh.run_search_with_labels = AsyncMock(side_effect=RuntimeError("bhulekh failed"))
        ec = _mock_ecourts([])
        igr = _mock_igr([])

        with patch("api.land_case_worker.AsyncSessionLocal", session_factory), patch(
            "bhulekh_scraper.BhulekhScraper", return_value=bh
        ), patch("scraper.HybridECourtsScraper", return_value=ec), patch(
            "igr_freesearch_scraper.IGRFreeSearchScraper", return_value=igr
        ):
            from api.land_case_worker import run_land_case_workflow

            await run_land_case_workflow(wf.id)

        async with session_factory() as session:
            result = await session.execute(select(LandCaseWorkflow).where(LandCaseWorkflow.id == wf.id))
            final = result.scalar_one()
            assert final.status == "failed"
            assert "bhulekh failed" in (final.error_message or "")
        await eng.dispose()

    async def test_fails_when_ecourts_api_key_missing_and_fallback_disabled(self, monkeypatch):
        session_factory, eng = await _make_test_session_factory()
        wf = await _create_workflow(session_factory)
        bh = _mock_bhulekh()
        ec = _mock_ecourts([])
        igr = _mock_igr([])

        monkeypatch.delenv("ECOURTS_API_KEY", raising=False)
        monkeypatch.delenv("ECOURTS_ALLOW_SCRAPER_FALLBACK", raising=False)

        with patch("api.land_case_worker.AsyncSessionLocal", session_factory), patch(
            "bhulekh_scraper.BhulekhScraper", return_value=bh
        ), patch("scraper.HybridECourtsScraper", return_value=ec), patch(
            "igr_freesearch_scraper.IGRFreeSearchScraper", return_value=igr
        ):
            from api.land_case_worker import run_land_case_workflow

            await run_land_case_workflow(wf.id)

        async with session_factory() as session:
            result = await session.execute(select(LandCaseWorkflow).where(LandCaseWorkflow.id == wf.id))
            final = result.scalar_one()
            assert final.status == "failed"
            assert "ECOURTS_API_KEY is missing or invalid" in (final.error_message or "")
        ec.search_petitioner.assert_not_called()
        await eng.dispose()

    async def test_uses_ecourts_api_when_key_present(self, monkeypatch):
        session_factory, eng = await _make_test_session_factory()
        wf = await _create_workflow(session_factory)
        bh = _mock_bhulekh()
        ec = _mock_ecourts([])
        igr = _mock_igr([])

        class _FakeApiClient:
            def __init__(self, *args, **kwargs):
                self.metrics = type("M", (), {})()
                self.metrics.search_requests = 1
                self.metrics.detail_requests = 1
                self.metrics.refresh_requests = 0
                self.metrics.total_requests = 2
                self.metrics.estimated_cost_inr = 0.7
                self.metrics.request_log = [
                    {
                        "kind": "case_search_get",
                        "endpoint": "/search",
                        "method": "GET",
                        "status_code": 200,
                        "request_params": {"litigants": "Snehal Bhushan Dhut"},
                        "response_json": {"data": [{"cnr": "CNRX"}]},
                    },
                    {
                        "kind": "case_detail_get",
                        "endpoint": "/case/CNRX",
                        "method": "GET",
                        "status_code": 200,
                        "request_params": None,
                        "response_json": {"cnr": "CNRX", "court": "Court A"},
                    },
                ]

            async def search_cases(self, **kwargs):
                return [
                    {
                        "case_id": "RCA/10/2024",
                        "cnr": "CNRX",
                        "case_type": "Regular Civil Appeal",
                        "case_status": "Pending",
                        "parties_text": "Snehal Bhushan Dhut vs State",
                        "search_year": "2024",
                    }
                ]

            async def get_case_detail(self, cnr):
                return {
                    "data": {
                        "courtCaseData": {
                            "cnr": cnr,
                            "caseType": "RCA",
                            "caseTypeRaw": "Regular Civil Appeal",
                            "caseStatus": "PENDING",
                            "courtName": "Court A",
                            "courtNo": 2,
                            "district": "Pune",
                            "state": "MH",
                            "caseNumber": "RCA/10/2024",
                            "cnrYear": "2024",
                            "filingNumber": "FN-1",
                            "filingDate": "2024-01-11",
                            "registrationNumber": "RN-1",
                            "registrationDate": "2024-01-12",
                            "firstHearingDate": "2024-02-01",
                            "nextHearingDate": "2024-03-01",
                            "decisionDate": None,
                            "petitioners": ["Snehal Bhushan Dhut"],
                            "respondents": ["State"],
                            "petitionerAdvocates": ["Adv A"],
                            "respondentAdvocates": ["Adv B"],
                            "caseCategoryFacetPath": "Civil/Appeal",
                        }
                    }
                }

            async def close(self):
                return None

        monkeypatch.setenv("ECOURTS_API_KEY", "eci_test_key_123456")
        with patch("api.land_case_worker.AsyncSessionLocal", session_factory), patch(
            "bhulekh_scraper.BhulekhScraper", return_value=bh
        ), patch("scraper.HybridECourtsScraper", return_value=ec), patch(
            "igr_freesearch_scraper.IGRFreeSearchScraper", return_value=igr
        ), patch(
            "api.ecourts_api_client.EcourtsApiClient", _FakeApiClient
        ):
            from api.land_case_worker import run_land_case_workflow

            await run_land_case_workflow(wf.id)

        async with session_factory() as session:
            result = await session.execute(select(LandCaseWorkflow).where(LandCaseWorkflow.id == wf.id))
            final = result.scalar_one()
            assert final.status == "ranked_done"
            assert final.total_hits >= 1
            assert final.ecourts_api_metrics_json is not None
            call_rows = (await session.execute(select(EcourtsApiCall))).scalars().all()
            case_rows = (await session.execute(select(EcourtsApiCase))).scalars().all()
            cache_rows = (await session.execute(select(EcourtsRankCache))).scalars().all()
            assert len(call_rows) >= 1
            assert len(case_rows) >= 1
            assert len(cache_rows) >= 1
            assert call_rows[0].request_params_json is not None
            assert call_rows[0].response_json is not None
            assert case_rows[0].case_type_raw == "Regular Civil Appeal"
            assert case_rows[0].court_no == "2"
            assert case_rows[0].district == "Pune"
            assert case_rows[0].state == "MH"
            assert case_rows[0].case_number == "RCA/10/2024"
            assert case_rows[0].petitioners_json is not None
            assert case_rows[0].respondents_json is not None
            assert case_rows[0].is_civil is True
            assert case_rows[0].final_rank == 1
        ec.setup_driver.assert_not_called()
        await eng.dispose()

    async def test_uses_rank_cache_and_skips_api_search(self, monkeypatch):
        session_factory, eng = await _make_test_session_factory()
        wf = await _create_workflow(session_factory)
        bh = _mock_bhulekh()
        ec = _mock_ecourts([])
        igr = _mock_igr([])

        async with session_factory() as session:
            session.add(
                EcourtsRankCache(
                    owner_name_norm="snehal bhushan dhut",
                    district_label="pune",
                    taluka_label="haveli",
                    village_label="wagholi",
                    survey_token="1530/3",
                    source_mode="api",
                    cached_ranked_json=json.dumps(
                        [
                            {
                                "case_id": "RCA/10/2024",
                                "cnr": "CNRX",
                                "case_type": "Regular Civil Appeal",
                                "case_status": "Pending",
                                "parties_text": "Snehal Bhushan Dhut vs State",
                                "search_year": "2024",
                            }
                        ]
                    ),
                    expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
                )
            )
            await session.commit()

        class _FakeApiClient:
            def __init__(self, *args, **kwargs):
                self.metrics = type("M", (), {})()
                self.metrics.search_requests = 0
                self.metrics.detail_requests = 0
                self.metrics.refresh_requests = 0
                self.metrics.total_requests = 0
                self.metrics.estimated_cost_inr = 0.0
                self.metrics.request_log = []

            async def search_cases(self, **kwargs):
                raise AssertionError("search_cases should not run on cache hit")

            async def get_case_detail(self, cnr):
                raise AssertionError("get_case_detail should not run on cache hit")

            async def close(self):
                return None

        monkeypatch.setenv("ECOURTS_API_KEY", "eci_test_key_123456")
        with patch("api.land_case_worker.AsyncSessionLocal", session_factory), patch(
            "bhulekh_scraper.BhulekhScraper", return_value=bh
        ), patch("scraper.HybridECourtsScraper", return_value=ec), patch(
            "igr_freesearch_scraper.IGRFreeSearchScraper", return_value=igr
        ), patch(
            "api.ecourts_api_client.EcourtsApiClient", _FakeApiClient
        ):
            from api.land_case_worker import run_land_case_workflow

            await run_land_case_workflow(wf.id)

        async with session_factory() as session:
            result = await session.execute(select(LandCaseWorkflow).where(LandCaseWorkflow.id == wf.id))
            final = result.scalar_one()
            assert final.status == "ranked_done"
            metrics = json.loads(final.ecourts_api_metrics_json or "{}")
            assert metrics.get("cache_hit") is True
            assert metrics.get("api_requests_saved", 0) >= 1
        await eng.dispose()


def test_igr_years_span_2002_through_current():
    from api.land_case_worker import _igr_years_from_2002_to_current

    years = _igr_years_from_2002_to_current()
    current = datetime.now().year
    assert years[0] == str(current)
    assert years[-1] == "2002"
    assert len(years) == current - 2002 + 1


def test_ecourts_stage_does_not_reset_igr_year_counters():
    """years_total/years_done must stay at IGR values through eCourts completion."""
    src = Path(__file__).resolve().parents[1].joinpath("api", "land_case_worker.py").read_text(encoding="utf-8")
    assert "wf.years_total = 1" not in src
    assert "wf.years_done = 1" not in src
