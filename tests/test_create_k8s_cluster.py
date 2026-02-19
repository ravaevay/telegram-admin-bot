import pytest
from unittest.mock import patch, AsyncMock, MagicMock

from modules.create_k8s_cluster import (
    _auth_headers,
    get_k8s_versions,
    get_k8s_sizes,
    create_k8s_cluster,
    get_k8s_cluster,
    delete_k8s_cluster,
)
import modules.create_k8s_cluster as k8s_mod


# --- Helpers ---


def _make_options_response():
    """Mock response for GET /v2/kubernetes/options."""
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "options": {
            "versions": [
                {"slug": "1.28.0-do.0", "kubernetes_version": "1.28.0"},
                {"slug": "1.29.0-do.0", "kubernetes_version": "1.29.0"},
            ],
            "sizes": [
                {"name": "s-2vcpu-4gb", "price_monthly": 24.0, "price_hourly": 0.03571},
                {"name": "s-4vcpu-8gb", "price_monthly": 48.0, "price_hourly": 0.07143},
            ],
        }
    }
    resp.raise_for_status = MagicMock()
    return resp


def _make_cluster_create_response():
    """Mock response for POST /v2/kubernetes/clusters."""
    resp = MagicMock()
    resp.status_code = 201
    resp.json.return_value = {
        "kubernetes_cluster": {
            "id": "k8s-uuid-1234",
            "name": "my-cluster",
            "status": {"state": "provisioning"},
            "endpoint": "",
        }
    }
    resp.raise_for_status = MagicMock()
    return resp


def _make_cluster_get_response(state="running", endpoint="https://api.k8s.example.com"):
    """Mock response for GET /v2/kubernetes/clusters/{id}."""
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "kubernetes_cluster": {
            "id": "k8s-uuid-1234",
            "name": "my-cluster",
            "status": {"state": state},
            "endpoint": endpoint,
        }
    }
    resp.raise_for_status = MagicMock()
    return resp


def _make_delete_response():
    """Mock response for DELETE /v2/kubernetes/clusters/{id} (204 No Content)."""
    resp = MagicMock()
    resp.status_code = 204
    resp.raise_for_status = MagicMock()
    return resp


def _make_mock_client(responses):
    """
    Build an AsyncMock client that returns responses in order per method.
    responses: dict of method → response or list of responses
    """
    client = AsyncMock()
    for method, value in responses.items():
        if isinstance(value, list):
            getattr(client, method).side_effect = value
        else:
            getattr(client, method).return_value = value
    return client


# --- Tests ---


class TestAuthHeaders:
    def test_bearer_token(self):
        headers = _auth_headers("my-token")
        assert headers == {"Authorization": "Bearer my-token"}


@pytest.mark.asyncio
class TestGetK8sVersions:
    async def test_returns_versions(self):
        # Reset cache
        k8s_mod._k8s_options_cache["data"] = None

        mock_client = _make_mock_client({"get": _make_options_response()})
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = mock_client

        with patch("httpx.AsyncClient", return_value=mock_ctx):
            result = await get_k8s_versions("fake-token")

        assert result["success"] is True
        assert len(result["versions"]) == 2
        assert result["default_slug"] == "1.29.0-do.0"

    async def test_uses_cache(self):
        # Pre-populate cache
        k8s_mod._k8s_options_cache["data"] = {
            "versions": [{"slug": "1.30.0-do.0", "kubernetes_version": "1.30.0"}],
            "sizes": [],
        }
        k8s_mod._k8s_options_cache["timestamp"] = 9_999_999_999  # far future

        result = await get_k8s_versions("fake-token")
        assert result["success"] is True
        assert result["default_slug"] == "1.30.0-do.0"

        # Clean up cache
        k8s_mod._k8s_options_cache["data"] = None


@pytest.mark.asyncio
class TestGetK8sSizes:
    async def test_returns_sizes(self):
        k8s_mod._k8s_options_cache["data"] = None

        mock_client = _make_mock_client({"get": _make_options_response()})
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = mock_client

        with patch("httpx.AsyncClient", return_value=mock_ctx):
            result = await get_k8s_sizes("fake-token")

        assert result["success"] is True
        assert "s-2vcpu-4gb" in result["sizes"]
        assert abs(result["sizes"]["s-2vcpu-4gb"]["price_monthly"] - 24.0) < 0.01

    async def test_returns_empty_on_error(self):
        k8s_mod._k8s_options_cache["data"] = None

        import httpx

        mock_client = AsyncMock()
        mock_client.get.side_effect = httpx.ConnectError("refused")
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = mock_client

        with patch("httpx.AsyncClient", return_value=mock_ctx):
            result = await get_k8s_sizes("fake-token")

        assert result["success"] is False
        assert result["sizes"] == {}


