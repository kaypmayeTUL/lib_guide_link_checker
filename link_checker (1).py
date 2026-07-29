#!/usr/bin/env python3
"""
Link checker for LibGuides asset exports (or any xlsx with a URL column).

For each URL it:
  1. Tries HEAD (fast, no body download)
  2. Falls back to GET when the server rejects HEAD (405/501) or the response
     is suspicious (some proxies return 200 to HEAD but 404 to GET)
  3. Follows redirects and records the final URL
  4. Classifies the result: OK / Redirect / Broken / Server Error / Connection /
     Timeout / SSL / Other

Output is a copy of the input xlsx with these columns appended:
  Status Code, Status Category, Final URL, Error Detail, Checked At

Usage:
    python link_checker.py input.xlsx
    python link_checker.py input.xlsx --output checked.xlsx --url-column URL \
        --workers 10 --timeout 20
"""

from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# A browser-like UA — many sites (including Cloudflare-fronted library
# vendors) refuse the default python-requests UA with a 403.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

DEFAULT_TIMEOUT = 20
DEFAULT_WORKERS = 10
# Statuses where HEAD is unreliable and we should retry with GET.
HEAD_UNRELIABLE = {403, 405, 500, 501, 502, 503, 999}


@dataclass
class CheckResult:
    status_code: str          # "200", "404", or "ERROR"
    category: str             # OK, Redirect, Broken, Server Error, Connection, Timeout, SSL, Other, Invalid URL, Proxy Blocked
    final_url: str            # empty if unchanged from input
    error_detail: str         # exception message or note; empty if none


def _proxy_block_reason(resp: requests.Response) -> Optional[str]:
    """Detect an egress-proxy block so we don't report it as a broken link.
    Sandboxes and many corporate proxies set an x-deny-reason header on
    intercepted responses.
    """
    reason = resp.headers.get("x-deny-reason")
    if reason:
        return f"proxy denied: {reason}"
    return None


