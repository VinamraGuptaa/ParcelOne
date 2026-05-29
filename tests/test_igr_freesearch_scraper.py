from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from igr_freesearch_scraper import IGRFreeSearchScraper


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "igr"


def test_parse_result_table_reads_header_and_rows():
    html = """
    <table>
      <tr><th>Doc No</th><th>Party</th></tr>
      <tr><td>123</td><td>Alice</td></tr>
      <tr><td>124</td><td>Bob</td></tr>
    </table>
    """
    rows = IGRFreeSearchScraper._parse_result_table(html)
    assert len(rows) == 2
    assert rows[0]["Doc No"] == "123"
    assert rows[1]["Party"] == "Bob"
    assert rows[0]["_table_index"] == "0"
    assert rows[0]["_row_index"] == "1"


def test_parse_result_table_collects_multiple_tables_and_survey_refs():
    html = """
    <table>
      <tr><th>Section</th><th>Label</th></tr>
      <tr><td>Menu</td><td>मिळकत निहाय/Property Details</td></tr>
    </table>
    <table>
      <tr><th>Survey</th><th>Owner</th></tr>
      <tr><td>70/4</td><td>Alice</td></tr>
      <tr><td>70/3</td><td>Bob</td></tr>
    </table>
    """
    rows = IGRFreeSearchScraper._parse_result_table(html)
    assert len(rows) == 3
    assert rows[1]["Survey"] == "70/4"
    assert rows[1]["_survey_refs"] == "70/4"
    assert rows[2]["_survey_refs"] == "70/3"


def test_normalize_captcha_text_trims_noise_and_length():
    assert IGRFreeSearchScraper._normalize_captcha_text(" ab-12_cd34 ") == "AB12CD"


def test_placeholder_row_detection_for_menu_and_disclaimer():
    menu = {"_row_text": "मिळकत निहाय/Property Details"}
    disclaimer = {"_row_text": "DISCLAIMER Send us feedback on feedback[at]igrmaharashtra[dot]gov[dot]in"}
    detail = {"_row_text": "DocNo 0011 Seller Name ABC Purchaser Name XYZ"}
    assert IGRFreeSearchScraper._is_placeholder_result_row(menu) is True
    assert IGRFreeSearchScraper._is_placeholder_result_row(disclaimer) is True
    assert IGRFreeSearchScraper._is_placeholder_result_row(detail) is False


def test_meaningful_result_rows_filters_placeholder_rows():
    rows = [
        {"_row_text": "मिळकत निहाय/Property Details"},
        {"_row_text": "DISCLAIMER feedback[at]igrmaharashtra[dot]gov[dot]in"},
        {"_row_text": "DocNo 0011 Seller Name ABC Purchaser Name XYZ"},
    ]
    meaningful = IGRFreeSearchScraper._meaningful_result_rows(rows)
    assert len(meaningful) == 1
    assert "DocNo 0011" in meaningful[0]["_row_text"]


def test_pick_option_match_pune_khed_taluka():
    taluka_options = [
        {"label": "---Select Tahsil----", "value": ""},
        {"label": "हवेली", "value": "1"},
        {"label": "खेड", "value": "5"},
        {"label": "मुळशी", "value": "8"},
    ]
    match, usable = IGRFreeSearchScraper._pick_option_match("Khed", taluka_options)
    assert match is not None
    assert match.label == "खेड"
    assert match.value == "5"
    assert len(usable) == 3


def test_pick_option_match_district_pune():
    district_options = [
        {"label": "--Select District--", "value": ""},
        {"label": "पुणे(Pune)", "value": "21"},
        {"label": "सातारा", "value": "22"},
    ]
    match, _ = IGRFreeSearchScraper._pick_option_match("Pune", district_options)
    assert match is not None
    assert "पुणे" in match.label


def test_match_option_label_english_to_marathi_aliases():
    assert IGRFreeSearchScraper._match_option_label("मुळ्शी", "Mulshi") is True
    assert IGRFreeSearchScraper._match_option_label("वाकड", "Wakad") is True
    assert IGRFreeSearchScraper._match_option_label("बाणेर", "Baner") is True
    assert IGRFreeSearchScraper._match_option_label("सातारा", "Satara") is True


