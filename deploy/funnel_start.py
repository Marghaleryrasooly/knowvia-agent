"""Local-only public-profile preparation; never activates Tailscale Funnel."""
import argparse
import json
import os
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
MODEL = "openai/gpt-oss-120b"


def public_environment(hostname, values):
    host = hostname.rstrip(".").lower()
    if len(host) > 253 or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+", host) or not host.endswith(".ts.net"):
        raise ValueError("An exact Tailscale HTTPS hostname is required")
    result = dict(values)
    for key in ("GROQ_GENERATION_MODEL", "GROQ_EVIDENCE_MODEL"):
        if result.get(key, MODEL) != MODEL:
            raise ValueError("Public profile requires the accepted Groq model")
        result[key] = MODEL
    result.update({
        "KNOWVIA_ENVIRONMENT": "demo",
        "KNOWVIA_GENERATION_PROVIDER": "groq",
        "KNOWVIA_EVIDENCE_EVALUATOR": "groq",
        "KNOWVIA_AGENT_ROUTER": "gemini",
        "KNOWVIA_ALLOWED_HOSTS": host + ",localhost,127.0.0.1",
        "KNOWVIA_AUTH_COOKIE_SECURE": "true",
        "KNOWVIA_ALLOW_DEVELOPMENT_IDENTITY_OVERRIDE": "false",
        "KNOWVIA_ALLOW_DEVELOPER_DETAILS": "false",
        "KNOWVIA_DEBUG": "false",
        "KNOWVIA_ENABLE_HSTS": "true",
        "KNOWVIA_REGISTRATION_ENABLED": "false",
        "KNOWVIA_SEED_DEMO_EMPLOYEE_DIRECTORY": "false",
        "KNOWVIA_AGENT_TOOL": "mock",
        "KNOWVIA_MCP_TICKET_BACKEND": "mock",
        "KNOWVIA_ENABLE_REAL_JIRA_MUTATIONS": "false",
    })
    # Keep the existing data root, embedding provider and inference settings.
    return result


def serve(port):
    from knowvia.deployment_readiness import validate_readiness
    import uvicorn
    validate_readiness()
    uvicorn.run("knowvia.web:app", host="127.0.0.1", port=port, workers=1,
                reload=False, proxy_headers=True,
                forwarded_allow_ips="127.0.0.1,::1", access_log=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hostname", required=True)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check-only", action="store_true",
                        help="Validate process configuration without starting a server or calling providers")
    parser.add_argument("--groq-accepted", action="store_true",
                        help="Operator acknowledgement of completed provider acceptance; not a quality test")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("Port must be between 1 and 65535")
    if not args.check_only and not args.groq_accepted:
        parser.error("Server start requires completed Groq acceptance and --groq-accepted")
    os.chdir(ROOT)
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")  # Existing loader precedence; credentials stay in this child process.
    try:
        os.environ.update(public_environment(args.hostname, os.environ))
        from knowvia.deployment import DeploymentConfig
        config = DeploymentConfig.from_environment()
    except ValueError:
        raise SystemExit("Public profile configuration rejected; check hostname, provider and server-side credentials") from None
    if args.check_only:
        print(json.dumps({"configuration": "valid", "generation": config.generation_provider,
                          "evidence": config.evidence_provider, "router": config.router_provider,
                          "embeddings": config.embedding_provider, "binding": "127.0.0.1",
                          "port": args.port, "secure_cookies": config.secure_cookie,
                          "funnel_activated": False, "inference_calls": 0}))
        return
    serve(args.port)


if __name__ == "__main__":
    main()
