"""End-to-end through the MCP protocol, using the SDK's in-process client."""

import copy
from datetime import date, timedelta

import anyio
import pytest
from mcp import Client

from agent_wallet_advisor import rewards
from agent_wallet_advisor.server import mcp


def call(name, args):
    async def go():
        async with Client(mcp) as client:
            return await client.call_tool(name, args)

    return anyio.run(go)


def test_tools_are_registered():
    async def go():
        async with Client(mcp) as client:
            return {t.name for t in (await client.list_tools()).tools}

    assert anyio.run(go) == {"best_card", "list_cards", "explain_rule"}


def test_best_card_returns_structured_result():
    r = call("best_card", {"merchant": "Whole Foods", "category": "groceries", "amount": 120})
    assert not r.is_error
    out = r.structured_content
    assert out["best"]["card"] == "Blue Cash Preferred Card from American Express"
    assert out["best"]["reward_usd"] == 7.20
    assert out["runner_up"]["reward_usd"] == 4.80


def test_empty_wallet_is_a_tool_error():
    r = call("best_card", {"merchant": "Whole Foods", "category": "groceries", "amount": 120, "wallet": []})
    assert r.is_error
    assert "wallet is empty" in r.content[0].text


def test_point_values_through_mcp():
    r = call("best_card", {"merchant": "Whole Foods", "category": "groceries", "amount": 120,
                           "point_values": {"amex_membership_rewards": 2.0}})
    assert r.structured_content["best"]["card_id"] == "amex-gold"
    assert r.structured_content["point_values_used"]["amex_membership_rewards"] == 2.0


def test_bad_point_values_is_a_tool_error():
    r = call("best_card", {"merchant": "Whole Foods", "category": "groceries", "amount": 120,
                           "point_values": {"cash": 2.0}})
    assert r.is_error and "can't be overridden" in r.content[0].text


def test_list_cards():
    r = call("list_cards", {})
    assert len(r.structured_content["result"]) >= 5


@pytest.mark.parametrize("name, ok", [("Amex Gold", True), ("Discover It", False)])
def test_explain_rule(name, ok):
    r = call("explain_rule", {"card": name})
    assert r.is_error is not ok
    if ok:
        assert "4x dining" in r.structured_content["summary"]


# --- data freshness -----------------------------------------------------------

AS_OF = date(2026, 11, 24)
WHOLE_FOODS = {"merchant": "Whole Foods", "category": "groceries", "amount": 120}


@pytest.fixture
def days_after_as_of(monkeypatch):
    monkeypatch.delenv(rewards.STALE_AFTER_ENV, raising=False)
    return lambda days: monkeypatch.setattr(rewards, "_today", lambda: AS_OF + timedelta(days=days))


def test_best_card_reports_freshness(days_after_as_of):
    days_after_as_of(5)
    out = call("best_card", WHOLE_FOODS).structured_content
    for pick in (out["best"], out["runner_up"]):
        assert (pick["data_as_of"], pick["data_age_days"], pick["stale"]) == ("2026-11-24", 5, False)
    assert out["stale_after_days"] == 90
    assert out["warning"] is None


def test_best_card_stale_warning_and_per_call_setting(days_after_as_of):
    days_after_as_of(120)
    out = call("best_card", WHOLE_FOODS).structured_content
    assert out["best"]["stale"] and "older than 90 days" in out["warning"]
    out = call("best_card", {**WHOLE_FOODS, "stale_after_days": 365}).structured_content
    assert not out["best"]["stale"] and out["warning"] is None


def test_explain_rule_reports_freshness(days_after_as_of):
    days_after_as_of(100)
    out = call("explain_rule", {"card": "Amex Gold"}).structured_content
    assert (out["data_as_of"], out["data_age_days"], out["stale"]) == ("2026-11-24", 100, True)
    assert "American Express Gold Card" in out["warning"]
    out = call("explain_rule", {"card": "Amex Gold", "stale_after_days": 365}).structured_content
    assert not out["stale"] and out["warning"] is None


@pytest.mark.parametrize("tool, args", [("best_card", WHOLE_FOODS), ("explain_rule", {"card": "Amex Gold"})])
def test_bad_stale_after_days_is_a_tool_error(tool, args):
    r = call(tool, {**args, "stale_after_days": 0})
    assert r.is_error and "whole number of days" in r.content[0].text


# --- dated rules over MCP -------------------------------------------------------

def gold_with_promotions(monkeypatch, promotions):
    cards = copy.deepcopy(rewards.load_cards())
    next(c for c in cards if c["id"] == "amex-gold")["promotions"] = promotions
    monkeypatch.setattr(rewards, "load_cards", lambda: cards)