def test_match_option_label_works_with_parenthesized_english():
    assert IGRFreeSearchScraper._match_option_label("पुणे(Pune)", "pune") is True


def test_match_option_label_uruli_aliases():
    assert IGRFreeSearchScraper._match_option_label("उरुळी कांचन", "Uruli") is True
    assert IGRFreeSearchScraper._match_option_label("उरळी कांचन", "Uruli Kanchan") is True


def test_match_option_label_karve_nagar_aliases():
    assert IGRFreeSearchScraper._match_option_label("म .कर्वेनगर", "Karve Nagar") is True
    assert IGRFreeSearchScraper._match_option_label("कर्वेनगर", "karvenagar") is True


def test_match_option_label_tolerates_lossy_prefix_question_mark():
    assert IGRFreeSearchScraper._match_option_label("दारवली", "?ारवली") is True


def test_match_option_label_tolerates_bom_and_zero_width():
    assert IGRFreeSearchScraper._match_option_label("दारवली", "\ufeff\u200bदारवली\u200d") is True


def test_save_raw_search_html_writes_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("IGR_SAVE_RAW_HTML", "1")
    monkeypatch.setenv("IGR_RAW_HTML_DIR", str(tmp_path))
    html = "<html><body><table><tr><td>DocNo</td></tr></table></body></html>"
    out = IGRFreeSearchScraper._save_raw_search_html(
        html, survey_number="1530/3", year="2024", attempt=1
    )
    assert out is not None
    assert out.exists()
    assert out.read_text(encoding="utf-8") == html


def test_html_indicates_zero_results_marathi_and_english():
    assert IGRFreeSearchScraper._html_indicates_zero_results("आढळून आलेली नाही")
    assert IGRFreeSearchScraper._html_indicates_zero_results(
        "<div>No records found for this search</div>"
    )
    assert not IGRFreeSearchScraper._html_indicates_zero_results("<html><form></form></html>")


def test_page_html_has_registration_grid():
    grid_html = """
    <table id="RegistrationGrid">
      <tr><th>DocNo</th><th>Seller</th></tr>
      <tr><td>100</td><td>Alice</td></tr>
    </table>
    """
    assert IGRFreeSearchScraper._page_html_has_registration_grid(grid_html)
    assert not IGRFreeSearchScraper._page_html_has_registration_grid("<html><form></form></html>")


def test_save_raw_search_html_skipped_by_default(monkeypatch):
    monkeypatch.delenv("IGR_SAVE_RAW_HTML", raising=False)
    out = IGRFreeSearchScraper._save_raw_search_html(
        "<html></html>", survey_number="70", year="2020", attempt=1
    )
    assert out is None


def test_classify_igr_search_html_detects_grid_zero_phase1_and_rejection():
    grid_html = """
    <html><body>
      <table id="RegistrationGrid">
        <tr><th>DocNo</th><th>Seller</th></tr>
        <tr><td>100</td><td>Alice</td></tr>
      </table>
    </body></html>
    """
    assert IGRFreeSearchScraper._classify_igr_search_html(grid_html) == "grid"
    assert (
        IGRFreeSearchScraper._classify_igr_search_html(
            "<html>आढळून आलेली नाही</html>"
        )
        == "zero"
    )
    assert (
        IGRFreeSearchScraper._classify_igr_search_html(
            "<html><form></form></html>",
            previous_captcha_fp="a",
            current_captcha_fp="b",
        )
        == "phase1"
    )
    assert (
        IGRFreeSearchScraper._classify_igr_search_html(
            "<html><div>No record found</div></html>"
        )
        == "zero"
    )
    assert (
        IGRFreeSearchScraper._classify_igr_search_html(
            "<html></html>",
            status_text="Incorrect captcha entered",
        )
        == "wrong_captcha"
    )
    assert (
        IGRFreeSearchScraper._classify_igr_search_html(
            "<html></html>",
            status_text="You have entered correct captcha",
        )
        == "pending"
    )


def test_captcha_status_indicates_rejection():
    assert IGRFreeSearchScraper._captcha_status_indicates_rejection("Incorrect captcha") is True
    assert IGRFreeSearchScraper._captcha_status_indicates_rejection("You have entered correct captcha") is False


