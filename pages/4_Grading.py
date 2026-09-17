from __future__ import annotations

import uuid
from datetime import date, timedelta

import pandas as pd
import streamlit as st

from core.business import load_data, refresh_database_cache
from core.cleaning import now_iso, clean_text, to_money, money_fmt
from core.config import (
    GRADING_COLUMNS,
    INVENTORY_COLUMNS,
    STATUS_ACTIVE,
    STATUS_GRADING,
    STATUS_RETURNED,
    GRADING_COMPANIES,
)
from core.sheets import get_ws_name, append_rows, update_rows_by_key
from core.market import fetch_market_prices


st.set_page_config(page_title="Grading", layout="wide")
st.title("Grading")


# =========================================================
# General helpers
# =========================================================

def add_business_days(start_d: date, n: int) -> date:
    d = start_d
    added = 0

    while added < n:
        d += timedelta(days=1)

        if d.weekday() < 5:
            added += 1

    return d


def _safe_df(df: pd.DataFrame | None) -> pd.DataFrame:
    return pd.DataFrame() if df is None else df.copy()


def _ensure_cols(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    out = df.copy()

    for col in cols:
        if col not in out.columns:
            out[col] = ""

    return out


def _normalize_inventory(inv: pd.DataFrame) -> pd.DataFrame:
    out = _ensure_cols(
        _safe_df(inv),
        [
            "inventory_id",
            "inventory_status",
            "product_type",
            "set_name",
            "card_name",
            "card_number",
            "variant",
            "card_subtype",
            "purchased_from",
            "purchase_date",
            "total_price",
            "total_cost",
            "market_value",
            "reference_link",
            "grading_company",
            "grading_fee",
            "grade",
            "condition",
            "sold_date",
            "sold_price",
            "fees_total",
            "net_proceeds",
            "profit",
            "sale_channel",
            "notes",
        ],
    )

    if not out.empty:
        out["inventory_id"] = out["inventory_id"].astype(str).str.strip()
        out["inventory_status"] = out["inventory_status"].astype(str).str.upper().str.strip()

    return out


def _normalize_grading(grading: pd.DataFrame) -> pd.DataFrame:
    out = _ensure_cols(
        _safe_df(grading),
        [
            "grading_row_id",
            "submission_id",
            "submission_date",
            "estimated_return_date",
            "returned_date",
            "received_grade",
            "inventory_id",
            "reference_link",
            "card_name",
            "card_number",
            "variant",
            "card_subtype",
            "purchased_from",
            "purchase_date",
            "purchase_total",
            "grading_company",
            "grading_fee_initial",
            "grading_fee_per_card",
            "additional_costs",
            "extra_costs",
            "total_grading_cost",
            "psa9_price",
            "psa10_price",
            "status",
            "notes",
            "created_at",
            "updated_at",
            "synced_to_inventory",
        ],
    )

    if not out.empty:
        out["inventory_id"] = out["inventory_id"].astype(str).str.strip()
        out["grading_row_id"] = out["grading_row_id"].astype(str).str.strip()
        out["status"] = out["status"].astype(str).str.upper().str.strip()

    return out


def _open_grading_status_mask(grading: pd.DataFrame) -> pd.Series:
    if grading.empty:
        return pd.Series(False, index=grading.index)

    closed_statuses = {
        "RETURNED",
        "COMPLETE",
        "COMPLETED",
        "DUPLICATE_CLEARED",
        "CANCELLED",
        "CANCELED",
    }

    return ~grading["status"].astype(str).str.upper().str.strip().isin(closed_statuses)


def _open_grading_inventory_ids(grading: pd.DataFrame) -> set[str]:
    if grading.empty or "inventory_id" not in grading.columns:
        return set()

    open_rows = grading[_open_grading_status_mask(grading)].copy()

    return set(
        open_rows["inventory_id"]
        .dropna()
        .astype(str)
        .str.strip()
        .replace("", pd.NA)
        .dropna()
        .tolist()
    )


def _duplicate_inventory_id_rows(inv: pd.DataFrame) -> pd.DataFrame:
    if inv.empty or "inventory_id" not in inv.columns:
        return pd.DataFrame()

    out = inv.copy()
    out["inventory_id"] = out["inventory_id"].astype(str).str.strip()

    blank = out["inventory_id"].eq("")
    duplicate = out["inventory_id"].ne("") & out["inventory_id"].duplicated(keep=False)

    return out[blank | duplicate].copy()


def _active_gradeable_cards(inv: pd.DataFrame, grading: pd.DataFrame) -> pd.DataFrame:
    if inv.empty:
        return pd.DataFrame()

    active_cards = inv[
        inv["inventory_status"].astype(str).str.upper().eq(STATUS_ACTIVE)
        & inv["product_type"].astype(str).str.lower().ne("sealed")
    ].copy()

    if active_cards.empty:
        return active_cards

    active_cards["inventory_id"] = active_cards["inventory_id"].astype(str).str.strip()

    # A blank or duplicated inventory_id cannot be safely updated by key.
    safe_id = active_cards["inventory_id"].ne("") & ~active_cards["inventory_id"].duplicated(keep=False)

    already_in_open_submission = active_cards["inventory_id"].isin(_open_grading_inventory_ids(grading))

    return active_cards[safe_id & ~already_in_open_submission].copy()


def _grading_option_label(row: pd.Series) -> str:
    inv_id = clean_text(row.get("inventory_id"))
    set_name = clean_text(row.get("set_name"))
    card_name = clean_text(row.get("card_name"))
    card_number = clean_text(row.get("card_number"))
    variant = clean_text(row.get("variant"))
    cost = money_fmt(row.get("total_cost"))

    bits = [inv_id, set_name, card_name]

    if card_number:
        bits.append(f"#{card_number}")

    if variant:
        bits.append(variant)

    bits.append(f"cost {cost}")

    return " — ".join([x for x in bits if clean_text(x)])


def _build_grading_option_map(active_cards: pd.DataFrame) -> tuple[list[str], dict[str, str]]:
    if active_cards.empty:
        return [], {}

    cards = active_cards.copy()
    cards["__label"] = cards.apply(_grading_option_label, axis=1)

    # Make the Streamlit option labels unique even if card names/details are identical.
    label_counts = cards["__label"].value_counts()
    cards.loc[cards["__label"].isin(label_counts[label_counts > 1].index), "__label"] = cards.apply(
        lambda r: f"{r['__label']} — row {r.name}",
        axis=1,
    )

    options = cards["__label"].tolist()
    mapping = dict(zip(cards["__label"], cards["inventory_id"].astype(str).str.strip()))

    return options, mapping


def _ordered_rows_by_inventory_id(df: pd.DataFrame, inventory_ids: list[str]) -> pd.DataFrame:
    if df.empty or not inventory_ids:
        return pd.DataFrame()

    safe = df.copy()
    safe["inventory_id"] = safe["inventory_id"].astype(str).str.strip()
    safe = safe.drop_duplicates(subset=["inventory_id"], keep="first").set_index("inventory_id", drop=False)

    present_ids = [inv_id for inv_id in inventory_ids if inv_id in safe.index]

    if not present_ids:
        return pd.DataFrame()

    return safe.loc[present_ids].reset_index(drop=True)


def _coalesce_sheet_field(df: pd.DataFrame, base_name: str) -> pd.Series:
    """
    Return the first nonblank value across a worksheet field and any deduped
    copies such as received_grade__dup2.

    Older grading sheets accumulated duplicate headers. core.sheets safely
    renames those duplicate columns when reading them, so KPI calculations
    should coalesce the populated value instead of assuming the first copy is
    always the one containing data.
    """
    if df.empty:
        return pd.Series(dtype="object", index=df.index)

    result = pd.Series("", index=df.index, dtype="object")
    prefix = f"{base_name}__dup"

    for col_pos, col_name in enumerate(df.columns):
        name = str(col_name or "").strip()
        if name != base_name and not name.startswith(prefix):
            continue

        values = df.iloc[:, col_pos]
        text = values.fillna("").astype(str).str.strip()
        usable = text.ne("") & ~text.str.lower().isin({"nan", "none", "<na>"})

        current_text = result.fillna("").astype(str).str.strip()
        current_blank = current_text.eq("") | current_text.str.lower().isin({"nan", "none", "<na>"})
        take = current_blank & usable
        result.loc[take] = values.loc[take]

    return result


def _resolve_valid_received_grade(df: pd.DataFrame) -> pd.Series:
    """
    Resolve a numeric received grade (1-10) across the canonical
    received_grade column and any duplicate-header copies.

    Unlike _coalesce_sheet_field(), this intentionally ignores non-grade
    junk values and keeps searching later duplicate columns until it finds
    a valid numeric grade. That matters for older grading rows where shifted
    legacy values can occupy one duplicate received_grade column.
    """
    if df.empty:
        return pd.Series(dtype="float64", index=df.index)

    resolved = pd.Series(float("nan"), index=df.index, dtype="float64")

    for col_pos, col_name in enumerate(df.columns):
        name = str(col_name or "").strip()
        base_name = name
        if "__dup" in base_name:
            base_name = base_name.split("__dup", 1)[0]

        if base_name != "received_grade":
            continue

        values = df.iloc[:, col_pos].fillna("").astype(str).str.strip()
        numeric = pd.to_numeric(
            values.str.extract(r"(-?\d+(?:\.\d+)?)", expand=False),
            errors="coerce",
        )
        numeric = numeric.where(numeric.between(1, 10, inclusive="both"))

        take = resolved.isna() & numeric.notna()
        resolved.loc[take] = numeric.loc[take]

    return resolved


def _grading_dashboard_metrics(inv: pd.DataFrame, grading: pd.DataFrame) -> dict[str, float | int]:
    """Build top-level grading KPIs from grading history + realized inventory sales."""
    empty_metrics = {
        "sent": 0,
        "received": 0,
        "outstanding": 0,
        "gem_10s": 0,
        "graded_received": 0,
        "gem_rate": 0.0,
        "sold_graded_cards": 0,
        "roi_dollars": 0.0,
        "avg_roi_pct": 0.0,
    }

    if grading.empty:
        return empty_metrics

    g = grading.copy()
    g["inventory_id"] = g["inventory_id"].astype(str).str.strip()
    g["status"] = g["status"].astype(str).str.upper().str.strip()

    # These are administrative/repair rows, not cards we want to count as submitted.
    excluded_statuses = {
        "DUPLICATE_CLEARED",
        "CANCELLED",
        "CANCELED",
    }
    valid = g[~g["status"].isin(excluded_statuses)].copy()

    # Ignore completely blank historical rows if they ever exist in the worksheet.
    valid = valid[
        valid["grading_row_id"].astype(str).str.strip().ne("")
        | valid["inventory_id"].astype(str).str.strip().ne("")
    ].copy()

    if valid.empty:
        return empty_metrics

    # Some historical grading worksheets contain duplicate received_grade
    # headers. read_sheet() renames those extras with __dupN suffixes.
    # Coalesce them so a populated grade is never missed by the KPI logic.
    valid_received_grade = _coalesce_sheet_field(valid, "received_grade")

    status_received = valid["status"].isin({"RETURNED", "COMPLETE", "COMPLETED"})
    returned_date_present = valid["returned_date"].astype(str).str.strip().replace("nan", "").ne("")
    grade_text = valid_received_grade.fillna("").astype(str).str.strip()
    grade_present = grade_text.ne("") & ~grade_text.str.lower().isin({"nan", "none", "<na>"})
    received_mask = status_received | returned_date_present | grade_present
    received = valid[received_mask].copy()

    sent_count = int(len(valid))
    received_count = int(len(received))
    outstanding_count = max(sent_count - received_count, 0)

    # Resolve the received grade from both grading history and inventory.
    # Historical grading sheets have accumulated duplicate/legacy grade columns,
    # while the return workflow also writes the final grade back to inventory.
    # We therefore prefer a plausible grade from grading history and fall back
    # to the inventory grade for the same inventory_id when needed.
    # Resolve a valid grade across every received_grade copy. Older rows can
    # contain non-grade junk in one duplicate column, so we must keep scanning
    # until an actual 1-10 grade is found instead of stopping at the first
    # merely nonblank value.
    grading_grade_numeric = _resolve_valid_received_grade(received)

    inventory_grade_numeric = pd.Series(float("nan"), index=received.index, dtype="float64")

    if not inv.empty and "inventory_id" in inv.columns and "grade" in inv.columns:
        inv_grade = inv[["inventory_id", "grade"]].copy()
        inv_grade["inventory_id"] = inv_grade["inventory_id"].astype(str).str.strip()
        inv_grade["__grade_text"] = inv_grade["grade"].fillna("").astype(str).str.strip()
        inv_grade["__grade_numeric"] = pd.to_numeric(
            inv_grade["__grade_text"].str.extract(r"(-?\d+(?:\.\d+)?)", expand=False),
            errors="coerce",
        )
        inv_grade["__valid_grade"] = inv_grade["__grade_numeric"].between(1, 10, inclusive="both")

        # If duplicate inventory IDs ever exist, prefer the copy that actually
        # contains a valid grade, then keep only one lookup row per ID.
        inv_grade = inv_grade[inv_grade["inventory_id"].ne("")].copy()
        inv_grade = inv_grade.sort_values("__valid_grade", ascending=False)
        inv_grade = inv_grade.drop_duplicates(subset=["inventory_id"], keep="first")
        inventory_grade_map = inv_grade.set_index("inventory_id")["__grade_numeric"].to_dict()

        inventory_grade_numeric = pd.to_numeric(
            received["inventory_id"].astype(str).str.strip().map(inventory_grade_map),
            errors="coerce",
        )
        inventory_grade_numeric = inventory_grade_numeric.where(
            inventory_grade_numeric.between(1, 10, inclusive="both")
        )

    resolved_grade_numeric = grading_grade_numeric.combine_first(inventory_grade_numeric)
    gem_10s = int(resolved_grade_numeric.eq(10).sum())
    graded_received_count = int(resolved_grade_numeric.notna().sum())
    gem_rate = (gem_10s / received_count * 100.0) if received_count else 0.0

    metrics = {
        "sent": sent_count,
        "received": received_count,
        "outstanding": outstanding_count,
        "gem_10s": gem_10s,
        "graded_received": graded_received_count,
        "gem_rate": gem_rate,
        "sold_graded_cards": 0,
        "roi_dollars": 0.0,
        "avg_roi_pct": 0.0,
    }

    if inv.empty or received.empty:
        return metrics

    # A sold inventory card should contribute to realized grading ROI only once,
    # even if grading history contains more than one historical row for that ID.
    received_ids = set(
        received["inventory_id"]
        .dropna()
        .astype(str)
        .str.strip()
        .replace("", pd.NA)
        .dropna()
        .tolist()
    )

    if not received_ids:
        return metrics

    sold = inv.copy()
    sold["inventory_id"] = sold["inventory_id"].astype(str).str.strip()
    sold["inventory_status"] = sold["inventory_status"].astype(str).str.upper().str.strip()

    sold_date_present = sold["sold_date"].astype(str).str.strip().replace("nan", "").ne("")
    sold_mask = sold["inventory_status"].eq("SOLD") | sold_date_present
    sold = sold[sold_mask & sold["inventory_id"].isin(received_ids)].copy()

    if sold.empty:
        return metrics

    sold = sold.drop_duplicates(subset=["inventory_id"], keep="first").copy()

    sold["__cost"] = pd.to_numeric(sold["total_cost"], errors="coerce")
    sold["__net"] = pd.to_numeric(sold["net_proceeds"], errors="coerce")
    sold["__sold_price"] = pd.to_numeric(sold["sold_price"], errors="coerce")
    sold["__fees"] = pd.to_numeric(sold["fees_total"], errors="coerce")
    sold["__stored_profit"] = pd.to_numeric(sold["profit"], errors="coerce")

    # Prefer actual net proceeds. If an older sold row lacks that field, fall back
    # to sold price less recorded fees. Profit is only used as a final fallback.
    fallback_net = sold["__sold_price"] - sold["__fees"].fillna(0.0)
    sold["__realized_net"] = sold["__net"].where(sold["__net"].notna(), fallback_net)
    sold["__profit_calc"] = sold["__realized_net"] - sold["__cost"]
    sold["__profit_calc"] = sold["__profit_calc"].where(
        sold["__profit_calc"].notna(),
        sold["__stored_profit"],
    )

    roi_rows = sold[sold["__cost"].notna() & sold["__profit_calc"].notna()].copy()

    if roi_rows.empty:
        return metrics

    metrics["sold_graded_cards"] = int(len(roi_rows))
    metrics["roi_dollars"] = float(roi_rows["__profit_calc"].sum())

    pct_rows = roi_rows[roi_rows["__cost"] > 0].copy()
    if not pct_rows.empty:
        pct_rows["__roi_pct"] = pct_rows["__profit_calc"] / pct_rows["__cost"] * 100.0
        metrics["avg_roi_pct"] = float(pct_rows["__roi_pct"].mean())

    return metrics


# =========================================================
# Repair helpers
# =========================================================

def _build_duplicate_open_grading_rows(grading: pd.DataFrame) -> pd.DataFrame:
    if grading.empty:
        return pd.DataFrame()

    open_rows = grading[_open_grading_status_mask(grading)].copy()
    open_rows = open_rows[open_rows["inventory_id"].astype(str).str.strip().ne("")].copy()

    if open_rows.empty:
        return pd.DataFrame()

    duplicate_ids = open_rows["inventory_id"].value_counts()
    duplicate_ids = duplicate_ids[duplicate_ids > 1].index.tolist()

    if not duplicate_ids:
        return pd.DataFrame()

    dupes = open_rows[open_rows["inventory_id"].isin(duplicate_ids)].copy()
    dupes["created_sort"] = pd.to_datetime(dupes["created_at"], errors="coerce")
    dupes["submission_sort"] = pd.to_datetime(dupes["submission_date"], errors="coerce")
    dupes = dupes.sort_values(
        ["inventory_id", "created_sort", "submission_sort", "grading_row_id"],
        ascending=[True, True, True, True],
        na_position="last",
    ).copy()

    dupes["repair_action"] = "CLEAR_DUPLICATE_GRADING_ROW"
    dupes.loc[~dupes.duplicated("inventory_id", keep="first"), "repair_action"] = "KEEP"

    show_cols = [
        "repair_action",
        "inventory_id",
        "grading_row_id",
        "submission_id",
        "submission_date",
        "status",
        "card_name",
        "card_number",
        "variant",
        "total_grading_cost",
        "notes",
    ]

    return dupes[[c for c in show_cols if c in dupes.columns]].copy()


def _build_orphan_grading_inventory_rows(inv: pd.DataFrame, grading: pd.DataFrame) -> pd.DataFrame:
    if inv.empty:
        return pd.DataFrame()

    open_ids = _open_grading_inventory_ids(grading)

    orphan = inv[
        inv["inventory_status"].astype(str).str.upper().eq(STATUS_GRADING)
        & ~inv["inventory_id"].astype(str).str.strip().isin(open_ids)
    ].copy()

    if orphan.empty:
        return orphan

    duplicate_inventory_ids = _duplicate_inventory_id_rows(inv)
    unsafe_ids = set(
        duplicate_inventory_ids.get("inventory_id", pd.Series(dtype=str))
        .dropna()
        .astype(str)
        .str.strip()
        .replace("", pd.NA)
        .dropna()
        .tolist()
    )

    orphan["repair_action"] = "CLEAR_ORPHAN_GRADING_STATUS"
    orphan.loc[orphan["inventory_id"].astype(str).str.strip().isin(unsafe_ids), "repair_action"] = "REVIEW_DUPLICATE_INVENTORY_ID"

    show_cols = [
        "repair_action",
        "inventory_id",
        "inventory_status",
        "card_name",
        "card_number",
        "variant",
        "set_name",
        "grading_company",
        "grading_fee",
        "total_price",
        "total_cost",
    ]

    return orphan[[c for c in show_cols if c in orphan.columns]].copy()


def _repair_grading_duplicates_and_orphans(inv: pd.DataFrame, grading: pd.DataFrame) -> tuple[int, int, pd.DataFrame, pd.DataFrame]:
    duplicate_open = _build_duplicate_open_grading_rows(grading)
    orphan_inventory = _build_orphan_grading_inventory_rows(inv, grading)

    grading_updates = {}

    if not duplicate_open.empty:
        to_clear_grading = duplicate_open[duplicate_open["repair_action"].eq("CLEAR_DUPLICATE_GRADING_ROW")].copy()

        for _, row in to_clear_grading.iterrows():
            row_id = clean_text(row.get("grading_row_id"))
            if not row_id:
                continue

            old_notes = clean_text(row.get("notes"))
            repair_note = "Duplicate open grading row cleared by repair tool."

            grading_updates[row_id] = {
                "status": "DUPLICATE_CLEARED",
                "notes": f"{repair_note} Previous notes: {old_notes}" if old_notes else repair_note,
                "updated_at": now_iso(),
                "synced_to_inventory": "NO",
            }

    inventory_updates = {}

    if not orphan_inventory.empty:
        to_clear_inventory = orphan_inventory[orphan_inventory["repair_action"].eq("CLEAR_ORPHAN_GRADING_STATUS")].copy()

        source = inv.copy()
        source["inventory_id"] = source["inventory_id"].astype(str).str.strip()
        source = source.drop_duplicates(subset=["inventory_id"], keep="first").set_index("inventory_id", drop=False)

        for _, row in to_clear_inventory.iterrows():
            inv_id = clean_text(row.get("inventory_id"))
            if not inv_id:
                continue

            inv_rec = source.loc[inv_id] if inv_id in source.index else row
            base_cost = to_money(inv_rec.get("total_price"))

            inventory_updates[inv_id] = {
                "inventory_status": STATUS_ACTIVE,
                "grading_company": "",
                "grading_fee": 0.0,
                "grade": "",
                "total_cost": round(base_cost, 2),
            }

    if grading_updates:
        update_rows_by_key(
            get_ws_name("grading_worksheet", "grading"),
            GRADING_COLUMNS,
            "grading_row_id",
            grading_updates,
        )

    if inventory_updates:
        update_rows_by_key(
            get_ws_name("inventory_worksheet", "inventory"),
            INVENTORY_COLUMNS,
            "inventory_id",
            inventory_updates,
        )

    return len(grading_updates), len(inventory_updates), duplicate_open, orphan_inventory


def _safe_sync_grading_rows_to_inventory(inv: pd.DataFrame, grading: pd.DataFrame) -> int:
    """
    Sync grading status/fees back to inventory using inventory_id only.

    This intentionally skips blank/duplicated inventory IDs and skips duplicate open grading rows.
    That prevents one grading row from updating two copies of the same card.
    """
    if inv.empty or grading.empty:
        return 0

    duplicate_inventory_ids = set(
        _duplicate_inventory_id_rows(inv)
        .get("inventory_id", pd.Series(dtype=str))
        .dropna()
        .astype(str)
        .str.strip()
        .replace("", pd.NA)
        .dropna()
        .tolist()
    )

    open_rows = grading[_open_grading_status_mask(grading)].copy()
    open_rows = open_rows[open_rows["inventory_id"].astype(str).str.strip().ne("")].copy()

    if open_rows.empty:
        return 0

    duplicate_open_ids = set(
        open_rows.loc[open_rows["inventory_id"].duplicated(keep=False), "inventory_id"]
        .dropna()
        .astype(str)
        .str.strip()
        .tolist()
    )

    source = inv.copy()
    source["inventory_id"] = source["inventory_id"].astype(str).str.strip()
    source = source.drop_duplicates(subset=["inventory_id"], keep="first").set_index("inventory_id", drop=False)

    updates = {}

    for _, row in open_rows.iterrows():
        inv_id = clean_text(row.get("inventory_id"))

        if not inv_id or inv_id in duplicate_inventory_ids or inv_id in duplicate_open_ids:
            continue

        if inv_id not in source.index:
            continue

        inv_rec = source.loc[inv_id]
        grading_fee = to_money(row.get("total_grading_cost")) or to_money(row.get("grading_fee_per_card")) or to_money(row.get("grading_fee_initial"))
        base_cost = to_money(inv_rec.get("total_price"))

        updates[inv_id] = {
            "inventory_status": STATUS_GRADING,
            "grading_company": clean_text(row.get("grading_company")),
            "grading_fee": round(grading_fee, 2),
            "total_cost": round(base_cost + grading_fee, 2),
        }

    if updates:
        update_rows_by_key(
            get_ws_name("inventory_worksheet", "inventory"),
            INVENTORY_COLUMNS,
            "inventory_id",
            updates,
        )

    return len(updates)


# =========================================================
# Load data
# =========================================================

if st.button("🔄 Refresh database"):
    refresh_database_cache()
    st.rerun()

data = load_data()
inv = _normalize_inventory(data.inventory)
grading = _normalize_grading(data.grading)

# =========================================================
# Grading performance KPIs
# =========================================================

grading_metrics = _grading_dashboard_metrics(inv, grading)

st.subheader("Grading Performance")
metric_cols = st.columns(6)

with metric_cols[0]:
    st.metric("Cards Sent", f"{grading_metrics['sent']:,}")

with metric_cols[1]:
    st.metric("Cards Received", f"{grading_metrics['received']:,}")

with metric_cols[2]:
    st.metric("Outstanding", f"{grading_metrics['outstanding']:,}")

with metric_cols[3]:
    st.metric(
        "Gem Rate",
        f"{grading_metrics['gem_rate']:.1f}%",
        help=(
            "PSA 10s divided by all cards that have been received back from grading. "
            f"Current: {grading_metrics['gem_10s']:,} gem mint 10(s) out of "
            f"{grading_metrics['received']:,} received card(s). "
            f"A usable grade was resolved for {grading_metrics['graded_received']:,} received card(s)."
        ),
    )

with metric_cols[4]:
    st.metric(
        "Realized ROI $",
        money_fmt(grading_metrics["roi_dollars"]),
        help=(
            "Total realized profit on cards that were sent for grading, received back, and sold. "
            "Uses inventory net proceeds minus total cost, where total cost includes grading cost."
        ),
    )

with metric_cols[5]:
    st.metric(
        "Avg ROI %",
        f"{grading_metrics['avg_roi_pct']:.1f}%",
        help=(
            "Average per-card ROI for graded cards that were received and sold: "
            "(net proceeds - total cost) / total cost. "
            f"Based on {grading_metrics['sold_graded_cards']:,} sold graded card(s) with usable cost/proceeds data."
        ),
    )

st.caption(
    "ROI metrics include only realized sales from cards that appear in grading history as received. "
    "Cards still held in inventory are not included in ROI."
)

duplicate_inventory_id_rows = _duplicate_inventory_id_rows(inv)
duplicate_open_grading_rows = _build_duplicate_open_grading_rows(grading)
orphan_grading_inventory_rows = _build_orphan_grading_inventory_rows(inv, grading)

if not duplicate_inventory_id_rows.empty:
    with st.expander("Inventory ID issue detected", expanded=True):
        st.warning("Some inventory rows have a blank or duplicated inventory_id. Grading tools will not offer those rows because updating by inventory_id could update more than one copy.")
        show_cols = [
            "inventory_id",
            "inventory_status",
            "product_type",
            "set_name",
            "card_name",
            "card_number",
            "variant",
            "total_cost",
        ]
        st.dataframe(
            duplicate_inventory_id_rows[[c for c in show_cols if c in duplicate_inventory_id_rows.columns]],
            use_container_width=True,
            hide_index=True,
            column_config={
                "total_cost": st.column_config.NumberColumn("Total Cost", format="$%.2f"),
            },
        )

if not duplicate_open_grading_rows.empty or not orphan_grading_inventory_rows.empty:
    with st.expander("Repair duplicate grading assignments", expanded=True):
        st.warning("The repair tool below fixes safe grading duplicates: duplicate open grading rows for the same inventory_id and inventory rows stuck in GRADING with no open grading row.")

        if not duplicate_open_grading_rows.empty:
            st.markdown("#### Duplicate open grading rows")
            st.dataframe(
                duplicate_open_grading_rows,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "total_grading_cost": st.column_config.NumberColumn("Total Grading Cost", format="$%.2f"),
                },
            )

        if not orphan_grading_inventory_rows.empty:
            st.markdown("#### Inventory rows stuck in GRADING with no matching open grading row")
            st.dataframe(
                orphan_grading_inventory_rows,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "grading_fee": st.column_config.NumberColumn("Grading Fee", format="$%.2f"),
                    "total_price": st.column_config.NumberColumn("Original Total Price", format="$%.2f"),
                    "total_cost": st.column_config.NumberColumn("Current Total Cost", format="$%.2f"),
                },
            )

            review_only = orphan_grading_inventory_rows[orphan_grading_inventory_rows["repair_action"].eq("REVIEW_DUPLICATE_INVENTORY_ID")].copy()
            if not review_only.empty:
                st.error("Some rows have duplicated inventory_id values, so they were marked REVIEW only and will not be changed automatically.")

        repair_confirmed = st.checkbox("I reviewed this table. Repair safe duplicate grading assignments.", value=False)

        if st.button("Repair grading duplicates", type="primary", disabled=not repair_confirmed):
            grading_changed, inventory_changed, _, _ = _repair_grading_duplicates_and_orphans(inv, grading)
            refresh_database_cache()
            st.success(f"Repaired {grading_changed:,} grading row(s) and {inventory_changed:,} inventory row(s).")
            st.rerun()


