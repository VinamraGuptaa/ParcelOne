"""Core land-to-cases pipeline helpers (extraction, variants, matching, ranking)."""

from __future__ import annotations

import difflib
import json
import logging
import re
import base64
import tempfile
from dataclasses import dataclass
from pathlib import Path

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

HONORIFICS = {
    "mr",
    "mrs",
    "ms",
    "dr",
    "shri",
    "smt",
    "late",
}

REPLACEMENTS = {
    "ph": "f",
    "oo": "u",
    "ee": "i",
    "aa": "a",
    "bh": "b",
}

LOCATION_ALIASES = {
    "satara": ("सातारा",),
    "सातारा": ("satara",),
    "koregaon": ("कोरेगाव",),
    "कोरेगाव": ("koregaon",),
    "pune": ("पुणे",),
    "पुणे": ("pune",),
    "haveli": ("हवेली",),
    "हवेली": ("haveli",),
    "shirur": ("शिरूर", "ghodnadi"),
    "शिरूर": ("shirur", "ghodnadi"),
    "ghodnadi": ("shirur", "शिरूर"),
    "talegaon dhamdhere": ("तालेगाव धामधरे",),
    "तालेगाव धामधरे": ("talegaon dhamdhere",),
}


@dataclass
class ExtractedLandEntity:
    occupant_primary_name: str | None
    occupant_candidates: list[str]
    mutation_numbers: list[str]
    extraction_confidence: float
    source: str


@dataclass
class GeneratedVariant:
    variant_text: str
    variant_kind: str
    quality_score: float


@dataclass
class RankedCaseHit:
    case_id: str | None
    cnr_number: str | None
    search_year: str | None
    case_type: str | None
    court: str | None
    parties_text: str | None
    is_civil: bool
    name_match_score: float
    owner_match_count: int
    owner_total: int
    matched_variant: str | None
    match_explanation: str
    raw_json: str
    primary_name_matched: bool = False
    # Fraction of names on the matched owners' party side that are searched owners.
    # 1.0 = all names on that side are owners (pure individual / group case).
    # < 1.0 = strangers are mixed in on the same side.
    owner_side_purity: float = 0.0
    village_location_score: float = 0.0
    taluka_location_score: float = 0.0
    district_location_score: float = 0.0
    is_pending: bool = False


def _normalize_name(name: str) -> str:
    txt = (name or "").lower()
    # Keep unicode letters/digits (including Marathi) for location matching.
    txt = re.sub(r"[^\w\s]", " ", txt, flags=re.UNICODE)
    txt = txt.replace("_", " ")
    tokens = [t for t in txt.split() if t and t not in HONORIFICS]
    return " ".join(tokens)


def _owner_side_purity(rec: dict, variants: list[str]) -> float:
    """
    Fraction of names on the *best* party side (petitioners or respondents) that
    are searched owner names.

    Examples
    --------
    Single owner "Lata Arun Narke", petitioners=["Lata Arun Narke"]:
        → 1/1 = 1.0  (pure individual case — ranks highest)

    Single owner "Lata Arun Narke", petitioners=["Lata Arun Narke", "Ram Singh"]:
        → 1/2 = 0.5  (stranger mixed in — ranks below pure case)

    5 owners all in petitioners, no strangers:
        → 5/5 = 1.0

    3 of 5 owners in petitioners alongside 2 strangers:
        → 3/5 = 0.6
    """
    petitioners = [str(p).strip() for p in (rec.get("petitioners") or []) if str(p).strip()]
    respondents = [str(r).strip() for r in (rec.get("respondents") or []) if str(r).strip()]
    best = 0.0
    for side in (petitioners, respondents):
        if not side:
            continue
        matched_on_side = sum(
            1 for name in side
            if any(_names_exact_equivalent(name, v) for v in variants)
        )
        if matched_on_side > 0:
            best = max(best, matched_on_side / len(side))
    return round(best, 4)


def owner_name_exact_in_parties(parties_text: str, owner: str) -> bool:
    """
    True iff the normalized owner name appears in party text as an exact match:
    - Multi-word owners: token sequence must match a consecutive run in party
      tokens (so "lata arun narke" matches that order, but "lata arun nark" does
      not match inside "lata arun narke" as a substring typo/prefix).
    - Single-token owners: must match a whole token in the party string (so "a"
      does not match inside "alice").

    Prefer record_matches_owner_names_exact() for eCourts ranking — it requires
    a full-name match on an individual petitioner/respondent entry.
    """
    hay = _normalize_name(parties_text)
    vn = _normalize_name(owner)
    if not hay or not vn:
        return False
    hay_toks = hay.split()
    vn_toks = vn.split()
    if not vn_toks:
        return False
    if len(vn_toks) == 1:
        return vn_toks[0] in hay_toks
    n = len(vn_toks)
    for i in range(len(hay_toks) - n + 1):
        if hay_toks[i : i + n] == vn_toks:
            return True
    return False


