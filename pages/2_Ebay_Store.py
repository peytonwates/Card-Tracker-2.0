# pages/2_Ebay_Store.py
from __future__ import annotations

import base64
import re
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import requests
import streamlit as st

from core.business import load_data, refresh_database_cache
from core.cleaning import clean_text, to_money, money_fmt, now_iso
from core.config import INVENTORY_COLUMNS, STATUS_ACTIVE, STATUS_LISTED, STATUS_SOLD
from core.sheets import get_ws_name, update_rows_by_key


st.set_page_config(page_title="eBay Store", page_icon="🛒", layout="wide")

st.title("🛒 eBay Store Sync")
st.caption("Pull active eBay listings, assign them to inventory, then sync sold eBay orders, fees, net proceeds, and profit.")


# =========================================================
# eBay auth / config
# =========================================================

def _safe_secret_get(source, key, default=None):
    try:
        if hasattr(source, "get"):
            return source.get(key, default)
    except Exception:
        pass

    try:
        return source[key]
    except Exception:
        return default


def _first_secret_value(source, keys: list[str], default: str = "") -> str:
    for key in keys:
        value = _safe_secret_get(source, key, None)

        if value is None:
            continue

        if isinstance(value, (list, tuple)):
            value = " ".join(str(v).strip() for v in value if str(v).strip())
        else:
            value = str(value).strip()

        if value:
            return value

    return default


def get_ebay_secrets():
    try:
        ebay_section = _safe_secret_get(st.secrets, "ebay", None)

        if ebay_section is not None:
            source = ebay_section
            source_label = "[ebay]"
            client_id_keys = ["client_id", "CLIENT_ID", "app_id", "APP_ID", "EBAY_CLIENT_ID", "EBAY_APP_ID"]
            client_secret_keys = ["client_secret", "CLIENT_SECRET", "cert_id", "CERT_ID", "EBAY_CLIENT_SECRET", "EBAY_CERT_ID"]
            ru_name_keys = ["ru_name", "RU_NAME", "runame", "RUNAME", "EBAY_RU_NAME", "EBAY_RUNAME"]
            scopes_keys = ["scopes", "scope", "SCOPES", "SCOPE", "EBAY_SCOPES", "EBAY_SCOPE"]
            refresh_token_keys = ["refresh_token", "REFRESH_TOKEN", "EBAY_REFRESH_TOKEN"]
            environment_keys = ["environment", "ENVIRONMENT", "EBAY_ENVIRONMENT"]
            marketplace_keys = ["marketplace_id", "MARKETPLACE_ID", "EBAY_MARKETPLACE_ID"]
        else:
            source = st.secrets
            source_label = "top-level secrets"
            client_id_keys = ["EBAY_CLIENT_ID", "ebay_client_id", "EBAY_APP_ID", "ebay_app_id", "app_id", "APP_ID", "client_id"]
            client_secret_keys = ["EBAY_CLIENT_SECRET", "ebay_client_secret", "EBAY_CERT_ID", "ebay_cert_id", "cert_id", "CERT_ID", "client_secret"]
            ru_name_keys = ["EBAY_RU_NAME", "EBAY_RUNAME", "ebay_ru_name", "ebay_runame", "ru_name", "runame", "RUNAME"]
            scopes_keys = ["EBAY_SCOPES", "EBAY_SCOPE", "ebay_scopes", "ebay_scope", "scopes", "scope"]
            refresh_token_keys = ["EBAY_REFRESH_TOKEN", "ebay_refresh_token", "refresh_token"]
            environment_keys = ["EBAY_ENVIRONMENT", "ebay_environment", "environment"]
            marketplace_keys = ["EBAY_MARKETPLACE_ID", "ebay_marketplace_id", "marketplace_id"]

        config = {
            "source_label": source_label,
            "environment": _first_secret_value(source, environment_keys, default="production"),
            "marketplace_id": _first_secret_value(source, marketplace_keys, default="EBAY_US"),
            "client_id": _first_secret_value(source, client_id_keys),
            "client_secret": _first_secret_value(source, client_secret_keys),
            "ru_name": _first_secret_value(source, ru_name_keys),
            "scopes": _first_secret_value(source, scopes_keys),
            "refresh_token": _first_secret_value(source, refresh_token_keys),
        }

        missing = [
            field
            for field in ["client_id", "client_secret", "ru_name", "scopes", "refresh_token"]
            if not clean_text(config.get(field))
        ]

        if missing:
            st.error(f"Could not load required eBay secret fields: {', '.join(missing)}")

            with st.expander("eBay secrets debug - key names only", expanded=True):
                st.write(f"Secrets source checked: `{source_label}`")

                try:
                    if ebay_section is not None:
                        st.write("Keys found under `[ebay]`:")
                        st.code("\n".join(list(ebay_section.keys())))
                    else:
                        st.write("No `[ebay]` section found. Top-level keys found:")
                        st.code("\n".join(list(st.secrets.keys())))
                except Exception as exc:
                    st.write(f"Could not list secret keys: {exc}")

                st.write("Expected format:")
                st.code(
                    """
[ebay]
environment = "production"
marketplace_id = "EBAY_US"
client_id = "..."
client_secret = "..."
ru_name = "..."
scopes = "..."
refresh_token = "..."
""".strip(),
                    language="toml",
                )

            return None

        return config

    except Exception as e:
        st.error("Could not load eBay secrets from Streamlit secrets.")
        st.exception(e)
        return None


def get_access_token_from_refresh_token(ebay_config):
    token_url = "https://api.ebay.com/identity/v1/oauth2/token"

    credentials = f"{ebay_config['client_id']}:{ebay_config['client_secret']}"
    encoded_credentials = base64.b64encode(credentials.encode("utf-8")).decode("utf-8")

    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Authorization": f"Basic {encoded_credentials}",
    }

    data = {
        "grant_type": "refresh_token",
        "refresh_token": ebay_config["refresh_token"],
        "scope": ebay_config["scopes"],
    }

    response = requests.post(token_url, headers=headers, data=data, timeout=30)

    try:
        payload = response.json()
    except Exception:
        payload = {"raw_response": response.text}

    return response.status_code, payload


def get_access_token_or_stop(ebay_config):
    with st.spinner("Getting eBay access token..."):
        token_status, token_payload = get_access_token_from_refresh_token(ebay_config)

    if token_status != 200:
        st.error(f"Could not get eBay access token. Status code: {token_status}")
        st.write(token_payload)
        st.stop()

    access_token = token_payload.get("access_token")

    if not access_token:
        st.error("eBay did not return an access token.")
        st.write(token_payload)
        st.stop()

    return access_token


# =========================================================
# Generic helpers
# =========================================================

def _as_bool(x) -> bool:
    if isinstance(x, bool):
        return x

    try:
        if pd.isna(x):
            return False
    except Exception:
        pass

    s = str(x).strip().lower()
    return s in {"true", "t", "yes", "y", "1"}


def _bool_count(series: pd.Series) -> int:
    if series is None:
        return 0
    return int(series.apply(_as_bool).sum())


def _amount_value(obj) -> float:
    if obj is None:
        return 0.0
    if isinstance(obj, dict):
        return to_money(obj.get("value"))
    return to_money(obj)


def _safe_df(df: pd.DataFrame | None) -> pd.DataFrame:
    return pd.DataFrame() if df is None else df.copy()


