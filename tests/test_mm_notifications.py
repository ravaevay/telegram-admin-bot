from unittest.mock import MagicMock, patch

import pytest

from modules.mm_notifications import send_notification, send_k8s_notification


@pytest.mark.asyncio
async def test_skip_when_channel_not_configured():
    """MM notification is skipped when MM_NOTIFICATION_CHANNEL_ID is empty."""
    with patch("modules.mm_notifications.MM_NOTIFICATION_CHANNEL_ID", ""):
        driver = MagicMock()
        await send_notification(driver, "created", "test", "1.2.3.4", "s-2vcpu-2gb", "2025-06-01 12:00:00", "user123")
        driver.posts.create_post.assert_not_called()


@pytest.mark.asyncio
async def test_sends_message_when_configured():
    """MM notification is sent when MM_NOTIFICATION_CHANNEL_ID is set."""
    with patch("modules.mm_notifications.MM_NOTIFICATION_CHANNEL_ID", "ch-123"):
        driver = MagicMock()
        await send_notification(
            driver, "created", "my-droplet", "1.2.3.4", "s-2vcpu-2gb", "2025-06-01 12:00:00", "user42"
        )
        driver.posts.create_post.assert_called_once()
        call_args = driver.posts.create_post.call_args[0][0]
        assert call_args["channel_id"] == "ch-123"
        assert "my-droplet" in call_args["message"]


@pytest.mark.asyncio
async def test_handles_send_failure():
    """MM notification failure does not raise."""
    with patch("modules.mm_notifications.MM_NOTIFICATION_CHANNEL_ID", "ch-123"):
        driver = MagicMock()
        driver.posts.create_post.side_effect = Exception("Network error")
        await send_notification(driver, "deleted", "test", "1.2.3.4", "s-2vcpu-2gb", "2025-06-01 12:00:00", "user123")


@pytest.mark.asyncio
async def test_extended_includes_duration():
    """Extended MM notification includes duration when provided."""
    with patch("modules.mm_notifications.MM_NOTIFICATION_CHANNEL_ID", "ch-123"):
        driver = MagicMock()
        await send_notification(
            driver, "extended", "my-drop", "1.2.3.4", "s-2vcpu-2gb", "2025-06-08 12:00:00", "user42", duration=7
        )
        driver.posts.create_post.assert_called_once()
        msg = driver.posts.create_post.call_args[0][0]["message"]
        assert "7" in msg
        assert "продлён" in msg.lower()


@pytest.mark.asyncio
async def test_created_shows_username():
    """Created MM notification shows creator_username when provided."""
    with patch("modules.mm_notifications.MM_NOTIFICATION_CHANNEL_ID", "ch-123"):
        driver = MagicMock()
        await send_notification(
            driver,
            "created",
            "my-drop",
            "1.2.3.4",
            "s-2vcpu-2gb",
            "2025-06-01 12:00:00",
            "user42",
            creator_username="@testuser",
        )
        msg = driver.posts.create_post.call_args[0][0]["message"]
        assert "@testuser" in msg


@pytest.mark.asyncio
async def test_created_shows_dns():
    """Created MM notification shows DNS name when provided."""
    with patch("modules.mm_notifications.MM_NOTIFICATION_CHANNEL_ID", "ch-123"):
        driver = MagicMock()
        await send_notification(
            driver,
            "created",
            "my-drop",
            "1.2.3.4",
            "s-2vcpu-2gb",
            "2025-06-01 12:00:00",
            "user42",
            domain_name="test.example.com",
        )
        msg = driver.posts.create_post.call_args[0][0]["message"]
        assert "test.example.com" in msg
        assert "DNS" in msg


@pytest.mark.asyncio
async def test_created_shows_cost():
    """Created MM notification shows cost when provided."""
    with patch("modules.mm_notifications.MM_NOTIFICATION_CHANNEL_ID", "ch-123"):
        driver = MagicMock()
        await send_notification(
            driver,
            "created",
            "my-drop",
            "1.2.3.4",
            "s-2vcpu-2gb",
            "2025-06-01 12:00:00",
            "user42",
            price_monthly=18.0,
        )
        msg = driver.posts.create_post.call_args[0][0]["message"]
        assert "18.0" in msg
        assert "Стоимость" in msg


@pytest.mark.asyncio
async def test_snapshot_created_notification():
    """Snapshot created MM notification contains expected info."""
    with patch("modules.mm_notifications.MM_NOTIFICATION_CHANNEL_ID", "ch-123"):
        driver = MagicMock()
        await send_notification(
            driver,
            "snapshot_created",
            "my-drop",
            "1.2.3.4",
            "s-2vcpu-2gb",
            "2025-06-01 12:00:00",
            "user42",
            creator_username="@admin",
        )
        msg = driver.posts.create_post.call_args[0][0]["message"]
        assert "Снэпшот" in msg
        assert "my-drop" in msg
        assert "@admin" in msg


# --- K8s notification tests ---


@pytest.mark.asyncio
async def test_k8s_skip_when_channel_not_configured():
    with patch("modules.mm_notifications.MM_NOTIFICATION_CHANNEL_ID", ""):
        driver = MagicMock()
        await send_k8s_notification(
            driver, "created", "my-cluster", "fra1", "s-2vcpu-4gb", 2, "2025-06-01 12:00:00", "user42"
        )
        driver.posts.create_post.assert_not_called()


@pytest.mark.asyncio
async def test_k8s_sends_message_when_configured():
    with patch("modules.mm_notifications.MM_NOTIFICATION_CHANNEL_ID", "ch-123"):
        driver = MagicMock()
        await send_k8s_notification(
            driver, "created", "my-cluster", "fra1", "s-2vcpu-4gb", 2, "2025-06-01 12:00:00", "user42"
        )
        driver.posts.create_post.assert_called_once()
        msg = driver.posts.create_post.call_args[0][0]["message"]
        assert "my-cluster" in msg


@pytest.mark.asyncio
async def test_k8s_ready_shows_endpoint():
    with patch("modules.mm_notifications.MM_NOTIFICATION_CHANNEL_ID", "ch-123"):
        driver = MagicMock()
        await send_k8s_notification(
            driver,
            "ready",
            "my-cluster",
            "fra1",
            "s-2vcpu-4gb",
            2,
            "2025-06-01 12:00:00",
            "user42",
            endpoint="https://k8s.example.com",
        )
        msg = driver.posts.create_post.call_args[0][0]["message"]
        assert "https://k8s.example.com" in msg


@pytest.mark.asyncio
async def test_k8s_errored_notification():
    with patch("modules.mm_notifications.MM_NOTIFICATION_CHANNEL_ID", "ch-123"):
        driver = MagicMock()
        await send_k8s_notification(
            driver, "errored", "bad-cluster", "fra1", "s-2vcpu-4gb", 2, "2025-06-01 12:00:00", "user42"
        )
        msg = driver.posts.create_post.call_args[0][0]["message"]
        assert "ошибк" in msg.lower()
