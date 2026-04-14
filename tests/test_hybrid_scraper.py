"""
Unit tests for HybridECourtsScraper (scraper.py).

Tests cover:
  - ScrapingSession dataclass and SessionExpiredError
  - bootstrap_session(): PHPSESSID extraction, error on missing cookie
  - _session_is_fresh(): TTL boundary behaviour
  - _fetch_captcha_http(): success, empty OCR result, HTTP error
  - _http_search_year(): captcha accepted, captcha rejected/retry, session expired, exhaustion
  - _http_fetch_detail(): correct viewHistory arg mapping, invalid JS, HTTP error
  - search_petitioner(): bootstrap on first call, stale re-bootstrap, SessionExpiredError recovery
  - setup_driver() / navigate_and_select(): confirmed no-ops
  - close(): browser closed if open, safe if already closed
  - Parser refactoring: _parse_summary_table(html=...) and _parse_detail_page(html=...) bypass browser
"""

import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call

import httpx

from scraper import (
    ECourtsScraper,
    HybridECourtsScraper,
    ScrapingSession,
    SessionExpiredError,
    _SEARCH_URL,
    _VIEW_HISTORY_URL,
    _CAPTCHA_URL,
    _GET_CAPTCHA_URL,
    _STATE_CODE,
    _DIST_CODE,
    _COURT_COMPLEX_CODE,
    _COURT_COMPLEX_BARE,
)


# ── HTML fixtures (reused from scraper parsing tests) ─────────────────────────

SUMMARY_HTML = """
<html><body>
<table id="dispTable" class="table table-bordered">
  <thead>
    <tr>
      <th>Sr No</th>
      <th>Case Type/Case Number/Case Year</th>
      <th>Petitioner Name versus Respondent Name</th>
      <th>View</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td>1</td><td>R.C.A./181/2017</td><td>Asha Rajesh Gupta</td>
      <td><a href="#"
         onclick="viewHistory(200100001812017,'MHPU010023222017',1,'','CScaseNumber',1,25,1010303,'CSpartyName')">
        View</a></td>
    </tr>
    <tr>
      <td>2</td><td>Civil M.A./465/2017</td><td>Vipin Gupta Vs Rajesh Gupta</td>
      <td><a href="#"
         onclick="viewHistory(200300004652017,'MHPU020028962017',2,'','CScaseNumber',1,25,1010303,'CSpartyName')">
        View</a></td>
    </tr>
  </tbody>
</table>
</body></html>
"""

_PAD = "<!-- " + "x" * 100 + " -->\n"

SUMMARY_NO_RECORDS_HTML = (
    "<html><body>\n"
    "<div class='alert'>No record found for this search criteria</div>\n"
    + _PAD * 6
    + "</body></html>\n"
)

DETAIL_HTML = """
<html><body>
<table>
  <tr><td>Case Type</td><td>R.C.A. - Regular Civil Appeal</td></tr>
  <tr><td>Filing Number</td><td>1252/2017</td></tr>
  <tr><td>Filing Date</td><td>16-02-2017</td></tr>
  <tr><td>Registration Number</td><td>181/2017</td></tr>
  <tr><td>Registration Date</td><td>21-03-2017</td></tr>
  <tr><td>CNR Number</td><td><span class="text-danger text-uppercase">MHPU010023222017</span></td></tr>
  <tr><td>Decision Date</td><td>21st September 2021</td></tr>
  <tr><td>Case Status</td><td>Case disposed</td></tr>
  <tr><td>Court Number and Judge</td><td>53-DISTRICT JUDGE -15</td></tr>
</table>
</body></html>
"""

CAPTCHA_ERROR_HTML = (
    "<html><body>\n"
    "<div id='validateError' style='display:block'>Captcha code incorrect</div>\n"
    + _PAD * 6
    + "</body></html>\n"
)

SESSION_EXPIRED_HTML = "<html><body>Session Expired. Please login again.</body></html>"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_http_response(text: str = "", content: bytes = b"", status: int = 200):
    resp = MagicMock(spec=httpx.Response)
    resp.text = text
    resp.content = content
    resp.status_code = status
    resp.raise_for_status = MagicMock()
    headers = MagicMock()
    headers.get = MagicMock(return_value="text/html; charset=utf-8")
    resp.headers = headers
    return resp


def _mock_client(get_resp=None, post_resps=None):
    """
    Return a mock httpx.AsyncClient.
    post_resps may be a single response or a list for sequential calls.
    """
    client = AsyncMock(spec=httpx.AsyncClient)
    client.is_closed = False  # must be False or search_petitioner triggers refresh
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    if get_resp is not None:
        client.get = AsyncMock(return_value=get_resp)
    if post_resps is not None:
        if isinstance(post_resps, list):
            client.post = AsyncMock(side_effect=post_resps)
        else:
            client.post = AsyncMock(return_value=post_resps)
    return client


