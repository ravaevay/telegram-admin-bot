# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Telegram admin bot for managing ONLYOFFICE test infrastructure: mailbox creation/password reset on an iRedMail server via SSH, DigitalOcean droplet lifecycle management (create, extend, auto-delete) with DNS A-record automation, live pricing display, cost tracking, creator tagging, automatic snapshots before expiry deletion, and SSH key preference learning. Also manages DigitalOcean Kubernetes (DOKS) clusters: create, extend, auto-delete, provisioning status polling, and per-user listing. Deploys test stands (WordPress, Moodle, Odoo, Drupal, Jira, etc.) by dispatching Gitea Actions workflows in the [stands-for-connectors](https://git.onlyoffice.com/ONLYOFFICE-DevOps/stands-for-connectors) repo (terraform + ansible run on the Gitea runner side); the bot tracks deploy/destroy runs and handles expiry. Includes a Mattermost bot mirror that runs alongside the Telegram bot, providing the same functionality via DM-based conversations and interactive buttons. Built with python-telegram-bot 20.3 (async, job-queue), mattermostdriver, aiohttp, httpx, and Python 3.9.

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run locally
python src/bot.py

# Run Mattermost bot locally
python src/mattermost_bot.py

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

**Testing gotcha:** `config.py` calls `int(os.getenv(...))` at import time. Tests must set env vars (e.g. `AUTHORIZED_MAIL_USERS=1`, `AUTHORIZED_DROPLET_USERS=1`, `AUTHORIZED_K8S_USERS=1`, `AUTHORIZED_STAND_USERS=1`) *before* any `src/` import. `MM_*` env vars also need defaults in `conftest.py` (already handled). This is managed in `tests/conftest.py`.

**Database tests:** `database.py` has module-level `DB_PATH`. Tests use `monkeypatch.setattr` to redirect to a temp file via the `tmp_db` fixture.

## Architecture

**Entry points:** `src/bot.py` — registers all Telegram handlers and starts polling. `src/mattermost_bot.py` — second entry point for the Mattermost bot, runs independently alongside the Telegram bot.

**State machine pattern:** Multi-step user workflows use `ConversationHandler` from python-telegram-bot with states and `context.user_data` to track conversation state. Droplet creation and management conversations use `allow_reentry=True` so users can restart a flow without waiting for the 10-minute timeout.

**Handler flow:**
- `/start` → `start()` shows inline keyboard with 8 actions (mail create, password reset, create droplet, manage droplets, create K8s cluster, manage K8s clusters, create test stand, manage test stands)
- Eight `ConversationHandler`s (mail creation, password reset, droplet creation, droplet management, K8s creation, K8s management, stand creation, stand management) route callback/text input through state machines
- Standalone `CallbackQueryHandler`s for `extend_*`, `delete_*`, `k8s_extend_*`, `k8s_delete_*`, `stand_extend_*`, and `stand_delete_*` actions (used by background-job notification buttons)

**Droplet creation conversation states:** `SELECT_SSH_KEY → SELECT_IMAGE → SELECT_DNS_ZONE → INPUT_SUBDOMAIN → SELECT_TYPE → SELECT_DURATION → INPUT_NAME`. DNS steps are skippable (user can choose "Пропустить" or auto-skipped if no domains exist). When DNS is configured, the FQDN is used as the droplet name and the `INPUT_NAME` step is skipped. At `SELECT_SSH_KEY`, keys are reordered by the user's usage history (most frequently used first) and the top-3 preferred keys are auto-selected; new users get the default first-3 selection.

**K8s creation conversation states (200–204):** `K8S_SELECT_VERSION → K8S_SELECT_NODE_SIZE → K8S_SELECT_NODE_COUNT → K8S_SELECT_DURATION → K8S_INPUT_NAME`. Creates cluster immediately (status `provisioning`); background job polls for `running` state. K8s management states (205–208): `K8S_MANAGE_LIST → K8S_MANAGE_ACTION → K8S_MANAGE_EXTEND / K8S_MANAGE_CONFIRM_DELETE`.

**Test stand creation conversation states (300–303):** `STAND_SELECT_SERVICE → STAND_INPUT_SUBDOMAIN → STAND_INPUT_PARAM → STAND_SELECT_DURATION`. `STAND_INPUT_PARAM` is a single looping state driven by a queue in `user_data` (`stand_param_queue`/`stand_param_index`/`stand_inputs`): each catalog input renders either a "По умолчанию: X" button + free-text prompt (string) or option buttons referenced by index `stand_par_opt_<i>` (choice). On duration selection the bot dispatches the service's `deploy-<service>.yml` workflow (`mode=deploy`, `subdomain`, plus collected inputs) via `deploy_stand()` and saves a `stands` row with status `deploying`. Stand management states (310–312): `STAND_MANAGE_ACTION → STAND_MANAGE_EXTEND / STAND_MANAGE_CONFIRM_DELETE` (entry: `manage_stands`). Deleting a stand dispatches the same workflow with `mode=destroy` and sets status `destroying`; the DB row is removed only after the destroy run succeeds (failed destroy → `destroy_failed`, retried by the expiry job once expired). Stand statuses: `deploying | active | deploy_failed | destroying | destroy_failed`. Separate authorization group `"stand"` (`AUTHORIZED_STAND_USERS`); stand features are disabled if `GITEA_TOKEN` is unset. Caveat: all services except FineBI share one terraform state per service — a second stand of the same service replaces the first.

