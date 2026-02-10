from unittest.mock import patch

from modules.authorization import is_authorized, is_authorized_for_bot


class TestIsAuthorized:
    @patch("modules.authorization.AUTHORIZED_GROUPS", {"mail": [1, 2], "droplet": [3]})
    def test_authorized_user(self):
        assert is_authorized(1, "mail") is True

    @patch("modules.authorization.AUTHORIZED_GROUPS", {"mail": [1, 2], "droplet": [3]})
    def test_unauthorized_user(self):
        assert is_authorized(99, "mail") is False

    @patch("modules.authorization.AUTHORIZED_GROUPS", {"mail": [1]})
    def test_missing_module(self):
        assert is_authorized(1, "nonexistent") is False


class TestIsAuthorizedForBot:
    @patch("modules.authorization.AUTHORIZED_GROUPS", {"mail": [1], "droplet": [2]})
    def test_user_in_any_group(self):
        assert is_authorized_for_bot(2) is True

    @patch("modules.authorization.AUTHORIZED_GROUPS", {"mail": [1], "droplet": [2]})
    def test_user_in_no_group(self):
        assert is_authorized_for_bot(99) is False
