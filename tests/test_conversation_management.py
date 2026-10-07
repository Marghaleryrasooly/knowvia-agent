from __future__ import annotations

from datetime import datetime, timedelta, timezone
from threading import Event, Thread

from starlette.testclient import TestClient

from knowvia.conversation import ConversationTurn, SqliteConversationStore
from knowvia.identity import LocalIdentityProvider
from knowvia.web import KnowviaWebService, WebAuthenticationConfig, create_web_app


DEV_AUTH = WebAuthenticationConfig.local_development()


def turn(question: str, when: datetime) -> ConversationTurn:
    return ConversationTurn(question, question, "hr", "KNOWLEDGE", when)


def service_client(tmp_path):
    store = SqliteConversationStore(tmp_path / "conversations.db")
    service = KnowviaWebService(object(), LocalIdentityProvider(), store)
    return TestClient(create_web_app(service, authentication_config=DEV_AUTH)), store


class SlowConversationService(KnowviaWebService):
    def __init__(self, store, started: Event, release: Event):
        super().__init__(object(), LocalIdentityProvider(), store)
        self.started = started
        self.release = release

    def chat(self, session_id, message, identity_id, demo=False):
        self.started.set()
        assert self.release.wait(timeout=10)
        return {
            "kind": "KNOWLEDGE",
            "text": "Response owned by conversation A.",
            "sources": [],
        }


class FailingConversationService(KnowviaWebService):
    def __init__(self, store):
        super().__init__(object(), LocalIdentityProvider(), store)

    def chat(self, session_id, message, identity_id, demo=False):
        raise RuntimeError("simulated assistant failure")


def test_initial_history_listing_and_browser_reload_persistence(tmp_path) -> None:
    web, store = service_client(tmp_path)
    now = datetime.now(timezone.utc)
    store.append("vacation", turn("How many vacation days do I get?", now), "employee-001")
    store.append("vpn", turn("How do I fix the VPN?", now + timedelta(seconds=1)), "employee-001")

    first = web.get("/api/conversations?identity_id=employee-001")
    reopened = TestClient(create_web_app(KnowviaWebService(
        object(), LocalIdentityProvider(), SqliteConversationStore(store.path)
    ), authentication_config=DEV_AUTH))
    second = reopened.get("/api/conversations?identity_id=employee-001")

    assert first.status_code == second.status_code == 200
    assert [item["session_id"] for item in first.json()["conversations"]] == ["vpn", "vacation"]
    assert second.json() == first.json()


def test_history_and_mutations_are_scoped_to_trusted_identity(tmp_path) -> None:
    web, store = service_client(tmp_path)
    now = datetime.now(timezone.utc)
    store.append("shared-name", turn("Employee one conversation", now), "employee-001")
    store.append("shared-name", turn("Employee two conversation", now), "employee-002")

    employee_one = web.get("/api/conversations?identity_id=employee-001").json()
    employee_two = web.get("/api/conversations?identity_id=employee-002").json()
    cross_rename = web.patch(
        "/api/conversations/shared-name",
        json={"identity_id": "it-employee-001", "title": "Not mine"},
    )
    cross_delete = web.delete(
        "/api/conversations/shared-name?identity_id=it-employee-001"
    )

    assert [item["title"] for item in employee_one["conversations"]] == ["Employee one conversation"]
    assert [item["title"] for item in employee_two["conversations"]] == ["Employee two conversation"]
    assert cross_rename.status_code == cross_delete.status_code == 404
    assert store.load("shared-name", "employee-001").turns
    assert store.load("shared-name", "employee-002").turns


def test_pin_unpin_and_rename_persist_without_duplicates(tmp_path) -> None:
    web, store = service_client(tmp_path)
    now = datetime.now(timezone.utc)
    store.append("one", turn("First conversation", now), "employee-001")
    store.append("two", turn("Second conversation", now + timedelta(seconds=1)), "employee-001")

    pinned = web.patch(
        "/api/conversations/one",
        json={"identity_id": "employee-001", "pinned": True},
    )
    renamed = web.patch(
        "/api/conversations/one",
        json={"identity_id": "employee-001", "title": "  Vacation policy  "},
    )
    listed = web.get("/api/conversations?identity_id=employee-001").json()["conversations"]

    assert pinned.status_code == renamed.status_code == 200
    assert renamed.json()["conversation"]["title"] == "Vacation policy"
    assert [item["session_id"] for item in listed].count("one") == 1
    assert listed[0]["session_id"] == "one" and listed[0]["pinned"] is True

    unpinned = web.patch(
        "/api/conversations/one",
        json={"identity_id": "employee-001", "pinned": False},
    )
    persisted = SqliteConversationStore(store.path).list_conversations("employee-001")
    assert unpinned.status_code == 200
    assert next(item for item in persisted if item.session_id == "one").pinned is False
    assert next(item for item in persisted if item.session_id == "one").title == "Vacation policy"


