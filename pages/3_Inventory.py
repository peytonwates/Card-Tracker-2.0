from __future__ import annotations

import hashlib
import io
import re
import unicodedata
import uuid
from datetime import date
from difflib import SequenceMatcher
from typing import Any
from urllib.parse import urlparse, urljoin, unquote

import pandas as pd
import requests
import streamlit as st

try:
    from bs4 import BeautifulSoup
except Exception:
    BeautifulSoup = None

from core.business import load_data, refresh_database_cache
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
from core.sheets import get_ws_name, append_rows


st.set_page_config(page_title="Inventory", layout="wide")
st.title("Inventory")

st.caption(
    "Inventory is the source of truth for active cards, graded cards, listed items, sold items, "
    "show sales, and eBay-matched sales."
)


# =========================================================
# Helpers
# =========================================================

BULK_INPUT_COLUMNS = [
    "reference_link",
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
    "purchase_date",
    "purchased_from",
    "purchase_price",
    "shipping",
    "tax",
    "grading_fee",
    "sticker_price",
    "market_value",
    "condition",
    "image_url",
    "quantity",
]

UPLOAD_COLUMN_ALIASES = {
    "reference_link": [
        "reference_link",
        "Reference link",
        "Reference Link",
        "PriceCharting Link",
        "Pricecharting Link",
        "SportsCardsPro Link",
        "URL",
        "Link",
    ],
    "inventory_type": [
        "inventory_type",
        "Inventory Type",
        "inventory type",
        "Inventory",
    ],
    "product_type": [
        "product_type",
        "Product Type",
        "product type",
        "Type",
    ],
    "card_type": [
        "card_type",
        "Card Type",
        "card type",
        "Category",
        "Pokemon / Sports",
    ],
    "brand_or_league": [
        "brand_or_league",
        "Brand/League",
        "Brand / League",
        "Brand",
        "League",
        "Sport",
    ],
    "set_name": [
        "set_name",
        "Set",
        "Set Name",
        "set",
        "Brand/Set",
    ],
    "year": [
        "year",
        "Year",
    ],
    "card_name": [
        "card_name",
        "Card Name",
        "Item Name",
        "Name",
        "Card",
        "Product Name",
    ],
    "card_number": [
        "card_number",
        "Card #",
        "Card Number",
        "Card No",
        "Number",
        "#",
    ],
    "variant": [
        "variant",
        "Variant",
        "Parallel",
    ],
    "card_subtype": [
        "card_subtype",
        "Card Subtype",
        "Subtype",
        "Rarity",
        "Card Rarity",
    ],
    "sealed_product_type": [
        "sealed_product_type",
        "Sealed Product Type",
        "Sealed Type",
        "Sealed Product",
    ],
    "grading_company": [
        "grading_company",
        "Grading Company",
        "Grader",
        "Company",
    ],
    "grade": [
        "grade",
        "Grade",
    ],
    "purchase_date": [
        "purchase_date",
        "Purchase Date",
        "Date Purchased",
        "Purchased Date",
        "Date",
    ],
    "purchased_from": [
        "purchased_from",
        "Purchased From",
        "Purchased from",
        "Seller",
        "Source",
        "Vendor",
    ],
    "purchase_price": [
        "purchase_price",
        "Purchase Price",
        "Cost",
        "My Cost",
        "Price Paid",
        "Buy Price",
    ],
    "shipping": [
        "shipping",
        "Shipping",
        "Shipping Cost",
        "Ship Cost",
    ],
    "tax": [
        "tax",
        "Tax",
        "Sales Tax",
    ],
    "grading_fee": [
        "grading_fee",
        "Grading Fee",
        "Grading Cost",
    ],
    "sticker_price": [
        "sticker_price",
        "Sticker Price",
        "Ask Price",
        "Asking Price",
        "List Price",
        "Marked Price",
    ],
    "market_value": [
        "market_value",
        "Market Value",
        "Market Price",
        "Current Value",
        "Value",
        "Comps",
    ],
    "condition": [
        "condition",
        "Condition",
    ],
    "image_url": [
        "image_url",
        "Image URL",
        "Image",
        "Photo URL",
    ],
    "quantity": [
        "quantity",
        "Quantity",
        "Qty",
        "QTY",
        "Count",
    ],
}

IGNORED_BULK_COLUMNS = {
    "inventory_id",
    "inventory_status",
    "created_at",
    "updated_at",
    "transaction_type",
    "platform",
    "list_date",
    "list_price",
    "sold_date",
    "sold_price",
    "fees",
    "shipping_charged",
    "fees_total",
    "net_proceeds",
    "profit",
    "sale_channel",
    "sale_notes",
    "show_id",
    "show_name",
    "sold_transaction_id",
    "sold_created_at",
    "sold_updated_at",
    "ebay_item_id",
    "ebay_listing_id",
    "ebay_listing_url",
    "ebay_listing_status",
    "ebay_order_id",
    "ebay_line_item_id",
    "ebay_transaction_id",
    "ebay_payout_id",
    "ebay_last_sync_at",
    "ebay_sku",
    "total_price",
    "total_cost",
    "market_price_updated_at",
}


def _safe_df(df: pd.DataFrame | None) -> pd.DataFrame:
    return pd.DataFrame() if df is None else df.copy()


def _date_series(df: pd.DataFrame, col: str) -> pd.Series:
    if df.empty or col not in df.columns:
        return pd.Series(pd.NaT, index=df.index)
    return pd.to_datetime(df[col], errors="coerce")


def _clean_or_blank(x) -> str:
    try:
        if pd.isna(x):
            return ""
    except Exception:
        pass
    return clean_text(x)


def _normalize_header_name(x: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(x or "").strip().lower())


def _find_matching_column(columns, aliases):
    wanted = {_normalize_header_name(a) for a in aliases}
    for col in columns:
        if _normalize_header_name(col) in wanted:
            return col
    return None


def _option_index(options: list[str], value: str, default: int = 0) -> int:
    value = _clean_or_blank(value)

    if value in options:
        return options.index(value)

    return default


def _condition_options() -> list[str]:
    out = list(CONDITION_OPTIONS)

    for extra in ["Sealed", "Graded"]:
        if extra not in out:
            out.append(extra)

    return out


def _normalize_product_type_value(x: str) -> str:
    val = _clean_or_blank(x).lower()

    if val in {"card", "raw", "raw card", "single", "singles"}:
        return "Card"

    if val in {"sealed", "sealed product", "product"}:
        return "Sealed"

    if val in {"graded", "graded card", "slab", "slabbed", "slabbed card"}:
        return "Graded Card"

    raw = _clean_or_blank(x)
    return raw


def _normalize_inventory_type_value(x: str) -> str:
    val = _clean_or_blank(x).lower().replace("_", " ").replace("-", " ")

    if val in {"show inventory", "show", "showinventory"}:
        return "Show Inventory"

    if val in {"personal inventory", "personal", "personalinventory"}:
        return "Personal Inventory"

    return _clean_or_blank(x)


def _normalize_card_type_value(x: str) -> str:
    val = _clean_or_blank(x).lower()

    if val in {"pokemon", "pokémon", "pokemon tcg", "pokémon tcg"}:
        return "Pokemon"

    if val in {"sports", "sport"}:
        return "Sports"

    return _clean_or_blank(x)


def _normalize_condition_value(x: str) -> str:
    raw = _clean_or_blank(x)

    if not raw:
        return ""

    key = _normalize_header_name(raw)

    aliases = {
        "nm": "Near Mint",
        "nearmint": "Near Mint",
        "lp": "Lightly Played",
        "lightplayed": "Lightly Played",
        "lightlyplayed": "Lightly Played",
        "mp": "Moderately Played",
        "moderatelyplayed": "Moderately Played",
        "hp": "Heavily Played",
        "heavilyplayed": "Heavily Played",
        "dmg": "Damaged",
        "damaged": "Damaged",
        "sealed": "Sealed",
        "graded": "Graded",
    }

    if key in aliases:
        return aliases[key]

    for option in _condition_options():
        if _normalize_header_name(option) == key:
            return option

    return raw


def _normalize_grading_company_value(x: str) -> str:
    raw = _clean_or_blank(x)
    val = raw.upper()

    if val == "PSA":
        return "PSA"

    if val == "CGC":
        return "CGC"

    if val in {"BGS", "BECKETT"}:
        return "Beckett"

    return raw


def _coerce_purchase_date(x) -> str:
    txt = _clean_or_blank(x)

    if not txt:
        return ""

    parsed = pd.to_datetime(txt, errors="coerce")

    if pd.isna(parsed):
        return ""

    return str(parsed.date())


def _coerce_quantity(x) -> int:
    txt = _clean_or_blank(x)

    if not txt:
        return 1

    try:
        q = int(float(str(txt).replace(",", "")))
        return max(1, q)
    except Exception:
        return 1


