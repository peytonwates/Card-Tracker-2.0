from __future__ import annotations

import uuid
from datetime import date
import pandas as pd
import streamlit as st

from core.business import load_data, add_inventory_row, refresh_database_cache
from core.cleaning import now_iso, age_bucket, money_fmt, to_money
from core.config import PRODUCT_TYPE_OPTIONS, CARD_TYPE_OPTIONS, INVENTORY_TYPE_OPTIONS, CONDITION_OPTIONS, STATUS_ACTIVE, INVENTORY_COLUMNS
from core.market import fetch_market_prices, price_for_inventory_row

st.set_page_config(page_title="Inventory", layout="wide")
st.title("Inventory")

if st.button("🔄 Refresh database", use_container_width=False):
    refresh_database_cache()
    st.rerun()

data = load_data()
inv = data.inventory
active = inv[inv["inventory_status"].isin([STATUS_ACTIVE, "GRADING", "LISTED"])] if not inv.empty else inv

tab_overview, tab_add, tab_bulk, tab_table = st.tabs(["Overview", "Add Single", "Bulk Add", "Inventory Table"])

with tab_overview:
    st.subheader("Inventory Summary")
    if inv.empty:
        st.info("No inventory yet.")
    else:
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Active items", f"{len(active):,}")
        c2.metric("Active cost", money_fmt(active["total_cost"].sum()))
        c3.metric("Active market", money_fmt(active["market_value"].sum()))
        c4.metric("Sold items", f"{len(inv[inv['inventory_status'].eq('SOLD')]):,}")

        c1, c2 = st.columns(2)
        with c1:
            by_set = active.groupby("set_name", dropna=False).agg(items=("inventory_id", "count"), cost=("total_cost", "sum"), market=("market_value", "sum")).reset_index().sort_values("market", ascending=False)
            st.markdown("#### By set")
            st.dataframe(by_set.head(50).style.format({"cost": "${:,.2f}", "market": "${:,.2f}"}), use_container_width=True, hide_index=True)
        with c2:
            tmp = active.copy()
            tmp["purchase_dt"] = pd.to_datetime(tmp["purchase_date"], errors="coerce")
            tmp["age_days"] = (pd.Timestamp(date.today()) - tmp["purchase_dt"]).dt.days
            tmp["age_bucket"] = tmp["age_days"].apply(age_bucket)
            by_age = tmp.groupby("age_bucket").agg(items=("inventory_id", "count"), cost=("total_cost", "sum"), market=("market_value", "sum")).reset_index()
            st.markdown("#### By age")
            st.dataframe(by_age.style.format({"cost": "${:,.2f}", "market": "${:,.2f}"}), use_container_width=True, hide_index=True)

with tab_add:
    st.subheader("Add One Item")
    with st.form("add_single_inventory", clear_on_submit=True):
        c1, c2, c3, c4 = st.columns(4)
        with c1:
            product_type = st.selectbox("Product type", PRODUCT_TYPE_OPTIONS)
            inventory_type = st.selectbox("Inventory type", INVENTORY_TYPE_OPTIONS)
            card_type = st.selectbox("Card type", CARD_TYPE_OPTIONS)
        with c2:
            brand = st.text_input("Brand / league", value="Pokemon TCG")
            set_name = st.text_input("Set")
            year = st.text_input("Year")
        with c3:
            card_name = st.text_input("Card / item name")
            card_number = st.text_input("Card #")
            variant = st.text_input("Variant")
        with c4:
            card_subtype = st.text_input("Subtype")
            condition = st.selectbox("Condition", CONDITION_OPTIONS, index=0)
            reference_link = st.text_input("Reference link")

        c5, c6, c7, c8 = st.columns(4)
        with c5:
            purchase_date = st.date_input("Purchase date", value=date.today())
            purchased_from = st.text_input("Purchased from")
        with c6:
            purchase_price = st.number_input("Purchase price", min_value=0.0, step=1.0, format="%.2f")
            shipping = st.number_input("Shipping", min_value=0.0, step=0.5, format="%.2f")
        with c7:
            tax = st.number_input("Tax", min_value=0.0, step=0.5, format="%.2f")
            sticker_price = st.number_input("Sticker price", min_value=0.0, step=1.0, format="%.2f")
        with c8:
            grading_company = st.text_input("Grading company")
            grade = st.text_input("Grade")

        notes = st.text_area("Notes")
        pull_market = st.checkbox("Pull market value now from reference link", value=False)
        submitted = st.form_submit_button("Add item", type="primary")

    if submitted:
        if not card_name and not reference_link:
            st.error("Add at least a card/item name or a reference link.")
        else:
            row = {
                "inventory_id": str(uuid.uuid4())[:8],
                "inventory_type": inventory_type,
                "product_type": product_type,
                "inventory_status": STATUS_ACTIVE,
                "card_type": card_type,
                "brand_or_league": brand,
                "set_name": set_name,
                "year": year,
                "card_name": card_name,
                "card_number": card_number,
                "variant": variant,
                "card_subtype": card_subtype,
                "grading_company": grading_company,
                "grade": grade,
                "reference_link": reference_link,
                "purchase_date": str(purchase_date),
                "purchased_from": purchased_from,
                "purchase_price": purchase_price,
                "shipping": shipping,
                "tax": tax,
                "sticker_price": sticker_price,
                "condition": condition,
                "notes": notes,
                "created_at": now_iso(),
            }
            if pull_market and reference_link:
                prices = fetch_market_prices(reference_link)
                market = price_for_inventory_row(pd.Series(row), prices)
                row["market_price"] = market
                row["market_value"] = market
                row["market_price_debug"] = prices.get("debug", "")
                row["image_url"] = prices.get("image_url", "")
                row["market_price_updated_at"] = now_iso()
            add_inventory_row(row)
            st.success("Inventory item added.")
            refresh_database_cache()

