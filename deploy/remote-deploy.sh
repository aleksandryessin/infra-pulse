#!/usr/bin/env bash
# Build, (re)start and smoke-test the stand. Runs ON THE SERVER from the delivered
# checkout: <DEPLOY_PATH>/app/deploy/remote-deploy.sh. Called over SSH by
# .github/workflows/deploy.yml; the operator can run it by hand the same way.
#
#   bash deploy/remote-deploy.sh [--revision SHA] [--ref LABEL] [--smoke-timeout SEC]
#                                [--log-lines N] [--smoke-only] [--public]
#
# --public (or STAND_PUBLIC=true in the server .env, which the GitHub workflow relies
# on) adds deploy/compose.public.yaml: Caddy on 80/443 with Let's Encrypt, lldap and
# directory login; the smoke then goes through Caddy with curl --resolve. Without it
# the stack stays tunnel-only (127.0.0.1 ports) and a running public stack is refused.
#
# PostgreSQL roles (deploy/README.md «Роли PostgreSQL»): INFRA_DB_APP_PASSWORD is
# generated into the server .env when missing (deploy/app-db-password.sh, never
# printed); the migrate service, as the owner POSTGRES_USER, applies the migrations and
# sets LOGIN and the password of the runtime role infra_pulse_app before every `up`;
# the smoke checks that api and worker connect as that role without superuser.
# INFRA_DB_APP_DSN in the .env (rollback to the owner) turns that check into a warning.
#
# Layout (see deploy/README.md):
#   <DEPLOY_PATH>/app/               code delivered by rsync (replaced on every deploy)
#   <DEPLOY_PATH>/shared/.env        server secrets and mode, chmod 600, never in Git
#   <DEPLOY_PATH>/shared/deploy-history.log   one line per deploy attempt
#
# Environment overrides for a local smoke only: INFRA_SERVER_ENV_FILE (path to the
# env file) and COMPOSE_PROJECT_NAME (separate project so no other stack is touched).
# Exit code is non-zero if the build, start or any smoke check fails; in that case
# container states and the last log lines are printed.
set -euo pipefail

REVISION="${APP_REVISION:-manual}"
REF_LABEL="manual"
SMOKE_TIMEOUT=180
LOG_LINES=150
SMOKE_ONLY=0
PUBLIC=0

while [ "$#" -gt 0 ]; do
  case "$1" in
    --revision) REVISION="$2"; shift 2 ;;
    --ref) REF_LABEL="$2"; shift 2 ;;
    --smoke-timeout) SMOKE_TIMEOUT="$2"; shift 2 ;;
    --log-lines) LOG_LINES="$2"; shift 2 ;;
    --smoke-only) SMOKE_ONLY=1; shift ;;
    --public) PUBLIC=1; shift ;;
    -h|--help) sed -n '2,29p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

case "$REVISION" in *[!A-Za-z0-9._-]*|"") echo "invalid --revision" >&2; exit 2 ;; esac
case "$REF_LABEL" in *[!A-Za-z0-9._/-]*|"") echo "invalid --ref" >&2; exit 2 ;; esac
case "$SMOKE_TIMEOUT" in *[!0-9]*|"") echo "invalid --smoke-timeout" >&2; exit 2 ;; esac
case "$LOG_LINES" in *[!0-9]*|"") echo "invalid --log-lines" >&2; exit 2 ;; esac

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPLOY_ROOT="$(dirname "$APP_DIR")"
ENV_FILE="${INFRA_SERVER_ENV_FILE:-$DEPLOY_ROOT/shared/.env}"
HISTORY_FILE="$(dirname "$ENV_FILE")/deploy-history.log"

log() { printf '[deploy %s] %s\n' "$(date -u +%H:%M:%S)" "$*"; }
fail() { printf '[deploy] ERROR: %s\n' "$*" >&2; exit 1; }

COMPOSE_FILES=(-f "$APP_DIR/compose.yaml" -f "$APP_DIR/deploy/compose.server.yaml")

compose() {
  docker compose --project-directory "$APP_DIR" "${COMPOSE_FILES[@]}" \
    --env-file "$ENV_FILE" "$@"
}

record() {
  printf '%s result=%s revision=%s ref=%s mode=%s public=%s\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" "$REVISION" "$REF_LABEL" "${MODE:-unknown}" \
    "$PUBLIC" >> "$HISTORY_FILE" 2>/dev/null || true
}

