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


def _grading_valid_rows(grading: pd.DataFrame) -> pd.DataFrame:
    """Return real grading-card rows, excluding repair/cancelled records and blanks."""
    if grading.empty:
        return pd.DataFrame()

    g = grading.copy()
    g["inventory_id"] = g["inventory_id"].astype(str).str.strip()
    g["status"] = g["status"].astype(str).str.upper().str.strip()

    excluded_statuses = {
        "DUPLICATE_CLEARED",
        "CANCELLED",
        "CANCELED",
    }

    g = g[~g["status"].isin(excluded_statuses)].copy()
    g = g[
        g["grading_row_id"].astype(str).str.strip().ne("")
        | g["inventory_id"].astype(str).str.strip().ne("")
    ].copy()

    return g


def _grading_received_rows(grading: pd.DataFrame) -> pd.DataFrame:
    """Return grading rows that have actually come back from the grader."""
    valid = _grading_valid_rows(grading)
    if valid.empty:
        return pd.DataFrame()

    status_received = valid["status"].isin({"RETURNED", "COMPLETE", "COMPLETED"})
    returned_date_text = valid["returned_date"].fillna("").astype(str).str.strip()
    returned_date_present = returned_date_text.ne("") & ~returned_date_text.str.lower().isin(
        {"nan", "none", "<na>"}
    )

    return valid[status_received | returned_date_present].copy()


def _numeric_grade_series(series: pd.Series) -> pd.Series:
    """Parse grades such as 10, 10.0, 'PSA 10', etc. and keep only 1-10."""
    if series is None:
        return pd.Series(dtype="float64")

    text = series.fillna("").astype(str).str.strip()
    numeric = pd.to_numeric(
        text.str.extract(r"(-?\d+(?:\.\d+)?)", expand=False),
        errors="coerce",
    )
    return numeric.where(numeric.between(1, 10, inclusive="both"))


def _grading_grade_audit(inv: pd.DataFrame, grading: pd.DataFrame) -> pd.DataFrame:
    """
    Build one audit row per received grading row.

    Inventory is the preferred final-grade source because the return workflow
    writes the received grade back to inventory. Grading history remains a
    fallback/source-of-record check. A card is treated as a gem if either valid
    source records a 10; any source disagreement is explicitly surfaced.
    """
    received = _grading_received_rows(grading)
    if received.empty:
        return pd.DataFrame()

    audit = received.copy()
    audit["grading_grade"] = _resolve_valid_received_grade(audit)

    inventory_grade_map: dict[str, float] = {}
    if not inv.empty and "inventory_id" in inv.columns and "grade" in inv.columns:
        inv_grade = inv[["inventory_id", "grade"]].copy()
        inv_grade["inventory_id"] = inv_grade["inventory_id"].fillna("").astype(str).str.strip()
        inv_grade["inventory_grade"] = _numeric_grade_series(inv_grade["grade"])
        inv_grade = inv_grade[inv_grade["inventory_id"].ne("")].copy()
        inv_grade["__has_grade"] = inv_grade["inventory_grade"].notna()
        inv_grade = inv_grade.sort_values("__has_grade", ascending=False)
        inv_grade = inv_grade.drop_duplicates(subset=["inventory_id"], keep="first")
        inventory_grade_map = inv_grade.set_index("inventory_id")["inventory_grade"].to_dict()

    audit["inventory_grade"] = pd.to_numeric(
        audit["inventory_id"].fillna("").astype(str).str.strip().map(inventory_grade_map),
        errors="coerce",
    )
    audit["inventory_grade"] = audit["inventory_grade"].where(
        audit["inventory_grade"].between(1, 10, inclusive="both")
    )

    audit["resolved_grade"] = audit["inventory_grade"].combine_first(audit["grading_grade"])

    both_present = audit["inventory_grade"].notna() & audit["grading_grade"].notna()
    audit["grade_conflict"] = both_present & (
        audit["inventory_grade"].round(3) != audit["grading_grade"].round(3)
    )

    audit["is_gem_10"] = audit["inventory_grade"].eq(10) | audit["grading_grade"].eq(10)

    return audit


