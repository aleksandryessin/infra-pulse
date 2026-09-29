"""LDAP directory adapter (SEC-02): simple bind as the user, groups -> roles.

The stand uses a test lldap (deploy/compose.public.yaml). Layout, as in lldap:
users ``uid=<name>,ou=people,<base_dn>``, groups ``cn=<group>,ou=groups,<base_dn>``
with ``member`` and the user's ``memberOf``. Group names equal role names
(``dispatcher``, ``analyst``, ``admin``); other groups give no role. Accounts stay
in the directory; the service keeps the subject ID and display name only.

A failed bind is ``InvalidCredentials`` (401). Any transport or server error is
``DirectoryUnavailable`` (503): the service never logs anyone in without a bind.
"""

import math
import ssl
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Protocol, get_args

from ldap3 import BASE, NONE, SIMPLE, SUBTREE, Connection, Server, Tls
from ldap3.core.exceptions import LDAPException
from ldap3.utils.conv import escape_filter_chars
from ldap3.utils.dn import escape_rdn, parse_dn

from infra_pulse_core.contracts.auth import Role

# Directory groups give human roles only. The ``integration`` role (B1x) belongs to
# bearer tokens: a directory group of that name must not grant ``ingest`` to a session.
DIRECTORY_ROLES = frozenset({"dispatcher", "analyst", "admin"})
ROLE_ORDER: tuple[Role, ...] = tuple(role for role in get_args(Role) if role in DIRECTORY_ROLES)
# Bind result codes that mean "these credentials are not accepted", not an outage:
# noSuchObject, inappropriateAuthentication, invalidCredentials,
# insufficientAccessRights, unwillingToPerform.
_REJECTED_BIND = frozenset({32, 48, 49, 50, 53})
_USER_ATTRIBUTES = ["uid", "cn", "displayName", "memberOf"]


class DirectoryUnavailable(RuntimeError):
    """The directory cannot be reached or answered with an error."""


class InvalidCredentials(Exception):
    """Unknown user or wrong password (never distinguished for the caller)."""


@dataclass(frozen=True)
class DirectoryUser:
    subject_id: str
    display_name: str
    groups: frozenset[str]


class Directory(Protocol):
    def authenticate(self, username: str, password: str) -> DirectoryUser: ...


def roles_for_groups(groups: Iterable[str]) -> list[Role]:
    """Directory groups -> roles in a fixed order; unknown groups are ignored."""
    names = {group.lower() for group in groups}
    return [role for role in ROLE_ORDER if role in names]


ConnectionFactory = Callable[[str, str], Connection]


class LdapDirectory:
    def __init__(
        self,
        url: str,
        base_dn: str,
        *,
        timeout: float = 5.0,
        connection_factory: ConnectionFactory | None = None,
    ) -> None:
        if not url.startswith(("ldap://", "ldaps://")):
            raise ValueError("LDAP URL must start with ldap:// or ldaps://")
        if not base_dn.strip():
            raise ValueError("LDAP base DN is required")
        self.url = url
        self.base_dn = base_dn.strip()
        self.people_dn = f"ou=people,{self.base_dn}"
        self.groups_dn = f"ou=groups,{self.base_dn}"
        self.timeout = timeout
        self._connect = connection_factory or self._default_connection

    def _default_connection(self, user_dn: str, password: str) -> Connection:
        tls = None
        if self.url.startswith("ldaps://"):
            tls = Tls(validate=ssl.CERT_REQUIRED, version=ssl.PROTOCOL_TLS_CLIENT)
        server = Server(self.url, connect_timeout=self.timeout, get_info=NONE, tls=tls)
        return Connection(
            server,
            user=user_dn,
            password=password,
            authentication=SIMPLE,
            # ldap3 packs SO_RCVTIMEO with struct.pack("LL", ...) on Linux: a float
            # raises struct.error at bind, so the receive timeout is whole seconds.
            receive_timeout=max(1, math.ceil(self.timeout)),
            read_only=True,
            raise_exceptions=False,
            auto_referrals=False,
        )

    def user_dn(self, username: str) -> str:
        return f"uid={escape_rdn(username)},{self.people_dn}"

    def authenticate(self, username: str, password: str) -> DirectoryUser:
        # An empty password would be an anonymous "unauthenticated bind" (RFC 4513 5.1.2).
        if not username or not password:
            raise InvalidCredentials
        dn = self.user_dn(username)
        try:
            connection = self._connect(dn, password)
            if not connection.bind():
                code = (connection.result or {}).get("result")
                if code in _REJECTED_BIND:
                    raise InvalidCredentials
                raise DirectoryUnavailable(f"bind failed with LDAP result {code}")
            try:
                return self._read_user(connection, dn, username)
            finally:
                connection.unbind()
        except LDAPException as error:
            raise DirectoryUnavailable(type(error).__name__) from error

    def _read_user(self, connection: Connection, dn: str, username: str) -> DirectoryUser:
        if not connection.search(dn, "(objectClass=*)", BASE, attributes=_USER_ATTRIBUTES):
            # Some directories hide the base entry; fall back to a subtree search.
            connection.search(
                self.people_dn,
                f"(uid={escape_filter_chars(username)})",
                SUBTREE,
                attributes=_USER_ATTRIBUTES,
                size_limit=2,
            )
        entries = list(connection.entries)
        # Bound but unreadable entry: identity is the login name, roles come only
        # from the group search below (none -> the login is refused with 403).
        attributes = entries[0].entry_attributes_as_dict if len(entries) == 1 else {}
        subject = _first(attributes.get("uid")) or username
        display = _first(attributes.get("displayName")) or _first(attributes.get("cn")) or subject
        groups = {name for value in attributes.get("memberOf", []) if (name := self._group(value))}
        if not groups:
            member = escape_filter_chars(dn)
            connection.search(
                self.groups_dn,
                f"(|(member={member})(uniqueMember={member}))",
                SUBTREE,
                attributes=["cn"],
            )
            groups = {name for entry in connection.entries if (name := self._group(entry.entry_dn))}
        return DirectoryUser(
            subject_id=str(subject)[:256],
            display_name=str(display)[:256],
            groups=frozenset(str(group) for group in groups),
        )

    def _group(self, group_dn: object) -> str | None:
        """CN of a group DN directly under ``ou=groups,<base_dn>``; else None."""
        try:
            parts = parse_dn(str(group_dn))
        except LDAPException:
            return None
        if len(parts) < 2 or parts[0][0].lower() != "cn":
            return None
        parent = ",".join(f"{name}={value}" for name, value, _ in parts[1:])
        if parent.replace(" ", "").lower() != self.groups_dn.replace(" ", "").lower():
            return None
        return parts[0][1]


def _first(values: object) -> str | None:
    if isinstance(values, list | tuple):
        return str(values[0]) if values else None
    return str(values) if values else None
