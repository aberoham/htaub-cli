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
iHCM People Home Extractor

Extracts employee data from ADP's iHCM portal, combining the directory API
(for bulk pagination) with the employee-card API (for additional HR fields
like employee code, reference number, and status).

Uses Playwright-based authentication from ihcm_auth.py.

Usage:
    uv run people_home_extractor.py

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


class PeopleHomeExtractor:
    """
    Extracts employee data by combining directory and employee-card APIs.

    The directory API supports pagination but lacks HR fields. The employee-card
    API has the HR fields but requires individual requests per employee.

    Employee card responses are cached to `.cache/employee_cards/` to avoid
    re-fetching on subsequent runs (useful when sessions expire mid-extraction).
    """

    EMPLOYEE_API = 'https://ihcm.adp.com/whrmux/webapi/api/employee'
    CARD_API = 'https://ihcm.adp.com/whrmux/webapi/api/employee-card'
    CACHE_DIR = Path('.cache/employee_cards')

    CSV_FIELDS = [
        'id',
        'fullName',
        'firstName',
        'lastName',
        'knownAs',
        'email',
        'userId',
        'jobTitle',
        'department',
        'location',
        'reportsTo',
        'reportsToFullName',
        'directReports',
        'employeeCode',
        'referenceNumber',
        'status',
        'context',
    ]

    def __init__(
        self,
        session: requests.Session,
        batch_size: int = 100,
        delay_seconds: float = 0.1,
        enrich_with_card: bool = True,
    ):
        self.session = session
        self.batch_size = batch_size
        self.delay_seconds = delay_seconds
        self.enrich_with_card = enrich_with_card

    def fetch_employee_batch(self, start: int) -> tuple[list[dict], int]:
        """Fetch a batch of employees from the directory API."""
        payload = {
            'start': start,
            'end': start + self.batch_size - 1,
            'limit': self.batch_size,
            'filter': [],
            'boolStr': 'AND',
            'orderBy': 'PEOPLE.LASTNAME, PEOPLE.FIRSTNAME',
            'showParameterics': False,
        }

        response = self.session.post(self.EMPLOYEE_API, json=payload)

        if response.status_code == 401:
            raise RuntimeError(
                'Authentication failed (401). Session may have expired.\n'
                'Try running with --clear-cache to force fresh authentication.'
            )

        if response.status_code != 200:
            raise RuntimeError(f'API returned status {response.status_code}: {response.text[:200]}')

        if response.text.strip().startswith('<!'):
            raise RuntimeError('Session expired - received login page.')

        result = response.json()
        return result.get('data', []), result.get('total', 0)

    def fetch_employee_card(self, people_id: str) -> tuple[dict, bool]:
        """Fetch additional HR fields for a single employee.

        Returns a tuple of (card_data, was_cached) where card_data contains the
        HR fields and was_cached indicates whether the data came from cache.
        The full API response is cached to disk for future runs.
        """
        cache_file = self.CACHE_DIR / f'{people_id}.json'
        if cache_file.exists():
            card = json.loads(cache_file.read_text())
            return {
                'employeeCode': card.get('EMPLOYEECODE'),
                'referenceNumber': card.get('REFERENCENUMBER'),
                'status': card.get('STATUS'),
            }, True

        response = self.session.get(f'{self.CARD_API}?peopleId={people_id}')

        if response.status_code != 200:
            return {}, False

        if response.text.strip().startswith('<!'):
            raise RuntimeError('Session expired - received login page.')

        try:
            data = response.json()
        except requests.exceptions.JSONDecodeError:
            print(f'  Warning: Empty response for {people_id}')
            return {}, False

        if not data:
            return {}, False

        card = data[0]

        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(card, indent=2))

        return {
            'employeeCode': card.get('EMPLOYEECODE'),
            'referenceNumber': card.get('REFERENCENUMBER'),
            'status': card.get('STATUS'),
        }, False

    def extract_all(self) -> list[dict]:
        """Extract all employees with optional enrichment from employee-card API."""
        all_employees = []
        start = 0
        total = None

        print(f'Starting extraction with batch size {self.batch_size}...')
        print()

        # Phase 1: Fetch all employees from directory API
        while True:
            batch, reported_total = self.fetch_employee_batch(start)

            if total is None:
                total = reported_total
                print(f'Total employees to extract: {total:,}')
                print()

            if not batch:
                break

            all_employees.extend(batch)

            progress = len(all_employees)
            pct = (progress / total * 100) if total > 0 else 0
            print(f'  Directory: {progress:,} / {total:,} ({pct:.1f}%)')

            if progress >= total:
                break

            start += len(batch)
            time.sleep(self.delay_seconds)

        print()
        print(f'Directory extraction complete: {len(all_employees):,} employees')

        # Phase 2: Enrich with employee-card data
        if self.enrich_with_card:
            print()
            cached_count = sum(1 for e in all_employees if (self.CACHE_DIR / f'{e["id"]}.json').exists())
            if cached_count > 0:
                print(f'Found {cached_count:,} cached employee cards.')
                print('Enriching with employee-card data...')
            else:
                print('Enriching with employee-card data (this takes ~20 minutes)...')
            print()

            cached = 0
            fetched = 0
            enriched = 0

            try:
                for i, emp in enumerate(all_employees):
                    card_data, was_cached = self.fetch_employee_card(emp['id'])
                    emp.update(card_data)
                    enriched = i + 1

                    if was_cached:
                        cached += 1
                    else:
                        fetched += 1
                        time.sleep(self.delay_seconds)

                    if (i + 1) % 100 == 0 or i == len(all_employees) - 1:
                        pct = ((i + 1) / len(all_employees) * 100)
                        print(f'  Cards: {i + 1:,} / {len(all_employees):,} ({pct:.1f}%) '
                              f'[cached: {cached:,}, fetched: {fetched:,}]')

                print()
                print('Enrichment complete.')

            except (RuntimeError, KeyboardInterrupt) as e:
                print()
                print(f'Interrupted: {e}')
                print(f'Saving partial results ({enriched:,} of {len(all_employees):,} enriched)...')
                print('Re-run with fresh credentials to continue from cache.')

        return all_employees


