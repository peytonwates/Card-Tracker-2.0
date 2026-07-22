# pages/8_TGCPlayer.py
from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlparse

import pandas as pd
import streamlit as st

try:
    from apify_client import ApifyClient
except ImportError:
    ApifyClient = None


st.set_page_config(page_title="TCGplayer Price Test", page_icon="📊", layout="wide")
st.title("📊 TCGplayer Sales & Market Price Test")
st.caption(
    "Test the community Apify Actor `scraped/tcgplayer-sales-history` "
    "with one TCGplayer product URL. This page does not write to inventory."
)

ACTOR_ID_DEFAULT = "scraped/tcgplayer-sales-history"


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


def _load_apify_config() -> dict[str, str]:
    section = _secret_get(st.secrets, "apify", None)

    section_token = ""
    section_actor = ""
    if section is not None:
        section_token = _first_text(
            _secret_get(section, "api_token", None),
            _secret_get(section, "token", None),
            _secret_get(section, "APIFY_API_TOKEN", None),
        )
        section_actor = _first_text(
            _secret_get(section, "actor_id", None),
            _secret_get(section, "APIFY_ACTOR_ID", None),
        )

    return {
        "api_token": _first_text(
            section_token,
            _secret_get(st.secrets, "APIFY_API_TOKEN", None),
            _secret_get(st.secrets, "apify_api_token", None),
            _secret_get(st.secrets, "apify_token", None),
        ),
        "actor_id": _first_text(
            section_actor,
            _secret_get(st.secrets, "APIFY_ACTOR_ID", None),
            _secret_get(st.secrets, "apify_actor_id", None),
            default=ACTOR_ID_DEFAULT,
        ),
    }


def _clean(value: Any) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass
    return str(value).strip()


