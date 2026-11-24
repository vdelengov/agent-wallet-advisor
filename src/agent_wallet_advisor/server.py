"""MCP server: thin tool wrappers around rewards.py."""

from __future__ import annotations

from typing import Any

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from agent_wallet_advisor import rewards

mcp = MCPServer("agent-wallet-advisor")


@mcp.tool()
def best_card(
    merchant: str,
    category: str,
    amount: float,
    wallet: list[str] | None = None,
    point_values: dict[str, float] | None = None,
    stale_after_days: int | None = None,
    ending_soon_days: int | None = None,
) -> rewards.BestCardResult:
    """Pick the card that earns the most rewards for a purchase. Call this before paying.

    Args:
        merchant: Merchant name, e.g. "Whole Foods".
        category: One of dining, groceries, travel, gas, online_shopping, streaming,
            drugstores, entertainment, transit, other. If unknown or empty, the category
            is guessed from the merchant and the result says so.
        amount: Purchase amount in US dollars.
        wallet: Card names the user holds (see list_cards). Omit to compare all cards.
        point_values: Optional cents-per-point the user assigns to a points program, e.g.
            {"amex_membership_rewards": 1.8}. Keys: chase_ultimate_rewards,
            amex_membership_rewards, citi_thankyou. Defaults are 1.0. The ranking can
            change with these values, and the result says where the break-even is.
        stale_after_days: Optional age in days after which card data counts as stale
            (default 90). A rule is also stale once its valid_until has passed. The best and
            runner-up cards report data_as_of, data_age_days, stale and stale_reason.
        ending_soon_days: Optional window in days (default 30). If the rule behind the best or
            runner-up card ends within it, the result lists it under ending_soon.
        `warning` is set when either recommended card is stale or its rule ends soon. Expired
        and not-yet-started rules are skipped automatically; `rule` on each card shows which
        rule (promotion, category or base rate) set the rate.
    """
    try:
        return rewards.best_card(merchant, category, amount, wallet, point_values, stale_after_days, ending_soon_days)
    except ValueError as e:
        # ToolError sends the message to the agent; other exceptions are masked by the SDK.
        raise ToolError(str(e)) from e


@mcp.tool()
def list_cards() -> list[dict[str, Any]]:
    """List supported cards with their bonus categories and annual fees."""
    return [
        {
            "id": c["id"],
            "name": c["name"],
            "issuer": c["issuer"],
            "annual_fee": c["annual_fee"],
            "base_rate": c["base_rate"],
            "points_program": c["points_program"],
            "bonus_categories": {cat: rule["rate"] for cat, rule in c["categories"].items()},
            "needs_verification": c["needs_verification"],
        }
        for c in rewards.load_cards()
    ]


@mcp.tool()
def explain_rule(
    card: str, stale_after_days: int | None = None, ending_soon_days: int | None = None
) -> dict[str, Any]:
    """Explain one card's reward rules: rates, caps, dates, point value, fee, what isn't modeled, and data freshness.

    Args:
        card: Card name, id, or alias (e.g. "Amex Gold").
        stale_after_days: Optional age in days after which card data counts as stale (default 90).
        ending_soon_days: Optional window in days for marking a rule as ending soon (default 30).

    `rules` lists every category rule and promotion with valid_from, valid_until and a status:
    active, ending_soon, upcoming or expired.
    """
    found = rewards.find_card(card)
    if found is None:
        names = ", ".join(c["name"] for c in rewards.load_cards())
        raise ToolError(f"Unknown card '{card}'. Known cards: {names}")
    try:
        values = rewards.resolve_point_values()
        stale_after = rewards.resolve_stale_after_days(stale_after_days)
        ending_window = rewards.resolve_ending_soon_days(ending_soon_days)
    except ValueError as e:
        raise ToolError(str(e)) from e
    program = {**rewards.load_programs()[found["points_program"]], "current_cents": values[found["points_program"]]}
    return {
        "summary": rewards.describe_card(found),
        **found,
        "points_program_details": program,
        **rewards.rule_report(found, stale_after, ending_window),
    }


def main() -> None:
    mcp.run()  # stdio transport


if __name__ == "__main__":
    main()
