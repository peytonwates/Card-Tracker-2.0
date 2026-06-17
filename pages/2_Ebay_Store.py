import base64
import requests
import streamlit as st

st.set_page_config(page_title="eBay Store", page_icon="🛒", layout="wide")

st.title("🛒 eBay Store Sync")
st.caption("Step 3: Confirm refresh token can create a new access token.")


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

st.subheader("2. Refresh Token Test")

st.write(
    "Click this once to confirm your saved refresh token can generate a new eBay access token."
)

if st.button("Test Refresh Token"):
    with st.spinner("Testing refresh token with eBay..."):
        status_code, payload = get_access_token_from_refresh_token(ebay_config)

    if status_code == 200:
        st.success("✅ Refresh token worked. eBay returned a new access token.")

        st.write(
            {
                "access_token_received": bool(payload.get("access_token")),
                "token_type": payload.get("token_type"),
                "expires_in_seconds": payload.get("expires_in"),
            }
        )

        st.info("Stop here. Tell me: Refresh token worked.")
    else:
        st.error(f"Refresh token test failed. Status code: {status_code}")
        st.write(payload)