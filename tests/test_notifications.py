from unittest.mock import AsyncMock, patch

import pytest

from modules.notifications import send_notification


@pytest.mark.asyncio
async def test_skip_when_channel_not_configured():
    """Notification is skipped when NOTIFICATION_CHANNEL_ID is empty."""
    with patch("modules.notifications.NOTIFICATION_CHANNEL_ID", ""):
        bot = AsyncMock()
        await send_notification(bot, "created", "test", "1.2.3.4", "s-2vcpu-2gb", "2025-06-01 12:00:00", 123)
        bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_sends_message_when_configured():
    """Notification is sent when NOTIFICATION_CHANNEL_ID is set."""
    with patch("modules.notifications.NOTIFICATION_CHANNEL_ID", "-100123456"):
        bot = AsyncMock()
        await send_notification(bot, "created", "my-droplet", "1.2.3.4", "s-2vcpu-2gb", "2025-06-01 12:00:00", 42)
        bot.send_message.assert_called_once()
        call_kwargs = bot.send_message.call_args
        assert call_kwargs[1]["chat_id"] == "-100123456"
        assert "my-droplet" in call_kwargs[1]["text"]


@pytest.mark.asyncio
async def test_handles_send_failure():
    """Notification failure does not raise."""
    with patch("modules.notifications.NOTIFICATION_CHANNEL_ID", "-100123456"):
        bot = AsyncMock()
        bot.send_message.side_effect = Exception("Network error")
        # Should not raise
        await send_notification(bot, "deleted", "test", "1.2.3.4", "s-2vcpu-2gb", "2025-06-01 12:00:00", 123)


@pytest.mark.asyncio
async def test_extended_includes_duration():
    """Extended notification includes duration when provided."""
    with patch("modules.notifications.NOTIFICATION_CHANNEL_ID", "-100123456"):
        bot = AsyncMock()
        await send_notification(
            bot, "extended", "my-drop", "1.2.3.4", "s-2vcpu-2gb", "2025-06-08 12:00:00", 42, duration=7
        )
        bot.send_message.assert_called_once()
        text = bot.send_message.call_args[1]["text"]
        assert "7" in text
        assert "продлён" in text.lower()


@pytest.mark.asyncio
async def test_created_shows_username():
    """Created notification shows creator_username when provided."""
    with patch("modules.notifications.NOTIFICATION_CHANNEL_ID", "-100123456"):
        bot = AsyncMock()
        await send_notification(
            bot, "created", "my-drop", "1.2.3.4", "s-2vcpu-2gb", "2025-06-01 12:00:00", 42,
            creator_username="@testuser",
        )
        bot.send_message.assert_called_once()
        text = bot.send_message.call_args[1]["text"]
        assert "@testuser" in text
        assert "42" not in text


@pytest.mark.asyncio
async def test_created_falls_back_to_creator_id():
    """Created notification falls back to creator_id when no username."""
    with patch("modules.notifications.NOTIFICATION_CHANNEL_ID", "-100123456"):
        bot = AsyncMock()
        await send_notification(
            bot, "created", "my-drop", "1.2.3.4", "s-2vcpu-2gb", "2025-06-01 12:00:00", 42,
        )
        bot.send_message.assert_called_once()
        text = bot.send_message.call_args[1]["text"]
        assert "42" in text


@pytest.mark.asyncio
async def test_created_shows_dns():
    """Created notification shows DNS name when provided."""
    with patch("modules.notifications.NOTIFICATION_CHANNEL_ID", "-100123456"):
        bot = AsyncMock()
        await send_notification(
            bot, "created", "my-drop", "1.2.3.4", "s-2vcpu-2gb", "2025-06-01 12:00:00", 42,
            domain_name="test.example.com",
        )
        bot.send_message.assert_called_once()
        text = bot.send_message.call_args[1]["text"]
        assert "test.example.com" in text
        assert "DNS" in text


@pytest.mark.asyncio
async def test_created_shows_cost():
    """Created notification shows cost when provided."""
    with patch("modules.notifications.NOTIFICATION_CHANNEL_ID", "-100123456"):
        bot = AsyncMock()
        await send_notification(
            bot, "created", "my-drop", "1.2.3.4", "s-2vcpu-2gb", "2025-06-01 12:00:00", 42,
            price_monthly=18.0,
        )
        bot.send_message.assert_called_once()
        text = bot.send_message.call_args[1]["text"]
        assert "18.0" in text
        assert "Стоимость" in text


@pytest.mark.asyncio
async def test_deleted_shows_username():
    """Deleted notification shows creator_username when provided."""
    with patch("modules.notifications.NOTIFICATION_CHANNEL_ID", "-100123456"):
        bot = AsyncMock()
        await send_notification(
            bot, "deleted", "my-drop", "1.2.3.4", "s-2vcpu-2gb", "2025-06-01 12:00:00", 42,
            creator_username="@admin",
        )
        bot.send_message.assert_called_once()
        text = bot.send_message.call_args[1]["text"]
        assert "@admin" in text


@pytest.mark.asyncio
async def test_auto_deleted_shows_username():
    """Auto-deleted notification shows creator_username when provided."""
    with patch("modules.notifications.NOTIFICATION_CHANNEL_ID", "-100123456"):
        bot = AsyncMock()
        await send_notification(
            bot, "auto_deleted", "my-drop", "1.2.3.4", "s-2vcpu-2gb", "2025-06-01 12:00:00", 42,
            creator_username="@admin",
        )
        bot.send_message.assert_called_once()
        text = bot.send_message.call_args[1]["text"]
        assert "@admin" in text


@pytest.mark.asyncio
async def test_snapshot_created_notification():
    """Snapshot created notification contains expected info."""
    with patch("modules.notifications.NOTIFICATION_CHANNEL_ID", "-100123456"):
        bot = AsyncMock()
        await send_notification(
            bot, "snapshot_created", "my-drop", "1.2.3.4", "s-2vcpu-2gb", "2025-06-01 12:00:00", 42,
            creator_username="@admin",
        )
        bot.send_message.assert_called_once()
        text = bot.send_message.call_args[1]["text"]
        assert "Снэпшот" in text
        assert "my-drop" in text
        assert "@admin" in text