def _money_value(x) -> float:
    return round(to_money(x), 2)


def _money_input_bad(x) -> bool:
    txt = _clean_or_blank(x)

    if txt == "":
        return False

    cleaned = re.sub(r"[^0-9.\-]", "", txt)
    if cleaned in {"", ".", "-", "-."}:
        return True

    try:
        float(cleaned)
        return False
    except Exception:
        return True


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
        "ebay_item_id",
        "ebay_listing_id",
        "ebay_listing_status",
        "ebay_order_id",
        "ebay_line_item_id",
        "ebay_transaction_id",
        "ebay_payout_id",
        "ebay_last_sync_at",
        "reference_link",
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
    sealed_product_type: str = "",
    image_url: str = "",
    grading_fee: float = 0.0,
    market_value: float = 0.0,
    notes: str = "",
) -> dict:
    purchase_price = _money_value(purchase_price)
    shipping = _money_value(shipping)
    tax = _money_value(tax)
    grading_fee = _money_value(grading_fee)
    sticker_price = _money_value(sticker_price)
    market_value = _money_value(market_value)

    total_price = round(purchase_price + shipping + tax, 2)
    total_cost = round(total_price + grading_fee, 2)

    row = {c: "" for c in INVENTORY_COLUMNS}
    row.update(
        {
            "inventory_id": str(uuid.uuid4())[:8],
            "image_url": _clean_or_blank(image_url),
            "inventory_type": _normalize_inventory_type_value(inventory_type),
            "product_type": _normalize_product_type_value(product_type),
            "inventory_status": STATUS_ACTIVE,
            "sealed_product_type": _clean_or_blank(sealed_product_type),
            "card_type": _normalize_card_type_value(card_type),
            "brand_or_league": _clean_or_blank(brand_or_league),
            "set_name": _clean_or_blank(set_name),
            "year": _clean_or_blank(year),
            "card_name": _clean_or_blank(card_name),
            "card_number": _clean_or_blank(card_number),
            "variant": _clean_or_blank(variant),
            "card_subtype": _clean_or_blank(card_subtype),
            "grading_company": _normalize_grading_company_value(grading_company),
            "grade": _clean_or_blank(grade),
            "reference_link": _clean_or_blank(reference_link),
            "purchase_date": _coerce_purchase_date(purchase_date_value),
            "purchased_from": _clean_or_blank(purchased_from),
            "purchase_price": purchase_price,
            "shipping": shipping,
            "tax": tax,
            "total_price": total_price,
            "grading_fee": grading_fee,
            "total_cost": total_cost,
            "condition": _normalize_condition_value(condition),
            "created_at": now_iso(),
            "updated_at": "",
            "market_value": market_value,
            "market_price_updated_at": now_iso() if market_value > 0 else "",
            "sticker_price": sticker_price,
            "notes": _clean_or_blank(notes),
        }
    )
    return row


def _append_inventory_rows(rows: list[dict]) -> None:
    append_rows(
        get_ws_name("inventory_worksheet", "inventory"),
        INVENTORY_COLUMNS,
        rows,
    )


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
# PriceCharting / SportsCardsPro detail pull
# =========================================================

SPORT_TOKENS = {
    "football": "Football",
    "basketball": "Basketball",
    "baseball": "Baseball",
    "hockey": "Hockey",
    "soccer": "Soccer",
    "golf": "Golf",
    "ufc": "UFC",
    "wrestling": "Wrestling",
}

SEALED_TYPE_KEYWORDS = {
    "elite-trainer-box": "Elite Trainer Box",
    "etb": "Elite Trainer Box",
    "booster-box": "Booster Box",
    "booster-display": "Booster Box",
    "booster-bundle": "Booster Bundle",
    "blister": "Blister Pack",
    "tech-sticker-collection": "Tech Sticker Collection",
    "collection-box": "Collection Box",
    "premium-collection": "Premium Collection Box",
}


def _title_case_from_slug(slug: str) -> str:
    slug = unquote(str(slug or ""))
    slug = slug.replace("&", " & ")
    words = [w for w in slug.replace("-", " ").replace("_", " ").split() if w]
    return " ".join(words).title()


def _find_best_title(soup) -> str:
    if soup is None:
        return ""

    for meta in [
        soup.find("meta", property="og:title"),
        soup.find("meta", attrs={"name": "twitter:title"}),
    ]:
        if meta and meta.get("content"):
            return meta["content"].strip()

    h1 = soup.find("h1")
    if h1 and h1.get_text(strip=True):
        return h1.get_text(" ", strip=True)

    if soup.title and soup.title.string:
        return soup.title.string.strip()

    return ""


def _find_best_image(soup, base_url: str) -> str:
    if soup is None:
        return ""

    candidates = []

    for meta in [
        soup.find("meta", property="og:image"),
        soup.find("meta", attrs={"name": "twitter:image"}),
    ]:
        if meta and meta.get("content"):
            candidates.append(meta["content"].strip())

    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        if "storage.googleapis.com/images.pricecharting.com" in href:
            candidates.append(href)

    for img in soup.find_all("img", src=True):
        src = (img.get("src") or "").strip()
        if "storage.googleapis.com/images.pricecharting.com" in src:
            candidates.append(src)

    for img in soup.find_all("img", src=True):
        src = (img.get("src") or "").strip()
        if src and "/images/pokemon-sets/" not in src:
            candidates.append(src)

    for url in candidates:
        if not url:
            continue
        if url.startswith("//"):
            return "https:" + url
        return urljoin(base_url, url)

    return ""


def _parse_set_slug(set_slug: str) -> dict:
    tokens = [t for t in str(set_slug or "").split("-") if t]
    year = ""

    for token in tokens:
        if re.fullmatch(r"(19|20)\d{2}", token):
            year = token
            break

    if tokens and tokens[0].lower() == "pokemon":
        remaining = tokens[1:]
        set_name = _title_case_from_slug("-".join(remaining))
        return {
            "card_type": "Pokemon",
            "brand_or_league": "Pokemon TCG",
            "set_name": set_name,
            "year": year,
        }

    sport_token = tokens[0].lower() if tokens else ""
    if sport_token in SPORT_TOKENS:
        brand_or_league = SPORT_TOKENS[sport_token]
        remaining = tokens[1:]

        if remaining and remaining[0].lower() == "cards":
            remaining = remaining[1:]

        remaining_no_year = [t for t in remaining if t != year]
        set_name = _title_case_from_slug("-".join(remaining_no_year))
        return {
            "card_type": "Sports",
            "brand_or_league": brand_or_league,
            "set_name": set_name,
            "year": year,
        }

    return {
        "card_type": "",
        "brand_or_league": "",
        "set_name": _title_case_from_slug(set_slug),
        "year": year,
    }


def _looks_like_single_card_slug(card_slug: str) -> bool:
    return bool(re.search(r"-(\d+[A-Za-z0-9]*)$", str(card_slug or "")))


def _infer_sealed_type(slug: str, title: str) -> str:
    combined = f"{slug or ''} {title or ''}".lower()

    for key, value in SEALED_TYPE_KEYWORDS.items():
        if key in combined:
            return value

    if "elite trainer box" in combined:
        return "Elite Trainer Box"
    if "booster box" in combined:
        return "Booster Box"
    if "booster bundle" in combined:
        return "Booster Bundle"
    if "tech sticker collection" in combined:
        return "Tech Sticker Collection"
    if "premium collection" in combined:
        return "Premium Collection Box"
    if "collection box" in combined:
        return "Collection Box"
    if "blister" in combined:
        return "Blister Pack"

    return ""


def _parse_card_from_slug(card_slug: str) -> dict:
    slug = unquote(str(card_slug or "").strip())
    slug = slug.split("?")[0].strip("/")

    number = ""
    name_slug = slug

    m = re.search(r"-(\d+[A-Za-z0-9]*)$", slug)
    if m:
        number = m.group(1)
        name_slug = slug[: m.start()]

    tokens = [t for t in name_slug.split("-") if t]

    variant_tokens = []
    while tokens and tokens[-1].lower() in {
        "ex",
        "gx",
        "v",
        "vmax",
        "vstar",
        "holo",
        "reverse",
        "silver",
        "gold",
        "promo",
    }:
        variant_tokens.insert(0, tokens.pop())

    card_name = _title_case_from_slug("-".join(tokens))
    variant = " ".join(variant_tokens).upper() if variant_tokens else ""

    # Make common Pokemon suffixes look natural.
    variant = (
        variant.replace("EX", "ex")
        .replace("GX", "GX")
        .replace("VMAX", "VMAX")
        .replace("VSTAR", "VSTAR")
    )

    return {
        "card_name": card_name,
        "card_number": number,
        "variant": variant,
    }


