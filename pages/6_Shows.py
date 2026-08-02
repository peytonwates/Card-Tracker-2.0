from __future__ import annotations

import hashlib
import io
import re
import unicodedata
import uuid
from datetime import date
from difflib import SequenceMatcher
from typing import Any
from urllib.parse import unquote, urlparse

import pandas as pd
import streamlit as st

from core.business import load_data, refresh_database_cache
from core.cleaning import clean_text, money_fmt, now_iso, to_money
from core.config import (
    INVENTORY_COLUMNS,
    SHOW_COLUMNS,
    STATUS_ACTIVE,
    STATUS_SOLD,
)
from core.sheets import append_rows, get_ws_name, update_rows_by_key


st.set_page_config(page_title="Shows", layout="wide")
st.title("Shows")
st.caption(
    "Reconcile a Collectr snapshot against ACTIVE and LISTED business-owned Pokémon "
    "card inventory, manually review matches, then process purchases and sales."
)
st.caption(
    "Shows page build: 2026-07-20 · Collectr reconciliation matching v5 · "
    "Whole-word sealed detection and interactive one-to-one match review"
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

AUTO_MATCH_THRESHOLD_DEFAULT = 80.0
REVIEW_MATCH_THRESHOLD = 60.0

SEALED_COLLECTR_TERMS = {
    "booster box",
    "booster bundle",
    "booster pack",
    "elite trainer box",
    "etb",
    "collection box",
    "trainer toolkit",
    "theme deck",
    "battle deck",
    "tin",
}

MATCH_AUDIT_COLUMNS = [
    "match_status",
    "match_confidence",
    "match_score",
    "match_method",
    "score_details",
    "name_score_pct",
    "number_score_pct",
    "set_score_pct",
    "grade_score_pct",
    "condition_score_pct",
    "next_best_score",
    "score_gap",
    "candidate_count",
    "collectr_row_id",
    "collectr_source_row",
    "collectr_source_copy",
    "collectr_source_quantity",
    "collectr_set_name",
    "collectr_card_name",
    "collectr_card_number",
    "collectr_rarity",
    "collectr_variant",
    "collectr_condition",
    "collectr_grading_company",
    "collectr_grade",
    "collectr_market_value",
    "inventory_id",
    "inventory_status",
    "inventory_set_name",
    "inventory_card_name",
    "inventory_card_number",
    "inventory_reference_link",
    "inventory_reference_set",
    "inventory_reference_name",
    "inventory_reference_number",
    "inventory_variant",
    "inventory_card_subtype",
    "inventory_condition",
    "inventory_grading_company",
    "inventory_grade",
    "inventory_purchase_date",
    "inventory_purchased_from",
    "inventory_total_cost",
    "inventory_market_value",
    "inventory_sticker_price",
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
    "card_type",
    "set_name",
    "year",
    "card_name",
    "card_number",
    "variant",
    "card_subtype",
    "grading_company",
    "grade",
    "condition",
    "reference_link",
    "market_value",
    "sticker_price",
    "notes",
    "suggested_inventory_id",
    "suggested_inventory_reference_link",
    "suggested_match_score",
    "suggested_match_status",
    "reconciliation_batch_id",
    "collectr_row_id",
]

MISSING_SALE_COLUMNS = [
    "process_row",
    "inventory_id",
    "sold_date",
    "sold_price",
    "fees",
    "transaction_type",
    "sale_channel",
    "platform",
    "sale_notes",
    "show_id",
    "show_name",
    "inventory_status",
    "set_name",
    "card_name",
    "card_number",
    "reference_link",
    "variant",
    "grading_company",
    "grade",
    "condition",
    "total_cost",
    "market_value",
    "sticker_price",
    "best_collectr_row_id",
    "best_collectr_card_name",
    "best_collectr_card_number",
    "best_collectr_set_name",
    "best_possible_match_score",
    "best_possible_match_status",
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
    text = re.sub(r"\b(alternate full art|full art|illustration rare|special illustration rare|secret rare)\b", "", text)
    return " ".join(text.split())


def _normalized_set(value: Any) -> str:
    text = _norm(value)
    text = re.sub(r"\bpokemon\b", " ", text)
    text = re.sub(r"\bjapanese\b", " ", text)
    text = re.sub(r"\btcg\b", " ", text)
    text = " ".join(text.split()).replace("promos", "promo")

    replacements = {
        "sv 151": "151",
        "scarlet violet 151": "151",
        "scarlet and violet 151": "151",
        "scarlet violet base set": "scarlet violet",
        "scarlet violet base": "scarlet violet",
        "scarlet and violet base set": "scarlet violet",
        "scarlet and violet base": "scarlet violet",
        "crown zenith galarian gallery": "crown zenith",
        "brilliant stars trainer gallery": "brilliant stars",
        "silver tempest trainer gallery": "silver tempest",
        "astral radiance trainer gallery": "astral radiance",
        "generations radiant collection": "generations",
        "paldea fates": "paldean fates",
        "ascended heros": "ascended heroes",
        "mega evolutions": "mega evolution",
        "mega evoltuon": "mega evolution",
        "mega evolutions promo": "mega evolution promo",
        "mega evolution promos": "mega evolution promo",
        "mega evolutions promos": "mega evolution promo",
        "scarlet violet promo": "promo",
        "scarlet and violet promo": "promo",
        "wotc promo": "promo",
        "xy promo": "promo",
        "xy base set": "xy",
        "world championships 2007": "world championship 2007",
        "base set unlimited": "base set",
    }
    return replacements.get(text, text)


def _normalized_card_number(value: Any) -> str:
    """Normalize the numerator while preserving prefixes such as TG, GG, RC, and SWSH.

    Collectr exports numbers such as 182/167. The former implementation normalized
    punctuation before splitting, which converted that value into 182167 and caused
    most otherwise-obvious matches to fail.
    """
    raw = _text(value)
    raw = unicodedata.normalize("NFKD", raw)
    raw = "".join(ch for ch in raw if not unicodedata.combining(ch))
    raw = raw.upper().replace("#", "")
    raw = re.sub(r"[^A-Z0-9/]+", "", raw)
    if not raw:
        return ""

    numerator = raw.split("/", 1)[0]
    match = re.fullmatch(r"([A-Z]*)(\d+)([A-Z]*)", numerator)
    if not match:
        return numerator

    prefix, digits, suffix = match.groups()
    return f"{prefix}{int(digits)}{suffix}"


def _card_number_parts(value: Any) -> tuple[str, str, str]:
    normalized = _normalized_card_number(value)
    if not normalized:
        return "", "", ""
    match = re.fullmatch(r"([A-Z]*)(\d+)([A-Z]*)", normalized)
    if not match:
        return "", normalized, ""
    prefix, digits, suffix = match.groups()
    return prefix, str(int(digits)), suffix


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
            "purchased_from",
            "notes",
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
    return any(
        _contains_whole_term(product_text, term)
        for term in sealed_terms
    )


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
    eligible_statuses = {_norm(STATUS_ACTIVE), "listed"}
    if status not in eligible_statuses:
        if "grad" in status:
            return "GRADING / excluded"
        return "Not ACTIVE or LISTED"

    if _is_explicit_consignment(row):
        return "Consignment"

    if _is_explicit_personal(row):
        return "Personal inventory"

    if _is_sealed_or_non_card(row):
        return "Sealed / non-card inventory"

    if not _is_pokemon_inventory(row):
        return "Sports / non-Pokémon"

    return "Eligible ACTIVE/LISTED owned Pokémon card"


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
        inv["__scope_reason"].eq("Eligible ACTIVE/LISTED owned Pokémon card")
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

    # Collectr commonly exports values such as "PSA 10.0 GEM - MT".
    match = re.search(
        r"\b(psa|bgs|cgc|sgc|ace)\b.*?(\d+(?:\.\d+)?)",
        normalized,
    )
    if match:
        return match.group(1).upper(), f"{float(match.group(2)):g}"

    numeric = pd.to_numeric(text, errors="coerce")
    if pd.notna(numeric):
        return "", f"{float(numeric):g}"

    return "", text


def _contains_whole_term(value: Any, term: str) -> bool:
    """
    Match a normalized word or phrase without treating a partial card-name
    substring as a product type.

    Example:
      - "Pokemon Tin" matches the sealed term "tin".
      - "Tinkatink" does NOT match the sealed term "tin".
    """
    normalized_value = _norm(value)
    normalized_term = _norm(term)
    if not normalized_value or not normalized_term:
        return False

    pattern = rf"(?<![a-z0-9]){re.escape(normalized_term)}(?![a-z0-9])"
    return re.search(pattern, normalized_value) is not None


def _collectr_is_sealed_or_non_card(row: pd.Series) -> bool:
    product_name = _norm(row.get("card_name"))
    rarity = _norm(row.get("rarity"))

    if any(
        _contains_whole_term(product_name, term)
        for term in SEALED_COLLECTR_TERMS
    ):
        return True

    if _contains_whole_term(rarity, "sealed"):
        return True

    return False


def _parse_collectr(uploaded_file) -> tuple[pd.DataFrame, dict[str, int]]:
    raw = _read_csv_or_excel(uploaded_file)
    raw.columns = [_text(col) for col in raw.columns]

    canonical = pd.DataFrame(index=raw.index)
    for target, aliases in COLLECTR_ALIASES.items():
        source = _find_collectr_source_column(raw.columns.tolist(), aliases)
        canonical[target] = raw[source] if source else ""
    # Excel/CSV row number including the header row, useful when reviewing the audit.
    canonical["__source_row_number"] = range(2, len(canonical) + 2)

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

    sealed_mask = canonical.apply(_collectr_is_sealed_or_non_card, axis=1)
    stats["sealed_rows_ignored"] = int(sealed_mask.sum())
    canonical = canonical[~sealed_mask].copy()

    expanded_rows: list[dict[str, Any]] = []
    for _, row in canonical.iterrows():
        source_position = int(row.get("__source_row_number", 0) or 0)
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
                    "source_row_number": source_position,
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


def _grade_signature(row: pd.Series, *, collectr_row: bool = False) -> str:
    company = _norm(row.get("grading_company"))
    grade = _normalized_grade(row.get("grade"))

    if not grade:
        if not collectr_row:
            type_text = " ".join(
                _norm(row.get(col))
                for col in ["product_type", "card_type", "condition"]
            )
            if "graded" in type_text:
                return "graded:unknown"
        return "raw"

    match = re.search(
        r"\b(psa|bgs|cgc|sgc|ace)\b.*?(\d+(?:\.\d+)?)",
        f"{company} {grade}",
    )
    if match:
        return f"{match.group(1)}:{float(match.group(2)):g}"
    return f"{company}:{grade}" if company else grade


def _reference_parts(value: Any) -> dict[str, str]:
    link = _text(value)
    if not link:
        return {"set": "", "name": "", "number": ""}

    try:
        path = unquote(urlparse(link).path)
        parts = [part for part in path.split("/") if part]
        if len(parts) < 2:
            return {"set": "", "name": "", "number": ""}

        reference_set = _normalized_set(parts[-2].replace("-", " "))
        item_slug = parts[-1].split("?", 1)[0]
        tokens = item_slug.split("-")
        reference_number = ""
        if tokens and re.fullmatch(r"[A-Za-z]*\d+[A-Za-z]*", tokens[-1]):
            reference_number = _normalized_card_number(tokens[-1])
            tokens = tokens[:-1]

        reference_name = _norm(" ".join(tokens))
        return {
            "set": reference_set,
            "name": reference_name,
            "number": reference_number,
        }
    except Exception:
        return {"set": "", "name": "", "number": ""}


def _dedupe_text(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _name_candidates(row: pd.Series, *, include_reference: bool) -> dict[str, list[str]]:
    direct = _text(row.get("card_name"))
    number_candidates = {
        _normalized_card_number(row.get("card_number")),
    }
    reference = _reference_parts(row.get("reference_link")) if include_reference else {
        "name": "",
        "number": "",
        "set": "",
    }
    if reference["number"]:
        number_candidates.add(reference["number"])

    descriptor_terms = {
        "secret",
        "full",
        "art",
        "alternate",
        "illustration",
        "special",
        "rare",
        "holo",
        "holofoil",
        "foil",
        "stamped",
        "metal",
        "card",
        "radiant",
    }

    def clean_candidate(value: str) -> str:
        normalized = _norm(value)
        tokens = normalized.split()
        number_digit_cores = {
            _card_number_parts(number)[1]
            for number in number_candidates
            if _card_number_parts(number)[1]
        }
        while tokens:
            token_number = _normalized_card_number(tokens[-1])
            _, token_digits, _ = _card_number_parts(token_number)
            if token_number and (
                token_number in number_candidates
                or (token_digits and token_digits in number_digit_cores)
            ):
                tokens.pop()
            else:
                break
        return " ".join(tokens)

    direct_candidates = [clean_candidate(direct)]
    without_parenthetical = re.sub(r"[\(\[].*?[\)\]]", " ", direct)
    direct_candidates.append(clean_candidate(without_parenthetical))

    for candidate in list(direct_candidates):
        direct_candidates.append(
            " ".join(
                token for token in candidate.split() if token not in descriptor_terms
            )
        )

    variant = _norm(row.get("variant"))
    if variant in {"ex", "v", "vmax", "vstar", "gx", "lv x", "lvx"}:
        for candidate in list(direct_candidates):
            if variant not in candidate.split():
                direct_candidates.append(f"{candidate} {variant}".strip())

    reference_candidates: list[str] = []
    if reference["name"]:
        reference_candidates.append(clean_candidate(reference["name"]))
        reference_candidates.append(
            " ".join(
                token
                for token in reference["name"].split()
                if token not in descriptor_terms
            )
        )

    return {
        "direct": _dedupe_text(direct_candidates),
        "reference": _dedupe_text(reference_candidates),
        "all": _dedupe_text(direct_candidates + reference_candidates),
    }


def _set_candidates(row: pd.Series, *, include_reference: bool) -> dict[str, list[str]]:
    direct = _normalized_set(row.get("set_name"))
    direct_candidates = [direct] if direct else []

    raw_set = _norm(row.get("set_name"))
    for suffix in [" galarian gallery", " trainer gallery", " radiant collection"]:
        if raw_set.endswith(suffix):
            direct_candidates.append(_normalized_set(raw_set[: -len(suffix)]))

    if "promo" in direct:
        direct_candidates.append("promo")

    reference = _reference_parts(row.get("reference_link")) if include_reference else {
        "set": ""
    }
    reference_candidates = [reference["set"]] if reference.get("set") else []
    if reference.get("set") and "promo" in reference["set"]:
        reference_candidates.append("promo")

    return {
        "direct": _dedupe_text(direct_candidates),
        "reference": _dedupe_text(reference_candidates),
        "all": _dedupe_text(direct_candidates + reference_candidates),
    }


def _number_candidates(row: pd.Series, *, include_reference: bool) -> dict[str, list[str]]:
    direct_candidates: list[str] = []
    direct = _normalized_card_number(row.get("card_number"))
    if direct:
        direct_candidates.append(direct)

    # Older inventory rows sometimes embedded the number in card_name.
    if include_reference:
        for token in re.findall(r"\b[A-Za-z]*\d+[A-Za-z]*\b", _text(row.get("card_name"))):
            normalized = _normalized_card_number(token)
            if normalized:
                direct_candidates.append(normalized)

    reference = _reference_parts(row.get("reference_link")) if include_reference else {
        "number": ""
    }
    reference_candidates = [reference["number"]] if reference.get("number") else []

    return {
        "direct": _dedupe_text(direct_candidates),
        "reference": _dedupe_text(reference_candidates),
        "all": _dedupe_text(direct_candidates + reference_candidates),
    }


def _string_similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    left_sorted = " ".join(sorted(left.split()))
    right_sorted = " ".join(sorted(right.split()))
    return max(
        SequenceMatcher(None, left, right).ratio(),
        SequenceMatcher(None, left_sorted, right_sorted).ratio(),
    )


def _best_similarity(left_values: list[str], right_values: list[str]) -> float:
    return max(
        (
            _string_similarity(left, right)
            for left in left_values
            for right in right_values
        ),
        default=0.0,
    )


def _number_match(
    collectr_numbers: list[str],
    inventory_numbers: list[str],
) -> tuple[float, str, bool]:
    if not collectr_numbers and not inventory_numbers:
        return 0.50, "both card numbers unavailable", True
    if not collectr_numbers or not inventory_numbers:
        return 0.40, "card number unavailable on one side", True
    if set(collectr_numbers).intersection(inventory_numbers):
        return 1.00, "exact card number", True

    for collectr_number in collectr_numbers:
        collectr_prefix, collectr_digits, collectr_suffix = _card_number_parts(
            collectr_number
        )
        for inventory_number in inventory_numbers:
            inventory_prefix, inventory_digits, inventory_suffix = _card_number_parts(
                inventory_number
            )
            if (
                collectr_digits
                and collectr_digits == inventory_digits
                and collectr_suffix == inventory_suffix
                and (not collectr_prefix or not inventory_prefix)
            ):
                return 0.92, "numeric card number match; prefix omitted", True

    return 0.0, "card number conflict", False


def _score_candidate_pair(
    collectr_row: pd.Series,
    inventory_row: pd.Series,
) -> dict[str, Any] | None:
    collectr_grade = _grade_signature(collectr_row, collectr_row=True)
    inventory_grade = _grade_signature(inventory_row, collectr_row=False)
    if collectr_grade != inventory_grade:
        return None

    collectr_names = _name_candidates(collectr_row, include_reference=False)
    inventory_names = _name_candidates(inventory_row, include_reference=True)
    name_score = _best_similarity(collectr_names["all"], inventory_names["all"])
    if name_score < 0.55:
        return None

    collectr_numbers = _number_candidates(collectr_row, include_reference=False)
    inventory_numbers = _number_candidates(inventory_row, include_reference=True)
    number_score, number_reason, number_compatible = _number_match(
        collectr_numbers["all"], inventory_numbers["all"]
    )
    if not number_compatible:
        return None

    collectr_sets = _set_candidates(collectr_row, include_reference=False)
    inventory_sets = _set_candidates(inventory_row, include_reference=True)
    if collectr_sets["all"] and inventory_sets["all"]:
        set_score = _best_similarity(collectr_sets["all"], inventory_sets["all"])
    elif collectr_sets["all"] or inventory_sets["all"]:
        set_score = 0.40
    else:
        set_score = 0.50

    collectr_condition = _normalized_condition(collectr_row.get("condition"))
    inventory_condition = _normalized_condition(inventory_row.get("condition"))
    if inventory_condition == "graded":
        condition_score = 0.50
    elif collectr_condition and inventory_condition:
        condition_score = 1.0 if collectr_condition == inventory_condition else 0.25
    else:
        condition_score = 0.50

    grade_score = 1.0
    score = round(
        40.0 * name_score
        + 30.0 * number_score
        + 20.0 * set_score
        + 8.0 * grade_score
        + 2.0 * condition_score,
        1,
    )

    reference_name_score = _best_similarity(
        collectr_names["all"], inventory_names["reference"]
    )
    direct_name_score = _best_similarity(
        collectr_names["all"], inventory_names["direct"]
    )
    reference_set_score = _best_similarity(
        collectr_sets["all"], inventory_sets["reference"]
    )
    direct_set_score = _best_similarity(
        collectr_sets["all"], inventory_sets["direct"]
    )
    reference_number_used = bool(
        set(collectr_numbers["all"]).intersection(inventory_numbers["reference"])
    )
    reference_used = (
        reference_name_score > direct_name_score + 0.001
        or reference_set_score > direct_set_score + 0.001
        or reference_number_used
    )

    exact_name = name_score >= 0.999
    exact_set = set_score >= 0.999
    exact_number = number_score >= 0.999

    if exact_name and exact_set and exact_number:
        method = "Exact identity"
    elif reference_used and exact_number:
        method = "Reference-link identity"
    elif exact_number and exact_set:
        method = "Number/set + name similarity"
    elif exact_number and exact_name:
        method = "Name/number + set similarity"
    elif number_score < 0.60:
        method = "Name/set match; number unavailable"
    else:
        method = "Weighted identity match"

    score_details = (
        f"Name {name_score * 100:.0f}% | Number {number_score * 100:.0f}% "
        f"({number_reason}) | Set {set_score * 100:.0f}% | "
        f"Grade {grade_score * 100:.0f}% | Condition {condition_score * 100:.0f}%"
    )
    if reference_used:
        score_details += " | Inventory reference link improved the match"

    return {
        "score": score,
        "match_method": method,
        "score_details": score_details,
        "name_score": name_score,
        "number_score": number_score,
        "set_score": set_score,
        "grade_score": grade_score,
        "condition_score": condition_score,
        "exact_name": exact_name,
        "exact_number": exact_number,
        "exact_set": exact_set,
        "reference_used": reference_used,
    }


def _confidence_label(score: float, matched: bool) -> str:
    if not matched:
        if score >= AUTO_MATCH_THRESHOLD_DEFAULT:
            return "Strong candidate but unavailable"
        if score >= REVIEW_MATCH_THRESHOLD:
            return "Review"
        return "No reliable candidate"
    if score >= 95:
        return "Excellent"
    if score >= 88:
        return "High"
    if score >= 80:
        return "Good"
    return "Review"


def _inventory_purchase_rank(row: pd.Series) -> int:
    parsed = pd.to_datetime(row.get("purchase_date"), errors="coerce")
    return int(parsed.value) if pd.notna(parsed) else -1


def _audit_row(
    collectr_row: pd.Series,
    inventory_row: pd.Series | None,
    candidate: dict[str, Any] | None,
    *,
    matched: bool,
    match_status: str,
    next_best_score: float | None,
    candidate_count: int,
) -> dict[str, Any]:
    score = float(candidate.get("score", 0.0)) if candidate else 0.0
    reference = (
        _reference_parts(inventory_row.get("reference_link"))
        if inventory_row is not None
        else {"set": "", "name": "", "number": ""}
    )
    gap = (
        round(score - next_best_score, 1)
        if next_best_score is not None
        else ""
    )

    return {
        "match_status": match_status,
        "match_confidence": _confidence_label(score, matched),
        "match_score": score,
        "match_method": candidate.get("match_method", "") if candidate else "",
        "score_details": candidate.get("score_details", "") if candidate else "",
        "name_score_pct": round(candidate.get("name_score", 0.0) * 100, 1) if candidate else 0.0,
        "number_score_pct": round(candidate.get("number_score", 0.0) * 100, 1) if candidate else 0.0,
        "set_score_pct": round(candidate.get("set_score", 0.0) * 100, 1) if candidate else 0.0,
        "grade_score_pct": round(candidate.get("grade_score", 0.0) * 100, 1) if candidate else 0.0,
        "condition_score_pct": round(candidate.get("condition_score", 0.0) * 100, 1) if candidate else 0.0,
        "next_best_score": next_best_score if next_best_score is not None else "",
        "score_gap": gap,
        "candidate_count": candidate_count,
        "collectr_row_id": _text(collectr_row.get("collectr_row_id")),
        "collectr_source_row": collectr_row.get("source_row_number", ""),
        "collectr_source_copy": collectr_row.get("source_copy_number", ""),
        "collectr_source_quantity": collectr_row.get("source_quantity", ""),
        "collectr_set_name": _text(collectr_row.get("set_name")),
        "collectr_card_name": _text(collectr_row.get("card_name")),
        "collectr_card_number": _text(collectr_row.get("card_number")),
        "collectr_rarity": _text(collectr_row.get("card_subtype")),
        "collectr_variant": _text(collectr_row.get("variant")),
        "collectr_condition": _text(collectr_row.get("condition")),
        "collectr_grading_company": _text(collectr_row.get("grading_company")),
        "collectr_grade": _text(collectr_row.get("grade")),
        "collectr_market_value": to_money(collectr_row.get("market_value")),
        "inventory_id": _text(inventory_row.get("inventory_id")) if inventory_row is not None else "",
        "inventory_status": _text(inventory_row.get("inventory_status")) if inventory_row is not None else "",
        "inventory_set_name": _text(inventory_row.get("set_name")) if inventory_row is not None else "",
        "inventory_card_name": _text(inventory_row.get("card_name")) if inventory_row is not None else "",
        "inventory_card_number": _text(inventory_row.get("card_number")) if inventory_row is not None else "",
        "inventory_reference_link": _text(inventory_row.get("reference_link")) if inventory_row is not None else "",
        "inventory_reference_set": reference["set"],
        "inventory_reference_name": reference["name"],
        "inventory_reference_number": reference["number"],
        "inventory_variant": _text(inventory_row.get("variant")) if inventory_row is not None else "",
        "inventory_card_subtype": _text(inventory_row.get("card_subtype")) if inventory_row is not None else "",
        "inventory_condition": _text(inventory_row.get("condition")) if inventory_row is not None else "",
        "inventory_grading_company": _text(inventory_row.get("grading_company")) if inventory_row is not None else "",
        "inventory_grade": _text(inventory_row.get("grade")) if inventory_row is not None else "",
        "inventory_purchase_date": _text(inventory_row.get("purchase_date")) if inventory_row is not None else "",
        "inventory_purchased_from": _text(inventory_row.get("purchased_from")) if inventory_row is not None else "",
        "inventory_total_cost": to_money(inventory_row.get("total_cost")) if inventory_row is not None else "",
        "inventory_market_value": to_money(inventory_row.get("market_value")) if inventory_row is not None else "",
        "inventory_sticker_price": to_money(inventory_row.get("sticker_price")) if inventory_row is not None else "",
    }


def _reconcile_collectr(
    collectr: pd.DataFrame,
    eligible_inventory: pd.DataFrame,
    *,
    auto_match_threshold: float = AUTO_MATCH_THRESHOLD_DEFAULT,
    duplicate_policy: str = "Keep newest eligible inventory",
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    collectr_work = collectr.reset_index(drop=True).copy()
    inventory_work = eligible_inventory.reset_index(drop=True).copy()

    pair_records: list[dict[str, Any]] = []
    pairs_by_collectr: dict[int, list[dict[str, Any]]] = {
        idx: [] for idx in collectr_work.index
    }
    pairs_by_inventory: dict[int, list[dict[str, Any]]] = {
        idx: [] for idx in inventory_work.index
    }

    for collectr_idx, collectr_row in collectr_work.iterrows():
        for inventory_idx, inventory_row in inventory_work.iterrows():
            scored = _score_candidate_pair(collectr_row, inventory_row)
            if scored is None:
                continue
            record = {
                **scored,
                "collectr_index": int(collectr_idx),
                "inventory_index": int(inventory_idx),
                "purchase_rank": _inventory_purchase_rank(inventory_row),
            }
            pair_records.append(record)
            pairs_by_collectr[int(collectr_idx)].append(record)
            pairs_by_inventory[int(inventory_idx)].append(record)

    keep_newest = duplicate_policy == "Keep newest eligible inventory"
    pair_records.sort(
        key=lambda record: (
            record["score"],
            int(record["exact_number"]),
            int(record["exact_name"]),
            int(record["exact_set"]),
            record["purchase_rank"] if keep_newest else -record["purchase_rank"],
        ),
        reverse=True,
    )

    assigned_collectr: dict[int, dict[str, Any]] = {}
    assigned_inventory: dict[int, dict[str, Any]] = {}
    for record in pair_records:
        if record["score"] < auto_match_threshold:
            continue
        collectr_idx = record["collectr_index"]
        inventory_idx = record["inventory_index"]
        if collectr_idx in assigned_collectr or inventory_idx in assigned_inventory:
            continue
        assigned_collectr[collectr_idx] = record
        assigned_inventory[inventory_idx] = record

    audit_rows: list[dict[str, Any]] = []
    for collectr_idx, collectr_row in collectr_work.iterrows():
        candidates = sorted(
            pairs_by_collectr.get(int(collectr_idx), []),
            key=lambda record: record["score"],
            reverse=True,
        )
        assigned = assigned_collectr.get(int(collectr_idx))

        if assigned is not None:
            candidate = assigned
            inventory_row = inventory_work.loc[candidate["inventory_index"]]
            next_scores = [
                record["score"]
                for record in candidates
                if record["inventory_index"] != candidate["inventory_index"]
            ]
            next_best = max(next_scores) if next_scores else None
            audit_rows.append(
                _audit_row(
                    collectr_row,
                    inventory_row,
                    candidate,
                    matched=True,
                    match_status="MATCHED",
                    next_best_score=next_best,
                    candidate_count=len(candidates),
                )
            )
            continue

        candidate = candidates[0] if candidates else None
        inventory_row = (
            inventory_work.loc[candidate["inventory_index"]]
            if candidate is not None
            else None
        )
        next_best = candidates[1]["score"] if len(candidates) > 1 else None

        if candidate is None or candidate["score"] < REVIEW_MATCH_THRESHOLD:
            status = "NO RELIABLE MATCH"
        elif candidate["score"] >= auto_match_threshold and candidate["inventory_index"] in assigned_inventory:
            status = "UNMATCHED COPY - INVENTORY MATCH ALREADY USED"
        else:
            status = "REVIEW SUGGESTION"

        audit_rows.append(
            _audit_row(
                collectr_row,
                inventory_row,
                candidate,
                matched=False,
                match_status=status,
                next_best_score=next_best,
                candidate_count=len(candidates),
            )
        )

    audit = pd.DataFrame(audit_rows)
    for column in MATCH_AUDIT_COLUMNS:
        if column not in audit.columns:
            audit[column] = ""
    audit = audit[MATCH_AUDIT_COLUMNS].copy()

    matched = audit[audit["match_status"].eq("MATCHED")].copy()

    unmatched_collectr_indexes = sorted(
        set(collectr_work.index).difference(assigned_collectr)
    )
    new_cards = collectr_work.loc[unmatched_collectr_indexes].copy()
    if not new_cards.empty:
        audit_by_collectr = audit.set_index("collectr_row_id", drop=False)
        new_cards["suggested_inventory_id"] = new_cards["collectr_row_id"].map(
            audit_by_collectr["inventory_id"]
        )
        new_cards["suggested_inventory_reference_link"] = new_cards[
            "collectr_row_id"
        ].map(audit_by_collectr["inventory_reference_link"])
        new_cards["suggested_match_score"] = new_cards["collectr_row_id"].map(
            audit_by_collectr["match_score"]
        )
        new_cards["suggested_match_status"] = new_cards["collectr_row_id"].map(
            audit_by_collectr["match_status"]
        )
        new_cards["reference_link"] = ""
        new_cards["year"] = ""

    unmatched_inventory_indexes = sorted(
        set(inventory_work.index).difference(assigned_inventory)
    )
    missing_inventory = inventory_work.loc[unmatched_inventory_indexes].copy()
    if not missing_inventory.empty:
        best_collectr_rows: list[dict[str, Any]] = []
        for inventory_idx in unmatched_inventory_indexes:
            candidates = sorted(
                pairs_by_inventory.get(int(inventory_idx), []),
                key=lambda record: record["score"],
                reverse=True,
            )
            candidate = candidates[0] if candidates else None
            collectr_row = (
                collectr_work.loc[candidate["collectr_index"]]
                if candidate is not None
                else None
            )
            score = candidate["score"] if candidate is not None else 0.0
            if candidate is None or score < REVIEW_MATCH_THRESHOLD:
                status = "No reliable Collectr candidate"
            elif candidate["collectr_index"] in assigned_collectr:
                status = "Collectr candidate already matched to another inventory copy"
            else:
                status = "Review possible mismatch before marking sold"
            best_collectr_rows.append(
                {
                    "best_collectr_row_id": _text(collectr_row.get("collectr_row_id")) if collectr_row is not None else "",
                    "best_collectr_card_name": _text(collectr_row.get("card_name")) if collectr_row is not None else "",
                    "best_collectr_card_number": _text(collectr_row.get("card_number")) if collectr_row is not None else "",
                    "best_collectr_set_name": _text(collectr_row.get("set_name")) if collectr_row is not None else "",
                    "best_possible_match_score": score,
                    "best_possible_match_status": status,
                }
            )
        enrichment = pd.DataFrame(best_collectr_rows, index=missing_inventory.index)
        for column in enrichment.columns:
            missing_inventory[column] = enrichment[column]

    return audit, matched, new_cards, missing_inventory



# =========================================================
# Interactive reconciliation review
# =========================================================

NO_INVENTORY_OPTION = "— NEW PURCHASE / NO INVENTORY MATCH —"
NO_COLLECTR_OPTION = "— MISSING / ASSUMED SOLD —"


def _grade_condition_display(row: pd.Series) -> str:
    company = _text(row.get("grading_company"))
    grade = _text(row.get("grade"))
    condition = _text(row.get("condition"))
    if company or grade:
        return " ".join(part for part in [company, grade] if part).strip()
    return condition or "Condition not entered"


def _inventory_option_label(row: pd.Series) -> str:
    inventory_id = _text(row.get("inventory_id"))
    card_name = _text(row.get("card_name")) or "Unnamed card"
    card_number = _text(row.get("card_number")) or _reference_parts(
        row.get("reference_link")
    ).get("number", "")
    set_name = _text(row.get("set_name"))
    status = _text(row.get("inventory_status"))
    condition_or_grade = _grade_condition_display(row)
    number_text = f"#{card_number}" if card_number else "#—"
    return (
        f"{inventory_id} | {card_name} | {number_text} | {set_name or 'Set not entered'} "
        f"| {condition_or_grade} | {status} | Cost {money_fmt(row.get('total_cost'))}"
    )


def _collectr_option_label(row: pd.Series) -> str:
    row_id = _text(row.get("collectr_row_id"))
    card_name = _text(row.get("card_name")) or "Unnamed card"
    card_number = _text(row.get("card_number"))
    set_name = _text(row.get("set_name"))
    condition_or_grade = _grade_condition_display(row)
    number_text = f"#{card_number}" if card_number else "#—"
    return (
        f"{row_id} | {card_name} | {number_text} | {set_name or 'Set not entered'} "
        f"| {condition_or_grade} | Value {money_fmt(row.get('market_value'))}"
    )


def _inventory_option_maps(
    eligible_inventory: pd.DataFrame,
) -> tuple[list[str], dict[str, str], dict[str, str]]:
    rows = eligible_inventory.copy()
    if rows.empty:
        return [NO_INVENTORY_OPTION], {}, {}
    rows["__label"] = rows.apply(_inventory_option_label, axis=1)
    rows = rows.sort_values(
        [col for col in ["card_name", "set_name", "card_number", "inventory_id"] if col in rows.columns],
        kind="stable",
    )
    label_to_id = {
        _text(row.get("__label")): _text(row.get("inventory_id"))
        for _, row in rows.iterrows()
    }
    id_to_label = {inventory_id: label for label, inventory_id in label_to_id.items()}
    options = [NO_INVENTORY_OPTION] + list(label_to_id.keys())
    return options, label_to_id, id_to_label


def _collectr_option_maps(
    collectr_cards: pd.DataFrame,
) -> tuple[list[str], dict[str, str], dict[str, str]]:
    rows = collectr_cards.copy()
    rows["__label"] = rows.apply(_collectr_option_label, axis=1)
    rows = rows.sort_values(
        [col for col in ["card_name", "set_name", "card_number", "collectr_row_id"] if col in rows.columns],
        kind="stable",
    )
    label_to_id = {
        _text(row.get("__label")): _text(row.get("collectr_row_id"))
        for _, row in rows.iterrows()
    }
    id_to_label = {row_id: label for label, row_id in label_to_id.items()}
    options = [NO_COLLECTR_OPTION] + list(label_to_id.keys())
    return options, label_to_id, id_to_label


def _initial_review_assignments(audit: pd.DataFrame) -> dict[str, str]:
    assignments: dict[str, str] = {}
    for _, row in audit.iterrows():
        row_id = _text(row.get("collectr_row_id"))
        inventory_id = (
            _text(row.get("inventory_id"))
            if _text(row.get("match_status")) == "MATCHED"
            else ""
        )
        if row_id:
            assignments[row_id] = inventory_id
    return assignments


def _assignment_state_key(
    batch_id: str,
    eligible_inventory: pd.DataFrame,
    threshold: float,
    duplicate_policy: str,
) -> str:
    fingerprint_source = "|".join(
        sorted(
            f"{_text(row.get('inventory_id'))}:{_text(row.get('inventory_status'))}:"
            f"{_text(row.get('updated_at'))}"
            for _, row in eligible_inventory.iterrows()
        )
    )
    fingerprint = hashlib.sha256(fingerprint_source.encode("utf-8")).hexdigest()[:10]
    policy = _compact(duplicate_policy)[:12]
    return f"collectr_review_{batch_id}_{int(threshold)}_{policy}_{fingerprint}"


def _set_review_assignment(
    assignments: dict[str, str],
    collectr_row_id: str,
    inventory_id: str,
) -> None:
    """Set one-to-one assignment, automatically freeing a previously used inventory ID."""
    collectr_row_id = _text(collectr_row_id)
    inventory_id = _text(inventory_id)
    if not collectr_row_id:
        return

    if inventory_id:
        for other_row_id, other_inventory_id in list(assignments.items()):
            if other_row_id != collectr_row_id and other_inventory_id == inventory_id:
                assignments[other_row_id] = ""
    assignments[collectr_row_id] = inventory_id


def _best_collectr_for_inventory(
    inventory_row: pd.Series,
    collectr_cards: pd.DataFrame,
    assignments: dict[str, str],
) -> dict[str, Any]:
    candidates: list[tuple[float, pd.Series, dict[str, Any]]] = []
    for _, collectr_row in collectr_cards.iterrows():
        scored = _score_candidate_pair(collectr_row, inventory_row)
        if scored is not None:
            candidates.append((float(scored["score"]), collectr_row, scored))
    candidates.sort(key=lambda item: item[0], reverse=True)
    if not candidates:
        return {
            "best_collectr_row_id": "",
            "best_collectr_card_name": "",
            "best_collectr_card_number": "",
            "best_collectr_set_name": "",
            "best_possible_match_score": 0.0,
            "best_possible_match_status": "No reliable Collectr candidate",
        }

    score, collectr_row, _ = candidates[0]
    collectr_row_id = _text(collectr_row.get("collectr_row_id"))
    assigned_inventory_id = _text(assignments.get(collectr_row_id))
    if assigned_inventory_id:
        status = f"Collectr row is currently assigned to {assigned_inventory_id}"
    elif score >= AUTO_MATCH_THRESHOLD_DEFAULT:
        status = "Strong unassigned candidate — review before marking sold"
    elif score >= REVIEW_MATCH_THRESHOLD:
        status = "Possible candidate — review before marking sold"
    else:
        status = "No reliable Collectr candidate"

    return {
        "best_collectr_row_id": collectr_row_id,
        "best_collectr_card_name": _text(collectr_row.get("card_name")),
        "best_collectr_card_number": _text(collectr_row.get("card_number")),
        "best_collectr_set_name": _text(collectr_row.get("set_name")),
        "best_possible_match_score": score,
        "best_possible_match_status": status,
    }


def _build_reviewed_reconciliation(
    collectr_cards: pd.DataFrame,
    eligible_inventory: pd.DataFrame,
    initial_audit: pd.DataFrame,
    assignments: dict[str, str],
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, set[str]]:
    inventory_by_id = {
        _text(row.get("inventory_id")): row
        for _, row in eligible_inventory.iterrows()
        if _text(row.get("inventory_id"))
    }
    initial_by_collectr = {
        _text(row.get("collectr_row_id")): row
        for _, row in initial_audit.iterrows()
        if _text(row.get("collectr_row_id"))
    }

    selected_values = [
        _text(value)
        for value in assignments.values()
        if _text(value) and _text(value) in inventory_by_id
    ]
    duplicate_inventory_ids = {
        value for value in selected_values if selected_values.count(value) > 1
    }

    review_rows: list[dict[str, Any]] = []
    new_card_indexes: list[int] = []
    for collectr_index, collectr_row in collectr_cards.iterrows():
        collectr_row_id = _text(collectr_row.get("collectr_row_id"))
        selected_inventory_id = _text(assignments.get(collectr_row_id))
        if selected_inventory_id not in inventory_by_id:
            selected_inventory_id = ""

        initial = initial_by_collectr.get(collectr_row_id, pd.Series(dtype=object))
        initial_auto_inventory_id = (
            _text(initial.get("inventory_id"))
            if _text(initial.get("match_status")) == "MATCHED"
            else ""
        )
        suggested_inventory_id = _text(initial.get("inventory_id"))

        inventory_row = inventory_by_id.get(selected_inventory_id)
        scored = (
            _score_candidate_pair(collectr_row, inventory_row)
            if inventory_row is not None
            else None
        )
        selected_score = float(scored.get("score", 0.0)) if scored else 0.0
        selected_method = (
            _text(scored.get("match_method"))
            if scored
            else ("Manual override; automatic score unavailable" if inventory_row is not None else "")
        )

        if selected_inventory_id in duplicate_inventory_ids:
            review_status = "MATCH CONFLICT"
        elif selected_inventory_id:
            review_status = "MATCHED"
        else:
            review_status = "NEW IN COLLECTR"
            new_card_indexes.append(collectr_index)

        if selected_inventory_id and selected_inventory_id == initial_auto_inventory_id:
            assignment_source = "AUTO"
        elif selected_inventory_id:
            assignment_source = "MANUAL"
        else:
            assignment_source = "UNMATCHED"

        review_rows.append(
            {
                "review_status": review_status,
                "assignment_source": assignment_source,
                "collectr_row_id": collectr_row_id,
                "collectr_source_row": collectr_row.get("source_row_number", ""),
                "collectr_source_copy": collectr_row.get("source_copy_number", ""),
                "collectr_set_name": _text(collectr_row.get("set_name")),
                "collectr_card_name": _text(collectr_row.get("card_name")),
                "collectr_card_number": _text(collectr_row.get("card_number")),
                "collectr_rarity": _text(collectr_row.get("card_subtype")),
                "collectr_variant": _text(collectr_row.get("variant")),
                "collectr_condition": _text(collectr_row.get("condition")),
                "collectr_grading_company": _text(collectr_row.get("grading_company")),
                "collectr_grade": _text(collectr_row.get("grade")),
                "collectr_market_value": to_money(collectr_row.get("market_value")),
                "selected_inventory_id": selected_inventory_id,
                "selected_match_score": selected_score,
                "selected_match_method": selected_method,
                "inventory_status": _text(inventory_row.get("inventory_status")) if inventory_row is not None else "",
                "inventory_set_name": _text(inventory_row.get("set_name")) if inventory_row is not None else "",
                "inventory_card_name": _text(inventory_row.get("card_name")) if inventory_row is not None else "",
                "inventory_card_number": _text(inventory_row.get("card_number")) if inventory_row is not None else "",
                "inventory_reference_link": _text(inventory_row.get("reference_link")) if inventory_row is not None else "",
                "inventory_condition": _text(inventory_row.get("condition")) if inventory_row is not None else "",
                "inventory_grading_company": _text(inventory_row.get("grading_company")) if inventory_row is not None else "",
                "inventory_grade": _text(inventory_row.get("grade")) if inventory_row is not None else "",
                "inventory_total_cost": to_money(inventory_row.get("total_cost")) if inventory_row is not None else "",
                "initial_match_status": _text(initial.get("match_status")),
                "initial_match_score": to_money(initial.get("match_score")),
                "initial_suggested_inventory_id": suggested_inventory_id,
            }
        )

    review_audit = pd.DataFrame(review_rows)
    matched_review = review_audit[
        review_audit["review_status"].eq("MATCHED")
    ].copy()

    new_cards = collectr_cards.loc[new_card_indexes].copy() if new_card_indexes else collectr_cards.iloc[0:0].copy()
    if not new_cards.empty:
        new_cards["suggested_inventory_id"] = new_cards["collectr_row_id"].map(
            lambda row_id: _text(initial_by_collectr.get(_text(row_id), pd.Series(dtype=object)).get("inventory_id"))
        )
        new_cards["suggested_inventory_reference_link"] = new_cards["collectr_row_id"].map(
            lambda row_id: _text(initial_by_collectr.get(_text(row_id), pd.Series(dtype=object)).get("inventory_reference_link"))
        )
        new_cards["suggested_match_score"] = new_cards["collectr_row_id"].map(
            lambda row_id: to_money(initial_by_collectr.get(_text(row_id), pd.Series(dtype=object)).get("match_score"))
        )
        new_cards["suggested_match_status"] = new_cards["collectr_row_id"].map(
            lambda row_id: _text(initial_by_collectr.get(_text(row_id), pd.Series(dtype=object)).get("match_status"))
        )
        new_cards["reference_link"] = ""
        new_cards["year"] = ""

    used_inventory_ids = {
        _text(value)
        for value in assignments.values()
        if _text(value) and _text(value) in inventory_by_id
    }
    missing_inventory = eligible_inventory[
        ~eligible_inventory["inventory_id"].astype(str).str.strip().isin(used_inventory_ids)
    ].copy()
    if not missing_inventory.empty:
        enrichments = [
            _best_collectr_for_inventory(row, collectr_cards, assignments)
            for _, row in missing_inventory.iterrows()
        ]
        enrichment_df = pd.DataFrame(enrichments, index=missing_inventory.index)
        for column in enrichment_df.columns:
            missing_inventory[column] = enrichment_df[column]

    return review_audit, matched_review, new_cards, missing_inventory, duplicate_inventory_ids


def _collectr_editor_frame(
    review_rows: pd.DataFrame,
    assignments: dict[str, str],
    id_to_inventory_label: dict[str, str],
) -> pd.DataFrame:
    if review_rows.empty:
        return pd.DataFrame()
    out = review_rows.copy()
    out["inventory_match"] = out["collectr_row_id"].map(
        lambda row_id: id_to_inventory_label.get(
            _text(assignments.get(_text(row_id))), NO_INVENTORY_OPTION
        )
    )
    columns = [
        "collectr_row_id",
        "collectr_set_name",
        "collectr_card_name",
        "collectr_card_number",
        "collectr_condition",
        "collectr_grading_company",
        "collectr_grade",
        "collectr_market_value",
        "inventory_match",
        "selected_match_score",
        "assignment_source",
        "initial_match_status",
        "initial_match_score",
    ]
    return out[[column for column in columns if column in out.columns]].copy()


def _apply_collectr_editor_changes(
    edited: pd.DataFrame,
    assignments: dict[str, str],
    inventory_label_to_id: dict[str, str],
) -> bool:
    changed = False
    for _, row in edited.iterrows():
        collectr_row_id = _text(row.get("collectr_row_id"))
        selected_label = _text(row.get("inventory_match"))
        selected_inventory_id = inventory_label_to_id.get(selected_label, "")
        if selected_label == NO_INVENTORY_OPTION:
            selected_inventory_id = ""
        if _text(assignments.get(collectr_row_id)) != selected_inventory_id:
            _set_review_assignment(assignments, collectr_row_id, selected_inventory_id)
            changed = True
    return changed

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
    form["product_type"] = form.apply(
        lambda row: "Graded Card"
        if _text(row.get("grading_company")) or _text(row.get("grade"))
        else "Card",
        axis=1,
    )
    form["brand_or_league"] = "Pokemon TCG"
    form["card_type"] = "Pokemon"
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
    form["transaction_type"] = "Card Show"
    form["sale_channel"] = "Card Show"
    form["platform"] = ""
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
    product_type = "Graded Card" if grading_company or grade else "Card"

    created_at = now_iso()
    new_row = {
        "inventory_id": _text(row.get("inventory_id")) or str(uuid.uuid4())[:8],
        "image_url": "",
        "inventory_type": _text(row.get("inventory_type")) or "Show Inventory",
        "product_type": _text(row.get("product_type")) or product_type,
        "inventory_status": STATUS_ACTIVE,
        "sealed_product_type": "",
        "card_type": _text(row.get("card_type")) or "Pokemon",
        "brand_or_league": _text(row.get("brand_or_league")) or "Pokemon TCG",
        "set_name": _text(row.get("set_name")),
        "year": _text(row.get("year")),
        "card_name": _text(row.get("card_name")),
        "card_number": _text(row.get("card_number")),
        "variant": _text(row.get("variant")),
        "card_subtype": _text(row.get("card_subtype")),
        "grading_company": grading_company,
        "grade": grade,
        "reference_link": _text(row.get("reference_link")),
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
            if scope_reason != "Eligible ACTIVE/LISTED owned Pokémon card":
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
        "Comparison scope includes ACTIVE and LISTED business-owned Pokémon single "
        "cards. Consignment, personal inventory, sports/non-Pokémon cards, sealed "
        "products, GRADING, and SOLD records are excluded.",
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


# COLLECTR RECONCILIATION TAB SET — this replaces the former manual/bulk sale tabs.
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
        "Upload the Collectr export that represents the Pokémon cards you physically "
        "have now. The initial matcher compares it with eligible ACTIVE and LISTED "
        "business inventory, then you can correct every assignment before downloading "
        "purchase and sales forms."
    )

    st.markdown("### Inventory scope")
    scope_cols = st.columns(4)
    scope_cols[0].metric("All inventory rows", f"{len(inv):,}")
    scope_cols[1].metric(
        "Compared ACTIVE/LISTED Pokémon cards", f"{len(eligible_inventory):,}"
    )
    grading_count = (
        _series(inv, "inventory_status")
        .astype(str)
        .str.upper()
        .str.contains("GRAD", na=False)
        .sum()
    )
    scope_cols[2].metric("GRADING ignored", f"{int(grading_count):,}")
    excluded_owned = int(
        scope_summary.loc[
            scope_summary["scope_reason"].isin(
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
    scope_cols[3].metric("Other excluded rows", f"{excluded_owned:,}")

    with st.expander("See exactly what is excluded", expanded=False):
        st.dataframe(scope_summary, use_container_width=True, hide_index=True)
        st.caption(
            "A record must be ACTIVE or LISTED, Pokémon, non-consignment, "
            "non-personal, and a single card to participate in reconciliation."
        )

    selected_show = _selected_show(shows, "reconcile_show")

    option_cols = st.columns([1, 1])
    with option_cols[0]:
        auto_match_threshold = st.slider(
            "Minimum automatic match score",
            min_value=70,
            max_value=95,
            value=int(AUTO_MATCH_THRESHOLD_DEFAULT),
            step=1,
            help=(
                "The script initially assigns scores at or above this threshold. "
                "You can override any result in the review dropdowns."
            ),
        )
    with option_cols[1]:
        duplicate_policy = st.selectbox(
            "When duplicate copies are identical",
            ["Keep newest eligible inventory", "Keep oldest eligible inventory"],
            help=(
                "This only determines the initial automatic assignment. Your manual "
                "review can select the exact inventory ID afterward."
            ),
        )

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
                    "No Pokémon single-card rows were found in the Collectr file. "
                    "Check the Category column and confirm the export contains cards."
                )
            else:
                initial_audit, _, _, _ = _reconcile_collectr(
                    collectr_cards,
                    eligible_inventory,
                    auto_match_threshold=float(auto_match_threshold),
                    duplicate_policy=duplicate_policy,
                )

                review_state_key = _assignment_state_key(
                    batch_id,
                    eligible_inventory,
                    float(auto_match_threshold),
                    duplicate_policy,
                )
                if review_state_key not in st.session_state:
                    st.session_state[review_state_key] = _initial_review_assignments(
                        initial_audit
                    )
                assignments: dict[str, str] = st.session_state[review_state_key]

                inventory_options, inventory_label_to_id, inventory_id_to_label = (
                    _inventory_option_maps(eligible_inventory)
                )
                collectr_options, collectr_label_to_id, collectr_id_to_label = (
                    _collectr_option_maps(collectr_cards)
                )

                (
                    reviewed_audit,
                    matched_review,
                    new_cards,
                    missing_inventory,
                    duplicate_inventory_ids,
                ) = _build_reviewed_reconciliation(
                    collectr_cards,
                    eligible_inventory,
                    initial_audit,
                    assignments,
                )

                new_form = _build_new_purchase_form(
                    new_cards, selected_show, batch_id
                )
                sales_form = _build_missing_sales_form(
                    missing_inventory, selected_show, batch_id
                )

                st.success(
                    f"Initial matching complete. Review batch: {batch_id}", icon="✅"
                )

                controls = st.columns([1, 3])
                with controls[0]:
                    if st.button(
                        "Reset to script matches",
                        key=f"reset_{review_state_key}",
                        use_container_width=True,
                    ):
                        st.session_state[review_state_key] = _initial_review_assignments(
                            initial_audit
                        )
                        st.rerun()
                with controls[1]:
                    st.info(
                        "Changing a dropdown to an inventory ID automatically frees that "
                        "ID from any other Collectr row, preserving one-to-one matching. "
                        "Choose the NEW PURCHASE option to remove a match.",
                        icon="ℹ️",
                    )

                metrics = st.columns(7)
                metrics[0].metric(
                    "Collectr source rows", f"{collectr_stats['source_rows']:,}"
                )
                metrics[1].metric(
                    "Collectr card copies", f"{collectr_stats['individual_cards']:,}"
                )
                metrics[2].metric("Reviewed matches", f"{len(matched_review):,}")
                metrics[3].metric("New purchases", f"{len(new_cards):,}")
                metrics[4].metric("Missing / possible sales", f"{len(missing_inventory):,}")
                metrics[5].metric(
                    "Manual overrides",
                    f"{int(reviewed_audit['assignment_source'].eq('MANUAL').sum()):,}",
                )
                metrics[6].metric(
                    "Upload rows ignored",
                    f"{collectr_stats['non_pokemon_rows_ignored'] + collectr_stats.get('sealed_rows_ignored', 0):,}",
                )

                if duplicate_inventory_ids:
                    st.error(
                        "One or more inventory IDs are assigned more than once: "
                        + ", ".join(sorted(duplicate_inventory_ids))
                        + ". Correct these before downloading forms."
                    )

                reviewed_audit_download = reviewed_audit.copy()
                reviewed_audit_download["selected_inventory_label"] = (
                    reviewed_audit_download["selected_inventory_id"].map(
                        inventory_id_to_label
                    )
                )
                st.download_button(
                    "Download reviewed match audit CSV",
                    data=_csv_download_data(reviewed_audit_download),
                    file_name=(
                        f"{_compact(selected_show.get('show_name')) or 'show'}_"
                        f"{batch_id}_reviewed_match_audit.csv"
                    ),
                    mime="text/csv",
                    use_container_width=True,
                )

                result_tabs = st.tabs(
                    [
                        f"Full Review ({len(reviewed_audit):,})",
                        f"Matched ({len(matched_review):,})",
                        f"New in Collectr ({len(new_cards):,})",
                        f"Missing from Collectr ({len(missing_inventory):,})",
                    ]
                )

                editor_config = {
                    "inventory_match": st.column_config.SelectboxColumn(
                        "Matching inventory",
                        options=inventory_options,
                        required=True,
                        width="large",
                        help=(
                            "Inventory ID | card name | card number | set | "
                            "condition or graded company/grade | status | total cost"
                        ),
                    ),
                    "collectr_market_value": st.column_config.NumberColumn(
                        "Collectr value", format="$%.2f"
                    ),
                    "selected_match_score": st.column_config.NumberColumn(
                        "Current match score", format="%.1f"
                    ),
                    "initial_match_score": st.column_config.NumberColumn(
                        "Initial score", format="%.1f"
                    ),
                }

                with result_tabs[0]:
                    st.caption(
                        "All Collectr copies in standard format with the current inventory "
                        "assignment. You can make corrections here or in the Matched/New tabs."
                    )
                    full_editor = _collectr_editor_frame(
                        reviewed_audit, assignments, inventory_id_to_label
                    )
                    st.dataframe(
                        full_editor,
                        use_container_width=True,
                        hide_index=True,
                        column_config=editor_config,
                    )
                    st.caption(
                        "Use the Matched, New in Collectr, or Missing from Collectr tabs "
                        "to change assignments. This full view is a consolidated audit."
                    )

                with result_tabs[1]:
                    if matched_review.empty:
                        st.info("No Collectr rows are currently assigned to inventory.")
                    else:
                        st.caption(
                            "Review each automatic or manual match. Change the dropdown to "
                            "the correct inventory ID, or choose NEW PURCHASE to unmatch it."
                        )
                        matched_editor = _collectr_editor_frame(
                            matched_review, assignments, inventory_id_to_label
                        )
                        matched_row_hash = hashlib.sha256(
                            "|".join(
                                matched_editor["collectr_row_id"].astype(str).tolist()
                            ).encode("utf-8")
                        ).hexdigest()[:8]
                        edited_matched = st.data_editor(
                            matched_editor,
                            use_container_width=True,
                            hide_index=True,
                            key=f"matched_editor_{review_state_key}_{matched_row_hash}",
                            column_config=editor_config,
                            disabled=[
                                column
                                for column in matched_editor.columns
                                if column != "inventory_match"
                            ],
                        )
                        if _apply_collectr_editor_changes(
                            edited_matched, assignments, inventory_label_to_id
                        ):
                            st.session_state[review_state_key] = assignments
                            st.rerun()

                with result_tabs[2]:
                    if new_cards.empty:
                        st.success(
                            "Every Collectr card is currently assigned to an eligible "
                            "ACTIVE/LISTED inventory record."
                        )
                    else:
                        st.caption(
                            "These Collectr rows are currently treated as new purchases. "
                            "Use the Matching inventory dropdown when one is already in your "
                            "database. Leave NEW PURCHASE selected only for genuinely new cards."
                        )
                        new_review_rows = reviewed_audit[
                            reviewed_audit["review_status"].eq("NEW IN COLLECTR")
                        ].copy()
                        new_editor = _collectr_editor_frame(
                            new_review_rows, assignments, inventory_id_to_label
                        )
                        new_row_hash = hashlib.sha256(
                            "|".join(
                                new_editor["collectr_row_id"].astype(str).tolist()
                            ).encode("utf-8")
                        ).hexdigest()[:8]
                        edited_new = st.data_editor(
                            new_editor,
                            use_container_width=True,
                            hide_index=True,
                            key=f"new_editor_{review_state_key}_{new_row_hash}",
                            column_config=editor_config,
                            disabled=[
                                column
                                for column in new_editor.columns
                                if column != "inventory_match"
                            ],
                        )
                        if _apply_collectr_editor_changes(
                            edited_new, assignments, inventory_label_to_id
                        ):
                            st.session_state[review_state_key] = assignments
                            st.rerun()

                        st.download_button(
                            "Download reviewed new-purchase form",
                            data=_csv_download_data(new_form),
                            file_name=(
                                f"{_compact(selected_show.get('show_name')) or 'show'}_"
                                f"{batch_id}_new_inventory.csv"
                            ),
                            mime="text/csv",
                            use_container_width=True,
                            disabled=bool(duplicate_inventory_ids),
                        )
                        st.caption(
                            "Fill in purchase_date, purchased_from, purchase_price, shipping, "
                            "tax, grading_fee, reference_link, sticker_price, and notes as "
                            "needed. Reupload the completed file under Add New Inventory."
                        )

                with result_tabs[3]:
                    if missing_inventory.empty:
                        st.success(
                            "Every eligible ACTIVE/LISTED inventory record is currently "
                            "assigned to a Collectr card."
                        )
                    else:
                        st.caption(
                            "Each row below is an inventory record not currently assigned to "
                            "Collectr. The inventory record column contains ID, name, number, "
                            "condition/grade, status, and cost. To correct a false missing item, "
                            "select the Collectr card it should match. That Collectr card will be "
                            "moved from its previous assignment automatically."
                        )
                        missing_editor = missing_inventory.copy()
                        missing_editor["inventory_record"] = missing_editor.apply(
                            _inventory_option_label, axis=1
                        )
                        missing_editor["collectr_match"] = NO_COLLECTR_OPTION
                        missing_columns = [
                            "inventory_record",
                            "reference_link",
                            "best_collectr_card_name",
                            "best_collectr_card_number",
                            "best_collectr_set_name",
                            "best_possible_match_score",
                            "best_possible_match_status",
                            "collectr_match",
                        ]
                        missing_editor = missing_editor[
                            [column for column in missing_columns if column in missing_editor.columns]
                        ].copy()
                        missing_key_hash = hashlib.sha256(
                            "|".join(
                                sorted(missing_inventory["inventory_id"].astype(str).tolist())
                            ).encode("utf-8")
                        ).hexdigest()[:8]
                        edited_missing = st.data_editor(
                            missing_editor,
                            use_container_width=True,
                            hide_index=True,
                            key=f"missing_editor_{review_state_key}_{missing_key_hash}",
                            column_config={
                                "reference_link": st.column_config.LinkColumn(
                                    "Inventory reference link"
                                ),
                                "best_possible_match_score": st.column_config.NumberColumn(
                                    "Best script score", format="%.1f"
                                ),
                                "collectr_match": st.column_config.SelectboxColumn(
                                    "Match to Collectr item",
                                    options=collectr_options,
                                    required=True,
                                    width="large",
                                ),
                            },
                            disabled=[
                                column
                                for column in missing_editor.columns
                                if column != "collectr_match"
                            ],
                        )

                        selected_collectr_rows = edited_missing[
                            edited_missing["collectr_match"].astype(str).ne(
                                NO_COLLECTR_OPTION
                            )
                        ].copy()
                        if not selected_collectr_rows.empty:
                            duplicate_collectr_labels = selected_collectr_rows.loc[
                                selected_collectr_rows["collectr_match"].duplicated(
                                    keep=False
                                ),
                                "collectr_match",
                            ].tolist()
                            if duplicate_collectr_labels:
                                st.error(
                                    "The same Collectr row was selected for more than one "
                                    "missing inventory record. Choose only one inventory record "
                                    "for each Collectr copy."
                                )
                            else:
                                missing_label_to_id = {
                                    _inventory_option_label(row): _text(
                                        row.get("inventory_id")
                                    )
                                    for _, row in missing_inventory.iterrows()
                                }
                                changed = False
                                for _, row in selected_collectr_rows.iterrows():
                                    inventory_id = missing_label_to_id.get(
                                        _text(row.get("inventory_record")), ""
                                    )
                                    collectr_row_id = collectr_label_to_id.get(
                                        _text(row.get("collectr_match")), ""
                                    )
                                    if inventory_id and collectr_row_id:
                                        _set_review_assignment(
                                            assignments, collectr_row_id, inventory_id
                                        )
                                        changed = True
                                if changed:
                                    st.session_state[review_state_key] = assignments
                                    st.rerun()

                        st.download_button(
                            "Download reviewed missing-card sales form",
                            data=_csv_download_data(sales_form),
                            file_name=(
                                f"{_compact(selected_show.get('show_name')) or 'show'}_"
                                f"{batch_id}_missing_sales.csv"
                            ),
                            mime="text/csv",
                            use_container_width=True,
                            disabled=bool(duplicate_inventory_ids),
                        )
                        st.caption(
                            "Fill in sold_date, sold_price, fees, transaction_type, "
                            "sale_channel, platform, show information, and sale_notes. Set "
                            "process_row to NO for anything that was not sold. Reupload the "
                            "completed file under Update Missing Sales."
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
            current_inventory = inv.copy()
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
                    latest_inventory = current_inventory.copy()
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
        "as ACTIVE or LISTED business-owned Pokémon single cards can be marked SOLD."
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
            current_inventory = inv.copy()
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
                    f"I confirm that I want to mark {len(valid_sales):,} ACTIVE/LISTED "
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
                    latest_inventory = current_inventory.copy()
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
                        shared_batch_id = (
                            _text(final_valid.iloc[0].get("reconciliation_batch_id"))
                            if not final_valid.empty
                            else f"REC-{uuid.uuid4().hex[:10].upper()}"
                        )
                        if not shared_batch_id:
                            shared_batch_id = f"REC-{uuid.uuid4().hex[:10].upper()}"

                        # Build every inventory change in memory, then send the entire
                        # set to Google Sheets in one update_rows_by_key call. The old
                        # implementation called mark_inventory_sold once per row, which
                        # reopened the worksheet and reread its headers for every sale.
                        # Large show uploads therefore exhausted the Sheets per-minute
                        # read quota after only a few records.
                        inventory_updates: dict[str, dict[str, Any]] = {}
                        processed_at = now_iso()

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

                            inventory_updates[inventory_id] = {
                                "inventory_status": STATUS_SOLD,
                                "transaction_type": _text(
                                    row.get("transaction_type")
                                )
                                or "Card Show",
                                "platform": _text(row.get("platform")),
                                "sold_date": str(sold_date_value),
                                "sold_price": sold_price,
                                "fees": fees,
                                "fees_total": fees,
                                "shipping_charged": 0,
                                "net_proceeds": net,
                                "profit": profit,
                                "sale_channel": _text(row.get("sale_channel"))
                                or "Card Show",
                                "sale_notes": _text(row.get("sale_notes")),
                                "show_id": _text(row.get("show_id")),
                                "show_name": _text(row.get("show_name")),
                                "sold_transaction_id": shared_batch_id,
                                "reconciliation_batch_id": shared_batch_id,
                                "sold_created_at": processed_at,
                                "sold_updated_at": processed_at,
                                "updated_at": processed_at,
                            }

                        changed = update_rows_by_key(
                            get_ws_name("inventory_worksheet", "inventory"),
                            INVENTORY_COLUMNS,
                            "inventory_id",
                            inventory_updates,
                        )

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
