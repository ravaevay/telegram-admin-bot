import os
import sys
import tempfile

import pytest

# Set required env vars BEFORE any src/ module is imported.
# config.py runs load_dotenv() and int() on env vars at import time.
os.environ.setdefault("BOT_TOKEN", "fake-token")
os.environ.setdefault("SSH_HOST", "127.0.0.1")
os.environ.setdefault("SSH_PORT", "22")
os.environ.setdefault("SSH_USERNAME", "test")
os.environ.setdefault("SSH_KEY_PATH", "/tmp/fake.pem")
os.environ.setdefault("DIGITALOCEAN_TOKEN", "fake-do-token")
os.environ.setdefault("AUTHORIZED_MAIL_USERS", "1")
os.environ.setdefault("AUTHORIZED_DROPLET_USERS", "1")
os.environ.setdefault("MAIL_DEFAULT_DOMAIN", "example.com")
os.environ.setdefault("MAIL_DB_USER", "test")
os.environ.setdefault("MAIL_DB_PASSWORD", "test")
os.environ.setdefault("NOTIFICATION_CHANNEL_ID", "")

# Add src/ to sys.path so test imports work
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


@pytest.fixture()
def tmp_db(monkeypatch):
    """Provide a temporary database file and patch database.DB_PATH."""
    import modules.database as db_mod

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    monkeypatch.setattr(db_mod, "DB_PATH", path)
    yield path
    os.unlink(path)
