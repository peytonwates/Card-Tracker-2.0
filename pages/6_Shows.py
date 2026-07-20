from __future__ import annotations

import uuid
from datetime import date

import pandas as pd
import streamlit as st

from core.business import load_data, refresh_database_cache, mark_inventory_sold
from core.cleaning import now_iso, clean_text, to_money, money_fmt
from core.config import (
    SHOW_COLUMNS,
    STATUS_ACTIVE,
    STATUS_LISTED,
    STATUS_SOLD,
)
from core.sheets import get_ws_name, append_rows


st.set_page_config(page_title="Shows", layout="wide")
st.title("Shows")

st.caption(
    "Manage card shows and record show sales. Pricing-for-shows has been removed."
)


# =========================================================
# Helpers
# =========================================================

def _safe_df(df: pd.DataFrame | None) -> pd.DataFrame:
    return pd.DataFrame() if df is None else df.copy()


def _date_sort(df: pd.DataFrame, col: str, ascending: bool = False) -> pd.DataFrame:
    if df.empty or col not in df.columns:
        return df
    out = df.copy()
    out["__sort_dt"] = pd.to_datetime(out[col], errors="coerce")
    out = out.sort_values("__sort_dt", ascending=ascending, na_position="last")
    return out.drop(columns=["__sort_dt"], errors="ignore")


def _show_label(row: pd.Series) -> str:
    show_id = clean_text(row.get("show_id"))
    name = clean_text(row.get("show_name"))
    show_date = clean_text(row.get("show_date"))
    location = clean_text(row.get("location"))

    label = f"{show_id} — {name}"
    if show_date:
        label += f" ({show_date})"
    if location:
        label += f" — {location}"
    return label


def _item_label(row: pd.Series) -> str:
    inv_id = clean_text(row.get("inventory_id"))
    set_name = clean_text(row.get("set_name"))
    card_name = clean_text(row.get("card_name"))
    card_number = clean_text(row.get("card_number"))
    grade = clean_text(row.get("grade"))
    status = clean_text(row.get("inventory_status"))

    label = f"{inv_id} — {card_name}"

    if card_number:
        label += f" #{card_number}"
    if set_name:
        label += f" — {set_name}"
    if grade:
        label += f" — Grade {grade}"

    label += f" — {status} — Cost {money_fmt(row.get('total_cost'))}"

    sticker = to_money(row.get("sticker_price"))
    if sticker > 0:
        label += f" — Sticker {money_fmt(sticker)}"

    return label


def _display_inventory_cols() -> list[str]:
    return [
        "inventory_id",
        "inventory_status",
        "product_type",
        "inventory_type",
        "card_type",
        "set_name",
        "card_name",
        "card_number",
        "variant",
        "card_subtype",
        "grading_company",
        "grade",
        "purchase_date",
        "total_cost",
        "market_value",
        "sticker_price",
        "condition",
    ]


def _display_sale_cols() -> list[str]:
    return [
        "sold_date",
        "show_name",
        "inventory_id",
        "product_type",
        "set_name",
        "card_name",
        "card_number",
        "variant",
        "grade",
        "sold_price",
        "fees_total",
        "net_proceeds",
        "total_cost",
        "profit",
        "sale_notes",
    ]


def _build_sale_editor(selected_rows: pd.DataFrame) -> pd.DataFrame:
    editor = selected_rows.copy()

    editor["sold_price"] = editor["sticker_price"].apply(to_money)
    editor["sold_price"] = editor["sold_price"].where(
        editor["sold_price"] > 0,
        editor["market_value"].apply(to_money),
    )

    editor["fees"] = 0.0
    editor["sale_notes"] = ""

    keep = [
        "inventory_id",
        "set_name",
        "card_name",
        "card_number",
        "variant",
        "grade",
        "total_cost",
        "market_value",
        "sticker_price",
        "sold_price",
        "fees",
        "sale_notes",
    ]

    return editor[[c for c in keep if c in editor.columns]].copy()


