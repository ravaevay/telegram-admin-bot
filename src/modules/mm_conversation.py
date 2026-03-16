"""In-memory conversation state manager for Mattermost bot.

Replaces Telegram's ConversationHandler with a simple state machine
that tracks per-user conversation flow, state, and data.
"""

import logging
import time

logger = logging.getLogger(__name__)

CONVERSATION_TIMEOUT = 600  # 10 minutes


class ConversationState:
    """Holds state for a single user's active conversation."""

    __slots__ = ("flow_name", "state", "data", "last_activity")

    def __init__(self, flow_name, state, data=None):
        self.flow_name = flow_name
        self.state = state
        self.data = data or {}
        self.last_activity = time.time()

    def touch(self):
        """Update last activity timestamp."""
        self.last_activity = time.time()

    def is_expired(self, timeout=CONVERSATION_TIMEOUT):
        return (time.time() - self.last_activity) > timeout


class ConversationManager:
    """Maps user_id → ConversationState, with timeout cleanup."""

    def __init__(self, timeout=CONVERSATION_TIMEOUT):
        self._conversations = {}
        self._timeout = timeout

    def start(self, user_id, flow_name, initial_state, data=None):
        """Start a new conversation for a user (replaces any existing one)."""
        conv = ConversationState(flow_name, initial_state, data)
        self._conversations[user_id] = conv
        logger.debug(f"Conversation started: user={user_id}, flow={flow_name}, state={initial_state}")
        return conv

    def get(self, user_id):
        """Get active conversation for user, or None if expired/missing."""
        conv = self._conversations.get(user_id)
        if conv is None:
            return None
        if conv.is_expired(self._timeout):
            logger.debug(f"Conversation expired: user={user_id}, flow={conv.flow_name}")
            del self._conversations[user_id]
            return None
        return conv

    def update_state(self, user_id, new_state):
        """Update conversation state and touch timestamp."""
        conv = self.get(user_id)
        if conv:
            conv.state = new_state
            conv.touch()
            return True
        return False

    def end(self, user_id):
        """End a conversation for a user."""
        conv = self._conversations.pop(user_id, None)
        if conv:
            logger.debug(f"Conversation ended: user={user_id}, flow={conv.flow_name}")
        return conv is not None

    def cleanup_expired(self):
        """Remove all expired conversations. Call periodically."""
        expired = [uid for uid, conv in self._conversations.items() if conv.is_expired(self._timeout)]
        for uid in expired:
            del self._conversations[uid]
        if expired:
            logger.info(f"Cleaned up {len(expired)} expired conversations")
        return len(expired)

    def active_count(self):
        """Return number of active (non-expired) conversations."""
        return sum(1 for conv in self._conversations.values() if not conv.is_expired(self._timeout))
