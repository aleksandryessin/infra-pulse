#!/usr/bin/env bash
# Restore the stand from one backup of deploy/backup.sh (OPS-02: recovery <= 4 h).
# Runs ON THE SERVER from the delivered checkout, like remote-deploy.sh; runbook:
# deploy/README.md «Резервные копии и восстановление».
#
#   bash deploy/restore.sh [--replace] [--public] [--skip-build] [--timeout SEC]
#                          [--revision SHA] <backup directory>
#
# Stages, each timed: verify SHA256SUMS -> build images -> start db -> check the target
# -> stop api, worker, web, backup -> [--replace: safety backup] -> pg_restore -> compare
# rows and checksums with tables.tsv -> unpack uploads -> migrations -> start the stack
# -> smoke (/health/live; /health/ready in replay/received; audit rows).
#
# The target must be clean: a new server, or a new Compose project (COMPOSE_PROJECT_NAME)
# whose volumes do not exist yet. A database with tables but without rows counts as
# clean and is recreated. A database with rows or a non-empty uploads volume is refused
# before anything is stopped, unless --replace is given: then a safety backup labelled
# "pre-restore" is taken first, the database is recreated and the uploads are unpacked
# over the existing files. Nothing is deleted from the backup directory.
#
# --revision labels the containers as remote-deploy.sh does (default: APP_REVISION, the
# Git checkout, the running api container, else "unknown"). Environment overrides, as in
# remote-deploy.sh: INFRA_SERVER_ENV_FILE (server .env) and COMPOSE_PROJECT_NAME.
#
# PostgreSQL roles are global to the cluster and not part of the dump; pg_restore skips
# owners and grants. The migrate stage (as the owner) creates the runtime role
# infra_pulse_app if it is missing, grants it again, sets LOGIN and the password of
# INFRA_DB_APP_PASSWORD (generated into the .env when missing, deploy/app-db-password.sh);
# the smoke checks that api and worker connect as that role.
# Exit code is non-zero if any stage or check fails; the elapsed time is printed in both cases.
# $POSTGRES_USER / $POSTGRES_DB in single quotes expand inside the db container.
# shellcheck disable=SC2016
set -euo pipefail

REPLACE=0
PUBLIC=0
SKIP_BUILD=0
TIMEOUT=300
REVISION="${APP_REVISION:-}"
SRC=""

while [ "$#" -gt 0 ]; do
  case "$1" in
    --replace) REPLACE=1; shift ;;
    --public) PUBLIC=1; shift ;;
    --skip-build) SKIP_BUILD=1; shift ;;
    --timeout) TIMEOUT="$2"; shift 2 ;;
    --revision) REVISION="$2"; shift 2 ;;
    -h|--help) sed -n '2,31p' "$0"; exit 0 ;;
    -*) echo "unknown argument: $1" >&2; exit 2 ;;
    *) [ -z "$SRC" ] || { echo "only one backup directory" >&2; exit 2; }; SRC="$1"; shift ;;
  esac
done
[ -n "$SRC" ] || { echo "usage: restore.sh [--replace] [--public] [--skip-build] [--timeout SEC] <backup directory>" >&2; exit 2; }
case "$TIMEOUT" in *[!0-9]*|"") echo "invalid --timeout" >&2; exit 2 ;; esac
case "$REVISION" in *[!A-Za-z0-9._-]*) echo "invalid --revision" >&2; exit 2 ;; esac

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPLOY_ROOT="$(dirname "$APP_DIR")"
ENV_FILE="${INFRA_SERVER_ENV_FILE:-$DEPLOY_ROOT/shared/.env}"
HISTORY_FILE="$(dirname "$ENV_FILE")/restore-history.log"
PAYLOAD_FILES="db.dump uploads.tar.gz config.tar.gz tables.tsv manifest.json"
TAB="$(printf '\t')"

log() { printf '[restore %s] %s\n' "$(date -u +%H:%M:%S)" "$*"; }
fail() { printf '[restore] ERROR: %s\n' "$*" >&2; exit 1; }

