"""Grading prospects: GemRate populations and eBay sold comps."""

from __future__ import annotations

import re
import time
from typing import Any, Callable

import pandas as pd
import requests

from .cleaning import clean_text, to_money


GEMRATE_API_ROOT = "https://api.gemrate.com/v1"
EBAY_SOLD_ACTOR_ID = "memo23/apify-ebay-search-cheerio"

CATEGORIES = ["Pokemon", "Baseball", "Basketball", "Football", "Hockey", "Soccer"]
POKEMON_CATEGORY_ID = "183454"
SPORTS_CATEGORY_ID = "212"

MIN_PSA_POPULATION = 50
GEMRATE_MATCH_LIMIT = 100
GEMRATE_PAGE_LIMIT = 10
SHORTLIST_LIMIT = 50
MIN_PSA10_SALES = 20
MIN_RAW_SALES = 5
LAST_SALE_COUNT = 5
# Pull extra rows so title filters can still leave 20 real PSA 10 comps.
MAX_SOLD_RESULTS = 60

NAME_STOPWORDS = {"the", "a", "an", "of", "and", "card", "cards"}
LOT_PATTERNS = (
    r"\blots?\b",
    r"\bbundles?\b",
    r"\bbreaks?\b",
    r"\bbox(?:es)?\b",
    r"\bpacks?\b",
    r"\bcases?\b",
    r"\bdigital\b",
    r"\bcustom\b",
    r"\bproxy\b",
    r"\breprints?\b",
    r"\byou pick\b",
    r"\bchoose your\b",
)
OTHER_GRADER_RE = re.compile(r"\b(?:bgs|cgc|sgc|beckett)\b", re.IGNORECASE)
PSA10_RE = re.compile(r"\bpsa\s*-?\s*10\b|\bpsa10\b", re.IGNORECASE)
OTHER_PSA_GRADE_RE = re.compile(
    r"\bpsa\s*-?\s*(?:[1-9](?:\.5)?|9\.5)\b",
    re.IGNORECASE,
)
PLAYED_RE = re.compile(
    r"\b(?:lightly played|moderately played|heavily played|damaged|played|lp|mp|hp|dmg)\b",
    re.IGNORECASE,
)
NEAR_MINT_RE = re.compile(r"\bnear mint\b|\bnm\b", re.IGNORECASE)
NM_MT_RE = re.compile(r"\bnm\s*-?\s*mt\b", re.IGNORECASE)

ActorRunner = Callable[[list[str], str], list[dict]]


def normalize_text(value: Any) -> str:
    text = clean_text(value).lower()
    text = text.replace("é", "e")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def normalize_number(value: Any) -> str:
    text = normalize_text(value).replace(" ", "")
    match = re.fullmatch(r"([a-z]*)0*(\d+)([a-z]*)", text)
    if not match:
        return text
    return f"{match.group(1)}{match.group(2)}{match.group(3)}"


def category_id_for(category: str) -> str:
    if normalize_text(category) == "pokemon":
        return POKEMON_CATEGORY_ID
    return SPORTS_CATEGORY_ID


def confidence_label(sale_count: int) -> str:
    count = int(sale_count or 0)
    if count >= 100:
        return "Strong"
    if count >= 40:
        return "Good"
    if count >= 20:
        return "Fair"
    return ""


def qualifies(psa10_sales: int, raw_sales: int) -> bool:
    return int(psa10_sales or 0) >= MIN_PSA10_SALES and int(raw_sales or 0) >= MIN_RAW_SALES


def prospect_margins(psa10_price: float, raw_price: float, grading_fee: float) -> tuple[float, float]:
    """Return dollar margin and percent margin.

    Percent margin is dollar margin divided by raw price plus the grading fee,
    expressed on a 0-100 scale.
    """
    dollars = round(float(psa10_price) - float(raw_price) - float(grading_fee), 2)
    denominator = float(raw_price) + float(grading_fee)
    percent = round((dollars / denominator) * 100.0, 2) if denominator else 0.0
    return dollars, percent


def apply_grading_fee(prospects: list[dict], grading_fee: float) -> list[dict]:
    priced: list[dict] = []
    fee = round(float(grading_fee or 0.0), 2)
    for row in prospects:
        dollars, percent = prospect_margins(row.get("psa10_price") or 0.0, row.get("raw_price") or 0.0, fee)
        updated = dict(row)
        updated["grading_fee"] = fee
        updated["margin_dollars"] = dollars
        updated["margin_pct"] = percent
        priced.append(updated)
    return priced


