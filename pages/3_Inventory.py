from __future__ import annotations

import uuid
from datetime import date

import pandas as pd
import streamlit as st

from core.business import load_data, add_inventory_row, refresh_database_cache
from core.cleaning import now_iso, age_bucket, money_fmt, to_money, clean_text
from core.config import (
    PRODUCT_TYPE_OPTIONS,
    CARD_TYPE_OPTIONS,
    INVENTORY_TYPE_OPTIONS,
    CONDITION_OPTIONS,
    STATUS_ACTIVE,
    STATUS_GRADING,
    STATUS_LISTED,
    STATUS_SOLD,
    INVENTORY_COLUMNS,
)


st.set_page_config(page_title="Inventory", layout="wide")
st.title("Inventory")

st.caption(
    "Inventory is the source of truth for active cards, graded cards, listed items, sold items, "
    "show sales, and eBay-matched sales."
)


# =========================================================
# Helpers
# =========================================================

def _safe_df(df: pd.DataFrame | None) -> pd.DataFrame:
    return pd.DataFrame() if df is None else df.copy()


def _date_series(df: pd.DataFrame, col: str) -> pd.Series:
    if df.empty or col not in df.columns:
        return pd.Series(pd.NaT, index=df.index)
    return pd.to_datetime(df[col], errors="coerce")


def _inventory_display_cols() -> list[str]:
    return [
        "inventory_id",
        "inventory_status",
        "inventory_type",
        "product_type",
        "card_type",
        "brand_or_league",
        "set_name",
        "year",
        "card_name",
        "card_number",
        "variant",
        "card_subtype",
        "grading_company",
        "grade",
        "purchase_date",
        "purchased_from",
        "purchase_price",
        "shipping",
        "tax",
        "total_price",
        "grading_fee",
        "total_cost",
        "market_value",
        "sticker_price",
        "condition",
        "sold_date",
        "sold_price",
        "fees_total",
        "net_proceeds",
        "profit",
        "sale_channel",
        "show_name",
        "reference_link",
        "notes",
    ]


def _make_inventory_row(
    *,
    inventory_type: str,
    product_type: str,
    card_type: str,
    brand_or_league: str,
    set_name: str,
    year: str,
    card_name: str,
    card_number: str,
    variant: str,
    card_subtype: str,
    grading_company: str,
    grade: str,
    reference_link: str,
    purchase_date_value,
    purchased_from: str,
    purchase_price: float,
    shipping: float,
    tax: float,
    sticker_price: float,
    condition: str,
    notes: str,
    sealed_product_type: str = "",
    image_url: str = "",
) -> dict:
    total_price = round(to_money(purchase_price) + to_money(shipping) + to_money(tax), 2)

    row = {c: "" for c in INVENTORY_COLUMNS}
    row.update(
        {
            "inventory_id": str(uuid.uuid4())[:8],
            "image_url": image_url,
            "inventory_type": inventory_type,
            "product_type": product_type,
            "inventory_status": STATUS_ACTIVE,
            "sealed_product_type": sealed_product_type,
            "card_type": card_type,
            "brand_or_league": brand_or_league,
            "set_name": set_name,
            "year": year,
            "card_name": card_name,
            "card_number": card_number,
            "variant": variant,
            "card_subtype": card_subtype,
            "grading_company": grading_company,
            "grade": grade,
            "reference_link": reference_link,
            "purchase_date": str(purchase_date_value) if purchase_date_value else "",
            "purchased_from": purchased_from,
            "purchase_price": round(to_money(purchase_price), 2),
            "shipping": round(to_money(shipping), 2),
            "tax": round(to_money(tax), 2),
            "total_price": total_price,
            "grading_fee": 0,
            "total_cost": total_price,
            "sticker_price": round(to_money(sticker_price), 2),
            "condition": condition,
            "notes": notes,
            "created_at": now_iso(),
        }
    )
    return row


def _bulk_col(df: pd.DataFrame, *names: str, default=""):
    lookup = {str(c).strip().lower(): c for c in df.columns}
    for name in names:
        key = str(name).strip().lower()
        if key in lookup:
            return lookup[key]
    return default


