from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Iterable

import pandas as pd
import streamlit as st
import gspread
from gspread.exceptions import APIError, WorksheetNotFound, SpreadsheetNotFound
from google.oauth2.service_account import Credentials


def _secret(*names: str, default=None):
    for name in names:
        if name in st.secrets:
            return st.secrets[name]
    return default


def extract_spreadsheet_id(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", raw)
    if m:
        return m.group(1)
    raw = raw.replace("https://docs.google.com/spreadsheets/d/", "")
    raw = raw.split("/edit")[0].split("?")[0].split("#")[0]
    return raw.strip()


def _normalize_sa_info(info: dict) -> dict:
    info = dict(info)
    if isinstance(info.get("private_key"), str):
        info["private_key"] = info["private_key"].replace("\\n", "\n")
    return info


def _is_retryable_gspread_error(exc: Exception) -> bool:
    try:
        if isinstance(exc, APIError):
            status = getattr(getattr(exc, "response", None), "status_code", None)
            return status in {429, 500, 502, 503, 504}
    except Exception:
        pass
    msg = str(exc)
    return any(x in msg for x in ["429", "500", "502", "503", "504", "Quota exceeded", "RESOURCE_EXHAUSTED"])


def with_backoff(fn, tries: int = 6, base_sleep: float = 0.75):
    last = None
    for i in range(tries):
        try:
            return fn()
        except Exception as exc:
            last = exc
            if _is_retryable_gspread_error(exc):
                time.sleep(min(base_sleep * (2 ** i), 16))
                continue
            raise
    raise last


def service_account_email() -> str:
    try:
        if "gcp_service_account" not in st.secrets:
            return ""
        sa = st.secrets["gcp_service_account"]
        if isinstance(sa, str):
            return str(json.loads(sa).get("client_email", "")).strip()
        return str(sa.get("client_email", "")).strip()
    except Exception:
        return ""


def stop_with_sheets_error(message: str, exc: Exception | None = None):
    st.error(message)
    with st.expander("Google Sheets troubleshooting", expanded=True):
        st.write("1. Share the Google Sheet with the service account as Editor.")
        st.write("2. Make sure `spreadsheet_id` is only the Sheet ID or a valid Google Sheets URL.")
        st.write("3. Make sure the worksheet tab names match your secrets.")
        email = service_account_email()
        if email:
            st.write("Service account email:")
            st.code(email)
        if exc is not None:
            st.write("Error detail:")
            st.code(str(exc)[:2000])
    st.stop()


@st.cache_resource
def get_gspread_client():
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]

    if "gcp_service_account" in st.secrets and not isinstance(st.secrets["gcp_service_account"], str):
        try:
            sa = st.secrets["gcp_service_account"]
            info = _normalize_sa_info({k: sa[k] for k in sa.keys()})
            creds = Credentials.from_service_account_info(info, scopes=scopes)
            return gspread.authorize(creds)
        except Exception as exc:
            stop_with_sheets_error("Could not load Google service account from Streamlit secrets.", exc)

    if "gcp_service_account" in st.secrets and isinstance(st.secrets["gcp_service_account"], str):
        try:
            info = _normalize_sa_info(json.loads(st.secrets["gcp_service_account"]))
            creds = Credentials.from_service_account_info(info, scopes=scopes)
            return gspread.authorize(creds)
        except Exception as exc:
            stop_with_sheets_error("Could not parse Google service account JSON from Streamlit secrets.", exc)

    if "service_account_json_path" in st.secrets:
        try:
            p = Path(st.secrets["service_account_json_path"])
            if not p.is_absolute():
                p = Path.cwd() / p
            info = _normalize_sa_info(json.loads(p.read_text(encoding="utf-8")))
            creds = Credentials.from_service_account_info(info, scopes=scopes)
            return gspread.authorize(creds)
        except Exception as exc:
            stop_with_sheets_error("Could not load local service account JSON.", exc)

    stop_with_sheets_error('Missing Google credentials. Add `gcp_service_account` or `service_account_json_path`.')


@st.cache_resource
def get_spreadsheet():
    spreadsheet_id = extract_spreadsheet_id(_secret("spreadsheet_id", "GOOGLE_SHEET_ID", "SPREADSHEET_ID", default=""))
    if not spreadsheet_id:
        stop_with_sheets_error('Missing `spreadsheet_id` in Streamlit secrets.')
    client = get_gspread_client()
    try:
        return with_backoff(lambda: client.open_by_key(spreadsheet_id))
    except SpreadsheetNotFound as exc:
        stop_with_sheets_error("Google Sheet not found or service account lacks access.", exc)
    except Exception as exc:
        stop_with_sheets_error("Unexpected error opening Google Sheet.", exc)


def get_ws_name(secret_name: str, default: str) -> str:
    return str(_secret(secret_name, default=default) or default).strip()


