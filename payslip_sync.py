#!/usr/bin/env python3
# /// script
# dependencies = [
#   "requests",
#   "playwright",
#   "playwright-stealth",
#   "python-dotenv",
# ]
# ///
"""
Payslip Sync Script

Downloads all historical payslips (JSON + PDF) from ADP iHCM and supports
incremental sync on subsequent runs.

Usage:
    uv run payslip_sync.py              # Full sync (JSON + PDF)
    uv run payslip_sync.py --skip-pdf   # JSON only
    uv run payslip_sync.py --visible    # Show browser during auth
    uv run payslip_sync.py --clear-cache # Force fresh auth
    uv run payslip_sync.py --list       # List cached payslips
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

from ihcm_auth import (
    SessionCache,
    create_authenticated_session_with_browser,
)

# Playwright types for type hints
try:
    from playwright.sync_api import BrowserContext
except ImportError:
    BrowserContext = type(None)

IHCM_BASE_URL = "https://ihcm.adp.com/whrmux/webapi"


class PayslipIndex:
    """
    Manages the index.json file that tracks all synced payslips.

    The index stores metadata about each payslip including its encoded ID,
    pay date, and paths to the cached JSON/PDF files.
    """

    def __init__(self, cache_dir: Path) -> None:
        self.cache_dir = cache_dir
        self.index_file = cache_dir / "index.json"
        self._data: dict[str, Any] = {}

    def load(self) -> None:
        """Load existing index from disk."""
        if self.index_file.exists():
            try:
                self._data = json.loads(self.index_file.read_text())
            except (json.JSONDecodeError, OSError) as e:
                print(f"  Warning: Failed to load index: {e}")
                self._data = {}
        else:
            self._data = {}

        # Initialize structure if empty
        if "payslips" not in self._data:
            self._data["payslips"] = {}
        if "total_count" not in self._data:
            self._data["total_count"] = 0

    def save(self) -> None:
        """Persist index to disk."""
        self._data["last_sync"] = datetime.now().isoformat()
        self._data["total_count"] = len(self._data.get("payslips", {}))

        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.index_file.write_text(json.dumps(self._data, indent=2))

    def is_cached(self, encoded_id: str, verify_exists: bool = True) -> bool:
        """
        Check if a payslip is already downloaded.

        Args:
            encoded_id: The encoded payslip ID
            verify_exists: If True, also verify the JSON file exists on disk
        """
        if encoded_id not in self._data.get("payslips", {}):
            return False

        if not verify_exists:
            return True

        # Verify the JSON file actually exists on disk
        entry = self._data["payslips"][encoded_id]
        json_path = entry.get("json_path")
        if json_path:
            full_path = self.cache_dir / json_path
            if not full_path.exists():
                return False

        return True

    def mark_cached(
        self,
        encoded_id: str,
        pay_date: str,
        json_path: str,
        pdf_path: str | None = None,
    ) -> None:
        """Record a downloaded payslip in the index."""
        self._data["payslips"][encoded_id] = {
            "pay_date": pay_date,
            "json_path": json_path,
            "pdf_path": pdf_path,
            "cached_at": datetime.now().isoformat(),
        }

    def get_all(self) -> dict[str, dict[str, Any]]:
        """Return all indexed payslips."""
        return self._data.get("payslips", {})

    @property
    def last_sync(self) -> str | None:
        """Return the timestamp of the last sync."""
        return self._data.get("last_sync")


class PayslipSyncer:
    """
    Handles API calls and downloads for payslips.

    Supports incremental sync by checking the index before downloading.
    Can use a Playwright browser context for PDF downloads that require SSO.
    """

    CACHE_DIR = Path(".cache/payslips")
    QUERY_API = f"{IHCM_BASE_URL}/api/pay/pay-statement-query"
    DETAIL_API = f"{IHCM_BASE_URL}/api/pay/pay-statement"

    def __init__(
        self,
        session: requests.Session,
        browser_context: BrowserContext | None = None,
        delay_seconds: float = 0.2,
    ) -> None:
        self.session = session
        self.browser_context = browser_context
        self.delay_seconds = delay_seconds
        self.index = PayslipIndex(self.CACHE_DIR)

    def _check_session_valid(self, response: requests.Response) -> None:
        """Raise an error if the session has expired."""
        if response.status_code == 401:
            raise RuntimeError(
                "Authentication failed (401). Session may have expired.\n"
                "Try running with --clear-cache to force fresh authentication."
            )

        # Check for HTML login page response
        content = response.text.strip()
        if content.startswith("<!") or content.startswith("<html"):
            raise RuntimeError(
                "Session expired - received login page instead of JSON.\n"
                "Try running with --clear-cache to force fresh authentication."
            )

    def list_all_payslips(self) -> list[dict[str, Any]]:
        """
        Fetch all available payslips from the API.

        Returns a list of payslip metadata including encoded IDs and pay dates.
        """
        all_payslips: list[dict[str, Any]] = []
        start = 0
        batch_size = 100

        while True:
            # The API requires filter array with date range to fetch all history
            # Without filters, the API defaults to "Last 3" payslips
            payload = {
                "filter": [
                    {
                        "id": 6,
                        "operator": "lteq",
                        "value": "2019-01-01",
                        "property": "dateFilter",
                        "boolean": "OR",
                        "group": 2,
                        "groupName": "Date range",
                        "displayValue": "Start date",
                    },
                    {
                        "id": 7,
                        "operator": "gteq",
                        "value": "2030-12-31",
                        "property": "dateFilter",
                        "boolean": "OR",
                        "group": 2,
                        "groupName": "Date range",
                        "displayValue": "End date",
                    },
                ],
                "orderBy": "PAYCHECKDATE DESC",
                "limit": batch_size,
                "showParameterics": False,
                "start": start,
                "end": start + batch_size - 1,
            }

            response = self.session.post(self.QUERY_API, json=payload)
            self._check_session_valid(response)

            if response.status_code != 200:
                raise RuntimeError(
                    f"Failed to query payslips: HTTP {response.status_code}\n"
                    f"Response: {response.text[:500]}"
                )

            try:
                data = response.json()
            except requests.exceptions.JSONDecodeError as e:
                raise RuntimeError(
                    f"Invalid JSON response from payslip query: {e}"
                ) from e

            # Extract payslips from response
            if isinstance(data, list):
                batch = data
                total = len(data)
            elif isinstance(data, dict) and "data" in data:
                batch = data["data"]
                total = data.get("total", len(batch))
            else:
                print(f"  Warning: Unexpected response structure: {type(data)}")
                if isinstance(data, dict):
                    print(f"  Keys: {list(data.keys())}")
                break

            all_payslips.extend(batch)

            # Check if we've fetched all payslips
            if len(all_payslips) >= total or len(batch) == 0:
                break

            start += len(batch)
            time.sleep(0.1)  # Small delay between pagination requests

        return all_payslips

    def fetch_payslip_detail(self, encoded_id: str) -> dict[str, Any]:
        """
        Fetch detailed JSON breakdown for a single payslip.

        The encoded_id is a base64-encoded compound key.
        """
        url = f"{self.DETAIL_API}/{encoded_id}"
        response = self.session.get(url)
        self._check_session_valid(response)

        if response.status_code == 404:
            raise RuntimeError(f"Payslip not found: {encoded_id}")

        if response.status_code != 200:
            raise RuntimeError(
                f"Failed to fetch payslip detail: HTTP {response.status_code}"
            )

        return response.json()

    def _fetch_pdf_via_playwright(self, pdf_url: str) -> bytes | None:
        """
        Download PDF using Playwright browser context.

        This handles SiteMinder SSO automatically since the browser
        has an authenticated session.
        """
        if not self.browser_context:
            return None

        page = None
        try:
            page = self.browser_context.new_page()

            # Navigate to the Pay and Statements page first to establish session
            page.goto(
                "https://ihcm.adp.com/whrmux/web/me/pay-and-statements",
                timeout=60000,
                wait_until="networkidle",
            )

            # Wait for page to stabilize
            page.wait_for_timeout(2000)

            # Get the bearer token from sessionStorage
            bearer_token = page.evaluate("() => sessionStorage.getItem('iHcmBearerToken')")

            # Use JavaScript fetch with credentials and auth header to get the PDF
            result = page.evaluate(
                """async (args) => {
                const { url, token } = args;
                try {
                    const headers = {
                        'Accept': 'application/pdf, */*',
                    };
                    if (token) {
                        headers['Authorization'] = 'Bearer ' + token;
                    }

                    const response = await fetch(url, {
                        method: 'GET',
                        credentials: 'include',
                        redirect: 'follow',
                        headers: headers
                    });

                    if (!response.ok) {
                        return { error: 'HTTP ' + response.status, redirected: response.redirected, url: response.url };
                    }

                    const contentType = response.headers.get('content-type') || '';
                    if (!contentType.includes('pdf') && !contentType.includes('octet-stream')) {
                        return { error: 'wrong content-type: ' + contentType.substring(0, 50), url: response.url };
                    }

                    const arrayBuffer = await response.arrayBuffer();
                    const bytes = Array.from(new Uint8Array(arrayBuffer));
                    return { success: true, bytes: bytes, contentType: contentType };
                } catch (e) {
                    return { error: e.message };
                }
            }""",
                {"url": pdf_url, "token": bearer_token},
            )

            if result.get("error"):
                err = result["error"]
                url = result.get("url", "")
                if "login" in url.lower() or "signin" in url.lower():
                    print(" (PDF: SSO redirect)", end="")
                else:
                    print(f" (PDF fetch: {err[:40]})", end="")
                return None

            if result.get("success"):
                pdf_bytes = bytes(result["bytes"])
                if pdf_bytes and pdf_bytes[:4] == b"%PDF":
                    return pdf_bytes
                print(" (PDF: not a valid PDF)", end="")
                return None

            print(" (PDF: unknown result)", end="")
            return None

        except Exception as e:
            error_msg = str(e)
            if len(error_msg) > 50:
                error_msg = error_msg[:50] + "..."
            print(f" (PDF error: {error_msg})", end="")
            return None
        finally:
            if page:
                try:
                    page.close()
                except Exception:
                    pass

    def _fetch_pdf_via_requests(self, pdf_url: str) -> bytes | None:
        """
        Download PDF using requests session.

        This will likely fail for PDFs protected by SiteMinder SSO.
        """
        try:
            response = self.session.get(pdf_url, timeout=30, allow_redirects=False)

            # Follow redirects manually, up to 10 hops
            redirect_count = 0
            while response.status_code in (301, 302, 303, 307, 308) and redirect_count < 10:
                redirect_url = response.headers.get("Location")
                if not redirect_url:
                    break
                # Handle relative URLs
                if redirect_url.startswith("/"):
                    from urllib.parse import urlparse

                    parsed = urlparse(pdf_url)
                    redirect_url = f"{parsed.scheme}://{parsed.netloc}{redirect_url}"
                response = self.session.get(redirect_url, timeout=30, allow_redirects=False)
                redirect_count += 1

        except requests.RequestException as e:
            print(f" (PDF failed: {type(e).__name__})", end="")
            return None

        if response.status_code != 200:
            print(f" (HTTP {response.status_code})", end="")
            return None

        content_type = response.headers.get("Content-Type", "")
        if "application/pdf" not in content_type and "octet-stream" not in content_type:
            print(f" (bad content-type: {content_type[:30]})", end="")
            return None

        return response.content

    def fetch_payslip_pdf(self, pdf_url: str) -> bytes | None:
        """
        Download the PDF for a payslip from the given URL.

        Uses the /whrmux/webapi/api/pay/payslip/file endpoint which works
        with standard Bearer token authentication (no SSO issues).

        Returns the PDF bytes or None if download fails.
        """
        if not pdf_url:
            return None

        # The API endpoint works with standard requests session
        # (Bearer token + cookies are already configured)
        return self._fetch_pdf_via_requests(pdf_url)

    def _get_pay_date(self, payslip: dict[str, Any]) -> str:
        """Extract the pay date from a payslip record."""
        # Try common field names
        for field in ["payDate", "pay_date", "paymentDate", "date", "periodEndDate"]:
            if field in payslip:
                date_val = payslip[field]
                if isinstance(date_val, str):
                    # Return just the date portion if it's a datetime string
                    return date_val.split("T")[0]
        return "unknown"

    def _get_encoded_id(self, payslip: dict[str, Any]) -> str:
        """Extract the encoded ID from a payslip record."""
        # Primary method: extract from payDetailUri.href
        # Format: /v1_0/O/A/payStatement/{encodedId}
        if "payDetailUri" in payslip:
            uri = payslip["payDetailUri"]
            href = uri.get("href") if isinstance(uri, dict) else uri
            if href and "/payStatement/" in href:
                return href.split("/payStatement/")[1]

        # Fallback: try common field names
        for field in ["id", "encodedId", "encoded_id", "payStatementId"]:
            if field in payslip and payslip[field]:
                return str(payslip[field])

        raise ValueError(f"Could not find ID in payslip: {list(payslip.keys())}")

    def _get_pdf_info(self, payslip: dict[str, Any]) -> tuple[str | None, str | None]:
        """
        Extract PDF download info from a payslip record.

        Returns (statement_id, image_id) tuple for constructing the API URL.
        The UI uses /whrmux/webapi/api/pay/payslip/file endpoint.

        IMPORTANT: The API has TWO different ID formats:
        - payDetailUri uses base64-encoded IDs
        - statementImageUri uses a different format (GUIDs with underscores)

        The PDF download API requires the ID format from statementImageUri!
        Format: /v1_0/O/A/payStatement/{statement_id}/images/{image_id}.pdf
        """
        statement_id = None
        image_id = None

        # Extract BOTH statement_id AND image_id from statementImageUri
        # Format: /v1_0/O/A/payStatement/{statement_id}/images/{image_id}.pdf
        if "statementImageUri" in payslip:
            uri = payslip["statementImageUri"]
            href = uri.get("href") if isinstance(uri, dict) else uri
            if href and "/payStatement/" in href and "/images/" in href:
                # Extract statement_id (between /payStatement/ and /images/)
                after_paystatement = href.split("/payStatement/")[1]
                statement_id = after_paystatement.split("/images/")[0]

                # Extract image_id (filename without .pdf extension)
                filename = href.split("/images/")[1]
                if filename.endswith(".pdf"):
                    image_id = filename[:-4]

        return statement_id, image_id

    def _build_pdf_api_url(self, statement_id: str, image_id: str) -> str:
        """Build the correct API URL for PDF download."""
        return (
            f"{IHCM_BASE_URL}/api/pay/payslip/file"
            f"?statementId={statement_id}&imageId={image_id}&imageType=pdf"
        )

    def _make_file_path(self, pay_date: str, extension: str) -> Path:
        """Generate the file path for a payslip based on its date."""
        # Parse the date to extract year and month
        try:
            dt = datetime.fromisoformat(pay_date.split("T")[0])
            year = str(dt.year)
            month = f"{dt.month:02d}"
        except ValueError:
            year = "unknown"
            month = "unknown"

        return self.CACHE_DIR / year / month / f"{pay_date}.{extension}"

    def sync_all(self, skip_pdf: bool = False) -> tuple[int, int, int]:
        """
        Main sync loop - downloads all missing payslips.

        Returns a tuple of (total_available, already_cached, newly_downloaded).
        """
        print("Loading payslip index...")
        self.index.load()

        if self.index.last_sync:
            print(f"  Last sync: {self.index.last_sync}")
            print(f"  Cached payslips: {len(self.index.get_all())}")
        else:
            print("  No previous sync found")

        print()
        print("Querying available payslips...")
        payslips = self.list_all_payslips()
        total_available = len(payslips)
        print(f"  Found {total_available} payslips")

        if not payslips:
            print("No payslips available.")
            return 0, 0, 0

        # Find missing payslips
        missing = []
        for ps in payslips:
            try:
                encoded_id = self._get_encoded_id(ps)
                if not self.index.is_cached(encoded_id):
                    missing.append(ps)
            except ValueError as e:
                print(f"  Warning: {e}")

        already_cached = total_available - len(missing)
        print(f"  Already cached: {already_cached}")
        print(f"  To download: {len(missing)}")

        if not missing:
            print()
            print("All payslips are already cached!")
            self.index.save()
            return total_available, already_cached, 0

        # Download missing payslips
        print()
        print("Downloading missing payslips...")
        newly_downloaded = 0

        for i, ps in enumerate(missing):
            encoded_id = self._get_encoded_id(ps)
            pay_date = self._get_pay_date(ps)
            statement_id, image_id = self._get_pdf_info(ps)

            print(f"  [{i + 1}/{len(missing)}] {pay_date}...", end="", flush=True)

            try:
                # Fetch detailed JSON
                detail = self.fetch_payslip_detail(encoded_id)

                # Save JSON
                json_path = self._make_file_path(pay_date, "json")
                json_path.parent.mkdir(parents=True, exist_ok=True)
                json_path.write_text(json.dumps(detail, indent=2))

                # Fetch and save PDF if enabled and we have the required info
                pdf_rel_path: str | None = None
                if not skip_pdf and statement_id and image_id:
                    pdf_url = self._build_pdf_api_url(statement_id, image_id)
                    pdf_data = self.fetch_payslip_pdf(pdf_url)
                    if pdf_data:
                        pdf_path = self._make_file_path(pay_date, "pdf")
                        pdf_path.parent.mkdir(parents=True, exist_ok=True)
                        pdf_path.write_bytes(pdf_data)
                        pdf_rel_path = str(pdf_path.relative_to(self.CACHE_DIR))

                # Update index
                json_rel_path = str(json_path.relative_to(self.CACHE_DIR))
                self.index.mark_cached(encoded_id, pay_date, json_rel_path, pdf_rel_path)

                newly_downloaded += 1
                status = "OK" if pdf_rel_path or skip_pdf else "OK (no PDF)"
                print(f" {status}")

            except RuntimeError as e:
                if "401" in str(e) or "expired" in str(e).lower():
                    # Save progress before raising
                    print(" FAILED (session expired)")
                    print()
                    print("Session expired. Saving progress...")
                    self.index.save()
                    raise
                print(f" FAILED: {e}")

            time.sleep(self.delay_seconds)

        # Save final index
        self.index.save()

        return total_available, already_cached, newly_downloaded

    def _get_pdf_statement_ids(self) -> dict[str, tuple[str, str]]:
        """
        Query the API to get PDF statement IDs for all payslips.

        The API returns two different ID formats:
        - payDetailUri: base64 encoded (what we store in index)
        - statementImageUri: different format needed for PDF downloads

        Returns a dict mapping encoded_id -> (statement_id, image_id)
        """
        print("  Querying API for PDF statement IDs...")
        id_map: dict[str, tuple[str, str]] = {}

        payslips = self.list_all_payslips()
        for ps in payslips:
            # Get the base64 encoded ID (what we store in index)
            encoded_id = self._get_encoded_id(ps)

            # Get the statement ID and image ID from statementImageUri
            statement_id, image_id = self._get_pdf_info(ps)
            if statement_id and image_id:
                id_map[encoded_id] = (statement_id, image_id)

        print(f"  Found PDF info for {len(id_map)} payslips")
        return id_map

    def sync_pdfs_only(self) -> tuple[int, int, int]:
        """
        Download PDFs for cached payslips that are missing them.

        Returns a tuple of (total_cached, already_have_pdf, newly_downloaded).
        """
        print("Loading payslip index...")
        self.index.load()

        all_cached = self.index.get_all()
        total_cached = len(all_cached)
        print(f"  Cached payslips: {total_cached}")

        if not all_cached:
            print("No cached payslips found. Run without --pdf-only first.")
            return 0, 0, 0

        # Find entries missing PDFs
        missing_pdf = []
        for encoded_id, info in all_cached.items():
            pdf_path = info.get("pdf_path")
            if pdf_path:
                # Check if PDF file actually exists
                full_path = self.CACHE_DIR / pdf_path
                if full_path.exists():
                    continue
            missing_pdf.append((encoded_id, info))

        already_have_pdf = total_cached - len(missing_pdf)
        print(f"  Already have PDF: {already_have_pdf}")
        print(f"  Missing PDF: {len(missing_pdf)}")

        if not missing_pdf:
            print()
            print("All cached payslips already have PDFs!")
            return total_cached, already_have_pdf, 0

        # Query API to get the correct statement IDs for PDF downloads
        print()
        pdf_id_map = self._get_pdf_statement_ids()

        # Download missing PDFs
        print()
        print("Downloading missing PDFs...")
        newly_downloaded = 0

        for i, (encoded_id, info) in enumerate(missing_pdf):
            pay_date = info.get("pay_date", "unknown")

            print(f"  [{i + 1}/{len(missing_pdf)}] {pay_date}...", end="", flush=True)

            try:
                # Get the correct statement ID and image ID from the API response
                if encoded_id not in pdf_id_map:
                    print(" SKIP (no PDF info from API)")
                    continue

                statement_id, image_id = pdf_id_map[encoded_id]

                # Build PDF URL and download
                pdf_url = self._build_pdf_api_url(statement_id, image_id)
                pdf_data = self.fetch_payslip_pdf(pdf_url)

                if pdf_data:
                    pdf_path = self._make_file_path(pay_date, "pdf")
                    pdf_path.parent.mkdir(parents=True, exist_ok=True)
                    pdf_path.write_bytes(pdf_data)
                    pdf_rel_path = str(pdf_path.relative_to(self.CACHE_DIR))

                    # Update index with PDF path
                    self.index._data["payslips"][encoded_id]["pdf_path"] = pdf_rel_path
                    newly_downloaded += 1
                    print(" OK")
                else:
                    print(" FAILED")

            except Exception as e:
                error_msg = str(e)[:40]
                print(f" ERROR: {error_msg}")

            time.sleep(self.delay_seconds)

        # Save updated index
        self.index.save()

        return total_cached, already_have_pdf, newly_downloaded


def list_cached_payslips(cache_dir: Path) -> None:
    """Display a summary of cached payslips."""
    index = PayslipIndex(cache_dir)
    index.load()

    payslips = index.get_all()
    if not payslips:
        print("No cached payslips found.")
        return

    print(f"Cached payslips: {len(payslips)}")
    if index.last_sync:
        print(f"Last sync: {index.last_sync}")
    print()

    # Group by year
    by_year: dict[str, list[str]] = {}
    for _encoded_id, info in payslips.items():
        pay_date = info.get("pay_date", "unknown")
        year = pay_date[:4] if len(pay_date) >= 4 else "unknown"
        by_year.setdefault(year, []).append(pay_date)

    for year in sorted(by_year.keys(), reverse=True):
        dates = sorted(by_year[year], reverse=True)
        print(f"  {year}: {len(dates)} payslips")
        # Show first few dates
        for date in dates[:3]:
            print(f"    - {date}")
        if len(dates) > 3:
            print(f"    ... and {len(dates) - 3} more")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Payslip Sync Script",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--skip-pdf",
        action="store_true",
        help="Skip PDF downloads, only fetch JSON data",
    )
    parser.add_argument(
        "--pdf-only",
        action="store_true",
        help="Only download PDFs for already-cached payslips missing PDFs",
    )
    parser.add_argument(
        "--visible",
        action="store_true",
        help="Show browser window during authentication",
    )
    parser.add_argument(
        "--clear-cache",
        action="store_true",
        help="Clear cached session and force fresh authentication",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Skip session cache entirely",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List cached payslips and exit",
    )
    args = parser.parse_args()

    print("Payslip Sync")
    print("=" * 50)
    print()

    # Handle --list without authentication
    if args.list:
        list_cached_payslips(PayslipSyncer.CACHE_DIR)
        return 0

    # Handle cache options
    if args.clear_cache:
        cache = SessionCache(verbose=True)
        cache.clear()
        print()

    use_cache = not args.no_cache

    # For PDF downloads, we need a live browser context
    # For JSON-only mode, we can skip keeping the browser alive
    need_browser = not args.skip_pdf

    # Authenticate using Playwright
    # When PDFs are needed, keep the browser alive for SSO-protected downloads
    try:
        auth_result = create_authenticated_session_with_browser(
            verbose=True,
            headless=not args.visible,
            use_cache=use_cache,
        )
    except Exception as e:
        print(f"\nAuthentication failed: {e}")
        return 1

    print()

    # Run sync with context manager to ensure browser cleanup
    try:
        # Pass browser context only if we need PDFs and have a live browser
        browser_context = auth_result.browser_context if need_browser else None

        if need_browser and not browser_context:
            print("Note: No browser context available (using cached session).")
            print("      PDF downloads may fail. Use --clear-cache to force fresh auth.")
            print()

        syncer = PayslipSyncer(
            session=auth_result.session,
            browser_context=browser_context,
        )

        if args.pdf_only:
            # PDF-only mode: download PDFs for already-cached payslips
            total, have_pdf, downloaded = syncer.sync_pdfs_only()
            print()
            print("-" * 50)
            print("PDF Sync Summary")
            print("-" * 50)
            print(f"  Total cached payslips: {total}")
            print(f"  Already have PDF: {have_pdf}")
            print(f"  Newly downloaded: {downloaded}")
        else:
            # Normal sync mode
            total, cached, downloaded = syncer.sync_all(skip_pdf=args.skip_pdf)
            print()
            print("-" * 50)
            print("Sync Summary")
            print("-" * 50)
            print(f"  Total payslips available: {total}")
            print(f"  Already cached: {cached}")
            print(f"  Newly downloaded: {downloaded}")
        print()
        print("Done!")
        return 0

    except RuntimeError as e:
        print(f"\nSync failed: {e}")
        return 1
    except KeyboardInterrupt:
        print("\n\nInterrupted by user. Progress has been saved.")
        return 1
    finally:
        # Clean up browser resources
        auth_result.close_browser()


if __name__ == "__main__":
    sys.exit(main())