def _ensure_cols(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    out = df.copy()
    for col in cols:
        if col not in out.columns:
            out[col] = ""
    return out


def _safe_money_round(x) -> float:
    return round(to_money(x), 2)


# =========================================================
# Active listing profit estimator helpers
# =========================================================

DEFAULT_LISTING_ESTIMATOR_ASSUMPTIONS = {
    "fee_rate_pct": 13.25,
    "fixed_order_fee": 0.40,
    "promoted_listing_pct": 0.0,
    "estimated_buyer_tax_pct": 0.0,
    "standard_envelope_label_cost": 1.32,
    "ground_advantage_label_cost": 5.30,
    "other_label_cost": 5.30,
    "extra_per_order_cost": 0.00,
}


def _pct_to_rate(x) -> float:
    return max(to_money(x), 0.0) / 100.0


def _extract_primary_shipping_details(item) -> dict:
    """Pull the first domestic shipping option eBay returns for an active listing."""
    shipping_type = _xml_text(item, "e:ShippingDetails/e:ShippingType")
    shipping_profile_name = _xml_text(item, "e:SellerProfiles/e:SellerShippingProfile/e:ShippingProfileName")

    primary = {}
    options = item.findall("e:ShippingDetails/e:ShippingServiceOptions", EBAY_XML_NS)

    for opt in options:
        service = clean_text(_xml_text(opt, "e:ShippingService"))
        service_cost = _xml_money(opt, "e:ShippingServiceCost")
        free_shipping = _as_bool(_xml_text(opt, "e:FreeShipping"))
        priority = int(to_money(_xml_text(opt, "e:ShippingServicePriority", "999")) or 999)

        candidate = {
            "shipping_type": shipping_type,
            "shipping_profile_name": shipping_profile_name,
            "shipping_service": service,
            "buyer_shipping_charged": 0.0 if free_shipping else round(service_cost, 2),
            "free_shipping": free_shipping,
            "shipping_service_priority": priority,
        }

        if not primary or candidate["shipping_service_priority"] < primary.get("shipping_service_priority", 999):
            primary = candidate

    if primary:
        return primary

    return {
        "shipping_type": shipping_type,
        "shipping_profile_name": shipping_profile_name,
        "shipping_service": "",
        "buyer_shipping_charged": 0.0,
        "free_shipping": False,
        "shipping_service_priority": "",
    }


def _shipping_option_bucket(shipping_service: str, shipping_type: str = "", shipping_profile_name: str = "") -> str:
    raw = " ".join(
        [
            clean_text(shipping_service),
            clean_text(shipping_type),
            clean_text(shipping_profile_name),
        ]
    ).lower()
    compact = re.sub(r"[^a-z0-9]", "", raw)

    if "standardenvelope" in compact or "ebaystandard" in compact:
        return "eBay Standard Envelope"

    if "groundadvantage" in compact:
        return "USPS Ground Advantage"

    if "firstclass" in compact or "uspsparcel" in compact or "priority" in compact:
        return "USPS Ground Advantage"

    if raw.strip():
        return "Other / Unknown"

    return "Missing from eBay pull"


def _estimated_label_cost_for_shipping_option(shipping_option: str, assumptions: dict) -> float:
    option = clean_text(shipping_option).lower()

    if "standard envelope" in option:
        return round(to_money(assumptions.get("standard_envelope_label_cost")), 2)

    if "ground advantage" in option:
        return round(to_money(assumptions.get("ground_advantage_label_cost")), 2)

    return round(to_money(assumptions.get("other_label_cost")), 2)


def _estimate_listing_math(
    list_price: float,
    buyer_shipping_charged: float,
    total_cost: float,
    shipping_option: str,
    assumptions: dict,
    target_profit_pct: float = 0.0,
) -> dict:
    """
    Estimate eBay net proceeds and solve the list price needed for a target ROI.

    The target_profit_pct value is treated as ROI on total_cost:
    0.05 means cost + 5% of cost in profit.
    """
    list_price = round(to_money(list_price), 2)
    buyer_shipping_charged = round(to_money(buyer_shipping_charged), 2)
    total_cost = round(to_money(total_cost), 2)

    fee_rate = _pct_to_rate(assumptions.get("fee_rate_pct"))
    ad_rate = _pct_to_rate(assumptions.get("promoted_listing_pct"))
    buyer_tax_rate = _pct_to_rate(assumptions.get("estimated_buyer_tax_pct"))
    fixed_order_fee = round(to_money(assumptions.get("fixed_order_fee")), 2)
    extra_per_order_cost = round(to_money(assumptions.get("extra_per_order_cost")), 2)
    label_cost = _estimated_label_cost_for_shipping_option(shipping_option, assumptions)

    buyer_subtotal = round(list_price + buyer_shipping_charged, 2)
    estimated_tax = round(buyer_subtotal * buyer_tax_rate, 2)
    fee_basis = round(buyer_subtotal + estimated_tax, 2)
    estimated_variable_fee = round(fee_basis * fee_rate, 2)
    estimated_ad_fee = round(list_price * ad_rate, 2)
    estimated_total_fees = round(estimated_variable_fee + fixed_order_fee + estimated_ad_fee, 2)
    estimated_net = round(buyer_subtotal - estimated_total_fees - label_cost - extra_per_order_cost, 2)
    estimated_profit = round(estimated_net - total_cost, 2)

    target_profit = round(total_cost * max(to_money(target_profit_pct), 0.0), 2)
    desired_net = round(total_cost + target_profit, 2)

    # net = price + shipping - fee_rate * ((price + shipping) * (1 + tax_rate)) - ad_rate * price - fixed - label - extra
    denominator = 1 - (fee_rate * (1 + buyer_tax_rate)) - ad_rate
    shipping_net_after_fee = buyer_shipping_charged * (1 - (fee_rate * (1 + buyer_tax_rate)))

    if denominator <= 0:
        required_price = 0.0
    else:
        required_price = (
            desired_net
            + fixed_order_fee
            + label_cost
            + extra_per_order_cost
            - shipping_net_after_fee
        ) / denominator

    required_price = round(max(required_price, 0.0), 2)

    return {
        "estimated_buyer_total_before_tax": buyer_subtotal,
        "estimated_tax_for_fee_basis": estimated_tax,
        "estimated_fee_basis": fee_basis,
        "estimated_ebay_fees": estimated_total_fees,
        "estimated_label_cost": label_cost,
        "estimated_extra_cost": extra_per_order_cost,
        "estimated_net_proceeds": estimated_net,
        "estimated_profit_loss": estimated_profit,
        "required_price": required_price,
    }


def _build_assigned_listing_estimate_df(assigned: pd.DataFrame, inv: pd.DataFrame, assumptions: dict) -> pd.DataFrame:
    if assigned.empty:
        return pd.DataFrame()

    rows = []

    for _, listing in assigned.iterrows():
        ebay_item_id = clean_text(listing.get("ebay_item_id"))
        match_df = _find_inventory_match_by_ebay_id(inv, ebay_item_id)

        if match_df.empty:
            inv_match = pd.Series(dtype=object)
        else:
            inv_match = match_df.iloc[0]

        shipping_service = clean_text(listing.get("shipping_service"))
        shipping_type = clean_text(listing.get("shipping_type"))
        shipping_profile_name = clean_text(listing.get("shipping_profile_name"))
        shipping_option = _shipping_option_bucket(shipping_service, shipping_type, shipping_profile_name)

        list_price = to_money(listing.get("current_price"))
        buyer_shipping_charged = to_money(listing.get("buyer_shipping_charged"))
        total_cost = to_money(inv_match.get("total_cost"))

        current_math = _estimate_listing_math(
            list_price=list_price,
            buyer_shipping_charged=buyer_shipping_charged,
            total_cost=total_cost,
            shipping_option=shipping_option,
            assumptions=assumptions,
            target_profit_pct=0.0,
        )
        breakeven_math = _estimate_listing_math(
            list_price=list_price,
            buyer_shipping_charged=buyer_shipping_charged,
            total_cost=total_cost,
            shipping_option=shipping_option,
            assumptions=assumptions,
            target_profit_pct=0.0,
        )
        five_pct_math = _estimate_listing_math(
            list_price=list_price,
            buyer_shipping_charged=buyer_shipping_charged,
            total_cost=total_cost,
            shipping_option=shipping_option,
            assumptions=assumptions,
            target_profit_pct=0.05,
        )
        ten_pct_math = _estimate_listing_math(
            list_price=list_price,
            buyer_shipping_charged=buyer_shipping_charged,
            total_cost=total_cost,
            shipping_option=shipping_option,
            assumptions=assumptions,
            target_profit_pct=0.10,
        )

        rows.append(
            {
                "assigned": _as_bool(listing.get("assigned")),
                "inventory_id": clean_text(inv_match.get("inventory_id")),
                "inventory_status": clean_text(inv_match.get("inventory_status")),
                "card_name": clean_text(inv_match.get("card_name")),
                "card_number": clean_text(inv_match.get("card_number")),
                "set_name": clean_text(inv_match.get("set_name")),
                "total_cost": round(total_cost, 2),
                "ebay_item_id": ebay_item_id,
                "title": clean_text(listing.get("title")),
                "listing_status": clean_text(listing.get("listing_status")),
                "current_price": round(list_price, 2),
                "shipping_option": shipping_option,
                "shipping_service": shipping_service,
                "buyer_shipping_charged": round(buyer_shipping_charged, 2),
                "free_shipping": _as_bool(listing.get("free_shipping")),
                "estimated_label_cost": current_math["estimated_label_cost"],
                "estimated_ebay_fees": current_math["estimated_ebay_fees"],
                "estimated_net_proceeds": current_math["estimated_net_proceeds"],
                "estimated_profit_loss": current_math["estimated_profit_loss"],
                "break_even_price": breakeven_math["required_price"],
                "price_for_5pct_profit": five_pct_math["required_price"],
                "price_for_10pct_profit": ten_pct_math["required_price"],
                "quantity_available": int(to_money(listing.get("quantity_available")) or 0),
                "quantity_sold": int(to_money(listing.get("quantity_sold")) or 0),
                "listing_start_date": clean_text(listing.get("listing_start_date")),
                "listing_end_date": clean_text(listing.get("listing_end_date")),
                "listing_url": clean_text(listing.get("listing_url")),
            }
        )

    out = pd.DataFrame(rows)

    if not out.empty:
        out = out.sort_values(["estimated_profit_loss", "current_price"], ascending=[True, False]).reset_index(drop=True)

    return out


def _assigned_listing_estimate_cols() -> list[str]:
    return [
        "assigned",
        "inventory_id",
        "inventory_status",
        "card_name",
        "card_number",
        "set_name",
        "total_cost",
        "ebay_item_id",
        "title",
        "listing_status",
        "current_price",
        "shipping_option",
        "buyer_shipping_charged",
        "estimated_label_cost",
        "estimated_ebay_fees",
        "estimated_net_proceeds",
        "estimated_profit_loss",
        "break_even_price",
        "price_for_5pct_profit",
        "price_for_10pct_profit",
        "quantity_available",
        "quantity_sold",
        "listing_start_date",
        "listing_end_date",
        "listing_url",
    ]


def _style_loss_rows(row: pd.Series) -> list[str]:
    profit = to_money(row.get("estimated_profit_loss"))

    if profit >= 0:
        return ["" for _ in row.index]

    cost = max(to_money(row.get("total_cost")), 1.0)
    loss_ratio = min(abs(profit) / cost, 1.0)
    alpha = round(0.18 + (0.55 * loss_ratio), 3)

    return [f"background-color: rgba(255, 0, 0, {alpha}); color: #111111;" for _ in row.index]


# =========================================================
# XML helpers for Trading API
# =========================================================

EBAY_XML_NS = {"e": "urn:ebay:apis:eBLBaseComponents"}
TRADING_API_ENDPOINT = "https://api.ebay.com/ws/api.dll"
FINANCES_API_ENDPOINT = "https://apiz.ebay.com/sell/finances/v1/transaction"


def _xml_text(node, path: str, default: str = "") -> str:
    if node is None:
        return default

    found = node.find(path, EBAY_XML_NS)
    if found is None or found.text is None:
        return default

    return str(found.text).strip()


def _xml_money(node, path: str) -> float:
    return to_money(_xml_text(node, path, "0"))


def _xml_attr(node, path: str, attr: str, default: str = "") -> str:
    if node is None:
        return default

    found = node.find(path, EBAY_XML_NS)
    if found is None:
        return default

    return str(found.attrib.get(attr, default)).strip()


def _parse_ebay_datetime_to_date(x: str) -> str:
    txt = clean_text(x)
    if not txt:
        return ""

    parsed = pd.to_datetime(txt, errors="coerce", utc=True)
    if pd.isna(parsed):
        return txt

    return str(parsed.date())


def call_trading_api(access_token: str, call_name: str, xml_body: str):
    headers = {
        "X-EBAY-API-CALL-NAME": call_name,
        "X-EBAY-API-SITEID": "0",
        "X-EBAY-API-COMPATIBILITY-LEVEL": "1231",
        "X-EBAY-API-IAF-TOKEN": access_token,
        "Content-Type": "text/xml",
    }

    response = requests.post(
        TRADING_API_ENDPOINT,
        data=xml_body.encode("utf-8"),
        headers=headers,
        timeout=45,
    )

    return response.status_code, response.text


def _extract_trading_errors(xml_text: str) -> list[str]:
    try:
        root = ET.fromstring(xml_text)
    except Exception:
        return ["Could not parse XML response from eBay."]

    errors = []

    for err in root.findall(".//e:Errors", EBAY_XML_NS):
        severity = _xml_text(err, "e:SeverityCode")
        code = _xml_text(err, "e:ErrorCode")
        short_msg = _xml_text(err, "e:ShortMessage")
        long_msg = _xml_text(err, "e:LongMessage")

        msg = " | ".join([x for x in [severity, code, short_msg, long_msg] if x])
        if msg:
            errors.append(msg)

    return errors


# =========================================================
# eBay active listing pull
# =========================================================

def get_active_listings(
    access_token: str,
    entries_per_page: int = 100,
    max_pages: int = 5,
) -> tuple[pd.DataFrame, dict]:
    all_rows = []
    audit = {
        "pages_requested": 0,
        "acks": [],
        "errors": [],
        "raw_last_response": "",
    }

    entries_per_page = int(max(1, min(entries_per_page, 200)))
    max_pages = int(max(1, min(max_pages, 25)))

    for page_number in range(1, max_pages + 1):
        xml_body = f"""<?xml version="1.0" encoding="utf-8"?>
<GetMyeBaySellingRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <DetailLevel>ReturnAll</DetailLevel>
  <ActiveList>
    <Include>true</Include>
    <Sort>TimeLeft</Sort>
    <Pagination>
      <EntriesPerPage>{entries_per_page}</EntriesPerPage>
      <PageNumber>{page_number}</PageNumber>
    </Pagination>
  </ActiveList>
</GetMyeBaySellingRequest>"""

        status_code, xml_text = call_trading_api(
            access_token=access_token,
            call_name="GetMyeBaySelling",
            xml_body=xml_body,
        )

        audit["pages_requested"] += 1
        audit["raw_last_response"] = xml_text

        if status_code != 200:
            audit["errors"].append(f"HTTP {status_code}: {xml_text[:1000]}")
            break

        try:
            root = ET.fromstring(xml_text)
        except Exception as exc:
            audit["errors"].append(f"Could not parse XML response: {exc}")
            break

        ack = _xml_text(root, "e:Ack")
        audit["acks"].append(ack)

        errors = _extract_trading_errors(xml_text)
        if errors:
            audit["errors"].extend(errors)

        if ack not in {"Success", "Warning"}:
            break

        active_list = root.find(".//e:ActiveList", EBAY_XML_NS)
        if active_list is None:
            break

        total_pages = int(to_money(_xml_text(active_list, "e:PaginationResult/e:TotalNumberOfPages", "1")) or 1)
        item_nodes = active_list.findall(".//e:ItemArray/e:Item", EBAY_XML_NS)

        for item in item_nodes:
            ebay_item_id = _xml_text(item, "e:ItemID")
            title = _xml_text(item, "e:Title")
            sku = _xml_text(item, "e:SKU")
            listing_type = _xml_text(item, "e:ListingType")
            listing_status = _xml_text(item, "e:SellingStatus/e:ListingStatus")
            quantity = int(to_money(_xml_text(item, "e:Quantity", "0")))
            quantity_sold = int(to_money(_xml_text(item, "e:SellingStatus/e:QuantitySold", "0")))
            quantity_available = max(quantity - quantity_sold, 0)
            current_price = _xml_money(item, "e:SellingStatus/e:CurrentPrice")
            currency = _xml_attr(item, "e:SellingStatus/e:CurrentPrice", "currencyID")
            listing_url = _xml_text(item, "e:ListingDetails/e:ViewItemURL")
            start_time = _xml_text(item, "e:ListingDetails/e:StartTime")
            end_time = _xml_text(item, "e:ListingDetails/e:EndTime")
            image_url = _xml_text(item, "e:PictureDetails/e:GalleryURL")
            shipping_details = _extract_primary_shipping_details(item)

            if ebay_item_id:
                all_rows.append(
                    {
                        "ebay_item_id": ebay_item_id,
                        "ebay_listing_id": ebay_item_id,
                        "title": title,
                        "sku": sku,
                        "listing_type": listing_type,
                        "listing_status": listing_status,
                        "current_price": current_price,
                        "currency": currency,
                        "quantity": quantity,
                        "quantity_sold": quantity_sold,
                        "quantity_available": quantity_available,
                        "shipping_type": shipping_details.get("shipping_type", ""),
                        "shipping_profile_name": shipping_details.get("shipping_profile_name", ""),
                        "shipping_service": shipping_details.get("shipping_service", ""),
                        "buyer_shipping_charged": shipping_details.get("buyer_shipping_charged", 0.0),
                        "free_shipping": shipping_details.get("free_shipping", False),
                        "listing_start_date": _parse_ebay_datetime_to_date(start_time),
                        "listing_end_date": _parse_ebay_datetime_to_date(end_time),
                        "listing_url": listing_url,
                        "image_url": image_url,
                    }
                )

        if page_number >= total_pages:
            break

    df = pd.DataFrame(all_rows)

    if not df.empty:
        df = df.drop_duplicates(subset=["ebay_item_id"], keep="last").reset_index(drop=True)

    return df, audit


# =========================================================
# Fulfillment API order helpers
# =========================================================

def get_recent_orders(access_token, days_back=30, limit=100):
    end_dt = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=days_back)

    start_text = start_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    end_text = end_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    url = "https://api.ebay.com/sell/fulfillment/v1/order"

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }

    params = {
        "filter": f"creationdate:[{start_text}..{end_text}]",
        "limit": str(limit),
        "offset": "0",
        "fieldGroups": "TAX_BREAKDOWN",
    }

    response = requests.get(url, headers=headers, params=params, timeout=30)

    try:
        payload = response.json()
    except Exception:
        payload = {"raw_response": response.text}

    return response.status_code, payload, params


