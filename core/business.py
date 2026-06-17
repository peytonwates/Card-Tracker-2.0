from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import uuid
import pandas as pd
import numpy as np
import streamlit as st

from .config import (
    INVENTORY_COLUMNS, GRADING_COLUMNS, EXPENSE_COLUMNS, MILEAGE_COLUMNS, SHOW_COLUMNS,
    EBAY_ORDER_COLUMNS, EBAY_LISTING_COLUMNS,
    STATUS_ACTIVE, STATUS_GRADING, STATUS_RETURNED, STATUS_SOLD,
)
from .cleaning import clean_inventory, clean_generic, clean_text, now_iso, to_money
from .sheets import get_ws_name, read_sheet, update_rows_by_key, append_rows, clear_read_cache


def worksheet_names() -> dict[str, str]:
    return {
        "inventory": get_ws_name("inventory_worksheet", "inventory"),
        "grading": get_ws_name("grading_worksheet", "grading"),
        "expenses": get_ws_name("expenses_worksheet", get_ws_name("misc_worksheet", "misc")),
        "mileage": get_ws_name("mileage_worksheet", "mileage"),
        "shows": get_ws_name("shows_worksheet", "shows"),
        "ebay_orders": get_ws_name("ebay_orders_worksheet", "ebay_orders"),
        "ebay_listings": get_ws_name("ebay_listings_worksheet", "ebay_listings"),
    }


@dataclass
class AppData:
    inventory: pd.DataFrame
    grading: pd.DataFrame
    expenses: pd.DataFrame
    mileage: pd.DataFrame
    shows: pd.DataFrame
    ebay_orders: pd.DataFrame
    ebay_listings: pd.DataFrame


@st.cache_data(ttl=45, show_spinner=False)
def load_data_cached() -> AppData:
    names = worksheet_names()
    inv = clean_inventory(read_sheet(names["inventory"], tuple(INVENTORY_COLUMNS)), INVENTORY_COLUMNS)
    gr = clean_generic(read_sheet(names["grading"], tuple(GRADING_COLUMNS)), GRADING_COLUMNS)
    exp = clean_generic(read_sheet(names["expenses"], tuple(EXPENSE_COLUMNS)), EXPENSE_COLUMNS)
    miles = clean_generic(read_sheet(names["mileage"], tuple(MILEAGE_COLUMNS)), MILEAGE_COLUMNS)
    shows = clean_generic(read_sheet(names["shows"], tuple(SHOW_COLUMNS)), SHOW_COLUMNS)
    ebay = clean_generic(read_sheet(names["ebay_orders"], tuple(EBAY_ORDER_COLUMNS)), EBAY_ORDER_COLUMNS)
    listings = clean_generic(read_sheet(names["ebay_listings"], tuple(EBAY_LISTING_COLUMNS)), EBAY_LISTING_COLUMNS)

    for df, cols in [
        (gr, ["submission_date", "estimated_return_date", "returned_date", "created_at", "updated_at"]),
        (exp, ["expense_date", "created_at"]),
        (miles, ["trip_date", "created_at"]),
        (shows, ["show_date", "created_at", "updated_at"]),
        (ebay, ["sold_date", "order_created_at", "created_at", "updated_at"]),
    ]:
        for c in cols:
            if c in df.columns:
                df[f"__{c}_dt"] = pd.to_datetime(df[c], errors="coerce")
    return AppData(inv, gr, exp, miles, shows, ebay, listings)


def refresh_database_cache():
    load_data_cached.clear()
    clear_read_cache()


def load_data(force_refresh: bool = False) -> AppData:
    if force_refresh:
        refresh_database_cache()
    data = load_data_cached()
    inv_synced = apply_grading_fees_to_inventory_df(data.inventory, data.grading)
    return AppData(inv_synced, data.grading, data.expenses, data.mileage, data.shows, data.ebay_orders, data.ebay_listings)


