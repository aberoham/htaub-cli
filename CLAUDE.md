# ADP iHCM API Integration

This project contains scripts to extract employee data from ADP's iHCM portal for comparison with Azure Active Directory.

## Code Quality

Run linters before committing changes:

```bash
uvx ruff check *.py && uvx mypy *.py
```

Auto-fix ruff issues:

```bash
uvx ruff check --fix *.py
```

Verify no Windows line endings (CRLF):

```bash
file *.py | grep -v 'CRLF' || echo "ERROR: CRLF line endings detected"
```

Fix CRLF if detected:

```bash
sed -i '' 's/\r$//' *.py
```

## Configuration

Create a `.env` file to customize settings (not committed to git):

```bash
# 1Password item name containing ADP credentials
ONEPASSWORD_ITEM="ADP IHCM"
```

Requires `python-dotenv` to be installed (`uv pip install python-dotenv`), or export the variable in your shell.

## Quick Start

All scripts use Playwright-based authentication with stealth mode to avoid bot detection. If 1Password CLI is installed and configured, credentials are retrieved automatically; otherwise you'll be prompted to enter them.

### Directory Extractor

Extracts the employee directory with basic fields.

```bash
uv run ihcm_extractor.py
```

Options:
- `--visible` - Show browser window during authentication
- `--clear-cache` - Clear cached session and force fresh authentication
- `--no-cache` - Skip session cache entirely

Output files are timestamped: `ihcm_employees_YYYYMMDD_HHMMSS.{json,csv}`

### People Home Extractor

Extracts employee data with additional HR fields (employee code, reference number, status) by combining the directory API with the employee-card API.

```bash
uv run people_home_extractor.py
```

Options:
- `--visible` - Show browser window during authentication
- `--clear-cache` - Clear cached session and force fresh authentication
- `--no-cache` - Skip session cache entirely

**Note:** This script takes ~20 minutes to complete because it fetches individual employee-card data for each of ~4,700 employees. Employee card responses are cached locally to `.cache/employee_cards/` so subsequent runs resume from where they left off.

Output files are timestamped: `people_home_YYYYMMDD_HHMMSS.{json,csv}`

### Leave Request Processor

Process (list, approve, reject) employee leave requests using Playwright-based authentication.

```bash
# List pending leave requests
uv run leave_request_processor.py list

# List requests older than 14 days
uv run leave_request_processor.py list --older-than 14

# Approve a leave request
uv run leave_request_processor.py approve <record_id>

# Reject a leave request (reason required)
uv run leave_request_processor.py reject <record_id> --reason "Insufficient notice"

# Show details of a specific request
uv run leave_request_processor.py show <record_id>
```

**Note:** This script uses automatic authentication via Playwright. If 1Password CLI is installed and configured, credentials are retrieved automatically; otherwise you'll be prompted to enter them.

## iHCM API Reference

Base URL: `https://ihcm.adp.com/whrmux/webapi/`