async def _setup_bootstrap(
    scraper: HybridECourtsScraper,
    services_sessid: str = "abc123",
    jsession: str = "sess001",
):
    """
    Patch ECourtsScraper.setup_driver / navigate_and_select to inject mock
    browser objects, then call bootstrap_session(). Returns (session, mock_browser).
    """
    mock_context = AsyncMock()
    mock_context.cookies = AsyncMock(return_value=[
        {"name": "SERVICES_SESSID", "value": services_sessid},
        {"name": "JSESSION", "value": jsession},
    ])
    mock_browser = AsyncMock()
    mock_playwright = AsyncMock()

    async def _fake_setup():
        mock_page = AsyncMock()
        mock_page.evaluate = AsyncMock(return_value="")  # app_token
        scraper.page = mock_page
        scraper.context = mock_context
        scraper.browser = mock_browser
        scraper._playwright = mock_playwright

    with patch.object(ECourtsScraper, "setup_driver", side_effect=_fake_setup):
        with patch.object(ECourtsScraper, "navigate_and_select", AsyncMock()):
            session = await scraper.bootstrap_session()
    return session, mock_browser


# ── ScrapingSession & SessionExpiredError ─────────────────────────────────────

class TestScrapingSession:

    def test_services_sessid_stored(self):
        s = ScrapingSession(services_sessid="xyz999")
        assert s.services_sessid == "xyz999"

    def test_jsession_defaults_to_empty(self):
        s = ScrapingSession(services_sessid="x")
        assert s.jsession == ""

    def test_app_token_defaults_to_empty(self):
        s = ScrapingSession(services_sessid="x")
        assert s.app_token == ""

    def test_created_at_is_monotonic_float(self):
        before = time.monotonic()
        s = ScrapingSession(services_sessid="x")
        after = time.monotonic()
        assert before <= s.created_at <= after

    def test_two_sessions_have_different_created_at(self):
        s1 = ScrapingSession(services_sessid="a")
        s2 = ScrapingSession(services_sessid="b")
        assert s2.created_at >= s1.created_at

    def test_session_expired_error_is_exception(self):
        with pytest.raises(SessionExpiredError):
            raise SessionExpiredError("test")


# ── bootstrap_session ─────────────────────────────────────────────────────────

class TestBootstrapSession:

    async def test_returns_session_with_services_sessid(self):
        scraper = HybridECourtsScraper(headless=True)
        session, _ = await _setup_bootstrap(scraper, "testcookie99")
        assert isinstance(session, ScrapingSession)
        assert session.services_sessid == "testcookie99"

    async def test_session_stored_on_scraper(self):
        scraper = HybridECourtsScraper(headless=True)
        await _setup_bootstrap(scraper, "stored123")
        assert scraper._session is not None
        assert scraper._session.services_sessid == "stored123"

    async def test_jsession_captured(self):
        scraper = HybridECourtsScraper(headless=True)
        session, _ = await _setup_bootstrap(scraper, "s1", jsession="j999")
        assert session.jsession == "j999"

    async def test_browser_closed_after_bootstrap(self):
        scraper = HybridECourtsScraper(headless=True)
        _, mock_browser = await _setup_bootstrap(scraper, "abc")
        mock_browser.close.assert_called_once()

    async def test_playwright_stopped_after_bootstrap(self):
        scraper = HybridECourtsScraper(headless=True)
        mock_context = AsyncMock()
        mock_context.cookies = AsyncMock(return_value=[
            {"name": "SERVICES_SESSID", "value": "x"},
        ])
        mock_browser = AsyncMock()
        mock_playwright = AsyncMock()

        async def _fake_setup():
            mock_page = AsyncMock()
            mock_page.evaluate = AsyncMock(return_value="")
            scraper.page = mock_page
            scraper.context = mock_context
            scraper.browser = mock_browser
            scraper._playwright = mock_playwright

        with patch.object(ECourtsScraper, "setup_driver", side_effect=_fake_setup):
            with patch.object(ECourtsScraper, "navigate_and_select", AsyncMock()):
                await scraper.bootstrap_session()
        mock_playwright.stop.assert_called_once()

    async def test_browser_and_page_cleared_after_bootstrap(self):
        scraper = HybridECourtsScraper(headless=True)
        await _setup_bootstrap(scraper, "abc")
        assert scraper.browser is None
        assert scraper.page is None

    async def test_raises_when_no_services_sessid_in_cookies(self):
        scraper = HybridECourtsScraper(headless=True)
        mock_context = AsyncMock()
        mock_context.cookies = AsyncMock(return_value=[])
        mock_browser = AsyncMock()

        async def _fake_setup():
            mock_page = AsyncMock()
            mock_page.evaluate = AsyncMock(return_value="")
            scraper.page = mock_page
            scraper.context = mock_context
            scraper.browser = mock_browser
            scraper._playwright = AsyncMock()

        with patch.object(ECourtsScraper, "setup_driver", side_effect=_fake_setup):
            with patch.object(ECourtsScraper, "navigate_and_select", AsyncMock()):
                with pytest.raises(RuntimeError, match="SERVICES_SESSID"):
                    await scraper.bootstrap_session()

    async def test_browser_closed_even_when_navigate_fails(self):
        """Browser must always be closed, even if navigate_and_select raises."""
        scraper = HybridECourtsScraper(headless=True)
        mock_browser = AsyncMock()

        async def _fake_setup():
            scraper.page = AsyncMock()
            scraper.context = AsyncMock()
            scraper.browser = mock_browser
            scraper._playwright = AsyncMock()

        with patch.object(ECourtsScraper, "setup_driver", side_effect=_fake_setup):
            with patch.object(
                ECourtsScraper, "navigate_and_select",
                AsyncMock(side_effect=Exception("navigation error"))
            ):
                with pytest.raises(Exception):
                    await scraper.bootstrap_session()
        mock_browser.close.assert_called_once()

    async def test_reboostrap_resets_browser_state(self):
        """Second bootstrap call starts fresh even if browser is None."""
        scraper = HybridECourtsScraper(headless=True)
        await _setup_bootstrap(scraper, "first")
        session2, _ = await _setup_bootstrap(scraper, "second")
        assert session2.services_sessid == "second"


