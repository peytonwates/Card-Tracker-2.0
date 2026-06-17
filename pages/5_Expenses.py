from __future__ import annotations

import uuid
from datetime import date
import pandas as pd
import streamlit as st

from core.business import load_data, refresh_database_cache
from core.cleaning import now_iso, money_fmt
from core.config import EXPENSE_COLUMNS, MILEAGE_COLUMNS, EXPENSE_CATEGORIES
from core.sheets import get_ws_name, append_rows

st.set_page_config(page_title="Expenses", layout="wide")
st.title("Expenses")

if st.button("🔄 Refresh database"):
    refresh_database_cache()
    st.rerun()

data = load_data()
expenses = data.expenses
mileage = data.mileage

t1, t2, t3 = st.tabs(["Add Expense", "Mileage", "Summary / History"])

with t1:
    st.subheader("Add Expense")
    with st.form("expense_form", clear_on_submit=True):
        c1, c2, c3 = st.columns([1, 1, 2])
        with c1:
            expense_date = st.date_input("Expense date", value=date.today())
        with c2:
            category = st.selectbox("Category", EXPENSE_CATEGORIES)
        with c3:
            description = st.text_input("Description")
        c4, c5 = st.columns([1, 3])
        with c4:
            amount = st.number_input("Amount", min_value=0.0, step=1.0, format="%.2f")
        with c5:
            notes = st.text_input("Notes")
        submitted = st.form_submit_button("Add expense", type="primary")
    if submitted:
        if not description or amount <= 0:
            st.error("Description and amount are required.")
        else:
            append_rows(get_ws_name("expenses_worksheet", get_ws_name("misc_worksheet", "misc")), EXPENSE_COLUMNS, [{
                "misc_id": str(uuid.uuid4())[:10],
                "expense_date": str(expense_date),
                "category": category,
                "description": description,
                "amount": round(amount, 2),
                "notes": notes,
                "created_at": now_iso(),
            }])
            st.success("Expense added.")
            refresh_database_cache()
            st.rerun()

with t2:
    st.subheader("Mileage Log")
    with st.form("mileage_form", clear_on_submit=True):
        c1, c2, c3 = st.columns(3)
        with c1:
            trip_date = st.date_input("Trip date", value=date.today())
            show_name = st.text_input("Show name")
        with c2:
            purpose = st.text_input("Business purpose", value="Vending")
            round_trip = st.selectbox("Round trip", ["Yes", "No"])
        with c3:
            miles = st.number_input("Miles", min_value=0.0, step=1.0, format="%.1f")
            parking_tolls = st.number_input("Parking / tolls", min_value=0.0, step=1.0, format="%.2f")
        start = st.text_input("Start location")
        end = st.text_input("End location")
        notes = st.text_input("Notes")
        submitted = st.form_submit_button("Add mileage", type="primary")
    if submitted:
        append_rows(get_ws_name("mileage_worksheet", "mileage"), MILEAGE_COLUMNS, [{
            "mileage_id": str(uuid.uuid4())[:10],
            "trip_date": str(trip_date),
            "show_name": show_name,
            "business_purpose": purpose,
            "start_location": start,
            "end_location": end,
            "round_trip": round_trip,
            "miles": round(miles, 1),
            "parking_tolls": round(parking_tolls, 2),
            "notes": notes,
            "created_at": now_iso(),
        }])
        st.success("Mileage added.")
        refresh_database_cache()
        st.rerun()

with t3:
    st.subheader("Expense Summary")
    c1, c2, c3 = st.columns(3)
    c1.metric("Total expenses", money_fmt(expenses["amount"].sum() if not expenses.empty else 0))
    c2.metric("Total miles", f"{float(mileage['miles'].sum()) if not mileage.empty else 0:,.1f}")
    c3.metric("Parking/tolls", money_fmt(mileage["parking_tolls"].sum() if not mileage.empty else 0))

    if not expenses.empty:
        exp = expenses.copy()
        exp["expense_date"] = pd.to_datetime(exp["expense_date"], errors="coerce")
        exp["month"] = exp["expense_date"].dt.to_period("M").astype(str)
        by_cat = exp.groupby("category", dropna=False).agg(amount=("amount", "sum"), count=("misc_id", "count")).reset_index().sort_values("amount", ascending=False)
        by_month = exp.groupby("month", dropna=False).agg(amount=("amount", "sum"), count=("misc_id", "count")).reset_index().sort_values("month", ascending=False)
        c1, c2 = st.columns(2)
        with c1:
            st.markdown("#### By category")
            st.dataframe(by_cat.style.format({"amount": "${:,.2f}"}), use_container_width=True, hide_index=True)
        with c2:
            st.markdown("#### By month")
            st.dataframe(by_month.style.format({"amount": "${:,.2f}"}), use_container_width=True, hide_index=True)
        st.markdown("#### Expense history")
        st.dataframe(expenses.sort_values("expense_date", ascending=False), use_container_width=True, hide_index=True)
    else:
        st.info("No expenses yet.")

    st.markdown("#### Mileage history")
    if mileage.empty:
        st.info("No mileage yet.")
    else:
        st.dataframe(mileage.sort_values("trip_date", ascending=False), use_container_width=True, hide_index=True)