def _grading_roi_detail(inv: pd.DataFrame, grading: pd.DataFrame) -> pd.DataFrame:
    """
    Realized grading ROI comes directly from INVENTORY.

    Include an inventory row only when:
      1) its inventory_id appears in legitimate grading history, and
      2) inventory_status is SOLD.

    Dollars come from INVENTORY.profit. Cost comes from INVENTORY.total_cost.
    No sale proceeds or fees are reconstructed here.
    """
    if inv.empty or grading.empty:
        return pd.DataFrame()

    valid_grading = _grading_valid_rows(grading)
    if valid_grading.empty:
        return pd.DataFrame()

    grading_ids = set(
        valid_grading["inventory_id"]
        .dropna()
        .astype(str)
        .str.strip()
        .replace("", pd.NA)
        .dropna()
        .tolist()
    )
    if not grading_ids:
        return pd.DataFrame()

    sold = inv.copy()
    sold["inventory_id"] = sold["inventory_id"].fillna("").astype(str).str.strip()
    sold["inventory_status"] = sold["inventory_status"].fillna("").astype(str).str.upper().str.strip()

    sold = sold[
        sold["inventory_status"].eq("SOLD")
        & sold["inventory_id"].isin(grading_ids)
    ].copy()

    if sold.empty:
        return pd.DataFrame()

    sold = sold.drop_duplicates(subset=["inventory_id"], keep="first").copy()

    sold["roi_total_cost"] = pd.to_numeric(sold["total_cost"], errors="coerce")
    sold["roi_profit"] = pd.to_numeric(sold["profit"], errors="coerce")
    sold["included_in_roi"] = sold["roi_total_cost"].notna() & sold["roi_profit"].notna()

    sold["roi_pct"] = float("nan")
    pct_mask = sold["included_in_roi"] & sold["roi_total_cost"].gt(0)
    sold.loc[pct_mask, "roi_pct"] = (
        sold.loc[pct_mask, "roi_profit"] / sold.loc[pct_mask, "roi_total_cost"] * 100.0
    )

    sold["roi_note"] = "Included"
    sold.loc[sold["roi_total_cost"].isna(), "roi_note"] = "Missing total_cost"
    sold.loc[sold["roi_profit"].isna(), "roi_note"] = "Missing profit"
    sold.loc[
        sold["roi_total_cost"].notna() & sold["roi_total_cost"].le(0),
        "roi_note",
    ] = "Cost is zero/non-positive; included in ROI $ but not % denominator"

    return sold


def _grading_dashboard_metrics(inv: pd.DataFrame, grading: pd.DataFrame) -> dict[str, float | int]:
    """Build top-level grading KPIs from grading history + inventory sales."""
    empty_metrics = {
        "sent": 0,
        "received": 0,
        "outstanding": 0,
        "gem_10s": 0,
        "graded_received": 0,
        "grade_conflicts": 0,
        "gem_rate": 0.0,
        "sold_graded_cards": 0,
        "roi_missing_rows": 0,
        "roi_dollars": 0.0,
        "roi_pct": 0.0,
    }

    valid = _grading_valid_rows(grading)
    if valid.empty:
        return empty_metrics

    received = _grading_received_rows(grading)
    grade_audit = _grading_grade_audit(inv, grading)

    sent_count = int(len(valid))
    received_count = int(len(received))
    outstanding_count = max(sent_count - received_count, 0)

    gem_10s = int(grade_audit["is_gem_10"].sum()) if not grade_audit.empty else 0
    graded_received_count = (
        int(grade_audit[["inventory_grade", "grading_grade"]].notna().any(axis=1).sum())
        if not grade_audit.empty
        else 0
    )
    grade_conflicts = int(grade_audit["grade_conflict"].sum()) if not grade_audit.empty else 0
    gem_rate = (gem_10s / received_count * 100.0) if received_count else 0.0

    metrics = {
        "sent": sent_count,
        "received": received_count,
        "outstanding": outstanding_count,
        "gem_10s": gem_10s,
        "graded_received": graded_received_count,
        "grade_conflicts": grade_conflicts,
        "gem_rate": gem_rate,
        "sold_graded_cards": 0,
        "roi_missing_rows": 0,
        "roi_dollars": 0.0,
        "roi_pct": 0.0,
    }

    roi_detail = _grading_roi_detail(inv, grading)
    if roi_detail.empty:
        return metrics

    included = roi_detail[roi_detail["included_in_roi"]].copy()
    metrics["sold_graded_cards"] = int(len(roi_detail))
    metrics["roi_missing_rows"] = int((~roi_detail["included_in_roi"]).sum())

    if included.empty:
        return metrics

    metrics["roi_dollars"] = float(included["roi_profit"].sum())

    pct_rows = included[included["roi_total_cost"] > 0].copy()
    total_cost = float(pct_rows["roi_total_cost"].sum()) if not pct_rows.empty else 0.0
    total_profit_for_pct = float(pct_rows["roi_profit"].sum()) if not pct_rows.empty else 0.0
    metrics["roi_pct"] = (total_profit_for_pct / total_cost * 100.0) if total_cost > 0 else 0.0

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
            "PSA 10s divided by all cards received back from grading. "
            "The final Inventory grade is preferred and Grading history is used as a fallback/check. "
            f"Current: {grading_metrics['gem_10s']:,} gem mint 10(s) out of "
            f"{grading_metrics['received']:,} received card(s). "
            f"A usable grade exists for {grading_metrics['graded_received']:,} received card(s). "
            f"Grade-source conflicts: {grading_metrics['grade_conflicts']:,}."
        ),
    )

