from datetime import datetime, timedelta

from modules.database import (
    init_db,
    save_k8s_cluster,
    get_k8s_cluster_by_id,
    get_k8s_cluster_by_name,
    get_k8s_clusters_by_creator,
    update_k8s_cluster_status,
    delete_k8s_cluster,
    get_expiring_k8s_clusters,
    get_provisioning_k8s_clusters,
    extend_k8s_cluster_expiration,
)

CLUSTER_ID = "abc-123-def"
CLUSTER_NAME = "test-cluster"
REGION = "fra1"
VERSION = "1.29.0-do.0"
NODE_SIZE = "s-2vcpu-4gb"
NODE_COUNT = 2
CREATOR_ID = 42


def _exp(days=7):
    return (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


def _created_at():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _save_default(
    cluster_id=CLUSTER_ID, cluster_name=CLUSTER_NAME, creator_id=CREATOR_ID, days=7, status="provisioning"
):
    save_k8s_cluster(
        cluster_id=cluster_id,
        cluster_name=cluster_name,
        region=REGION,
        version=VERSION,
        node_size=NODE_SIZE,
        node_count=NODE_COUNT,
        status=status,
        endpoint="",
        creator_id=creator_id,
        creator_username="@testuser",
        expiration_date=_exp(days),
        created_at=_created_at(),
        price_hourly=0.0714,
        ha=False,
    )


class TestInitDbK8s:
    def test_creates_k8s_table(self, tmp_db):
        init_db()
        # Second call must not raise (idempotent)
        init_db()


class TestSaveAndGetK8sCluster:
    def test_round_trip(self, tmp_db):
        init_db()
        _save_default()

        cluster = get_k8s_cluster_by_id(CLUSTER_ID)
        assert cluster is not None
        assert cluster["cluster_id"] == CLUSTER_ID
        assert cluster["cluster_name"] == CLUSTER_NAME
        assert cluster["region"] == REGION
        assert cluster["version"] == VERSION
        assert cluster["node_size"] == NODE_SIZE
        assert cluster["node_count"] == NODE_COUNT
        assert cluster["creator_id"] == CREATOR_ID
        assert cluster["creator_username"] == "@testuser"
        assert cluster["status"] == "provisioning"
        assert cluster["ha"] == 0

    def test_get_missing_returns_none(self, tmp_db):
        init_db()
        assert get_k8s_cluster_by_id("nonexistent-id") is None

    def test_price_and_endpoint_stored(self, tmp_db):
        init_db()
        save_k8s_cluster(
            cluster_id="price-test",
            cluster_name="price-cluster",
            region=REGION,
            version=VERSION,
            node_size=NODE_SIZE,
            node_count=3,
            status="running",
            endpoint="https://k8s.example.com",
            creator_id=CREATOR_ID,
            expiration_date=_exp(),
            created_at=_created_at(),
            price_hourly=0.1071,
        )
        cluster = get_k8s_cluster_by_id("price-test")
        assert cluster is not None
        assert abs(cluster["price_hourly"] - 0.1071) < 0.0001
        assert cluster["endpoint"] == "https://k8s.example.com"
        assert cluster["node_count"] == 3

    def test_ha_flag_stored(self, tmp_db):
        init_db()
        save_k8s_cluster(
            cluster_id="ha-test",
            cluster_name="ha-cluster",
            region=REGION,
            version=VERSION,
            node_size=NODE_SIZE,
            node_count=2,
            status="provisioning",
            endpoint="",
            creator_id=CREATOR_ID,
            expiration_date=_exp(),
            created_at=_created_at(),
            ha=True,
        )
        cluster = get_k8s_cluster_by_id("ha-test")
        assert cluster is not None
        assert cluster["ha"] == 1


class TestGetK8sClusterByName:
    def test_finds_active_cluster(self, tmp_db):
        init_db()
        _save_default()
        result = get_k8s_cluster_by_name(CLUSTER_NAME, CREATOR_ID)
        assert result is not None
        assert result["cluster_id"] == CLUSTER_ID

    def test_returns_none_for_different_creator(self, tmp_db):
        init_db()
        _save_default()
        result = get_k8s_cluster_by_name(CLUSTER_NAME, 999)
        assert result is None

    def test_returns_none_for_deleted_status(self, tmp_db):
        init_db()
        _save_default(status="deleted")
        result = get_k8s_cluster_by_name(CLUSTER_NAME, CREATOR_ID)
        assert result is None

    def test_returns_none_for_missing_name(self, tmp_db):
        init_db()
        result = get_k8s_cluster_by_name("no-such-cluster", CREATOR_ID)
        assert result is None


class TestGetK8sClustersByCreator:
    def test_returns_matching_clusters(self, tmp_db):
        init_db()
        _save_default("id-1", "cluster-a", CREATOR_ID, days=3)
        _save_default("id-2", "cluster-b", CREATOR_ID, days=7)
        _save_default("id-3", "cluster-c", 99, days=5)  # different user

        result = get_k8s_clusters_by_creator(CREATOR_ID)
        assert len(result) == 2
        names = {r["cluster_name"] for r in result}
        assert names == {"cluster-a", "cluster-b"}

    def test_excludes_deleted(self, tmp_db):
        init_db()
        _save_default("id-active", "active-cluster", CREATOR_ID, status="running")
        _save_default("id-deleted", "deleted-cluster", CREATOR_ID, status="deleted")

        result = get_k8s_clusters_by_creator(CREATOR_ID)
        assert len(result) == 1
        assert result[0]["cluster_name"] == "active-cluster"

    def test_returns_empty_for_unknown_user(self, tmp_db):
        init_db()
        result = get_k8s_clusters_by_creator(999)
        assert result == []

    def test_returns_dicts(self, tmp_db):
        init_db()
        _save_default()
        result = get_k8s_clusters_by_creator(CREATOR_ID)
        assert len(result) == 1
        assert isinstance(result[0], dict)
        assert "cluster_id" in result[0]
        assert "cluster_name" in result[0]


class TestUpdateK8sClusterStatus:
    def test_updates_status(self, tmp_db):
        init_db()
        _save_default()

        update_k8s_cluster_status(CLUSTER_ID, "running")
        cluster = get_k8s_cluster_by_id(CLUSTER_ID)
        assert cluster["status"] == "running"

    def test_updates_status_and_endpoint(self, tmp_db):
        init_db()
        _save_default()

        update_k8s_cluster_status(CLUSTER_ID, "running", endpoint="https://api.k8s.example.com")
        cluster = get_k8s_cluster_by_id(CLUSTER_ID)
        assert cluster["status"] == "running"
        assert cluster["endpoint"] == "https://api.k8s.example.com"

    def test_returns_true_on_success(self, tmp_db):
        init_db()
        _save_default()
        result = update_k8s_cluster_status(CLUSTER_ID, "running")
        assert result is True


class TestDeleteK8sCluster:
    def test_deletes_existing(self, tmp_db):
        init_db()
        _save_default()

        result = delete_k8s_cluster(CLUSTER_ID)
        assert result is True
        assert get_k8s_cluster_by_id(CLUSTER_ID) is None

    def test_returns_false_for_missing(self, tmp_db):
        init_db()
        result = delete_k8s_cluster("nonexistent-id")
        assert result is False


class TestGetExpiringK8sClusters:
    def test_includes_expiring_cluster(self, tmp_db):
        init_db()
        _save_default(days=0, status="running")  # already expired

        result = get_expiring_k8s_clusters()
        assert len(result) == 1
        assert result[0]["cluster_id"] == CLUSTER_ID

    def test_includes_expiring_within_24h(self, tmp_db):
        init_db()
        exp = (datetime.now() + timedelta(hours=12)).strftime("%Y-%m-%d %H:%M:%S")
        save_k8s_cluster(
            cluster_id="expiring-id",
            cluster_name="expiring-cluster",
            region=REGION,
            version=VERSION,
            node_size=NODE_SIZE,
            node_count=2,
            status="running",
            endpoint="",
            creator_id=CREATOR_ID,
            expiration_date=exp,
            created_at=_created_at(),
        )
        result = get_expiring_k8s_clusters()
        assert len(result) == 1

    def test_excludes_far_future(self, tmp_db):
        init_db()
        _save_default(days=7, status="running")  # 7 days future

        result = get_expiring_k8s_clusters()
        assert len(result) == 0

    def test_excludes_deleted_status(self, tmp_db):
        init_db()
        _save_default(days=0, status="deleted")  # deleted, expired

        result = get_expiring_k8s_clusters()
        assert len(result) == 0


class TestGetProvisioningK8sClusters:
    def test_returns_provisioning_only(self, tmp_db):
        init_db()
        _save_default("id-prov", "prov-cluster", status="provisioning")
        _save_default("id-run", "run-cluster", status="running")

        result = get_provisioning_k8s_clusters()
        assert len(result) == 1
        assert result[0]["cluster_id"] == "id-prov"

    def test_empty_when_none_provisioning(self, tmp_db):
        init_db()
        _save_default(status="running")
        result = get_provisioning_k8s_clusters()
        assert result == []


class TestExtendK8sClusterExpiration:
    def test_extends_correctly(self, tmp_db):
        init_db()
        exp = datetime(2025, 6, 1, 12, 0, 0)
        save_k8s_cluster(
            cluster_id="ext-test",
            cluster_name="ext-cluster",
            region=REGION,
            version=VERSION,
            node_size=NODE_SIZE,
            node_count=2,
            status="running",
            endpoint="",
            creator_id=CREATOR_ID,
            expiration_date=exp.strftime("%Y-%m-%d %H:%M:%S"),
            created_at=_created_at(),
        )
        new_exp = extend_k8s_cluster_expiration("ext-test", 5)
        expected = (exp + timedelta(days=5)).strftime("%Y-%m-%d %H:%M:%S")
        assert new_exp == expected

    def test_returns_none_for_missing(self, tmp_db):
        init_db()
        result = extend_k8s_cluster_expiration("nonexistent", 3)
        assert result is None
