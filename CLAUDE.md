# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Telegram admin bot for managing ONLYOFFICE test infrastructure: mailbox creation/password reset on an iRedMail server via SSH, DigitalOcean droplet lifecycle management (create, extend, auto-delete) with DNS A-record automation, live pricing display, cost tracking, creator tagging, automatic snapshots before expiry deletion, and SSH key preference learning. Also manages DigitalOcean Kubernetes (DOKS) clusters: create, extend, auto-delete, provisioning status polling, and per-user listing. Built with python-telegram-bot 20.3 (async, job-queue) and Python 3.9.

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

# Tests — check results from GitHub Actions after push (do NOT run locally)
gh run list --limit 5              # recent runs
gh run view <run_id>               # run details
gh run view <run_id> --log         # full log output
```

CI/CD: `.github/workflows/ci.yml` — lint + test on push/PR to main; Docker build+push on push to main and `v*.*` / `v*.*.*` tags. Always verify test results via GitHub Actions after pushing, not by running tests locally.

**Testing gotcha:** `config.py` calls `int(os.getenv(...))` at import time. Tests must set env vars (e.g. `AUTHORIZED_MAIL_USERS=1`, `AUTHORIZED_DROPLET_USERS=1`, `AUTHORIZED_K8S_USERS=1`) *before* any `src/` import. This is handled in `tests/conftest.py`.

**Database tests:** `database.py` has module-level `DB_PATH`. Tests use `monkeypatch.setattr` to redirect to a temp file via the `tmp_db` fixture.

## Architecture

**Entry point:** `src/bot.py` — registers all handlers and starts polling.

**State machine pattern:** Multi-step user workflows use `ConversationHandler` from python-telegram-bot with states and `context.user_data` to track conversation state. Droplet creation and management conversations use `allow_reentry=True` so users can restart a flow without waiting for the 10-minute timeout.

**Handler flow:**
- `/start` → `start()` shows inline keyboard with 6 actions (mail create, password reset, create droplet, manage droplets, create K8s cluster, manage K8s clusters)
- Six `ConversationHandler`s (mail creation, password reset, droplet creation, droplet management, K8s creation, K8s management) route callback/text input through state machines
- Standalone `CallbackQueryHandler`s for `extend_*`, `delete_*`, `k8s_extend_*`, and `k8s_delete_*` actions (used by background-job notification buttons)

**Droplet creation conversation states:** `SELECT_SSH_KEY → SELECT_IMAGE → SELECT_DNS_ZONE → INPUT_SUBDOMAIN → SELECT_TYPE → SELECT_DURATION → INPUT_NAME`. DNS steps are skippable (user can choose "Пропустить" or auto-skipped if no domains exist). When DNS is configured, the FQDN is used as the droplet name and the `INPUT_NAME` step is skipped. At `SELECT_SSH_KEY`, keys are reordered by the user's usage history (most frequently used first) and the top-3 preferred keys are auto-selected; new users get the default first-3 selection.

**K8s creation conversation states (200–204):** `K8S_SELECT_VERSION → K8S_SELECT_NODE_SIZE → K8S_SELECT_NODE_COUNT → K8S_SELECT_DURATION → K8S_INPUT_NAME`. Creates cluster immediately (status `provisioning`); background job polls for `running` state. K8s management states (205–208): `K8S_MANAGE_LIST → K8S_MANAGE_ACTION → K8S_MANAGE_EXTEND / K8S_MANAGE_CONFIRM_DELETE`.

**Background job:** `notify_and_check_instances()` runs every 12 hours via `job_queue`. It warns creators about expiring droplets (within 24h) and auto-deletes expired ones. Before auto-deletion of a droplet, a snapshot is created and the bot waits for it to complete (up to 600s). Also handles K8s clusters: warns/auto-deletes expiring clusters (no snapshot — DOKS doesn't support it), and polls `provisioning` clusters for `running`/`errored` state, notifying the creator when ready.

**Modules:**
- `config.py` — loads `.env` via python-dotenv, builds `SSH_CONFIG` dict and `AUTHORIZED_GROUPS` dict (keyed by `"mail"`, `"droplet"`, `"k8s"`)
- `modules/authorization.py` — `is_authorized(user_id, module)` checks against `AUTHORIZED_GROUPS`; `is_authorized_for_bot(user_id)` returns `True` if user belongs to any group (used by `/start`)
- `modules/database.py` — SQLite CRUD for three tables: `instances` (droplets), `ssh_key_usage` (per-user SSH key preferences), and `k8s_clusters` (cluster_id TEXT PK, cluster_name, region, version, node_size, node_count, status, endpoint, creator_id, creator_username, expiration_date, created_at, price_hourly, ha). Schema migrations via `_migrate_add_column()`. K8s functions: `save_k8s_cluster()`, `get_k8s_cluster_by_id/name()`, `get_k8s_clusters_by_creator()`, `update_k8s_cluster_status()`, `delete_k8s_cluster()`, `get_expiring_k8s_clusters()`, `get_provisioning_k8s_clusters()`, `extend_k8s_cluster_expiration()`.
- `modules/mail.py` — Paramiko SSH to mail server, runs Python scripts inside `onlyoffice-mail-server` Docker container for mailbox creation/password reset
- `modules/create_test_instance.py` — DigitalOcean REST API calls (create/delete droplets, list SSH keys/images, DNS record management, size/pricing with 1h TTL cache, snapshot creation with action polling, creator tagging via `_sanitize_tag()`). Droplets created in `fra1` region.
- `modules/create_k8s_cluster.py` — DigitalOcean DOKS API layer: `create_k8s_cluster()` (returns immediately, status=`provisioning`), `delete_k8s_cluster()`, `get_k8s_cluster()`, `get_k8s_versions()`, `get_k8s_sizes()` (both cached via `_get_k8s_options()` with 1h TTL), `wait_for_cluster_ready()` (polling), `get_kubeconfig()`. Retry logic via `_do_request_with_retry()`: 429 → Retry-After, 5xx → exp backoff, timeout → retry, 4xx → raise immediately.
- `modules/notifications.py` — Sends event notifications to a Telegram channel. `send_notification()` for droplets (created/extended/deleted/auto_deleted/snapshot_created). `send_k8s_notification()` for clusters (created/ready/extended/deleted/auto_deleted/errored).

**Stale callback queries:** Telegram callback queries expire after ~30s. All standalone `CallbackQueryHandler`s (extend/delete) wrap `query.answer()` in `try/except BadRequest: pass` to avoid crashes on stale buttons.

**Authorization model:** Three permission groups (`mail`, `droplet`, `k8s`) configured via comma-separated Telegram user IDs in env vars `AUTHORIZED_MAIL_USERS`, `AUTHORIZED_DROPLET_USERS`, `AUTHORIZED_K8S_USERS`. Group chat support tracks authorized users in an `allowed_users` set.

## Environment Variables

Required: `BOT_TOKEN`, `SSH_HOST`, `SSH_PORT`, `SSH_USERNAME`, `SSH_KEY_PATH`, `DIGITALOCEAN_TOKEN`, `AUTHORIZED_MAIL_USERS` (comma-separated user IDs), `AUTHORIZED_DROPLET_USERS` (comma-separated user IDs), `MAIL_DEFAULT_DOMAIN`, `MAIL_DB_USER`, `MAIL_DB_PASSWORD`.

Optional: `AUTHORIZED_K8S_USERS` (comma-separated user IDs; if empty, K8s features are inaccessible but the bot still starts), `NOTIFICATION_CHANNEL_ID` (Telegram channel for droplet and K8s event notifications), `DB_PATH` (default `./instances.db`).

## Git Workflow

- **Features:** always create a `feature/<feature_name>` branch, then open a PR to `main`
- **Bug fixes:** always create a `hotfix/<name>` branch, then open a PR to `main`
- Never commit directly to `main`
- **Remote:** push only to `gitea` remote (`git.onlyoffice.com`). Do not push to `origin` (GitHub).

## Security

- Never read or display contents of `.env` files
- Never read `*.tfvars`, `*.pem`, `*.key` files
- Never output secrets, tokens, API keys, or credentials
- Never commit `.env`, database files, or SSH keys
- If a file may contain secrets, do not read it — ask the user first