def grading_fee_map(grading: pd.DataFrame) -> dict[str, float]:
    if grading is None or grading.empty or "inventory_id" not in grading.columns:
        return {}
    g = grading.copy()
    g["inventory_id"] = g["inventory_id"].astype(str).str.strip()
    g = g[g["inventory_id"].ne("")].copy()
    if g.empty:
        return {}
    if "__updated_at_dt" in g.columns:
        g = g.sort_values("__updated_at_dt", na_position="first")
    out = {}
    for _, r in g.iterrows():
        inv_id = clean_text(r.get("inventory_id"))
        fee = to_money(r.get("total_grading_cost"))
        if fee <= 0:
            fee = to_money(r.get("grading_fee_initial")) + to_money(r.get("additional_costs")) + to_money(r.get("extra_costs"))
        if fee <= 0:
            fee = to_money(r.get("grading_fee_per_card"))
        if inv_id and fee > 0:
            out[inv_id] = round(fee, 2)
    return out


def apply_grading_fees_to_inventory_df(inventory: pd.DataFrame, grading: pd.DataFrame) -> pd.DataFrame:
    inv = inventory.copy()
    if inv.empty:
        return inv
    fmap = grading_fee_map(grading)
    if not fmap:
        return inv
    mapped = inv["inventory_id"].astype(str).str.strip().map(fmap).fillna(0.0).astype(float)
    inv["grading_fee"] = np.where(mapped > 0, mapped, inv["grading_fee"].astype(float))
    inv["total_cost"] = (inv["total_price"].astype(float) + inv["grading_fee"].astype(float)).round(2)
    return inv


def sync_grading_fees_to_inventory(inventory: pd.DataFrame, grading: pd.DataFrame) -> int:
    names = worksheet_names()
    fmap = grading_fee_map(grading)
    if not fmap or inventory.empty:
        return 0
    updates = {}
    inv_by_id = inventory.set_index("inventory_id", drop=False).to_dict("index") if "inventory_id" in inventory.columns else {}
    for inv_id, fee in fmap.items():
        rec = inv_by_id.get(str(inv_id))
        if rec is None:
            continue
        base = to_money(rec.get("total_price"))
        updates[str(inv_id)] = {
            "grading_fee": round(fee, 2),
            "total_cost": round(base + fee, 2),
        }
    return update_rows_by_key(names["inventory"], INVENTORY_COLUMNS, "inventory_id", updates)


def build_sales_ledger(inventory: pd.DataFrame, ebay_orders: pd.DataFrame | None = None) -> pd.DataFrame:
    rows = []
    if inventory is not None and not inventory.empty:
        sold = inventory[inventory["inventory_status"].astype(str).str.upper().eq(STATUS_SOLD)].copy()
        sold = sold[sold["sold_date"].astype(str).str.strip().ne("") | (sold["sold_price"] > 0)].copy()
        for _, r in sold.iterrows():
            rows.append({
                "source": clean_text(r.get("sale_channel")) or clean_text(r.get("platform")) or "Inventory",
                "inventory_id": clean_text(r.get("inventory_id")),
                "date": pd.to_datetime(r.get("sold_date"), errors="coerce"),
                "card_type": clean_text(r.get("card_type")),
                "product_type": clean_text(r.get("product_type")),
                "set_name": clean_text(r.get("set_name")),
                "card_name": clean_text(r.get("card_name")),
                "sales": to_money(r.get("sold_price")),
                "fees": to_money(r.get("fees_total")) or to_money(r.get("fees")) + to_money(r.get("shipping_charged")),
                "net_proceeds": to_money(r.get("net_proceeds")),
                "cogs": to_money(r.get("total_cost")),
                "profit": to_money(r.get("profit")),
                "show_id": clean_text(r.get("show_id")),
                "show_name": clean_text(r.get("show_name")),
                "ebay_order_id": clean_text(r.get("ebay_order_id")),
            })
    if ebay_orders is not None and not ebay_orders.empty:
        matched_ids = {x for x in [clean_text(v) for v in inventory.get("ebay_order_id", [])] if x}
        for _, r in ebay_orders.iterrows():
            order_id = clean_text(r.get("ebay_order_id"))
            matched = clean_text(r.get("matched_to_inventory")).upper() in {"YES", "TRUE", "1"}
            if matched or order_id in matched_ids:
                continue
            rows.append({
                "source": "eBay Unmatched",
                "inventory_id": clean_text(r.get("inventory_id")),
                "date": pd.to_datetime(r.get("sold_date"), errors="coerce"),
                "card_type": "",
                "product_type": "",
                "set_name": "",
                "card_name": clean_text(r.get("title")),
                "sales": to_money(r.get("sold_price")),
                "fees": to_money(r.get("fees_total")),
                "net_proceeds": to_money(r.get("net_proceeds")),
                "cogs": 0.0,
                "profit": to_money(r.get("net_proceeds")),
                "show_id": "",
                "show_name": "",
                "ebay_order_id": order_id,
            })
    ledger = pd.DataFrame(rows)
    if ledger.empty:
        return pd.DataFrame(columns=["source", "inventory_id", "date", "card_type", "product_type", "set_name", "card_name", "sales", "fees", "net_proceeds", "cogs", "profit", "show_id", "show_name", "ebay_order_id"])
    ledger["date"] = pd.to_datetime(ledger["date"], errors="coerce")
    ledger = ledger[ledger["date"].notna()].copy()
    ledger["net_proceeds"] = np.where(ledger["net_proceeds"] != 0, ledger["net_proceeds"], ledger["sales"] - ledger["fees"])
    ledger["profit"] = np.where(ledger["profit"].abs() > 0, ledger["profit"], ledger["net_proceeds"] - ledger["cogs"])
    return ledger


