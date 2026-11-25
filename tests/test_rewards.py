import copy
from datetime import date, timedelta

import pytest

from agent_wallet_advisor import rewards
from agent_wallet_advisor.rewards import best_card, compute_reward, find_card, resolve_category


def card(name):
    c = find_card(name)
    assert c is not None, name
    return c


# --- reward math -----------------------------------------------------------

def test_flat_rate_card():
    r = compute_reward(card("Citi Double Cash"), "groceries", 100)
    assert r.rate == 2
    assert r.reward_usd == 2.00


def test_category_multiplier():
    r = compute_reward(card("Amex Gold"), "dining", 50)
    assert r.rate == 4
    assert r.reward_usd == 2.00  # 50 * 4 points * 1 cent


def test_category_not_on_card_uses_base_rate():
    r = compute_reward(card("Chase Freedom Unlimited"), "gas", 40)
    assert r.rate == 1.5
    assert r.reward_usd == 0.60


def test_point_value_scales_reward():
    r = compute_reward(card("Chase Sapphire Preferred"), "dining", 100, cents=1.5)
    assert r.reward_usd == 4.50  # 100 * 3 points * 1.5c
    assert r.points == 300 and r.cents_per_point == 1.5


def test_cash_back_ignores_point_value():
    r = compute_reward(card("Active Cash"), "other", 100, cents=3.0)
    assert r.reward_usd == 2.00
    assert r.points is None


def test_non_positive_amount_rejected():
    with pytest.raises(ValueError):
        compute_reward(card("Citi Double Cash"), "other", 0)


# --- caps -----------------------------------------------------------------

def test_purchase_under_cap():
    r = compute_reward(card("Blue Cash Preferred"), "groceries", 100)
    assert r.reward_usd == 6.00
    assert not r.cap_applied


def test_purchase_crossing_cap():
    # Cap is $6,000 at 6%, then 1%. $7,000 -> 6000*6% + 1000*1% = 360 + 10.
    r = compute_reward(card("Blue Cash Preferred"), "groceries", 7000)
    assert r.reward_usd == 370.00
    assert r.cap_applied


def test_prior_spend_counts_toward_cap():
    # $5,900 already spent leaves $100 at 6%; the other $100 earns 1%.
    r = compute_reward(card("Blue Cash Preferred"), "groceries", 200, spent_so_far=5900)
    assert r.reward_usd == 7.00
    assert r.cap_applied


def test_cap_already_exhausted():
    r = compute_reward(card("Amex Gold"), "groceries", 100, spent_so_far=30000)
    assert r.reward_usd == 1.00


def test_cap_changes_the_winner():
    # Small grocery run: BCP 6% wins. Huge one: BCP is capped and Amex Gold 4x wins.
    wallet = ["Blue Cash Preferred", "Amex Gold"]
    assert best_card("Kroger", "groceries", 100, wallet).best.card_id == "amex-blue-cash-preferred"
    big = best_card("Kroger", "groceries", 20000, wallet)
    assert big.best.card_id == "amex-gold"  # 20000*4% = 800 vs 360 + 140 = 500
    assert big.runner_up.cap_applied


# --- best_card ------------------------------------------------------------

def test_whole_foods_example():
    r = best_card("Whole Foods", "groceries", 120)
    assert r.best.card_id == "amex-blue-cash-preferred"
    assert r.best.reward_usd == 7.20
    assert r.runner_up.card_id == "amex-gold"
    assert r.runner_up.reward_usd == 4.80
    assert not r.category_guessed
    assert "Blue Cash Preferred" in r.explanation


def test_wallet_restricts_choices():
    r = best_card("Chipotle", "dining", 30, ["Double Cash", "Active Cash"])
    assert {r.best.card_id, r.runner_up.card_id} == {"citi-double-cash", "wells-fargo-active-cash"}
    # Tie at $0.60: Active Cash wins because Double Cash pays in ThankYou points,
    # and ties go to the card whose value doesn't depend on a points valuation.
    assert r.best.card_id == "wells-fargo-active-cash"


