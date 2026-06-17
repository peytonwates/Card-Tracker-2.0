from __future__ import annotations

import base64
import re
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import requests
import streamlit as st

from core.business import load_data, refresh_database_cache, mark_inventory_sold
from core.cleaning import clean_text, to_money, money_fmt, now_iso
from core.config import INVENTORY_COLUMNS, STATUS_ACTIVE, STATUS_LISTED, STATUS_SOLD
from core.sheets import get_ws_name, update_rows_by_key


st.set_page_config(page_title="eBay Store", page_icon="🛒", layout="wide")

st.title("🛒 eBay Store Sync")
st.caption(
    "Pull active eBay listings, assign them to inventory, then sync sold eBay orders back to inventory."
)


# =========================================================
# eBay auth / config
# =========================================================

def get_ebay_secrets():
    try:
        ebay = st.secrets["ebay"]
        return {
            "environment": ebay.get("environment", "production"),
            "marketplace_id": ebay.get("marketplace_id", "EBAY_US"),
            "client_id": ebay["client_id"],
            "client_secret": ebay["client_secret"],
            "ru_name": ebay["ru_name"],
            "scopes": ebay["scopes"],
            "refresh_token": ebay["refresh_token"],
        }
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
# XML helpers for Trading API
# =========================================================

EBAY_XML_NS = {"e": "urn:ebay:apis:eBLBaseComponents"}
TRADING_API_ENDPOINT = "https://api.ebay.com/ws/api.dll"


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


def get_active_listings(access_token: str, entries_per_page: int = 100, max_pages: int = 5) -> tuple[pd.DataFrame, dict]:
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
    }

    response = requests.get(url, headers=headers, params=params, timeout=30)

    try:
        payload = response.json()
    except Exception:
        payload = {"raw_response": response.text}

    return response.status_code, payload, params


def flatten_orders(order_payload):
    rows = []

    for order in order_payload.get("orders", []):
        order_id = order.get("orderId")
        creation_date = order.get("creationDate")
        order_status = order.get("orderFulfillmentStatus")
        payment_status = order.get("orderPaymentStatus")

        pricing_summary = order.get("pricingSummary", {})
        total = pricing_summary.get("total", {})
        total_value = total.get("value")
        total_currency = total.get("currency")

        for item in order.get("lineItems", []):
            line_cost = item.get("lineItemCost", {}) or {}

            legacy_item_id = clean_text(item.get("legacyItemId"))
            item_id = clean_text(item.get("itemId"))
            ebay_item_id = legacy_item_id or item_id

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
                    "sold_price": to_money(line_cost.get("value")),
                    "line_item_currency": line_cost.get("currency"),
                    "order_total_value": to_money(total_value),
                    "order_total_currency": total_currency,
                }
            )

    return pd.DataFrame(rows)


# =========================================================
# Inventory matching helpers
# =========================================================

def _safe_df(df: pd.DataFrame | None) -> pd.DataFrame:
    return pd.DataFrame() if df is None else df.copy()


def _ensure_cols(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    out = df.copy()

    for col in cols:
        if col not in out.columns:
            out[col] = ""

    return out


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


def _suggest_inventory_matches(inv: pd.DataFrame, listing_title: str, max_rows: int = 25) -> pd.DataFrame:
    if inv.empty:
        return inv.copy()

    ready = inv[
        inv["inventory_status"].astype(str).str.upper().isin([STATUS_ACTIVE, STATUS_LISTED])
    ].copy()

    if ready.empty:
        return ready

    title = clean_text(listing_title).lower()

    if not title:
        return ready.head(max_rows)

    def score(row: pd.Series) -> int:
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

    ready["__match_score"] = ready.apply(score, axis=1)
    ready = ready.sort_values(["__match_score", "market_value"], ascending=[False, False])

    return ready.head(max_rows).drop(columns=["__match_score"], errors="ignore")


def _display_listing_cols() -> list[str]:
    return [
        "assigned",
        "ebay_item_id",
        "title",
        "listing_status",
        "current_price",
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
        "inventory_id",
        "ebay_order_id",
        "ebay_line_item_id",
        "ebay_item_id",
        "sold_date",
        "title",
        "quantity",
        "sold_price",
        "order_status",
        "payment_status",
    ]


# =========================================================
# Load app data
# =========================================================

ebay_config = get_ebay_secrets()

if not ebay_config:
    st.stop()

required_fields = [
    "client_id",
    "client_secret",
    "ru_name",
    "scopes",
    "refresh_token",
]

missing = [field for field in required_fields if not ebay_config.get(field)]

if missing:
    st.error(f"Missing required eBay secret fields: {', '.join(missing)}")
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
# Top config / status
# =========================================================

top1, top2, top3 = st.columns([1, 1, 3])

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
        ]:
            st.session_state.pop(key, None)
        st.success("Cleared eBay page cache.")

