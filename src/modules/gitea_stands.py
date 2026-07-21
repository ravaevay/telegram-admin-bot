import asyncio
import logging

import httpx

from config import GITEA_TOKEN, GITEA_URL, STAND_DOMAIN, STANDS_REPO_NAME, STANDS_REPO_OWNER

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
BACKOFF_BASE = 1  # seconds; doubles: 1 → 2 → 4, cap 30

STAND_POLL_INTERVAL_SECONDS = 60
STAND_DEPLOY_TIMEOUT_SECONDS = 5400  # 90 min: terraform + ansible can be slow

CORRELATE_ATTEMPTS = 10
CORRELATE_INTERVAL = 3  # seconds

# Serialize dispatch+correlate so concurrent creations can't match each other's runs
_dispatch_lock = asyncio.Lock()

# One entry per workflow_dispatch workflow in the stands repo.
# "inputs" mirror the workflow inputs except the common "mode" and "subdomain",
# which the bot always supplies itself.
STAND_CATALOG = {
    "wordpress": {
        "workflow_file": "deploy-wordpress.yml",
        "url_path": "/",
        "inputs": [
            {"name": "wordpress_version", "label": "Версия WordPress", "type": "string", "default": "php8.3-apache"},
            {"name": "connector_branch", "label": "Ветка коннектора", "type": "string", "default": "main"},
        ],
    },
    "moodle": {
        "workflow_file": "deploy-moodle.yml",
        "url_path": "/",
        "inputs": [
            {"name": "moodle_version", "label": "Версия Moodle", "type": "string", "default": "4.5.11"},
            {"name": "connector_branch", "label": "Ветка коннектора", "type": "string", "default": "master"},
        ],
    },
    "odoo": {
        "workflow_file": "deploy-odoo.yml",
        "url_path": "/",
        "inputs": [
            {
                "name": "odoo_version",
                "label": "Версия Odoo",
                "type": "choice",
                "options": ["17.0", "18.0", "19.0"],
                "default": "19.0",
            },
            {"name": "connector_branch", "label": "Ветка коннектора", "type": "string", "default": "19.0"},
        ],
    },
    "drupal": {
        "workflow_file": "deploy-drupal.yml",
        "url_path": "/",
        "inputs": [
            {"name": "drupal_version", "label": "Версия Drupal", "type": "string", "default": "11-apache"},
            {"name": "connector_version", "label": "Версия коннектора", "type": "string", "default": "*"},
        ],
    },
    "finebi": {
        "workflow_file": "deploy-finebi.yml",
        "url_path": "/webroot/decision",
        "inputs": [
            {"name": "finebi_version", "label": "Версия FineBI", "type": "string", "default": "6.0"},
        ],
    },
    "plone": {
        "workflow_file": "deploy-plone.yml",
        "url_path": "/",
        "inputs": [
            {"name": "plone_version", "label": "Версия Plone", "type": "string", "default": "6.1"},
            {"name": "connector_branch", "label": "Ветка коннектора", "type": "string", "default": "develop"},
        ],
    },
    "jira": {
        "workflow_file": "deploy-jira.yml",
        "url_path": "/",
        "inputs": [
            {"name": "jira_version", "label": "Версия Jira", "type": "string", "default": "10.3"},
            {"name": "connector_version", "label": "Версия коннектора", "type": "string", "default": "4.1.1"},
        ],
    },
    "mattermost": {
        "workflow_file": "deploy-mattermost.yml",
        "url_path": "/",
        "inputs": [
            {"name": "mattermost_version", "label": "Версия Mattermost", "type": "string", "default": "10.5"},
            {"name": "connector_branch", "label": "Ветка коннектора", "type": "string", "default": "main"},
        ],
    },
    "dropbox": {
        "workflow_file": "deploy-dropbox.yml",
        "url_path": "/",
        "inputs": [],
    },
    "humhub": {
        "workflow_file": "deploy-humhub.yml",
        "url_path": "/",
        "inputs": [
            {"name": "connector_version", "label": "Версия коннектора", "type": "string", "default": "v4.0.0"},
            {
                "name": "admin_password",
                "label": "Пароль администратора",
                "type": "string",
                "default": "OnlyOffice2024!",
            },
        ],
    },
    "owncloud": {
        "workflow_file": "deploy-owncloud.yml",
        "url_path": "/",
        "inputs": [
            {"name": "owncloud_version", "label": "Версия ownCloud", "type": "string", "default": "10.15"},
        ],
    },
    "alfresco": {
        "workflow_file": "deploy-alfresco.yml",
        "url_path": "/",
        "inputs": [
            {"name": "alfresco_repo_version", "label": "Версия Alfresco", "type": "string", "default": "23.3.24"},
            {"name": "connector_version", "label": "Версия коннектора", "type": "string", "default": "8.3.0"},
        ],
    },
}