def test_single_card_wallet_has_no_runner_up():
    r = best_card("Shell", "gas", 50, ["Active Cash"])
    assert r.runner_up is None
    assert "only card" in r.explanation


def test_empty_wallet_raises():
    with pytest.raises(ValueError, match="wallet is empty"):
        best_card("Whole Foods", "groceries", 120, [])


def test_wallet_with_only_unknown_cards_raises():
    with pytest.raises(ValueError, match="none of the wallet cards"):
        best_card("Whole Foods", "groceries", 120, ["Discover It"])


def test_wallet_with_some_unknown_cards_notes_them():
    r = best_card("Whole Foods", "groceries", 120, ["Amex Gold", "Discover It"])
    assert r.best.card_id == "amex-gold"
    assert any("Discover It" in n for n in r.notes)


def test_card_names_are_case_insensitive():
    assert find_card("AMEX GOLD")["id"] == "amex-gold"
    assert find_card("chase-sapphire-preferred")["id"] == "chase-sapphire-preferred"


# --- unknown category -----------------------------------------------------

def test_category_alias():
    assert resolve_category("Restaurants", "anywhere") == ("dining", False, "")


def test_unknown_category_guesses_from_merchant():
    r = best_card("Whole Foods Market #123", "stuff", 120)
    assert r.category == "groceries"
    assert r.category_guessed
    assert any("guessed 'groceries'" in n for n in r.notes)


def test_missing_category_guesses_from_merchant():
    cat, guessed, note = resolve_category("", "Starbucks")
    assert (cat, guessed) == ("dining", True)
    assert "No category given" in note


def test_uber_eats_is_dining_not_transit():
    assert resolve_category(None, "Uber Eats")[0] == "dining"
    assert resolve_category(None, "Uber")[0] == "transit"


def test_unknown_category_and_merchant_uses_base_rates():
    r = best_card("Bob's Hardware", "widgets", 100)
    assert r.category == "other"
    assert r.category_guessed
    assert r.best.reward_usd == 2.00  # best flat 2% card


# --- point values are a setting ---------------------------------------------

MR = "amex_membership_rewards"


@pytest.fixture(autouse=True)
def no_env_point_values(monkeypatch):
    monkeypatch.delenv(rewards.POINT_VALUES_ENV, raising=False)


def test_default_point_value_note_gives_break_even():
    r = best_card("Whole Foods", "groceries", 120)
    assert r.point_values_used[MR] == 1.0
    # Gold earns 480 points; BCP pays $7.20, so Gold needs 1.5c/point to tie.
    assert any("would win if American Express Membership Rewards points were worth more than 1.50" in n for n in r.notes)


@pytest.mark.parametrize("cents, winner", [(1.0, "amex-blue-cash-preferred"), (1.5, "amex-blue-cash-preferred"), (2.0, "amex-gold")])
def test_point_value_changes_the_whole_foods_winner(cents, winner):
    r = best_card("Whole Foods", "groceries", 120, point_values={MR: cents})
    assert r.best.card_id == winner


def test_tie_goes_to_cash_back():
    r = best_card("Whole Foods", "groceries", 120, ["Amex Gold", "Blue Cash Preferred"], {MR: 1.5})
    assert r.best.reward_usd == r.runner_up.reward_usd == 7.20
    assert r.best.card_id == "amex-blue-cash-preferred"
    assert any("Tie with" in n for n in r.notes)


def test_gold_at_two_cents_reports_its_floor():
    r = best_card("Whole Foods", "groceries", 120, point_values={MR: 2.0})
    assert r.best.reward_usd == 9.60
    assert any("stays ahead only while American Express Membership Rewards points are worth more than 1.50" in n for n in r.notes)


def test_env_var_sets_server_default_and_call_overrides_it(monkeypatch):
    monkeypatch.setenv(rewards.POINT_VALUES_ENV, '{"amex_membership_rewards": 2.0}')
    assert best_card("Whole Foods", "groceries", 120).best.card_id == "amex-gold"
    assert best_card("Whole Foods", "groceries", 120, point_values={MR: 1.0}).best.card_id == "amex-blue-cash-preferred"