def _names_exact_equivalent(left: str, right: str) -> bool:
    """True when two party/owner names are the same person (spacing/case tolerant)."""
    left_norm = _normalize_name(left)
    right_norm = _normalize_name(right)
    if not left_norm or not right_norm:
        return False
    if left_norm == right_norm:
        return True
    if left_norm.replace(" ", "") == right_norm.replace(" ", ""):
        return True
    left_toks = left_norm.split()
    right_toks = right_norm.split()
    return len(left_toks) == len(right_toks) and left_toks == right_toks


def _split_party_side(side: str) -> list[str]:
    txt = (side or "").strip()
    if not txt or txt.lower() in {"nil", "na", "n/a", "-", "none"}:
        return []
    parts = [p.strip() for p in re.split(r",|;|\band\b|&", txt, flags=re.I) if p.strip()]
    return parts or [txt]


def _extract_individual_party_names(rec: dict) -> list[str]:
    """Return one entry per petitioner/respondent party (not combined blob)."""
    names: list[str] = []
    for key in ("petitioners", "respondents"):
        value = rec.get(key)
        if not isinstance(value, list):
            continue
        for entry in value:
            names.extend(_split_party_side(str(entry)))
    if names:
        return list(dict.fromkeys(n for n in names if n))

    parties = (
        rec.get("parties_text")
        or rec.get("parties")
        or rec.get("Petitioner Name versus Respondent Name")
        or ""
    )
    if isinstance(parties, str) and parties.strip():
        if re.search(r"\s+vs\.?\s+", parties, flags=re.I):
            lhs, rhs = re.split(r"\s+vs\.?\s+", parties, maxsplit=1, flags=re.I)
            names.extend(_split_party_side(lhs))
            names.extend(_split_party_side(rhs))
        else:
            names.extend(_split_party_side(parties))
    return list(dict.fromkeys(n for n in names if n))


def record_matches_owner_names_exact(rec: dict, owner_names: list[str]) -> bool:
    """True when any 7/12 owner name exactly matches an individual party entry."""
    party_names = _extract_individual_party_names(rec)
    owners = [o.strip() for o in owner_names if isinstance(o, str) and o.strip()]
    if not party_names or not owners:
        return False
    return any(
        _names_exact_equivalent(owner, party)
        for owner in owners
        for party in party_names
    )


def score_owner_variants_exact_phrase(
    parties_or_rec: str | dict, variants: list[str]
) -> tuple[float, str | None, str]:
    """Best score across owner variants using exact full-name party matching only."""
    if isinstance(parties_or_rec, dict):
        party_names = _extract_individual_party_names(parties_or_rec)
    else:
        party_names = _extract_individual_party_names({"parties_text": parties_or_rec})
    for variant in variants:
        if not isinstance(variant, str) or not variant.strip():
            continue
        for party in party_names:
            if _names_exact_equivalent(variant, party):
                return 1.0, variant, "exact_party"
    return 0.0, None, "no_exact_match"


def _mutation_tokens_from_text(text: str) -> list[str]:
    # Keeps common integer/parenthesized formats seen in the report table.
    found = re.findall(r"\(?\d{2,6}\)?", text or "")
    out: list[str] = []
    seen: set[str] = set()
    for tok in found:
        t = tok.strip()
        if t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out[:10]


def _pick_primary_name(candidates: list[str]) -> str | None:
    if not candidates:
        return None
    banned = (
        "name of",
        "rights",
        "area under crop",
        "uncultivable",
        "notforlegal",
        "view only",
        "record",
        "detail",
        "website content",
        "ministry",
    )
    best_name = None
    best_score = float("-inf")
    for cand in candidates:
        c = re.sub(r"\s+", " ", (cand or "").strip())
        low = c.lower()
        score = 0.0
        if any(b in low for b in banned):
            score -= 100
        tokens = c.split()
        if 2 <= len(tokens) <= 4:
            score += 8
        if re.fullmatch(r"[A-Za-z][A-Za-z\s]{3,60}", c):
            score += 4
        if all(t.isalpha() for t in tokens):
            score += 3
        score -= abs(len(tokens) - 3) * 1.5
        if score > best_score:
            best_score = score
            best_name = c
    return best_name