def _show_sale_updates(
    *,
    selected_show: pd.Series,
    sale_date: date | str,
    sold_price: float,
    fees: float,
    total_cost: float,
    sale_notes: str = "",
) -> dict:
    sold_price = round(to_money(sold_price), 2)
    fees = round(to_money(fees), 2)
    total_cost = round(to_money(total_cost), 2)
    net = round(sold_price - fees, 2)
    profit = round(net - total_cost, 2)

    return {
        "transaction_type": "Card Show",
        "platform": "",
        "sold_date": str(sale_date),
        "sold_price": sold_price,
        "fees": fees,
        "fees_total": fees,
        "shipping_charged": 0,
        "net_proceeds": net,
        "profit": profit,
        "sale_channel": "Card Show",
        "sale_notes": clean_text(sale_notes),
        "show_id": clean_text(selected_show.get("show_id")),
        "show_name": clean_text(selected_show.get("show_name")),
        "sold_transaction_id": str(uuid.uuid4()),
        "sold_created_at": now_iso(),
        "sold_updated_at": now_iso(),
    }


def _bulk_sale_export_cols() -> list[str]:
    return [
        "mark_sold",
        "sold_price",
        "fees",
        "sold_date",
        "sale_notes",
        "show_id",
        "show_name",
        "inventory_id",
        "inventory_status",
        "product_type",
        "inventory_type",
        "card_type",
        "set_name",
        "card_name",
        "card_number",
        "variant",
        "card_subtype",
        "grading_company",
        "grade",
        "condition",
        "purchase_date",
        "total_cost",
        "market_value",
        "sticker_price",
    ]


def _build_bulk_sale_template(
    inventory: pd.DataFrame,
    selected_show: pd.Series,
    default_sale_date: date,
) -> pd.DataFrame:
    template = inventory.copy()

    for col in ["sticker_price", "market_value"]:
        if col not in template.columns:
            template[col] = 0.0

    suggested_price = template["sticker_price"].apply(to_money)
    market_price = template["market_value"].apply(to_money)

    # Assign instead of DataFrame.insert because inventory may already contain
    # historical sale columns such as sold_price, sold_date, show_id, or show_name.
    template["mark_sold"] = ""
    template["sold_price"] = suggested_price.where(suggested_price > 0, market_price)
    template["fees"] = 0.0
    template["sold_date"] = str(default_sale_date)
    template["sale_notes"] = ""
    template["show_id"] = clean_text(selected_show.get("show_id"))
    template["show_name"] = clean_text(selected_show.get("show_name"))

    cols = [c for c in _bulk_sale_export_cols() if c in template.columns]
    return template[cols].copy()


def _read_bulk_sale_upload(uploaded_file) -> pd.DataFrame:
    filename = clean_text(getattr(uploaded_file, "name", "")).lower()

    if filename.endswith(".xlsx"):
        uploaded = pd.read_excel(uploaded_file, dtype=str)
    else:
        uploaded = pd.read_csv(uploaded_file, dtype=str, keep_default_na=False)

    uploaded.columns = [
        clean_text(col).replace("\ufeff", "").lower().replace(" ", "_")
        for col in uploaded.columns
    ]

    aliases = {
        "sold": "mark_sold",
        "mark_as_sold": "mark_sold",
        "sale_price": "sold_price",
        "price_sold": "sold_price",
        "fee": "fees",
        "fees_total": "fees",
        "notes": "sale_notes",
        "date_sold": "sold_date",
    }
    uploaded = uploaded.rename(columns={k: v for k, v in aliases.items() if k in uploaded.columns})
    return uploaded


def _is_marked_sold(value) -> bool:
    return clean_text(value).lower() in {
        "1",
        "true",
        "t",
        "yes",
        "y",
        "x",
        "sold",
        "sell",
    }


def _parse_sale_date(value, fallback: date) -> str:
    raw = clean_text(value)
    if not raw:
        return str(fallback)

    parsed = pd.to_datetime(raw, errors="coerce")
    if pd.isna(parsed):
        return ""
    return str(parsed.date())


