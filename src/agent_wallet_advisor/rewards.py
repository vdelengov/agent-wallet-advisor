"""Reward math and card lookup. No MCP imports, so this module is easy to test."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import date, timedelta
from functools import lru_cache
from importlib.resources import files
from typing import Any

KNOWN_CATEGORIES = {
    "dining",
    "groceries",
    "travel",
    "gas",
    "online_shopping",
    "streaming",
    "drugstores",
    "entertainment",
    "transit",
    "other",
}

# Common ways an agent might phrase a category -> our canonical category.
CATEGORY_ALIASES = {
    "restaurant": "dining",
    "restaurants": "dining",
    "food": "dining",
    "grocery": "groceries",
    "supermarket": "groceries",
    "supermarkets": "groceries",
    "airfare": "travel",
    "flights": "travel",
    "hotel": "travel",
    "hotels": "travel",
    "fuel": "gas",
    "gas_station": "gas",
    "gas_stations": "gas",
    "online": "online_shopping",
    "ecommerce": "online_shopping",
    "pharmacy": "drugstores",
    "drugstore": "drugstores",
    "rideshare": "transit",
}

# Fallback used only when the category is missing or unknown. Matched as a
# lowercase substring of the merchant name. Superstores and warehouse clubs map
# to "other" because issuers usually exclude them from grocery bonuses.
MERCHANT_CATEGORIES = {
    "whole foods": "groceries",
    "trader joe": "groceries",
    "kroger": "groceries",
    "safeway": "groceries",
    "walmart": "other",
    "target": "other",
    "costco": "other",
    "starbucks": "dining",
    "chipotle": "dining",
    "mcdonald": "dining",
    "doordash": "dining",
    "shell": "gas",
    "chevron": "gas",
    "exxon": "gas",
    "delta": "travel",
    "united airlines": "travel",
    "marriott": "travel",
    "airbnb": "travel",
    "expedia": "travel",
    "netflix": "streaming",
    "spotify": "streaming",
    "cvs": "drugstores",
    "walgreens": "drugstores",
    "uber eats": "dining",  # must come before "uber"
    "uber": "transit",
    "lyft": "transit",
    "amazon": "online_shopping",
    "ticketmaster": "entertainment",
}


POINT_VALUES_ENV = "AGENT_WALLET_POINT_VALUES"
STALE_AFTER_ENV = "AGENT_WALLET_STALE_AFTER_DAYS"
DEFAULT_STALE_AFTER_DAYS = 90
ENDING_SOON_ENV = "AGENT_WALLET_ENDING_SOON_DAYS"
DEFAULT_ENDING_SOON_DAYS = 30
MAX_DAYS_SETTING = 3650


@dataclass
class AppliedRule:
    """The rule that set a card's rate for this purchase."""
    kind: str  # "promotion", "category" or "base_rate"
    category: str
    rate: float
    valid_from: str | None = None
    valid_until: str | None = None
    description: str | None = None  # promotions only


@dataclass
class EndingSoon:
    card: str
    card_id: str
    rule: str  # readable label, e.g. "6x groceries"
    valid_until: str
    days_left: int  # 0 means today is the last day


@dataclass
class CardReward:
    card: str
    card_id: str
    rate: float
    reward_usd: float
    cap_applied: bool = False
    points_program: str = "cash"
    points: float | None = None  # points earned; None for cash-back cards
    cents_per_point: float | None = None  # the value assumed for those points
    data_as_of: str = ""  # date the rules behind this result were last checked
    data_age_days: int = 0
    stale: bool = False  # past a rule's valid_until, or older than stale_after_days
    rule: AppliedRule | None = None  # which rule set the rate
    stale_reason: str | None = None  # "valid_until" or "age"; None when fresh


@dataclass
class BestCardResult:
    merchant: str
    category: str
    category_guessed: bool
    amount: float
    best: CardReward
    runner_up: CardReward | None
    explanation: str
    point_values_used: dict[str, float] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    stale_after_days: int = DEFAULT_STALE_AFTER_DAYS
    warning: str | None = None  # set when a recommended card is stale or its rule ends soon
    ending_soon_days: int = DEFAULT_ENDING_SOON_DAYS
    ending_soon: list[EndingSoon] = field(default_factory=list)


@lru_cache(maxsize=1)
def load_data() -> dict[str, Any]:
    """Load the bundled cards.json (works from a checkout or an installed wheel)."""
    text = (files("agent_wallet_advisor") / "data" / "cards.json").read_text(encoding="utf-8")
    return json.loads(text)


