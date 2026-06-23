from __future__ import annotations

import re
import uuid
from datetime import date
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
    st.subheader("Bulk Add Inventory")

    st.caption(
        "Upload a CSV or Excel file. The page stages the rows first, validates them, and only writes to Google Sheets after you review and confirm."
    )

    template = get_upload_template_df()

    t1, t2 = st.columns([1, 1])

    with t1:
        st.download_button(
            "Download bulk template CSV",
            data=template.to_csv(index=False),
            file_name="inventory_upload_template.csv",
            mime="text/csv",
            use_container_width=True,
        )

    with t2:
        st.download_button(
            "Download bulk template Excel",
            data=template.to_csv(index=False),
            file_name="inventory_upload_template.csv",
            mime="text/csv",
            use_container_width=True,
            help="CSV is safest for Streamlit Cloud. Open it in Excel if needed.",
        )

    uploaded = st.file_uploader(
        "Upload inventory CSV/XLSX",
        type=["csv", "xlsx", "xls"],
        help="Each row becomes one inventory item unless you add Quantity. Transaction/sale/eBay columns are ignored on purpose.",
    )

    if uploaded is not None:
        try:
            raw = _read_upload_file(uploaded)

            st.markdown("#### Raw upload preview")
            st.dataframe(raw.head(50), use_container_width=True, hide_index=True)

            ignored_cols = _ignored_present_columns(raw)
            unexpected_cols = _unexpected_upload_columns(raw)

            if ignored_cols:
                st.warning(
                    "These database/sale/eBay columns were found in the upload and will be ignored so new inventory does not get created as sold/listed by accident: "
                    + ", ".join(ignored_cols[:20])
                    + ("..." if len(ignored_cols) > 20 else "")
                )

            if unexpected_cols:
                st.info(
                    "These columns are not recognized by the bulk uploader and will be ignored: "
                    + ", ".join(unexpected_cols[:20])
                    + ("..." if len(unexpected_cols) > 20 else ""),
                    icon="ℹ️",
                )

            st.markdown("#### Defaults for blank upload fields")

            d1, d2, d3, d4 = st.columns(4)

            with d1:
                default_inventory_type = st.selectbox(
                    "Default inventory type",
                    INVENTORY_TYPE_OPTIONS,
                    index=_option_index(INVENTORY_TYPE_OPTIONS, "Show Inventory"),
                    key="bulk_default_inventory_type",
                )

            with d2:
                default_product_type = st.selectbox(
                    "Default product type",
                    PRODUCT_TYPE_OPTIONS,
                    index=_option_index(PRODUCT_TYPE_OPTIONS, "Card"),
                    key="bulk_default_product_type",
                )

            with d3:
                default_card_type = st.selectbox(
                    "Default card type",
                    CARD_TYPE_OPTIONS,
                    index=_option_index(CARD_TYPE_OPTIONS, "Pokemon"),
                    key="bulk_default_card_type",
                )

            with d4:
                default_condition = st.selectbox(
                    "Default condition",
                    _condition_options(),
                    index=_option_index(_condition_options(), "Near Mint"),
                    key="bulk_default_condition",
                )

            d5, d6, d7 = st.columns(3)

            with d5:
                default_brand_or_league = st.text_input(
                    "Default brand / league",
                    value="Pokemon TCG",
                    key="bulk_default_brand",
                )

            with d6:
                default_purchase_date = st.date_input(
                    "Default purchase date",
                    value=date.today(),
                    key="bulk_default_purchase_date",
                )

            with d7:
                default_purchased_from = st.text_input(
                    "Default purchased from",
                    value="",
                    key="bulk_default_purchased_from",
                )

            normalized = normalize_uploaded_inventory_df(
                raw,
                default_inventory_type=default_inventory_type,
                default_product_type=default_product_type,
                default_card_type=default_card_type,
                default_condition=default_condition,
                default_brand_or_league=default_brand_or_league,
                default_purchase_date=default_purchase_date,
                default_purchased_from=default_purchased_from,
            )

            if normalized.empty:
                st.warning("The uploaded file does not have any data rows to process.")
            else:
                st.markdown("#### Staged rows — review/edit before upload")

                st.caption(
                    "This table is what the app will use. Fix anything wrong here before confirming."
                )

                editable_cols = ["source_row"] + BULK_INPUT_COLUMNS

                edited = st.data_editor(
                    normalized[editable_cols],
                    use_container_width=True,
                    hide_index=True,
                    height=430,
                    column_config={
                        "source_row": st.column_config.NumberColumn("Source row", disabled=True),
                        "purchase_price": st.column_config.NumberColumn("Purchase Price", format="$%.2f"),
                        "shipping": st.column_config.NumberColumn("Shipping", format="$%.2f"),
                        "tax": st.column_config.NumberColumn("Tax", format="$%.2f"),
                        "grading_fee": st.column_config.NumberColumn("Grading Fee", format="$%.2f"),
                        "sticker_price": st.column_config.NumberColumn("Sticker Price", format="$%.2f"),
                        "market_value": st.column_config.NumberColumn("Market Value", format="$%.2f"),
                        "quantity": st.column_config.NumberColumn("Quantity", min_value=1, step=1),
                        "reference_link": st.column_config.LinkColumn("Reference Link"),
                        "image_url": st.column_config.LinkColumn("Image URL"),
                    },
                    disabled=["source_row"],
                    key="bulk_staged_editor",
                )

                validated, error_df, warning_df, rows_to_insert = _validate_bulk_preview(edited)

                if validated.empty:
                    st.warning("No valid staged rows.")
                else:
                    ready_rows = validated[validated["row_status"].eq("Ready")].copy()
                    blocked_rows = validated[validated["row_status"].eq("Blocked")].copy()

                    m1, m2, m3, m4, m5 = st.columns(5)
                    m1.metric("Upload rows", f"{len(validated):,}")
                    m2.metric("Rows blocked", f"{len(blocked_rows):,}")
                    m3.metric("Inventory items to create", f"{len(rows_to_insert):,}")
                    m4.metric("Total cost to add", money_fmt(validated["total_cost"].sum()))
                    m5.metric("Sticker total", money_fmt(validated["sticker_price"].sum()))

                    summary = (
                        validated.groupby(["inventory_type", "product_type", "card_type"], dropna=False)
                        .agg(
                            upload_rows=("source_row", "count"),
                            quantity=("quantity", "sum"),
                            total_cost=("total_cost", "sum"),
                            sticker_price=("sticker_price", "sum"),
                            market_value=("market_value", "sum"),
                        )
                        .reset_index()
                    )

                    st.markdown("##### Upload summary")
                    st.dataframe(
                        summary.style.format(
                            {
                                "total_cost": "${:,.2f}",
                                "sticker_price": "${:,.2f}",
                                "market_value": "${:,.2f}",
                            }
                        ),
                        use_container_width=True,
                        hide_index=True,
                    )

                    st.markdown("##### Validation result")
                    validation_cols = [
                        "source_row",
                        "row_status",
                        "errors",
                        "warnings",
                        "quantity",
                        "inventory_type",
                        "product_type",
                        "card_type",
                        "set_name",
                        "card_name",
                        "card_number",
                        "variant",
                        "purchase_date",
                        "purchased_from",
                        "purchase_price",
                        "shipping",
                        "tax",
                        "total_price",
                        "grading_fee",
                        "total_cost",
                        "condition",
                        "reference_link",
                    ]

                    st.dataframe(
                        validated[[c for c in validation_cols if c in validated.columns]].style.format(
                            {
                                "purchase_price": "${:,.2f}",
                                "shipping": "${:,.2f}",
                                "tax": "${:,.2f}",
                                "total_price": "${:,.2f}",
                                "grading_fee": "${:,.2f}",
                                "total_cost": "${:,.2f}",
                            }
                        ),
                        use_container_width=True,
                        hide_index=True,
                        column_config={
                            "reference_link": st.column_config.LinkColumn("Reference Link"),
                        },
                    )

                    if not error_df.empty:
                        st.error("Nothing can upload until the blocked rows below are fixed.")
                        st.dataframe(error_df, use_container_width=True, hide_index=True)

                    if not warning_df.empty:
                        with st.expander("Warnings to review", expanded=True):
                            st.dataframe(warning_df, use_container_width=True, hide_index=True)

                    st.download_button(
                        "Download staged validation CSV",
                        data=validated.to_csv(index=False),
                        file_name="bulk_inventory_validation_preview.csv",
                        mime="text/csv",
                    )

                    confirm = st.checkbox(
                        "I reviewed the staged rows and want to add the ready rows to inventory.",
                        value=False,
                        disabled=not error_df.empty or not rows_to_insert,
                    )

                    if st.button(
                        "Add ready rows to inventory",
                        type="primary",
                        disabled=(not confirm or not error_df.empty or not rows_to_insert),
                        use_container_width=True,
                    ):
                        _append_inventory_rows(rows_to_insert)
                        refresh_database_cache()
                        st.success(f"Added {len(rows_to_insert):,} inventory item(s).")
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