def test_rename_rejects_empty_and_overlong_titles(tmp_path) -> None:
    web, store = service_client(tmp_path)
    now = datetime.now(timezone.utc)
    store.append("rename", turn("Original title", now), "employee-001")

    empty = web.patch(
        "/api/conversations/rename",
        json={"identity_id": "employee-001", "title": "   "},
    )
    long = web.patch(
        "/api/conversations/rename",
        json={"identity_id": "employee-001", "title": "x" * 81},
    )

    assert empty.status_code == long.status_code == 400
    assert store.list_conversations("employee-001")[0].title == "Original title"


def test_delete_removes_only_owned_selected_conversation(tmp_path) -> None:
    web, store = service_client(tmp_path)
    now = datetime.now(timezone.utc)
    store.append("delete-me", turn("Delete me", now), "employee-001")
    store.append("keep-me", turn("Keep me", now), "employee-001")

    deleted = web.delete("/api/conversations/delete-me?identity_id=employee-001")

    assert deleted.status_code == 200
    assert store.load("delete-me", "employee-001").turns == ()
    assert [item.session_id for item in store.list_conversations("employee-001")] == ["keep-me"]


def test_employee_visible_transcript_persists_and_is_identity_scoped(tmp_path) -> None:
    web, store = service_client(tmp_path)
    now = datetime.now(timezone.utc)
    store.append("history", turn("How many vacation days do I get?", now), "employee-001")
    store.append_message(
        "history", "user", {"text": "How many vacation days do I get?"}, "employee-001"
    )
    store.append_message(
        "history",
        "assistant",
        {"kind": "KNOWLEDGE", "text": "You receive 24 paid vacation days.", "sources": []},
        "employee-001",
    )

    own = web.get("/api/conversations/history?identity_id=employee-001")
    other = web.get("/api/conversations/history?identity_id=employee-002")

    assert own.status_code == 200
    assert own.json()["history_status"] == "complete"
    assert own.json()["messages"] == [
        {"role": "user", "text": "How many vacation days do I get?"},
        {"role": "assistant", "kind": "KNOWLEDGE", "text": "You receive 24 paid vacation days.", "sources": []},
    ]
    assert other.status_code == 404


def test_cancelled_proposal_state_update_is_identity_scoped_and_preserves_details(tmp_path) -> None:
    _, store = service_client(tmp_path)
    action = {
        "status": "PROPOSED",
        "proposal_id": "proposal-1",
        "title": "VPN problem",
        "description": "My VPN is not working.",
    }
    store.append_message(
        "owned", "assistant", {"kind": "CONFIRMATION_REQUIRED", "text": "Prepared.", "action": action}, "employee-001"
    )

    assert store.mark_proposal_cancelled("proposal-1", "employee-002") is False
    assert store.load_messages("owned", "employee-001")[0].payload["action"]["status"] == "PROPOSED"
    assert store.mark_proposal_cancelled("proposal-1", "employee-001") is True

    persisted = SqliteConversationStore(store.path).load_messages("owned", "employee-001")[0].payload["action"]
    assert persisted == {**action, "status": "CANCELLED"}


def test_executed_proposal_state_persists_identifier_and_is_identity_scoped(tmp_path) -> None:
    _, store = service_client(tmp_path)
    action = {
        "status": "PROPOSED",
        "proposal_id": "proposal-created",
        "title": "VPN problem",
        "description": "VPN disconnects repeatedly.",
    }
    store.append_message(
        "created",
        "assistant",
        {"kind": "CONFIRMATION_REQUIRED", "text": "Prepared.", "action": action},
        "employee-001",
    )

    assert store.update_proposal_state(
        "proposal-created", "EXECUTED", "employee-002", external_identifier="IT-1"
    ) is False
    assert store.update_proposal_state(
        "proposal-created", "EXECUTED", "employee-001", external_identifier="IT-1"
    ) is True

    persisted = SqliteConversationStore(store.path).load_messages(
        "created", "employee-001"
    )[0].payload["action"]
    assert persisted == {**action, "status": "EXECUTED", "ticket_id": "IT-1"}


