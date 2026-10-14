"""Offline preparation only: no Tailscale activation or inference."""
import pytest
from starlette.testclient import TestClient
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from deploy.funnel_start import public_environment, serve
from knowvia.deployment import DeploymentConfig

HOST = "computer.fictional.ts.net"


@pytest.mark.parametrize("host", ["*", "https://computer.fictional.ts.net", "example.com",
                                  "computer.ts.net/", "computer.ts.net:443", "-bad.ts.net", "a..ts.net"])
def test_invalid_or_wildcard_public_hosts_are_rejected(host):
    with pytest.raises(ValueError):
        public_environment(host, {})


def test_process_profile_is_explicit_safe_and_preserves_runtime_settings():
    values = {"GROQ_API_KEY": "fictional", "GEMINI_API_KEY": "fictional",
              "KNOWVIA_GENERATION_PROVIDER": "ollama", "KNOWVIA_DATA_DIR": "private-data",
              "GEMINI_ROUTER_MODEL": "unchanged-router-model", "GROQ_TIMEOUT_SECONDS": "120",
              "GROQ_EVIDENCE_REASONING_EFFORT": "medium", "GROQ_GENERATION_MAX_COMPLETION_TOKENS": "1024"}
    selected = public_environment(HOST + ".", values)
    config = DeploymentConfig.from_environment(selected)
    assert config.generation_provider == config.evidence_provider == "groq"
    assert config.router_provider == "gemini" and config.action_backend == "mock"
    assert config.secure_cookie and config.hsts_enabled and not config.registration_enabled
    assert not config.development_identity_override and not config.real_jira_mutations_enabled
    assert not config.debug_enabled and not config.developer_details_enabled
    for key in ("KNOWVIA_DATA_DIR", "GEMINI_ROUTER_MODEL", "GROQ_TIMEOUT_SECONDS",
                "GROQ_EVIDENCE_REASONING_EFFORT", "GROQ_GENERATION_MAX_COMPLETION_TOKENS"):
        assert selected[key] == values[key]
    assert values["KNOWVIA_GENERATION_PROVIDER"] == "ollama"
    assert DeploymentConfig.from_environment({"GROQ_API_KEY": "fictional"}).generation_provider == "groq"


def test_unaccepted_model_configuration_does_not_silently_switch():
    with pytest.raises(ValueError):
        public_environment(HOST, {"GROQ_EVIDENCE_MODEL": "different-model"})


def test_server_uses_loopback_and_one_worker_without_activating_funnel(monkeypatch):
    import knowvia.deployment_readiness as readiness
    import uvicorn
    events = []
    monkeypatch.setattr(readiness, "validate_readiness", lambda: events.append("readiness"))
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kw: events.append(kw))
    serve(8000)
    assert events[0] == "readiness"
    assert events[1]["host"] == "127.0.0.1" and events[1]["workers"] == 1
    assert events[1]["forwarded_allow_ips"] == "127.0.0.1,::1"
    assert not events[1]["reload"] and not events[1]["access_log"]


@pytest.mark.parametrize("client_ip, expected", [("127.0.0.1", "https"), ("192.0.2.1", "http")])
def test_https_forwarding_is_trusted_only_from_local_proxy(client_ip, expected):
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route
    async def scheme(request):
        return JSONResponse({"scheme": request.url.scheme})
    app = ProxyHeadersMiddleware(Starlette(routes=[Route("/", scheme)]), trusted_hosts="127.0.0.1,::1")
    with TestClient(app, client=(client_ip, 12345)) as client:
        assert client.get("/", headers={"x-forwarded-proto": "https"}).json()["scheme"] == expected


def test_public_https_auth_admin_origin_and_host_rules(tmp_path):
    from knowvia.admin import SqliteAuthority
    from knowvia.employee_directory import EmployeeRecord
    from knowvia.runtime_paths import RuntimePaths
    from knowvia.web import create_web_app
    authority = SqliteAuthority(RuntimePaths(tmp_path))
    password = "Offline-funnel-password!"
    authority.bootstrap(EmployeeRecord("admin", "Demo", "Admin", "admin@novatech.example",
                                       "Administration", "Administrator"), password, password)
    values = public_environment(HOST, {"GROQ_API_KEY": "fictional", "GEMINI_API_KEY": "fictional"})
    app = create_web_app(runtime_paths=authority.paths, admin_authority=authority,
                         deployment_config=DeploymentConfig.from_environment(values))
    with TestClient(app, base_url="https://" + HOST) as client:
        for path in ("/", "/admin", "/health", "/static/app.js"):
            assert client.get(path).status_code == 200
        assert client.get("/api/admin/accounts").status_code == 401
        login = client.post("/api/auth/login", json={"work_email": "admin@novatech.example", "password": password})
        assert login.status_code == 200
        cookie = login.headers["set-cookie"].lower()
        assert "secure" in cookie and "httponly" in cookie and "samesite=lax" in cookie
        assert client.get("/api/admin/accounts").status_code == 200
        for headers in ({}, {"origin": "https://foreign.example"}):
            assert client.post("/api/admin/accounts/admin/roles", json={"roles": ["administrator"]}, headers=headers).status_code == 403
        assert client.post("/api/admin/accounts/admin/roles", json={"roles": ["administrator"]},
                           headers={"origin": "https://" + HOST}).status_code == 200
        assert client.get("/", headers={"host": "foreign.example"}).status_code == 400
        assert client.post("/api/auth/logout").status_code == 200
        assert client.get("/api/admin/accounts").status_code == 401
