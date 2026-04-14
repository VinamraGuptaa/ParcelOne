"""
Unit tests for scraper HTML parsing methods (scraper.py).

All tests mock self.page so no real browser is launched.
The HTML fixtures mirror the actual eCourts website structure
observed during live runs.
"""

import os
import tempfile
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from scraper import ECourtsScraper


# ── HTML fixtures ─────────────────────────────────────────────────────────────

SUMMARY_NO_RECORDS_HTML = """
<html><body>
<div class="alert">No record found for this search criteria</div>
</body></html>
"""

SUMMARY_ONE_ROW_HTML = """
<html><body>
<table id="dispTable" class="table table-bordered">
  <thead>
    <tr>
      <th scope="col">Sr No</th>
      <th scope="col">Case Type/Case Number/Case Year</th>
      <th scope="col">Petitioner Name versus Respondent Name</th>
      <th scope="col">View</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <th colspan="3" scope="colgroup" id="td_court_name_1">District and Session Court ,Pune</th>
    </tr>
    <tr>
      <td>1</td>
      <td>R.C.A./181/2017</td>
      <td>Asha Rajesh Gupta</td>
      <td>
        <a href="#"
           onclick="viewHistory(200100001812017,'MHPU010023222017',1,'','CScaseNumber',1,25,1010303,'CSpartyName')">
          View
        </a>
      </td>
    </tr>
  </tbody>
</table>
</body></html>
"""

SUMMARY_MULTI_ROW_HTML = """
<html><body>
<table id="dispTable" class="table">
  <thead>
    <tr>
      <th scope="col">Sr No</th>
      <th scope="col">Case Type/Case Number/Case Year</th>
      <th scope="col">Petitioner Name versus Respondent Name</th>
      <th scope="col">View</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <th colspan="3" scope="colgroup">District and Session Court ,Pune</th>
    </tr>
    <tr>
      <td>1</td>
      <td>R.C.A./181/2017</td>
      <td>Asha Rajesh Gupta</td>
      <td><a href="#" onclick="viewHistory(200100001812017,'MHPU010023222017',1,'','CScaseNumber',1,25,1010303,'CSpartyName')">View</a></td>
    </tr>
    <tr>
      <th colspan="3" scope="colgroup">Civil Court Senior Division ,Pune</th>
    </tr>
    <tr>
      <td>2</td>
      <td>Civil M.A./465/2017</td>
      <td>Vipin Kumar Gupta Vs Rajesh Gupta</td>
      <td><a href="#" onclick="viewHistory(200300004652017,'MHPU020028962017',2,'','CScaseNumber',1,25,1010303,'CSpartyName')">View</a></td>
    </tr>
  </tbody>
</table>
</body></html>
"""

SUMMARY_BR_PETITIONER_HTML = """
<html><body>
<table id="dispTable" class="table">
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
      <td>1</td>
      <td>Civil M.A./465/2017</td>
      <td>Vipin Kumar Gupta<br>Vs<br>Rajesh Gupta</td>
      <td><a href="#" onclick="viewHistory(1,'CNR001',1,'','CS',1,25,1010303,'CSpartyName')">View</a></td>
    </tr>
  </tbody>
</table>
</body></html>
"""

DETAIL_FULL_HTML = """
<html><body>
<table>
  <tr><td>Case Type</td><td>R.C.A. - Regular Civil Appeal</td></tr>
  <tr><td>Filing Number</td><td>1252/2017</td></tr>
  <tr><td>Filing Date</td><td>16-02-2017</td></tr>
  <tr><td>Registration Number</td><td>181/2017</td></tr>
  <tr><td>Registration Date</td><td>21-03-2017</td></tr>
  <tr><td>e-Filing Number</td><td>EF001/2017</td></tr>
  <tr><td>e-Filing Date</td><td>15-02-2017</td></tr>
  <tr><td>CNR Number</td><td><span class="text-danger text-uppercase">MHPU010023222017</span></td></tr>
  <tr><td>First Hearing Date</td><td>21st March 2017</td></tr>
  <tr><td>Decision Date</td><td>21st September 2021</td></tr>
  <tr><td>Case Status</td><td>Case disposed</td></tr>
  <tr><td>Nature of Disposal</td><td>Uncontested--ALLOWED OTHERWISE</td></tr>
  <tr><td>Court Number and Judge</td><td>53-DISTRICT JUDGE -15</td></tr>
</table>
<table>
  <tr><th>Under Act(s)</th></tr>
  <tr><td>Maharashtra Rent Control Act</td></tr>
  <tr><td>Transfer of Property Act</td></tr>
</table>
<ul class="petitioner-advocate-list">
  <li>1) Test Petitioner</li>
  <li>Advocate- Test Advocate</li>
</ul>
<ul class="respondent-advocate-list">
  <li>1) Test Respondent</li>
  <li>2) Another Respondent</li>
</ul>
</body></html>
"""