t1, t2, t3 = st.tabs(["Create Submission", "Update Returns", "Submission History"])


# =========================================================
# Tab 1: Create submission
# =========================================================

with t1:
    st.subheader("Create Grading Submission")

    active_cards = _active_gradeable_cards(inv, grading)

    if active_cards.empty:
        st.info("No ACTIVE cards available for grading. Cards already in an open grading submission, rows with blank inventory_id, and rows with duplicated inventory_id are excluded.")
    else:
        inventory_options, inventory_label_to_id = _build_grading_option_map(active_cards)
        selected_labels = st.multiselect("Select cards", inventory_options)
        selected_inventory_ids = [inventory_label_to_id[label] for label in selected_labels if label in inventory_label_to_id]

        if selected_inventory_ids:
            selected_preview = _ordered_rows_by_inventory_id(active_cards, selected_inventory_ids)
            preview_cols = [
                "inventory_id",
                "inventory_status",
                "set_name",
                "card_name",
                "card_number",
                "variant",
                "total_cost",
                "market_value",
            ]

            st.caption("Selected inventory rows")
            st.dataframe(
                selected_preview[[c for c in preview_cols if c in selected_preview.columns]],
                use_container_width=True,
                hide_index=True,
                column_config={
                    "total_cost": st.column_config.NumberColumn("Total Cost", format="$%.2f"),
                    "market_value": st.column_config.NumberColumn("Market Value", format="$%.2f"),
                },
            )

        col1, col2, col3, col4 = st.columns(4)

        with col1:
            submission_date = st.date_input("Submission date", value=date.today())

        with col2:
            company = st.selectbox("Grading company", GRADING_COMPANIES)

        with col3:
            fee_per_card = st.number_input(
                "Grading fee per card",
                min_value=0.0,
                value=float(st.secrets.get("default_grading_fee_per_card", 22.0)),
                step=1.0,
                format="%.2f",
            )

        with col4:
            business_days = st.number_input(
                "Estimated return business days",
                min_value=1,
                value=int(st.secrets.get("default_business_days_return", 75)),
                step=1,
            )

        notes = st.text_area("Notes")
        pull_prices = st.checkbox("Pull PSA 9/10 market values for submission rows", value=False)

        if st.button("Create submission", type="primary", disabled=not selected_inventory_ids):
            if len(selected_inventory_ids) != len(set(selected_inventory_ids)):
                st.error("The same inventory_id was selected more than once. Remove the duplicate before creating the submission.")
                st.stop()

            # Re-load fresh data before writing so stale UI state cannot double-add a row that changed.
            fresh_data = load_data()
            fresh_inv = _normalize_inventory(fresh_data.inventory)
            fresh_grading = _normalize_grading(fresh_data.grading)

            fresh_active_cards = _active_gradeable_cards(fresh_inv, fresh_grading)
            fresh_active_ids = set(fresh_active_cards["inventory_id"].astype(str).str.strip().tolist())
            fresh_open_ids = _open_grading_inventory_ids(fresh_grading)

            no_longer_available = [
                inv_id
                for inv_id in selected_inventory_ids
                if inv_id not in fresh_active_ids or inv_id in fresh_open_ids
            ]

            if no_longer_available:
                st.error("One or more selected inventory rows are no longer available for grading. Refresh the page and try again.")
                st.write(no_longer_available)
                st.stop()

            chosen = _ordered_rows_by_inventory_id(fresh_active_cards, selected_inventory_ids)

            if chosen.empty:
                st.warning("No valid inventory rows were selected.")
                st.stop()

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

                if not inv_id:
                    continue

                rows.append(
                    {
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
                    }
                )

                inv_updates[inv_id] = {
                    "inventory_status": STATUS_GRADING,
                    "grading_company": company,
                    "grading_fee": grading_fee,
                    "total_cost": round(to_money(r.get("total_price")) + grading_fee, 2),
                }

            if not rows or not inv_updates:
                st.warning("No valid rows were built for this submission.")
                st.stop()

            append_rows(get_ws_name("grading_worksheet", "grading"), GRADING_COLUMNS, rows)
            update_rows_by_key(
                get_ws_name("inventory_worksheet", "inventory"),
                INVENTORY_COLUMNS,
                "inventory_id",
                inv_updates,
            )

            st.success(f"Created submission {sub_id} with {len(rows):,} card(s). Grading fees were written back to inventory.")
            refresh_database_cache()
            st.rerun()