def _extract_order_tax_total(order: dict) -> float:
    pricing_summary = order.get("pricingSummary", {}) or {}

    order_level_tax = _amount_value(pricing_summary.get("tax"))
    if order_level_tax > 0:
        return round(order_level_tax, 2)

    safe_line_tax = 0.0

    for item in order.get("lineItems", []) or []:
        for tax_container_name in ["taxes", "ebayCollectAndRemitTaxes", "taxBreakdown"]:
            tax_container = item.get(tax_container_name)

            if isinstance(tax_container, list):
                for tax_item in tax_container:
                    if isinstance(tax_item, dict):
                        safe_line_tax += _amount_value(tax_item.get("amount"))
                        safe_line_tax += _amount_value(tax_item.get("taxAmount"))
                    else:
                        safe_line_tax += _amount_value(tax_item)

            elif isinstance(tax_container, dict):
                safe_line_tax += _amount_value(tax_container.get("amount"))
                safe_line_tax += _amount_value(tax_container.get("taxAmount"))

    return round(safe_line_tax, 2)


def _calculate_order_total_including_tax(
    order_total_value: float,
    line_item_sum: float,
    shipping_value: float,
    tax_value: float,
) -> float:
    """
    With fieldGroups=TAX_BREAKDOWN, eBay may include collect/remit tax in pricingSummary.total.
    If total already includes tax, use it. If not, add tax once.
    """
    base_with_tax = round(line_item_sum + shipping_value + tax_value, 2)

    if order_total_value >= base_with_tax - 0.01:
        return round(order_total_value, 2)

    if tax_value > 0:
        return round(order_total_value + tax_value, 2)

    return round(order_total_value, 2)


def flatten_orders(order_payload):
    rows = []

    for order in order_payload.get("orders", []):
        order_id = order.get("orderId")
        creation_date = order.get("creationDate")
        order_status = order.get("orderFulfillmentStatus")
        payment_status = order.get("orderPaymentStatus")

        pricing_summary = order.get("pricingSummary", {}) or {}
        total = pricing_summary.get("total", {}) or {}
        subtotal = pricing_summary.get("priceSubtotal", {}) or {}
        delivery_cost = pricing_summary.get("deliveryCost", {}) or {}

        order_total_value = to_money(total.get("value"))
        order_subtotal_value = to_money(subtotal.get("value"))
        shipping_value = to_money(delivery_cost.get("value"))
        tax_value = _extract_order_tax_total(order)
        total_currency = total.get("currency")

        line_items = order.get("lineItems", []) or []
        line_count = max(len(line_items), 1)

        line_item_values = []
        for item in line_items:
            line_cost = item.get("lineItemCost", {}) or {}
            line_item_values.append(to_money(line_cost.get("value")))

        line_item_sum = sum(line_item_values) or order_subtotal_value

        order_total_including_tax = _calculate_order_total_including_tax(
            order_total_value=order_total_value,
            line_item_sum=line_item_sum,
            shipping_value=shipping_value,
            tax_value=tax_value,
        )

        for item in line_items:
            line_cost = item.get("lineItemCost", {}) or {}
            line_item_value = to_money(line_cost.get("value"))

            legacy_item_id = clean_text(item.get("legacyItemId"))
            item_id = clean_text(item.get("itemId"))
            ebay_item_id = legacy_item_id or item_id

            if line_count == 1:
                allocated_shipping = shipping_value
                allocated_tax = tax_value
                allocated_order_total = order_total_including_tax
            else:
                if line_item_sum > 0:
                    ratio = line_item_value / line_item_sum
                    allocated_shipping = round(shipping_value * ratio, 2)
                    allocated_tax = round(tax_value * ratio, 2)
                    allocated_order_total = round(order_total_including_tax * ratio, 2)
                else:
                    allocated_shipping = round(shipping_value / line_count, 2) if shipping_value else 0.0
                    allocated_tax = round(tax_value / line_count, 2) if tax_value else 0.0
                    allocated_order_total = round(order_total_including_tax / line_count, 2)

            rows.append(
                {
                    "ebay_order_id": order_id,
                    "creation_date": creation_date,
                    "sold_date": _parse_ebay_datetime_to_date(creation_date),
                    "order_status": order_status,
                    "payment_status": payment_status,
                    "ebay_line_item_id": item.get("lineItemId"),
                    "ebay_item_id": ebay_item_id,
                    "legacy_item_id": legacy_item_id,
                    "item_id": item_id,
                    "sku": item.get("sku"),
                    "title": item.get("title"),
                    "quantity": item.get("quantity"),
                    "item_price": line_item_value,
                    "shipping_charged": allocated_shipping,
                    "tax_collected": allocated_tax,
                    "sold_price": allocated_order_total,
                    "line_item_currency": line_cost.get("currency"),
                    "order_subtotal_value": order_subtotal_value,
                    "order_total_value": order_total_value,
                    "order_tax_value": tax_value,
                    "order_total_including_tax": order_total_including_tax,
                    "order_total_currency": total_currency,
                    "order_line_count": line_count,
                }
            )

    return pd.DataFrame(rows)


