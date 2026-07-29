"""
Streamlit UI for the link checker.

Run:
    streamlit run link_checker_app.py

Requires link_checker.py alongside this file.
"""

from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from io import BytesIO

import pandas as pd
import streamlit as st

from link_checker import (
    DEFAULT_TIMEOUT,
    DEFAULT_WORKERS,
    build_session,
    check_one,
)

# ─── page setup ───────────────────────────────────────────────────────────────
st.set_page_config(page_title="Link Checker", page_icon="🔗", layout="wide")
st.title("🔗 Link Checker")
st.caption(
    "Upload a spreadsheet, pick the URL column, and check every link. "
    "Handy for LibGuides asset exports, resource lists, or any xlsx/csv with URLs."
)

# Category order used everywhere (metrics, filters, sort)
CATEGORY_ORDER = [
    "OK", "Redirect", "Broken", "Server Error",
    "Connection", "Timeout", "SSL",
    "Proxy Blocked", "Invalid URL", "Other",
]
PROBLEM_CATEGORIES = [
    "Broken", "Server Error", "Connection", "Timeout", "SSL", "Other",
]


# ─── helpers ──────────────────────────────────────────────────────────────────
def read_table(uploaded_file) -> pd.DataFrame:
    name = uploaded_file.name.lower()
    if name.endswith(".csv"):
        return pd.read_csv(uploaded_file)
    return pd.read_excel(uploaded_file)


def to_excel_bytes(df: pd.DataFrame) -> bytes:
    buf = BytesIO()
    df.to_excel(buf, index=False, engine="openpyxl")
    return buf.getvalue()


def count_valid_urls(series: pd.Series) -> int:
    return int(series.apply(lambda u: isinstance(u, str) and bool(u.strip())).sum())


# ─── upload ──────────────────────────────────────────────────────────────────
uploaded = st.file_uploader(
    "Spreadsheet with a URL column",
    type=["xlsx", "xls", "csv"],
    help="First sheet is used for xlsx/xls files.",
)

if uploaded is None:
    st.info("Upload a file to begin.")
    st.stop()

# Detect a new upload by hashing bytes, and clear stale results if the file changed.
file_hash = hashlib.md5(uploaded.getvalue()).hexdigest()
if st.session_state.get("file_hash") != file_hash:
    st.session_state.file_hash = file_hash
    st.session_state.input_df = read_table(uploaded)
    st.session_state.input_name = uploaded.name
    st.session_state.pop("results_df", None)

df: pd.DataFrame = st.session_state.input_df

# ─── settings (sidebar) ──────────────────────────────────────────────────────
with st.sidebar:
    st.header("Settings")

    default_idx = list(df.columns).index("URL") if "URL" in df.columns else 0
    url_col = st.selectbox("URL column", list(df.columns), index=default_idx)

    workers = st.slider(
        "Concurrent workers", 1, 30, DEFAULT_WORKERS,
        help="More workers = faster, but heavier on the target servers. "
             "For a single vendor (e.g. a Primo permalink host) keep this modest.",
    )
    timeout = st.slider(
        "Timeout (seconds)", 5, 60, DEFAULT_TIMEOUT,
        help="Per-request timeout. Slow vendor sites may need 30+.",
    )

    n_valid = count_valid_urls(df[url_col])
    st.metric("URLs to check", n_valid, delta=f"of {len(df)} rows", delta_color="off")

    run_check = st.button(
        "Check links", type="primary", use_container_width=True,
        disabled=(n_valid == 0),
    )

    st.divider()
    st.caption(
        "Method: HEAD first, GET fallback on 403/405/5xx. Follows redirects. "
        "Uses a browser-like User-Agent so vendor proxies don't block. "
        "An `x-deny-reason` response header is treated as `Proxy Blocked`, "
        "not as a broken link."
    )

# ─── input preview ───────────────────────────────────────────────────────────
st.subheader(st.session_state.input_name)
st.caption(f"{len(df):,} rows · {n_valid:,} URLs in `{url_col}`")
with st.expander("Preview first 10 rows", expanded=False):
    st.dataframe(df.head(10), use_container_width=True, hide_index=True)

# ─── run check ───────────────────────────────────────────────────────────────
if run_check:
    urls = df[url_col].tolist()
    results = [None] * len(urls)
    session = build_session()

    progress = st.progress(0.0, text="Starting…")
    started = datetime.now()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(check_one, session, u, timeout): i
                   for i, u in enumerate(urls)}
        done = 0
        for fut in as_completed(futures):
            idx = futures[fut]
            results[idx] = fut.result()
            done += 1
            progress.progress(done / len(urls), text=f"Checked {done:,}/{len(urls):,}")

    progress.empty()
    elapsed = (datetime.now() - started).total_seconds()

    out = df.copy()
    out["Status Code"] = [r.status_code for r in results]
    out["Status Category"] = [r.category for r in results]
    out["Final URL"] = [r.final_url for r in results]
    out["Error Detail"] = [r.error_detail for r in results]
    out["Checked At"] = started.strftime("%Y-%m-%d %H:%M:%S")

    st.session_state.results_df = out
    st.session_state.results_elapsed = elapsed
    st.success(f"Checked {len(urls):,} URLs in {elapsed:.1f}s")

# ─── results ─────────────────────────────────────────────────────────────────
if "results_df" in st.session_state:
    out: pd.DataFrame = st.session_state.results_df

    st.divider()
    st.subheader("Results")

    # Metrics — one per category actually present, in the canonical order.
    counts = out["Status Category"].value_counts()
    present = [c for c in CATEGORY_ORDER if c in counts.index]
    if present:
        cols = st.columns(len(present))
        for col, cat in zip(cols, present):
            col.metric(cat, int(counts[cat]))

    # Filter
    default_filter = [c for c in PROBLEM_CATEGORIES if c in present] or present
    selected = st.multiselect(
        "Show categories",
        options=present,
        default=default_filter,
        help="Defaults to problem categories. Add 'OK' to see everything.",
    )
    view = out[out["Status Category"].isin(selected)] if selected else out.iloc[0:0]

    st.caption(f"Showing {len(view):,} of {len(out):,} rows")
    st.dataframe(
        view,
        use_container_width=True,
        hide_index=True,
        column_config={
            url_col: st.column_config.LinkColumn(url_col, display_text=r"https?://(.+)"),
            "Final URL": st.column_config.LinkColumn("Final URL"),
        },
    )

    # Downloads — full results and problems-only
    stem = st.session_state.input_name.rsplit(".", 1)[0]
    dl_cols = st.columns(2)
    dl_cols[0].download_button(
        "⬇︎ Download full checked xlsx",
        data=to_excel_bytes(out),
        file_name=f"{stem}_checked.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True,
    )
    problems = out[out["Status Category"].isin(PROBLEM_CATEGORIES)]
    dl_cols[1].download_button(
        f"⬇︎ Download problems only ({len(problems):,})",
        data=to_excel_bytes(problems),
        file_name=f"{stem}_problems.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True,
        disabled=(len(problems) == 0),
    )