if command -v sha256sum >/dev/null 2>&1; then
  SHA256=(sha256sum)
else
  SHA256=(shasum -a 256)
fi
sha_stdin() { "${SHA256[@]}" | awk '{print $1}'; }

STARTED="$(date +%s)"
STAGE=""
STAGE_START=0
STAGES=""
BACKUP_ID="?"
CHANGED=0
begin() { STAGE="$1"; STAGE_START="$(date +%s)"; log "== $1"; }
done_stage() {
  STAGES+="$(printf '  %-15s %6s s' "$STAGE" "$(( $(date +%s) - STAGE_START ))")"$'\n'
  STAGE=""
}

summary() {
  local status=$?
  trap - EXIT
  [ -z "$STAGE" ] || STAGES+="$(printf '  %-15s %6s s  <- failed here' "$STAGE" "$(( $(date +%s) - STAGE_START ))")"$'\n'
  printf '[restore] stages:\n%s' "$STAGES"
  printf '[restore] total %s s (target: 14400 s = 4 h), backup %s, exit %s\n' \
    "$(( $(date +%s) - STARTED ))" "$BACKUP_ID" "$status"
  printf '%s result=%s backup=%s seconds=%s replace=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    "$([ "$status" -eq 0 ] && echo ok || echo failed)" "$BACKUP_ID" "$(( $(date +%s) - STARTED ))" \
    "$REPLACE" >> "$HISTORY_FILE" 2>/dev/null || true
  if [ "$status" -ne 0 ] && [ "$CHANGED" -eq 0 ]; then
    printf '[restore] FAILED before any change: the running stack and its data are untouched\n' >&2
  elif [ "$status" -ne 0 ]; then
    compose ps -a 2>/dev/null || true
    printf '[restore] FAILED: api, worker and web may be stopped; see the stages above and deploy/README.md\n' >&2
  fi
  exit "$status"
}

# ---- Preconditions, as in remote-deploy.sh (nothing is touched before them) ----
[ -d "$SRC" ] || fail "backup directory not found: $SRC"
SRC="$(cd "$SRC" && pwd)"
command -v docker >/dev/null || fail "docker is not installed or not in PATH"
docker compose version >/dev/null 2>&1 || fail "docker compose v2 plugin is required"
docker info >/dev/null 2>&1 || fail "no access to the Docker daemon (is the user in the docker group?)"
[ -f "$ENV_FILE" ] || fail "server env file not found: $ENV_FILE (restore it from the secrets manager)"
perm="$(stat -c '%a' "$ENV_FILE" 2>/dev/null || stat -f '%Lp' "$ENV_FILE")"
if (( 8#$perm & 8#077 )); then
  fail "$ENV_FILE must not be readable by group/others (chmod 600)"
fi
if grep -E '^[[:space:]]*[A-Za-z_][A-Za-z0-9_]*=.*__SET_ON_SERVER__' "$ENV_FILE" \
    | grep -Evq '^[[:space:]]*INFRA_DB_APP_PASSWORD='; then
  fail "$ENV_FILE still assigns __SET_ON_SERVER__ placeholders"
fi
# A .env restored from the secrets manager may predate the runtime role (29.09.2026).
INFRA_SERVER_ENV_FILE="$ENV_FILE" bash "$APP_DIR/deploy/app-db-password.sh" \
  || fail "INFRA_DB_APP_PASSWORD in $ENV_FILE (see deploy/README.md «Роли PostgreSQL»)"

env_value() {
  sed -n "s/^[[:space:]]*$1=//p" "$ENV_FILE" | tail -n 1 | sed -e 's/^["'\'']//' -e 's/["'\'']$//'
}
APP_DSN_OVERRIDE="$(env_value INFRA_DB_APP_DSN)"
MODE="$(env_value INFRA_MODE)"
MODE="${MODE:-fixture}"
case "$(env_value STAND_PUBLIC)" in
  true) PUBLIC=1 ;;
  ""|false) ;;
  *) fail "STAND_PUBLIC must be true, false or empty" ;;
esac