def _validate_bulk_sale_upload(
    uploaded: pd.DataFrame,
    inventory: pd.DataFrame,
    selected_show: pd.Series,
    fallback_sale_date: date,
) -> pd.DataFrame:
    results: list[dict] = []

    if "mark_sold" not in uploaded.columns:
        return pd.DataFrame(
            [
                {
                    "row": "",
                    "inventory_id": "",
                    "card_name": "",
                    "sold_price": 0.0,
                    "fees": 0.0,
                    "net_proceeds": 0.0,
                    "total_cost": 0.0,
                    "profit": 0.0,
                    "sold_date": "",
                    "sale_notes": "",
                    "is_valid": False,
                    "validation": "Missing required mark_sold column.",
                }
            ]
        )

    marked = uploaded[uploaded["mark_sold"].apply(_is_marked_sold)].copy()
    if marked.empty:
        return pd.DataFrame()

    for required in ["inventory_id", "sold_price"]:
        if required not in marked.columns:
            marked[required] = ""

    for optional in ["fees", "sold_date", "sale_notes", "show_id", "show_name"]:
        if optional not in marked.columns:
            marked[optional] = ""

    inventory_lookup = inventory.copy()
    inventory_lookup["inventory_id"] = inventory_lookup["inventory_id"].astype(str).str.strip()

    upload_id_counts = marked["inventory_id"].apply(clean_text).value_counts()

    for row_index, row in marked.iterrows():
        inv_id = clean_text(row.get("inventory_id"))
        sold_price = round(to_money(row.get("sold_price")), 2)
        fees = round(to_money(row.get("fees")), 2)
        sold_date = _parse_sale_date(row.get("sold_date"), fallback_sale_date)
        sale_notes = clean_text(row.get("sale_notes"))
        uploaded_show_id = clean_text(row.get("show_id"))
        selected_show_id = clean_text(selected_show.get("show_id"))

        matches = inventory_lookup[inventory_lookup["inventory_id"].eq(inv_id)]
        validation: list[str] = []
        current = pd.Series(dtype=object)

        if not inv_id:
            validation.append("Missing inventory_id")
        elif upload_id_counts.get(inv_id, 0) > 1:
            validation.append("Duplicate inventory_id in upload")
        elif matches.empty:
            validation.append("inventory_id not found")
        elif len(matches) > 1:
            validation.append("inventory_id is duplicated in the inventory database")
        else:
            current = matches.iloc[0]
            current_status = clean_text(current.get("inventory_status")).upper()
            if current_status not in {STATUS_ACTIVE, STATUS_LISTED}:
                validation.append(f"Current status is {current_status or 'blank'}, not ACTIVE/LISTED")

        if uploaded_show_id and selected_show_id and uploaded_show_id != selected_show_id:
            validation.append(
                f"File show_id {uploaded_show_id} does not match selected show {selected_show_id}"
            )

        if sold_price <= 0:
            validation.append("Sold price must be greater than $0")
        if fees < 0:
            validation.append("Fees cannot be negative")
        if not sold_date:
            validation.append("Invalid sold date")

        total_cost = to_money(current.get("total_cost")) if not current.empty else 0.0
        net = round(sold_price - fees, 2)
        profit = round(net - total_cost, 2)

        results.append(
            {
                "row": int(row_index) + 2,
                "inventory_id": inv_id,
                "card_name": clean_text(current.get("card_name")) if not current.empty else "",
                "sold_price": sold_price,
                "fees": fees,
                "net_proceeds": net,
                "total_cost": round(total_cost, 2),
                "profit": profit,
                "sold_date": sold_date,
                "sale_notes": sale_notes,
                "is_valid": not validation,
                "validation": "; ".join(validation) if validation else "Ready",
            }
        )

    return pd.DataFrame(results)


# =========================================================
# Top actions / load
# =========================================================

top1, top2 = st.columns([1, 4])

with top1:
    if st.button("Refresh database", use_container_width=True):
        refresh_database_cache()
        st.rerun()

with top2:
    st.info(
        "Show sales update inventory directly. Sold items will move to SOLD and feed the Dashboard.",
        icon="ℹ️",
    )

data = load_data()
shows = _safe_df(data.shows)
inv = _safe_df(data.inventory)

if not shows.empty:
    shows["show_id"] = shows["show_id"].astype(str).str.strip()
    shows["show_name"] = shows["show_name"].astype(str).str.strip()
    shows["status"] = shows["status"].astype(str).str.strip()

if not inv.empty:
    inv["inventory_id"] = inv["inventory_id"].astype(str).str.strip()
    inv["inventory_status"] = inv["inventory_status"].astype(str).str.upper().str.strip()


if "show_bulk_success" in st.session_state:
    st.success(st.session_state.pop("show_bulk_success"))