def _parse_land_record_text(text: str) -> tuple[list[str], list[str]]:
    """
    Parse OCR/text payload from the Bhulekh land-record panel image.
    Returns (occupant_candidates, mutation_numbers).
    """
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    candidates: list[str] = []
    priority_mutations: list[str] = []
    fallback_text_for_mutation = "\n".join(lines)

    noise_fragments = (
        "view only",
        "not for legal purpose",
        "ministry of electronics",
        "national informatics",
        "website content managed",
        "disclaimer",
    )

    for idx, line in enumerate(lines):
        normalized_line = (
            line.replace("（", "(")
            .replace("）", ")")
            .replace("—", "-")
            .replace("–", "-")
        )
        low = normalized_line.lower()
        if any(n in low for n in noise_fragments):
            continue

        # Common OCR layout: account no + name on one line, mutation appears 2-5 lines later.
        if re.fullmatch(r"[a-z][a-z\s]{4,60}", low) and len(low.split()) >= 2:
            if not any(k in low for k in ("name of", "other rights", "detailsof", "uncultivable")):
                if low not in candidates:
                    candidates.append(low)
                near = " ".join(
                    lines[max(0, idx - 2) : min(len(lines), idx + 6)]
                ).replace("（", "(").replace("）", ")")
                mnear = re.search(r"\(\d{3,6}\)", near)
                if mnear:
                    mt = mnear.group(0)
                    if mt not in priority_mutations:
                        priority_mutations.append(mt)

        # strongest signal: occupant row often has account no + name + numeric columns + (mutation)
        row_match = re.search(
            r"\b\d{3,6}\b\s+([a-z][a-z\s]{3,60}?)\s+\d+\.\d+\.\d+.*?(\(?\d{3,6}\)?)",
            low,
        )
        if row_match:
            name = re.sub(r"\s+", " ", row_match.group(1)).strip()
            mut = row_match.group(2).strip()
            if name and name not in candidates:
                candidates.append(name)
            if mut and mut not in priority_mutations:
                priority_mutations.append(mut)
            continue

        # alternative OCR layout: name with mutation in parentheses later in line
        alt = re.search(r"([a-z][a-z\s]{4,60})\s+.*?(\(\d{3,6}\))", low)
        if alt:
            nm = re.sub(r"\s+", " ", alt.group(1)).strip()
            mt = alt.group(2).strip()
            if nm and nm not in candidates and not any(k in nm for k in ("name of", "other rights")):
                candidates.append(nm)
            if mt and mt not in priority_mutations:
                priority_mutations.append(mt)
            continue

        # fallback: line immediately after the "Name of the occupant" header
        if "name of the occupant" in low:
            win = " ".join(lines[idx : idx + 3])
            m = re.search(r"([A-Za-z][A-Za-z\s]{4,60})", win)
            if m:
                nm = re.sub(r"\s+", " ", m.group(1)).strip()
                nm_low = nm.lower()
                if not any(k in nm_low for k in ("name of the occupant", "assessment", "mutation")):
                    if nm not in candidates:
                        candidates.append(nm)
            muts = _mutation_tokens_from_text(win)
            for mt in muts:
                if mt not in priority_mutations:
                    priority_mutations.append(mt)

    # fallback if parser didn't hit row format
    if not candidates:
        candidates = _best_name_candidates_from_text(text)
    mutations: list[str] = []
    for mt in priority_mutations + _mutation_tokens_from_text(fallback_text_for_mutation):
        if mt not in mutations:
            mutations.append(mt)
    return candidates[:6], mutations[:10]


def _extract_from_imgpc_data_url(html: str) -> tuple[list[str], list[str]] | None:
    """Decode #ContentPlaceHolder1_ImgPC data URL and OCR it."""
    m = re.search(
        r'id="ContentPlaceHolder1_ImgPC"[^>]*src="([^"]+)"',
        html or "",
        flags=re.I | re.S,
    )
    if not m:
        return None
    src = m.group(1).strip()
    if not src.startswith("data:"):
        return None
    dm = re.match(r"^data:[^;]+;base64,(.*)$", src, flags=re.S)
    if not dm:
        return None
    try:
        raw = base64.b64decode(dm.group(1).replace("\n", ""))
    except Exception:
        return None

    tf = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    tf.close()
    p = Path(tf.name)
    try:
        p.write_bytes(raw)
        from rapidocr_onnxruntime import RapidOCR

        engine = RapidOCR()
        result, _ = engine(str(p))
        text = "\n".join(item[1] for item in (result or []))
        if not text.strip():
            return None
        return _parse_land_record_text(text)
    except Exception as exc:
        logger.info("ImgPC OCR extraction failed: %s", type(exc).__name__)
        return None
    finally:
        try:
            p.unlink(missing_ok=True)
        except Exception:
            pass


def _best_name_candidates_from_text(text: str) -> list[str]:
    candidates: list[str] = []
    # Fallback: lines with 2-5 alphabetic tokens
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if re.fullmatch(r"[A-Za-z][A-Za-z\s]{3,60}", line):
            low = line.lower()
            if any(
                marker in low
                for marker in ("name of the occupant", "mutation", "assessment", "area unit")
            ):
                continue
            tokens = [x for x in line.split() if x]
            if 2 <= len(tokens) <= 5:
                candidates.append(" ".join(tokens))
    # prefer unique stable order
    uniq: list[str] = []
    seen: set[str] = set()
    for c in candidates:
        key = c.lower()
        if key in seen:
            continue
        seen.add(key)
        uniq.append(c)
    return uniq[:8]


