import pytest

from knowvia.agent_tools import LocalITTicketTool, ToolValidationError


def test_local_ticket_tool_returns_generic_structured_result() -> None:
    tool = LocalITTicketTool()
    result = tool.execute({"title": "VPN failure", "description": "VPN is broken"})
    assert result.success is True
    assert result.tool_name == "create_it_ticket"
    assert result.data == {
        "ticket_id": "IT-0001",
        "status": "OPEN",
        "title": "VPN failure",
        "description": "VPN is broken",
    }


@pytest.mark.parametrize(
    "arguments",
    [
        {"title": "Missing description"},
        {"title": "", "description": "problem"},
        {"title": "x", "description": "y", "priority": "high"},
        {"title": 42, "description": "problem"},
    ],
)
def test_local_ticket_tool_rejects_invalid_arguments(arguments) -> None:
    with pytest.raises(ToolValidationError):
        LocalITTicketTool().validate_arguments(arguments)