tab_manage, tab_record, tab_bulk, tab_summary, tab_inventory = st.tabs(
    [
        "Manage Shows",
        "Record Show Sale",
        "Bulk Show Sale Upload",
        "Show Summary",
        "Show Inventory View",
    ]
)


# =========================================================
# Manage Shows
# =========================================================

with tab_manage:
    st.subheader("Add / View Shows")

    with st.form("add_show_form", clear_on_submit=True):
        c1, c2, c3 = st.columns([1, 1, 1])

        with c1:
            show_name = st.text_input("Show name*")
            show_date = st.date_input("Show date*", value=date.today())

        with c2:
            location = st.text_input("Location")
            status = st.selectbox(
                "Status",
                ["Planned", "Completed", "Cancelled"],
                index=0,
            )

        with c3:
            description = st.text_area("Description / notes", height=90)

        submitted = st.form_submit_button("Add show", type="primary")

    if submitted:
        if not clean_text(show_name):
            st.error("Show name is required.")
        else:
            row = {
                "show_id": str(uuid.uuid4())[:8],
                "show_name": clean_text(show_name),
                "show_date": str(show_date),
                "location": clean_text(location),
                "description": clean_text(description),
                "status": clean_text(status) or "Planned",
                "created_at": now_iso(),
                "updated_at": now_iso(),
            }

            append_rows(
                get_ws_name("shows_worksheet", "shows"),
                SHOW_COLUMNS,
                [row],
            )

            refresh_database_cache()
            st.success(f"Added show: {show_name}")
            st.rerun()

    st.markdown("### Shows")

    if shows.empty:
        st.info("No shows added yet.")
    else:
        view = _date_sort(shows, "show_date", ascending=False)

        show_cols = [
            "show_id",
            "show_name",
            "show_date",
            "location",
            "status",
            "description",
            "created_at",
            "updated_at",
        ]

        st.dataframe(
            view[[c for c in show_cols if c in view.columns]],
            use_container_width=True,
            hide_index=True,
        )


# =========================================================
# Record Show Sale
# =========================================================