# =========================================================
# Finances API helpers
# =========================================================

def get_finance_transactions_for_order(access_token: str, marketplace_id: str, order_id: str):
    order_id = clean_text(order_id)

    if not order_id:
        return 0, {"transactions": []}, {}

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "X-EBAY-C-MARKETPLACE-ID": marketplace_id or "EBAY_US",
    }

    params = {
        "filter": f"orderId:{{{order_id}}}",
        "limit": "1000",
        "offset": "0",
    }

    response = requests.get(
        FINANCES_API_ENDPOINT,
        headers=headers,
        params=params,
        timeout=30,
    )

    if response.status_code == 204:
        return 204, {"transactions": []}, params

    try:
        payload = response.json()
    except Exception:
        payload = {"raw_response": response.text, "transactions": []}

    return response.status_code, payload, params


def _choose_sale_transaction(finance_payload: dict, order_id: str) -> dict:
    transactions = finance_payload.get("transactions", []) or []
    order_id = clean_text(order_id)

    sale_transactions = [
        t for t in transactions
        if clean_text(t.get("transactionType")).upper() == "SALE"
        and clean_text(t.get("orderId")) == order_id
    ]

    if sale_transactions:
        return sale_transactions[0]

    sale_transactions = [
        t for t in transactions
        if clean_text(t.get("transactionType")).upper() == "SALE"
    ]

    if sale_transactions:
        return sale_transactions[0]

    return transactions[0] if transactions else {}


def _finance_negative_adjustments(finance_payload: dict) -> float:
    transactions = finance_payload.get("transactions", []) or []
    total = 0.0

    for tx in transactions:
        tx_type = clean_text(tx.get("transactionType")).upper()

        if tx_type == "SALE":
            continue

        amount = _amount_value(tx.get("amount"))

        if amount < 0:
            total += abs(amount)

    return round(total, 2)


def _line_finance_basis_from_transaction(sale_tx: dict, line_item_id: str) -> tuple[float, float]:
    line_item_id = clean_text(line_item_id)
    line_items = sale_tx.get("orderLineItems", []) or []

    if not line_items:
        return 0.0, 0.0

    target = None

    for li in line_items:
        if clean_text(li.get("lineItemId")) == line_item_id:
            target = li
            break

    if target is None and len(line_items) == 1:
        target = line_items[0]

    if target is None:
        return 0.0, 0.0

    basis = _amount_value(target.get("feeBasisAmount"))

    line_fee = 0.0

    for fee in target.get("marketplaceFees", []) or []:
        line_fee += _amount_value(fee.get("amount"))

    for donation in target.get("donations", []) or []:
        line_fee += _amount_value(donation.get("amount"))

    return basis, line_fee


def _finance_values_for_order_line(order_row: pd.Series, finance_payload: dict) -> dict:
    order_id = clean_text(order_row.get("ebay_order_id"))
    line_item_id = clean_text(order_row.get("ebay_line_item_id"))
    sale_tx = _choose_sale_transaction(finance_payload, order_id)

    fallback_sold_price = to_money(order_row.get("sold_price"))

    if not sale_tx:
        return {
            "finance_found": False,
            "finance_status": "No Finances SALE transaction found yet",
            "finance_transaction_id": "",
            "finance_payout_id": "",
            "finance_gross": fallback_sold_price,
            "finance_net": fallback_sold_price,
            "finance_fees": 0.0,
            "finance_fee_source": "fallback_order_no_finance",
            "finance_label_cost": 0.0,
            "finance_tax_or_basis_extra": to_money(order_row.get("tax_collected")),
        }

    row_buyer_total_including_tax = to_money(order_row.get("sold_price"))
    row_shipping_charged = to_money(order_row.get("shipping_charged"))
    row_tax_collected = to_money(order_row.get("tax_collected"))
    order_total_including_tax = to_money(order_row.get("order_total_including_tax"))
    line_count = int(to_money(order_row.get("order_line_count")) or 1)

    sale_amount_net_before_adjustments = abs(_amount_value(sale_tx.get("amount")))
    sale_total_fee_basis = _amount_value(sale_tx.get("totalFeeBasisAmount"))

    order_negative_adjustments = _finance_negative_adjustments(finance_payload)

    # Sold price = buyer-paid gross. This should be item + shipping + tax for single-line orders.
    order_gross = max(
        to_money(row_buyer_total_including_tax),
        to_money(order_total_including_tax),
        to_money(sale_total_fee_basis),
    )

    if order_negative_adjustments > 0:
        order_label_or_adjustment_cost = order_negative_adjustments
        adjustment_source = "finance_negative_transactions"
    else:
        # If label transaction is not yet returned with this order, use buyer shipping as practical label fallback.
        # This matches your Quaxly example: finance net before label - $1.32 shipping = final net proceeds.
        order_label_or_adjustment_cost = row_shipping_charged
        adjustment_source = "fallback_shipping_charged_as_label_cost"

    order_net_after_adjustments = round(sale_amount_net_before_adjustments - order_label_or_adjustment_cost, 2)

    if order_net_after_adjustments < 0:
        order_net_after_adjustments = sale_amount_net_before_adjustments

    if line_count <= 1:
        line_gross = order_gross
        line_label_or_adjustment_cost = order_label_or_adjustment_cost
        line_net = order_net_after_adjustments
        line_fees = round(line_gross - line_net, 2)
    else:
        line_basis, line_fee_direct = _line_finance_basis_from_transaction(sale_tx, line_item_id)
        line_gross = max(row_buyer_total_including_tax, line_basis)

        if order_gross > 0 and line_gross > 0:
            ratio = line_gross / order_gross
        else:
            ratio = 1 / max(line_count, 1)

        line_label_or_adjustment_cost = round(order_label_or_adjustment_cost * ratio, 2)

        if sale_amount_net_before_adjustments > 0:
            line_net_before_adjustments = round(sale_amount_net_before_adjustments * ratio, 2)
        else:
            line_net_before_adjustments = round(line_gross - line_fee_direct, 2)

        line_net = round(line_net_before_adjustments - line_label_or_adjustment_cost, 2)

        if line_net < 0:
            line_net = max(0.0, line_net_before_adjustments)

        line_fees = round(line_gross - line_net, 2)

    line_fees = round(max(line_fees, 0.0), 2)

    tax_or_basis_extra = round(
        max(row_tax_collected, order_gross - to_money(order_row.get("order_total_value")), 0.0),
        2,
    )

    return {
        "finance_found": True,
        "finance_status": "Finances API matched",
        "finance_transaction_id": clean_text(sale_tx.get("transactionId")),
        "finance_payout_id": clean_text(sale_tx.get("payoutId")),
        "finance_gross": round(line_gross, 2),
        "finance_net": round(line_net, 2),
        "finance_fees": line_fees,
        "finance_fee_source": f"sold_price_minus_net__{adjustment_source}",
        "finance_label_cost": round(line_label_or_adjustment_cost, 2),
        "finance_tax_or_basis_extra": tax_or_basis_extra,
    }


def build_finance_map_for_orders(access_token: str, marketplace_id: str, orders_df: pd.DataFrame) -> tuple[dict, dict]:
    finance_by_order_id = {}
    audit = {}

    if orders_df.empty or "ebay_order_id" not in orders_df.columns:
        return finance_by_order_id, audit

    order_ids = (
        orders_df["ebay_order_id"]
        .dropna()
        .astype(str)
        .str.strip()
        .replace("", pd.NA)
        .dropna()
        .drop_duplicates()
        .tolist()
    )

    for order_id in order_ids:
        status, payload, params = get_finance_transactions_for_order(
            access_token=access_token,
            marketplace_id=marketplace_id,
            order_id=order_id,
        )

        audit[order_id] = {
            "status_code": status,
            "params": params,
            "transaction_count": len(payload.get("transactions", []) or []),
            "payload": payload,
        }

        finance_by_order_id[order_id] = payload

    return finance_by_order_id, audit


# =========================================================
# Inventory matching / sync helpers
# =========================================================

def _inventory_label(row: pd.Series) -> str:
    inv_id = clean_text(row.get("inventory_id"))
    status = clean_text(row.get("inventory_status"))
    set_name = clean_text(row.get("set_name"))
    card_name = clean_text(row.get("card_name"))
    card_number = clean_text(row.get("card_number"))
    variant = clean_text(row.get("variant"))
    grade = clean_text(row.get("grade"))
    cost = money_fmt(row.get("total_cost"))
    market = money_fmt(row.get("market_value"))

    bits = [inv_id, status, set_name, card_name]

    if card_number:
        bits.append(f"#{card_number}")
    if variant:
        bits.append(variant)
    if grade:
        bits.append(f"Grade {grade}")

    bits.append(f"Cost {cost}")
    bits.append(f"Market {market}")

    return " — ".join([b for b in bits if clean_text(b)])


def _assigned_ebay_ids(inv: pd.DataFrame) -> set[str]:
    if inv.empty:
        return set()

    ids = set()

    for col in ["ebay_item_id", "ebay_listing_id"]:
        if col in inv.columns:
            ids.update(
                inv[col]
                .dropna()
                .astype(str)
                .str.strip()
                .replace("", pd.NA)
                .dropna()
                .tolist()
            )

    return ids


def _find_inventory_match_by_ebay_id(inv: pd.DataFrame, ebay_item_id: str) -> pd.DataFrame:
    if inv.empty:
        return pd.DataFrame()

    ebay_item_id = clean_text(ebay_item_id)
    if not ebay_item_id:
        return pd.DataFrame()

    mask = pd.Series(False, index=inv.index)

    for col in ["ebay_item_id", "ebay_listing_id"]:
        if col in inv.columns:
            mask = mask | inv[col].astype(str).str.strip().eq(ebay_item_id)

    return inv[mask].copy()


def _inventory_ready_for_ebay_assignment(inv: pd.DataFrame) -> pd.DataFrame:
    if inv.empty:
        return pd.DataFrame()

    ready = inv[
        inv["inventory_status"].astype(str).str.upper().isin([STATUS_ACTIVE, STATUS_LISTED])
    ].copy()

    if ready.empty:
        return ready

    for col in ["ebay_item_id", "ebay_listing_id"]:
        if col not in ready.columns:
            ready[col] = ""

    already_linked = (
        ready["ebay_item_id"].astype(str).str.strip().ne("")
        | ready["ebay_listing_id"].astype(str).str.strip().ne("")
    )

    return ready[~already_linked].copy()