def get_or_create_ws(ws_name: str, headers: list[str] | None = None, rows: int = 1000, cols: int = 30):
    sh = get_spreadsheet()
    try:
        ws = with_backoff(lambda: sh.worksheet(ws_name))
    except WorksheetNotFound:
        ws = with_backoff(lambda: sh.add_worksheet(title=ws_name, rows=rows, cols=max(cols, len(headers or []) + 5)))
        if headers:
            with_backoff(lambda: ws.update(values=[headers], range_name="1:1", value_input_option="USER_ENTERED"))
        return ws
    if headers:
        ensure_headers(ws, headers)
    return ws


def _dedupe_headers(headers: Iterable[str]) -> list[str]:
    counts = {}
    out = []
    for i, h in enumerate(headers, start=1):
        base = str(h or "").strip() or f"unnamed__col{i}"
        counts[base] = counts.get(base, 0) + 1
        out.append(base if counts[base] == 1 else f"{base}__dup{counts[base]}")
    return out


def _strip_dup_suffix(h: str) -> str:
    return re.sub(r"__dup\d+$", "", str(h or "").strip())


def ensure_headers(ws, needed_headers: list[str]) -> list[str]:
    existing = with_backoff(lambda: ws.row_values(1))
    if not existing:
        with_backoff(lambda: ws.update(values=[needed_headers], range_name="1:1", value_input_option="USER_ENTERED"))
        return needed_headers
    existing_clean = [str(x or "").strip() for x in existing]
    existing_base = {_strip_dup_suffix(x) for x in existing_clean}
    additions = [h for h in needed_headers if h not in existing_base]
    if additions:
        new_headers = existing_clean + additions
        with_backoff(lambda: ws.update(values=[new_headers], range_name="1:1", value_input_option="USER_ENTERED"))
        return new_headers
    return existing_clean


@st.cache_data(ttl=45, show_spinner=False)
def read_sheet(ws_name: str, headers: tuple[str, ...] | None = None) -> pd.DataFrame:
    ws = get_or_create_ws(ws_name, list(headers) if headers else None)
    values = with_backoff(lambda: ws.get_all_values())
    if not values or not values[0]:
        return pd.DataFrame(columns=list(headers or []))
    raw_header = [str(x or "").strip() for x in values[0]]
    header = _dedupe_headers(raw_header)
    width = len(header)
    rows = []
    for r in values[1:]:
        r = list(r)
        if len(r) < width:
            r = r + [""] * (width - len(r))
        elif len(r) > width:
            r = r[:width]
        if any(str(x).strip() for x in r):
            rows.append(r)
    df = pd.DataFrame(rows, columns=header)
    if headers:
        for h in headers:
            if h not in df.columns:
                df[h] = ""
    return df


def clear_read_cache():
    read_sheet.clear()


def _last_nonempty_row(values: list[list[str]]) -> int:
    """Return the 1-based last row containing any visible value.

    Google Sheets' values.append endpoint appends after the end of a detected
    logical table. That is unsafe for worksheets that can contain blank/gapped
    rows because the detected table can end before the actual last data row.
    We therefore calculate the physical last non-empty row ourselves and write
    new rows to an explicit range below it.
    """
    last_row = 0
    for row_num, row in enumerate(values, start=1):
        if any(str(cell or "").strip() for cell in row):
            last_row = row_num
    return last_row


def append_rows(ws_name: str, headers: list[str], rows: list[dict]):
    """Safely insert new rows below the physical last non-empty worksheet row.

    IMPORTANT: do not replace this with gspread Worksheet.append_rows().
    Google Sheets append operations use logical-table detection. On a worksheet
    with gaps, that can select an insertion point before the actual last data row.
    insert_rows() is used here so existing rows are shifted rather than overwritten.
    """
    if not rows:
        return

    ws = get_or_create_ws(ws_name, headers)
    sheet_headers = ensure_headers(ws, headers)

    if not sheet_headers:
        raise ValueError(f"Worksheet {ws_name!r} has no header row; refusing to add rows.")

    normalized_headers = [str(h or "").strip() for h in sheet_headers]
    duplicate_headers = sorted(
        {h for h in normalized_headers if h and normalized_headers.count(h) > 1}
    )
    if duplicate_headers:
        raise ValueError(
            "Worksheet contains duplicate header name(s): "
            + ", ".join(duplicate_headers)
            + ". Refusing to add rows until the header row is corrected."
        )

    values_to_write = [
        [row.get(_strip_dup_suffix(h), row.get(h, "")) for h in sheet_headers]
        for row in rows
    ]

    existing_values = with_backoff(lambda: ws.get_all_values())
    last_used_row = _last_nonempty_row(existing_values)
    insert_at = max(2, last_used_row + 1)

    # insert_rows changes the grid itself instead of writing into an existing row.
    # If anything exists at/after insert_at, it is shifted down rather than replaced.
    with_backoff(
        lambda: ws.insert_rows(
            values_to_write,
            row=insert_at,
            value_input_option="USER_ENTERED",
            inherit_from_before=False,
        )
    )

    # Verify the exact inserted range now contains at least the expected row count.
    end_row = insert_at + len(values_to_write) - 1
    last_col = gspread.utils.rowcol_to_a1(1, len(sheet_headers)).rstrip("1")
    inserted = with_backoff(
        lambda: ws.get(f"A{insert_at}:{last_col}{end_row}")
    )
    if len(inserted or []) < len(values_to_write):
        raise RuntimeError(
            "Google Sheets did not return all newly inserted rows during verification. "
            "Check the worksheet before retrying."
        )

    clear_read_cache()


