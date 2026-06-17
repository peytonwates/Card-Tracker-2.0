# Card Tracker 2.0 — Streamlined Rebuild

This is a fresh Streamlit rebuild that keeps your existing Google Sheets database and moves shared logic into `core/` so the pages are easier to maintain.

## Pages

1. Dashboard
2. eBay Store
3. Inventory
4. Grading
5. Expenses
6. Shows

I treated Shows as its own page because the old app already had it as a page and you asked to keep it mostly the same while removing pricing-for-shows. If you truly want exactly five pages, move Shows into Expenses as a tab.

## Install

```powershell
pip install -r requirements.txt
streamlit run app.py
```

## Secrets

Copy `.streamlit/secrets.toml.example` to `.streamlit/secrets.toml` locally, then fill in the values.

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
