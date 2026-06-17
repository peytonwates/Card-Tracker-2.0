from __future__ import annotations

STATUS_ACTIVE = "ACTIVE"
STATUS_LISTED = "LISTED"
STATUS_SOLD = "SOLD"
STATUS_TRADED = "TRADED"
STATUS_GRADING = "GRADING"
STATUS_RETURNED = "RETURNED"

PRODUCT_TYPE_OPTIONS = ["Card", "Sealed", "Graded Card"]
CARD_TYPE_OPTIONS = ["Pokemon", "Sports"]
INVENTORY_TYPE_OPTIONS = ["Show Inventory", "Personal Inventory"]
CONDITION_OPTIONS = ["Near Mint", "Lightly Played", "Moderately Played", "Heavily Played", "Damaged", "Sealed", "Graded"]
GRADING_COMPANIES = ["PSA", "CGC", "Beckett"]

EXPENSE_CATEGORIES = [
    "Packaging materials",
    "Shipping supplies",
    "Card show fees",
    "Taxes - Sales Tax",
    "Supplies",
    "Equipment / Displays",
    "Travel / Food",
    "Subscriptions / Software",
    "Payment / Platform Fees",
    "Other",
]

INVENTORY_COLUMNS = [
    "inventory_id", "image_url", "inventory_type", "product_type", "inventory_status",
    "sealed_product_type", "card_type", "brand_or_league", "set_name", "year",
    "card_name", "card_number", "variant", "card_subtype", "grading_company", "grade",
    "reference_link", "purchase_date", "purchased_from", "purchase_price", "shipping", "tax",
    "total_price", "grading_fee", "total_cost", "condition", "notes", "created_at",
    "listed_transaction_id", "market_price", "market_value", "market_price_updated_at", "market_price_debug",
    "transaction_type", "platform", "list_date", "list_price", "sold_date", "sold_price",
    "fees", "fees_total", "shipping_charged", "net_proceeds", "profit", "sale_channel",
    "sale_notes", "show_id", "show_name", "sold_transaction_id", "sold_created_at", "sold_updated_at",
    "sticker_price", "break_id", "break_box_name", "ebay_order_id", "ebay_line_item_id", "ebay_item_id", "ebay_sku",
]

GRADING_COLUMNS = [
    "grading_row_id", "submission_id", "submission_date", "estimated_return_date",
    "inventory_id", "reference_link", "card_name", "card_number", "variant", "card_subtype",
    "purchased_from", "purchase_date", "purchase_total", "grading_company",
    "grading_fee_initial", "grading_fee_per_card", "additional_costs", "extra_costs", "total_grading_cost",
    "psa9_price", "psa10_price", "status", "returned_date", "received_grade", "notes",
    "created_at", "updated_at", "synced_to_inventory",
]

EXPENSE_COLUMNS = ["misc_id", "expense_date", "category", "description", "amount", "notes", "created_at"]
MILEAGE_COLUMNS = ["mileage_id", "trip_date", "show_name", "business_purpose", "start_location", "end_location", "round_trip", "miles", "parking_tolls", "notes", "created_at"]
SHOW_COLUMNS = ["show_id", "show_name", "show_date", "location", "description", "status", "created_at", "updated_at"]

EBAY_ORDER_COLUMNS = [
    "ebay_order_id", "ebay_line_item_id", "legacy_item_id", "sku", "inventory_id", "title",
    "order_created_at", "sold_date", "quantity", "sold_price", "shipping_charged", "tax",
    "gross_paid", "fees_total", "net_proceeds", "currency", "order_status", "fulfillment_status",
    "matched_to_inventory", "sync_status", "raw_order_json", "created_at", "updated_at",
]

EBAY_LISTING_COLUMNS = [
    "sku", "inventory_id", "title", "condition", "availability", "quantity", "offer_id",
    "listing_id", "listing_status", "price", "currency", "marketplace_id", "last_synced_at", "raw_json",
]

HEADER_ALIASES = {
    "inventory_id": ["inventory_id", "Inventory ID", "inv_id"],
    "image_url": ["image_url", "Image URL", "image", "Image"],
    "inventory_type": ["inventory_type", "Inventory Type"],
    "product_type": ["product_type", "Product Type"],
    "sealed_product_type": ["sealed_product_type", "Sealed Product Type"],
    "inventory_status": ["inventory_status", "Inventory Status", "inventoryStatus"],
    "brand_or_league": ["brand_or_league", "Brand/League", "Brand / League", "Brand or League"],
    "set_name": ["set_name", "Set", "Set Name"],
    "card_name": ["card_name", "Card Name", "Item Name"],
    "card_number": ["card_number", "Card #", "Card Number"],
    "card_subtype": ["card_subtype", "Card Subtype"],
    "grading_company": ["grading_company", "Grading Company", "company"],
    "grade": ["grade", "Grade", "received_grade", "returned_grade"],
    "reference_link": ["reference_link", "Reference Link", "Reference link"],
    "purchase_date": ["purchase_date", "Purchase Date"],
    "purchased_from": ["purchased_from", "Purchased From", "Purchased from"],
    "purchase_price": ["purchase_price", "Purchase Price"],
    "total_price": ["total_price", "Total Price", "Purchase Total", "purchase_total"],
    "grading_fee": ["grading_fee", "Grading Fee", "grading_fee_total"],
    "total_cost": ["total_cost", "Total Cost", "All In Cost", "all_in_cost"],
    "market_price": ["market_price", "Market Price"],
    "market_value": ["market_value", "Market Value"],
    "market_price_updated_at": ["market_price_updated_at", "Market Price Updated At"],
    "transaction_type": ["transaction_type", "Transaction Type", "listing_type"],
    "list_date": ["list_date", "List Date", "listed_date"],
    "list_price": ["list_price", "List Price", "listed_price"],
    "sold_date": ["sold_date", "Sold Date", "sale_date", "date"],
    "sold_price": ["sold_price", "Sold Price", "sale_price", "sell_price", "price"],
    "fees": ["fees", "Fees", "platform_fees", "fee"],
    "fees_total": ["fees_total", "Fees Total", "total_fees", "total_fee"],
    "shipping_charged": ["shipping_charged", "Shipping Charged", "shipping_cost"],
    "net_proceeds": ["net_proceeds", "Net Proceeds", "net"],
    "profit": ["profit", "Profit", "Profit/Loss", "profit_loss"],
    "sale_channel": ["sale_channel", "Sale Channel", "sales_channel"],
    "sale_notes": ["sale_notes", "Sale Notes"],
    "show_id": ["show_id", "Show ID"],
    "show_name": ["show_name", "Show Name"],
    "show_date": ["show_date", "Show Date"],
    "status": ["status", "Status", "TX Status", "tx_status", "Show Status"],
}

NUMERIC_COLUMNS = {
    "purchase_price", "shipping", "tax", "total_price", "grading_fee", "total_cost",
    "market_price", "market_value", "list_price", "sold_price", "fees", "fees_total",
    "shipping_charged", "net_proceeds", "profit", "sticker_price", "amount", "miles",
    "parking_tolls", "quantity", "gross_paid", "price", "grading_fee_initial", "grading_fee_per_card",
    "additional_costs", "extra_costs", "total_grading_cost", "psa9_price", "psa10_price", "purchase_total",
}
