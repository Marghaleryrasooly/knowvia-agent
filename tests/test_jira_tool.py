import base64

import pytest

from knowvia.jira import HttpResponse, JiraConfig, JiraTicketTool


class FakeHttp:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.calls = response, error, []

    def post_json(self, url, payload, headers, timeout):
        self.calls.append((url, payload, headers, timeout))
        if self.error:
            raise self.error
        return self.response


def config(token="super-secret-token"):
    return JiraConfig("https://example.atlassian.net", "agent@example.com", token, "IT", "Task")


def test_jira_success_maps_to_generic_result_and_adf() -> None:
    transport = FakeHttp(HttpResponse(201, '{"id":"1001","key":"IT-7"}'))
    result = JiraTicketTool(config(), transport).execute({"title": "VPN", "description": "VPN fails"})
    assert result.success and result.data["issue_key"] == "IT-7"
    url, payload, headers, _ = transport.calls[0]
    assert url.endswith("/rest/api/3/issue")
    assert payload["fields"]["description"]["type"] == "doc"
    assert base64.b64decode(headers["Authorization"].split()[1]).decode() == "agent@example.com:super-secret-token"
    assert "super-secret-token" not in repr(result)


@pytest.mark.parametrize(("status", "category"), [(401,"AUTHENTICATION"),(403,"PERMISSION"),(400,"VALIDATION"),(429,"RATE_LIMIT"),(503,"SERVER")])
def test_jira_failures_are_safely_categorized(status, category) -> None:
    result = JiraTicketTool(config(), FakeHttp(HttpResponse(status, "body contains super-secret-token"))).execute({"title":"x","description":"y"})
    assert not result.success and result.error_category == category
    assert "super-secret-token" not in repr(result)


def test_jira_network_failure_is_unknown_and_not_retried() -> None:
    transport = FakeHttp(error=ConnectionError("super-secret-token"))
    result = JiraTicketTool(config(), transport).execute({"title":"x","description":"y"})
    assert not result.success and result.error_category == "UNKNOWN_EXECUTION_OUTCOME"
    assert len(transport.calls) == 1
    assert "super-secret-token" not in repr(result)


def test_jira_configuration_rejects_credentials_in_url() -> None:
    with pytest.raises(ValueError, match="without credentials"):
        JiraConfig("https://user:token@example.atlassian.net", "a@b.com", "token", "IT", "Task").validate()