DETAIL_PENDING_HTML = """
<html><body>
<table>
  <tr><td>Case Type</td><td>Civil M.A. - Civil Misc. Application</td></tr>
  <tr><td>Filing Number</td><td>3868/2017</td></tr>
  <tr><td>Filing Date</td><td>29-04-2017</td></tr>
  <tr><td>Registration Number</td><td>465/2017</td></tr>
  <tr><td>Registration Date</td><td>14-06-2017</td></tr>
  <tr><td>CNR Number</td><td><span class="text-danger text-uppercase">MHPU020028962017</span></td></tr>
  <tr><td>Court Number and Judge</td><td>8-15TH JOINT CJSD PUNE</td></tr>
</table>
<ul class="petitioner-advocate-list">
  <li>1) Vipin Kumar Gupta</li>
</ul>
<ul class="respondent-advocate-list">
  <li>1) Rajesh Gupta</li>
</ul>
</body></html>
"""

DETAIL_CNR_BOLD_HTML = """
<html><body>
<table>
  <tr><td>Case Type</td><td>R.C.A. - Regular Civil Appeal</td></tr>
  <tr><td>CNR Number</td><td><span class="fw-bold text-uppercase">MHPU999999992024</span></td></tr>
</table>
</body></html>
"""


# ── Helpers ───────────────────────────────────────────────────────────────────

def _scraper_with_html(html: str) -> ECourtsScraper:
    """Return an ECourtsScraper whose page.content() returns the given HTML."""
    scraper = ECourtsScraper(headless=True)
    mock_page = MagicMock()
    mock_page.content = AsyncMock(return_value=html)
    scraper.page = mock_page
    return scraper


# ── _parse_summary_table ──────────────────────────────────────────────────────

class TestParseSummaryTable:

    async def test_no_records_returns_empty_list(self):
        scraper = _scraper_with_html(SUMMARY_NO_RECORDS_HTML)
        rows = await scraper._parse_summary_table()
        assert rows == []

    async def test_single_row_parsed(self):
        scraper = _scraper_with_html(SUMMARY_ONE_ROW_HTML)
        rows = await scraper._parse_summary_table()
        assert len(rows) == 1

    async def test_column_names_are_proper_headers(self):
        scraper = _scraper_with_html(SUMMARY_ONE_ROW_HTML)
        rows = await scraper._parse_summary_table()
        row = rows[0]
        assert "Sr No" in row
        assert "Case Type/Case Number/Case Year" in row
        assert "Petitioner Name versus Respondent Name" in row

    async def test_sr_no_value(self):
        scraper = _scraper_with_html(SUMMARY_ONE_ROW_HTML)
        rows = await scraper._parse_summary_table()
        assert rows[0]["Sr No"] == "1"

    async def test_case_number_value(self):
        scraper = _scraper_with_html(SUMMARY_ONE_ROW_HTML)
        rows = await scraper._parse_summary_table()
        assert rows[0]["Case Type/Case Number/Case Year"] == "R.C.A./181/2017"

    async def test_petitioner_value(self):
        scraper = _scraper_with_html(SUMMARY_ONE_ROW_HTML)
        rows = await scraper._parse_summary_table()
        assert rows[0]["Petitioner Name versus Respondent Name"] == "Asha Rajesh Gupta"

    async def test_view_onclick_captured_as_view_js(self):
        scraper = _scraper_with_html(SUMMARY_ONE_ROW_HTML)
        rows = await scraper._parse_summary_table()
        assert "_view_js" in rows[0]
        assert "viewHistory" in rows[0]["_view_js"]
        assert "MHPU010023222017" in rows[0]["_view_js"]

    async def test_view_key_not_in_row_data(self):
        scraper = _scraper_with_html(SUMMARY_ONE_ROW_HTML)
        rows = await scraper._parse_summary_table()
        assert "View" not in rows[0]

    async def test_section_header_rows_are_skipped(self):
        scraper = _scraper_with_html(SUMMARY_MULTI_ROW_HTML)
        rows = await scraper._parse_summary_table()
        # 2 data rows, 2 section headers — only data rows returned
        assert len(rows) == 2
        for row in rows:
            # No section header text should appear as a value
            for val in row.values():
                assert "District and Session Court" not in str(val)
                assert "Civil Court Senior Division" not in str(val)

    async def test_multiple_rows_all_parsed(self):
        scraper = _scraper_with_html(SUMMARY_MULTI_ROW_HTML)
        rows = await scraper._parse_summary_table()
        case_numbers = [r["Case Type/Case Number/Case Year"] for r in rows]
        assert "R.C.A./181/2017" in case_numbers
        assert "Civil M.A./465/2017" in case_numbers

    async def test_petitioner_with_br_tag_joined_with_space(self):
        scraper = _scraper_with_html(SUMMARY_BR_PETITIONER_HTML)
        rows = await scraper._parse_summary_table()
        petitioner = rows[0]["Petitioner Name versus Respondent Name"]
        assert "Vipin Kumar Gupta" in petitioner
        assert "Rajesh Gupta" in petitioner

    async def test_empty_table_returns_no_rows(self):
        html = """
        <html><body>
        <table id="dispTable" class="table">
          <thead><tr><th>Sr No</th><th>Case Type/Case Number/Case Year</th>
          <th>Petitioner Name versus Respondent Name</th><th>View</th></tr></thead>
          <tbody></tbody>
        </table>
        </body></html>
        """
        scraper = _scraper_with_html(html)
        rows = await scraper._parse_summary_table()
        assert rows == []

    async def test_second_row_has_correct_onclick(self):
        scraper = _scraper_with_html(SUMMARY_MULTI_ROW_HTML)
        rows = await scraper._parse_summary_table()
        assert "MHPU020028962017" in rows[1]["_view_js"]


