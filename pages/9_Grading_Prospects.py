from __future__ import annotations

from typing import Any

import pandas as pd
import streamlit as st

from core.prospects import (
    CATEGORIES,
    EBAY_SOLD_ACTOR_ID,
    MIN_PSA_POPULATION,
    apply_grading_fee,
    find_prospects,
    mark_owned,
)


st.set_page_config(page_title="Grading Prospects", layout="wide")
st.title("Grading Prospects")
st.caption(
    "PSA gem rate from GemRate, PSA 10 and raw prices from eBay sold listings, "
    "and the margin after the grading fee you enter. "
    "eBay’s public sold index covers about the last 90 days, so a card needs "
    "at least 20 matching PSA 10 sales in that window."
)


def _secret_get(source: Any, key: str, default: Any = None) -> Any:
    try:
        if hasattr(source, "get"):
            return source.get(key, default)
    except Exception:
        pass
    try:
        return source[key]
    except Exception:
        return default


def _first_text(*values: Any, default: str = "") -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return default


def _gemrate_api_key() -> str:
    section = _secret_get(st.secrets, "gemrate", None)
    section_key = ""
    if section is not None:
        section_key = _first_text(
            _secret_get(section, "api_key", None),
            _secret_get(section, "apiKey", None),
        )
    return _first_text(section_key, _secret_get(st.secrets, "gemrate_api_key", None))


def _apify_settings() -> dict[str, str]:
    section = _secret_get(st.secrets, "apify", None)
    section_token = ""
    section_actor = ""
    if section is not None:
        section_token = _first_text(
            _secret_get(section, "api_token", None),
            _secret_get(section, "token", None),
            _secret_get(section, "APIFY_API_TOKEN", None),
        )
        section_actor = _first_text(_secret_get(section, "ebay_sold_actor_id", None))
    return {
        "api_token": _first_text(
            section_token,
            _secret_get(st.secrets, "APIFY_API_TOKEN", None),
            _secret_get(st.secrets, "apify_api_token", None),
            _secret_get(st.secrets, "apify_token", None),
        ),
        "actor_id": _first_text(section_actor, default=EBAY_SOLD_ACTOR_ID),
    }


def _default_fee() -> float:
    try:
        return float(_secret_get(st.secrets, "default_grading_fee_per_card", 22.0) or 22.0)
    except (TypeError, ValueError):
        return 22.0


def _inventory() -> tuple[pd.DataFrame, bool]:
    try:
        from core.business import load_data

        return load_data().inventory, True
    except Exception:
        return pd.DataFrame(), False


@st.cache_data(ttl=60 * 60 * 12, show_spinner=False)
def _cached_prospects(
    api_key: str,
    apify_token: str,
    actor_id: str,
    category: str,
    set_name: str,
    year: str,
) -> dict:
    return find_prospects(
        api_key,
        apify_token,
        category,
        set_name,
        year,
        actor_id=actor_id,
    )


def _display_frame(rows: list[dict], sort_by: str) -> pd.DataFrame:
    sort_key = "margin_dollars" if sort_by == "Margin $" else "margin_pct"
    ordered = sorted(rows, key=lambda row: float(row.get(sort_key) or 0.0), reverse=True)
    display = pd.DataFrame(
        [
            {
                "Card": row.get("card_name", ""),
                "Year": row.get("year", ""),
                "Set": row.get("set_name", ""),
                "Number": row.get("card_number", ""),
                "Parallel": row.get("parallel", ""),
                "PSA gem rate": round(float(row.get("psa_gem_rate") or 0.0) * 100.0, 2),
                "PSA population": int(row.get("psa_population") or 0),
                "PSA 10 price": float(row.get("psa10_price") or 0.0),
                "PSA 10 sales": int(row.get("psa10_sales") or 0),
                "Confidence": row.get("confidence", ""),
                "Raw price": float(row.get("raw_price") or 0.0),
                "Raw sales": int(row.get("raw_sales") or 0),
                "Grading fee": float(row.get("grading_fee") or 0.0),
                "Margin $": float(row.get("margin_dollars") or 0.0),
                "Margin %": float(row.get("margin_pct") or 0.0),
                "Owned": "Yes" if row.get("owned") else "",
                "GemRate": row.get("gemrate_url", ""),
            }
            for row in ordered
        ]
    )
    return display


