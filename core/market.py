from __future__ import annotations

import re
import time
from urllib.parse import urlparse, urlunparse

import pandas as pd
import requests
from bs4 import BeautifulSoup
import streamlit as st

from .cleaning import clean_text, now_iso, to_money
from .config import INVENTORY_COLUMNS, STATUS_ACTIVE, STATUS_GRADING
from .sheets import get_ws_name, update_rows_by_key


def canonicalize_reference_link(url: str) -> str:
    url = clean_text(url)
    if not url:
        return ""
    if url.startswith("//"):
        url = "https:" + url
    elif not url.startswith(("http://", "https://")):
        url = "https://" + url
    try:
        p = urlparse(url)
        netloc = p.netloc.lower()
        if "sportscardspro.com" in netloc:
            netloc = "www.sportscardspro.com"
        return urlunparse(("https", netloc, (p.path or "").rstrip("/"), "", "", ""))
    except Exception:
        return url


@st.cache_resource
def http_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    })
    return s


def _parse_money(text: str) -> float:
    m = re.search(r"\$\s*([0-9][0-9,]*\.?[0-9]{0,2})", text or "")
    if not m:
        return 0.0
    return to_money(m.group(1))


def _pick(price_map: dict[str, float], labels: list[str]) -> float:
    lower = {k.lower(): v for k, v in price_map.items()}
    for lab in labels:
        if lab.lower() in lower:
            return float(lower[lab.lower()] or 0.0)
    for k, v in lower.items():
        for lab in labels:
            if lab.lower() in k:
                return float(v or 0.0)
    return 0.0


@st.cache_data(ttl=60 * 60 * 12, show_spinner=False)
def fetch_market_prices(reference_link: str) -> dict:
    out = {"raw": 0.0, "psa9": 0.0, "psa10": 0.0, "image_url": "", "debug": ""}
    url = canonicalize_reference_link(reference_link)
    if not url:
        out["debug"] = "no_link"
        return out
    if "pricecharting.com" not in url.lower() and "sportscardspro.com" not in url.lower():
        out["debug"] = "unsupported_domain"
        return out
    try:
        resp = http_session().get(url, timeout=18)
        if resp.status_code != 200:
            out["debug"] = f"http_{resp.status_code}"
            return out
        txt = resp.text or ""
        if any(x in txt.lower()[:5000] for x in ["captcha", "access denied", "verify you are a human", "cloudflare"]):
            out["debug"] = "blocked_or_captcha"
            return out
        soup = BeautifulSoup(txt, "lxml")
        img = soup.select_one("meta[property='og:image']")
        if img and img.get("content"):
            out["image_url"] = img.get("content", "")
        price_map = {}
        for tr in soup.find_all("tr"):
            cells = tr.find_all(["td", "th"])
            if len(cells) < 2:
                continue
            label = re.sub(r"\s+", " ", cells[0].get_text(" ", strip=True)).strip()
            if not label:
                continue
            price_val = 0.0
            for c in cells[1:]:
                price_val = _parse_money(c.get_text(" ", strip=True))
                if price_val > 0:
                    break
            if price_val > 0:
                price_map[label] = price_val
        out["raw"] = _pick(price_map, ["Ungraded", "Raw"])
        out["psa9"] = _pick(price_map, ["PSA 9", "Grade 9"])
        out["psa10"] = _pick(price_map, ["PSA 10", "Grade 10"])
        if max(out["raw"], out["psa9"], out["psa10"]) > 0:
            out["debug"] = "success"
        else:
            out["debug"] = "prices_not_found"
        return out
    except Exception as exc:
        out["debug"] = f"error: {str(exc)[:80]}"
        return out


def price_for_inventory_row(row: pd.Series, prices: dict) -> float:
    product_type = clean_text(row.get("product_type")).lower()
    grade = clean_text(row.get("grade"))
    company = clean_text(row.get("grading_company")).upper()
    if "graded" in product_type or company or grade:
        if grade == "10":
            return float(prices.get("psa10") or prices.get("raw") or 0.0)
        if grade == "9":
            return float(prices.get("psa9") or prices.get("raw") or 0.0)
    return float(prices.get("raw") or 0.0)


def refresh_market_prices(inventory: pd.DataFrame, limit: int | None = None, include_grading: bool = True) -> tuple[int, pd.DataFrame]:
    if inventory.empty:
        return 0, pd.DataFrame()
    statuses = [STATUS_ACTIVE, STATUS_GRADING] if include_grading else [STATUS_ACTIVE]
    work = inventory[inventory["inventory_status"].isin(statuses)].copy()
    work = work[work["reference_link"].astype(str).str.strip().ne("")].copy()
    if limit:
        work = work.head(int(limit)).copy()
    updates = {}
    audit_rows = []
    for _, r in work.iterrows():
        prices = fetch_market_prices(r.get("reference_link"))
        market = price_for_inventory_row(r, prices)
        inv_id = clean_text(r.get("inventory_id"))
        if inv_id and market > 0:
            updates[inv_id] = {
                "market_price": round(market, 2),
                "market_value": round(market, 2),
                "market_price_updated_at": now_iso(),
                "market_price_debug": prices.get("debug", ""),
                "image_url": clean_text(r.get("image_url")) or prices.get("image_url", ""),
            }
        audit_rows.append({
            "inventory_id": inv_id,
            "card_name": r.get("card_name", ""),
            "market_price": market,
            "debug": prices.get("debug", ""),
        })
        time.sleep(0.25)
    changed = update_rows_by_key(get_ws_name("inventory_worksheet", "inventory"), INVENTORY_COLUMNS, "inventory_id", updates)
    return changed, pd.DataFrame(audit_rows)
