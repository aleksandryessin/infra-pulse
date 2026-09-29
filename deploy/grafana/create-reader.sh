#!/usr/bin/env bash
# Create or update the PostgreSQL login of Grafana (OPS-06). Runs ON THE SERVER from
# the delivered checkout, after migration 0016 created role analytics_read:
#
#   bash deploy/grafana/create-reader.sh [--check]
#
# Reads GRAFANA_DB_USER (default grafana_reader) and GRAFANA_DB_PASSWORD from the
# server .env (<DEPLOY_PATH>/shared/.env, chmod 600). The login becomes a member of
# analytics_read with read-only transactions, statement_timeout 10 s and at most 5
# connections (deploy/grafana/create-reader.sql). Re-running rotates the password to
# the current .env value; restart grafana afterwards. The password goes through stdin
# into psql in the db container and is never printed or put on a command line.
# --check validates the .env values only and does not contact the database.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DEPLOY_ROOT="$(dirname "$APP_DIR")"
ENV_FILE="${INFRA_SERVER_ENV_FILE:-$DEPLOY_ROOT/shared/.env}"
CHECK_ONLY=0

while [ "$#" -gt 0 ]; do
  case "$1" in
    --check) CHECK_ONLY=1; shift ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

fail() { printf '[grafana-reader] ERROR: %s\n' "$*" >&2; exit 1; }
log() { printf '[grafana-reader] %s\n' "$*"; }

[ -f "$ENV_FILE" ] || fail "server env file not found: $ENV_FILE"
perm="$(stat -c '%a' "$ENV_FILE" 2>/dev/null || stat -f '%Lp' "$ENV_FILE")"
if (( 8#$perm & 8#077 )); then
  fail "$ENV_FILE must not be readable by group/others (chmod 600)"
fi

env_value() {
  # Last assignment wins, as in Compose; surrounding quotes are stripped.
  sed -n "s/^[[:space:]]*$1=//p" "$ENV_FILE" | tail -n 1 | sed -e 's/^["'\'']//' -e 's/["'\'']$//'
}

READER="$(env_value GRAFANA_DB_USER)"
READER="${READER:-grafana_reader}"
PASSWORD="$(env_value GRAFANA_DB_PASSWORD)"
DB_USER="$(env_value POSTGRES_USER)"
DB_USER="${DB_USER:-infra_pulse}"
DB_NAME="$(env_value POSTGRES_DB)"
DB_NAME="${DB_NAME:-infra_pulse}"

[[ "$READER" =~ ^[a-z_][a-z0-9_]{0,62}$ ]] || fail "GRAFANA_DB_USER must match [a-z_][a-z0-9_]{0,62}"
[ "$READER" != "$DB_USER" ] || fail "GRAFANA_DB_USER must differ from POSTGRES_USER"
[ "$READER" != analytics_read ] || fail "GRAFANA_DB_USER must not be the group analytics_read"
# URL-safe characters only: the value is embedded in SQL and in the data source.
[[ "$PASSWORD" =~ ^[A-Za-z0-9._~+/=-]{24,128}$ ]] \
  || fail "GRAFANA_DB_PASSWORD must be 24-128 URL-safe characters (openssl rand -hex 32)"
log "env OK: login $READER"
if [ "$CHECK_ONLY" -eq 1 ]; then
  exit 0
fi

command -v docker >/dev/null || fail "docker is not installed or not in PATH"
compose() {
  docker compose --project-directory "$APP_DIR" \
    -f "$APP_DIR/compose.yaml" -f "$APP_DIR/deploy/compose.server.yaml" \
    -f "$APP_DIR/deploy/compose.public.yaml" --env-file "$ENV_FILE" "$@"
}

compose exec -T db true || fail "db is not running: deploy the stand first"
{
  printf "SET infrapulse.reader = '%s';\n" "$READER"
  printf "SET infrapulse.reader_password = '%s';\n" "$PASSWORD"
  cat "$APP_DIR/deploy/grafana/create-reader.sql"
} | compose exec -T db psql -X -q -v ON_ERROR_STOP=1 -U "$DB_USER" -d "$DB_NAME"
log "done; restart grafana if the password changed: docker compose ... restart grafana"
