from modules.mail import validate_mailbox_name, generate_password, _escape_md, ensure_mailbox_format


class TestValidateMailboxName:
    def test_valid_simple(self):
        ok, err = validate_mailbox_name("john.doe")
        assert ok is True
        assert err == ""

    def test_valid_with_hyphens_underscores(self):
        ok, _ = validate_mailbox_name("test-user_01")
        assert ok is True

    def test_empty_name(self):
        ok, err = validate_mailbox_name("")
        assert ok is False
        assert "пустым" in err

    def test_too_long(self):
        ok, err = validate_mailbox_name("a" * 65)
        assert ok is False
        assert "длинное" in err

    def test_invalid_chars(self):
        ok, err = validate_mailbox_name("user name!")
        assert ok is False
        assert "недопустимые" in err

    def test_with_at_domain(self):
        ok, _ = validate_mailbox_name("user@example.com")
        assert ok is True

    def test_at_with_empty_local(self):
        ok, err = validate_mailbox_name("@example.com")
        assert ok is False


class TestGeneratePassword:
    def test_default_length(self):
        pwd = generate_password()
        assert len(pwd) == 10

    def test_custom_length(self):
        pwd = generate_password(16)
        assert len(pwd) == 16

    def test_charset(self):
        pwd = generate_password(100)
        assert pwd.isalnum()


class TestEscapeMd:
    def test_special_chars(self):
        assert _escape_md("hello_world") == r"hello\_world"
        assert _escape_md("a*b") == r"a\*b"
        assert _escape_md("1.2") == r"1\.2"

    def test_plain_text(self):
        assert _escape_md("hello") == "hello"


class TestEnsureMailboxFormat:
    def test_without_domain(self):
        result = ensure_mailbox_format("user")
        assert "@" in result
        assert result == "user@example.com"

    def test_with_domain(self):
        result = ensure_mailbox_format("user@custom.org")
        assert result == "user@custom.org"
