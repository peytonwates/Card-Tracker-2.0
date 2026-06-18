from __future__ import annotations

from datetime import date, timedelta

import altair as alt
import pandas as pd
import streamlit as st

from core.business import (
    load_data,
    refresh_database_cache,
    build_sales_ledger,
    period_summary,
    sync_grading_fees_to_inventory,
)
from core.cleaning import money_fmt, age_bucket, to_money, clean_text
from core.market import refresh_market_prices
from core.config import STATUS_ACTIVE, STATUS_GRADING

try:
    from core.config import STATUS_LISTED, STATUS_SOLD
except Exception:
    STATUS_LISTED = "LISTED"
    STATUS_SOLD = "SOLD"


st.set_page_config(page_title="Dashboard", layout="wide")
st.title("Dashboard")


# =========================================================
# Helpers
# =========================================================

BOOK_STATUSES = [STATUS_ACTIVE, STATUS_GRADING, STATUS_LISTED]


def _empty_df() -> pd.DataFrame:
    return pd.DataFrame()


def _ensure_cols(df: pd.DataFrame | None, cols: list[str], default="") -> pd.DataFrame:
    out = pd.DataFrame() if df is None else df.copy()

    for col in cols:
        if col not in out.columns:
            out[col] = default

    return out


def _money_series(df: pd.DataFrame, col: str) -> pd.Series:
    if df.empty or col not in df.columns:
        return pd.Series(dtype=float)

    return df[col].apply(to_money).astype(float)


def _safe_sum(df: pd.DataFrame, col: str) -> float:
    if df.empty or col not in df.columns:
        return 0.0

    return float(_money_series(df, col).sum())


def _safe_count(df: pd.DataFrame, col: str = "inventory_id") -> int:
    if df.empty:
        return 0

    if col in df.columns:
        return int(df[col].astype(str).str.strip().ne("").sum())

    return int(len(df))


def _safe_div(num: float, den: float) -> float:
    try:
        den = float(den)
        if den == 0:
            return 0.0
        return float(num) / den
    except Exception:
        return 0.0


def _pct_fmt(x: float) -> str:
    return f"{x * 100:,.1f}%"


def _date_series(df: pd.DataFrame, preferred: list[str]) -> pd.Series:
    if df.empty:
        return pd.Series(dtype="datetime64[ns]")

    for col in preferred:
        if col in df.columns:
            return pd.to_datetime(df[col], errors="coerce")

    return pd.Series(pd.NaT, index=df.index)


def _product_bucket(row: pd.Series) -> str:
    inventory_type = clean_text(row.get("inventory_type")).lower()
    product_type = clean_text(row.get("product_type")).lower()
    sealed_product_type = clean_text(row.get("sealed_product_type")).lower()
    grade = clean_text(row.get("grade"))
    grading_company = clean_text(row.get("grading_company"))
    card_subtype = clean_text(row.get("card_subtype")).lower()
    variant = clean_text(row.get("variant")).lower()

    combined = " ".join(
        [
            inventory_type,
            product_type,
            sealed_product_type,
            card_subtype,
            variant,
        ]
    )

    if "sealed" in combined or sealed_product_type:
        return "Sealed"

    if grading_company or grade or "graded" in combined or "psa" in combined or "cgc" in combined or "bgs" in combined:
        return "Graded"

    return "Singles"


def _prep_inventory(inv: pd.DataFrame | None) -> pd.DataFrame:
    needed = [
        "inventory_id",
        "inventory_status",
        "inventory_type",
        "product_type",
        "sealed_product_type",
        "card_type",
        "brand_or_league",
        "set_name",
        "card_name",
        "card_number",
        "variant",
        "card_subtype",
        "grading_company",
        "grade",
        "purchase_date",
        "purchase_price",
        "shipping",
        "tax",
        "total_price",
        "grading_fee",
        "total_cost",
        "market_value",
        "sticker_price",
        "list_price",
        "platform",
        "sale_channel",
        "sold_date",
        "sold_price",
        "fees",
        "fees_total",
        "net_proceeds",
        "profit",
        "ebay_item_id",
        "ebay_listing_status",
    ]

    out = _ensure_cols(inv, needed)

    if out.empty:
        return out

    out["inventory_id"] = out["inventory_id"].astype(str).str.strip()
    out["inventory_status"] = out["inventory_status"].astype(str).str.upper().str.strip()
    out["inventory_type"] = out["inventory_type"].astype(str).str.strip()
    out["product_type"] = out["product_type"].astype(str).str.strip()
    out["card_type"] = out["card_type"].astype(str).str.strip()
    out["set_name"] = out["set_name"].astype(str).str.strip()
    out["card_name"] = out["card_name"].astype(str).str.strip()

    for col in [
        "purchase_price",
        "shipping",
        "tax",
        "total_price",
        "grading_fee",
        "total_cost",
        "market_value",
        "sticker_price",
        "list_price",
        "sold_price",
        "fees",
        "fees_total",
        "net_proceeds",
        "profit",
    ]:
        out[col] = out[col].apply(to_money).astype(float)

    out["purchase_dt"] = pd.to_datetime(out["purchase_date"], errors="coerce")
    out["sold_dt"] = pd.to_datetime(out["sold_date"], errors="coerce")
    out["product_bucket"] = out.apply(_product_bucket, axis=1)

    today = pd.Timestamp(date.today())
    out["age_days"] = (today - out["purchase_dt"]).dt.days
    out["age_bucket"] = out["age_days"].apply(age_bucket)

    return out