@pytest.mark.parametrize("values, msg", [
    ({"amex_mr": 1.5}, "unknown points program"),
    ({"cash": 1.5}, "can't be overridden"),
    ({MR: 0}, "between 0 and 5"),
    ({MR: 12}, "between 0 and 5"),
])
def test_invalid_point_values_rejected(values, msg):
    with pytest.raises(ValueError, match=msg):
        best_card("Whole Foods", "groceries", 120, point_values=values)


def test_bad_env_var_rejected(monkeypatch):
    monkeypatch.setenv(rewards.POINT_VALUES_ENV, "not json")
    with pytest.raises(ValueError, match="not valid JSON"):
        best_card("Whole Foods", "groceries", 120)


def test_sapphire_preferred_earns_3x_gas():
    # Added by Chase recently; caught while checking the issuer page.
    assert compute_reward(card("Sapphire Preferred"), "gas", 100).rate == 3


def test_data_loads_from_package():
    rewards.load_data.cache_clear()
    assert len(rewards.load_cards()) >= 5


# --- data freshness -----------------------------------------------------------

AS_OF = date(2026, 11, 24)  # every card's as_of in cards.json


@pytest.fixture(autouse=True)
def no_env_stale_after(monkeypatch):
    monkeypatch.delenv(rewards.STALE_AFTER_ENV, raising=False)


def pin_today(monkeypatch, days_after_as_of):
    monkeypatch.setattr(rewards, "_today", lambda: AS_OF + timedelta(days=days_after_as_of))


def test_fresh_data_reports_age_and_no_warning(monkeypatch):
    pin_today(monkeypatch, 10)
    r = best_card("Whole Foods", "groceries", 120)
    assert (r.best.data_as_of, r.best.data_age_days, r.best.stale) == ("2026-11-24", 10, False)
    assert r.runner_up.data_age_days == 10 and not r.runner_up.stale
    assert r.stale_after_days == 90
    assert r.warning is None


@pytest.mark.parametrize("days, stale", [(90, False), (91, True)])
def test_stale_means_older_than_the_setting(monkeypatch, days, stale):
    pin_today(monkeypatch, days)
    r = best_card("Whole Foods", "groceries", 120)
    assert r.best.stale is stale
    assert (r.warning is not None) is stale


def test_warning_names_recommended_cards_and_where_to_check(monkeypatch):
    pin_today(monkeypatch, 120)
    r = best_card("Whole Foods", "groceries", 120, ["Blue Cash Preferred", "Amex Gold", "Double Cash"])
    assert r.best.stale and r.runner_up.stale
    assert "older than 90 days" in r.warning
    assert "Blue Cash Preferred Card from American Express (as of 2026-11-24, 120 days old" in r.warning
    assert "American Express Gold Card" in r.warning
    assert "https://www.americanexpress.com/us/credit-cards/card/blue-cash-preferred/" in r.warning
    assert "Double Cash" not in r.warning  # third place isn't recommended


def test_warning_covers_only_the_stale_card(monkeypatch):
    cards = copy.deepcopy(rewards.load_cards())
    next(c for c in cards if c["id"] == "amex-gold")["as_of"] = "2026-06-01"
    monkeypatch.setattr(rewards, "load_cards", lambda: cards)
    pin_today(monkeypatch, 10)
    r = best_card("Whole Foods", "groceries", 120, ["Blue Cash Preferred", "Amex Gold"])
    assert not r.best.stale
    assert r.runner_up.stale and r.runner_up.data_as_of == "2026-06-01" and r.runner_up.data_age_days == 186
    assert "American Express Gold Card" in r.warning
    assert "Blue Cash Preferred" not in r.warning


def test_single_card_wallet_warns_about_that_card(monkeypatch):
    pin_today(monkeypatch, 200)
    r = best_card("Whole Foods", "groceries", 120, ["Double Cash"])
    assert r.runner_up is None
    assert "Citi Double Cash" in r.warning