def extract_land_entity(html: str, pdf_path: str | None = None) -> ExtractedLandEntity:
    """
    Extract occupant/mutation details from Bhulekh response.

    Primary path uses submit HTML (stable and available now). Optional PDF text extraction
    is attempted when pypdf is available in the runtime.
    """
    soup = BeautifulSoup(html or "", "html.parser")
    text = soup.get_text("\n", strip=True)
    candidates: list[str] = []
    mutations: list[str] = []
    source = "html"

    # Preferred path for Bhulekh: OCR the embedded result image from submit HTML.
    imgpc = _extract_from_imgpc_data_url(html or "")
    if imgpc:
        candidates, mutations = imgpc
        source = "imgpc_ocr"
    else:
        candidates = _best_name_candidates_from_text(text)
        mutations = _mutation_tokens_from_text(text)

    if not candidates and pdf_path:
        try:
            from pypdf import PdfReader  # type: ignore

            ptext = "\n".join((p.extract_text() or "") for p in PdfReader(pdf_path).pages)
            p_candidates, p_mutations = _parse_land_record_text(ptext)
            candidates = p_candidates or _best_name_candidates_from_text(ptext)
            mutations = p_mutations or _mutation_tokens_from_text(ptext) or mutations
            source = "pdf"
        except Exception as exc:  # optional dependency / parse failure
            logger.info("PDF text extraction unavailable or failed: %s", type(exc).__name__)

    primary = _pick_primary_name(candidates)
    confidence = 0.0
    if primary:
        confidence += 0.6
    if mutations:
        confidence += 0.3
    if len(candidates) > 1:
        confidence += 0.1
    confidence = min(confidence, 1.0)

    entity = ExtractedLandEntity(
        occupant_primary_name=primary,
        occupant_candidates=candidates,
        mutation_numbers=mutations,
        extraction_confidence=confidence,
        source=source,
    )
    logger.info(
        "Land entity extracted: source=%s primary_name=%r candidates=%s mutation_count=%s confidence=%.2f",
        entity.source,
        entity.occupant_primary_name,
        len(entity.occupant_candidates),
        len(entity.mutation_numbers),
        entity.extraction_confidence,
    )
    return entity


def build_name_variants(base_name: str, max_variants: int = 12) -> list[GeneratedVariant]:
    norm = _normalize_name(base_name)
    if not norm:
        return []
    tokens = [t for t in norm.split() if t]
    variants: list[GeneratedVariant] = [
        GeneratedVariant(variant_text=norm, variant_kind="normalized", quality_score=1.0)
    ]

    if len(tokens) > 1:
        variants.append(
            GeneratedVariant(
                variant_text=" ".join(reversed(tokens)),
                variant_kind="token_reorder",
                quality_score=0.85,
            )
        )

    for src, dst in REPLACEMENTS.items():
        if src in norm:
            variants.append(
                GeneratedVariant(
                    variant_text=norm.replace(src, dst),
                    variant_kind=f"replacement:{src}>{dst}",
                    quality_score=0.75,
                )
            )

    # Initials + surname style: "r g" / "r gupta"
    if len(tokens) >= 2:
        initials = " ".join(t[0] for t in tokens[:-1] if t)
        variants.append(
            GeneratedVariant(
                variant_text=f"{initials} {tokens[-1]}".strip(),
                variant_kind="initials_plus_surname",
                quality_score=0.65,
            )
        )

    uniq: list[GeneratedVariant] = []
    seen: set[str] = set()
    for v in variants:
        key = v.variant_text.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        uniq.append(v)
        if len(uniq) >= max_variants:
            break
    logger.info(
        "Name variants generated: base_name=%r count=%s max=%s",
        base_name,
        len(uniq),
        max_variants,
    )
    return uniq


def is_civil_case(case_type: str | None) -> bool:
    txt = (case_type or "").lower()
    return "civil" in txt or "regular civil" in txt or "r.c.a" in txt


def is_pending_case(case_status: str | None) -> bool:
    txt = (case_status or "").strip().lower()
    if not txt:
        return False
    if any(word in txt for word in ("disposed", "closed", "dismissed", "withdrawn", "decided")):
        return False
    return "pending" in txt or txt in ("active", "running")


def _party_overlap_score(parties_text: str, party_names: list[str]) -> float:
    hay = _normalize_name(parties_text or "")
    if not hay:
        return 0.0
    hay_tokens = set(hay.split())
    best = 0.0
    for name in party_names:
        nn = _normalize_name(name or "")
        if not nn:
            continue
        name_tokens = set(nn.split())
        if not name_tokens:
            continue
        overlap = len(hay_tokens & name_tokens) / len(name_tokens)
        if overlap > best:
            best = overlap
    return round(best, 4)


