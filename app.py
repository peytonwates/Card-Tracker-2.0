import streamlit as st

st.set_page_config(page_title="Card Tracker 2.0", layout="wide")

st.title("Card Tracker 2.0")
st.write("Use the pages in the sidebar to manage the business.")

st.markdown("""
### Rebuild principles

- **Inventory is the source of truth** for status, sale details, cost basis, and market value.
- **Database refresh** and **market value refresh** are separate so normal page loads stay fast.
- **Grading fees sync back to inventory** when you create or update submissions.
- **eBay sync imports orders** into a separate ledger first, then matches to inventory by SKU/inventory ID.
""")