def _score_inventory_match(row: pd.Series, listing_title: str) -> int:
    title = clean_text(listing_title).lower()
    if not title:
        return 0

    s = 0

    card_name = clean_text(row.get("card_name")).lower()
    set_name = clean_text(row.get("set_name")).lower()
    card_number = clean_text(row.get("card_number")).lower()
    variant = clean_text(row.get("variant")).lower()
    grade = clean_text(row.get("grade")).lower()

    if card_name and card_name in title:
        s += 50
    if set_name and set_name in title:
        s += 30
    if card_number and re.search(rf"(^|\D){re.escape(card_number)}($|\D)", title):
        s += 25
    if variant and variant in title:
        s += 10
    if grade and grade in title:
        s += 10

    return s


def _inventory_option_map(inv: pd.DataFrame) -> tuple[list[str], dict[str, str]]:
    ready = _inventory_ready_for_ebay_assignment(inv)

    if ready.empty:
        return [""], {"": ""}

    ready = ready.sort_values(["market_value", "card_name"], ascending=[False, True])

    options = [""]
    mapping = {"": ""}

    for _, row in ready.iterrows():
        label = _inventory_label(row)
        inv_id = clean_text(row.get("inventory_id"))

        if label and inv_id:
            options.append(label)
            mapping[label] = inv_id

    return options, mapping


def _best_inventory_label_for_listing(
    inv: pd.DataFrame,
    listing_title: str,
    exclude_inventory_ids: set[str] | None = None,
) -> tuple[str, str, int]:
    ready = _inventory_ready_for_ebay_assignment(inv)

    if ready.empty:
        return "", "", 0

    exclude_inventory_ids = exclude_inventory_ids or set()

    ready["inventory_id"] = ready["inventory_id"].astype(str).str.strip()
    ready = ready[~ready["inventory_id"].isin(exclude_inventory_ids)].copy()

    if ready.empty:
        return "", "", 0

    ready["__match_score"] = ready.apply(
        lambda r: _score_inventory_match(r, listing_title),
        axis=1,
    )

    ready = ready.sort_values(
        ["__match_score", "market_value"],
        ascending=[False, False],
    )

    best = ready.iloc[0]
    score = int(best.get("__match_score", 0) or 0)

    if score <= 0:
        return "", "", 0

    return _inventory_label(best), clean_text(best.get("inventory_id")), score


def _build_order_sync_df(inv: pd.DataFrame, orders_df: pd.DataFrame, finance_by_order_id: dict | None = None) -> pd.DataFrame:
    if orders_df.empty:
        return pd.DataFrame()

    finance_by_order_id = finance_by_order_id or {}
    matched_rows = []

    for _, order_row in orders_df.iterrows():
        ebay_item_id = clean_text(order_row.get("ebay_item_id"))
        order_id = clean_text(order_row.get("ebay_order_id"))
        match_df = _find_inventory_match_by_ebay_id(inv, ebay_item_id)

        finance_payload = finance_by_order_id.get(order_id, {"transactions": []})
        finance_values = _finance_values_for_order_line(order_row, finance_payload)

        base_row = {
            **order_row.to_dict(),
            **finance_values,
            "sync_sold_price": finance_values["finance_gross"],
            "sync_fees": finance_values["finance_fees"],
            "sync_net_proceeds": finance_values["finance_net"],
        }

        if match_df.empty:
            matched_rows.append(
                {
                    **base_row,
                    "matched": False,
                    "already_sold": False,
                    "inventory_id": "",
                    "inventory_status": "",
                    "total_cost": 0.0,
                    "sync_profit": 0.0,
                }
            )
        else:
            inv_match = match_df.iloc[0]
            status = clean_text(inv_match.get("inventory_status")).upper()
            existing_order_id = clean_text(inv_match.get("ebay_order_id"))
            already_sold = bool(status == STATUS_SOLD or (bool(existing_order_id) and existing_order_id == order_id))
            total_cost = to_money(inv_match.get("total_cost"))
            sync_net = to_money(finance_values["finance_net"])
            sync_profit = round(sync_net - total_cost, 2)

            matched_rows.append(
                {
                    **base_row,
                    "matched": True,
                    "already_sold": already_sold,
                    "inventory_id": clean_text(inv_match.get("inventory_id")),
                    "inventory_status": status,
                    "total_cost": total_cost,
                    "sync_profit": sync_profit,
                }
            )

    out = pd.DataFrame(matched_rows)

    if not out.empty:
        out["matched"] = out["matched"].apply(_as_bool)
        out["already_sold"] = out["already_sold"].apply(_as_bool)

    return out


def _sync_ebay_sales_to_inventory(sync_df: pd.DataFrame) -> tuple[int, pd.DataFrame]:
    if sync_df.empty:
        return 0, pd.DataFrame()

    working = sync_df.copy()
    working["matched"] = working["matched"].apply(_as_bool)

    # Intentionally update every matched eBay order line, even if already SOLD.
    # This lets fee/net/profit corrections overwrite earlier test sync values.
    ready_to_update = working[working["matched"].eq(True)].copy()

    if ready_to_update.empty:
        return 0, ready_to_update

    updates_by_inventory_id = {}

    for _, row in ready_to_update.iterrows():
        inv_id = clean_text(row.get("inventory_id"))

        sold_price = _safe_money_round(row.get("sync_sold_price"))
        fees = _safe_money_round(row.get("sync_fees"))
        net = _safe_money_round(row.get("sync_net_proceeds"))
        total_cost = to_money(row.get("total_cost"))
        profit = round(net - total_cost, 2)

        if not inv_id:
            continue

        updates_by_inventory_id[inv_id] = {
            "inventory_status": STATUS_SOLD,
            "transaction_type": "eBay Order",
            "platform": "eBay",
            "sold_date": clean_text(row.get("sold_date")) or str(date.today()),
            "sold_price": sold_price,
            "fees": fees,
            "shipping_charged": _safe_money_round(row.get("shipping_charged")),
            "fees_total": fees,
            "net_proceeds": net,
            "profit": profit,
            "sale_channel": "eBay",
            "sale_notes": f"Synced from eBay. Fee source: {clean_text(row.get('finance_fee_source')) or 'unknown'}",
            "ebay_order_id": clean_text(row.get("ebay_order_id")),
            "ebay_line_item_id": clean_text(row.get("ebay_line_item_id")),
            "ebay_item_id": clean_text(row.get("ebay_item_id")),
            "ebay_listing_id": clean_text(row.get("ebay_item_id")),
            "ebay_listing_status": "Sold",
            "ebay_transaction_id": clean_text(row.get("finance_transaction_id")),
            "ebay_payout_id": clean_text(row.get("finance_payout_id")),
            "ebay_last_sync_at": now_iso(),
            "sold_transaction_id": clean_text(row.get("ebay_line_item_id"))
            or clean_text(row.get("ebay_order_id"))
            or clean_text(row.get("ebay_item_id")),
            "sold_created_at": now_iso(),
            "sold_updated_at": now_iso(),
        }

    if not updates_by_inventory_id:
        return 0, ready_to_update

    update_rows_by_key(
        get_ws_name("inventory_worksheet", "inventory"),
        INVENTORY_COLUMNS,
        "inventory_id",
        updates_by_inventory_id,
    )

    return len(updates_by_inventory_id), ready_to_update


def _pull_orders_and_build_sync_df(access_token: str, marketplace_id: str, inv: pd.DataFrame, days_back: int, limit: int):
    order_status, order_payload, used_params = get_recent_orders(
        access_token=access_token,
        days_back=int(days_back),
        limit=int(limit),
    )

    if order_status != 200:
        return order_status, order_payload, used_params, pd.DataFrame(), pd.DataFrame(), {}, {}

    orders_df = flatten_orders(order_payload)

    finance_by_order_id, finance_audit = build_finance_map_for_orders(
        access_token=access_token,
        marketplace_id=marketplace_id,
        orders_df=orders_df,
    )

    sync_df = _build_order_sync_df(inv, orders_df, finance_by_order_id)

    return order_status, order_payload, used_params, orders_df, sync_df, finance_by_order_id, finance_audit


def _display_listing_cols() -> list[str]:
    return [
        "assigned",
        "ebay_item_id",
        "title",
        "listing_status",
        "current_price",
        "shipping_service",
        "buyer_shipping_charged",
        "free_shipping",
        "quantity_available",
        "quantity_sold",
        "listing_start_date",
        "listing_end_date",
        "listing_url",
    ]


def _display_order_cols() -> list[str]:
    return [
        "matched",
        "already_sold",
        "finance_found",
        "finance_status",
        "inventory_id",
        "inventory_status",
        "ebay_order_id",
        "ebay_line_item_id",
        "ebay_item_id",
        "sold_date",
        "title",
        "quantity",
        "item_price",
        "shipping_charged",
        "tax_collected",
        "finance_tax_or_basis_extra",
        "finance_label_cost",
        "sync_sold_price",
        "sync_fees",
        "sync_net_proceeds",
        "sync_profit",
        "order_status",
        "payment_status",
    ]


# =========================================================
# Load app data
# =========================================================

ebay_config = get_ebay_secrets()

if not ebay_config:
    st.stop()

data = load_data()
inv = _safe_df(data.inventory)

needed_inv_cols = [
    "inventory_id",
    "inventory_status",
    "product_type",
    "inventory_type",
    "set_name",
    "card_name",
    "card_number",
    "variant",
    "card_subtype",
    "grading_company",
    "grade",
    "total_cost",
    "market_value",
    "sticker_price",
    "reference_link",
    "list_price",
    "ebay_item_id",
    "ebay_listing_id",
    "ebay_listing_url",
    "ebay_listing_status",
    "ebay_order_id",
    "ebay_line_item_id",
    "ebay_transaction_id",
    "ebay_payout_id",
    "ebay_last_sync_at",
]

inv = _ensure_cols(inv, needed_inv_cols)

if not inv.empty:
    inv["inventory_id"] = inv["inventory_id"].astype(str).str.strip()
    inv["inventory_status"] = inv["inventory_status"].astype(str).str.upper().str.strip()


# =========================================================
# Top controls
# =========================================================

top1, top2, top3, top4 = st.columns([1, 1, 1.25, 2.75])