def _prep_expenses(expenses: pd.DataFrame | None) -> pd.DataFrame:
    out = _ensure_cols(
        expenses,
        ["expense_date", "category", "description", "amount", "notes", "created_at"],
    )

    if out.empty:
        return out

    out["expense_dt"] = pd.to_datetime(out["expense_date"], errors="coerce")
    out["amount"] = out["amount"].apply(to_money).astype(float)
    out["category"] = out["category"].astype(str).str.strip().replace("", "Uncategorized")
    out["description"] = out["description"].astype(str).str.strip()

    return out


def _prep_ledger(ledger: pd.DataFrame | None, inv: pd.DataFrame) -> pd.DataFrame:
    out = pd.DataFrame() if ledger is None else ledger.copy()

    if out.empty:
        return out

    out = _ensure_cols(
        out,
        [
            "inventory_id",
            "date",
            "sales",
            "fees",
            "net_proceeds",
            "cogs",
            "gross_profit",
            "profit",
            "items_sold",
            "platform",
            "sale_channel",
        ],
    )

    out["date"] = _date_series(out, ["date", "sold_date", "created_at"])
    out = out[out["date"].notna()].copy()

    for col in ["sales", "fees", "net_proceeds", "cogs", "gross_profit", "profit", "items_sold"]:
        out[col] = out[col].apply(to_money).astype(float)

    if "gross_profit" not in out.columns or out["gross_profit"].abs().sum() == 0:
        out["gross_profit"] = out["profit"]

    if "net_proceeds" not in out.columns or out["net_proceeds"].abs().sum() == 0:
        out["net_proceeds"] = out["sales"] - out["fees"]

    if "cogs" not in out.columns or out["cogs"].abs().sum() == 0:
        out["cogs"] = out["net_proceeds"] - out["gross_profit"]

    if "items_sold" not in out.columns or out["items_sold"].abs().sum() == 0:
        out["items_sold"] = 1

    # Add inventory attributes to ledger so dashboard filters work for sold items too.
    if "inventory_id" in out.columns and "inventory_id" in inv.columns and not inv.empty:
        attrs = [
            "inventory_id",
            "inventory_type",
            "product_type",
            "sealed_product_type",
            "card_type",
            "brand_or_league",
            "set_name",
            "card_name",
            "card_number",
            "variant",
            "card_subtype",
            "grading_company",
            "grade",
            "product_bucket",
        ]

        attr_cols = [c for c in attrs if c in inv.columns]
        inv_attrs = inv[attr_cols].drop_duplicates(subset=["inventory_id"], keep="last")

        keep_cols = ["inventory_id"]
        for c in attr_cols:
            if c != "inventory_id" and c not in out.columns:
                keep_cols.append(c)
            elif c != "inventory_id" and c in out.columns:
                # Fill blank ledger values from inventory using a temporary suffix.
                keep_cols.append(c)

        merged = out.merge(
            inv_attrs[keep_cols],
            on="inventory_id",
            how="left",
            suffixes=("", "_inv"),
        )

        for c in attrs:
            inv_col = f"{c}_inv"
            if inv_col in merged.columns:
                if c in merged.columns:
                    blank = merged[c].astype(str).str.strip().eq("") | merged[c].isna()
                    merged[c] = merged[c].where(~blank, merged[inv_col])
                    merged = merged.drop(columns=[inv_col])
                else:
                    merged[c] = merged[inv_col]
                    merged = merged.drop(columns=[inv_col])

        out = merged

    out = _ensure_cols(
        out,
        [
            "inventory_type",
            "product_type",
            "sealed_product_type",
            "card_type",
            "brand_or_league",
            "set_name",
            "card_name",
            "card_number",
            "variant",
            "card_subtype",
            "grading_company",
            "grade",
            "product_bucket",
            "platform",
            "sale_channel",
        ],
    )

    if "product_bucket" not in out.columns or out["product_bucket"].astype(str).str.strip().eq("").all():
        out["product_bucket"] = out.apply(_product_bucket, axis=1)

    out["inventory_type"] = out["inventory_type"].astype(str).str.strip()
    out["product_type"] = out["product_type"].astype(str).str.strip()
    out["card_type"] = out["card_type"].astype(str).str.strip()
    out["product_bucket"] = out["product_bucket"].astype(str).str.strip().replace("", "Unknown")
    out["sale_channel"] = out["sale_channel"].astype(str).str.strip().replace("", "Unknown")
    out["platform"] = out["platform"].astype(str).str.strip().replace("", "Unknown")

    return out


def _filter_by_date(df: pd.DataFrame, date_col: str, start_dt: date, end_dt: date) -> pd.DataFrame:
    if df.empty or date_col not in df.columns:
        return df.copy()

    out = df.copy()
    d = pd.to_datetime(out[date_col], errors="coerce")

    start_ts = pd.Timestamp(start_dt)
    end_ts = pd.Timestamp(end_dt) + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)

    return out[(d >= start_ts) & (d <= end_ts)].copy()


def _filter_scope(df: pd.DataFrame, inventory_scope: str) -> pd.DataFrame:
    if df.empty or inventory_scope == "All Inventory" or "inventory_type" not in df.columns:
        return df.copy()

    return df[df["inventory_type"].astype(str).str.strip().eq(inventory_scope)].copy()


def _filter_multiselect(df: pd.DataFrame, col: str, selected: list[str]) -> pd.DataFrame:
    if df.empty or not selected or col not in df.columns:
        return df.copy()

    return df[df[col].astype(str).str.strip().isin(selected)].copy()