# =========================================================
# Tab 2: Update returns
# =========================================================

with t2:
    st.subheader("Update Returns")

    if grading.empty:
        st.info("No grading records yet.")
    else:
        open_rows = grading[_open_grading_status_mask(grading)].copy()

        if open_rows.empty:
            st.info("No open grading rows.")
        else:
            open_rows["submission_date_clean"] = open_rows["submission_date"].astype(str).str.strip()
            open_rows["submission_id_clean"] = open_rows["submission_id"].astype(str).str.strip()
            open_rows["estimated_return_date_clean"] = open_rows["estimated_return_date"].astype(str).str.strip()

            # Group the return workflow by submission first. This keeps the card dropdown
            # focused on one actual return instead of mixing every open grading row together.
            submission_summary = (
                open_rows.groupby(
                    ["submission_date_clean", "submission_id_clean", "estimated_return_date_clean"],
                    dropna=False,
                )
                .agg(
                    open_cards=("grading_row_id", "count"),
                    grading_cost=("total_grading_cost", "sum"),
                    purchase_total=("purchase_total", "sum"),
                )
                .reset_index()
                .sort_values(
                    ["submission_date_clean", "submission_id_clean"],
                    ascending=[False, False],
                )
            )

            submission_summary["submission_filter_label"] = submission_summary.apply(
                lambda r: (
                    f"{clean_text(r.get('submission_date_clean')) or 'No submission date'}"
                    f" — Sub {clean_text(r.get('submission_id_clean')) or 'No submission ID'}"
                    f" — {int(r.get('open_cards', 0))} open card(s)"
                    f" — est. {clean_text(r.get('estimated_return_date_clean')) or 'N/A'}"
                ),
                axis=1,
            )

            selected_submission_label = st.selectbox(
                "Filter by submission date / submission",
                submission_summary["submission_filter_label"].tolist(),
            )

            selected_submission = submission_summary[
                submission_summary["submission_filter_label"].eq(selected_submission_label)
            ].iloc[0]

            selected_submission_date = clean_text(selected_submission.get("submission_date_clean"))
            selected_submission_id = clean_text(selected_submission.get("submission_id_clean"))
            selected_est_return = clean_text(selected_submission.get("estimated_return_date_clean"))

            filtered_rows = open_rows[
                open_rows["submission_date_clean"].eq(selected_submission_date)
                & open_rows["submission_id_clean"].eq(selected_submission_id)
                & open_rows["estimated_return_date_clean"].eq(selected_est_return)
            ].copy()

            filtered_rows = filtered_rows.sort_values(
                ["card_name", "card_number", "variant", "inventory_id"],
                ascending=[True, True, True, True],
                na_position="last",
            )

            st.caption("Cards in the selected open submission")
            preview_cols = [
                "submission_date",
                "estimated_return_date",
                "submission_id",
                "grading_row_id",
                "inventory_id",
                "card_name",
                "card_number",
                "variant",
                "purchase_total",
                "total_grading_cost",
                "psa9_price",
                "psa10_price",
                "status",
            ]
            st.dataframe(
                filtered_rows[[c for c in preview_cols if c in filtered_rows.columns]],
                use_container_width=True,
                hide_index=True,
                column_config={
                    "purchase_total": st.column_config.NumberColumn("Purchase Total", format="$%.2f"),
                    "total_grading_cost": st.column_config.NumberColumn("Grading Cost", format="$%.2f"),
                    "psa9_price": st.column_config.NumberColumn("PSA 9 Value", format="$%.2f"),
                    "psa10_price": st.column_config.NumberColumn("PSA 10 Value", format="$%.2f"),
                },
            )

            if filtered_rows.empty:
                st.info("No open cards found for this submission filter.")
                st.stop()

            filtered_rows["label"] = filtered_rows.apply(
                lambda r: (
                    f"{clean_text(r.get('inventory_id'))} — "
                    f"{clean_text(r.get('card_name'))}"
                    f" #{clean_text(r.get('card_number'))}"
                    f" — {clean_text(r.get('variant'))}"
                    f" — Row {clean_text(r.get('grading_row_id'))}"
                ),
                axis=1,
            )

            selected = st.selectbox("Select returned card from this submission", filtered_rows["label"].tolist())
            rec = filtered_rows[filtered_rows["label"].eq(selected)].iloc[0]

            card_cost = to_money(rec.get("purchase_total"))
            existing_grading_cost_preview = (
                to_money(rec.get("total_grading_cost"))
                or to_money(rec.get("grading_fee_per_card"))
                or to_money(rec.get("grading_fee_initial"))
            )

            with st.expander("Selected card details", expanded=True):
                detail_cols = [
                    "grading_row_id",
                    "inventory_id",
                    "card_name",
                    "card_number",
                    "variant",
                    "card_subtype",
                    "purchase_total",
                    "total_grading_cost",
                    "psa9_price",
                    "psa10_price",
                    "notes",
                ]
                detail_df = pd.DataFrame([rec])
                st.dataframe(
                    detail_df[[c for c in detail_cols if c in detail_df.columns]],
                    use_container_width=True,
                    hide_index=True,
                    column_config={
                        "purchase_total": st.column_config.NumberColumn("Purchase Total", format="$%.2f"),
                        "total_grading_cost": st.column_config.NumberColumn("Grading Cost", format="$%.2f"),
                        "psa9_price": st.column_config.NumberColumn("PSA 9 Value", format="$%.2f"),
                        "psa10_price": st.column_config.NumberColumn("PSA 10 Value", format="$%.2f"),
                    },
                )
                st.caption(
                    f"Current cost basis before extra return costs: "
                    f"{money_fmt(card_cost + existing_grading_cost_preview)}"
                )

            col1, col2, col3 = st.columns(3)

            with col1:
                returned_date = st.date_input("Returned date", value=date.today())

            with col2:
                received_grade = st.text_input("Received grade", key="single_return_received_grade")

            with col3:
                additional_cost = st.number_input(
                    "Additional cost",
                    min_value=0.0,
                    step=1.0,
                    format="%.2f",
                    key="single_return_additional_cost",
                )

            if st.button("Mark returned", type="primary"):
                row_id = clean_text(rec.get("grading_row_id"))
                inv_id = clean_text(rec.get("inventory_id"))

                if not row_id or not inv_id:
                    st.error("This grading row is missing a grading_row_id or inventory_id. It cannot be safely returned.")
                    st.stop()

                # Use the already-stored total grading cost as the base. The old version added
                # grading_fee_initial + grading_fee_per_card, which could double-count the same fee.
                existing_grading_cost = (
                    to_money(rec.get("total_grading_cost"))
                    or to_money(rec.get("grading_fee_per_card"))
                    or to_money(rec.get("grading_fee_initial"))
                )
                total_grading_cost = round(existing_grading_cost + additional_cost, 2)

                update_rows_by_key(
                    get_ws_name("grading_worksheet", "grading"),
                    GRADING_COLUMNS,
                    "grading_row_id",
                    {
                        row_id: {
                            "status": STATUS_RETURNED,
                            "returned_date": str(returned_date),
                            "received_grade": received_grade,
                            "additional_costs": additional_cost,
                            "total_grading_cost": round(total_grading_cost, 2),
                            "updated_at": now_iso(),
                            "synced_to_inventory": "YES",
                        }
                    },
                )

                inv_lookup = inv.copy()
                inv_lookup["inventory_id"] = inv_lookup["inventory_id"].astype(str).str.strip()
                inv_lookup = inv_lookup.drop_duplicates(subset=["inventory_id"], keep="first").set_index("inventory_id", drop=False)

                inv_rec = inv_lookup.loc[inv_id] if inv_id in inv_lookup.index else None
                base_cost = to_money(inv_rec.get("total_price")) if inv_rec is not None else 0.0

                update_rows_by_key(
                    get_ws_name("inventory_worksheet", "inventory"),
                    INVENTORY_COLUMNS,
                    "inventory_id",
                    {
                        inv_id: {
                            "inventory_status": STATUS_ACTIVE,
                            "product_type": "Graded Card",
                            "grading_company": clean_text(rec.get("grading_company")),
                            "grade": received_grade,
                            "condition": "Graded",
                            "grading_fee": round(total_grading_cost, 2),
                            "total_cost": round(base_cost + total_grading_cost, 2),
                        }
                    },
                )

                st.success("Return updated and grading fee synced to inventory.")
                refresh_database_cache()
                st.rerun()


# =========================================================
# Tab 3: Submission history
# =========================================================

with t3:
    st.subheader("Submission History")

    if st.button("Safely sync open grading fees to inventory"):
        changed = _safe_sync_grading_rows_to_inventory(inv, grading)
        st.success(f"Synced {changed:,} inventory row(s). Rows with blank/duplicated inventory_id or duplicate open grading rows were skipped.")
        refresh_database_cache()
        st.rerun()

    if grading.empty:
        st.info("No grading records yet.")
    else:
        summary = (
            grading.groupby(["submission_id", "status"], dropna=False)
            .agg(
                cards=("grading_row_id", "count"),
                grading_cost=("total_grading_cost", "sum"),
                purchase_total=("purchase_total", "sum"),
            )
            .reset_index()
        )

        st.dataframe(
            summary.style.format({"grading_cost": "${:,.2f}", "purchase_total": "${:,.2f}"}),
            use_container_width=True,
            hide_index=True,
        )

        st.dataframe(
            grading.sort_values("submission_date", ascending=False),
            use_container_width=True,
            hide_index=True,
        )
