#!/usr/bin/env bash

subscription=41ff069d-9e57-40a1-971f-710630f3d6bc
group=rg-docintel-dev-erik3
registry=crc4ryis6hullf4
api=ca-backend-docintel-dev-erik3
job=caj-docintel-batch-dev-erik3
repository=docintel/backend

docintel_usage() {
  printf '%s\n' \
    './run.sh build TAG' \
    './run.sh deploy REGISTRY/REPOSITORY@sha256:DIGEST' \
    './run.sh start RUN_ID' \
    './run.sh export BATCH_ID OUTPUT_DIRECTORY' \
    './run.sh cost EXECUTION' \
    './run.sh cost --file METER_JSON' \
    'Start requires QUALITY_BATCH_ID and QUALITY_OWNER; it prints the execution name.' \
    'Export: API_BASE_URL + AUTH_COOKIE_FILE (frontend), or DOCINTEL_API_URL + DOCINTEL_ACCESS_TOKEN (backend).' \
    'Optional export: EXPORT_MODE=console + QUALITY_OWNER uses existing Azure app-exec permission.'
}

docintel_build() {
  [[ $# == 1 && "$1" =~ ^[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,127}$ ]] || { docintel_usage >&2; return 2; }
  az acr build --subscription "$subscription" --registry "$registry" \
    --file backend/Dockerfile --image "$repository:$1" . >&2
  local digest
  digest=$(az acr repository show --subscription "$subscription" --name "$registry" \
    --image "$repository:$1" --query digest --output tsv)
  [[ "$digest" =~ ^sha256:[a-f0-9]{64}$ ]] || { printf 'ACR did not return an image digest.\n' >&2; return 1; }
  printf '%s.azurecr.io/%s@%s\n' "$registry" "$repository" "$digest"
}

docintel_deploy() {
  [[ $# == 1 && "$1" =~ ^crc4ryis6hullf4\.azurecr\.io/docintel/backend@sha256:[a-f0-9]{64}$ ]] \
    || { printf 'Deploy requires the immutable backend digest returned by build.\n' >&2; return 2; }
  az containerapp update --subscription "$subscription" --resource-group "$group" --name "$api" --image "$1"
  az containerapp job update --subscription "$subscription" --resource-group "$group" --name "$job" \
    --image "$1" --command /app/.venv/bin/python --args=-mbackend.quality_worker \
    --parallelism 1 --replica-completion-count 1 --replica-retry-limit 0
}

docintel_run_id() {
  [[ "$1" =~ ^[a-zA-Z0-9][a-zA-Z0-9_-]{0,99}$ ]] \
    || { printf 'Run ID must contain only letters, digits, underscores or hyphens.\n' >&2; return 2; }
}

docintel_start() {
  [[ $# == 1 ]] || { docintel_usage >&2; return 2; }
  docintel_run_id "$1"
  : "${QUALITY_BATCH_ID:?Set QUALITY_BATCH_ID to the existing batch ID}"
  : "${QUALITY_OWNER:?Set QUALITY_OWNER to the existing tenant/object owner identity}"
  docintel_run_id "$QUALITY_BATCH_ID"
  local environment=("QUALITY_RUN_ID=$1" "QUALITY_BATCH_ID=$QUALITY_BATCH_ID" "QUALITY_OWNER=$QUALITY_OWNER")
  if [[ -n "${QUALITY_WEB_ENABLED:-}" ]]; then environment+=("QUALITY_WEB_ENABLED=$QUALITY_WEB_ENABLED"); fi
  if [[ -n "${QUALITY_FORD_IMAGE_BLOB:-}" ]]; then environment+=("QUALITY_FORD_IMAGE_BLOB=$QUALITY_FORD_IMAGE_BLOB"); fi
  if [[ -n "${QUALITY_SMOKE_ONLY:-}" ]]; then environment+=("QUALITY_SMOKE_ONLY=$QUALITY_SMOKE_ONLY"); fi
  az containerapp job start --subscription "$subscription" --resource-group "$group" --name "$job" \
    --container-name "$job" --env-vars "${environment[@]}" --query name --output tsv
}

docintel_get() {
  local base="${API_BASE_URL:-${DOCINTEL_API_URL:-}}"
  [[ "$base" =~ ^https://[a-zA-Z0-9][a-zA-Z0-9.-]*(:[0-9]+)?/?$ ]] \
    || { printf 'Set API_BASE_URL (frontend) or DOCINTEL_API_URL (backend) to an HTTPS origin.\n' >&2; return 2; }
  local options=(--silent --show-error --fail --location --max-redirs 0 --proto '=https'
    --connect-timeout 10 --max-time 120 --output "$2")
  if [[ -n "${AUTH_COOKIE_FILE:-}" ]]; then
    [[ -f "$AUTH_COOKIE_FILE" && -r "$AUTH_COOKIE_FILE" && "$AUTH_COOKIE_FILE" != *=* ]] \
      || { printf 'AUTH_COOKIE_FILE must name a readable Netscape cookie jar.\n' >&2; return 2; }
    curl "${options[@]}" --cookie "$AUTH_COOKIE_FILE" "${base%/}/api$1"
    return
  fi
  : "${DOCINTEL_ACCESS_TOKEN:?Set AUTH_COOKIE_FILE or DOCINTEL_ACCESS_TOKEN to use the existing owner session}"
  [[ "$DOCINTEL_ACCESS_TOKEN" =~ ^[a-zA-Z0-9._~-]+$ ]] \
    || { printf 'Invalid bearer token format.\n' >&2; return 2; }
  # Keep bearer tokens out of the process argument list; never follow a redirect.
  printf 'header = "Authorization: Bearer %s"\n' "$DOCINTEL_ACCESS_TOKEN" \
    | curl "${options[@]}" --config - "${base%/}/api/v1$1"
}

docintel_console_export() (
  : "${QUALITY_OWNER:?Set QUALITY_OWNER to the existing batch owner}"
  local transcript="${2%.part}.console.$$.${RANDOM}.log" remote
  (umask 077; set -o noclobber; : > "$transcript") || return
  chmod 600 "./$transcript" || return
  printf 'Console export diagnostic: %s\n' "$transcript" >&2
  remote=$(.venv/bin/python scripts/console_export.py command "$1" "$QUALITY_OWNER") || return
  local command=(az containerapp exec --subscription "$subscription" --resource-group "$group" --name "$api" --command "$remote" --only-show-errors)
  if [[ "$(uname -s)" == Darwin ]]; then
    sleep 30 | script -q "$transcript" "${command[@]}" >/dev/null || return
  else
    local quoted
    printf -v quoted '%q ' "${command[@]}"
    sleep 30 | script -q -e -c "$quoted" "$transcript" >/dev/null || return
  fi
  .venv/bin/python scripts/console_export.py decode "$transcript" "$2"
)

docintel_export() {
  [[ $# == 2 && -n "$2" && "$2" != /* && "/$2/" != *"/../"* ]] \
    || { docintel_usage >&2; return 2; }
  docintel_run_id "$1"
  case "${EXPORT_MODE:-http}" in
    http) docintel_get "/batches/$1" /dev/null ;;
    console) : "${QUALITY_OWNER:?Set QUALITY_OWNER to the existing batch owner}" ;;
    *) printf 'EXPORT_MODE must be http or console.\n' >&2; return 2 ;;
  esac
  umask 077
  mkdir -p -- "$2"
  local partial="$2/$1.xlsx.part"
  local result=0
  if [[ "${EXPORT_MODE:-http}" == console ]]; then
    docintel_console_export "$1" "$partial" || result=$?
  else
    docintel_get "/batches/$1/export" "$partial" || result=$?
  fi
  if [[ "$result" != 0 ]]; then
    printf 'Export failed; any partial output is retained at %s.\n' "$partial" >&2
    return "$result"
  fi
  mv -- "$partial" "$2/$1.xlsx"
  printf 'Exported %s/%s.xlsx\n' "$2" "$1"
}

docintel_cost() {
  if [[ $# == 2 && "$1" == --file && -f "$2" ]]; then
    cat -- "$2"
    return
  fi
  [[ $# == 1 ]] || { docintel_usage >&2; return 2; }
  docintel_run_id "$1"
  az containerapp job logs show --subscription "$subscription" --resource-group "$group" --name "$job" \
    --container "$job" --execution "$1" --tail 100
}
