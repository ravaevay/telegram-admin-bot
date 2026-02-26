import asyncio
import logging
import time

import httpx

from datetime import datetime, timedelta
from modules.database import (
    save_k8s_cluster,
    delete_k8s_cluster as db_delete_k8s_cluster,
    get_k8s_cluster_by_name,
)

logger = logging.getLogger(__name__)

BASE_URL = "https://api.digitalocean.com/v2/"
DEFAULT_REGION = "fra1"
DEFAULT_NODE_SIZE = "s-2vcpu-4gb"
DEFAULT_NODE_COUNT = 2
DEFAULT_AUTO_SCALE = False
BOT_TAG = "createdby:telegram-admin-bot"

MAX_RETRIES = 3
BACKOFF_BASE = 1  # seconds; doubles: 1 → 2 → 4, cap 30

CLUSTER_POLL_TIMEOUT = 600  # seconds
CLUSTER_POLL_INTERVAL = 15  # seconds

_k8s_options_cache = {"data": None, "timestamp": 0}
_K8S_OPTIONS_CACHE_TTL = 3600  # 1 hour


def _auth_headers(token):
    return {"Authorization": f"Bearer {token}"}


async def _do_request_with_retry(client, method, url, **kwargs):
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


async def _get_k8s_options(token):
    """Fetch K8s versions and node sizes from DO API with 1h caching."""
    global _k8s_options_cache
    now = time.time()
    if _k8s_options_cache["data"] is not None and (now - _k8s_options_cache["timestamp"]) < _K8S_OPTIONS_CACHE_TTL:
        return _k8s_options_cache["data"]

    try:
        async with httpx.AsyncClient(headers=_auth_headers(token)) as client:
            response = await _do_request_with_retry(client, "get", BASE_URL + "kubernetes/options")
        data = response.json().get("options", {})
        _k8s_options_cache["data"] = data
        _k8s_options_cache["timestamp"] = now
        return data
    except httpx.HTTPError as e:
        logger.error(f"Ошибка при получении K8s options: {e}")
        return {}


async def get_k8s_versions(token):
    """Return available Kubernetes versions: {success, versions[], default_slug}."""
    options = await _get_k8s_options(token)
    versions = options.get("versions", [])
    default_slug = versions[-1]["slug"] if versions else None
    return {
        "success": bool(versions),
        "versions": versions,
        "default_slug": default_slug,
    }


async def get_k8s_sizes(token):
    """Return available K8s node sizes with pricing: {success, sizes: {slug: {price_monthly, price_hourly}}}."""
    options = await _get_k8s_options(token)
    sizes_list = options.get("sizes", [])
    sizes = {}
    for s in sizes_list:
        # DO API returns "name" for K8s node sizes
        slug = s.get("slug") or s.get("name")
        if slug:
            sizes[slug] = {
                "price_monthly": s.get("price_monthly", 0),
                "price_hourly": s.get("price_hourly", 0),
            }
    return {"success": bool(sizes), "sizes": sizes}


async def create_k8s_cluster(
    token,
    name,
    region,
    version,
    node_size,
    node_count,
    duration,
    creator_id,
    creator_username=None,
    price_hourly=None,
    ha=False,
):
    """Create a DOKS cluster. Returns immediately with status='provisioning'."""
    # Idempotency check
    existing = get_k8s_cluster_by_name(name, creator_id)
    if existing:
        return {
            "success": False,
            "message": f"Кластер с именем '{name}' уже существует.",
            "cluster_id": existing["cluster_id"],
            "cluster_name": existing["cluster_name"],
            "status": existing["status"],
            "endpoint": None,
            "region": None,
            "version": None,
            "node_size": None,
            "node_count": None,
            "price_hourly": None,
            "expiration_date": None,
            "kubeconfig": None,
        }

    expiration_date = (datetime.now() + timedelta(days=duration)).strftime("%Y-%m-%d %H:%M:%S")
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    payload = {
        "name": name,
        "region": region,
        "version": version,
        "node_pools": [
            {
                "size": node_size,
                "name": f"{name}-pool",
                "count": node_count,
            }
        ],
        "ha": ha,
        "tags": [BOT_TAG],
    }

    try:
        headers = {**_auth_headers(token), "Content-Type": "application/json"}
        async with httpx.AsyncClient(headers=headers) as client:
            response = await _do_request_with_retry(client, "post", BASE_URL + "kubernetes/clusters", json=payload)

        cluster = response.json().get("kubernetes_cluster", {})
        cluster_id = cluster.get("id")
        cluster_name = cluster.get("name")
        status = cluster.get("status", {}).get("state", "provisioning")
        endpoint = cluster.get("endpoint", "")

        save_k8s_cluster(
            cluster_id=cluster_id,
            cluster_name=cluster_name,
            region=region,
            version=version,
            node_size=node_size,
            node_count=node_count,
            status=status,
            endpoint=endpoint,
            creator_id=creator_id,
            creator_username=creator_username,
            expiration_date=expiration_date,
            created_at=created_at,
            price_hourly=price_hourly,
            ha=ha,
        )
        logger.info(f"K8s кластер '{cluster_name}' создан. ID: {cluster_id}, статус: {status}")

        return {
            "success": True,
            "message": "Кластер создаётся (~5-10 мин).",
            "cluster_id": cluster_id,
            "cluster_name": cluster_name,
            "status": status,
            "endpoint": endpoint,
            "region": region,
            "version": version,
            "node_size": node_size,
            "node_count": node_count,
            "price_hourly": price_hourly,
            "expiration_date": expiration_date,
            "kubeconfig": None,
        }

    except httpx.HTTPError as e:
        logger.error(f"Ошибка при создании K8s кластера: {e}")
        return {
            "success": False,
            "message": str(e),
            "cluster_id": None,
            "cluster_name": None,
            "status": None,
            "endpoint": None,
            "region": None,
            "version": None,
            "node_size": None,
            "node_count": None,
            "price_hourly": None,
            "expiration_date": None,
            "kubeconfig": None,
        }