def _summary_table(df: pd.DataFrame, group_col: str) -> pd.DataFrame:
    if df.empty or group_col not in df.columns:
        return pd.DataFrame(columns=[group_col, "items", "cost", "market_value", "potential_profit"])

    tmp = df.copy()
    tmp[group_col] = tmp[group_col].astype(str).str.strip().replace("", "Unknown")

    out = (
        tmp.groupby(group_col, dropna=False)
        .agg(
            items=("inventory_id", "count"),
            cost=("total_cost", "sum"),
            market_value=("market_value", "sum"),
        )
        .reset_index()
    )
    out["potential_profit"] = out["market_value"] - out["cost"]
    return out.sort_values("market_value", ascending=False)


# =========================================================
# Top actions
# =========================================================

top1, top2 = st.columns([1, 4])

with top1:
    if st.button("Refresh database", use_container_width=True):
        refresh_database_cache()
        st.rerun()

with top2:
    st.info(
        "Market value refresh is handled separately on the Dashboard so regular inventory work stays faster.",
        icon="ℹ️",
    )


# =========================================================
# Load data
# =========================================================

data = load_data()
inv = _safe_df(data.inventory)

if inv.empty:
    active = inv.copy()
else:
    inv["inventory_status"] = inv["inventory_status"].astype(str).str.upper().str.strip()
    active = inv[
        inv["inventory_status"].isin([STATUS_ACTIVE, STATUS_GRADING, STATUS_LISTED])
    ].copy()


tab_overview, tab_add, tab_bulk, tab_table = st.tabs(
    ["Overview", "Add Single", "Bulk Add", "Inventory Table"]
)


# =========================================================
# Overview
# =========================================================

with tab_overview:
    st.subheader("Inventory Overview")

    if inv.empty:
        st.info("No inventory loaded yet.")
    else:
        sold = inv[inv["inventory_status"].eq(STATUS_SOLD)].copy()

        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Active / available", f"{len(active):,}")
        c2.metric("Sold", f"{len(sold):,}")
        c3.metric("Active cost", money_fmt(active["total_cost"].sum()))
        c4.metric("Active market", money_fmt(active["market_value"].sum()))
        c5.metric(
            "Potential profit",
            money_fmt(active["market_value"].sum() - active["total_cost"].sum()),
        )

        st.markdown("### Breakdown")

        b1, b2 = st.columns(2)

        with b1:
            st.markdown("#### By set")
            by_set = _summary_table(active, "set_name")
            st.dataframe(
                by_set.head(50).style.format(
                    {
                        "cost": "${:,.2f}",
                        "market_value": "${:,.2f}",
                        "potential_profit": "${:,.2f}",
                    }
                ),
                use_container_width=True,
                hide_index=True,
            )

        with b2:
            st.markdown("#### By product type")
            by_product = _summary_table(active, "product_type")
            st.dataframe(
                by_product.style.format(
                    {
                        "cost": "${:,.2f}",
                        "market_value": "${:,.2f}",
                        "potential_profit": "${:,.2f}",
                    }
                ),
                use_container_width=True,
                hide_index=True,
            )

        st.markdown("### Inventory Age")

        tmp = active.copy()
        tmp["purchase_dt"] = _date_series(tmp, "purchase_date")
        tmp["age_days"] = (pd.Timestamp(date.today()) - tmp["purchase_dt"]).dt.days
        tmp["age_bucket"] = tmp["age_days"].apply(age_bucket)

        by_age = (
            tmp.groupby("age_bucket", dropna=False)
            .agg(
                items=("inventory_id", "count"),
                cost=("total_cost", "sum"),
                market_value=("market_value", "sum"),
            )
            .reset_index()
        )
        by_age["potential_profit"] = by_age["market_value"] - by_age["cost"]

        st.dataframe(
            by_age.style.format(
                {
                    "cost": "${:,.2f}",
                    "market_value": "${:,.2f}",
                    "potential_profit": "${:,.2f}",
                }
            ),
            use_container_width=True,
            hide_index=True,
        )

        st.markdown("### Oldest active inventory")

        oldest = tmp[tmp["purchase_dt"].notna()].sort_values("purchase_dt").head(25)
        oldest_cols = [
            "inventory_id",
            "purchase_date",
            "age_bucket",
            "inventory_status",
            "product_type",
            "set_name",
            "card_name",
            "card_number",
            "total_cost",
            "market_value",
            "sticker_price",
        ]

        st.dataframe(
            oldest[[c for c in oldest_cols if c in oldest.columns]],
            use_container_width=True,
            hide_index=True,
        )