# ── _session_is_fresh ─────────────────────────────────────────────────────────

class TestSessionIsFresh:

    def test_fresh_session_returns_true(self):
        scraper = HybridECourtsScraper(headless=True)
        scraper._session = ScrapingSession(services_sessid="x")
        assert scraper._session_is_fresh() is True

    def test_no_session_returns_false(self):
        scraper = HybridECourtsScraper(headless=True)
        assert scraper._session_is_fresh() is False

    def test_expired_session_returns_false(self):
        scraper = HybridECourtsScraper(headless=True)
        old_time = time.monotonic() - HybridECourtsScraper.SESSION_TTL - 1
        scraper._session = ScrapingSession(services_sessid="x", created_at=old_time)
        assert scraper._session_is_fresh() is False

    def test_just_within_ttl_returns_true(self):
        scraper = HybridECourtsScraper(headless=True)
        just_fresh = time.monotonic() - HybridECourtsScraper.SESSION_TTL + 10
        scraper._session = ScrapingSession(services_sessid="x", created_at=just_fresh)
        assert scraper._session_is_fresh() is True


# ── _fetch_captcha_http ───────────────────────────────────────────────────────

class TestFetchCaptchaHTTP:

    async def test_returns_solved_text_on_success(self):
        scraper = HybridECourtsScraper(headless=True)
        scraper._session = ScrapingSession(services_sessid="x")
        client = _mock_client(get_resp=_make_http_response(content=b"PNG"))
        with patch("captcha_solver.solve", return_value="A3X9K2"):
            result = await scraper._fetch_captcha_http(client)
        assert result == "A3X9K2"

    async def test_returns_none_when_ocr_returns_empty(self):
        scraper = HybridECourtsScraper(headless=True)
        scraper._session = ScrapingSession(services_sessid="x")
        client = _mock_client(get_resp=_make_http_response(content=b"PNG"))
        with patch("captcha_solver.solve", return_value=""):
            result = await scraper._fetch_captcha_http(client)
        assert result is None

    async def test_returns_none_on_http_error(self):
        scraper = HybridECourtsScraper(headless=True)
        scraper._session = ScrapingSession(services_sessid="x")
        client = _mock_client()
        client.get = AsyncMock(side_effect=httpx.ConnectError("timeout"))
        result = await scraper._fetch_captcha_http(client)
        assert result is None

    async def test_requests_correct_captcha_url(self):
        """getCaptcha JSON is parsed to extract the exact hash URL for client.get."""
        scraper = HybridECourtsScraper(headless=True)
        scraper._session = ScrapingSession(services_sessid="x")

        # Simulate getCaptcha returning JSON with a Securimage hash URL
        hash_path = "/ecourtindia_v6/vendor/securimage/securimage_show.php?abc123hash"
        get_captcha_json = f'{{"div_captcha": "<img src=\\"{hash_path}\\">"}}'
        get_captcha_resp = _make_http_response(text=get_captcha_json)
        get_image_resp = _make_http_response(content=b"PNG")

        client = AsyncMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(return_value=get_captcha_resp)
        client.get = AsyncMock(return_value=get_image_resp)

        with patch("captcha_solver.solve", return_value="ABC"):
            await scraper._fetch_captcha_http(client)

        client.get.assert_called_once()
        call_url = client.get.call_args[0][0]
        assert call_url == f"https://services.ecourts.gov.in{hash_path}"

    async def test_raise_for_status_called(self):
        scraper = HybridECourtsScraper(headless=True)
        scraper._session = ScrapingSession(services_sessid="x")
        get_resp = _make_http_response(content=b"PNG")
        client = _mock_client(get_resp=get_resp)
        with patch("captcha_solver.solve", return_value="XY"):
            await scraper._fetch_captcha_http(client)
        get_resp.raise_for_status.assert_called_once()


