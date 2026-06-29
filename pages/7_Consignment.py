# pages/5_Consignment.py
# Streamlit Consignment Page
# Separate Google Sheet tab: consignment
# Designed to be self-contained so it will not touch your normal inventory worksheet.

from __future__ import annotations

import base64
import html
import json
import math
import re
import statistics
import time
import uuid
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote_plus, urlparse

import pandas as pd
import requests
import streamlit as st

try:
    import gspread
    from google.oauth2.service_account import Credentials
except Exception:  # pragma: no cover - Streamlit will show a friendly setup error
    gspread = None
    Credentials = None


# -----------------------------------------------------------------------------
# Page config
# -----------------------------------------------------------------------------

st.set_page_config(page_title="Consignment", page_icon="🤝", layout="wide")

WORKSHEET_NAME = "consignment"
DEFAULT_CONSIGNOR = "Family Consignment"
DEFAULT_COMMISSION_RATE = 0.10
EBAY_MARKETPLACE_ID = "EBAY_US"
EBAY_API_BASE = "https://api.ebay.com"

STATUS_OPTIONS = ["ACTIVE", "SOLD", "PULLED", "RETURNED"]
PAYOUT_STATUS_OPTIONS = ["UNPAID", "PAID", "PARTIAL", "N/A"]
CONDITION_OPTIONS = [
    "Near Mint",
    "Lightly Played",
    "Moderately Played",
    "Heavily Played",
    "Damaged",
    "PSA 10",
    "PSA 9",
    "PSA 8",
    "PSA 7",
    "PSA 6",
    "PSA 5",
    "PSA 4",
    "PSA 3",
    "PSA 2",
    "PSA 1",
    "CGC",
    "BGS",
    "Other",
]

CONSIGNMENT_COLUMNS = [
    "consignment_id",
    "created_at",
    "updated_at",
    "consignor",
    "pricecharting_link",
    "image_url",
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
    "condition",
    "condition_notes",
    "status",
    "sticker_price",
    "sold_date",
    "sold_price",
    "sale_channel",
    "commission_rate",
    "commission_amount",
    "final_payout",
    "payout_status",
    "payout_date",
    "payout_notes",
    "pricecharting_raw",
    "pricecharting_grade_7",
    "pricecharting_grade_8",
    "pricecharting_grade_9",
    "pricecharting_grade_10",
    "pricecharting_checked_at",
    "ebay_query",
    "ebay_sold_search_url",
    "ebay_sold_avg",
    "ebay_sold_median",
    "ebay_sold_low",
    "ebay_sold_high",
    "ebay_sold_checked_at",
    "ebay_sold_comp_1_title",
    "ebay_sold_comp_1_price",
    "ebay_sold_comp_1_date",
    "ebay_sold_comp_1_url",
    "ebay_sold_comp_2_title",
    "ebay_sold_comp_2_price",
    "ebay_sold_comp_2_date",
    "ebay_sold_comp_2_url",
    "ebay_sold_comp_3_title",
    "ebay_sold_comp_3_price",
    "ebay_sold_comp_3_date",
    "ebay_sold_comp_3_url",
    "ebay_low_list_title",
    "ebay_low_list_price",
    "ebay_low_list_shipping",
    "ebay_low_list_total",
    "ebay_low_list_condition",
    "ebay_low_list_url",
    "ebay_low_list_checked_at",
    "notes",
]

FAMILY_VIEW_COLUMNS = [
    "consignment_id",
    "consignor",
    "set_name",
    "card_name",
    "card_number",
    "variant",
    "condition",
    "condition_notes",
    "status",
    "sticker_price",
    "sold_date",
    "sold_price",
    "commission_amount",
    "final_payout",
    "payout_status",
    "payout_date",
    "pricecharting_link",
    "ebay_sold_search_url",
    "ebay_low_list_url",
    "notes",
]

EXCLUDED_COMP_WORDS = [
    "proxy",
    "orica",
    "custom",
    "digital",
    "mystery",
    "repack",
    "pack fresh lot",
    "empty",
    "code card",
    "jumbo",
    "oversized",
    "sticker",
    "topps",
    "gold metal",
]

RAW_CONDITION_WORDS = {
    "Near Mint": ["near mint", "nm", "mint"],
    "Lightly Played": ["lightly played", "lp", "excellent"],
    "Moderately Played": ["moderately played", "mp"],
    "Heavily Played": ["heavily played", "hp"],
    "Damaged": ["damaged", "crease", "creased", "dmg"],
}


# -----------------------------------------------------------------------------
# General helpers
# -----------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def today_iso() -> str:
    return date.today().isoformat()


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    value = html.unescape(str(value))
    value = re.sub(r"\s+", " ", value).strip()
    return value


def slug_to_title(slug: str) -> str:
    return clean_text(slug.replace("-", " ").replace("_", " ").title())


def safe_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if math.isnan(value):
            return default
        return float(value)
    text = str(value).strip()
    if not text:
        return default
    text = text.replace("$", "").replace(",", "").replace("%", "")
    try:
        return float(text)
    except Exception:
        return default


def money_or_blank(value: Any) -> str:
    num = safe_float(value, default=float("nan"))
    if math.isnan(num):
        return ""
    return f"${num:,.2f}"


def normalize_url(url: str) -> str:
    url = clean_text(url)
    if url and not url.lower().startswith(("http://", "https://")):
        url = "https://" + url
    return url


def normalize_condition_for_query(condition: str) -> str:
    condition = clean_text(condition)
    if condition in ["Near Mint", "Lightly Played", "Moderately Played", "Heavily Played", "Damaged"]:
        return condition
    return condition.replace("PSA ", "PSA") if condition.startswith("PSA ") else condition


def make_consignment_id(existing_ids: Iterable[str] | None = None) -> str:
    existing = set(str(x) for x in (existing_ids or []))
    while True:
        candidate = f"CON-{datetime.now().strftime('%y%m%d')}-{uuid.uuid4().hex[:6].upper()}"
        if candidate not in existing:
            return candidate


def get_nested_secret(*paths: str, default: Any = None) -> Any:
    """Read st.secrets using root keys or dotted paths.

    Examples this supports:
    - st.secrets["spreadsheet_id"]
    - st.secrets["google"]["spreadsheet_id"]
    - st.secrets["ebay"]["client_id"]
    """
    for path in paths:
        try:
            current: Any = st.secrets
            found = True
            for part in path.split("."):
                if isinstance(current, dict):
                    if part in current:
                        current = current[part]
                    else:
                        found = False
                        break
                else:
                    if part in current:
                        current = current[part]
                    else:
                        found = False
                        break
            if found and current not in (None, ""):
                return current
        except Exception:
            continue
    return default


def to_plain_dict(value: Any) -> Dict[str, Any]:
    try:
        return dict(value)
    except Exception:
        return json.loads(json.dumps(value))


# -----------------------------------------------------------------------------
# Google Sheets helpers
# -----------------------------------------------------------------------------