# =========================================================
# Add Single
# =========================================================

with tab_add:
    st.subheader("Add Single Inventory Item")

    with st.form("add_single_inventory", clear_on_submit=True):
        c1, c2, c3, c4 = st.columns(4)

        with c1:
            product_type = st.selectbox("Product type*", PRODUCT_TYPE_OPTIONS)
            inventory_type = st.selectbox("Inventory type*", INVENTORY_TYPE_OPTIONS)
            card_type = st.selectbox("Card type*", CARD_TYPE_OPTIONS)

        with c2:
            brand_or_league = st.text_input("Brand / League", value="Pokemon TCG")
            set_name = st.text_input("Set")
            year = st.text_input("Year")

        with c3:
            card_name = st.text_input("Card / item name*")
            card_number = st.text_input("Card #")
            variant = st.text_input("Variant")

        with c4:
            card_subtype = st.text_input("Subtype")
            sealed_product_type = st.text_input("Sealed product type")
            reference_link = st.text_input("Reference link")

        c5, c6, c7, c8 = st.columns(4)

        with c5:
            purchase_date_value = st.date_input("Purchase date", value=date.today())
            purchased_from = st.text_input("Purchased from")

        with c6:
            purchase_price = st.number_input(
                "Purchase price",
                min_value=0.0,
                step=1.0,
                format="%.2f",
            )
            shipping = st.number_input(
                "Shipping",
                min_value=0.0,
                step=0.5,
                format="%.2f",
            )

        with c7:
            tax = st.number_input(
                "Tax",
                min_value=0.0,
                step=0.5,
                format="%.2f",
            )
            sticker_price = st.number_input(
                "Sticker price",
                min_value=0.0,
                step=1.0,
                format="%.2f",
            )

        with c8:
            grading_company = st.text_input("Grading company")
            grade = st.text_input("Grade")
            condition = st.selectbox("Condition", CONDITION_OPTIONS, index=0)

        image_url = st.text_input("Image URL")
        notes = st.text_area("Notes")

        submitted = st.form_submit_button("Add item", type="primary")

    if submitted:
        if not clean_text(card_name) and not clean_text(reference_link):
            st.error("Add at least a card/item name or a reference link.")
        else:
            row = _make_inventory_row(
                inventory_type=inventory_type,
                product_type=product_type,
                card_type=card_type,
                brand_or_league=brand_or_league,
                set_name=set_name,
                year=year,
                card_name=card_name,
                card_number=card_number,
                variant=variant,
                card_subtype=card_subtype,
                grading_company=grading_company,
                grade=grade,
                reference_link=reference_link,
                purchase_date_value=purchase_date_value,
                purchased_from=purchased_from,
                purchase_price=purchase_price,
                shipping=shipping,
                tax=tax,
                sticker_price=sticker_price,
                condition=condition,
                notes=notes,
                sealed_product_type=sealed_product_type,
                image_url=image_url,
            )

            add_inventory_row(row)
            refresh_database_cache()
            st.success(f"Added {card_name or reference_link} to inventory.")


# =========================================================
# Bulk Add
# =========================================================