with top1:
    if st.button("Refresh database", use_container_width=True):
        refresh_database_cache()
        st.rerun()

with top2:
    if st.button("Clear eBay page cache", use_container_width=True):
        for key in [
            "ebay_active_listings_df",
            "ebay_active_listings_audit",
            "ebay_orders_df",
            "ebay_orders_payload",
            "ebay_orders_filter",
            "ebay_order_sync_df",
            "ebay_finance_audit",
        ]:
            st.session_state.pop(key, None)
        st.success("Cleared eBay page cache.")

with top3:
    sync_now = st.button("Sync eBay sales now", type="primary", use_container_width=True)

with top4:
    st.info("Workflow: pull active listings → review assignments → sync sold eBay orders with fees.", icon="ℹ️")

if sync_now:
    access_token = get_access_token_or_stop(ebay_config)

    with st.spinner("Pulling recent eBay orders, finance transactions, and syncing matched sales..."):
        order_status, order_payload, used_params, orders_df, sync_df, finance_by_order_id, finance_audit = _pull_orders_and_build_sync_df(
            access_token=access_token,
            marketplace_id=ebay_config.get("marketplace_id", "EBAY_US"),
            inv=inv,
            days_back=30,
            limit=100,
        )

    st.session_state["ebay_orders_payload"] = order_payload
    st.session_state["ebay_orders_filter"] = used_params
    st.session_state["ebay_orders_df"] = orders_df
    st.session_state["ebay_order_sync_df"] = sync_df
    st.session_state["ebay_finance_audit"] = finance_audit

    if order_status != 200:
        st.error(f"Order pull failed. Status code: {order_status}")
        st.write(order_payload)
        st.stop()

    changed, ready = _sync_ebay_sales_to_inventory(sync_df)
    refresh_database_cache()

    if changed:
        st.success(f"Synced {changed:,} matched eBay order line(s), including fees, net proceeds, and profit.")
    else:
        st.info("No matched eBay order lines were updated. Check Sold Order Sync for unmatched order lines.")

    st.rerun()

with st.expander("eBay config check", expanded=False):
    st.write(
        {
            "secrets_source": ebay_config.get("source_label", "unknown"),
            "environment": ebay_config["environment"],
            "marketplace_id": ebay_config["marketplace_id"],
            "client_id_prefix": ebay_config["client_id"][:12] + "...",
            "ru_name": ebay_config["ru_name"],
            "refresh_token_loaded": bool(ebay_config.get("refresh_token")),
            "scopes": ebay_config["scopes"],
        }
    )


tab_active, tab_assign, tab_orders, tab_audit = st.tabs(
    [
        "1. Pull Active Listings",
        "2. Assign Listings",
        "3. Sold Order Sync",
        "Audit / Raw Data",
    ]
)


# =========================================================
# Tab 1: Pull Active Listings
# =========================================================

with tab_active:
    st.subheader("Pull Active eBay Listings")
    st.caption("This pulls listings currently active in your eBay account. Sold listings usually disappear from this list, so use Sync eBay sales now after sales.")

    c1, c2, c3 = st.columns([1, 1, 2])

    with c1:
        entries_per_page = st.number_input("Listings per page", min_value=25, max_value=200, value=100, step=25)

    with c2:
        max_pages = st.number_input("Max pages", min_value=1, max_value=25, value=5, step=1)

    with c3:
        st.write("")
        st.write("")
        pull_active = st.button("Pull Active eBay Listings", type="primary", use_container_width=True)

    if pull_active:
        access_token = get_access_token_or_stop(ebay_config)

        with st.spinner("Pulling active eBay listings..."):
            listings_df, audit = get_active_listings(
                access_token=access_token,
                entries_per_page=int(entries_per_page),
                max_pages=int(max_pages),
            )

        st.session_state["ebay_active_listings_df"] = listings_df
        st.session_state["ebay_active_listings_audit"] = audit

        if audit.get("errors"):
            st.warning("eBay returned warnings/errors. Check Audit / Raw Data.")

        st.success(f"Pulled {len(listings_df):,} active eBay listing(s).")

    listings_df = st.session_state.get("ebay_active_listings_df", pd.DataFrame()).copy()

    if listings_df.empty:
        st.info("No active listings pulled yet. Click the button above.")
    else:
        assigned_ids = _assigned_ebay_ids(inv)
        listings_df["assigned"] = listings_df["ebay_item_id"].astype(str).str.strip().isin(assigned_ids)
        unassigned_count = int((~listings_df["assigned"]).sum())

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Active listings pulled", f"{len(listings_df):,}")
        m2.metric("Assigned", f"{_bool_count(listings_df['assigned']):,}")
        m3.metric("Needs assignment", f"{unassigned_count:,}")
        m4.metric("Active list value", money_fmt(listings_df["current_price"].apply(to_money).sum()))

        st.markdown("### Active listing preview")

        cols = [c for c in _display_listing_cols() if c in listings_df.columns]

        st.dataframe(
            listings_df[cols],
            use_container_width=True,
            hide_index=True,
            column_config={
                "listing_url": st.column_config.LinkColumn("Listing URL"),
                "current_price": st.column_config.NumberColumn("Current Price", format="$%.2f"),
                "assigned": st.column_config.CheckboxColumn("Assigned"),
            },
        )

        active_item_ids = set(listings_df["ebay_item_id"].astype(str).str.strip().tolist())
        listed_assigned = inv[
            inv["inventory_status"].astype(str).str.upper().eq(STATUS_LISTED)
            & inv["ebay_item_id"].astype(str).str.strip().ne("")
        ].copy()

        if not listed_assigned.empty:
            listed_assigned["still_active_on_ebay"] = listed_assigned["ebay_item_id"].astype(str).str.strip().isin(active_item_ids)
            missing_from_active = listed_assigned[~listed_assigned["still_active_on_ebay"]].copy()

            if not missing_from_active.empty:
                st.warning("Some LISTED inventory items are no longer in the active eBay listing pull. They may have sold or ended. Click Sync eBay sales now to check orders and mark sold.")

                show_cols = [
                    "inventory_id",
                    "inventory_status",
                    "card_name",
                    "card_number",
                    "set_name",
                    "list_price",
                    "ebay_item_id",
                    "ebay_listing_status",
                    "ebay_listing_url",
                ]

                st.dataframe(
                    missing_from_active[[c for c in show_cols if c in missing_from_active.columns]],
                    use_container_width=True,
                    hide_index=True,
                    column_config={
                        "ebay_listing_url": st.column_config.LinkColumn("Listing URL"),
                        "list_price": st.column_config.NumberColumn("List Price", format="$%.2f"),
                    },
                )


# =========================================================
# Tab 2: Assign Listings
# =========================================================

