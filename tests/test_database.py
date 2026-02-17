from datetime import datetime, timedelta

from modules.database import (
    init_db,
    save_instance,
    get_instance_by_id,
    delete_instance,
    extend_instance_expiration,
    get_instances_by_creator,
    get_expiring_instances,
    update_instance_dns,
)


class TestInitDb:
    def test_creates_table(self, tmp_db):
        init_db()
        # Should not raise on second call
        init_db()


class TestSaveAndGet:
    def test_round_trip(self, tmp_db):
        init_db()
        exp = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
        save_instance(123, "test-droplet", "1.2.3.4", "s-2vcpu-2gb", exp, 456, 789)

        instance = get_instance_by_id(123)
        assert instance is not None
        assert instance["droplet_id"] == 123
        assert instance["name"] == "test-droplet"
        assert instance["ip_address"] == "1.2.3.4"
        assert instance["droplet_type"] == "s-2vcpu-2gb"
        assert instance["ssh_key_id"] == 456
        assert instance["creator_id"] == 789

    def test_get_missing(self, tmp_db):
        init_db()
        assert get_instance_by_id(999) is None

    def test_save_with_creator_username(self, tmp_db):
        init_db()
        exp = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
        save_instance(124, "test-drop", "1.2.3.4", "s-2vcpu-2gb", exp, 456, 789, creator_username="@testuser")

        instance = get_instance_by_id(124)
        assert instance is not None
        assert instance["creator_username"] == "@testuser"

    def test_save_without_creator_username(self, tmp_db):
        init_db()
        exp = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
        save_instance(125, "test-drop", "1.2.3.4", "s-2vcpu-2gb", exp, 456, 789)

        instance = get_instance_by_id(125)
        assert instance is not None
        assert instance["creator_username"] is None


class TestDeleteInstance:
    def test_delete_existing(self, tmp_db):
        init_db()
        exp = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        save_instance(100, "drop", "0.0.0.0", "s-2vcpu-2gb", exp, 1, 1)

        assert delete_instance(100) is True
        assert get_instance_by_id(100) is None

    def test_delete_not_found(self, tmp_db):
        init_db()
        assert delete_instance(999) is False


class TestExtendExpiration:
    def test_extends_correctly(self, tmp_db):
        init_db()
        exp = datetime(2025, 6, 1, 12, 0, 0)
        save_instance(200, "ext", "0.0.0.0", "s-2vcpu-2gb", exp.strftime("%Y-%m-%d %H:%M:%S"), 1, 1)

        new_exp = extend_instance_expiration(200, 5)
        assert new_exp is not None
        expected = (exp + timedelta(days=5)).strftime("%Y-%m-%d %H:%M:%S")
        assert new_exp == expected

    def test_extend_missing_id(self, tmp_db):
        init_db()
        assert extend_instance_expiration(999, 3) is None


class TestGetInstancesByCreator:
    def test_returns_matching_instances(self, tmp_db):
        init_db()
        exp1 = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
        exp2 = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
        save_instance(10, "drop-a", "1.1.1.1", "s-2vcpu-2gb", exp1, 1, 42)
        save_instance(20, "drop-b", "2.2.2.2", "s-2vcpu-4gb", exp2, 1, 42)
        save_instance(30, "drop-c", "3.3.3.3", "s-2vcpu-2gb", exp1, 1, 99)

        result = get_instances_by_creator(42)
        assert len(result) == 2
        assert result[0]["name"] == "drop-a"
        assert result[1]["name"] == "drop-b"

    def test_returns_empty_list(self, tmp_db):
        init_db()
        result = get_instances_by_creator(999)
        assert result == []

    def test_returns_dicts(self, tmp_db):
        init_db()
        exp = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        save_instance(50, "d", "0.0.0.0", "s-2vcpu-2gb", exp, 1, 77)

        result = get_instances_by_creator(77)
        assert len(result) == 1
        assert isinstance(result[0], dict)
        assert "droplet_id" in result[0]
        assert "name" in result[0]


