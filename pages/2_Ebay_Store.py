import base64
import secrets
from urllib.parse import urlencode

import requests
import streamlit as st


st.set_page_config(page_title="eBay Store", page_icon="🛒", layout="wide")

st.title("🛒 eBay Store Sync")
st.caption("Step 2: Exchange eBay authorization code for tokens.")


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
        }
    except Exception as e:
        st.error("Could not load eBay secrets from Streamlit secrets.")
        st.exception(e)
        return None


def build_auth_url(ebay_config):
    state_value = secrets.token_urlsafe(16)

    params = {
        "client_id": ebay_config["client_id"],
        "redirect_uri": ebay_config["ru_name"],
        "response_type": "code",
        "scope": ebay_config["scopes"],
        "state": state_value,
    }

    return f"https://auth.ebay.com/oauth2/authorize?{urlencode(params)}"


def exchange_code_for_tokens(ebay_config, auth_code):
    token_url = "https://api.ebay.com/identity/v1/oauth2/token"

    credentials = f"{ebay_config['client_id']}:{ebay_config['client_secret']}"
    encoded_credentials = base64.b64encode(credentials.encode("utf-8")).decode("utf-8")

    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Authorization": f"Basic {encoded_credentials}",
    }

    data = {
        "grant_type": "authorization_code",
        "code": auth_code,
        "redirect_uri": ebay_config["ru_name"],
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

required_fields = ["client_id", "client_secret", "ru_name", "scopes"]
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
            "scopes": ebay_config["scopes"],
        }
    )


st.subheader("2. eBay OAuth Test")

query_params = st.query_params

if "error" in query_params:
    st.error("eBay returned an error.")
    st.write(dict(query_params))
    st.stop()


auth_code = query_params.get("code")

if auth_code:
    st.success("✅ eBay returned an authorization code.")

    st.warning(
        "Next, click the button below one time to exchange the temporary code for tokens. "
        "Do not share screenshots after this point if token details are visible."
    )

    if st.button("Exchange Authorization Code for Tokens"):
        with st.spinner("Exchanging authorization code with eBay..."):
            status_code, token_payload = exchange_code_for_tokens(ebay_config, auth_code)

        if status_code == 200:
            st.success("✅ Token exchange worked.")

            access_token = token_payload.get("access_token")
            refresh_token = token_payload.get("refresh_token")

            st.write(
                {
                    "token_type": token_payload.get("token_type"),
                    "access_token_received": bool(access_token),
                    "refresh_token_received": bool(refresh_token),
                    "access_token_expires_in_seconds": token_payload.get("expires_in"),
                    "refresh_token_expires_in_seconds": token_payload.get("refresh_token_expires_in"),
                }
            )

            if refresh_token:
                st.subheader("Refresh Token")
                st.warning(
                    "This refresh token is sensitive. Do not paste it into chat. "
                    "You will add it to Streamlit secrets in the next step."
                )

                st.text_area(
                    "Copy this refresh token into Streamlit secrets later:",
                    value=refresh_token,
                    height=160,
                )

                st.info(
                    "Stop here. Tell me: Token exchange worked and I have the refresh token."
                )
        else:
            st.error(f"Token exchange failed. Status code: {status_code}")
            st.write(token_payload)
            st.info(
                "If the error says the code is expired or already used, generate a new eBay sign-in link and try again."
            )

else:
    st.info("No eBay authorization code found yet.")

    if st.button("Generate eBay Sign-In Link"):
        auth_url = build_auth_url(ebay_config)
        st.success("Sign-in link generated.")
        st.link_button("Connect eBay Account", auth_url)
        st.caption("After approving access on eBay, you should be redirected back to this Streamlit page.")