with tab_assign:
    st.subheader("Assign eBay Listings to Inventory")

    listings_df = st.session_state.get("ebay_active_listings_df", pd.DataFrame()).copy()

    if listings_df.empty:
        st.info("Pull active listings first on tab 1.")
    elif inv.empty:
        st.info("No inventory loaded.")
    else:
        assigned_ids = _assigned_ebay_ids(inv)
        listings_df["assigned"] = listings_df["ebay_item_id"].astype(str).str.strip().isin(assigned_ids)
        unassigned = listings_df[~listings_df["assigned"]].copy()

        st.caption(f"{len(unassigned):,} active eBay listing(s) need assignment.")

        if unassigned.empty:
            st.success("All pulled active listings are already assigned to inventory.")
        else:
            inventory_options, inventory_label_to_id = _inventory_option_map(inv)
            rows = []
            auto_reserved_inventory_ids = set()

            for _, listing in unassigned.iterrows():
                best_label, best_inv_id, score = _best_inventory_label_for_listing(
                    inv,
                    clean_text(listing.get("title")),
                    exclude_inventory_ids=auto_reserved_inventory_ids,
                )

                if best_inv_id:
                    auto_reserved_inventory_ids.add(best_inv_id)

                rows.append(
                    {
                        "assign": bool(best_inv_id),
                        "ebay_item_id": clean_text(listing.get("ebay_item_id")),
                        "title": clean_text(listing.get("title")),
                        "current_price": to_money(listing.get("current_price")),
                        "listing_status": clean_text(listing.get("listing_status")),
                        "listing_start_date": clean_text(listing.get("listing_start_date")),
                        "listing_url": clean_text(listing.get("listing_url")),
                        "match_score": score,
                        "selected_inventory": best_label,
                    }
                )

            assign_df = pd.DataFrame(rows)
            matched_count = int(assign_df["selected_inventory"].astype(str).str.strip().ne("").sum())

            m1, m2, m3 = st.columns(3)
            m1.metric("Unassigned listings", f"{len(assign_df):,}")
            m2.metric("Auto-matched", f"{matched_count:,}")
            m3.metric("Needs manual pick", f"{len(assign_df) - matched_count:,}")

            st.info("Review the table. The draft will not auto-suggest the same inventory item twice. If you manually choose the same inventory item twice, the app will show exactly which rows need fixing.", icon="ℹ️")

            edited_assignments = st.data_editor(
                assign_df,
                use_container_width=True,
                hide_index=True,
                height=650,
                column_config={
                    "assign": st.column_config.CheckboxColumn("Assign", help="Checked rows will be linked to inventory when you click Apply."),
                    "ebay_item_id": st.column_config.TextColumn("eBay Item ID", disabled=True),
                    "title": st.column_config.TextColumn("eBay Title", disabled=True, width="large"),
                    "current_price": st.column_config.NumberColumn("List Price", format="$%.2f", disabled=True),
                    "listing_status": st.column_config.TextColumn("Status", disabled=True),
                    "listing_start_date": st.column_config.TextColumn("List Date", disabled=True),
                    "listing_url": st.column_config.LinkColumn("Listing URL", disabled=True),
                    "match_score": st.column_config.NumberColumn("Match Score", disabled=True),
                    "selected_inventory": st.column_config.SelectboxColumn("Selected Inventory Item", options=inventory_options, required=False, width="large"),
                },
                disabled=["ebay_item_id", "title", "current_price", "listing_status", "listing_start_date", "listing_url", "match_score"],
            )

            st.markdown("---")
            apply_col, _ = st.columns([1, 3])

            with apply_col:
                apply_assignments = st.button("Apply checked assignments", type="primary", use_container_width=True)

            if apply_assignments:
                to_apply = edited_assignments[
                    edited_assignments["assign"].eq(True)
                    & edited_assignments["selected_inventory"].astype(str).str.strip().ne("")
                ].copy()

                if to_apply.empty:
                    st.warning("No checked rows with selected inventory items to assign.")
                    st.stop()

                to_apply["inventory_id"] = to_apply["selected_inventory"].map(inventory_label_to_id)
                to_apply["inventory_id"] = to_apply["inventory_id"].fillna("").astype(str).str.strip()

                missing_inventory = to_apply[to_apply["inventory_id"].eq("")]
                if not missing_inventory.empty:
                    st.error("One or more selected inventory values could not be mapped to an inventory_id.")
                    st.dataframe(missing_inventory, use_container_width=True, hide_index=True)
                    st.stop()

                duplicate_inventory = to_apply["inventory_id"].value_counts().reset_index()
                duplicate_inventory.columns = ["inventory_id", "count"]
                duplicate_inventory = duplicate_inventory[duplicate_inventory["count"] > 1]

                if not duplicate_inventory.empty:
                    duplicate_ids = duplicate_inventory["inventory_id"].astype(str).tolist()
                    duplicate_detail = to_apply[to_apply["inventory_id"].astype(str).isin(duplicate_ids)].copy()
                    duplicate_detail = duplicate_detail[["inventory_id", "ebay_item_id", "title", "current_price", "selected_inventory"]].sort_values(["inventory_id", "title"])

                    st.error("The same inventory item is selected for more than one eBay listing. The rows below are the duplicates.")
                    st.dataframe(
                        duplicate_detail,
                        use_container_width=True,
                        hide_index=True,
                        column_config={"current_price": st.column_config.NumberColumn("List Price", format="$%.2f")},
                    )
                    st.stop()

                updates_by_inventory_id = {}
                listings_lookup = unassigned.copy()
                listings_lookup["ebay_item_id"] = listings_lookup["ebay_item_id"].astype(str).str.strip()
                listings_lookup = listings_lookup.drop_duplicates(subset=["ebay_item_id"], keep="last")
                listings_lookup = listings_lookup.set_index("ebay_item_id", drop=False)

                for _, row in to_apply.iterrows():
                    inv_id = clean_text(row.get("inventory_id"))
                    item_id = clean_text(row.get("ebay_item_id"))

                    if not inv_id or not item_id:
                        continue

                    if item_id in listings_lookup.index:
                        listing = listings_lookup.loc[item_id]
                    else:
                        listing = pd.Series(dtype=object)

                    listing_price = to_money(row.get("current_price"))

                    updates_by_inventory_id[inv_id] = {
                        "inventory_status": STATUS_LISTED,
                        "transaction_type": clean_text(listing.get("listing_type")) or "eBay Listing",
                        "platform": "eBay",
                        "sale_channel": "Online",
                        "list_date": clean_text(row.get("listing_start_date")) or str(date.today()),
                        "list_price": round(listing_price, 2),
                        "ebay_item_id": item_id,
                        "ebay_listing_id": clean_text(listing.get("ebay_listing_id")) or item_id,
                        "ebay_listing_url": clean_text(row.get("listing_url")),
                        "ebay_listing_status": clean_text(row.get("listing_status")) or "Active",
                        "ebay_last_sync_at": now_iso(),
                    }

                if not updates_by_inventory_id:
                    st.warning("No valid assignments found.")
                    st.stop()

                update_rows_by_key(
                    get_ws_name("inventory_worksheet", "inventory"),
                    INVENTORY_COLUMNS,
                    "inventory_id",
                    updates_by_inventory_id,
                )

                refresh_database_cache()
                st.session_state.pop("ebay_active_listings_df", None)
                st.success(f"Assigned {len(updates_by_inventory_id):,} eBay listing(s) to inventory.")
                st.rerun()

        st.markdown("---")
        st.markdown("### Already assigned listings")
        assigned = listings_df[listings_df["assigned"]].copy()

        if assigned.empty:
            st.info("No assigned listings from the pulled active list.")
        else:
            with st.expander("Listing profit estimator assumptions", expanded=False):
                st.caption("These are estimates for active listings. eBay's actual order sync still uses the Finances API after a sale.")

                a1, a2, a3, a4 = st.columns(4)

                with a1:
                    fee_rate_pct = st.number_input(
                        "eBay variable fee %",
                        min_value=0.0,
                        max_value=30.0,
                        value=float(DEFAULT_LISTING_ESTIMATOR_ASSUMPTIONS["fee_rate_pct"]),
                        step=0.05,
                        format="%.2f",
                    )

                with a2:
                    fixed_order_fee = st.number_input(
                        "Fixed order fee",
                        min_value=0.0,
                        max_value=5.0,
                        value=float(DEFAULT_LISTING_ESTIMATOR_ASSUMPTIONS["fixed_order_fee"]),
                        step=0.01,
                        format="%.2f",
                    )

                with a3:
                    promoted_listing_pct = st.number_input(
                        "Promoted/ad fee %",
                        min_value=0.0,
                        max_value=30.0,
                        value=float(DEFAULT_LISTING_ESTIMATOR_ASSUMPTIONS["promoted_listing_pct"]),
                        step=0.25,
                        format="%.2f",
                    )

                with a4:
                    estimated_buyer_tax_pct = st.number_input(
                        "Buyer tax % for fee basis",
                        min_value=0.0,
                        max_value=15.0,
                        value=float(DEFAULT_LISTING_ESTIMATOR_ASSUMPTIONS["estimated_buyer_tax_pct"]),
                        step=0.25,
                        format="%.2f",
                    )

                s1, s2, s3, s4 = st.columns(4)

                with s1:
                    standard_envelope_label_cost = st.number_input(
                        "Standard Envelope label",
                        min_value=0.0,
                        max_value=10.0,
                        value=float(DEFAULT_LISTING_ESTIMATOR_ASSUMPTIONS["standard_envelope_label_cost"]),
                        step=0.01,
                        format="%.2f",
                    )

                with s2:
                    ground_advantage_label_cost = st.number_input(
                        "Ground Advantage label",
                        min_value=0.0,
                        max_value=20.0,
                        value=float(DEFAULT_LISTING_ESTIMATOR_ASSUMPTIONS["ground_advantage_label_cost"]),
                        step=0.01,
                        format="%.2f",
                    )

                with s3:
                    other_label_cost = st.number_input(
                        "Other/unknown label",
                        min_value=0.0,
                        max_value=20.0,
                        value=float(DEFAULT_LISTING_ESTIMATOR_ASSUMPTIONS["other_label_cost"]),
                        step=0.01,
                        format="%.2f",
                    )

                with s4:
                    extra_per_order_cost = st.number_input(
                        "Extra packaging/order cost",
                        min_value=0.0,
                        max_value=10.0,
                        value=float(DEFAULT_LISTING_ESTIMATOR_ASSUMPTIONS["extra_per_order_cost"]),
                        step=0.01,
                        format="%.2f",
                    )

            estimator_assumptions = {
                "fee_rate_pct": fee_rate_pct,
                "fixed_order_fee": fixed_order_fee,
                "promoted_listing_pct": promoted_listing_pct,
                "estimated_buyer_tax_pct": estimated_buyer_tax_pct,
                "standard_envelope_label_cost": standard_envelope_label_cost,
                "ground_advantage_label_cost": ground_advantage_label_cost,
                "other_label_cost": other_label_cost,
                "extra_per_order_cost": extra_per_order_cost,
            }

            assigned_estimates = _build_assigned_listing_estimate_df(
                assigned=assigned,
                inv=inv,
                assumptions=estimator_assumptions,
            )

            if assigned_estimates.empty:
                st.info("No assigned listing estimates could be built.")
            else:
                loss_rows = assigned_estimates[assigned_estimates["estimated_profit_loss"].apply(to_money) < 0].copy()
                total_estimated_loss = abs(loss_rows["estimated_profit_loss"].apply(to_money).sum()) if not loss_rows.empty else 0.0
                total_estimated_profit = assigned_estimates["estimated_profit_loss"].apply(to_money).sum()

                p1, p2, p3, p4 = st.columns(4)
                p1.metric("Assigned active listings", f"{len(assigned_estimates):,}")
                p2.metric("Listings estimated at loss", f"{len(loss_rows):,}")
                p3.metric("Total estimated P/L", money_fmt(total_estimated_profit))
                p4.metric("Estimated loss exposure", money_fmt(total_estimated_loss))

                st.caption("Rows shaded red are estimated to lose money at the current list price. Darker red means the loss is larger relative to your total cost.")

                cols = [c for c in _assigned_listing_estimate_cols() if c in assigned_estimates.columns]
                styled_assigned_estimates = assigned_estimates[cols].style.apply(_style_loss_rows, axis=1)

                st.dataframe(
                    styled_assigned_estimates,
                    use_container_width=True,
                    hide_index=True,
                    height=650,
                    column_config={
                        "assigned": st.column_config.CheckboxColumn("Assigned"),
                        "listing_url": st.column_config.LinkColumn("Listing URL"),
                        "total_cost": st.column_config.NumberColumn("Total Cost", format="$%.2f"),
                        "current_price": st.column_config.NumberColumn("List Price", format="$%.2f"),
                        "buyer_shipping_charged": st.column_config.NumberColumn("Buyer Shipping", format="$%.2f"),
                        "estimated_label_cost": st.column_config.NumberColumn("Est. Label", format="$%.2f"),
                        "estimated_ebay_fees": st.column_config.NumberColumn("Est. eBay Fees", format="$%.2f"),
                        "estimated_net_proceeds": st.column_config.NumberColumn("Est. Net", format="$%.2f"),
                        "estimated_profit_loss": st.column_config.NumberColumn("Est. Profit/Loss", format="$%.2f"),
                        "break_even_price": st.column_config.NumberColumn("Break Even Price", format="$%.2f"),
                        "price_for_5pct_profit": st.column_config.NumberColumn("Price for 5% Profit", format="$%.2f"),
                        "price_for_10pct_profit": st.column_config.NumberColumn("Price for 10% Profit", format="$%.2f"),
                    },
                )

                with st.expander("Download assigned listing estimate CSV", expanded=False):
                    st.download_button(
                        "Download assigned listing estimates",
                        data=assigned_estimates.to_csv(index=False),
                        file_name="ebay_assigned_listing_profit_estimates.csv",
                        mime="text/csv",
                    )


# =========================================================
# Tab 3: Sold Order Sync
# =========================================================

