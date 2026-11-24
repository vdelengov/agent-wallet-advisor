"""Sanity checks on the hand-entered cards.json. Typos in data are the likeliest bugs."""

from datetime import date

import pytest

from agent_wallet_advisor.rewards import KNOWN_CATEGORIES, MERCHANT_CATEGORIES, CATEGORY_ALIASES, load_cards, load_programs

REQUIRED = {
    "id": str,
    "name": str,
    "aliases": list,
    "issuer": str,
    "base_rate": (int, float),
    "categories": dict,
    "promotions": list,
    "points_program": str,
    "source_url": str,
    "annual_fee": (int, float),
    "not_modeled": list,
    "as_of": str,
    "source_note": str,
    "needs_verification": bool,
    "verification_notes": str,
}

CARDS = load_cards()


@pytest.mark.parametrize("card", CARDS, ids=lambda c: c.get("id", "?"))
def test_required_fields_and_types(card):
    for key, typ in REQUIRED.items():
        assert key in card, f"missing {key}"
        assert isinstance(card[key], typ), f"{key} has wrong type"
    # bool is a subclass of int in Python; make sure rates aren't accidentally true/false.
    assert not isinstance(card["base_rate"], bool)


@pytest.mark.parametrize("card", CARDS, ids=lambda c: c["id"])
def test_numbers_are_sane(card):
    assert card["base_rate"] > 0
    assert card["points_program"] in load_programs()
    assert card["annual_fee"] >= 0
    for cat, rule in card["categories"].items():
        assert rule["rate"] > 0, cat
        if "cap" in rule:
            cap = rule["cap"]
            assert cap["amount"] > 0
            assert cap.get("rate_after", card["base_rate"]) >= 0
            assert cap["period"] in {"calendar_year", "quarter", "month"}


@pytest.mark.parametrize("card", CARDS, ids=lambda c: c["id"])
def test_categories_are_known(card):
    unknown = set(card["categories"]) - KNOWN_CATEGORIES
    assert not unknown, f"unknown categories: {unknown}"


@pytest.mark.parametrize("card", CARDS, ids=lambda c: c["id"])
def test_dates_and_sources(card):
    date.fromisoformat(card["as_of"])
    assert card["source_note"].strip()
    assert card["source_url"].startswith("https://")
    if card["needs_verification"]:
        assert card["verification_notes"].strip(), "say what needs checking"


def _date_or_none(value, where, problems):
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        problems.append(f"{where}: {value!r} is not an ISO date")
        return None


def rule_problems(card):
    """Problems with a card's dated rules: unparsable dates, valid_from after valid_until, missing sources."""
    problems = []
    rules = [(f"categories.{cat}", r, False) for cat, r in card["categories"].items()]
    rules += [(f"promotions[{i}]", p, True) for i, p in enumerate(card["promotions"])]
    for where, rule, is_promo in rules:
        for key in ("valid_from", "valid_until"):
            if key not in rule:
                problems.append(f"{where}: missing {key} (use null for no known date)")
        start = _date_or_none(rule.get("valid_from"), f"{where}.valid_from", problems)
        end = _date_or_none(rule.get("valid_until"), f"{where}.valid_until", problems)
        if start and end and start > end:
            problems.append(f"{where}: valid_from {start} is after valid_until {end}")
        if is_promo:
            if rule.get("category") not in KNOWN_CATEGORIES:
                problems.append(f"{where}: unknown category {rule.get('category')!r}")
            if not isinstance(rule.get("rate"), (int, float)) or isinstance(rule.get("rate"), bool) or rule["rate"] <= 0:
                problems.append(f"{where}: rate must be a positive number")
            if rule.get("valid_until") is None:
                problems.append(f"{where}: a promotion needs the end date shown on the issuer's page")
            if not str(rule.get("description", "")).strip():
                problems.append(f"{where}: missing description")
        if rule.get("valid_from") is not None or rule.get("valid_until") is not None:
            if not str(rule.get("source_url", "")).startswith("https://"):
                problems.append(f"{where}: a dated rule needs its own source_url")
            if _date_or_none(rule.get("as_of"), f"{where}.as_of", problems) is None:
                problems.append(f"{where}: a dated rule needs its own as_of")
    return problems


@pytest.mark.parametrize("card", CARDS, ids=lambda c: c["id"])
def test_dated_rules_are_valid_and_sourced(card):
    assert rule_problems(card) == []


GOOD_PROMO = {"category": "groceries", "rate": 10, "valid_from": None, "valid_until": "2026-12-31",
              "description": "example", "source_url": "https://example.com/offer", "as_of": "2026-11-24"}


@pytest.mark.parametrize("change, expected", [
    ({}, None),
    ({"valid_until": "2026-13-01"}, "not an ISO date"),
    ({"valid_from": "2027-01-01"}, "is after valid_until"),
    ({"source_url": ""}, "needs its own source_url"),
    ({"as_of": None}, "needs its own as_of"),
    ({"valid_until": None}, "end date shown on the issuer's page"),
    ({"category": "shoes"}, "unknown category"),
])
def test_rule_validator_catches_bad_promotions(change, expected):
    card = {"categories": {}, "promotions": [{**GOOD_PROMO, **change}]}
    problems = rule_problems(card)
    if expected is None:
        assert problems == []
    else:
        assert any(expected in p for p in problems), problems


def test_rule_validator_catches_dated_category_rule_without_source():
    card = {"categories": {"gas": {"rate": 3, "valid_from": None, "valid_until": "2027-03-31"}}, "promotions": []}
    assert any("needs its own source_url" in p for p in rule_problems(card))
    card["categories"]["gas"].pop("valid_from")
    assert any("missing valid_from" in p for p in rule_problems(card))


def test_blue_cash_preferred_matches_verified_terms():
    # Source: American Express product page, as of 2026-11-24.
    bcp = next(c for c in CARDS if c["id"] == "amex-blue-cash-preferred")
    assert not bcp["needs_verification"]
    assert bcp["points_program"] == "cash"
    assert bcp["base_rate"] == 1
    assert bcp["annual_fee"] == 95
    assert {cat: r["rate"] for cat, r in bcp["categories"].items()} == {
        "groceries": 6, "streaming": 6, "gas": 3, "transit": 3,
    }
    capped = {cat for cat, r in bcp["categories"].items() if "cap" in r}
    assert capped == {"groceries"}, "only supermarkets are capped"
    assert bcp["categories"]["groceries"]["cap"] == {"amount": 6000, "period": "calendar_year", "rate_after": 1}


def test_ids_names_and_aliases_are_unique():
    owner = {}
    for card in CARDS:
        for n in [card["id"], card["name"], *card["aliases"]]:
            key = n.lower().replace("-", " ")
            assert owner.get(key, card["id"]) == card["id"], f"'{n}' used by {owner[key]} and {card['id']}"
            owner[key] = card["id"]


def test_lookup_tables_point_to_known_categories():
    assert set(MERCHANT_CATEGORIES.values()) <= KNOWN_CATEGORIES
    assert set(CATEGORY_ALIASES.values()) <= KNOWN_CATEGORIES


def test_point_programs():
    programs = load_programs()
    assert programs["cash"] == {**programs["cash"], "default_cents": 1.0, "overridable": False}
    for key, p in programs.items():
        assert 0 < p["default_cents"] <= 5, key
        assert p["name"].strip() and p["basis"].strip(), key