def test_proposal_state_update_can_be_restricted_to_owned_session(tmp_path) -> None:
    _, store = service_client(tmp_path)
    action = {
        "status": "PROPOSED",
        "proposal_id": "shared-proposal-reference",
        "title": "VPN problem",
        "description": "VPN disconnects repeatedly.",
    }
    for session_id in ("correct-session", "other-session"):
        store.append_message(
            session_id,
            "assistant",
            {"kind": "CONFIRMATION_REQUIRED", "text": "Prepared.", "action": action},
            "employee-001",
        )

    assert store.update_proposal_state(
        "shared-proposal-reference",
        "EXECUTED",
        "employee-001",
        session_id="correct-session",
        external_identifier="IT-1",
    ) is True

    correct = store.load_messages("correct-session", "employee-001")[0].payload["action"]
    other = store.load_messages("other-session", "employee-001")[0].payload["action"]
    assert correct["status"] == "EXECUTED"
    assert correct["ticket_id"] == "IT-1"
    assert other["status"] == "PROPOSED"


def test_failed_proposal_state_is_terminal_without_inventing_result(tmp_path) -> None:
    _, store = service_client(tmp_path)
    action = {
        "status": "PROPOSED",
        "proposal_id": "proposal-failed",
        "title": "VPN problem",
        "description": "VPN disconnects repeatedly.",
    }
    store.append_message(
        "failed",
        "assistant",
        {"kind": "CONFIRMATION_REQUIRED", "text": "Prepared.", "action": action},
        "employee-001",
    )

    assert store.update_proposal_state(
        "proposal-failed", "FAILED", "employee-001"
    ) is True
    persisted = store.load_messages("failed", "employee-001")[0].payload["action"]
    assert persisted == {**action, "status": "FAILED"}


def test_pre_presentation_history_recovers_employee_turns_in_order(tmp_path) -> None:
    web, store = service_client(tmp_path)
    now = datetime.now(timezone.utc)
    store.append("legacy", turn("First old question", now), "employee-001")
    store.append(
        "legacy", turn("Second old question", now + timedelta(seconds=1)), "employee-001"
    )

    response = web.get("/api/conversations/legacy?identity_id=employee-001")

    assert response.status_code == 200
    assert response.json() == {
        "history_status": "partial",
        "messages": [
            {"role": "user", "text": "First old question"},
            {"role": "user", "text": "Second old question"},
        ],
    }


def test_metadata_only_history_reports_unavailable_without_cross_identity_access(
    tmp_path,
) -> None:
    web, store = service_client(tmp_path)
    now = datetime.now(timezone.utc).isoformat()
    with store._connect() as connection:
        connection.execute(
            "INSERT INTO conversation_sessions "
            "(subject_id, session_id, title, pinned, created_at, updated_at) "
            "VALUES (?, ?, ?, 0, ?, ?)",
            ("employee-001", "metadata-only", "Old conversation", now, now),
        )
        connection.commit()

    own = web.get("/api/conversations/metadata-only?identity_id=employee-001")
    other = web.get("/api/conversations/metadata-only?identity_id=employee-002")

    assert own.status_code == 200
    assert own.json() == {"history_status": "unavailable", "messages": []}
    assert other.status_code == 404


def test_partial_persisted_transcript_does_not_duplicate_legacy_turn(tmp_path) -> None:
    web, store = service_client(tmp_path)
    now = datetime.now(timezone.utc)
    store.append("partial", turn("Legacy question", now), "employee-001")
    store.append_message("partial", "user", {"text": "Legacy question"}, "employee-001")

    response = web.get("/api/conversations/partial?identity_id=employee-001")

    assert response.json() == {
        "history_status": "partial",
        "messages": [{"role": "user", "text": "Legacy question"}],
    }


def test_delete_also_removes_presentation_transcript(tmp_path) -> None:
    web, store = service_client(tmp_path)
    store.append_message("transcript", "user", {"text": "Hello"}, "employee-001")
    assert store.load_messages("transcript", "employee-001")

    deleted = web.delete("/api/conversations/transcript?identity_id=employee-001")

    assert deleted.status_code == 200
    assert store.load_messages("transcript", "employee-001") == ()


