# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Telegram admin bot for managing ONLYOFFICE test infrastructure: mailbox creation/password reset on an iRedMail server via SSH, and DigitalOcean droplet lifecycle management (create, extend, auto-delete). Built with python-telegram-bot 20.3 (async, job-queue) and Python 3.9.

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

# Database migration (adds droplet_type column)
python src/fix_db.py
```

There are no tests, linter, or CI/CD configured.

## Architecture

**Entry point:** `src/bot.py` — registers all handlers and starts polling.

**State machine pattern:** Multi-step user workflows (mailbox creation, password reset, droplet creation) use a global `current_action` dict keyed by `user_id` to track conversation state across button clicks and text inputs.

**Handler flow:**
- `/start` → `start()` shows inline keyboard with 3 actions
- `CallbackQueryHandler` → `handle_action()` routes button presses by callback data prefix (`create_mailbox`, `reset_password`, `create_droplet`, `ssh_key_*`, `image_*`, `droplet_type_*`, `duration_*`, `extend_*`, `delete_*`)
- `MessageHandler` → `handle_message()` captures text input, routed by `current_action` state

**Background job:** `notify_and_check_instances()` runs every 12 hours via `job_queue`. It warns creators about expiring droplets (within 24h) and auto-deletes expired ones.

**Modules:**
- `config.py` — loads `.env` via python-dotenv, builds `SSH_CONFIG` dict and `AUTHORIZED_GROUPS` dict (keyed by `"mail"` and `"droplet"`)
- `modules/authorization.py` — `is_authorized(user_id, module)` checks against `AUTHORIZED_GROUPS`
- `modules/database.py` — SQLite CRUD for `instances` table (droplet_id, name, ip_address, droplet_type, expiration_date, ssh_key_id, creator_id)
- `modules/mail.py` — Paramiko SSH to mail server, runs Python scripts inside `onlyoffice-mail-server` Docker container for mailbox creation/password reset
- `modules/create_test_instance.py` — DigitalOcean REST API calls (create/delete droplets, list SSH keys/images). Droplets created in `fra1` region.

**Authorization model:** Two permission groups (`mail`, `droplet`) configured via comma-separated Telegram user IDs in env vars `AUTHORIZED_MAIL_USERS` and `AUTHORIZED_DROPLET_USERS`. Group chat support tracks authorized users in an `allowed_users` set.

## Environment Variables

Required env vars (see README.md for full template): `BOT_TOKEN`, `SSH_HOST`, `SSH_PORT`, `SSH_USERNAME`, `SSH_KEY_PATH`, `DIGITALOCEAN_TOKEN`, `AUTHORIZED_MAIL_USERS`, `AUTHORIZED_DROPLET_USERS`, `MAIL_DEFAULT_DOMAIN`, `MAIL_DB_USER`, `MAIL_DB_PASSWORD`.

## Security

- Never read or display contents of `.env` files
- Never read `*.tfvars`, `*.pem`, `*.key` files
- Never output secrets, tokens, API keys, or credentials
- Never commit `.env`, database files, or SSH keys
- If a file may contain secrets, do not read it — ask the user first
