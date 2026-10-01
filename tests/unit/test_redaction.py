from ordering.common.redaction import redact, redact_text


def test_redacts_secrets_and_pii():
    text = ("mail priya@acme.example, card 4242 4242 4242 4242, call +1 512 555 0100, "
            "key sk-ABCDEFGHIJKLMNOPQRSTUV, auth Bearer abcdefghijklmnopqrstuvwxyz")
    out = redact_text(text)
    for secret in ("priya@acme.example", "4242 4242 4242 4242", "555 0100", "sk-ABCDEF", "abcdefghijklmnop"):
        assert secret not in out


def test_non_luhn_numbers_are_kept():
    assert "1234 5678 9012 3456" in redact_text("order ref 1234 5678 9012 3456")


def test_sensitive_keys_masked_recursively():
    out = redact({"authorization": "Bearer x", "nested": [{"password": "p", "qty": 3}]})
    assert out["authorization"] == "[REDACTED]" and out["nested"][0] == {"password": "[REDACTED]", "qty": 3}