def _parse_card_from_title(title: str) -> dict:
    out = {"card_name": "", "card_number": "", "variant": ""}

    title = _clean_or_blank(title)
    if not title:
        return out

    # Examples:
    # "Pikachu ex #247 Prices | Pokemon Surging Sparks"
    # "Pikachu ex 247 Pokemon Surging Sparks"
    title = re.sub(r"\s+Prices?\s*\|.*$", "", title, flags=re.IGNORECASE).strip()
    title = re.sub(r"\s+Pokemon Card Prices.*$", "", title, flags=re.IGNORECASE).strip()

    m = re.search(r"#\s*([A-Za-z0-9\-]+)", title)
    if m:
        out["card_number"] = m.group(1).strip()
        name_part = title[: m.start()].strip()
    else:
        name_part = title

    for sep in [" - ", " – ", " | "]:
        if sep in name_part:
            name_part = name_part.split(sep)[0].strip()

    tokens = name_part.split()
    if tokens and tokens[-1].lower() in {"ex", "gx", "v", "vmax", "vstar", "holo", "silver"}:
        out["variant"] = tokens[-1]
        name_part = " ".join(tokens[:-1]).strip()

    out["card_name"] = name_part.strip()
    return out


@st.cache_data(show_spinner=False, ttl=60 * 60 * 12)
def fetch_reference_details(reference_link: str) -> dict:
    url = _clean_or_blank(reference_link)

    result = {
        "image_url": "",
        "product_type": "Card",
        "sealed_product_type": "",
        "card_type": "",
        "brand_or_league": "",
        "set_name": "",
        "year": "",
        "card_name": "",
        "card_number": "",
        "variant": "",
        "card_subtype": "",
        "reference_link": url,
    }

    if not url:
        return result

    parsed = urlparse(url)
    host = (parsed.netloc or "").lower()
    path_parts = [p for p in (parsed.path or "").split("/") if p]

    if "pricecharting.com" not in host and "sportscardspro.com" not in host:
        return result

    if len(path_parts) >= 3 and path_parts[0].lower() == "game":
        set_slug = path_parts[1]
        card_slug = path_parts[2]

        result.update(_parse_set_slug(set_slug))

        title_card = _parse_card_from_slug(card_slug)
        result.update({k: v for k, v in title_card.items() if v})

        sealed_type = _infer_sealed_type(card_slug, "")

        if sealed_type or not _looks_like_single_card_slug(card_slug):
            result["product_type"] = "Sealed"
            result["sealed_product_type"] = sealed_type
            if not result["card_name"]:
                result["card_name"] = _title_case_from_slug(card_slug)

    if BeautifulSoup is not None:
        try:
            headers = {"User-Agent": "Mozilla/5.0 (CardTracker; Streamlit)"}
            response = requests.get(url, headers=headers, timeout=15)
            response.raise_for_status()

            soup = BeautifulSoup(response.text, "html.parser")
            page_title = _find_best_title(soup)
            image_url = _find_best_image(soup, url)

            if image_url:
                result["image_url"] = image_url

            title_details = _parse_card_from_title(page_title)

            for key in ["card_name", "card_number", "variant"]:
                if not result.get(key) and title_details.get(key):
                    result[key] = title_details[key]

            sealed_type = _infer_sealed_type(
                path_parts[2] if len(path_parts) >= 3 else "",
                page_title,
            )

            if sealed_type:
                result["product_type"] = "Sealed"
                result["sealed_product_type"] = sealed_type
                if not result["card_name"]:
                    result["card_name"] = sealed_type

        except Exception:
            # Slug parsing above is usually good enough; do not block entry if scraping fails.
            pass

    if result.get("card_type") == "Pokemon" and not result.get("brand_or_league"):
        result["brand_or_league"] = "Pokemon TCG"

    return result


# =========================================================
# Bulk upload helpers
# =========================================================

def get_upload_template_df() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Reference Link": "https://www.pricecharting.com/game/pokemon-surging-sparks/pikachu-ex-247",
                "Inventory Type": "Show Inventory",
                "Product Type": "Card",
                "Card Type": "Pokemon",
                "Brand/League": "Pokemon TCG",
                "Set": "Surging Sparks",
                "Year": "2024",
                "Card Name": "Pikachu",
                "Card #": "247",
                "Variant": "ex",
                "Card Subtype": "Illustration Rare",
                "Sealed Product Type": "",
                "Grading Company": "",
                "Grade": "",
                "Purchase Date": "2026-06-16",
                "Purchased From": "Card Show",
                "Purchase Price": 18.00,
                "Shipping": 0.00,
                "Tax": 1.53,
                "Grading Fee": 0.00,
                "Sticker Price": 25.00,
                "Market Value": "",
                "Condition": "Near Mint",
                "Image URL": "",
                "Quantity": 1,
            }
        ]
    )


def _read_upload_file(uploaded) -> pd.DataFrame:
    name = (uploaded.name or "").lower()

    if name.endswith(".csv"):
        return pd.read_csv(uploaded, dtype=object)

    return pd.read_excel(uploaded, dtype=object)


def _known_upload_header_norms() -> set[str]:
    out = set()

    for aliases in UPLOAD_COLUMN_ALIASES.values():
        for alias in aliases:
            out.add(_normalize_header_name(alias))

    for col in IGNORED_BULK_COLUMNS:
        out.add(_normalize_header_name(col))

    return out


def _unexpected_upload_columns(df: pd.DataFrame) -> list[str]:
    known = _known_upload_header_norms()
    unexpected = []

    for col in df.columns:
        if _normalize_header_name(col) not in known:
            unexpected.append(str(col))

    return unexpected


def _ignored_present_columns(df: pd.DataFrame) -> list[str]:
    ignored = {_normalize_header_name(c) for c in IGNORED_BULK_COLUMNS}
    present = []

    for col in df.columns:
        if _normalize_header_name(col) in ignored:
            present.append(str(col))

    return present


def normalize_uploaded_inventory_df(
    upload_df: pd.DataFrame,
    *,
    default_inventory_type: str,
    default_product_type: str,
    default_card_type: str,
    default_condition: str,
    default_brand_or_league: str,
    default_purchase_date,
    default_purchased_from: str,
) -> pd.DataFrame:
    if upload_df is None or upload_df.empty:
        return pd.DataFrame(columns=["source_row"] + BULK_INPUT_COLUMNS)

    df = upload_df.copy()
    rename_map = {}

    for internal, aliases in UPLOAD_COLUMN_ALIASES.items():
        match = _find_matching_column(df.columns, aliases)
        if match:
            rename_map[match] = internal

    df = df.rename(columns=rename_map)

    for col in BULK_INPUT_COLUMNS:
        if col not in df.columns:
            df[col] = ""

    df = df[BULK_INPUT_COLUMNS].copy()

    # Drop fully blank data rows, but keep any row that has even one upload field.
    nonempty_mask = df.apply(
        lambda r: any(_clean_or_blank(v) for v in r.tolist()),
        axis=1,
    )
    df = df[nonempty_mask].copy()

    if df.empty:
        return pd.DataFrame(columns=["source_row"] + BULK_INPUT_COLUMNS)

    # Excel row number = dataframe position + header row + one-indexing
    df.insert(0, "source_row", [int(i) + 2 for i in df.index])

    for col in [
        "reference_link",
        "set_name",
        "year",
        "card_name",
        "card_number",
        "variant",
        "card_subtype",
        "sealed_product_type",
        "grade",
        "purchased_from",
        "image_url",
    ]:
        df[col] = df[col].apply(_clean_or_blank)

    df["inventory_type"] = df["inventory_type"].apply(_normalize_inventory_type_value)
    df["inventory_type"] = df["inventory_type"].replace("", default_inventory_type)

    df["product_type"] = df["product_type"].apply(_normalize_product_type_value)
    df["product_type"] = df["product_type"].replace("", default_product_type)

    df["card_type"] = df["card_type"].apply(_normalize_card_type_value)
    df["card_type"] = df["card_type"].replace("", default_card_type)

    df["brand_or_league"] = df["brand_or_league"].apply(_clean_or_blank)
    df["brand_or_league"] = df["brand_or_league"].replace("", default_brand_or_league)

    df["condition"] = df["condition"].apply(_normalize_condition_value)
    df["condition"] = df["condition"].replace("", default_condition)

    df["grading_company"] = df["grading_company"].apply(_normalize_grading_company_value)

    df["purchase_date"] = df["purchase_date"].apply(
        lambda x: _coerce_purchase_date(x) or _coerce_purchase_date(default_purchase_date)
    )

    df["purchased_from"] = df["purchased_from"].apply(_clean_or_blank)
    df["purchased_from"] = df["purchased_from"].replace("", _clean_or_blank(default_purchased_from))

    for col in [
        "purchase_price",
        "shipping",
        "tax",
        "grading_fee",
        "sticker_price",
        "market_value",
    ]:
        df[col] = df[col].apply(_money_value)

    df["quantity"] = df["quantity"].apply(_coerce_quantity)

    return df.reset_index(drop=True)