def _number(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        try:
            if pd.isna(value):
                return None
        except Exception:
            pass
        return float(value)

    text = str(value).strip().replace("$", "").replace(",", "").replace("%", "")
    if not text:
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _money(value: Any) -> float | None:
    parsed = _number(value)
    return None if parsed is None else round(parsed, 2)


def _run_value(run: Any, *keys: str) -> Any:
    for key in keys:
        if isinstance(run, dict) and key in run:
            return run.get(key)
        try:
            value = getattr(run, key)
            if value is not None:
                return value
        except Exception:
            pass
    return None


def _is_product_url(url: str) -> bool:
    try:
        parsed = urlparse(_clean(url))
    except Exception:
        return False
    host = (parsed.hostname or "").lower()
    return host.endswith("tcgplayer.com") and "/product/" in parsed.path.lower()


def _run_actor(api_token: str, actor_id: str, product_url: str) -> tuple[dict, list[dict]]:
    if ApifyClient is None:
        raise RuntimeError(
            "The `apify-client` package is not installed. Add it to requirements.txt."
        )

    client = ApifyClient(api_token)
    run = client.actor(actor_id).call(run_input={"url": product_url})
    if run is None:
        raise RuntimeError("The Actor did not return a run record.")

    dataset_id = _run_value(run, "defaultDatasetId", "default_dataset_id")
    if not dataset_id:
        raise RuntimeError("The Actor run did not return a default dataset ID.")

    items = list(client.dataset(dataset_id).iterate_items())
    info = {
        "run_id": _clean(_run_value(run, "id")),
        "status": _clean(_run_value(run, "status")),
        "dataset_id": _clean(dataset_id),
        "actor_id": actor_id,
        "product_url": product_url,
    }
    return info, items


def _flatten_history(items: list[dict]) -> pd.DataFrame:
    rows: list[dict] = []
    for result_index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            continue

        sku_id = _clean(item.get("skuId") or item.get("skuID") or item.get("sku_id"))
        variant = _clean(item.get("variant") or item.get("printing"))
        language = _clean(item.get("language"))
        condition = _clean(item.get("condition"))

        buckets = item.get("buckets")
        if not isinstance(buckets, list):
            buckets = []

        for bucket_index, bucket in enumerate(buckets, start=1):
            if not isinstance(bucket, dict):
                continue
            rows.append(
                {
                    "result_item": result_index,
                    "bucket_index": bucket_index,
                    "sku_id": sku_id,
                    "variant": variant,
                    "language": language,
                    "condition": condition,
                    "bucket_start_date": _clean(bucket.get("bucketStartDate")),
                    "market_price": _money(bucket.get("marketPrice")),
                    "quantity_sold": _number(bucket.get("quantitySold")),
                    "transaction_count": _number(bucket.get("transactionCount")),
                    "low_sale_price": _money(bucket.get("lowSalePrice")),
                    "low_sale_with_shipping": _money(bucket.get("lowSalePriceWithShipping")),
                    "high_sale_price": _money(bucket.get("highSalePrice")),
                    "high_sale_with_shipping": _money(bucket.get("highSalePriceWithShipping")),
                }
            )

    out = pd.DataFrame(rows)
    if not out.empty:
        out["__date"] = pd.to_datetime(out["bucket_start_date"], errors="coerce")
        out = (
            out.sort_values(
                ["condition", "variant", "language", "__date"],
                ascending=[True, True, True, False],
                na_position="last",
            )
            .drop(columns=["__date"])
            .reset_index(drop=True)
        )
    return out


def _build_summary(items: list[dict], history: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict] = []

    for result_index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            continue

        item_history = history[history["result_item"].eq(result_index)].copy() if not history.empty else pd.DataFrame()
        recent = item_history.head(4).copy()

        latest = item_history.iloc[0] if not item_history.empty else pd.Series(dtype=object)

        weighted_market = None
        if not recent.empty:
            recent["__weight"] = pd.to_numeric(recent["quantity_sold"], errors="coerce").fillna(1.0)
            recent.loc[recent["__weight"].le(0), "__weight"] = 1.0
            recent["__market"] = pd.to_numeric(recent["market_price"], errors="coerce")
            valid = recent[recent["__market"].notna()].copy()
            if not valid.empty:
                weighted_market = round(
                    (valid["__market"] * valid["__weight"]).sum() / valid["__weight"].sum(),
                    2,
                )

        rows.append(
            {
                "result_item": result_index,
                "sku_id": _clean(item.get("skuId") or item.get("skuID") or item.get("sku_id")),
                "variant": _clean(item.get("variant") or item.get("printing")),
                "language": _clean(item.get("language")),
                "condition": _clean(item.get("condition")),
                "latest_bucket_date": _clean(latest.get("bucket_start_date")),
                "latest_market_price": _money(latest.get("market_price")),
                "latest_low_sale_price": _money(latest.get("low_sale_price")),
                "latest_low_with_shipping": _money(latest.get("low_sale_with_shipping")),
                "latest_high_sale_price": _money(latest.get("high_sale_price")),
                "latest_high_with_shipping": _money(latest.get("high_sale_with_shipping")),
                "recent_4_bucket_weighted_market": weighted_market,
                "average_daily_quantity_sold": _number(item.get("averageDailyQuantitySold")),
                "average_daily_transaction_count": _number(item.get("averageDailyTransactionCount")),
                "total_quantity_sold": _number(item.get("totalQuantitySold")),
                "total_transaction_count": _number(item.get("totalTransactionCount")),
                "bucket_count": int(len(item_history)),
                "trending_market_price_percentages": json.dumps(
                    item.get("trendingMarketPricePercentages", {}),
                    ensure_ascii=False,
                ),
            }
        )

    return pd.DataFrame(rows)


config = _load_apify_config()
api_token = config["api_token"]
actor_id = config["actor_id"]

if ApifyClient is None:
    st.error("Add `apify-client` to requirements.txt, commit, and redeploy.")

if not api_token:
    st.warning("Apify API token not found in Streamlit secrets.")

with st.expander("Setup required", expanded=not bool(api_token)):
    st.markdown(
        """
1. Create/sign in to Apify.
2. Open the `scraped/tcgplayer-sales-history` Actor and activate its subscription or trial.
3. In Apify Console, open **Settings → API & Integrations** and copy/create an API token.
4. Add this to Streamlit secrets:

```toml
[apify]
api_token = "YOUR_APIFY_API_TOKEN"
actor_id = "scraped/tcgplayer-sales-history"
```

5. Add this line to `requirements.txt`:

```text
apify-client
```

6. Commit and redeploy.
        """
    )
    st.caption("Do not hardcode the token in this page or commit it to GitHub.")

product_url = st.text_input(
    "TCGplayer product URL",
    placeholder="https://www.tcgplayer.com/product/...",
    help="Use one specific product page URL containing /product/.",
)

cost_confirmed = st.checkbox(
    "I understand this Actor may have a subscription and usage cost.",
    value=False,
)

run_disabled = (
    ApifyClient is None
    or not api_token
    or not _is_product_url(product_url)
    or not cost_confirmed
)

button1, button2, _ = st.columns([1.4, 1, 3])
with button1:
    run_clicked = st.button(
        "Run TCGplayer sales test",
        type="primary",
        use_container_width=True,
        disabled=run_disabled,
    )
with button2:
    clear_clicked = st.button("Clear results", use_container_width=True)

if product_url and not _is_product_url(product_url):
    st.error("Enter a TCGplayer URL containing `/product/`.")

if clear_clicked:
    for key in ["tcg_apify_run_info", "tcg_apify_items"]:
        st.session_state.pop(key, None)
    st.rerun()

if run_clicked:
    try:
        with st.spinner("Running the Apify Actor and waiting for its dataset..."):
            run_info, items = _run_actor(api_token, actor_id, product_url)
        st.session_state["tcg_apify_run_info"] = run_info
        st.session_state["tcg_apify_items"] = items
        if items:
            st.success(f"Actor returned {len(items):,} dataset item(s).")
        else:
            st.warning("The Actor completed, but its dataset was empty.")
    except Exception as exc:
        st.error("The TCGplayer Actor run failed.")
        st.exception(exc)

run_info = st.session_state.get("tcg_apify_run_info", {})
items = st.session_state.get("tcg_apify_items", [])

if not items:
    st.info("Run one product URL to test the data returned by the Actor.")
    st.stop()

history_df = _flatten_history(items)
summary_df = _build_summary(items, history_df)

st.markdown("---")
metric_cols = st.columns(4)
metric_cols[0].metric("Dataset items", f"{len(items):,}")
metric_cols[1].metric("Variants / conditions", f"{len(summary_df):,}")
metric_cols[2].metric("Sales buckets", f"{len(history_df):,}")
metric_cols[3].metric("Run status", run_info.get("status") or "Completed")

dataset_id = _clean(run_info.get("dataset_id"))
if dataset_id:
    st.markdown(
        f"[Open result dataset in Apify Console]"
        f"(https://console.apify.com/storage/datasets/{dataset_id})"
    )

summary_tab, history_tab, raw_tab = st.tabs(
    ["Market Summary", "Sales History", "Raw Actor Output"]
)

with summary_tab:
    st.subheader("Market summary by variant, language, and condition")
    if summary_df.empty:
        st.warning("No summary rows could be built from the Actor output.")
    else:
        st.dataframe(
            summary_df,
            use_container_width=True,
            hide_index=True,
            height=550,
            column_config={
                "latest_market_price": st.column_config.NumberColumn("Latest Market", format="$%.2f"),
                "latest_low_sale_price": st.column_config.NumberColumn("Latest Low", format="$%.2f"),
                "latest_low_with_shipping": st.column_config.NumberColumn("Latest Low + Shipping", format="$%.2f"),
                "latest_high_sale_price": st.column_config.NumberColumn("Latest High", format="$%.2f"),
                "latest_high_with_shipping": st.column_config.NumberColumn("Latest High + Shipping", format="$%.2f"),
                "recent_4_bucket_weighted_market": st.column_config.NumberColumn("Recent 4-Bucket Market", format="$%.2f"),
            },
        )
        st.download_button(
            "Download market summary CSV",
            data=summary_df.to_csv(index=False),
            file_name="tcgplayer_market_summary.csv",
            mime="text/csv",
        )

with history_tab:
    st.subheader("Sales-history buckets")
    if history_df.empty:
        st.warning("No `buckets` were found in the returned data.")
    else:
        c1, c2, c3 = st.columns(3)
        with c1:
            variants = sorted(history_df["variant"].dropna().astype(str).replace("", pd.NA).dropna().unique().tolist())
            selected_variants = st.multiselect("Variant", variants)
        with c2:
            conditions = sorted(history_df["condition"].dropna().astype(str).replace("", pd.NA).dropna().unique().tolist())
            selected_conditions = st.multiselect("Condition", conditions)
        with c3:
            languages = sorted(history_df["language"].dropna().astype(str).replace("", pd.NA).dropna().unique().tolist())
            selected_languages = st.multiselect("Language", languages)

        view = history_df.copy()
        if selected_variants:
            view = view[view["variant"].isin(selected_variants)]
        if selected_conditions:
            view = view[view["condition"].isin(selected_conditions)]
        if selected_languages:
            view = view[view["language"].isin(selected_languages)]

        st.dataframe(
            view,
            use_container_width=True,
            hide_index=True,
            height=600,
            column_config={
                "market_price": st.column_config.NumberColumn("Market Price", format="$%.2f"),
                "low_sale_price": st.column_config.NumberColumn("Low Sale", format="$%.2f"),
                "low_sale_with_shipping": st.column_config.NumberColumn("Low + Shipping", format="$%.2f"),
                "high_sale_price": st.column_config.NumberColumn("High Sale", format="$%.2f"),
                "high_sale_with_shipping": st.column_config.NumberColumn("High + Shipping", format="$%.2f"),
            },
        )
        st.download_button(
            "Download current sales-history view",
            data=view.to_csv(index=False),
            file_name="tcgplayer_sales_history.csv",
            mime="text/csv",
        )

with raw_tab:
    st.subheader("Raw Actor output")
    st.caption("Use this if the community Actor changes its response structure.")
    st.json(items)
    st.download_button(
        "Download raw Actor JSON",
        data=json.dumps(items, indent=2, ensure_ascii=False),
        file_name="tcgplayer_actor_raw_output.json",
        mime="application/json",
    )