def _district_court_overlap_score(court_text: str, district_label: str) -> float:
    district_norm = _normalize_name(district_label or "")
    court_norm = _normalize_name(court_text or "")
    if not district_norm or not court_norm:
        return 0.0
    district_tokens = set(district_norm.split())
    if not district_tokens:
        return 0.0
    court_tokens = set(court_norm.split())
    overlap = len(district_tokens & court_tokens) / len(district_tokens)
    return round(min(overlap, 1.0), 4)


def _location_label_variants(label: str) -> list[str]:
    """Build location strings to match inside court names (Latin + Devanagari)."""
    variants: list[str] = []
    raw = (label or "").strip().lower()
    if raw:
        variants.append(raw)
    norm = _normalize_name(label or "")
    if norm and norm not in variants:
        variants.append(norm)
    for v in list(variants):
        compact = v.replace(" ", "")
        if compact and compact not in variants:
            variants.append(compact)
    for key in {(label or "").strip().lower(), norm}:
        if not key:
            continue
        for alias in LOCATION_ALIASES.get(key, ()):
            alias_raw = alias.strip()
            if not alias_raw:
                continue
            alias_lower = alias_raw.lower()
            if alias_lower not in variants:
                variants.append(alias_lower)
            alias_compact = alias_lower.replace(" ", "")
            if alias_compact and alias_compact not in variants:
                variants.append(alias_compact)
    return list(dict.fromkeys(v for v in variants if v))


def _location_token_overlap(court_text: str, label: str) -> float:
    court_norm = _normalize_name(court_text or "")
    if not court_norm:
        return 0.0
    court_compact = court_norm.replace(" ", "")
    variants = _location_label_variants(label)
    if not variants:
        return 0.0

    for variant in variants:
        variant_norm = _normalize_name(variant)
        variant_compact = variant_norm.replace(" ", "") if variant_norm else variant.replace(" ", "")
        if variant_norm and (variant_norm in court_norm or variant_compact in court_compact):
            return 1.0
        if variant in court_norm or variant.replace(" ", "") in court_compact:
            return 1.0

    # Multi-word Latin labels (e.g. "talegaon dhamdhere"): require all words in court.
    latin_tokens = {
        t
        for t in _normalize_name(label or "").split()
        if t and re.search(r"[a-z]", t, re.I)
    }
    if not latin_tokens:
        return 0.0
    court_tokens = set(court_norm.split())
    return len(latin_tokens & court_tokens) / len(latin_tokens)


def _court_location_tier_scores(
    court_text: str,
    *,
    district_label: str = "",
    taluka_label: str = "",
    village_label: str = "",
) -> tuple[float, float, float]:
    """Return (village, taluka, district) court-name overlap scores in [0, 1]."""
    return (
        round(_location_token_overlap(court_text, village_label), 4),
        round(_location_token_overlap(court_text, taluka_label), 4),
        round(_location_token_overlap(court_text, district_label), 4),
    )


def _court_location_overlap_score(
    court_text: str,
    *,
    district_label: str = "",
    taluka_label: str = "",
    village_label: str = "",
) -> float:
    """
    Court location relevance signal.
    Prioritizes village, then taluka, then district overlap with the court name.
    """
    village_score, taluka_score, district_score = _court_location_tier_scores(
        court_text,
        district_label=district_label,
        taluka_label=taluka_label,
        village_label=village_label,
    )
    if not any((village_score, taluka_score, district_score)):
        return 0.0
    # Village is most specific; district-only matches (e.g. "Pune" in a Pune bench)
    # should not outrank a taluka-local court (e.g. Shirur) for a Shirur parcel.
    score = village_score * 0.50 + taluka_score * 0.35 + district_score * 0.15
    return round(min(score, 1.0), 4)