# ── _http_search_year ─────────────────────────────────────────────────────────

class TestHTTPSearchYear:

    def _make_scraper_with_session(self, phpsessid="sess001"):
        scraper = HybridECourtsScraper(headless=True)
        scraper._session = ScrapingSession(services_sessid=phpsessid)
        return scraper

    async def test_returns_parsed_rows_on_success(self):
        scraper = self._make_scraper_with_session()
        client = _mock_client(
            post_resps=_make_http_response(text=SUMMARY_HTML)
        )
        with patch.object(scraper, "_fetch_captcha_http", AsyncMock(return_value="ABCD")):
            with patch.object(scraper, "_http_fetch_detail", AsyncMock(return_value={})):
                rows = await scraper._http_search_year(client, "Rajesh Gupta", "2017")
        assert len(rows) == 2

    async def test_no_records_returns_empty_list(self):
        scraper = self._make_scraper_with_session()
        client = _mock_client(
            post_resps=_make_http_response(text=SUMMARY_NO_RECORDS_HTML)
        )
        with patch.object(scraper, "_fetch_captcha_http", AsyncMock(return_value="ABCD")):
            rows = await scraper._http_search_year(client, "Rajesh Gupta", "2020")
        assert rows == []

    async def test_posts_to_correct_search_url(self):
        scraper = self._make_scraper_with_session()
        post_resp = _make_http_response(text=SUMMARY_NO_RECORDS_HTML)
        client = _mock_client(post_resps=post_resp)
        with patch.object(scraper, "_fetch_captcha_http", AsyncMock(return_value="XY")):
            await scraper._http_search_year(client, "Test Name", "2019")
        call_url = client.post.call_args[0][0]
        assert call_url == _SEARCH_URL

    async def test_petitioner_name_and_year_in_post_body(self):
        scraper = self._make_scraper_with_session()
        client = _mock_client(post_resps=_make_http_response(text=SUMMARY_NO_RECORDS_HTML))
        with patch.object(scraper, "_fetch_captcha_http", AsyncMock(return_value="ZZ")):
            await scraper._http_search_year(client, "Rajesh Gupta", "2019")
        post_data = client.post.call_args[1]["data"]
        assert post_data["petres_name"] == "Rajesh Gupta"
        assert post_data["rgyearP"] == "2019"

    async def test_court_codes_in_post_body(self):
        scraper = self._make_scraper_with_session()
        client = _mock_client(post_resps=_make_http_response(text=SUMMARY_NO_RECORDS_HTML))
        with patch.object(scraper, "_fetch_captcha_http", AsyncMock(return_value="ZZ")):
            await scraper._http_search_year(client, "Name", "2018")
        post_data = client.post.call_args[1]["data"]
        assert post_data["state_code"] == _STATE_CODE
        assert post_data["dist_code"] == _DIST_CODE
        assert post_data["court_complex_code"] == _COURT_COMPLEX_BARE

    async def test_captcha_text_sent_in_post_body(self):
        scraper = self._make_scraper_with_session()
        client = _mock_client(post_resps=_make_http_response(text=SUMMARY_NO_RECORDS_HTML))
        with patch.object(scraper, "_fetch_captcha_http", AsyncMock(return_value="MYCODE")):
            await scraper._http_search_year(client, "Name", "2018")
        post_data = client.post.call_args[1]["data"]
        assert post_data["fcaptcha_code"] == "MYCODE"

    async def test_captcha_rejected_retries(self):
        """If first POST returns captcha error HTML, retries with fresh captcha."""
        scraper = self._make_scraper_with_session()
        responses = [
            _make_http_response(text=CAPTCHA_ERROR_HTML),   # attempt 1: rejected
            _make_http_response(text=SUMMARY_NO_RECORDS_HTML),  # attempt 2: accepted
        ]
        client = _mock_client(post_resps=responses)
        call_count = 0

        async def _captcha(_client):
            nonlocal call_count
            call_count += 1
            return "CODE"

        with patch.object(scraper, "_fetch_captcha_http", side_effect=_captcha):
            rows = await scraper._http_search_year(client, "Name", "2017")

        assert call_count == 2
        assert rows == []

    async def test_session_expired_marker_raises_session_expired(self):
        scraper = self._make_scraper_with_session()
        client = _mock_client(post_resps=_make_http_response(text=SESSION_EXPIRED_HTML))
        with patch.object(scraper, "_fetch_captcha_http", AsyncMock(return_value="X")):
            with pytest.raises(SessionExpiredError):
                await scraper._http_search_year(client, "Name", "2017")

    async def test_short_no_records_does_not_raise_session_expired(self):
        """A short 'no records' AJAX response must NOT trigger SessionExpiredError."""
        scraper = self._make_scraper_with_session()
        short_no_records = "<div>No records found</div>"
        client = _mock_client(post_resps=_make_http_response(text=short_no_records))
        with patch.object(scraper, "_fetch_captcha_http", AsyncMock(return_value="X")):
            # Should not raise — returns empty list
            rows = await scraper._http_search_year(client, "Name", "2017")
        assert rows == []

    async def test_captcha_exhaustion_raises_runtime_error(self):
        """Fails all MAX_CAPTCHA_RETRIES attempts → RuntimeError."""
        scraper = self._make_scraper_with_session()
        # All attempts return captcha-rejected HTML
        bad_resps = [_make_http_response(text=CAPTCHA_ERROR_HTML)] * 10
        client = _mock_client(post_resps=bad_resps)
        with patch.object(scraper, "_fetch_captcha_http", AsyncMock(return_value="BAD")):
            with pytest.raises(RuntimeError, match="Failed to solve captcha"):
                await scraper._http_search_year(client, "Name", "2017")

    async def test_detail_merged_into_summary_row(self):
        scraper = self._make_scraper_with_session()
        client = _mock_client(post_resps=_make_http_response(text=SUMMARY_HTML))
        fake_detail = {"CNR_Number": "MHPU010023222017", "Case_Status": "Case disposed"}
        with patch.object(scraper, "_fetch_captcha_http", AsyncMock(return_value="OK")):
            with patch.object(scraper, "_http_fetch_detail", AsyncMock(return_value=fake_detail)):
                rows = await scraper._http_search_year(client, "Rajesh Gupta", "2017")
        assert rows[0]["CNR_Number"] == "MHPU010023222017"
        assert rows[0]["Case_Status"] == "Case disposed"

    async def test_view_js_not_in_final_row(self):
        """_view_js should be popped and not appear in final output."""
        scraper = self._make_scraper_with_session()
        client = _mock_client(post_resps=_make_http_response(text=SUMMARY_HTML))
        with patch.object(scraper, "_fetch_captcha_http", AsyncMock(return_value="X")):
            with patch.object(scraper, "_http_fetch_detail", AsyncMock(return_value={})):
                rows = await scraper._http_search_year(client, "Name", "2017")
        for row in rows:
            assert "_view_js" not in row

    async def test_empty_captcha_text_retries_without_posting(self):
        """If _fetch_captcha_http returns None, must retry without a POST."""
        scraper = self._make_scraper_with_session()
        client = _mock_client(post_resps=_make_http_response(text=SUMMARY_NO_RECORDS_HTML))
        # Return None twice, then a valid code
        captcha_values = [None, None, "VALID"]

        async def _captcha(_):
            return captcha_values.pop(0)

        with patch.object(scraper, "_fetch_captcha_http", side_effect=_captcha):
            rows = await scraper._http_search_year(client, "Name", "2018")
        # POST should have been called only once (on the VALID captcha attempt)
        assert client.post.call_count == 1
        assert rows == []