def _validate_bulk_preview(preview_df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, list[dict]]:
    if preview_df is None or preview_df.empty:
        return (
            pd.DataFrame(),
            pd.DataFrame(columns=["source_row", "card_name", "errors"]),
            pd.DataFrame(columns=["source_row", "card_name", "warnings"]),
            [],
        )

    rows = []
    errors = []
    warnings = []
    rows_to_insert = []

    for _, r in preview_df.iterrows():
        row_errors = []
        row_warnings = []

        source_row = int(to_money(r.get("source_row")) or 0)
        inventory_type = _normalize_inventory_type_value(r.get("inventory_type"))
        product_type = _normalize_product_type_value(r.get("product_type"))
        card_type = _normalize_card_type_value(r.get("card_type"))
        condition = _normalize_condition_value(r.get("condition"))
        purchase_date_value = _coerce_purchase_date(r.get("purchase_date"))
        quantity = _coerce_quantity(r.get("quantity"))

        card_name = _clean_or_blank(r.get("card_name"))
        reference_link = _clean_or_blank(r.get("reference_link"))
        set_name = _clean_or_blank(r.get("set_name"))
        purchase_price = _money_value(r.get("purchase_price"))
        shipping = _money_value(r.get("shipping"))
        tax = _money_value(r.get("tax"))
        grading_fee = _money_value(r.get("grading_fee"))
        total_price = round(purchase_price + shipping + tax, 2)
        total_cost = round(total_price + grading_fee, 2)

        if inventory_type not in INVENTORY_TYPE_OPTIONS:
            row_errors.append(f"Invalid inventory_type: {inventory_type or 'blank'}")

        if product_type not in PRODUCT_TYPE_OPTIONS:
            row_errors.append(f"Invalid product_type: {product_type or 'blank'}")

        if card_type not in CARD_TYPE_OPTIONS:
            row_errors.append(f"Invalid card_type: {card_type or 'blank'}")

        if not card_name and not reference_link:
            row_errors.append("Missing card_name or reference_link")

        if not purchase_date_value:
            row_errors.append("Missing or invalid purchase_date")

        if not condition:
            row_errors.append("Missing condition")

        if quantity <= 0:
            row_errors.append("Quantity must be at least 1")

        if product_type == "Sealed" and not _clean_or_blank(r.get("sealed_product_type")):
            row_warnings.append("Sealed product has no sealed_product_type")

        if product_type == "Graded Card" and not _clean_or_blank(r.get("grade")):
            row_warnings.append("Graded Card has no grade")

        if product_type == "Graded Card" and not _clean_or_blank(r.get("grading_company")):
            row_warnings.append("Graded Card has no grading_company")

        if purchase_price == 0 and total_cost == 0:
            row_warnings.append("Total cost is $0")

        if not set_name:
            row_warnings.append("Set is blank")

        status = "Ready" if not row_errors else "Blocked"

        preview_row = {
            "source_row": source_row,
            "row_status": status,
            "errors": "; ".join(row_errors),
            "warnings": "; ".join(row_warnings),
            "quantity": quantity,
            "inventory_type": inventory_type,
            "product_type": product_type,
            "card_type": card_type,
            "brand_or_league": _clean_or_blank(r.get("brand_or_league")),
            "set_name": set_name,
            "year": _clean_or_blank(r.get("year")),
            "card_name": card_name,
            "card_number": _clean_or_blank(r.get("card_number")),
            "variant": _clean_or_blank(r.get("variant")),
            "card_subtype": _clean_or_blank(r.get("card_subtype")),
            "sealed_product_type": _clean_or_blank(r.get("sealed_product_type")),
            "grading_company": _normalize_grading_company_value(r.get("grading_company")),
            "grade": _clean_or_blank(r.get("grade")),
            "purchase_date": purchase_date_value,
            "purchased_from": _clean_or_blank(r.get("purchased_from")),
            "purchase_price": purchase_price,
            "shipping": shipping,
            "tax": tax,
            "total_price": total_price,
            "grading_fee": grading_fee,
            "total_cost": total_cost,
            "market_value": _money_value(r.get("market_value")),
            "sticker_price": _money_value(r.get("sticker_price")),
            "condition": condition,
            "reference_link": reference_link,
            "image_url": _clean_or_blank(r.get("image_url")),
        }

        rows.append(preview_row)

        if row_errors:
            errors.append(
                {
                    "source_row": source_row,
                    "card_name": card_name,
                    "errors": "; ".join(row_errors),
                }
            )

        if row_warnings:
            warnings.append(
                {
                    "source_row": source_row,
                    "card_name": card_name,
                    "warnings": "; ".join(row_warnings),
                }
            )

        if not row_errors:
            for _ in range(quantity):
                rows_to_insert.append(
                    _make_inventory_row(
                        inventory_type=inventory_type,
                        product_type=product_type,
                        card_type=card_type,
                        brand_or_league=_clean_or_blank(r.get("brand_or_league")),
                        set_name=set_name,
                        year=_clean_or_blank(r.get("year")),
                        card_name=card_name,
                        card_number=_clean_or_blank(r.get("card_number")),
                        variant=_clean_or_blank(r.get("variant")),
                        card_subtype=_clean_or_blank(r.get("card_subtype")),
                        grading_company=_normalize_grading_company_value(r.get("grading_company")),
                        grade=_clean_or_blank(r.get("grade")),
                        reference_link=reference_link,
                        purchase_date_value=purchase_date_value,
                        purchased_from=_clean_or_blank(r.get("purchased_from")),
                        purchase_price=purchase_price,
                        shipping=shipping,
                        tax=tax,
                        sticker_price=_money_value(r.get("sticker_price")),
                        condition=condition,
                        sealed_product_type=_clean_or_blank(r.get("sealed_product_type")),
                        image_url=_clean_or_blank(r.get("image_url")),
                        grading_fee=grading_fee,
                        market_value=_money_value(r.get("market_value")),
                    )
                )

    return (
        pd.DataFrame(rows),
        pd.DataFrame(errors),
        pd.DataFrame(warnings),
        rows_to_insert,
    )




# =========================================================
# Collectr Bulk Add reconciliation helpers
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
MATCHING_ENGINE_VERSION = "v6-availability-aware"

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


def _file_bytes(uploaded_file) -> bytes:
    if hasattr(uploaded_file, "getvalue"):
        return uploaded_file.getvalue()
    current_position = uploaded_file.tell()
    uploaded_file.seek(0)
    data = uploaded_file.read()
    uploaded_file.seek(current_position)
    return data


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

    def candidate_preference_key(record: dict[str, Any]) -> tuple[Any, ...]:
        """Sort one Collectr row's candidates from strongest to weakest.

        Purchase date is only a tie-breaker for genuinely identical copies. The
        match score and exact identity fields always take priority.
        """
        purchase_preference = (
            record["purchase_rank"]
            if keep_newest
            else -record["purchase_rank"]
        )
        return (
            record["score"],
            int(record["exact_number"]),
            int(record["exact_name"]),
            int(record["exact_set"]),
            purchase_preference,
            -record["inventory_index"],
        )

    qualifying_by_collectr: dict[int, list[dict[str, Any]]] = {}
    for collectr_idx in collectr_work.index:
        qualifying = [
            record
            for record in pairs_by_collectr.get(int(collectr_idx), [])
            if record["score"] >= auto_match_threshold
        ]
        qualifying.sort(key=candidate_preference_key, reverse=True)
        qualifying_by_collectr[int(collectr_idx)] = qualifying

    # Match the most constrained Collectr copies first. Then use an augmenting
    # path whenever the preferred inventory row is already occupied. This is
    # the key duplicate fix: a second copy does not stop at the already-used
    # best candidate; it can move the first copy to another valid inventory row
    # and consume the remaining copy instead.
    collectr_match_order = sorted(
        [
            int(collectr_idx)
            for collectr_idx in collectr_work.index
            if qualifying_by_collectr.get(int(collectr_idx))
        ],
        key=lambda collectr_idx: (
            len(qualifying_by_collectr[collectr_idx]),
            -qualifying_by_collectr[collectr_idx][0]["score"],
            collectr_idx,
        ),
    )

    assigned_collectr: dict[int, dict[str, Any]] = {}
    assigned_inventory: dict[int, dict[str, Any]] = {}

    def try_assign_collectr(
        collectr_idx: int,
        seen_collectr: set[int],
        seen_inventory: set[int],
    ) -> bool:
        if collectr_idx in seen_collectr:
            return False
        seen_collectr.add(collectr_idx)

        for record in qualifying_by_collectr.get(collectr_idx, []):
            inventory_idx = int(record["inventory_index"])
            if inventory_idx in seen_inventory:
                continue
            seen_inventory.add(inventory_idx)

            existing = assigned_inventory.get(inventory_idx)
            if existing is None:
                assigned_collectr[collectr_idx] = record
                assigned_inventory[inventory_idx] = record
                return True

            existing_collectr_idx = int(existing["collectr_index"])
            if try_assign_collectr(
                existing_collectr_idx,
                seen_collectr,
                seen_inventory,
            ):
                assigned_collectr[collectr_idx] = record
                assigned_inventory[inventory_idx] = record
                return True

        return False

    for collectr_idx in collectr_match_order:
        try_assign_collectr(collectr_idx, set(), set())

    # Rebuild the inventory-side map from the final Collectr assignments. During
    # an augmenting path, a Collectr row can move to a different inventory row;
    # rebuilding prevents an old inventory key from retaining a stale record.
    assigned_inventory = {
        int(record["inventory_index"]): record
        for record in assigned_collectr.values()
    }

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
            status = "UNMATCHED COPY - ALL COMPATIBLE INVENTORY COPIES USED"
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