def build_session() -> requests.Session:
    """Session with connection pooling and a modest retry policy for transients."""
    s = requests.Session()
    s.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
    })
    # Retry only on transient network-level failures, not on 4xx.
    retry = Retry(
        total=2,
        backoff_factor=0.5,
        status_forcelist=[502, 503, 504],
        allowed_methods=["HEAD", "GET"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=50, pool_maxsize=50)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    return s


def classify(status_code: int) -> str:
    if 200 <= status_code < 300:
        return "OK"
    if 300 <= status_code < 400:
        return "Redirect"
    if 400 <= status_code < 500:
        return "Broken"
    if 500 <= status_code < 600:
        return "Server Error"
    return "Other"


def check_one(session: requests.Session, url: str, timeout: int) -> CheckResult:
    if not isinstance(url, str) or not url.strip():
        return CheckResult("", "Invalid URL", "", "empty or non-string URL")

    url = url.strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return CheckResult("", "Invalid URL", "", f"unsupported scheme or missing host: {url}")

    def _request(method: str) -> requests.Response:
        return session.request(
            method,
            url,
            timeout=timeout,
            allow_redirects=True,
            # stream=True lets GET close the connection before downloading the body
            stream=(method == "GET"),
        )

    try:
        resp = _request("HEAD")
        blocked = _proxy_block_reason(resp)
        if blocked:
            resp.close()
            return CheckResult(str(resp.status_code), "Proxy Blocked", "", blocked)
        if resp.status_code in HEAD_UNRELIABLE:
            resp.close()
            resp = _request("GET")
            blocked = _proxy_block_reason(resp)
            if blocked:
                resp.close()
                return CheckResult(str(resp.status_code), "Proxy Blocked", "", blocked)
        final = resp.url if resp.url != url else ""
        code = resp.status_code
        resp.close()
        return CheckResult(str(code), classify(code), final, "")
    except requests.exceptions.SSLError as e:
        return CheckResult("ERROR", "SSL", "", f"SSL error: {e.__class__.__name__}: {e}")
    except requests.exceptions.ConnectTimeout as e:
        return CheckResult("ERROR", "Timeout", "", f"connect timeout: {e}")
    except requests.exceptions.ReadTimeout as e:
        return CheckResult("ERROR", "Timeout", "", f"read timeout: {e}")
    except requests.exceptions.ConnectionError as e:
        return CheckResult("ERROR", "Connection", "", f"connection error: {e.__class__.__name__}: {e}")
    except requests.exceptions.TooManyRedirects as e:
        return CheckResult("ERROR", "Other", "", f"too many redirects: {e}")
    except requests.exceptions.InvalidURL as e:
        return CheckResult("ERROR", "Invalid URL", "", f"invalid URL: {e}")
    except requests.exceptions.RequestException as e:
        return CheckResult("ERROR", "Other", "", f"{e.__class__.__name__}: {e}")


def check_all(
    urls: list[str],
    workers: int = DEFAULT_WORKERS,
    timeout: int = DEFAULT_TIMEOUT,
    progress: bool = True,
) -> list[CheckResult]:
    """Check URLs by index-preserving parallel dispatch."""
    session = build_session()
    results: list[Optional[CheckResult]] = [None] * len(urls)
    started = time.time()
    done = 0

    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_to_idx = {pool.submit(check_one, session, u, timeout): i for i, u in enumerate(urls)}
        for fut in as_completed(future_to_idx):
            idx = future_to_idx[fut]
            results[idx] = fut.result()
            done += 1
            if progress and (done % 25 == 0 or done == len(urls)):
                elapsed = time.time() - started
                rate = done / elapsed if elapsed else 0
                print(f"  checked {done}/{len(urls)}  ({rate:.1f}/s)", file=sys.stderr)

    return [r if r is not None else CheckResult("ERROR", "Other", "", "no result") for r in results]


def run(
    input_path: Path,
    output_path: Path,
    url_column: str = "URL",
    workers: int = DEFAULT_WORKERS,
    timeout: int = DEFAULT_TIMEOUT,
) -> pd.DataFrame:
    print(f"Reading {input_path}", file=sys.stderr)
    df = pd.read_excel(input_path)

    if url_column not in df.columns:
        raise SystemExit(
            f"URL column {url_column!r} not found. Available columns: {list(df.columns)}"
        )

    urls = df[url_column].tolist()
    n_valid = sum(1 for u in urls if isinstance(u, str) and u.strip())
    print(f"Checking {n_valid} URLs across {len(urls)} rows with {workers} workers...", file=sys.stderr)

    results = check_all(urls, workers=workers, timeout=timeout)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    df["Status Code"] = [r.status_code for r in results]
    df["Status Category"] = [r.category for r in results]
    df["Final URL"] = [r.final_url for r in results]
    df["Error Detail"] = [r.error_detail for r in results]
    df["Checked At"] = timestamp

    print(f"Writing {output_path}", file=sys.stderr)
    df.to_excel(output_path, index=False)

    # Summary to stderr
    print("\nSummary by category:", file=sys.stderr)
    counts = df["Status Category"].value_counts()
    for cat, n in counts.items():
        print(f"  {cat:<18} {n}", file=sys.stderr)

    return df


def main() -> None:
    p = argparse.ArgumentParser(description="Check URLs in an xlsx file.")
    p.add_argument("input", type=Path, help="Input xlsx file")
    p.add_argument("--output", type=Path, default=None,
                   help="Output xlsx (default: <input>_checked.xlsx)")
    p.add_argument("--url-column", default="URL", help="Column containing URLs (default: URL)")
    p.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                   help=f"Concurrent workers (default: {DEFAULT_WORKERS})")
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT,
                   help=f"Per-request timeout in seconds (default: {DEFAULT_TIMEOUT})")
    args = p.parse_args()

    output = args.output or args.input.with_name(args.input.stem + "_checked.xlsx")
    run(args.input, output, args.url_column, args.workers, args.timeout)


if __name__ == "__main__":
    main()
