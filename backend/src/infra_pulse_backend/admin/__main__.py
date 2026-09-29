"""Integration tokens for the observation API::

    python -m infra_pulse_backend.admin token create --name scada-ods-1
    python -m infra_pulse_backend.admin token list [--all]
    python -m infra_pulse_backend.admin token revoke tok-0123456789ab
    python -m infra_pulse_backend.admin token revoke --name scada-ods-1

``create`` prints the token once; only its SHA-256 is stored. ``revoke`` applies to
the next request. The actor recorded in ``audit_events`` is ``cli:<OS user>`` unless
``--actor`` names the administrator. Exit codes: 0 done, 1 storage error,
2 usage error or nothing to revoke.
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from uuid import uuid4

import psycopg

from infra_pulse_backend.config import Settings
from infra_pulse_backend.storage.integration_pg import (
    TokenInfo,
    create_token,
    list_tokens,
    revoke_tokens,
)


def _default_actor() -> str:
    try:
        user = getpass.getuser()
    except (KeyError, OSError):
        user = "unknown"
    return f"cli:{user}"[:256]


def _row(token: TokenInfo) -> dict:
    return {
        "token_id": token.token_id,
        "name": token.name,
        "created_by": token.created_by,
        "created_at": token.created_at.isoformat(),
        "last_used_at": token.last_used_at.isoformat() if token.last_used_at else None,
        "revoked_at": token.revoked_at.isoformat() if token.revoked_at else None,
        "revoked_by": token.revoked_by,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m infra_pulse_backend.admin")
    parser.add_argument("--actor", default=None, help="administrator for the audit record")
    areas = parser.add_subparsers(dest="area", required=True)
    token = areas.add_parser("token", help="integration tokens of the observation API")
    actions = token.add_subparsers(dest="action", required=True)
    create = actions.add_parser("create", help="issue a token (printed once)")
    create.add_argument("--name", required=True, help="integration name, e.g. scada-ods-1")
    create.add_argument("--json", action="store_true", help="print JSON")
    listing = actions.add_parser("list", help="list tokens without secrets")
    listing.add_argument("--all", action="store_true", help="include revoked tokens")
    listing.add_argument("--json", action="store_true", help="print JSON")
    revoke = actions.add_parser("revoke", help="revoke a token now")
    target = revoke.add_mutually_exclusive_group(required=True)
    target.add_argument("token_id", nargs="?", help="token ID, e.g. tok-0123456789ab")
    target.add_argument("--name", help="revoke every live token of this integration")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    # The DSN comes from the environment only: a command-line secret shows in `ps`.
    configured = Settings().db_dsn
    if configured is None:
        print("INFRA_DB_DSN is required", file=sys.stderr)
        return 2
    dsn = configured.get_secret_value()
    actor = (args.actor or _default_actor())[:256]
    request = f"cli-{uuid4().hex}"
    try:
        if args.action == "create":
            try:
                token, info = create_token(dsn, name=args.name, actor=actor, request_id=request)
            except ValueError as error:
                print(str(error), file=sys.stderr)
                return 2
            if args.json:
                print(json.dumps({**_row(info), "token": token}, ensure_ascii=False))
            else:
                print(f"token_id: {info.token_id}")
                print(f"name:     {info.name}")
                print(f"token:    {token}")
                print("The token is shown once and is not stored; pass it to the integration.")
            return 0
        if args.action == "list":
            tokens = list_tokens(dsn, actor=actor, request_id=request, include_revoked=args.all)
            if args.json:
                print(json.dumps([_row(item) for item in tokens], ensure_ascii=False))
            else:
                print("token_id\tname\tcreated_at\tlast_used_at\trevoked_at")
                for item in tokens:
                    row = _row(item)
                    print(
                        "\t".join(
                            str(row[key] or "-")
                            for key in (
                                "token_id",
                                "name",
                                "created_at",
                                "last_used_at",
                                "revoked_at",
                            )
                        )
                    )
            return 0
        revoked = revoke_tokens(
            dsn, actor=actor, request_id=request, token_id=args.token_id, name=args.name
        )
        if not revoked:
            print("no live token matched; nothing revoked", file=sys.stderr)
            return 2
        for item in revoked:
            print(f"revoked {item.token_id} ({item.name}) at {item.revoked_at.isoformat()}")
        return 0
    except psycopg.Error as error:
        print(f"storage error: {type(error).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
