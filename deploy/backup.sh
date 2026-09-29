#!/usr/bin/env bash
# Backup of the stand (OPS-02, D09): PostgreSQL dump, the uploads volume and the
# configuration without secrets. Runbook: deploy/README.md «Резервные копии и восстановление».
#
# Normally runs inside the `backup` service of deploy/compose.server.yaml (profile
# "backup", same postgres image as db): the service waits for BACKUP_AT and repeats daily.
#
#   bash deploy/backup.sh --loop     service command: catch-up, then daily at BACKUP_AT (UTC)
#   bash deploy/backup.sh --once     one backup now, then rotation
#   bash deploy/backup.sh --check    newest backup: age and SHA-256 (no database needed)
#
# Options: --dir DIR (default $BACKUP_DIR or /backups), --label NAME (suffix of the
# backup name, e.g. pre-restore), --no-rotate.
#
# Environment:
#   PGHOST PGPORT PGUSER PGPASSWORD PGDATABASE   libpq connection to the stand database
#   BACKUP_UPLOADS_DIR   uploads volume, read-only (default /uploads)
#   BACKUP_APP_DIR       checkout with compose files (default: this script's ../)
#   BACKUP_AT            daily time, HH:MM UTC (default 00:30 = 03:30 MSK)
#   BACKUP_KEEP_DAYS     rotation: delete backups older than N days (default 14) ...
#   BACKUP_KEEP_MIN      ... but always keep the newest N complete ones (default 3)
#   BACKUP_MAX_AGE_HOURS --check fails when the newest backup is older (default 26)
#   BACKUP_CHECKSUM_TABLES  tables whose rows get a content SHA-256 in tables.tsv
#   APP_REVISION         Git SHA of the deployed code (set by remote-deploy.sh)
#
# One backup is a directory <BACKUP_DIR>/<YYYYMMDDTHHMMSSZ>[-label]/:
#   db.dump          pg_dump -Fc of the database, as the owner: data and grants (restore.sh
#                    skips grants; the migrations grant infra_pulse_app and analytics_read)
#   uploads.tar.gz   files of the uploads volume (without .incoming-* partial uploads)
#   config.tar.gz    compose files, Caddyfile, lldap scripts, .env template; never the real .env
#   tables.tsv       table, rows in the dump, SHA-256 of its sorted rows (or -)
#   manifest.json    revision, PostgreSQL version, sizes, SHA-256, durations
#   SHA256SUMS       `sha256sum -c` list of the five files above; restore.sh checks it first
# It is written as <name>.part and renamed only when complete. The off-VPS copy is not
# made here: another machine pulls BACKUP_DIR (runbook). Exit code is non-zero on any error.
set -euo pipefail
umask 077

MODE=""
LABEL=""
ROTATE=1
DIR="${BACKUP_DIR:-/backups}"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --loop|--once|--check) MODE="${1#--}"; shift ;;
    --dir) DIR="$2"; shift 2 ;;
    --label) LABEL="$2"; shift 2 ;;
    --no-rotate) ROTATE=0; shift ;;
    -h|--help) sed -n '2,35p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

[ -n "$MODE" ] || { echo "usage: backup.sh --loop|--once|--check [--dir DIR] [--label NAME] [--no-rotate]" >&2; exit 2; }

BACKUP_AT="${BACKUP_AT:-00:30}"
KEEP_DAYS="${BACKUP_KEEP_DAYS:-14}"
KEEP_MIN="${BACKUP_KEEP_MIN:-3}"
MAX_AGE_HOURS="${BACKUP_MAX_AGE_HOURS:-26}"
UPLOADS_DIR="${BACKUP_UPLOADS_DIR:-/uploads}"
APP_DIR="${BACKUP_APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
CHECKSUM_TABLES="${BACKUP_CHECKSUM_TABLES:-public.audit_events public.forecast_decisions public.work_order_drafts public.import_files}"
REVISION="${APP_REVISION:-unknown}"

