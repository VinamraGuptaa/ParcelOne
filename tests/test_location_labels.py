"""Tests for Bhulekh ↔ IGR location label matching."""

from api.location_labels import (
    best_option_match,
    build_lookup_from_district_tree,
    canonical_label,
    labels_match,
    resolve_igr_labels,
)


def test_labels_match_parenthesized_english():
    assert labels_match("पुणे(Pune)", "Pune") is True
    assert labels_match("पुणे(Pune)", "पुणे") is True


def test_labels_match_khed_nighoje():
    options = [{"label": "खेड", "value": "1"}]
    match = best_option_match("खेड", options)
    assert match is not None
    assert match.label == "खेड"

    village_options = [{"label": "निघोज", "value": "2"}, {"label": "निघोजे", "value": "3"}]
    match_v = best_option_match("निघोजे", village_options)
    assert match_v is not None
    assert match_v.label == "निघोजे"


def test_build_lookup_and_resolve():
    tree = [
        {
            "bhulekh": {"label": "पुणे"},
            "igr": {"label": "पुणे(Pune)"},
            "match_method": "alias",
            "talukas": [
                {
                    "bhulekh": {"label": "खेड"},
                    "igr": {"label": "खेड"},
                    "match_method": "alias",
                    "villages": [
                        {
                            "bhulekh": {"label": "निघोजे"},
                            "igr": {"label": "निघोजे"},
                            "match_method": "alias",
                        }
                    ],
                }
            ],
        }
    ]
    lookup = build_lookup_from_district_tree(tree)
    key = canonical_label("पुणे") + "|" + canonical_label("खेड") + "|" + canonical_label("निघोजे")
    assert key in lookup

    # Monkeypatch via direct resolve test would need map file; test lookup structure instead.
    assert lookup[key]["igr_village"] == "निघोजे"


def test_canonical_label_strips_spaces_and_punctuation():
    assert canonical_label("ग. नं. 970") == canonical_label("ग.नं970")


def test_fukeri_not_ker_village():
    options = [{"label": "केर", "value": "केर"}, {"label": "फुकेरी", "value": "फुकेरी"}]
    match = best_option_match("Fukeri", options)
    assert match is not None
    assert match.label == "फुकेरी"


def test_resolve_english_pune_khed_nighoje():
    d, t, v, method = resolve_igr_labels("Pune", "Khed", "Nighoje")
    assert method == "alias"
    assert d == "पुणे"
    assert t == "खेड"
    assert v == "निघोजे"