def load_cards() -> list[dict[str, Any]]:
    return load_data()["cards"]


def load_programs() -> dict[str, dict[str, Any]]:
    return load_data()["point_programs"]


def _apply_overrides(values: dict[str, float], overrides: dict[str, float], source: str) -> None:
    programs = load_programs()
    for key, cents in overrides.items():
        if key not in programs:
            valid = sorted(k for k, p in programs.items() if p["overridable"])
            raise ValueError(f"unknown points program '{key}' in {source}; valid keys: {valid}")
        if not programs[key]["overridable"]:
            raise ValueError(f"'{key}' is cash back and is always worth face value; it can't be overridden")
        if not isinstance(cents, (int, float)) or isinstance(cents, bool) or not 0 < cents <= 5:
            raise ValueError(f"point value for '{key}' must be a number of cents between 0 and 5, got {cents!r}")
        values[key] = float(cents)


def resolve_point_values(overrides: dict[str, float] | None = None) -> dict[str, float]:
    """Cents per point for each program: data-file default < server env var < per-call override."""
    values = {k: p["default_cents"] for k, p in load_programs().items()}
    env = os.environ.get(POINT_VALUES_ENV)
    if env:
        try:
            parsed = json.loads(env)
        except json.JSONDecodeError as e:
            raise ValueError(f"{POINT_VALUES_ENV} is not valid JSON: {e}") from e
        _apply_overrides(values, parsed, POINT_VALUES_ENV)
    if overrides:
        _apply_overrides(values, overrides, "point_values")
    return values


def _today() -> date:
    """Today's date. A function so tests can pin it."""
    return date.today()


