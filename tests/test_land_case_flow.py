"""Unit tests for land-to-cases helper logic."""

from api.land_case_flow import (
    _parse_land_record_text,
    build_name_variants,
    extract_survey_option_labels,
    extract_land_entity,
    rank_case_hits,
    rank_api_case_hits,
    score_case_against_variants,
    dedupe_case_key,
)


def test_extract_land_entity_from_html_text():
    html = """
    <html><body>
      <table>
        <tr><td>Name of the occupant</td><td>snehal bhushan dhut</td></tr>
        <tr><td>Mutation number</td><td>(20133)</td></tr>
      </table>
    </body></html>
    """
    out = extract_land_entity(html)
    assert out.occupant_primary_name is not None
    assert any("snehal" in c.lower() for c in out.occupant_candidates)
    assert "(20133)" in out.mutation_numbers or "20133" in out.mutation_numbers
    assert out.extraction_confidence > 0.5


def test_parse_land_record_text_extracts_occupant_and_mutation():
    text = """
    Name of the occupant
    18193 snehal bhushan dhut 1.38.50 2.72 0.22.00 (20133)
    """
    names, muts = _parse_land_record_text(text)
    assert any("snehal bhushan dhut" in n for n in names)
    assert any("20133" in m for m in muts)


def test_build_name_variants_is_bounded_and_deterministic():
    v1 = build_name_variants("Snehal Bhushan Dhut", max_variants=6)
    v2 = build_name_variants("Snehal Bhushan Dhut", max_variants=6)
    assert [x.variant_text for x in v1] == [x.variant_text for x in v2]
    assert 1 <= len(v1) <= 6


def test_score_case_against_variants_prefers_exact():
    score, variant, reason = score_case_against_variants(
        "Snehal Bhushan Dhut versus State of Maharashtra",
        ["Snehal Bhushan Dhut", "S B Dhut"],
    )
    assert score == 1.0
    assert variant == "Snehal Bhushan Dhut"
    assert reason == "exact_substring"


def test_rank_case_hits_civil_first_then_score():
    records = [
        {
            "Search_Year": "2024",
            "Case_Type": "Criminal Appeal",
            "Petitioner Name versus Respondent Name": "Snehal Bhushan Dhut vs X",
            "CNR_Number": "A1",
        },
        {
            "Search_Year": "2023",
            "Case_Type": "Regular Civil Appeal",
            "Petitioner Name versus Respondent Name": "SNEHAL BHUSHAN DHUT vs Y",
            "CNR_Number": "A2",
        },
    ]
    variants = build_name_variants("Snehal Bhushan Dhut")
    out = rank_case_hits(records, variants, min_score=0.2)
    assert len(out) >= 2
    assert out[0].is_civil is True
    assert out[0].name_match_score >= out[1].name_match_score or out[1].is_civil is False


def test_extract_survey_option_labels_filters_by_part1():
    html = """
    <select id="ContentPlaceHolder1_ddlsurveyno">
      <option value="">--निवडा--</option>
      <option value="1530/1">1530/1</option>
      <option value="1530/2">1530/2</option>
      <option value="1530/3">1530/3</option>
      <option value="1531/1">1531/1</option>
    </select>
    """
    out = extract_survey_option_labels(html, "1530")
    assert out == ["1530/1", "1530/2", "1530/3"]


def test_rank_api_case_hits_prioritizes_civil_pending_and_party_overlap():
    records = [
        {
            "case_id": "A",
            "case_type": "Regular Civil Appeal",
            "case_status": "Pending",
            "parties_text": "Bhagwan Ramchandra Murkute vs Seller A",
            "search_year": "2024",
            "cnr": "CNR-A",
        },
        {
            "case_id": "B",
            "case_type": "Criminal Case",
            "case_status": "Disposed",
            "parties_text": "Bhagwan Ramchandra Murkute vs Unknown",
            "search_year": "2025",
            "cnr": "CNR-B",
        },
    ]
    out = rank_api_case_hits(
        records,
        owner_name="Bhagwan Ramchandra Murkute",
        igr_party_names=["Seller A", "Purchaser A"],
        min_score=0.0,
    )
    assert len(out) == 2
    assert out[0].case_id == "A"
    assert out[0].is_civil is True
    assert "pending=True" in (out[0].match_explanation or "")


def test_rank_api_case_hits_adds_low_district_court_bonus():
    records = [
        {
            "case_id": "A",
            "case_type": "Criminal Case",
            "case_status": "Pending",
            "parties_text": "Snehal Bhooshan Dhoot vs Unknown",
            "court": "District Court Pune",
            "search_year": "2024",
            "cnr": "CNR-A",
        },
        {
            "case_id": "B",
            "case_type": "Criminal Case",
            "case_status": "Pending",
            "parties_text": "Snehal Bhooshan Dhoot vs Unknown",
            "court": "District Court Nashik",
            "search_year": "2024",
            "cnr": "CNR-B",
        },
    ]
    out = rank_api_case_hits(
        records,
        owner_name="Snehal Bhooshan Dhoot",
        igr_party_names=[],
        district_label="Pune",
        taluka_label="Haveli",
        min_score=0.0,
    )
    assert len(out) == 2
    assert out[0].case_id == "A"
    assert "district_court_overlap=1.00" in (out[0].match_explanation or "")
    assert "court_location_overlap=" in (out[0].match_explanation or "")


