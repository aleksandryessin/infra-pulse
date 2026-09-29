"""Directory login, server-side sessions and login throttling (B3, SEC-02).

``ldap`` binds as the user and maps directory groups to roles; ``sessions`` keeps
server-side sessions in PostgreSQL; ``throttle`` limits failed logins. Request-level
checks live in ``infra_pulse_backend.api.auth_deps``.
"""
