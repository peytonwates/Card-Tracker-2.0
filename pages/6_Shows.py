from __future__ import annotations

import hashlib
import io
import re
import unicodedata
import uuid
from collections import defaultdict, deque
from datetime import date
from typing import Any

import pandas as pd
import streamlit as st

from core.business import load_data, mark_inventory_sold, refresh_database_cache
from core.cleaning import clean_text, money_fmt, now_iso, to_money
from core.config import (
    INVENTORY_COLUMNS,
    SHOW_COLUMNS,
    STATUS_ACTIVE,
    STATUS_SOLD,
)
from core.sheets import append_rows, get_ws_name


st.set_page_config(page_title="Shows", layout="wide")
st.title("Shows")
st.caption(
    "Reconcile a Collectr snapshot against ACTIVE, business-owned Pokémon card "
    "inventory, then process new purchases and missing-card sales."
)
st.caption(
    "Shows page build: 2026-07-20 · Collectr reconciliation · "
    "Owned Pokémon ACTIVE cards only"
)


# =========================================================
# Constants
# =========================================================

TRUTHY_VALUES = {"1", "true", "yes", "y", "x", "sold", "add", "process"}
FALSEY_VALUES = {"0", "false", "no", "n", "", "skip", "ignore"}

COLLECTR_ALIASES = {
    "portfolio_name": ["portfolio_name", "portfolio"],
    "category": ["category", "game", "brand_or_league"],
    "set_name": ["set", "set_name"],
    "card_name": ["product_name", "card_name", "name"],
    "card_number": ["card_number", "number", "card_no"],
    "rarity": ["rarity", "card_subtype"],
    "variant": ["variance", "variant", "finish"],
    "grade": ["grade"],
    "condition": ["card_condition", "condition"],
    "average_cost_paid": ["average_cost_paid", "cost_paid"],
    "quantity": ["quantity", "qty"],
    "market_value": ["market_price", "market_value", "price"],
    "price_override": ["price_override"],
    "date_added": ["date_added"],
    "notes": ["notes"],
}

MATCH_DISPLAY_COLUMNS = [
    "match_method",
    "inventory_id",
    "set_name",
    "card_name",
    "card_number",
    "variant",
    "grading_company",
    "grade",
    "condition",
    "market_value",
]

NEW_PURCHASE_COLUMNS = [
    "process_row",
    "inventory_id",
    "purchase_date",
    "purchased_from",
    "purchase_price",
    "shipping",
    "tax",
    "grading_fee",
    "inventory_type",
    "product_type",
    "brand_or_league",
    "set_name",
    "card_name",
    "card_number",
    "variant",
    "card_subtype",
    "grading_company",
    "grade",
    "condition",
    "market_value",
    "sticker_price",
    "notes",
    "reconciliation_batch_id",
    "collectr_row_id",
]

MISSING_SALE_COLUMNS = [
    "process_row",
    "inventory_id",
    "sold_date",
    "sold_price",
    "fees",
    "sale_notes",
    "show_id",
    "show_name",
    "set_name",
    "card_name",
    "card_number",
    "variant",
    "grading_company",
    "grade",
    "condition",
    "total_cost",
    "market_value",
    "sticker_price",
    "reconciliation_batch_id",
]


# =========================================================
# General helpers
# =========================================================


def _safe_df(df: pd.DataFrame | None) -> pd.DataFrame:
    return pd.DataFrame() if df is None else df.copy()


def _series(df: pd.DataFrame, col: str, default: Any = "") -> pd.Series:
    if col in df.columns:
        return df[col]
    return pd.Series([default] * len(df), index=df.index)


def _clean_column_name(value: Any) -> str:
    text = clean_text(value).replace("\ufeff", "").lower()
    text = re.sub(r"\([^)]*\)", "", text)
    text = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
    return text


