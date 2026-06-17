from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import streamlit as st

from core.business import load_data, refresh_database_cache
from core.cleaning import money_fmt, clean_text
from core.ebay import (
    ebay_is_configured,
    get_access_token,
    fetch_orders,
    normalize_orders_to_rows,
    upsert_ebay_order_rows,
    apply_matched_orders_to_inventory,
    fetch_order_earnings_summary,
    sync_listings,
)


st.set_page_config(page_title="eBay Store", layout="wide")
st.title("eBay Store")

st.caption(
    "This page syncs eBay orders into Google Sheets. "
    "Inventory is updated only when the eBay line item SKU equals your inventory_id."
)

data = load_data()
inv = data.inventory
orders_df = data.ebay_orders
listings_df = data.ebay_listings


if not ebay_is_configured():
    st.warning(
        "eBay is not configured yet. Add ebay_client_id, ebay_client_secret, "
        "and ebay_refresh_token to Streamlit secrets."
    )

    with st.expander("Required eBay secrets", expanded=True):
        st.code(
            '''
ebay_environment = "production"
ebay_client_id = "..."
ebay_client_secret = "..."
ebay_refresh_token = "..."
ebay_marketplace_id = "EBAY_US"
ebay_scopes = "https://api.ebay.com/oauth/api_scope/sell.fulfillment.readonly https://api.ebay.com/oauth/api_scope/sell.inventory.readonly https://api.ebay.com/oauth/api_scope/sell.finances.earnings.read"
'''
        )

else:
    c1, c2, c3 = st.columns(3)

    with c1:
        if st.button("Test eBay token", use_container_width=True):
            try:
                tok = get_access_token()
                st.success(f"Token OK. Starts with: {tok[:12]}...")
            except Exception as exc:
                st.error(str(exc))

    with c2:
        if st.button("Sync eBay listings", use_container_width=True):
            try:
                count = sync_listings()
                st.success(f"Synced {count:,} listing/inventory records from eBay.")
                refresh_database_cache()
                st.rerun()
            except Exception as exc:
                st.error(str(exc))

    with c3:
        if st.button("Refresh database", use_container_width=True):
            refresh_database_cache()
            st.rerun()

    st.markdown("---")

    st.subheader("Sync eBay orders")

    today = datetime.now(timezone.utc).date()

    col1, col2, col3 = st.columns([1, 1, 2])

    with col1:
        start_date = st.date_input("Start date", value=today - timedelta(days=30))

    with col2:
        end_date = st.date_input("End date", value=today)

    with col3:
        update_inventory = st.checkbox(
            "Mark matched inventory as SOLD after sync",
            value=True,
        )

    if st.button("Sync orders", type="primary"):
        try:
            start_dt = datetime.combine(
                start_date,
                datetime.min.time(),
                tzinfo=timezone.utc,
            )
            end_dt = datetime.combine(
                end_date,
                datetime.max.time(),
                tzinfo=timezone.utc,
            )

            orders = fetch_orders(start_dt, end_dt)

            inv_ids = (
                set(inv["inventory_id"].astype(str).str.strip())
                if not inv.empty and "inventory_id" in inv.columns
                else set()
            )

            rows = normalize_orders_to_rows(orders, inv_ids)
            inserted = upsert_ebay_order_rows(rows)

            changed = (
                apply_matched_orders_to_inventory(rows, inv)
                if update_inventory
                else 0
            )

            st.success(
                f"Fetched {len(orders):,} order(s), "
                f"added {inserted:,} new line item row(s), "
                f"updated {changed:,} inventory row(s)."
            )

            refresh_database_cache()
            st.rerun()

        except Exception as exc:
            st.error(str(exc))

    st.subheader("Financial summary from eBay")
    st.caption(
        "This uses eBay Finances order earnings summary when your developer app "
        "has access to that scope."
    )

    if st.button("Pull eBay earnings summary for selected dates"):
        try:
            summary = fetch_order_earnings_summary(
                datetime.combine(
                    start_date,
                    datetime.min.time(),
                    tzinfo=timezone.utc,
                ),
                datetime.combine(
                    end_date,
                    datetime.max.time(),
                    tzinfo=timezone.utc,
                ),
            )
            st.json(summary)
        except Exception as exc:
            st.error(str(exc))


st.markdown("---")

m1, m2, m3, m4 = st.columns(4)

if orders_df.empty:
    m1.metric("eBay orders imported", "0")
    m2.metric("eBay gross", "$0.00")
    m3.metric("eBay net", "$0.00")
    m4.metric("Unmatched lines", "0")
else:
    m1.metric("eBay lines imported", f"{len(orders_df):,}")

    gross = orders_df["sold_price"].sum() if "sold_price" in orders_df.columns else 0
    net = orders_df["net_proceeds"].sum() if "net_proceeds" in orders_df.columns else 0

    m2.metric("eBay gross", money_fmt(gross))
    m3.metric("eBay net", money_fmt(net))

    if "matched_to_inventory" in orders_df.columns:
        unmatched = orders_df[
            ~orders_df["matched_to_inventory"]
            .astype(str)
            .str.upper()
            .isin(["YES", "TRUE", "1"])
        ]
    else:
        unmatched = orders_df

    m4.metric("Unmatched lines", f"{len(unmatched):,}")


st.subheader("Imported eBay order lines")

if orders_df.empty:
    st.info("No eBay orders imported yet.")
else:
    view = orders_df.copy()

    if "sold_date" in view.columns:
        view = view.sort_values("sold_date", ascending=False)

    cols = [
        "sold_date",
        "ebay_order_id",
        "ebay_line_item_id",
        "sku",
        "inventory_id",
        "title",
        "sold_price",
        "shipping_charged",
        "fees_total",
        "net_proceeds",
        "matched_to_inventory",
        "sync_status",
    ]

    show_cols = [c for c in cols if c in view.columns]

    st.dataframe(
        view[show_cols],
        use_container_width=True,
        hide_index=True,
    )


st.subheader("eBay listings / inventory items")

if listings_df.empty:
    st.info("No eBay listing records synced yet.")
else:
    view = listings_df.copy()

    cols = [
        "sku",
        "inventory_id",
        "title",
        "listing_status",
        "price",
        "quantity",
        "offer_id",
        "listing_id",
        "last_synced_at",
    ]

    show_cols = [c for c in cols if c in view.columns]

    st.dataframe(
        view[show_cols],
        use_container_width=True,
        hide_index=True,
    )