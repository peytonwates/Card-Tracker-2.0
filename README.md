# Card Tracker 2.0 — Streamlined Rebuild

This is a fresh Streamlit rebuild that keeps your existing Google Sheets database and moves shared logic into `core/` so the pages are easier to maintain.

## Pages

1. Dashboard
2. eBay Store
3. Inventory
4. Grading
5. Expenses
6. Shows
7. Consignment
8. TCGplayer
9. Grading Prospects

Grading Prospects looks up one sport or Pokemon set at a time. It uses the GemRate API for the PSA gem rate and eBay sold listings for the PSA 10 and raw prices. A card stays on the list when eBay still shows at least 20 matching PSA 10 sales and 5 matching raw sales. The table shows at most 50 cards.

I treated Shows as its own page because the old app already had it as a page and you asked to keep it mostly the same while removing pricing-for-shows. If you truly want exactly five pages, move Shows into Expenses as a tab.

## Install

```powershell
pip install -r requirements.txt
streamlit run app.py
```

## Secrets

Copy `.streamlit/secrets.toml.example` to `.streamlit/secrets.toml` locally, then fill in the values.

Grading Prospects needs a GemRate key and the Apify token already used for TCGplayer. The sold-listings actor is stored separately from the TCGplayer actor:

```toml
[gemrate]
api_key = "YOUR_GEMRATE_API_KEY"

[apify]
api_token = "YOUR_APIFY_API_TOKEN"
ebay_sold_actor_id = "memo23/apify-ebay-search-cheerio"
```

## eBay setup

The eBay page uses OAuth refresh-token flow. You need:

- `ebay_client_id`
- `ebay_client_secret`
- `ebay_refresh_token`
- `ebay_marketplace_id = "EBAY_US"`
- `ebay_environment = "production"`

The first version syncs eBay orders into an `ebay_orders` worksheet and can update inventory rows when an order line item SKU equals your `inventory_id`. For existing manual eBay listings that do not have SKU set to your inventory ID, the sync will still import the sale but mark it as unmatched so you can map it.

## Important data model rules

- Inventory is the source of truth for card-level status, cost, market value, and sale details.
- Grading creates rows in `grading` and immediately writes `grading_fee` and `total_cost` back to `inventory`.
- Dashboard reads from Google Sheets only unless you click `Refresh market values`.
- Market value scraping is intentionally separate from normal database refresh because it is slower.
