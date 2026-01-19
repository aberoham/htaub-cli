# htaub-cli

CLI automation for ADP iHCM. Access your own HR data without clicking through their "cutting-edge" Angular UI.

## Scripts

| Script | Purpose |
|--------|---------|
| `leave_request_processor.py` | List, approve, reject leave requests |
| `ihcm_extractor.py` | Extract employee directory to CSV/JSON |
| `people_home_extractor.py` | Extract HR data with employee codes |
| `payslip_sync.py` | Download all historical payslips (JSON + PDF) |

All scripts use Playwright-based authentication with optional 1Password integration.

## Setup Instructions

### Step 1: Install Python

These scripts require Python 3.10 or newer. Check if you have Python installed:

```bash
python3 --version
```

If not installed or the version is older than 3.10:
- **macOS:** `brew install python@3.12` (requires [Homebrew](https://brew.sh))
- **Windows:** Download from [python.org](https://www.python.org/downloads/)

### Step 2: Install uv (Python Package Manager)

[uv](https://github.com/astral-sh/uv) is a fast Python package manager that handles dependencies automatically. Install it with:

**macOS/Linux:**
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

**Windows (PowerShell):**
```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

After installation, restart your terminal or run `source ~/.bashrc` (or `~/.zshrc` on macOS).

Verify the installation:
```bash
uv --version
```

### Step 3: Install Playwright Browsers

The automatic authentication scripts use Playwright to control a browser. Install the required browser:

```bash
uv run --with playwright playwright install chromium
```

This downloads a Chromium browser that the scripts will use for logging in.

### Step 4 (Optional): Set Up 1Password CLI

**This step is optional.** Without 1Password, the scripts will prompt you to enter your username and password when the session expires (~25 minutes). With 1Password, credentials are retrieved automatically.

The [1Password CLI](https://developer.1password.com/docs/cli/get-started/) allows the scripts to retrieve your ADP credentials automatically:

1. Install the 1Password CLI (see link above)
2. Sign in: `op signin`
3. Create a 1Password item named **"ADP IHCM"** with your ADP username and password in fields named `username` and `password`

To verify 1Password CLI is working:
```bash
op item get "ADP IHCM" --fields username
```

**Without 1Password:** When you run a script, you'll be prompted:
```
Please enter your ADP iHCM credentials:
  Username (email): your.email@example.com
  Password:
```

## Usage

### Processing Leave Requests

The leave request processor uses automatic authentication via Playwright. Credentials are retrieved from 1Password if available, or you'll be prompted to enter them:

```bash
# List all pending leave requests
uv run leave_request_processor.py list

# List requests older than 14 days
uv run leave_request_processor.py list --older-than 14

# Show details of a specific request
uv run leave_request_processor.py show <record_id>

# Approve a leave request
uv run leave_request_processor.py approve <record_id>

# Reject a leave request (reason required)
uv run leave_request_processor.py reject <record_id> --reason "Insufficient notice"
```

The first time you run a command, it will open a browser window to log into ADP. The session is cached for approximately 25 minutes, so subsequent commands won't require logging in again.

### Extracting Employee Data

These scripts also use automatic Playwright authentication:

```bash
# Extract employee directory (~2 minutes)
uv run ihcm_extractor.py

# Extract HR data with employee codes (~20 minutes)
uv run people_home_extractor.py
```

### Syncing Payslips

Download all your historical payslips with incremental sync support:

```bash
# Full sync (JSON + PDF)
uv run payslip_sync.py

# JSON only (skip PDF downloads)
uv run payslip_sync.py --skip-pdf

# PDF only (for already-cached payslips)
uv run payslip_sync.py --pdf-only

# List cached payslips without syncing
uv run payslip_sync.py --list
```

Payslips are cached locally in `.cache/payslips/` organized by year and month. The script performs incremental sync, so running it multiple times only downloads new payslips.

Options available for all scripts:
- `--visible` - Show browser window during authentication
- `--clear-cache` - Clear cached session and force fresh authentication
- `--no-cache` - Skip session cache entirely

## Output Files

All exports are timestamped and saved in the current directory:
- `ihcm_employees_YYYYMMDD_HHMMSS.csv` - Employee directory
- `ihcm_employees_YYYYMMDD_HHMMSS.json` - Employee directory (JSON format)
- `people_home_YYYYMMDD_HHMMSS.csv` - HR data with employee codes
- `people_home_YYYYMMDD_HHMMSS.json` - HR data (JSON format)

Payslips are cached in `.cache/payslips/` organized by year/month:
```
.cache/payslips/
├── index.json              # Master index
├── 2025/12/
│   ├── 2025-12-22.json     # Detailed payslip data
│   └── 2025-12-22.pdf      # PDF document
└── 2024/...
```

## Troubleshooting

### "uv: command not found"
Restart your terminal after installing uv, or run:
```bash
source ~/.bashrc   # Linux
source ~/.zshrc    # macOS
```

### "op: command not found" or prompted for credentials
This is normal if you haven't installed the 1Password CLI. You can either:
- Enter your credentials manually when prompted (works fine)
- Install the 1Password CLI for automatic credential management: https://developer.1password.com/docs/cli/get-started/

### "Session expired" or login required frequently
Sessions last about 25 minutes. If you need a fresh session:
```bash
uv run leave_request_processor.py list --clear-cache
```

### Browser window doesn't appear
By default, the browser runs in "headless" mode (invisible). To see the browser for debugging:
```bash
uv run ihcm_auth.py --visible
```

### "No item found" from 1Password
Make sure you have a 1Password item named exactly **"ADP IHCM"** with fields `username` and `password`.

## How It Works

These scripts interact with ADP's iHCM API to extract data and process requests. The authentication flow:

1. Scripts retrieve your credentials (from 1Password if available, otherwise prompts you to enter them)
2. Playwright opens a browser and logs into ADP
3. The session token is extracted and cached locally (~25 minutes)
4. API requests use this token until it expires

For technical details about the APIs, see `CLAUDE.md`.
