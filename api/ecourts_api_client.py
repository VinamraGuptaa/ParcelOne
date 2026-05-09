"""Official eCourts API client with cost-aware request accounting."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)


ECOURTS_API_BASE_URL = "https://webapi.ecourtsindia.com/api/partner"

# User-provided base rates (enterprise 1x column in shared table).
ECOURTS_RATE_CARD = {
    "case_search_get": 0.20,
    "case_detail_get": 0.50,
    "case_refresh_post": 0.25,
    "causelist_search_get": 1.00,
    "order_download_get": 1.25,
    "order_ai_get": 2.50,
    "order_markdown_get": 1.75,
}


def _split_csv_env(value: str) -> list[str]:
    return [part.strip() for part in (value or "").split(",") if part.strip()]


def is_valid_ecourts_api_key(value: str) -> bool:
    token = (value or "").strip()
    return bool(token) and token.startswith("eci_") and len(token) >= 16


@dataclass
class EcourtsApiMetrics:
    search_requests: int = 0
    detail_requests: int = 0
    refresh_requests: int = 0
    total_requests: int = 0
    estimated_cost_inr: float = 0.0
    request_log: list[dict[str, Any]] = field(default_factory=list)

    def add(
        self,
        kind: str,
        endpoint: str,
        *,
        method: str | None = None,
        status_code: int | None = None,
        attempt: int | None = None,
        retryable: bool | None = None,
        error_type: str | None = None,
        provider_code: str | None = None,
        request_params: dict[str, Any] | None = None,
        response_json: Any | None = None,
        from_cache: bool = False,
    ) -> None:
        if from_cache:
            return
        self.total_requests += 1
        if kind == "case_search_get":
            self.search_requests += 1
        elif kind == "case_detail_get":
            self.detail_requests += 1
        elif kind == "case_refresh_post":
            self.refresh_requests += 1
        self.estimated_cost_inr = round(
            self.search_requests * ECOURTS_RATE_CARD["case_search_get"]
            + self.detail_requests * ECOURTS_RATE_CARD["case_detail_get"]
            + self.refresh_requests * ECOURTS_RATE_CARD["case_refresh_post"],
            2,
        )
        self.request_log.append(
            {
                "kind": kind,
                "endpoint": endpoint,
                "method": method,
                "status_code": status_code,
                "attempt": attempt,
                "retryable": retryable,
                "error_type": error_type,
                "provider_code": provider_code,
                "request_params": request_params,
                "response_json": response_json,
                "estimated_cost_inr_after_request": self.estimated_cost_inr,
            }
        )


class EcourtsApiClient:
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout_seconds: float = 30.0,
        max_retries: int = 3,
        retry_delay_seconds: float = 1.0,
    ) -> None:
        self.api_key = api_key or os.getenv("ECOURTS_API_KEY", "").strip()
        self.base_url = (base_url or os.getenv("ECOURTS_API_BASE_URL") or ECOURTS_API_BASE_URL).rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.retry_delay_seconds = retry_delay_seconds
        self.min_interval_ms = int(os.getenv("ECOURTS_API_MIN_INTERVAL_MS", "300"))
        self.metrics = EcourtsApiMetrics()
        self._client = httpx.AsyncClient(timeout=self.timeout_seconds)
        self._last_request_ts = 0.0

    async def close(self) -> None:
        await self._client.aclose()

    def _headers(self) -> dict[str, str]:
        if not is_valid_ecourts_api_key(self.api_key):
            raise RuntimeError(
                "ECOURTS_API_KEY is missing or invalid. Expected provider key prefixed with 'eci_'."
            )
        return {"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"}

    async def _respect_min_interval(self) -> None:
        if self.min_interval_ms <= 0:
            return
        now = time.monotonic()
        elapsed_ms = (now - self._last_request_ts) * 1000.0
        remaining_ms = self.min_interval_ms - elapsed_ms
        if remaining_ms > 0:
            logger.debug("eCourts API throttle sleep: %.2fms", remaining_ms)
            await asyncio.sleep(remaining_ms / 1000.0)

    @staticmethod
    def _provider_error_code(payload: Any) -> str | None:
        if not isinstance(payload, dict):
            return None
        for key in ("code", "error_code", "errorCode", "status_code", "statusCode"):
            value = payload.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()
        return None

    @staticmethod
    def _is_retryable_status(status_code: int | None) -> bool:
        if status_code is None:
            return True
        return status_code == 429 or status_code >= 500

    async def _request(
        self,
        method: str,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        cost_kind: str,
    ) -> Any:
        last_exc: Exception | None = None
        url = f"{self.base_url}{endpoint}"
        for attempt in range(1, self.max_retries + 1):
            try:
                await self._respect_min_interval()
                res = await self._client.request(
                    method,
                    url,
                    headers=self._headers(),
                    params=params,
                    json=json_body,
                )
                self._last_request_ts = time.monotonic()
                payload = None
                try:
                    payload = res.json()
                except Exception:
                    payload = None
                provider_code = self._provider_error_code(payload)
                retryable = self._is_retryable_status(res.status_code)
                self.metrics.add(
                    cost_kind,
                    endpoint,
                    method=method,
                    status_code=res.status_code,
                    attempt=attempt,
                    retryable=retryable,
                    provider_code=provider_code,
                    request_params=params if params is not None else json_body,
                    response_json=payload,
                )
                res.raise_for_status()
                data = payload if payload is not None else res.json()
                logger.info(
                    "eCourts API request success",
                    extra={
                        "method": method,
                        "endpoint": endpoint,
                        "status_code": res.status_code,
                        "attempt": attempt,
                        "retryable": retryable,
                        "provider_code": provider_code,
                    },
                )
                return data
            except httpx.HTTPStatusError as exc:
                res = exc.response
                retryable = self._is_retryable_status(res.status_code if res is not None else None)
                provider_code = None
                if res is not None:
                    try:
                        provider_code = self._provider_error_code(res.json())
                    except Exception:
                        provider_code = None
                logger.warning(
                    "eCourts API request failed with status",
                    extra={
                        "method": method,
                        "endpoint": endpoint,
                        "status_code": res.status_code if res is not None else None,
                        "attempt": attempt,
                        "max_retries": self.max_retries,
                        "retryable": retryable,
                        "error_type": "HTTPStatusError",
                        "provider_code": provider_code,
                    },
                )
                fail_payload = None
                if res is not None:
                    try:
                        fail_payload = res.json()
                    except Exception:
                        fail_payload = None
                self.metrics.add(
                    cost_kind,
                    endpoint,
                    method=method,
                    status_code=res.status_code if res is not None else None,
                    attempt=attempt,
                    retryable=retryable,
                    error_type="HTTPStatusError",
                    provider_code=provider_code,
                    request_params=params if params is not None else json_body,
                    response_json=fail_payload,
                )
                last_exc = exc
                if attempt < self.max_retries and retryable:
                    logger.info(
                        "eCourts API retry scheduled (status error): endpoint=%s attempt=%s/%s",
                        endpoint,
                        attempt + 1,
                        self.max_retries,
                    )
                    await asyncio.sleep(self.retry_delay_seconds * attempt)
                    continue
                break
            except Exception as exc:
                retryable = True
                logger.warning(
                    "eCourts API request failed with transport/runtime error",
                    extra={
                        "method": method,
                        "endpoint": endpoint,
                        "attempt": attempt,
                        "max_retries": self.max_retries,
                        "retryable": retryable,
                        "error_type": type(exc).__name__,
                    },
                )
                self.metrics.add(
                    cost_kind,
                    endpoint,
                    method=method,
                    status_code=None,
                    attempt=attempt,
                    retryable=retryable,
                    error_type=type(exc).__name__,
                    request_params=params if params is not None else json_body,
                )
                last_exc = exc
                if attempt < self.max_retries:
                    logger.info(
                        "eCourts API retry scheduled (transport error): endpoint=%s attempt=%s/%s",
                        endpoint,
                        attempt + 1,
                        self.max_retries,
                    )
                    await asyncio.sleep(self.retry_delay_seconds * attempt)
                    continue
                break
        logger.error("eCourts API request exhausted retries: %s %s", method, endpoint)
        raise RuntimeError(f"eCourts API {method} {endpoint} failed: {last_exc}") from last_exc

    @staticmethod
    def _extract_rows(payload: Any) -> list[dict]:
        if isinstance(payload, list):
            return [x for x in payload if isinstance(x, dict)]
        if not isinstance(payload, dict):
            return []
        # Provider commonly returns rows under nested `data.results`.
        data_obj = payload.get("data")
        if isinstance(data_obj, dict):
            for key in ("results", "cases", "items", "data"):
                value = data_obj.get(key)
                if isinstance(value, list):
                    return [x for x in value if isinstance(x, dict)]
        for key in ("data", "results", "cases", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
        return []

    async def search_cases(
        self,
        *,
        owner_name: str,
        district: str,
        taluka: str,
        village: str,
        survey_number: str,
    ) -> list[dict]:
        # Use general-party search on partner API so we are not limited
        # to petitioner-only matches.
        # caseStatuses / judicialSections are opt-in: only sent when explicitly
        # set in env so the minimal request matches the documented example.
        case_statuses = _split_csv_env(os.getenv("ECOURTS_API_CASE_STATUSES", ""))
        judicial_sections = _split_csv_env(os.getenv("ECOURTS_API_JUDICIAL_SECTIONS", ""))
        case_types = _split_csv_env(os.getenv("ECOURTS_API_CASE_TYPES", ""))
        params: list[tuple[str, Any]] = [
            ("page", 1),
            ("pageSize", int(os.getenv("ECOURTS_API_SEARCH_PAGE_SIZE", "20"))),
        ]
        for status in case_statuses:
            params.append(("caseStatuses", status))
        for section in judicial_sections:
            params.append(("judicialSections", section))
        for case_type in case_types:
            params.append(("caseTypes", case_type))
        if owner_name:
            params.append(("litigants", owner_name))
        logger.info(
            "eCourts case search request prepared: owner=%r statuses=%s sections=%s case_types=%s",
            owner_name,
            case_statuses,
            judicial_sections,
            case_types,
        )
        payload = await self._request("GET", "/search", params=params, cost_kind="case_search_get")
        return self._extract_rows(payload)

    async def get_case_detail(self, cnr: str) -> dict:
        return await self._request("GET", f"/case/{cnr}", cost_kind="case_detail_get")

    async def refresh_case(self, cnr: str) -> dict:
        return await self._request("POST", f"/case/{cnr}/refresh", cost_kind="case_refresh_post")