with tab_bulk:
    st.subheader("Bulk Add Inventory")
    st.caption("Upload CSV/XLSX. If you include a Quantity column, one row will be created per quantity.")
    template = pd.DataFrame([{c: "" for c in [
        "inventory_type", "product_type", "card_type", "brand_or_league", "set_name", "year", "card_name", "card_number", "variant", "card_subtype", "reference_link", "purchase_date", "purchased_from", "purchase_price", "shipping", "tax", "condition", "notes", "Quantity"
    ]}])
    st.download_button("Download bulk template", template.to_csv(index=False), file_name="inventory_upload_template.csv", mime="text/csv")
    upload = st.file_uploader("Upload inventory CSV/XLSX", type=["csv", "xlsx", "xls"])
    if upload:
        raw = pd.read_csv(upload) if upload.name.lower().endswith(".csv") else pd.read_excel(upload)
        st.write("Preview")
        st.dataframe(raw.head(50), use_container_width=True, hide_index=True)
        if st.button("Add uploaded rows", type="primary"):
            count = 0
            for _, r in raw.iterrows():
                qty = int(to_money(r.get("Quantity", 1)) or 1)
                for _ in range(max(qty, 1)):
                    row = {c: r.get(c, "") for c in INVENTORY_COLUMNS}
                    row["inventory_id"] = str(uuid.uuid4())[:8]
                    row["inventory_status"] = STATUS_ACTIVE
                    row["created_at"] = now_iso()
                    add_inventory_row(row)
                    count += 1
            st.success(f"Added {count:,} inventory rows.")
            refresh_database_cache()
            st.rerun()

with tab_table:
    st.subheader("Inventory Table")
    if inv.empty:
        st.info("No inventory yet.")
    else:
        f1, f2, f3, f4 = st.columns(4)
        with f1:
            statuses = st.multiselect("Status", sorted(inv["inventory_status"].dropna().unique().tolist()), default=[])
        with f2:
            card_types = st.multiselect("Card type", sorted(inv["card_type"].dropna().unique().tolist()), default=[])
        with f3:
            product_types = st.multiselect("Product type", sorted(inv["product_type"].dropna().unique().tolist()), default=[])
        with f4:
            search = st.text_input("Search")
        view = inv.copy()
        if statuses:
            view = view[view["inventory_status"].isin(statuses)]
        if card_types:
            view = view[view["card_type"].isin(card_types)]
        if product_types:
            view = view[view["product_type"].isin(product_types)]
        if search.strip():
            q = search.lower().strip()
            view = view[view.apply(lambda r: q in str(r.get("card_name", "")).lower() or q in str(r.get("set_name", "")).lower() or q in str(r.get("card_number", "")).lower(), axis=1)]
        cols = ["inventory_id", "inventory_status", "product_type", "card_type", "set_name", "card_name", "card_number", "variant", "purchase_date", "total_cost", "market_value", "sticker_price", "sold_date", "sold_price", "profit"]
        st.caption(f"{len(view):,} item(s) shown")
        st.dataframe(view[[c for c in cols if c in view.columns]], use_container_width=True, hide_index=True)
