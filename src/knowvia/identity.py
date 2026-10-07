from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Protocol


class IdentityError(ValueError):
    """A trusted identity could not be established."""


@dataclass(frozen=True)
class IdentityContext:
    subject_id: str
    display_name: str
    roles: frozenset[str]
    permissions: frozenset[str]
    authenticated: bool
    identity_source: str

    def __post_init__(self) -> None:
        for field_name in ("subject_id", "display_name", "identity_source"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise IdentityError(f"Identity {field_name} must be a non-empty string")
        if not isinstance(self.roles, frozenset) or not all(
            isinstance(role, str) and role.strip() for role in self.roles
        ):
            raise IdentityError("Identity roles must be non-empty strings")
        if not isinstance(self.permissions, frozenset) or not all(
            isinstance(permission, str) and permission.strip()
            for permission in self.permissions
        ):
            raise IdentityError("Identity permissions must be non-empty strings")
        if not isinstance(self.authenticated, bool):
            raise IdentityError("Identity authenticated must be boolean")


class IdentityProvider(Protocol):
    def resolve(self, subject_id: str) -> IdentityContext: ...


class LocalIdentityProvider:
    """Static development identities; this is not authentication or SSO."""

    def __init__(self, identities: Iterable[IdentityContext] | None = None) -> None:
        values = tuple(identities or default_local_identities())
        self._identities: dict[str, IdentityContext] = {}
        for identity in values:
            if not isinstance(identity, IdentityContext):
                raise IdentityError("Local identities must be IdentityContext objects")
            if identity.subject_id in self._identities:
                raise IdentityError("Duplicate local identity subject")
            self._identities[identity.subject_id] = identity

    def resolve(self, subject_id: str) -> IdentityContext:
        if not isinstance(subject_id, str) or not subject_id.strip():
            raise IdentityError("An identity selector is required")
        identity = self._identities.get(subject_id.strip())
        if identity is None or not identity.authenticated:
            raise IdentityError("Unknown or unauthenticated local identity")
        return identity

    def list_identities(self) -> tuple[IdentityContext, ...]:
        """Return safe development identity records for demo selectors."""
        return tuple(self._identities.values())


def default_local_identities() -> tuple[IdentityContext, ...]:
    all_knowledge = frozenset({
        "knowledge.hr.read",
        "knowledge.it.read",
        "knowledge.finance.read",
    })
    return (
        IdentityContext(
            subject_id="employee-001",
            display_name="Demo Employee",
            roles=frozenset({"employee", "it-ticket-requester"}),
            permissions=all_knowledge | {
                "action.it_ticket.create_own",
                "action.it_ticket.read_own",
            },
            authenticated=True,
            identity_source="local-development",
        ),
        IdentityContext(
            subject_id="employee-002",
            display_name="Knowledge-Only Employee",
            roles=frozenset({"employee"}),
            permissions=all_knowledge,
            authenticated=True,
            identity_source="local-development",
        ),
        IdentityContext(
            subject_id="it-employee-001",
            display_name="IT Demo Employee",
            roles=frozenset({"employee", "it-ticket-requester"}),
            permissions=frozenset({
                "knowledge.it.read",
                "action.it_ticket.create_own",
                "action.it_ticket.read_own",
            }),
            authenticated=True,
            identity_source="local-development",
        ),
        IdentityContext(
            subject_id="hr-employee-001",
            display_name="HR Demo Employee",
            roles=frozenset({"employee"}),
            permissions=frozenset({"knowledge.hr.read"}),
            authenticated=True,
            identity_source="local-development",
        ),
    )
