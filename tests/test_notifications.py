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
