# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Telegram admin bot for managing ONLYOFFICE test infrastructure: mailbox creation/password reset on an iRedMail server via SSH, and DigitalOcean droplet lifecycle management (create, extend, auto-delete) with DNS A-record automation, live pricing display, cost tracking, creator tagging, and automatic snapshots before expiry deletion. Built with python-telegram-bot 20.3 (async, job-queue) and Python 3.9.

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run locally
python src/bot.py

# Run with Docker
docker-compose up --build -d

# View database entries (utility script)
python show_entries.py

# Legacy migration (adds droplet_type column) — no longer needed,
# init_db() handles all migrations automatically via _migrate_add_column()
python src/fix_db.py
```

## Lint & Test

```bash
# Lint (Ruff — config in pyproject.toml)
ruff check src/
ruff format --check src/

# Tests (pytest)
pytest tests/ -v
```

CI/CD: `.github/workflows/ci.yml` — lint + test on push/PR to main; Docker build+push on push to main and `v*.*` / `v*.*.*` tags.

**Testing gotcha:** `config.py` calls `int(os.getenv(...))` at import time. Tests must set env vars (e.g. `AUTHORIZED_MAIL_USERS=1`) *before* any `src/` import. This is handled in `tests/conftest.py`.

**Database tests:** `database.py` has module-level `DB_PATH`. Tests use `monkeypatch.setattr` to redirect to a temp file via the `tmp_db` fixture.

## Architecture

**Entry point:** `src/bot.py` — registers all handlers and starts polling.

**State machine pattern:** Multi-step user workflows use `ConversationHandler` from python-telegram-bot with states and `context.user_data` to track conversation state.

**Handler flow:**
- `/start` → `start()` shows inline keyboard with 4 actions (mail create, password reset, create droplet, manage droplets)
- Four `ConversationHandler`s (mail creation, password reset, droplet creation, droplet management) route callback/text input through state machines
- Standalone `CallbackQueryHandler`s for `extend_*` and `delete_*` actions

**Droplet creation conversation states:** `SELECT_SSH_KEY → SELECT_IMAGE → SELECT_DNS_ZONE → INPUT_SUBDOMAIN → SELECT_TYPE → SELECT_DURATION → INPUT_NAME`. DNS steps are skippable (user can choose "Пропустить" or auto-skipped if no domains exist). When DNS is configured, the FQDN is used as the droplet name and the `INPUT_NAME` step is skipped.

**Background job:** `notify_and_check_instances()` runs every 12 hours via `job_queue`. It warns creators about expiring droplets (within 24h) and auto-deletes expired ones. Before auto-deletion, a snapshot is created and the bot waits for it to complete (up to 600s).

**Modules:**
- `config.py` — loads `.env` via python-dotenv, builds `SSH_CONFIG` dict and `AUTHORIZED_GROUPS` dict (keyed by `"mail"` and `"droplet"`)
- `modules/authorization.py` — `is_authorized(user_id, module)` checks against `AUTHORIZED_GROUPS`; `is_authorized_for_bot(user_id)` returns `True` if user belongs to any group (used by `/start`)
- `modules/database.py` — SQLite CRUD for `instances` table (droplet_id, name, ip_address, droplet_type, expiration_date, ssh_key_id, creator_id, creator_username, domain_name, dns_record_id, dns_zone, created_at, price_hourly). Schema migrations via `_migrate_add_column()`. `get_expiring_instances()` returns `list[dict]`.
- `modules/mail.py` — Paramiko SSH to mail server, runs Python scripts inside `onlyoffice-mail-server` Docker container for mailbox creation/password reset
- `modules/create_test_instance.py` — DigitalOcean REST API calls (create/delete droplets, list SSH keys/images, DNS record management, size/pricing with 1h TTL cache, snapshot creation with action polling, creator tagging via `_sanitize_tag()`). Droplets created in `fra1` region.
- `modules/notifications.py` — Sends droplet event notifications to a Telegram channel. Supports `creator_username` (falls back to ID), `domain_name`, `price_monthly` display, and `snapshot_created` action in templates.

**Stale callback queries:** Telegram callback queries expire after ~30s. All standalone `CallbackQueryHandler`s (extend/delete) wrap `query.answer()` in `try/except BadRequest: pass` to avoid crashes on stale buttons.

**Authorization model:** Two permission groups (`mail`, `droplet`) configured via comma-separated Telegram user IDs in env vars `AUTHORIZED_MAIL_USERS` and `AUTHORIZED_DROPLET_USERS`. Group chat support tracks authorized users in an `allowed_users` set.

## Environment Variables

Required: `BOT_TOKEN`, `SSH_HOST`, `SSH_PORT`, `SSH_USERNAME`, `SSH_KEY_PATH`, `DIGITALOCEAN_TOKEN`, `AUTHORIZED_MAIL_USERS` (comma-separated user IDs), `AUTHORIZED_DROPLET_USERS` (comma-separated user IDs), `MAIL_DEFAULT_DOMAIN`, `MAIL_DB_USER`, `MAIL_DB_PASSWORD`.

Optional: `NOTIFICATION_CHANNEL_ID` (Telegram channel for droplet event notifications), `DB_PATH` (default `./instances.db`).

## Security

- Never read or display contents of `.env` files
- Never read `*.tfvars`, `*.pem`, `*.key` files
- Never output secrets, tokens, API keys, or credentials
- Never commit `.env`, database files, or SSH keys
- If a file may contain secrets, do not read it — ask the user first
