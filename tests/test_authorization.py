from unittest.mock import patch

from modules.authorization import is_authorized, is_authorized_for_bot, is_global_admin
from modules.database import init_db, grant_permission


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


class TestGlobalAdmin:
    @patch("modules.authorization.GLOBAL_ADMIN_USERS", [42])
    def test_is_global_admin(self):
        assert is_global_admin(42) is True
        assert is_global_admin(43) is False

    @patch("modules.authorization.GLOBAL_ADMIN_USERS", [42])
    @patch("modules.authorization.AUTHORIZED_GROUPS", {"mail": [], "droplet": [], "k8s": [], "stand": []})
    def test_global_admin_authorized_for_all_modules(self, tmp_db):
        init_db()
        assert is_authorized(42, "mail") is True
        assert is_authorized(42, "stand") is True
        assert is_authorized_for_bot(42) is True


class TestDbGrantedPermissions:
    @patch("modules.authorization.GLOBAL_ADMIN_USERS", [])
    @patch("modules.authorization.AUTHORIZED_GROUPS", {"mail": [], "droplet": [], "k8s": [], "stand": []})
    def test_db_grant_authorizes_module(self, tmp_db):
        init_db()
        assert is_authorized(500, "stand") is False
        grant_permission(500, "stand", granted_by=42)
        assert is_authorized(500, "stand") is True
        assert is_authorized(500, "k8s") is False

    @patch("modules.authorization.GLOBAL_ADMIN_USERS", [])
    @patch("modules.authorization.AUTHORIZED_GROUPS", {"mail": [], "droplet": [], "k8s": [], "stand": []})
    def test_db_grant_authorizes_for_bot(self, tmp_db):
        init_db()
        assert is_authorized_for_bot(500) is False
        grant_permission(500, "k8s", granted_by=42)
        assert is_authorized_for_bot(500) is True
