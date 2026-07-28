"""Tests for the runtime user_permissions table (module grants)."""

from modules.database import (
    init_db,
    grant_permission,
    revoke_permission,
    get_user_permissions,
    get_permission_users,
)


class TestGrantAndGet:
    def test_grant_and_get(self, tmp_db):
        init_db()
        assert get_user_permissions(100) == []
        assert grant_permission(100, "stand", granted_by=42) is True
        assert get_user_permissions(100) == ["stand"]

    def test_grant_is_idempotent(self, tmp_db):
        init_db()
        grant_permission(100, "stand", granted_by=42)
        grant_permission(100, "stand", granted_by=43)  # UPSERT, no duplicate row
        assert get_user_permissions(100) == ["stand"]

    def test_int_and_str_ids_match(self, tmp_db):
        init_db()
        grant_permission(100, "k8s", granted_by=42)
        assert get_user_permissions("100") == ["k8s"]  # TEXT column, str() comparison

    def test_multiple_modules(self, tmp_db):
        init_db()
        grant_permission(100, "stand", granted_by=42)
        grant_permission(100, "k8s", granted_by=42)
        assert set(get_user_permissions(100)) == {"stand", "k8s"}


class TestRevoke:
    def test_revoke(self, tmp_db):
        init_db()
        grant_permission(100, "stand", granted_by=42)
        assert revoke_permission(100, "stand") is True
        assert get_user_permissions(100) == []

    def test_revoke_missing_returns_false(self, tmp_db):
        init_db()
        assert revoke_permission(100, "stand") is False


class TestGetPermissionUsers:
    def test_lists_all_grants(self, tmp_db):
        init_db()
        grant_permission(100, "stand", granted_by=42)
        grant_permission(100, "k8s", granted_by=42)
        grant_permission(200, "mail", granted_by=42)
        users = get_permission_users()
        assert set(users.keys()) == {"100", "200"}
        assert set(users["100"]) == {"stand", "k8s"}
        assert users["200"] == ["mail"]

    def test_platform_filter(self, tmp_db):
        init_db()
        grant_permission(100, "stand", granted_by=42, platform="telegram")
        grant_permission(300, "stand", granted_by=42, platform="mattermost")
        assert set(get_permission_users(platform="telegram").keys()) == {"100"}
        assert set(get_permission_users(platform="mattermost").keys()) == {"300"}
