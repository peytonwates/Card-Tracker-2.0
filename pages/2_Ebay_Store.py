import base64
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests
import streamlit as st


st.set_page_config(page_title="eBay Store", page_icon="🛒", layout="wide")

st.title("🛒 eBay Store Sync")
st.caption("Step 4: Confirm we can pull recent eBay orders.")


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


def get_recent_orders(access_token, days_back=30, limit=25):
    end_dt = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=days_back)

    # eBay expects UTC timestamps like 2026-06-17T00:00:00.000Z
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
            line_cost = item.get("lineItemCost", {})
            rows.append(
                {
                    "order_id": order_id,
                    "creation_date": creation_date,
                    "order_status": order_status,
                    "payment_status": payment_status,
                    "line_item_id": item.get("lineItemId"),
                    "legacy_item_id": item.get("legacyItemId"),
                    "sku": item.get("sku"),
                    "title": item.get("title"),
                    "quantity": item.get("quantity"),
                    "line_item_value": line_cost.get("value"),
                    "line_item_currency": line_cost.get("currency"),
                    "order_total_value": total_value,
                    "order_total_currency": total_currency,
                }
            )

    return pd.DataFrame(rows)


ebay_config = get_ebay_secrets()

if not ebay_config:
    st.stop()

st.subheader("1. Secret Check")

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

st.success("eBay secrets loaded successfully.")

with st.expander("Show non-secret config"):
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

st.subheader("2. Pull Recent Orders")

days_back = st.number_input(
    "Days back to pull",
    min_value=1,
    max_value=90,
    value=30,
    step=1,
)

if st.button("Pull Recent eBay Orders"):
    with st.spinner("Getting access token..."):
        token_status, token_payload = get_access_token_from_refresh_token(ebay_config)

    if token_status != 200:
        st.error(f"Could not get access token. Status code: {token_status}")
        st.write(token_payload)
        st.stop()

    access_token = token_payload.get("access_token")

    if not access_token:
        st.error("eBay did not return an access token.")
        st.write(token_payload)
        st.stop()

    with st.spinner("Pulling recent eBay orders..."):
        order_status, order_payload, used_params = get_recent_orders(
            access_token=access_token,
            days_back=int(days_back),
            limit=25,
        )

    st.write("Request filter used:")
    st.code(used_params["filter"])

    if order_status != 200:
        st.error(f"Order pull failed. Status code: {order_status}")
        st.write(order_payload)
        st.stop()

    total_orders = order_payload.get("total", 0)
    orders = order_payload.get("orders", [])

    st.success(f"✅ eBay order pull worked. Orders returned on this page: {len(orders)}. Total matching orders: {total_orders}.")

    df_orders = flatten_orders(order_payload)

    if df_orders.empty:
        st.info("No orders found in this date range. That can be okay if you had no completed eBay checkout orders during this period.")
    else:
        st.dataframe(df_orders, use_container_width=True)

    with st.expander("Show raw eBay response"):
        st.json(order_payload)