def update_rows_by_key(ws_name: str, headers: list[str], key_col: str, updates_by_key: dict[str, dict]):
    """Update existing rows by a unique key, failing closed on ambiguous keys."""
    if not updates_by_key:
        return 0

    clean_updates = {str(k or "").strip(): v for k, v in updates_by_key.items()}
    if "" in clean_updates:
        raise ValueError(f"Blank {key_col} supplied to update_rows_by_key; refusing to update.")

    # Persist every potentially updated column in the worksheet header BEFORE any
    # data row is changed. The prior implementation could extend a data row without
    # first extending row 1, which caused schema drift and truncated reads.
    required_headers = list(headers)
    if key_col not in required_headers:
        required_headers.append(key_col)
    for update in clean_updates.values():
        for col in update.keys():
            if col not in required_headers:
                required_headers.append(col)

    ws = get_or_create_ws(ws_name, required_headers)
    ensure_headers(ws, required_headers)
    values = with_backoff(lambda: ws.get_all_values())
    if not values:
        return 0

    header = [str(x or "").strip() for x in values[0]]
    if header.count(key_col) != 1:
        raise ValueError(
            f"Worksheet must contain exactly one {key_col!r} header; found {header.count(key_col)}. "
            "No rows were updated."
        )

    key_idx = header.index(key_col)

    # Build the live key index and reject duplicate nonblank keys. Updating by a
    # duplicated ID would otherwise modify multiple inventory rows.
    key_to_rows: dict[str, list[int]] = {}
    for row_num, row in enumerate(values[1:], start=2):
        key = str(row[key_idx] if key_idx < len(row) else "").strip()
        if key:
            key_to_rows.setdefault(key, []).append(row_num)

    duplicate_keys = {k: nums for k, nums in key_to_rows.items() if len(nums) > 1}
    if duplicate_keys:
        sample = ", ".join(
            f"{key} (rows {'/'.join(map(str, nums[:5]))})"
            for key, nums in list(duplicate_keys.items())[:10]
        )
        raise ValueError(
            f"Duplicate nonblank {key_col} values exist in worksheet {ws_name!r}: {sample}. "
            "No rows were updated. Fix the duplicates first."
        )

    missing_keys = sorted(k for k in clean_updates if k not in key_to_rows)
    if missing_keys:
        raise ValueError(
            f"{len(missing_keys)} requested {key_col} value(s) are not present in the live worksheet: "
            + ", ".join(missing_keys[:20])
            + ("..." if len(missing_keys) > 20 else "")
            + ". No rows were updated."
        )

    last_col = gspread.utils.rowcol_to_a1(1, len(header)).rstrip("1")
    changed = 0
    for key, update in clean_updates.items():
        row_num = key_to_rows[key][0]
        original = list(values[row_num - 1])
        if len(original) < len(header):
            original += [""] * (len(header) - len(original))
        elif len(original) > len(header):
            original = original[: len(header)]

        new_row = list(original)
        for col, val in update.items():
            if col not in header:
                # Should be impossible because required_headers was persisted above.
                raise RuntimeError(
                    f"Header {col!r} was not persisted before update; no further rows updated."
                )
            new_row[header.index(col)] = val

        with_backoff(
            lambda rn=row_num, rw=new_row: ws.update(
                values=[rw],
                range_name=f"A{rn}:{last_col}{rn}",
                value_input_option="USER_ENTERED",
            )
        )
        changed += 1

    if changed:
        clear_read_cache()
    return changed


def overwrite_sheet(ws_name: str, headers: list[str], df: pd.DataFrame):
    ws = get_or_create_ws(ws_name, headers)
    rows = [headers] + df.reindex(columns=headers).fillna("").astype(str).values.tolist()
    with_backoff(lambda: ws.clear())
    with_backoff(lambda: ws.update(values=rows, range_name="A1", value_input_option="USER_ENTERED"))
    clear_read_cache()
