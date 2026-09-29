"""Session identity and roles (SEC-01–SEC-03).

Accounts live in an external LDAP directory (a test lldap on the stand); the service
keeps the external subject ID and display name only. Roles come from directory groups
and add up. The author of every write is taken from the session, never from a body.
Territorial scopes (district / ODS / complex) are planned, not part of this contract.
"""

from typing import Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, SecretStr, model_validator

# integration (C0.2): a service token of an external system; it may only send data.
Role = Literal["dispatcher", "analyst", "admin", "integration"]
ROLE_LABELS: dict[str, str] = {
    "dispatcher": "диспетчер",
    "analyst": "аналитик",
    "admin": "администратор",
    "integration": "интеграция",
}
# Permission -> roles allowed. Checked on the server for every route.
PERMISSIONS: dict[str, frozenset[str]] = {
    "read": frozenset({"dispatcher", "analyst", "admin"}),
    "decide": frozenset({"dispatcher", "admin"}),
    "import": frozenset({"admin"}),
    # «Исследование» and quality ratios (P, R): analysts and administrators only.
    "research": frozenset({"analyst", "admin"}),
    # Streaming data through POST /api/v1/observations (C0.2).
    "ingest": frozenset({"integration", "admin"}),
    # Management reports (C0.2).
    "report": frozenset({"analyst", "admin"}),
}


class Me(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject_id: str = Field(min_length=1, max_length=256)
    display_name: str = Field(min_length=1, max_length=256)
    roles: list[Role] = Field(min_length=1)
    # dev_stub: no authentication (local and tunnel-only stand); ldap: directory bind.
    # token: integration service token (Authorization: Bearer), C0.2.
    auth_source: Literal["ldap", "dev_stub", "fixture", "token"]
    session_expires_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def check_roles(self) -> Self:
        if len(set(self.roles)) != len(self.roles):
            raise ValueError("duplicate role")
        if (self.auth_source == "ldap") != (self.session_expires_at is not None):
            raise ValueError("only a directory session has an expiry")
        return self

    def can(self, permission: str) -> bool:
        return bool(PERMISSIONS[permission] & set(self.roles))


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$")
    password: SecretStr = Field(min_length=1, max_length=256)
