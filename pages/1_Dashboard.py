from __future__ import annotations

from datetime import date

import altair as alt
import pandas as pd
import streamlit as st

from core.business import load_data, refresh_database_cache, build_sales_ledger, period_summary, sync_grading_fees_to_inventory
from core.cleaning import money_fmt, age_bucket
from core.market import refresh_market_prices
from core.config import STATUS_ACTIVE, STATUS_GRADING

st.set_page_config(page_title="Dashboard", layout="wide")
st.title("Dashboard")

c1, c2, c3 = st.columns([1, 1, 2])
with c1:
    refresh_db = st.button("🔄 Refresh database", use_container_width=True)
with c2:
    refresh_market = st.button("💸 Refresh market values", use_container_width=True)
with c3:
    st.caption("Database refresh reloads Google Sheets only. Market refresh scrapes PriceCharting/SportsCardsPro and is intentionally separate.")

if refresh_db:
    refresh_database_cache()
    st.rerun()

data = load_data()
inv = data.inventory
grading = data.grading
expenses = data.expenses

if refresh_market:
    with st.spinner("Refreshing market prices. This is the slower action by design..."):
        changed, audit = refresh_market_prices(inv, limit=None, include_grading=True)
        st.success(f"Updated market values for {changed:,} inventory rows.")
        with st.expander("Market refresh audit", expanded=False):
            st.dataframe(audit, use_container_width=True, hide_index=True)
    refresh_database_cache()
    st.rerun()

if st.button("Fix/sync grading fees into inventory", use_container_width=False):
    changed = sync_grading_fees_to_inventory(inv, grading)
    st.success(f"Synced grading fee / total cost on {changed:,} inventory rows.")
    refresh_database_cache()
    st.rerun()

ledger = build_sales_ledger(inv, data.ebay_orders)

active = inv[inv["inventory_status"].isin([STATUS_ACTIVE, STATUS_GRADING])].copy() if not inv.empty else inv
held_cost = float(active["total_cost"].sum()) if not active.empty else 0.0
held_market = float(active["market_value"].sum()) if not active.empty else 0.0
sold_profit = float(ledger["profit"].sum()) if not ledger.empty else 0.0
sales_total = float(ledger["sales"].sum()) if not ledger.empty else 0.0
other_expenses = float(expenses["amount"].sum()) if not expenses.empty else 0.0

m1, m2, m3, m4, m5 = st.columns(5)
m1.metric("Active inventory cost", money_fmt(held_cost))
m2.metric("Active market value", money_fmt(held_market))
m3.metric("Unrealized spread", money_fmt(held_market - held_cost))
m4.metric("Total sales", money_fmt(sales_total))
m5.metric("Realized profit", money_fmt(sold_profit - other_expenses))

st.markdown("---")

left, right = st.columns([1, 2])
with left:
    freq_label = st.selectbox("Breakdown", ["Month", "Week", "Quarter", "Year", "Day"], index=0)
    include_future_expenses = st.checkbox("Include future scheduled expenses", value=False)
    freq_map = {"Day": "D", "Week": "W", "Month": "M", "Quarter": "Q", "Year": "Y"}
    freq = freq_map[freq_label]
with right:
    st.caption("Use this to see sales/profit by week or month without changing the raw Google Sheet.")

summary = period_summary(ledger, expenses, freq=freq, include_future_expenses=include_future_expenses)

st.subheader(f"{freq_label}ly Sales / Profit")
if summary.empty:
    st.info("No sales or expense data yet.")
else:
    chart_df = summary.melt(
        id_vars=["period"],
        value_vars=["sales", "gross_profit", "other_expenses", "profit_after_expenses"],
        var_name="metric",
        value_name="amount",
    )
    chart_df["metric"] = chart_df["metric"].map({
        "sales": "Sales",
        "gross_profit": "Gross Profit",
        "other_expenses": "Expenses",
        "profit_after_expenses": "Profit After Expenses",
    })
    chart = alt.Chart(chart_df).mark_bar().encode(
        x=alt.X("period:T", title=freq_label),
        y=alt.Y("amount:Q", title="$"),
        color=alt.Color("metric:N", title=""),
        xOffset="metric:N",
        tooltip=[alt.Tooltip("period:T"), "metric:N", alt.Tooltip("amount:Q", format="$.2f")],
    ).properties(height=360).interactive()
    st.altair_chart(chart, use_container_width=True)

    show = summary.copy()
    show["Period"] = show["period"].dt.strftime("%Y-%m-%d")
    show = show[["Period", "sales", "fees", "net_proceeds", "cogs", "gross_profit", "other_expenses", "profit_after_expenses", "items_sold"]]
    st.dataframe(
        show.style.format({c: "${:,.2f}" for c in ["sales", "fees", "net_proceeds", "cogs", "gross_profit", "other_expenses", "profit_after_expenses"]}),
        use_container_width=True,
        hide_index=True,
    )

st.markdown("---")

c1, c2 = st.columns(2)
with c1:
    st.subheader("Inventory by set")
    if active.empty:
        st.info("No active inventory.")
    else:
        by_set = active.groupby("set_name", dropna=False).agg(items=("inventory_id", "count"), cost=("total_cost", "sum"), market=("market_value", "sum")).reset_index().sort_values("market", ascending=False).head(25)
        st.dataframe(by_set.style.format({"cost": "${:,.2f}", "market": "${:,.2f}"}), use_container_width=True, hide_index=True)
with c2:
    st.subheader("Inventory age")
    if active.empty:
        st.info("No active inventory.")
    else:
        tmp = active.copy()
        tmp["age_days"] = (pd.Timestamp(date.today()) - pd.to_datetime(tmp["purchase_date"], errors="coerce")).dt.days
        tmp["age_bucket"] = tmp["age_days"].apply(age_bucket)
        by_age = tmp.groupby("age_bucket").agg(items=("inventory_id", "count"), cost=("total_cost", "sum"), market=("market_value", "sum")).reset_index()
        st.dataframe(by_age.style.format({"cost": "${:,.2f}", "market": "${:,.2f}"}), use_container_width=True, hide_index=True)

with st.expander("Sales ledger audit", expanded=False):
    st.dataframe(ledger.sort_values("date", ascending=False), use_container_width=True, hide_index=True)
