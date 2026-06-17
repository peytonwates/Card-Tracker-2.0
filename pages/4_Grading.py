from __future__ import annotations

import uuid
from datetime import date, timedelta
import pandas as pd
import streamlit as st

from core.business import load_data, refresh_database_cache, sync_grading_fees_to_inventory
from core.cleaning import now_iso, clean_text, to_money, money_fmt
from core.config import GRADING_COLUMNS, INVENTORY_COLUMNS, STATUS_ACTIVE, STATUS_GRADING, STATUS_RETURNED, GRADING_COMPANIES
from core.sheets import get_ws_name, append_rows, update_rows_by_key
from core.market import fetch_market_prices

st.set_page_config(page_title="Grading", layout="wide")
st.title("Grading")


def add_business_days(start_d: date, n: int) -> date:
    d = start_d
    added = 0
    while added < n:
        d += timedelta(days=1)
        if d.weekday() < 5:
            added += 1
    return d

if st.button("🔄 Refresh database"):
    refresh_database_cache()
    st.rerun()

data = load_data()
inv = data.inventory
grading = data.grading

t1, t2, t3 = st.tabs(["Create Submission", "Update Returns", "Submission History"])

with t1:
    st.subheader("Create Grading Submission")
    active_cards = inv[(inv["inventory_status"].eq(STATUS_ACTIVE)) & (inv["product_type"].astype(str).str.lower().ne("sealed"))].copy() if not inv.empty else inv
    if active_cards.empty:
        st.info("No ACTIVE cards available for grading.")
    else:
        active_cards["label"] = active_cards.apply(lambda r: f"{r['inventory_id']} — {r.get('set_name','')} — {r.get('card_name','')} #{r.get('card_number','')} — cost {money_fmt(r.get('total_cost'))}", axis=1)
        selected = st.multiselect("Select cards", active_cards["label"].tolist())
        col1, col2, col3, col4 = st.columns(4)
        with col1:
            submission_date = st.date_input("Submission date", value=date.today())
        with col2:
            company = st.selectbox("Grading company", GRADING_COMPANIES)
        with col3:
            fee_per_card = st.number_input("Grading fee per card", min_value=0.0, value=float(st.secrets.get("default_grading_fee_per_card", 22.0)), step=1.0, format="%.2f")
        with col4:
            business_days = st.number_input("Estimated return business days", min_value=1, value=int(st.secrets.get("default_business_days_return", 75)), step=1)
        notes = st.text_area("Notes")
        pull_prices = st.checkbox("Pull PSA 9/10 market values for submission rows", value=False)
        if st.button("Create submission", type="primary", disabled=not selected):
            chosen = active_cards[active_cards["label"].isin(selected)].copy()
            sub_id = str(int(pd.Timestamp.utcnow().timestamp()))
            est_return = add_business_days(submission_date, int(business_days))
            rows = []
            inv_updates = {}
            for _, r in chosen.iterrows():
                prices = {"psa9": 0, "psa10": 0}
                if pull_prices and clean_text(r.get("reference_link")):
                    prices = fetch_market_prices(r.get("reference_link"))
                grading_fee = round(float(fee_per_card), 2)
                inv_id = clean_text(r.get("inventory_id"))
                rows.append({
                    "grading_row_id": str(uuid.uuid4())[:10],
                    "submission_id": sub_id,
                    "submission_date": str(submission_date),
                    "estimated_return_date": str(est_return),
                    "inventory_id": inv_id,
                    "reference_link": clean_text(r.get("reference_link")),
                    "card_name": clean_text(r.get("card_name")),
                    "card_number": clean_text(r.get("card_number")),
                    "variant": clean_text(r.get("variant")),
                    "card_subtype": clean_text(r.get("card_subtype")),
                    "purchased_from": clean_text(r.get("purchased_from")),
                    "purchase_date": clean_text(r.get("purchase_date")),
                    "purchase_total": round(to_money(r.get("total_price")), 2),
                    "grading_company": company,
                    "grading_fee_initial": grading_fee,
                    "grading_fee_per_card": grading_fee,
                    "additional_costs": 0.0,
                    "extra_costs": 0.0,
                    "total_grading_cost": grading_fee,
                    "psa9_price": prices.get("psa9", 0),
                    "psa10_price": prices.get("psa10", 0),
                    "status": "SUBMITTED",
                    "notes": notes,
                    "created_at": now_iso(),
                    "updated_at": now_iso(),
                    "synced_to_inventory": "YES",
                })
                inv_updates[inv_id] = {
                    "inventory_status": STATUS_GRADING,
                    "grading_company": company,
                    "grading_fee": grading_fee,
                    "total_cost": round(to_money(r.get("total_price")) + grading_fee, 2),
                }
            append_rows(get_ws_name("grading_worksheet", "grading"), GRADING_COLUMNS, rows)
            update_rows_by_key(get_ws_name("inventory_worksheet", "inventory"), INVENTORY_COLUMNS, "inventory_id", inv_updates)
            st.success(f"Created submission {sub_id} with {len(rows):,} card(s). Grading fees were written back to inventory.")
            refresh_database_cache()
            st.rerun()