def _spaced(title: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", clean_text(title).lower())).strip()


def _name_words(card_name: str) -> list[str]:
    words = [word for word in normalize_text(card_name).split() if word not in NAME_STOPWORDS and len(word) > 1]
    return words


def _number_in_title(title: str, card_number: str) -> bool:
    key = normalize_number(card_number)
    if not key:
        return False
    match = re.fullmatch(r"([a-z]*)(\d+)([a-z]*)", key)
    haystack = _spaced(title)
    if not match:
        return f" {key} " in f" {haystack} "
    prefix, digits, suffix = match.groups()
    pattern = rf"(?<![a-z0-9]){re.escape(prefix)}0*{re.escape(digits)}{re.escape(suffix)}(?![a-z0-9])"
    return re.search(pattern, haystack) is not None


def _words_in_title(title: str, phrase: str) -> bool:
    words = [word for word in normalize_text(phrase).split() if len(word) > 1]
    if not words:
        return True
    haystack = f" {_spaced(title)} "
    return all(f" {word} " in haystack for word in words)


def _is_lot(title: str) -> bool:
    return any(re.search(pattern, title, re.IGNORECASE) for pattern in LOT_PATTERNS)


def _is_near_mint(title: str) -> bool:
    if NM_MT_RE.search(title):
        return False
    return NEAR_MINT_RE.search(title) is not None


def title_matches(title: str, card: dict, kind: str) -> bool:
    """True when a sold title is this card, at the requested grade or raw condition."""
    if not clean_text(title):
        return False
    if not _words_in_title(title, card.get("card_name", "")):
        return False
    if not _number_in_title(title, card.get("card_number", "")):
        return False
    parallel = normalize_text(card.get("parallel"))
    if parallel and parallel not in {"base", "base set"} and not _words_in_title(title, card.get("parallel", "")):
        return False
    if _is_lot(title):
        return False

    if kind == "psa10":
        if not PSA10_RE.search(title):
            return False
        if re.search(r"\bnot\s+psa\b", title, re.IGNORECASE):
            return False
        if OTHER_GRADER_RE.search(title):
            return False
        if OTHER_PSA_GRADE_RE.search(title):
            return False
        return True

    if OTHER_GRADER_RE.search(title) or PSA10_RE.search(title) or re.search(r"\b(?:psa|graded)\b", title, re.IGNORECASE):
        return False
    if PLAYED_RE.search(title):
        return False
    if normalize_text(card.get("category")) == "pokemon" and not _is_near_mint(title):
        return False
    return True


def build_search_keyword(card: dict, kind: str) -> str:
    parts = [
        clean_text(card.get("year")),
        clean_text(card.get("set_name")),
        clean_text(card.get("card_name")),
        clean_text(card.get("card_number")),
        clean_text(card.get("parallel")),
    ]
    if kind == "psa10":
        parts.extend(["PSA", "10"])
    keyword = re.sub(r"\s+", " ", " ".join(part for part in parts if part)).strip()
    return keyword[:180].strip()


def average_last_sales(sales: list[dict], count: int = LAST_SALE_COUNT) -> tuple[float | None, int]:
    """Average the most recent `count` prices. The returned count is every match."""
    usable = [sale for sale in sales if to_money(sale.get("price")) > 0]
    if not usable:
        return None, 0

    def sort_key(sale: dict) -> tuple[int, float]:
        sold_at = sale.get("sold_at")
        if sold_at is None or pd.isna(sold_at):
            return (1, 0.0)
        stamp = pd.Timestamp(sold_at)
        if pd.isna(stamp):
            return (1, 0.0)
        return (0, -float(stamp.timestamp()))

    ordered = sorted(usable, key=sort_key)
    recent = ordered[:count]
    average = round(sum(to_money(sale.get("price")) for sale in recent) / len(recent), 2)
    return average, len(usable)


def parse_sold_date(value: Any) -> pd.Timestamp | None:
    text = clean_text(value)
    if not text:
        return None
    text = re.sub(r"(?i)^sold\s+", "", text).strip()
    parsed = pd.to_datetime(text, errors="coerce")
    if pd.isna(parsed):
        return None
    return pd.Timestamp(parsed)


def listing_price(item: dict) -> float:
    if item.get("priceIsAskingPrice") is True:
        return 0.0
    display = clean_text(item.get("price"))
    if " to " in display.lower():
        return 0.0
    currency = clean_text(item.get("currency")).upper()
    if currency and currency != "USD":
        return 0.0
    amount = item.get("priceValue")
    if amount is None or clean_text(amount) == "":
        amount = display
    return to_money(amount)


def is_sold_listing(item: dict) -> bool:
    if not isinstance(item, dict):
        return False
    if item.get("_searchMeta") or item.get("_analytics"):
        return False
    marker = clean_text(item.get("type"))
    if marker in {"no-exact-sold-matches", "category-page", "sold-price-summary"}:
        return False
    if item.get("sold") is False:
        return False
    if not clean_text(item.get("title")):
        return False
    return item.get("sold") is True or bool(clean_text(item.get("soldDate")))


def matching_sales(items: list[dict], card: dict, kind: str) -> list[dict]:
    keyword = normalize_text(card.get("queries", {}).get(kind) or build_search_keyword(card, kind))
    seen: set[str] = set()
    sales: list[dict] = []
    for item in items:
        if not is_sold_listing(item):
            continue
        if normalize_text(item.get("searchQuery")) != keyword:
            continue
        if not title_matches(item.get("title", ""), card, kind):
            continue
        item_id = clean_text(item.get("itemId") or item.get("item_id") or item.get("url"))
        if item_id and item_id in seen:
            continue
        price = listing_price(item)
        if price <= 0:
            continue
        if item_id:
            seen.add(item_id)
        sales.append({"price": price, "sold_at": parse_sold_date(item.get("soldDate")), "title": item.get("title", "")})
    return sales


def queries_without_matches(cards: list[dict], items: list[dict]) -> list[str]:
    """Search keywords whose category results produced no usable comps."""
    missing: list[str] = []
    for card in cards:
        for kind in ("psa10", "raw"):
            keyword = clean_text(card.get("queries", {}).get(kind))
            if keyword and not matching_sales(items, card, kind):
                missing.append(keyword)
    return missing


def _category_matches(card_category: str, selected: str) -> bool:
    left = normalize_text(card_category)
    right = normalize_text(selected)
    if not left or not right:
        return False
    return left == right or right in left or left in right


def _set_matches(card_set: str, description: str, selected_set: str) -> bool:
    wanted = normalize_text(selected_set)
    if not wanted:
        return False
    observed = normalize_text(card_set)
    if observed:
        return wanted == observed or wanted in observed or observed in wanted
    return wanted in normalize_text(description)


def _year_matches(card_year: str, description: str, selected_year: str) -> bool:
    wanted = normalize_text(selected_year)
    if not wanted:
        return True
    observed = normalize_text(card_year)
    if observed:
        return observed == wanted
    return wanted in normalize_text(description)


def card_matches_query(card: dict, category: str, set_name: str, year: str = "") -> bool:
    parsed = card.get("parsed_description") or {}
    description = clean_text(card.get("description"))
    observed_category = clean_text(parsed.get("category")) or description
    observed_set = clean_text(parsed.get("set_name"))
    observed_year = clean_text(parsed.get("year"))
    return (
        _category_matches(observed_category, category)
        and _set_matches(observed_set, description, set_name)
        and _year_matches(observed_year, description, year)
    )


def _unwrap(payload: Any) -> Any:
    if isinstance(payload, dict) and "data" in payload:
        return payload.get("data")
    return payload


def gemrate_get(api_key: str, path: str, params: dict | None = None, attempts: int = 3) -> dict:
    key = clean_text(api_key)
    if not key:
        raise RuntimeError("Add a GemRate API key under [gemrate] api_key in Streamlit secrets.")
    url = f"{GEMRATE_API_ROOT}{path}"
    headers = {"x-api-key": key, "Accept": "application/json", "User-Agent": "CardTracker/2.0"}
    last_error = "GemRate request failed."
    for attempt in range(attempts):
        response = requests.get(url, params=params or {}, headers=headers, timeout=30)
        if response.status_code == 429 and attempt + 1 < attempts:
            time.sleep(int(response.headers.get("Retry-After", "2") or 2))
            continue
        if response.status_code == 401:
            raise RuntimeError("GemRate rejected the API key. Check [gemrate] api_key in Streamlit secrets.")
        if response.status_code >= 400:
            last_error = f"GemRate error {response.status_code}: {response.text[:300]}"
            break
        body = response.json() if response.text else {}
        return body if isinstance(body, dict) else {}
    raise RuntimeError(last_error)


def search_gemrate_cards(api_key: str, category: str, set_name: str, year: str = "") -> list[dict]:
    query = " ".join(part for part in [clean_text(year), clean_text(set_name), clean_text(category)] if part)
    if not clean_text(set_name):
        raise RuntimeError("Enter a set name.")
    matches: list[dict] = []
    seen: set[str] = set()
    for page in range(1, GEMRATE_PAGE_LIMIT + 1):
        payload = gemrate_get(api_key, "/cards/search/simple", {"query": query[:200], "page": page})
        data = _unwrap(payload) or {}
        results = data.get("results") or []
        for result in results:
            if not isinstance(result, dict):
                continue
            if not card_matches_query(result, category, set_name, year):
                continue
            gemrate_id = clean_text(result.get("gemrate_id"))
            if gemrate_id and gemrate_id in seen:
                continue
            if gemrate_id:
                seen.add(gemrate_id)
            matches.append(result)
            if len(matches) >= GEMRATE_MATCH_LIMIT:
                return matches
        if not data.get("has_more"):
            break
    return matches


def _psa_block(payload: dict) -> dict:
    data = _unwrap(payload) or {}
    population = data.get("population") or {}
    population_data = population.get("population_data") or data.get("population_data") or {}
    by_grader = population_data.get("by_grader") or {}
    psa = by_grader.get("psa") or {}
    return psa if isinstance(psa, dict) else {}


def fetch_psa_population(api_key: str, gemrate_id: str) -> dict:
    payload = gemrate_get(api_key, f"/cards/{gemrate_id}/population", {"grader": "psa"})
    psa = _psa_block(payload)
    total = int(to_money(psa.get("total")))
    gem_rate = psa.get("gem_rate")
    try:
        rate = float(gem_rate or 0.0)
    except (TypeError, ValueError):
        rate = 0.0
    if rate > 1:
        rate = rate / 100.0
    return {
        "psa_population": total,
        "psa_gem_total": int(to_money(psa.get("gem_total"))),
        "psa_gem_rate": rate,
    }


def population_record(result: dict, population: dict) -> dict:
    parsed = result.get("parsed_description") or {}
    return {
        "gemrate_id": clean_text(result.get("gemrate_id")),
        "description": clean_text(result.get("description")),
        "gemrate_url": clean_text(result.get("gemrate_url")),
        "category": clean_text(parsed.get("category")),
        "year": clean_text(parsed.get("year")),
        "set_name": clean_text(parsed.get("set_name")),
        "card_name": clean_text(parsed.get("name")) or clean_text(result.get("description")),
        "card_number": clean_text(parsed.get("card_number")),
        "parallel": clean_text(parsed.get("parallel")),
        "psa_population": int(population.get("psa_population") or 0),
        "psa_gem_total": int(population.get("psa_gem_total") or 0),
        "psa_gem_rate": float(population.get("psa_gem_rate") or 0.0),
    }


def shortlist_cards(records: list[dict], limit: int = SHORTLIST_LIMIT) -> list[dict]:
    eligible = [
        record
        for record in records
        if int(record.get("psa_population") or 0) >= MIN_PSA_POPULATION
        and clean_text(record.get("card_name"))
        and clean_text(record.get("card_number"))
    ]
    eligible.sort(key=lambda record: (float(record.get("psa_gem_rate") or 0.0), int(record.get("psa_population") or 0)), reverse=True)
    shortlist = []
    for record in eligible[:limit]:
        card = dict(record)
        card["queries"] = {
            "psa10": build_search_keyword(card, "psa10"),
            "raw": build_search_keyword(card, "raw"),
        }
        shortlist.append(card)
    return shortlist


def run_ebay_sold_actor(api_token: str, actor_id: str, keywords: list[str], category_id: str = "") -> list[dict]:
    token = clean_text(api_token)
    actor = clean_text(actor_id) or EBAY_SOLD_ACTOR_ID
    if not token:
        raise RuntimeError("Add an Apify API token under [apify] api_token in Streamlit secrets.")
    if not keywords:
        return []
    try:
        from apify_client import ApifyClient
    except ImportError as exc:
        raise RuntimeError("The apify-client package is not installed.") from exc

    run_input: dict[str, Any] = {
        "searchQueries": keywords,
        "mode": "sold",
        "marketplace": "ebay.com",
        "detailedItems": False,
        "includeLegacyFields": False,
        "includeSummary": False,
        "titleMustMatchQuery": False,
        "maxItems": MAX_SOLD_RESULTS,
    }
    if clean_text(category_id):
        run_input["categoryId"] = clean_text(category_id)

    client = ApifyClient(token)
    run = client.actor(actor).call(run_input=run_input, timeout_secs=3600)
    if not run:
        raise RuntimeError("The eBay sold actor did not return a run record.")
    dataset_id = run.get("defaultDatasetId") or run.get("default_dataset_id")
    if not dataset_id:
        raise RuntimeError("The eBay sold actor did not return a dataset.")
    return [item for item in client.dataset(dataset_id).iterate_items() if isinstance(item, dict)]


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        text = clean_text(value)
        key = normalize_text(text)
        if not key or key in seen:
            continue
        seen.add(key)
        ordered.append(text)
    return ordered


def collect_sold_items(cards: list[dict], run_actor: ActorRunner, category_id: str) -> list[dict]:
    keywords = _unique([card["queries"][kind] for card in cards for kind in ("psa10", "raw")])
    if not keywords:
        return []
    items = list(run_actor(keywords, category_id))
    fallback = queries_without_matches(cards, items)
    if fallback and clean_text(category_id):
        items.extend(run_actor(_unique(fallback), ""))
    return items


def score_shortlist(cards: list[dict], items: list[dict]) -> list[dict]:
    prospects: list[dict] = []
    for card in cards:
        psa_average, psa_count = average_last_sales(matching_sales(items, card, "psa10"))
        raw_average, raw_count = average_last_sales(matching_sales(items, card, "raw"))
        if psa_average is None or raw_average is None or not qualifies(psa_count, raw_count):
            continue
        prospect = {key: value for key, value in card.items() if key != "queries"}
        prospect.update(
            {
                "psa10_price": psa_average,
                "psa10_sales": psa_count,
                "raw_price": raw_average,
                "raw_sales": raw_count,
                "confidence": confidence_label(psa_count),
            }
        )
        prospects.append(prospect)
    return prospects


def load_populations(api_key: str, matches: list[dict]) -> list[dict]:
    records: list[dict] = []
    for match in matches:
        gemrate_id = clean_text(match.get("gemrate_id"))
        if not gemrate_id:
            continue
        try:
            population = fetch_psa_population(api_key, gemrate_id)
        except RuntimeError as exc:
            message = str(exc).lower()
            if "api key" in message or "rejected" in message:
                raise
            continue
        records.append(population_record(match, population))
    return records


def find_prospects(
    api_key: str,
    apify_token: str,
    category: str,
    set_name: str,
    year: str = "",
    actor_id: str = EBAY_SOLD_ACTOR_ID,
    run_actor: ActorRunner | None = None,
) -> dict:
    """Search one set and return cards that clear the sale-count rules."""
    matches = search_gemrate_cards(api_key, category, set_name, year)
    records = load_populations(api_key, matches)
    shortlist = shortlist_cards(records)
    items: list[dict] = []
    if shortlist:
        if run_actor is None:
            def run_actor(keywords: list[str], category_id: str) -> list[dict]:
                return run_ebay_sold_actor(apify_token, actor_id, keywords, category_id)

        items = collect_sold_items(shortlist, run_actor, category_id_for(category))
    prospects = score_shortlist(shortlist, items)
    return {
        "prospects": prospects,
        "gemrate_matches": len(matches),
        "populations_checked": len(records),
        "shortlisted": len(shortlist),
        "qualified": len(prospects),
    }


def _is_graded_inventory(row: pd.Series) -> bool:
    product_type = clean_text(row.get("product_type")).lower()
    if "graded" in product_type:
        return True
    if clean_text(row.get("grade")) or clean_text(row.get("grading_company")):
        return True
    condition = clean_text(row.get("condition")).lower()
    return condition == "graded"


def _names_match(left: str, right: str) -> bool:
    a = normalize_text(left)
    b = normalize_text(right)
    if not a or not b:
        return False
    return a == b or a in b or b in a


def _sets_match(left: str, right: str) -> bool:
    a = normalize_text(left)
    b = normalize_text(right)
    if not a or not b:
        return False
    return a == b or a in b or b in a


def _types_match(category: str, card_type: str) -> bool:
    observed = normalize_text(card_type)
    if not observed:
        return True
    if normalize_text(category) == "pokemon":
        return "pokemon" in observed
    return "sport" in observed or observed in normalize_text(category)


def inventory_owns_card(card: dict, inventory: pd.DataFrame) -> bool:
    if inventory is None or inventory.empty:
        return False
    for _, row in inventory.iterrows():
        if _is_graded_inventory(row):
            continue
        if not _types_match(card.get("category", ""), row.get("card_type", "")):
            continue
        if normalize_number(card.get("card_number")) != normalize_number(row.get("card_number")):
            continue
        if not _names_match(card.get("card_name", ""), row.get("card_name", "")):
            continue
        if not _sets_match(card.get("set_name", ""), row.get("set_name", "")):
            continue
        return True
    return False


def mark_owned(prospects: list[dict], inventory: pd.DataFrame) -> list[dict]:
    marked: list[dict] = []
    for prospect in prospects:
        updated = dict(prospect)
        updated["owned"] = inventory_owns_card(prospect, inventory)
        marked.append(updated)
    return marked
