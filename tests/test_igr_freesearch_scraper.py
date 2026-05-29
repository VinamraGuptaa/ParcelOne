from igr_freesearch_scraper import IGRFreeSearchScraper


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
    from pathlib import Path

    html_path = Path("artifacts/igr_debug/igr_2025_204_attempt2.html")
    if not html_path.exists():
        return
    html = html_path.read_text(encoding="utf-8")
    current, pages = IGRFreeSearchScraper._registration_grid_pager_pages(html)
    assert current == 1
    assert 2 in pages
    assert max(pages) >= 10


def test_parse_registration_grid_excludes_pager_row():
    from pathlib import Path

    html_path = Path("artifacts/igr_debug/igr_2025_204_attempt2.html")
    if not html_path.exists():
        return
    html = html_path.read_text(encoding="utf-8")
    rows = IGRFreeSearchScraper._parse_registration_grid(html)
    assert len(rows) >= 10
    assert all(r.get("DocNo") for r in rows)
    assert all("Page$" not in (r.get("_row_text") or "") for r in rows)