def promo(**kw):
    return {"category": "groceries", "rate": 10, "valid_from": None, "valid_until": "2026-12-31",
            "description": "test offer", "source_url": "https://www.americanexpress.com/us/credit-cards/card/gold-card/",
            "as_of": "2026-11-24", **kw}


@pytest.fixture
def pinned(monkeypatch):
    for env in (rewards.STALE_AFTER_ENV, rewards.ENDING_SOON_ENV):
        monkeypatch.delenv(env, raising=False)
    return lambda d: monkeypatch.setattr(rewards, "_today", lambda: d)


def test_whole_foods_demo_unchanged_over_mcp(pinned):
    pinned(AS_OF)
    out = call("best_card", WHOLE_FOODS).structured_content
    assert (out["best"]["card_id"], out["best"]["reward_usd"]) == ("amex-blue-cash-preferred", 7.20)
    assert (out["runner_up"]["card_id"], out["runner_up"]["reward_usd"]) == ("amex-gold", 4.80)
    assert out["warning"] is None and out["ending_soon"] == [] and out["ending_soon_days"] == 30
    assert out["best"]["rule"] == {"kind": "category", "category": "groceries", "rate": 6,
                                   "valid_from": None, "valid_until": None, "description": None}
    assert out["best"]["stale_reason"] is None
    assert out["explanation"].endswith("next best is American Express Gold Card at $4.80. Rates as of 2026-11-24.")


def test_best_card_ending_soon_over_mcp(monkeypatch, pinned):
    gold_with_promotions(monkeypatch, [promo()])
    pinned(date(2026, 12, 11))
    out = call("best_card", WHOLE_FOODS).structured_content
    assert out["best"]["rule"]["kind"] == "promotion"
    assert out["ending_soon"] == [{"card": "American Express Gold Card", "card_id": "amex-gold",
                                   "rule": "10x groceries promotion (test offer)",
                                   "valid_until": "2026-12-31", "days_left": 20}]
    assert "Ending soon" in out["warning"]


def test_ending_soon_call_beats_env_over_mcp(monkeypatch, pinned):
    gold_with_promotions(monkeypatch, [promo()])
    pinned(date(2026, 12, 11))
    monkeypatch.setenv(rewards.ENDING_SOON_ENV, "10")
    assert call("best_card", WHOLE_FOODS).structured_content["ending_soon"] == []
    out = call("best_card", {**WHOLE_FOODS, "ending_soon_days": 25}).structured_content
    assert out["ending_soon_days"] == 25 and out["ending_soon"][0]["days_left"] == 20


def test_explain_rule_marks_rule_status(monkeypatch, pinned):
    gold_with_promotions(monkeypatch, [
        promo(description="ended", valid_until="2026-12-01"),
        promo(description="soon", valid_until="2026-12-30"),
        promo(description="later", valid_from="2027-02-01", valid_until="2027-04-30"),
        promo(description="running", category="dining", valid_until="2027-06-30"),
    ])
    pinned(date(2026, 12, 10))
    out = call("explain_rule", {"card": "Amex Gold"}).structured_content
    status = {r["label"]: (r["status"], r["valid_from"], r["valid_until"], r["days_left"]) for r in out["rules"]}
    assert status["4x dining"] == ("active", None, None, None)
    assert status["10x groceries promotion (ended)"] == ("expired", None, "2026-12-01", None)
    assert status["10x groceries promotion (soon)"] == ("ending_soon", None, "2026-12-30", 20)
    assert status["10x groceries promotion (later)"] == ("upcoming", "2027-02-01", "2027-04-30", 141)
    assert status["10x dining promotion (running)"] == ("active", None, "2027-06-30", 202)
    assert [e["rule"] for e in out["ending_soon"]] == ["10x groceries promotion (soon)"]
    assert out["stale"] and out["stale_reason"] == "valid_until"  # the 'ended' promotion is past its date
    assert "Ending soon" in out["warning"] and "past a rule's end date" in out["warning"]
    assert out["categories"]["dining"]["rate"] == 4  # existing fields unchanged


@pytest.mark.parametrize("tool, args", [("best_card", WHOLE_FOODS), ("explain_rule", {"card": "Amex Gold"})])
@pytest.mark.parametrize("value", [0, 3651])
def test_bad_ending_soon_days_is_a_tool_error(tool, args, value):
    r = call(tool, {**args, "ending_soon_days": value})
    assert r.is_error and "ending_soon_days must be a whole number of days" in r.content[0].text