def rank_api_case_hits(
    records: list[dict],
    *,
    owner_name: str,
    owner_names: list[str] | None = None,
    primary_owner_names: list[str] | None = None,
    igr_party_names: list[str],
    district_label: str = "",
    taluka_label: str = "",
    village_label: str = "",
    min_score: float = 0.10,
) -> list[RankedCaseHit]:
    def _first_non_empty(rec: dict, keys: tuple[str, ...]) -> str | None:
        for key in keys:
            value = rec.get(key)
            if value is None:
                continue
            txt = str(value).strip()
            if txt:
                return txt
        return None

    def _list_text(rec: dict, key: str) -> list[str]:
        value = rec.get(key)
        if not isinstance(value, list):
            return []
        out: list[str] = []
        for item in value:
            txt = str(item).strip()
            if txt:
                out.append(txt)
        return out

    def _canonicalize_case_record(rec: dict) -> dict:
        # Case detail payloads are often nested at data.courtCaseData.
        detail = rec.get("data")
        court_case_data = detail.get("courtCaseData") if isinstance(detail, dict) else None
        src = court_case_data if isinstance(court_case_data, dict) else rec
        out = dict(rec)
        out["cnr"] = _first_non_empty(out, ("cnr", "cnr_number", "CNR_Number")) or _first_non_empty(src, ("cnr",))
        out["caseType"] = _first_non_empty(out, ("caseType", "case_type", "Case_Type")) or _first_non_empty(
            src, ("caseType",)
        )
        out["caseTypeRaw"] = _first_non_empty(out, ("caseTypeRaw", "case_type_raw")) or _first_non_empty(
            src, ("caseTypeRaw",)
        )
        out["caseStatus"] = _first_non_empty(out, ("caseStatus", "case_status", "Case_Status")) or _first_non_empty(
            src, ("caseStatus",)
        )
        out["courtName"] = _first_non_empty(out, ("courtName", "court", "Court")) or _first_non_empty(
            src, ("courtName",)
        )
        out["courtNo"] = _first_non_empty(out, ("courtNo", "court_no")) or _first_non_empty(src, ("courtNo",))
        out["district"] = _first_non_empty(out, ("district",)) or _first_non_empty(src, ("district",))
        out["state"] = _first_non_empty(out, ("state",)) or _first_non_empty(src, ("state",))
        out["caseNumber"] = _first_non_empty(out, ("caseNumber", "case_number")) or _first_non_empty(
            src, ("caseNumber",)
        )
        out["cnrYear"] = _first_non_empty(out, ("cnrYear", "cnr_year")) or _first_non_empty(src, ("cnrYear",))
        out["filingNumber"] = _first_non_empty(out, ("filingNumber", "filing_number")) or _first_non_empty(
            src, ("filingNumber",)
        )
        out["filingDate"] = _first_non_empty(out, ("filingDate", "filing_date")) or _first_non_empty(
            src, ("filingDate",)
        )
        out["registrationNumber"] = _first_non_empty(
            out, ("registrationNumber", "registration_number")
        ) or _first_non_empty(src, ("registrationNumber",))
        out["registrationDate"] = _first_non_empty(
            out, ("registrationDate", "registration_date")
        ) or _first_non_empty(src, ("registrationDate",))
        out["firstHearingDate"] = _first_non_empty(
            out, ("firstHearingDate", "first_hearing_date")
        ) or _first_non_empty(src, ("firstHearingDate",))
        out["nextHearingDate"] = _first_non_empty(
            out, ("nextHearingDate", "next_hearing_date")
        ) or _first_non_empty(src, ("nextHearingDate",))
        out["decisionDate"] = _first_non_empty(out, ("decisionDate", "decision_date")) or _first_non_empty(
            src, ("decisionDate",)
        )
        out["caseCategoryFacetPath"] = _first_non_empty(
            out, ("caseCategoryFacetPath", "case_category_facet_path")
        ) or _first_non_empty(src, ("caseCategoryFacetPath",))
        out["petitioners"] = _list_text(out, "petitioners") or _list_text(src, "petitioners")
        out["respondents"] = _list_text(out, "respondents") or _list_text(src, "respondents")
        out["petitionerAdvocates"] = _list_text(
            out, "petitionerAdvocates"
        ) or _list_text(src, "petitionerAdvocates")
        out["respondentAdvocates"] = _list_text(
            out, "respondentAdvocates"
        ) or _list_text(src, "respondentAdvocates")
        if not out.get("parties_text") and (out["petitioners"] or out["respondents"]):
            out["parties_text"] = f"{', '.join(out['petitioners'])} vs {', '.join(out['respondents'])}".strip()
        return out

    def _party_text_from_api_record(rec: dict) -> str:
        direct = (
            rec.get("parties_text")
            or rec.get("parties")
            or rec.get("Petitioner Name versus Respondent Name")
            or ""
        )
        if isinstance(direct, str) and direct.strip():
            return direct
        petitioners = rec.get("petitioners")
        respondents = rec.get("respondents")
        if isinstance(petitioners, list) and petitioners:
            lhs = ", ".join(str(x).strip() for x in petitioners if str(x).strip())
            rhs = ""
            if isinstance(respondents, list) and respondents:
                rhs = ", ".join(str(x).strip() for x in respondents if str(x).strip())
            return f"{lhs} vs {rhs}".strip()
        return ""

    def _search_year_from_api_record(rec: dict) -> str | None:
        for key in ("search_year", "Search_Year", "filingYear", "decisionYear"):
            value = rec.get(key)
            if value is None:
                continue
            txt = str(value).strip()
            if txt:
                return txt
        return None

    owner_variants = [o.strip() for o in (owner_names or []) if isinstance(o, str) and o.strip()]
    if not owner_variants and owner_name:
        owner_variants = [owner_name]
    owner_variants = list(dict.fromkeys(owner_variants))

    # Primary names = 7/12 Bhulekh occupant names (highest priority).
    # Fall back to all owner variants when not explicitly provided.
    primary_variants = [o.strip() for o in (primary_owner_names or []) if isinstance(o, str) and o.strip()]
    if not primary_variants:
        primary_variants = owner_variants

    # Secondary names = IGR purchaser names only (names in owner_variants but
    # not in primary_variants).
    primary_set = set(primary_variants)
    secondary_variants = [o for o in owner_variants if o not in primary_set]

    out: list[RankedCaseHit] = []
    for original in records:
        rec = _canonicalize_case_record(original)
        parties = _party_text_from_api_record(rec)
        party_names = _extract_individual_party_names(rec)

        # Score owners only when a full 7/12 name exactly matches one party entry.
        primary_score, matched_owner, reason = score_owner_variants_exact_phrase(rec, primary_variants)
        secondary_score = 0.0
        sec_reason = "no_exact_match"
        if secondary_variants:
            secondary_score, sec_match, sec_reason = score_owner_variants_exact_phrase(
                rec, secondary_variants
            )
            if secondary_score > primary_score:
                matched_owner = matched_owner or sec_match
                reason = reason if primary_score >= 1.0 else sec_reason

        owner_score = max(primary_score, secondary_score * 0.6)
        primary_name_matched = primary_score >= 1.0

        owner_match_count = 0
        for owner in owner_variants:
            if any(_names_exact_equivalent(owner, party) for party in party_names):
                owner_match_count += 1
        owner_total = len(owner_variants)
        all_owner_match = owner_total > 1 and owner_match_count == owner_total
        owner_match_boost = 0.0
        if owner_total > 0:
            owner_match_boost = 0.08 if all_owner_match else 0.03 * (owner_match_count / owner_total)
        side_purity = _owner_side_purity(rec, owner_variants)
        # Keep only cases where at least one searched owner name is present.
        # User-facing ordering and visibility should be strictly name-match driven.
        if owner_match_count == 0:
            continue

        party_score = _party_overlap_score(parties, igr_party_names)
        court = (
            rec.get("court")
            or rec.get("courtName")
            or rec.get("Court_Number_Judge")
            or rec.get("Court")
            or ""
        )
        district_score = _district_court_overlap_score(str(court), district_label)
        village_loc_score, taluka_loc_score, district_loc_score = _court_location_tier_scores(
            str(court),
            district_label=district_label,
            taluka_label=taluka_label,
            village_label=village_label,
        )
        location_score = _court_location_overlap_score(
            str(court),
            district_label=district_label,
            taluka_label=taluka_label,
            village_label=village_label,
        )
        final_score = round(
            primary_score * 0.45 + secondary_score * 0.07 + party_score * 0.25 + location_score * 0.23 + owner_match_boost,
            4,
        )
        if final_score < min_score:
            continue
        case_type = rec.get("case_type") or rec.get("Case_Type") or rec.get("caseType")
        case_status = rec.get("case_status") or rec.get("Case_Status") or rec.get("caseStatus")
        pending = is_pending_case(case_status)
        out.append(
            RankedCaseHit(
                case_id=(
                    rec.get("case_id")
                    or rec.get("caseNumber")
                    or rec.get("id")
                    or rec.get("registrationNumber")
                    or rec.get("Case Type/Case Number/Case Year")
                ),
                cnr_number=rec.get("cnr") or rec.get("cnr_number") or rec.get("CNR_Number"),
                search_year=_search_year_from_api_record(rec),
                case_type=case_type,
                court=str(court) if court else None,
                parties_text=parties,
                is_civil=is_civil_case(case_type),
                name_match_score=final_score,
                owner_match_count=owner_match_count,
                owner_total=owner_total,
                matched_variant=matched_owner,
                primary_name_matched=primary_name_matched,
                owner_side_purity=side_purity,
                village_location_score=village_loc_score,
                taluka_location_score=taluka_loc_score,
                district_location_score=district_loc_score,
                is_pending=pending,
                match_explanation=(
                    f"{reason};primary_score={primary_score:.2f};secondary_score={secondary_score:.2f};"
                    f"owner_matches={owner_match_count}/{owner_total};"
                    f"owner_match_scope={'all' if all_owner_match else 'partial'};"
                    f"owner_side_purity={side_purity:.2f};"
                    f"igr_party_overlap={party_score:.2f};"
                    f"district_court_overlap={district_score:.2f};pending={pending}"
                    f";village_court_overlap={village_loc_score:.2f}"
                    f";taluka_court_overlap={taluka_loc_score:.2f}"
                    f";district_location_overlap={district_loc_score:.2f}"
                    f";court_location_overlap={location_score:.2f}"
                ),
                raw_json=json.dumps(rec, ensure_ascii=False),
            )
        )
    out.sort(
        key=lambda h: (
            -h.owner_match_count,                                                # 1st: most owners matched (5→4→3→2→1)
            not h.is_pending,                                                    # 2nd: active/pending before closed/disposed
            -h.owner_side_purity,                                                # 3rd: matched owners dominate their side
            -(h.owner_match_count / h.owner_total) if h.owner_total else 0.0,   # 4th: match density tiebreak
            not h.primary_name_matched,                                          # 5th: prefer 7/12 Bhulekh match
            -h.village_location_score,                                           # 6th: village court match
            -h.taluka_location_score,                                            # 7th: taluka court match
            -h.district_location_score,                                          # 8th: district court match (weakest)
            -h.name_match_score,                                                 # 9th: overall score
            h.search_year or "9999",
        )
    )
    return out