with tab_record:
    st.subheader("Record Show Sale")

    if shows.empty:
        st.info("Add a show first before recording show sales.")
    elif inv.empty:
        st.info("No inventory loaded.")
    else:
        ready = inv[
            inv["inventory_status"].isin([STATUS_ACTIVE, STATUS_LISTED])
        ].copy()

        if ready.empty:
            st.info("No ACTIVE or LISTED inventory is available to sell.")
        else:
            shows_for_select = _date_sort(shows, "show_date", ascending=False).copy()
            shows_for_select["label"] = shows_for_select.apply(_show_label, axis=1)

            show_label = st.selectbox(
                "Show",
                shows_for_select["label"].tolist(),
            )

            selected_show = shows_for_select[
                shows_for_select["label"].eq(show_label)
            ].iloc[0]

            st.markdown("### Filter inventory")

            f1, f2, f3, f4 = st.columns(4)

            with f1:
                product_options = sorted(
                    ready["product_type"].dropna().astype(str).str.strip().unique().tolist()
                )
                selected_products = st.multiselect("Product type", product_options)

            with f2:
                inv_type_options = sorted(
                    ready["inventory_type"].dropna().astype(str).str.strip().unique().tolist()
                )
                selected_inv_types = st.multiselect("Inventory type", inv_type_options)

            with f3:
                set_options = sorted(
                    ready["set_name"].dropna().astype(str).str.strip().replace("", pd.NA).dropna().unique().tolist()
                )
                selected_sets = st.multiselect("Set", set_options)

            with f4:
                search = st.text_input("Search card / set / number / ID")

            filtered = ready.copy()

            if selected_products:
                filtered = filtered[filtered["product_type"].isin(selected_products)]

            if selected_inv_types:
                filtered = filtered[filtered["inventory_type"].isin(selected_inv_types)]

            if selected_sets:
                filtered = filtered[filtered["set_name"].isin(selected_sets)]

            if search.strip():
                q = search.lower().strip()

                def _match(row: pd.Series) -> bool:
                    fields = [
                        row.get("inventory_id", ""),
                        row.get("set_name", ""),
                        row.get("card_name", ""),
                        row.get("card_number", ""),
                        row.get("variant", ""),
                        row.get("grade", ""),
                    ]
                    return q in " ".join(str(x).lower() for x in fields)

                filtered = filtered[filtered.apply(_match, axis=1)]

            filtered["label"] = filtered.apply(_item_label, axis=1)

            st.caption(f"{len(filtered):,} available item(s) match your filters.")

            selected_labels = st.multiselect(
                "Select sold item(s)",
                filtered["label"].tolist(),
            )

            if selected_labels:
                selected_rows = filtered[
                    filtered["label"].isin(selected_labels)
                ].copy()

                st.markdown("### Sale details")

                sale_date = st.date_input("Sold date", value=date.today())

                st.caption(
                    "Edit sold price and fees per item below. Fees are usually $0 for cash show sales."
                )

                editor_df = _build_sale_editor(selected_rows)

                edited = st.data_editor(
                    editor_df,
                    use_container_width=True,
                    hide_index=True,
                    column_config={
                        "sold_price": st.column_config.NumberColumn(
                            "Sold price",
                            min_value=0.0,
                            step=1.0,
                            format="$%.2f",
                        ),
                        "fees": st.column_config.NumberColumn(
                            "Fees",
                            min_value=0.0,
                            step=1.0,
                            format="$%.2f",
                        ),
                        "total_cost": st.column_config.NumberColumn(
                            "Total cost",
                            format="$%.2f",
                            disabled=True,
                        ),
                        "market_value": st.column_config.NumberColumn(
                            "Market value",
                            format="$%.2f",
                            disabled=True,
                        ),
                        "sticker_price": st.column_config.NumberColumn(
                            "Sticker price",
                            format="$%.2f",
                            disabled=True,
                        ),
                    },
                    disabled=[
                        "inventory_id",
                        "set_name",
                        "card_name",
                        "card_number",
                        "variant",
                        "grade",
                        "total_cost",
                        "market_value",
                        "sticker_price",
                    ],
                )

                total_sales = edited["sold_price"].apply(to_money).sum()
                total_fees = edited["fees"].apply(to_money).sum()
                total_cost = edited["total_cost"].apply(to_money).sum()
                total_profit = total_sales - total_fees - total_cost

                m1, m2, m3, m4 = st.columns(4)
                m1.metric("Selected items", f"{len(edited):,}")
                m2.metric("Sales", money_fmt(total_sales))
                m3.metric("Fees", money_fmt(total_fees))
                m4.metric("Estimated profit", money_fmt(total_profit))

                if st.button("Record selected show sale(s)", type="primary"):
                    changed = 0

                    for _, row in edited.iterrows():
                        inv_id = clean_text(row.get("inventory_id"))
                        sold_price = to_money(row.get("sold_price"))
                        fees = to_money(row.get("fees"))
                        total_cost_row = to_money(row.get("total_cost"))

                        updates = _show_sale_updates(
                            selected_show=selected_show,
                            sale_date=sale_date,
                            sold_price=sold_price,
                            fees=fees,
                            total_cost=total_cost_row,
                            sale_notes=row.get("sale_notes"),
                        )

                        changed += mark_inventory_sold(inv_id, updates)

                    refresh_database_cache()
                    st.success(f"Recorded {changed:,} show sale(s).")
                    st.rerun()

            else:
                st.info("Select one or more sold items to enter sale details.")


# =========================================================
# Bulk Show Sale Upload
# =========================================================

