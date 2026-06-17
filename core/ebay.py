from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from urllib.parse import quote

import pandas as pd
import requests
import streamlit as st

from .cleaning import clean_text, now_iso, to_money
from .config import EBAY_ORDER_COLUMNS, EBAY_LISTING_COLUMNS, INVENTORY_COLUMNS, STATUS_SOLD
from .sheets import get_ws_name, append_rows, read_sheet, update_rows_by_key, overwrite_sheet


def ebay_config() -> dict:
    env = str(st.secrets.get("ebay_environment", "production") or "production").lower()
    sandbox = env == "sandbox"
    return {
        "environment": env,
        "api_root": "https://api.sandbox.ebay.com" if sandbox else "https://api.ebay.com",
        "finance_root": "https://apiz.sandbox.ebay.com" if sandbox else "https://apiz.ebay.com",
        "client_id": st.secrets.get("ebay_client_id", ""),
        "client_secret": st.secrets.get("ebay_client_secret", ""),
        "refresh_token": st.secrets.get("ebay_refresh_token", ""),
        "marketplace_id": st.secrets.get("ebay_marketplace_id", "EBAY_US"),
        "scopes": st.secrets.get("ebay_scopes", ""),
    }


def ebay_is_configured() -> bool:
    cfg = ebay_config()
    return all(clean_text(cfg.get(k)) for k in ["client_id", "client_secret", "refresh_token"])


@st.cache_data(ttl=6900, show_spinner=False)
def get_access_token() -> str:
    cfg = ebay_config()
    if not ebay_is_configured():
        raise RuntimeError("Missing eBay client_id, client_secret, or refresh_token in Streamlit secrets.")
    raw = f"{cfg['client_id']}:{cfg['client_secret']}".encode("utf-8")
    basic = base64.b64encode(raw).decode("ascii")
    data = {"grant_type": "refresh_token", "refresh_token": cfg["refresh_token"]}
    if clean_text(cfg.get("scopes")):
        data["scope"] = cfg["scopes"]
    resp = requests.post(
        f"{cfg['api_root']}/identity/v1/oauth2/token",
        headers={"Content-Type": "application/x-www-form-urlencoded", "Authorization": f"Basic {basic}"},
        data=data,
        timeout=30,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"eBay token error {resp.status_code}: {resp.text[:1000]}")
    return resp.json()["access_token"]


def ebay_get(path: str, params: dict | None = None, finance: bool = False) -> dict:
    cfg = ebay_config()
    root = cfg["finance_root"] if finance else cfg["api_root"]
    resp = requests.get(
        root + path,
        params=params,
        headers={
            "Authorization": f"Bearer {get_access_token()}",
            "X-EBAY-C-MARKETPLACE-ID": cfg["marketplace_id"],
            "Accept": "application/json",
        },
        timeout=45,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"eBay API error {resp.status_code}: {resp.text[:1500]}")
    return resp.json() if resp.text else {}


def _amount(obj) -> float:
    if not isinstance(obj, dict):
        return 0.0
    return to_money(obj.get("value"))


def fetch_orders(start_utc: datetime, end_utc: datetime, limit: int = 100) -> list[dict]:
    start = start_utc.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    end = end_utc.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.999Z")
    filter_value = f"creationdate:[{start}..{end}]"
    offset = 0
    orders = []
    while True:
        data = ebay_get("/sell/fulfillment/v1/order", params={"filter": filter_value, "limit": str(limit), "offset": str(offset)})
        batch = data.get("orders", []) or []
        orders.extend(batch)
        total = int(data.get("total", len(orders)) or len(orders))
        if len(batch) < limit or len(orders) >= total:
            break
        offset += limit
    return orders


def fetch_order_earnings_summary(start_utc: datetime, end_utc: datetime) -> dict:
    start = start_utc.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    end = end_utc.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.999Z")
    return ebay_get("/sell/finances/v1/order_earnings_summary", params={"filter": f"orderCreationDate:[{start}..{end}]"}, finance=True)


