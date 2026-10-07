from pathlib import Path

from starlette.testclient import TestClient

from knowvia.web import WebAuthenticationConfig, create_web_app


STATIC = Path(__file__).parents[1] / "src" / "knowvia" / "static"


class StubService:
    def identities(self):
        return [{"id": "employee-001", "name": "Demo Employee"}]


def test_authentication_screen_and_safe_account_fields_are_present() -> None:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert 'id="auth-screen"' in html
    assert 'id="signin-form"' in html and 'id="register-form"' in html
    for field in ("register-employee-id", "register-email", "register-password", "register-confirmation"):
        assert f'id="{field}"' in html
    registration = html[html.index('id="register-form"'):html.index('id="auth-demo-guidance"')]
    assert "department" not in registration.lower()
    assert "permissions" not in registration.lower()
    assert "admin" not in registration.lower()


def test_frontend_restores_cookie_session_and_does_not_store_auth_tokens() -> None:
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert 'fetch("/api/auth/me")' in js
    assert 'fetch("/api/auth/login"' in js
    assert 'fetch("/api/auth/logout"' in js
    assert "data.employee" in js
    assert "authToken" not in js and "sessionToken" not in js
    assert "localStorage.setItem(storageKey(state.identity)" in js
    assert "localStorage.removeItem(storageKey(state.identity))" in js


def test_registration_payload_contains_only_allowed_fields() -> None:
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    expected = 'const request = { employee_id: $("register-employee-id").value.trim(), work_email: $("register-email").value.trim(), password, password_confirmation: confirmation };'
    assert expected in js
    assert 'if (password !== confirmation)' in js


def test_duplicate_form_submission_is_guarded() -> None:
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert js.count("if (state.authBusy) return") >= 3
    assert '$("signin-submit").disabled = true' in js
    assert '$("register-submit").disabled = true' in js


def test_profile_exposes_safe_employee_metadata_only() -> None:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    profile = html[html.index('id="profile-menu"'):html.index('</header>')]
    for label in ("Employee ID", "Department", "Role", "Sign out"):
        assert label in profile
    for forbidden in ("Permissions", "Session token", "Password"):
        assert forbidden not in profile


def test_production_config_hides_development_identity_selector() -> None:
    client = TestClient(create_web_app(StubService(), authentication_config=WebAuthenticationConfig()))
    config = client.get("/api/config").json()
    assert config["development_identity_override"] is False
    assert config["identities"] == []
    assert config["default_identity"] is None


def test_development_identity_selector_requires_explicit_mode() -> None:
    client = TestClient(create_web_app(StubService(), authentication_config=WebAuthenticationConfig.local_development()))
    config = client.get("/api/config").json()
    assert config["development_identity_override"] is True
    assert config["identities"] == [{"id": "employee-001", "name": "Demo Employee"}]
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "if (config.development_identity_override)" in js
    assert '$("developer-menu").hidden = true' in js
    assert '$("profile-menu").hidden = false' in js


def test_logout_and_identity_change_clear_employee_scoped_ui_state() -> None:
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "function clearEmployeeState(removeCache = false)" in js
    assert "state.chats = []; state.active = null; state.employee = null; state.identity = null" in js
    assert "if (state.identity && state.identity !== employee.employee_id) clearEmployeeState(true)" in js
    assert 'showAuthentication();' in js[js.index("async function signOut"):]


def test_authentication_ui_is_responsive_and_accessible() -> None:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    assert 'autocomplete="current-password"' in html
    assert html.count('autocomplete="new-password"') == 2
    assert 'role="status"' in html and 'aria-live="polite"' in html
    assert ".auth-screen" in css and "100dvh" in css
    assert "@media(max-width:520px){.auth-screen" in css


def test_final_ui_grid_sidebar_preference_and_header_controls_are_accessible() -> None:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    js = (STATIC / "app.js").read_text(encoding="utf-8")

    assert "--sidebar-width:300px" in css
    assert "--conversation-width:960px" in css
    assert "--message-column-width:680px" in css
    assert "grid-template-columns:var(--sidebar-width) 1px minmax(0,1fr)" in css
    assert ".messages{max-width:min(var(--message-column-width),var(--conversation-width))}" in css
    assert ".composer{max-width:min(var(--message-column-width),var(--conversation-width))}" in css
    assert ".workspace{grid-template-rows:58px minmax(0,1fr) auto}" in css
    assert ".workspace:not(.welcome-state) .composer-wrap{margin-top:" not in css
    assert ".messages {\n  gap: 0;\n}" in css
    assert ".message.user + .message.assistant {\n  margin-top: 10px;\n}" in css
    assert ".message.assistant + .message.user {\n  margin-top: 28px;\n}" in css
    assert "background: #dce6eb;" in css
    assert "border-color: #c8d6de;" in css
    assert 'id="sidebar-resizer"' in html
    assert 'role="separator"' in html and 'aria-orientation="vertical"' in html
    assert "SIDEBAR_MIN_WIDTH = 240" in js
    assert "SIDEBAR_DEFAULT_WIDTH = 300" in js
    assert "SIDEBAR_MAX_WIDTH = 480" in js
    assert 'localStorage.setItem(SIDEBAR_WIDTH_KEY' in js
    assert '["ArrowLeft", "ArrowRight", "Home", "End"]' in js
    assert "@media(max-width:780px){.shell{grid-template-columns:1fr}.sidebar-resizer{display:none}" in css
    assert "NovaTech Workspace" in html
    assert 'id="notification-button" aria-label="Notifications" aria-controls="notification-panel"' in html
    assert '<svg viewBox="0 0 24 24" aria-hidden="true">' in html
    assert "Local demo workspace" not in html