case "$LABEL" in *[!a-z0-9-]*) echo "invalid --label (a-z, 0-9, -)" >&2; exit 2 ;; esac
case "$REVISION" in *[!A-Za-z0-9._-]*|"") REVISION="unknown" ;; esac
[[ "$BACKUP_AT" =~ ^([01][0-9]|2[0-3]):[0-5][0-9]$ ]] || { echo "BACKUP_AT must be HH:MM (UTC)" >&2; exit 2; }
for value in "$KEEP_DAYS" "$KEEP_MIN" "$MAX_AGE_HOURS"; do
  case "$value" in *[!0-9]*|"") echo "BACKUP_KEEP_DAYS, BACKUP_KEEP_MIN and BACKUP_MAX_AGE_HOURS must be integers" >&2; exit 2 ;; esac
done

# Same text form of dumped rows and of the rows restore.sh reads back (tables.tsv).
export PGTZ=UTC PGCLIENTENCODING=UTF8

# Files of one complete backup, in SHA256SUMS order.
PAYLOAD_FILES="db.dump uploads.tar.gz config.tar.gz tables.tsv manifest.json"
# Configuration copied into config.tar.gz. An allowlist: the real .env and the lldap
# accounts file live in <DEPLOY_PATH>/shared/ and are never read by this script.
CONFIG_FILES="compose.yaml deploy/compose.server.yaml deploy/compose.public.yaml
deploy/Caddyfile deploy/.env.server.example deploy/rsync-filter deploy/remote-deploy.sh
deploy/backup.sh deploy/restore.sh deploy/app-db-password.sh deploy/lldap/bootstrap.sh
deploy/lldap/render-configs.sh"

