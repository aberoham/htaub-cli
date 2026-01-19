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
iHCM Authentication Module

Handles programmatic authentication to ADP iHCM using credentials from 1Password.
Uses Playwright for browser-based authentication to handle the SiteMinder SSO flow,
then extracts the bearer token for use with requests.

Usage:
    from ihcm_auth import create_authenticated_session

    session = create_authenticated_session()
    # session is now ready for API calls to ihcm.adp.com
"""

import base64
import getpass
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# Load .env file if python-dotenv is available
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Try to import playwright and playwright-stealth - required for browser auth
try:
    from playwright.sync_api import Browser, BrowserContext, Playwright, sync_playwright
    from playwright.sync_api import TimeoutError as PlaywrightTimeout
    from playwright_stealth import Stealth
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False
    # Define dummy types for type hints when playwright not installed
    Browser = type(None)
    BrowserContext = type(None)
    Playwright = type(None)


# Debug log file - captures full request/response details
DEBUG_LOG_FILE = Path('auth_debug.log')


class DebugLogger:
    """Logs all HTTP request/response details to a file for debugging."""

    def __init__(self, filepath: Path = DEBUG_LOG_FILE):
        self.filepath = filepath
        self.enabled = False
        self._file = None

    def enable(self):
        self.enabled = True
        self._file = open(self.filepath, 'w', encoding='utf-8')
        self._write('=== Authentication Debug Log ===')
        self._write(f'Started: {datetime.now().isoformat()}')
        self._write('')

    def disable(self):
        if self._file:
            self._file.close()
            self._file = None
        self.enabled = False

    def _write(self, text: str):
        if self._file:
            self._file.write(text + '\n')
            self._file.flush()

    def log_section(self, title: str):
        if not self.enabled:
            return
        self._write('')
        self._write('=' * 80)
        self._write(f'  {title}')
        self._write('=' * 80)

    def log_cookies(self, session: requests.Session, label: str = 'Current Cookies'):
        if not self.enabled:
            return
        self._write(f'\n--- {label} ---')
        for cookie in session.cookies:
            self._write(f'  {cookie.name}:')
            self._write(f'    value: {cookie.value[:100]}{"..." if len(cookie.value) > 100 else ""}')
            self._write(f'    domain: {cookie.domain}')
            self._write(f'    path: {cookie.path}')
            self._write(f'    secure: {cookie.secure}')

    def log_request(self, method: str, url: str, headers: dict, body=None):
        if not self.enabled:
            return
        self._write(f'\n>>> REQUEST: {method} {url}')
        self._write('--- Request Headers ---')
        for k, v in headers.items():
            # Truncate long values but show enough to debug
            v_str = str(v)
            if len(v_str) > 200:
                v_str = v_str[:200] + '...'
            self._write(f'  {k}: {v_str}')
        if body:
            self._write('--- Request Body ---')
            if isinstance(body, dict):
                # Mask password
                safe_body = body.copy()
                if 'response' in safe_body and isinstance(safe_body['response'], dict):
                    if 'password' in safe_body['response']:
                        safe_body['response'] = safe_body['response'].copy()
                        safe_body['response']['password'] = '***MASKED***'
                self._write(json.dumps(safe_body, indent=2))
            else:
                self._write(str(body)[:500])

    def log_response(self, response: requests.Response):
        if not self.enabled:
            return
        self._write(f'\n<<< RESPONSE: {response.status_code} {response.reason}')
        self._write(f'    Final URL: {response.url}')
        self._write('--- Response Headers ---')
        for k, v in response.headers.items():
            self._write(f'  {k}: {v}')
        self._write('--- Response Body ---')
        content_type = response.headers.get('Content-Type', '')
        if 'json' in content_type:
            try:
                self._write(json.dumps(response.json(), indent=2))
            except Exception:
                self._write(response.text[:2000])
        elif 'html' in content_type:
            self._write(f'[HTML Response - {len(response.text)} chars]')
            self._write(response.text[:1000])
            if len(response.text) > 1000:
                self._write('... [truncated]')
        else:
            self._write(response.text[:2000] if response.text else '[empty]')

    def log_redirect_history(self, response: requests.Response):
        if not self.enabled:
            return
        if response.history:
            self._write('\n--- Redirect History ---')
            for i, r in enumerate(response.history):
                self._write(f'  {i+1}. {r.status_code} {r.url}')
                location = r.headers.get('Location', 'N/A')
                self._write(f'      -> Location: {location}')


# Global debug logger instance
debug_log = DebugLogger()


# ADP authentication endpoints
AUTH_BASE_URL = 'https://online.emea.adp.com'
IHCM_BASE_URL = 'https://ihcm.adp.com'

# iHCM product ID (from the login URL)
IHCM_PRODUCT_ID = 'b376f1f2-a35a-025b-e053-f282530b8ccb'

# 1Password item name for credentials (can be overridden via .env or environment variable)
ONEPASSWORD_ITEM = os.environ.get('ONEPASSWORD_ITEM', 'ADP IHCM')


class SessionCache:
    """
    Persists authenticated session state to disk for reuse across script invocations.

    The cache stores bearer tokens, cookies, and timestamps, allowing subsequent runs
    to skip the Playwright authentication flow when the session is still valid.
    """

    CACHE_DIR = Path.home() / '.cache' / 'ihcm'
    CACHE_FILE = CACHE_DIR / 'session.json'

    def __init__(self, verbose: bool = True):
        self.verbose = verbose

    def _log(self, message: str):
        if self.verbose:
            print(message)

    def save(
        self,
        bearer_token: str,
        cookies: list[dict],
        xsrf_token: str | None,
    ) -> None:
        """Save session state to the cache file."""
        self.CACHE_DIR.mkdir(parents=True, exist_ok=True)

        cache_data = {
            'bearer_token': bearer_token,
            'cookies': cookies,
            'xsrf_token': xsrf_token,
            'created_at': datetime.now().isoformat(),
            'last_validated': datetime.now().isoformat(),
        }

        self.CACHE_FILE.write_text(json.dumps(cache_data, indent=2))
        self.CACHE_FILE.chmod(0o600)  # Restrict permissions since it contains auth tokens
        self._log(f'  Session cached to {self.CACHE_FILE}')

    def load(self) -> dict | None:
        """Load cached session data if it exists and has required fields."""
        if not self.CACHE_FILE.exists():
            return None

        try:
            data = json.loads(self.CACHE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            self._log('  Cache file corrupted, will re-authenticate')
            self.clear()
            return None

        required_fields = ['bearer_token', 'cookies', 'created_at']
        if not all(field in data for field in required_fields):
            self._log('  Cache missing required fields, will re-authenticate')
            self.clear()
            return None

        return data

    def update_last_validated(self) -> None:
        """Update the last_validated timestamp in the cache."""
        data = self.load()
        if data:
            data['last_validated'] = datetime.now().isoformat()
            self.CACHE_FILE.write_text(json.dumps(data, indent=2))

    def clear(self) -> None:
        """Delete the cache file."""
        if self.CACHE_FILE.exists():
            self.CACHE_FILE.unlink()
            self._log('  Session cache cleared')

    def is_token_expired(self, bearer_token: str, buffer_minutes: int = 5) -> bool:
        """
        Check if a JWT bearer token is expired or will expire soon.

        Decodes the JWT payload (without verification) to read the exp claim.
        Returns True if the token will expire within buffer_minutes.
        """
        try:
            # JWT format: header.payload.signature
            parts = bearer_token.split('.')
            if len(parts) != 3:
                return True

            # Decode the payload (middle part), adding padding as needed
            payload_b64 = parts[1]
            padding = 4 - len(payload_b64) % 4
            if padding != 4:
                payload_b64 += '=' * padding

            payload_json = base64.urlsafe_b64decode(payload_b64)
            payload = json.loads(payload_json)

            exp_timestamp = payload.get('exp')
            if not exp_timestamp:
                # No expiry claim, assume it's valid but we'll validate with API
                return False

            # Check if token expires within buffer_minutes
            exp_datetime = datetime.fromtimestamp(exp_timestamp)
            buffer_seconds = buffer_minutes * 60
            now = datetime.now()

            if exp_datetime.timestamp() - now.timestamp() < buffer_seconds:
                self._log(f'  Token expires at {exp_datetime}, within {buffer_minutes}min buffer')
                return True

            return False

        except (ValueError, KeyError, json.JSONDecodeError):
            # If we can't decode the token, consider it potentially expired
            return True


def is_1password_available() -> bool:
    """Check if the 1Password CLI (op) is installed and available."""
    return shutil.which('op') is not None


def get_credentials_from_prompt() -> tuple[str, str]:
    """
    Prompt the user to enter their credentials manually.

    Returns (username, password) tuple.
    """
    print()
    print('Please enter your ADP iHCM credentials:')
    username = input('  Username (email): ').strip()
    password = getpass.getpass('  Password: ')

    if not username or not password:
        raise ValueError('Username and password are required')

    return username, password


def get_credentials_from_1password(item_name: str = ONEPASSWORD_ITEM) -> tuple[str, str]:
    """
    Retrieve username and password from 1Password using the op CLI.

    Returns (username, password) tuple.
    """
    try:
        # Get username
        username_result = subprocess.run(
            ['op', 'item', 'get', item_name, '--fields', 'username'],
            capture_output=True,
            text=True,
            check=True,
        )
        username = username_result.stdout.strip()

        # Get password (requires --reveal for secret fields)
        password_result = subprocess.run(
            ['op', 'item', 'get', item_name, '--fields', 'password', '--reveal'],
            capture_output=True,
            text=True,
            check=True,
        )
        password = password_result.stdout.strip()

        if not username or not password:
            raise ValueError(f'Empty credentials retrieved from 1Password item "{item_name}"')

        return username, password

    except subprocess.CalledProcessError as e:
        raise RuntimeError(
            f'Failed to get credentials from 1Password: {e.stderr}\n'
            f'Make sure you are signed into 1Password CLI (run: op signin)'
        ) from e
    except FileNotFoundError as e:
        raise RuntimeError(
            '1Password CLI (op) not found. Please install it:\n'
            '  brew install 1password-cli'
        ) from e


def get_credentials(item_name: str = ONEPASSWORD_ITEM, verbose: bool = True) -> tuple[str, str]:
    """
    Get credentials, trying 1Password first and falling back to manual prompt.

    This is the recommended way to get credentials as it handles both cases:
    - Users with 1Password CLI configured get seamless authentication
    - Users without 1Password can still use the scripts by entering credentials manually

    Returns (username, password) tuple.
    """
    if is_1password_available():
        if verbose:
            print(f'Getting credentials from 1Password ({item_name})...')
        try:
            return get_credentials_from_1password(item_name)
        except RuntimeError as e:
            if verbose:
                print(f'  Warning: {e}')
                print('  Falling back to manual credential entry...')
            return get_credentials_from_prompt()
    else:
        if verbose:
            print('1Password CLI not found. You can install it for automatic credential management.')
            print('  See: https://developer.1password.com/docs/cli/get-started/')
        return get_credentials_from_prompt()


def create_session() -> requests.Session:
    """Create a requests session with retry logic and connection pooling."""
    session = requests.Session()

    retry_strategy = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=['POST', 'GET'],
    )
    adapter = HTTPAdapter(
        max_retries=retry_strategy,
        pool_connections=10,
        pool_maxsize=10,
    )
    session.mount('https://', adapter)

    # Set common headers matching browser behavior
    session.headers.update({
        'Accept': 'application/json, text/plain, */*',
        'Accept-Language': 'en-GB,en;q=0.9',
        'Content-Type': 'application/json',
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
        'Origin': AUTH_BASE_URL,
        'Referer': f'{AUTH_BASE_URL}/signin/v1',
        'Connection': 'keep-alive',
    })

    return session


def get_csrf_token(session: requests.Session) -> str:
    """Fetch CSRF token from the auth server (returned as a cookie)."""
    debug_log.log_section('GET CSRF TOKEN')
    url = f'{AUTH_BASE_URL}/csrf'
    debug_log.log_request('GET', url, dict(session.headers))

    response = session.get(url)

    debug_log.log_response(response)
    debug_log.log_cookies(session, 'Cookies after CSRF')
    response.raise_for_status()

    csrf_token = session.cookies.get('XSRF-TOKEN')
    if not csrf_token:
        raise RuntimeError(f'No XSRF-TOKEN cookie set. Cookies: {dict(session.cookies)}')

    return csrf_token


def start_signin(session: requests.Session, csrf_token: str) -> str:
    """
    Initialize the sign-in flow.

    Returns the session token (JWT) for subsequent requests.
    """
    debug_log.log_section('START SIGN-IN')
    session.headers['X-XSRF-TOKEN'] = csrf_token

    payload = {
        'appId': 'IHCM',
        'productId': IHCM_PRODUCT_ID,
        'returnUrl': f'{IHCM_BASE_URL}/whrmux/web/me/home',
        'callingAppId': 'IHCM',
    }

    url = f'{AUTH_BASE_URL}/api/sign-in-service/v1/sign-in.start'
    debug_log.log_request('POST', url, dict(session.headers), payload)

    response = session.post(url, json=payload)

    debug_log.log_response(response)
    debug_log.log_cookies(session, 'Cookies after start')
    response.raise_for_status()

    data = response.json()
    session_token = data.get('session')

    if not session_token:
        raise RuntimeError(f'No session token in start response: {data}')

    return session_token


def identify_account(session: requests.Session, username: str, session_token: str) -> str:
    """
    Submit username to identify the account.

    Returns the updated session token.
    """
    debug_log.log_section('IDENTIFY ACCOUNT')
    payload = {
        'session': session_token,
        'userId': username,
    }

    url = f'{AUTH_BASE_URL}/api/sign-in-service/v1/sign-in.account.identify'
    debug_log.log_request('POST', url, dict(session.headers), payload)

    response = session.post(url, json=payload)

    debug_log.log_response(response)
    debug_log.log_cookies(session, 'Cookies after identify')
    response.raise_for_status()

    # The identify endpoint may return an empty body or updated session info
    # The session cookie is what matters for the next step
    if response.text:
        try:
            data = response.json()
            # If there's a new session token, use it
            if 'session' in data:
                return data['session']
        except ValueError:
            pass

    return session_token


def respond_to_password_challenge(
    session: requests.Session,
    password: str,
    session_token: str,
) -> str | None:
    """
    Submit password to complete authentication.

    Returns the redirect URL to iHCM on success. The auth server may return a JSON
    response with redirectUrl, or an empty response with session cookies set.
    """
    debug_log.log_section('PASSWORD CHALLENGE')
    payload = {
        'response': {
            'type': 'PASSWORD_VERIFICATION_RESPONSE',
            'password': password,
            'locale': 'en_US',
        },
        'session': session_token,
    }

    url = f'{AUTH_BASE_URL}/api/sign-in-service/v1/sign-in.challenge.respond'
    debug_log.log_request('POST', url, dict(session.headers), payload)

    response = session.post(url, json=payload)

    debug_log.log_response(response)
    debug_log.log_redirect_history(response)
    debug_log.log_cookies(session, 'Cookies after challenge')
    response.raise_for_status()

    # Response may be empty (just sets cookies) or JSON with redirect info
    if not response.text.strip():
        # Empty response is OK - session cookies should be set
        return None

    try:
        data = response.json()
    except ValueError:
        # Non-JSON response, likely a redirect page - that's fine
        return None

    # Check for authentication errors
    if data.get('status') == 'FAILED' or 'error' in data:
        error_msg = data.get('message', data.get('error', 'Unknown error'))
        raise RuntimeError(f'Authentication failed: {error_msg}')

    # Get the redirect URL to iHCM if provided
    return data.get('redirectUrl')


def get_ihcm_bearer_token(session: requests.Session) -> str:
    """
    Exchange the session cookie for a JWT bearer token.

    After authenticating to online.emea.adp.com, the k8Ksj346 session cookie is set.
    The iHCM frontend exchanges this for a bearer token by calling /whrmux/webapi/token.
    This token is required for all subsequent API calls.
    """
    debug_log.log_section('GET BEARER TOKEN')
    debug_log.log_cookies(session, 'Cookies before token request')

    url = f'{IHCM_BASE_URL}/whrmux/webapi/token'
    debug_log.log_request('POST', url, dict(session.headers))

    response = session.post(url)

    debug_log.log_response(response)
    debug_log.log_redirect_history(response)
    debug_log.log_cookies(session, 'Cookies after token request')

    if response.status_code == 401:
        raise RuntimeError(
            'Token exchange failed (401). Session cookie may not have been set correctly.'
        )

    response.raise_for_status()

    # Handle empty or non-JSON responses
    if not response.text.strip():
        raise RuntimeError('Token endpoint returned empty response')

    # Check if we got redirected to login page (HTML response)
    if response.text.strip().startswith('<!doctype') or response.text.strip().startswith('<html'):
        raise RuntimeError(
            f'Token endpoint returned HTML (login page). '
            f'Final URL: {response.url}. Session may not be valid for iHCM.'
        )

    data = response.json()

    bearer_token = data.get('access_token')
    if not bearer_token:
        raise RuntimeError(f'No access_token in token response: {data}')

    return bearer_token


def complete_sso_authorization(session: requests.Session) -> None:
    """
    Complete the SiteMinder SSO flow by calling the authorization endpoint.

    After authenticating via sign-in-service, we need to establish the session
    with the SiteMinder gateway that protects ihcm.adp.com. This is done by
    calling the authorization endpoint which validates our session and sets
    the necessary cookies for accessing ihcm.adp.com.
    """
    debug_log.log_section('COMPLETE SSO AUTHORIZATION')

    # The authorization endpoint validates our session and redirects to the target app
    authorize_url = (
        f'{AUTH_BASE_URL}/api/authorization-service/v1/authorize'
        f'?APPID=IHCM'
        f'&productId={IHCM_PRODUCT_ID}'
        f'&returnURL={IHCM_BASE_URL}/whrmux/web/me/home'
        f'&callingAppId=IHCM'
        f'&TARGET=-SM-{IHCM_BASE_URL}/whrmux/web/me/home'
    )

    debug_log.log_request('GET', authorize_url, dict(session.headers))

    response = session.get(authorize_url, allow_redirects=True)

    debug_log.log_response(response)
    debug_log.log_redirect_history(response)
    debug_log.log_cookies(session, 'Cookies after authorization')


def establish_ihcm_session(session: requests.Session, redirect_url: str = None) -> None:
    """
    Follow the redirect to iHCM and establish the session.

    This involves:
    1. Complete SSO authorization to establish SiteMinder session
    2. Exchange the session cookie for a JWT bearer token via /webapi/token
    """
    debug_log.log_section('ESTABLISH iHCM SESSION')

    # First, complete the SSO authorization flow
    complete_sso_authorization(session)

    # Update headers for iHCM domain
    session.headers['Origin'] = IHCM_BASE_URL
    session.headers['Referer'] = f'{IHCM_BASE_URL}/whrmux/web/'

    # Update XSRF token from cookies (may have been updated during redirect)
    xsrf_token = session.cookies.get('XSRF-TOKEN')
    if xsrf_token:
        session.headers['X-XSRF-TOKEN'] = xsrf_token

    # Verify we have the session cookie
    if not session.cookies.get('k8Ksj346'):
        raise RuntimeError(
            'Session cookie (k8Ksj346) not set. Authentication may have failed.'
        )

    # Now try to access iHCM to see if SSO is working
    debug_log.log_section('ACCESS iHCM HOME')
    home_url = f'{IHCM_BASE_URL}/whrmux/web/me/home'
    debug_log.log_request('GET', home_url, dict(session.headers))

    response = session.get(home_url, allow_redirects=True)

    debug_log.log_response(response)
    debug_log.log_redirect_history(response)
    debug_log.log_cookies(session, 'Cookies after accessing iHCM home')

    # Exchange session cookie for bearer token - this is the key step the browser does
    bearer_token = get_ihcm_bearer_token(session)

    # Configure session for API calls with the bearer token
    session.headers.update({
        'Accept': 'application/json, text/plain, */*',
        'Content-Type': 'application/json;charset=UTF-8',
        'X-Requested-With': 'XMLHttpRequest',
        'Origin': IHCM_BASE_URL,
        'Referer': f'{IHCM_BASE_URL}/whrmux/web/me/directory/',
        'Authorization': f'Bearer {bearer_token}',
    })


def authenticate(
    username: str, password: str, verbose: bool = True, debug: bool = False
) -> requests.Session:
    """
    Perform full authentication flow to ADP iHCM.

    Args:
        username: ADP username (email)
        password: ADP password
        verbose: Print progress messages to stdout
        debug: Write detailed debug log to auth_debug.log

    Returns an authenticated requests.Session ready for API calls.
    """
    if debug:
        debug_log.enable()
        print(f'  Debug logging enabled: {DEBUG_LOG_FILE}')

    try:
        if verbose:
            print('Authenticating to ADP iHCM...')

        session = create_session()

        # Step 1: Get CSRF token
        if verbose:
            print('  Getting CSRF token...')
        csrf_token = get_csrf_token(session)

        # Step 2: Start sign-in flow
        if verbose:
            print('  Starting sign-in flow...')
        session_token = start_signin(session, csrf_token)

        # Step 3: Identify account with username
        if verbose:
            print('  Identifying account...')
        session_token = identify_account(session, username, session_token)

        # Step 4: Submit password
        if verbose:
            print('  Submitting credentials...')
        redirect_url = respond_to_password_challenge(session, password, session_token)

        # Step 5: Follow redirect to iHCM and exchange session for bearer token
        if verbose:
            print('  Following redirect to iHCM...')
        establish_ihcm_session(session, redirect_url)

        if verbose:
            print('  Bearer token acquired!')
            print('  Authentication successful!')

        return session

    finally:
        if debug:
            debug_log.disable()
            print(f'  Debug log written to: {DEBUG_LOG_FILE}')


def create_authenticated_session(
    item_name: str = ONEPASSWORD_ITEM,
    verbose: bool = True,
    debug: bool = False,
) -> requests.Session:
    """
    Create an authenticated session using credentials from 1Password or manual entry.

    This is the main entry point for other scripts.

    Args:
        item_name: Name of the 1Password item containing credentials
        verbose: Whether to print progress messages
        debug: Whether to write detailed debug log to auth_debug.log

    Returns:
        An authenticated requests.Session ready for iHCM API calls
    """
    username, password = get_credentials(item_name, verbose=verbose)

    if verbose:
        print(f'  Username: {username}')

    return authenticate(username, password, verbose=verbose, debug=debug)


def authenticate_with_playwright(
    username: str,
    password: str,
    verbose: bool = True,
    headless: bool = True,
    timeout: int = 60000,
    browser_type: str = 'chromium',
) -> requests.Session:
    """
    Authenticate to iHCM using Playwright browser automation.

    This approach handles the full SSO flow including JavaScript-based token exchange
    that cannot be replicated with Python requests alone.

    Args:
        username: ADP username (email)
        password: ADP password
        verbose: Print progress messages
        headless: Run browser in headless mode
        timeout: Timeout in milliseconds for page operations
        browser_type: Browser to use ('chromium', 'firefox', or 'webkit')

    Returns:
        An authenticated requests.Session with bearer token and cookies
    """
    if not PLAYWRIGHT_AVAILABLE:
        raise RuntimeError(
            'Playwright and playwright-stealth are required for browser-based authentication.\n'
            'Install with: uv pip install playwright playwright-stealth && uv run playwright install chromium'
        )

    if verbose:
        print(f'Starting browser-based authentication ({browser_type})...')

    with sync_playwright() as p:
        browser_launcher = getattr(p, browser_type, p.chromium)
        # Use slow_mo to add delays between Playwright actions - helps with browser stability
        browser = browser_launcher.launch(headless=headless, slow_mo=100)
        context = browser.new_context(
            user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
            viewport={'width': 1280, 'height': 800},
        )
        page = context.new_page()

        # Apply stealth mode to evade bot detection
        if verbose:
            print('  Applying stealth mode...')
        Stealth().apply_stealth_sync(page)

        try:
            # Navigate to iHCM - let the full redirect chain complete
            if verbose:
                print('  Navigating to iHCM (following SSO redirects)...')

            page.goto(
                f'{IHCM_BASE_URL}/whrmux/web/me/home',
                wait_until='networkidle',
                timeout=timeout,
            )

            # Wait for page to fully stabilize after navigation
            time.sleep(10)

            # Wait for username field to appear - this is the definitive sign we're on the login page
            if verbose:
                print('  Waiting for login form...')

            username_input = page.wait_for_selector(
                'input[name="userId"], input[name="user"], input[type="email"], input[type="text"]',
                state='visible',
                timeout=timeout,
            )

            if verbose:
                print('  Entering username...')
            username_input.fill(username)

            # Click Next and wait for password field to appear
            if verbose:
                print('  Clicking Next...')

            # Give the page a moment to react to username input
            time.sleep(2)

            try:
                # Try multiple selectors for the Next button
                next_btn = page.locator('text=Next').first
                next_btn.wait_for(state='visible', timeout=timeout)
                next_btn.click()
            except PlaywrightTimeout:
                # Save debug screenshot on failure
                screenshot_path = Path('auth_failure_next_btn.png')
                page.screenshot(path=str(screenshot_path))
                if verbose:
                    print(f'  Debug screenshot saved to {screenshot_path}')
                    print(f'  Current URL: {page.url}')
                raise

            # Wait for page transition after clicking Next
            time.sleep(5)

            # Wait for password field - this confirms we've moved to the password step
            if verbose:
                print('  Waiting for password field...')

            password_input = page.wait_for_selector(
                'input[type="password"]',
                state='visible',
                timeout=timeout,
            )

            if verbose:
                print('  Entering password...')
            password_input.fill(password)

            # Submit and wait for navigation back to iHCM
            if verbose:
                print('  Submitting login...')

            submit_btn = page.query_selector(
                'button[type="submit"], button:has-text("Sign In"), button:has-text("Login")'
            )
            if submit_btn:
                submit_btn.click()
            else:
                password_input.press('Enter')

            # Wait for login to process and redirect
            time.sleep(10)

            # Wait for successful redirect to iHCM by checking for the bearer token
            if verbose:
                print('  Waiting for authentication to complete...')

            # Poll for bearer token in sessionStorage - this is the definitive sign of success
            bearer_token = None
            for _ in range(30):  # Up to 30 seconds
                time.sleep(1)
                try:
                    bearer_token = page.evaluate('() => sessionStorage.getItem("iHcmBearerToken")')
                    if bearer_token:
                        break
                except Exception:
                    # Page might still be navigating
                    pass

            if not bearer_token:
                raise RuntimeError(
                    f'Login did not complete successfully. Final URL: {page.url}'
                )

            if verbose:
                print('  Bearer token acquired!')

            # Extract all cookies from browser context
            if verbose:
                print('  Extracting cookies...')
            cookies = context.cookies()

            xsrf_token = None
            for cookie in cookies:
                if cookie['name'] == 'XSRF-TOKEN':
                    xsrf_token = cookie['value']
                    break

            if verbose:
                print(f'  Extracted {len(cookies)} cookies')

        except PlaywrightTimeout as e:
            raise RuntimeError(f'Authentication timed out: {e}') from e
        except Exception as e:
            error_msg = str(e)
            if 'Page crashed' in error_msg or 'browser has disconnected' in error_msg.lower():
                raise RuntimeError(
                    f'Browser crashed during authentication. This may indicate the browser '
                    f'is not properly installed. Try running:\n'
                    f'  uv run playwright install {browser_type}\n\n'
                    f'Original error: {e}'
                ) from e
            raise RuntimeError(f'Authentication failed: {e}') from e
        finally:
            browser.close()

    # Build a requests session with the extracted auth
    if verbose:
        print('  Configuring session...')

    session = create_session()

    # Add cookies to session
    for cookie in cookies:
        session.cookies.set(
            cookie['name'],
            cookie['value'],
            domain=cookie.get('domain', ''),
            path=cookie.get('path', '/'),
        )

    # Configure headers for API calls
    session.headers.update({
        'Accept': 'application/json, text/plain, */*',
        'Content-Type': 'application/json;charset=UTF-8',
        'X-Requested-With': 'XMLHttpRequest',
        'Origin': IHCM_BASE_URL,
        'Referer': f'{IHCM_BASE_URL}/whrmux/web/me/directory/',
        'Authorization': f'Bearer {bearer_token}',
    })

    if xsrf_token:
        session.headers['X-XSRF-TOKEN'] = xsrf_token

    if verbose:
        print('  Browser authentication complete!')

    return session


def _reconstruct_session_from_cache(
    cache_data: dict,
    verbose: bool = True,
) -> requests.Session:
    """Reconstruct a requests.Session from cached authentication data."""
    session = create_session()

    # Add cookies to session
    for cookie in cache_data['cookies']:
        session.cookies.set(
            cookie['name'],
            cookie['value'],
            domain=cookie.get('domain', ''),
            path=cookie.get('path', '/'),
        )

    # Configure headers for API calls
    session.headers.update({
        'Accept': 'application/json, text/plain, */*',
        'Content-Type': 'application/json;charset=UTF-8',
        'X-Requested-With': 'XMLHttpRequest',
        'Origin': IHCM_BASE_URL,
        'Referer': f'{IHCM_BASE_URL}/whrmux/web/me/directory/',
        'Authorization': f'Bearer {cache_data["bearer_token"]}',
    })

    if cache_data.get('xsrf_token'):
        session.headers['X-XSRF-TOKEN'] = cache_data['xsrf_token']

    return session


def _validate_session(session: requests.Session, verbose: bool = True) -> bool:
    """
    Validate a session by making a lightweight API call.

    Returns True if the session is valid, False otherwise.
    """
    try:
        response = session.get(f'{IHCM_BASE_URL}/whrmux/webapi/api/data/me/home', timeout=10)
        if response.status_code == 200:
            return True
        if verbose:
            print(f'  Session validation failed: HTTP {response.status_code}')
        return False
    except requests.RequestException as e:
        if verbose:
            print(f'  Session validation failed: {e}')
        return False


class AuthResult:
    """
    Result of browser-based authentication.

    Contains both a requests.Session for API calls and optionally a browser context
    that can be used for operations requiring a live browser (e.g., PDF downloads).
    """

    def __init__(
        self,
        session: requests.Session,
        browser_context: 'BrowserContext | None' = None,
        playwright_instance: 'Playwright | None' = None,
        browser: 'Browser | None' = None,
    ):
        self.session = session
        self.browser_context = browser_context
        self._playwright = playwright_instance
        self._browser = browser

    def close_browser(self) -> None:
        """Close the browser and playwright instance if open."""
        if self._browser:
            self._browser.close()
            self._browser = None
            self.browser_context = None
        if self._playwright:
            self._playwright.stop()
            self._playwright = None

    def __enter__(self) -> 'AuthResult':
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close_browser()


def create_authenticated_session_playwright(
    item_name: str = ONEPASSWORD_ITEM,
    verbose: bool = True,
    headless: bool = True,
    use_cache: bool = True,
    browser_type: str = 'chromium',
) -> requests.Session:
    """
    Create an authenticated session using Playwright and 1Password credentials.

    This is the recommended entry point as it handles the full SSO flow correctly.
    When use_cache is True (default), attempts to reuse a previously cached session
    before falling back to fresh Playwright authentication.

    Args:
        item_name: Name of the 1Password item containing credentials
        verbose: Whether to print progress messages
        headless: Whether to run browser in headless mode
        use_cache: Whether to attempt using a cached session (default: True)
        browser_type: Browser to use ('chromium', 'firefox', or 'webkit')

    Returns:
        An authenticated requests.Session ready for iHCM API calls
    """
    cache = SessionCache(verbose=verbose)

    # Try to use cached session first
    if use_cache:
        if verbose:
            print('Checking for cached session...')

        cache_data = cache.load()
        if cache_data:
            if verbose:
                created = cache_data.get('created_at', 'unknown')
                print(f'  Found cached session from {created}')

            # Check JWT expiry as informational only - session cookies may still work
            token_expired = cache.is_token_expired(cache_data['bearer_token'])
            if token_expired and verbose:
                print('  Note: JWT token expired, but trying session anyway...')

            # Always try to validate - session cookies often have sliding expiration
            if verbose:
                print('  Validating cached session...')

            session = _reconstruct_session_from_cache(cache_data, verbose)

            if _validate_session(session, verbose):
                if verbose:
                    print('  Cached session is valid!')
                cache.update_last_validated()
                return session
            else:
                if verbose:
                    print('  Cached session invalid, will re-authenticate')
                cache.clear()
        else:
            if verbose:
                print('  No cached session found')

    # No valid cache, proceed with fresh authentication
    username, password = get_credentials(item_name, verbose=verbose)

    if verbose:
        print(f'  Username: {username}')

    session = authenticate_with_playwright(
        username, password, verbose=verbose, headless=headless, browser_type=browser_type
    )

    # Cache the new session for future use
    if use_cache:
        # Extract cookies and tokens from the session to save
        cookies = []
        for cookie in session.cookies:
            cookies.append({
                'name': cookie.name,
                'value': cookie.value,
                'domain': cookie.domain,
                'path': cookie.path,
            })

        bearer_token = session.headers.get('Authorization', '').replace('Bearer ', '')
        xsrf_token = session.headers.get('X-XSRF-TOKEN')

        cache.save(bearer_token, cookies, xsrf_token)

    return session


def _authenticate_with_browser_kept_alive(
    username: str,
    password: str,
    verbose: bool = True,
    headless: bool = True,
    timeout: int = 60000,
    browser_type: str = 'chromium',
) -> AuthResult:
    """
    Authenticate and return both session and live browser context.

    Unlike authenticate_with_playwright(), this keeps the browser running
    so it can be used for subsequent requests (e.g., PDF downloads) that
    require the SiteMinder SSO session.
    """
    if not PLAYWRIGHT_AVAILABLE:
        raise RuntimeError(
            'Playwright and playwright-stealth are required for browser-based authentication.\n'
            'Install with: uv pip install playwright playwright-stealth && uv run playwright install chromium'
        )

    if verbose:
        print(f'Starting browser-based authentication ({browser_type})...')

    p = sync_playwright().start()
    browser_launcher = getattr(p, browser_type, p.chromium)
    browser = browser_launcher.launch(headless=headless, slow_mo=100)
    context = browser.new_context(
        user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
        viewport={'width': 1280, 'height': 800},
    )
    page = context.new_page()

    # Apply stealth mode to evade bot detection
    if verbose:
        print('  Applying stealth mode...')
    Stealth().apply_stealth_sync(page)

    try:
        # Navigate to iHCM - let the full redirect chain complete
        if verbose:
            print('  Navigating to iHCM (following SSO redirects)...')

        page.goto(
            f'{IHCM_BASE_URL}/whrmux/web/me/home',
            wait_until='networkidle',
            timeout=timeout,
        )

        # Wait for page to fully stabilize after navigation
        time.sleep(10)

        # Wait for username field to appear
        if verbose:
            print('  Waiting for login form...')

        username_input = page.wait_for_selector(
            'input[name="userId"], input[name="user"], input[type="email"], input[type="text"]',
            state='visible',
            timeout=timeout,
        )

        if verbose:
            print('  Entering username...')
        username_input.fill(username)

        # Click Next and wait for password field
        if verbose:
            print('  Clicking Next...')

        time.sleep(2)

        try:
            next_btn = page.locator('text=Next').first
            next_btn.wait_for(state='visible', timeout=timeout)
            next_btn.click()
        except PlaywrightTimeout:
            screenshot_path = Path('auth_failure_next_btn.png')
            page.screenshot(path=str(screenshot_path))
            if verbose:
                print(f'  Debug screenshot saved to {screenshot_path}')
                print(f'  Current URL: {page.url}')
            raise

        time.sleep(5)

        # Wait for password field
        if verbose:
            print('  Waiting for password field...')

        password_input = page.wait_for_selector(
            'input[type="password"]',
            state='visible',
            timeout=timeout,
        )

        if verbose:
            print('  Entering password...')
        password_input.fill(password)

        # Submit and wait for navigation back to iHCM
        if verbose:
            print('  Submitting login...')

        submit_btn = page.query_selector(
            'button[type="submit"], button:has-text("Sign In"), button:has-text("Login")'
        )
        if submit_btn:
            submit_btn.click()
        else:
            password_input.press('Enter')

        time.sleep(10)

        # Poll for bearer token in sessionStorage
        if verbose:
            print('  Waiting for authentication to complete...')

        bearer_token = None
        for _ in range(30):
            time.sleep(1)
            try:
                bearer_token = page.evaluate('() => sessionStorage.getItem("iHcmBearerToken")')
                if bearer_token:
                    break
            except Exception:
                pass

        if not bearer_token:
            raise RuntimeError(
                f'Login did not complete successfully. Final URL: {page.url}'
            )

        if verbose:
            print('  Bearer token acquired!')

        # Extract cookies from browser context
        if verbose:
            print('  Extracting cookies...')
        cookies = context.cookies()

        xsrf_token = None
        for cookie in cookies:
            if cookie['name'] == 'XSRF-TOKEN':
                xsrf_token = cookie['value']
                break

        if verbose:
            print(f'  Extracted {len(cookies)} cookies')

        # Close the page but keep context and browser alive
        page.close()

    except PlaywrightTimeout as e:
        browser.close()
        p.stop()
        raise RuntimeError(f'Authentication timed out: {e}') from e
    except Exception as e:
        browser.close()
        p.stop()
        error_msg = str(e)
        if 'Page crashed' in error_msg or 'browser has disconnected' in error_msg.lower():
            raise RuntimeError(
                f'Browser crashed during authentication. This may indicate the browser '
                f'is not properly installed. Try running:\n'
                f'  uv run playwright install {browser_type}\n\n'
                f'Original error: {e}'
            ) from e
        raise RuntimeError(f'Authentication failed: {e}') from e

    # Build a requests session with the extracted auth
    if verbose:
        print('  Configuring session...')

    session = create_session()

    for cookie in cookies:
        session.cookies.set(
            cookie['name'],
            cookie['value'],
            domain=cookie.get('domain', ''),
            path=cookie.get('path', '/'),
        )

    session.headers.update({
        'Accept': 'application/json, text/plain, */*',
        'Content-Type': 'application/json;charset=UTF-8',
        'X-Requested-With': 'XMLHttpRequest',
        'Origin': IHCM_BASE_URL,
        'Referer': f'{IHCM_BASE_URL}/whrmux/web/me/directory/',
        'Authorization': f'Bearer {bearer_token}',
    })

    if xsrf_token:
        session.headers['X-XSRF-TOKEN'] = xsrf_token

    if verbose:
        print('  Browser authentication complete (browser kept alive for PDF downloads)')

    return AuthResult(
        session=session,
        browser_context=context,
        playwright_instance=p,
        browser=browser,
    )


def create_authenticated_session_with_browser(
    item_name: str = ONEPASSWORD_ITEM,
    verbose: bool = True,
    headless: bool = True,
    use_cache: bool = True,
    browser_type: str = 'chromium',
) -> AuthResult:
    """
    Create an authenticated session with optional live browser context.

    This is designed for workflows that need both:
    1. A requests.Session for fast JSON API calls
    2. A live browser context for operations requiring SiteMinder SSO (e.g., PDF downloads)

    When the cache is valid, returns AuthResult with session only (no browser).
    When fresh auth is needed, returns AuthResult with both session and browser context.

    Use as a context manager to ensure browser cleanup:

        with create_authenticated_session_with_browser() as auth:
            # Use auth.session for API calls
            response = auth.session.get(api_url)

            # Use auth.browser_context for PDF downloads if available
            if auth.browser_context:
                page = auth.browser_context.new_page()
                # ... download PDF via browser

    Args:
        item_name: Name of the 1Password item containing credentials
        verbose: Whether to print progress messages
        headless: Whether to run browser in headless mode
        use_cache: Whether to attempt using a cached session (default: True)
        browser_type: Browser to use ('chromium', 'firefox', or 'webkit')

    Returns:
        AuthResult containing session and optionally browser_context
    """
    cache = SessionCache(verbose=verbose)

    # Try to use cached session first
    if use_cache:
        if verbose:
            print('Checking for cached session...')

        cache_data = cache.load()
        if cache_data:
            if verbose:
                created = cache_data.get('created_at', 'unknown')
                print(f'  Found cached session from {created}')

            # Check JWT expiry as informational only - session cookies may still work
            token_expired = cache.is_token_expired(cache_data['bearer_token'])
            if token_expired and verbose:
                print('  Note: JWT token expired, but trying session anyway...')

            # Always try to validate - session cookies often have sliding expiration
            if verbose:
                print('  Validating cached session...')

            session = _reconstruct_session_from_cache(cache_data, verbose)

            if _validate_session(session, verbose):
                if verbose:
                    print('  Cached session is valid!')
                cache.update_last_validated()
                # Return session-only AuthResult (no browser)
                return AuthResult(session=session)
            else:
                if verbose:
                    print('  Cached session invalid, will re-authenticate')
                cache.clear()
        else:
            if verbose:
                print('  No cached session found')

    # No valid cache, proceed with fresh authentication (keeping browser alive)
    username, password = get_credentials(item_name, verbose=verbose)

    if verbose:
        print(f'  Username: {username}')

    auth_result = _authenticate_with_browser_kept_alive(
        username, password, verbose=verbose, headless=headless, browser_type=browser_type
    )

    # Cache the new session for future use
    if use_cache:
        cookies = []
        for cookie in auth_result.session.cookies:
            cookies.append({
                'name': cookie.name,
                'value': cookie.value,
                'domain': cookie.domain,
                'path': cookie.path,
            })

        bearer_token = auth_result.session.headers.get('Authorization', '').replace('Bearer ', '')
        xsrf_token = auth_result.session.headers.get('X-XSRF-TOKEN')

        cache.save(bearer_token, cookies, xsrf_token)

    return auth_result


def test_authentication(
    debug: bool = True,
    use_playwright: bool = True,
    use_cache: bool = True,
) -> bool:
    """Test authentication by making a simple API call."""
    try:
        if use_playwright:
            session = create_authenticated_session_playwright(headless=True, use_cache=use_cache)
        else:
            session = create_authenticated_session(debug=debug)

        print()
        print('Testing API access...')

        # Try to fetch a small batch of employees
        response = session.post(
            f'{IHCM_BASE_URL}/whrmux/webapi/api/employee',
            json={
                'start': 0,
                'end': 2,
                'limit': 3,
                'filter': [],
                'boolStr': 'AND',
                'orderBy': 'PEOPLE.LASTNAME, PEOPLE.FIRSTNAME',
                'showParameterics': False,
            },
        )

        if response.status_code == 401:
            print('API returned 401 - authentication may have failed')
            return False

        response.raise_for_status()
        data = response.json()

        total = data.get('total', 0)
        employees = data.get('data', [])

        print(f'  API reports {total:,} total employees')
        print(f'  Retrieved {len(employees)} test records')

        if employees:
            print(f'  Sample: {employees[0].get("fullName")} ({employees[0].get("email")})')

        print()
        print('Authentication test PASSED!')
        return True

    except Exception as e:
        print(f'Authentication test FAILED: {e}')
        import traceback
        traceback.print_exc()
        return False


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='iHCM Authentication Module')
    parser.add_argument('--no-playwright', action='store_true', help='Use API-based auth (likely to fail)')
    parser.add_argument('--visible', action='store_true', help='Show browser window during auth')
    parser.add_argument('--debug', action='store_true', help='Enable debug logging (API mode only)')
    parser.add_argument('--clear-cache', action='store_true', help='Clear cached session and force fresh authentication')
    parser.add_argument('--no-cache', action='store_true', help='Skip cache entirely (don\'t read or write)')
    args = parser.parse_args()

    print('iHCM Authentication Module')
    print('=' * 50)
    print()

    # Handle cache clearing
    if args.clear_cache:
        cache = SessionCache(verbose=True)
        cache.clear()
        print()

    use_cache = not args.no_cache

    if args.no_playwright:
        print('Using API-based authentication (may fail due to SSO)')
        success = test_authentication(debug=args.debug, use_playwright=False)
    else:
        print('Using Playwright browser authentication')
        # For visible mode, we need to run differently
        if args.visible:
            try:
                session = create_authenticated_session_playwright(headless=False, use_cache=use_cache)
                success = True
                print()
                print('Authentication test PASSED!')
            except Exception as e:
                print(f'Authentication test FAILED: {e}')
                success = False
        else:
            success = test_authentication(use_playwright=True, use_cache=use_cache)

    sys.exit(0 if success else 1)