def test_rank_api_case_hits_prioritizes_taluka_court_overlap():
    records = [
        {
            "case_id": "KOREGAON",
            "case_type": "Criminal Case",
            "case_status": "Pending",
            "parties_text": "A vs B",
            "court": "Civil Judge Junior Division Koregaon, Satara",
            "search_year": "2024",
            "cnr": "CNR-K",
        },
        {
            "case_id": "SATARA_ONLY",
            "case_type": "Criminal Case",
            "case_status": "Pending",
            "parties_text": "A vs B",
            "court": "District Court Satara",
            "search_year": "2024",
            "cnr": "CNR-S",
        },
    ]
    out = rank_api_case_hits(
        records,
        owner_name="A",
        igr_party_names=[],
        district_label="Satara",
        taluka_label="Koregaon",
        village_label="Dhamne",
        min_score=0.0,
    )
    assert len(out) == 2
    assert out[0].case_id == "KOREGAON"


def test_rank_api_case_hits_marathi_taluka_matches_english_court():
    records = [
        {
            "case_id": "K",
            "case_type": "Criminal Case",
            "case_status": "Pending",
            "parties_text": "A vs B",
            "court": "CIVIL JUDGE JR.DN. J.M.F.C. KOREGAON",
            "search_year": "2024",
            "cnr": "CNR-K",
        }
    ]
    out = rank_api_case_hits(
        records,
        owner_name="A",
        igr_party_names=[],
        district_label="सातारा",
        taluka_label="कोरेगाव",
        village_label="धामणेर",
        min_score=0.0,
    )
    assert len(out) == 1
    assert "court_location_overlap=" in (out[0].match_explanation or "")
    assert "court_location_overlap=0.00" not in (out[0].match_explanation or "")


def test_rank_api_case_hits_prioritizes_all_owner_matches_over_single():
    records = [
        {
            "case_id": "ALL",
            "case_type": "Criminal Case",
            "case_status": "Pending",
            "parties_text": "Alice Patil and Bob Patil vs State",
            "court": "District Court Pune",
            "search_year": "2024",
        },
        {
            "case_id": "ONE",
            "case_type": "Regular Civil Appeal",
            "case_status": "Pending",
            "parties_text": "Alice Patil vs State",
            "court": "District Court Pune",
            "search_year": "2024",
        },
    ]
    out = rank_api_case_hits(
        records,
        owner_name="Alice Patil",
        owner_names=["Alice Patil", "Bob Patil"],
        igr_party_names=[],
        district_label="Pune",
        min_score=0.0,
    )
    assert len(out) == 2
    assert out[0].case_id == "ALL"
    assert "owner_match_scope=all" in (out[0].match_explanation or "")


def test_rank_api_case_hits_supports_partner_api_party_lists():
    records = [
        {
            "id": "MHPU130014502019",
            "cnr": "MHPU130014502019",
            "caseType": "CS",
            "caseStatus": "PENDING",
            "courtName": "CIVIL AND CRIMINAL COURT, SASWAD",
            "filingYear": 2019,
            "petitioners": ["Laxman Manohar Bhujbal", "Another Petitioner"],
            "respondents": ["State of Maharashtra"],
        }
    ]
    out = rank_api_case_hits(
        records,
        owner_name="Laxman Manohar Bhujbal",
        owner_names=["Laxman Manohar Bhujbal"],
        igr_party_names=[],
        district_label="Pune",
        min_score=0.0,
    )
    assert len(out) == 1
    assert out[0].cnr_number == "MHPU130014502019"
    assert out[0].case_id == "MHPU130014502019"
    assert out[0].search_year == "2019"
    assert "Laxman Manohar Bhujbal" in (out[0].parties_text or "")


def test_rank_api_case_hits_supports_nested_case_detail_payload():
    records = [
        {
            "id": "DLND020047882015",
            "data": {
                "courtCaseData": {
                    "cnr": "DLND020047882015",
                    "caseType": "CC",
                    "caseTypeRaw": "Ct Cases",
                    "caseStatus": "DISPOSED",
                    "courtName": "Chief Metropolitan Magistrate",
                    "courtNo": 2,
                    "district": "New Delhi",
                    "state": "DL",
                    "caseNumber": "202400248072016",
                    "cnrYear": "2015",
                    "filingNumber": "27843/2015",
                    "filingDate": "2015-12-21",
                    "registrationNumber": "24807/2016",
                    "registrationDate": "2015-12-21",
                    "firstHearingDate": "2016-01-05",
                    "nextHearingDate": "2018-07-07",
                    "decisionDate": "2018-07-07",
                    "petitioners": ["MR. ARUN JAITLEY"],
                    "respondents": ["MR. ARVIND KEJRIWAL"],
                    "caseCategoryFacetPath": "Criminal Law/Other Criminal Matters",
                }
            },
        }
    ]
    out = rank_api_case_hits(
        records,
        owner_name="Arun Jaitley",
        igr_party_names=[],
        district_label="New Delhi",
        min_score=0.0,
    )
    assert len(out) == 1
    assert out[0].cnr_number == "DLND020047882015"
    assert out[0].case_type == "CC"
    assert out[0].case_id == "202400248072016"
    assert "ARUN JAITLEY" in (out[0].parties_text or "").upper()


def test_dedupe_case_key_uses_lowercase_cnr_for_api_rows():
    a = {"cnr": "MHPU130014502019", "petitioners": ["A"]}
    b = {"cnr": "MHPU130014502019", "petitioners": ["B"]}
    assert dedupe_case_key(a) == dedupe_case_key(b)