COMPOSE_FILES=(-f "$APP_DIR/compose.yaml" -f "$APP_DIR/deploy/compose.server.yaml")
[ "$PUBLIC" -eq 0 ] || COMPOSE_FILES+=(-f "$APP_DIR/deploy/compose.public.yaml")
compose() {
  docker compose --project-directory "$APP_DIR" "${COMPOSE_FILES[@]}" --env-file "$ENV_FILE" "$@"
}

# SQL from stdin, unaligned output; the optional argument names another database.
db_psql() {
  compose exec -T db sh -c 'exec psql -X -q -v ON_ERROR_STOP=1 -At -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
}
in_worker() { compose run --rm --no-deps -T worker "$@"; }
api_get() {
  compose exec -T api python -c 'import sys, urllib.error, urllib.request
try:
    with urllib.request.urlopen("http://127.0.0.1:8000" + sys.argv[1], timeout=5) as response:
        print(response.status, response.read().decode())
except urllib.error.HTTPError as error:
    print(error.code, error.read().decode())' "$1"
}
# current_user of a service's own DSN, read inside its container (never passed here).
db_session_of() {
  compose exec -T "$1" python -c 'import os, psycopg
with psycopg.connect(os.environ["INFRA_DB_DSN"], connect_timeout=5) as connection:
    user, superuser = connection.execute(
        "SELECT current_user, rolsuper FROM pg_roles WHERE rolname = current_user"
    ).fetchone()
print(f"{user} superuser={superuser}")'
}
manifest_value() {
  sed -n "s/^  \"$1\": //p" "$SRC/manifest.json" | head -n 1 | sed -e 's/,$//' -e 's/^"//' -e 's/"$//'
}

compose config --quiet
trap summary EXIT

# ---- 1. The copy itself ----
begin verify
for file in $PAYLOAD_FILES SHA256SUMS; do
  [ -f "$SRC/$file" ] || fail "$SRC: $file is missing (not a complete backup)"
done
for file in $PAYLOAD_FILES; do
  grep -q "  $file\$" "$SRC/SHA256SUMS" || fail "$file is not listed in SHA256SUMS"
done
(cd "$SRC" && "${SHA256[@]}" -c SHA256SUMS) || fail "SHA-256 mismatch: the copy is damaged, take another one"
BACKUP_ID="$(manifest_value backup_id)"
BACKUP_REVISION="$(manifest_value app_revision)"
BACKUP_PG_NUM="$(manifest_value postgres_server_version_num)"
UPLOADS_FILES="$(manifest_value uploads_files)"
case "$BACKUP_PG_NUM$UPLOADS_FILES" in *[!0-9]*|"") fail "manifest.json: unexpected content" ;; esac
if [ -z "$REVISION" ]; then
  REVISION="$(git -C "$APP_DIR" rev-parse HEAD 2>/dev/null || true)"
fi
if [ -z "$REVISION" ]; then
  api_id="$(compose ps -q api 2>/dev/null | head -n 1 || true)"
  [ -z "$api_id" ] || REVISION="$(docker inspect -f '{{index .Config.Labels "infra-pulse.revision"}}' "$api_id" 2>/dev/null || true)"
fi
case "$REVISION" in *[!A-Za-z0-9._-]*|"") REVISION=unknown ;; esac
CURRENT_REVISION="$REVISION"
# Containers started below carry this revision label, as after remote-deploy.sh.
export APP_REVISION="$CURRENT_REVISION"
log "backup $BACKUP_ID: revision $BACKUP_REVISION, PostgreSQL $(manifest_value postgres_server_version), $UPLOADS_FILES upload files; code revision $CURRENT_REVISION, mode $MODE, public $PUBLIC"
if [ "$BACKUP_REVISION" != "$CURRENT_REVISION" ] && [ "$BACKUP_REVISION" != unknown ] && [ "$CURRENT_REVISION" != unknown ]; then
  log "WARN the code revision differs from the backup; migrations only go forward (OPS-03)"
fi
done_stage

