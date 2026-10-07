from starlette.testclient import TestClient

import knowvia.web as web_module
from knowvia.web import WebAuthenticationConfig, create_web_app


DEV_AUTH = WebAuthenticationConfig.local_development()


class StubService:
    def identities(self):
        return [{"id": "employee-001", "name": "Demo Employee"}]

    def chat(self, session_id, message, identity_id, demo=False):
        return {
            "kind": "KNOWLEDGE",
            "text": "You receive 24 paid vacation days.",
            "sources": [{"document": "Employee Handbook", "section": "Annual Leave", "source": "01_employee_handbook.md"}],
        }

    def confirm(self, proposal_id, identity_id, demo=False):
        return {"kind": "ACTION_SUCCESS", "text": "Done.", "sources": [], "ticket": {"ticket_id": "IT-1", "status": "OPEN"}}

    def cancel(self, proposal_id, identity_id, demo=False):
        return {"kind": "CANCELLED", "text": "Cancelled.", "sources": []}

    def notifications(self, identity_id):
        return {"notifications": []}


def test_employee_ui_and_static_assets_are_served() -> None:
    client = TestClient(create_web_app(StubService(), authentication_config=DEV_AUTH))
    page = client.get("/")
    css = client.get("/static/app.css")
    js = client.get("/static/app.js")
    assert page.status_code == css.status_code == js.status_code == 200
    assert "Knowvia" in page.text and "New conversation" in page.text
    assert "Demo identity" in page.text
    assert "skill IDs" not in page.text


def test_chat_api_returns_employee_safe_structured_response() -> None:
    client = TestClient(create_web_app(StubService(), authentication_config=DEV_AUTH))
    response = client.post("/api/chat", json={
        "session_id": "demo-1", "identity_id": "employee-001",
        "message": "How many vacation days do I get?", "demo": False,
    })
    assert response.status_code == 200
    assert response.json() == {
        "kind": "KNOWLEDGE",
        "text": "You receive 24 paid vacation days.",
        "sources": [{"document": "Employee Handbook", "section": "Annual Leave", "source": "01_employee_handbook.md"}],
    }


def test_invalid_ui_payload_fails_closed_and_no_execute_endpoint_exists() -> None:
    client = TestClient(create_web_app(StubService(), authentication_config=DEV_AUTH))
    invalid = client.post("/api/chat", json={"session_id": "../bad", "identity_id": "employee-001", "message": "hello"})
    bypass = client.post("/api/proposals/proposal-1/execute", json={"identity_id": "employee-001"})
    assert invalid.status_code == 400
    assert invalid.json()["kind"] == "UNSUPPORTED"
    assert bypass.status_code == 404


def test_metadata_endpoints_do_not_initialize_expensive_agent_runtime(monkeypatch) -> None:
    def unexpected_embedder(_provider):
        raise AssertionError("Conversation metadata must not initialize embeddings")

    monkeypatch.setattr(web_module, "create_embedder", unexpected_embedder)
    client = TestClient(create_web_app(authentication_config=DEV_AUTH))

    config = client.get("/api/config")
    conversations = client.get("/api/conversations?identity_id=employee-001")

    assert config.status_code == conversations.status_code == 200
    assert isinstance(conversations.json()["conversations"], list)