with tab_bulk:
    st.subheader("Bulk Show Sale Upload")
    st.caption(
        "Download a sale template, mark multiple inventory rows as sold, then upload it to update inventory, net proceeds, and profit in one step."
    )

    if shows.empty:
        st.info("Add a show first before using the bulk sale upload.")
    elif inv.empty:
        st.info("No inventory loaded.")
    else:
        bulk_ready = inv[
            inv["inventory_status"].isin([STATUS_ACTIVE, STATUS_LISTED])
        ].copy()

        if bulk_ready.empty:
            st.info("No ACTIVE or LISTED inventory is available to sell.")
        else:
            bulk_shows = _date_sort(shows, "show_date", ascending=False).copy()
            bulk_shows["label"] = bulk_shows.apply(_show_label, axis=1)

            b1, b2 = st.columns([2, 1])

            with b1:
                bulk_show_label = st.selectbox(
                    "Show for this upload",
                    bulk_shows["label"].tolist(),
                    key="bulk_show_label",
                )

            bulk_selected_show = bulk_shows[
                bulk_shows["label"].eq(bulk_show_label)
            ].iloc[0]

            parsed_show_date = pd.to_datetime(
                bulk_selected_show.get("show_date"),
                errors="coerce",
            )
            bulk_default_date = (
                parsed_show_date.date()
                if not pd.isna(parsed_show_date)
                else date.today()
            )

            with b2:
                bulk_sale_date = st.date_input(
                    "Default sold date",
                    value=bulk_default_date,
                    key="bulk_sale_date",
                    help="Used when the uploaded sold_date cell is blank.",
                )

            scope = st.radio(
                "Inventory to include in the download",
                ["All ACTIVE/LISTED inventory", "Show Inventory only"],
                horizontal=True,
                key="bulk_inventory_scope",
            )

            export_inventory = bulk_ready.copy()
            if scope == "Show Inventory only":
                export_inventory = export_inventory[
                    export_inventory["inventory_type"]
                    .astype(str)
                    .str.lower()
                    .str.contains("show", na=False)
                ].copy()

            st.markdown("### 1. Download and edit the template")
            st.info(
                "In the **mark_sold** column, enter X, YES, TRUE, 1, or SOLD only for items that sold. Update sold_price, fees, sold_date, and sale_notes as needed. Do not change inventory_id.",
                icon="ℹ️",
            )

            bulk_template = _build_bulk_sale_template(
                export_inventory,
                bulk_selected_show,
                bulk_sale_date,
            )

            d1, d2, d3, d4 = st.columns(4)
            d1.metric("Rows in template", f"{len(bulk_template):,}")
            d2.metric(
                "Inventory cost",
                money_fmt(export_inventory["total_cost"].apply(to_money).sum()),
            )
            d3.metric(
                "Market value",
                money_fmt(export_inventory["market_value"].apply(to_money).sum()),
            )
            d4.metric(
                "Sticker total",
                money_fmt(export_inventory["sticker_price"].apply(to_money).sum()),
            )

            safe_show_name = (
                clean_text(bulk_selected_show.get("show_name"))
                .lower()
                .replace(" ", "_")
            ) or "show"

            st.download_button(
                "Download bulk show sale template CSV",
                data=bulk_template.to_csv(index=False),
                file_name=f"{safe_show_name}_bulk_sale_template.csv",
                mime="text/csv",
                use_container_width=True,
                disabled=bulk_template.empty,
            )

            st.markdown("### 2. Upload the completed file")

            uploaded_bulk_sales = st.file_uploader(
                "Upload completed CSV or Excel file",
                type=["csv", "xlsx"],
                key="bulk_show_sale_upload",
            )

            if uploaded_bulk_sales is not None:
                try:
                    uploaded_df = _read_bulk_sale_upload(uploaded_bulk_sales)
                    validation_df = _validate_bulk_sale_upload(
                        uploaded_df,
                        inv,
                        bulk_selected_show,
                        bulk_sale_date,
                    )
                except Exception as exc:
                    st.error(f"Could not read the uploaded file: {exc}")
                    validation_df = pd.DataFrame()

                if validation_df.empty:
                    st.warning(
                        "No rows were marked as sold. Put X, YES, TRUE, 1, or SOLD in mark_sold for each sold item."
                    )
                else:
                    valid_sales = validation_df[validation_df["is_valid"]].copy()
                    invalid_sales = validation_df[~validation_df["is_valid"]].copy()

                    v1, v2, v3, v4, v5 = st.columns(5)
                    v1.metric("Marked rows", f"{len(validation_df):,}")
                    v2.metric("Ready to sync", f"{len(valid_sales):,}")
                    v3.metric("Needs correction", f"{len(invalid_sales):,}")
                    v4.metric(
                        "Sales",
                        money_fmt(valid_sales["sold_price"].sum()),
                    )
                    v5.metric(
                        "Profit",
                        money_fmt(valid_sales["profit"].sum()),
                    )

                    st.dataframe(
                        validation_df,
                        use_container_width=True,
                        hide_index=True,
                        column_config={
                            "sold_price": st.column_config.NumberColumn(
                                "Sold price", format="$%.2f"
                            ),
                            "fees": st.column_config.NumberColumn(
                                "Fees", format="$%.2f"
                            ),
                            "net_proceeds": st.column_config.NumberColumn(
                                "Net proceeds", format="$%.2f"
                            ),
                            "total_cost": st.column_config.NumberColumn(
                                "Total cost", format="$%.2f"
                            ),
                            "profit": st.column_config.NumberColumn(
                                "Profit", format="$%.2f"
                            ),
                            "is_valid": st.column_config.CheckboxColumn(
                                "Valid", disabled=True
                            ),
                        },
                    )

                    if not invalid_sales.empty:
                        st.warning(
                            "Rows needing correction will not be synced. You can still process the valid rows below."
                        )

                    if st.button(
                        f"Sync {len(valid_sales):,} valid show sale(s)",
                        type="primary",
                        use_container_width=True,
                        disabled=valid_sales.empty,
                        key="sync_bulk_show_sales",
                    ):
                        changed = 0
                        failed_ids: list[str] = []

                        for _, sale in valid_sales.iterrows():
                            inv_id = clean_text(sale.get("inventory_id"))
                            updates = _show_sale_updates(
                                selected_show=bulk_selected_show,
                                sale_date=clean_text(sale.get("sold_date")) or bulk_sale_date,
                                sold_price=sale.get("sold_price"),
                                fees=sale.get("fees"),
                                total_cost=sale.get("total_cost"),
                                sale_notes=sale.get("sale_notes"),
                            )

                            row_changed = mark_inventory_sold(inv_id, updates)
                            changed += row_changed
                            if not row_changed:
                                failed_ids.append(inv_id)

                        refresh_database_cache()

                        message = (
                            f"Bulk upload complete: {changed:,} inventory item(s) marked SOLD for "
                            f"{clean_text(bulk_selected_show.get('show_name'))}."
                        )
                        if failed_ids:
                            message += (
                                f" {len(failed_ids):,} row(s) were not changed: "
                                + ", ".join(failed_ids[:10])
                            )
                            if len(failed_ids) > 10:
                                message += ", ..."

                        st.session_state["show_bulk_success"] = message
                        st.rerun()


