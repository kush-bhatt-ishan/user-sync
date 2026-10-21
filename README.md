# User Sync (PeopleStrong to Microsoft Entra ID)

> A Django-based enterprise synchronization service that automatically fetches employee records from the **PeopleStrong HRIS API** and provisions/updates user accounts in **Microsoft Entra ID (Azure AD)** via the **Microsoft Graph API**.

---

## Table of Contents
1. [Overview & How It Works](#overview--how-it-works)
2. [Architecture & Sync Workflow](#architecture--sync-workflow)
3. [Prerequisites](#prerequisites)
4. [Environment Configuration (`.env`)](#environment-configuration-env)
5. [Getting Started (Step-by-Step)](#getting-started-step-by-step)
6. [Complete Command Reference](#complete-command-reference)
7. [Database Models & Logging](#database-models--logging)
8. [Testing & Verification](#testing--verification)
9. [Troubleshooting & Common Issues](#troubleshooting--common-issues)

---

## Overview & How It Works

### Core Functionality
- **PeopleStrong Outbound HRIS Data Fetching**: Retrieves active employee profiles using token-based API authentication.
- **Dynamic Field Mapping**: Normalizes and maps custom HRIS fields (`EmployeeCode`, `OfficialMailID`, `DesignationName`, `EmploymentStatus`, etc.) into Microsoft Entra ID standards (`userPrincipalName`, `mail`, `displayName`, `jobTitle`, `department`, `accountEnabled`, etc.).
- **Account Provisioning & Upsert (Create / Update / Disable)**:
  - **Status Filter**: Only employee records with `EmploymentStatus` equal to **`Active`** or **`Inactive`** (case-insensitive) are synced/updated in Microsoft Entra ID.
  - **Other Statuses**: If an employee has any other status (e.g., `Probation`, `Resigned`, `Notice Period`, `Suspended`, or blank), the data is stored in the local SQLite database, but Microsoft Entra ID sync is skipped. A clear message explaining the skipped status is saved in `last_error_message` on the `EmployeeSyncRecord` and `SyncLog`.
  - **Create**: Generates new user accounts in Microsoft Entra ID with secure initial password profiles.
  - **Update**: Syncs changes in employee details (department, designation, phone numbers, location) to existing Entra ID user objects.
  - **Disable**: Deactivates accounts in Microsoft Entra ID when employment status is `Inactive`.
- **Audit Logging & State Tracking**: Every sync run creates tracking records in the SQLite database (`SyncRun`, `EmployeeSyncRecord`, `SyncLog`) and logs detailed events to `sync.log`.

---

## Architecture & Sync Workflow

```
┌─────────────────────────┐          ┌────────────────────────────────┐          ┌───────────────────────────┐
│   PeopleStrong HRIS     │          │    Django User-Sync Engine     │          │  Microsoft Entra ID       │
│  (OAuth Token + REST)   │ ───────► │ (models, sync_services, CLI)   │ ───────► │ (Azure AD / Graph API)    │
└─────────────────────────┘          └────────────────────────────────┘          └───────────────────────────┘
                                                     │
                                                     ▼
                                     ┌────────────────────────────────┐
                                     │  SQLite DB / sync.log Tracking │
                                     │  (SyncRun, EmployeeSyncRecord) │
                                     └────────────────────────────────┘
```

### Detailed Execution Flow
1. **Trigger**: Execution is initiated via Django management command (`python manage.py sync_users`) or programmatically.
2. **Fetch Data**: `PeopleStrongClient` requests an OAuth token and fetches employee JSON data from the HRIS endpoint.
3. **Field Normalization**: `build_graph_payload()` transforms the HRIS payload into a compliant Graph API JSON schema.
4. **Record Tracking**: `get_or_create_sync_record()` stores or updates local state in `EmployeeSyncRecord`.
5. **Azure AD Synchronization**:
   - Checks if the user exists in Microsoft Entra ID via Graph API search (`mail` or `userPrincipalName`).
   - If user exists: Issues a `PATCH` request to update changed attributes.
   - If user does not exist: Issues a `POST` request to create the user account (if `SYNC_CREATE=true`).
6. **Logging**: Records outcome (success/failure, error code, timestamp) in `SyncLog` and updates `SyncRun` statistics.

---

## Prerequisites
- **Python**: Version 3.10+ (Python 3.14 included in `venv`)
- **Database**: SQLite3 (default, included as `db.sqlite3`)
- **Credentials**:
  - PeopleStrong HRIS API Client ID, Client Secret, Token URL & API Key
  - Azure AD App Registration (Tenant ID, Client ID, Client Secret) with Microsoft Graph Application Permissions (`User.ReadWrite.All`, `Directory.ReadWrite.All`)

---

## Environment Configuration (`.env`)

Create a `.env` file in the project root by copying `.env.example`:

```bash
cp .env.example .env
```

Configure the following variables in `.env`:

```ini
# PeopleStrong HRIS API Settings
PS_TOKEN_URL=https://auth.peoplestrong.com/auth/realms/1454/protocol/openid-connect/token
PS_API_URL=https://api.peoplestrong.com/api/integration/Outbound/IshanInfotechLimited_HRIS_EmployeedataD365
PS_APIKEY=your-peoplestrong-api-key
PS_INTEGRATION_NAME=EmployeedataD365
PS_CLIENT_ID=your-peoplestrong-client-id
PS_CLIENT_SECRET=your-peoplestrong-client-secret

# Microsoft Entra ID (Azure AD) Settings
AZ_TENANT_ID=your-azure-tenant-id
AZ_CLIENT_ID=your-azure-client-id
AZ_CLIENT_SECRET=your-azure-client-secret

# Synchronization Flags
SYNC_CREATE=true
SYNC_UPDATE=true
SYNC_DISABLE=false

# Logging & Company Defaults
LOG_LEVEL=INFO
LOG_FILE=sync.log
DEFAULT_COMPANY_NAME=Ishan Technologies
```

---

## Getting Started (Step-by-Step)

### 1. Activate Virtual Environment
```powershell
# Windows PowerShell
.\venv\Scripts\Activate.ps1

# Windows CMD
.\venv\Scripts\activate.bat
```

### 2. Apply Database Migrations
Initialize the local SQLite database schema:
```powershell
python manage.py migrate
```

### 3. Run Web Server & Admin Panel (Optional)
To inspect sync records via the Django Admin UI:
```powershell
python manage.py runserver
```
Navigate to `http://127.0.0.1:8000/admin/` in your browser.

### 4. Execute Employee Synchronization
To run the sync service for all employees:
```powershell
python manage.py sync_users
```

---

## Complete Command Reference

### Environment & Virtual Environment
| Description | Command |
| :--- | :--- |
| **Activate Virtual Environment (PowerShell)** | `.\venv\Scripts\Activate.ps1` |
| **Activate Virtual Environment (CMD)** | `.\venv\Scripts\activate.bat` |
| **Copy `.env` Template** | `Copy-Item .env.example .env` |

---

### Core Sync Management Commands (`sync_users`)

All synchronization operations are controlled via the `sync_users` Django management command:

#### 1. Full Synchronization (All Employees)
Syncs all active employees returned by the PeopleStrong API:
```powershell
python manage.py sync_users
```

#### 2. Sync Single Employee by ID
Sync a specific employee using their PeopleStrong `EmployeeCode`:
```powershell
python manage.py sync_users --employee-id E1001
```

#### 3. Sync Batch of Specific Employees
Sync multiple specific employee codes in a single run:
```powershell
python manage.py sync_users --employee-ids E1001 E1002 E1003
```

#### 4. Limit the Number of Employees to Sync
Limit the sync process to the first N employees (useful for testing):
```powershell
python manage.py sync_users --limit 10
```

#### 5. Retry Failed Sync Records Only
Retry syncing only the employee records that failed in previous runs:
```powershell
python manage.py sync_users --retry-only
```

#### 6. Combined Options
Retry a specific failed employee ID:
```powershell
python manage.py sync_users --retry-only --employee-id E1001
```

---

### Django Management & Development Commands

| Description | Command |
| :--- | :--- |
| **Start Web Server** | `python manage.py runserver` |
| **Apply Database Migrations** | `python manage.py migrate` |
| **Generate New Migrations** | `python manage.py makemigrations` |
| **Create Admin Superuser** | `python manage.py createsuperuser` |
| **Open Interactive Python Shell** | `python manage.py shell` |
| **Run System Check** | `python manage.py check` |

---

### Unit Testing Commands

| Description | Command |
| :--- | :--- |
| **Run Unit Tests (App scope)** | `python manage.py test myapp` |
| **Run All Unit Tests** | `python manage.py test` |

---

## Database Models & Logging

### Models Structure ([`myapp/models.py`](file:///d:/python/user-sync/myapp/models.py))
- **`EmployeeSyncRecord`**: Tracks individual employee status (`PENDING`, `SYNCED`, `FAILED`), raw HRIS payload, generated Graph API payload, attempt count, last error code/message, and sync timestamp.
- **`SyncRun`**: Tracks each sync job run (`RUNNING`, `COMPLETED`, `FAILED`), start/end times, total record count, synced count, and failure count.
- **`SyncLog`**: Detailed log entries associated with a `SyncRun` for auditing and debugging.

### Log Files
- **Log Location**: Defined by `LOG_FILE` in `.env` (default: `sync.log`).
- Inspect logs in real time:
  ```powershell
  Get-Content sync.log -Tail 50 -Wait
  ```

---

## Troubleshooting & Common Issues

1. **`KeyError` or Invalid Credentials in Graph API**:
   - Verify `AZ_TENANT_ID`, `AZ_CLIENT_ID`, and `AZ_CLIENT_SECRET` in `.env`.
   - Ensure the Azure App Registration has `User.ReadWrite.All` application permissions consented by an admin.

2. **PeopleStrong API Authentication Failures**:
   - Check `PS_TOKEN_URL`, `PS_CLIENT_ID`, and `PS_CLIENT_SECRET`.

3. **Failed Records Recovery**:
   - Execute `python manage.py sync_users --retry-only` to attempt reprocessing failed employee records without re-fetching all data.

---

## Server Deployment Guide (Production Setup)

### Option A: Linux Server Setup (Ubuntu / Debian / RHEL)

#### 1. System Dependencies & Repository Setup
```bash
sudo apt update && sudo apt install -y python3 python3-venv python3-pip git
cd /opt
git clone <your-repository-url> user-sync
cd user-sync
```

#### 2. Virtual Environment & Configuration
```bash
python3 -m venv venv
source venv/bin/activate
pip install django requests

# Create environment file
cp .env.example .env
nano .env   # Add real PeopleStrong & Azure AD credentials
```

#### 3. Database Migration & Admin Setup
```bash
python manage.py migrate
python manage.py createsuperuser
```

#### 4. Automate Sync via Cron Job (Periodic Execution)
To run the synchronization automatically (e.g. every hour):
```bash
crontab -e
```
Add the following line:
```cron
0 * * * * cd /opt/user-sync && /opt/user-sync/venv/bin/python manage.py sync_users >> /opt/user-sync/sync_cron.log 2>&1
```

#### 5. Web Admin Dashboard (Gunicorn + Nginx Reverse Proxy)
If you need browser access to the Django Admin panel:
```bash
pip install gunicorn
sudo apt install nginx -y
python manage.py collectstatic --noinput
```

Create `/etc/systemd/system/user-sync-web.service`:
```ini
[Unit]
Description=Gunicorn daemon for User Sync Admin
After=network.target

[Service]
User=www-data
Group=www-data
WorkingDirectory=/opt/user-sync
ExecStart=/opt/user-sync/venv/bin/gunicorn --workers 3 --bind 127.0.0.1:8000 userSync.wsgi:application

[Install]
WantedBy=multi-user.target
```

Enable and start the web service:
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now user-sync-web
```

---

### Option B: Windows Server Setup

#### 1. Installation & Environment
1. Install **Python 3.10+** (ensure "Add Python to PATH" is checked).
2. Copy the project folder to `C:\user-sync`.
3. Open PowerShell as Administrator:
   ```powershell
   cd C:\user-sync
   python -m venv venv
   .\venv\Scripts\Activate.ps1
   pip install django requests
   Copy-Item .env.example .env
   # Edit .env file with your API credentials
   python manage.py migrate
   ```

#### 2. Automated Scheduled Task (Windows Task Scheduler)
1. Open **Task Scheduler** (`taskschd.msc`).
2. Select **Create Basic Task** -> Name: `UserSyncScheduledJob`.
3. Trigger: Select **Daily**, recur every **1 day**, repeat task every **1 hour**.
4. Action: Select **Start a program**:
   - **Program/script**: `C:\user-sync\venv\Scripts\python.exe`
   - **Add arguments**: `manage.py sync_users`
   - **Start in**: `C:\user-sync`
5. Save the task and enable **Run whether user is logged on or not**.