def export_to_json(employees: list[dict], filepath: Path) -> None:
    """Export employee data to a JSON file."""
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
    """Export employee data to a CSV file."""
    with open(filepath, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(employees)
    print(f'Exported to CSV: {filepath}')


def test_connection(session: requests.Session) -> bool:
    """Test connectivity by fetching a few records."""
    print('Testing connection...')

    try:
        # Test directory API
        response = session.post(
            'https://ihcm.adp.com/whrmux/webapi/api/employee',
            json={'start': 0, 'end': 2, 'limit': 3, 'filter': [], 'boolStr': 'AND',
                  'orderBy': 'PEOPLE.LASTNAME', 'showParameterics': False}
        )

        if response.status_code != 200:
            print(f'Directory API failed: {response.status_code}')
            return False

        if response.text.strip().startswith('<!'):
            print('Session expired - received login page.')
            return False

        data = response.json()
        print(f'Directory API: {data.get("total", 0):,} total employees')

        # Test employee-card API
        if data.get('data'):
            emp_id = data['data'][0]['id']
            card_resp = session.get(f'https://ihcm.adp.com/whrmux/webapi/api/employee-card?peopleId={emp_id}')
            if card_resp.status_code == 200 and card_resp.json():
                card = card_resp.json()[0]
                print(f'Employee-card API: OK (sample: code={card.get("EMPLOYEECODE")})')
            else:
                print('Employee-card API: Failed or empty')

        return True

    except Exception as e:
        print(f'Connection failed: {e}')
        return False


def main():
    parser = argparse.ArgumentParser(
        description='iHCM People Home Extractor',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument('--visible', action='store_true', help='Show browser during authentication')
    parser.add_argument('--clear-cache', action='store_true', help='Clear cached session before auth')
    parser.add_argument('--no-cache', action='store_true', help='Skip session cache entirely')
    args = parser.parse_args()

    print('iHCM People Home Extractor')
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

    extractor = PeopleHomeExtractor(
        session,
        batch_size=100,
        delay_seconds=0.1,
        enrich_with_card=True,
    )

    employees = extractor.extract_all()

    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    json_path = Path(f'people_home_{timestamp}.json')
    csv_path = Path(f'people_home_{timestamp}.csv')

    print()
    print('Exporting data...')
    export_to_json(employees, json_path)
    export_to_csv(employees, csv_path, PeopleHomeExtractor.CSV_FIELDS)

    print()
    print('Done!')


if __name__ == '__main__':
    main()