def test_in_flight_chat_survives_unrelated_sidebar_mutations_and_blocks_own_delete(
    tmp_path,
) -> None:
    store = SqliteConversationStore(tmp_path / "concurrent.db")
    now = datetime.now(timezone.utc)
    store.append("conversation-a", turn("Existing A", now), "employee-001")
    store.append("conversation-b", turn("Existing B", now), "employee-001")
    started, release = Event(), Event()
    web = TestClient(create_web_app(
        SlowConversationService(store, started, release),
        authentication_config=DEV_AUTH,
    ))
    result = {}

    def submit_chat():
        result["response"] = web.post(
            "/api/chat",
            json={
                "session_id": "conversation-a",
                "identity_id": "employee-001",
                "message": "How many vacation days do I get?",
            },
        )

    worker = Thread(target=submit_chat)
    worker.start()
    assert started.wait(timeout=5)

    renamed = web.patch(
        "/api/conversations/conversation-b",
        json={"identity_id": "employee-001", "title": "Renamed B"},
    )
    pinned = web.patch(
        "/api/conversations/conversation-b",
        json={"identity_id": "employee-001", "pinned": True},
    )
    deleted_b = web.delete(
        "/api/conversations/conversation-b?identity_id=employee-001"
    )
    blocked_a = web.delete(
        "/api/conversations/conversation-a?identity_id=employee-001"
    )

    assert renamed.status_code == pinned.status_code == deleted_b.status_code == 200
    assert blocked_a.status_code == 409
    assert worker.is_alive()

    release.set()
    worker.join(timeout=10)
    assert not worker.is_alive()
    assert result["response"].status_code == 200
    messages = web.get(
        "/api/conversations/conversation-a?identity_id=employee-001"
    ).json()["messages"]
    assert messages[-1]["text"] == "Response owned by conversation A."
    assert web.get(
        "/api/conversations/conversation-b?identity_id=employee-001"
    ).status_code == 404


def test_first_message_is_authoritative_and_navigable_before_response_completion(
    tmp_path,
) -> None:
    store = SqliteConversationStore(tmp_path / "accepted-first-message.db")
    store.append_message(
        "conversation-b", "user", {"text": "Existing conversation"}, "employee-001"
    )
    started, release = Event(), Event()
    web = TestClient(create_web_app(
        SlowConversationService(store, started, release),
        authentication_config=DEV_AUTH,
    ))

    accepted = web.post(
        "/api/conversations/conversation-a/messages",
        json={"identity_id": "employee-001", "message": "my vpn dose not work"},
    )
    assert accepted.status_code == 201
    listed_before = web.get(
        "/api/conversations?identity_id=employee-001"
    ).json()["conversations"]
    assert [item["session_id"] for item in listed_before].count("conversation-a") == 1
    assert next(
        item for item in listed_before if item["session_id"] == "conversation-a"
    )["title"] == "my vpn dose not work"
    assert web.get(
        "/api/conversations?identity_id=employee-002"
    ).json()["conversations"] == []

    result = {}

    def submit_chat():
        result["response"] = web.post(
            "/api/chat",
            json={
                "session_id": "conversation-a",
                "identity_id": "employee-001",
                "message": "my vpn dose not work",
            },
        )

    worker = Thread(target=submit_chat)
    worker.start()
    assert started.wait(timeout=5)

    # Navigating to B and back is represented by independent transcript reads.
    assert web.get(
        "/api/conversations/conversation-b?identity_id=employee-001"
    ).status_code == 200
    in_progress = web.get(
        "/api/conversations/conversation-a?identity_id=employee-001"
    ).json()
    assert in_progress["messages"] == [
        {"role": "user", "text": "my vpn dose not work"}
    ]
    assert web.delete(
        "/api/conversations/conversation-b?identity_id=employee-001"
    ).status_code == 200
    assert web.delete(
        "/api/conversations/conversation-a?identity_id=employee-001"
    ).status_code == 409

    release.set()
    worker.join(timeout=10)
    assert result["response"].status_code == 200
    completed = web.get(
        "/api/conversations/conversation-a?identity_id=employee-001"
    ).json()["messages"]
    assert [item["role"] for item in completed] == ["user", "assistant"]
    assert completed[0]["text"] == "my vpn dose not work"
    assert completed[1]["text"] == "Response owned by conversation A."
    listed_after = web.get(
        "/api/conversations?identity_id=employee-001"
    ).json()["conversations"]
    assert [item["session_id"] for item in listed_after].count("conversation-a") == 1


def test_first_message_remains_when_assistant_processing_fails(tmp_path) -> None:
    store = SqliteConversationStore(tmp_path / "failed-after-accept.db")
    web = TestClient(create_web_app(
        FailingConversationService(store),
        authentication_config=DEV_AUTH,
    ))

    failed = web.post(
        "/api/chat",
        json={
            "session_id": "failed-conversation",
            "identity_id": "employee-001",
            "message": "Tell me about vacation policy",
        },
    )

    assert failed.status_code == 400
    listed = web.get(
        "/api/conversations?identity_id=employee-001"
    ).json()["conversations"]
    assert [item["session_id"] for item in listed] == ["failed-conversation"]
    transcript = web.get(
        "/api/conversations/failed-conversation?identity_id=employee-001"
    ).json()["messages"]
    assert transcript == [
        {"role": "user", "text": "Tell me about vacation policy"}
    ]