def _runs_api_base():
    return f"{GITEA_URL}/api/v1/repos/{STANDS_REPO_OWNER}/{STANDS_REPO_NAME}/actions"


def _auth_headers():
    return {"Authorization": f"token {GITEA_TOKEN}"}


def build_stand_fqdn(subdomain):
    return f"{subdomain}.{STAND_DOMAIN}"


def build_stand_url(service, subdomain):
    url_path = STAND_CATALOG[service]["url_path"]
    return f"https://{build_stand_fqdn(subdomain)}{url_path}"


async def _gitea_request_with_retry(client, method, url, **kwargs):
    """HTTP request with retry: 429 → Retry-After, 5xx → exp backoff, timeout → retry. 4xx (other) → raise immediately."""
    delay = BACKOFF_BASE
    last_exc = None
    for attempt in range(MAX_RETRIES):
        try:
            response = await getattr(client, method)(url, **kwargs)

            if response.status_code == 429:
                retry_after = int(response.headers.get("Retry-After", delay))
                logger.warning(f"Rate limited on {url}. Waiting {retry_after}s.")
                await asyncio.sleep(retry_after)
                delay = min(delay * 2, 30)
                continue

            if response.status_code >= 500:
                if attempt < MAX_RETRIES - 1:
                    logger.warning(f"Server error {response.status_code} on {url}, retry in {delay}s.")
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 30)
                    continue
                response.raise_for_status()

            # 4xx (except 429) — raise immediately, no retry
            response.raise_for_status()
            return response

        except httpx.TimeoutException as exc:
            last_exc = exc
            if attempt < MAX_RETRIES - 1:
                logger.warning(f"Timeout on {url} (attempt {attempt + 1}), retry in {delay}s.")
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)
                continue
            raise

    raise last_exc or RuntimeError("Max retries exceeded")


def _extract_runs(data):
    """Gitea returns either {"workflow_runs": [...], "total_count": N} or a bare list."""
    if isinstance(data, dict):
        return data.get("workflow_runs") or []
    return data or []


async def list_runs(limit=50):
    """List recent action runs in the stands repo, newest first."""
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await _gitea_request_with_retry(
                client,
                "get",
                f"{_runs_api_base()}/runs",
                headers=_auth_headers(),
                params={"limit": limit},
            )
            return {"success": True, "runs": _extract_runs(response.json()), "message": ""}
    except httpx.HTTPError as e:
        logger.error(f"Failed to list Gitea action runs: {e}")
        return {"success": False, "runs": [], "message": str(e)}


async def dispatch_workflow(workflow_file, inputs, ref="main"):
    """Trigger a workflow_dispatch run. Gitea returns 204 with no body."""
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            await _gitea_request_with_retry(
                client,
                "post",
                f"{_runs_api_base()}/workflows/{workflow_file}/dispatches",
                headers=_auth_headers(),
                json={"ref": ref, "inputs": inputs},
            )
            return {"success": True, "message": ""}
    except httpx.HTTPStatusError as e:
        detail = e.response.text[:200] if e.response is not None else str(e)
        logger.error(f"Failed to dispatch {workflow_file}: {detail}")
        return {"success": False, "message": detail}
    except httpx.HTTPError as e:
        logger.error(f"Failed to dispatch {workflow_file}: {e}")
        return {"success": False, "message": str(e)}