@pytest.mark.asyncio
class TestCreateK8sCluster:
    @patch("modules.create_k8s_cluster.save_k8s_cluster")
    @patch("modules.create_k8s_cluster.get_k8s_cluster_by_name", return_value=None)
    async def test_creates_cluster_successfully(self, mock_check, mock_save):
        mock_client = _make_mock_client({"post": _make_cluster_create_response()})
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = mock_client

        with patch("httpx.AsyncClient", return_value=mock_ctx):
            result = await create_k8s_cluster(
                token="fake-token",
                name="my-cluster",
                region="fra1",
                version="1.29.0-do.0",
                node_size="s-2vcpu-4gb",
                node_count=2,
                duration=7,
                creator_id=42,
                creator_username="@tester",
                price_hourly=0.0714,
            )

        assert result["success"] is True
        assert result["cluster_id"] == "k8s-uuid-1234"
        assert result["cluster_name"] == "my-cluster"
        assert result["status"] == "provisioning"
        assert result["kubeconfig"] is None
        mock_save.assert_called_once()

    @patch("modules.create_k8s_cluster.get_k8s_cluster_by_name")
    async def test_idempotency_check(self, mock_check):
        mock_check.return_value = {
            "cluster_id": "existing-id",
            "cluster_name": "my-cluster",
            "status": "running",
        }

        result = await create_k8s_cluster(
            token="fake-token",
            name="my-cluster",
            region="fra1",
            version="1.29.0-do.0",
            node_size="s-2vcpu-4gb",
            node_count=2,
            duration=7,
            creator_id=42,
        )

        assert result["success"] is False
        assert "уже существует" in result["message"]
        assert result["cluster_id"] == "existing-id"

    @patch("modules.create_k8s_cluster.save_k8s_cluster")
    @patch("modules.create_k8s_cluster.get_k8s_cluster_by_name", return_value=None)
    async def test_bot_tag_in_payload(self, mock_check, mock_save):
        mock_client = _make_mock_client({"post": _make_cluster_create_response()})
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = mock_client

        with patch("httpx.AsyncClient", return_value=mock_ctx):
            await create_k8s_cluster(
                token="fake-token",
                name="my-cluster",
                region="fra1",
                version="1.29.0-do.0",
                node_size="s-2vcpu-4gb",
                node_count=2,
                duration=7,
                creator_id=42,
            )

        payload = mock_client.post.call_args[1]["json"]
        assert "createdby:telegram-admin-bot" in payload["tags"]

    @patch("modules.create_k8s_cluster.save_k8s_cluster")
    @patch("modules.create_k8s_cluster.get_k8s_cluster_by_name", return_value=None)
    async def test_returns_failure_on_api_error(self, mock_check, mock_save):
        import httpx as _httpx

        error_resp = MagicMock()
        error_resp.status_code = 422
        error_resp.raise_for_status.side_effect = _httpx.HTTPStatusError(
            "422", request=MagicMock(), response=error_resp
        )

        mock_client = _make_mock_client({"post": error_resp})
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = mock_client

        with patch("httpx.AsyncClient", return_value=mock_ctx):
            result = await create_k8s_cluster(
                token="fake-token",
                name="bad-cluster",
                region="fra1",
                version="1.29.0-do.0",
                node_size="s-2vcpu-4gb",
                node_count=2,
                duration=7,
                creator_id=42,
            )

        assert result["success"] is False
        mock_save.assert_not_called()

    @patch("modules.create_k8s_cluster.save_k8s_cluster")
    @patch("modules.create_k8s_cluster.get_k8s_cluster_by_name", return_value=None)
    async def test_node_pool_in_payload(self, mock_check, mock_save):
        mock_client = _make_mock_client({"post": _make_cluster_create_response()})
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = mock_client

        with patch("httpx.AsyncClient", return_value=mock_ctx):
            await create_k8s_cluster(
                token="fake-token",
                name="pool-cluster",
                region="fra1",
                version="1.29.0-do.0",
                node_size="s-4vcpu-8gb",
                node_count=3,
                duration=1,
                creator_id=42,
            )

        payload = mock_client.post.call_args[1]["json"]
        assert len(payload["node_pools"]) == 1
        assert payload["node_pools"][0]["size"] == "s-4vcpu-8gb"
        assert payload["node_pools"][0]["count"] == 3


@pytest.mark.asyncio
class TestGetK8sCluster:
    async def test_returns_status(self):
        mock_client = _make_mock_client({"get": _make_cluster_get_response("running", "https://api.k8s.example.com")})
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = mock_client

        with patch("httpx.AsyncClient", return_value=mock_ctx):
            result = await get_k8s_cluster("fake-token", "k8s-uuid-1234")

        assert result["success"] is True
        assert result["status"] == "running"
        assert result["endpoint"] == "https://api.k8s.example.com"
        assert result["cluster_id"] == "k8s-uuid-1234"

    async def test_returns_failure_on_error(self):
        import httpx as _httpx

        error_resp = MagicMock()
        error_resp.status_code = 404
        error_resp.raise_for_status.side_effect = _httpx.HTTPStatusError(
            "404", request=MagicMock(), response=error_resp
        )
        mock_client = _make_mock_client({"get": error_resp})
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = mock_client

        with patch("httpx.AsyncClient", return_value=mock_ctx):
            result = await get_k8s_cluster("fake-token", "nonexistent")

        assert result["success"] is False


@pytest.mark.asyncio
class TestDeleteK8sCluster:
    @patch("modules.create_k8s_cluster.db_delete_k8s_cluster")
    async def test_deletes_and_removes_from_db(self, mock_db_delete):
        mock_client = _make_mock_client({"delete": _make_delete_response()})
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = mock_client

        with patch("httpx.AsyncClient", return_value=mock_ctx):
            result = await delete_k8s_cluster("fake-token", "k8s-uuid-1234")

        assert result["success"] is True
        mock_db_delete.assert_called_once_with("k8s-uuid-1234")

    @patch("modules.create_k8s_cluster.db_delete_k8s_cluster")
    async def test_returns_failure_on_api_error(self, mock_db_delete):
        import httpx as _httpx

        error_resp = MagicMock()
        error_resp.status_code = 404
        error_resp.raise_for_status.side_effect = _httpx.HTTPStatusError(
            "404", request=MagicMock(), response=error_resp
        )
        mock_client = _make_mock_client({"delete": error_resp})
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = mock_client

        with patch("httpx.AsyncClient", return_value=mock_ctx):
            result = await delete_k8s_cluster("fake-token", "bad-id")

        assert result["success"] is False
        mock_db_delete.assert_not_called()