@st.cache_resource(show_spinner=False)
def get_gspread_client():
    if gspread is None or Credentials is None:
        raise RuntimeError("Missing gspread or google-auth. Add them to requirements.txt.")

    sa_info = None
    for key in ["gcp_service_account", "google_service_account", "service_account"]:
        raw = get_nested_secret(key)
        if raw:
            sa_info = to_plain_dict(raw)
            break

    if not sa_info:
        root_type = get_nested_secret("type")
        root_email = get_nested_secret("client_email")
        root_key = get_nested_secret("private_key")
        if root_type and root_email and root_key:
            sa_info = {
                "type": get_nested_secret("type"),
                "project_id": get_nested_secret("project_id"),
                "private_key_id": get_nested_secret("private_key_id"),
                "private_key": get_nested_secret("private_key"),
                "client_email": get_nested_secret("client_email"),
                "client_id": get_nested_secret("client_id"),
                "auth_uri": get_nested_secret("auth_uri", default="https://accounts.google.com/o/oauth2/auth"),
                "token_uri": get_nested_secret("token_uri", default="https://oauth2.googleapis.com/token"),
                "auth_provider_x509_cert_url": get_nested_secret(
                    "auth_provider_x509_cert_url",
                    default="https://www.googleapis.com/oauth2/v1/certs",
                ),
                "client_x509_cert_url": get_nested_secret("client_x509_cert_url"),
            }

    if not sa_info:
        raise RuntimeError(
            "No Google service account found in Streamlit secrets. Expected [gcp_service_account] or equivalent."
        )

    if "private_key" in sa_info and isinstance(sa_info["private_key"], str):
        sa_info["private_key"] = sa_info["private_key"].replace("\\n", "\n")

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(sa_info, scopes=scopes)
    return gspread.authorize(creds)


def get_spreadsheet_id() -> str:
    spreadsheet_id = get_nested_secret(
        "spreadsheet_id",
        "SPREADSHEET_ID",
        "gsheet_id",
        "GOOGLE_SHEET_ID",
        "google.spreadsheet_id",
        "gspread.spreadsheet_id",
        "connections.gsheets.spreadsheet_id",
        default="",
    )
    if not spreadsheet_id:
        raise RuntimeError("Missing spreadsheet_id in Streamlit secrets.")
    return str(spreadsheet_id).strip()


@st.cache_resource(show_spinner=False)
def get_workbook():
    client = get_gspread_client()
    return client.open_by_key(get_spreadsheet_id())


def get_or_create_worksheet():
    workbook = get_workbook()
    try:
        ws = workbook.worksheet(WORKSHEET_NAME)
    except Exception:
        ws = workbook.add_worksheet(title=WORKSHEET_NAME, rows=1000, cols=len(CONSIGNMENT_COLUMNS) + 5)
        ws.update([CONSIGNMENT_COLUMNS], value_input_option="USER_ENTERED")
        return ws

    values = ws.get_all_values()
    if not values:
        ws.update([CONSIGNMENT_COLUMNS], value_input_option="USER_ENTERED")
        return ws

    headers = values[0]
    missing = [c for c in CONSIGNMENT_COLUMNS if c not in headers]
    if missing:
        new_headers = headers + missing
        ws.update("1:1", [new_headers], value_input_option="USER_ENTERED")
    return ws


def load_consignment_df() -> pd.DataFrame:
    ws = get_or_create_worksheet()
    records = ws.get_all_records(default_blank="")
    df = pd.DataFrame(records)
    for col in CONSIGNMENT_COLUMNS:
        if col not in df.columns:
            df[col] = ""
    df = df[CONSIGNMENT_COLUMNS]
    return recalc_financials(df)


def write_consignment_df(df: pd.DataFrame) -> None:
    ws = get_or_create_worksheet()
    out = df.copy()
    for col in CONSIGNMENT_COLUMNS:
        if col not in out.columns:
            out[col] = ""
    out = out[CONSIGNMENT_COLUMNS]
    out = recalc_financials(out)
    out = out.fillna("").astype(str)
    values = [CONSIGNMENT_COLUMNS] + out.values.tolist()
    ws.clear()
    ws.update(values, value_input_option="USER_ENTERED")


def append_consignment_rows(rows: List[Dict[str, Any]]) -> None:
    if not rows:
        return
    ws = get_or_create_worksheet()
    df_existing = load_consignment_df()
    existing_ids = set(df_existing.get("consignment_id", pd.Series(dtype=str)).astype(str).tolist())

    normalized_rows: List[List[Any]] = []
    for row in rows:
        new_row = {col: row.get(col, "") for col in CONSIGNMENT_COLUMNS}
        if not new_row["consignment_id"]:
            new_row["consignment_id"] = make_consignment_id(existing_ids)
            existing_ids.add(new_row["consignment_id"])
        if not new_row["created_at"]:
            new_row["created_at"] = now_iso()
        new_row["updated_at"] = now_iso()
        normalized_rows.append([new_row.get(col, "") for col in CONSIGNMENT_COLUMNS])

    ws.append_rows(normalized_rows, value_input_option="USER_ENTERED")


