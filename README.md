# HVAC Manager

Equipment, technical documents and maintenance management. English and Arabic (RTL).
Django + PostgreSQL, server-rendered pages, with a background worker for reminders and backups.

## Run it

Needs Python 3.11+ and PostgreSQL installed on Windows. From PowerShell in this folder:

```powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1
powershell -ExecutionPolicy Bypass -File .\run.ps1
```

Open http://localhost:8000. The first administrator's temporary password is in `FIRST_LOGIN.txt`; you are
asked to change it at first sign-in, then delete that file. `setup.ps1` is safe to run again.

Setup creates a private PostgreSQL cluster in `.pgdata` on port 5433, the virtualenv in
`%LOCALAPPDATA%\HvacManager\venv` (compiled DLLs fail to load from long Windows paths), and a `.env` with
generated secrets. Any PostgreSQL already on the machine is not touched.

## First steps in the app

1. System Settings → *Load suggested subtypes and fields* (optional), or add your own under Categories & Subtypes.
2. Manufacturers → add manufacturers and their models, with shared specifications on the model.
3. Clients → add clients, then their equipment units.
4. Technical Documents → upload and assign to a unit, subtype or main type.
5. Maintenance Contracts → create, choose equipment, add schedules, then **Activate**. Visits are generated on activation.

## Going to production

Set these in `.env`:

| Setting | Purpose |
|---|---|
| `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` | Use your own PostgreSQL server. Remove `LOCAL_PG=1`. |
| `LISTEN=0.0.0.0:8000`, `ALLOWED_HOSTS=hvac.company.com` | Accept connections from other devices. |
| `SITE_URL=https://hvac.company.com` | The address printed into QR codes. Reprint if it changes. |
| `HTTPS=1`, `CSRF_TRUSTED_ORIGINS=https://hvac.company.com` | Required behind an HTTPS reverse proxy. Do not expose plain HTTP to the internet. |
| `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, `DEFAULT_FROM_EMAIL` | Email reminders, then enable them in System Settings. |
| `PRIVATE_ROOT`, `BACKUP_ROOT` | Where uploads and backups live. Keep backups on a different disk. |

**Reminders with nobody signed in to Windows:** `run.ps1` stops when its window closes or the user logs off.
On a server, run it from Task Scheduler at startup with "Run whether user is logged on or not", under the same
account that ran `setup.ps1`.

## How the important rules work

- **Document inheritance** is computed on read (`core/services.py: documents_for_unit`), so future units inherit with no new rows and no file copies.
- **Visit generation** (`sync_contract_visits`) is idempotent. Individual schedules beat the general one; a unique `(unit, planned_date)` constraint blocks duplicates; only untouched future visits are ever replaced. Past dates are not generated.
- **Reminders** (`run_reminders`) select by window rather than exact date and rely on a unique `(user, dedupe_key)`, so they catch up after downtime and cannot duplicate. A rescheduled visit gets a fresh reminder.
- **Files** are stored under random names outside any public path and only served by permission-checked views. Only PDF, TXT and common images display inline; everything else downloads.
- **Technicians** (no `scope.all` permission) see only units and visits assigned to them, plus subtype and main-type documents.

## Commands

```powershell
manage.py test core    # automated tests; needs the database running
manage.py reminders    # run the reminder check once
manage.py backup       # database + files zip; the worker also does this daily, keeping 14
```

Restore a backup: unzip it, `pg_restore -c -d hvac db-*.dump`, and copy `files\` back to `PRIVATE_ROOT`.