def normalize_orders_to_rows(orders: list[dict], inventory_ids: set[str] | None = None) -> list[dict]:
    inventory_ids = inventory_ids or set()
    rows = []
    for order in orders:
        order_id = clean_text(order.get("orderId"))
        created = clean_text(order.get("creationDate")) or clean_text(order.get("lastModifiedDate"))
        status = clean_text(order.get("orderPaymentStatus"))
        fulfillment = clean_text(order.get("orderFulfillmentStatus"))
        pricing = order.get("pricingSummary", {}) or {}
        order_total = _amount(pricing.get("total"))
        currency = (pricing.get("total") or {}).get("currency", "USD")
        line_items = order.get("lineItems", []) or []
        if not line_items:
            rows.append({
                "ebay_order_id": order_id,
                "ebay_line_item_id": "",
                "order_created_at": created,
                "sold_date": created[:10],
                "gross_paid": order_total,
                "currency": currency,
                "order_status": status,
                "fulfillment_status": fulfillment,
                "raw_order_json": json.dumps(order)[:45000],
                "created_at": now_iso(),
                "updated_at": now_iso(),
            })
            continue
        for li in line_items:
            line_id = clean_text(li.get("lineItemId"))
            sku = clean_text(li.get("sku"))
            inv_id = sku if sku in inventory_ids else ""
            item_price = _amount(li.get("lineItemCost"))
            qty = int(to_money(li.get("quantity", 1)) or 1)
            title = clean_text(li.get("title"))
            legacy_item_id = clean_text(li.get("legacyItemId"))
            delivery = _amount((li.get("deliveryCost") or {}).get("shippingCost"))
            taxes = 0.0
            for tax in li.get("taxes", []) or []:
                taxes += _amount(tax.get("amount"))
            gross = item_price + delivery + taxes
            rows.append({
                "ebay_order_id": order_id,
                "ebay_line_item_id": line_id,
                "legacy_item_id": legacy_item_id,
                "sku": sku,
                "inventory_id": inv_id,
                "title": title,
                "order_created_at": created,
                "sold_date": created[:10],
                "quantity": qty,
                "sold_price": item_price,
                "shipping_charged": delivery,
                "tax": taxes,
                "gross_paid": gross,
                "fees_total": 0.0,
                "net_proceeds": item_price + delivery,
                "currency": currency,
                "order_status": status,
                "fulfillment_status": fulfillment,
                "matched_to_inventory": "YES" if inv_id else "NO",
                "sync_status": "matched_by_sku" if inv_id else "unmatched",
                "raw_order_json": json.dumps(order)[:45000],
                "created_at": now_iso(),
                "updated_at": now_iso(),
            })
    return rows


def upsert_ebay_order_rows(rows: list[dict]) -> int:
    if not rows:
        return 0
    ws_name = get_ws_name("ebay_orders_worksheet", "ebay_orders")
    existing = read_sheet(ws_name, tuple(EBAY_ORDER_COLUMNS))
    existing_keys = set()
    if not existing.empty:
        for _, r in existing.iterrows():
            existing_keys.add(f"{clean_text(r.get('ebay_order_id'))}|{clean_text(r.get('ebay_line_item_id'))}")
    new_rows = []
    for r in rows:
        key = f"{clean_text(r.get('ebay_order_id'))}|{clean_text(r.get('ebay_line_item_id'))}"
        if key not in existing_keys:
            new_rows.append(r)
    append_rows(ws_name, EBAY_ORDER_COLUMNS, new_rows)
    return len(new_rows)