# ── _http_fetch_detail ────────────────────────────────────────────────────────

class TestHTTPFetchDetail:

    VALID_VIEW_JS = (
        "viewHistory(200100001812017,'MHPU010023222017',1,'','CScaseNumber',"
        "1,25,1010303,'CSpartyName')"
    )

    def _make_scraper(self):
        scraper = HybridECourtsScraper(headless=True)
        scraper._session = ScrapingSession(services_sessid="s")
        return scraper

    async def test_returns_parsed_detail_on_success(self):
        scraper = self._make_scraper()
        client = _mock_client(post_resps=_make_http_response(text=DETAIL_HTML))
        detail = await scraper._http_fetch_detail(client, self.VALID_VIEW_JS)
        assert detail.get("Case_Type") == "R.C.A. - Regular Civil Appeal"

    async def test_posts_to_view_history_url(self):
        scraper = self._make_scraper()
        client = _mock_client(post_resps=_make_http_response(text=DETAIL_HTML))
        await scraper._http_fetch_detail(client, self.VALID_VIEW_JS)
        call_url = client.post.call_args[0][0]
        assert call_url == _VIEW_HISTORY_URL

    async def test_case_no_extracted_as_arg0(self):
        scraper = self._make_scraper()
        client = _mock_client(post_resps=_make_http_response(text=DETAIL_HTML))
        await scraper._http_fetch_detail(client, self.VALID_VIEW_JS)
        post_data = client.post.call_args[1]["data"]
        assert post_data["case_no"] == "200100001812017"

    async def test_cino_extracted_as_arg1(self):
        scraper = self._make_scraper()
        client = _mock_client(post_resps=_make_http_response(text=DETAIL_HTML))
        await scraper._http_fetch_detail(client, self.VALID_VIEW_JS)
        post_data = client.post.call_args[1]["data"]
        assert post_data["cino"] == "MHPU010023222017"

    async def test_court_code_extracted_as_arg2(self):
        scraper = self._make_scraper()
        client = _mock_client(post_resps=_make_http_response(text=DETAIL_HTML))
        await scraper._http_fetch_detail(client, self.VALID_VIEW_JS)
        post_data = client.post.call_args[1]["data"]
        assert post_data["court_code"] == "1"

    async def test_different_court_code_for_court2(self):
        """court_code=2 for second case."""
        scraper = self._make_scraper()
        view_js = (
            "viewHistory(200300004652017,'MHPU020028962017',2,'','CScaseNumber',"
            "1,25,1010303,'CSpartyName')"
        )
        client = _mock_client(post_resps=_make_http_response(text=DETAIL_HTML))
        await scraper._http_fetch_detail(client, view_js)
        post_data = client.post.call_args[1]["data"]
        assert post_data["court_code"] == "2"

    async def test_state_dist_complex_from_onclick_args(self):
        scraper = self._make_scraper()
        client = _mock_client(post_resps=_make_http_response(text=DETAIL_HTML))
        await scraper._http_fetch_detail(client, self.VALID_VIEW_JS)
        post_data = client.post.call_args[1]["data"]
        assert post_data["state_code"] == "1"
        assert post_data["dist_code"] == "25"
        assert post_data["court_complex_code"] == "1010303"

    async def test_search_flag_and_by_from_onclick(self):
        scraper = self._make_scraper()
        client = _mock_client(post_resps=_make_http_response(text=DETAIL_HTML))
        await scraper._http_fetch_detail(client, self.VALID_VIEW_JS)
        post_data = client.post.call_args[1]["data"]
        assert post_data["search_flag"] == "CScaseNumber"
        assert post_data["search_by"] == "CSpartyName"

    async def test_invalid_onclick_returns_empty_dict(self):
        """No viewHistory() match → return empty dict without raising."""
        scraper = self._make_scraper()
        client = _mock_client()
        result = await scraper._http_fetch_detail(client, "onclick=\"alert('hello')\"")
        assert result == {}
        client.post.assert_not_called()

    async def test_too_few_args_returns_empty_dict(self):
        """Less than 3 args → return empty dict."""
        scraper = self._make_scraper()
        client = _mock_client()
        result = await scraper._http_fetch_detail(client, "viewHistory(123,'CNR')")
        assert result == {}

    async def test_http_error_returns_empty_dict(self):
        scraper = self._make_scraper()
        client = _mock_client()
        client.post = AsyncMock(side_effect=httpx.ConnectError("refused"))
        result = await scraper._http_fetch_detail(client, self.VALID_VIEW_JS)
        assert result == {}