gemrate_key = _gemrate_api_key()
apify = _apify_settings()
ready = bool(gemrate_key and apify["api_token"])

with st.expander("Setup", expanded=not ready):
    st.markdown(
        f"""
GemRate supplies the PSA gem rate. eBay sold listings come from the Apify actor
`{EBAY_SOLD_ACTOR_ID}`, using the same Apify token as the TCGplayer page.

```toml
[gemrate]
api_key = "YOUR_GEMRATE_API_KEY"

[apify]
api_token = "YOUR_APIFY_API_TOKEN"
ebay_sold_actor_id = "{EBAY_SOLD_ACTOR_ID}"
```

`ebay_sold_actor_id` is separate from the TCGplayer `actor_id`.
"""
    )

if not gemrate_key:
    st.warning("Add a GemRate API key before searching.")
if not apify["api_token"]:
    st.warning("Add an Apify API token before searching.")

fee_col, sort_col = st.columns([1, 1])
with fee_col:
    grading_fee = st.number_input(
        "Grading fee",
        min_value=0.0,
        value=_default_fee(),
        step=1.0,
        format="%.2f",
        help="Dollar margin is the PSA 10 average minus the raw average minus this fee.",
    )
with sort_col:
    sort_by = st.selectbox("Sort by", ["Margin %", "Margin $"])

with st.form("grading_prospects_search"):
    category_col, set_col, year_col = st.columns([1, 2, 1])
    with category_col:
        category = st.selectbox("Category", CATEGORIES, key="prospect_category")
    with set_col:
        set_name = st.text_input("Set", placeholder="Surging Sparks, Prizm, Chrome", key="prospect_set_name")
    with year_col:
        year = st.text_input("Year", placeholder="Optional", key="prospect_year")
    submitted = st.form_submit_button("Find prospects", type="primary", disabled=not ready)

if submitted:
    if not set_name.strip():
        st.warning("Enter a set name.")
    else:
        st.session_state["prospect_search"] = {
            "category": category,
            "set_name": set_name.strip(),
            "year": year.strip(),
        }

search = st.session_state.get("prospect_search")
if search and ready:
    st.caption(
        f"{search['category']} · {search['set_name']}"
        + (f" · {search['year']}" if search.get("year") else "")
        + f". Cards need at least {MIN_PSA_POPULATION} PSA copies to be priced."
    )
    try:
        with st.spinner("Looking up GemRate populations and eBay sold listings. The first search of a set can take several minutes."):
            result = _cached_prospects(
                gemrate_key,
                apify["api_token"],
                apify["actor_id"],
                search["category"],
                search["set_name"],
                search.get("year") or "",
            )
    except Exception as exc:
        st.error(str(exc))
    else:
        inventory, inventory_loaded = _inventory()
        if not inventory_loaded:
            st.caption("Inventory did not load, so the owned column is blank.")
        rows = mark_owned(apply_grading_fee(result.get("prospects") or [], grading_fee), inventory)
        st.caption(
            f"GemRate matches: {int(result.get('gemrate_matches') or 0):,}. "
            f"Priced {int(result.get('shortlisted') or 0):,} highest PSA gem-rate cards. "
            f"{int(result.get('qualified') or 0):,} cleared 20 PSA 10 sales and 5 raw sales."
        )
        if not rows:
            st.info("No cards in this set cleared the sale-count rules.")
        else:
            frame = _display_frame(rows, sort_by)
            st.dataframe(
                frame,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "PSA gem rate": st.column_config.NumberColumn("PSA gem rate", format="%.1f%%"),
                    "PSA population": st.column_config.NumberColumn("PSA population", format="%d"),
                    "PSA 10 price": st.column_config.NumberColumn("PSA 10 price", format="$%.2f"),
                    "PSA 10 sales": st.column_config.NumberColumn("PSA 10 sales", format="%d"),
                    "Raw price": st.column_config.NumberColumn("Raw price", format="$%.2f"),
                    "Raw sales": st.column_config.NumberColumn("Raw sales", format="%d"),
                    "Grading fee": st.column_config.NumberColumn("Grading fee", format="$%.2f"),
                    "Margin $": st.column_config.NumberColumn("Margin $", format="$%.2f"),
                    "Margin %": st.column_config.NumberColumn("Margin %", format="%.1f%%"),
                    "GemRate": st.column_config.LinkColumn("GemRate", display_text="Open"),
                },
            )