def _format_money_cols(df: pd.DataFrame, cols: list[str]) -> dict:
    return {c: "${:,.2f}" for c in cols if c in df.columns}


def _add_margin_cols(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()

    if out.empty:
        return out

    out["gross_margin_pct"] = out.apply(
        lambda r: _safe_div(to_money(r.get("gross_profit")), to_money(r.get("sales"))),
        axis=1,
    )
    out["net_margin_pct"] = out.apply(
        lambda r: _safe_div(to_money(r.get("profit_after_expenses", r.get("gross_profit"))), to_money(r.get("sales"))),
        axis=1,
    )
    out["roc_on_cogs"] = out.apply(
        lambda r: _safe_div(to_money(r.get("gross_profit")), to_money(r.get("cogs"))),
        axis=1,
    )
    out["avg_sale"] = out.apply(
        lambda r: _safe_div(to_money(r.get("sales")), to_money(r.get("items_sold"))),
        axis=1,
    )

    return out


def _group_sales(df: pd.DataFrame, group_cols: list[str]) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()

    safe_group_cols = [c for c in group_cols if c in df.columns]

    if not safe_group_cols:
        return pd.DataFrame()

    grouped = (
        df.groupby(safe_group_cols, dropna=False)
        .agg(
            items_sold=("items_sold", "sum"),
            sales=("sales", "sum"),
            fees=("fees", "sum"),
            net_proceeds=("net_proceeds", "sum"),
            cogs=("cogs", "sum"),
            gross_profit=("gross_profit", "sum"),
        )
        .reset_index()
    )

    grouped = _add_margin_cols(grouped)
    return grouped.sort_values("gross_profit", ascending=False)


def _group_inventory(df: pd.DataFrame, group_cols: list[str]) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()

    safe_group_cols = [c for c in group_cols if c in df.columns]

    if not safe_group_cols:
        return pd.DataFrame()

    grouped = (
        df.groupby(safe_group_cols, dropna=False)
        .agg(
            items=("inventory_id", "count"),
            cost=("total_cost", "sum"),
            market=("market_value", "sum"),
            sticker_value=("sticker_price", "sum"),
        )
        .reset_index()
    )

    grouped["unrealized_spread"] = grouped["market"] - grouped["cost"]
    grouped["unrealized_roc"] = grouped.apply(lambda r: _safe_div(r["unrealized_spread"], r["cost"]), axis=1)

    return grouped.sort_values("market", ascending=False)


def _display_metric_row(metrics: list[tuple[str, str, str | None]]) -> None:
    cols = st.columns(len(metrics))

    for col, (label, value, delta) in zip(cols, metrics):
        with col:
            st.metric(label, value, delta=delta)


def _bar_chart(df: pd.DataFrame, x_col: str, y_col: str, color_col: str | None = None, title: str = "", height: int = 320):
    if df.empty or x_col not in df.columns or y_col not in df.columns:
        st.info("Not enough data for this chart.")
        return

    enc = {
        "x": alt.X(f"{x_col}:N", sort="-y", title=None),
        "y": alt.Y(f"{y_col}:Q", title="$"),
        "tooltip": [
            alt.Tooltip(f"{x_col}:N", title=x_col.replace("_", " ").title()),
            alt.Tooltip(f"{y_col}:Q", format="$,.2f"),
        ],
    }

    if color_col and color_col in df.columns:
        enc["color"] = alt.Color(f"{color_col}:N", title=color_col.replace("_", " ").title())
        enc["tooltip"].append(alt.Tooltip(f"{color_col}:N"))

    chart = alt.Chart(df).mark_bar().encode(**enc).properties(title=title, height=height).interactive()
    st.altair_chart(chart, use_container_width=True)


# =========================================================
# Header actions
# =========================================================

c1, c2, c3 = st.columns([1, 1, 2])

with c1:
    refresh_db = st.button("🔄 Refresh database", use_container_width=True)

with c2:
    refresh_market = st.button("💸 Refresh market values", use_container_width=True)

with c3:
    st.caption(
        "Database refresh reloads Google Sheets only. Market refresh scrapes PriceCharting/SportsCardsPro and is intentionally separate."
    )

if refresh_db:
    refresh_database_cache()
    st.rerun()


# =========================================================
# Load data
# =========================================================

data = load_data()

inv_raw = data.inventory
grading = data.grading
expenses_raw = data.expenses

inv = _prep_inventory(inv_raw)
expenses = _prep_expenses(expenses_raw)

if refresh_market:
    with st.spinner("Refreshing market prices. This is the slower action by design..."):
        changed, audit = refresh_market_prices(inv_raw, limit=None, include_grading=True)
        st.success(f"Updated market values for {changed:,} inventory rows.")
        with st.expander("Market refresh audit", expanded=False):
            st.dataframe(audit, use_container_width=True, hide_index=True)
    refresh_database_cache()
    st.rerun()

if st.button("Fix/sync grading fees into inventory", use_container_width=False):
    changed = sync_grading_fees_to_inventory(inv_raw, grading)
    st.success(f"Synced grading fee / total cost on {changed:,} inventory rows.")
    refresh_database_cache()
    st.rerun()

ledger_raw = build_sales_ledger(inv_raw, data.ebay_orders)
ledger = _prep_ledger(ledger_raw, inv)


# =========================================================
# Global filters
# =========================================================

st.sidebar.header("Dashboard Filters")

all_dates = []

if not ledger.empty and "date" in ledger.columns:
    all_dates.extend(pd.to_datetime(ledger["date"], errors="coerce").dropna().dt.date.tolist())

if not expenses.empty and "expense_dt" in expenses.columns:
    all_dates.extend(pd.to_datetime(expenses["expense_dt"], errors="coerce").dropna().dt.date.tolist())

today = date.today()

if all_dates:
    min_available = min(all_dates)
    max_available = max(max(all_dates), today)
else:
    min_available = today - timedelta(days=90)
    max_available = today

default_start = min_available
default_end = today if max_available >= today else max_available

date_range = st.sidebar.date_input(
    "Sales / Expenses date range",
    value=(default_start, default_end),
    min_value=min_available,
    max_value=max_available,
)

if isinstance(date_range, tuple) and len(date_range) == 2:
    start_date, end_date = date_range
else:
    start_date, end_date = default_start, default_end

if start_date > end_date:
    start_date, end_date = end_date, start_date

inventory_scope = st.sidebar.selectbox(
    "Inventory scope",
    ["All Inventory", "Show Inventory", "Personal Inventory"],
    index=0,
)

include_future_expenses = st.sidebar.checkbox(
    "Include future scheduled expenses",
    value=False,
)

available_product_buckets = sorted(
    [x for x in inv["product_bucket"].dropna().astype(str).unique().tolist() if x]
)
selected_product_buckets = st.sidebar.multiselect(
    "Product bucket",
    available_product_buckets,
    default=available_product_buckets,
)

available_card_types = sorted(
    [x for x in inv["card_type"].dropna().astype(str).unique().tolist() if x]
)
selected_card_types = st.sidebar.multiselect(
    "Pokemon / Sports",
    available_card_types,
    default=available_card_types,
)

available_product_types = sorted(
    [x for x in inv["product_type"].dropna().astype(str).unique().tolist() if x]
)
selected_product_types = st.sidebar.multiselect(
    "Product type",
    available_product_types,
    default=available_product_types,
)

available_channels = sorted(
    [x for x in ledger["sale_channel"].dropna().astype(str).unique().tolist() if x]
) if not ledger.empty and "sale_channel" in ledger.columns else []

selected_channels = st.sidebar.multiselect(
    "Sale channel",
    available_channels,
    default=available_channels,
)

available_sets = sorted(
    [x for x in inv["set_name"].dropna().astype(str).unique().tolist() if x]
)
selected_sets = st.sidebar.multiselect(
    "Set / Category",
    available_sets,
    default=[],
    help="Leave blank to include all sets.",
)


# Filter inventory
inv_filtered = inv.copy()
inv_filtered = _filter_scope(inv_filtered, inventory_scope)
inv_filtered = _filter_multiselect(inv_filtered, "product_bucket", selected_product_buckets)
inv_filtered = _filter_multiselect(inv_filtered, "card_type", selected_card_types)
inv_filtered = _filter_multiselect(inv_filtered, "product_type", selected_product_types)

if selected_sets:
    inv_filtered = _filter_multiselect(inv_filtered, "set_name", selected_sets)

# Filter sales ledger
ledger_filtered = ledger.copy()
ledger_filtered = _filter_by_date(ledger_filtered, "date", start_date, end_date)
ledger_filtered = _filter_scope(ledger_filtered, inventory_scope)
ledger_filtered = _filter_multiselect(ledger_filtered, "product_bucket", selected_product_buckets)
ledger_filtered = _filter_multiselect(ledger_filtered, "card_type", selected_card_types)
ledger_filtered = _filter_multiselect(ledger_filtered, "product_type", selected_product_types)
ledger_filtered = _filter_multiselect(ledger_filtered, "sale_channel", selected_channels)

if selected_sets:
    ledger_filtered = _filter_multiselect(ledger_filtered, "set_name", selected_sets)

# Filter expenses
expenses_filtered = expenses.copy()
expenses_filtered = _filter_by_date(expenses_filtered, "expense_dt", start_date, end_date)

if not include_future_expenses and not expenses_filtered.empty:
    expenses_filtered = expenses_filtered[
        pd.to_datetime(expenses_filtered["expense_dt"], errors="coerce") <= pd.Timestamp(today)
    ].copy()


# =========================================================
# Summary calculations
# =========================================================

book_inventory = inv_filtered[
    inv_filtered["inventory_status"].astype(str).str.upper().isin(BOOK_STATUSES)
].copy() if not inv_filtered.empty else inv_filtered

sold_inventory = inv_filtered[
    inv_filtered["inventory_status"].astype(str).str.upper().eq(STATUS_SOLD)
].copy() if not inv_filtered.empty else inv_filtered

book_cost = _safe_sum(book_inventory, "total_cost")
book_market = _safe_sum(book_inventory, "market_value")
book_spread = book_market - book_cost

sales_total = _safe_sum(ledger_filtered, "sales")
fees_total = _safe_sum(ledger_filtered, "fees")
net_proceeds_total = _safe_sum(ledger_filtered, "net_proceeds")
cogs_total = _safe_sum(ledger_filtered, "cogs")
gross_profit_total = _safe_sum(ledger_filtered, "gross_profit")
items_sold_total = _safe_sum(ledger_filtered, "items_sold")
expense_total = _safe_sum(expenses_filtered, "amount")
profit_after_expenses = gross_profit_total - expense_total

capital_deployed = book_cost + cogs_total
gross_margin = _safe_div(gross_profit_total, sales_total)
net_margin_after_expenses = _safe_div(profit_after_expenses, sales_total)
realized_roc = _safe_div(gross_profit_total, cogs_total)
all_in_roc = _safe_div(profit_after_expenses, capital_deployed)
ebitda = profit_after_expenses
ebitda_margin = _safe_div(ebitda, sales_total)


# =========================================================
# Executive metric strip
# =========================================================

st.markdown("### Grand Summary")

_display_metric_row(
    [
        ("Book inventory cost", money_fmt(book_cost), f"{_safe_count(book_inventory):,} items"),
        ("Book market value", money_fmt(book_market), f"Spread {money_fmt(book_spread)}"),
        ("Total sales", money_fmt(sales_total), f"{items_sold_total:,.0f} items sold"),
        ("Gross profit", money_fmt(gross_profit_total), _pct_fmt(gross_margin)),
        ("Profit after expenses", money_fmt(profit_after_expenses), _pct_fmt(net_margin_after_expenses)),
    ]
)

_display_metric_row(
    [
        ("Expenses", money_fmt(expense_total), None),
        ("Net proceeds", money_fmt(net_proceeds_total), None),
        ("COGS", money_fmt(cogs_total), None),
        ("Realized ROC", _pct_fmt(realized_roc), "Gross profit / COGS"),
        ("All-in ROC", _pct_fmt(all_in_roc), "Profit / active cost + COGS"),
    ]
)

st.caption(
    "EBITDA-style operating profit is shown as profit after expenses because this tracker does not currently separate interest, taxes, depreciation, or amortization."
)


# =========================================================
# Tabs
# =========================================================

tab_exec, tab_trends, tab_sales, tab_inventory, tab_expenses, tab_audit = st.tabs(
    [
        "Executive Summary",
        "Trends",
        "Sales Drivers",
        "Inventory",
        "Expenses",
        "Audit",
    ]
)


# =========================================================
# Executive Summary
# =========================================================

with tab_exec:
    st.subheader("Business Performance Snapshot")

    col_a, col_b = st.columns([1.1, 1])

    with col_a:
        st.markdown("#### P&L Summary")

        pnl_rows = pd.DataFrame(
            [
                {"metric": "Gross Sales", "amount": sales_total},
                {"metric": "Fees", "amount": -fees_total},
                {"metric": "Net Proceeds", "amount": net_proceeds_total},
                {"metric": "COGS", "amount": -cogs_total},
                {"metric": "Gross Profit", "amount": gross_profit_total},
                {"metric": "Expenses", "amount": -expense_total},
                {"metric": "Profit After Expenses", "amount": profit_after_expenses},
                {"metric": "EBITDA-style Operating Profit", "amount": ebitda},
            ]
        )

        st.dataframe(
            pnl_rows.style.format({"amount": "${:,.2f}"}),
            use_container_width=True,
            hide_index=True,
        )

        chart_rows = pnl_rows[
            pnl_rows["metric"].isin(
                ["Gross Sales", "Fees", "COGS", "Expenses", "Profit After Expenses"]
            )
        ].copy()

        chart = (
            alt.Chart(chart_rows)
            .mark_bar()
            .encode(
                x=alt.X("metric:N", sort=None, title=None),
                y=alt.Y("amount:Q", title="$"),
                tooltip=["metric:N", alt.Tooltip("amount:Q", format="$,.2f")],
            )
            .properties(height=300)
        )
        st.altair_chart(chart, use_container_width=True)

    with col_b:
        st.markdown("#### Inventory on the Books")

        inv_summary = pd.DataFrame(
            [
                {"metric": "Book inventory count", "value": _safe_count(book_inventory)},
                {"metric": "Book inventory cost", "value": book_cost},
                {"metric": "Book market value", "value": book_market},
                {"metric": "Unrealized spread", "value": book_spread},
                {"metric": "Unrealized ROC", "value": _safe_div(book_spread, book_cost)},
                {"metric": "Sold inventory count", "value": _safe_count(sold_inventory)},
            ]
        )

        pretty = inv_summary.copy()
        pretty["display_value"] = pretty.apply(
            lambda r: _pct_fmt(r["value"])
            if "ROC" in str(r["metric"])
            else (
                f"{int(r['value']):,}"
                if "count" in str(r["metric"]).lower()
                else money_fmt(r["value"])
            ),
            axis=1,
        )

        st.dataframe(
            pretty[["metric", "display_value"]],
            use_container_width=True,
            hide_index=True,
        )

        by_bucket = _group_inventory(book_inventory, ["product_bucket", "card_type"])

        if by_bucket.empty:
            st.info("No book inventory for selected filters.")
        else:
            st.markdown("#### Book Inventory by Product / Category")
            st.dataframe(
                by_bucket.style.format(
                    {
                        "cost": "${:,.2f}",
                        "market": "${:,.2f}",
                        "sticker_value": "${:,.2f}",
                        "unrealized_spread": "${:,.2f}",
                        "unrealized_roc": "{:.1%}",
                    }
                ),
                use_container_width=True,
                hide_index=True,
            )

    st.markdown("#### Performance Drivers")

    c1, c2, c3 = st.columns(3)

    with c1:
        sales_by_bucket = _group_sales(ledger_filtered, ["product_bucket", "card_type"])
        if not sales_by_bucket.empty:
            _bar_chart(
                sales_by_bucket.head(10),
                "product_bucket",
                "gross_profit",
                color_col="card_type",
                title="Gross Profit by Product Bucket",
                height=280,
            )
        else:
            st.info("No sales data for selected filters.")

    with c2:
        if not expenses_filtered.empty:
            exp_by_cat = (
                expenses_filtered.groupby("category", dropna=False)
                .agg(amount=("amount", "sum"))
                .reset_index()
                .sort_values("amount", ascending=False)
                .head(10)
            )
            _bar_chart(exp_by_cat, "category", "amount", title="Expenses by Category", height=280)
        else:
            st.info("No expenses for selected filters.")

    with c3:
        if not book_inventory.empty:
            inv_by_status = _group_inventory(book_inventory, ["inventory_status"])
            _bar_chart(
                inv_by_status.head(10),
                "inventory_status",
                "cost",
                title="Book Cost by Status",
                height=280,
            )
        else:
            st.info("No book inventory for selected filters.")


# =========================================================
# Trends
# =========================================================

with tab_trends:
    st.subheader("Sales / Profit Trends")

    left, right = st.columns([1, 2])

    with left:
        freq_label = st.selectbox(
            "Breakdown",
            ["Month", "Week", "Quarter", "Year", "Day"],
            index=0,
        )
        freq_map = {"Day": "D", "Week": "W", "Month": "M", "Quarter": "Q", "Year": "Y"}
        freq = freq_map[freq_label]

    with right:
        st.caption(
            "This trend view respects the dashboard filters for date range, inventory scope, product bucket, card type, product type, sale channel, and set."
        )

    summary = period_summary(
        ledger_filtered,
        expenses_filtered,
        freq=freq,
        include_future_expenses=include_future_expenses,
    )

    if summary.empty:
        st.info("No sales or expense data for the selected filters.")
    else:
        chart_cols = [
            c
            for c in ["sales", "gross_profit", "other_expenses", "profit_after_expenses"]
            if c in summary.columns
        ]

        chart_df = summary.melt(
            id_vars=["period"],
            value_vars=chart_cols,
            var_name="metric",
            value_name="amount",
        )

        chart_df["metric"] = chart_df["metric"].map(
            {
                "sales": "Sales",
                "gross_profit": "Gross Profit",
                "other_expenses": "Expenses",
                "profit_after_expenses": "Profit After Expenses",
            }
        )

        chart = (
            alt.Chart(chart_df)
            .mark_bar()
            .encode(
                x=alt.X("period:T", title=freq_label),
                y=alt.Y("amount:Q", title="$"),
                color=alt.Color("metric:N", title=""),
                xOffset="metric:N",
                tooltip=[
                    alt.Tooltip("period:T"),
                    "metric:N",
                    alt.Tooltip("amount:Q", format="$,.2f"),
                ],
            )
            .properties(height=360)
            .interactive()
        )

        st.altair_chart(chart, use_container_width=True)

        cumulative = summary.copy()
        cumulative["cumulative_profit_after_expenses"] = cumulative["profit_after_expenses"].cumsum()

        line = (
            alt.Chart(cumulative)
            .mark_line(point=True)
            .encode(
                x=alt.X("period:T", title=freq_label),
                y=alt.Y("cumulative_profit_after_expenses:Q", title="Cumulative Profit After Expenses"),
                tooltip=[
                    alt.Tooltip("period:T"),
                    alt.Tooltip("cumulative_profit_after_expenses:Q", format="$,.2f"),
                ],
            )
            .properties(height=280, title="Cumulative Profit After Expenses")
            .interactive()
        )

        st.altair_chart(line, use_container_width=True)

        show = summary.copy()
        show["Period"] = show["period"].dt.strftime("%Y-%m-%d")

        wanted_cols = [
            "Period",
            "sales",
            "fees",
            "net_proceeds",
            "cogs",
            "gross_profit",
            "other_expenses",
            "profit_after_expenses",
            "items_sold",
        ]

        show = show[[c for c in wanted_cols if c in show.columns]].copy()
        show = _add_margin_cols(show)

        st.dataframe(
            show.style.format(
                {
                    **_format_money_cols(
                        show,
                        [
                            "sales",
                            "fees",
                            "net_proceeds",
                            "cogs",
                            "gross_profit",
                            "other_expenses",
                            "profit_after_expenses",
                            "avg_sale",
                        ],
                    ),
                    "gross_margin_pct": "{:.1%}",
                    "net_margin_pct": "{:.1%}",
                    "roc_on_cogs": "{:.1%}",
                }
            ),
            use_container_width=True,
            hide_index=True,
        )


# =========================================================
# Sales Drivers
# =========================================================

with tab_sales:
    st.subheader("Sales Drivers and Margins")

    if ledger_filtered.empty:
        st.info("No sales data for selected filters.")
    else:
        c1, c2 = st.columns(2)

        with c1:
            st.markdown("#### Average Margins by Product / Pokemon-Sports")

            by_product = _group_sales(ledger_filtered, ["product_bucket", "card_type"])
            st.dataframe(
                by_product.style.format(
                    {
                        "sales": "${:,.2f}",
                        "fees": "${:,.2f}",
                        "net_proceeds": "${:,.2f}",
                        "cogs": "${:,.2f}",
                        "gross_profit": "${:,.2f}",
                        "gross_margin_pct": "{:.1%}",
                        "net_margin_pct": "{:.1%}",
                        "roc_on_cogs": "{:.1%}",
                        "avg_sale": "${:,.2f}",
                    }
                ),
                use_container_width=True,
                hide_index=True,
            )

        with c2:
            st.markdown("#### Margin by Channel / Platform")

            by_channel = _group_sales(ledger_filtered, ["sale_channel", "platform"])
            st.dataframe(
                by_channel.style.format(
                    {
                        "sales": "${:,.2f}",
                        "fees": "${:,.2f}",
                        "net_proceeds": "${:,.2f}",
                        "cogs": "${:,.2f}",
                        "gross_profit": "${:,.2f}",
                        "gross_margin_pct": "{:.1%}",
                        "net_margin_pct": "{:.1%}",
                        "roc_on_cogs": "{:.1%}",
                        "avg_sale": "${:,.2f}",
                    }
                ),
                use_container_width=True,
                hide_index=True,
            )

        c3, c4 = st.columns(2)

        with c3:
            st.markdown("#### Top Sets by Gross Profit")
            by_set = _group_sales(ledger_filtered, ["set_name"]).head(25)
            st.dataframe(
                by_set.style.format(
                    {
                        "sales": "${:,.2f}",
                        "fees": "${:,.2f}",
                        "net_proceeds": "${:,.2f}",
                        "cogs": "${:,.2f}",
                        "gross_profit": "${:,.2f}",
                        "gross_margin_pct": "{:.1%}",
                        "net_margin_pct": "{:.1%}",
                        "roc_on_cogs": "{:.1%}",
                        "avg_sale": "${:,.2f}",
                    }
                ),
                use_container_width=True,
                hide_index=True,
            )

        with c4:
            st.markdown("#### Top Sold Items by Profit")
            item_cols = [
                "date",
                "inventory_id",
                "card_name",
                "card_number",
                "set_name",
                "product_bucket",
                "card_type",
                "sales",
                "net_proceeds",
                "cogs",
                "gross_profit",
                "sale_channel",
                "platform",
            ]

            detail = ledger_filtered[[c for c in item_cols if c in ledger_filtered.columns]].copy()
            detail = detail.sort_values("gross_profit", ascending=False).head(50)

            st.dataframe(
                detail.style.format(
                    _format_money_cols(detail, ["sales", "net_proceeds", "cogs", "gross_profit"])
                ),
                use_container_width=True,
                hide_index=True,
            )

        st.markdown("#### Profit Scatter: Cost vs Gross Profit")

        scatter_df = ledger_filtered.copy()
        scatter_df = scatter_df[(scatter_df["cogs"] > 0) | (scatter_df["gross_profit"] != 0)].copy()

        if scatter_df.empty:
            st.info("Not enough sold-item detail for scatter chart.")
        else:
            scatter = (
                alt.Chart(scatter_df)
                .mark_circle(size=80)
                .encode(
                    x=alt.X("cogs:Q", title="COGS"),
                    y=alt.Y("gross_profit:Q", title="Gross Profit"),
                    color=alt.Color("product_bucket:N", title="Product"),
                    tooltip=[
                        alt.Tooltip("card_name:N", title="Card"),
                        alt.Tooltip("set_name:N", title="Set"),
                        alt.Tooltip("sales:Q", format="$,.2f"),
                        alt.Tooltip("cogs:Q", format="$,.2f"),
                        alt.Tooltip("gross_profit:Q", format="$,.2f"),
                        alt.Tooltip("sale_channel:N"),
                    ],
                )
                .properties(height=360)
                .interactive()
            )

            st.altair_chart(scatter, use_container_width=True)


# =========================================================
# Inventory
# =========================================================

with tab_inventory:
    st.subheader("Inventory on the Books")

    if book_inventory.empty:
        st.info("No book inventory for selected filters.")
    else:
        c1, c2 = st.columns(2)

        with c1:
            st.markdown("#### Inventory by Set")
            by_set = _group_inventory(book_inventory, ["set_name"]).head(25)
            st.dataframe(
                by_set.style.format(
                    {
                        "cost": "${:,.2f}",
                        "market": "${:,.2f}",
                        "sticker_value": "${:,.2f}",
                        "unrealized_spread": "${:,.2f}",
                        "unrealized_roc": "{:.1%}",
                    }
                ),
                use_container_width=True,
                hide_index=True,
            )

        with c2:
            st.markdown("#### Inventory Age")
            by_age = _group_inventory(book_inventory, ["age_bucket"])
            age_order = ["Future", "0-30 days", "31-60 days", "61-90 days", "91-180 days", "181+ days", "Unknown"]
            if not by_age.empty:
                by_age["age_bucket"] = pd.Categorical(by_age["age_bucket"], categories=age_order, ordered=True)
                by_age = by_age.sort_values("age_bucket")
            st.dataframe(
                by_age.style.format(
                    {
                        "cost": "${:,.2f}",
                        "market": "${:,.2f}",
                        "sticker_value": "${:,.2f}",
                        "unrealized_spread": "${:,.2f}",
                        "unrealized_roc": "{:.1%}",
                    }
                ),
                use_container_width=True,
                hide_index=True,
            )

        c3, c4 = st.columns(2)

        with c3:
            st.markdown("#### Inventory by Product / Pokemon-Sports")
            by_bucket = _group_inventory(book_inventory, ["product_bucket", "card_type"])
            st.dataframe(
                by_bucket.style.format(
                    {
                        "cost": "${:,.2f}",
                        "market": "${:,.2f}",
                        "sticker_value": "${:,.2f}",
                        "unrealized_spread": "${:,.2f}",
                        "unrealized_roc": "{:.1%}",
                    }
                ),
                use_container_width=True,
                hide_index=True,
            )

        with c4:
            st.markdown("#### Inventory by Status")
            by_status = _group_inventory(book_inventory, ["inventory_status"])
            st.dataframe(
                by_status.style.format(
                    {
                        "cost": "${:,.2f}",
                        "market": "${:,.2f}",
                        "sticker_value": "${:,.2f}",
                        "unrealized_spread": "${:,.2f}",
                        "unrealized_roc": "{:.1%}",
                    }
                ),
                use_container_width=True,
                hide_index=True,
            )

        st.markdown("#### Inventory Detail")

        detail_cols = [
            "inventory_id",
            "inventory_status",
            "inventory_type",
            "product_bucket",
            "card_type",
            "product_type",
            "set_name",
            "card_name",
            "card_number",
            "variant",
            "grading_company",
            "grade",
            "purchase_date",
            "total_cost",
            "market_value",
            "sticker_price",
            "age_bucket",
            "ebay_item_id",
            "ebay_listing_status",
        ]

        detail = book_inventory[[c for c in detail_cols if c in book_inventory.columns]].copy()
        detail = detail.sort_values(["market_value", "total_cost"], ascending=False)

        st.dataframe(
            detail.style.format(
                _format_money_cols(detail, ["total_cost", "market_value", "sticker_price"])
            ),
            use_container_width=True,
            hide_index=True,
        )


# =========================================================
# Expenses
# =========================================================

with tab_expenses:
    st.subheader("Expenses")

    if expenses_filtered.empty:
        st.info("No expenses for selected filters.")
    else:
        exp_total = _safe_sum(expenses_filtered, "amount")
        exp_count = len(expenses_filtered)
        avg_expense = _safe_div(exp_total, exp_count)

        _display_metric_row(
            [
                ("Expense total", money_fmt(exp_total), None),
                ("Expense count", f"{exp_count:,}", None),
                ("Average expense", money_fmt(avg_expense), None),
                ("Expense % of sales", _pct_fmt(_safe_div(exp_total, sales_total)), None),
            ]
        )

        c1, c2 = st.columns(2)

        with c1:
            st.markdown("#### Expenses by Category")
            by_category = (
                expenses_filtered.groupby("category", dropna=False)
                .agg(count=("amount", "count"), amount=("amount", "sum"))
                .reset_index()
                .sort_values("amount", ascending=False)
            )

            st.dataframe(
                by_category.style.format({"amount": "${:,.2f}"}),
                use_container_width=True,
                hide_index=True,
            )

        with c2:
            st.markdown("#### Expense Trend")
            exp_trend = expenses_filtered.copy()
            exp_trend["period"] = exp_trend["expense_dt"].dt.to_period("M").dt.to_timestamp()

            exp_trend = (
                exp_trend.groupby("period")
                .agg(amount=("amount", "sum"))
                .reset_index()
                .sort_values("period")
            )

            chart = (
                alt.Chart(exp_trend)
                .mark_bar()
                .encode(
                    x=alt.X("period:T", title="Month"),
                    y=alt.Y("amount:Q", title="$"),
                    tooltip=[alt.Tooltip("period:T"), alt.Tooltip("amount:Q", format="$,.2f")],
                )
                .properties(height=300)
                .interactive()
            )

            st.altair_chart(chart, use_container_width=True)

        st.markdown("#### Expense Detail")
        exp_detail_cols = ["expense_date", "category", "description", "amount", "notes", "created_at"]
        exp_detail = expenses_filtered[[c for c in exp_detail_cols if c in expenses_filtered.columns]].copy()
        exp_detail = exp_detail.sort_values("expense_date", ascending=False)

        st.dataframe(
            exp_detail.style.format({"amount": "${:,.2f}"}),
            use_container_width=True,
            hide_index=True,
        )


# =========================================================
# Audit
# =========================================================

with tab_audit:
    st.subheader("Audit / Raw Tables")

    with st.expander("Sales ledger audit", expanded=False):
        if ledger.empty:
            st.info("No sales ledger data.")
        else:
            st.dataframe(
                ledger.sort_values("date", ascending=False),
                use_container_width=True,
                hide_index=True,
            )

    with st.expander("Filtered sales ledger", expanded=False):
        if ledger_filtered.empty:
            st.info("No filtered sales data.")
        else:
            st.dataframe(
                ledger_filtered.sort_values("date", ascending=False),
                use_container_width=True,
                hide_index=True,
            )

    with st.expander("Filtered inventory", expanded=False):
        if inv_filtered.empty:
            st.info("No filtered inventory.")
        else:
            st.dataframe(inv_filtered, use_container_width=True, hide_index=True)

    with st.expander("Filtered expenses", expanded=False):
        if expenses_filtered.empty:
            st.info("No filtered expenses.")
        else:
            st.dataframe(expenses_filtered, use_container_width=True, hide_index=True)

    st.markdown("#### Field Coverage Check")

    checks = pd.DataFrame(
        [
            {"table": "inventory", "rows": len(inv), "columns": len(inv.columns) if not inv.empty else 0},
            {"table": "sales_ledger", "rows": len(ledger), "columns": len(ledger.columns) if not ledger.empty else 0},
            {"table": "expenses", "rows": len(expenses), "columns": len(expenses.columns) if not expenses.empty else 0},
            {"table": "filtered_inventory", "rows": len(inv_filtered), "columns": len(inv_filtered.columns) if not inv_filtered.empty else 0},
            {"table": "filtered_sales_ledger", "rows": len(ledger_filtered), "columns": len(ledger_filtered.columns) if not ledger_filtered.empty else 0},
            {"table": "filtered_expenses", "rows": len(expenses_filtered), "columns": len(expenses_filtered.columns) if not expenses_filtered.empty else 0},
        ]
    )

    st.dataframe(checks, use_container_width=True, hide_index=True)