# ── _parse_detail_page ────────────────────────────────────────────────────────

class TestParseDetailPage:

    async def test_case_type_extracted(self):
        scraper = _scraper_with_html(DETAIL_FULL_HTML)
        detail = await scraper._parse_detail_page()
        assert detail["Case_Type"] == "R.C.A. - Regular Civil Appeal"

    async def test_filing_number_extracted(self):
        scraper = _scraper_with_html(DETAIL_FULL_HTML)
        detail = await scraper._parse_detail_page()
        assert detail["Filing_Number"] == "1252/2017"

    async def test_filing_date_extracted(self):
        scraper = _scraper_with_html(DETAIL_FULL_HTML)
        detail = await scraper._parse_detail_page()
        assert detail["Filing_Date"] == "16-02-2017"

    async def test_registration_number_extracted(self):
        scraper = _scraper_with_html(DETAIL_FULL_HTML)
        detail = await scraper._parse_detail_page()
        assert detail["Registration_Number"] == "181/2017"

    async def test_registration_date_extracted(self):
        scraper = _scraper_with_html(DETAIL_FULL_HTML)
        detail = await scraper._parse_detail_page()
        assert detail["Registration_Date"] == "21-03-2017"

    async def test_efiling_number_extracted(self):
        scraper = _scraper_with_html(DETAIL_FULL_HTML)
        detail = await scraper._parse_detail_page()
        assert detail.get("eFiling_Number") == "EF001/2017"

    async def test_cnr_number_from_span_text_danger(self):
        scraper = _scraper_with_html(DETAIL_FULL_HTML)
        detail = await scraper._parse_detail_page()
        assert detail["CNR_Number"] == "MHPU010023222017"

    async def test_cnr_number_from_fw_bold_span(self):
        scraper = _scraper_with_html(DETAIL_CNR_BOLD_HTML)
        detail = await scraper._parse_detail_page()
        assert detail["CNR_Number"] == "MHPU999999992024"

    async def test_under_acts_single_act(self):
        scraper = _scraper_with_html(DETAIL_FULL_HTML)
        detail = await scraper._parse_detail_page()
        assert "Maharashtra Rent Control Act" in detail["Under_Acts"]

    async def test_under_acts_multiple_joined_with_pipe(self):
        scraper = _scraper_with_html(DETAIL_FULL_HTML)
        detail = await scraper._parse_detail_page()
        assert " | " in detail["Under_Acts"]
        assert "Transfer of Property Act" in detail["Under_Acts"]

    async def test_first_hearing_date_extracted(self):
        scraper = _scraper_with_html(DETAIL_FULL_HTML)
        detail = await scraper._parse_detail_page()
        assert detail["First_Hearing_Date"] == "21st March 2017"

    async def test_decision_date_extracted(self):
        scraper = _scraper_with_html(DETAIL_FULL_HTML)
        detail = await scraper._parse_detail_page()
        assert detail["Decision_Date"] == "21st September 2021"

    async def test_case_status_extracted(self):
        scraper = _scraper_with_html(DETAIL_FULL_HTML)
        detail = await scraper._parse_detail_page()
        assert detail["Case_Status"] == "Case disposed"

    async def test_nature_of_disposal_extracted(self):
        scraper = _scraper_with_html(DETAIL_FULL_HTML)
        detail = await scraper._parse_detail_page()
        assert detail["Nature_of_Disposal"] == "Uncontested--ALLOWED OTHERWISE"

    async def test_court_number_judge_extracted(self):
        scraper = _scraper_with_html(DETAIL_FULL_HTML)
        detail = await scraper._parse_detail_page()
        assert detail["Court_Number_Judge"] == "53-DISTRICT JUDGE -15"

    async def test_petitioner_and_advocate_extracted(self):
        scraper = _scraper_with_html(DETAIL_FULL_HTML)
        detail = await scraper._parse_detail_page()
        assert "Test Petitioner" in detail["Petitioner_and_Advocate"]
        assert "Test Advocate" in detail["Petitioner_and_Advocate"]

    async def test_respondent_and_advocate_extracted(self):
        scraper = _scraper_with_html(DETAIL_FULL_HTML)
        detail = await scraper._parse_detail_page()
        assert "Test Respondent" in detail["Respondent_and_Advocate"]

    async def test_pending_case_has_no_decision_date(self):
        scraper = _scraper_with_html(DETAIL_PENDING_HTML)
        detail = await scraper._parse_detail_page()
        assert "Decision_Date" not in detail

    async def test_pending_case_has_no_case_status(self):
        scraper = _scraper_with_html(DETAIL_PENDING_HTML)
        detail = await scraper._parse_detail_page()
        assert "Case_Status" not in detail

    async def test_empty_fields_not_in_result(self):
        # Only Case_Type present — everything else empty
        html = """
        <html><body>
        <table>
          <tr><td>Case Type</td><td>R.C.A. - Test</td></tr>
          <tr><td>Filing Number</td><td></td></tr>
        </table>
        </body></html>
        """
        scraper = _scraper_with_html(html)
        detail = await scraper._parse_detail_page()
        assert "Case_Type" in detail
        assert "Filing_Number" not in detail

    async def test_returns_dict_on_exception(self):
        # page.content() raises — should return empty dict, not raise
        scraper = ECourtsScraper(headless=True)
        mock_page = MagicMock()
        mock_page.content = AsyncMock(side_effect=Exception("network error"))
        scraper.page = mock_page
        detail = await scraper._parse_detail_page()
        assert isinstance(detail, dict)


