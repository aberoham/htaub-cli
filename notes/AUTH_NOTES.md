# ADP iHCM Authentication - Research Notes

## Goal
Replace curl-based authentication with programmatic login using 1Password CLI credentials.

## Authentication Flow Discovered

### Endpoints (on online.emea.adp.com)
1. `GET /csrf` - Returns XSRF-TOKEN as a cookie (not in response body)
2. `POST /api/sign-in-service/v1/sign-in.start` - Initialize sign-in
3. `POST /api/sign-in-service/v1/sign-in.account.identify` - Submit username
4. `POST /api/sign-in-service/v1/sign-in.challenge.respond` - Submit password

### Request Formats

**sign-in.start:**
```json
{
  "appId": "IHCM",
  "productId": "b376f1f2-a35a-025b-e053-f282530b8ccb",
  "returnUrl": "https://ihcm.adp.com/whrmux/web/me/home",
  "callingAppId": "IHCM"
}
```
Returns: `{"session": "<JWT token>", "identityProviders": [...]}`

**sign-in.account.identify:**
```json
{
  "session": "<JWT from start>",
  "userId": "user@example.com"
}
```
Returns: Empty body, 200 OK

**sign-in.challenge.respond:**
```json
{
  "response": {
    "type": "PASSWORD_VERIFICATION_RESPONSE",
    "password": "<password>",
    "locale": "en_US"
  },
  "session": "<JWT from start>"
}
```
Returns: 302 redirect to `https://online.emea.adp.com/`

### Cookies Set During Auth
- `XSRF-TOKEN` - domain: .adp.com
- `k8Ksj346` - domain: .adp.com (main session cookie)
- `ak_bmsc` - domain: .emea.adp.com (Akamai bot manager)
- `bm_sv` - domain: .emea.adp.com (Akamai bot manager)

## Current Issue
After completing the auth flow with Python requests:
- All API calls return correctly
- Session cookies are set with domain `.adp.com`
- BUT: Accessing ihcm.adp.com redirects back to login page

In Playwright browser:
- Same auth flow works correctly
- After login, iHCM loads and API calls succeed
- Network shows `/whrmux/webapi/token` being called after initial 401s

## Hypothesis
There may be additional cookies or headers set during the browser redirect flow that we're missing. The `token` endpoint might be key.

## Next Steps
1. Record HAR file during successful browser login
2. Compare exact cookies/headers between browser and Python requests
3. Look for any JavaScript-initiated token exchange

## Files Created
- `ihcm_auth.py` - Authentication module (partially working)
  - Successfully authenticates to online.emea.adp.com
  - Gets session cookies
  - Fails to establish iHCM session

## 1Password Integration
Working correctly:
```bash
op item get "ADP IHCM" --fields username
op item get "ADP IHCM" --fields password --reveal
```