with tab_bulk:
    st.subheader("Bulk Add Inventory")

    st.caption(
        "Upload a CSV or Excel file. If you include a Quantity column, the app creates one inventory row per quantity."
    )

    template_cols = [
        "inventory_type",
        "product_type",
        "card_type",
        "brand_or_league",
        "set_name",
        "year",
        "card_name",
        "card_number",
        "variant",
        "card_subtype",
        "sealed_product_type",
        "grading_company",
        "grade",
        "reference_link",
        "purchase_date",
        "purchased_from",
        "purchase_price",
        "shipping",
        "tax",
        "sticker_price",
        "condition",
        "notes",
        "image_url",
        "Quantity",
    ]

    template = pd.DataFrame([{c: "" for c in template_cols}])

    st.download_button(
        "Download bulk template",
        data=template.to_csv(index=False),
        file_name="inventory_upload_template.csv",
        mime="text/csv",
    )

    uploaded = st.file_uploader(
        "Upload inventory CSV/XLSX",
        type=["csv", "xlsx", "xls"],
    )

    if uploaded is not None:
        try:
            raw = (
                pd.read_csv(uploaded)
                if uploaded.name.lower().endswith(".csv")
                else pd.read_excel(uploaded)
            )

            st.markdown("#### Preview")
            st.dataframe(raw.head(100), use_container_width=True, hide_index=True)

            st.markdown("#### Defaults for blank upload fields")

            d1, d2, d3, d4 = st.columns(4)
            with d1:
                default_inventory_type = st.selectbox(
                    "Default inventory type",
                    INVENTORY_TYPE_OPTIONS,
                    index=0,
                )
            with d2:
                default_product_type = st.selectbox(
                    "Default product type",
                    PRODUCT_TYPE_OPTIONS,
                    index=0,
                )
            with d3:
                default_card_type = st.selectbox(
                    "Default card type",
                    CARD_TYPE_OPTIONS,
                    index=0,
                )
            with d4:
                default_condition = st.selectbox(
                    "Default condition",
                    CONDITION_OPTIONS,
                    index=0,
                )

            if st.button("Add uploaded rows", type="primary"):
                count = 0

                col_inventory_type = _bulk_col(raw, "inventory_type", "Inventory Type")
                col_product_type = _bulk_col(raw, "product_type", "Product Type")
                col_card_type = _bulk_col(raw, "card_type", "Card Type")
                col_brand = _bulk_col(raw, "brand_or_league", "Brand/League", "Brand / League")
                col_set = _bulk_col(raw, "set_name", "Set", "Set Name")
                col_year = _bulk_col(raw, "year", "Year")
                col_card_name = _bulk_col(raw, "card_name", "Card Name", "Item Name")
                col_card_number = _bulk_col(raw, "card_number", "Card #", "Card Number")
                col_variant = _bulk_col(raw, "variant", "Variant")
                col_subtype = _bulk_col(raw, "card_subtype", "Card Subtype", "Subtype")
                col_sealed_type = _bulk_col(raw, "sealed_product_type", "Sealed Product Type")
                col_grading_company = _bulk_col(raw, "grading_company", "Grading Company")
                col_grade = _bulk_col(raw, "grade", "Grade")
                col_link = _bulk_col(raw, "reference_link", "Reference Link")
                col_purchase_date = _bulk_col(raw, "purchase_date", "Purchase Date")
                col_purchased_from = _bulk_col(raw, "purchased_from", "Purchased From")
                col_purchase_price = _bulk_col(raw, "purchase_price", "Purchase Price")
                col_shipping = _bulk_col(raw, "shipping", "Shipping")
                col_tax = _bulk_col(raw, "tax", "Tax")
                col_sticker = _bulk_col(raw, "sticker_price", "Sticker Price")
                col_condition = _bulk_col(raw, "condition", "Condition")
                col_notes = _bulk_col(raw, "notes", "Notes")
                col_image = _bulk_col(raw, "image_url", "Image URL")
                col_qty = _bulk_col(raw, "Quantity", "quantity", "Qty", default="")

                for _, r in raw.iterrows():
                    qty = int(max(to_money(r.get(col_qty, 1)) if col_qty else 1, 1))

                    for _ in range(qty):
                        row = _make_inventory_row(
                            inventory_type=clean_text(r.get(col_inventory_type, "")) or default_inventory_type,
                            product_type=clean_text(r.get(col_product_type, "")) or default_product_type,
                            card_type=clean_text(r.get(col_card_type, "")) or default_card_type,
                            brand_or_league=clean_text(r.get(col_brand, "")) or "Pokemon TCG",
                            set_name=clean_text(r.get(col_set, "")),
                            year=clean_text(r.get(col_year, "")),
                            card_name=clean_text(r.get(col_card_name, "")),
                            card_number=clean_text(r.get(col_card_number, "")),
                            variant=clean_text(r.get(col_variant, "")),
                            card_subtype=clean_text(r.get(col_subtype, "")),
                            grading_company=clean_text(r.get(col_grading_company, "")),
                            grade=clean_text(r.get(col_grade, "")),
                            reference_link=clean_text(r.get(col_link, "")),
                            purchase_date_value=clean_text(r.get(col_purchase_date, "")) or date.today(),
                            purchased_from=clean_text(r.get(col_purchased_from, "")),
                            purchase_price=to_money(r.get(col_purchase_price, 0)),
                            shipping=to_money(r.get(col_shipping, 0)),
                            tax=to_money(r.get(col_tax, 0)),
                            sticker_price=to_money(r.get(col_sticker, 0)),
                            condition=clean_text(r.get(col_condition, "")) or default_condition,
                            notes=clean_text(r.get(col_notes, "")),
                            sealed_product_type=clean_text(r.get(col_sealed_type, "")),
                            image_url=clean_text(r.get(col_image, "")),
                        )

                        add_inventory_row(row)
                        count += 1

                refresh_database_cache()
                st.success(f"Added {count:,} inventory row(s).")
                st.rerun()

        except Exception as exc:
            st.error(f"Could not process upload: {exc}")