def _text(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return clean_text(value)


def _norm(value: Any) -> str:
    text = _text(value)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower().replace("pokémon", "pokemon")
    text = text.replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def _compact(value: Any) -> str:
    return _norm(value).replace(" ", "")


def _normalized_card_name(value: Any) -> str:
    text = _norm(value)
    # Parenthetical wording often moves between Collectr's product name and
    # the inventory variant field. Keep the base identity for matching.
    text = re.sub(
        r"\b(alternate full art|full art|illustration rare|special illustration rare|secret rare)\b",
        "",
        text,
    )
    return " ".join(text.split())


def _normalized_set(value: Any) -> str:
    text = _norm(value)
    replacements = {
        "scarlet and violet base set": "scarlet violet",
        "base set unlimited": "base set",
        "sv 151": "151",
        "pokemon 151": "151",
        "wotc promo": "wizards promo",
        "xy promos": "xy promo",
        "sword and shield promos": "sword shield promo",
        "scarlet and violet promos": "scarlet violet promo",
    }
    return replacements.get(text, text)


def _normalized_card_number(value: Any) -> str:
    raw = _compact(value).upper()
    if not raw:
        return ""

    parts = raw.split("/")
    normalized_parts: list[str] = []
    for part in parts:
        match = re.fullmatch(r"([A-Z]*)(\d+)([A-Z]*)", part)
        if match:
            prefix, digits, suffix = match.groups()
            normalized_parts.append(f"{prefix}{int(digits)}{suffix}")
        else:
            normalized_parts.append(part)
    return "/".join(normalized_parts)


def _normalized_variant(value: Any) -> str:
    text = _norm(value)
    aliases = {
        "holo": "holofoil",
        "reverse holo": "reverse holofoil",
        "normal foil": "holofoil",
        "non holo": "normal",
        "non holofoil": "normal",
    }
    return aliases.get(text, text)


def _normalized_condition(value: Any) -> str:
    text = _norm(value)
    aliases = {
        "nm": "near mint",
        "lp": "lightly played",
        "mp": "moderately played",
        "hp": "heavily played",
        "dmg": "damaged",
    }
    return aliases.get(text, text)


def _truthy(value: Any, default: bool = False) -> bool:
    text = _norm(value)
    if text in TRUTHY_VALUES:
        return True
    if text in FALSEY_VALUES:
        return False
    return default


def _has_value(value: Any) -> bool:
    if value is None or pd.isna(value):
        return False
    return bool(_text(value))


def _date_sort(df: pd.DataFrame, col: str, ascending: bool = False) -> pd.DataFrame:
    if df.empty or col not in df.columns:
        return df
    out = df.copy()
    out["__sort_dt"] = pd.to_datetime(out[col], errors="coerce")
    out = out.sort_values("__sort_dt", ascending=ascending, na_position="last")
    return out.drop(columns=["__sort_dt"], errors="ignore")


def _show_label(row: pd.Series) -> str:
    show_id = _text(row.get("show_id"))
    name = _text(row.get("show_name")) or "Unnamed show"
    show_date = _text(row.get("show_date"))
    location = _text(row.get("location"))

    label = f"{name}"
    if show_date:
        label += f" ({show_date})"
    if location:
        label += f" — {location}"
    if show_id:
        label += f" — {show_id}"
    return label


def _selected_show(shows: pd.DataFrame, key: str) -> pd.Series | None:
    if shows.empty:
        st.info("Add a show in Manage Shows before reconciling inventory.")
        return None

    options = _date_sort(shows, "show_date", ascending=False).copy()
    options["__label"] = options.apply(_show_label, axis=1)
    selected_label = st.selectbox("Show", options["__label"].tolist(), key=key)
    return options[options["__label"].eq(selected_label)].iloc[0]


def _file_bytes(uploaded_file) -> bytes:
    if hasattr(uploaded_file, "getvalue"):
        return uploaded_file.getvalue()
    current_position = uploaded_file.tell()
    uploaded_file.seek(0)
    data = uploaded_file.read()
    uploaded_file.seek(current_position)
    return data


def _batch_id(uploaded_file, selected_show: pd.Series) -> str:
    digest = hashlib.sha256(_file_bytes(uploaded_file)).hexdigest()[:10]
    show_id = _compact(selected_show.get("show_id")) or "show"
    return f"REC-{show_id[:10]}-{digest}".upper()


def _csv_download_data(df: pd.DataFrame) -> bytes:
    return df.to_csv(index=False).encode("utf-8-sig")


def _read_csv_or_excel(uploaded_file) -> pd.DataFrame:
    filename = _text(getattr(uploaded_file, "name", "")).lower()
    raw = _file_bytes(uploaded_file)

    if filename.endswith(".xlsx") or filename.endswith(".xls"):
        return pd.read_excel(io.BytesIO(raw), dtype=str)

    # utf-8-sig handles Excel-generated CSVs with a BOM. Fall back to latin-1
    # for older exports that contain non-UTF characters.
    try:
        return pd.read_csv(
            io.BytesIO(raw), dtype=str, keep_default_na=False, encoding="utf-8-sig"
        )
    except UnicodeDecodeError:
        return pd.read_csv(
            io.BytesIO(raw), dtype=str, keep_default_na=False, encoding="latin-1"
        )


# =========================================================
# Inventory scope rules
# =========================================================


def _status_value(row: pd.Series) -> str:
    return _norm(row.get("inventory_status"))


def _is_explicit_consignment(row: pd.Series) -> bool:
    structured_text = " ".join(
        _norm(row.get(col))
        for col in [
            "inventory_type",
            "ownership_type",
            "owner_type",
            "inventory_owner",
        ]
    )
    if "consign" in structured_text:
        return True

    if _truthy(row.get("is_consignment"), default=False):
        return True

    return any(
        _has_value(row.get(col))
        for col in ["consignor", "consignor_name", "consignor_id"]
    )


def _is_explicit_personal(row: pd.Series) -> bool:
    structured_text = " ".join(
        _norm(row.get(col))
        for col in [
            "inventory_type",
            "ownership_type",
            "owner_type",
            "inventory_owner",
            "portfolio_name",
        ]
    )
    if "personal" in structured_text:
        return True
    return _truthy(row.get("is_personal"), default=False)


def _is_sealed_or_non_card(row: pd.Series) -> bool:
    if _has_value(row.get("sealed_product_type")):
        return True

    product_text = " ".join(
        _norm(row.get(col))
        for col in ["product_type", "card_type", "inventory_type"]
    )
    sealed_terms = {
        "sealed",
        "booster box",
        "booster bundle",
        "elite trainer box",
        "etb",
        "collection box",
        "tin",
    }
    return any(term in product_text for term in sealed_terms)


def _is_pokemon_inventory(row: pd.Series) -> bool:
    structured_values = [
        row.get("brand_or_league"),
        row.get("category"),
        row.get("game"),
        row.get("franchise"),
        row.get("sport"),
    ]
    structured_text = " ".join(_norm(value) for value in structured_values)
    return "pokemon" in structured_text


def _inventory_scope_reason(row: pd.Series) -> str:
    status = _status_value(row)
    if status != _norm(STATUS_ACTIVE):
        if "grad" in status:
            return "GRADING / not ACTIVE"
        return "Not ACTIVE"

    if _is_explicit_consignment(row):
        return "Consignment"

    if _is_explicit_personal(row):
        return "Personal inventory"

    if _is_sealed_or_non_card(row):
        return "Sealed / non-card inventory"

    if not _is_pokemon_inventory(row):
        return "Sports / non-Pokémon"

    return "Eligible ACTIVE owned Pokémon card"


def _prepare_inventory_scope(inventory: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    inv = inventory.copy()
    if inv.empty:
        return inv, pd.DataFrame(columns=["scope_reason", "count"])

    inv["inventory_id"] = _series(inv, "inventory_id").astype(str).str.strip()
    inv["inventory_status"] = (
        _series(inv, "inventory_status").astype(str).str.upper().str.strip()
    )
    inv["__scope_reason"] = inv.apply(_inventory_scope_reason, axis=1)

    eligible = inv[
        inv["__scope_reason"].eq("Eligible ACTIVE owned Pokémon card")
    ].copy()

    summary = (
        inv.groupby("__scope_reason", dropna=False)
        .size()
        .reset_index(name="count")
        .rename(columns={"__scope_reason": "scope_reason"})
        .sort_values("count", ascending=False)
    )
    return eligible, summary


# =========================================================
# Collectr parsing and matching
# =========================================================


def _find_collectr_source_column(columns: list[str], aliases: list[str]) -> str | None:
    normalized = {_clean_column_name(col): col for col in columns}

    for alias in aliases:
        if alias in normalized:
            return normalized[alias]

    # Collectr's market-price heading includes an as-of date, so prefix matching
    # is needed after the date text is normalized out.
    for alias in aliases:
        for normalized_name, original_name in normalized.items():
            if normalized_name.startswith(alias):
                return original_name
    return None


def _parse_collectr_grade(value: Any) -> tuple[str, str]:
    text = _text(value)
    normalized = _norm(text)
    if not normalized or normalized in {"ungraded", "raw", "none", "na"}:
        return "", ""

    match = re.match(r"^(psa|bgs|cgc|sgc|ace)\s*([0-9]+(?:\.[0-9]+)?)$", normalized)
    if match:
        return match.group(1).upper(), match.group(2)

    return "", text


def _parse_collectr(uploaded_file) -> tuple[pd.DataFrame, dict[str, int]]:
    raw = _read_csv_or_excel(uploaded_file)
    raw.columns = [_text(col) for col in raw.columns]

    canonical = pd.DataFrame(index=raw.index)
    for target, aliases in COLLECTR_ALIASES.items():
        source = _find_collectr_source_column(raw.columns.tolist(), aliases)
        canonical[target] = raw[source] if source else ""

    canonical["category"] = canonical["category"].astype(str).str.strip()
    pokemon_mask = canonical["category"].apply(lambda value: "pokemon" in _norm(value))

    stats = {
        "source_rows": int(len(canonical)),
        "pokemon_rows": int(pokemon_mask.sum()),
        "non_pokemon_rows_ignored": int((~pokemon_mask).sum()),
    }

    canonical = canonical[pokemon_mask].copy()
    canonical = canonical[
        canonical["card_name"].astype(str).str.strip().ne("")
    ].copy()

    expanded_rows: list[dict[str, Any]] = []
    for source_position, (_, row) in enumerate(canonical.iterrows(), start=1):
        quantity_number = pd.to_numeric(row.get("quantity"), errors="coerce")
        quantity = int(quantity_number) if pd.notna(quantity_number) else 1
        quantity = max(1, min(quantity, 500))

        grading_company, grade = _parse_collectr_grade(row.get("grade"))
        market_value = to_money(row.get("market_value"))
        price_override = to_money(row.get("price_override"))
        if price_override > 0:
            market_value = price_override

        for copy_number in range(1, quantity + 1):
            expanded_rows.append(
                {
                    "collectr_row_id": f"C{source_position:04d}-{copy_number:03d}",
                    "portfolio_name": _text(row.get("portfolio_name")),
                    "category": "Pokemon",
                    "set_name": _text(row.get("set_name")),
                    "card_name": _text(row.get("card_name")),
                    "card_number": _text(row.get("card_number")),
                    "card_subtype": _text(row.get("rarity")),
                    "variant": _text(row.get("variant")),
                    "grading_company": grading_company,
                    "grade": grade,
                    "condition": _text(row.get("condition")),
                    "average_cost_paid": to_money(row.get("average_cost_paid")),
                    "market_value": market_value,
                    "date_added": _text(row.get("date_added")),
                    "collectr_notes": _text(row.get("notes")),
                    "source_quantity": quantity,
                    "source_copy_number": copy_number,
                }
            )

    expanded = pd.DataFrame(expanded_rows)
    stats["individual_cards"] = int(len(expanded))
    return expanded, stats


def _normalized_grade(value: Any) -> str:
    text = _text(value)
    normalized = _norm(text)
    if not normalized or normalized in {"ungraded", "raw", "none", "na"}:
        return ""

    numeric = pd.to_numeric(text, errors="coerce")
    if pd.notna(numeric):
        return f"{float(numeric):g}"

    return normalized


def _grade_signature(row: pd.Series) -> str:
    company = _norm(row.get("grading_company"))
    grade = _normalized_grade(row.get("grade"))
    if not grade:
        return "raw"
    return f"{company}:{grade}" if company else grade


def _identity_parts(row: pd.Series) -> dict[str, str]:
    return {
        "name": _normalized_card_name(row.get("card_name")),
        "number": _normalized_card_number(row.get("card_number")),
        "set": _normalized_set(row.get("set_name")),
        "grade": _grade_signature(row),
        "variant": _normalized_variant(row.get("variant")),
        "condition": _normalized_condition(row.get("condition")),
    }


def _key(row: pd.Series, fields: tuple[str, ...]) -> tuple[str, ...]:
    parts = _identity_parts(row)
    return tuple(parts[field] for field in fields)


def _match_stage(
    collectr: pd.DataFrame,
    inventory: pd.DataFrame,
    unmatched_collectr: set[int],
    unmatched_inventory: set[int],
    fields: tuple[str, ...],
    method: str,
    *,
    require_nonblank_number: bool = False,
    unique_inventory_identity_fields: tuple[str, ...] | None = None,
) -> list[dict[str, Any]]:
    collectr_groups: dict[tuple[str, ...], deque[int]] = defaultdict(deque)
    inventory_groups: dict[tuple[str, ...], deque[int]] = defaultdict(deque)

    for idx in sorted(unmatched_collectr):
        row = collectr.loc[idx]
        if require_nonblank_number and not _normalized_card_number(row.get("card_number")):
            continue
        stage_key = _key(row, fields)
        if not stage_key or not stage_key[0]:
            continue
        collectr_groups[stage_key].append(idx)

    for idx in sorted(unmatched_inventory):
        row = inventory.loc[idx]
        if require_nonblank_number and not _normalized_card_number(row.get("card_number")):
            continue
        stage_key = _key(row, fields)
        if not stage_key or not stage_key[0]:
            continue
        inventory_groups[stage_key].append(idx)

    matches: list[dict[str, Any]] = []

    for stage_key in sorted(set(collectr_groups).intersection(inventory_groups)):
        collectr_queue = collectr_groups[stage_key]
        inventory_queue = inventory_groups[stage_key]

        if unique_inventory_identity_fields:
            identities = {
                _key(inventory.loc[idx], unique_inventory_identity_fields)
                for idx in inventory_queue
            }
            if len(identities) > 1:
                continue

        while collectr_queue and inventory_queue:
            collectr_idx = collectr_queue.popleft()
            inventory_idx = inventory_queue.popleft()

            if collectr_idx not in unmatched_collectr or inventory_idx not in unmatched_inventory:
                continue

            unmatched_collectr.remove(collectr_idx)
            unmatched_inventory.remove(inventory_idx)
            matches.append(
                {
                    "collectr_index": collectr_idx,
                    "inventory_index": inventory_idx,
                    "match_method": method,
                }
            )

    return matches


def _reconcile_collectr(
    collectr: pd.DataFrame,
    eligible_inventory: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    collectr_work = collectr.reset_index(drop=True).copy()
    inventory_work = eligible_inventory.reset_index(drop=True).copy()

    unmatched_collectr = set(collectr_work.index.tolist())
    unmatched_inventory = set(inventory_work.index.tolist())
    match_records: list[dict[str, Any]] = []

    # Highest confidence: full identity including variant and condition.
    match_records.extend(
        _match_stage(
            collectr_work,
            inventory_work,
            unmatched_collectr,
            unmatched_inventory,
            ("name", "number", "set", "grade", "variant", "condition"),
            "Exact",
        )
    )

    # Condition is often not maintained identically between systems.
    match_records.extend(
        _match_stage(
            collectr_work,
            inventory_work,
            unmatched_collectr,
            unmatched_inventory,
            ("name", "number", "set", "grade", "variant"),
            "Exact except condition",
        )
    )

    # Variant wording may be stored in Collectr's product name instead.
    match_records.extend(
        _match_stage(
            collectr_work,
            inventory_work,
            unmatched_collectr,
            unmatched_inventory,
            ("name", "number", "set", "grade"),
            "Name/number/set/grade",
            unique_inventory_identity_fields=("set", "variant", "grade"),
        )
    )

    # Set names vary between Collectr and inventory. Only use this fallback when
    # the remaining inventory candidates share one set/variant identity.
    match_records.extend(
        _match_stage(
            collectr_work,
            inventory_work,
            unmatched_collectr,
            unmatched_inventory,
            ("name", "number", "grade", "variant"),
            "Unique name/number/grade/variant",
            require_nonblank_number=True,
            unique_inventory_identity_fields=("set", "variant", "grade"),
        )
    )

    match_records.extend(
        _match_stage(
            collectr_work,
            inventory_work,
            unmatched_collectr,
            unmatched_inventory,
            ("name", "number", "grade"),
            "Unique name/number/grade",
            require_nonblank_number=True,
            unique_inventory_identity_fields=("set", "variant", "grade"),
        )
    )

    # Cards without numbers can still match when set/name/grade/variant are exact.
    match_records.extend(
        _match_stage(
            collectr_work,
            inventory_work,
            unmatched_collectr,
            unmatched_inventory,
            ("name", "set", "grade", "variant"),
            "Name/set/grade/variant",
            unique_inventory_identity_fields=("set", "variant", "grade"),
        )
    )

    matched_rows: list[dict[str, Any]] = []
    for record in match_records:
        collectr_row = collectr_work.loc[record["collectr_index"]]
        inventory_row = inventory_work.loc[record["inventory_index"]]
        merged = collectr_row.to_dict()
        merged.update(
            {
                "match_method": record["match_method"],
                "inventory_id": _text(inventory_row.get("inventory_id")),
                "inventory_total_cost": to_money(inventory_row.get("total_cost")),
                "inventory_market_value": to_money(inventory_row.get("market_value")),
                "inventory_sticker_price": to_money(inventory_row.get("sticker_price")),
            }
        )
        matched_rows.append(merged)

    matched = pd.DataFrame(matched_rows)
    new_cards = collectr_work.loc[sorted(unmatched_collectr)].copy()
    missing_inventory = inventory_work.loc[sorted(unmatched_inventory)].copy()

    return matched, new_cards, missing_inventory


def _build_new_purchase_form(
    new_cards: pd.DataFrame,
    selected_show: pd.Series,
    batch_id: str,
) -> pd.DataFrame:
    if new_cards.empty:
        return pd.DataFrame(columns=NEW_PURCHASE_COLUMNS)

    form = new_cards.copy()
    show_date = _text(selected_show.get("show_date")) or str(date.today())

    form["process_row"] = "YES"
    form["inventory_id"] = [str(uuid.uuid4())[:8] for _ in range(len(form))]
    form["purchase_date"] = show_date
    form["purchased_from"] = ""
    form["purchase_price"] = ""
    form["shipping"] = 0.0
    form["tax"] = 0.0
    form["grading_fee"] = 0.0
    form["inventory_type"] = "Show Inventory"
    form["product_type"] = "Single Card"
    form["brand_or_league"] = "Pokemon"
    form["sticker_price"] = form["market_value"].apply(to_money)
    form["notes"] = form.apply(
        lambda row: " | ".join(
            part
            for part in [
                _text(row.get("collectr_notes")),
                f"Added from Collectr reconciliation {batch_id}",
            ]
            if part
        ),
        axis=1,
    )
    form["reconciliation_batch_id"] = batch_id

    for col in NEW_PURCHASE_COLUMNS:
        if col not in form.columns:
            form[col] = ""

    return form[NEW_PURCHASE_COLUMNS].copy()


def _build_missing_sales_form(
    missing_inventory: pd.DataFrame,
    selected_show: pd.Series,
    batch_id: str,
) -> pd.DataFrame:
    if missing_inventory.empty:
        return pd.DataFrame(columns=MISSING_SALE_COLUMNS)

    form = missing_inventory.copy()
    show_date = _text(selected_show.get("show_date")) or str(date.today())

    form["process_row"] = "YES"
    form["sold_date"] = show_date
    form["sold_price"] = ""
    form["fees"] = 0.0
    form["sale_notes"] = ""
    form["show_id"] = _text(selected_show.get("show_id"))
    form["show_name"] = _text(selected_show.get("show_name"))
    form["reconciliation_batch_id"] = batch_id

    for col in MISSING_SALE_COLUMNS:
        if col not in form.columns:
            form[col] = ""

    return form[MISSING_SALE_COLUMNS].copy()


# =========================================================
# Upload processors
# =========================================================


def _normalized_upload_columns(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out.columns = [_clean_column_name(col) for col in out.columns]
    return out


def _inventory_row_from_purchase_upload(row: pd.Series) -> dict[str, Any]:
    purchase_price = round(to_money(row.get("purchase_price")), 2)
    shipping = round(to_money(row.get("shipping")), 2)
    tax = round(to_money(row.get("tax")), 2)
    grading_fee = round(to_money(row.get("grading_fee")), 2)
    total_price = round(purchase_price + shipping + tax, 2)
    total_cost = round(total_price + grading_fee, 2)

    grading_company = _text(row.get("grading_company"))
    grade = _text(row.get("grade"))
    card_type = "Graded" if grading_company or grade else "Raw"

    created_at = now_iso()
    new_row = {
        "inventory_id": _text(row.get("inventory_id")) or str(uuid.uuid4())[:8],
        "image_url": "",
        "inventory_type": _text(row.get("inventory_type")) or "Show Inventory",
        "product_type": _text(row.get("product_type")) or "Single Card",
        "inventory_status": STATUS_ACTIVE,
        "sealed_product_type": "",
        "card_type": card_type,
        "brand_or_league": "Pokemon",
        "set_name": _text(row.get("set_name")),
        "year": _text(row.get("year")),
        "card_name": _text(row.get("card_name")),
        "card_number": _text(row.get("card_number")),
        "variant": _text(row.get("variant")),
        "card_subtype": _text(row.get("card_subtype")),
        "grading_company": grading_company,
        "grade": grade,
        "reference_link": "",
        "purchase_date": _text(row.get("purchase_date")) or str(date.today()),
        "purchased_from": _text(row.get("purchased_from")),
        "purchase_price": purchase_price,
        "shipping": shipping,
        "tax": tax,
        "total_price": total_price,
        "grading_fee": grading_fee,
        "total_cost": total_cost,
        "sticker_price": round(to_money(row.get("sticker_price")), 2),
        "condition": _text(row.get("condition")),
        "notes": _text(row.get("notes")),
        "created_at": created_at,
        "updated_at": created_at,
        "sold_date": "",
        "sold_price": "",
        "fees_total": "",
        "net_proceeds": "",
        "profit": "",
        "sale_channel": "",
        "show_name": "",
        "market_value": round(to_money(row.get("market_value")), 2),
        "reconciliation_batch_id": _text(row.get("reconciliation_batch_id")),
    }

    return {col: new_row.get(col, "") for col in INVENTORY_COLUMNS}


def _validate_purchase_upload(
    upload: pd.DataFrame,
    current_inventory: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    out = _normalized_upload_columns(upload)
    required = {"process_row", "inventory_id", "card_name", "purchase_price"}
    missing_columns = sorted(required.difference(out.columns))
    if missing_columns:
        raise ValueError(
            "The purchase form is missing required column(s): "
            + ", ".join(missing_columns)
        )

    out = out[out["process_row"].apply(lambda value: _truthy(value, default=False))].copy()
    existing_ids = set(
        _series(current_inventory, "inventory_id").astype(str).str.strip().tolist()
    )
    duplicate_upload_ids = set(
        out.loc[out["inventory_id"].duplicated(keep=False), "inventory_id"]
        .astype(str)
        .str.strip()
        .tolist()
    )

    validation_rows: list[dict[str, Any]] = []
    for idx, row in out.iterrows():
        inventory_id = _text(row.get("inventory_id"))
        errors: list[str] = []

        if not inventory_id:
            errors.append("Missing inventory_id")
        if inventory_id in existing_ids:
            errors.append("inventory_id already exists")
        if inventory_id in duplicate_upload_ids:
            errors.append("Duplicate inventory_id in upload")
        if not _text(row.get("card_name")):
            errors.append("Missing card_name")
        if not _has_value(row.get("purchase_price")):
            errors.append("Missing purchase_price")
        if "pokemon" not in _norm(row.get("brand_or_league") or "Pokemon"):
            errors.append("Only Pokémon inventory is allowed")
        if "consign" in _norm(row.get("inventory_type")):
            errors.append("Consignment inventory is not processed here")
        if "personal" in _norm(row.get("inventory_type")):
            errors.append("Personal inventory is not processed here")

        validation_rows.append(
            {
                "__index": idx,
                "inventory_id": inventory_id,
                "card_name": _text(row.get("card_name")),
                "status": "VALID" if not errors else "ERROR",
                "validation_message": "; ".join(errors),
            }
        )

    validation = pd.DataFrame(validation_rows)
    valid_indexes = (
        validation.loc[validation["status"].eq("VALID"), "__index"].tolist()
        if not validation.empty
        else []
    )
    valid = out.loc[valid_indexes].copy() if valid_indexes else out.iloc[0:0].copy()
    return valid, validation


def _validate_sales_upload(
    upload: pd.DataFrame,
    current_inventory: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    out = _normalized_upload_columns(upload)
    required = {"process_row", "inventory_id", "sold_date", "sold_price"}
    missing_columns = sorted(required.difference(out.columns))
    if missing_columns:
        raise ValueError(
            "The sales form is missing required column(s): "
            + ", ".join(missing_columns)
        )

    out = out[out["process_row"].apply(lambda value: _truthy(value, default=False))].copy()

    current = current_inventory.copy()
    current["inventory_id"] = _series(current, "inventory_id").astype(str).str.strip()
    current_by_id = {
        _text(row.get("inventory_id")): row
        for _, row in current.iterrows()
        if _text(row.get("inventory_id"))
    }

    duplicate_upload_ids = set(
        out.loc[out["inventory_id"].duplicated(keep=False), "inventory_id"]
        .astype(str)
        .str.strip()
        .tolist()
    )

    validation_rows: list[dict[str, Any]] = []
    for idx, row in out.iterrows():
        inventory_id = _text(row.get("inventory_id"))
        current_row = current_by_id.get(inventory_id)
        errors: list[str] = []

        if not inventory_id:
            errors.append("Missing inventory_id")
        elif current_row is None:
            errors.append("inventory_id not found")
        else:
            scope_reason = _inventory_scope_reason(current_row)
            if scope_reason != "Eligible ACTIVE owned Pokémon card":
                errors.append(f"Not eligible: {scope_reason}")

        if inventory_id in duplicate_upload_ids:
            errors.append("Duplicate inventory_id in upload")

        sold_date = pd.to_datetime(row.get("sold_date"), errors="coerce")
        if pd.isna(sold_date):
            errors.append("Invalid sold_date")
        if not _has_value(row.get("sold_price")):
            errors.append("Missing sold_price")

        validation_rows.append(
            {
                "__index": idx,
                "inventory_id": inventory_id,
                "card_name": _text(row.get("card_name")),
                "status": "VALID" if not errors else "ERROR",
                "validation_message": "; ".join(errors),
            }
        )

    validation = pd.DataFrame(validation_rows)
    valid_indexes = (
        validation.loc[validation["status"].eq("VALID"), "__index"].tolist()
        if not validation.empty
        else []
    )
    valid = out.loc[valid_indexes].copy() if valid_indexes else out.iloc[0:0].copy()
    return valid, validation


# =========================================================
# Load data
# =========================================================


top_left, top_right = st.columns([1, 4])
with top_left:
    if st.button("Refresh database", use_container_width=True):
        refresh_database_cache()
        st.rerun()
with top_right:
    st.info(
        "Comparison scope is locked to ACTIVE, business-owned Pokémon single cards. "
        "Consignment, personal inventory, sports/non-Pokémon cards, sealed products, "
        "GRADING, LISTED, and SOLD records are not considered missing.",
        icon="ℹ️",
    )


data = load_data()
shows = _safe_df(data.shows)
inv = _safe_df(data.inventory)

if not shows.empty:
    shows["show_id"] = _series(shows, "show_id").astype(str).str.strip()
    shows["show_name"] = _series(shows, "show_name").astype(str).str.strip()
    shows["status"] = _series(shows, "status").astype(str).str.strip()

if not inv.empty:
    inv["inventory_id"] = _series(inv, "inventory_id").astype(str).str.strip()
    inv["inventory_status"] = (
        _series(inv, "inventory_status").astype(str).str.upper().str.strip()
    )

eligible_inventory, scope_summary = _prepare_inventory_scope(inv)


tab_manage, tab_reconcile, tab_add, tab_sales, tab_summary = st.tabs(
    [
        "Manage Shows",
        "Reconcile Collectr",
        "Add New Inventory",
        "Update Missing Sales",
        "Show Summary",
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
                "Status", ["Planned", "Completed", "Cancelled"], index=0
            )
        with c3:
            description = st.text_area("Description / notes", height=90)

        submitted = st.form_submit_button("Add show", type="primary")

    if submitted:
        if not _text(show_name):
            st.error("Show name is required.")
        else:
            row = {
                "show_id": str(uuid.uuid4())[:8],
                "show_name": _text(show_name),
                "show_date": str(show_date),
                "location": _text(location),
                "description": _text(description),
                "status": _text(status) or "Planned",
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
        view = _date_sort(shows, "show_date", ascending=False)
        st.dataframe(
            view[[col for col in show_cols if col in view.columns]],
            use_container_width=True,
            hide_index=True,
        )


# =========================================================
# Reconcile Collectr
# =========================================================

with tab_reconcile:
    st.subheader("Reconcile Current Collectr Inventory")
    st.write(
        "Upload the Collectr export that represents the cards you physically have "
        "right now. The app compares that snapshot only with eligible ACTIVE Pokémon "
        "business inventory."
    )

    st.markdown("### Inventory scope")
    scope_cols = st.columns(4)
    scope_cols[0].metric("All inventory rows", f"{len(inv):,}")
    scope_cols[1].metric(
        "Compared ACTIVE Pokémon cards", f"{len(eligible_inventory):,}"
    )
    grading_count = (
        _series(inv, "inventory_status")
        .astype(str)
        .str.upper()
        .str.contains("GRAD", na=False)
        .sum()
    )
    scope_cols[2].metric("GRADING ignored", f"{int(grading_count):,}")
    excluded_active = int(
        scope_summary.loc[
            ~scope_summary["scope_reason"].eq(
                "Eligible ACTIVE owned Pokémon card"
            )
            & scope_summary["scope_reason"].isin(
                [
                    "Consignment",
                    "Personal inventory",
                    "Sports / non-Pokémon",
                    "Sealed / non-card inventory",
                ]
            ),
            "count",
        ].sum()
    )
    scope_cols[3].metric("Other excluded ACTIVE rows", f"{excluded_active:,}")

    with st.expander("See exactly what is excluded", expanded=False):
        st.dataframe(scope_summary, use_container_width=True, hide_index=True)
        st.caption(
            "A record must be ACTIVE, Pokémon, non-consignment, non-personal, and a "
            "single card to participate in the comparison."
        )

    selected_show = _selected_show(shows, "reconcile_show")
    collectr_file = st.file_uploader(
        "Upload current Collectr export",
        type=["csv", "xlsx", "xls"],
        key="collectr_reconciliation_upload",
    )

    if selected_show is not None and collectr_file is not None:
        try:
            collectr_cards, collectr_stats = _parse_collectr(collectr_file)
            batch_id = _batch_id(collectr_file, selected_show)

            if collectr_cards.empty:
                st.error(
                    "No Pokémon card rows were found in the Collectr file. "
                    "Check that the export includes a Category column with Pokemon."
                )
            else:
                matched, new_cards, missing_inventory = _reconcile_collectr(
                    collectr_cards,
                    eligible_inventory,
                )

                new_form = _build_new_purchase_form(
                    new_cards, selected_show, batch_id
                )
                sales_form = _build_missing_sales_form(
                    missing_inventory, selected_show, batch_id
                )

                st.success(
                    f"Reconciliation complete. Batch ID: {batch_id}", icon="✅"
                )

                metrics = st.columns(6)
                metrics[0].metric(
                    "Collectr source rows", f"{collectr_stats['source_rows']:,}"
                )
                metrics[1].metric(
                    "Collectr card copies", f"{collectr_stats['individual_cards']:,}"
                )
                metrics[2].metric("Matched", f"{len(matched):,}")
                metrics[3].metric("Possible new buys", f"{len(new_cards):,}")
                metrics[4].metric(
                    "ACTIVE cards missing", f"{len(missing_inventory):,}"
                )
                metrics[5].metric(
                    "Non-Pokémon upload rows ignored",
                    f"{collectr_stats['non_pokemon_rows_ignored']:,}",
                )

                st.warning(
                    "Missing means absent from this Collectr snapshot—not automatically "
                    "sold. Review and complete the sales form before uploading it. "
                    "Consignment, personal, sports/non-Pokémon, sealed, and GRADING "
                    "inventory were excluded before this list was created.",
                    icon="⚠️",
                )

                result_tabs = st.tabs(
                    [
                        f"Matched ({len(matched):,})",
                        f"New in Collectr ({len(new_cards):,})",
                        f"Missing from Collectr ({len(missing_inventory):,})",
                    ]
                )

                with result_tabs[0]:
                    if matched.empty:
                        st.info("No matches were found.")
                    else:
                        display = matched[
                            [
                                col
                                for col in MATCH_DISPLAY_COLUMNS
                                if col in matched.columns
                            ]
                        ].copy()
                        st.dataframe(
                            display,
                            use_container_width=True,
                            hide_index=True,
                        )

                with result_tabs[1]:
                    if new_cards.empty:
                        st.success(
                            "Every Collectr card matched an eligible ACTIVE inventory row."
                        )
                    else:
                        new_display_cols = [
                            "collectr_row_id",
                            "set_name",
                            "card_name",
                            "card_number",
                            "variant",
                            "grading_company",
                            "grade",
                            "condition",
                            "market_value",
                        ]
                        st.dataframe(
                            new_cards[
                                [
                                    col
                                    for col in new_display_cols
                                    if col in new_cards.columns
                                ]
                            ],
                            use_container_width=True,
                            hide_index=True,
                        )
                        st.download_button(
                            "Download new-purchase form",
                            data=_csv_download_data(new_form),
                            file_name=(
                                f"{_compact(selected_show.get('show_name')) or 'show'}_"
                                f"{batch_id}_new_inventory.csv"
                            ),
                            mime="text/csv",
                            use_container_width=True,
                        )
                        st.caption(
                            "Fill in purchase price and source. Change process_row to NO "
                            "for anything that should not be added. Then upload the form in "
                            "Add New Inventory."
                        )

                with result_tabs[2]:
                    if missing_inventory.empty:
                        st.success(
                            "No eligible ACTIVE Pokémon cards are missing from Collectr."
                        )
                    else:
                        missing_display_cols = [
                            "inventory_id",
                            "set_name",
                            "card_name",
                            "card_number",
                            "variant",
                            "grading_company",
                            "grade",
                            "condition",
                            "total_cost",
                            "market_value",
                            "sticker_price",
                        ]
                        st.dataframe(
                            missing_inventory[
                                [
                                    col
                                    for col in missing_display_cols
                                    if col in missing_inventory.columns
                                ]
                            ],
                            use_container_width=True,
                            hide_index=True,
                        )
                        st.download_button(
                            "Download missing-card sales form",
                            data=_csv_download_data(sales_form),
                            file_name=(
                                f"{_compact(selected_show.get('show_name')) or 'show'}_"
                                f"{batch_id}_missing_sales.csv"
                            ),
                            mime="text/csv",
                            use_container_width=True,
                        )
                        st.caption(
                            "Fill in sold price. Change process_row to NO for any card that "
                            "was not sold. Then upload the form in Update Missing Sales."
                        )

        except Exception as exc:
            st.exception(exc)


# =========================================================
# Add New Inventory
# =========================================================

with tab_add:
    st.subheader("Add New Inventory from Completed Form")
    st.write(
        "Upload the completed new-purchase form downloaded from the reconciliation. "
        "Each processed row becomes one ACTIVE Pokémon inventory record."
    )
    st.info(
        "This processor will not add consignment, personal, or non-Pokémon inventory. "
        "Inventory IDs already present in the database are blocked, which protects "
        "against uploading the same completed form twice.",
        icon="ℹ️",
    )

    purchase_upload_file = st.file_uploader(
        "Upload completed new-purchase form",
        type=["csv", "xlsx", "xls"],
        key="completed_new_purchase_upload",
    )

    if purchase_upload_file is not None:
        try:
            purchase_upload = _read_csv_or_excel(purchase_upload_file)
            current_inventory = _safe_df(load_data(force_refresh=True).inventory)
            valid_purchases, purchase_validation = _validate_purchase_upload(
                purchase_upload,
                current_inventory,
            )

            valid_count = int(purchase_validation["status"].eq("VALID").sum())
            error_count = int(purchase_validation["status"].eq("ERROR").sum())

            validation_metrics = st.columns(3)
            validation_metrics[0].metric(
                "Rows marked for processing", f"{len(purchase_validation):,}"
            )
            validation_metrics[1].metric("Valid", f"{valid_count:,}")
            validation_metrics[2].metric("Errors", f"{error_count:,}")

            if not purchase_validation.empty:
                st.dataframe(
                    purchase_validation.drop(columns=["__index"], errors="ignore"),
                    use_container_width=True,
                    hide_index=True,
                )

            if not valid_purchases.empty:
                preview = valid_purchases.copy()
                preview["calculated_total_cost"] = preview.apply(
                    lambda row: round(
                        to_money(row.get("purchase_price"))
                        + to_money(row.get("shipping"))
                        + to_money(row.get("tax"))
                        + to_money(row.get("grading_fee")),
                        2,
                    ),
                    axis=1,
                )
                preview_cols = [
                    "inventory_id",
                    "set_name",
                    "card_name",
                    "card_number",
                    "purchase_date",
                    "purchased_from",
                    "purchase_price",
                    "calculated_total_cost",
                    "market_value",
                ]
                st.markdown("### Valid rows to add")
                st.dataframe(
                    preview[
                        [col for col in preview_cols if col in preview.columns]
                    ],
                    use_container_width=True,
                    hide_index=True,
                )

                confirm_add = st.checkbox(
                    f"I confirm that I want to add {len(valid_purchases):,} new "
                    "ACTIVE inventory record(s).",
                    key="confirm_add_reconciled_inventory",
                )

                if st.button(
                    "Add valid rows to inventory",
                    type="primary",
                    disabled=not confirm_add,
                    key="process_reconciled_purchases",
                ):
                    # Revalidate immediately before writing.
                    latest_inventory = _safe_df(
                        load_data(force_refresh=True).inventory
                    )
                    final_valid, final_validation = _validate_purchase_upload(
                        valid_purchases,
                        latest_inventory,
                    )
                    final_errors = int(
                        final_validation["status"].eq("ERROR").sum()
                    )

                    if final_errors:
                        st.error(
                            "Inventory changed after the preview. Refresh and review the "
                            "new validation results before trying again."
                        )
                        st.dataframe(
                            final_validation.drop(
                                columns=["__index"], errors="ignore"
                            ),
                            use_container_width=True,
                            hide_index=True,
                        )
                    else:
                        rows_to_add = [
                            _inventory_row_from_purchase_upload(row)
                            for _, row in final_valid.iterrows()
                        ]
                        append_rows(
                            get_ws_name("inventory_worksheet", "inventory"),
                            INVENTORY_COLUMNS,
                            rows_to_add,
                        )
                        refresh_database_cache()
                        st.success(
                            f"Added {len(rows_to_add):,} ACTIVE Pokémon inventory "
                            "record(s)."
                        )
                        st.rerun()
            elif not purchase_validation.empty:
                st.warning("There are no valid rows available to add.")

        except Exception as exc:
            st.exception(exc)


# =========================================================
# Update Missing Sales
# =========================================================

with tab_sales:
    st.subheader("Update Missing Cards as Show Sales")
    st.write(
        "Upload the completed missing-card sales form. Only rows still qualifying "
        "as ACTIVE, business-owned Pokémon single cards can be marked SOLD."
    )
    st.warning(
        "Set process_row to NO for anything that is still owned, was omitted from "
        "Collectr, or should not be treated as a sale. Cards already in GRADING, "
        "consignment, personal, or sports/non-Pokémon inventory will fail validation.",
        icon="⚠️",
    )

    sales_upload_file = st.file_uploader(
        "Upload completed missing-card sales form",
        type=["csv", "xlsx", "xls"],
        key="completed_missing_sales_upload",
    )

    if sales_upload_file is not None:
        try:
            sales_upload = _read_csv_or_excel(sales_upload_file)
            current_inventory = _safe_df(load_data(force_refresh=True).inventory)
            valid_sales, sales_validation = _validate_sales_upload(
                sales_upload,
                current_inventory,
            )

            valid_count = int(sales_validation["status"].eq("VALID").sum())
            error_count = int(sales_validation["status"].eq("ERROR").sum())

            validation_metrics = st.columns(3)
            validation_metrics[0].metric(
                "Rows marked for processing", f"{len(sales_validation):,}"
            )
            validation_metrics[1].metric("Valid", f"{valid_count:,}")
            validation_metrics[2].metric("Errors", f"{error_count:,}")

            if not sales_validation.empty:
                st.dataframe(
                    sales_validation.drop(columns=["__index"], errors="ignore"),
                    use_container_width=True,
                    hide_index=True,
                )

            if not valid_sales.empty:
                current_by_id = {
                    _text(row.get("inventory_id")): row
                    for _, row in current_inventory.iterrows()
                    if _text(row.get("inventory_id"))
                }

                preview_rows: list[dict[str, Any]] = []
                for _, row in valid_sales.iterrows():
                    inventory_id = _text(row.get("inventory_id"))
                    current_row = current_by_id[inventory_id]
                    sold_price = round(to_money(row.get("sold_price")), 2)
                    fees = round(to_money(row.get("fees")), 2)
                    cost = round(to_money(current_row.get("total_cost")), 2)
                    net = round(sold_price - fees, 2)
                    profit = round(net - cost, 2)
                    preview_rows.append(
                        {
                            "inventory_id": inventory_id,
                            "card_name": _text(current_row.get("card_name")),
                            "sold_date": _text(row.get("sold_date")),
                            "sold_price": sold_price,
                            "fees": fees,
                            "total_cost": cost,
                            "net_proceeds": net,
                            "profit": profit,
                            "show_name": _text(row.get("show_name")),
                        }
                    )

                preview = pd.DataFrame(preview_rows)
                st.markdown("### Valid sales to process")
                st.dataframe(
                    preview,
                    use_container_width=True,
                    hide_index=True,
                    column_config={
                        "sold_price": st.column_config.NumberColumn(
                            "Sold price", format="$%.2f"
                        ),
                        "fees": st.column_config.NumberColumn(
                            "Fees", format="$%.2f"
                        ),
                        "total_cost": st.column_config.NumberColumn(
                            "Total cost", format="$%.2f"
                        ),
                        "net_proceeds": st.column_config.NumberColumn(
                            "Net proceeds", format="$%.2f"
                        ),
                        "profit": st.column_config.NumberColumn(
                            "Profit", format="$%.2f"
                        ),
                    },
                )

                totals = st.columns(4)
                totals[0].metric("Items", f"{len(preview):,}")
                totals[1].metric(
                    "Sales", money_fmt(preview["sold_price"].sum())
                )
                totals[2].metric(
                    "Net", money_fmt(preview["net_proceeds"].sum())
                )
                totals[3].metric("Profit", money_fmt(preview["profit"].sum()))

                confirm_sales = st.checkbox(
                    f"I confirm that I want to mark {len(valid_sales):,} ACTIVE "
                    "inventory record(s) SOLD.",
                    key="confirm_reconciled_sales",
                )

                if st.button(
                    "Process valid show sales",
                    type="primary",
                    disabled=not confirm_sales,
                    key="process_reconciled_sales",
                ):
                    # Revalidate against fresh data immediately before changing statuses.
                    latest_inventory = _safe_df(
                        load_data(force_refresh=True).inventory
                    )
                    final_valid, final_validation = _validate_sales_upload(
                        valid_sales,
                        latest_inventory,
                    )
                    final_errors = int(
                        final_validation["status"].eq("ERROR").sum()
                    )

                    if final_errors:
                        st.error(
                            "Inventory changed after the preview. Refresh and review the "
                            "new validation results before trying again."
                        )
                        st.dataframe(
                            final_validation.drop(
                                columns=["__index"], errors="ignore"
                            ),
                            use_container_width=True,
                            hide_index=True,
                        )
                    else:
                        latest_by_id = {
                            _text(row.get("inventory_id")): row
                            for _, row in latest_inventory.iterrows()
                            if _text(row.get("inventory_id"))
                        }
                        changed = 0
                        shared_batch_id = (
                            _text(final_valid.iloc[0].get("reconciliation_batch_id"))
                            if not final_valid.empty
                            else f"REC-{uuid.uuid4().hex[:10].upper()}"
                        )
                        if not shared_batch_id:
                            shared_batch_id = f"REC-{uuid.uuid4().hex[:10].upper()}"

                        for _, row in final_valid.iterrows():
                            inventory_id = _text(row.get("inventory_id"))
                            current_row = latest_by_id[inventory_id]
                            sold_price = round(to_money(row.get("sold_price")), 2)
                            fees = round(to_money(row.get("fees")), 2)
                            total_cost = round(
                                to_money(current_row.get("total_cost")), 2
                            )
                            net = round(sold_price - fees, 2)
                            profit = round(net - total_cost, 2)
                            sold_date_value = pd.to_datetime(
                                row.get("sold_date"), errors="coerce"
                            ).date()

                            updates = {
                                "transaction_type": "Card Show",
                                "platform": "",
                                "sold_date": str(sold_date_value),
                                "sold_price": sold_price,
                                "fees": fees,
                                "fees_total": fees,
                                "shipping_charged": 0,
                                "net_proceeds": net,
                                "profit": profit,
                                "sale_channel": "Card Show",
                                "sale_notes": _text(row.get("sale_notes")),
                                "show_id": _text(row.get("show_id")),
                                "show_name": _text(row.get("show_name")),
                                "sold_transaction_id": shared_batch_id,
                                "reconciliation_batch_id": shared_batch_id,
                                "sold_created_at": now_iso(),
                                "sold_updated_at": now_iso(),
                            }
                            changed += mark_inventory_sold(inventory_id, updates)

                        refresh_database_cache()
                        st.success(f"Recorded {changed:,} show sale(s).")
                        st.rerun()
            elif not sales_validation.empty:
                st.warning("There are no valid rows available to process.")

        except Exception as exc:
            st.exception(exc)


# =========================================================
# Show Summary
# =========================================================

with tab_summary:
    st.subheader("Show Summary")

    if inv.empty:
        st.info("No inventory loaded.")
    else:
        sale_channel = _series(inv, "sale_channel").astype(str)
        sold = inv[
            _series(inv, "inventory_status").astype(str).str.upper().eq(STATUS_SOLD)
            & sale_channel.str.lower().str.contains("card show", na=False)
        ].copy()

        if sold.empty:
            st.info("No show sales recorded yet.")
        else:
            for money_col in [
                "sold_price",
                "fees_total",
                "net_proceeds",
                "total_cost",
                "profit",
            ]:
                sold[money_col] = _series(sold, money_col).apply(to_money)

            sold["sold_dt"] = pd.to_datetime(
                _series(sold, "sold_date"), errors="coerce"
            )

            metrics = st.columns(5)
            metrics[0].metric("Show items sold", f"{len(sold):,}")
            metrics[1].metric("Show sales", money_fmt(sold["sold_price"].sum()))
            metrics[2].metric("Show fees", money_fmt(sold["fees_total"].sum()))
            metrics[3].metric("Show net", money_fmt(sold["net_proceeds"].sum()))
            metrics[4].metric("Show profit", money_fmt(sold["profit"].sum()))

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
                summary,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "sales": st.column_config.NumberColumn(
                        "Sales", format="$%.2f"
                    ),
                    "fees": st.column_config.NumberColumn("Fees", format="$%.2f"),
                    "net": st.column_config.NumberColumn("Net", format="$%.2f"),
                    "cost": st.column_config.NumberColumn("Cost", format="$%.2f"),
                    "profit": st.column_config.NumberColumn(
                        "Profit", format="$%.2f"
                    ),
                },
            )

            st.markdown("### Sale detail")
            detail_cols = [
                "sold_date",
                "show_name",
                "inventory_id",
                "set_name",
                "card_name",
                "card_number",
                "variant",
                "grading_company",
                "grade",
                "sold_price",
                "fees_total",
                "net_proceeds",
                "total_cost",
                "profit",
                "sale_notes",
                "sold_transaction_id",
            ]
            detail = _date_sort(sold, "sold_date", ascending=False)
            detail = detail[
                [col for col in detail_cols if col in detail.columns]
            ]
            st.dataframe(detail, use_container_width=True, hide_index=True)
            st.download_button(
                "Download show sale detail CSV",
                data=_csv_download_data(detail),
                file_name="show_sales_detail.csv",
                mime="text/csv",
            )