with tab_orders:
    st.subheader("Sold eBay Order Sync")
    st.caption("This pulls recent sold orders, pulls eBay Finances transactions, and writes sold price / fees / net / profit to inventory.")

    c1, c2, c3, c4 = st.columns([1, 1, 1.5, 1.5])

    with c1:
        days_back = st.number_input("Days back to pull", min_value=1, max_value=90, value=30, step=1)

    with c2:
        order_limit = st.number_input("Order limit", min_value=10, max_value=200, value=100, step=10)

    with c3:
        st.write("")
        st.write("")
        pull_orders = st.button("Pull Recent eBay Orders + Fees", use_container_width=True)

    with c4:
        st.write("")
        st.write("")
        pull_and_sync = st.button("Pull + Sync Sold Orders + Fees", type="primary", use_container_width=True)

    if pull_orders or pull_and_sync:
        access_token = get_access_token_or_stop(ebay_config)

        with st.spinner("Pulling recent eBay orders and finance transactions..."):
            order_status, order_payload, used_params, df_orders, sync_df, finance_by_order_id, finance_audit = _pull_orders_and_build_sync_df(
                access_token=access_token,
                marketplace_id=ebay_config.get("marketplace_id", "EBAY_US"),
                inv=inv,
                days_back=int(days_back),
                limit=int(order_limit),
            )

        st.session_state["ebay_orders_payload"] = order_payload
        st.session_state["ebay_orders_filter"] = used_params
        st.session_state["ebay_finance_audit"] = finance_audit

        if order_status != 200:
            st.error(f"Order pull failed. Status code: {order_status}")
            st.write(order_payload)
            st.stop()

        st.session_state["ebay_orders_df"] = df_orders
        st.session_state["ebay_order_sync_df"] = sync_df

        total_orders = order_payload.get("total", 0)
        st.success(f"Pulled {len(df_orders):,} order line item(s). Total matching orders reported by eBay: {total_orders}.")

        if pull_and_sync:
            changed, ready = _sync_ebay_sales_to_inventory(sync_df)
            refresh_database_cache()

            if changed:
                st.success(f"Synced {changed:,} matched eBay order line(s), including fees, net proceeds, and profit.")
            else:
                st.info("No matched eBay order lines were updated.")

            st.rerun()

    orders_df = st.session_state.get("ebay_orders_df", pd.DataFrame()).copy()
    sync_df = st.session_state.get("ebay_order_sync_df", pd.DataFrame()).copy()

    if orders_df.empty:
        st.info("No eBay orders pulled yet. Click Pull Recent eBay Orders + Fees or Pull + Sync Sold Orders + Fees.")
    else:
        if sync_df.empty:
            sync_df = _build_order_sync_df(inv, orders_df, {})

        if not sync_df.empty:
            sync_df["matched"] = sync_df["matched"].apply(_as_bool)
            sync_df["already_sold"] = sync_df["already_sold"].apply(_as_bool)

        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric("Order line items", f"{len(sync_df):,}")
        m2.metric("Matched", f"{_bool_count(sync_df['matched']):,}")
        m3.metric("Unmatched", f"{int((~sync_df['matched']).sum()):,}")
        m4.metric("Finance found", f"{_bool_count(sync_df['finance_found']):,}" if "finance_found" in sync_df.columns else "0")
        m5.metric("Already sold", f"{_bool_count(sync_df['already_sold']):,}")

        cols = [c for c in _display_order_cols() if c in sync_df.columns]

        st.markdown("### Order match preview")

        st.dataframe(
            sync_df[cols],
            use_container_width=True,
            hide_index=True,
            column_config={
                "matched": st.column_config.CheckboxColumn("Matched"),
                "already_sold": st.column_config.CheckboxColumn("Already Sold"),
                "finance_found": st.column_config.CheckboxColumn("Finance Found"),
                "item_price": st.column_config.NumberColumn("Item Price", format="$%.2f"),
                "shipping_charged": st.column_config.NumberColumn("Shipping Charged", format="$%.2f"),
                "tax_collected": st.column_config.NumberColumn("Tax Collected", format="$%.2f"),
                "finance_tax_or_basis_extra": st.column_config.NumberColumn("Tax/Basis Extra", format="$%.2f"),
                "finance_label_cost": st.column_config.NumberColumn("Label Cost/Adjustment", format="$%.2f"),
                "sync_sold_price": st.column_config.NumberColumn("Sold Price to Write", format="$%.2f"),
                "sync_fees": st.column_config.NumberColumn("Fees to Write", format="$%.2f"),
                "sync_net_proceeds": st.column_config.NumberColumn("Net Proceeds to Write", format="$%.2f"),
                "sync_profit": st.column_config.NumberColumn("Profit to Write", format="$%.2f"),
            },
        )

        unmatched = sync_df[~sync_df["matched"]].copy()

        if not unmatched.empty:
            with st.expander("Unmatched order lines", expanded=True):
                st.warning("These sold items could not be matched to inventory. Check that the eBay listing was assigned to inventory.")

                show_cols = [
                    "ebay_order_id",
                    "ebay_line_item_id",
                    "ebay_item_id",
                    "sold_date",
                    "title",
                    "sync_sold_price",
                    "sync_fees",
                    "sync_net_proceeds",
                    "finance_status",
                ]

                st.dataframe(
                    unmatched[[c for c in show_cols if c in unmatched.columns]],
                    use_container_width=True,
                    hide_index=True,
                    column_config={
                        "sync_sold_price": st.column_config.NumberColumn("Sold Price", format="$%.2f"),
                        "sync_fees": st.column_config.NumberColumn("Fees", format="$%.2f"),
                        "sync_net_proceeds": st.column_config.NumberColumn("Net", format="$%.2f"),
                    },
                )

        ready_to_sync = sync_df[sync_df["matched"].eq(True)].copy()

        st.markdown("### Sync matched eBay orders")

        if ready_to_sync.empty:
            st.info("No matched order lines are ready to sync.")
        else:
            st.caption("This will update every matched eBay order line, including rows already marked SOLD, so corrected fee/net/profit values can overwrite older test sync values.")

            preview_cols = [
                "inventory_id",
                "ebay_order_id",
                "ebay_line_item_id",
                "ebay_item_id",
                "sold_date",
                "title",
                "sync_sold_price",
                "sync_fees",
                "sync_net_proceeds",
                "total_cost",
                "sync_profit",
                "finance_status",
            ]

            st.dataframe(
                ready_to_sync[[c for c in preview_cols if c in ready_to_sync.columns]],
                use_container_width=True,
                hide_index=True,
                column_config={
                    "sync_sold_price": st.column_config.NumberColumn("Sold Price", format="$%.2f"),
                    "sync_fees": st.column_config.NumberColumn("Fees", format="$%.2f"),
                    "sync_net_proceeds": st.column_config.NumberColumn("Net Proceeds", format="$%.2f"),
                    "total_cost": st.column_config.NumberColumn("Total Cost", format="$%.2f"),
                    "sync_profit": st.column_config.NumberColumn("Profit", format="$%.2f"),
                },
            )

            confirm = st.checkbox("I reviewed the rows above. Sync matched eBay order lines and write fees/net/profit.", value=False)

            if st.button("Sync selected matched eBay sales + fees", type="primary", disabled=not confirm):
                changed, ready = _sync_ebay_sales_to_inventory(sync_df)
                refresh_database_cache()

                if changed:
                    st.success(f"Synced {changed:,} matched eBay order line(s), including fees, net proceeds, and profit.")
                else:
                    st.info("No matched eBay order lines were updated.")

                st.rerun()


# =========================================================
# Tab 4: Audit / Raw Data
# =========================================================

with tab_audit:
    st.subheader("Audit / Raw Data")

    st.markdown("### Active listings raw table")
    listings_df = st.session_state.get("ebay_active_listings_df", pd.DataFrame()).copy()

    if listings_df.empty:
        st.info("No active listing data cached yet.")
    else:
        st.dataframe(listings_df, use_container_width=True, hide_index=True)
        st.download_button(
            "Download active listings CSV",
            data=listings_df.to_csv(index=False),
            file_name="ebay_active_listings.csv",
            mime="text/csv",
        )

    st.markdown("### Active listings audit")
    audit = st.session_state.get("ebay_active_listings_audit", {})

    if not audit:
        st.info("No active listing audit data cached yet.")
    else:
        st.write(
            {
                "pages_requested": audit.get("pages_requested"),
                "acks": audit.get("acks"),
                "errors": audit.get("errors"),
            }
        )

        with st.expander("Last raw Trading API response", expanded=False):
            st.code(audit.get("raw_last_response", ""), language="xml")

    st.markdown("### Recent orders raw table")
    orders_df = st.session_state.get("ebay_orders_df", pd.DataFrame()).copy()

    if orders_df.empty:
        st.info("No order data cached yet.")
    else:
        st.dataframe(orders_df, use_container_width=True, hide_index=True)
        st.download_button(
            "Download eBay orders CSV",
            data=orders_df.to_csv(index=False),
            file_name="ebay_orders.csv",
            mime="text/csv",
        )

    st.markdown("### Recent order sync table")
    sync_df = st.session_state.get("ebay_order_sync_df", pd.DataFrame()).copy()

    if sync_df.empty:
        st.info("No order sync data cached yet.")
    else:
        st.dataframe(sync_df, use_container_width=True, hide_index=True)
        st.download_button(
            "Download eBay order sync CSV",
            data=sync_df.to_csv(index=False),
            file_name="ebay_order_sync.csv",
            mime="text/csv",
        )

    st.markdown("### eBay Finances audit")
    finance_audit = st.session_state.get("ebay_finance_audit", {})

    if not finance_audit:
        st.info("No finance audit data cached yet.")
    else:
        summary_rows = []

        for order_id, details in finance_audit.items():
            summary_rows.append(
                {
                    "ebay_order_id": order_id,
                    "status_code": details.get("status_code"),
                    "transaction_count": details.get("transaction_count"),
                    "filter": (details.get("params") or {}).get("filter", ""),
                }
            )

        st.dataframe(pd.DataFrame(summary_rows), use_container_width=True, hide_index=True)

        with st.expander("Raw Finances API responses", expanded=False):
            st.json(finance_audit)

    st.markdown("### Recent orders raw JSON")
    order_payload = st.session_state.get("ebay_orders_payload", {})
    order_filter = st.session_state.get("ebay_orders_filter", {})

    if order_filter:
        st.write("Order request filter used:")
        st.code(str(order_filter))

    if order_payload:
        with st.expander("Raw Fulfillment API order response", expanded=False):
            st.json(order_payload)
