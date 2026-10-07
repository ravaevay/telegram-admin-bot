from unittest.mock import AsyncMock, MagicMock

import pytest

from bot import SUBDOMAIN_RE, STAND_INPUT_PARAM, _build_ssh_key_keyboard, stand_input_subdomain


def _make_keys(n):
    return [{"id": i + 1, "name": f"key{i + 1}"} for i in range(n)]


def _reorder_keys(ssh_keys, preferred_ids):
    """Reproduce the reordering logic from droplet_entry() for testing."""
    if preferred_ids:
        available_ids = {k["id"] for k in ssh_keys}
        valid_preferred = [pid for pid in preferred_ids if pid in available_ids]
        preferred_set = set(valid_preferred)
        preferred_keys = [k for pid in valid_preferred for k in ssh_keys if k["id"] == pid]
        remaining_keys = [k for k in ssh_keys if k["id"] not in preferred_set]
        ssh_keys = preferred_keys + remaining_keys
        preselect = {str(pid) for pid in valid_preferred[:3]}
    else:
        preselect = {str(k["id"]) for k in ssh_keys[:3]}
    return ssh_keys, preselect


class TestBuildSshKeyKeyboard:
    def test_3_keys_all_selected_no_expand(self):
        keys = _make_keys(3)
        markup = _build_ssh_key_keyboard(keys, {"1", "2", "3"}, expanded=False)
        rows = markup.inline_keyboard
        assert len(rows) == 4  # 3 key rows + 1 confirm
        # No "Другие ключи" button anywhere
        all_texts = [btn.text for row in rows for btn in row]
        assert not any("Другие ключи" in t for t in all_texts)

    def test_5_keys_collapsed(self):
        keys = _make_keys(5)
        markup = _build_ssh_key_keyboard(keys, {"1", "2", "3"}, expanded=False)
        rows = markup.inline_keyboard
        assert len(rows) == 5  # 3 key rows + 1 expand + 1 confirm
        expand_btn = rows[3][0]
        assert "Другие ключи" in expand_btn.text
        assert "(2)" in expand_btn.text
        assert expand_btn.callback_data == "ssh_more_keys"

    def test_5_keys_expanded(self):
        keys = _make_keys(5)
        markup = _build_ssh_key_keyboard(keys, {"1", "2", "3"}, expanded=True)
        rows = markup.inline_keyboard
        assert len(rows) == 6  # 5 key rows + 1 confirm
        all_texts = [btn.text for row in rows for btn in row]
        assert not any("Другие ключи" in t for t in all_texts)

    def test_selected_checkmark_prefix(self):
        keys = _make_keys(1)
        markup = _build_ssh_key_keyboard(keys, {"1"}, expanded=False)
        btn = markup.inline_keyboard[0][0]
        assert btn.text.startswith("✅")

    def test_unselected_empty_prefix(self):
        keys = _make_keys(1)
        markup = _build_ssh_key_keyboard(keys, set(), expanded=False)
        btn = markup.inline_keyboard[0][0]
        assert btn.text.startswith("⬜")

    def test_confirm_shows_count(self):
        keys = _make_keys(3)
        markup = _build_ssh_key_keyboard(keys, {"1", "3"}, expanded=False)
        confirm_btn = markup.inline_keyboard[-1][0]
        assert "(2)" in confirm_btn.text
        assert confirm_btn.callback_data == "ssh_confirm"

    def test_single_key(self):
        keys = _make_keys(1)
        markup = _build_ssh_key_keyboard(keys, {"1"}, expanded=False)
        rows = markup.inline_keyboard
        assert len(rows) == 2  # 1 key row + 1 confirm


class TestSshKeyPreferenceReordering:
    def test_preferred_keys_moved_to_front(self):
        keys = _make_keys(5)  # ids: 1,2,3,4,5
        preferred_ids = [4, 5]
        reordered, preselect = _reorder_keys(keys, preferred_ids)
        assert reordered[0]["id"] == 4
        assert reordered[1]["id"] == 5
        assert preselect == {"4", "5"}

    def test_deleted_preferred_key_skipped(self):
        keys = _make_keys(3)  # ids: 1,2,3
        preferred_ids = [99, 2]  # 99 doesn't exist in DO
        reordered, preselect = _reorder_keys(keys, preferred_ids)
        assert reordered[0]["id"] == 2
        assert 99 not in {k["id"] for k in reordered}
        assert preselect == {"2"}

    def test_preselection_respects_valid_keys_only(self):
        keys = _make_keys(5)  # ids: 1,2,3,4,5
        preferred_ids = [5, 99, 3, 100, 1]  # 99, 100 don't exist
        reordered, preselect = _reorder_keys(keys, preferred_ids)
        assert reordered[0]["id"] == 5
        assert reordered[1]["id"] == 3
        assert reordered[2]["id"] == 1
        assert preselect == {"5", "3", "1"}


class TestSubdomainRegex:
    """SUBDOMAIN_RE encodes the lower-case DNS label rule the stand workflows enforce."""

    def test_accepts_plain_label(self):
        assert SUBDOMAIN_RE.match("popov-chamilo")

    def test_accepts_single_character(self):
        assert SUBDOMAIN_RE.match("a")

    def test_accepts_max_length(self):
        assert SUBDOMAIN_RE.match("a" * 63)

    def test_rejects_too_long(self):
        assert not SUBDOMAIN_RE.match("a" * 64)

    def test_rejects_uppercase(self):
        assert not SUBDOMAIN_RE.match("Popov-Chamilo")

    def test_rejects_leading_hyphen(self):
        assert not SUBDOMAIN_RE.match("-chamilo")

    def test_rejects_trailing_hyphen(self):
        assert not SUBDOMAIN_RE.match("chamilo-")

    def test_rejects_dot(self):
        assert not SUBDOMAIN_RE.match("stand.chamilo")


@pytest.mark.asyncio
class TestStandSubdomainNormalization:
    """Mixed-case input is normalized before it reaches the workflow dispatch."""

    @staticmethod
    def _make_update(text):
        update = MagicMock()
        update.message.text = text
        update.message.reply_text = AsyncMock()
        return update

    @staticmethod
    def _make_context():
        context = MagicMock()
        context.user_data = {"stand_service": "wordpress"}
        return context

    async def test_uppercase_input_is_lowercased(self):
        update, context = self._make_update("  Popov-Chamilo  "), self._make_context()

        state = await stand_input_subdomain(update, context)

        assert state == STAND_INPUT_PARAM
        assert context.user_data["stand_subdomain"] == "popov-chamilo"

    async def test_invalid_subdomain_reprompts(self):
        update, context = self._make_update("bad_subdomain"), self._make_context()

        await stand_input_subdomain(update, context)

        assert "stand_subdomain" not in context.user_data
        update.message.reply_text.assert_awaited_once()