def _check_days(value: Any, source: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= MAX_DAYS_SETTING:
        raise ValueError(f"{source} must be a whole number of days from 1 to {MAX_DAYS_SETTING}, got {value!r}")
    return value


def _resolve_days(override: int | None, arg: str, env_name: str, default: int) -> int:
    """A days setting: default < server env var < per-call override."""
    if override is not None:
        return _check_days(override, arg)
    env = os.environ.get(env_name)
    if env:
        try:
            parsed = int(env.strip())
        except ValueError as e:
            raise ValueError(f"{env_name} must be a whole number of days, got {env!r}") from e
        return _check_days(parsed, env_name)
    return default


def resolve_stale_after_days(override: int | None = None) -> int:
    """Days before card data counts as stale: default < server env var < per-call override."""
    return _resolve_days(override, "stale_after_days", STALE_AFTER_ENV, DEFAULT_STALE_AFTER_DAYS)


def resolve_ending_soon_days(override: int | None = None) -> int:
    """How close to its valid_until a rule counts as ending soon: default < env var < per-call."""
    return _resolve_days(override, "ending_soon_days", ENDING_SOON_ENV, DEFAULT_ENDING_SOON_DAYS)


# --- dated rules ----------------------------------------------------------------

def _d(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


def rule_is_active(rule: dict[str, Any], today: date) -> bool:
    """valid_from and valid_until are inclusive; null means no known start or end."""
    start, end = _d(rule.get("valid_from")), _d(rule.get("valid_until"))
    return not (start and today < start) and not (end and today > end)


def rule_status(rule: dict[str, Any], today: date, ending_soon_days: int) -> str:
    """'upcoming', 'expired', 'ending_soon' or 'active'."""
    start, end = _d(rule.get("valid_from")), _d(rule.get("valid_until"))
    if start and today < start:
        return "upcoming"
    if end and today > end:
        return "expired"
    if end and (end - today).days <= ending_soon_days:
        return "ending_soon"
    return "active"


def category_rules(card: dict[str, Any], category: str) -> list[tuple[str, dict[str, Any]]]:
    """Rules for one category in priority order: promotions first, then the standing rule."""
    rules = [("promotion", p) for p in card.get("promotions", []) if p["category"] == category]
    if category in card["categories"]:
        rules.append(("category", card["categories"][category]))
    return rules


def all_rules(card: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    """(kind, category, rule) for every standing rule and promotion on a card."""
    return [("category", cat, r) for cat, r in card["categories"].items()] + [
        ("promotion", p["category"], p) for p in card.get("promotions", [])
    ]


def applicable_rule(card: dict[str, Any], category: str, today: date) -> tuple[str, dict[str, Any] | None]:
    """The first active rule for the category, skipping expired and upcoming ones; else the base rate."""
    for kind, rule in category_rules(card, category):
        if rule_is_active(rule, today):
            return kind, rule
    return "base_rate", None


def rule_label(kind: str, category: str, rate: float, description: str | None = None) -> str:
    if kind == "base_rate":
        return f"{rate:g}x base rate"
    label = f"{rate:g}x {category}"
    if kind == "promotion":
        label += " promotion" + (f" ({description})" if description else "")
    return label


def _freshness(
    card: dict[str, Any], rules: list[tuple[str, str, dict[str, Any]]], as_of: str, stale_after_days: int
) -> tuple[dict[str, Any], str | None]:
    """Freshness fields plus a readable detail when stale.

    Stale at whichever comes first: a rule's valid_until has passed, or as_of + stale_after_days.
    """
    today = _today()
    as_of_d = date.fromisoformat(as_of)
    age = max(0, (today - as_of_d).days)
    reasons = []  # (last good day, tie order, reason, detail)
    age_limit = as_of_d + timedelta(days=stale_after_days)
    if today > age_limit:
        reasons.append((age_limit, 1, "age", f"as of {as_of}, {age} days old"))
    for kind, cat, rule in rules:
        end = _d(rule.get("valid_until"))
        if end and today > end:
            label = rule_label(kind, cat, rule["rate"], rule.get("description"))
            reasons.append((end, 0, "valid_until", f"{label} ended {rule['valid_until']}"))
    fields = {"data_as_of": as_of, "data_age_days": age, "stale": bool(reasons), "stale_reason": None}
    if not reasons:
        return fields, None
    first = min(reasons)
    fields["stale_reason"] = first[2]
    return fields, first[3]


def build_warning(
    stale: list[tuple[dict[str, Any], str, str]], ending: list[EndingSoon], stale_after_days: int
) -> str | None:
    """One top-level warning. stale holds (card, reason, detail) for each stale card."""
    parts = []
    by_age = [f"{c['name']} ({d}; check {c['source_url']})" for c, reason, d in stale if reason == "age"]
    by_end = [f"{c['name']} ({d}; check {c['source_url']})" for c, reason, d in stale if reason == "valid_until"]
    if by_age:
        parts.append(f"Card data older than {stale_after_days} days: " + "; ".join(by_age) + ".")
    if by_end:
        parts.append("Card data past a rule's end date: " + "; ".join(by_end) + ".")
    if stale:
        parts.append("Rates may have changed since then; verify before relying on this recommendation.")
    if ending:
        parts.append("Ending soon: " + "; ".join(
            f"{e.card}'s {e.rule} ends {e.valid_until} ({e.days_left} days left)" for e in ending) + ".")
    return " ".join(parts) or None


def rule_report(card: dict[str, Any], stale_after_days: int, ending_soon_days: int) -> dict[str, Any]:
    """explain_rule's freshness and per-rule view: each rule's dates and status, plus the warning."""
    today = _today()
    rules, ending = [], []
    for kind, cat, rule in all_rules(card):
        status = rule_status(rule, today, ending_soon_days)
        end = _d(rule.get("valid_until"))
        days_left = (end - today).days if end and status != "expired" else None
        label = rule_label(kind, cat, rule["rate"], rule.get("description"))
        rules.append({
            "kind": kind, "category": cat, "rate": rule["rate"], "label": label,
            "valid_from": rule.get("valid_from"), "valid_until": rule.get("valid_until"),
            "status": status, "days_left": days_left,
            "source_url": rule.get("source_url", card["source_url"]), "as_of": rule.get("as_of", card["as_of"]),
        })
        if status == "ending_soon":
            ending.append(EndingSoon(card["name"], card["id"], label, rule["valid_until"], days_left))
    fresh, detail = _freshness(card, all_rules(card), card["as_of"], stale_after_days)
    stale = [(card, fresh["stale_reason"], detail)] if fresh["stale"] else []
    return {
        **fresh,
        "stale_after_days": stale_after_days,
        "warning": build_warning(stale, ending, stale_after_days),
        "rules": rules,
        "ending_soon_days": ending_soon_days,
        "ending_soon": [vars(e) for e in ending],
    }


def _norm(s: str) -> str:
    return s.strip().lower().replace("-", " ")


def find_card(query: str, cards: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    """Match a card by id, full name, or alias (case-insensitive)."""
    cards = cards if cards is not None else load_cards()
    q = _norm(query)
    for card in cards:
        names = [card["id"], card["name"], *card.get("aliases", [])]
        if q in {_norm(n) for n in names}:
            return card
    return None


def resolve_category(category: str | None, merchant: str) -> tuple[str, bool, str]:
    """Return (category, guessed, note). guessed=True when the merchant map was used."""
    if category:
        key = category.strip().lower().replace(" ", "_").replace("-", "_")
        key = CATEGORY_ALIASES.get(key, key)
        if key in KNOWN_CATEGORIES:
            return key, False, ""
    m = merchant.strip().lower()
    for needle, cat in MERCHANT_CATEGORIES.items():
        if needle in m:
            given = f"'{category}' is not a known category" if category else "No category given"
            return cat, True, f"{given}; guessed '{cat}' from merchant '{merchant}'."
    given = f"'{category}' is not a known category" if category else "No category given"
    return "other", True, f"{given} and merchant '{merchant}' is not in the map; used base rates."


def compute_reward(
    card: dict[str, Any],
    category: str,
    amount: float,
    spent_so_far: float = 0.0,
    cents: float | None = None,
    today: date | None = None,
) -> CardReward:
    """Dollar value of rewards for one purchase.

    reward = amount * rate * cents_per_point / 100. cents defaults to the card's
    points program default (1.0 for cash back). If the category has a cap,
    only the spend left under the cap (cap - spent_so_far) earns the bonus rate;
    the rest earns rate_after. Expired and not-yet-started rules are skipped (see applicable_rule).
    """
    if amount <= 0:
        raise ValueError("amount must be greater than 0")
    kind, rule = applicable_rule(card, category, today or _today())
    base = card["base_rate"]
    rate = rule["rate"] if rule else base
    program = card["points_program"]
    if program == "cash" or cents is None:
        cents = load_programs()[program]["default_cents"]

    cap_applied = False
    if rule and "cap" in rule:
        cap = rule["cap"]
        room = max(0.0, cap["amount"] - spent_so_far)
        bonus_spend = min(amount, room)
        rest = amount - bonus_spend
        points = bonus_spend * rate + rest * cap.get("rate_after", base)
        cap_applied = rest > 0
    else:
        points = amount * rate

    return CardReward(
        card=card["name"],
        card_id=card["id"],
        rate=rate,
        reward_usd=round(points * cents / 100, 2),
        cap_applied=cap_applied,
        points_program=program,
        points=None if program == "cash" else round(points, 2),
        cents_per_point=None if program == "cash" else cents,
        rule=AppliedRule(
            kind=kind,
            category=category,
            rate=rate,
            valid_from=rule.get("valid_from") if rule else None,
            valid_until=rule.get("valid_until") if rule else None,
            description=rule.get("description") if rule else None,
        ),
    )


def select_wallet(wallet: list[str] | None) -> tuple[list[dict[str, Any]], list[str]]:
    """Return (cards, unknown_names). None means every card in the data file."""
    cards = load_cards()
    if wallet is None:
        return cards, []
    if len(wallet) == 0:
        raise ValueError("wallet is empty; pass at least one card name, or omit wallet to compare all cards")
    chosen, unknown = [], []
    for name in wallet:
        card = find_card(name, cards)
        if card is None:
            unknown.append(name)
        elif card not in chosen:
            chosen.append(card)
    if not chosen:
        raise ValueError(f"none of the wallet cards are known: {unknown}. Call list_cards to see valid names.")
    return chosen, unknown


def _earn_phrase(r: CardReward, category: str) -> str:
    if r.points is None:
        return f"{r.rate:g}% back on {category}"
    return f"{r.rate:g}x points on {category} (valued at {r.cents_per_point:g}\u00a2 each)"


def _break_even_note(best: CardReward, runner_up: CardReward | None) -> str | None:
    """Say at what point value the top two would swap, when a points value decides it."""
    if runner_up is None:
        return None
    names = {k: p["name"] for k, p in load_programs().items()}
    if runner_up.points:
        x = best.reward_usd * 100 / runner_up.points
        return (
            f"{runner_up.card} would win if {names[runner_up.points_program]} points were worth "
            f"more than {x:.2f}\u00a2 (assumed {runner_up.cents_per_point:g}\u00a2). Pass point_values to use your own."
        )
    if best.points:
        x = runner_up.reward_usd * 100 / best.points
        return (
            f"{best.card} stays ahead only while {names[best.points_program]} points are worth "
            f"more than {x:.2f}\u00a2 (assumed {best.cents_per_point:g}\u00a2). Pass point_values to use your own."
        )
    return None


def best_card(
    merchant: str,
    category: str | None,
    amount: float,
    wallet: list[str] | None = None,
    point_values: dict[str, float] | None = None,
    stale_after_days: int | None = None,
    ending_soon_days: int | None = None,
) -> BestCardResult:
    cards, unknown = select_wallet(wallet)
    cents = resolve_point_values(point_values)
    stale_after = resolve_stale_after_days(stale_after_days)
    ending_window = resolve_ending_soon_days(ending_soon_days)
    cat, guessed, note = resolve_category(category, merchant)
    today = _today()
    # Ties go to cash back: same dollars, but no assumption about point value.
    ranked = sorted(
        (compute_reward(c, cat, amount, cents=cents[c["points_program"]], today=today) for c in cards),
        key=lambda r: (-r.reward_usd, r.points_program != "cash", r.card),
    )
    best = ranked[0]
    runner_up = ranked[1] if len(ranked) > 1 else None
    by_id = {c["id"]: c for c in cards}
    recommended = [r for r in (best, runner_up) if r is not None]
    stale, ending = [], []
    for r in recommended:
        card = by_id[r.card_id]
        _, applied = applicable_rule(card, cat, today)
        as_of = (applied or {}).get("as_of") or card["as_of"]  # a dated rule records its own as_of
        rules = [(k, cat, rule) for k, rule in category_rules(card, cat)]
        f, detail = _freshness(card, rules, as_of, stale_after)
        r.data_as_of, r.data_age_days, r.stale, r.stale_reason = (
            f["data_as_of"], f["data_age_days"], f["stale"], f["stale_reason"])
        if r.stale:
            stale.append((card, r.stale_reason, detail))
        end = _d(r.rule.valid_until)
        if end and (end - today).days <= ending_window:
            ending.append(EndingSoon(
                card=r.card, card_id=r.card_id,
                rule=rule_label(r.rule.kind, cat, r.rule.rate, r.rule.description),
                valid_until=r.rule.valid_until, days_left=(end - today).days,
            ))

    explanation = f"{best.card} earns {_earn_phrase(best, cat)} -> ${best.reward_usd:.2f} on ${amount:.2f}"
    if runner_up:
        explanation += f"; next best is {runner_up.card} at ${runner_up.reward_usd:.2f}."
    else:
        explanation += "; it is the only card in the wallet."
    # The older date is the one a reader should trust least, so that's the one to show.
    explanation += f" Rates as of {min(r.data_as_of for r in recommended)}."

    notes = []
    if guessed:
        notes.append(note)
    if runner_up and runner_up.reward_usd == best.reward_usd:
        notes.append(f"Tie with {runner_up.card}; preferred {best.card} because "
                     + ("its value doesn't depend on a points valuation." if best.points is None else "of name order."))
    be = _break_even_note(best, runner_up)
    if be:
        notes.append(be)
    if best.cap_applied:
        notes.append(f"{best.card}'s {cat} cap was reached; spend above it earns the lower rate.")
    if unknown:
        notes.append(f"Ignored unknown wallet entries: {unknown}.")
    notes.append("Assumes no prior spend toward caps this period. Estimates only, not financial advice.")

    used = {c["points_program"] for c in cards if c["points_program"] != "cash"}
    return BestCardResult(
        merchant=merchant,
        category=cat,
        category_guessed=guessed,
        amount=amount,
        best=best,
        runner_up=runner_up,
        explanation=explanation,
        point_values_used={k: cents[k] for k in sorted(used)},
        notes=notes,
        stale_after_days=stale_after,
        warning=build_warning(stale, ending, stale_after),
        ending_soon_days=ending_window,
        ending_soon=ending,
    )


def _value_sentence(card: dict[str, Any]) -> str:
    prog = load_programs()[card["points_program"]]
    if card["points_program"] == "cash":
        return ". Cash back."
    return f". Earns {prog['name']} points, valued by default at {prog['default_cents']:g}\u00a2 each (adjustable)."


def describe_card(card: dict[str, Any]) -> str:
    """One readable paragraph of a card's rules."""
    parts = []
    for kind, cat, rule in all_rules(card):
        s = f"{rule['rate']:g}x {cat}" + (" promotion" if kind == "promotion" else "")
        if "cap" in rule:
            c = rule["cap"]
            s += f" (first ${c['amount']:,} per {c['period'].replace('_', ' ')}, then {c.get('rate_after', card['base_rate']):g}x)"
        if rule.get("valid_from") or rule.get("valid_until"):
            s += f" [{rule.get('valid_from') or '…'} to {rule.get('valid_until') or '…'}]"
        parts.append(s)
    parts.append(f"{card['base_rate']:g}x everything else")
    return (
        f"{card['name']} ({card['issuer']}, ${card['annual_fee']} annual fee): "
        + ", ".join(parts)
        + _value_sentence(card)
    )