def test_frontend_loads_history_and_preserves_management_and_scroll_contract() -> None:
    web = TestClient(create_web_app(authentication_config=DEV_AUTH))
    script = web.get("/static/app.js").text
    css = web.get("/static/app.css").text

    assert "await Promise.all([loadConversations(), loadNotifications()])" in script
    assert "data-chat-action=\"pin\"" in script
    assert "data-chat-action=\"rename\"" in script
    assert "data-chat-action=\"delete\"" in script
    assert "title=\"${label}\"" in script
    assert "if (state.active === action.id) { state.active = null; newChat(false); }" in script
    assert "loadConversation(button.dataset.chat)" in script
    assert 'const LEGACY_STORAGE_KEY = "knowvia-demo-chats-v1"' in script
    assert "cached.get(item.session_id) || legacy.get(item.session_id)" in script
    assert 'data.history_status === "complete" || !cachedHasAssistant' in script
    assert "if (selected?.saved) await loadConversation(selected.id, false)" in script
    assert "This older conversation's message history is unavailable." in script
    assert "Some messages from this older conversation are unavailable." in script
    assert "const chats = state.chats.filter(chat => chat.saved)" in script
    assert "value.chats.filter(chat => chat?.saved)" in script
    assert "state.chats = state.chats.filter(chat => chat.saved || chat.pending)" in script
    assert "const recent = state.chats.filter(chat => chat.saved && !chat.pinned)" in script
    assert "const recent = state.chats.filter(chat => !chat.saved || !chat.pinned)" not in script
    assert "session_id: sessionId, identity_id: identityId" in script
    assert 'id="conversation-modal"' in web.get("/").text
    assert 'id="conversation-modal-name"' in web.get("/").text
    assert "openConversationModal(action, chat)" in script
    assert "submitConversationModal" in script
    assert "if (!action || action.busy) return" in script
    assert 'method: "DELETE"' in script
    assert "event.target === $(\"conversation-modal\")" in script
    assert "window.confirm" not in script
    assert "window.prompt" not in script
    assert "window.alert" not in script
    assert ".chat-menu{inline-size:116px" in css
    assert ".chat-menu button{block-size:30px" in css
    assert "font-size:13px" in css
    assert ".chat-menu button.danger{color:#a14d55;border-top:1px solid #edf0f2" in css
    assert ".chat-menu button.danger:hover,.chat-menu button.danger:focus-visible{background:#fdf3f4}" in css
    assert ".conversation-modal::backdrop" in css
    assert "overflow-wrap:anywhere" in css
    assert ".sidebar{height:100%;min-height:0;overflow-y:auto;overflow-x:hidden" in css
    assert ".chat{min-height:0;overflow-y:auto;overflow-x:hidden" in css
    assert 'const actionPending = actionStatus === "PROPOSED" || actionStatus === "PENDING"' in script
    assert 'actionStatus === "EXECUTED"' in script
    assert 'message.action.status = data.proposal_update.status' in script
    assert 'message.action.ticket_id = data.proposal_update.ticket_id' in script
    assert ".action-card.terminal" in css


def test_frontend_binds_async_results_to_originating_conversation() -> None:
    web = TestClient(create_web_app(authentication_config=DEV_AUTH))
    script = web.get("/static/app.js").text

    assert "const sessionId = chat.id; const identityId = state.identity" in script
    accept_call = script.index("/messages`, { method: \"POST\"")
    chat_call = script.index('apiFetch("/api/chat"')
    assert accept_call < chat_call
    assert "chat.saved = true;" in script
    assert "await loadConversations();" in script[accept_call:chat_call]
    assert "addMessageToConversation(sessionId" in script
    assert "activeChat().messages.push" not in script
    assert "chat.saved || chat.pending" in script
    assert "chat.messages.length === 0 || chat.pending" in script
    assert "if (!chat.saved || chat.pending) return" in script
    assert "state.inFlight?.sessionId === action.id" in script
    assert "Wait for this conversation's response before deleting it." in script
    assert "const typing = chat.pending" in script
    assert "session_id: sessionId, identity_id: identityId" in script