# =========================================================
# Show Summary
# =========================================================

with tab_summary:
    st.subheader("Show Summary")

    if inv.empty:
        st.info("No inventory loaded.")
    else:
        sold = inv[
            inv["inventory_status"].eq(STATUS_SOLD)
            & inv["sale_channel"].astype(str).str.lower().str.contains("card show", na=False)
        ].copy()

        if sold.empty:
            st.info("No show sales recorded yet.")
        else:
            sold["sold_dt"] = pd.to_datetime(sold["sold_date"], errors="coerce")

            c1, c2, c3, c4, c5 = st.columns(5)
            c1.metric("Show items sold", f"{len(sold):,}")
            c2.metric("Show sales", money_fmt(sold["sold_price"].sum()))
            c3.metric("Show fees", money_fmt(sold["fees_total"].sum()))
            c4.metric("Show net", money_fmt(sold["net_proceeds"].sum()))
            c5.metric("Show profit", money_fmt(sold["profit"].sum()))

            st.markdown("### Summary by show")

            summary = (
                sold.groupby(["show_id", "show_name"], dropna=False)
                .agg(
                    first_sale=("sold_dt", "min"),
                    last_sale=("sold_dt", "max"),
                    items_sold=("inventory_id", "count"),
                    sales=("sold_price", "sum"),
                    fees=("fees_total", "sum"),
                    net=("net_proceeds", "sum"),
                    cost=("total_cost", "sum"),
                    profit=("profit", "sum"),
                )
                .reset_index()
            )

            summary["first_sale"] = summary["first_sale"].dt.date.astype(str)
            summary["last_sale"] = summary["last_sale"].dt.date.astype(str)
            summary = summary.sort_values("last_sale", ascending=False)

            st.dataframe(
                summary.style.format(
                    {
                        "sales": "${:,.2f}",
                        "fees": "${:,.2f}",
                        "net": "${:,.2f}",
                        "cost": "${:,.2f}",
                        "profit": "${:,.2f}",
                    }
                ),
                use_container_width=True,
                hide_index=True,
            )

            st.markdown("### Sale detail")

            d1, d2, d3 = st.columns(3)

            with d1:
                show_names = sorted(
                    sold["show_name"].dropna().astype(str).str.strip().replace("", pd.NA).dropna().unique().tolist()
                )
                selected_show_names = st.multiselect("Filter by show", show_names)

            with d2:
                sale_sets = sorted(
                    sold["set_name"].dropna().astype(str).str.strip().replace("", pd.NA).dropna().unique().tolist()
                )
                selected_sale_sets = st.multiselect("Filter by set", sale_sets)

            with d3:
                sale_search = st.text_input("Search sales")

            detail = sold.copy()

            if selected_show_names:
                detail = detail[detail["show_name"].isin(selected_show_names)]

            if selected_sale_sets:
                detail = detail[detail["set_name"].isin(selected_sale_sets)]

            if sale_search.strip():
                q = sale_search.lower().strip()

                def _sale_match(row: pd.Series) -> bool:
                    fields = [
                        row.get("show_name", ""),
                        row.get("inventory_id", ""),
                        row.get("set_name", ""),
                        row.get("card_name", ""),
                        row.get("card_number", ""),
                        row.get("variant", ""),
                        row.get("grade", ""),
                        row.get("sale_notes", ""),
                    ]
                    return q in " ".join(str(x).lower() for x in fields)

                detail = detail[detail.apply(_sale_match, axis=1)]

            detail = _date_sort(detail, "sold_date", ascending=False)

            cols = [c for c in _display_sale_cols() if c in detail.columns]

            st.dataframe(
                detail[cols],
                use_container_width=True,
                hide_index=True,
            )

            st.download_button(
                "Download show sale detail CSV",
                data=detail[cols].to_csv(index=False),
                file_name="show_sales_detail.csv",
                mime="text/csv",
            )


