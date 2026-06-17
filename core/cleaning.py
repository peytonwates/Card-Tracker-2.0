from __future__ import annotations

import re
from datetime import datetime, timezone
import pandas as pd
import numpy as np

from .config import HEADER_ALIASES, NUMERIC_COLUMNS


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def clean_text(x) -> str:
    try:
        if x is None or pd.isna(x):
            return ""
    except Exception:
        if x is None:
            return ""
    return str(x).strip()


def norm_header(s: str) -> str:
    s = str(s or "").strip().lower()
    s = re.sub(r"__dup\d+$", "", s)
    s = re.sub(r"[^a-z0-9]+", "_", s)
    return re.sub(r"_+", "_", s).strip("_")


def canonical_header(h: str) -> str:
    hn = norm_header(h)
    for internal, aliases in HEADER_ALIASES.items():
        if hn == norm_header(internal):
            return internal
        for alias in aliases:
            if hn == norm_header(alias):
                return internal
    return hn


def normalize_headers(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame() if df is None else df.copy()
    out = df.copy()
    new_cols = []
    seen = {}
    for c in out.columns:
        cc = canonical_header(c)
        seen[cc] = seen.get(cc, 0) + 1
        new_cols.append(cc if seen[cc] == 1 else f"{cc}__dup{seen[cc]}")
    out.columns = new_cols

    base_cols = []
    for c in out.columns:
        b = re.sub(r"__dup\d+$", "", c)
        if b not in base_cols:
            base_cols.append(b)
    merged = pd.DataFrame(index=out.index)
    for b in base_cols:
        candidates = [c for c in out.columns if c == b or c.startswith(f"{b}__dup")]
        if len(candidates) == 1:
            merged[b] = out[candidates[0]]
        else:
            s = out[candidates[0]].copy()
            for c in candidates[1:]:
                blank = s.astype(str).str.strip().eq("") | s.isna()
                s = s.where(~blank, out[c])
            merged[b] = s
    return merged


def to_money(x) -> float:
    try:
        if x is None:
            return 0.0
        if isinstance(x, (int, float, np.number)) and not pd.isna(x):
            return float(x)
        s = str(x).strip()
        if not s or s.lower() in {"nan", "none", "null"}:
            return 0.0
        neg = s.startswith("(") and s.endswith(")")
        s = s.replace(",", "")
        s = re.sub(r"[^0-9.\-]", "", s)
        if s in {"", ".", "-", "-."}:
            return 0.0
        val = float(s)
        return -abs(val) if neg else val
    except Exception:
        return 0.0


def ensure_columns(df: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    out = normalize_headers(df) if df is not None else pd.DataFrame()
    for c in columns:
        if c not in out.columns:
            out[c] = ""
    return out[columns].copy()


def coerce_numeric(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for c in out.columns:
        base = re.sub(r"__dup\d+$", "", c)
        if base in NUMERIC_COLUMNS:
            out[c] = out[c].apply(to_money).astype(float)
    return out


def normalize_status(x: str) -> str:
    s = clean_text(x).upper()
    return s if s else "ACTIVE"


def normalize_card_type(x: str) -> str:
    s = clean_text(x).lower()
    if "sport" in s or s in {"football", "basketball", "baseball", "hockey", "soccer", "ufc", "golf"}:
        return "Sports"
    if "pok" in s:
        return "Pokemon"
    return clean_text(x) or "Pokemon"


def clean_inventory(df: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    out = ensure_columns(df, columns)
    out = coerce_numeric(out)
    out["inventory_id"] = out["inventory_id"].astype(str).str.strip()
    out = out[out["inventory_id"].ne("")].copy()
    out["inventory_status"] = out["inventory_status"].apply(normalize_status)
    out["card_type"] = out["card_type"].apply(normalize_card_type)
    for c in ["purchase_date", "sold_date", "list_date", "market_price_updated_at", "created_at"]:
        if c in out.columns:
            out[f"__{c}_dt"] = pd.to_datetime(out[c], errors="coerce")
    out["total_price"] = out["total_price"].where(out["total_price"] > 0, out["purchase_price"] + out["shipping"] + out["tax"])
    out["grading_fee"] = out["grading_fee"].fillna(0).astype(float)
    out["total_cost"] = out["total_cost"].where(out["total_cost"] > 0, out["total_price"] + out["grading_fee"])
    out["market_value"] = out["market_value"].where(out["market_value"] > 0, out["market_price"])
    out["net_proceeds"] = out["net_proceeds"].where(out["net_proceeds"] > 0, out["sold_price"] - out["fees_total"])
    out["profit"] = out["profit"].where(out["profit"].abs() > 0, out["net_proceeds"] - out["total_cost"])
    return out


def clean_generic(df: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    out = ensure_columns(df, columns)
    out = coerce_numeric(out)
    return out


def age_bucket(days: float) -> str:
    try:
        d = float(days)
    except Exception:
        return "Unknown"
    if d < 0:
        return "Future"
    if d <= 30:
        return "0-30 days"
    if d <= 60:
        return "31-60 days"
    if d <= 90:
        return "61-90 days"
    if d <= 180:
        return "91-180 days"
    return "181+ days"


def money_fmt(x) -> str:
    return f"${to_money(x):,.2f}"
