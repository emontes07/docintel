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
    './run.sh start-shards RUN_ID' \
    './run.sh export BATCH_ID OUTPUT_DIRECTORY' \
    './run.sh cost EXECUTION' \
    './run.sh cost --file METER_JSON' \
    'Start requires QUALITY_BATCH_ID and QUALITY_OWNER; start-shards uses QUALITY_SHARD_COUNT (1-32) and returns one execution name per shard.' \
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
  [[ -n "${QUALITY_WEB_ENABLED:-}" ]] && environment+=("QUALITY_WEB_ENABLED=$QUALITY_WEB_ENABLED")
  [[ -n "${QUALITY_FORD_IMAGE_BLOB:-}" ]] && environment+=("QUALITY_FORD_IMAGE_BLOB=$QUALITY_FORD_IMAGE_BLOB")
  [[ -n "${QUALITY_SMOKE_ONLY:-}" ]] && environment+=("QUALITY_SMOKE_ONLY=$QUALITY_SMOKE_ONLY")
  [[ -n "${QUALITY_SMOKE_FIRST:-}" ]] && environment+=("QUALITY_SMOKE_FIRST=$QUALITY_SMOKE_FIRST")
  [[ -n "${QUALITY_OCR_SMOKE:-}" ]] && environment+=("QUALITY_OCR_SMOKE=$QUALITY_OCR_SMOKE")
  [[ -n "${QUALITY_COST_RUN_ID:-}" ]] && environment+=("QUALITY_COST_RUN_ID=$QUALITY_COST_RUN_ID")
  [[ -n "${QUALITY_RUN_BASE_COST_USD:-}" ]] && environment+=("QUALITY_RUN_BASE_COST_USD=$QUALITY_RUN_BASE_COST_USD")
  [[ -n "${QUALITY_OVERNIGHT_PRIOR_COST_USD:-}" ]] && environment+=("QUALITY_OVERNIGHT_PRIOR_COST_USD=$QUALITY_OVERNIGHT_PRIOR_COST_USD")
  [[ -n "${QUALITY_RUN_CAP_USD:-}" ]] && environment+=("QUALITY_RUN_CAP_USD=$QUALITY_RUN_CAP_USD")
  [[ -n "${QUALITY_SESSION_CAP_USD:-}" ]] && environment+=("QUALITY_SESSION_CAP_USD=$QUALITY_SESSION_CAP_USD")
  [[ -n "${QUALITY_TOOL_LOOP_ENABLED:-}" ]] && environment+=("QUALITY_TOOL_LOOP_ENABLED=$QUALITY_TOOL_LOOP_ENABLED")
  [[ -n "${QUALITY_SECOND_LOOK_ENABLED:-}" ]] && environment+=("QUALITY_SECOND_LOOK_ENABLED=$QUALITY_SECOND_LOOK_ENABLED")
  [[ -n "${QUALITY_TOOL_LOOP_MAX_STEPS:-}" ]] && environment+=("QUALITY_TOOL_LOOP_MAX_STEPS=$QUALITY_TOOL_LOOP_MAX_STEPS")
  [[ -n "${QUALITY_AMORTIZED_PIPELINE_ENABLED:-}" ]] && environment+=("QUALITY_AMORTIZED_PIPELINE_ENABLED=$QUALITY_AMORTIZED_PIPELINE_ENABLED")
  [[ -n "${QUALITY_MODEL_TIERING_ENABLED:-}" ]] && environment+=("QUALITY_MODEL_TIERING_ENABLED=$QUALITY_MODEL_TIERING_ENABLED")
  [[ -n "${QUALITY_VENDOR_MODEL_DEPLOYMENT:-}" ]] && environment+=("QUALITY_VENDOR_MODEL_DEPLOYMENT=$QUALITY_VENDOR_MODEL_DEPLOYMENT")
  [[ -n "${QUALITY_VENDOR_MODEL_EFFORT:-}" ]] && environment+=("QUALITY_VENDOR_MODEL_EFFORT=$QUALITY_VENDOR_MODEL_EFFORT")
  [[ -n "${QUALITY_VENDOR_MODEL_MAX_OUTPUT_TOKENS:-}" ]] && environment+=("QUALITY_VENDOR_MODEL_MAX_OUTPUT_TOKENS=$QUALITY_VENDOR_MODEL_MAX_OUTPUT_TOKENS")
  [[ -n "${QUALITY_VENDOR_MODEL_INPUT_USD_PER_MILLION:-}" ]] && environment+=("QUALITY_VENDOR_MODEL_INPUT_USD_PER_MILLION=$QUALITY_VENDOR_MODEL_INPUT_USD_PER_MILLION")
  [[ -n "${QUALITY_VENDOR_MODEL_CACHED_INPUT_USD_PER_MILLION:-}" ]] && environment+=("QUALITY_VENDOR_MODEL_CACHED_INPUT_USD_PER_MILLION=$QUALITY_VENDOR_MODEL_CACHED_INPUT_USD_PER_MILLION")
  [[ -n "${QUALITY_VENDOR_MODEL_CACHE_WRITE_USD_PER_MILLION:-}" ]] && environment+=("QUALITY_VENDOR_MODEL_CACHE_WRITE_USD_PER_MILLION=$QUALITY_VENDOR_MODEL_CACHE_WRITE_USD_PER_MILLION")
  [[ -n "${QUALITY_VENDOR_MODEL_OUTPUT_USD_PER_MILLION:-}" ]] && environment+=("QUALITY_VENDOR_MODEL_OUTPUT_USD_PER_MILLION=$QUALITY_VENDOR_MODEL_OUTPUT_USD_PER_MILLION")
  [[ -n "${QUALITY_VENDOR_MODEL_PRICE_BASIS:-}" ]] && environment+=("QUALITY_VENDOR_MODEL_PRICE_BASIS=$QUALITY_VENDOR_MODEL_PRICE_BASIS")
  [[ -n "${QUALITY_AZURE_BATCH_ENABLED:-}" ]] && environment+=("QUALITY_AZURE_BATCH_ENABLED=$QUALITY_AZURE_BATCH_ENABLED")
  [[ -n "${QUALITY_PREPARE_APPROVED_SLICE:-}" ]] && environment+=("QUALITY_PREPARE_APPROVED_SLICE=$QUALITY_PREPARE_APPROVED_SLICE")
  [[ -n "${QUALITY_SHARD_INDEX:-}" ]] && environment+=("QUALITY_SHARD_INDEX=$QUALITY_SHARD_INDEX")
  [[ -n "${QUALITY_SHARD_COUNT:-}" ]] && environment+=("QUALITY_SHARD_COUNT=$QUALITY_SHARD_COUNT")
  [[ -n "${QUALITY_MAX_OUTPUT_TOKENS_EXTRACT:-}" ]] && environment+=("QUALITY_MAX_OUTPUT_TOKENS_EXTRACT=$QUALITY_MAX_OUTPUT_TOKENS_EXTRACT")
  [[ -n "${QUALITY_MAX_OUTPUT_TOKENS_REFINE:-}" ]] && environment+=("QUALITY_MAX_OUTPUT_TOKENS_REFINE=$QUALITY_MAX_OUTPUT_TOKENS_REFINE")
  [[ -n "${QUALITY_MAX_OUTPUT_TOKENS_SECOND_LOOK:-}" ]] && environment+=("QUALITY_MAX_OUTPUT_TOKENS_SECOND_LOOK=$QUALITY_MAX_OUTPUT_TOKENS_SECOND_LOOK")
  [[ -n "${QUALITY_MAX_OUTPUT_TOKENS_JUDGE:-}" ]] && environment+=("QUALITY_MAX_OUTPUT_TOKENS_JUDGE=$QUALITY_MAX_OUTPUT_TOKENS_JUDGE")
  [[ -n "${QUALITY_MAX_OUTPUT_TOKENS_TOOL_STEP:-}" ]] && environment+=("QUALITY_MAX_OUTPUT_TOKENS_TOOL_STEP=$QUALITY_MAX_OUTPUT_TOKENS_TOOL_STEP")
  [[ -n "${QUALITY_MAX_OUTPUT_TOKENS_CLOSEOUT:-}" ]] && environment+=("QUALITY_MAX_OUTPUT_TOKENS_CLOSEOUT=$QUALITY_MAX_OUTPUT_TOKENS_CLOSEOUT")
  local image base_env_json
  image=$(az containerapp job show --subscription "$subscription" --resource-group "$group" --name "$job" \
    --query 'properties.template.containers[0].image' --output tsv)
  [[ "$image" =~ ^crc4ryis6hullf4\.azurecr\.io/docintel/backend@sha256:[a-f0-9]{64}$ ]] \
    || { printf 'Job does not have the approved immutable backend image.\n' >&2; return 1; }
  base_env_json=$(az containerapp job show --subscription "$subscription" --resource-group "$group" --name "$job" \
    --query 'properties.template.containers[0].env' --output json)
  local -a merged_environment=()
  while IFS= read -r entry; do
    [[ -n "$entry" ]] && merged_environment+=("$entry")
  done < <(printf '%s' "$base_env_json" | .venv/bin/python -c '
import json,sys
values={}
for entry in json.load(sys.stdin) or []:
    name=entry["name"]
    if "secretRef" in entry:
        values[name]=name+"=secretref:"+entry["secretRef"]
    elif "value" in entry and entry["value"] is not None:
        values[name]=name+"="+str(entry["value"])
for entry in sys.argv[1:]:
    name,sep,value=entry.partition("=")
    if not sep:
        raise SystemExit("Environment override must be key=value")
    values[name]=entry
print("\n".join(values.values()))
' "${environment[@]}")
  az containerapp job start --subscription "$subscription" --resource-group "$group" --name "$job" \
    --image "$image" --container-name "$job" --command /app/.venv/bin/python \
    --args=-mbackend.quality_worker --env-vars "${merged_environment[@]}" \
    --only-show-errors --query name --output tsv
}

docintel_start_shards() {
  [[ $# == 1 ]] || { docintel_usage >&2; return 2; }
  docintel_run_id "$1"
  : "${QUALITY_BATCH_ID:?Set QUALITY_BATCH_ID to the existing batch ID}"
  : "${QUALITY_OWNER:?Set QUALITY_OWNER to the existing tenant/object owner identity}"
  docintel_run_id "$QUALITY_BATCH_ID"
  local count="${QUALITY_SHARD_COUNT:-1}" index name
  [[ "$count" =~ ^([1-9]|[12][0-9]|3[0-2])$ ]] || {
    printf 'QUALITY_SHARD_COUNT must be an integer from 1 through 32.\n' >&2
    return 2
  }
  for ((index=0; index<count; index++)); do
    name="$1-s$(printf '%02d' "$index")"
    docintel_run_id "$name"
    QUALITY_COST_RUN_ID="$name" QUALITY_SHARD_INDEX="$index" QUALITY_SHARD_COUNT="$count" docintel_start "$name"
  done
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
