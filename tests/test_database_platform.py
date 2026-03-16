"""Tests for the platform column in instances and k8s_clusters tables."""

from datetime import datetime, timedelta

from modules.database import (
    init_db,
    save_instance,
    get_instance_by_id,
    get_expiring_instances,
    save_k8s_cluster,
    get_expiring_k8s_clusters,
    get_provisioning_k8s_clusters,
)


def _exp(hours=12):
    return (datetime.now() + timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")


def _created_at():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class TestInstancePlatform:
    def test_default_platform_telegram(self, tmp_db):
        init_db()
        save_instance(1, "drop1", "1.1.1.1", "s-2vcpu-2gb", _exp(), 1, 42)
        inst = get_instance_by_id(1)
        assert inst["platform"] == "telegram"

    def test_explicit_mattermost_platform(self, tmp_db):
        init_db()
        save_instance(2, "drop2", "2.2.2.2", "s-2vcpu-2gb", _exp(), 1, 42, platform="mattermost")
        inst = get_instance_by_id(2)
        assert inst["platform"] == "mattermost"

    def test_expiring_filter_by_platform(self, tmp_db):
        init_db()
        save_instance(10, "tg-drop", "1.1.1.1", "s-2vcpu-2gb", _exp(), 1, 42, platform="telegram")
        save_instance(11, "mm-drop", "2.2.2.2", "s-2vcpu-2gb", _exp(), 1, 42, platform="mattermost")

        tg = get_expiring_instances(platform="telegram")
        mm = get_expiring_instances(platform="mattermost")
        all_inst = get_expiring_instances()

        assert len(tg) == 1
        assert tg[0]["name"] == "tg-drop"
        assert len(mm) == 1
        assert mm[0]["name"] == "mm-drop"
        assert len(all_inst) == 2

    def test_expiring_no_platform_filter_returns_all(self, tmp_db):
        init_db()
        save_instance(20, "a", "1.1.1.1", "s-2vcpu-2gb", _exp(), 1, 42, platform="telegram")
        save_instance(21, "b", "2.2.2.2", "s-2vcpu-2gb", _exp(), 1, 42, platform="mattermost")
        result = get_expiring_instances()
        assert len(result) == 2


class TestK8sPlatform:
    def _save(self, cluster_id, platform="telegram", status="provisioning", days=0):
        save_k8s_cluster(
            cluster_id=cluster_id,
            cluster_name=f"cluster-{cluster_id}",
            region="fra1",
            version="1.29.0-do.0",
            node_size="s-2vcpu-4gb",
            node_count=2,
            status=status,
            endpoint="",
            creator_id=42,
            expiration_date=(datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S"),
            created_at=_created_at(),
            platform=platform,
        )

    def test_default_platform_telegram(self, tmp_db):
        init_db()
        self._save("k-1")
        from modules.database import get_k8s_cluster_by_id

        cluster = get_k8s_cluster_by_id("k-1")
        assert cluster["platform"] == "telegram"

    def test_explicit_mattermost_platform(self, tmp_db):
        init_db()
        self._save("k-2", platform="mattermost")
        from modules.database import get_k8s_cluster_by_id

        cluster = get_k8s_cluster_by_id("k-2")
        assert cluster["platform"] == "mattermost"

    def test_expiring_filter_by_platform(self, tmp_db):
        init_db()
        self._save("k-tg", platform="telegram", status="running", days=0)
        self._save("k-mm", platform="mattermost", status="running", days=0)

        tg = get_expiring_k8s_clusters(platform="telegram")
        mm = get_expiring_k8s_clusters(platform="mattermost")

        assert len(tg) == 1
        assert tg[0]["cluster_id"] == "k-tg"
        assert len(mm) == 1
        assert mm[0]["cluster_id"] == "k-mm"

    def test_provisioning_filter_by_platform(self, tmp_db):
        init_db()
        self._save("p-tg", platform="telegram", status="provisioning")
        self._save("p-mm", platform="mattermost", status="provisioning")

        tg = get_provisioning_k8s_clusters(platform="telegram")
        mm = get_provisioning_k8s_clusters(platform="mattermost")

        assert len(tg) == 1
        assert tg[0]["cluster_id"] == "p-tg"
        assert len(mm) == 1
        assert mm[0]["cluster_id"] == "p-mm"

    def test_provisioning_no_filter_returns_all(self, tmp_db):
        init_db()
        self._save("p-a", platform="telegram", status="provisioning")
        self._save("p-b", platform="mattermost", status="provisioning")
        result = get_provisioning_k8s_clusters()
        assert len(result) == 2
