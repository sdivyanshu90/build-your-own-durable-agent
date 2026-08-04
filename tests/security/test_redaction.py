from durable_agent.security.redaction import SecretRedactor


def test_redacts_keys_tokens_urls_and_explicit_values() -> None:
    redactor = SecretRedactor(("unique-value",))
    value = {
        "password": "hello",
        "nested": [
            "Bearer abcdefghijklmnop",
            "postgresql://user:password@example.test/db",
            "unique-value",
        ],
        "safe": "kept",
    }
    redacted = redactor.redact(value)
    assert redacted["password"] == "[REDACTED]"
    assert redacted["nested"] == [
        "[REDACTED]",
        "postgresql://user:[REDACTED]@example.test/db",
        "[REDACTED]",
    ]
    assert redacted["safe"] == "kept"


def test_log_injection_characters_are_neutralized() -> None:
    assert SecretRedactor().redact_text("line\rforged\x00") == "line\\rforged"