# ── _get_available_years ──────────────────────────────────────────────────────

class TestGetAvailableYears:

    def test_returns_list_of_strings(self):
        scraper = ECourtsScraper()
        years = scraper._get_available_years()
        assert all(isinstance(y, str) for y in years)

    def test_current_year_is_first(self):
        import datetime
        scraper = ECourtsScraper()
        years = scraper._get_available_years()
        assert years[0] == str(datetime.datetime.now().year)

    def test_descending_order(self):
        scraper = ECourtsScraper()
        years = scraper._get_available_years()
        as_ints = [int(y) for y in years]
        assert as_ints == sorted(as_ints, reverse=True)

    def test_goes_back_15_years(self):
        import datetime
        scraper = ECourtsScraper()
        years = scraper._get_available_years()
        oldest_expected = str(datetime.datetime.now().year - 14)
        assert oldest_expected in years

    def test_has_exactly_15_years(self):
        scraper = ECourtsScraper()
        years = scraper._get_available_years()
        assert len(years) == 15


# ── export_to_csv ─────────────────────────────────────────────────────────────

class TestExportToCsv:

    def test_creates_csv_file(self):
        data = [{"Name": "Rajesh", "Year": "2017", "CNR": "MHPU001"}]
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
            path = f.name
        try:
            ECourtsScraper.export_to_csv(data, path)
            assert os.path.exists(path)
            assert os.path.getsize(path) > 0
        finally:
            os.unlink(path)

    def test_csv_contains_expected_data(self):
        import pandas as pd
        data = [
            {"Case_Type": "R.C.A.", "Filing_Number": "1252/2017", "Search_Year": "2017"},
        ]
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
            path = f.name
        try:
            ECourtsScraper.export_to_csv(data, path)
            df = pd.read_csv(path, encoding="utf-8-sig")
            assert "Case_Type" in df.columns
            assert df.iloc[0]["Filing_Number"] == "1252/2017"
        finally:
            os.unlink(path)

    def test_empty_data_does_not_create_file(self):
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
            path = f.name
        os.unlink(path)  # remove so we can check it's not recreated
        ECourtsScraper.export_to_csv([], path)
        assert not os.path.exists(path)

    def test_multiple_records_all_written(self):
        import pandas as pd
        data = [{"Col": str(i)} for i in range(5)]
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
            path = f.name
        try:
            ECourtsScraper.export_to_csv(data, path)
            df = pd.read_csv(path, encoding="utf-8-sig")
            assert len(df) == 5
        finally:
            os.unlink(path)
