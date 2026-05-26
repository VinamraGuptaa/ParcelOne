"""API tests for /api/workflows land-case endpoints."""

from pathlib import Path
from unittest.mock import AsyncMock, patch
import api.routes.workflows as workflows_routes

class TestWorkflowApi:
    async def test_create_workflow_returns_202(self, client):
        with patch("api.routes.workflows.run_land_case_workflow", new_callable=AsyncMock), patch.object(
            workflows_routes,
            "ACTIVE_WORKFLOW_STATUSES",
            ("bhulekh_running", "name_variants_ready", "igr_running", "ecourts_running"),
        ):
            resp = await client.post(
                "/api/workflows/land-case-search",
                json={
                    "district_label": "Pune",
                    "taluka_label": "Haveli",
                    "village_label": "Wagholi",
                    "survey_part1": "1530",
                    "survey_option_label": "1530/3",
                },
            )
        assert resp.status_code == 202
        body = resp.json()
        assert body["status"] == "pending_input"
        assert "workflow_id" in body

    async def test_create_workflow_prefills_district_taluka_defaults(self, client):
        with patch("api.routes.workflows.run_land_case_workflow", new_callable=AsyncMock), patch.object(
            workflows_routes,
            "ACTIVE_WORKFLOW_STATUSES",
            ("bhulekh_running", "name_variants_ready", "igr_running", "ecourts_running"),
        ):
            resp = await client.post(
                "/api/workflows/land-case-search",
                json={
                    "village_label": "Wagholi",
                    "survey_part1": "1530",
                    "survey_option_label": "1530/3",
                },
            )
        assert resp.status_code == 202
        body = resp.json()
        assert body["district_label"] == "Pune"
        assert body["taluka_label"] == "Haveli"

    async def test_idempotency_key_reuses_existing_workflow(self, client):
        with patch("api.routes.workflows.run_land_case_workflow", new_callable=AsyncMock) as mock_run, patch.object(
            workflows_routes,
            "ACTIVE_WORKFLOW_STATUSES",
            ("bhulekh_running", "name_variants_ready", "igr_running", "ecourts_running"),
        ):
            payload = {
                "district_label": "Pune",
                "taluka_label": "Haveli",
                "village_label": "Wagholi",
                "survey_part1": "1530",
                "survey_option_label": "1530/3",
                "idempotency_key": "abc-123",
            }
            r1 = await client.post("/api/workflows/land-case-search", json=payload)
            r2 = await client.post("/api/workflows/land-case-search", json=payload)
        assert r1.status_code == 202
        assert r2.status_code == 202
        assert r1.json()["workflow_id"] == r2.json()["workflow_id"]
        assert mock_run.await_count == 1

    async def test_create_workflow_returns_409_when_another_is_active(self, client, engine):
        from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession
        from api.models import LandCaseWorkflow

        Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with Session() as session:
            session.add(
                LandCaseWorkflow(
                    district_label="Pune",
                    taluka_label="Haveli",
                    village_label="Wagholi",
                    survey_part1="1530",
                    survey_option_label="1530/3",
                    status="ecourts_running",
                    progress_message="Running...",
                )
            )
            await session.commit()

        with patch("api.routes.workflows.run_land_case_workflow", new_callable=AsyncMock):
            payload = {
                "district_label": "Pune",
                "taluka_label": "Haveli",
                "village_label": "Wagholi",
                "survey_part1": "1530",
                "survey_option_label": "1530/3",
            }
            resp = await client.post("/api/workflows/land-case-search", json=payload)
        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert "already in progress" in (detail["message"] if isinstance(detail, dict) else detail)
        if isinstance(detail, dict):
            assert "active_workflow_id" in detail

    async def test_get_unknown_workflow_404(self, client):
        resp = await client.get("/api/workflows/not-found")
        assert resp.status_code == 404

    async def test_list_workflows_returns_valid_shape(self, client):
        resp = await client.get("/api/workflows")
        assert resp.status_code == 200
        body = resp.json()
        assert "workflows" in body
        assert "total" in body
        assert isinstance(body["workflows"], list)
        assert isinstance(body["total"], int)
        assert len(body["workflows"]) <= body["total"]

    async def test_list_workflows_returns_summaries(self, client, engine):
        from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession
        from api.models import LandCaseWorkflow

        Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        created_id = None
        async with Session() as session:
            wf_obj = LandCaseWorkflow(
                district_label="Pune",
                taluka_label="Haveli",
                village_label="Wagholi_list_test",
                survey_part1="9999",
                survey_option_label="9999/list",
                status="ranked_done",
                progress_message="Done.",
                total_hits=2,
            )
            session.add(wf_obj)
            await session.commit()
            await session.refresh(wf_obj)
            created_id = wf_obj.id

        resp = await client.get("/api/workflows")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] >= 1
        # Find the specific workflow we created by id.
        match = next((w for w in body["workflows"] if w["workflow_id"] == created_id), None)
        assert match is not None, f"Created workflow {created_id} not found in list"
        assert match["village_label"] == "Wagholi_list_test"
        assert match["survey_option_label"] == "9999/list"
        assert match["status"] == "ranked_done"
        assert match["total_hits"] == 2
        assert "created_at" in match

    async def test_results_and_artifacts_shape(self, client, engine):
        from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession
        from sqlalchemy import select
        from api.models import (
            EcourtsApiCall,
            EcourtsApiCase,
            LandCaseWorkflow,
            LandEntity,
            NameVariant,
            WorkflowCaseHit,
            WorkflowIgrHit,
        )

        Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with Session() as session:
            wf = LandCaseWorkflow(
                district_label="Pune",
                taluka_label="Haveli",
                village_label="Wagholi",
                survey_part1="1530",
                survey_option_label="1530/3",
                status="ranked_done",
                pdf_path="artifacts/x.pdf",
                html_path="artifacts/x.html",
                years_total=15,
                years_done=15,
                total_hits=1,
                ecourts_api_metrics_json='{"provider":"ecourts_api","total_requests":2,"estimated_cost_inr":0.7}',
            )
            session.add(wf)
            await session.commit()
            await session.refresh(wf)
            session.add(
                LandEntity(
                    workflow_id=wf.id,
                    occupant_primary_name="Snehal Bhushan Dhut",
                    occupant_candidates_json='["Snehal Bhushan Dhut"]',
                    mutation_numbers_json='["20133"]',
                    extraction_confidence=0.9,
                    source="html",
                )
            )
            session.add(
                NameVariant(
                    workflow_id=wf.id,
                    base_name="Snehal Bhushan Dhut",
                    variant_text="snehal bhushan dhut",
                    variant_kind="normalized",
                    quality_score=1.0,
                )
            )
            session.add(
                WorkflowCaseHit(
                    workflow_id=wf.id,
                    search_year="2024",
                    case_id="RCA/1/2024",
                    cnr_number="X",
                    case_type="Regular Civil Appeal",
                    parties_text="Snehal Bhushan Dhut vs State",
                    is_civil=True,
                    name_match_score=0.99,
                    matched_variant="snehal bhushan dhut",
                    match_explanation="exact_substring",
                    final_rank=1,
                    raw_json="{}",
                )
            )
            session.add(
                WorkflowIgrHit(
                    workflow_id=wf.id,
                    survey_number="1530/1",
                    search_year="2024",
                    district_label="Pune",
                    taluka_label="Haveli",
                    village_label="Wagholi",
                    source_region="rest_of_maharashtra",
                    raw_json='{"survey_number":"1530/1","search_year":"2024","Purchaser Name":"Buyer One"}',
                )
            )
            session.add(
                EcourtsApiCall(
                    workflow_id=wf.id,
                    owner_name_query="Snehal Bhushan Dhut",
                    request_kind="case_search_get",
                    endpoint="/search",
                    method="GET",
                    request_params_json='{"litigants":"Snehal Bhushan Dhut"}',
                    response_status=200,
                    response_json='{"data":[]}',
                    provider_error_code=None,
                    retryable=False,
                    is_success=True,
                )
            )
            session.add(
                EcourtsApiCase(
                    workflow_id=wf.id,
                    cnr_number="X",
                    case_type="CC",
                    case_type_raw="Ct Cases",
                    court="Court A",
                    court_no="2",
                    district="New Delhi",
                    state="DL",
                    case_number="202400248072016",
                    cnr_year="2015",
                    filing_number="27843/2015",
                    filing_date="2015-12-21",
                    registration_number="24807/2016",
                    registration_date="2015-12-21",
                    first_hearing_date="2016-01-05",
                    next_hearing_date="2018-07-07",
                    decision_date="2018-07-07",
                    petitioners_json='["MR. ARUN JAITLEY"]',
                    respondents_json='["MR. ARVIND KEJRIWAL"]',
                    petitioner_advocates_json="[]",
                    respondent_advocates_json="[]",
                    case_category_facet_path="Criminal Law/Other Criminal Matters",
                    parties_text="Snehal Bhushan Dhut vs State",
                    case_status="DISPOSED",
                    is_civil=False,
                    is_pending=True,
                    final_rank=1,
                    source_stage="detail",
                    raw_json='{"cnr":"X"}',
                )
            )
            await session.commit()
            workflow_id = wf.id

        r_results = await client.get(f"/api/workflows/{workflow_id}/results")
        assert r_results.status_code == 200
        body = r_results.json()
        assert body["workflow_id"] == workflow_id
        assert body["entity"]["occupant_primary_name"] == "Snehal Bhushan Dhut"
        assert len(body["variants"]) == 1
        assert isinstance(body["survey_options"], list)
        assert len(body["hits"]) == 1
        assert len(body["igr_hits"]) == 1
        assert body["igr_purchaser_names"] == ["Buyer One"]
        assert body["ecourts_api_metrics"]["provider"] == "ecourts_api"
        assert body["ecourts_api_metrics"]["estimated_cost_inr"] == 0.7
        assert len(body["ecourts_api_calls"]) == 1
        assert body["ecourts_api_calls"][0]["endpoint"] == "/search"
        assert len(body["ecourts_api_cases"]) == 1
        assert body["ecourts_api_cases"][0]["cnr_number"] == "X"
        assert body["ecourts_api_cases"][0]["case_type_raw"] == "Ct Cases"
        assert body["ecourts_api_cases"][0]["court_no"] == "2"
        assert body["ecourts_api_cases"][0]["petitioners"] == ["MR. ARUN JAITLEY"]
        assert body["ecourts_api_cases"][0]["final_rank"] == 1

        r_artifacts = await client.get(f"/api/workflows/{workflow_id}/artifacts")
        assert r_artifacts.status_code == 200
        assert r_artifacts.json()["pdf_path"] == "artifacts/x.pdf"
        assert "ranked_csv_path" in r_artifacts.json()
        csv_path = Path("artifacts/workflows") / f"{workflow_id}_ranked_hits.csv"
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        csv_path.write_text("final_rank,case_id\n1,RCA/1/2024\n", encoding="utf-8")
        r_artifacts_2 = await client.get(f"/api/workflows/{workflow_id}/artifacts")
        assert r_artifacts_2.status_code == 200
        assert r_artifacts_2.json()["ranked_csv_path"] == str(csv_path)