async def get_k8s_cluster(token, cluster_id):
    """Get current status of a K8s cluster from DO API."""
    try:
        async with httpx.AsyncClient(headers=_auth_headers(token)) as client:
            response = await _do_request_with_retry(client, "get", BASE_URL + f"kubernetes/clusters/{cluster_id}")
        cluster = response.json().get("kubernetes_cluster", {})
        return {
            "success": True,
            "status": cluster.get("status", {}).get("state"),
            "endpoint": cluster.get("endpoint", ""),
            "cluster_id": cluster.get("id"),
            "cluster_name": cluster.get("name"),
        }
    except httpx.HTTPError as e:
        logger.error(f"Ошибка при получении статуса K8s кластера {cluster_id}: {e}")
        return {"success": False, "message": str(e)}


async def delete_k8s_cluster(token, cluster_id):
    """Delete a DOKS cluster from DO and remove from DB. Returns 204 (no body)."""
    try:
        async with httpx.AsyncClient(headers=_auth_headers(token)) as client:
            await _do_request_with_retry(client, "delete", BASE_URL + f"kubernetes/clusters/{cluster_id}")
        db_delete_k8s_cluster(cluster_id)
        logger.info(f"K8s кластер {cluster_id} удалён из DigitalOcean и базы данных.")
        return {"success": True}
    except httpx.HTTPError as e:
        logger.error(f"Ошибка при удалении K8s кластера {cluster_id}: {e}")
        return {"success": False, "message": str(e)}


async def wait_for_cluster_ready(token, cluster_id, timeout=CLUSTER_POLL_TIMEOUT, interval=CLUSTER_POLL_INTERVAL):
    """Poll cluster status until state=='running' or error/timeout."""
    deadline = time.time() + timeout
    try:
        async with httpx.AsyncClient(headers=_auth_headers(token)) as client:
            while time.time() < deadline:
                response = await _do_request_with_retry(client, "get", BASE_URL + f"kubernetes/clusters/{cluster_id}")
                cluster = response.json().get("kubernetes_cluster", {})
                state = cluster.get("status", {}).get("state")
                if state in ("running", "degraded"):
                    endpoint = cluster.get("endpoint", "")
                    logger.info(f"K8s кластер {cluster_id} готов (state={state!r}). Endpoint: {endpoint}")
                    return {"success": True, "endpoint": endpoint, "degraded": state == "degraded"}
                if state == "errored":
                    logger.error(f"K8s кластер {cluster_id} завершился с ошибкой.")
                    return {"success": False, "message": "Cluster errored"}
                await asyncio.sleep(interval)
        logger.warning(f"K8s кластер {cluster_id} не стал готов за {timeout}с.")
        return {"success": False, "message": "Timeout"}
    except httpx.HTTPError as e:
        logger.error(f"Ошибка при ожидании готовности K8s кластера {cluster_id}: {e}")
        return {"success": False, "message": str(e)}


async def get_kubeconfig(token, cluster_id):
    """Fetch kubeconfig YAML for a running cluster."""
    try:
        headers = {**_auth_headers(token), "Accept": "application/yaml"}
        async with httpx.AsyncClient(headers=headers) as client:
            response = await _do_request_with_retry(
                client, "get", BASE_URL + f"kubernetes/clusters/{cluster_id}/kubeconfig"
            )
        return {"success": True, "kubeconfig": response.text}
    except httpx.HTTPError as e:
        logger.error(f"Ошибка при получении kubeconfig кластера {cluster_id}: {e}")
        return {"success": False, "message": str(e)}