def _run_matches_workflow(run, workflow_file):
    """Match a run object to a workflow file across Gitea versions (path/workflow_id/name fields)."""
    path = run.get("path") or ""
    if path:
        return path.endswith(workflow_file)
    workflow_id = str(run.get("workflow_id") or "")
    if workflow_id:
        return workflow_id == workflow_file
    return True  # no identifying field — accept any new run


async def dispatch_and_correlate(workflow_file, inputs, ref="main"):
    """Dispatch a workflow and find the run it created.

    The dispatch endpoint returns no run id, so we remember the newest run id
    before dispatching and then poll for a newer workflow_dispatch run.
    """
    async with _dispatch_lock:
        before = await list_runs(limit=1)
        if not before["success"]:
            return {"success": False, "run_id": None, "run_url": None, "message": before["message"]}
        last_id = before["runs"][0]["id"] if before["runs"] else 0

        dispatched = await dispatch_workflow(workflow_file, inputs, ref=ref)
        if not dispatched["success"]:
            return {"success": False, "run_id": None, "run_url": None, "message": dispatched["message"]}

        for _ in range(CORRELATE_ATTEMPTS):
            await asyncio.sleep(CORRELATE_INTERVAL)
            result = await list_runs(limit=20)
            if not result["success"]:
                continue
            candidates = [
                r
                for r in result["runs"]
                if r.get("id", 0) > last_id
                and r.get("event") == "workflow_dispatch"
                and _run_matches_workflow(r, workflow_file)
            ]
            if candidates:
                run = min(candidates, key=lambda r: r["id"])  # oldest new run = ours
                return {
                    "success": True,
                    "run_id": run["id"],
                    "run_url": run.get("html_url"),
                    "message": "",
                }

        # Dispatch succeeded but the run was not found; the poll job falls back to a timeout.
        logger.warning(f"Dispatched {workflow_file} but could not correlate a run id.")
        return {"success": True, "run_id": None, "run_url": None, "message": ""}


async def get_run_status(run_id):
    """Return {"success", "status", "conclusion", "run_url"} for a run.

    Tries GET /actions/runs/{id}; falls back to filtering the run list
    on Gitea versions without the single-run endpoint.
    """
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            try:
                response = await _gitea_request_with_retry(
                    client,
                    "get",
                    f"{_runs_api_base()}/runs/{run_id}",
                    headers=_auth_headers(),
                )
                run = response.json()
            except httpx.HTTPStatusError as e:
                if e.response is not None and e.response.status_code == 404:
                    result = await list_runs(limit=100)
                    if not result["success"]:
                        return {"success": False, "status": None, "conclusion": None, "run_url": None}
                    run = next((r for r in result["runs"] if r.get("id") == run_id), None)
                    if run is None:
                        return {"success": False, "status": None, "conclusion": None, "run_url": None}
                else:
                    raise
            return {
                "success": True,
                "status": run.get("status"),
                "conclusion": run.get("conclusion"),
                "run_url": run.get("html_url"),
            }
    except httpx.HTTPError as e:
        logger.error(f"Failed to get run {run_id} status: {e}")
        return {"success": False, "status": None, "conclusion": None, "run_url": None}


async def deploy_stand(service, subdomain, extra_inputs):
    """Dispatch a deploy run for a stand service."""
    workflow_file = STAND_CATALOG[service]["workflow_file"]
    inputs = {"mode": "deploy", "subdomain": subdomain}
    inputs.update(extra_inputs)
    return await dispatch_and_correlate(workflow_file, inputs)


async def destroy_stand(service, subdomain):
    """Dispatch a destroy run for a stand (terraform destroy of droplet + DNS)."""
    workflow_file = STAND_CATALOG[service]["workflow_file"]
    return await dispatch_and_correlate(workflow_file, {"mode": "destroy", "subdomain": subdomain})
