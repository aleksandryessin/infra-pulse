"""LDAP adapter (SEC-02) against an offline ldap3 MOCK_SYNC directory in the lldap layout.

Covers bind as the user, groups -> roles via ``memberOf`` and via a group search,
foreign groups ignored, wrong/empty password rejected and outages reported as
``DirectoryUnavailable``. A live lldap bind is part of the stand smoke, not this test.
"""

import pytest
from ldap3 import MOCK_SYNC, Connection, Server
from ldap3.core.exceptions import LDAPSocketOpenError

from infra_pulse_backend.auth.ldap import (
    DirectoryUnavailable,
    InvalidCredentials,
    LdapDirectory,
    roles_for_groups,
)

BASE = "dc=infrapulse,dc=test"
PASSWORD = "synthetic-password-1"


def person(uid: str, groups: list[str], *, member_of: bool = True) -> tuple[str, dict]:
    attributes = {
        "objectClass": ["person"],
        "uid": uid,
        "cn": f"Тест {uid}",
        "userPassword": PASSWORD,
    }
    if member_of and groups:
        attributes["memberOf"] = [f"cn={group},ou=groups,{BASE}" for group in groups]
    return f"uid={uid},ou=people,{BASE}", attributes


@pytest.fixture
def directory() -> LdapDirectory:
    server = Server("mock-lldap")
    setup = Connection(server, client_strategy=MOCK_SYNC)
    for dn, attributes in (
        person("dispatcher1", ["dispatcher"]),
        person("lead", ["dispatcher", "analyst", "lldap_password_manager"]),
        person("plain", []),
        person("legacy", ["admin"], member_of=False),
    ):
        setup.strategy.add_entry(dn, attributes)
    setup.strategy.add_entry(
        f"cn=admin,ou=groups,{BASE}",
        {
            "objectClass": ["groupOfUniqueNames"],
            "cn": "admin",
            "member": [f"uid=legacy,ou=people,{BASE}"],
        },
    )
    # Same group name outside ou=groups of this base: must not grant a role.
    setup.strategy.add_entry(
        "uid=spoof,ou=people,dc=infrapulse,dc=test",
        {
            "objectClass": ["person"],
            "uid": "spoof",
            "userPassword": PASSWORD,
            "memberOf": ["cn=admin,ou=groups,dc=other,dc=test"],
        },
    )

    def connect(user_dn: str, password: str) -> Connection:
        return Connection(server, user=user_dn, password=password, client_strategy=MOCK_SYNC)

    return LdapDirectory("ldap://lldap:3890", BASE, connection_factory=connect)


def test_bind_maps_groups_to_roles(directory):
    user = directory.authenticate("dispatcher1", PASSWORD)
    assert (user.subject_id, user.display_name) == ("dispatcher1", "Тест dispatcher1")
    assert roles_for_groups(user.groups) == ["dispatcher"]
    lead = directory.authenticate("lead", PASSWORD)
    assert roles_for_groups(lead.groups) == ["dispatcher", "analyst"]


def test_group_search_fallback_and_foreign_groups(directory):
    assert roles_for_groups(directory.authenticate("legacy", PASSWORD).groups) == ["admin"]
    assert roles_for_groups(directory.authenticate("plain", PASSWORD).groups) == []
    assert roles_for_groups(directory.authenticate("spoof", PASSWORD).groups) == []


@pytest.mark.parametrize(
    ("username", "password"),
    [("dispatcher1", "wrong"), ("nobody", PASSWORD), ("dispatcher1", "")],
)
def test_rejected_credentials(directory, username, password):
    with pytest.raises(InvalidCredentials):
        directory.authenticate(username, password)


def test_outage_is_unavailable_not_rejection():
    def refused(user_dn: str, password: str) -> Connection:
        raise LDAPSocketOpenError("connection refused")

    directory = LdapDirectory("ldap://lldap:3890", BASE, connection_factory=refused)
    with pytest.raises(DirectoryUnavailable):
        directory.authenticate("dispatcher1", PASSWORD)

    class Busy:
        result = {"result": 51}

        def bind(self) -> bool:
            return False

    busy = LdapDirectory("ldap://lldap:3890", BASE, connection_factory=lambda dn, pw: Busy())
    with pytest.raises(DirectoryUnavailable):
        busy.authenticate("dispatcher1", PASSWORD)


def test_user_dn_escaping_and_configuration():
    directory = LdapDirectory("ldaps://ldap.test:636", BASE)
    assert directory.user_dn("a,b") == f"uid=a\\,b,ou=people,{BASE}"
    with pytest.raises(ValueError):
        LdapDirectory("http://lldap:17170", BASE)
    with pytest.raises(ValueError):
        LdapDirectory("ldap://lldap:3890", " ")
    assert roles_for_groups(["ADMIN", "Dispatcher", "other"]) == ["dispatcher", "admin"]


def test_default_connection_uses_whole_second_receive_timeout():
    # ldap3 on Linux packs SO_RCVTIMEO with struct.pack("LL", timeout, 0); a float
    # (the 5.0 s default) raised struct.error at bind and the login answered 500.
    import struct

    directory = LdapDirectory("ldap://lldap:3890", "dc=example,dc=org", timeout=2.5)
    connection = directory._default_connection("uid=u,ou=people,dc=example,dc=org", "p")
    assert connection.receive_timeout == 3
    assert isinstance(connection.receive_timeout, int)
    struct.pack("LL", connection.receive_timeout, 0)