def period_summary(ledger: pd.DataFrame, expenses: pd.DataFrame, freq: str = "M", include_future_expenses: bool = False) -> pd.DataFrame:
    today = pd.Timestamp(date.today())
    if ledger.empty:
        sales = pd.DataFrame(columns=["period", "sales", "fees", "net_proceeds", "cogs", "gross_profit", "items_sold"])
    else:
        x = ledger.copy()
        x["period"] = x["date"].dt.to_period(freq).dt.to_timestamp()
        sales = x.groupby("period", as_index=False).agg(
            sales=("sales", "sum"), fees=("fees", "sum"), net_proceeds=("net_proceeds", "sum"),
            cogs=("cogs", "sum"), gross_profit=("profit", "sum"), items_sold=("inventory_id", "count")
        )
    if expenses is not None and not expenses.empty:
        e = expenses.copy()
        e["expense_date"] = pd.to_datetime(e["expense_date"], errors="coerce")
        e = e[e["expense_date"].notna()].copy()
        if not include_future_expenses:
            e = e[e["expense_date"] <= today].copy()
        e["period"] = e["expense_date"].dt.to_period(freq).dt.to_timestamp()
        exp = e.groupby("period", as_index=False).agg(other_expenses=("amount", "sum"))
    else:
        exp = pd.DataFrame(columns=["period", "other_expenses"])
    out = pd.merge(sales, exp, on="period", how="outer").fillna(0)
    if out.empty:
        return out
    out["profit_after_expenses"] = out["gross_profit"] - out["other_expenses"]
    return out.sort_values("period")


def add_inventory_row(row: dict):
    names = worksheet_names()
    if not row.get("inventory_id"):
        row["inventory_id"] = str(uuid.uuid4())[:8]
    now = now_iso()
    row.setdefault("created_at", now)
    row.setdefault("inventory_status", STATUS_ACTIVE)
    total_price = to_money(row.get("total_price")) or to_money(row.get("purchase_price")) + to_money(row.get("shipping")) + to_money(row.get("tax"))
    row["total_price"] = round(total_price, 2)
    row["grading_fee"] = round(to_money(row.get("grading_fee")), 2)
    row["total_cost"] = round(total_price + to_money(row.get("grading_fee")), 2)
    append_rows(names["inventory"], INVENTORY_COLUMNS, [row])


def mark_inventory_sold(inv_id: str, updates: dict) -> int:
    names = worksheet_names()
    updates = dict(updates)
    updates["inventory_status"] = STATUS_SOLD
    updates.setdefault("sold_updated_at", now_iso())
    sold_price = to_money(updates.get("sold_price"))
    fees_total = to_money(updates.get("fees_total")) or to_money(updates.get("fees")) + to_money(updates.get("shipping_charged"))
    updates["fees_total"] = round(fees_total, 2)
    updates["net_proceeds"] = round(to_money(updates.get("net_proceeds")) or sold_price - fees_total, 2)
    return update_rows_by_key(names["inventory"], INVENTORY_COLUMNS, "inventory_id", {inv_id: updates})
