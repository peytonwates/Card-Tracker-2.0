"""Grade-or-hold math for raw inventory the user already tracks.

Prices are current PriceCharting / SportsCardsPro guide values. This module
does not rank a market-wide catalog and does not reproduce anyone else's score.
"""

from __future__ import annotations

from collections.abc import Iterable

import pandas as pd

from .cleaning import clean_text, to_money

SORT_COLUMNS = ("psa10_margin", "psa10_multiple", "expected_net")

SKIP_LABELS = {
    "missing_link": "No PriceCharting or SportsCardsPro link",
    "unsupported_link": "Reference link is not PriceCharting or SportsCardsPro",
    "no_psa10_price": "No PSA 10 guide price on that page",
}


def reference_status(url: str) -> str:
    text = clean_text(url).lower()
    if not text:
        return "missing_link"
    if "pricecharting.com" in text or "sportscardspro.com" in text:
        return "ok"
    return "unsupported_link"


def purchase_cost(total_price, total_cost) -> float:
    """Prefer the original purchase total. Fall back to all-in cost when purchase is blank."""
    purchase = round(to_money(total_price), 2)
    if purchase > 0:
        return purchase
    return round(to_money(total_cost), 2)


def clamp_rates(gem_rate: float, psa9_rate: float) -> tuple[float, float]:
    gem = min(max(float(gem_rate), 0.0), 1.0)
    psa9 = min(max(float(psa9_rate), 0.0), max(0.0, 1.0 - gem))
    return gem, psa9


def received_psa9_percent(resolved_grades: Iterable, received_count: int) -> float:
    """PSA 9 share of received grading rows, using the same denominator as the gem-rate KPI."""
    if received_count <= 0:
        return 0.0
    nines = 0
    for grade in resolved_grades:
        number = pd.to_numeric(grade, errors="coerce")
        if pd.notna(number) and float(number) == 9.0:
            nines += 1
    return 100.0 * nines / received_count


def score_prospect(
    *,
    cost: float,
    fee: float,
    raw: float,
    psa9: float,
    psa10: float,
    gem_rate: float,
    psa9_rate: float,
) -> dict:
    fee = round(float(fee), 2)
    cost = round(float(cost), 2)
    outlay = round(cost + fee, 2)
    raw = float(raw or 0.0)
    psa9 = float(psa9 or 0.0)
    psa10 = float(psa10 or 0.0)
    gem, nine = clamp_rates(gem_rate, psa9_rate)
    remainder = 1.0 - gem - nine

    scored = {
        "cost_basis": cost,
        "grading_fee": fee,
        "outlay": outlay,
        "raw_price": round(raw, 2) if raw > 0 else None,
        "psa9_price": round(psa9, 2) if psa9 > 0 else None,
        "psa10_price": round(psa10, 2) if psa10 > 0 else None,
        "psa10_margin": round(psa10 - outlay, 2) if psa10 > 0 else None,
        "psa10_multiple": round(psa10 / outlay, 2) if psa10 > 0 and outlay > 0 else None,
        "psa9_margin": round(psa9 - outlay, 2) if psa9 > 0 else None,
        "expected_net": None,
    }

    missing_used_price = (
        (gem > 0 and psa10 <= 0)
        or (nine > 0 and psa9 <= 0)
        or (remainder > 0 and raw <= 0)
    )
    has_any_price = psa10 > 0 or psa9 > 0 or raw > 0
    if has_any_price and not missing_used_price:
        expected = gem * psa10 + nine * psa9 + remainder * raw - outlay
        scored["expected_net"] = round(expected, 2)
    return scored


def _identity(row: pd.Series) -> dict:
    return {
        "inventory_id": clean_text(row.get("inventory_id")),
        "set_name": clean_text(row.get("set_name")),
        "card_name": clean_text(row.get("card_name")),
        "card_number": clean_text(row.get("card_number")),
        "variant": clean_text(row.get("variant")),
        "reference_link": clean_text(row.get("reference_link")),
    }


def build_prospect_tables(
    cards: pd.DataFrame,
    prices_by_inventory_id: dict[str, dict] | None,
    fee: float,
    gem_rate: float,
    psa9_rate: float,
) -> dict[str, pd.DataFrame]:
    """Split active cards into ranked, skipped, and not-yet-priced rows.

    ``prices_by_inventory_id`` is None until the user pulls guide prices.
    """
    empty = pd.DataFrame()
    if cards is None or cards.empty:
        return {"ranked": empty, "skipped": empty, "waiting": empty}

    ranked_rows: list[dict] = []
    skipped_rows: list[dict] = []
    waiting_rows: list[dict] = []
    prices = prices_by_inventory_id or {}

    for _, row in cards.iterrows():
        identity = _identity(row)
        status = reference_status(identity["reference_link"])
        if status != "ok":
            skipped_rows.append({**identity, "reason": SKIP_LABELS[status]})
            continue

        if prices_by_inventory_id is None or identity["inventory_id"] not in prices:
            waiting_rows.append(identity)
            continue

        quote = prices.get(identity["inventory_id"]) or {}
        scored = score_prospect(
            cost=purchase_cost(row.get("total_price"), row.get("total_cost")),
            fee=fee,
            raw=float(quote.get("raw") or 0.0),
            psa9=float(quote.get("psa9") or 0.0),
            psa10=float(quote.get("psa10") or 0.0),
            gem_rate=gem_rate,
            psa9_rate=psa9_rate,
        )
        if scored["psa10_price"] is None:
            skipped_rows.append({**identity, "reason": SKIP_LABELS["no_psa10_price"]})
            continue
        ranked_rows.append({**identity, **scored})

    return {
        "ranked": pd.DataFrame(ranked_rows),
        "skipped": pd.DataFrame(skipped_rows),
        "waiting": pd.DataFrame(waiting_rows),
    }


def sort_prospects(ranked: pd.DataFrame, column: str) -> pd.DataFrame:
    if ranked is None or ranked.empty:
        return pd.DataFrame() if ranked is None else ranked.copy()
    key = column if column in SORT_COLUMNS else "psa10_margin"
    ordered = ranked.sort_values(
        by=[key, "card_name", "inventory_id"],
        ascending=[False, True, True],
        na_position="last",
        kind="mergesort",
    ).reset_index(drop=True)
    ordered.insert(0, "rank", ordered.index + 1)
    return ordered
