"""Background worker for the unified land-to-cases workflow."""

from __future__ import annotations

import asyncio
import csv
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select, delete

from api.database import AsyncSessionLocal
from api.land_case_flow import (
    build_name_variants,
    dedupe_case_key,
    extract_land_entity,
    extract_survey_option_labels,
    rank_api_case_hits,
    rank_case_hits,
    record_matches_owner_names_exact,
    write_html_artifact,
)
from api.models import (
    EcourtsApiCall,
    EcourtsApiCase,
    EcourtsRankCache,
    LandCaseWorkflow,
    LandEntity,
    NameVariant,
    WorkflowCaseHit,
)

logger = logging.getLogger(__name__)
STAGE_RETRY_ATTEMPTS = 3
STAGE_RETRY_DELAY_SECONDS = 2.0
CACHE_TTL_SECONDS = 15 * 60


class WorkflowCancelled(Exception):
    """Raised when a land-case workflow has been cancelled by the user."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _last_15_years() -> list[str]:
    import datetime as dt

    current_year = dt.datetime.now().year
    return [str(y) for y in range(current_year, current_year - 15, -1)]


def _igr_years_from_2002_to_current() -> list[str]:
    import datetime as dt

    current_year = dt.datetime.now().year
    return [str(y) for y in range(current_year, 2001, -1)]


def _normalize_survey_token(value: str) -> str:
    return (value or "").strip().lower()


_LATIN_TO_DEV_SUFFIX = {
    "a": "अ",
    "b": "ब",
    "c": "क",
    "d": "ड",
}
_DEV_TO_LATIN_SUFFIX = {v: k for k, v in _LATIN_TO_DEV_SUFFIX.items()}
_DEV_TO_ASCII_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")


def _suffix_variants(part: str) -> set[str]:
    """Latin ↔ Devanagari suffix equivalents (e.g. 6A ↔ 6अ)."""
    tok = (part or "").strip().lower()
    if not tok:
        return set()
    out = {tok}
    m_latin = re.fullmatch(r"(.+?)([a-z])", tok, flags=re.IGNORECASE)
    if m_latin:
        stem, suffix = m_latin.group(1), m_latin.group(2).lower()
        dev = _LATIN_TO_DEV_SUFFIX.get(suffix)
        if dev:
            out.add(f"{stem}{dev}")
    m_dev = re.fullmatch(r"(.+?)([\u0900-\u097f])", tok)
    if m_dev:
        stem, suffix = m_dev.group(1), m_dev.group(2)
        latin = _DEV_TO_LATIN_SUFFIX.get(suffix)
        if latin:
            out.add(f"{stem}{latin}")
    return out


def _survey_token_variants(value: str) -> set[str]:
    return _suffix_variants(_normalize_survey_token(value))


def _row_get_any(row: dict, keys: list[str]) -> str:
    for k in keys:
        v = row.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    low_map = {str(k).strip().lower(): v for k, v in row.items()}
    for k in keys:
        v = low_map.get(k.lower())
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _normalize_text(value: str) -> str:
    txt = (value or "").strip().lower().translate(_DEV_TO_ASCII_DIGITS)
    return " ".join(txt.split())


# Words that follow a plain number and indicate it is an AREA measurement,
# not a Gat/survey identifier.  Used to avoid false-positive survey matches.
_AREA_UNIT_TOKENS = frozenset([
    "चौरस", "चौ", "sq", "sqft", "sqm", "square", "feet", "foot",
    "फूट", "फुट", "मीटर", "meter", "metre", "हेक्टर", "hectare", "acre",
])

# Marathi / transliterated keywords for Gat/Survey-number labels in IGR descriptions.
# IGR uses many variations:
#   "गट नंबर 1530 हिस्सा नंबर 3"
#   "गट क्रमांक 1530 हिस्सा क्रमांक 3"
#   "ग.नं 970"                       (abbreviated Gat number — common in IGR lists)
#   "स.नं. 1530 हिस्सा 3"           (सर्वे नं prefix, no गट)
#   "सर्वे नं. 1530 भाग 3"           (भाग = part, alternative to हिस्सा)
_NUM_LABEL = r"(?:नंबर|नं\.?|क्रमांक|क्र\.?|क्रं\.?|no\.?|num\.?|number)?"
# Abbreviated Gat label used standalone (ग.नं / ग. नं. / ग.नं.)
_GAT_ABBREV_LABEL = r"(?:ग\.?\s*नं\.?)"
# Gat/Survey prefix — also matches abbreviated Gat (ग.नं) and सर्वे prefixes (स.नं.)
_GAT_LABEL = (
    r"(?:(?:गट|gut|gat)"
    r"|(?:ग\.?\s*नं\.?(?:\s+|\.))"        # ग.नं / ग. नं. / ग.नं.
    r"|(?:स\.?\s*नं\.?(?:\s+|\.))"        # स.नं. / स नं / स.नं
    r"|(?:सर्वे\s*(?:नं\.?|नंबर|क्र\.?|क्रमांक)?)"  # सर्वे नं / सर्वे नंबर
    r"|(?:survey\s*(?:no\.?|num\.?|number)?)"
    r")\s*" + _NUM_LABEL
)
# Hissa/sub-survey separator — हिस्सा, भाग (part), or plain English "hissa"
_HISSA_LABEL = r"(?:हिस्सा|हिस्से|hissa|हि\.?|ह\.?|भाग)\s*" + _NUM_LABEL

# False-positive contexts: document/deed numbers, not survey identifiers.
_DOC_REF_BEFORE = re.compile(
    r"(?:"
    r"करार(?:ानाम)?|अनुबंध|"
    r"document|doc(?:ument)?|deed|"
    r"registration|reg(?:istration)?|"
    r"दस्त(?:ावेज)?|"
    r"पूर्व(?:गाम)?(?:ी)?"
    r")\s*(?:क्र\.?|क्रमांक|no\.?|num(?:ber)?)?\s*\.?\s*$",
    re.IGNORECASE | re.UNICODE,
)
_KRAMANK_REF_BEFORE = re.compile(
    r"(?:क्र\.?|क्रमांक|doc\s*no\.?|reg\s*no\.?)\s*\.?\s*$",
    re.IGNORECASE | re.UNICODE,
)
_AREA_AFTER = re.compile(
    r"^\s*(?:"
    r"चौ\.?\s*(?:फु\.?|फुट|फु|मी\.?|मी)|"
    r"sq\.?\s*(?:ft|feet|m|meter|metre)|"
    r"square|feet|foot|"
    + "|".join(re.escape(u) for u in sorted(_AREA_UNIT_TOKENS, key=len, reverse=True))
    + r")",
    re.IGNORECASE | re.UNICODE,
)
_CALC_EQUALS_BEFORE = re.compile(r"=\s*$")
_CALC_EXPRESSION_BEFORE = re.compile(r"[\d.]+\s*[x×*]\s*[\d.]+\s*=\s*$", re.IGNORECASE)
_YEAR_SLASH_AFTER = re.compile(r"^\s*/\s*20\d{2}\b")
_CITY_SURVEY_LABEL_BEFORE = re.compile(
    r"(?:सि\.?\s*स\.?\s*नं\.?|city\s*survey\s*(?:no\.?|num(?:ber)?)?)\s*$",
    re.IGNORECASE | re.UNICODE,
)
_OLD_SURVEY_LABEL_BEFORE = re.compile(
    r"(?:जूना\s*सर्वे|old\s*survey)\s*(?:नं\.?|no\.?|num(?:ber)?)?\s*$",
    re.IGNORECASE | re.UNICODE,
)
_ENGLISH_SURVEY_LABEL_BEFORE = re.compile(
    r"(?:survey|gat|gut)\s*(?:no\.?|num(?:ber)?)?\s*$",
    re.IGNORECASE,
)


def _context_slice(hay: str, start: int, end: int, before: int = 45, after: int = 25) -> tuple[str, str]:
    return hay[max(0, start - before):start], hay[end:end + after]


def _gat_label_before(window: str) -> bool:
    if re.search(_GAT_LABEL + r"\s*$", window, re.IGNORECASE | re.UNICODE):
        return True
    # Glued form "ग.नं970" — abbrev immediately precedes digits with no trailing space.
    return bool(re.search(_GAT_ABBREV_LABEL + r"\s*$", window, re.IGNORECASE | re.UNICODE))


def _has_required_survey_label(hay: str, start: int) -> bool:
    """Bare numbers must appear next to an explicit survey/Gat/CS label."""
    before, _ = _context_slice(hay, start, start)
    return any(
        (
            _gat_label_before(before),
            _CITY_SURVEY_LABEL_BEFORE.search(before),
            _OLD_SURVEY_LABEL_BEFORE.search(before),
            _ENGLISH_SURVEY_LABEL_BEFORE.search(before),
        )
    )


def _accept_igr_survey_match(hay: str, start: int, end: int, survey_token: str) -> bool:
    """Reject area calculations, deed/agreement numbers, and unlabeled bare digits."""
    before, after = _context_slice(hay, start, end)

    if _CALC_EQUALS_BEFORE.search(before) or _CALC_EXPRESSION_BEFORE.search(before):
        return False
    if _AREA_AFTER.match(after):
        return False
    if _DOC_REF_BEFORE.search(before) or _KRAMANK_REF_BEFORE.search(before):
        return False
    if "/" not in survey_token and _YEAR_SLASH_AFTER.match(after):
        return False

    if "/" in survey_token:
        return True
    return _has_required_survey_label(hay, start)


def _contains_exact_survey_token(text: str, survey_token: str) -> bool:
    """
    Match a survey/Gat token inside an IGR Property Description.

    Handles slash notation, spaced slash, Marathi Gat+Hissa labels, and city/old
    survey labels. Rejects false positives such as:
      - Area calculations: "21.6 x 25.8 = 557 चौ.फुट"
      - Prior agreement refs: "पूर्वगामी करारानामा क्र.557/ 2019"
      - Bare numbers without a survey/Gat/CS label
    """
    hay = _normalize_text(text)
    toks = _survey_token_variants(survey_token)
    if not hay or not toks:
        return False

    def _match_accepts(pattern: re.Pattern[str]) -> bool:
        for m in pattern.finditer(hay):
            if _accept_igr_survey_match(hay, m.start(), m.end(), survey_token):
                return True
        return False

    # ── 1. Exact token (e.g. "1530/3" or labeled bare "सि.स.नं. 557") ───────
    for tok in toks:
        exact = re.compile(
            rf"(?<![0-9a-z\u0900-\u097f]){re.escape(tok)}(?![0-9a-z\u0900-\u097f])",
            re.IGNORECASE,
        )
        if _match_accepts(exact):
            return True

    # ── 1b. Abbreviated Gat label + bare number: "ग.नं 970" / "ग.नं970" ─────
    for tok in toks:
        gat_abbrev_num = re.compile(
            rf"{_GAT_ABBREV_LABEL}\s*\.?\s*{re.escape(tok)}(?![0-9a-z\u0900-\u097f])",
            re.IGNORECASE | re.UNICODE,
        )
        for m in gat_abbrev_num.finditer(hay):
            tok_start = m.end() - len(tok)
            if _accept_igr_survey_match(hay, tok_start, m.end(), survey_token):
                return True

    if "/" not in survey_token:
        return False

    base, _, hissa = survey_token.partition("/")
    base = base.strip()
    hissa = hissa.strip()
    if not base or not hissa:
        return False

    base_at = re.compile(rf"(?<![0-9]){re.escape(base)}")
    hissa_variants = _suffix_variants(hissa)

    def _match_accepts_with_base(pattern: re.Pattern[str]) -> bool:
        for m in pattern.finditer(hay):
            bm = base_at.search(hay, m.start(), m.end())
            if bm and _accept_igr_survey_match(hay, bm.start(), bm.end(), survey_token):
                return True
        return False

    # ── 2. Spaced slash: "1530 / 3" ──────────────────────────────────────────
    for hissa_tok in hissa_variants:
        spaced = re.compile(
            rf"(?<![0-9]){re.escape(base)}\s*/\s*{re.escape(hissa_tok)}(?![0-9a-z\u0900-\u097f])",
            re.IGNORECASE,
        )
        if _match_accepts_with_base(spaced):
            return True

    # ── 3–5. Gat label + BASE + Hissa label + HISSA ────────────────────────
    for hissa_tok in hissa_variants:
        gat_hissa = re.compile(
            rf"{_GAT_LABEL}\s*{re.escape(base)}\s*{_HISSA_LABEL}\s*{re.escape(hissa_tok)}(?![0-9a-z\u0900-\u097f])",
            re.IGNORECASE | re.UNICODE,
        )
        if _match_accepts_with_base(gat_hissa):
            return True

    # ── 6. Bare BASE + Hissa label + HISSA ───────────────────────────────────
    for hissa_tok in hissa_variants:
        bare_hissa = re.compile(
            rf"(?<![0-9]){re.escape(base)}\s+{_HISSA_LABEL}\s*{re.escape(hissa_tok)}(?![0-9a-z\u0900-\u097f])",
            re.IGNORECASE | re.UNICODE,
        )
        if _match_accepts_with_base(bare_hissa):
            return True

    # ── 7. Comma-separated: "204,हिस्सा नं. 6अ" (common IGR list notation) ───
    for hissa_tok in hissa_variants:
        comma_hissa = re.compile(
            rf"(?<![0-9]){re.escape(base)}\s*,\s*{_HISSA_LABEL}\s*{re.escape(hissa_tok)}(?![0-9a-z\u0900-\u097f])",
            re.IGNORECASE | re.UNICODE,
        )
        if _match_accepts_with_base(comma_hissa):
            return True

    return False


def _case_bool_pending(case_status: str) -> bool:
    status = (case_status or "").strip().lower()
    if not status:
        return False
    return "pending" in status and "disposed" not in status


def _to_text(value: Any) -> str | None:
    if value is None:
        return None
    txt = str(value).strip()
    return txt or None


def _to_text_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        txt = _to_text(item)
        if txt:
            out.append(txt)
    return out


def _first_text(*values: Any) -> str | None:
    for value in values:
        txt = _to_text(value)
        if txt:
            return txt
    return None


def _canonicalize_ecourts_case_record(row: dict) -> dict:
    data = row.get("data")
    detail = data.get("courtCaseData") if isinstance(data, dict) else None
    src = detail if isinstance(detail, dict) else row
    out = dict(row)
    out["cnr"] = _first_text(row.get("cnr"), row.get("cnr_number"), row.get("CNR_Number"), src.get("cnr"))
    out["caseType"] = _first_text(row.get("caseType"), row.get("case_type"), row.get("Case_Type"), src.get("caseType"))
    out["caseTypeRaw"] = _first_text(row.get("caseTypeRaw"), row.get("case_type_raw"), src.get("caseTypeRaw"))
    out["caseStatus"] = _first_text(
        row.get("caseStatus"), row.get("case_status"), row.get("Case_Status"), src.get("caseStatus")
    )
    out["courtName"] = _first_text(row.get("courtName"), row.get("court"), row.get("Court"), src.get("courtName"))
    out["courtNo"] = _first_text(row.get("courtNo"), row.get("court_no"), src.get("courtNo"))
    out["district"] = _first_text(row.get("district"), src.get("district"))
    out["state"] = _first_text(row.get("state"), src.get("state"))
    out["caseNumber"] = _first_text(row.get("caseNumber"), row.get("case_number"), src.get("caseNumber"))
    out["cnrYear"] = _first_text(row.get("cnrYear"), row.get("cnr_year"), src.get("cnrYear"))
    out["filingNumber"] = _first_text(row.get("filingNumber"), row.get("filing_number"), src.get("filingNumber"))
    out["filingDate"] = _first_text(row.get("filingDate"), row.get("filing_date"), src.get("filingDate"))
    out["registrationNumber"] = _first_text(
        row.get("registrationNumber"), row.get("registration_number"), src.get("registrationNumber")
    )
    out["registrationDate"] = _first_text(
        row.get("registrationDate"), row.get("registration_date"), src.get("registrationDate")
    )
    out["firstHearingDate"] = _first_text(
        row.get("firstHearingDate"), row.get("first_hearing_date"), src.get("firstHearingDate")
    )
    out["nextHearingDate"] = _first_text(
        row.get("nextHearingDate"), row.get("next_hearing_date"), src.get("nextHearingDate")
    )
    out["decisionDate"] = _first_text(row.get("decisionDate"), row.get("decision_date"), src.get("decisionDate"))
    out["caseCategoryFacetPath"] = _first_text(
        row.get("caseCategoryFacetPath"), row.get("case_category_facet_path"), src.get("caseCategoryFacetPath")
    )
    out["petitioners"] = _to_text_list(row.get("petitioners")) or _to_text_list(src.get("petitioners"))
    out["respondents"] = _to_text_list(row.get("respondents")) or _to_text_list(src.get("respondents"))
    out["petitionerAdvocates"] = _to_text_list(row.get("petitionerAdvocates")) or _to_text_list(
        src.get("petitionerAdvocates")
    )
    out["respondentAdvocates"] = _to_text_list(row.get("respondentAdvocates")) or _to_text_list(
        src.get("respondentAdvocates")
    )
    if not _to_text(out.get("parties_text")):
        out["parties_text"] = (
            f"{', '.join(out['petitioners'])} vs {', '.join(out['respondents'])}".strip()
            if (out["petitioners"] or out["respondents"])
            else None
        )
    if not _to_text(out.get("search_year")):
        out["search_year"] = _first_text(
            row.get("search_year"),
            row.get("Search_Year"),
            out.get("filingYear"),
            out.get("cnrYear"),
            src.get("filingYear"),
            src.get("cnrYear"),
        )
    return out


def _cache_key_parts(owner_name: str, district: str, taluka: str, village: str, survey: str) -> dict[str, str]:
    return {
        "owner_name_norm": _normalize_text(owner_name),
        "district_label": _normalize_text(district),
        "taluka_label": _normalize_text(taluka),
        "village_label": _normalize_text(village),
        "survey_token": _normalize_text(survey),
    }


def _igr_dedupe_key(row: dict) -> str:
    year = _normalize_text(str(row.get("search_year") or ""))
    survey = _normalize_survey_token(str(row.get("matched_target_survey") or row.get("survey_number") or ""))
    seller = _normalize_text(str(row.get("seller_name") or row.get("Seller Name") or ""))
    purchaser = _normalize_text(str(row.get("purchaser_name") or row.get("Purchaser Name") or ""))
    prop = _normalize_text(str(row.get("property_description") or row.get("Property Description") or ""))
    return "|".join([year, survey, seller, purchaser, prop])


def _split_owner_names(owner_name_input: str) -> list[str]:
    raw = (owner_name_input or "").strip()
    if not raw:
        return []
    parts = [p.strip() for p in re.split(r",|;|\||\band\b|&", raw, flags=re.I) if p.strip()]
    if not parts:
        return [raw]
    if len(parts) == 1:
        return parts
    return list(dict.fromkeys(parts))


# Words that indicate a Bhulekh field label or land description rather than a
# person/entity name.  Candidates containing any of these are skipped when
# building the eCourts search list.
_NON_NAME_TOKENS = frozenset([
    # Land/agriculture descriptors
    "irrigated", "non", "crop", "area", "unit", "details", "survey",
    "field", "total", "possession", "land", "record", "khata", "plot",
    "khasra", "gat", "block", "village", "taluka", "district", "boundary",
    # Compass directions (often appear as plot boundary labels)
    "east", "west", "north", "south",
    # Legal/administrative labels that appear verbatim in 7/12 other-occupant list
    "assessment", "purpose", "legal", "notforlegal", "notfor",
    "special", "nil", "vacant", "encumbrance", "revenue", "government",
    "owner", "occupant", "tenant", "holder", "patta",
])


def _is_plausible_ecourts_name(name: str) -> bool:
    """Return True only if *name* looks like a searchable person/entity name."""
    # Strip trailing '--' delimiter artifact common in Bhulekh/IGR data.
    cleaned = name.rstrip("-").strip()
    if not cleaned:
        return False
    words = cleaned.lower().split()
    # Too few words → likely a single token label, not a name.
    if len(words) < 2:
        return False
    # Too many words → likely a company description (e.g. 'माय लँन्ड … तर्फे भागीदार …').
    if len(words) > 6:
        return False
    # Contains land-record descriptor tokens → not a person name.
    if _NON_NAME_TOKENS.intersection(words):
        return False
    return True


# Mapping from Marathi IGR document-type labels to English equivalents.
MARATHI_DOC_TYPE_EN: dict[str, str] = {
    "खरेदीखत": "Sale deed",
    "गहाणखत": "Mortgage deed",
    "बक्षीसपत्र": "Gift deed",
    "मृत्युपत्र": "Will",
    "भाडेपट्टा": "Lease deed",
    "रिकन्व्हेन्स": "Reconveyance",
    "विक्री करारनामा": "Agreement to sell",
    "खरेदी करारनामा": "Sale agreement",
    "दान पत्र": "Donation deed",
    "हक्क सोड पत्र": "Release deed",
    "वाटणी पत्र": "Partition deed",
    "विमुक्ती": "Release",
    "प्रतिज्ञापत्र": "Affidavit",
    "बोजा पत्र": "Charge deed",
    "करारनामा": "Agreement",
    "36-अ-लिव्ह अॅड लायसन्सेस": "Leave & Licence",
    "लिव्ह अँड लायसन्स": "Leave & Licence",
    "लिव्ह अॅड लायसन्सेस": "Leave & Licence",
    "साठेखत": "Sale agreement",
}


def _igr_doc_type_en(marathi: str) -> str:
    """Return English label for a Marathi IGR DName, or the original if unknown."""
    m = (marathi or "").strip()
    return MARATHI_DOC_TYPE_EN.get(m, m)


def parse_igr_hit_raw(raw: dict) -> dict:
    """Extract timeline fields from one IGR hit raw_json dict."""
    doc_type_m = (raw.get("DName") or "").strip()
    sellers = _split_party_name_blob(raw.get("Seller Name") or "")
    buyers = _split_party_name_blob(raw.get("Purchaser Name") or "")
    return {
        "doc_no": (raw.get("DocNo") or "").strip(),
        "doc_type": _igr_doc_type_en(doc_type_m),
        "doc_type_marathi": doc_type_m,
        "reg_date": (raw.get("RDate") or "").strip(),
        "sro_name": (raw.get("SROName") or "").strip(),
        "seller": sellers[0] if sellers else "",
        "buyer": buyers[0] if buyers else "",
        "sellers": sellers,
        "buyers": buyers,
    }


def _split_party_name_blob(value: str) -> list[str]:
    """
    Split IGR party-name blobs (often wrapped with braces/quotes) into names.
    """
    raw = (value or "").strip()
    if not raw:
        return []
    cleaned = re.sub(r"[{}\[\]\"]+", " ", raw)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return _split_owner_names(cleaned)


def _to_json_text(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return json.dumps(value, ensure_ascii=False)
    except Exception:
        return None


def _request_param_values(request_params: Any, key: str) -> list[str]:
    out: list[str] = []
    if isinstance(request_params, list):
        for pair in request_params:
            if (
                isinstance(pair, (list, tuple))
                and len(pair) == 2
                and str(pair[0]).strip() == key
            ):
                val = str(pair[1]).strip()
                if val:
                    out.append(val)
    elif isinstance(request_params, dict):
        value = request_params.get(key)
        if isinstance(value, list):
            out.extend([str(v).strip() for v in value if str(v).strip()])
        elif value is not None and str(value).strip():
            out.append(str(value).strip())
    return out


def _is_valid_ecourts_api_key(value: str) -> bool:
    token = (value or "").strip()
    # Provider-issued keys observed so far are prefixed eci_*
    return bool(token) and token.startswith("eci_") and len(token) >= 16


def _write_ranked_hits_csv(workflow_id: str, hits: list[Any], artifacts_dir: Path) -> Path:
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    out = artifacts_dir / f"{workflow_id}_ranked_hits.csv"
    fieldnames = [
        "final_rank",
        "primary_name_matched",
        "search_year",
        "case_id",
        "cnr_number",
        "case_type",
        "court",
        "parties_text",
        "name_match_score",
        "matched_variant",
        "match_explanation",
    ]
    with out.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for idx, hit in enumerate(hits, start=1):
            writer.writerow(
                {
                    "final_rank": idx,
                    "primary_name_matched": getattr(hit, "primary_name_matched", ""),
                    "search_year": hit.search_year,
                    "case_id": hit.case_id,
                    "cnr_number": hit.cnr_number,
                    "case_type": hit.case_type,
                    "court": hit.court,
                    "parties_text": hit.parties_text,
                    "name_match_score": hit.name_match_score,
                    "matched_variant": hit.matched_variant,
                    "match_explanation": hit.match_explanation,
                }
            )
    return out


def _write_unranked_csv(
    workflow_id: str,
    records: list[dict],
    bhulekh_names: list[str],
    igr_names: list[str],
    artifacts_dir: Path,
) -> Path:
    """Write raw normalized API records before any ranking or filtering."""
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    out = artifacts_dir / f"{workflow_id}_unranked_raw.csv"
    fieldnames = [
        "row_index",
        "bhulekh_names_searched",
        "igr_names_searched",
        "cnr",
        "caseType",
        "caseStatus",
        "filingNumber",
        "filingDate",
        "courtName",
        "petitioners",
        "respondents",
        "nextHearingDate",
        "decisionDate",
        "stateCode",
        "districtCode",
    ]
    bhulekh_str = " | ".join(bhulekh_names)
    igr_str = " | ".join(igr_names)
    with out.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for idx, rec in enumerate(records, start=1):
            canon = _canonicalize_ecourts_case_record(rec)
            writer.writerow(
                {
                    "row_index": idx,
                    "bhulekh_names_searched": bhulekh_str,
                    "igr_names_searched": igr_str,
                    "cnr": canon.get("cnr") or canon.get("cnr_number") or canon.get("CNR_Number"),
                    "caseType": canon.get("caseType"),
                    "caseStatus": canon.get("caseStatus"),
                    "filingNumber": canon.get("filingNumber"),
                    "filingDate": canon.get("filingDate"),
                    "courtName": canon.get("courtName"),
                    "petitioners": "; ".join(canon.get("petitioners") or []),
                    "respondents": "; ".join(canon.get("respondents") or []),
                    "nextHearingDate": canon.get("nextHearingDate"),
                    "decisionDate": canon.get("decisionDate"),
                    "stateCode": canon.get("stateCode"),
                    "districtCode": canon.get("districtCode"),
                }
            )
    return out


def _extract_igr_party_row_for_target_survey(row: dict, target_survey: str) -> dict | None:
    """
    Keep only IGR rows whose Property Description matches the full target survey
    (e.g. ``204/6A``), not merely the base Gat number (``204``).

    IGR portal search is intentionally broad (base number only); this filter
    narrows results to the exact sub-survey the user selected.
    """
    target = _normalize_survey_token(target_survey)
    if not target:
        return None
    prop_desc = _row_get_any(row, ["Property Description", "PropertyDescription", "property description"])
    if not prop_desc:
        return None
    if not _contains_exact_survey_token(prop_desc, target):
        return None
    seller = _row_get_any(row, ["Seller Name", "SellerName", "seller name"])
    purchaser = _row_get_any(row, ["Purchaser Name", "PurchaserName", "purchaser name"])
    if not seller and not purchaser:
        return None
    out = dict(row)
    out["target_survey"] = target_survey
    out["matched_target_survey"] = target_survey
    out["seller_name"] = seller
    out["purchaser_name"] = purchaser
    out["property_description"] = prop_desc
    return out


async def run_land_case_workflow(workflow_id: str) -> None:
    from bhulekh_scraper import BhulekhScraper
    from igr_freesearch_scraper import IGRFreeSearchScraper
    from scraper import HybridECourtsScraper
    from api.models import WorkflowIgrHit

    logger.info("[workflow:%s] Land workflow started.", workflow_id)
    bhulekh = BhulekhScraper(headless=True)
    igr_scrapers: list[IGRFreeSearchScraper] = []
    ecourts = HybridECourtsScraper(headless=True)

    async def _update_workflow(**kwargs: Any) -> LandCaseWorkflow | None:
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(LandCaseWorkflow).where(LandCaseWorkflow.id == workflow_id))
            wf_row = result.scalar_one_or_none()
            if wf_row is None:
                logger.error("[workflow:%s] Workflow row not found while updating state.", workflow_id)
                return None
            for k, v in kwargs.items():
                setattr(wf_row, k, v)
            await db.commit()
            return wf_row

    async def _fail_workflow(stage: str, exc: Exception) -> None:
        msg = f"{stage} failed: {exc}"
        logger.exception("[workflow:%s] %s", workflow_id, msg)
        await _update_workflow(
            status="failed",
            error_message=msg,
            progress_message=f"Workflow failed during {stage}.",
            finished_at=_now(),
        )

    async def _ensure_not_cancelled(stage: str = "") -> None:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(LandCaseWorkflow.status).where(LandCaseWorkflow.id == workflow_id)
            )
            status = result.scalar_one_or_none()
        if status == "cancelled":
            raise WorkflowCancelled(stage or "workflow")

    async def _run_with_retries(
        stage: str,
        operation: str,
        op_factory: Any,
        attempts: int = STAGE_RETRY_ATTEMPTS,
        delay_seconds: float = STAGE_RETRY_DELAY_SECONDS,
    ) -> Any:
        last_exc: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                logger.info(
                    "[workflow:%s] %s attempt %s/%s started.",
                    workflow_id,
                    operation,
                    attempt,
                    attempts,
                )
                result = await op_factory()
                logger.info(
                    "[workflow:%s] %s attempt %s/%s succeeded.",
                    workflow_id,
                    operation,
                    attempt,
                    attempts,
                )
                return result
            except Exception as exc:  # pragma: no cover - branch is exercised by integration retries
                last_exc = exc
                logger.warning(
                    "[workflow:%s] %s attempt %s/%s failed: %s",
                    workflow_id,
                    operation,
                    attempt,
                    attempts,
                    exc,
                )
                if attempt < attempts:
                    logger.info(
                        "[workflow:%s] Retrying %s after %.1fs delay.",
                        workflow_id,
                        operation,
                        delay_seconds,
                    )
                    await asyncio.sleep(delay_seconds)
        raise RuntimeError(
            f"{operation} failed after {attempts} attempts: {last_exc}"
        ) from last_exc

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(LandCaseWorkflow).where(LandCaseWorkflow.id == workflow_id))
        wf = result.scalar_one_or_none()
        if wf is None:
            logger.error("[workflow:%s] Land workflow not found.", workflow_id)
            return
        wf.status = "bhulekh_running"
        wf.progress_message = "Running Bhulekh search..."
        wf.started_at = _now()
        await db.commit()
        logger.info(
            "[workflow:%s] Input accepted: district=%r taluka=%r village=%r survey=%r/%r",
            workflow_id,
            wf.district_label,
            wf.taluka_label,
            wf.village_label,
            wf.survey_part1,
            wf.survey_option_label,
        )

    try:
        await _ensure_not_cancelled("startup")
        logger.info("[workflow:%s] Stage bhulekh_running started.", workflow_id)
        try:
            await _run_with_retries(
                stage="bhulekh_running",
                operation="Bhulekh browser setup",
                op_factory=bhulekh.setup_driver,
            )
            html = await _run_with_retries(
                stage="bhulekh_running",
                operation="Bhulekh search submission",
                op_factory=lambda: bhulekh.run_search_with_labels(
                    district_label=wf.district_label,
                    taluka_label=wf.taluka_label,
                    village_label=wf.village_label,
                    survey_part1=wf.survey_part1,
                    survey_option_label=wf.survey_option_label,
                ),
            )
        except WorkflowCancelled:
            raise
        except Exception as exc:
            await _fail_workflow("bhulekh_running", exc)
            return

        logger.info("[workflow:%s] Bhulekh submit succeeded (html_len=%s).", workflow_id, len(html))
        artifacts_dir = Path("artifacts/workflows")
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        try:
            pdf_path = await bhulekh.save_verification_pdf(artifacts_dir / f"{workflow_id}_land_record.pdf")
        except WorkflowCancelled:
            raise
        except Exception as exc:
            await _fail_workflow("pdf_export", exc)
            return
        html_path = write_html_artifact(artifacts_dir, workflow_id, html)
        survey_options = extract_survey_option_labels(html, wf.survey_part1)
        survey_options_path = artifacts_dir / f"{workflow_id}_survey_options.json"
        survey_options_path.write_text(
            json.dumps(
                {
                    "survey_part1": wf.survey_part1,
                    "selected_survey_option_label": wf.survey_option_label,
                    "survey_options": survey_options,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        logger.info(
            "[workflow:%s] Artifacts saved: pdf=%s html=%s survey_options=%s",
            workflow_id,
            pdf_path,
            html_path,
            survey_options_path,
        )

        entity = extract_land_entity(html=html, pdf_path=str(pdf_path))
        variants = build_name_variants(entity.occupant_primary_name or "")
        logger.info(
            "[workflow:%s] Extraction complete: primary_name=%r candidates=%s mutation_count=%s confidence=%.2f source=%s",
            workflow_id,
            entity.occupant_primary_name,
            len(entity.occupant_candidates),
            len(entity.mutation_numbers),
            entity.extraction_confidence,
            entity.source,
        )
        logger.info("[workflow:%s] Name variants generated: %s", workflow_id, len(variants))

        async with AsyncSessionLocal() as db:
            result = await db.execute(select(LandCaseWorkflow).where(LandCaseWorkflow.id == workflow_id))
            wf = result.scalar_one()

            wf.status = "name_variants_ready"
            wf.progress_message = "Extracted occupant/mutation and generated name variants."
            wf.occupant_primary_name = entity.occupant_primary_name
            wf.mutation_numbers_json = json.dumps(entity.mutation_numbers, ensure_ascii=False)
            wf.extraction_confidence = entity.extraction_confidence
            wf.variant_count = len(variants)
            wf.pdf_path = str(pdf_path)
            wf.html_path = str(html_path)

            db.add(
                LandEntity(
                    workflow_id=workflow_id,
                    occupant_primary_name=entity.occupant_primary_name,
                    occupant_candidates_json=json.dumps(entity.occupant_candidates, ensure_ascii=False),
                    mutation_numbers_json=json.dumps(entity.mutation_numbers, ensure_ascii=False),
                    extraction_confidence=entity.extraction_confidence,
                    source=entity.source,
                )
            )
            for v in variants:
                db.add(
                    NameVariant(
                        workflow_id=workflow_id,
                        base_name=entity.occupant_primary_name or "",
                        variant_text=v.variant_text,
                        variant_kind=v.variant_kind,
                        quality_score=v.quality_score,
                    )
                )
            await db.commit()

        if not variants:
            logger.warning(
                "[workflow:%s] Name variants are empty; API mode will still use exact owner name.",
                workflow_id,
            )

        sibling_surveys = [
            s for s in survey_options if s and s.strip() and s.strip() != (wf.survey_option_label or "").strip()
        ]
        target_survey_option = (wf.survey_option_label or "").strip()
        await _ensure_not_cancelled("before_igr")
        logger.info(
            "[workflow:%s] Stage igr_running started (target_survey=%r sibling_surveys=%s).",
            workflow_id,
            target_survey_option,
            sibling_surveys,
        )
        # IGR portal search uses the plain base number (e.g. "1530"), never "1530/3" with a
        # slash — the portal accepts only the Gat base.  It returns all documents whose
        # property descriptions mention that base survey in any format.  We then apply
        # _extract_igr_party_row_for_target_survey which uses _contains_exact_survey_token
        # to filter for the exact sub-survey (e.g. "1530/3") across all text variations:
        # slash notation, spaced-slash, Marathi Gat+Hissa label, English Gut+Hissa label, etc.
        base_survey = (wf.survey_part1 or "").strip()
        igr_search_survey = base_survey
        igr_years = _igr_years_from_2002_to_current()
        igr_year_total = len(igr_years)
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(LandCaseWorkflow).where(LandCaseWorkflow.id == workflow_id))
            wf_igr = result.scalar_one()
            wf_igr.status = "igr_running"
            wf_igr.years_total = max(1, igr_year_total)
            wf_igr.years_done = 0
            wf_igr.progress_message = (
                f"Searching land transaction records ({igr_year_total} registration years to check)…"
            )
            await db.commit()

        parallel_contexts = max(1, min(2, int(os.getenv("IGR_PARALLEL_CONTEXTS", "1"))))
        # Seconds to delay slice2's browser launch relative to slice1.  Staggering
        # avoids hammering the IGR server with two simultaneous page.goto calls,
        # which increases the chance of both timing out.
        igr_slice_stagger = float(os.getenv("IGR_SLICE_STAGGER_SECONDS", "10"))
        completed_years = 0
        progress_lock = asyncio.Lock()

        async def _run_igr_year_loop(
            igr: IGRFreeSearchScraper,
            year_slice: list[str],
            slice_id: str,
        ) -> list[dict]:
            """Run the per-year search loop on an already-setup IGR scraper."""
            nonlocal completed_years
            local_matches: list[dict] = []
            for year in year_slice:
                await _ensure_not_cancelled(f"igr_running year={year}")
                try:
                    recs = await _run_with_retries(
                        stage="igr_running",
                        operation=f"IGR search year={year} survey={igr_search_survey} slice={slice_id}",
                        op_factory=lambda year=year: igr.search_rest_maharashtra(
                            district_label=wf.district_label,
                            taluka_label=wf.taluka_label,
                            village_label=wf.village_label or "Wagholi",
                            survey_number=igr_search_survey,
                            year=year,
                        ),
                    )
                except Exception as exc:
                    raise RuntimeError(
                        f"survey={igr_search_survey!r} year={year!r} search failed (slice={slice_id}): {exc}"
                    ) from exc

                matched = [
                    r2
                    for r in recs
                    if (r2 := _extract_igr_party_row_for_target_survey(r, target_survey_option)) is not None
                ]
                # Log raw property descriptions when rows come back for the
                # base survey but none match the target — helps diagnose
                # format differences in the IGR portal data.
                if recs and not matched:
                    for r in recs[:3]:
                        prop = _row_get_any(r, ["Property Description", "PropertyDescription", "property description"])
                        if prop:
                            logger.warning(
                                "[workflow:%s] IGR row not matched target=%r year=%s prop_desc=%r",
                                workflow_id,
                                target_survey_option,
                                year,
                                prop[:200],
                            )
                if matched:
                    logger.info(
                        "[workflow:%s] IGR target matches: year=%s base=%r matched_rows=%s target=%r slice=%s",
                        workflow_id,
                        year,
                        base_survey,
                        len(matched),
                        target_survey_option,
                        slice_id,
                    )
                elif not recs:
                    logger.info(
                        "[workflow:%s] IGR year=%s returned no portal rows (empty registration year).",
                        workflow_id,
                        year,
                    )
                local_matches.extend(matched)

                async with progress_lock:
                    completed_years += 1
                    current_done = completed_years
                await _update_workflow(
                    progress_message=(
                        f"Searching land transaction records — year {year} "
                        f"({current_done} of {igr_year_total})…"
                    ),
                    years_done=current_done,
                )
            return local_matches

        async def _setup_igr_scraper(slice_id: str, shared_browser=None) -> IGRFreeSearchScraper:
            igr = IGRFreeSearchScraper(headless=True, shared_browser=shared_browser)
            igr_scrapers.append(igr)
            await _run_with_retries(
                stage="igr_bootstrap",
                operation=f"IGR browser setup slice={slice_id}",
                op_factory=igr.setup_driver,
            )
            return igr

        try:
            if parallel_contexts == 1 or len(igr_years) < 2:
                # Single context: one browser, one tab, all years in sequence.
                igr1 = await _setup_igr_scraper("slice1")
                slice_results = list(await asyncio.gather(_run_igr_year_loop(igr1, igr_years, "slice1")))
            else:
                # Two contexts on ONE shared browser process:
                #   Phase 1 — launch the browser via slice1 (full ~30s startup).
                #   Phase 2 — open a second context on the same process (< 1s).
                #   Phase 3 — run both year loops concurrently.
                # No stagger needed; browser launch happens only once.
                mid = (len(igr_years) + 1) // 2
                igr1 = await _setup_igr_scraper("slice1")
                igr2 = await _setup_igr_scraper("slice2", shared_browser=igr1.browser)
                logger.info(
                    "[workflow:%s] IGR: 1 browser, 2 contexts — slice1=%d years, slice2=%d years.",
                    workflow_id,
                    mid,
                    len(igr_years) - mid,
                )
                results = await asyncio.gather(
                    _run_igr_year_loop(igr1, igr_years[:mid], "slice1"),
                    _run_igr_year_loop(igr2, igr_years[mid:], "slice2"),
                )
                slice_results = list(results)
        except WorkflowCancelled:
            raise
        except Exception as exc:
            await _fail_workflow("igr_running", exc)
            return

        igr_collected_raw = [item for sublist in slice_results for item in sublist]
        igr_seen: set[str] = set()
        igr_collected: list[dict] = []
        for rec in igr_collected_raw:
            key = _igr_dedupe_key(rec)
            if key in igr_seen:
                continue
            igr_seen.add(key)
            igr_collected.append(rec)
        async with AsyncSessionLocal() as db:
            for rec in igr_collected:
                db.add(
                    WorkflowIgrHit(
                        workflow_id=workflow_id,
                        survey_number=rec.get("survey_number") or base_survey,
                        search_year=rec.get("search_year") or "",
                        district_label=rec.get("district_label"),
                        taluka_label=rec.get("taluka_label"),
                        village_label=rec.get("village_label") or wf.village_label or "Wagholi",
                        source_region="rest_of_maharashtra",
                        raw_json=json.dumps(rec, ensure_ascii=False),
                    )
                )
            await db.commit()
        logger.info(
            "[workflow:%s] IGR stage completed: sibling_surveys=%s records=%s",
            workflow_id,
            len(sibling_surveys),
            len(igr_collected),
        )

        logger.info("[workflow:%s] Stage ecourts_running started.", workflow_id)
        await _ensure_not_cancelled("before_ecourts")
        owner_name_input = (wf.owner_name_input or "").strip()
        owner_names_for_api = _split_owner_names(owner_name_input)
        owner_source = "input"
        if not owner_names_for_api:
            # Collect ALL 7/12 occupant names: primary + every plausible candidate.
            # A single survey number can have multiple co-owners (see 7/12 extract)
            # and searching for all of them maximises eCourts hit recall.
            bhulekh_names: list[str] = []
            if entity.occupant_primary_name:
                primary = entity.occupant_primary_name.strip()
                if _is_plausible_ecourts_name(primary):
                    bhulekh_names.append(primary)
            for n in (entity.occupant_candidates or []):
                if isinstance(n, str) and n.strip() and _is_plausible_ecourts_name(n):
                    candidate = n.strip()
                    if candidate not in bhulekh_names:
                        bhulekh_names.append(candidate)
            owner_names_for_api = bhulekh_names
            owner_source = "bhulekh_all"

        # Preserve the 7/12 Bhulekh names before IGR names are appended so
        # the ranker can prioritise 7/12 matches over IGR purchaser matches.
        bhulekh_owner_names: list[str] = list(owner_names_for_api)

        igr_purchaser_names: list[str] = []
        for row in igr_collected:
            for key in ("purchaser_name", "Purchaser Name", "PurchaserName"):
                value = row.get(key)
                if isinstance(value, str) and value.strip():
                    igr_purchaser_names.extend(_split_party_name_blob(value))

        # Do not use IGR names for eCourts search input. Keep them only for
        # diagnostics/output artifacts so API queries remain Bhulekh-driven.

        owner_names_for_api = list(dict.fromkeys(owner_names_for_api))
        owner_name = owner_names_for_api[0] if owner_names_for_api else ""
        if not owner_names_for_api:
            await _fail_workflow(
                "ecourts_running",
                RuntimeError("Owner name not available from Bhulekh extraction."),
            )
            return
        logger.info(
            "[workflow:%s] Owner selection for eCourts: source=%s owner_count=%s owners=%s",
            workflow_id,
            owner_source,
            len(owner_names_for_api),
            owner_names_for_api,
        )

        igr_party_names: list[str] = []
        for row in igr_collected:
            for key in ("seller_name", "purchaser_name", "Seller Name", "Purchaser Name"):
                value = row.get(key)
                if isinstance(value, str) and value.strip():
                    igr_party_names.append(value.strip())

        dedupe: set[str] = set()
        collected: list[dict] = []
        api_metrics: dict[str, Any] | None = None
        api_key = os.getenv("ECOURTS_API_KEY", "").strip()
        allow_scraper_fallback = os.getenv("ECOURTS_ALLOW_SCRAPER_FALLBACK", "").strip().lower() in {
            "1",
            "true",
            "yes",
        }
        cache_hit = False
        cached_age_seconds: int | None = None
        expected_requests_if_miss = 0
        expected_cost_if_miss = 0.0
        cache_key = _cache_key_parts(
            owner_name="|".join(sorted(_normalize_text(x) for x in owner_names_for_api)),
            district=wf.district_label,
            taluka=wf.taluka_label,
            village=wf.village_label,
            survey=wf.survey_option_label,
        )

        async with AsyncSessionLocal() as db:
            result = await db.execute(select(LandCaseWorkflow).where(LandCaseWorkflow.id == workflow_id))
            wf = result.scalar_one()
            wf.status = "ecourts_running"
            wf.progress_message = "Searching eCourts via API..."
            # Keep years_total / years_done from the IGR stage — they reflect registration
            # years searched (2002→current), not eCourts API batch progress.
            await db.commit()

        if not _is_valid_ecourts_api_key(api_key):
            msg = (
                "ECOURTS_API_KEY is missing or invalid. Configure a valid eCourts API key "
                "(expected prefix 'eci_')."
            )
            if not allow_scraper_fallback:
                await _fail_workflow("ecourts_running", RuntimeError(msg))
                return
            logger.warning(
                "[workflow:%s] %s Fallback to scraper is enabled via ECOURTS_ALLOW_SCRAPER_FALLBACK.",
                workflow_id,
                msg,
            )

        if _is_valid_ecourts_api_key(api_key):
            from api.ecourts_api_client import EcourtsApiClient

            async with AsyncSessionLocal() as db:
                await db.execute(
                    delete(EcourtsRankCache).where(EcourtsRankCache.expires_at < _now())
                )
                await db.commit()
                cached_result = await db.execute(
                    select(EcourtsRankCache).where(
                        EcourtsRankCache.owner_name_norm == cache_key["owner_name_norm"],
                        EcourtsRankCache.district_label == cache_key["district_label"],
                        EcourtsRankCache.taluka_label == cache_key["taluka_label"],
                        EcourtsRankCache.village_label == cache_key["village_label"],
                        EcourtsRankCache.survey_token == cache_key["survey_token"],
                        EcourtsRankCache.source_mode == "api",
                        EcourtsRankCache.expires_at >= _now(),
                    )
                )
                cache_row = cached_result.scalar_one_or_none()
                if cache_row is not None:
                    cache_hit = True
                    created_at = cache_row.created_at
                    if created_at is not None and created_at.tzinfo is None:
                        created_at = created_at.replace(tzinfo=timezone.utc)
                    if created_at is not None:
                        cached_age_seconds = int((_now() - created_at).total_seconds())
                    try:
                        collected = json.loads(cache_row.cached_ranked_json or "[]")
                    except Exception as exc:
                        logger.warning(
                            "[workflow:%s] Failed to parse cached ranked JSON; continuing with empty cache payload: %s",
                            workflow_id,
                            type(exc).__name__,
                        )
                        collected = []
                    detail_limit = int(os.getenv("ECOURTS_API_DETAIL_LIMIT", "20"))
                    rows_with_cnr = sum(
                        1
                        for row in collected
                        if str(
                            row.get("cnr") or row.get("cnr_number") or row.get("CNR_Number") or ""
                        ).strip()
                    )
                    expected_detail_calls = min(detail_limit, rows_with_cnr)
                    expected_requests_if_miss = 1 + expected_detail_calls
                    expected_cost_if_miss = round(0.20 + 0.50 * expected_detail_calls, 2)
            logger.info(
                "[workflow:%s] eCourts cache lookup: hit=%s key=%s cached_rows=%s",
                workflow_id,
                cache_hit,
                cache_key,
                len(collected),
            )

            api_client = EcourtsApiClient(api_key=api_key)
            try:
                if not cache_hit:
                    # Fire all per-owner searches concurrently — each is an
                    # independent HTTP request so there is no ordering dependency.
                    async def _search_owner(oq: str) -> list[dict]:
                        return await _run_with_retries(
                            stage="ecourts_running",
                            operation=f"eCourts API case search owner={oq!r}",
                            op_factory=lambda oq=oq: api_client.search_cases(
                                owner_name=oq,
                                district=wf.district_label,
                                taluka=wf.taluka_label,
                                village=wf.village_label,
                                survey_number=wf.survey_part1,
                            ),
                        )

                    per_owner_results = await asyncio.gather(
                        *[_search_owner(oq) for oq in owner_names_for_api],
                        return_exceptions=True,
                    )
                    search_rows: list[dict] = []
                    for result in per_owner_results:
                        if isinstance(result, Exception):
                            logger.warning(
                                "[workflow:%s] One owner search failed (skipped): %s",
                                workflow_id,
                                result,
                            )
                        else:
                            search_rows.extend(result)

                    # Write unranked CSV immediately after search so raw
                    # normalized API data is always available for debugging.
                    try:
                        _write_unranked_csv(
                            workflow_id,
                            search_rows,
                            bhulekh_names=bhulekh_owner_names,
                            igr_names=igr_purchaser_names,
                            artifacts_dir=Path("artifacts/workflows"),
                        )
                    except Exception as _exc:
                        logger.warning(
                            "[workflow:%s] Unranked CSV export failed: %s",
                            workflow_id,
                            type(_exc).__name__,
                        )

                    detail_limit = int(os.getenv("ECOURTS_API_DETAIL_LIMIT", "20"))
                    detail_concurrency = max(1, int(os.getenv("ECOURTS_API_DETAIL_CONCURRENCY", "3")))
                    sem = asyncio.Semaphore(detail_concurrency)
                    rows_with_cnr = sum(
                        1
                        for row in search_rows
                        if str(
                            row.get("cnr") or row.get("cnr_number") or row.get("CNR_Number") or ""
                        ).strip()
                    )
                    expected_detail_calls = min(detail_limit, rows_with_cnr)
                    expected_requests_if_miss = len(owner_names_for_api) + expected_detail_calls
                    expected_cost_if_miss = round(0.20 * len(owner_names_for_api) + 0.50 * expected_detail_calls, 2)
                    skipped_detail_count = max(0, len(search_rows) - detail_limit)
                    logger.info(
                        "[workflow:%s] eCourts API search rows=%s detail_limit=%s detail_concurrency=%s skipped_detail_rows=%s",
                        workflow_id,
                        len(search_rows),
                        detail_limit,
                        detail_concurrency,
                        skipped_detail_count,
                    )

                    async def _fetch_detail_row(in_row: dict, idx: int) -> dict:
                        cnr = str(
                            in_row.get("cnr")
                            or in_row.get("cnr_number")
                            or in_row.get("CNR_Number")
                            or ""
                        ).strip()
                        merged = _canonicalize_ecourts_case_record(in_row)
                        if not cnr or idx >= detail_limit:
                            return merged
                        async with sem:
                            detail = await _run_with_retries(
                                stage="ecourts_running",
                                operation=f"eCourts API case detail {cnr}",
                                op_factory=lambda cnr=cnr: api_client.get_case_detail(cnr),
                            )
                        if isinstance(detail, dict):
                            merged.update(_canonicalize_ecourts_case_record(detail))
                        return _canonicalize_ecourts_case_record(merged)

                    enriched = await asyncio.gather(
                        *[_fetch_detail_row(row, idx) for idx, row in enumerate(search_rows)]
                    )
                    for merged in enriched:
                        key = dedupe_case_key(merged)
                        if key in dedupe:
                            continue
                        dedupe.add(key)
                        collected.append(merged)

                    async with AsyncSessionLocal() as db:
                        request_log = getattr(api_client.metrics, "request_log", []) or []
                        for req in request_log:
                            req_params = req.get("request_params")
                            litigants_values = _request_param_values(req_params, "litigants")
                            search_filters = {
                                "caseStatuses": _request_param_values(req_params, "caseStatuses"),
                                "judicialSections": _request_param_values(req_params, "judicialSections"),
                                "caseTypes": _request_param_values(req_params, "caseTypes"),
                            }
                            db.add(
                                EcourtsApiCall(
                                    workflow_id=workflow_id,
                                    owner_name_query=" | ".join(owner_names_for_api),
                                    request_kind=req.get("kind") or "unknown",
                                    endpoint=req.get("endpoint") or "",
                                    method=req.get("method")
                                    or (
                                        "GET"
                                        if (req.get("kind") or "").endswith("_get")
                                        else "POST"
                                    ),
                                    litigants_query=", ".join(litigants_values) or None,
                                    search_filters_json=_to_json_text(search_filters),
                                    request_params_json=_to_json_text(req_params),
                                    response_status=req.get("status_code"),
                                    response_json=_to_json_text(req.get("response_json")),
                                    provider_error_code=req.get("provider_code"),
                                    retryable=req.get("retryable"),
                                    is_success=(
                                        isinstance(req.get("status_code"), int)
                                        and 200 <= req.get("status_code") < 300
                                    ),
                                )
                            )
                        for row in collected:
                            canonical = _canonicalize_ecourts_case_record(row)
                            case_type = _first_text(
                                canonical.get("caseType"), canonical.get("case_type"), canonical.get("Case_Type")
                            )
                            case_type_raw = _first_text(canonical.get("caseTypeRaw"), canonical.get("case_type_raw"))
                            case_status = _first_text(
                                canonical.get("caseStatus"), canonical.get("case_status"), canonical.get("Case_Status")
                            ) or ""
                            civil_type_text = " ".join(
                                [x for x in [case_type or "", case_type_raw or ""] if x]
                            ).lower()
                            db.add(
                                EcourtsApiCase(
                                    workflow_id=workflow_id,
                                    cnr_number=_first_text(
                                        canonical.get("cnr"),
                                        canonical.get("cnr_number"),
                                        canonical.get("CNR_Number"),
                                    ),
                                    case_type=case_type,
                                    case_type_raw=case_type_raw,
                                    court=_first_text(canonical.get("courtName"), canonical.get("court"), canonical.get("Court")),
                                    court_no=_first_text(canonical.get("courtNo"), canonical.get("court_no")),
                                    district=_first_text(canonical.get("district")),
                                    state=_first_text(canonical.get("state")),
                                    case_number=_first_text(canonical.get("caseNumber"), canonical.get("case_number")),
                                    cnr_year=_first_text(canonical.get("cnrYear"), canonical.get("cnr_year")),
                                    filing_number=_first_text(canonical.get("filingNumber"), canonical.get("filing_number")),
                                    filing_date=_first_text(canonical.get("filingDate"), canonical.get("filing_date")),
                                    registration_number=_first_text(
                                        canonical.get("registrationNumber"), canonical.get("registration_number")
                                    ),
                                    registration_date=_first_text(
                                        canonical.get("registrationDate"), canonical.get("registration_date")
                                    ),
                                    first_hearing_date=_first_text(
                                        canonical.get("firstHearingDate"), canonical.get("first_hearing_date")
                                    ),
                                    next_hearing_date=_first_text(
                                        canonical.get("nextHearingDate"), canonical.get("next_hearing_date")
                                    ),
                                    decision_date=_first_text(canonical.get("decisionDate"), canonical.get("decision_date")),
                                    petitioners_json=_to_json_text(canonical.get("petitioners") or []),
                                    respondents_json=_to_json_text(canonical.get("respondents") or []),
                                    petitioner_advocates_json=_to_json_text(canonical.get("petitionerAdvocates") or []),
                                    respondent_advocates_json=_to_json_text(canonical.get("respondentAdvocates") or []),
                                    case_category_facet_path=_first_text(
                                        canonical.get("caseCategoryFacetPath"), canonical.get("case_category_facet_path")
                                    ),
                                    parties_text=_first_text(
                                        canonical.get("parties_text"),
                                        canonical.get("Petitioner Name versus Respondent Name"),
                                    ),
                                    case_status=case_status or None,
                                    is_civil=bool("civil" in civil_type_text),
                                    is_pending=_case_bool_pending(case_status),
                                    final_rank=None,
                                    source_stage="detail",
                                    raw_json=json.dumps(canonical, ensure_ascii=False),
                                )
                            )
                        await db.commit()
                    logger.info(
                        "[workflow:%s] Persisted eCourts API records: calls=%s cases=%s",
                        workflow_id,
                        len(request_log),
                        len(collected),
                    )

                    async with AsyncSessionLocal() as db:
                        db.add(
                            EcourtsRankCache(
                                owner_name_norm=cache_key["owner_name_norm"],
                                district_label=cache_key["district_label"],
                                taluka_label=cache_key["taluka_label"],
                                village_label=cache_key["village_label"],
                                survey_token=cache_key["survey_token"],
                                source_mode="api",
                                cached_ranked_json=json.dumps(collected, ensure_ascii=False),
                                expires_at=_now() + timedelta(seconds=CACHE_TTL_SECONDS),
                            )
                        )
                        await db.commit()
                    logger.info(
                        "[workflow:%s] eCourts rank cache updated: ttl_seconds=%s stored_rows=%s",
                        workflow_id,
                        CACHE_TTL_SECONDS,
                        len(collected),
                    )
                else:
                    logger.info(
                        "[workflow:%s] eCourts API bypassed due to cache hit.",
                        workflow_id,
                    )
                api_metrics = {
                    "provider": "ecourts_api",
                    "owner_name_query": " | ".join(owner_names_for_api),
                    "owner_names_used": owner_names_for_api,
                    "owner_source": owner_source,
                    "cache_hit": cache_hit,
                    "cache_key": cache_key,
                    "cached_age_seconds": cached_age_seconds,
                    "api_requests_saved": expected_requests_if_miss if cache_hit else 0,
                    "estimated_cost_saved_inr": expected_cost_if_miss if cache_hit else 0.0,
                    **api_client.metrics.__dict__,
                }
                logger.info(
                    "[workflow:%s] eCourts API metrics prepared: cache_hit=%s total_requests=%s estimated_cost_inr=%s",
                    workflow_id,
                    cache_hit,
                    api_metrics.get("total_requests"),
                    api_metrics.get("estimated_cost_inr"),
                )
            except WorkflowCancelled:
                raise
            except Exception as exc:
                await _fail_workflow("ecourts_running", exc)
                await api_client.close()
                return
            finally:
                await api_client.close()
        else:
            logger.warning(
                "[workflow:%s] Using scraper fallback for eCourts stage (ECOURTS_ALLOW_SCRAPER_FALLBACK enabled).",
                workflow_id,
            )
            try:
                await _run_with_retries(
                    stage="ecourts_bootstrap",
                    operation="eCourts browser setup",
                    op_factory=ecourts.setup_driver,
                )
                await _run_with_retries(
                    stage="ecourts_bootstrap",
                    operation="eCourts navigation and court selection",
                    op_factory=ecourts.navigate_and_select,
                )
                for year in _last_15_years():
                    records = await _run_with_retries(
                        stage="ecourts_running",
                        operation=f"eCourts scraper search year={year} owner={owner_name!r}",
                        op_factory=lambda year=year: ecourts.search_petitioner(owner_name, year),
                    )
                    for rec in records:
                        rec["Search_Year"] = year
                        key = dedupe_case_key(rec)
                        if key in dedupe:
                            continue
                        dedupe.add(key)
                        collected.append(rec)
            except WorkflowCancelled:
                raise
            except Exception as exc:
                await _fail_workflow("ecourts_running", exc)
                return

        # API-first ranking: exact owner query + IGR party overlap.
        owner_match_pool = bhulekh_owner_names or owner_names_for_api
        before_owner_filter = len(collected)
        collected = [
            row
            for row in collected
            if record_matches_owner_names_exact(
                _canonicalize_ecourts_case_record(row),
                owner_match_pool,
            )
        ]
        logger.info(
            "[workflow:%s] eCourts owner exact-match filter: kept=%s dropped=%s owners=%s",
            workflow_id,
            len(collected),
            before_owner_filter - len(collected),
            owner_match_pool,
        )
        ranked = rank_api_case_hits(
            collected,
            owner_name=owner_name,
            owner_names=owner_names_for_api,
            primary_owner_names=bhulekh_owner_names,
            igr_party_names=igr_party_names,
            district_label=wf.district_label,
            taluka_label=wf.taluka_label,
            village_label=wf.village_label,
            min_score=0.0,
        )
        ranked_csv_path: Path | None = None
        try:
            ranked_csv_path = _write_ranked_hits_csv(workflow_id, ranked, Path("artifacts/workflows"))
        except Exception as exc:
            logger.warning(
                "[workflow:%s] Ranked CSV export failed; continuing without CSV artifact: %s",
                workflow_id,
                type(exc).__name__,
            )
        logger.info(
            "[workflow:%s] Ranking complete: collected=%s ranked_hits=%s csv=%s",
            workflow_id,
            len(collected),
            len(ranked),
            str(ranked_csv_path) if ranked_csv_path else None,
        )
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(LandCaseWorkflow).where(LandCaseWorkflow.id == workflow_id))
            wf = result.scalar_one()
            for idx, hit in enumerate(ranked, start=1):
                db.add(
                    WorkflowCaseHit(
                        workflow_id=workflow_id,
                        search_year=hit.search_year,
                        case_id=hit.case_id,
                        cnr_number=hit.cnr_number,
                        case_type=hit.case_type,
                        court=hit.court,
                        parties_text=hit.parties_text,
                        matched_variant=hit.matched_variant,
                        match_explanation=hit.match_explanation,
                        name_match_score=hit.name_match_score,
                        is_civil=hit.is_civil,
                        final_rank=idx,
                        raw_json=hit.raw_json,
                    )
                )
            # Persist final rerank position back to normalized API-case table for user output.
            rank_by_cnr = {str(h.cnr_number).strip(): idx for idx, h in enumerate(ranked, start=1) if h.cnr_number}
            if rank_by_cnr:
                api_case_rows = (
                    await db.execute(select(EcourtsApiCase).where(EcourtsApiCase.workflow_id == workflow_id))
                ).scalars().all()
                for c in api_case_rows:
                    cnr = (c.cnr_number or "").strip()
                    c.final_rank = rank_by_cnr.get(cnr)
            wf.total_hits = len(ranked)
            wf.status = "ranked_done"
            wf.progress_message = f"Completed. Ranked {len(ranked)} cases by relevance."
            wf.ecourts_api_metrics_json = json.dumps(api_metrics, ensure_ascii=False) if api_metrics else None
            wf.finished_at = _now()
            await db.commit()
        logger.info("[workflow:%s] Workflow completed successfully.", workflow_id)

    except WorkflowCancelled:
        logger.info("[workflow:%s] Workflow cancelled by user.", workflow_id)
    except Exception as exc:
        await _fail_workflow("workflow_unhandled", exc)
    finally:
        try:
            await bhulekh.close()
        except Exception:
            logger.exception("[workflow:%s] Failed to close Bhulekh scraper.", workflow_id)
        for igr in igr_scrapers:
            try:
                await igr.close()
            except Exception:
                logger.exception("[workflow:%s] Failed to close IGR scraper.", workflow_id)
        try:
            await ecourts.close()
        except Exception:
            logger.exception("[workflow:%s] Failed to close eCourts scraper.", workflow_id)
