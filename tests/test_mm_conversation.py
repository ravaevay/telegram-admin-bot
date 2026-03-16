import time

from modules.mm_conversation import ConversationManager, ConversationState


class TestConversationState:
    def test_initial_state(self):
        conv = ConversationState("mail_create", "mail_input")
        assert conv.flow_name == "mail_create"
        assert conv.state == "mail_input"
        assert conv.data == {}
        assert not conv.is_expired()

    def test_data_storage(self):
        conv = ConversationState("droplet_create", "select_ssh", data={"foo": "bar"})
        assert conv.data["foo"] == "bar"

    def test_touch_updates_timestamp(self):
        conv = ConversationState("test", "s1")
        old_ts = conv.last_activity
        time.sleep(0.01)
        conv.touch()
        assert conv.last_activity > old_ts

    def test_is_expired(self):
        conv = ConversationState("test", "s1")
        assert not conv.is_expired(timeout=10)
        conv.last_activity = time.time() - 20
        assert conv.is_expired(timeout=10)


class TestConversationManager:
    def test_start_and_get(self):
        mgr = ConversationManager()
        mgr.start("user1", "mail_create", "mail_input")
        conv = mgr.get("user1")
        assert conv is not None
        assert conv.flow_name == "mail_create"
        assert conv.state == "mail_input"

    def test_get_missing(self):
        mgr = ConversationManager()
        assert mgr.get("nonexistent") is None

    def test_get_expired(self):
        mgr = ConversationManager(timeout=0.01)
        mgr.start("user1", "test", "s1")
        time.sleep(0.02)
        assert mgr.get("user1") is None

    def test_update_state(self):
        mgr = ConversationManager()
        mgr.start("user1", "droplet", "ssh")
        mgr.update_state("user1", "image")
        conv = mgr.get("user1")
        assert conv.state == "image"

    def test_update_state_missing(self):
        mgr = ConversationManager()
        assert mgr.update_state("nonexistent", "s1") is False

    def test_end(self):
        mgr = ConversationManager()
        mgr.start("user1", "test", "s1")
        assert mgr.end("user1") is True
        assert mgr.get("user1") is None

    def test_end_missing(self):
        mgr = ConversationManager()
        assert mgr.end("nonexistent") is False

    def test_start_replaces_existing(self):
        mgr = ConversationManager()
        mgr.start("user1", "flow_a", "s1")
        mgr.start("user1", "flow_b", "s2")
        conv = mgr.get("user1")
        assert conv.flow_name == "flow_b"
        assert conv.state == "s2"

    def test_cleanup_expired(self):
        mgr = ConversationManager(timeout=0.01)
        mgr.start("user1", "a", "s1")
        mgr.start("user2", "b", "s2")
        time.sleep(0.02)
        mgr.start("user3", "c", "s3")  # still active
        removed = mgr.cleanup_expired()
        assert removed == 2
        assert mgr.get("user3") is not None

    def test_active_count(self):
        mgr = ConversationManager()
        mgr.start("user1", "a", "s1")
        mgr.start("user2", "b", "s2")
        assert mgr.active_count() == 2

    def test_data_persists_through_state_change(self):
        mgr = ConversationManager()
        conv = mgr.start("user1", "droplet", "ssh", data={"key": "val"})
        conv.data["extra"] = 42
        mgr.update_state("user1", "image")
        updated = mgr.get("user1")
        assert updated.data["key"] == "val"
        assert updated.data["extra"] == 42
