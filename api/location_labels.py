"""Bhulekh ↔ IGR location label matching and mapping."""

from __future__ import annotations

import json
import logging
import os
import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_MAP_PATH = Path("static/data/igr_location_map.json")

# Manual overrides and common spelling variants (extend via map rebuild + review).
_LOCATION_ALIASES: dict[str, tuple[str, ...]] = {
    "pune": ("पुणे", "pune"),
    "satara": ("सातारा", "satara"),
    "sindhudurg": ("सिंधुदुर्ग", "sindhudurg"),
    "haveli": ("हवेली", "haveli"),
    "khed": ("खेड", "khed"),
    "shirur": ("शिरूर", "shirur", "shirur"),
    "nighoje": ("निघोजे", "nighoje", "nighoje"),
    "baner": ("बाणेर", "baner", "bner", "baaner"),
    "mulshi": ("मुळशी", "मुळ्शी", "mulshi", "mulashi"),
    "wakad": ("वाकड", "wakad"),
    "uruli": ("उरुळी", "उरली", "uruli"),
    "uruli kanchan": ("उरुळी कांचन", "उरळी कांचन", "uruli kanchan"),
    "uruli devachi": ("उरुळी देवाची", "उरुळीदेवाची", "uruli devachi"),
    "waghol": ("वाघोली", "वाघोळी", "waghol", "wagoli"),
    "wagholi": ("वाघोली", "वाघोळी", "waghol", "wagoli"),
    "talegaon dhamdhere": ("तळेगांव ढमढेरे", "talegaon dhamdhere", "talegaon dhamdere"),
    "karve nagar": ("कर्वेनगर", "म .कर्वेनगर", "karvenagar", "karve nagar"),
    "karvenagar": ("कर्वेनगर", "म .कर्वेनगर", "karve nagar"),
    "darawali": ("दारवली", "daravali", "darawali", "dara vali"),
    "daravali": ("दारवली", "darawali", "dara vali"),
}

_DEV_TO_ASCII_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")
_PLACEHOLDER_LABEL_RE = re.compile(r"^--|select|निवडा|-----", re.IGNORECASE)


def _build_alias_lookup() -> dict[str, tuple[str, ...]]:
    clusters: list[set[str]] = []
    for key, members in _LOCATION_ALIASES.items():
        cluster = {key.strip().lower()}
        cluster.update(m.strip().lower() for m in members if (m or "").strip())
        clusters.append(cluster)
    lookup: dict[str, tuple[str, ...]] = {}
    for cluster in clusters:
        expanded = tuple(sorted(cluster))
        for token in cluster:
            lookup[token] = expanded
    return lookup


_ALIAS_LOOKUP = _build_alias_lookup()


def sanitize_label(value: str) -> str:
    txt = unicodedata.normalize("NFKC", value or "")
    txt = txt.replace("\ufeff", "")
    txt = txt.replace("\u200b", "").replace("\u200c", "").replace("\u200d", "")
    txt = txt.replace("\ufffd", "").replace("?", "")
    return txt.strip()


def canonical_label(value: str) -> str:
    """Compact normalized form for fuzzy compare."""
    txt = sanitize_label(value).translate(_DEV_TO_ASCII_DIGITS).lower()
    txt = re.sub(r"\([^)]*\)", "", txt)
    txt = re.sub(r"[\s.\-_/]+", "", txt)
    return txt


def is_placeholder_label(label: str) -> bool:
    lab = sanitize_label(label)
    if not lab:
        return True
    return bool(_PLACEHOLDER_LABEL_RE.search(lab))


def expand_label_needles(label: str) -> list[str]:
    cleaned = sanitize_label(label)
    base = cleaned.lower()
    out: list[str] = []
    if base:
        out.append(base)
    extra = _ALIAS_LOOKUP.get(base)
    if extra:
        out.extend(extra)
    paren = re.search(r"\(([^)]+)\)", cleaned)
    if paren:
        inner = (paren.group(1) or "").strip().lower()
        if inner and inner not in out:
            out.append(inner)
    # Dedupe preserving order.
    seen: set[str] = set()
    deduped: list[str] = []
    for item in out:
        if item and item not in seen:
            seen.add(item)
            deduped.append(item)
    return deduped


def labels_match(option_label: str, wanted: str) -> bool:
    o = sanitize_label(option_label).lower()
    if not o or is_placeholder_label(o):
        return False
    o_canonical = canonical_label(option_label)
    for needle in expand_label_needles(wanted):
        n = needle.strip().lower()
        if not n:
            continue
        n_canonical = canonical_label(needle)
        if n in o or o in n:
            return True
        if n_canonical and n_canonical in o_canonical:
            return True
        if n_canonical and o_canonical and n_canonical == o_canonical:
            return True
        for m in re.finditer(r"\(([^)]+)\)", o):
            inner = (m.group(1) or "").strip().lower()
            if n == inner or n in inner or inner in n:
                return True
    return False