def score_case_against_variants(parties_text: str, variants: list[str]) -> tuple[float, str | None, str]:
    hay = _normalize_name(parties_text)
    if not hay or not variants:
        return 0.0, None, "no_match"

    best_score = 0.0
    best_variant: str | None = None
    best_reason = "no_match"

    for v in variants:
        vn = _normalize_name(v)
        if not vn:
            continue
        if vn in hay:
            score = 1.0
            reason = "exact_substring"
        else:
            ratio = difflib.SequenceMatcher(None, vn, hay).ratio()
            token_overlap = len(set(vn.split()) & set(hay.split())) / max(1, len(set(vn.split())))
            score = max(ratio * 0.8, token_overlap * 0.9)
            reason = "fuzzy_ratio" if ratio >= token_overlap else "token_overlap"
        if score > best_score:
            best_score = score
            best_variant = v
            best_reason = reason
    return round(best_score, 4), best_variant, best_reason


def dedupe_case_key(rec: dict) -> str:
    cnr = (
        rec.get("cnr")
        or rec.get("cnr_number")
        or rec.get("CNR_Number")
        or rec.get("CNR Number")
        or ""
    )
    cnr = str(cnr).strip()
    if cnr:
        return f"cnr:{cnr}"
    fallback = (
        rec.get("case_id")
        or rec.get("id")
        or rec.get("caseNumber")
        or rec.get("registrationNumber")
        or rec.get("Case Type/Case Number/Case Year")
        or ""
    )
    fallback = str(fallback).strip()
    parties = rec.get("parties_text") or rec.get("Petitioner Name versus Respondent Name") or ""
    if not parties:
        petitioners = rec.get("petitioners")
        respondents = rec.get("respondents")
        if isinstance(petitioners, list) and petitioners:
            left = ", ".join(str(x).strip() for x in petitioners if str(x).strip())
            right = ", ".join(str(x).strip() for x in (respondents or []) if str(x).strip())
            parties = f"{left} vs {right}".strip()
    parties = str(parties).strip()
    return f"fallback:{fallback}|{parties}"