def _bulk_collectr_inventory_scope(inventory: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Inventory records that should count as an already-owned copy during Bulk Add.

    Unlike the Shows physical reconciliation, GRADING is included here. If a card is
    already in your database and is currently at grading, a Collectr row for that card
    should not create a second inventory record.

    SOLD inventory is excluded because a newly acquired replacement copy should be
    allowed to enter inventory even if an older copy of the same card was sold.
    """
    inv_scope = _safe_df(inventory)
    if inv_scope.empty:
        return inv_scope, pd.DataFrame(columns=["scope_reason", "count"])

    for col in [
        "inventory_id",
        "inventory_status",
        "inventory_type",
        "product_type",
        "card_type",
        "brand_or_league",
        "sealed_product_type",
    ]:
        if col not in inv_scope.columns:
            inv_scope[col] = ""

    inv_scope["inventory_id"] = inv_scope["inventory_id"].astype(str).str.strip()
    inv_scope["inventory_status"] = (
        inv_scope["inventory_status"].astype(str).str.upper().str.strip()
    )

    def scope_reason(row: pd.Series) -> str:
        status = _text(row.get("inventory_status")).upper()
        if status not in {STATUS_ACTIVE, STATUS_LISTED, STATUS_GRADING}:
            return "Not currently owned"

        ownership_text = " ".join(
            _norm(row.get(col))
            for col in [
                "inventory_type",
                "ownership_type",
                "owner_type",
                "inventory_owner",
                "portfolio_name",
            ]
        )
        if "consign" in ownership_text or _truthy(row.get("is_consignment"), default=False):
            return "Consignment"
        if "personal" in ownership_text or _truthy(row.get("is_personal"), default=False):
            return "Personal inventory"

        if _has_value(row.get("sealed_product_type")):
            return "Sealed / non-card inventory"

        product_text = " ".join(
            _norm(row.get(col))
            for col in ["product_type", "card_type", "inventory_type"]
        )
        if any(
            _contains_whole_term(product_text, term)
            for term in [
                "sealed",
                "booster box",
                "booster bundle",
                "elite trainer box",
                "collection box",
                "tin",
            ]
        ):
            return "Sealed / non-card inventory"

        pokemon_text = " ".join(
            _norm(row.get(col))
            for col in [
                "brand_or_league",
                "card_type",
                "category",
                "game",
                "franchise",
            ]
        )
        if "pokemon" not in pokemon_text:
            return "Sports / non-Pokémon"

        return "Existing owned Pokémon card"

    inv_scope["__bulk_scope_reason"] = inv_scope.apply(scope_reason, axis=1)
    eligible = inv_scope[
        inv_scope["__bulk_scope_reason"].eq("Existing owned Pokémon card")
    ].copy()

    summary = (
        inv_scope.groupby("__bulk_scope_reason", dropna=False)
        .size()
        .reset_index(name="count")
        .rename(columns={"__bulk_scope_reason": "scope_reason"})
        .sort_values("count", ascending=False)
    )

    return eligible, summary


def _collectr_upload_fingerprint(uploaded_file, eligible_inventory: pd.DataFrame, threshold: float) -> str:
    upload_hash = hashlib.sha256(_file_bytes(uploaded_file)).hexdigest()[:10]

    if eligible_inventory.empty:
        inventory_hash = "empty"
    else:
        fingerprint_source = "|".join(
            sorted(
                f"{_text(row.get('inventory_id'))}:"
                f"{_text(row.get('inventory_status'))}:"
                f"{_text(row.get('updated_at'))}"
                for _, row in eligible_inventory.iterrows()
            )
        )
        inventory_hash = hashlib.sha256(
            fingerprint_source.encode("utf-8")
        ).hexdigest()[:10]

    return f"{upload_hash}_{inventory_hash}_{int(threshold)}"


def _build_collectr_bulk_add_frame(
    new_cards: pd.DataFrame,
    *,
    default_purchase_date,
    default_purchased_from: str,
    default_inventory_type: str,
    prefill_collectr_cost: bool,
) -> pd.DataFrame:
    if new_cards.empty:
        return pd.DataFrame()

    frame = new_cards.copy()

    frame["add_to_inventory"] = True
    frame["purchase_date"] = str(default_purchase_date)
    frame["purchased_from"] = _text(default_purchased_from)

    if prefill_collectr_cost:
        frame["purchase_price"] = frame["average_cost_paid"].apply(
            lambda value: to_money(value) if to_money(value) > 0 else None
        )
    else:
        frame["purchase_price"] = None

    frame["shipping"] = 0.0
    frame["tax"] = 0.0
    frame["grading_fee"] = 0.0
    frame["inventory_type"] = default_inventory_type
    frame["product_type"] = frame.apply(
        lambda row: "Graded Card"
        if _text(row.get("grading_company")) or _text(row.get("grade"))
        else "Card",
        axis=1,
    )
    frame["brand_or_league"] = "Pokemon TCG"
    frame["card_type"] = "Pokemon"
    frame["reference_link"] = ""
    frame["sticker_price"] = frame["market_value"].apply(to_money)
    frame["notes"] = frame.apply(
        lambda row: " | ".join(
            part
            for part in [
                _text(row.get("collectr_notes")),
                "Added from Collectr Bulk Add",
            ]
            if part
        ),
        axis=1,
    )

    frame["condition"] = frame.apply(
        lambda row: (
            "Graded"
            if _text(row.get("grading_company")) or _text(row.get("grade"))
            else (_text(row.get("condition")) or "Near Mint")
        ),
        axis=1,
    )

    columns = [
        "add_to_inventory",
        "collectr_row_id",
        "source_row_number",
        "source_copy_number",
        "set_name",
        "card_name",
        "card_number",
        "card_subtype",
        "variant",
        "condition",
        "grading_company",
        "grade",
        "market_value",
        "average_cost_paid",
        "purchase_date",
        "purchased_from",
        "purchase_price",
        "shipping",
        "tax",
        "grading_fee",
        "inventory_type",
        "product_type",
        "reference_link",
        "sticker_price",
        "notes",
        "suggested_inventory_id",
        "suggested_inventory_reference_link",
        "suggested_match_score",
        "suggested_match_status",
    ]
    for col in columns:
        if col not in frame.columns:
            frame[col] = ""

    return frame[columns].copy()


def _validate_collectr_bulk_add_editor(
    edited: pd.DataFrame,
    current_inventory: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame, list[dict]]:
    if edited is None or edited.empty:
        return (
            pd.DataFrame(),
            pd.DataFrame(columns=["collectr_row_id", "card_name", "errors"]),
            [],
        )

    selected = edited[
        edited["add_to_inventory"].fillna(False).astype(bool)
    ].copy()

    if selected.empty:
        return selected, pd.DataFrame(
            columns=["collectr_row_id", "card_name", "errors"]
        ), []

    current_ids = set()
    if not current_inventory.empty and "inventory_id" in current_inventory.columns:
        current_ids = set(
            current_inventory["inventory_id"].astype(str).str.strip().tolist()
        )

    validation_rows: list[dict[str, Any]] = []
    rows_to_add: list[dict] = []

    for _, row in selected.iterrows():
        errors: list[str] = []

        collectr_row_id = _text(row.get("collectr_row_id"))
        card_name = _text(row.get("card_name"))
        purchase_date_value = pd.to_datetime(
            row.get("purchase_date"), errors="coerce"
        )
        purchased_from = _text(row.get("purchased_from"))
        purchase_price_raw = row.get("purchase_price")

        if not card_name:
            errors.append("Missing card name")
        if pd.isna(purchase_date_value):
            errors.append("Invalid purchase date")
        if not purchased_from:
            errors.append("Purchased from is required")
        if purchase_price_raw is None or (
            isinstance(purchase_price_raw, float) and pd.isna(purchase_price_raw)
        ) or _text(purchase_price_raw) == "":
            errors.append("Purchase price is required")

        inventory_type = _normalize_inventory_type_value(
            row.get("inventory_type")
        )
        if inventory_type not in INVENTORY_TYPE_OPTIONS:
            errors.append(
                f"Invalid inventory type: {inventory_type or 'blank'}"
            )

        product_type = _normalize_product_type_value(row.get("product_type"))
        if product_type not in PRODUCT_TYPE_OPTIONS:
            errors.append(
                f"Invalid product type: {product_type or 'blank'}"
            )

        validation_rows.append(
            {
                "collectr_row_id": collectr_row_id,
                "card_name": card_name,
                "status": "READY" if not errors else "ERROR",
                "errors": "; ".join(errors),
            }
        )

        if errors:
            continue

        new_inventory_row = _make_inventory_row(
            inventory_type=inventory_type,
            product_type=product_type,
            card_type="Pokemon",
            brand_or_league="Pokemon TCG",
            set_name=_text(row.get("set_name")),
            year="",
            card_name=card_name,
            card_number=_text(row.get("card_number")),
            variant=_text(row.get("variant")),
            card_subtype=_text(row.get("card_subtype")),
            grading_company=_text(row.get("grading_company")),
            grade=_text(row.get("grade")),
            reference_link=_text(row.get("reference_link")),
            purchase_date_value=str(purchase_date_value.date()),
            purchased_from=purchased_from,
            purchase_price=to_money(row.get("purchase_price")),
            shipping=to_money(row.get("shipping")),
            tax=to_money(row.get("tax")),
            sticker_price=to_money(row.get("sticker_price")),
            condition=_text(row.get("condition")),
            grading_fee=to_money(row.get("grading_fee")),
            market_value=to_money(row.get("market_value")),
            notes=_text(row.get("notes")),
        )

        # Defensive check: IDs generated here must not collide with current inventory.
        while new_inventory_row["inventory_id"] in current_ids:
            new_inventory_row["inventory_id"] = str(uuid.uuid4())[:8]
        current_ids.add(new_inventory_row["inventory_id"])

        rows_to_add.append(new_inventory_row)

    validation = pd.DataFrame(validation_rows)
    return selected, validation, rows_to_add


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
    for col in ["inventory_status", "inventory_id", "product_type", "card_type", "inventory_type"]:
        if col not in inv.columns:
            inv[col] = ""

    inv["inventory_status"] = inv["inventory_status"].astype(str).str.upper().str.strip()

    for col in [
        "purchase_price",
        "shipping",
        "tax",
        "total_price",
        "grading_fee",
        "total_cost",
        "market_value",
        "sticker_price",
    ]:
        if col in inv.columns:
            inv[col] = inv[col].apply(to_money).astype(float)

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

    st.caption("Paste a PriceCharting or SportsCardsPro link, pull details, review the fields, then add the item.")

    link_col1, link_col2 = st.columns([4, 1])

    with link_col1:
        reference_link_input = st.text_input(
            "Reference link",
            key="single_reference_link_input",
            placeholder="https://www.pricecharting.com/game/pokemon-surging-sparks/pikachu-ex-247",
        )

    with link_col2:
        st.write("")
        pull_details = st.button("Pull details", use_container_width=True)

    if pull_details:
        if not clean_text(reference_link_input):
            st.warning("Paste a PriceCharting or SportsCardsPro link first.")
        else:
            with st.spinner("Pulling card details..."):
                details = fetch_reference_details(reference_link_input)
            st.session_state["single_prefill_details"] = details
            st.success("Pulled details. Review/adjust below before adding.")

    prefill = st.session_state.get("single_prefill_details", {}) or {}

    if prefill.get("image_url"):
        try:
            st.image(prefill.get("image_url"), width=170)
        except Exception:
            st.caption("Image unavailable.")

    with st.form("add_single_inventory", clear_on_submit=False):
        c1, c2, c3, c4 = st.columns(4)

        with c1:
            product_type = st.selectbox(
                "Product type*",
                PRODUCT_TYPE_OPTIONS,
                index=_option_index(PRODUCT_TYPE_OPTIONS, prefill.get("product_type", "Card")),
            )
            inventory_type = st.selectbox(
                "Inventory type*",
                INVENTORY_TYPE_OPTIONS,
                index=_option_index(INVENTORY_TYPE_OPTIONS, "Show Inventory"),
            )
            card_type = st.selectbox(
                "Card type*",
                CARD_TYPE_OPTIONS,
                index=_option_index(CARD_TYPE_OPTIONS, prefill.get("card_type", "Pokemon")),
            )

        with c2:
            brand_or_league = st.text_input(
                "Brand / League",
                value=prefill.get("brand_or_league") or ("Pokemon TCG" if (prefill.get("card_type") or "Pokemon") == "Pokemon" else ""),
            )
            set_name = st.text_input("Set", value=prefill.get("set_name", ""))
            year = st.text_input("Year", value=prefill.get("year", ""))

        with c3:
            card_name = st.text_input("Card / item name*", value=prefill.get("card_name", ""))
            card_number = st.text_input("Card #", value=prefill.get("card_number", ""))
            variant = st.text_input("Variant", value=prefill.get("variant", ""))

        with c4:
            card_subtype = st.text_input("Subtype", value=prefill.get("card_subtype", ""))
            sealed_product_type = st.text_input(
                "Sealed product type",
                value=prefill.get("sealed_product_type", ""),
            )
            reference_link = st.text_input(
                "Reference link to store",
                value=prefill.get("reference_link") or reference_link_input,
            )

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
            grading_fee = st.number_input(
                "Grading fee",
                min_value=0.0,
                step=1.0,
                format="%.2f",
            )

        with c8:
            sticker_price = st.number_input(
                "Sticker price",
                min_value=0.0,
                step=1.0,
                format="%.2f",
            )
            market_value = st.number_input(
                "Market value",
                min_value=0.0,
                step=1.0,
                format="%.2f",
            )

        c9, c10, c11, c12 = st.columns(4)

        with c9:
            grading_company = st.text_input("Grading company")
        with c10:
            grade = st.text_input("Grade")
        with c11:
            condition_default = "Sealed" if product_type == "Sealed" else ("Graded" if product_type == "Graded Card" else "Near Mint")
            condition_options = _condition_options()
            condition = st.selectbox(
                "Condition",
                condition_options,
                index=_option_index(condition_options, condition_default),
            )
        with c12:
            quantity = st.number_input(
                "Quantity",
                min_value=1,
                max_value=250,
                value=1,
                step=1,
            )

        image_url = st.text_input("Image URL", value=prefill.get("image_url", ""))

        estimated_total_price = round(to_money(purchase_price) + to_money(shipping) + to_money(tax), 2)
        estimated_total_cost = round(estimated_total_price + to_money(grading_fee), 2)

        st.info(
            f"Estimated total price per item: {money_fmt(estimated_total_price)} | "
            f"Estimated total cost per item: {money_fmt(estimated_total_cost)}",
            icon="🧮",
        )

        submitted = st.form_submit_button("Add item(s)", type="primary")

    if submitted:
        errors = []

        if not clean_text(card_name) and not clean_text(reference_link):
            errors.append("Add at least a card/item name or a reference link.")

        if not clean_text(purchase_date_value):
            errors.append("Purchase date is required.")

        if product_type not in PRODUCT_TYPE_OPTIONS:
            errors.append("Invalid product type.")

        if inventory_type not in INVENTORY_TYPE_OPTIONS:
            errors.append("Invalid inventory type.")

        if card_type not in CARD_TYPE_OPTIONS:
            errors.append("Invalid card type.")

        if errors:
            for err in errors:
                st.error(err)
        else:
            rows = []

            for _ in range(int(quantity)):
                rows.append(
                    _make_inventory_row(
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
                        sealed_product_type=sealed_product_type,
                        image_url=image_url,
                        grading_fee=grading_fee,
                        market_value=market_value,
                    )
                )

            _append_inventory_rows(rows)
            st.session_state["single_prefill_details"] = {}
            refresh_database_cache()
            st.success(f"Added {len(rows):,} item(s) to inventory.")
            st.rerun()


# =========================================================
# Bulk Add
# =========================================================

with tab_bulk:
    st.subheader("Bulk Add from Collectr")
    st.write(
        "Upload a Collectr export. The app compares each Pokémon card copy against "
        "your current ACTIVE, LISTED, and GRADING business inventory using the same "
        "duplicate-aware matching logic as the Shows page. Only Collectr copies that "
        "do not have an existing inventory match are staged for adding."
    )
    st.info(
        "This tab only adds inventory. It does not identify missing cards or mark "
        "anything SOLD. SOLD inventory is intentionally excluded from matching so a "
        "newly acquired replacement copy can be added.",
        icon="ℹ️",
    )

    bulk_existing_inventory, bulk_scope_summary = _bulk_collectr_inventory_scope(inv)

    scope_metrics = st.columns(3)
    scope_metrics[0].metric("All inventory rows", f"{len(inv):,}")
    scope_metrics[1].metric(
        "Existing owned Pokémon cards compared",
        f"{len(bulk_existing_inventory):,}",
    )
    scope_metrics[2].metric(
        "SOLD / excluded rows",
        f"{max(0, len(inv) - len(bulk_existing_inventory)):,}",
    )

    with st.expander("See Bulk Add comparison scope", expanded=False):
        st.dataframe(
            bulk_scope_summary,
            use_container_width=True,
            hide_index=True,
        )
        st.caption(
            "ACTIVE, LISTED, and GRADING business-owned Pokémon single cards count "
            "as existing inventory. SOLD, consignment, personal, sealed, sports, and "
            "non-Pokémon records do not."
        )

    option_cols = st.columns([1, 1, 1, 1])

    with option_cols[0]:
        bulk_match_threshold = st.slider(
            "Automatic match score",
            min_value=70,
            max_value=95,
            value=int(AUTO_MATCH_THRESHOLD_DEFAULT),
            step=1,
            key="inventory_bulk_collectr_match_threshold",
            help=(
                "Higher values are stricter and can produce more 'new' rows. "
                "80 matches the Shows page default."
            ),
        )

    with option_cols[1]:
        bulk_default_purchase_date = st.date_input(
            "Default purchase date",
            value=date.today(),
            key="inventory_bulk_collectr_purchase_date",
        )

    with option_cols[2]:
        bulk_default_purchased_from = st.text_input(
            "Default purchased from",
            value="",
            key="inventory_bulk_collectr_purchased_from",
        )

    with option_cols[3]:
        bulk_default_inventory_type = st.selectbox(
            "Default inventory type",
            INVENTORY_TYPE_OPTIONS,
            index=_option_index(INVENTORY_TYPE_OPTIONS, "Show Inventory"),
            key="inventory_bulk_collectr_inventory_type",
        )

    bulk_prefill_collectr_cost = st.checkbox(
        "Prefill Purchase Price from Collectr Average Cost Paid when available",
        value=True,
        key="inventory_bulk_collectr_prefill_cost",
        help=(
            "You can still change every Purchase Price in the table before adding."
        ),
    )

    collectr_bulk_file = st.file_uploader(
        "Upload Collectr CSV/XLSX",
        type=["csv", "xlsx", "xls"],
        key="inventory_collectr_bulk_upload",
    )

    if collectr_bulk_file is not None:
        try:
            collectr_cards, collectr_stats = _parse_collectr(
                collectr_bulk_file
            )

            if collectr_cards.empty:
                st.error(
                    "No Pokémon single-card rows were found in this Collectr file. "
                    "Confirm the export contains Pokémon cards and a Category column."
                )
            else:
                (
                    bulk_match_audit,
                    bulk_matched,
                    bulk_new_cards,
                    _,
                ) = _reconcile_collectr(
                    collectr_cards,
                    bulk_existing_inventory,
                    auto_match_threshold=float(bulk_match_threshold),
                    duplicate_policy="Keep newest eligible inventory",
                )

                bulk_fingerprint = _collectr_upload_fingerprint(
                    collectr_bulk_file,
                    bulk_existing_inventory,
                    float(bulk_match_threshold),
                )

                metrics = st.columns(5)
                metrics[0].metric(
                    "Collectr source rows",
                    f"{collectr_stats['source_rows']:,}",
                )
                metrics[1].metric(
                    "Collectr card copies",
                    f"{collectr_stats['individual_cards']:,}",
                )
                metrics[2].metric(
                    "Matched to inventory",
                    f"{len(bulk_matched):,}",
                )
                metrics[3].metric(
                    "New copies to review",
                    f"{len(bulk_new_cards):,}",
                )
                metrics[4].metric(
                    "Ignored non-card/non-Pokémon rows",
                    f"{collectr_stats['non_pokemon_rows_ignored'] + collectr_stats.get('sealed_rows_ignored', 0):,}",
                )

                result_tabs = st.tabs(
                    [
                        f"New in Collectr ({len(bulk_new_cards):,})",
                        f"Matched Existing ({len(bulk_matched):,})",
                        "Match Audit",
                    ]
                )

                with result_tabs[0]:
                    if bulk_new_cards.empty:
                        st.success(
                            "Every Pokémon card copy in this Collectr file already has "
                            "a matching current inventory record. There is nothing new "
                            "to add."
                        )
                    else:
                        st.caption(
                            "These are the Collectr copies that were not matched to current "
                            "inventory. Leave Add checked for real new purchases. Uncheck any "
                            "row you do not want to add. Enter Purchase Date, Purchased From, "
                            "Purchase Price, and any shipping/tax/grading costs directly here."
                        )

                        bulk_editor_base = _build_collectr_bulk_add_frame(
                            bulk_new_cards,
                            default_purchase_date=bulk_default_purchase_date,
                            default_purchased_from=bulk_default_purchased_from,
                            default_inventory_type=bulk_default_inventory_type,
                            prefill_collectr_cost=bulk_prefill_collectr_cost,
                        )

                        bulk_editor_key = (
                            f"inventory_collectr_bulk_editor_{bulk_fingerprint}_"
                            f"{int(bulk_prefill_collectr_cost)}_"
                            f"{hashlib.sha256((_text(bulk_default_purchased_from) + str(bulk_default_purchase_date) + _text(bulk_default_inventory_type)).encode('utf-8')).hexdigest()[:8]}"
                        )

                        edited_bulk = st.data_editor(
                            bulk_editor_base,
                            use_container_width=True,
                            hide_index=True,
                            height=650,
                            key=bulk_editor_key,
                            column_config={
                                "add_to_inventory": st.column_config.CheckboxColumn(
                                    "Add",
                                    default=True,
                                    width="small",
                                ),
                                "collectr_row_id": st.column_config.TextColumn(
                                    "Collectr Row",
                                    disabled=True,
                                ),
                                "source_row_number": st.column_config.NumberColumn(
                                    "Source Row",
                                    disabled=True,
                                ),
                                "source_copy_number": st.column_config.NumberColumn(
                                    "Copy",
                                    disabled=True,
                                ),
                                "set_name": st.column_config.TextColumn(
                                    "Set",
                                    disabled=True,
                                ),
                                "card_name": st.column_config.TextColumn(
                                    "Card",
                                    disabled=True,
                                    width="medium",
                                ),
                                "card_number": st.column_config.TextColumn(
                                    "Card #",
                                    disabled=True,
                                ),
                                "card_subtype": st.column_config.TextColumn(
                                    "Rarity / Subtype",
                                    disabled=True,
                                ),
                                "variant": st.column_config.TextColumn(
                                    "Variant",
                                    disabled=True,
                                ),
                                "grading_company": st.column_config.TextColumn(
                                    "Grader",
                                    disabled=True,
                                ),
                                "grade": st.column_config.TextColumn(
                                    "Grade",
                                    disabled=True,
                                ),
                                "market_value": st.column_config.NumberColumn(
                                    "Collectr Market",
                                    format="$%.2f",
                                    disabled=True,
                                ),
                                "average_cost_paid": st.column_config.NumberColumn(
                                    "Collectr Avg Cost",
                                    format="$%.2f",
                                    disabled=True,
                                ),
                                "purchase_price": st.column_config.NumberColumn(
                                    "Purchase Price*",
                                    min_value=0.0,
                                    format="$%.2f",
                                ),
                                "shipping": st.column_config.NumberColumn(
                                    "Shipping",
                                    min_value=0.0,
                                    format="$%.2f",
                                ),
                                "tax": st.column_config.NumberColumn(
                                    "Tax",
                                    min_value=0.0,
                                    format="$%.2f",
                                ),
                                "grading_fee": st.column_config.NumberColumn(
                                    "Grading Fee",
                                    min_value=0.0,
                                    format="$%.2f",
                                ),
                                "inventory_type": st.column_config.SelectboxColumn(
                                    "Inventory Type",
                                    options=INVENTORY_TYPE_OPTIONS,
                                    required=True,
                                ),
                                "product_type": st.column_config.SelectboxColumn(
                                    "Product Type",
                                    options=PRODUCT_TYPE_OPTIONS,
                                    required=True,
                                ),
                                "reference_link": st.column_config.LinkColumn(
                                    "Reference Link",
                                ),
                                "sticker_price": st.column_config.NumberColumn(
                                    "Sticker Price",
                                    min_value=0.0,
                                    format="$%.2f",
                                ),
                                "suggested_inventory_reference_link": st.column_config.LinkColumn(
                                    "Suggested Existing Link",
                                    disabled=True,
                                ),
                                "suggested_match_score": st.column_config.NumberColumn(
                                    "Best Match Score",
                                    format="%.1f",
                                    disabled=True,
                                ),
                            },
                            disabled=[
                                "collectr_row_id",
                                "source_row_number",
                                "source_copy_number",
                                "set_name",
                                "card_name",
                                "card_number",
                                "card_subtype",
                                "variant",
                                "grading_company",
                                "grade",
                                "market_value",
                                "average_cost_paid",
                                "suggested_inventory_id",
                                "suggested_inventory_reference_link",
                                "suggested_match_score",
                                "suggested_match_status",
                            ],
                        )

                        (
                            selected_bulk_rows,
                            bulk_validation,
                            bulk_rows_to_add,
                        ) = _validate_collectr_bulk_add_editor(
                            edited_bulk,
                            inv,
                        )

                        selected_count = len(selected_bulk_rows)
                        error_count = (
                            int(bulk_validation["status"].eq("ERROR").sum())
                            if not bulk_validation.empty
                            else 0
                        )
                        total_cost_to_add = 0.0
                        total_market_to_add = 0.0

                        if bulk_rows_to_add:
                            total_cost_to_add = sum(
                                to_money(row.get("total_cost"))
                                for row in bulk_rows_to_add
                            )
                            total_market_to_add = sum(
                                to_money(row.get("market_value"))
                                for row in bulk_rows_to_add
                            )

                        add_metrics = st.columns(4)
                        add_metrics[0].metric(
                            "Selected to add",
                            f"{selected_count:,}",
                        )
                        add_metrics[1].metric(
                            "Validation errors",
                            f"{error_count:,}",
                        )
                        add_metrics[2].metric(
                            "Total cost",
                            money_fmt(total_cost_to_add),
                        )
                        add_metrics[3].metric(
                            "Collectr market value",
                            money_fmt(total_market_to_add),
                        )

                        if not bulk_validation.empty:
                            error_rows = bulk_validation[
                                bulk_validation["status"].eq("ERROR")
                            ].copy()
                            if not error_rows.empty:
                                st.error(
                                    "Fix the selected rows below before adding inventory."
                                )
                                st.dataframe(
                                    error_rows,
                                    use_container_width=True,
                                    hide_index=True,
                                )

                        confirm_bulk_add = st.checkbox(
                            f"I reviewed the Collectr matches and want to add "
                            f"{len(bulk_rows_to_add):,} new inventory record(s).",
                            value=False,
                            disabled=(
                                not bulk_rows_to_add
                                or error_count > 0
                            ),
                            key=f"confirm_collectr_bulk_add_{bulk_fingerprint}",
                        )

                        if st.button(
                            "Add selected Collectr cards to inventory",
                            type="primary",
                            use_container_width=True,
                            disabled=(
                                not confirm_bulk_add
                                or not bulk_rows_to_add
                                or error_count > 0
                            ),
                            key=f"process_collectr_bulk_add_{bulk_fingerprint}",
                        ):
                            # Reload the database immediately before writing so a
                            # duplicate cannot be created if inventory changed after
                            # the preview.
                            latest_data = load_data(force_refresh=True)
                            latest_inv = _safe_df(latest_data.inventory)
                            latest_existing, _ = _bulk_collectr_inventory_scope(
                                latest_inv
                            )

                            (
                                _latest_audit,
                                _latest_matched,
                                latest_new_cards,
                                _latest_missing,
                            ) = _reconcile_collectr(
                                collectr_cards,
                                latest_existing,
                                auto_match_threshold=float(
                                    bulk_match_threshold
                                ),
                                duplicate_policy="Keep newest eligible inventory",
                            )

                            latest_new_ids = set(
                                latest_new_cards["collectr_row_id"]
                                .astype(str)
                                .str.strip()
                                .tolist()
                            )

                            selected_editor_ids = set(
                                selected_bulk_rows["collectr_row_id"]
                                .astype(str)
                                .str.strip()
                                .tolist()
                            )

                            no_longer_new = sorted(
                                selected_editor_ids.difference(
                                    latest_new_ids
                                )
                            )

                            if no_longer_new:
                                st.error(
                                    "Inventory changed after the preview and one or "
                                    "more selected Collectr copies now match an existing "
                                    "inventory record. Refresh/review before adding: "
                                    + ", ".join(no_longer_new[:20])
                                    + (
                                        "..."
                                        if len(no_longer_new) > 20
                                        else ""
                                    )
                                )
                            else:
                                _append_inventory_rows(
                                    bulk_rows_to_add
                                )
                                refresh_database_cache()
                                st.success(
                                    f"Added {len(bulk_rows_to_add):,} new "
                                    "inventory record(s) from Collectr."
                                )
                                st.rerun()

                with result_tabs[1]:
                    if bulk_matched.empty:
                        st.info(
                            "No Collectr cards were matched to current inventory."
                        )
                    else:
                        matched_cols = [
                            "collectr_row_id",
                            "collectr_set_name",
                            "collectr_card_name",
                            "collectr_card_number",
                            "collectr_condition",
                            "collectr_grading_company",
                            "collectr_grade",
                            "collectr_market_value",
                            "inventory_id",
                            "inventory_status",
                            "inventory_set_name",
                            "inventory_card_name",
                            "inventory_card_number",
                            "inventory_condition",
                            "inventory_grading_company",
                            "inventory_grade",
                            "inventory_total_cost",
                            "match_score",
                            "match_method",
                            "inventory_reference_link",
                        ]
                        st.dataframe(
                            bulk_matched[
                                [
                                    col
                                    for col in matched_cols
                                    if col in bulk_matched.columns
                                ]
                            ],
                            use_container_width=True,
                            hide_index=True,
                            column_config={
                                "collectr_market_value": st.column_config.NumberColumn(
                                    "Collectr Market",
                                    format="$%.2f",
                                ),
                                "inventory_total_cost": st.column_config.NumberColumn(
                                    "Inventory Cost",
                                    format="$%.2f",
                                ),
                                "match_score": st.column_config.NumberColumn(
                                    "Match Score",
                                    format="%.1f",
                                ),
                                "inventory_reference_link": st.column_config.LinkColumn(
                                    "Reference Link"
                                ),
                            },
                        )

                with result_tabs[2]:
                    st.caption(
                        "Use this audit when a card appears unexpectedly as new. "
                        "It shows the best candidate, score components, and whether "
                        "the candidate was already consumed by another Collectr copy."
                    )
                    st.dataframe(
                        bulk_match_audit,
                        use_container_width=True,
                        hide_index=True,
                        column_config={
                            "match_score": st.column_config.NumberColumn(
                                "Match Score",
                                format="%.1f",
                            ),
                            "collectr_market_value": st.column_config.NumberColumn(
                                "Collectr Market",
                                format="$%.2f",
                            ),
                            "inventory_total_cost": st.column_config.NumberColumn(
                                "Inventory Cost",
                                format="$%.2f",
                            ),
                            "inventory_reference_link": st.column_config.LinkColumn(
                                "Inventory Reference Link"
                            ),
                        },
                    )

                    st.download_button(
                        "Download Bulk Add match audit CSV",
                        data=bulk_match_audit.to_csv(index=False).encode(
                            "utf-8-sig"
                        ),
                        file_name="inventory_collectr_bulk_add_match_audit.csv",
                        mime="text/csv",
                        use_container_width=True,
                    )

        except Exception as exc:
            st.exception(exc)




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
            search = st.text_input("Search card, set, number, variant, ID, eBay ID")

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
                    r.get("ebay_item_id", ""),
                    r.get("ebay_listing_id", ""),
                    r.get("ebay_order_id", ""),
                    r.get("ebay_line_item_id", ""),
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
                sort_series = view[sort_col]
                if sort_col in ["market_value", "total_cost", "sticker_price", "profit"]:
                    view["__sort_num"] = sort_series.apply(to_money)
                    view = view.sort_values("__sort_num", ascending=sort_ascending, na_position="last")
                    view = view.drop(columns=["__sort_num"], errors="ignore")
                else:
                    view = view.sort_values(sort_col, ascending=sort_ascending, na_position="last")

        st.caption(f"{len(view):,} item(s) shown")

        display_cols = [c for c in _inventory_display_cols() if c in view.columns]

        st.dataframe(
            view[display_cols],
            use_container_width=True,
            hide_index=True,
            column_config={
                "image_url": st.column_config.ImageColumn("Image", width="small"),
                "reference_link": st.column_config.LinkColumn("Reference Link"),
                "ebay_listing_url": st.column_config.LinkColumn("eBay Listing"),
            },
        )

        csv = view[display_cols].to_csv(index=False)

        st.download_button(
            "Download filtered inventory CSV",
            data=csv,
            file_name="filtered_inventory.csv",
            mime="text/csv",
        )