### Key Endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/employee` | POST | Directory listing with pagination |
| `/api/employee/search` | GET | Search employees by name |
| `/api/employee/direct-reports` | POST | Get direct reports for a manager |
| `/api/data/me/home` | GET | Employee home dashboard |
| `/api/data/me/my-details` | GET | Personal details |
| `/api/leave-absence/balance/{date}` | GET | Leave balances |
| `/api/pay/pay-statements/{count}` | GET | Pay statements |
| `/api/navigation` | GET | Navigation menu structure |
| `/api/employee-card` | GET | Individual employee HR details (code, ref#, status) |
| `/api/schema-grid/data` | POST | Grid data for various sections (People Home, Leave Requests) |
| `/api/messages/drawer` | GET | Todo items / inbox messages including leave requests |
| `/api/messages/messagebody/{id}/0` | GET | Full details of a message/notification |
| `/api/data/expert/pending-requests` | GET | Pending leave requests metadata |
| `/api/data/expert/leave-approve` | POST | Submit leave approval/rejection |
| `/api/screen-action/expert.leave-approve/{id}/save/none` | GET | Trigger save action for approval |
| `/api/action-button` | GET | Get available actions for a grid record |

### Employee API Request Format

```json
{
  "start": 0,
  "end": 99,
  "limit": 100,
  "filter": [],
  "boolStr": "AND",
  "orderBy": "PEOPLE.LASTNAME, PEOPLE.FIRSTNAME",
  "showParameterics": false
}
```

### Employee API Response Format

```json
{
  "parametricGroup": null,
  "data": [{ /* employee objects */ }],
  "total": 4738,
  "persona": null
}
```

### Employee Record Fields

| Field | Type | Description |
|-------|------|-------------|
| `id` | GUID | Unique employee identifier |
| `email` | string | Work email address |
| `userId` | string | System user ID (often uppercase email) |
| `fullName` | string | Display name |
| `firstName`, `lastName` | string | Name components |
| `knownAs` | string | Preferred name |
| `jobTitle` | string | Current job title |
| `department` | string | Full org hierarchy path |
| `location` | string/null | Work location |
| `workTelephone`, `workMobileTelephone` | string/null | Contact numbers |
| `reportsTo` | GUID | Manager's employee ID |
| `reportsToFullName`, `reportsToUserId` | string | Manager details |
| `directReports` | number | Count of direct reports |
| `isSSOOnly` | boolean | SSO-only login flag |
| `context` | string | Country code (e.g., "GB") |

### Employee-Card API

Returns additional HR fields for a single employee that aren't available in the directory API.

**Endpoint:** `GET /api/employee-card?peopleId={uuid}`

**Response:**
```json
[{
  "FULLNAME": "John Smith",
  "EMPLOYEECODE": "0012345",
  "REFERENCENUMBER": "12345",
  "STATUS": "Current",
  "JOBTITLE": "Customer Service Advisor",
  "REPORTSTO": "uuid",
  "REPORTSTOFULLNAME": "Jane Doe",
  "EMAILPRIMARY": "john.smith@example.com",
  "CONTEXT": "GB"
}]
```

### Leave Request APIs

The leave request workflow involves multiple endpoints depending on the entry point (todo list vs grid).

#### Messages/Todo Approach

Leave requests appear in the "Things to do" section on the Expert home page:

**Get Todo Items:**
```
GET /api/messages/drawer?todoItemsOnly=true
```

**Get Leave Request Details:**
```
GET /api/messages/messagebody/{messageId}/0
```

Response includes leave request details like dates, hours, leave type, and approval status.

#### Grid-Based Approach (Monitor Leave Requests)

The Expert > Leave and absence > Monitor leave requests page uses:

**Get Pending Requests Metadata:**
```
GET /api/data/expert/pending-requests?insertMode=false&parameters={"_AGEOFLEAVEREQUEST":"14"}
```

**Get Leave Requests Grid Data:**
```
POST /api/schema-grid/data
```

Request payload:
```json
{
  "parentRoute": "expert",
  "dataFromApi": false,
  "instanceName": "grid-controller-pendingRequestsGrid",
  "gridName": "pendingRequestsGrid",
  "searchValue": "",
  "formParams": {
    "parentRoute": "expert",
    "endpoint": "pending-requests",
    "insertMode": false
  },
  "formControlParameters": {
    "_AGEOFLEAVEREQUEST": "14"
  },
  "filter": {
    "showParameterics": false,
    "filters": [],
    "filterEmployeeId": null
  },
  "paging": {
    "start": 1,
    "limit": 100,
    "end": 100,
    "total": 0
  },
  "sort": {
    "orderBy": "startDate",
    "sortDirection": "asc"
  }
}
```

#### Approval Flow

When approving a leave request from the grid:

1. **Get Action Buttons:**
```
GET /api/action-button?formId={formId}&tableId={tableId}&recordId={recordId}&primaryKeys={keys}
```

2. **Submit Approval Data:**
```
POST /api/data/expert/leave-approve
```

3. **Trigger Save Action (called repeatedly as a polling mechanism):**
```
GET /api/screen-action/expert.leave-approve/{recordId}/save/none
```

The UI polls this endpoint multiple times until the action completes.

#### Observed Behavior (Jan 2026)

- Leave requests appear in "Things to do" on the Expert home page with subject lines like "Working from Home 16/01/2026 - 16/01/2026"
- The approval modal shows: employee name, reference number, code number, leave type, dates, hours off work, entitlement used, details given, and a comments field
- After approval, a green "Request has been accepted" or "Your changes have been successfully saved" banner appears
- The pending requests count updates in real-time (observed 1543 → 1540 after approvals)

### Schema-Grid API

The `/api/schema-grid/data` endpoint works for some grids (e.g., pending leave requests) but fails for others.

**People Home Grid - Non-Functional:** Returns 400 "InvalidRequest" errors regardless of payload format. The `people_home_extractor.py` script uses a hybrid approach instead (directory API + employee-card API).

**Pending Requests Grid - Functional:** Works correctly for the Monitor Leave Requests page with the payload documented above.

**Endpoint:** `POST /api/schema-grid/data`

**Request Format:**

```json
{
  "parentRoute": "expert",
  "dataFromApi": false,
  "recordId": "<user-record-id>",
  "instanceName": "grid-controller-copListingGridSection",
  "gridName": "copListingGridSection",
  "searchValue": "",
  "formParams": {
    "parentRoute": "expert",
    "recordId": "<user-record-id>",
    "parentId": "<user-record-id>",
    "endpoint": "people-home",
    "insertMode": false
  },
  "formControlParameters": {
    "_STATUSFILTER": null,
    "_JOBTITLEFILTER": null,
    "_LOCATIONFILTER": null,
    "_COMPANYLOCATION": null
  },
  "filter": {
    "showParameterics": true,
    "filters": [],
    "filterEmployeeId": null
  },
  "paging": {
    "start": 1,
    "limit": 100,
    "end": 100,
    "total": 0
  },
  "sort": {
    "orderBy": "fullName",
    "sortDirection": "asc"
  }
}
```

**Key Differences from Employee API:**
- Pagination is **1-based** (start: 1), not 0-based
- Requires a `recordId` (user's UUID) from the `selected-person` header
- Filter parameters use `_STATUSFILTER` values: `"Current"`, `"Leaver"`, `"None"`, or `null` for all

**Response Fields:**

| Field | Type | Description |
|-------|------|-------------|
| `id` | GUID | Record identifier |
| `peopleId` | GUID | Employee identifier |
| `fullName` | string | Display name |
| `firstName`, `lastName` | string | Name components |
| `knownAs` | string | Preferred name |
| `status` | string | Employment status (Current, Leaver, None) |
| `referenceNumber` | string | Employee reference number |
| `employeeCode` | string | Employee code (e.g., "0006140") |
| `jobTitle` | string | Job title with code prefix |
| `jobTitle_DISPLAY` | string | Clean job title without code |
| `dateOfLeaving` | string/null | ISO date if employee has left |
| `niNumber` | string | National Insurance Number |
| `context` | string | Country code (e.g., "GB") |

## Known Quirks and Gotchas

### Session Management

- **EMEASMSESSION cookie rotates**: The server issues a new session cookie with each response. The `requests.Session` object handles this automatically, but be aware if debugging.
- **JWT expiry**: The Bearer token has an `exp` claim and will expire. Session typically lasts as long as the browser session is active.
- **Multiple auth layers**: Authentication requires both the Bearer JWT token AND the XSRF-TOKEN header AND valid session cookies. Missing any one causes 401.

### Pagination Oddities

- **Off-by-one behavior**: Requesting `limit: 100` may return 99 records. The script handles this by using `start += len(batch)` rather than `start += batch_size`.
- **Total count is approximate**: The `total` field may not exactly match the sum of all batches due to real-time changes in the employee database.
- **Empty final batch**: When you reach the end, you may get an empty `data` array rather than a short batch.

### API Behavior

- **Schema-driven UI**: The iHCM frontend uses `/schemaapi/` endpoints to dynamically render forms. Field availability may vary by context/locale.
- **Context parameter**: Many endpoints accept `?context=GB` for localization. The employee API doesn't require this but other endpoints might.
- **Date format**: Uses ISO format throughout (e.g., `2026-01-05`).
- **GUID identifiers**: All employee IDs are GUIDs in lowercase with hyphens.

### Rate Limiting

- No explicit rate limiting observed, but the scripts use delays between requests to be polite.
- AWS load balancer cookies (`AWSALB`, `AWSALBCORS`) maintain session affinity; don't strip these.
- The `people_home_extractor.py` makes ~4,700 individual API calls and takes ~20 minutes to complete.

### Data Quality Notes

- **Email addresses**: Mix of corporate (`@example.com`) and personal (`@gmail.com`, `@icloud.com`) addresses.
- **userId vs email**: `userId` is typically the uppercase version of email but not always.
- **Null fields**: `location`, `workTelephone`, `workMobileTelephone` are frequently null.
- **Department paths**: Full hierarchy paths like "Division - Region - Department - Team"

## Future Enhancements

Potential additional scripts based on discovered APIs:

- Leave/absence calendar extraction
- Org chart traversal (using `reportsTo` relationships)
- Pay statement retrieval
- Team-level reports for managers
- Sickness record extraction (via `/api/data/expert/leave-absence` endpoints)

## Files

| File | Purpose |
|------|---------|
| `ihcm_extractor.py` | Directory extraction script (uses `/api/employee`) |
| `people_home_extractor.py` | HR data extraction (uses `/api/employee` + `/api/employee-card`) |
| `leave_request_processor.py` | CLI for listing/approving/rejecting leave requests |
| `ihcm_auth.py` | Common authentication library using Playwright (1Password optional) |
| `AUTH_NOTES.md` | Research notes on authentication flow |
| `claude-chrome-decomposition.txt` | Original API discovery session |

## Programmatic Authentication (Working)

Automatic login using Playwright browser automation. Credentials can be retrieved from 1Password CLI (recommended) or entered manually when prompted. This replaces the manual curl-based auth for scripts that need it.

### Architecture Overview

ADP iHCM uses a multi-layer authentication system:

1. **Sign-in Service** (`online.emea.adp.com/api/sign-in-service/v1/`) - Initial authentication
2. **SiteMinder SSO** (`online.emea.adp.com/olp/login.html`) - Session federation gateway
3. **iHCM Token Exchange** (`ihcm.adp.com/whrmux/webapi/token`) - JWT bearer token for API calls

### Authentication Flow

```
1. GET /csrf                              → XSRF-TOKEN cookie
2. POST /api/sign-in-service/v1/sign-in.start
   Body: {appId: "IHCM", productId: "...", returnUrl: "...", callingAppId: "IHCM"}
   Returns: {session: "<JWT>", identityProviders: [...]}

3. POST /api/sign-in-service/v1/sign-in.account.identify
   Body: {session: "<JWT>", userId: "user@example.com"}
   Returns: 200 OK (empty body)

4. POST /api/sign-in-service/v1/sign-in.challenge.respond
   Body: {session: "<JWT>", response: {type: "PASSWORD_VERIFICATION_RESPONSE", password: "...", locale: "en_US"}}
   Returns: 302 redirect to https://online.emea.adp.com/

5. [Browser JavaScript] SiteMinder SSO redirect chain establishes session
6. GET ihcm.adp.com/whrmux/web/me/home → Initial 401s trigger token exchange
7. POST /whrmux/webapi/token → Returns JWT bearer token stored in sessionStorage as iHcmBearerToken
```

### Current Status: Working

The Playwright-based authentication is fully functional. It:
1. Launches headless Chromium browser
2. Navigates to iHCM (redirects to ADP login)
3. Fills username (from 1Password or manual entry), clicks Next
4. Fills password, submits login
5. Waits for redirect back to iHCM
6. Extracts `iHcmBearerToken` from sessionStorage
7. Extracts all cookies from browser context
8. Returns configured `requests.Session` ready for API calls

### Session Caching

Authenticated sessions are cached to `~/.cache/ihcm/session.json` for reuse across script invocations. This mimics how a real browser maintains sessions - if you run a script, take a break, then run it again while the session is still valid, it won't require a fresh login.

**Session lifetime:**
- JWT bearer tokens issued by ADP have a ~30 minute lifetime
- The cache validation uses a 5-minute buffer, so tokens are considered expired 5 minutes before actual expiry
- In practice, this means you have ~25 minutes between script runs before re-authentication is required
- For longer workflows, consider running scripts in quick succession or accepting periodic re-authentication

**Cache validation:**
1. JWT bearer token expiry is checked (with 5-minute buffer)
2. Lightweight API call to `/api/data/me/home` confirms server accepts the session
3. If either check fails, cache is cleared and fresh authentication occurs

**Cache location:** `~/.cache/ihcm/session.json` (file permissions set to 0600 for security)

**Disabling cache:**
- `--clear-cache` - Clears cached session before authenticating
- `--no-cache` - Skips cache entirely (neither reads nor writes)

**Programmatic control:**
```python
# Default: uses cache
session = create_authenticated_session_playwright()

# Force fresh authentication
session = create_authenticated_session_playwright(use_cache=False)
```

### Usage

```python
from ihcm_auth import create_authenticated_session_playwright

# Uses 1Password item "ADP IHCM" for credentials (or prompts if not available)
session = create_authenticated_session_playwright()

# Session is now ready for API calls
response = session.post(
    'https://ihcm.adp.com/whrmux/webapi/api/employee',
    json={'start': 0, 'end': 99, 'limit': 100, ...}
)
```

### Command Line Testing

```bash
# Run authentication test (headless, uses cached session if valid)
uv run ihcm_auth.py

# Force fresh authentication (clears cache first)
uv run ihcm_auth.py --clear-cache

# Skip cache entirely
uv run ihcm_auth.py --no-cache

# Run with visible browser window
uv run ihcm_auth.py --visible

# Run legacy API-based auth (doesn't work due to SSO)
uv run --with requests python ihcm_auth.py --no-playwright --debug
```

### Why Playwright is Required

The ADP authentication flow involves SiteMinder SSO which uses JavaScript-based token exchange that cannot be replicated with Python `requests` alone. The debug analysis showed:

```
Redirect chain when using Python requests:
1. ihcm.adp.com/whrmux/web/me/home → 302 to /_index/...
2. /_index/... → 302 to online.emea.adp.com/olp/login.html (SiteMinder)
3. /olp/login.html → 302 to /api/authorization-service/v1/authorize
4. /authorize → 302 to /signin/v1 (back to login!)
```

The browser-based approach handles all the JavaScript and redirect magic automatically.

### Key Cookies

| Cookie | Domain | Purpose |
|--------|--------|---------|
| `XSRF-TOKEN` | .adp.com | CSRF protection |
| `k8Ksj346` | .adp.com | Main session cookie |
| `ak_bmsc` | .emea.adp.com | Akamai bot manager |
| `bm_sv` | .emea.adp.com | Akamai bot manager |
| `AWSALB`, `AWSALBCORS` | ihcm.adp.com | AWS load balancer affinity |

### 1Password CLI Usage (Optional)

```bash
op item get "ADP IHCM" --fields username
op item get "ADP IHCM" --fields password --reveal
```

### Related Files

| File | Purpose |
|------|---------|
| `ihcm_auth.py` | Authentication module using Playwright |
| `AUTH_NOTES.md` | Research notes on authentication flow |