def rank_case_hits(
    records: list[dict],
    variants: list[GeneratedVariant],
    *,
    min_score: float = 0.45,
) -> list[RankedCaseHit]:
    variant_texts = [v.variant_text for v in variants]
    hits: list[RankedCaseHit] = []

    for rec in records:
        parties = rec.get("Petitioner Name versus Respondent Name") or rec.get("col_2") or ""
        score, matched_variant, reason = score_case_against_variants(parties, variant_texts)
        if score < min_score:
            continue
        case_type = rec.get("Case_Type") or rec.get("Case Type/Case Number/Case Year")
        hits.append(
            RankedCaseHit(
                case_id=(rec.get("Case Type/Case Number/Case Year") or rec.get("CNR_Number")),
                cnr_number=rec.get("CNR_Number"),
                search_year=rec.get("Search_Year"),
                case_type=case_type,
                court=rec.get("Court_Number_Judge"),
                parties_text=parties,
                is_civil=is_civil_case(case_type),
                name_match_score=score,
                owner_match_count=0,
                owner_total=0,
                matched_variant=matched_variant,
                match_explanation=reason,
                raw_json=json.dumps(rec, ensure_ascii=False),
            )
        )

    hits.sort(key=lambda h: (-h.name_match_score, h.search_year or "9999"))
    logger.info(
        "Case ranking completed: input_records=%s output_hits=%s min_score=%.2f",
        len(records),
        len(hits),
        min_score,
    )
    return hits


def write_html_artifact(root: Path, workflow_id: str, html: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    out = root / f"{workflow_id}_submitted.html"
    out.write_text(html or "", encoding="utf-8")
    return out


def extract_survey_option_labels(html: str, survey_part1: str) -> list[str]:
    """
    Extract survey dropdown labels from submitted Bhulekh HTML for a given part1.

    Example:
      survey_part1=1530 -> returns ["1530/1", "1530/2", "1530/3"] when present.
    """
    soup = BeautifulSoup(html or "", "html.parser")
    select = soup.select_one("#ContentPlaceHolder1_ddlsurveyno")
    if not select:
        return []

    prefix = (survey_part1 or "").strip().lower()
    out: list[str] = []
    seen: set[str] = set()
    for opt in select.select("option"):
        label = " ".join((opt.get_text() or "").split()).strip()
        if not label:
            continue
        if label.startswith("--"):
            continue
        low = label.lower()
        if prefix and not (low == prefix or low.startswith(f"{prefix}/")):
            continue
        if low in seen:
            continue
        seen.add(low)
        out.append(label)
    return out