class TestGetExpiringInstances:
    def test_returns_dicts(self, tmp_db):
        init_db()
        # Instance expiring soon (within 24h)
        exp = (datetime.now() + timedelta(hours=12)).strftime("%Y-%m-%d %H:%M:%S")
        save_instance(300, "expiring", "1.1.1.1", "s-2vcpu-2gb", exp, 1, 42, creator_username="@user")

        result = get_expiring_instances()
        assert len(result) == 1
        assert isinstance(result[0], dict)
        assert result[0]["droplet_id"] == 300
        assert result[0]["name"] == "expiring"
        assert result[0]["creator_username"] == "@user"

    def test_includes_dns_columns(self, tmp_db):
        init_db()
        exp = (datetime.now() + timedelta(hours=6)).strftime("%Y-%m-%d %H:%M:%S")
        save_instance(301, "dns-test", "2.2.2.2", "s-2vcpu-2gb", exp, 1, 42)
        update_instance_dns(301, "test.example.com", 12345, "example.com")

        result = get_expiring_instances()
        assert len(result) == 1
        assert result[0]["domain_name"] == "test.example.com"
        assert result[0]["dns_record_id"] == 12345
        assert result[0]["dns_zone"] == "example.com"


class TestSaveWithPricing:
    def test_save_with_pricing_data(self, tmp_db):
        init_db()
        exp = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
        created = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        save_instance(
            126, "priced-drop", "1.2.3.4", "s-2vcpu-2gb", exp, 456, 789,
            created_at=created, price_hourly=0.02679,
        )
        instance = get_instance_by_id(126)
        assert instance is not None
        assert instance["created_at"] == created
        assert abs(instance["price_hourly"] - 0.02679) < 0.0001

    def test_save_without_pricing_data(self, tmp_db):
        """Old-style save without pricing — columns default to None."""
        init_db()
        exp = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
        save_instance(127, "no-price", "1.2.3.4", "s-2vcpu-2gb", exp, 456, 789)
        instance = get_instance_by_id(127)
        assert instance is not None
        assert instance["created_at"] is None
        assert instance["price_hourly"] is None

    def test_pricing_in_creator_list(self, tmp_db):
        init_db()
        exp = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        created = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        save_instance(
            128, "list-drop", "5.5.5.5", "s-2vcpu-4gb", exp, 1, 42,
            created_at=created, price_hourly=0.05,
        )
        result = get_instances_by_creator(42)
        assert len(result) == 1
        assert result[0]["created_at"] == created
        assert result[0]["price_hourly"] == 0.05

    def test_pricing_in_expiring_list(self, tmp_db):
        init_db()
        exp = (datetime.now() + timedelta(hours=6)).strftime("%Y-%m-%d %H:%M:%S")
        created = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        save_instance(
            129, "expiring-priced", "6.6.6.6", "s-2vcpu-2gb", exp, 1, 42,
            created_at=created, price_hourly=0.03,
        )
        result = get_expiring_instances()
        assert len(result) == 1
        assert result[0]["created_at"] == created
        assert result[0]["price_hourly"] == 0.03


class TestUpdateInstanceDns:
    def test_updates_dns_info(self, tmp_db):
        init_db()
        exp = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
        save_instance(400, "dns-drop", "1.2.3.4", "s-2vcpu-2gb", exp, 1, 42)

        result = update_instance_dns(400, "sub.example.com", 99999, "example.com")
        assert result is True

        instance = get_instance_by_id(400)
        assert instance["domain_name"] == "sub.example.com"
        assert instance["dns_record_id"] == 99999
        assert instance["dns_zone"] == "example.com"

    def test_update_nonexistent_instance(self, tmp_db):
        init_db()
        # Should still return True (UPDATE succeeds with 0 rows affected)
        result = update_instance_dns(999, "sub.example.com", 99999, "example.com")
        assert result is True
