"""Contract tests for ``GET /api/workflows/{id}/artifact/{kind}``."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.models import LandCaseWorkflow


async def _seed_workflow(
    engine,
    *,
    pdf_path: str | None = None,
    html_path: str | None = None,
) -> str:
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with Session() as session:
        wf = LandCaseWorkflow(
            district_label="Pune",
            taluka_label="Haveli",
            village_label="Wagholi",
            survey_part1="1530",
            survey_option_label="1530/3",
            status="ranked_done",
            pdf_path=pdf_path,
            html_path=html_path,
        )
        session.add(wf)
        await session.commit()
        await session.refresh(wf)
        return wf.id


class TestArtifactRoute:
    async def test_unknown_workflow_returns_404(self, client):
        resp = await client.get("/api/workflows/missing/artifact/pdf")
        assert resp.status_code == 404

    async def test_unknown_kind_returns_400(self, client, engine):
        wf_id = await _seed_workflow(engine)
        resp = await client.get(f"/api/workflows/{wf_id}/artifact/zip")
        assert resp.status_code == 400
        assert "Unsupported" in resp.json()["detail"]

    async def test_pdf_missing_file_returns_404(self, client, engine):
        wf_id = await _seed_workflow(engine)
        resp = await client.get(f"/api/workflows/{wf_id}/artifact/pdf")
        assert resp.status_code == 404

    async def test_streams_csv_with_correct_content_type(
        self, client, engine, tmp_path: Path, monkeypatch
    ):
        artifacts_root = tmp_path / "workflows"
        artifacts_root.mkdir(parents=True, exist_ok=True)
        # Patch ARTIFACTS_ROOT so the test does not touch the repo's
        # real artifacts directory.
        monkeypatch.setattr(
            "api.routes.workflows.ARTIFACTS_ROOT", artifacts_root.resolve()
        )

        wf_id = await _seed_workflow(engine)
        csv_file = artifacts_root / f"{wf_id}_ranked_hits.csv"
        csv_file.write_text("final_rank,case_id\n1,RCA/1/2024\n", encoding="utf-8")

        resp = await client.get(f"/api/workflows/{wf_id}/artifact/csv")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/csv")
        assert "RCA/1/2024" in resp.text

    async def test_streams_html_artifact(self, client, engine, tmp_path: Path, monkeypatch):
        artifacts_root = tmp_path / "workflows"
        artifacts_root.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(
            "api.routes.workflows.ARTIFACTS_ROOT", artifacts_root.resolve()
        )

        wf_id = await _seed_workflow(engine)
        html_file = artifacts_root / f"{wf_id}_submitted.html"
        html_file.write_text("<html>ok</html>", encoding="utf-8")

        resp = await client.get(f"/api/workflows/{wf_id}/artifact/html")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")
        assert "<html>ok</html>" in resp.text

    async def test_pdf_path_outside_root_is_rejected(
        self, client, engine, tmp_path: Path, monkeypatch
    ):
        artifacts_root = tmp_path / "workflows"
        artifacts_root.mkdir(parents=True, exist_ok=True)

        # Write a "hostile" pdf outside the artifacts root and store its path
        # on the workflow row.
        outside = tmp_path / "secret.pdf"
        outside.write_bytes(b"%PDF-1.4 fake")
        monkeypatch.setattr(
            "api.routes.workflows.ARTIFACTS_ROOT", artifacts_root.resolve()
        )
        wf_id = await _seed_workflow(engine, pdf_path=str(outside))

        resp = await client.get(f"/api/workflows/{wf_id}/artifact/pdf")
        # Path is outside ARTIFACTS_ROOT and no fallback file exists, so 404.
        assert resp.status_code == 404
