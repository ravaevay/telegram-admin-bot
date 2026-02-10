from datetime import datetime, timedelta

from modules.database import (
    init_db,
    save_instance,
    get_instance_by_id,
    delete_instance,
    extend_instance_expiration,
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