with t2:
    st.subheader("Update Returns")
    if grading.empty:
        st.info("No grading records yet.")
    else:
        open_rows = grading[~grading["status"].astype(str).str.upper().isin(["RETURNED", "COMPLETE", "COMPLETED"])].copy()
        if open_rows.empty:
            st.info("No open grading rows.")
        else:
            open_rows["label"] = open_rows.apply(lambda r: f"{r['grading_row_id']} — {r.get('submission_id','')} — {r.get('card_name','')} #{r.get('card_number','')}", axis=1)
            selected = st.selectbox("Select returned card", open_rows["label"].tolist())
            rec = open_rows[open_rows["label"].eq(selected)].iloc[0]
            col1, col2, col3 = st.columns(3)
            with col1:
                returned_date = st.date_input("Returned date", value=date.today())
            with col2:
                received_grade = st.text_input("Received grade")
            with col3:
                additional_cost = st.number_input("Additional cost", min_value=0.0, step=1.0, format="%.2f")
            if st.button("Mark returned", type="primary"):
                row_id = clean_text(rec.get("grading_row_id"))
                inv_id = clean_text(rec.get("inventory_id"))
                total_grading_cost = to_money(rec.get("grading_fee_initial")) + to_money(rec.get("grading_fee_per_card")) + additional_cost
                if total_grading_cost <= 0:
                    total_grading_cost = to_money(rec.get("total_grading_cost")) + additional_cost
                update_rows_by_key(get_ws_name("grading_worksheet", "grading"), GRADING_COLUMNS, "grading_row_id", {
                    row_id: {
                        "status": STATUS_RETURNED,
                        "returned_date": str(returned_date),
                        "received_grade": received_grade,
                        "additional_costs": additional_cost,
                        "total_grading_cost": round(total_grading_cost, 2),
                        "updated_at": now_iso(),
                        "synced_to_inventory": "YES",
                    }
                })
                inv_rec = inv[inv["inventory_id"].eq(inv_id)].iloc[0] if inv_id in set(inv["inventory_id"]) else None
                base_cost = to_money(inv_rec.get("total_price")) if inv_rec is not None else 0.0
                update_rows_by_key(get_ws_name("inventory_worksheet", "inventory"), INVENTORY_COLUMNS, "inventory_id", {
                    inv_id: {
                        "inventory_status": STATUS_ACTIVE,
                        "product_type": "Graded Card",
                        "grading_company": clean_text(rec.get("grading_company")),
                        "grade": received_grade,
                        "condition": "Graded",
                        "grading_fee": round(total_grading_cost, 2),
                        "total_cost": round(base_cost + total_grading_cost, 2),
                    }
                })
                st.success("Return updated and grading fee synced to inventory.")
                refresh_database_cache()
                st.rerun()

with t3:
    st.subheader("Submission History")
    if st.button("Sync all grading fees to inventory"):
        changed = sync_grading_fees_to_inventory(inv, grading)
        st.success(f"Synced {changed:,} inventory rows.")
        refresh_database_cache()
        st.rerun()
    if grading.empty:
        st.info("No grading records yet.")
    else:
        summary = grading.groupby(["submission_id", "status"], dropna=False).agg(cards=("grading_row_id", "count"), grading_cost=("total_grading_cost", "sum"), purchase_total=("purchase_total", "sum")).reset_index()
        st.dataframe(summary.style.format({"grading_cost": "${:,.2f}", "purchase_total": "${:,.2f}"}), use_container_width=True, hide_index=True)
        st.dataframe(grading.sort_values("submission_date", ascending=False), use_container_width=True, hide_index=True)