# ── search_petitioner ─────────────────────────────────────────────────────────

def _scraper_with_http(phpsessid="sess001") -> HybridECourtsScraper:
    """Return a HybridECourtsScraper with a pre-set session and mock HTTP client."""
    scraper = HybridECourtsScraper(headless=True)
    scraper._session = ScrapingSession(services_sessid=phpsessid)
    scraper._http = _mock_client()
    return scraper


class TestSearchPetitioner:

    async def test_bootstraps_when_http_is_none(self):
        """If _http is None, bootstrap_session is called before HTTP search."""
        scraper = HybridECourtsScraper(headless=True)

        bootstrap_called = []
        async def _fake_bootstrap():
            bootstrap_called.append(True)
            scraper._session = ScrapingSession(services_sessid="new")
            return scraper._session

        with patch.object(scraper, "bootstrap_session", side_effect=_fake_bootstrap):
            with patch.object(scraper, "_open_http_client", MagicMock(side_effect=lambda: setattr(scraper, "_http", _mock_client()))):
                with patch.object(scraper, "_http_search_year", AsyncMock(return_value=[])):
                    await scraper.search_petitioner("Name", "2017")

        assert len(bootstrap_called) == 1

    async def test_does_not_bootstrap_when_session_fresh_and_client_open(self):
        scraper = _scraper_with_http()
        bootstrap_called = []

        async def _fake_bootstrap():
            bootstrap_called.append(True)
            return scraper._session

        with patch.object(scraper, "bootstrap_session", side_effect=_fake_bootstrap):
            with patch.object(scraper, "_http_search_year", AsyncMock(return_value=[])):
                await scraper.search_petitioner("Name", "2017")

        assert len(bootstrap_called) == 0

    async def test_bootstraps_when_session_stale(self):
        scraper = HybridECourtsScraper(headless=True)
        old = time.monotonic() - HybridECourtsScraper.SESSION_TTL - 1
        scraper._session = ScrapingSession(services_sessid="old", created_at=old)
        scraper._http = _mock_client()

        bootstrap_called = []
        async def _fake_bootstrap():
            bootstrap_called.append(True)
            scraper._session = ScrapingSession(services_sessid="fresh")
            return scraper._session

        with patch.object(scraper, "bootstrap_session", side_effect=_fake_bootstrap):
            with patch.object(scraper, "_open_http_client", MagicMock(side_effect=lambda: setattr(scraper, "_http", _mock_client()))):
                with patch.object(scraper, "_http_search_year", AsyncMock(return_value=[])):
                    await scraper.search_petitioner("Name", "2017")

        assert len(bootstrap_called) == 1

    async def test_re_bootstraps_and_retries_on_session_expired_error(self):
        scraper = _scraper_with_http()
        search_calls = []

        async def _search(client, name, year):
            search_calls.append(1)
            if len(search_calls) == 1:
                raise SessionExpiredError("expired")
            return []

        bootstrap_count = [0]
        async def _fake_bootstrap():
            bootstrap_count[0] += 1
            scraper._session = ScrapingSession(services_sessid="renewed")
            return scraper._session

        with patch.object(scraper, "bootstrap_session", side_effect=_fake_bootstrap):
            with patch.object(scraper, "_open_http_client", MagicMock(side_effect=lambda: setattr(scraper, "_http", _mock_client()))):
                with patch.object(scraper, "_http_search_year", side_effect=_search):
                    result = await scraper.search_petitioner("Name", "2017")

        assert bootstrap_count[0] == 1
        assert len(search_calls) == 2
        assert result == []

    async def test_reuses_same_http_client_across_calls(self):
        """The same _http client must be passed to _http_search_year every call."""
        scraper = _scraper_with_http()
        original_http = scraper._http
        received_clients = []

        async def _capture_client(client, name, year):
            received_clients.append(client)
            return []

        with patch.object(scraper, "_http_search_year", side_effect=_capture_client):
            await scraper.search_petitioner("Name", "2017")
            await scraper.search_petitioner("Name", "2018")
            await scraper.search_petitioner("Name", "2019")

        # Same client instance for all three calls
        assert all(c is original_http for c in received_clients)
        assert len(received_clients) == 3

    async def test_returns_records_from_http_search(self):
        scraper = _scraper_with_http()
        expected = [{"Case_Type": "R.C.A.", "Sr No": "1"}]

        with patch.object(scraper, "_http_search_year", AsyncMock(return_value=expected)):
            result = await scraper.search_petitioner("Name", "2017")

        assert result == expected

    async def test_runtime_error_propagates(self):
        """Captcha exhaustion RuntimeError should propagate to worker."""
        scraper = _scraper_with_http()

        async def _fail(client, name, year):
            raise RuntimeError("Failed to solve captcha after 5 retries")

        with patch.object(scraper, "_http_search_year", side_effect=_fail):
            with pytest.raises(RuntimeError, match="Failed to solve captcha"):
                await scraper.search_petitioner("Name", "2017")