with metric_cols[4]:
    st.metric(
        "Realized ROI $",
        money_fmt(grading_metrics["roi_dollars"]),
        help=(
            "Sum of INVENTORY.profit for inventory IDs that appear in grading history and are currently marked SOLD. "
            "The app does not reconstruct profit from proceeds or fees for this KPI."
        ),
    )

with metric_cols[5]:
    st.metric(
        "Realized ROI %",
        f"{grading_metrics['roi_pct']:.1f}%",
        help=(
            "Aggregate realized grading ROI: SUM(INVENTORY.profit) / SUM(INVENTORY.total_cost) × 100 "
            "for graded inventory IDs marked SOLD. "
            f"Matched {grading_metrics['sold_graded_cards']:,} sold graded card(s); "
            f"{grading_metrics['roi_missing_rows']:,} are missing usable profit or total_cost and are excluded."
        ),
    )

st.caption(
    "ROI uses the Inventory sheet only: inventory_id must appear in grading history, inventory_status must be SOLD, "
    "ROI $ is the stored profit, and ROI % is total profit divided by total cost."
)

grade_audit = _grading_grade_audit(inv, grading)
if not grade_audit.empty:
    with st.expander("Gem rate audit", expanded=False):
        st.caption(
            "Use this to verify every received card contributing to Gem Rate. "
            "A card counts as a gem when either the Inventory final grade or Grading received grade is 10. "
            "Any disagreement between those sources is flagged."
        )
        grade_show = grade_audit.copy()
        grade_show["gem_10"] = grade_show["is_gem_10"].map({True: "YES", False: ""})
        grade_show["grade_conflict"] = grade_show["grade_conflict"].map({True: "REVIEW", False: ""})
        grade_cols = [
            "inventory_id",
            "card_name",
            "card_number",
            "status",
            "returned_date",
            "grading_grade",
            "inventory_grade",
            "resolved_grade",
            "gem_10",
            "grade_conflict",
        ]
        st.dataframe(
            grade_show[[c for c in grade_cols if c in grade_show.columns]],
            use_container_width=True,
            hide_index=True,
        )

roi_detail = _grading_roi_detail(inv, grading)
if not roi_detail.empty:
    with st.expander("Realized grading ROI detail", expanded=False):
        st.caption(
            "These are the exact Inventory rows used for grading ROI. "
            "ROI $ = sum of Profit. ROI % = sum of Profit ÷ sum of Total Cost. "
            "Rows missing Profit or Total Cost are shown but excluded from the KPI."
        )
        roi_show = roi_detail.copy()
        roi_show["included"] = roi_show["included_in_roi"].map({True: "YES", False: "NO"})
        roi_cols = [
            "inventory_id",
            "card_name",
            "grade",
            "sold_date",
            "sale_channel",
            "roi_total_cost",
            "roi_profit",
            "roi_pct",
            "included",
            "roi_note",
        ]
        st.dataframe(
            roi_show[[c for c in roi_cols if c in roi_show.columns]],
            use_container_width=True,
            hide_index=True,
            column_config={
                "roi_total_cost": st.column_config.NumberColumn("Total Cost", format="$%.2f"),
                "roi_profit": st.column_config.NumberColumn("Profit", format="$%.2f"),
                "roi_pct": st.column_config.NumberColumn("Card ROI %", format="%.1f%%"),
            },
        )

        included_roi = roi_detail[roi_detail["included_in_roi"]].copy()
        if not included_roi.empty:
            pct_roi = included_roi[included_roi["roi_total_cost"] > 0].copy()
            total_cost_roi = float(pct_roi["roi_total_cost"].sum()) if not pct_roi.empty else 0.0
            total_profit_roi = float(included_roi["roi_profit"].sum())
            total_profit_pct_roi = float(pct_roi["roi_profit"].sum()) if not pct_roi.empty else 0.0
            aggregate_roi_pct = (total_profit_pct_roi / total_cost_roi * 100.0) if total_cost_roi > 0 else 0.0
            st.caption(
                f"Included totals — Total Cost: {money_fmt(total_cost_roi)} | "
                f"Profit: {money_fmt(total_profit_roi)} | "
                f"Realized ROI: {aggregate_roi_pct:.1f}%"
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
