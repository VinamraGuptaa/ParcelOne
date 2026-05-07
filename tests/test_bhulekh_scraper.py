"""Unit tests for bhulekh_scraper helpers (no live browser required)."""

from bhulekh_scraper import (
    BhulekhSearchParams,
    _extension_from_content_type_or_url,
    _extract_options_from_select_html,
    extract_document_resource_urls,
    BhulekhScraper,
    find_option_value_by_label,
    normalize_indian_mobile,
)


SAMPLE_SELECT = """
<select id="ContentPlaceHolder1_ddlMainDist" class="form-control">
  <option selected="selected" value="">--निवडा--</option>
  <option value="123">पुणे(Pune)</option>
  <option value="456">मुंबई(Mumbai)</option>
</select>
"""


def test_extract_options_from_select_html():
    opts = _extract_options_from_select_html(SAMPLE_SELECT, "ContentPlaceHolder1_ddlMainDist")
    assert opts == [
        {"value": "", "label": "--निवडा--"},
        {"value": "123", "label": "पुणे(Pune)"},
        {"value": "456", "label": "मुंबई(Mumbai)"},
    ]


def test_find_option_value_by_label():
    opts = [
        {"value": "123", "label": "पुणे(Pune)"},
        {"value": "456", "label": "मुंबई(Mumbai)"},
    ]
    assert find_option_value_by_label(opts, "Pune") == "123"
    assert find_option_value_by_label(opts, "pune") == "123"
    assert find_option_value_by_label(opts, "nosuch") is None


def test_find_option_marathi_alias_for_pune():
    opts = [{"value": "99", "label": "पुणे"}]
    assert find_option_value_by_label(opts, "Pune") == "99"


def test_find_option_marathi_alias_for_satara():
    opts = [{"value": "31", "label": "सातारा"}]
    assert find_option_value_by_label(opts, "Satara") == "31"


def test_find_option_marathi_alias_for_baner():
    opts = [{"value": "11", "label": "बाणेर"}]
    assert find_option_value_by_label(opts, "Baner") == "11"


def test_find_option_marathi_input_variant_maps_to_mulshi_aliases():
    opts = [{"value": "12", "label": "मुळशी"}]
    assert find_option_value_by_label(opts, "मुळ्शी") == "12"


def test_find_option_marathi_alias_for_karve_nagar():
    opts = [{"value": "22", "label": "म .कर्वेनगर"}]
    assert find_option_value_by_label(opts, "Karve Nagar") == "22"


def test_find_option_does_not_reverse_match_survey_prefix():
    opts = [
        {"value": "a", "label": "15/6क/1"},
        {"value": "b", "label": "15/6क/16"},
    ]
    assert find_option_value_by_label(opts, "15/6क/16") == "b"


def test_find_option_matches_latin_suffix_to_devanagari_survey_label():
    opts = [
        {"value": "x", "label": "204/6अ"},
        {"value": "y", "label": "204/6ब"},
    ]
    assert find_option_value_by_label(opts, "204/6A") == "x"


def test_find_option_matches_devanagari_digits_and_spacing_variants():
    opts = [{"value": "x", "label": "२०४ / ६अ"}]
    assert find_option_value_by_label(opts, "204/6A") == "x"


def test_find_option_parentheses_english_only():
    opts = [{"value": "1", "label": "Foo(Bar)"}]
    assert find_option_value_by_label(opts, "Bar") == "1"


def test_find_option_tolerates_question_mark_prefix_from_lossy_decode():
    opts = [{"value": "42", "label": "दारवली"}]
    assert find_option_value_by_label(opts, "?ारवली") == "42"


def test_find_option_tolerates_bom_and_zero_width_chars():
    opts = [{"value": "77", "label": "दारवली"}]
    noisy = "\ufeff\u200bदारवली\u200d"
    assert find_option_value_by_label(opts, noisy) == "77"


def test_normalize_indian_mobile():
    assert normalize_indian_mobile("9999999999") is True
    assert normalize_indian_mobile("5999999999") is False
    assert normalize_indian_mobile("") is False


def test_bhulekh_search_params_defaults():
    p = BhulekhSearchParams(
        district_value="1",
        taluka_value="2",
        village_value="3",
        survey_part1="10",
        survey_number_value="99",
    )
    assert p.mobile == "9999999999"
    assert p.language_value == "en_in"
    assert p.survey_type_option_value == "2"


def test_extract_document_resource_urls_filters_noise():
    html = """
    <html><body>
      <img src="images/dept-logo.png" />
      <img src="reports/output.png" />
      <a href="downloadDoc.aspx?id=1">Download</a>
      <iframe src="/report/view?id=1"></iframe>
      <img src="data:image/png;base64,AAAA" />
    </body></html>
    """
    urls = extract_document_resource_urls(html, "https://bhulekh.mahabhumi.gov.in/NewBhulekh.aspx")
    assert "https://bhulekh.mahabhumi.gov.in/reports/output.png" in urls
    assert "https://bhulekh.mahabhumi.gov.in/downloadDoc.aspx?id=1" in urls
    assert "https://bhulekh.mahabhumi.gov.in/report/view?id=1" in urls
    assert any(u.startswith("data:image/png;base64,") for u in urls)
    assert not any("dept-logo" in u for u in urls)


def test_extension_from_content_type_or_url():
    assert _extension_from_content_type_or_url("application/pdf", "https://x/y") == ".pdf"
    assert _extension_from_content_type_or_url("image/jpeg", "https://x/y") == ".jpg"
    assert _extension_from_content_type_or_url("", "https://x/y/file.png") == ".png"


def test_looks_like_unchanged_form_detects_same_payload():
    before = """
    <html><body>
      Do You Know Your 11 Digit Property UID Number?
      <input id="ContentPlaceHolder1_btnmainsubmit" />
      <input id="ContentPlaceHolder1_txtcaptcha" />
      <select id="ContentPlaceHolder1_ddlMainDist"></select>
    </body></html>
    """
    after = before.replace("</body>", "<span>tiny change</span></body>")
    assert BhulekhScraper._looks_like_unchanged_form(before, after) is True
