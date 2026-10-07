from knowvia.audit import make_audit_event, sanitize_arguments


def test_audit_redacts_secret_fields_and_inline_secrets() -> None:
    values = sanitize_arguments({"api_token":"abc", "description":"password=hidden VPN broken"})
    assert values["api_token"] == "[REDACTED]"
    assert "hidden" not in values["description"]
    event = make_audit_event("PROPOSED", "tool", values)
    assert event.event_id and event.timestamp and event.tool_name == "tool"