diagnostics() {
  local status=$?
  trap - EXIT
  if [ "$status" -ne 0 ]; then
    echo "::group::Container state"
    compose ps -a || true
    echo "::endgroup::"
    if [ "$LOG_LINES" -gt 0 ]; then
      echo "::group::Last $LOG_LINES log lines per service"
      compose logs --no-color --timestamps --tail="$LOG_LINES" || true
      echo "::endgroup::"
    fi
    record failed
    printf '[deploy] FAILED (exit %s) revision=%s\n' "$status" "$REVISION" >&2
  fi
  exit "$status"
}

# ---- Preconditions (fail before touching running containers) ----
command -v docker >/dev/null || fail "docker is not installed or not in PATH"
docker compose version >/dev/null 2>&1 || fail "docker compose v2 plugin is required"
docker info >/dev/null 2>&1 || fail "no access to the Docker daemon (is the user in the docker group?)"
[ -f "$ENV_FILE" ] || fail "server env file not found: $ENV_FILE (copy deploy/.env.server.example)"
perm="$(stat -c '%a' "$ENV_FILE" 2>/dev/null || stat -f '%Lp' "$ENV_FILE")"
if (( 8#$perm & 8#077 )); then
  fail "$ENV_FILE must not be readable by group/others (chmod 600)"
fi
# INFRA_DB_APP_PASSWORD is the exception: deploy/app-db-password.sh generates it below.
if grep -E '^[[:space:]]*[A-Za-z_][A-Za-z0-9_]*=.*__SET_ON_SERVER__' "$ENV_FILE" \
    | grep -Evq '^[[:space:]]*INFRA_DB_APP_PASSWORD='; then
  fail "$ENV_FILE still assigns __SET_ON_SERVER__ placeholders"
fi

env_value() {
  # Last assignment wins, as in Compose; surrounding quotes are stripped.
  sed -n "s/^[[:space:]]*$1=//p" "$ENV_FILE" | tail -n 1 | sed -e 's/^["'\'']//' -e 's/["'\'']$//'
}
MODE="$(env_value INFRA_MODE)"
MODE="${MODE:-fixture}"
case "$MODE" in
  fixture|scaffold|replay|received) ;;
  *) fail "unsupported INFRA_MODE=$MODE" ;;
esac

case "$(env_value STAND_PUBLIC)" in
  true) PUBLIC=1 ;;
  ""|false) ;;
  *) fail "STAND_PUBLIC must be true, false or empty" ;;
esac
if [ "$PUBLIC" -eq 1 ]; then
  for key in PUBLIC_HOST ACME_EMAIL LLDAP_JWT_SECRET LLDAP_KEY_SEED LLDAP_ADMIN_PASSWORD; do
    [ -n "$(env_value "$key")" ] || fail "public access needs $key in $ENV_FILE"
  done
  PUBLIC_HOST="$(env_value PUBLIC_HOST)"
  host_label='[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?'
  [[ "$PUBLIC_HOST" =~ ^$host_label(\.$host_label)+$ ]] || fail "PUBLIC_HOST must be a DNS name"
  ACME_CA="$(env_value ACME_CA)"
  command -v curl >/dev/null || fail "the public smoke needs curl on the server"
  COMPOSE_FILES+=(-f "$APP_DIR/deploy/compose.public.yaml")
elif docker ps -a -q --filter "label=com.docker.compose.project.working_dir=$APP_DIR" \
    --filter "label=com.docker.compose.service=caddy" | grep -q .; then
  # --remove-orphans would silently stop Caddy and lldap of a running public stand.
  fail "a public stack (caddy) runs from $APP_DIR: pass --public or set STAND_PUBLIC=true"
fi

if command -v flock >/dev/null; then
  exec 9>"$(dirname "$ENV_FILE")/.deploy.lock"
  flock -n 9 || fail "another deploy is running on this server"
fi

# Under the deploy lock: at most one deploy writes the generated password. It runs before
# the diagnostics trap (no containers touched yet), so its refusal is recorded here.
INFRA_SERVER_ENV_FILE="$ENV_FILE" bash "$APP_DIR/deploy/app-db-password.sh" \
  || { record failed; fail "INFRA_DB_APP_PASSWORD in $ENV_FILE (see deploy/README.md «Роли PostgreSQL»)"; }
APP_DSN_OVERRIDE="$(env_value INFRA_DB_APP_DSN)"

trap diagnostics EXIT
export APP_REVISION="$REVISION"

log "revision=$REVISION ref=$REF_LABEL mode=$MODE public=$PUBLIC app=$APP_DIR"
if [ -n "$APP_DSN_OVERRIDE" ]; then
  log "WARNING: INFRA_DB_APP_DSN is set: api and worker do not use the runtime role infra_pulse_app"