def apply_matched_orders_to_inventory(order_rows: list[dict], inventory: pd.DataFrame) -> int:
    if not order_rows or inventory.empty:
        return 0
    inv_lookup = inventory.set_index("inventory_id", drop=False).to_dict("index") if "inventory_id" in inventory.columns else {}
    updates = {}
    for r in order_rows:
        inv_id = clean_text(r.get("inventory_id"))
        if not inv_id or inv_id not in inv_lookup:
            continue
        rec = inv_lookup[inv_id]
        if clean_text(rec.get("inventory_status")).upper() == STATUS_SOLD:
            continue
        sold_price = to_money(r.get("sold_price"))
        fees_total = to_money(r.get("fees_total"))
        net = to_money(r.get("net_proceeds")) or sold_price - fees_total
        cost = to_money(rec.get("total_cost"))
        updates[inv_id] = {
            "inventory_status": STATUS_SOLD,
            "transaction_type": "eBay Order",
            "platform": "eBay",
            "sold_date": clean_text(r.get("sold_date")),
            "sold_price": round(sold_price, 2),
            "shipping_charged": round(to_money(r.get("shipping_charged")), 2),
            "fees_total": round(fees_total, 2),
            "net_proceeds": round(net, 2),
            "profit": round(net - cost, 2),
            "sale_channel": "Online",
            "sale_notes": f"Synced from eBay order {clean_text(r.get('ebay_order_id'))}",
            "ebay_order_id": clean_text(r.get("ebay_order_id")),
            "ebay_line_item_id": clean_text(r.get("ebay_line_item_id")),
            "ebay_item_id": clean_text(r.get("legacy_item_id")),
            "ebay_sku": clean_text(r.get("sku")),
            "sold_updated_at": now_iso(),
        }
    return update_rows_by_key(get_ws_name("inventory_worksheet", "inventory"), INVENTORY_COLUMNS, "inventory_id", updates)


def fetch_inventory_items(limit: int = 100) -> list[dict]:
    offset = 0
    items = []
    while True:
        data = ebay_get("/sell/inventory/v1/inventory_item", params={"limit": str(limit), "offset": str(offset)})
        batch = data.get("inventoryItems", []) or []
        items.extend(batch)
        total = int(data.get("total", len(items)) or len(items))
        if len(batch) < limit or len(items) >= total:
            break
        offset += limit
    return items


def fetch_offers_for_sku(sku: str) -> list[dict]:
    try:
        data = ebay_get("/sell/inventory/v1/offer", params={"sku": sku})
        return data.get("offers", []) or []
    except Exception:
        return []


def sync_listings() -> int:
    items = fetch_inventory_items()
    rows = []
    cfg = ebay_config()
    for item in items:
        sku = clean_text(item.get("sku"))
        product = item.get("product", {}) or {}
        availability = item.get("availability", {}) or {}
        quantity = 0
        ship = availability.get("shipToLocationAvailability", {}) or {}
        quantity = to_money(ship.get("quantity"))
        offers = fetch_offers_for_sku(sku) if sku else []
        if not offers:
            rows.append({
                "sku": sku,
                "inventory_id": sku,
                "title": clean_text(product.get("title")),
                "condition": clean_text(item.get("condition")),
                "availability": "inventory_item_only",
                "quantity": quantity,
                "marketplace_id": cfg["marketplace_id"],
                "last_synced_at": now_iso(),
                "raw_json": json.dumps(item)[:45000],
            })
        else:
            for offer in offers:
                listing = offer.get("listing", {}) or {}
                price = offer.get("pricingSummary", {}).get("price", {}) or {}
                rows.append({
                    "sku": sku,
                    "inventory_id": sku,
                    "title": clean_text(product.get("title")) or clean_text(offer.get("listingDescription")),
                    "condition": clean_text(item.get("condition")),
                    "availability": clean_text(offer.get("availableQuantity")),
                    "quantity": quantity,
                    "offer_id": clean_text(offer.get("offerId")),
                    "listing_id": clean_text(listing.get("listingId")),
                    "listing_status": clean_text(listing.get("listingStatus")),
                    "price": to_money(price.get("value")),
                    "currency": clean_text(price.get("currency")),
                    "marketplace_id": clean_text(offer.get("marketplaceId")) or cfg["marketplace_id"],
                    "last_synced_at": now_iso(),
                    "raw_json": json.dumps({"item": item, "offer": offer})[:45000],
                })
    df = pd.DataFrame(rows)
    overwrite_sheet(get_ws_name("ebay_listings_worksheet", "ebay_listings"), EBAY_LISTING_COLUMNS, df)
    return len(rows)