log() { printf '[backup %s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
fail() { printf '[backup %s] ERROR: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2; exit 1; }

if command -v sha256sum >/dev/null 2>&1; then
  SHA256=(sha256sum)
else
  SHA256=(shasum -a 256)
fi
sha_stdin() { "${SHA256[@]}" | awk '{print $1}'; }
size_of() { wc -c < "$1" | tr -d ' '; }

# Epoch seconds -> backup name stamp (GNU/busybox date, then BSD date).
utc_stamp() {
  date -u -d "@$1" +%Y%m%dT%H%M%SZ 2>/dev/null || date -u -r "$1" +%Y%m%dT%H%M%SZ
}

json_str() {
  local value="${1//\\/\\\\}"
  printf '"%s"' "${value//\"/\\\"}"
}

# Complete backups (directory with SHA256SUMS), oldest first; names sort by time.
list_complete() {
  local path name
  for path in "$DIR"/*/; do
    [ -d "$path" ] || continue
    name="$(basename "$path")"
    [[ "$name" =~ ^[0-9]{8}T[0-9]{6}Z(-[a-z0-9-]+)?$ ]] || continue
    [ -f "$path/SHA256SUMS" ] && printf '%s\n' "$name"
  done
}

newest_complete() { list_complete | tail -n 1; }

CURRENT_PART=""
LOCK_DIR=""
cleanup() {
  if [ -n "$CURRENT_PART" ] && [ -d "$CURRENT_PART" ]; then
    rm -rf -- "$CURRENT_PART"
    printf '[backup] removed incomplete %s\n' "$CURRENT_PART" >&2
  fi
  if [ -n "$LOCK_DIR" ]; then
    rmdir "$LOCK_DIR" 2>/dev/null || true
  fi
}

acquire_lock() {
  # One backup or rotation at a time per directory (scheduled run vs. a manual one).
  if command -v flock >/dev/null 2>&1; then
    exec 8>"$DIR/.backup.lock"
    flock -n 8 || fail "another backup is running in $DIR"
  else
    mkdir "$DIR/.backup.lock.d" 2>/dev/null || fail "another backup is running in $DIR (or remove a stale $DIR/.backup.lock.d)"
    LOCK_DIR="$DIR/.backup.lock.d"
  fi
}

rotate() {
  local names=() name stamp count index=0 cutoff removed=0 path
  cutoff="$(utc_stamp $(( $(date +%s) - KEEP_DAYS * 86400 )))"
  while IFS= read -r name; do names+=("$name"); done < <(list_complete)
  count=${#names[@]}
  for name in ${names[@]+"${names[@]}"}; do
    # The newest KEEP_MIN complete backups survive even when older than KEEP_DAYS.
    if [ "$index" -lt $(( count - KEEP_MIN )) ]; then
      stamp="${name%%-*}"
      if [[ "$stamp" < "$cutoff" ]]; then
        rm -rf -- "${DIR:?}/$name"
        log "rotation: removed $name (older than $KEEP_DAYS days)"
        removed=$((removed + 1))
      fi
    fi
    index=$((index + 1))
  done
  for path in "$DIR"/*.part; do
    [ -d "$path" ] || continue
    [ "$path" = "$CURRENT_PART" ] && continue
    rm -rf -- "$path"
    log "rotation: removed incomplete $(basename "$path")"
  done
  log "rotation: kept $(( count - removed )) backups (keep ${KEEP_DAYS} days, at least ${KEEP_MIN})"
}

# Rows per table and, for CHECKSUM_TABLES, the table's COPY lines, read back from the
# dump itself. This also proves the archive is readable by pg_restore.
dump_tables() {
  local part="$1" table rows sum rows_file
  pg_restore --data-only --file=- "$part/db.dump" | awk -v dir="$part" -v tables="$CHECKSUM_TABLES" '
    BEGIN { n = split(tables, list, " "); for (i = 1; i <= n; i++) wanted[list[i]] = 1 }
    !inside && /^COPY / && / FROM stdin;$/ {
      name = $2; rows = 0; inside = 1; keep = (name in wanted)
      if (keep) { file = name; gsub(/[^A-Za-z0-9_.]/, "_", file); out = dir "/.rows." file; printf "" > out }
      next
    }
    inside && $0 == "\\." { print name "\t" rows; if (keep) close(out); inside = 0; keep = 0; next }
    inside { rows++; if (keep) print > out }
  ' > "$part/.counts"
  while IFS="$(printf '\t')" read -r table rows; do
    sum="-"
    rows_file="$part/.rows.$(printf '%s' "$table" | tr -c 'A-Za-z0-9_.' '_')"
    if [ -f "$rows_file" ]; then
      sum="$(LC_ALL=C sort "$rows_file" | sha_stdin)"
      rm -f -- "$rows_file"
    fi
    printf '%s\t%s\t%s\n' "$table" "$rows" "$sum"
  done < "$part/.counts" > "$part/tables.tsv"
  rm -f -- "$part/.counts"
}

run_backup() {
  local started name part now t server_version server_num pg_dump_version database
  local db_bytes uploads_kb free_kb d_dump d_tables d_uploads d_config uploads_files
  local config_list=() file tables_json="" table rows sum first=1 files_json="" size digest total_bytes=0

  [ -d "$DIR" ] || fail "backup directory $DIR does not exist"
  [ -w "$DIR" ] || fail "backup directory $DIR is not writable (BACKUP_UID/BACKUP_GID)"
  [ -d "$UPLOADS_DIR" ] || fail "uploads directory $UPLOADS_DIR not found (BACKUP_UPLOADS_DIR)"
  [ -f "$APP_DIR/compose.yaml" ] || fail "compose.yaml not found in BACKUP_APP_DIR=$APP_DIR"
  acquire_lock

  started="$(date +%s)"
  name="$(utc_stamp "$started")${LABEL:+-$LABEL}"
  part="$DIR/$name.part"
  [ ! -e "$DIR/$name" ] || fail "$DIR/$name already exists"
  mkdir "$part"
  CURRENT_PART="$part"

  server_version="$(psql -X -At -v ON_ERROR_STOP=1 -c 'SHOW server_version')"
  server_num="$(psql -X -At -v ON_ERROR_STOP=1 -c 'SHOW server_version_num')"
  database="$(psql -X -At -v ON_ERROR_STOP=1 -c 'SELECT current_database()')"
  db_bytes="$(psql -X -At -v ON_ERROR_STOP=1 -c 'SELECT pg_database_size(current_database())')"
  pg_dump_version="$(pg_dump --version)"
  uploads_kb="$(du -sk "$UPLOADS_DIR" | awk '{print $1}')"
  free_kb="$(df -Pk "$DIR" | awk 'NR == 2 {print $4}')"
  log "start $name: database $database (PostgreSQL $server_version, $((db_bytes / 1048576)) MiB on disk), uploads $((uploads_kb / 1024)) MiB, free in $DIR $((free_kb / 1024)) MiB, revision $REVISION"
  if [ $(( free_kb * 1024 )) -lt $(( db_bytes + uploads_kb * 1024 )) ]; then
    log "WARN free space is below database size + uploads; the compressed copy is usually smaller"
  fi

  t="$(date +%s)"
  pg_dump -Fc --file="$part/db.dump"
  d_dump=$(( $(date +%s) - t ))

  t="$(date +%s)"
  dump_tables "$part"
  d_tables=$(( $(date +%s) - t ))

  t="$(date +%s)"
  tar -C "$UPLOADS_DIR" --exclude='.incoming-*' --exclude='./.incoming-*' -czf "$part/uploads.tar.gz" .
  uploads_files="$(tar -tvzf "$part/uploads.tar.gz" | grep -c '^-' || true)"
  d_uploads=$(( $(date +%s) - t ))

  t="$(date +%s)"
  for file in $CONFIG_FILES; do
    case "$(basename "$file")" in .env|lldap-users*) fail "refusing to copy $file" ;; esac
    [ -f "$APP_DIR/$file" ] && config_list+=("$file")
  done
  tar -C "$APP_DIR" -czf "$part/config.tar.gz" "${config_list[@]}"
  d_config=$(( $(date +%s) - t ))

  while IFS="$(printf '\t')" read -r table rows sum; do
    [ "$first" -eq 1 ] || tables_json+=","
    first=0
    tables_json+=$'\n    '"$(json_str "$table"): {\"rows\": $rows, \"sha256_sorted_rows\": $(json_str "$sum")}"
  done < "$part/tables.tsv"

  first=1
  for file in db.dump uploads.tar.gz config.tar.gz tables.tsv; do
    size="$(size_of "$part/$file")"
    digest="$("${SHA256[@]}" "$part/$file" | awk '{print $1}')"
    total_bytes=$((total_bytes + size))
    [ "$first" -eq 1 ] || files_json+=","
    first=0
    files_json+=$'\n    '"{\"name\": \"$file\", \"bytes\": $size, \"sha256\": \"$digest\"}"
  done

  now="$(date +%s)"
  {
    printf '{\n'
    printf '  "format": "infra-pulse-backup/1",\n'
    printf '  "backup_id": %s,\n' "$(json_str "$name")"
    printf '  "created_at": "%s",\n' "$(date -u -d "@$started" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -r "$started" +%Y-%m-%dT%H:%M:%SZ)"
    printf '  "app_revision": %s,\n' "$(json_str "$REVISION")"
    printf '  "postgres_server_version": %s,\n' "$(json_str "$server_version")"
    printf '  "postgres_server_version_num": %s,\n' "$server_num"
    printf '  "pg_dump_version": %s,\n' "$(json_str "$pg_dump_version")"
    printf '  "database": %s,\n' "$(json_str "$database")"
    printf '  "database_size_bytes": %s,\n' "$db_bytes"
    printf '  "uploads_files": %s,\n' "$uploads_files"
    printf '  "uploads_source_bytes": %s,\n' "$((uploads_kb * 1024))"
    printf '  "payload_bytes": %s,\n' "$total_bytes"
    printf '  "secrets_included": false,\n'
    printf '  "offsite_copy": "not made by backup.sh; see deploy/README.md",\n'
    printf '  "durations_seconds": {"dump": %s, "tables": %s, "uploads": %s, "config": %s, "total": %s},\n' \
      "$d_dump" "$d_tables" "$d_uploads" "$d_config" "$((now - started))"
    printf '  "config_files": ['
    first=1
    for file in "${config_list[@]}"; do
      [ "$first" -eq 1 ] || printf ', '
      first=0
      json_str "$file"
    done
    printf '],\n'
    printf '  "files": [%s\n  ],\n' "$files_json"
    printf '  "tables": {%s\n  }\n' "$tables_json"
    printf '}\n'
  } > "$part/manifest.json"

  (cd "$part" && for file in $PAYLOAD_FILES; do "${SHA256[@]}" "$file"; done) > "$part/SHA256SUMS"
  sync 2>/dev/null || true
  mv "$part" "$DIR/$name"
  CURRENT_PART=""
  printf '%s\n' "$name" > "$DIR/.LATEST.tmp" && mv "$DIR/.LATEST.tmp" "$DIR/LATEST"

  log "OK $name: $((total_bytes / 1048576)) MiB ($total_bytes bytes; dump $(size_of "$DIR/$name/db.dump"), uploads $(size_of "$DIR/$name/uploads.tar.gz") in $uploads_files files), $((now - started)) s (dump ${d_dump} s, tables ${d_tables} s, uploads ${d_uploads} s)"
  log "WARN off-VPS copy is not made by this script: pull $DIR from another machine (deploy/README.md)"
  if [ "$ROTATE" -eq 1 ]; then
    rotate
  fi
}

check_latest() {
  local latest cutoff file
  [ -d "$DIR" ] || fail "backup directory $DIR does not exist"
  latest="$(newest_complete)"
  [ -n "$latest" ] || fail "no complete backup in $DIR"
  for file in $PAYLOAD_FILES; do
    grep -q "  $file\$" "$DIR/$latest/SHA256SUMS" || fail "$latest: $file is not listed in SHA256SUMS"
  done
  (cd "$DIR/$latest" && "${SHA256[@]}" -c SHA256SUMS >/dev/null) || fail "$latest: SHA-256 mismatch"
  cutoff="$(utc_stamp $(( $(date +%s) - MAX_AGE_HOURS * 3600 )))"
  if [[ "${latest%%-*}" < "$cutoff" ]]; then
    fail "newest backup $latest is older than $MAX_AGE_HOURS h"
  fi
  log "OK newest backup $latest: SHA-256 verified, younger than $MAX_AGE_HOURS h, $(list_complete | wc -l | tr -d ' ') complete in $DIR"
}

seconds_until_backup_at() {
  local now target delta
  now=$(( 10#$(date -u +%H) * 3600 + 10#$(date -u +%M) * 60 + 10#$(date -u +%S) ))
  target=$(( 10#${BACKUP_AT%%:*} * 3600 + 10#${BACKUP_AT##*:} * 60 ))
  delta=$(( (target - now + 86400) % 86400 ))
  [ "$delta" -gt 0 ] || delta=86400
  printf '%s\n' "$delta"
}

# A failed run must not stop the schedule. Each attempt is a separate process, so
# `set -e` stays in force inside it (it would be ignored in a subshell under ||).
attempt() {
  local args=(--once --dir "$DIR")
  [ "$ROTATE" -eq 1 ] || args+=(--no-rotate)
  "${BASH:-bash}" "${BASH_SOURCE[0]}" "${args[@]}" \
    || log "ERROR backup failed (see above); next attempt at $BACKUP_AT UTC"
}

pause() {
  sleep "$1" &
  wait $! || true
}

loop() {
  local latest wait_s
  trap 'log "stopping"; exit 0' TERM INT
  log "schedule: daily at $BACKUP_AT UTC into $DIR; keep $KEEP_DAYS days (at least $KEEP_MIN)"
  latest="$(newest_complete || true)"
  if [ -z "$latest" ] || [[ "${latest%%-*}" < "$(utc_stamp $(( $(date +%s) - 86400 )))" ]]; then
    log "no backup in the last 24 h (newest: ${latest:-none}): catch-up run in 120 s"
    pause 120
    attempt
  fi
  while true; do
    wait_s="$(seconds_until_backup_at)"
    log "next backup in ${wait_s} s"
    pause "$wait_s"
    attempt
  done
}

case "$MODE" in
  once) trap cleanup EXIT; run_backup ;;
  check) check_latest ;;
  loop) loop ;;
esac