def recalc_financials(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    for col in ["sticker_price", "sold_price", "commission_rate", "commission_amount", "final_payout"]:
        if col not in out.columns:
            out[col] = ""
    for idx, row in out.iterrows():
        sold_price = safe_float(row.get("sold_price"), 0.0)
        commission_rate = safe_float(row.get("commission_rate"), DEFAULT_COMMISSION_RATE)
        if commission_rate > 1:
            commission_rate = commission_rate / 100
        commission_amount = sold_price * commission_rate if sold_price else 0.0
        final_payout = sold_price - commission_amount if sold_price else 0.0
        out.at[idx, "commission_rate"] = round(commission_rate, 4)
        out.at[idx, "commission_amount"] = round(commission_amount, 2) if sold_price else ""
        out.at[idx, "final_payout"] = round(final_payout, 2) if sold_price else ""
        status = clean_text(row.get("status")) or "ACTIVE"
        out.at[idx, "status"] = status.upper()
        payout_status = clean_text(row.get("payout_status")) or ("UNPAID" if status.upper() == "SOLD" else "N/A")
        if status.upper() == "SOLD" and payout_status == "N/A":
            payout_status = "UNPAID"
        out.at[idx, "payout_status"] = payout_status.upper()
    return out


# -----------------------------------------------------------------------------
# PriceCharting helpers
# -----------------------------------------------------------------------------

def fetch_html(url: str, timeout: int = 20) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; CardTrackerConsignment/1.0; +https://streamlit.io)",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    response = requests.get(url, headers=headers, timeout=timeout)
    response.raise_for_status()
    return response.text


def extract_meta_content(page_html: str, prop_or_name: str) -> str:
    patterns = [
        rf'<meta[^>]+property=["\']{re.escape(prop_or_name)}["\'][^>]+content=["\']([^"\']+)["\']',
        rf'<meta[^>]+name=["\']{re.escape(prop_or_name)}["\'][^>]+content=["\']([^"\']+)["\']',
        rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']{re.escape(prop_or_name)}["\']',
        rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']{re.escape(prop_or_name)}["\']',
    ]
    for pattern in patterns:
        m = re.search(pattern, page_html, flags=re.IGNORECASE | re.DOTALL)
        if m:
            return clean_text(m.group(1))
    return ""


def extract_title(page_html: str) -> str:
    h1 = re.search(r"<h1[^>]*>(.*?)</h1>", page_html, flags=re.IGNORECASE | re.DOTALL)
    if h1:
        return clean_text(re.sub(r"<[^>]+>", " ", h1.group(1)))
    title = re.search(r"<title[^>]*>(.*?)</title>", page_html, flags=re.IGNORECASE | re.DOTALL)
    if title:
        return clean_text(re.sub(r"<[^>]+>", " ", title.group(1)))
    return ""


def text_from_html(page_html: str) -> str:
    text = re.sub(r"<script[\s\S]*?</script>", " ", page_html, flags=re.IGNORECASE)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    return clean_text(text)


def extract_money_after_labels(text: str, labels: List[str]) -> str:
    # Handles patterns like "Ungraded $23.50" or "PSA 10 Price $410.00".
    for label in labels:
        escaped = re.escape(label)
        patterns = [
            rf"{escaped}\s*(?:Price)?\s*[:\-]?\s*\$\s*([0-9][0-9,]*(?:\.\d{{2}})?)",
            rf"{escaped}[\s\S]{{0,80}}?\$\s*([0-9][0-9,]*(?:\.\d{{2}})?)",
        ]
        for pattern in patterns:
            m = re.search(pattern, text, flags=re.IGNORECASE)
            if m:
                return str(round(safe_float(m.group(1)), 2))
    return ""


def parse_pricecharting_url_fields(url: str, title: str) -> Dict[str, str]:
    parsed = urlparse(url)
    parts = [p for p in parsed.path.split("/") if p]
    set_name = ""
    card_name = ""
    card_number = ""
    product_type = "Card"
    card_type = "Pokemon"
    brand = "Pokemon TCG"

    if len(parts) >= 3 and parts[0].lower() == "game":
        set_slug = parts[1]
        product_slug = parts[2]
        set_name = slug_to_title(set_slug.replace("pokemon-", ""))
        card_name_from_slug = slug_to_title(product_slug)
        # Common PriceCharting pattern: charizard-4, dark-charizard-4-82, pikachu-ex-247
        number_match = re.search(r"(?:^|[-\s#])([A-Za-z]*\d+[A-Za-z]?\/?\d*[A-Za-z]?)$", card_name_from_slug)
        if number_match:
            card_number = number_match.group(1).strip()
            card_name = clean_text(card_name_from_slug[: number_match.start(1)].replace("#", "").strip(" -#"))
        else:
            card_name = card_name_from_slug

    # Title often has better casing. Use it when it looks useful.
    title_clean = clean_text(title)
    if title_clean:
        first_piece = re.split(r"\s+Prices?\s*\|", title_clean, flags=re.IGNORECASE)[0]
        first_piece = re.sub(r"\s+PriceCharting.*$", "", first_piece, flags=re.IGNORECASE).strip()
        first_piece = re.sub(r"\s+Pokemon Cards?.*$", "", first_piece, flags=re.IGNORECASE).strip()
        if first_piece and len(first_piece) < 120:
            # Preserve manually parsed card number if present.
            number_match = re.search(r"#\s*([A-Za-z]*\d+[A-Za-z]?\/?\d*[A-Za-z]?)", first_piece)
            if number_match:
                card_number = number_match.group(1).strip()
                card_name = clean_text(first_piece[: number_match.start()].strip(" -#")) or card_name
            elif not card_name:
                card_name = first_piece

        pipe_parts = [clean_text(p) for p in title_clean.split("|") if clean_text(p)]
        for piece in pipe_parts:
            if "pokemon" in piece.lower() and "price" not in piece.lower():
                maybe_set = re.sub(r"\s+Pokemon Cards?.*$", "", piece, flags=re.IGNORECASE).strip()
                if maybe_set and len(maybe_set) < 80 and not set_name:
                    set_name = maybe_set

    # Guess year from title or set name if present.
    year_match = re.search(r"\b(199[5-9]|20\d{2})\b", title_clean)
    year = year_match.group(1) if year_match else ""

    return {
        "product_type": product_type,
        "card_type": card_type,
        "brand_or_league": brand,
        "set_name": set_name,
        "year": year,
        "card_name": card_name,
        "card_number": card_number,
    }


def parse_pricecharting_page(url: str) -> Dict[str, Any]:
    url = normalize_url(url)
    page = fetch_html(url)
    title = extract_title(page)
    text = text_from_html(page)
    image_url = extract_meta_content(page, "og:image")

    fields = parse_pricecharting_url_fields(url, title)

    prices = {
        "pricecharting_raw": extract_money_after_labels(text, ["Ungraded", "Ungraded Price", "Loose", "Loose Price"]),
        "pricecharting_grade_7": extract_money_after_labels(text, ["Grade 7", "Graded 7", "PSA 7"]),
        "pricecharting_grade_8": extract_money_after_labels(text, ["Grade 8", "Graded 8", "PSA 8"]),
        "pricecharting_grade_9": extract_money_after_labels(text, ["Grade 9", "Graded 9", "PSA 9"]),
        "pricecharting_grade_10": extract_money_after_labels(text, ["Grade 10", "Graded 10", "PSA 10"]),
    }

    return {
        "pricecharting_link": url,
        "image_url": image_url,
        **fields,
        **prices,
        "pricecharting_checked_at": now_iso(),
    }


# -----------------------------------------------------------------------------
# eBay helpers
# -----------------------------------------------------------------------------

def get_ebay_secret(*names: str, default: str = "") -> str:
    paths = []
    for name in names:
        paths.extend([name, name.upper(), f"ebay.{name}", f"ebay.{name.upper()}"])
    return str(get_nested_secret(*paths, default=default) or "").strip()


@st.cache_data(ttl=60 * 45, show_spinner=False)
def get_ebay_app_token() -> str:
    client_id = get_ebay_secret("client_id", "app_id", "EBAY_CLIENT_ID")
    client_secret = get_ebay_secret("client_secret", "cert_id", "EBAY_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise RuntimeError("Missing eBay client_id/client_secret in Streamlit secrets.")

    basic = base64.b64encode(f"{client_id}:{client_secret}".encode("utf-8")).decode("utf-8")
    headers = {
        "Authorization": f"Basic {basic}",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    data = {
        "grant_type": "client_credentials",
        "scope": "https://api.ebay.com/oauth/api_scope",
    }
    response = requests.post(
        f"{EBAY_API_BASE}/identity/v1/oauth2/token",
        headers=headers,
        data=data,
        timeout=25,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"eBay token error {response.status_code}: {response.text[:500]}")
    payload = response.json()
    return payload["access_token"]


def build_ebay_query(row: Dict[str, Any], include_condition: bool = True) -> str:
    pieces = []
    card_name = clean_text(row.get("card_name"))
    card_number = clean_text(row.get("card_number"))
    set_name = clean_text(row.get("set_name"))
    condition = clean_text(row.get("condition"))

    if card_name:
        pieces.append(card_name)
    if card_number:
        pieces.append(card_number)
    if set_name:
        pieces.append(set_name)
    pieces.append("pokemon")

    if include_condition and condition:
        cond = normalize_condition_for_query(condition)
        if cond in ["Near Mint", "Lightly Played", "Moderately Played", "Heavily Played", "Damaged"]:
            pieces.append(cond)
        elif cond.startswith("PSA"):
            pieces.append(cond.replace("PSA", "PSA ").strip())

    query = clean_text(" ".join(pieces))
    # eBay Browse API truncates q after 100 characters, so keep it short but useful.
    return query[:100].strip()


def ebay_sold_search_url(query: str) -> str:
    return f"https://www.ebay.com/sch/i.html?_nkw={quote_plus(query)}&LH_Sold=1&LH_Complete=1&rt=nc"


def ebay_active_low_search_url(query: str) -> str:
    return f"https://www.ebay.com/sch/i.html?_nkw={quote_plus(query)}&LH_BIN=1&_sop=15&rt=nc"


def parse_ebay_money(container: Any) -> float:
    if not container:
        return 0.0
    if isinstance(container, dict):
        return safe_float(container.get("value"), 0.0)
    return safe_float(container, 0.0)


def first_shipping_cost(item: Dict[str, Any]) -> float:
    options = item.get("shippingOptions") or []
    if not options:
        return 0.0
    for option in options:
        cost = option.get("shippingCost") if isinstance(option, dict) else None
        if cost:
            return parse_ebay_money(cost)
    return 0.0


def looks_bad_comp(title: str, condition: str = "") -> bool:
    lower = title.lower()
    if any(word in lower for word in EXCLUDED_COMP_WORDS):
        return True
    condition_lower = condition.lower()
    is_graded_search = any(x in condition_lower for x in ["psa", "cgc", "bgs", "sgc"])
    if not is_graded_search:
        # If you are pricing raw vintage, do not let slabs become the low-list reference.
        if any(x in lower for x in ["psa", "cgc", "bgs", "sgc", "graded", "slab"]):
            return True
    return False


def title_match_score(title: str, row: Dict[str, Any]) -> int:
    lower = title.lower()
    score = 0
    for field in ["card_name", "card_number", "set_name"]:
        value = clean_text(row.get(field)).lower()
        if value and value in lower:
            score += 2 if field != "set_name" else 1
    condition = clean_text(row.get("condition"))
    for word in RAW_CONDITION_WORDS.get(condition, []):
        if re.search(rf"\b{re.escape(word)}\b", lower):
            score += 1
    if "pokemon" in lower:
        score += 1
    return score


def browse_api_search_active(query: str, limit: int = 25) -> List[Dict[str, Any]]:
    token = get_ebay_app_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "X-EBAY-C-MARKETPLACE-ID": EBAY_MARKETPLACE_ID,
        "Accept": "application/json",
    }
    params = {
        "q": query,
        "limit": min(max(limit, 1), 50),
        "sort": "price",
        "filter": "buyingOptions:{FIXED_PRICE}",
    }
    response = requests.get(
        f"{EBAY_API_BASE}/buy/browse/v1/item_summary/search",
        headers=headers,
        params=params,
        timeout=30,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"eBay Browse API error {response.status_code}: {response.text[:700]}")
    return response.json().get("itemSummaries") or []


def find_low_list(row: Dict[str, Any], query: str, limit: int = 25) -> Dict[str, Any]:
    checked_at = now_iso()
    result = {
        "ebay_low_list_title": "",
        "ebay_low_list_price": "",
        "ebay_low_list_shipping": "",
        "ebay_low_list_total": "",
        "ebay_low_list_condition": "",
        "ebay_low_list_url": ebay_active_low_search_url(query),
        "ebay_low_list_checked_at": checked_at,
    }
    items = browse_api_search_active(query, limit=limit)
    candidates = []
    for item in items:
        title = clean_text(item.get("title"))
        if not title or looks_bad_comp(title, clean_text(row.get("condition"))):
            continue
        score = title_match_score(title, row)
        if score < 2:
            continue
        price = parse_ebay_money(item.get("price"))
        shipping = first_shipping_cost(item)
        total = price + shipping
        candidates.append((total, price, shipping, score, item))

    if not candidates:
        return result

    # Lowest total first. If tied, prefer stronger title match.
    candidates.sort(key=lambda x: (x[0], -x[3]))
    total, price, shipping, score, item = candidates[0]
    result.update(
        {
            "ebay_low_list_title": clean_text(item.get("title")),
            "ebay_low_list_price": round(price, 2),
            "ebay_low_list_shipping": round(shipping, 2),
            "ebay_low_list_total": round(total, 2),
            "ebay_low_list_condition": clean_text(item.get("condition")),
            "ebay_low_list_url": item.get("itemWebUrl") or ebay_active_low_search_url(query),
            "ebay_low_list_checked_at": checked_at,
        }
    )
    return result


def marketplace_insights_sold_search(query: str, limit: int = 20) -> Tuple[List[Dict[str, Any]], str]:
    """Try official eBay sold/completed data.

    Important: eBay marks Marketplace Insights as restricted/not open to new users.
    This will work only if your developer keyset has access.
    """
    token = get_ebay_app_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "X-EBAY-C-MARKETPLACE-ID": EBAY_MARKETPLACE_ID,
        "Accept": "application/json",
    }
    params = {"q": query, "limit": min(max(limit, 1), 50)}
    response = requests.get(
        f"{EBAY_API_BASE}/buy/marketplace_insights/v1_beta/item_sales/search",
        headers=headers,
        params=params,
        timeout=30,
    )
    if response.status_code in (401, 403):
        return [], "Marketplace Insights is blocked for this eBay developer keyset. Use the manual sold-search URL or request access from eBay."
    if response.status_code >= 400:
        return [], f"Marketplace Insights error {response.status_code}: {response.text[:700]}"

    payload = response.json()
    items = payload.get("itemSales") or payload.get("itemSummaries") or []
    return items, ""


def summarize_sold_comps(row: Dict[str, Any], query: str, limit: int = 20) -> Tuple[Dict[str, Any], str]:
    checked_at = now_iso()
    result = {
        "ebay_query": query,
        "ebay_sold_search_url": ebay_sold_search_url(query),
        "ebay_sold_avg": "",
        "ebay_sold_median": "",
        "ebay_sold_low": "",
        "ebay_sold_high": "",
        "ebay_sold_checked_at": checked_at,
        "ebay_sold_comp_1_title": "",
        "ebay_sold_comp_1_price": "",
        "ebay_sold_comp_1_date": "",
        "ebay_sold_comp_1_url": "",
        "ebay_sold_comp_2_title": "",
        "ebay_sold_comp_2_price": "",
        "ebay_sold_comp_2_date": "",
        "ebay_sold_comp_2_url": "",
        "ebay_sold_comp_3_title": "",
        "ebay_sold_comp_3_price": "",
        "ebay_sold_comp_3_date": "",
        "ebay_sold_comp_3_url": "",
    }

    items, error = marketplace_insights_sold_search(query, limit=limit)
    if error:
        return result, error

    comps = []
    for item in items:
        title = clean_text(item.get("title"))
        if not title or looks_bad_comp(title, clean_text(row.get("condition"))):
            continue
        score = title_match_score(title, row)
        if score < 2:
            continue
        price = parse_ebay_money(item.get("price") or item.get("lastSoldPrice"))
        if price <= 0:
            continue
        sold_date = clean_text(item.get("itemEndDate") or item.get("dateSold") or item.get("lastSoldDate"))
        url = item.get("itemWebUrl") or item.get("itemAffiliateWebUrl") or item.get("itemHref") or ""
        comps.append({"title": title, "price": round(price, 2), "date": sold_date[:10], "url": url, "score": score})

    comps = sorted(comps, key=lambda x: (-x["score"], x["price"]))[:10]
    prices = [c["price"] for c in comps]
    if prices:
        result.update(
            {
                "ebay_sold_avg": round(statistics.mean(prices), 2),
                "ebay_sold_median": round(statistics.median(prices), 2),
                "ebay_sold_low": round(min(prices), 2),
                "ebay_sold_high": round(max(prices), 2),
            }
        )
    for i, comp in enumerate(comps[:3], start=1):
        result[f"ebay_sold_comp_{i}_title"] = comp["title"]
        result[f"ebay_sold_comp_{i}_price"] = comp["price"]
        result[f"ebay_sold_comp_{i}_date"] = comp["date"]
        result[f"ebay_sold_comp_{i}_url"] = comp["url"]

    return result, ""


# -----------------------------------------------------------------------------
# Row creation / updates
# -----------------------------------------------------------------------------

def build_row_from_inputs(
    parsed_pc: Dict[str, Any],
    consignor: str,
    condition: str,
    condition_notes: str,
    sticker_price: Any,
    notes: str,
    ebay_payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    row = {col: "" for col in CONSIGNMENT_COLUMNS}
    row.update(parsed_pc or {})
    row.update(ebay_payload or {})

    row["consignor"] = clean_text(consignor) or DEFAULT_CONSIGNOR
    row["condition"] = clean_text(condition)
    row["condition_notes"] = clean_text(condition_notes)
    row["status"] = "ACTIVE"
    row["payout_status"] = "N/A"
    row["commission_rate"] = DEFAULT_COMMISSION_RATE
    row["sticker_price"] = round(safe_float(sticker_price), 2) if safe_float(sticker_price) else ""
    row["notes"] = clean_text(notes)
    row["created_at"] = now_iso()
    row["updated_at"] = now_iso()
    row["consignment_id"] = make_consignment_id()

    if not row.get("product_type"):
        row["product_type"] = "Card"
    if not row.get("card_type"):
        row["card_type"] = "Pokemon"
    if not row.get("brand_or_league"):
        row["brand_or_league"] = "Pokemon TCG"

    return row


def refresh_research_for_row(row: Dict[str, Any], include_condition: bool, do_pricecharting: bool, do_ebay: bool) -> Tuple[Dict[str, Any], List[str]]:
    updated = dict(row)
    messages = []

    if do_pricecharting and clean_text(updated.get("pricecharting_link")):
        try:
            parsed = parse_pricecharting_page(clean_text(updated.get("pricecharting_link")))
            # Do not overwrite manually entered condition/status/sale fields.
            for key, value in parsed.items():
                if key not in ["condition", "status", "sold_price", "sold_date", "sticker_price"]:
                    updated[key] = value
            messages.append("PriceCharting refreshed.")
        except Exception as exc:
            messages.append(f"PriceCharting refresh failed: {exc}")

    if do_ebay:
        query = clean_text(updated.get("ebay_query")) or build_ebay_query(updated, include_condition=include_condition)
        updated["ebay_query"] = query
        try:
            low = find_low_list(updated, query=query)
            updated.update(low)
            messages.append("Low-list refreshed.")
        except Exception as exc:
            updated["ebay_low_list_url"] = ebay_active_low_search_url(query)
            messages.append(f"Low-list refresh failed: {exc}")
        try:
            sold, sold_error = summarize_sold_comps(updated, query=query)
            updated.update(sold)
            if sold_error:
                messages.append(sold_error)
            else:
                messages.append("Sold comps refreshed.")
        except Exception as exc:
            updated["ebay_sold_search_url"] = ebay_sold_search_url(query)
            messages.append(f"Sold comps refresh failed: {exc}")

    updated["updated_at"] = now_iso()
    return updated, messages


def make_suggested_sticker(parsed: Dict[str, Any], low_list: Dict[str, Any] | None = None, sold: Dict[str, Any] | None = None) -> str:
    candidates = []
    condition = clean_text(parsed.get("condition"))
    if condition.startswith("PSA 10"):
        candidates.append(safe_float(parsed.get("pricecharting_grade_10"), 0))
    elif condition.startswith("PSA 9"):
        candidates.append(safe_float(parsed.get("pricecharting_grade_9"), 0))
    elif condition.startswith("PSA 8"):
        candidates.append(safe_float(parsed.get("pricecharting_grade_8"), 0))
    elif condition.startswith("PSA 7"):
        candidates.append(safe_float(parsed.get("pricecharting_grade_7"), 0))
    else:
        candidates.append(safe_float(parsed.get("pricecharting_raw"), 0))

    if low_list:
        candidates.append(safe_float(low_list.get("ebay_low_list_total"), 0))
    if sold:
        candidates.append(safe_float(sold.get("ebay_sold_median"), 0))

    candidates = [c for c in candidates if c > 0]
    if not candidates:
        return ""
    suggested = max(candidates)
    # Simple show sticker rounding: under $20 round to nearest $1; over $20 round to nearest $5.
    if suggested >= 20:
        suggested = math.ceil(suggested / 5) * 5
    else:
        suggested = math.ceil(suggested)
    return str(round(suggested, 2))


# -----------------------------------------------------------------------------
# UI helpers
# -----------------------------------------------------------------------------

def render_metric_row(df: pd.DataFrame) -> None:
    total = len(df)
    active = int((df["status"].astype(str).str.upper() == "ACTIVE").sum()) if not df.empty else 0
    sold = int((df["status"].astype(str).str.upper() == "SOLD").sum()) if not df.empty else 0
    sold_total = df.loc[df["status"].astype(str).str.upper() == "SOLD", "sold_price"].map(safe_float).sum() if not df.empty else 0
    unpaid = df.loc[
        (df["status"].astype(str).str.upper() == "SOLD")
        & (df["payout_status"].astype(str).str.upper() != "PAID"),
        "final_payout",
    ].map(safe_float).sum() if not df.empty else 0
    paid = df.loc[df["payout_status"].astype(str).str.upper() == "PAID", "final_payout"].map(safe_float).sum() if not df.empty else 0

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Total cards", f"{total:,}")
    c2.metric("Active", f"{active:,}")
    c3.metric("Sold", f"{sold:,}", f"${sold_total:,.2f} sales")
    c4.metric("Owed / unpaid", f"${unpaid:,.2f}")
    c5.metric("Paid out", f"${paid:,.2f}")


def render_pricecharting_preview(parsed: Dict[str, Any]) -> None:
    if not parsed:
        return
    left, right = st.columns([1, 3])
    image_url = clean_text(parsed.get("image_url"))
    with left:
        if image_url:
            st.image(image_url, use_container_width=True)
        else:
            st.info("No image found.")
    with right:
        title = f"{parsed.get('card_name', '')} {('#' + str(parsed.get('card_number'))) if parsed.get('card_number') else ''}".strip()
        st.subheader(title or "Parsed card")
        st.caption(f"Set: {parsed.get('set_name', '') or '—'}")
        p1, p2, p3, p4, p5 = st.columns(5)
        p1.metric("Raw", money_or_blank(parsed.get("pricecharting_raw")) or "—")
        p2.metric("PSA 7", money_or_blank(parsed.get("pricecharting_grade_7")) or "—")
        p3.metric("PSA 8", money_or_blank(parsed.get("pricecharting_grade_8")) or "—")
        p4.metric("PSA 9", money_or_blank(parsed.get("pricecharting_grade_9")) or "—")
        p5.metric("PSA 10", money_or_blank(parsed.get("pricecharting_grade_10")) or "—")


def render_ebay_preview(payload: Dict[str, Any], sold_error: str = "") -> None:
    if not payload:
        return
    st.markdown("#### eBay research")
    low_total = money_or_blank(payload.get("ebay_low_list_total")) or "—"
    sold_median = money_or_blank(payload.get("ebay_sold_median")) or "—"
    sold_avg = money_or_blank(payload.get("ebay_sold_avg")) or "—"
    c1, c2, c3 = st.columns(3)
    c1.metric("Low active list", low_total)
    c2.metric("Sold median", sold_median)
    c3.metric("Sold average", sold_avg)

    low_url = clean_text(payload.get("ebay_low_list_url"))
    if clean_text(payload.get("ebay_low_list_title")):
        st.write(
            f"**Low list:** [{payload.get('ebay_low_list_title')}]({low_url}) — "
            f"{money_or_blank(payload.get('ebay_low_list_price'))} + "
            f"{money_or_blank(payload.get('ebay_low_list_shipping'))} shipping"
        )
    elif low_url:
        st.write(f"**Low list search:** [Open eBay active search]({low_url})")

    sold_url = clean_text(payload.get("ebay_sold_search_url"))
    if sold_error:
        st.warning(sold_error)
    if sold_url:
        st.write(f"**Sold search:** [Open eBay sold comps search]({sold_url})")

    comp_rows = []
    for i in range(1, 4):
        if payload.get(f"ebay_sold_comp_{i}_title"):
            comp_rows.append(
                {
                    "Comp": i,
                    "Title": payload.get(f"ebay_sold_comp_{i}_title"),
                    "Price": money_or_blank(payload.get(f"ebay_sold_comp_{i}_price")),
                    "Date": payload.get(f"ebay_sold_comp_{i}_date"),
                    "URL": payload.get(f"ebay_sold_comp_{i}_url"),
                }
            )
    if comp_rows:
        st.dataframe(pd.DataFrame(comp_rows), hide_index=True, use_container_width=True)


def editor_column_config() -> Dict[str, Any]:
    return {
        "pricecharting_link": st.column_config.LinkColumn("PriceCharting", display_text="Open"),
        "ebay_sold_search_url": st.column_config.LinkColumn("Sold Search", display_text="Sold"),
        "ebay_low_list_url": st.column_config.LinkColumn("Low List", display_text="Low"),
        "image_url": st.column_config.ImageColumn("Image"),
        "status": st.column_config.SelectboxColumn("Status", options=STATUS_OPTIONS),
        "payout_status": st.column_config.SelectboxColumn("Payout", options=PAYOUT_STATUS_OPTIONS),
        "condition": st.column_config.SelectboxColumn("Condition", options=CONDITION_OPTIONS),
        "sticker_price": st.column_config.NumberColumn("Sticker", format="$%.2f", step=1.0),
        "sold_price": st.column_config.NumberColumn("Sold", format="$%.2f", step=1.0),
        "commission_rate": st.column_config.NumberColumn("Commission Rate", format="%.2f", step=0.01),
        "commission_amount": st.column_config.NumberColumn("Commission", format="$%.2f", disabled=True),
        "final_payout": st.column_config.NumberColumn("Final Payout", format="$%.2f", disabled=True),
        "pricecharting_raw": st.column_config.NumberColumn("PC Raw", format="$%.2f", disabled=True),
        "pricecharting_grade_7": st.column_config.NumberColumn("PC PSA 7", format="$%.2f", disabled=True),
        "pricecharting_grade_8": st.column_config.NumberColumn("PC PSA 8", format="$%.2f", disabled=True),
        "pricecharting_grade_9": st.column_config.NumberColumn("PC PSA 9", format="$%.2f", disabled=True),
        "pricecharting_grade_10": st.column_config.NumberColumn("PC PSA 10", format="$%.2f", disabled=True),
        "ebay_sold_avg": st.column_config.NumberColumn("Sold Avg", format="$%.2f", disabled=True),
        "ebay_sold_median": st.column_config.NumberColumn("Sold Median", format="$%.2f", disabled=True),
        "ebay_low_list_total": st.column_config.NumberColumn("Low List Total", format="$%.2f", disabled=True),
    }


def family_view(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for col in FAMILY_VIEW_COLUMNS:
        if col not in out.columns:
            out[col] = ""
    out = out[FAMILY_VIEW_COLUMNS]
    return recalc_financials(out)


# -----------------------------------------------------------------------------
# Main app
# -----------------------------------------------------------------------------

st.title("🤝 Consignment")
st.caption(
    "Separate consignment tracker for family-member cards. This uses a dedicated Google Sheet tab named "
    f"`{WORKSHEET_NAME}` and does not mix with your normal inventory."
)

try:
    df = load_consignment_df()
except Exception as exc:
    st.error(f"Could not load the `{WORKSHEET_NAME}` worksheet: {exc}")
    st.stop()

render_metric_row(df)

add_tab, inventory_tab, payout_tab, export_tab, setup_tab = st.tabs(
    ["Add / Research", "Inventory", "Payouts", "Family View / Export", "Setup / Debug"]
)


with add_tab:
    st.subheader("Add cards fast")
    st.write(
        "Paste a PriceCharting link, choose the condition, then let the app fill the card details and eBay reference fields. "
        "For a large lot, use the bulk loader below and refresh eBay comps later card-by-card."
    )

    with st.form("single_add_form", clear_on_submit=False):
        c1, c2 = st.columns([2, 1])
        with c1:
            pc_url = st.text_input("PriceCharting link", placeholder="https://www.pricecharting.com/game/pokemon-base-set/charizard-4")
            consignor = st.text_input("Consignor", value=DEFAULT_CONSIGNOR)
            condition = st.selectbox("Condition", CONDITION_OPTIONS, index=0)
            condition_notes = st.text_input("Condition notes", placeholder="light holo scratches, tiny front crease, clean back, etc.")
        with c2:
            sticker_price = st.number_input("Sticker price", min_value=0.0, step=1.0, format="%.2f")
            notes = st.text_area("Internal notes", height=88)
            include_condition = st.checkbox("Include condition in eBay query", value=True)
            run_ebay = st.checkbox("Run eBay research now", value=True)

        submitted = st.form_submit_button("Pull details / preview", use_container_width=True)

    if submitted:
        if not pc_url.strip():
            st.error("Add a PriceCharting link first.")
        else:
            with st.spinner("Researching card..."):
                try:
                    parsed = parse_pricecharting_page(pc_url)
                    parsed["condition"] = condition
                    st.session_state["consignment_parsed"] = parsed
                    query = build_ebay_query({**parsed, "condition": condition}, include_condition=include_condition)
                    ebay_payload: Dict[str, Any] = {"ebay_query": query, "ebay_sold_search_url": ebay_sold_search_url(query)}
                    sold_error = ""
                    if run_ebay:
                        try:
                            ebay_payload.update(find_low_list({**parsed, "condition": condition}, query=query))
                        except Exception as exc:
                            ebay_payload["ebay_low_list_url"] = ebay_active_low_search_url(query)
                            st.warning(f"Could not pull active low-list automatically: {exc}")
                        try:
                            sold_payload, sold_error = summarize_sold_comps({**parsed, "condition": condition}, query=query)
                            ebay_payload.update(sold_payload)
                        except Exception as exc:
                            sold_error = f"Could not pull sold comps automatically: {exc}"
                            ebay_payload["ebay_sold_search_url"] = ebay_sold_search_url(query)
                    st.session_state["consignment_ebay"] = ebay_payload
                    st.session_state["consignment_sold_error"] = sold_error
                    suggested = make_suggested_sticker({**parsed, "condition": condition}, ebay_payload, ebay_payload)
                    st.session_state["consignment_suggested_sticker"] = suggested
                    st.success("Preview ready. Review it below, then add it to the sheet.")
                except Exception as exc:
                    st.error(f"Could not pull PriceCharting data: {exc}")

    parsed_preview = st.session_state.get("consignment_parsed", {})
    ebay_preview = st.session_state.get("consignment_ebay", {})
    sold_error_preview = st.session_state.get("consignment_sold_error", "")
    suggested_sticker = st.session_state.get("consignment_suggested_sticker", "")

    if parsed_preview:
        render_pricecharting_preview(parsed_preview)
        render_ebay_preview(ebay_preview, sold_error_preview)

        c1, c2, c3 = st.columns([1, 1, 1])
        with c1:
            if suggested_sticker:
                st.info(f"Suggested sticker reference: **${safe_float(suggested_sticker):,.2f}**")
        with c2:
            use_suggested = st.checkbox("Use suggested sticker when adding", value=False)
        with c3:
            add_now = st.button("Add previewed card to consignment sheet", type="primary", use_container_width=True)

        if add_now:
            final_sticker = suggested_sticker if use_suggested and suggested_sticker else sticker_price
            row = build_row_from_inputs(
                parsed_pc=parsed_preview,
                consignor=consignor,
                condition=condition,
                condition_notes=condition_notes,
                sticker_price=final_sticker,
                notes=notes,
                ebay_payload=ebay_preview,
            )
            append_consignment_rows([row])
            st.success(f"Added {row.get('card_name', 'card')} to `{WORKSHEET_NAME}`.")
            st.cache_data.clear()
            st.rerun()

    st.divider()
    st.subheader("Bulk add from PriceCharting links")
    st.write("Paste one PriceCharting link per line. This is the fastest path for a large vintage lot.")

    with st.form("bulk_add_form", clear_on_submit=False):
        bulk_links = st.text_area("PriceCharting links", height=180, placeholder="One URL per line")
        bc1, bc2, bc3 = st.columns(3)
        with bc1:
            bulk_consignor = st.text_input("Bulk consignor", value=DEFAULT_CONSIGNOR)
        with bc2:
            bulk_condition = st.selectbox("Bulk condition", CONDITION_OPTIONS, index=0, key="bulk_condition")
        with bc3:
            bulk_run_ebay = st.checkbox("Also run eBay low-list/sold research", value=False)
        bulk_condition_notes = st.text_input("Bulk condition notes", placeholder="optional note applied to all pasted links")
        bulk_submitted = st.form_submit_button("Bulk add cards", use_container_width=True)

    if bulk_submitted:
        urls = [normalize_url(x) for x in bulk_links.splitlines() if clean_text(x)]
        urls = list(dict.fromkeys(urls))
        if not urls:
            st.error("Paste at least one PriceCharting link.")
        else:
            rows = []
            failures = []
            progress = st.progress(0)
            status_box = st.empty()
            for i, url in enumerate(urls, start=1):
                status_box.write(f"Processing {i}/{len(urls)}: {url}")
                try:
                    parsed = parse_pricecharting_page(url)
                    parsed["condition"] = bulk_condition
                    ebay_payload = {}
                    if bulk_run_ebay:
                        query = build_ebay_query(parsed, include_condition=True)
                        ebay_payload["ebay_query"] = query
                        try:
                            ebay_payload.update(find_low_list(parsed, query=query, limit=15))
                        except Exception as exc:
                            ebay_payload["ebay_low_list_url"] = ebay_active_low_search_url(query)
                            failures.append(f"{url}: low-list failed — {exc}")
                        try:
                            sold_payload, sold_error = summarize_sold_comps(parsed, query=query, limit=15)
                            ebay_payload.update(sold_payload)
                            if sold_error:
                                failures.append(f"{url}: {sold_error}")
                        except Exception as exc:
                            ebay_payload["ebay_sold_search_url"] = ebay_sold_search_url(query)
                            failures.append(f"{url}: sold comps failed — {exc}")
                    else:
                        query = build_ebay_query(parsed, include_condition=True)
                        ebay_payload = {
                            "ebay_query": query,
                            "ebay_sold_search_url": ebay_sold_search_url(query),
                            "ebay_low_list_url": ebay_active_low_search_url(query),
                        }
                    row = build_row_from_inputs(
                        parsed_pc=parsed,
                        consignor=bulk_consignor,
                        condition=bulk_condition,
                        condition_notes=bulk_condition_notes,
                        sticker_price="",
                        notes="",
                        ebay_payload=ebay_payload,
                    )
                    rows.append(row)
                except Exception as exc:
                    failures.append(f"{url}: {exc}")
                progress.progress(i / len(urls))
                # Be polite with public sites/API calls.
                time.sleep(0.25)

            if rows:
                append_consignment_rows(rows)
                st.success(f"Added {len(rows)} consignment cards.")
            if failures:
                with st.expander(f"Failures / warnings ({len(failures)})"):
                    st.write("\n".join(f"- {f}" for f in failures))
            st.cache_data.clear()
            st.rerun()


with inventory_tab:
    st.subheader("Consignment inventory")
    df = load_consignment_df()

    f1, f2, f3, f4 = st.columns([1, 1, 2, 1])
    with f1:
        status_filter = st.multiselect("Status", STATUS_OPTIONS, default=STATUS_OPTIONS)
    with f2:
        payout_filter = st.multiselect("Payout", PAYOUT_STATUS_OPTIONS, default=PAYOUT_STATUS_OPTIONS)
    with f3:
        text_filter = st.text_input("Search card/set/notes")
    with f4:
        show_cols_mode = st.selectbox("Columns", ["Selling View", "Research View", "All Columns"], index=0)

    filtered = df.copy()
    if status_filter:
        filtered = filtered[filtered["status"].astype(str).str.upper().isin(status_filter)]
    if payout_filter:
        filtered = filtered[filtered["payout_status"].astype(str).str.upper().isin(payout_filter)]
    if text_filter.strip():
        needle = text_filter.strip().lower()
        hay_cols = ["card_name", "set_name", "card_number", "variant", "condition_notes", "notes"]
        mask = pd.Series(False, index=filtered.index)
        for col in hay_cols:
            mask = mask | filtered[col].astype(str).str.lower().str.contains(re.escape(needle), na=False)
        filtered = filtered[mask]

    selling_cols = [
        "consignment_id",
        "image_url",
        "set_name",
        "card_name",
        "card_number",
        "variant",
        "condition",
        "condition_notes",
        "status",
        "sticker_price",
        "sold_date",
        "sold_price",
        "sale_channel",
        "commission_rate",
        "commission_amount",
        "final_payout",
        "payout_status",
        "payout_date",
        "notes",
        "pricecharting_link",
        "ebay_sold_search_url",
        "ebay_low_list_url",
    ]
    research_cols = [
        "consignment_id",
        "image_url",
        "set_name",
        "card_name",
        "card_number",
        "condition",
        "sticker_price",
        "pricecharting_raw",
        "pricecharting_grade_7",
        "pricecharting_grade_8",
        "pricecharting_grade_9",
        "pricecharting_grade_10",
        "ebay_sold_median",
        "ebay_sold_avg",
        "ebay_low_list_total",
        "ebay_query",
        "pricecharting_link",
        "ebay_sold_search_url",
        "ebay_low_list_url",
        "pricecharting_checked_at",
        "ebay_sold_checked_at",
        "ebay_low_list_checked_at",
    ]
    visible_cols = CONSIGNMENT_COLUMNS
    if show_cols_mode == "Selling View":
        visible_cols = selling_cols
    elif show_cols_mode == "Research View":
        visible_cols = research_cols

    st.caption("Edit values directly, then click Save Changes. Commission and payout recalculate automatically.")
    edited = st.data_editor(
        filtered[visible_cols],
        hide_index=True,
        use_container_width=True,
        height=560,
        num_rows="fixed",
        column_config=editor_column_config(),
        disabled=["consignment_id", "created_at", "updated_at", "commission_amount", "final_payout"],
        key="consignment_editor",
    )

    b1, b2, b3 = st.columns([1, 1, 2])
    with b1:
        save_changes = st.button("Save changes", type="primary", use_container_width=True)
    with b2:
        refresh_table = st.button("Reload sheet", use_container_width=True)

    if refresh_table:
        st.cache_data.clear()
        st.rerun()

    if save_changes:
        base = df.copy()
        edited_by_id = edited.set_index("consignment_id", drop=False).to_dict("index") if not edited.empty else {}
        for idx, row in base.iterrows():
            cid = str(row.get("consignment_id"))
            if cid in edited_by_id:
                for col in visible_cols:
                    if col in ["commission_amount", "final_payout"]:
                        continue
                    base.at[idx, col] = edited_by_id[cid].get(col, base.at[idx, col])
                base.at[idx, "updated_at"] = now_iso()
        base = recalc_financials(base)
        write_consignment_df(base)
        st.success("Saved consignment changes.")
        st.cache_data.clear()
        st.rerun()

    st.divider()
    st.subheader("Refresh research for one card")
    if df.empty:
        st.info("No consignment rows yet.")
    else:
        label_map = {
            f"{row.get('consignment_id')} — {row.get('card_name')} #{row.get('card_number')} ({row.get('condition')})": row.get("consignment_id")
            for _, row in df.iterrows()
        }
        selected_label = st.selectbox("Card", list(label_map.keys()))
        rc1, rc2, rc3 = st.columns(3)
        with rc1:
            refresh_pc = st.checkbox("Refresh PriceCharting", value=True)
        with rc2:
            refresh_ebay = st.checkbox("Refresh eBay research", value=True)
        with rc3:
            refresh_include_condition = st.checkbox("Use condition in eBay query", value=True, key="refresh_include_condition")
        if st.button("Refresh selected card", use_container_width=True):
            selected_id = label_map[selected_label]
            base = df.copy()
            idx_matches = base.index[base["consignment_id"].astype(str) == str(selected_id)].tolist()
            if not idx_matches:
                st.error("Could not find selected card.")
            else:
                idx = idx_matches[0]
                with st.spinner("Refreshing research..."):
                    updated, messages = refresh_research_for_row(
                        base.loc[idx].to_dict(),
                        include_condition=refresh_include_condition,
                        do_pricecharting=refresh_pc,
                        do_ebay=refresh_ebay,
                    )
                    for col, value in updated.items():
                        if col in base.columns:
                            base.at[idx, col] = value
                    write_consignment_df(base)
                for message in messages:
                    if "failed" in message.lower() or "blocked" in message.lower() or "error" in message.lower():
                        st.warning(message)
                    else:
                        st.success(message)
                st.cache_data.clear()
                st.rerun()


with payout_tab:
    st.subheader("Payout tracker")
    df = recalc_financials(load_consignment_df())
    unpaid = df[
        (df["status"].astype(str).str.upper() == "SOLD")
        & (df["payout_status"].astype(str).str.upper() != "PAID")
    ].copy()

    if unpaid.empty:
        st.success("No unpaid sold consignment cards right now.")
    else:
        unpaid["pay_now"] = False
        display_cols = [
            "pay_now",
            "consignment_id",
            "card_name",
            "card_number",
            "condition",
            "sold_date",
            "sold_price",
            "commission_amount",
            "final_payout",
            "payout_status",
        ]
        edited_pay = st.data_editor(
            unpaid[display_cols],
            hide_index=True,
            use_container_width=True,
            column_config={
                "pay_now": st.column_config.CheckboxColumn("Pay now"),
                "sold_price": st.column_config.NumberColumn("Sold", format="$%.2f"),
                "commission_amount": st.column_config.NumberColumn("Commission", format="$%.2f"),
                "final_payout": st.column_config.NumberColumn("Final Payout", format="$%.2f"),
                "payout_status": st.column_config.SelectboxColumn("Payout", options=PAYOUT_STATUS_OPTIONS),
            },
            disabled=["consignment_id", "card_name", "card_number", "condition", "sold_date", "sold_price", "commission_amount", "final_payout"],
            key="payout_editor",
        )

        total_selected = edited_pay.loc[edited_pay["pay_now"] == True, "final_payout"].map(safe_float).sum()  # noqa: E712
        st.info(f"Selected payout total: **${total_selected:,.2f}**")
        payout_date = st.date_input("Payout date", value=date.today())
        payout_note = st.text_input("Payout note", placeholder="Venmo, cash, check #, etc.")
        if st.button("Mark selected as paid", type="primary", use_container_width=True):
            selected_ids = edited_pay.loc[edited_pay["pay_now"] == True, "consignment_id"].astype(str).tolist()  # noqa: E712
            if not selected_ids:
                st.error("Select at least one card to mark paid.")
            else:
                base = df.copy()
                mask = base["consignment_id"].astype(str).isin(selected_ids)
                base.loc[mask, "payout_status"] = "PAID"
                base.loc[mask, "payout_date"] = payout_date.isoformat()
                if payout_note.strip():
                    base.loc[mask, "payout_notes"] = payout_note.strip()
                base.loc[mask, "updated_at"] = now_iso()
                write_consignment_df(base)
                st.success(f"Marked {len(selected_ids)} cards as paid. Payout total: ${total_selected:,.2f}")
                st.cache_data.clear()
                st.rerun()


with export_tab:
    st.subheader("Family view / export")
    st.write("This view hides your internal research clutter and is the clean version you can share or export.")
    df = recalc_financials(load_consignment_df())
    clean_df = family_view(df)
    st.dataframe(
        clean_df,
        hide_index=True,
        use_container_width=True,
        height=560,
        column_config={
            "pricecharting_link": st.column_config.LinkColumn("PriceCharting", display_text="Open"),
            "ebay_sold_search_url": st.column_config.LinkColumn("Sold Search", display_text="Sold"),
            "ebay_low_list_url": st.column_config.LinkColumn("Low List", display_text="Low"),
            "sticker_price": st.column_config.NumberColumn("Sticker", format="$%.2f"),
            "sold_price": st.column_config.NumberColumn("Sold", format="$%.2f"),
            "commission_amount": st.column_config.NumberColumn("Commission", format="$%.2f"),
            "final_payout": st.column_config.NumberColumn("Final Payout", format="$%.2f"),
        },
    )
    csv = clean_df.to_csv(index=False).encode("utf-8")
    st.download_button(
        "Download family-view CSV",
        data=csv,
        file_name=f"consignment_family_view_{date.today().isoformat()}.csv",
        mime="text/csv",
        use_container_width=True,
    )


with setup_tab:
    st.subheader("Setup / Debug")
    st.write("Worksheet and API checks.")
    s1, s2, s3 = st.columns(3)
    with s1:
        if st.button("Ensure consignment worksheet exists", use_container_width=True):
            try:
                get_or_create_worksheet()
                st.success(f"`{WORKSHEET_NAME}` worksheet is ready.")
            except Exception as exc:
                st.error(exc)
    with s2:
        if st.button("Test eBay token", use_container_width=True):
            try:
                token = get_ebay_app_token()
                st.success(f"eBay token OK. Token starts with: {token[:10]}...")
            except Exception as exc:
                st.error(exc)
    with s3:
        if st.button("Clear cached data", use_container_width=True):
            st.cache_data.clear()
            st.cache_resource.clear()
            st.success("Cache cleared.")

    with st.expander("Required Streamlit secrets"):
        st.code(
            '''
spreadsheet_id = "YOUR_GOOGLE_SHEET_ID"

[gcp_service_account]
type = "service_account"
project_id = "..."
private_key_id = "..."
private_key = "-----BEGIN PRIVATE KEY-----\\n...\\n-----END PRIVATE KEY-----\\n"
client_email = "...@...iam.gserviceaccount.com"
client_id = "..."
auth_uri = "https://accounts.google.com/o/oauth2/auth"
token_uri = "https://oauth2.googleapis.com/token"
auth_provider_x509_cert_url = "https://www.googleapis.com/oauth2/v1/certs"
client_x509_cert_url = "..."

[ebay]
client_id = "YOUR_EBAY_PROD_CLIENT_ID"
client_secret = "YOUR_EBAY_PROD_CLIENT_SECRET"
            '''.strip(),
            language="toml",
        )

    st.warning(
        "Sold comps are attempted through eBay Marketplace Insights. If your eBay keyset does not have access, "
        "the app will still save a one-click eBay sold-search URL so you can manually validate comps."
    )