def test_registration_grid_pager_pages_from_saved_html():
    html_path = FIXTURE_DIR / "registration_grid_page1.html"
    html = html_path.read_text(encoding="utf-8")
    current, pages = IGRFreeSearchScraper._registration_grid_pager_pages(html)
    assert current == 1
    assert 2 in pages
    assert pages == [1, 2]


def test_parse_registration_grid_excludes_pager_row():
    html_path = FIXTURE_DIR / "registration_grid_page1.html"
    html = html_path.read_text(encoding="utf-8")
    rows = IGRFreeSearchScraper._parse_registration_grid(html)
    assert len(rows) == 2
    assert all(r.get("DocNo") for r in rows)
    assert all("Page$" not in (r.get("_row_text") or "") for r in rows)


@pytest.mark.asyncio
async def test_collect_registration_grid_pages_dedupes_across_pages(monkeypatch):
    page1 = (FIXTURE_DIR / "registration_grid_page1.html").read_text(encoding="utf-8")
    page2 = (FIXTURE_DIR / "registration_grid_page2.html").read_text(encoding="utf-8")
    scraper = IGRFreeSearchScraper()
    scraper.page = MagicMock()
    scraper.page.content = AsyncMock(return_value=page2)
    scraper._go_to_registration_grid_page = AsyncMock()

    rows = await scraper._collect_all_registration_grid_pages(
        page1,
        survey_number="204",
        year="2025",
        attempt=1,
    )

    assert [r["DocNo"] for r in rows] == ["1001", "1002", "1003"]
    scraper._go_to_registration_grid_page.assert_awaited_once_with(2)


def test_submit_appears_unresponsive_idle_form(monkeypatch):
    monkeypatch.setenv("IGR_NO_RESPONSE_SECONDS", "25")
    from importlib import reload

    import igr_freesearch_scraper as mod

    reload(mod)
    html = "<html><form><input id='txtImg1'></form></html>"
    assert mod.IGRFreeSearchScraper._submit_appears_unresponsive(
        elapsed_s=25,
        captcha_fp_before="fp1",
        captcha_fp_current="fp1",
        html=html,
        still_loading=False,
    )


def test_submit_appears_unresponsive_not_during_phase2_accepted(monkeypatch):
    monkeypatch.setenv("IGR_NO_RESPONSE_SECONDS", "10")
    from importlib import reload

    import igr_freesearch_scraper as mod

    reload(mod)
    assert not mod.IGRFreeSearchScraper._submit_appears_unresponsive(
        elapsed_s=12,
        captcha_fp_before="fp1",
        captcha_fp_current="fp1",
        html="<html></html>",
        still_loading=False,
        status_text="You have entered correct captcha",
    )


def test_submit_appears_unresponsive_not_when_captcha_rotated(monkeypatch):
    monkeypatch.setenv("IGR_NO_RESPONSE_SECONDS", "25")
    from importlib import reload

    import igr_freesearch_scraper as mod

    reload(mod)
    html = "<html><form></form></html>"
    assert not mod.IGRFreeSearchScraper._submit_appears_unresponsive(
        elapsed_s=25,
        captcha_fp_before="fp1",
        captcha_fp_current="fp2",
        html=html,
        still_loading=False,
    )


def test_submit_appears_unresponsive_not_while_loading(monkeypatch):
    monkeypatch.setenv("IGR_NO_RESPONSE_SECONDS", "25")
    from importlib import reload

    import igr_freesearch_scraper as mod

    reload(mod)
    assert not mod.IGRFreeSearchScraper._submit_appears_unresponsive(
        elapsed_s=25,
        captcha_fp_before="fp1",
        captcha_fp_current="fp1",
        html="<html></html>",
        still_loading=True,
    )