fi
compose config --quiet

if [ "$SMOKE_ONLY" -eq 0 ]; then
  # Every mode: api and worker connect as infra_pulse_app, whose LOGIN, password and
  # grants the owner sets here, after the migrations and before the new containers.
  # A failure stops the deploy before `up`: the running containers stay as they are.
  log "starting PostgreSQL, applying operational migrations, enabling the runtime role"
  compose up -d --wait --wait-timeout "$SMOKE_TIMEOUT" db
  compose run --build --rm --no-TTY migrate
  log "building images on this server and starting the stack"
  compose up -d --build --remove-orphans --wait --wait-timeout "$SMOKE_TIMEOUT"
fi

# ---- Smoke, both modes: api and worker reach PostgreSQL as the runtime role ----
# The DSN is read inside the container from its environment, never passed here.
db_session_of() {
  compose exec -T "$1" python -c 'import os, psycopg
with psycopg.connect(os.environ["INFRA_DB_DSN"], connect_timeout=5) as connection:
    user, superuser = connection.execute(
        "SELECT current_user, rolsuper FROM pg_roles WHERE rolname = current_user"
    ).fetchone()
print(f"{user} superuser={superuser}")'
}

db_role_smoke() {
  local service session
  for service in api worker; do
    if ! session="$(db_session_of "$service" 2>&1 | tail -n 1)"; then
      echo "smoke failed: $service cannot open a PostgreSQL session: $session" >&2
      return 1
    fi
    if [ -n "$APP_DSN_OVERRIDE" ]; then
      log "WARNING: $service connects as $session (INFRA_DB_APP_DSN override)"
    elif [ "$session" != "infra_pulse_app superuser=False" ]; then
      echo "smoke failed: $service connects as '$session', expected infra_pulse_app without superuser" >&2
      return 1
    fi
  done
  log "PostgreSQL: api and worker connect as ${session%% *}"
}

# ---- Smoke, tunnel-only stand: loopback-only ports, liveness, web, readiness ----
http_get() {
  if command -v curl >/dev/null; then
    curl -fsS --max-time 5 "$1"
  else
    python3 - "$1" <<'PY'
import sys, urllib.request
with urllib.request.urlopen(sys.argv[1], timeout=5) as response:
    sys.stdout.write(response.read().decode("utf-8", "replace"))
PY
  fi
}

wait_http() {
  local url="$1" expect="$2" deadline=$((SECONDS + SMOKE_TIMEOUT)) body=""
  while (( SECONDS < deadline )); do
    if body="$(http_get "$url" 2>/dev/null)" && [[ "$body" == *"$expect"* ]]; then
      return 0
    fi
    sleep 3
  done
  echo "smoke failed: $url did not return '$expect' within ${SMOKE_TIMEOUT}s" >&2
  return 1
}

tunnel_smoke() {
  local web_addr api_addr addr published live
  web_addr="$(compose port web 80)"
  api_addr="$(compose port api 8000)"
  for addr in "$web_addr" "$api_addr"; do
    [[ "$addr" == 127.0.0.1:* ]] || { echo "smoke failed: port not bound to loopback: $addr" >&2; return 1; }
  done
  published="$(compose ps --format '{{.Service}} {{.Ports}}')"
  if grep -E -- '(0\.0\.0\.0|\[::\]|:::)[0-9]*->' <<<"$published"; then
    echo "smoke failed: a port is published on a public interface" >&2
    return 1
  fi
  if grep -E -- '^db .*->' <<<"$published"; then
    echo "smoke failed: PostgreSQL must not be published on the host" >&2
    return 1
  fi

  wait_http "http://$api_addr/health/live" '"status":"alive"'
  live="$(http_get "http://$api_addr/health/live")"
  [[ "$live" == *"\"mode\":\"$MODE\""* ]] || { echo "smoke failed: API reports $live, expected mode $MODE" >&2; return 1; }
  wait_http "http://$web_addr/" 'id="root"'
  wait_http "http://$web_addr/health/live" '"status":"alive"'
  case "$MODE" in
    replay|received)
      wait_http "http://$api_addr/health/ready" '"status":"ready"' ;;
    fixture)
      wait_http "http://$web_addr/api/v1/attention" '"synthetic-fixture"' ;;
  esac
  SMOKE_SUMMARY="web http://$web_addr (through the SSH tunnel), API liveness 200"
}

# ---- Smoke, public stand: through Caddy on this host with curl --resolve ----
CURL_TLS=()