def test_future_as_of_counts_as_fresh(monkeypatch):
    pin_today(monkeypatch, -5)
    r = best_card("Whole Foods", "groceries", 120)
    assert r.best.data_age_days == 0 and not r.best.stale


def test_stale_env_var_sets_server_default_and_call_overrides_it(monkeypatch):
    pin_today(monkeypatch, 45)
    monkeypatch.setenv(rewards.STALE_AFTER_ENV, "30")
    r = best_card("Whole Foods", "groceries", 120)
    assert r.stale_after_days == 30 and r.best.stale and r.warning
    r = best_card("Whole Foods", "groceries", 120, stale_after_days=60)
    assert r.stale_after_days == 60 and not r.best.stale and r.warning is None


@pytest.mark.parametrize("value", [0, -1, 3651, True, 1.5, "90"])
def test_invalid_stale_after_days_rejected(value):
    with pytest.raises(ValueError, match="whole number of days"):
        best_card("Whole Foods", "groceries", 120, stale_after_days=value)


@pytest.mark.parametrize("env", ["ninety", "0", "1.5"])
def test_bad_stale_env_var_rejected(monkeypatch, env):
    monkeypatch.setenv(rewards.STALE_AFTER_ENV, env)
    with pytest.raises(ValueError, match=rewards.STALE_AFTER_ENV):
        best_card("Whole Foods", "groceries", 120)


# --- dated rules: promotions, expiry, ending soon -------------------------------

GOLD_URL = "https://www.americanexpress.com/us/credit-cards/card/gold-card/"


@pytest.fixture(autouse=True)
def no_env_ending_soon(monkeypatch):
    monkeypatch.delenv(rewards.ENDING_SOON_ENV, raising=False)


def pin_date(monkeypatch, d):
    monkeypatch.setattr(rewards, "_today", lambda: d)


def promo(**kw):
    """A synthetic issuer promotion (tests only; real data has none yet)."""
    return {"category": "groceries", "rate": 10, "valid_from": None, "valid_until": "2026-12-31",
            "description": "test offer", "source_url": GOLD_URL, "as_of": "2026-11-24", **kw}


def patch_cards(monkeypatch, card_id, promotions=(), **category_changes):
    """Copy the real cards, add promotions to one, and change its category rules."""
    cards = copy.deepcopy(rewards.load_cards())
    card = next(c for c in cards if c["id"] == card_id)
    card["promotions"] = list(promotions)
    for cat, changes in category_changes.items():
        card["categories"][cat].update(changes)
    monkeypatch.setattr(rewards, "load_cards", lambda: cards)
    return card


GOLD_VS_BCP = ["Amex Gold", "Blue Cash Preferred"]


def test_whole_foods_demo_unchanged_today(monkeypatch):
    pin_date(monkeypatch, AS_OF)
    r = best_card("Whole Foods", "groceries", 120)
    assert (r.best.card_id, r.best.reward_usd) == ("amex-blue-cash-preferred", 7.20)
    assert (r.runner_up.card_id, r.runner_up.reward_usd) == ("amex-gold", 4.80)
    assert r.warning is None and r.ending_soon == []
    assert r.best.rule.kind == r.runner_up.rule.kind == "category"
    assert r.best.rule.valid_until is None and r.best.stale_reason is None
    assert r.ending_soon_days == 30


def test_active_promo_applies_and_records_its_rule(monkeypatch):
    patch_cards(monkeypatch, "amex-gold", [promo(as_of="2026-11-20")])
    pin_date(monkeypatch, date(2026, 11, 24))
    r = best_card("Whole Foods", "groceries", 120, GOLD_VS_BCP)
    assert r.best.card_id == "amex-gold" and r.best.reward_usd == 12.00
    assert r.best.rule.kind == "promotion" and r.best.rule.valid_until == "2026-12-31"
    assert r.best.data_as_of == "2026-11-20"  # the promotion's own as_of


def test_expired_promo_is_skipped_and_falls_back_to_category_rule(monkeypatch):
    gold = patch_cards(monkeypatch, "amex-gold", [promo()])
    reward = compute_reward(gold, "groceries", 100, today=date(2027, 1, 1))
    assert reward.rate == 4 and reward.rule.kind == "category"