@dataclass(frozen=True)
class LabelMatch:
    label: str
    value: str
    method: str
    score: float


def best_option_match(wanted: str, options: list[dict[str, str]]) -> LabelMatch | None:
    """Pick the best IGR/Bhulekh dropdown option for a Bhulekh label."""
    wanted = sanitize_label(wanted)
    if not wanted:
        return None

    usable = [
        o
        for o in options
        if sanitize_label(o.get("label", "")) and not is_placeholder_label(o.get("label", ""))
    ]
    if not usable:
        return None

    # 1) Alias / substring rules — pick strongest match, not first partial hit.
    alias_best: LabelMatch | None = None
    wanted_canonical = canonical_label(wanted)
    for opt in usable:
        opt_label = opt.get("label", "").strip()
        if not labels_match(opt_label, wanted):
            continue
        opt_canonical = canonical_label(opt_label)
        score = 1.0 if opt_canonical == wanted_canonical else 0.92
        candidate = LabelMatch(
            label=opt_label,
            value=(opt.get("value") or "").strip(),
            method="alias",
            score=score,
        )
        if alias_best is None or candidate.score > alias_best.score:
            alias_best = candidate
        elif candidate.score == alias_best.score and len(opt_canonical) > len(canonical_label(alias_best.label)):
            alias_best = candidate
    if alias_best is not None:
        return alias_best

    # 2) Canonical exact.
    for opt in usable:
        if canonical_label(opt.get("label", "")) == wanted_canonical:
            return LabelMatch(
                label=opt.get("label", "").strip(),
                value=(opt.get("value") or "").strip(),
                method="canonical_exact",
                score=0.98,
            )

    # 3) Fuzzy ratio on canonical strings.
    best: LabelMatch | None = None
    for opt in usable:
        opt_label = opt.get("label", "").strip()
        ratio = SequenceMatcher(None, wanted_canonical, canonical_label(opt_label)).ratio()
        if ratio < 0.82:
            continue
        candidate = LabelMatch(
            label=opt_label,
            value=(opt.get("value") or "").strip(),
            method="fuzzy",
            score=ratio,
        )
        if best is None or candidate.score > best.score:
            best = candidate
    return best


def location_map_path() -> Path:
    raw = (os.getenv("IGR_LOCATION_MAP_PATH") or "").strip()
    return Path(raw) if raw else DEFAULT_MAP_PATH


@lru_cache(maxsize=1)
def load_location_map() -> dict:
    path = location_map_path()
    if not path.is_file():
        return {"version": 1, "lookup": {}, "districts": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Failed to load IGR location map %s: %s", path, exc)
        return {"version": 1, "lookup": {}, "districts": []}


def _lookup_key(district: str, taluka: str, village: str) -> str:
    return "|".join(
        canonical_label(x) for x in (district, taluka, village) if sanitize_label(x)
    )


def resolve_igr_labels(
    district_label: str,
    taluka_label: str,
    village_label: str,
) -> tuple[str, str, str, str | None]:
    """
    Resolve Bhulekh labels to IGR dropdown labels using the static map.

    Returns (igr_district, igr_taluka, igr_village, match_method).
    Falls back to original labels when no map entry exists.
    """
    key = _lookup_key(district_label, taluka_label, village_label)
    lookup = load_location_map().get("lookup") or {}
    entry = lookup.get(key)
    if not entry:
        return district_label, taluka_label, village_label, None

    return (
        entry.get("igr_district") or district_label,
        entry.get("igr_taluka") or taluka_label,
        entry.get("igr_village") or village_label,
        entry.get("match_method") or "mapped",
    )


def build_lookup_from_district_tree(districts: list[dict]) -> dict[str, dict]:
    """Flatten hierarchical map tree into lookup[key] entries."""
    out: dict[str, dict] = {}
    for district in districts:
        d_lab = district.get("bhulekh", {}).get("label", "")
        d_igr = (district.get("igr") or {}).get("label", "")
        for taluka in district.get("talukas") or []:
            t_lab = taluka.get("bhulekh", {}).get("label", "")
            t_igr = (taluka.get("igr") or {}).get("label", "")
            for village in taluka.get("villages") or []:
                v_lab = village.get("bhulekh", {}).get("label", "")
                v_igr = (village.get("igr") or {}).get("label", "")
                if not all([d_lab, t_lab, v_lab, d_igr, t_igr, v_igr]):
                    continue
                key = _lookup_key(d_lab, t_lab, v_lab)
                out[key] = {
                    "bhulekh_district": d_lab,
                    "bhulekh_taluka": t_lab,
                    "bhulekh_village": v_lab,
                    "igr_district": d_igr,
                    "igr_taluka": t_igr,
                    "igr_village": v_igr,
                    "match_method": village.get("match_method")
                    or taluka.get("match_method")
                    or district.get("match_method")
                    or "mapped",
                }
    return out