# ── setup_driver bootstraps and opens HTTP client ─────────────────────────────

class TestSetupDriver:

    async def test_setup_driver_is_noop(self):
        """setup_driver does nothing — bootstrap happens inside scrape_all_years."""
        scraper = HybridECourtsScraper(headless=True)
        mock_bs = AsyncMock()
        mock_open = MagicMock()
        with patch.object(scraper, "bootstrap_session", mock_bs):
            with patch.object(scraper, "_open_http_client", mock_open):
                await scraper.setup_driver()
        mock_bs.assert_not_called()
        mock_open.assert_not_called()

    async def test_setup_driver_leaves_http_none(self):
        scraper = HybridECourtsScraper(headless=True)
        await scraper.setup_driver()
        assert scraper._http is None

    async def test_navigate_and_select_is_noop(self):
        scraper = HybridECourtsScraper(headless=True)
        scraper.page = None
        await scraper.navigate_and_select()
        assert scraper.page is None


# ── close() ───────────────────────────────────────────────────────────────────

class TestHybridClose:

    async def test_close_closes_http_client(self):
        scraper = HybridECourtsScraper(headless=True)
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.is_closed = False
        scraper._http = mock_http
        await scraper.close()
        mock_http.aclose.assert_called_once()

    async def test_close_clears_http_client_reference(self):
        scraper = HybridECourtsScraper(headless=True)
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.is_closed = False
        scraper._http = mock_http
        await scraper.close()
        assert scraper._http is None

    async def test_close_skips_http_client_if_already_closed(self):
        scraper = HybridECourtsScraper(headless=True)
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.is_closed = True
        scraper._http = mock_http
        await scraper.close()
        mock_http.aclose.assert_not_called()

    async def test_close_closes_open_browser(self):
        scraper = HybridECourtsScraper(headless=True)
        mock_browser = AsyncMock()
        mock_playwright = AsyncMock()
        scraper.browser = mock_browser
        scraper._playwright = mock_playwright
        await scraper.close()
        mock_browser.close.assert_called_once()
        mock_playwright.stop.assert_called_once()

    async def test_close_is_safe_when_nothing_open(self):
        scraper = HybridECourtsScraper(headless=True)
        # Nothing set — should not raise
        await scraper.close()

    async def test_close_clears_browser_reference(self):
        scraper = HybridECourtsScraper(headless=True)
        scraper.browser = AsyncMock()
        scraper._playwright = AsyncMock()
        await scraper.close()
        assert scraper.browser is None
        assert scraper._playwright is None

    async def test_close_does_not_raise_if_browser_close_fails(self):
        scraper = HybridECourtsScraper(headless=True)
        mock_browser = AsyncMock()
        mock_browser.close = AsyncMock(side_effect=Exception("already closed"))
        mock_playwright = AsyncMock()
        scraper.browser = mock_browser
        scraper._playwright = mock_playwright
        await scraper.close()