def test_expired_category_rule_falls_back_to_base_rate(monkeypatch):
    bcp = patch_cards(monkeypatch, "amex-blue-cash-preferred",
                      groceries={"valid_until": "2026-12-31", "source_url": GOLD_URL, "as_of": "2026-11-24"})
    assert compute_reward(bcp, "groceries", 100, today=date(2026, 12, 31)).rate == 6  # last day still counts
    after = compute_reward(bcp, "groceries", 100, today=date(2027, 1, 1))
    assert after.rate == 1 and after.rule.kind == "base_rate"


def test_upcoming_promo_is_ignored_until_it_starts(monkeypatch):
    gold = patch_cards(monkeypatch, "amex-gold", [promo(valid_from="2027-01-01", valid_until="2027-03-31")])
    assert compute_reward(gold, "groceries", 100, today=date(2026, 12, 31)).rule.kind == "category"
    assert compute_reward(gold, "groceries", 100, today=date(2027, 1, 1)).rate == 10  # start is inclusive


def test_promo_changes_the_winner_only_while_active(monkeypatch):
    patch_cards(monkeypatch, "amex-gold", [promo()])
    pin_date(monkeypatch, date(2026, 12, 15))
    assert best_card("Whole Foods", "groceries", 120, GOLD_VS_BCP).best.card_id == "amex-gold"
    pin_date(monkeypatch, date(2027, 1, 1))
    r = best_card("Whole Foods", "groceries", 120, GOLD_VS_BCP)
    assert (r.best.card_id, r.best.reward_usd) == ("amex-blue-cash-preferred", 7.20)
    assert r.runner_up.reward_usd == 4.80 and r.runner_up.rule.kind == "category"


@pytest.mark.parametrize("days_left, listed", [(30, True), (31, False), (0, True)])
def test_ending_soon_boundary(monkeypatch, days_left, listed):
    patch_cards(monkeypatch, "amex-gold", [promo()])
    pin_date(monkeypatch, date(2026, 12, 31) - timedelta(days=days_left))
    r = best_card("Whole Foods", "groceries", 120, GOLD_VS_BCP)
    if listed:
        [e] = r.ending_soon
        assert (e.card_id, e.valid_until, e.days_left) == ("amex-gold", "2026-12-31", days_left)
        assert e.rule == "10x groceries promotion (test offer)"
        assert "Ending soon: American Express Gold Card's 10x groceries promotion (test offer) ends 2026-12-31" in r.warning
    else:
        assert r.ending_soon == [] and r.warning is None


def test_ending_soon_ignores_cards_outside_the_top_two(monkeypatch):
    # 2.5x groceries ($3.00) ending in 7 days, but Wells Fargo stays third behind BCP and Gold.
    patch_cards(monkeypatch, "wells-fargo-active-cash", [promo(rate=2.5, valid_until="2026-12-01")])
    pin_date(monkeypatch, AS_OF)
    r = best_card("Whole Foods", "groceries", 120, ["Blue Cash Preferred", "Amex Gold", "Active Cash"])
    assert r.runner_up.card_id == "amex-gold"
    assert r.ending_soon == [] and r.warning is None


def test_stale_by_valid_until(monkeypatch):
    patch_cards(monkeypatch, "amex-gold", [promo()])
    pin_date(monkeypatch, date(2027, 1, 5))  # 42 days after as_of: fresh by age
    r = best_card("Whole Foods", "groceries", 120, GOLD_VS_BCP)
    gold = r.runner_up
    assert gold.stale and gold.stale_reason == "valid_until"
    assert not r.best.stale and r.best.stale_reason is None
    assert "past a rule's end date: American Express Gold Card (10x groceries promotion (test offer) ended 2026-12-31" in r.warning
    assert "older than" not in r.warning


def test_stale_by_age(monkeypatch):
    pin_date(monkeypatch, AS_OF + timedelta(days=91))
    r = best_card("Whole Foods", "groceries", 120)
    assert r.best.stale_reason == r.runner_up.stale_reason == "age"
    assert "older than 90 days" in r.warning


