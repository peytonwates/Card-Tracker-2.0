import secrets
from urllib.parse import urlencode

import streamlit as st


st.set_page_config(page_title="eBay Store", page_icon="🛒", layout="wide")

st.title("🛒 eBay Store Sync")
st.caption("Step 1: Connect to eBay and confirm OAuth redirect works.")


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

if "code" in query_params:
    st.success("✅ eBay returned an authorization code. This part is working.")
    st.info("Do not paste the authorization code here. It is temporary and we will exchange it in the next step.")
    st.write("Next, tell me: **I see the green authorization code success message.**")
    st.stop()

if "error" in query_params:
    st.error("eBay returned an error.")
    st.write(dict(query_params))
    st.stop()


if st.button("Generate eBay Sign-In Link"):
    state_value = secrets.token_urlsafe(16)

    auth_base_url = "https://auth.ebay.com/oauth2/authorize"

    params = {
        "client_id": ebay_config["client_id"],
        "redirect_uri": ebay_config["ru_name"],
        "response_type": "code",
        "scope": ebay_config["scopes"],
        "state": state_value,
    }

    auth_url = f"{auth_base_url}?{urlencode(params)}"

    st.success("Sign-in link generated.")
    st.link_button("Connect eBay Account", auth_url)

    st.caption("After approving access on eBay, you should be redirected back to this Streamlit page.")