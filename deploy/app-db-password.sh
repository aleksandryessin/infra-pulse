#!/usr/bin/env bash
# Password of the runtime PostgreSQL role infra_pulse_app in the server .env
# (deploy/README.md «Роли PostgreSQL»). Runs ON THE SERVER from the delivered checkout;
# deploy/remote-deploy.sh and deploy/restore.sh call it before `docker compose config`.
#
#   bash deploy/app-db-password.sh [--check]
#
# INFRA_DB_APP_PASSWORD missing: a line with `openssl rand -hex 32` is appended.
# Empty or __SET_ON_SERVER__: the file is rewritten through a temporary file in the
# same directory (chmod 600) with the new value instead. An existing value is kept;
# only its format is checked (24-128 characters [A-Za-z0-9._~-]: it is part of the
# api and worker DSN). The value is never printed and never passed as an argument of
# another process. --check only validates (exit 1 when a value would be generated).
# Environment override, as in remote-deploy.sh: INFRA_SERVER_ENV_FILE.
set -euo pipefail
umask 077

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPLOY_ROOT="$(dirname "$APP_DIR")"
ENV_FILE="${INFRA_SERVER_ENV_FILE:-$DEPLOY_ROOT/shared/.env}"
KEY=INFRA_DB_APP_PASSWORD
CHECK_ONLY=0

while [ "$#" -gt 0 ]; do
  case "$1" in
    --check) CHECK_ONLY=1; shift ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

log() { printf '[app-db-password] %s\n' "$*"; }
fail() { printf '[app-db-password] ERROR: %s\n' "$*" >&2; exit 1; }

[ -f "$ENV_FILE" ] || fail "server env file not found: $ENV_FILE"
[ ! -L "$ENV_FILE" ] || fail "$ENV_FILE is a symbolic link: add $KEY by hand (openssl rand -hex 32)"
perm="$(stat -c '%a' "$ENV_FILE" 2>/dev/null || stat -f '%Lp' "$ENV_FILE")"
if (( 8#$perm & 8#077 )); then
  fail "$ENV_FILE must not be readable by group/others (chmod 600)"
fi

# Last assignment wins, as in Compose; surrounding quotes are stripped.
present=0
grep -Eq "^[[:space:]]*$KEY=" "$ENV_FILE" && present=1
current="$(sed -n "s/^[[:space:]]*$KEY=//p" "$ENV_FILE" | tail -n 1 | sed -e 's/^["'\'']//' -e 's/["'\'']$//')"

case "$current" in
  ""|__SET_ON_SERVER__) ;;
  *)
    [[ "$current" =~ ^[A-Za-z0-9._~-]{24,128}$ ]] \
      || fail "$KEY in $ENV_FILE must be 24-128 characters [A-Za-z0-9._~-] (openssl rand -hex 32)"
    log "$KEY is set in $ENV_FILE"
    exit 0 ;;
esac

[ "$CHECK_ONLY" -eq 0 ] || fail "$KEY is not set in $ENV_FILE (remote-deploy.sh generates it)"
command -v openssl >/dev/null || fail "openssl is needed to generate $KEY"
secret="$(openssl rand -hex 32)"
[[ "$secret" =~ ^[0-9a-f]{64}$ ]] || fail "openssl rand did not return 64 hex characters"
comment="# Runtime PostgreSQL role infra_pulse_app (api, worker): generated $(date -u +%Y-%m-%dT%H:%M:%SZ) by deploy/app-db-password.sh"

if [ "$present" -eq 0 ]; then
  # A file without a final newline would glue the new line to its last assignment.
  separator=""
  [ -z "$(tail -c 1 "$ENV_FILE")" ] || separator=$'\n'
  printf '%s%s\n%s=%s\n' "$separator" "$comment" "$KEY" "$secret" >> "$ENV_FILE"
else
  tmp="$(mktemp "$ENV_FILE.XXXXXX")"
  trap 'rm -f -- "$tmp"' EXIT
  chmod 600 "$tmp"
  {
    grep -Ev "^[[:space:]]*$KEY=" "$ENV_FILE" || true
    printf '%s\n%s=%s\n' "$comment" "$KEY" "$secret"
  } > "$tmp"
  mv -f -- "$tmp" "$ENV_FILE"
  trap - EXIT
fi
log "generated $KEY in $ENV_FILE (value not printed)"
