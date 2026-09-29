#!/usr/bin/env bash
# Create or update the test accounts of the public stand in lldap (SEC-02).
# Runs ON THE SERVER from the delivered checkout, after a public deploy:
#
#   bash deploy/lldap/bootstrap.sh [--users FILE] [--check]
#
# FILE (default <DEPLOY_PATH>/shared/lldap-users.txt) exists only on the server,
# chmod 600, never in Git. One account per line; blank lines and '#' comments allowed:
#
#   username:group[,group]:Display name:password
#
# Groups are the application roles: dispatcher, analyst, admin. The password is the
# rest of the line (it may contain ':'), at least 12 characters. The three groups are
# created when missing; listed accounts get their display name, groups and password;
# accounts absent from the file are kept (remove them in the lldap UI).
# --check validates the file only and does not contact lldap.
#
# Passwords travel only through stdin into the lldap container, are written to its
# memory (/dev/shm) for the image's /app/bootstrap.sh and removed after the run; they
# are never printed. The directory administrator password comes from the container
# environment (LLDAP_ADMIN_PASSWORD in the server .env).
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DEPLOY_ROOT="$(dirname "$APP_DIR")"
ENV_FILE="${INFRA_SERVER_ENV_FILE:-$DEPLOY_ROOT/shared/.env}"
USERS_FILE="$DEPLOY_ROOT/shared/lldap-users.txt"
CHECK_ONLY=0
ROLES=" dispatcher analyst admin "

while [ "$#" -gt 0 ]; do
  case "$1" in
    --users) USERS_FILE="$2"; shift 2 ;;
    --check) CHECK_ONLY=1; shift ;;
    -h|--help) sed -n '2,24p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

fail() { printf '[lldap-bootstrap] ERROR: %s\n' "$*" >&2; exit 1; }
log() { printf '[lldap-bootstrap] %s\n' "$*"; }

private_file() {
  local perm
  [ -f "$1" ] || fail "file not found: $1"
  perm="$(stat -c '%a' "$1" 2>/dev/null || stat -f '%Lp' "$1")"
  if (( 8#$perm & 8#077 )); then
    fail "$1 must not be readable by group/others (chmod 600)"
  fi
}

# Validate every line before anything reaches the directory; report line numbers,
# never the content.
validate_users() {
  local number=0 line id groups display password group seen=" "
  local -a list
  ACCOUNTS=0
  SUMMARY=""
  while IFS= read -r line || [ -n "$line" ]; do
    number=$((number + 1))
    line="${line%$'\r'}"
    case "$line" in ""|\#*) continue ;; esac
    [[ "$line" == *:*:*:* ]] || fail "line $number: expected username:groups:display name:password"
    id="${line%%:*}"; line="${line#*:}"
    groups="${line%%:*}"; line="${line#*:}"
    display="${line%%:*}"; password="${line#*:}"
    [[ "$id" =~ ^[a-z0-9][a-z0-9._-]{0,63}$ ]] \
      || fail "line $number: username must match [a-z0-9][a-z0-9._-]{0,63}"
    [[ "$seen" != *" $id "* ]] || fail "line $number: duplicate username"
    seen="$seen$id "
    [ -n "$groups" ] || fail "line $number: at least one group is required"
    IFS=, read -r -a list <<<"$groups"
    for group in "${list[@]}"; do
      [[ "$ROLES" == *" $group "* ]] \
        || fail "line $number: unknown group (allowed: dispatcher, analyst, admin)"
    done
    { [ -n "$display" ] && [ "${#display}" -le 128 ]; } \
      || fail "line $number: display name must be 1-128 characters"
    [ "${#password}" -ge 12 ] || fail "line $number: password shorter than 12 characters"
    ACCOUNTS=$((ACCOUNTS + 1))
    SUMMARY="$SUMMARY $id($groups)"
  done < "$USERS_FILE"
  [ "$ACCOUNTS" -gt 0 ] || fail "no accounts in $USERS_FILE"
}

private_file "$USERS_FILE"
validate_users
log "users file OK: $ACCOUNTS account(s):$SUMMARY"
if [ "$CHECK_ONLY" -eq 1 ]; then
  exit 0
fi

command -v docker >/dev/null || fail "docker is not installed or not in PATH"
private_file "$ENV_FILE"

compose() {
  docker compose --project-directory "$APP_DIR" \
    -f "$APP_DIR/compose.yaml" -f "$APP_DIR/deploy/compose.server.yaml" \
    -f "$APP_DIR/deploy/compose.public.yaml" --env-file "$ENV_FILE" "$@"
}

compose exec -T lldap true \
  || fail "lldap is not running: deploy with --public (or STAND_PUBLIC=true) first"
log "creating groups and accounts in lldap"
compose exec -T lldap bash -c "$(cat "$APP_DIR/deploy/lldap/render-configs.sh")" \
  render-configs < "$USERS_FILE"
log "done; roles take effect at the next login of each account"
