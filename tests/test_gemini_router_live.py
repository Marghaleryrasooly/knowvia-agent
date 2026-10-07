import os

import pytest
from dotenv import load_dotenv

from knowvia.agent_models import AgentDecisionKind
from knowvia.agent_tools import LocalITTicketTool
from knowvia.gemini_routing import GeminiAgentRouter, GoogleGenAIRoutingTransport

load_dotenv()


@pytest.mark.gemini_live
@pytest.mark.skipif(
    not os.getenv("GEMINI_API_KEY") or os.getenv("RUN_GEMINI_LIVE") != "1",
    reason="Set GEMINI_API_KEY and RUN_GEMINI_LIVE=1 for fictional routing-only calls",
)
def test_live_gemini_routes_fictional_requests_without_execution() -> None:
    router = GeminiAgentRouter(
        [LocalITTicketTool.specification], GoogleGenAIRoutingTransport()
    )
    knowledge = router.route("How many vacation days does fictional NovaTech provide?")
    action = router.route("Create an IT ticket because my fictional NovaTech VPN is broken.")
    assert knowledge.decision is AgentDecisionKind.KNOWLEDGE
    assert action.decision is AgentDecisionKind.ACTION
    assert action.tool_name == "create_it_ticket"
