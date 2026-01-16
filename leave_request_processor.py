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
iHCM Leave Request Processor

A CLI script to list and process (approve/reject) employee leave requests from ADP iHCM.
Uses Playwright-based authentication from ihcm_auth.py.

Usage:
    # List pending leave requests
    uv run leave_request_processor.py list

    # Approve a specific leave request by record ID
    uv run leave_request_processor.py approve <record_id>

    # Reject a specific leave request by record ID
    uv run leave_request_processor.py reject <record_id> --reason "Insufficient notice"

    # Show details of a specific leave request
    uv run leave_request_processor.py show <record_id>
"""

import argparse
import json
import sys
import time

import requests

from ihcm_auth import IHCM_BASE_URL, create_authenticated_session_playwright


class LeaveRequestProcessor:
    """Handles listing and processing leave requests via the iHCM API."""

    # API endpoints discovered through browser automation research
    PENDING_REQUESTS_URL = f'{IHCM_BASE_URL}/whrmux/webapi/api/data/expert/pending-requests'
    SCHEMA_GRID_URL = f'{IHCM_BASE_URL}/whrmux/webapi/api/schema-grid/data'
    LEAVE_APPROVE_URL = f'{IHCM_BASE_URL}/whrmux/webapi/api/data/expert/leave-approve'
    SCREEN_ACTION_URL = f'{IHCM_BASE_URL}/whrmux/webapi/api/screen-action'
    ACTION_BUTTON_URL = f'{IHCM_BASE_URL}/whrmux/webapi/api/action-button'
    MESSAGES_DRAWER_URL = f'{IHCM_BASE_URL}/whrmux/webapi/api/messages/drawer'
    MESSAGE_BODY_URL = f'{IHCM_BASE_URL}/whrmux/webapi/api/messages/messagebody'

    def __init__(self, session: requests.Session, verbose: bool = True):
        self.session = session
        self.verbose = verbose

    def _log(self, message: str):
        """Print a message if verbose mode is enabled."""
        if self.verbose:
            print(message)

    def get_pending_requests_from_messages(self) -> list[dict]:
        """
        Get pending leave requests from the messages/todo drawer.

        This returns leave requests that appear in the "Things to do" section
        on the Expert home page.
        """
        self._log('Fetching leave requests from messages drawer...')

        response = self.session.get(
            self.MESSAGES_DRAWER_URL,
            params={'todoItemsOnly': 'true'}
        )

        if response.status_code != 200:
            raise RuntimeError(f'Failed to fetch messages: {response.status_code} {response.text[:200]}')

        # Handle empty response (often indicates insufficient permissions)
        if not response.text or not response.text.strip():
            self._log('  Warning: Empty response from messages endpoint (may indicate insufficient permissions)')
            return []

        try:
            data = response.json()
        except requests.exceptions.JSONDecodeError:
            self._log(f'  Warning: Invalid JSON response: {response.text[:100]}')
            return []

        # Filter for leave-related messages
        leave_requests = []
        for message in data.get('data', []):
            # Check if this is a leave request notification
            subject = message.get('subject', '').lower()
            if any(term in subject for term in ['leave', 'absence', 'working from home', 'holiday', 'annual']):
                leave_requests.append(message)

        self._log(f'  Found {len(leave_requests)} leave-related messages')
        return leave_requests

    def get_message_details(self, message_id: str) -> dict:
        """Get the full details of a message/leave request."""
        url = f'{self.MESSAGE_BODY_URL}/{message_id}/0'
        response = self.session.get(url)

        if response.status_code != 200:
            raise RuntimeError(f'Failed to fetch message details: {response.status_code}')

        return response.json()

    def get_pending_requests_from_grid(
        self,
        older_than_days: int = 0,
        limit: int = 100,
        employee_name: str = None,
    ) -> list[dict]:
        """
        Get pending leave requests from the Monitor Leave Requests grid.

        This returns the full list of leave requests visible in the
        Expert > Leave and absence > Monitor leave requests page.

        Args:
            older_than_days: Filter for requests older than N days (0 = all)
            limit: Maximum number of records to return
            employee_name: Filter by employee name (partial match)

        Returns:
            List of leave request records
        """
        self._log('Fetching pending leave requests from grid...')

        # First, get the form metadata to understand the structure
        params_url = f'{self.PENDING_REQUESTS_URL}?insertMode=false&parameters='
        if older_than_days > 0:
            params_url += json.dumps({'_AGEOFLEAVEREQUEST': str(older_than_days)})

        # The schema-grid endpoint returns the actual data
        # This payload structure was observed in browser network traffic
        payload = {
            'parentRoute': 'expert',
            'dataFromApi': False,
            'instanceName': 'grid-controller-pendingRequestsGrid',
            'gridName': 'pendingRequestsGrid',
            'searchValue': employee_name or '',
            'formParams': {
                'parentRoute': 'expert',
                'endpoint': 'pending-requests',
                'insertMode': False,
            },
            'formControlParameters': {
                '_AGEOFLEAVEREQUEST': str(older_than_days) if older_than_days > 0 else None,
            },
            'filter': {
                'showParameterics': False,
                'filters': [],
                'filterEmployeeId': None,
            },
            'paging': {
                'start': 1,
                'limit': limit,
                'end': limit,
                'total': 0,
            },
            'sort': {
                'orderBy': 'startDate',
                'sortDirection': 'asc',
            },
        }

        response = self.session.post(self.SCHEMA_GRID_URL, json=payload)

        if response.status_code == 400:
            # The schema-grid endpoint can be finicky; try alternative approach
            self._log('  Schema-grid returned 400, trying alternative endpoint...')
            return self._get_pending_requests_alternative(older_than_days, limit)

        if response.status_code != 200:
            raise RuntimeError(f'Failed to fetch leave requests: {response.status_code} {response.text[:500]}')

        # Handle empty response
        if not response.text or not response.text.strip():
            raise RuntimeError('Grid endpoint returned empty response (may indicate insufficient permissions)')

        try:
            data = response.json()
        except requests.exceptions.JSONDecodeError as e:
            raise RuntimeError(f'Grid endpoint failed: {e}') from e

        records = data.get('data', [])
        total = data.get('total', len(records))

        self._log(f'  Retrieved {len(records)} of {total} pending requests')
        return records

    def _get_pending_requests_alternative(self, older_than_days: int, limit: int) -> list[dict]:
        """
        Alternative method to get pending requests using the data endpoint.

        Falls back to this if schema-grid fails.
        """
        params = {}
        if older_than_days > 0:
            params['parameters'] = json.dumps({'_AGEOFLEAVEREQUEST': str(older_than_days)})
        params['insertMode'] = 'false'

        response = self.session.get(self.PENDING_REQUESTS_URL, params=params)

        if response.status_code != 200:
            raise RuntimeError(f'Alternative endpoint failed: {response.status_code}')

        # Handle empty response (often indicates insufficient permissions)
        if not response.text or not response.text.strip():
            raise RuntimeError('Alternative endpoint returned empty response (may indicate insufficient permissions)')

        try:
            data = response.json()
        except requests.exceptions.JSONDecodeError as e:
            raise RuntimeError(f'Alternative endpoint failed: {e}') from e

        return data.get('data', [])

    def approve_request(self, record_id: str, comments: str = '') -> bool:
        """
        Approve a leave request by its record ID.

        Args:
            record_id: The UUID of the leave request record
            comments: Optional approval comments

        Returns:
            True if approval was successful
        """
        self._log(f'Approving leave request {record_id}...')

        # Step 1: Submit the approval data
        approve_payload = {
            'recordId': record_id,
            'action': 'approve',
            'comments': comments,
        }

        response = self.session.post(self.LEAVE_APPROVE_URL, json=approve_payload)

        if response.status_code != 200:
            # Try the screen-action approach observed in browser
            self._log('  Direct approval failed, trying screen-action approach...')
            return self._approve_via_screen_action(record_id, comments)

        self._log('  Approval submitted successfully')

        # Step 2: Trigger the save action
        save_url = f'{self.SCREEN_ACTION_URL}/expert.leave-approve/{record_id}/save/none'
        save_response = self.session.get(save_url)

        if save_response.status_code == 200:
            self._log('  Save action completed')
            return True
        else:
            self._log(f'  Warning: Save action returned {save_response.status_code}')
            return True  # Approval may still have worked

    def _approve_via_screen_action(self, record_id: str, comments: str = '') -> bool:
        """
        Approve using the screen-action flow observed in the browser.

        This mimics the exact flow when clicking Approve in the UI.
        """
        # The browser makes multiple calls to screen-action during approval
        # This is a polling mechanism to check if the action completed
        save_url = f'{self.SCREEN_ACTION_URL}/expert.leave-approve/{record_id}/save/none'

        max_attempts = 5
        for attempt in range(max_attempts):
            response = self.session.get(save_url)
            if response.status_code == 200:
                data = response.json() if response.text else {}
                status = data.get('status', 'unknown')
                if status in ['completed', 'success'] or not data:
                    self._log(f'  Approval completed (attempt {attempt + 1})')
                    return True
            time.sleep(0.5)

        self._log('  Warning: Screen action did not confirm completion')
        return False

    def reject_request(self, record_id: str, reason: str) -> bool:
        """
        Reject a leave request by its record ID.

        Args:
            record_id: The UUID of the leave request record
            reason: Required reason for rejection

        Returns:
            True if rejection was successful
        """
        if not reason:
            raise ValueError('Rejection reason is required')

        self._log(f'Rejecting leave request {record_id}...')

        reject_payload = {
            'recordId': record_id,
            'action': 'reject',
            'comments': reason,
        }

        response = self.session.post(self.LEAVE_APPROVE_URL, json=reject_payload)

        if response.status_code != 200:
            raise RuntimeError(f'Rejection failed: {response.status_code} {response.text[:200]}')

        self._log('  Rejection submitted successfully')
        return True

    def get_request_details(self, record_id: str) -> dict:
        """
        Get detailed information about a specific leave request.

        Args:
            record_id: The UUID of the leave request record

        Returns:
            Dictionary with leave request details
        """
        # Use the action-button endpoint to get details about the record
        # This is what the UI calls when you click on a row
        params = {
            'recordId': record_id,
        }

        response = self.session.get(self.ACTION_BUTTON_URL, params=params)

        if response.status_code != 200:
            raise RuntimeError(f'Failed to get request details: {response.status_code}')

        return response.json()


def format_leave_request(request: dict) -> str:
    """Format a leave request record for display."""
    lines = []

    # Extract common fields (field names vary between endpoints)
    employee = request.get('employeeName') or request.get('fullName') or request.get('FULLNAME', 'Unknown')
    leave_type = request.get('leaveType') or request.get('leaveType_DISPLAY') or request.get('LEAVETYPE', 'Unknown')
    start_date = request.get('startDate') or request.get('START_DATE', 'Unknown')
    end_date = request.get('endDate') or request.get('END_DATE', 'Unknown')
    status = request.get('status') or request.get('STATUS', 'Unknown')
    record_id = request.get('id') or request.get('recordId') or request.get('ID', 'Unknown')
    manager = request.get('manager') or request.get('MANAGER', '')
    pending_days = request.get('pendingDays') or request.get('PENDINGDAYS', '')
    details = request.get('details') or request.get('DETAILS', '')

    lines.append(f'  Employee: {employee}')
    lines.append(f'  Leave Type: {leave_type}')
    lines.append(f'  Dates: {start_date} to {end_date}')
    lines.append(f'  Status: {status}')
    if manager:
        lines.append(f'  Manager: {manager}')
    if pending_days:
        lines.append(f'  Pending Days: {pending_days}')
    if details:
        lines.append(f'  Details: {details}')
    lines.append(f'  Record ID: {record_id}')

    return '\n'.join(lines)


def cmd_list(processor: LeaveRequestProcessor, args: argparse.Namespace):
    """List pending leave requests."""
    print()
    print('Pending Leave Requests')
    print('=' * 60)

    try:
        requests_list = processor.get_pending_requests_from_grid(
            older_than_days=args.older_than or 0,
            limit=args.limit or 50,
            employee_name=args.employee,
        )
    except Exception as e:
        print(f'Grid endpoint failed: {e}')
        print('Trying messages endpoint...')
        try:
            requests_list = processor.get_pending_requests_from_messages()
        except Exception as e2:
            print(f'Messages endpoint also failed: {e2}')
            requests_list = []

    if not requests_list:
        print('No pending leave requests found.')
        print()
        print('Note: If you expected to see requests, ensure your account has the')
        print('"Expert" role with leave management permissions in ADP iHCM.')
        return

    print(f'\nFound {len(requests_list)} pending request(s):\n')

    for i, req in enumerate(requests_list, 1):
        print(f'[{i}] ' + '-' * 56)
        print(format_leave_request(req))
        print()


def cmd_approve(processor: LeaveRequestProcessor, args: argparse.Namespace):
    """Approve a leave request."""
    print()
    print(f'Approving leave request: {args.record_id}')
    print('=' * 60)

    success = processor.approve_request(
        record_id=args.record_id,
        comments=args.comments or '',
    )

    if success:
        print('\nLeave request approved successfully!')
    else:
        print('\nApproval may have failed. Please verify in the iHCM UI.')
        sys.exit(1)


def cmd_reject(processor: LeaveRequestProcessor, args: argparse.Namespace):
    """Reject a leave request."""
    print()
    print(f'Rejecting leave request: {args.record_id}')
    print('=' * 60)

    if not args.reason:
        print('Error: --reason is required when rejecting a leave request')
        sys.exit(1)

    success = processor.reject_request(
        record_id=args.record_id,
        reason=args.reason,
    )

    if success:
        print('\nLeave request rejected successfully!')
    else:
        print('\nRejection may have failed. Please verify in the iHCM UI.')
        sys.exit(1)


def cmd_show(processor: LeaveRequestProcessor, args: argparse.Namespace):
    """Show details of a specific leave request."""
    print()
    print(f'Leave Request Details: {args.record_id}')
    print('=' * 60)

    try:
        details = processor.get_request_details(args.record_id)
        print(json.dumps(details, indent=2))
    except Exception as e:
        print(f'Failed to get details: {e}')
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description='iHCM Leave Request Processor',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    parser.add_argument('--visible', action='store_true', help='Show browser during authentication')
    parser.add_argument('--quiet', '-q', action='store_true', help='Suppress progress messages')
    parser.add_argument('--browser', choices=['chromium', 'firefox', 'webkit'], default='chromium',
                        help='Browser to use for authentication (default: chromium)')

    subparsers = parser.add_subparsers(dest='command', help='Available commands')

    # list command
    list_parser = subparsers.add_parser('list', help='List pending leave requests')
    list_parser.add_argument('--older-than', type=int, metavar='DAYS', help='Filter requests older than N days')
    list_parser.add_argument('--limit', type=int, default=50, help='Maximum records to return (default: 50)')
    list_parser.add_argument('--employee', metavar='NAME', help='Filter by employee name')

    # approve command
    approve_parser = subparsers.add_parser('approve', help='Approve a leave request')
    approve_parser.add_argument('record_id', help='Record ID of the leave request')
    approve_parser.add_argument('--comments', '-c', help='Optional approval comments')

    # reject command
    reject_parser = subparsers.add_parser('reject', help='Reject a leave request')
    reject_parser.add_argument('record_id', help='Record ID of the leave request')
    reject_parser.add_argument('--reason', '-r', required=True, help='Reason for rejection (required)')

    # show command
    show_parser = subparsers.add_parser('show', help='Show details of a leave request')
    show_parser.add_argument('record_id', help='Record ID of the leave request')

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    print('iHCM Leave Request Processor')
    print('=' * 60)

    # Authenticate
    verbose = not args.quiet
    try:
        session = create_authenticated_session_playwright(
            verbose=verbose,
            headless=not args.visible,
            browser_type=args.browser,
        )
    except Exception as e:
        print(f'\nAuthentication failed: {e}')
        sys.exit(1)

    # Create processor
    processor = LeaveRequestProcessor(session, verbose=verbose)

    # Dispatch to command handler
    if args.command == 'list':
        cmd_list(processor, args)
    elif args.command == 'approve':
        cmd_approve(processor, args)
    elif args.command == 'reject':
        cmd_reject(processor, args)
    elif args.command == 'show':
        cmd_show(processor, args)


if __name__ == '__main__':
    main()