# =========================================================
# Inventory Table
# =========================================================

with tab_table:
    st.subheader("Inventory Table")

    if inv.empty:
        st.info("No inventory loaded yet.")
    else:
        f1, f2, f3, f4 = st.columns(4)

        with f1:
            status_options = sorted(inv["inventory_status"].dropna().astype(str).unique().tolist())
            selected_statuses = st.multiselect(
                "Status",
                status_options,
                default=[],
            )

        with f2:
            product_options = sorted(inv["product_type"].dropna().astype(str).unique().tolist())
            selected_products = st.multiselect(
                "Product type",
                product_options,
                default=[],
            )

        with f3:
            card_type_options = sorted(inv["card_type"].dropna().astype(str).unique().tolist())
            selected_card_types = st.multiselect(
                "Card type",
                card_type_options,
                default=[],
            )

        with f4:
            search = st.text_input("Search card, set, number, variant")

        view = inv.copy()

        if selected_statuses:
            view = view[view["inventory_status"].isin(selected_statuses)]

        if selected_products:
            view = view[view["product_type"].isin(selected_products)]

        if selected_card_types:
            view = view[view["card_type"].isin(selected_card_types)]

        if search.strip():
            q = search.lower().strip()

            def row_match(r) -> bool:
                fields = [
                    r.get("card_name", ""),
                    r.get("set_name", ""),
                    r.get("card_number", ""),
                    r.get("variant", ""),
                    r.get("card_subtype", ""),
                    r.get("inventory_id", ""),
                    r.get("reference_link", ""),
                ]
                return q in " ".join([str(x).lower() for x in fields])

            view = view[view.apply(row_match, axis=1)]

        sort_col = st.selectbox(
            "Sort by",
            [
                "purchase_date",
                "market_value",
                "total_cost",
                "sticker_price",
                "profit",
                "sold_date",
                "card_name",
                "set_name",
            ],
            index=0,
        )

        sort_ascending = st.checkbox("Sort ascending", value=False)

        if sort_col in view.columns:
            if sort_col in ["purchase_date", "sold_date"]:
                view["__sort_dt"] = pd.to_datetime(view[sort_col], errors="coerce")
                view = view.sort_values("__sort_dt", ascending=sort_ascending, na_position="last")
                view = view.drop(columns=["__sort_dt"], errors="ignore")
            else:
                view = view.sort_values(sort_col, ascending=sort_ascending, na_position="last")

        st.caption(f"{len(view):,} item(s) shown")

        display_cols = [c for c in _inventory_display_cols() if c in view.columns]

        st.dataframe(
            view[display_cols],
            use_container_width=True,
            hide_index=True,
        )

        csv = view[display_cols].to_csv(index=False)

        st.download_button(
            "Download filtered inventory CSV",
            data=csv,
            file_name="filtered_inventory.csv",
            mime="text/csv",
        )