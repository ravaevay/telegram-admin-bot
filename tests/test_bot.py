from bot import _build_ssh_key_keyboard


def _make_keys(n):
    return [{"id": i + 1, "name": f"key{i + 1}"} for i in range(n)]


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