# ---- 2. Images (a new server has none) ----
if [ "$SKIP_BUILD" -eq 0 ]; then
  begin build
  compose --profile tools build api worker web migrate
  done_stage
fi

# ---- 3. Database up (a no-op on a running stack) ----
begin db-start
compose up -d --wait --wait-timeout "$TIMEOUT" db
target_num="$(printf 'SHOW server_version_num;\n' | db_psql)"
if [ "${target_num:0:2}" -lt "${BACKUP_PG_NUM:0:2}" ]; then
  fail "PostgreSQL $target_num is older than the backup's $BACKUP_PG_NUM"
fi
done_stage

# ---- 4. Is the target clean? Decided before anything is stopped or changed ----
begin target-check
TABLES="$(db_psql <<'SQL'
SELECT format('%I.%I', n.nspname, c.relname)
FROM pg_class AS c JOIN pg_namespace AS n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p') AND n.nspname NOT IN ('pg_catalog', 'information_schema')
  AND n.nspname !~ '^pg_toast'
ORDER BY 1;
SQL
)"
NONEMPTY=0
if [ -n "$TABLES" ]; then
  sql=""
  while IFS= read -r table; do
    sql+="SELECT 1 WHERE EXISTS (SELECT 1 FROM $table) UNION ALL "
  done <<<"$TABLES"
  NONEMPTY="$(printf 'SELECT count(*) FROM (%s) AS s;\n' "${sql% UNION ALL }" | db_psql)"
fi
UPLOAD_ENTRIES="$(in_worker sh -c 'find /app/var/uploads -mindepth 1 -maxdepth 1 | wc -l' | tail -n 1 | tr -d ' \r')"
case "$NONEMPTY$UPLOAD_ENTRIES" in *[!0-9]*|"") fail "could not inspect the target (rows: '$NONEMPTY', uploads: '$UPLOAD_ENTRIES')" ;; esac
log "target: $(printf '%s' "$TABLES" | grep -c . || true) tables, $NONEMPTY with rows; $UPLOAD_ENTRIES entries in uploads"
if [ "$NONEMPTY" -gt 0 ] || [ "$UPLOAD_ENTRIES" -gt 0 ]; then
  [ "$REPLACE" -eq 1 ] || fail "the target is not clean ($NONEMPTY tables with rows, $UPLOAD_ENTRIES entries in uploads). Restore onto a new server or a new COMPOSE_PROJECT_NAME, or pass --replace (a safety backup is taken first)"
fi
done_stage

# ---- 5. Nothing may write while the data are replaced ----
begin stop
CHANGED=1
compose stop api worker web backup received-watcher
done_stage

if [ "$REPLACE" -eq 1 ] && { [ "$NONEMPTY" -gt 0 ] || [ "$UPLOAD_ENTRIES" -gt 0 ]; }; then
  begin safety-backup
  compose run --rm --no-deps -T backup --once --no-rotate --label pre-restore
  done_stage
fi

# ---- 6. Database ----
begin pg_restore
if [ -n "$TABLES" ]; then
  log "recreating database (the schema without rows or --replace)"
  printf '%s\n' 'DROP DATABASE IF EXISTS :"target" WITH (FORCE);' \
    'CREATE DATABASE :"target" OWNER :"owner" TEMPLATE template0;' \
    | compose exec -T db sh -c 'exec psql -X -q -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d postgres -v target="$POSTGRES_DB" -v owner="$POSTGRES_USER"'
fi
# Roles and grants are not part of the dump; migrations recreate what the app needs.
compose exec -T db sh -c 'exec pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --exit-on-error --single-transaction --no-owner --no-privileges' < "$SRC/db.dump"
printf 'ANALYZE;\n' | db_psql
done_stage

# ---- 7. Every table has the rows of the dump; key tables the same content ----
begin verify-data
sql=""
index=0
while IFS="$TAB" read -r table rows sum; do
  index=$((index + 1))
  sql+="SELECT $index, count(*) FROM $table UNION ALL "