public_curl() {
  curl -sS --max-time 10 \
    --resolve "$PUBLIC_HOST:443:127.0.0.1" --resolve "$PUBLIC_HOST:80:127.0.0.1" \
    ${CURL_TLS[@]+"${CURL_TLS[@]}"} "$@"
}

wait_public() {
  local path="$1" expect="$2" deadline=$((SECONDS + SMOKE_TIMEOUT)) body=""
  while (( SECONDS < deadline )); do
    if body="$(public_curl -f "https://$PUBLIC_HOST$path" 2>/dev/null)" \
        && [[ "$body" == *"$expect"* ]]; then
      return 0
    fi
    sleep 5
  done
  echo "smoke failed: https://$PUBLIC_HOST$path did not return '$expect' within ${SMOKE_TIMEOUT}s" >&2
  return 1
}

expect_status() {
  local want="$1" got
  shift
  got="$(public_curl -o /dev/null -w '%{http_code}' "$@" 2>/dev/null || true)"
  [ "$got" = "$want" ] || { echo "smoke failed: $* answered '$got', expected $want" >&2; return 1; }
}

public_smoke() {
  local published line headers header live base="https://$PUBLIC_HOST" ca="production"
  case "${ACME_CA:-staging}" in
    *staging*)
      ca="staging"
      # The staging chain is untrusted by design; TLS, redirect and headers are still checked.
      CURL_TLS=(--insecure)
      log "ACME staging: certificate chain not verified; switch ACME_CA after this smoke" ;;
  esac

  published="$(compose ps --format '{{.Service}} {{.Ports}}')"
  while IFS= read -r line; do
    case "$line" in
      caddy\ *) ;;
      *'->'*) echo "smoke failed: only caddy may publish ports: $line" >&2; return 1 ;;
    esac
  done <<<"$published"
  grep -Eq -- '^caddy .*:443->443/tcp' <<<"$published" \
    || { echo "smoke failed: caddy does not publish 443/tcp" >&2; return 1; }

  wait_public /health/live '"status":"alive"'
  live="$(public_curl -f "$base/health/live")"
  [[ "$live" == *"\"mode\":\"$MODE\""* ]] || { echo "smoke failed: API reports $live, expected mode $MODE" >&2; return 1; }
  wait_public / 'id="root"'
  expect_status 308 "http://$PUBLIC_HOST/"

  headers="$(public_curl -f -D - -o /dev/null "$base/")"
  for header in 'strict-transport-security: max-age=' 'x-content-type-options: nosniff' \
      'content-security-policy:' 'x-frame-options: deny'; do
    grep -qi -- "^$header" <<<"$headers" || { echo "smoke failed: missing header $header" >&2; return 1; }
  done
  if public_curl --tlsv1 --tls-max 1.1 -o /dev/null "$base/health/live" 2>/dev/null; then
    echo "smoke failed: TLS 1.1 or older was accepted" >&2
    return 1
  fi

  # Closed without a session; schema closed at the edge; login needs anti-CSRF.
  expect_status 401 "$base/api/v1/auth/me"
  expect_status 401 "$base/api/v1/forecasts"
  expect_status 401 "$base/api/v1/imports"
  expect_status 404 "$base/docs"
  expect_status 404 "$base/openapi.json"
  # Public read-only API docs (INFRA_PUBLIC_DOCS=true); GET, since HEAD answers 405 there.
  if [ "$(env_value INFRA_PUBLIC_DOCS)" = true ]; then
    expect_status 200 "$base/api/docs"
  fi
  # A new probe name per run: the per-username throttle would answer 429 on reruns.
  local login="{\"username\":\"deploy-smoke-$$\",\"password\":\"not-a-real-password\"}"
  expect_status 403 -X POST -H 'Content-Type: application/json' -d "$login" \
    "$base/api/v1/auth/login"
  # A rejected bind (401) proves API -> lldap works; 503 would mean no directory.
  expect_status 401 -X POST -H 'Content-Type: application/json' -H 'X-CSRF-Token: smoke' \
    -d "$login" "$base/api/v1/auth/login"
  SMOKE_SUMMARY="https://$PUBLIC_HOST (Caddy, $ca certificate), login closed-by-default"
}

SMOKE_SUMMARY=""
db_role_smoke
if [ "$PUBLIC" -eq 1 ]; then
  public_smoke
else
  tunnel_smoke
fi

compose ps
record ok
log "OK: $SMOKE_SUMMARY, mode=$MODE"
if [ "$PUBLIC" -eq 1 ]; then
  log "accounts: bash deploy/lldap/bootstrap.sh (see deploy/README.md «Публичный доступ»)"
fi
