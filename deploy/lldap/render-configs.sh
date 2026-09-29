#!/usr/bin/env bash
# Runs INSIDE the lldap container; started by deploy/lldap/bootstrap.sh with the
# validated users file on stdin (username:groups:display name:password per line).
# Writes lldap bootstrap configs to memory, runs the image's /app/bootstrap.sh as the
# directory administrator (LLDAP_LDAP_USER_PASS from the container environment) and
# removes the configs. WORK_ROOT and LLDAP_BOOTSTRAP exist for a local dry run only.
set -euo pipefail
umask 077

WORK="$(mktemp -d "${WORK_ROOT:-/dev/shm}/infrapulse-bootstrap.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/users" "$WORK/groups"

for group in dispatcher analyst admin; do
  jq -n --arg name "$group" '{name: $name}' > "$WORK/groups/$group.json"
done

while IFS= read -r line || [ -n "$line" ]; do
  line="${line%$'\r'}"
  case "$line" in ""|\#*) continue ;; esac
  id="${line%%:*}"; rest="${line#*:}"
  groups="${rest%%:*}"; rest="${rest#*:}"
  display="${rest%%:*}"; password="${rest#*:}"
  # The password goes through stdin, not through jq arguments.
  printf '%s' "$password" | jq -Rs \
    --arg id "$id" --arg display "$display" --arg groups "$groups" \
    '{id: $id, email: ($id + "@users.infrapulse.invalid"), displayName: $display,
      password: ., groups: ($groups | split(","))}' > "$WORK/users/$id.json"
done

LLDAP_ADMIN_PASSWORD="${LLDAP_LDAP_USER_PASS:?directory administrator password is not set}" \
USER_CONFIGS_DIR="$WORK/users" GROUP_CONFIGS_DIR="$WORK/groups" \
USER_SCHEMAS_DIR="$WORK/no-schemas" GROUP_SCHEMAS_DIR="$WORK/no-schemas" \
  "${LLDAP_BOOTSTRAP:-/app/bootstrap.sh}"