def _prepared_search_scraper(monkeypatch, *, snapshot: dict | None = None) -> IGRFreeSearchScraper:
    monkeypatch.setattr(
        "api.location_labels.resolve_igr_labels",
        lambda d, t, v: (d, t, v, None),
    )
    scraper = IGRFreeSearchScraper()
    scraper.page = MagicMock()
    scraper.page.url = "https://freesearchigrservice.maharashtra.gov.in/"
    scraper.page.content = AsyncMock(return_value="<html><form></form></html>")
    scraper._close_startup_popup = AsyncMock()
    scraper._fill_search_form = AsyncMock()
    scraper._read_form_snapshot = AsyncMock(
        return_value=snapshot
        or {
            "district": "Pune",
            "taluka": "Shirur",
            "village": "Talegaon Dhamdhere",
            "survey": "3954",
            "year": "2025",
        }
    )
    scraper._snapshot_matches_expected = MagicMock(return_value=True)
    scraper._get_captcha_src_fingerprint = AsyncMock(return_value="fp")
    scraper._solve_captcha = AsyncMock(return_value="ABC123")
    scraper._fill_captcha_field = AsyncMock(return_value=True)
    scraper._submit_search = AsyncMock()
    scraper._wait_for_postback_settle = AsyncMock()
    scraper._save_raw_search_html = MagicMock(return_value=None)
    return scraper


@pytest.mark.asyncio
async def test_search_rest_maharashtra_marks_second_submit_as_phase2(monkeypatch):
    grid_html = (FIXTURE_DIR / "registration_grid_page1.html").read_text(encoding="utf-8")
    scraper = _prepared_search_scraper(monkeypatch)
    scraper._solve_captcha = AsyncMock(side_effect=["111111", "222222"])
    scraper._wait_for_igr_search_outcome = AsyncMock(
        side_effect=[("phase1", "<html></html>"), ("grid", grid_html)]
    )
    scraper._wait_for_captcha_image_ready = AsyncMock(return_value=True)
    scraper._collect_all_registration_grid_pages = AsyncMock(
        return_value=[
            {
                "DocNo": "1001",
                "RDate": "01/01/2025",
                "Seller Name": "Alice",
                "Purchaser Name": "Bob",
                "Property Description": "गट नंबर 204 हिस्सा 6अ",
                "_row_text": "DocNo 1001 Alice Bob",
            }
        ]
    )
    scraper._click_cancel_for_next_year = AsyncMock()

    rows = await scraper.search_rest_maharashtra(
        district_label="Pune",
        taluka_label="Shirur",
        village_label="Talegaon Dhamdhere",
        survey_number="3954",
        year="2025",
    )

    assert len(rows) == 1
    assert scraper._wait_for_igr_search_outcome.await_args_list[0].kwargs["phase2_submit"] is False
    assert scraper._wait_for_igr_search_outcome.await_args_list[1].kwargs["phase2_submit"] is True


@pytest.mark.asyncio
async def test_search_rest_maharashtra_retries_no_response_then_skips(monkeypatch):
    import igr_freesearch_scraper as mod

    monkeypatch.setattr(mod, "IGR_PAGE_REFRESH_RETRIES", 2)
    scraper = _prepared_search_scraper(monkeypatch)
    scraper._wait_for_igr_search_outcome = AsyncMock(
        side_effect=[("no_response", "<html></html>"), ("no_response", "<html></html>")]
    )
    scraper._recover_search_page_after_stall = AsyncMock()
    scraper._skip_year_as_empty = AsyncMock(return_value=[])

    rows = await scraper.search_rest_maharashtra(
        district_label="Pune",
        taluka_label="Shirur",
        village_label="Talegaon Dhamdhere",
        survey_number="3954",
        year="2025",
    )

    assert rows == []
    scraper._recover_search_page_after_stall.assert_awaited_once()
    scraper._skip_year_as_empty.assert_awaited_once()


@pytest.mark.asyncio
async def test_fill_survey_number_retries_when_value_does_not_stick():
    scraper = IGRFreeSearchScraper()
    scraper.page = MagicMock()
    scraper.page.fill = AsyncMock()
    scraper.page.evaluate = AsyncMock()
    scraper._read_form_snapshot = AsyncMock(
        side_effect=[
            {"survey": ""},
            {"survey": "3954"},
        ]
    )

    await scraper._fill_survey_number("3954")

    assert scraper.page.fill.await_count == 2
    assert scraper.page.evaluate.await_count == 2
