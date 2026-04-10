"""Tests for test stand functionality."""

import pytest
from unittest.mock import patch, AsyncMock, MagicMock
from datetime import datetime, timedelta

from modules.create_test_instance import build_stand_user_data, create_droplet
from modules.database import init_db, save_instance, get_instance_by_id


class TestBuildStandUserData:
    def test_basic(self):
        result = build_stand_user_data("nextcloud")
        assert "#cloud-config" in result
        assert "git clone -b main" in result
        assert "services4integration.git" in result
        assert "bash /app/nextcloud/install.sh -st latest -dt latest" in result

    def test_custom_tags(self):
        result = build_stand_user_data("wordpress", ds_tag="8.3.0", service_tag="6.4")
        assert "-st 6.4 -dt 8.3.0" in result
        assert "/app/wordpress/install.sh" in result

    def test_with_domain(self):
        result = build_stand_user_data("moodle", domain_name="test.example.com")
        assert "-dn test.example.com" in result

    def test_without_domain(self):
        result = build_stand_user_data("moodle")
        assert "-dn" not in result

    def test_service_with_slash(self):
        result = build_stand_user_data("jira/standalone")
        assert "/app/jira/standalone/install.sh" in result


class TestStandDatabase:
    def test_save_with_stand_type(self, tmp_db):
        init_db()
        exp = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
        save_instance(
            500,
            "test-stand",
            "1.2.3.4",
            "s-2vcpu-4gb",
            exp,
            456,
            789,
            stand_type="nextcloud",
        )
        inst = get_instance_by_id(500)
        assert inst is not None
        assert inst["stand_type"] == "nextcloud"

    def test_save_without_stand_type(self, tmp_db):
        init_db()
        exp = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
        save_instance(501, "plain-droplet", "1.2.3.5", "s-2vcpu-2gb", exp, 456, 789)
        inst = get_instance_by_id(501)
        assert inst is not None
        assert inst["stand_type"] is None


def _make_mock_client():
    mock_client = AsyncMock()
    post_response = MagicMock()
    post_response.json.return_value = {"droplet": {"id": 99999, "name": "stand-test"}}
    post_response.raise_for_status = MagicMock()
    mock_client.post.return_value = post_response
    get_response = MagicMock()
    get_response.json.return_value = {"droplet": {"networks": {"v4": [{"ip_address": "10.0.0.1"}]}}}
    get_response.raise_for_status = MagicMock()
    mock_client.get.return_value = get_response
    return mock_client


@pytest.mark.asyncio
class TestCreateDropletStand:
    @patch("modules.create_test_instance.save_instance")
    async def test_user_data_passed_to_api(self, mock_save):
        mock_client = _make_mock_client()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = mock_client

        ud = build_stand_user_data("nextcloud")

        with patch("httpx.AsyncClient", return_value=mock_ctx):
            await create_droplet(
                token="fake",
                name="stand-nc",
                ssh_key_ids=[1],
                droplet_type="s-2vcpu-4gb",
                image="ubuntu-20-04-x64",
                duration=3,
                creator_id=111,
                user_data=ud,
                stand_type="nextcloud",
            )

        payload = mock_client.post.call_args[1]["json"]
        assert payload["user_data"] == ud
        assert "connectors" in payload["tags"]

    @patch("modules.create_test_instance.save_instance")
    async def test_no_user_data_no_connectors_tag(self, mock_save):
        mock_client = _make_mock_client()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = mock_client

        with patch("httpx.AsyncClient", return_value=mock_ctx):
            await create_droplet(
                token="fake",
                name="plain",
                ssh_key_ids=[1],
                droplet_type="s-2vcpu-2gb",
                image="ubuntu-22-04-x64",
                duration=1,
                creator_id=111,
            )

        payload = mock_client.post.call_args[1]["json"]
        assert "user_data" not in payload
        assert "connectors" not in payload["tags"]

    @patch("modules.create_test_instance.save_instance")
    async def test_stand_type_passed_to_save(self, mock_save):
        mock_client = _make_mock_client()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = mock_client

        with patch("httpx.AsyncClient", return_value=mock_ctx):
            await create_droplet(
                token="fake",
                name="stand-wp",
                ssh_key_ids=[1],
                droplet_type="s-2vcpu-4gb",
                image="ubuntu-20-04-x64",
                duration=7,
                creator_id=222,
                stand_type="wordpress",
            )

        mock_save.assert_called_once()
        call_kwargs = mock_save.call_args
        assert call_kwargs[1].get("stand_type") == "wordpress" or (
            len(call_kwargs[0]) > 11 and call_kwargs[0][11] is not None
        )
