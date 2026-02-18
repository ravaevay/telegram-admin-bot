import pytest
from unittest.mock import patch, AsyncMock, MagicMock

from modules.create_test_instance import _sanitize_tag, create_droplet


class TestSanitizeTag:
    def test_strips_at_prefix(self):
        assert _sanitize_tag("@johndoe") == "johndoe"

    def test_removes_spaces(self):
        assert _sanitize_tag("First Name") == "FirstName"

    def test_keeps_valid_chars(self):
        assert _sanitize_tag("user_name-123") == "user_name-123"

    def test_keeps_colons_and_dots(self):
        assert _sanitize_tag("team:dev.ops") == "team:dev.ops"

    def test_empty_after_clean_returns_unknown(self):
        assert _sanitize_tag("!!!") == "unknown"

    def test_empty_string_returns_unknown(self):
        assert _sanitize_tag("") == "unknown"

    def test_truncates_long_input(self):
        long_tag = "a" * 300
        result = _sanitize_tag(long_tag)
        assert len(result) == 255

    def test_unicode_removed(self):
        assert _sanitize_tag("пользователь") == "unknown"

    def test_mixed_valid_invalid(self):
        assert _sanitize_tag("@user (admin)") == "useradmin"


def _make_mock_client():
    """Create a mock httpx.AsyncClient that captures POST payload."""
    mock_client = AsyncMock()

    # POST response — droplet created
    post_response = MagicMock()
    post_response.json.return_value = {
        "droplet": {"id": 12345, "name": "test-droplet"}
    }
    post_response.raise_for_status = MagicMock()
    mock_client.post.return_value = post_response

    # GET response — IP polling (returns IP immediately)
    get_response = MagicMock()
    get_response.json.return_value = {
        "droplet": {
            "networks": {
                "v4": [{"ip_address": "1.2.3.4"}]
            }
        }
    }
    get_response.raise_for_status = MagicMock()
    mock_client.get.return_value = get_response

    return mock_client


@pytest.mark.asyncio
class TestCreateDropletTags:
    """Tests for create_droplet tagging behavior."""

    @patch("modules.create_test_instance.save_instance")
    async def test_createdby_tag_always_present(self, mock_save):
        mock_client = _make_mock_client()

        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = mock_client

        with patch("httpx.AsyncClient", return_value=mock_ctx):
            await create_droplet(
                token="fake-token",
                name="test",
                ssh_key_ids=[123],
                droplet_type="s-2vcpu-2gb",
                image="ubuntu-22-04-x64",
                duration=1,
                creator_id=111,
                creator_tag="testuser",
            )

        payload = mock_client.post.call_args[1]["json"]
        assert "createdby:telegram-admin-bot" in payload["tags"]
        assert "creator:testuser" in payload["tags"]

    @patch("modules.create_test_instance.save_instance")
    async def test_createdby_tag_without_creator(self, mock_save):
        mock_client = _make_mock_client()

        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = mock_client

        with patch("httpx.AsyncClient", return_value=mock_ctx):
            await create_droplet(
                token="fake-token",
                name="test",
                ssh_key_ids=[123],
                droplet_type="s-2vcpu-2gb",
                image="ubuntu-22-04-x64",
                duration=1,
                creator_id=111,
                creator_tag=None,
            )

        payload = mock_client.post.call_args[1]["json"]
        assert payload["tags"] == ["createdby:telegram-admin-bot"]