@pytest.mark.parametrize("valid_until, today, reason", [
    ("2026-12-31", date(2027, 3, 1), "valid_until"),  # the end date came before the 90-day mark (2027-02-22)
    ("2027-02-28", date(2027, 3, 1), "age"),          # the 90-day mark came first
])
def test_stale_reports_whichever_came_first(monkeypatch, valid_until, today, reason):
    patch_cards(monkeypatch, "amex-gold", [promo(valid_until=valid_until)])
    pin_date(monkeypatch, today)
    r = best_card("Whole Foods", "groceries", 120, GOLD_VS_BCP)
    gold = r.best if r.best.card_id == "amex-gold" else r.runner_up
    assert gold.stale and gold.stale_reason == reason


def test_ending_soon_env_var_and_call_override(monkeypatch):
    patch_cards(monkeypatch, "amex-gold", [promo()])
    pin_date(monkeypatch, date(2026, 12, 11))  # 20 days left
    monkeypatch.setenv(rewards.ENDING_SOON_ENV, "10")
    r = best_card("Whole Foods", "groceries", 120, GOLD_VS_BCP)
    assert r.ending_soon_days == 10 and r.ending_soon == []
    r = best_card("Whole Foods", "groceries", 120, GOLD_VS_BCP, ending_soon_days=25)
    assert r.ending_soon_days == 25 and r.ending_soon[0].days_left == 20


@pytest.mark.parametrize("value", [0, -1, 3651, True, 1.5, "30"])
def test_invalid_ending_soon_days_rejected(value):
    with pytest.raises(ValueError, match="ending_soon_days must be a whole number of days"):
        best_card("Whole Foods", "groceries", 120, ending_soon_days=value)


@pytest.mark.parametrize("env", ["soon", "0", "4000"])
def test_bad_ending_soon_env_var_rejected(monkeypatch, env):
    monkeypatch.setenv(rewards.ENDING_SOON_ENV, env)
    with pytest.raises(ValueError, match=rewards.ENDING_SOON_ENV):
        best_card("Whole Foods", "groceries", 120)


# --- data date in the explanation -------------------------------------------------

def test_explanation_ends_with_the_data_date(monkeypatch):
    pin_date(monkeypatch, AS_OF)
    r = best_card("Whole Foods", "groceries", 120)
    assert r.explanation == (
        "Blue Cash Preferred Card from American Express earns 6% back on groceries -> $7.20 on $120.00; "
        "next best is American Express Gold Card at $4.80. Rates as of 2026-11-24."
    )


def test_explanation_uses_the_older_of_best_and_runner_up_dates(monkeypatch):
    cards = copy.deepcopy(rewards.load_cards())
    next(c for c in cards if c["id"] == "amex-gold")["as_of"] = "2026-10-01"
    monkeypatch.setattr(rewards, "load_cards", lambda: cards)
    pin_date(monkeypatch, AS_OF)
    r = best_card("Whole Foods", "groceries", 120, GOLD_VS_BCP)
    assert (r.best.data_as_of, r.runner_up.data_as_of) == ("2026-11-24", "2026-10-01")  # fields unchanged
    assert r.explanation.endswith(" Rates as of 2026-10-01.")


def test_explanation_date_for_a_single_card_wallet(monkeypatch):
    pin_date(monkeypatch, AS_OF)
    r = best_card("Shell", "gas", 50, ["Active Cash"])
    assert r.explanation.endswith("it is the only card in the wallet. Rates as of 2026-11-24.")


def test_explanation_date_comes_from_the_promotion_when_one_applies(monkeypatch):
    patch_cards(monkeypatch, "amex-gold", [promo(as_of="2026-11-20")])
    pin_date(monkeypatch, AS_OF)
    r = best_card("Whole Foods", "groceries", 120, GOLD_VS_BCP)
    assert r.best.rule.kind == "promotion"
    assert r.explanation.endswith(" Rates as of 2026-11-20.")