done < "$SRC/tables.tsv"
checked=0
if [ "$index" -gt 0 ]; then
  counts="$(printf '%s;\n' "${sql% UNION ALL }" | db_psql)"
  index=0
  while IFS="$TAB" read -r table rows sum; do
    index=$((index + 1))
    got="$(printf '%s\n' "$counts" | awk -F'|' -v i="$index" '$1 == i {print $2}')"
    [ "$got" = "$rows" ] || fail "$table: $got rows after restore, $rows in the backup"
    if [ "$sum" != "-" ]; then
      got="$(printf "SET TimeZone = 'UTC'; SET DateStyle = ISO; SET IntervalStyle = postgres; SET extra_float_digits = 3; SET client_encoding = 'UTF8';\nCOPY %s TO STDOUT;\n" "$table" \
        | db_psql | LC_ALL=C sort | sha_stdin)"
      [ "$got" = "$sum" ] || fail "$table: content differs from the backup (sorted-rows SHA-256)"
      checked=$((checked + 1))
    fi
  done < "$SRC/tables.tsv"
fi
log "rows match the backup in $index tables; content matches in $checked (tables.tsv)"
AUDIT_ROWS="$(awk -F"$TAB" '$1 == "public.audit_events" {print $2}' "$SRC/tables.tsv")"
done_stage

# ---- 8. Uploaded files ----
begin uploads
in_worker tar -xzf - -C /app/var/uploads < "$SRC/uploads.tar.gz"
got="$(in_worker sh -c 'find /app/var/uploads -type f ! -name ".incoming-*" | wc -l' | tail -n 1 | tr -d ' \r')"
[ "$got" -ge "$UPLOADS_FILES" ] || fail "uploads: $got files after unpacking, $UPLOADS_FILES in the backup"
log "uploads: $got files (backup: $UPLOADS_FILES)"
done_stage

# ---- 9. Schema of this code revision, then the whole stack ----
begin migrate
compose run --rm -T migrate
done_stage

begin start
compose up -d --wait --wait-timeout "$TIMEOUT"
done_stage

# ---- 10. Smoke ----
begin smoke
deadline=$((SECONDS + TIMEOUT))
until live="$(api_get /health/live 2>/dev/null)" && [[ "$live" == 200* ]]; do
  (( SECONDS < deadline )) || fail "/health/live did not answer 200 within ${TIMEOUT}s"
  sleep 3
done
[[ "$live" == *'"status":"alive"'* && "$live" == *"\"mode\":\"$MODE\""* ]] \
  || fail "/health/live: $live (expected mode $MODE)"
case "$MODE" in
  replay|received)
    until ready="$(api_get /health/ready 2>/dev/null)" && [[ "$ready" == 200* ]]; do
      (( SECONDS < deadline )) || fail "/health/ready: ${ready:-no answer} within ${TIMEOUT}s"
      sleep 3
    done
    log "/health/ready: ${ready#200 }" ;;
  *)
    log "/health/ready is 503 by design in mode $MODE; liveness only" ;;
esac
for service in api worker; do
  session="$(db_session_of "$service" 2>&1 | tail -n 1)" \
    || fail "$service cannot open a PostgreSQL session: $session"
  if [ -n "$APP_DSN_OVERRIDE" ]; then
    log "WARNING $service connects as $session (INFRA_DB_APP_DSN override)"
  elif [ "$session" != "infra_pulse_app superuser=False" ]; then
    fail "$service connects as '$session', expected infra_pulse_app without superuser"
  fi
done
log "PostgreSQL: api and worker connect as ${session%% *}"
if [ -n "$AUDIT_ROWS" ]; then
  now_rows="$(printf 'SELECT count(*) FROM public.audit_events;\n' | db_psql)"
  [ "$now_rows" -ge "$AUDIT_ROWS" ] || fail "audit_events: $now_rows rows after start, $AUDIT_ROWS in the backup"
  log "audit_events: $now_rows rows after start (backup: $AUDIT_ROWS, append-only)"
fi
done_stage
log "OK restored $BACKUP_ID into Compose project ${COMPOSE_PROJECT_NAME:-infra-pulse-msk} (mode $MODE)"