with top3:
    st.info(
        "Workflow: pull active listings → assign unassigned listings to inventory → sync sold orders.",
        icon="ℹ️",
    )

with st.expander("eBay config check", expanded=False):
    st.write(
        {
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

    st.caption(
        "This pulls listings currently active in your eBay account. It does not create or edit eBay listings."
    )

    c1, c2, c3 = st.columns([1, 1, 2])

    with c1:
        entries_per_page = st.number_input(
            "Listings per page",
            min_value=25,
            max_value=200,
            value=100,
            step=25,
        )

    with c2:
        max_pages = st.number_input(
            "Max pages",
            min_value=1,
            max_value=25,
            value=5,
            step=1,
        )

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
        m2.metric("Assigned", f"{listings_df['assigned'].sum():,}")
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
            unassigned["label"] = unassigned.apply(
                lambda r: f"{r.get('ebay_item_id')} — {money_fmt(r.get('current_price'))} — {r.get('title')}",
                axis=1,
            )

            selected_listing_label = st.selectbox(
                "Unassigned eBay listing",
                unassigned["label"].tolist(),
            )

            selected_listing = unassigned[
                unassigned["label"].eq(selected_listing_label)
            ].iloc[0]

            st.markdown("### Listing selected")

            l1, l2 = st.columns([1, 3])

            with l1:
                image_url = clean_text(selected_listing.get("image_url"))
                if image_url:
                    st.image(image_url, width=160)
                st.metric("Current price", money_fmt(selected_listing.get("current_price")))
                st.write(f"Item ID: `{selected_listing.get('ebay_item_id')}`")
                st.write(f"Status: `{selected_listing.get('listing_status')}`")

            with l2:
                st.write(f"**{selected_listing.get('title')}**")
                url = clean_text(selected_listing.get("listing_url"))
                if url:
                    st.link_button("Open eBay listing", url)
                st.write(
                    {
                        "listing_start_date": selected_listing.get("listing_start_date"),
                        "listing_end_date": selected_listing.get("listing_end_date"),
                        "quantity_available": selected_listing.get("quantity_available"),
                        "quantity_sold": selected_listing.get("quantity_sold"),
                        "listing_type": selected_listing.get("listing_type"),
                    }
                )

            st.markdown("### Pick matching inventory item")

            suggested = _suggest_inventory_matches(inv, clean_text(selected_listing.get("title")), max_rows=50)

            search = st.text_input("Filter inventory suggestions", placeholder="Search name, set, number, ID...")

            if search.strip() and not suggested.empty:
                q = search.lower().strip()

                def _match(row: pd.Series) -> bool:
                    fields = [
                        row.get("inventory_id", ""),
                        row.get("set_name", ""),
                        row.get("card_name", ""),
                        row.get("card_number", ""),
                        row.get("variant", ""),
                        row.get("grade", ""),
                        row.get("reference_link", ""),
                    ]
                    return q in " ".join(str(x).lower() for x in fields)

                suggested = suggested[suggested.apply(_match, axis=1)].copy()

            if suggested.empty:
                st.warning("No suggested inventory rows found. Try changing the search.")
            else:
                suggested["label"] = suggested.apply(_inventory_label, axis=1)

                selected_inventory_label = st.selectbox(
                    "Inventory item",
                    suggested["label"].tolist(),
                )

                selected_inventory = suggested[
                    suggested["label"].eq(selected_inventory_label)
                ].iloc[0]

                preview_cols = [
                    "inventory_id",
                    "inventory_status",
                    "inventory_type",
                    "product_type",
                    "set_name",
                    "card_name",
                    "card_number",
                    "variant",
                    "grade",
                    "total_cost",
                    "market_value",
                    "sticker_price",
                ]

                st.dataframe(
                    selected_inventory[[c for c in preview_cols if c in selected_inventory.index]].to_frame().T,
                    use_container_width=True,
                    hide_index=True,
                )

                if st.button("Assign this eBay listing to selected inventory", type="primary"):
                    inv_id = clean_text(selected_inventory.get("inventory_id"))
                    item_id = clean_text(selected_listing.get("ebay_item_id"))
                    listing_price = to_money(selected_listing.get("current_price"))

                    updates = {
                        "inventory_status": STATUS_LISTED,
                        "transaction_type": clean_text(selected_listing.get("listing_type")) or "eBay Listing",
                        "platform": "eBay",
                        "sale_channel": "Online",
                        "list_date": clean_text(selected_listing.get("listing_start_date")) or str(date.today()),
                        "list_price": round(listing_price, 2),
                        "ebay_item_id": item_id,
                        "ebay_listing_id": clean_text(selected_listing.get("ebay_listing_id")) or item_id,
                        "ebay_listing_url": clean_text(selected_listing.get("listing_url")),
                        "ebay_listing_status": clean_text(selected_listing.get("listing_status")) or "Active",
                        "ebay_last_sync_at": now_iso(),
                    }

                    update_rows_by_key(
                        get_ws_name("inventory_worksheet", "inventory"),
                        INVENTORY_COLUMNS,
                        "inventory_id",
                        {inv_id: updates},
                    )

                    refresh_database_cache()

                    st.success(f"Assigned eBay listing {item_id} to inventory {inv_id}.")
                    st.rerun()

        st.markdown("---")
        st.markdown("### Already assigned listings")

        assigned = listings_df[listings_df["assigned"]].copy()

        if assigned.empty:
            st.info("No assigned listings from the pulled active list.")
        else:
            cols = [c for c in _display_listing_cols() if c in assigned.columns]
            st.dataframe(
                assigned[cols],
                use_container_width=True,
                hide_index=True,
                column_config={
                    "listing_url": st.column_config.LinkColumn("Listing URL"),
                    "current_price": st.column_config.NumberColumn("Current Price", format="$%.2f"),
                    "assigned": st.column_config.CheckboxColumn("Assigned"),
                },
            )


# =========================================================
# Tab 3: Sold Order Sync
# =========================================================

with tab_orders:
    st.subheader("Sold eBay Order Sync")

    st.caption(
        "This pulls recent sold orders, matches them to assigned inventory, and can mark matched items SOLD."
    )

    c1, c2, c3 = st.columns([1, 1, 2])

    with c1:
        days_back = st.number_input(
            "Days back to pull",
            min_value=1,
            max_value=90,
            value=30,
            step=1,
        )

    with c2:
        order_limit = st.number_input(
            "Order limit",
            min_value=10,
            max_value=200,
            value=100,
            step=10,
        )

    with c3:
        st.write("")
        st.write("")
        pull_orders = st.button("Pull Recent eBay Orders", type="primary", use_container_width=True)

    if pull_orders:
        access_token = get_access_token_or_stop(ebay_config)

        with st.spinner("Pulling recent eBay orders..."):
            order_status, order_payload, used_params = get_recent_orders(
                access_token=access_token,
                days_back=int(days_back),
                limit=int(order_limit),
            )

        st.session_state["ebay_orders_payload"] = order_payload
        st.session_state["ebay_orders_filter"] = used_params

        if order_status != 200:
            st.error(f"Order pull failed. Status code: {order_status}")
            st.write(order_payload)
            st.stop()

        df_orders = flatten_orders(order_payload)
        st.session_state["ebay_orders_df"] = df_orders

        total_orders = order_payload.get("total", 0)
        st.success(
            f"Pulled {len(df_orders):,} order line item(s). Total matching orders reported by eBay: {total_orders}."
        )

    orders_df = st.session_state.get("ebay_orders_df", pd.DataFrame()).copy()

    if orders_df.empty:
        st.info("No eBay orders pulled yet. Click the button above.")
    else:
        matched_rows = []

        for _, order_row in orders_df.iterrows():
            ebay_item_id = clean_text(order_row.get("ebay_item_id"))
            match_df = _find_inventory_match_by_ebay_id(inv, ebay_item_id)

            if match_df.empty:
                matched_rows.append(
                    {
                        **order_row.to_dict(),
                        "matched": False,
                        "already_sold": False,
                        "inventory_id": "",
                        "inventory_status": "",
                        "total_cost": 0.0,
                    }
                )
            else:
                inv_match = match_df.iloc[0]
                already_sold = clean_text(inv_match.get("inventory_status")).upper() == STATUS_SOLD

                matched_rows.append(
                    {
                        **order_row.to_dict(),
                        "matched": True,
                        "already_sold": already_sold,
                        "inventory_id": clean_text(inv_match.get("inventory_id")),
                        "inventory_status": clean_text(inv_match.get("inventory_status")),
                        "total_cost": to_money(inv_match.get("total_cost")),
                    }
                )

        sync_df = pd.DataFrame(matched_rows)

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Order line items", f"{len(sync_df):,}")
        m2.metric("Matched to inventory", f"{int(sync_df['matched'].sum()):,}")
        m3.metric("Unmatched", f"{int((~sync_df['matched']).sum()):,}")
        m4.metric("Already sold", f"{int(sync_df['already_sold'].sum()):,}")

        cols = [c for c in _display_order_cols() if c in sync_df.columns]

        st.markdown("### Order match preview")

        st.dataframe(
            sync_df[cols],
            use_container_width=True,
            hide_index=True,
            column_config={
                "matched": st.column_config.CheckboxColumn("Matched"),
                "already_sold": st.column_config.CheckboxColumn("Already Sold"),
                "sold_price": st.column_config.NumberColumn("Sold Price", format="$%.2f"),
            },
        )

        unmatched = sync_df[~sync_df["matched"]].copy()

        if not unmatched.empty:
            with st.expander("Unmatched order lines", expanded=True):
                st.warning(
                    "These sold items could not be matched to inventory yet. Assign the active listing first, then rerun order sync."
                )
                st.dataframe(
                    unmatched[
                        [
                            "ebay_order_id",
                            "ebay_line_item_id",
                            "ebay_item_id",
                            "sold_date",
                            "title",
                            "sold_price",
                        ]
                    ],
                    use_container_width=True,
                    hide_index=True,
                )

        ready_to_mark = sync_df[
            sync_df["matched"].eq(True)
            & sync_df["already_sold"].eq(False)
        ].copy()

        st.markdown("### Mark matched eBay orders SOLD")

        if ready_to_mark.empty:
            st.info("No matched unsold order lines are ready to mark SOLD.")
        else:
            st.caption(
                "Fees are not pulled yet in this step. This will record sold price and leave fees at $0 until we add Finances API reconciliation."
            )

            preview = ready_to_mark.copy()
            preview["estimated_net_proceeds"] = preview["sold_price"].apply(to_money)
            preview["estimated_profit"] = preview["estimated_net_proceeds"] - preview["total_cost"].apply(to_money)

            st.dataframe(
                preview[
                    [
                        "inventory_id",
                        "ebay_order_id",
                        "ebay_line_item_id",
                        "ebay_item_id",
                        "sold_date",
                        "title",
                        "sold_price",
                        "total_cost",
                        "estimated_profit",
                    ]
                ],
                use_container_width=True,
                hide_index=True,
                column_config={
                    "sold_price": st.column_config.NumberColumn("Sold Price", format="$%.2f"),
                    "total_cost": st.column_config.NumberColumn("Total Cost", format="$%.2f"),
                    "estimated_profit": st.column_config.NumberColumn("Est Profit Before Fees", format="$%.2f"),
                },
            )

            confirm = st.checkbox(
                "I understand fees are not included yet. Mark these matched eBay order lines SOLD.",
                value=False,
            )

            if st.button(
                "Mark matched eBay orders SOLD",
                type="primary",
                disabled=not confirm,
            ):
                changed = 0

                for _, row in ready_to_mark.iterrows():
                    inv_id = clean_text(row.get("inventory_id"))
                    sold_price = to_money(row.get("sold_price"))
                    total_cost = to_money(row.get("total_cost"))

                    fees = 0.0
                    shipping_charged = 0.0
                    fees_total = 0.0
                    net = round(sold_price - fees_total, 2)
                    profit = round(net - total_cost, 2)

                    updates = {
                        "transaction_type": "eBay Order",
                        "platform": "eBay",
                        "sold_date": clean_text(row.get("sold_date")) or str(date.today()),
                        "sold_price": round(sold_price, 2),
                        "fees": round(fees, 2),
                        "shipping_charged": round(shipping_charged, 2),
                        "fees_total": round(fees_total, 2),
                        "net_proceeds": net,
                        "profit": profit,
                        "sale_channel": "eBay",
                        "sale_notes": "Synced from eBay order pull. Fees not reconciled yet.",
                        "ebay_order_id": clean_text(row.get("ebay_order_id")),
                        "ebay_line_item_id": clean_text(row.get("ebay_line_item_id")),
                        "ebay_item_id": clean_text(row.get("ebay_item_id")),
                        "ebay_listing_id": clean_text(row.get("ebay_item_id")),
                        "ebay_listing_status": "Sold",
                        "ebay_last_sync_at": now_iso(),
                        "sold_transaction_id": clean_text(row.get("ebay_line_item_id")) or clean_text(row.get("ebay_order_id")),
                        "sold_created_at": now_iso(),
                        "sold_updated_at": now_iso(),
                    }

                    changed += mark_inventory_sold(inv_id, updates)

                refresh_database_cache()

                st.success(f"Marked {changed:,} inventory item(s) SOLD from eBay orders.")
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

    st.markdown("### Recent orders raw JSON")
    order_payload = st.session_state.get("ebay_orders_payload", {})
    order_filter = st.session_state.get("ebay_orders_filter", {})

    if order_filter:
        st.write("Order request filter used:")
        st.code(order_filter.get("filter", ""))

    if order_payload:
        with st.expander("Raw Fulfillment API order response", expanded=False):
            st.json(order_payload)