**Background jobs (Telegram):** `notify_and_check_instances()` runs every 12 hours via `job_queue`. It warns creators about expiring droplets (within 24h) and auto-deletes expired ones. Before auto-deletion of a droplet, a snapshot is created and the bot waits for it to complete (up to 600s). Also handles K8s clusters: warns/auto-deletes expiring clusters (no snapshot — DOKS doesn't support it). It also warns about expiring stands (status `active`) and auto-destroys expired stands (statuses `active`/`deploy_failed`/`destroy_failed`; `destroying` is skipped to avoid double dispatch; no snapshot — destroy is a terraform run). `poll_provisioning_clusters()` (30s) polls K8s clusters. `poll_stand_runs()` (60s) polls Gitea Actions runs for `deploying`/`destroying` stands: deploy success → `active` + DM with URL; deploy failure or 90-min timeout → `deploy_failed` + run URL; destroy success → row deleted + `deleted`/`auto_deleted` notification (per `auto_destroy` flag); destroy failure → `destroy_failed`.

**Mattermost bot architecture:**
- Uses `mattermostdriver.Driver` (sync) for REST API, wrapped with `asyncio.to_thread()` for async compatibility
- WebSocket connection via `aiohttp` for real-time events (DM messages)
- `aiohttp` HTTP server on `MM_WEBHOOK_PORT` for interactive button callbacks
- `ConversationManager` (in-memory state machine) replaces Telegram's `ConversationHandler`
- Commands via `!start` and `!cancel` in DMs; subsequent steps via interactive buttons
- Background jobs: `asyncio.create_task` loops for instance/K8s/stand expiry (12h), K8s provisioning poll (30s), stand run poll (`mm_poll_stand_runs`, 60s), conversation cleanup (5min)
- Only processes `platform='mattermost'` records in background jobs

**Modules:**
- `config.py` — loads `.env` via python-dotenv, builds `SSH_CONFIG` dict and `AUTHORIZED_GROUPS` dict (keyed by `"mail"`, `"droplet"`, `"k8s"`, `"stand"`). Test stand config: `GITEA_URL`, `GITEA_TOKEN`, `STANDS_REPO_OWNER`, `STANDS_REPO_NAME`, `STAND_DOMAIN`
- `modules/authorization.py` — `is_authorized(user_id, module)` checks against `AUTHORIZED_GROUPS`; `is_authorized_for_bot(user_id)` returns `True` if user belongs to any group (used by `/start`)
- `modules/database.py` — SQLite CRUD for four tables: `instances` (droplets), `ssh_key_usage` (per-user SSH key preferences), `k8s_clusters` (cluster_id TEXT PK, cluster_name, region, version, node_size, node_count, status, endpoint, creator_id, creator_username, expiration_date, created_at, price_hourly, ha), and `stands` (id INTEGER PK AUTOINCREMENT, service, subdomain, url, status, deploy_run_id/deploy_run_url, destroy_run_id, inputs_json, auto_destroy, creator_id TEXT — Telegram int or MM string, always compare via `str()`, creator_username, expiration_date, created_at, platform). Schema migrations via `_migrate_add_column()` (the legacy `instances.stand_type` migration line is kept for old DBs). All resource tables have a `platform` column (`TEXT DEFAULT 'telegram'`) to distinguish records created by each bot. WAL mode is enabled for safe concurrent access from both bots. `get_expiring_*` and status-filter functions accept an optional `platform` filter. Stand functions: `save_stand()` (returns lastrowid), `get_stand_by_id()`, `get_stands_by_creator()`, `get_expiring_stands()` (excludes `destroying`), `get_deploying_stands()`, `get_destroying_stands()`, `update_stand_status()` (optionally sets destroy_run_id/auto_destroy), `extend_stand_expiration()`, `delete_stand()`.
- `modules/mail.py` — Paramiko SSH to mail server, runs Python scripts inside `onlyoffice-mail-server` Docker container for mailbox creation/password reset
- `modules/create_test_instance.py` — DigitalOcean REST API calls (create/delete droplets, list SSH keys/images, DNS record management, size/pricing with 1h TTL cache, snapshot creation with action polling, creator tagging via `_sanitize_tag()`). Droplets created in `fra1` region.
- `modules/gitea_stands.py` — Gitea Actions layer for test stands. `STAND_CATALOG`: static dict of 12 services → `{workflow_file, url_path, inputs: [{name, label (RU), type: string|choice, default, options?}]}` mirroring each workflow's `workflow_dispatch` inputs (minus the common `mode`/`subdomain` supplied by the bot). `dispatch_workflow()` POSTs `/repos/{owner}/{repo}/actions/workflows/{file}/dispatches` (returns 204, no run id). `dispatch_and_correlate()` (serialized via a lazily-created per-event-loop asyncio lock — a module-level Lock breaks on Python 3.9) remembers the newest run id before dispatch, then polls for a newer `workflow_dispatch` run (matching `path` if the API returns it); if correlation fails it still returns success with `run_id=None` and the poll job falls back to a 90-min timeout. `get_run_status()` GETs `/actions/runs/{id}` with a list-based fallback for older Gitea. `list_runs()` handles both `{"workflow_runs": [...]}` and bare-list responses. `deploy_stand()`/`destroy_stand()` wrap dispatch with `mode=deploy|destroy`. Retry helper `_gitea_request_with_retry()` copied from the K8s module (429 → Retry-After, 5xx → exp backoff, timeout → retry, 4xx → raise).
- `modules/create_k8s_cluster.py` — DigitalOcean DOKS API layer: `create_k8s_cluster()` (returns immediately, status=`provisioning`), `delete_k8s_cluster()`, `get_k8s_cluster()`, `get_k8s_versions()`, `get_k8s_sizes()` (both cached via `_get_k8s_options()` with 1h TTL), `wait_for_cluster_ready()` (polling), `get_kubeconfig()`. Retry logic via `_do_request_with_retry()`: 429 → Retry-After, 5xx → exp backoff, timeout → retry, 4xx → raise immediately.
- `modules/notifications.py` — Sends event notifications to a Telegram channel. `send_notification()` for droplets (created/extended/deleted/auto_deleted/snapshot_created). `send_k8s_notification()` for clusters (created/ready/extended/deleted/auto_deleted/errored). `send_stand_notification()` for stands (created/ready/errored/extended/deleted/auto_deleted/destroy_failed); text built by `build_stand_notification_text()`, shared with the MM module.
- `modules/mm_conversation.py` — `ConversationManager` in-memory state machine (replaces Telegram's `ConversationHandler`): `ConversationState`, `start()`, `get()`, `end()`, `cleanup_expired()`, 10-min timeout.
- `modules/mm_notifications.py` — mirrors `notifications.py` for Mattermost: `send_notification()`, `send_k8s_notification()`, `send_stand_notification()` posting to `MM_NOTIFICATION_CHANNEL_ID` via driver.

**Stale callback queries:** Telegram callback queries expire after ~30s. All standalone `CallbackQueryHandler`s (extend/delete) wrap `query.answer()` in `try/except BadRequest: pass` to avoid crashes on stale buttons.

**Authorization model:** Four permission groups (`mail`, `droplet`, `k8s`, `stand`) configured via comma-separated Telegram user IDs in env vars `AUTHORIZED_MAIL_USERS`, `AUTHORIZED_DROPLET_USERS`, `AUTHORIZED_K8S_USERS`, `AUTHORIZED_STAND_USERS`. Group chat support tracks authorized users in an `allowed_users` set.

## Environment Variables

Required: `BOT_TOKEN`, `SSH_HOST`, `SSH_PORT`, `SSH_USERNAME`, `SSH_KEY_PATH`, `DIGITALOCEAN_TOKEN`, `AUTHORIZED_MAIL_USERS` (comma-separated user IDs), `AUTHORIZED_DROPLET_USERS` (comma-separated user IDs), `MAIL_DEFAULT_DOMAIN`, `MAIL_DB_USER`, `MAIL_DB_PASSWORD`.

Optional: `AUTHORIZED_K8S_USERS` (comma-separated user IDs; if empty, K8s features are inaccessible but the bot still starts), `AUTHORIZED_STAND_USERS` (comma-separated user IDs; if empty, test stand features are inaccessible), `NOTIFICATION_CHANNEL_ID` (Telegram channel for droplet, K8s and stand event notifications), `DB_PATH` (default `./instances.db`), `MM_BOT_TOKEN` (Mattermost bot personal access token; required for MM bot), `MM_SERVER_URL` (Mattermost server URL; required for MM bot), `MM_WEBHOOK_PORT` (default 8065, for button callback HTTP server), `MM_WEBHOOK_HOST` (default localhost, hostname for callback URLs), `MM_AUTHORIZED_MAIL_USERS`, `MM_AUTHORIZED_DROPLET_USERS`, `MM_AUTHORIZED_K8S_USERS`, `MM_AUTHORIZED_STAND_USERS` (comma-separated MM user IDs), `MM_NOTIFICATION_CHANNEL_ID` (MM channel for event notifications), `GITEA_TOKEN` (Gitea API token with write access to the stands repo; if unset, stand features are disabled but the bot still starts), `GITEA_URL` (default `https://git.onlyoffice.com`), `STANDS_REPO_OWNER` (default `ONLYOFFICE-DevOps`), `STANDS_REPO_NAME` (default `stands-for-connectors`), `STAND_DOMAIN` (default `onlyoffice.fun`).

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
