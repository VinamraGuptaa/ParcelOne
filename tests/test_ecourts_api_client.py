from api.ecourts_api_client import EcourtsApiClient


def test_extract_rows_from_common_payload_shapes():
    assert EcourtsApiClient._extract_rows([{"a": 1}]) == [{"a": 1}]
    assert EcourtsApiClient._extract_rows({"data": [{"a": 1}]}) == [{"a": 1}]
    assert EcourtsApiClient._extract_rows({"data": {"results": [{"a": 1}]}}) == [{"a": 1}]
    assert EcourtsApiClient._extract_rows({"results": [{"a": 1}]}) == [{"a": 1}]
    assert EcourtsApiClient._extract_rows({"cases": [{"a": 1}]}) == [{"a": 1}]
    assert EcourtsApiClient._extract_rows({"items": [{"a": 1}]}) == [{"a": 1}]
    assert EcourtsApiClient._extract_rows({"x": 1}) == []


async def test_request_metrics_cost_accumulates(monkeypatch):
    seen_params = []

    class _Resp:
        def __init__(self, payload, status_code=200):
            self.status_code = status_code
            self._payload = payload

        def raise_for_status(self):
            if self.status_code >= 400:
                import httpx

                req = httpx.Request("GET", "https://example.test")
                raise httpx.HTTPStatusError("boom", request=req, response=self)
            return None

        def json(self):
            return self._payload

    class _Client:
        async def request(self, method, url, headers=None, params=None, json=None):
            if params:
                seen_params.extend(params)
            if method == "GET" and url.endswith("/search"):
                return _Resp({"data": [{"cnr": "X"}]})
            if method == "GET" and "/case/" in url:
                return _Resp({"cnr": "X", "case_status": "Pending"})
            return _Resp({})

        async def aclose(self):
            return None

    monkeypatch.setenv("ECOURTS_API_KEY", "eci_test_key_123456")
    c = EcourtsApiClient()
    c._client = _Client()
    rows = await c.search_cases(
        owner_name="abc",
        district="Pune",
        taluka="Haveli",
        village="Baner",
        survey_number="70",
    )
    assert len(rows) == 1
    await c.get_case_detail("X")
    assert c.metrics.search_requests == 1
    assert c.metrics.detail_requests == 1
    assert c.metrics.estimated_cost_inr == 0.7
    assert ("litigants", "abc") in seen_params
    assert ("caseStatuses", "PENDING") in seen_params
    assert ("judicialSections", "CIV") in seen_params
    assert c.metrics.request_log[0]["method"] == "GET"
    assert ("litigants", "abc") in c.metrics.request_log[0]["request_params"]
    assert isinstance(c.metrics.request_log[0]["response_json"], dict)


async def test_search_cases_adds_configured_case_type_filters(monkeypatch):
    seen_params = []

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [{"cnr": "X"}]}

    class _Client:
        async def request(self, method, url, headers=None, params=None, json=None):
            if params:
                seen_params.extend(params)
            return _Resp()

        async def aclose(self):
            return None

    monkeypatch.setenv("ECOURTS_API_KEY", "eci_test_key_123456")
    monkeypatch.setenv("ECOURTS_API_CASE_TYPES", "CS,WP_C")
    c = EcourtsApiClient()
    c._client = _Client()
    await c.search_cases(
        owner_name="abc",
        district="Pune",
        taluka="Haveli",
        village="Baner",
        survey_number="70",
    )
    assert ("caseTypes", "CS") in seen_params
    assert ("caseTypes", "WP_C") in seen_params


async def test_retries_on_429(monkeypatch):
    class _Resp:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self._payload = payload

        def raise_for_status(self):
            if self.status_code >= 400:
                import httpx

                req = httpx.Request("GET", "https://example.test")
                raise httpx.HTTPStatusError("boom", request=req, response=self)
            return None

        def json(self):
            return self._payload

    class _Client:
        def __init__(self):
            self.calls = 0

        async def request(self, method, url, headers=None, params=None, json=None):
            self.calls += 1
            if self.calls == 1:
                return _Resp(429, {"code": "RATE_LIMITED"})
            return _Resp(200, {"data": [{"cnr": "X"}]})

        async def aclose(self):
            return None

    monkeypatch.setenv("ECOURTS_API_KEY", "eci_test_key_123456")
    c = EcourtsApiClient(max_retries=2, retry_delay_seconds=0.01)
    fake = _Client()
    c._client = fake
    rows = await c.search_cases(
        owner_name="abc",
        district="Pune",
        taluka="Haveli",
        village="Baner",
        survey_number="70",
    )
    assert len(rows) == 1
    assert fake.calls == 2
    assert c.metrics.request_log[0]["provider_code"] == "RATE_LIMITED"
    assert c.metrics.request_log[0]["retryable"] is True
