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
