from modules.create_test_instance import _sanitize_tag


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
