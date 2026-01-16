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
iHCM Employee Directory Extractor

A script to extract employee data from ADP's iHCM portal using their internal API.
Uses Playwright-based authentication from ihcm_auth.py.

Usage:
    uv run ihcm_extractor.py

Options:
    --visible       Show browser window during authentication
    --clear-cache   Clear cached session and force fresh authentication
    --no-cache      Skip session cache entirely
"""

import argparse
import csv
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

from ihcm_auth import create_authenticated_session_playwright


class IHCMExtractor:
    """Handles paginated extraction of employee data from iHCM API."""

    API_URL = 'https://ihcm.adp.com/whrmux/webapi/api/employee'

    CSV_FIELDS = [
        'id',
        'email',
        'userId',
        'fullName',
        'firstName',
        'lastName',
        'knownAs',
        'knownAsFnc',
        'initials',
        'jobTitle',
        'department',
        'location',
        'workTelephone',
        'workMobileTelephone',
        'reportsTo',
        'reportsToFullName',
        'reportsToUserId',
        'reportsToJobtitle',
        'reportsToFnc',
        'reportsToKnownas',
        'directReports',
        'isSSOOnly',
        'context',
    ]

    def __init__(
        self,
        session: requests.Session,
        batch_size: int = 100,
        delay_seconds: float = 0.5,
    ):
        self.session = session
        self.batch_size = batch_size
        self.delay_seconds = delay_seconds

    def fetch_batch(self, start: int) -> tuple[list[dict], int]:
        """
        Fetch a single batch of employees starting at the given index.
        Returns (employee_list, total_count).
        """
        payload = {
            'start': start,
            'end': start + self.batch_size - 1,
            'limit': self.batch_size,
            'filter': [],
            'boolStr': 'AND',
            'orderBy': 'PEOPLE.LASTNAME, PEOPLE.FIRSTNAME',
            'showParameterics': False,
        }

        response = self.session.post(self.API_URL, json=payload)

        if response.status_code == 401:
            raise RuntimeError(
                'Authentication failed (401). Session may have expired.\n'
                'Try running with --clear-cache to force fresh authentication.'
            )

        if response.status_code != 200:
            raise RuntimeError(f'API returned status {response.status_code}: {response.text[:200]}')

        if response.text.strip().startswith('<!doctype') or response.text.strip().startswith('<html'):
            raise RuntimeError('Session expired - received login page. Please refresh credentials.')

        result = response.json()
        return result.get('data', []), result.get('total', 0)

    def extract_all(self) -> list[dict]:
        """Extract all employees using paginated requests with rate limiting."""
        all_employees = []
        start = 0
        total = None

        print(f'Starting extraction with batch size {self.batch_size}...')
        print(f'Rate limiting: {self.delay_seconds}s delay between requests')
        print()

        while True:
            batch, reported_total = self.fetch_batch(start)

            if total is None:
                total = reported_total
                print(f'Total employees to extract: {total:,}')
                print()

            if not batch:
                # Empty batch means we've reached the end
                break

            all_employees.extend(batch)

            progress = len(all_employees)
            pct = (progress / total * 100) if total > 0 else 0
            print(f'  Retrieved {progress:,} / {total:,} employees ({pct:.1f}%)')

            if progress >= total:
                break

            # Move to next page (use actual batch length for accuracy)
            start += len(batch)
            time.sleep(self.delay_seconds)

        print()
        print(f'Extraction complete: {len(all_employees):,} employees')
        return all_employees


def export_to_json(employees: list[dict], filepath: Path) -> None:
    """Export employee data to a JSON file with pretty formatting."""
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(
            {
                'exported_at': datetime.now().isoformat(),
                'total_count': len(employees),
                'employees': employees,
            },
            f,
            indent=2,
            ensure_ascii=False,
        )
    print(f'Exported to JSON: {filepath}')


def export_to_csv(employees: list[dict], filepath: Path, fields: list[str]) -> None:
    """Export employee data to a CSV file with specified fields."""
    with open(filepath, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(employees)
    print(f'Exported to CSV: {filepath}')


def test_connection(session: requests.Session) -> bool:
    """Test connectivity by fetching a few employee records."""
    extractor = IHCMExtractor(session, batch_size=3)

    print(f'Testing connection to {extractor.API_URL}...')

    try:
        employees, total = extractor.fetch_batch(0)
        print(f'Success! API reports {total:,} total employees.')
        print(f'Retrieved {len(employees)} employee record(s) in test request.')

        if employees:
            print('\nSample employee:')
            print(f'  Name: {employees[0].get("fullName")}')
            print(f'  Email: {employees[0].get("email")}')
            print(f'  Title: {employees[0].get("jobTitle")}')

        return True

    except Exception as e:
        print(f'Connection failed: {e}')
        return False


def main():
    parser = argparse.ArgumentParser(
        description='iHCM Employee Directory Extractor',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument('--visible', action='store_true', help='Show browser during authentication')
    parser.add_argument('--clear-cache', action='store_true', help='Clear cached session before auth')
    parser.add_argument('--no-cache', action='store_true', help='Skip session cache entirely')
    args = parser.parse_args()

    print('iHCM Employee Directory Extractor')
    print('=' * 50)
    print()

    # Handle cache options
    if args.clear_cache:
        from ihcm_auth import SessionCache
        cache = SessionCache(verbose=True)
        cache.clear()
        print()

    use_cache = not args.no_cache

    # Authenticate using Playwright
    try:
        session = create_authenticated_session_playwright(
            verbose=True,
            headless=not args.visible,
            use_cache=use_cache,
        )
    except Exception as e:
        print(f'\nAuthentication failed: {e}')
        sys.exit(1)

    print()

    if not test_connection(session):
        print('\nConnection test failed. Try --clear-cache to force fresh authentication.')
        sys.exit(1)

    print()
    print('-' * 50)
    print('Starting full extraction...')
    print('-' * 50)
    print()

    extractor = IHCMExtractor(
        session,
        batch_size=100,
        delay_seconds=0.5,
    )

    employees = extractor.extract_all()

    # Generate timestamped filenames
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    output_dir = Path('.')

    json_path = output_dir / f'ihcm_employees_{timestamp}.json'
    csv_path = output_dir / f'ihcm_employees_{timestamp}.csv'

    print()
    print('Exporting data...')
    export_to_json(employees, json_path)
    export_to_csv(employees, csv_path, IHCMExtractor.CSV_FIELDS)

    print()
    print('Done!')


if __name__ == '__main__':
    main()