# =========================================================
# Show Inventory View
# =========================================================

with tab_inventory:
    st.subheader("Show Inventory View")

    if inv.empty:
        st.info("No inventory loaded.")
    else:
        show_inv = inv[
            inv["inventory_type"].astype(str).str.lower().str.contains("show", na=False)
            & inv["inventory_status"].isin([STATUS_ACTIVE, STATUS_LISTED])
        ].copy()

        if show_inv.empty:
            st.info("No active Show Inventory items found.")
        else:
            f1, f2, f3, f4 = st.columns(4)

            with f1:
                product_options = sorted(
                    show_inv["product_type"].dropna().astype(str).str.strip().unique().tolist()
                )
                selected_products = st.multiselect(
                    "Product type",
                    product_options,
                    key="show_inv_product",
                )

            with f2:
                status_options = sorted(
                    show_inv["inventory_status"].dropna().astype(str).str.strip().unique().tolist()
                )
                selected_statuses = st.multiselect(
                    "Status",
                    status_options,
                    key="show_inv_status",
                )

            with f3:
                set_options = sorted(
                    show_inv["set_name"].dropna().astype(str).str.strip().replace("", pd.NA).dropna().unique().tolist()
                )
                selected_sets = st.multiselect(
                    "Set",
                    set_options,
                    key="show_inv_set",
                )

            with f4:
                search = st.text_input("Search", key="show_inv_search")

            view = show_inv.copy()

            if selected_products:
                view = view[view["product_type"].isin(selected_products)]

            if selected_statuses:
                view = view[view["inventory_status"].isin(selected_statuses)]

            if selected_sets:
                view = view[view["set_name"].isin(selected_sets)]

            if search.strip():
                q = search.lower().strip()

                def _inventory_match(row: pd.Series) -> bool:
                    fields = [
                        row.get("inventory_id", ""),
                        row.get("set_name", ""),
                        row.get("card_name", ""),
                        row.get("card_number", ""),
                        row.get("variant", ""),
                        row.get("grade", ""),
                    ]
                    return q in " ".join(str(x).lower() for x in fields)

                view = view[view.apply(_inventory_match, axis=1)]

            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Show items", f"{len(view):,}")
            c2.metric("Cost", money_fmt(view["total_cost"].sum()))
            c3.metric("Market value", money_fmt(view["market_value"].sum()))
            c4.metric("Sticker total", money_fmt(view["sticker_price"].sum()))

            cols = [c for c in _display_inventory_cols() if c in view.columns]

            st.dataframe(
                view[cols],
                use_container_width=True,
                hide_index=True,
            )

            st.download_button(
                "Download show inventory CSV",
                data=view[cols].to_csv(index=False),
                file_name="show_inventory.csv",
                mime="text/csv",
            )

            # what am I doin