# ── Parser html= parameter (bypass browser) ───────────────────────────────────

class TestParserHtmlParam:
    """
    Verify that _parse_summary_table(html=...) and _parse_detail_page(html=...)
    work without a browser (no page.content() call).
    """

    async def test_summary_table_with_html_param_no_page_call(self):
        scraper = ECourtsScraper(headless=True)
        # page is intentionally None — should not be touched when html= is provided
        scraper.page = None
        rows = await scraper._parse_summary_table(html=SUMMARY_HTML)
        assert len(rows) == 2

    async def test_summary_table_html_param_correct_values(self):
        scraper = ECourtsScraper(headless=True)
        scraper.page = None
        rows = await scraper._parse_summary_table(html=SUMMARY_HTML)
        assert rows[0]["Case Type/Case Number/Case Year"] == "R.C.A./181/2017"
        assert rows[1]["Case Type/Case Number/Case Year"] == "Civil M.A./465/2017"

    async def test_summary_table_html_param_captures_view_js(self):
        scraper = ECourtsScraper(headless=True)
        scraper.page = None
        rows = await scraper._parse_summary_table(html=SUMMARY_HTML)
        assert "MHPU010023222017" in rows[0]["_view_js"]
        assert "MHPU020028962017" in rows[1]["_view_js"]

    async def test_summary_table_none_html_falls_back_to_page(self):
        scraper = ECourtsScraper(headless=True)
        mock_page = MagicMock()
        mock_page.content = AsyncMock(return_value=SUMMARY_HTML)
        scraper.page = mock_page
        rows = await scraper._parse_summary_table()
        mock_page.content.assert_called_once()
        assert len(rows) == 2

    async def test_detail_page_with_html_param_no_page_call(self):
        scraper = ECourtsScraper(headless=True)
        scraper.page = None
        detail = await scraper._parse_detail_page(html=DETAIL_HTML)
        assert detail["Case_Type"] == "R.C.A. - Regular Civil Appeal"
        assert detail["CNR_Number"] == "MHPU010023222017"

    async def test_detail_page_with_html_param_full_fields(self):
        scraper = ECourtsScraper(headless=True)
        scraper.page = None
        detail = await scraper._parse_detail_page(html=DETAIL_HTML)
        assert detail["Filing_Number"] == "1252/2017"
        assert detail["Decision_Date"] == "21st September 2021"
        assert detail["Case_Status"] == "Case disposed"

    async def test_detail_page_none_html_falls_back_to_page(self):
        scraper = ECourtsScraper(headless=True)
        mock_page = MagicMock()
        mock_page.content = AsyncMock(return_value=DETAIL_HTML)
        scraper.page = mock_page
        detail = await scraper._parse_detail_page()
        mock_page.content.assert_called_once()
        assert detail["Case_Type"] == "R.C.A. - Regular Civil Appeal"


# ── HTTP constants ─────────────────────────────────────────────────────────────

class TestHTTPConstants:

    def test_search_url_points_to_submit_party_name(self):
        assert "submitPartyName" in _SEARCH_URL

    def test_view_history_url_contains_view_history(self):
        assert "viewHistory" in _VIEW_HISTORY_URL

    def test_captcha_url_contains_securimage(self):
        assert "securimage" in _CAPTCHA_URL

    def test_state_code_is_maharashtra(self):
        assert _STATE_CODE == "1"

    def test_dist_code_is_pune(self):
        assert _DIST_CODE == "25"

    def test_court_complex_code_is_pune_district(self):
        assert _COURT_COMPLEX_CODE == "1010303@1,2,3,22,23@N"

    def test_all_urls_use_https(self):
        for url in (_SEARCH_URL, _VIEW_HISTORY_URL, _CAPTCHA_URL):
            assert url.startswith("https://")


class TestUnwrapAjaxHtml:
    def test_unwrap_ajax_html_extracts_case_history_from_jsonish_payload(self):
        raw = (
            '{"case_history":"<div><h3>Petitioner and Advocate<\\/h3>'
            '\\n<ul><li>1) Test Person<\\/li><\\/ul><\\/div>",'
            '"status":1,"search_by":"CSpartyName"}'
        )
        html = HybridECourtsScraper._unwrap_ajax_html(raw)
        assert "Petitioner and Advocate" in html
        assert "<\\/h3>" not in html

    def test_unwrap_ajax_html_prefers_tab_data_over_case_history(self):
        raw = (
            '{"case_history":"<div>history</div>",'
            '"tab_data":"<table><tr><td>Case Type</td><td>R.C.A.</td></tr></table>"}'
        )
        html = HybridECourtsScraper._unwrap_ajax_html(raw)
        assert "Case Type" in html
