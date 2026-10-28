#!/usr/bin/env bash

# Screening v3 multi-dataset batch runner.
#
# Edit only the configuration block below. Every dataset directory must contain
# files with the same SOURCE_FILENAME and TAXONOMY_FILENAME. The final dataset
# name is the last component of the dataset directory.

set -u
set -o pipefail

###############################################################################
# User configuration
###############################################################################

DATASET_DIRS=(
  "/data/project_a"
  "/data/project_b"
)

SOURCE_FILENAME="cluster.jsonl"
TAXONOMY_FILENAME="taxonomy.json"
RISK_KEYWORDS="/data/common/risk_keywords.xlsx"

# Relative paths in this block are resolved from the directory containing this
# script, not from the shell's current working directory.
OUTPUT_BASE="batch_outputs"
PUBLISH_BASE="batch_publications"
CONFIG_FILE="screening/artifacts/config/pipeline.default.json"

###############################################################################
# End of user configuration
###############################################################################

SCRIPT_PATH="${BASH_SOURCE[0]}"
SCRIPT_DIR="$(cd -- "$(dirname -- "$SCRIPT_PATH")" && pwd -P)"
SCRIPT_PATH="${SCRIPT_DIR}/$(basename -- "$SCRIPT_PATH")"
STATE_DIR="${SCRIPT_DIR}/.screening_batch"
RUNS_DIR="${STATE_DIR}/runs"
PID_FILE="${STATE_DIR}/current.pid"
LATEST_FILE="${STATE_DIR}/latest"

ACTIVE_BATCH_DIR=""
FINAL_STATUS_WRITTEN=0

usage() {
  cat <<'EOF'
Usage: ./run_screening_batch.sh {check|run|start|status}

  check   Validate Bash, the active Python environment, configuration and inputs.
  run     Run the batch in the foreground and keep per-dataset logs.
  start   Start the batch with nohup so it survives an SSH disconnect.
  status  Show the latest batch, process state and success/failure counts.
EOF
}

log() {
  printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

resolve_from_script() {
  local value="$1"
  if [[ "$value" == /* ]]; then
    printf '%s\n' "$value"
  else
    printf '%s/%s\n' "$SCRIPT_DIR" "$value"
  fi
}

trim_trailing_slashes() {
  local value="$1"
  while [[ "$value" != "/" && "$value" == */ ]]; do
    value="${value%/}"
  done
  printf '%s\n' "$value"
}

dataset_path() {
  local value
  value="$(trim_trailing_slashes "$1")"
  resolve_from_script "$value"
}

dataset_id() {
  local value
  value="$(trim_trailing_slashes "$1")"
  basename -- "$value"
}

contains_unsafe_record_characters() {
  local value="$1"
  [[ "$value" == *$'\n'* || "$value" == *$'\r'* || "$value" == *$'\t'* ]]
}

read_pid() {
  local pid=""
  if [[ -f "$PID_FILE" ]]; then
    IFS= read -r pid < "$PID_FILE" || true
  fi
  printf '%s\n' "$pid"
}

pid_is_active() {
  local pid="$1"
  [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null
}

current_batch_is_active() {
  local pid
  pid="$(read_pid)"
  pid_is_active "$pid"
}

write_status() {
  local batch_dir="$1"
  local status="$2"
  printf '%s\n' "$status" > "${batch_dir}/status"
}

cleanup_pid_file() {
  local stored_pid
  stored_pid="$(read_pid)"
  if [[ "$stored_pid" == "$$" ]]; then
    rm -f -- "$PID_FILE"
  fi
}

on_exit() {
  local rc=$?
  if [[ -n "$ACTIVE_BATCH_DIR" && "$FINAL_STATUS_WRITTEN" -eq 0 ]]; then
    write_status "$ACTIVE_BATCH_DIR" "INTERRUPTED"
    printf 'finished_at\t%s\nexit_code\t%s\n' \
      "$(date '+%Y-%m-%d %H:%M:%S')" "$rc" >> "${ACTIVE_BATCH_DIR}/summary.txt"
  fi
  cleanup_pid_file
}

on_signal() {
  local signal_number="$1"
  exit "$signal_number"
}

trap on_exit EXIT
trap 'on_signal 130' INT
trap 'on_signal 143' TERM

validate_bash() {
  if (( BASH_VERSINFO[0] < 4 )); then
    printf 'ERROR: Bash 4 or newer is required; found %s.\n' "$BASH_VERSION" >&2
    return 1
  fi
  return 0
}

validate_common_configuration() {
  local errors=0
  local config_file risk_keywords
  config_file="$(resolve_from_script "$CONFIG_FILE")"
  risk_keywords="$(resolve_from_script "$RISK_KEYWORDS")"

  if [[ ${#DATASET_DIRS[@]} -eq 0 ]]; then
    printf 'ERROR: DATASET_DIRS is empty.\n' >&2
    errors=$((errors + 1))
  fi
  if [[ -z "$SOURCE_FILENAME" || "$SOURCE_FILENAME" == */* ]]; then
    printf 'ERROR: SOURCE_FILENAME must be a non-empty filename, not a path.\n' >&2
    errors=$((errors + 1))
  fi
  if [[ -z "$TAXONOMY_FILENAME" || "$TAXONOMY_FILENAME" == */* ]]; then
    printf 'ERROR: TAXONOMY_FILENAME must be a non-empty filename, not a path.\n' >&2
    errors=$((errors + 1))
  fi
  if [[ ! -f "$config_file" ]]; then
    printf 'ERROR: pipeline config does not exist: %s\n' "$config_file" >&2
    errors=$((errors + 1))
  fi
  if [[ ! -f "$risk_keywords" ]]; then
    printf 'ERROR: risk keyword workbook does not exist: %s\n' "$risk_keywords" >&2
    errors=$((errors + 1))
  fi
  if [[ ! -f "${SCRIPT_DIR}/screening/run_pipeline.py" ]]; then
    printf 'ERROR: screening module not found beside this script: %s\n' \
      "${SCRIPT_DIR}/screening/run_pipeline.py" >&2
    errors=$((errors + 1))
  fi
  if ! command -v python >/dev/null 2>&1; then
    printf 'ERROR: python is not available on PATH; activate the intended environment first.\n' >&2
    errors=$((errors + 1))
  elif ! python --version >/dev/null 2>&1; then
    printf 'ERROR: python from the active environment cannot be executed.\n' >&2
    errors=$((errors + 1))
  fi
  return "$errors"
}

validate_unique_dataset_ids() {
  local raw id errors=0
  declare -A seen=()

  for raw in "${DATASET_DIRS[@]}"; do
    id="$(dataset_id "$raw")"
    if [[ -z "$raw" || -z "$id" || "$id" == "/" || "$id" == "." || "$id" == ".." ]]; then
      printf 'ERROR: invalid dataset directory: %q\n' "$raw" >&2
      errors=$((errors + 1))
      continue
    fi
    if contains_unsafe_record_characters "$raw" || contains_unsafe_record_characters "$id"; then
      printf 'ERROR: dataset paths cannot contain tabs or line breaks: %q\n' "$raw" >&2
      errors=$((errors + 1))
      continue
    fi
    if [[ -n "${seen[$id]+x}" ]]; then
      printf 'ERROR: duplicate dataset name %q derived from both %q and %q.\n' \
        "$id" "${seen[$id]}" "$raw" >&2
      errors=$((errors + 1))
    else
      seen[$id]="$raw"
    fi
  done
  return "$errors"
}

check_dataset_inputs() {
  local raw="$1"
  local dir source taxonomy errors=0
  dir="$(dataset_path "$raw")"
  source="${dir}/${SOURCE_FILENAME}"
  taxonomy="${dir}/${TAXONOMY_FILENAME}"

  if [[ ! -d "$dir" ]]; then
    printf 'ERROR: dataset directory does not exist: %s\n' "$dir" >&2
    errors=$((errors + 1))
  fi
  if [[ ! -f "$source" ]]; then
    printf 'ERROR: source file does not exist: %s\n' "$source" >&2
    errors=$((errors + 1))
  fi
  if [[ ! -f "$taxonomy" ]]; then
    printf 'ERROR: taxonomy file does not exist: %s\n' "$taxonomy" >&2
    errors=$((errors + 1))
  fi
  return "$errors"
}

check_all() {
  local errors=0 raw id dir output_root publish_root

  validate_bash || errors=$((errors + 1))
  validate_common_configuration || errors=$((errors + $?))
  validate_unique_dataset_ids || errors=$((errors + $?))

  printf '\nResolved datasets:\n'
  for raw in "${DATASET_DIRS[@]}"; do
    id="$(dataset_id "$raw")"
    dir="$(dataset_path "$raw")"
    output_root="$(resolve_from_script "$OUTPUT_BASE")/${id}"
    publish_root="$(resolve_from_script "$PUBLISH_BASE")/${id}"
    printf '  - %s\n    input:   %s\n    output:  %s\n    publish: %s\n' \
      "$id" "$dir" "$output_root" "$publish_root"
    check_dataset_inputs "$raw" || errors=$((errors + $?))
  done

  if (( errors > 0 )); then
    printf '\nCheck failed with %d error(s).\n' "$errors" >&2
    return 1
  fi
  printf '\nCheck passed. No pipeline or LLM request was executed.\n'
  return 0
}

new_batch_dir() {
  local batch_id
  batch_id="$(date '+%Y%m%d_%H%M%S')_$$"
  printf '%s/%s\n' "$RUNS_DIR" "$batch_id"
}

prepare_batch_dir() {
  local batch_dir="$1"
  mkdir -p -- "$batch_dir"
  printf '%s\n' "$batch_dir" > "$LATEST_FILE"
  : > "${batch_dir}/summary.tsv"
  printf 'index\tdataset\tstatus\texit_code\tsource\ttaxonomy\toutput_root\tpublish_root\tlog\n' \
    >> "${batch_dir}/summary.tsv"
  printf 'started_at\t%s\n' "$(date '+%Y-%m-%d %H:%M:%S')" > "${batch_dir}/summary.txt"
  write_status "$batch_dir" "RUNNING"
}

append_summary_row() {
  local batch_dir="$1" index="$2" id="$3" result="$4" rc="$5"
  local source="$6" taxonomy="$7" output_root="$8" publish_root="$9" item_log="${10}"
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
    "$index" "$id" "$result" "$rc" "$source" "$taxonomy" \
    "$output_root" "$publish_root" "$item_log" >> "${batch_dir}/summary.tsv"
}

run_one_dataset() {
  local batch_dir="$1" index="$2" raw="$3"
  local id dir source taxonomy output_root publish_root risk_keywords config_file item_log
  local rc result
  local -a command

  id="$(dataset_id "$raw")"
  dir="$(dataset_path "$raw")"
  source="${dir}/${SOURCE_FILENAME}"
  taxonomy="${dir}/${TAXONOMY_FILENAME}"
  output_root="$(resolve_from_script "$OUTPUT_BASE")/${id}"
  publish_root="$(resolve_from_script "$PUBLISH_BASE")/${id}"
  risk_keywords="$(resolve_from_script "$RISK_KEYWORDS")"
  config_file="$(resolve_from_script "$CONFIG_FILE")"
  item_log="${batch_dir}/$(printf '%03d' "$index")-${id}.log"

  {
    printf 'dataset=%s\nsource=%s\ntaxonomy=%s\noutput_root=%s\npublish_root=%s\n' \
      "$id" "$source" "$taxonomy" "$output_root" "$publish_root"
  } > "$item_log"

  if ! check_dataset_inputs "$raw" >> "$item_log" 2>&1; then
    rc=64
    result="FAILED_INPUT"
    printf 'result=%s exit_code=%s\n' "$result" "$rc" >> "$item_log"
    append_summary_row "$batch_dir" "$index" "$id" "$result" "$rc" \
      "$source" "$taxonomy" "$output_root" "$publish_root" "$item_log"
    log "[$index/${#DATASET_DIRS[@]}] $id failed input validation; continuing"
    return 1
  fi

  command=(
    python -m screening.run_pipeline
    --config "$config_file"
    --source "$source"
    --taxonomy "$taxonomy"
    --risk-keywords "$risk_keywords"
    --output-root "$output_root"
    --publish-root "$publish_root"
  )

  {
    printf 'command='
    printf '%q ' "${command[@]}"
    printf '\nstarted_at=%s\n' "$(date '+%Y-%m-%d %H:%M:%S')"
  } >> "$item_log"

  log "[$index/${#DATASET_DIRS[@]}] starting $id"
  (
    cd -- "$SCRIPT_DIR" || exit 1
    "${command[@]}"
  ) >> "$item_log" 2>&1
  rc=$?

  if (( rc == 0 )); then
    result="SUCCESS"
    log "[$index/${#DATASET_DIRS[@]}] $id completed"
  else
    result="FAILED_PIPELINE"
    log "[$index/${#DATASET_DIRS[@]}] $id failed with exit code $rc; continuing"
  fi
  printf 'finished_at=%s\nresult=%s\nexit_code=%s\n' \
    "$(date '+%Y-%m-%d %H:%M:%S')" "$result" "$rc" >> "$item_log"
  append_summary_row "$batch_dir" "$index" "$id" "$result" "$rc" \
    "$source" "$taxonomy" "$output_root" "$publish_root" "$item_log"
  (( rc == 0 ))
}

run_batch() {
  local batch_dir="$1"
  local total=${#DATASET_DIRS[@]} successes=0 failures=0 index=0 raw final_status final_rc

  ACTIVE_BATCH_DIR="$batch_dir"
  if ! validate_bash || ! validate_common_configuration || ! validate_unique_dataset_ids; then
    write_status "$batch_dir" "CONFIGURATION_FAILED"
    printf 'finished_at\t%s\nstatus\tCONFIGURATION_FAILED\n' \
      "$(date '+%Y-%m-%d %H:%M:%S')" >> "${batch_dir}/summary.txt"
    FINAL_STATUS_WRITTEN=1
    return 1
  fi

  for raw in "${DATASET_DIRS[@]}"; do
    index=$((index + 1))
    if run_one_dataset "$batch_dir" "$index" "$raw"; then
      successes=$((successes + 1))
    else
      failures=$((failures + 1))
    fi
  done

  if (( failures == 0 )); then
    final_status="SUCCESS"
    final_rc=0
  else
    final_status="PARTIAL_FAILED"
    final_rc=1
  fi
  write_status "$batch_dir" "$final_status"
  {
    printf 'finished_at\t%s\n' "$(date '+%Y-%m-%d %H:%M:%S')"
    printf 'status\t%s\n' "$final_status"
    printf 'total\t%s\n' "$total"
    printf 'successes\t%s\n' "$successes"
    printf 'failures\t%s\n' "$failures"
  } >> "${batch_dir}/summary.txt"
  FINAL_STATUS_WRITTEN=1
  log "batch finished: status=$final_status total=$total successes=$successes failures=$failures"
  return "$final_rc"
}

run_foreground() {
  local batch_dir
  mkdir -p -- "$RUNS_DIR"
  if current_batch_is_active; then
    printf 'ERROR: another batch is active with PID %s.\n' "$(read_pid)" >&2
    return 1
  fi
  batch_dir="$(new_batch_dir)"
  prepare_batch_dir "$batch_dir"
  printf '%s\n' "$$" > "$PID_FILE"
  log "foreground batch logs: $batch_dir"
  run_batch "$batch_dir"
}

start_background() {
  local batch_dir pid startup_status="UNKNOWN"
  if ! command -v nohup >/dev/null 2>&1; then
    printf 'ERROR: nohup is not available on PATH.\n' >&2
    return 1
  fi
  mkdir -p -- "$RUNS_DIR"
  if current_batch_is_active; then
    printf 'ERROR: another batch is active with PID %s.\n' "$(read_pid)" >&2
    return 1
  fi
  batch_dir="$(new_batch_dir)"
  prepare_batch_dir "$batch_dir"
  nohup "$SCRIPT_PATH" __worker "$batch_dir" \
    > "${batch_dir}/batch.log" 2>&1 < /dev/null &
  pid=$!
  printf '%s\n' "$pid" > "$PID_FILE"
  sleep 1
  if ! pid_is_active "$pid"; then
    if [[ -f "${batch_dir}/status" ]]; then
      IFS= read -r startup_status < "${batch_dir}/status" || true
    fi
    if [[ "$startup_status" == "SUCCESS" ]]; then
      printf 'Screening batch finished during startup.\nStatus: %s\nLogs: %s\n' \
        "$startup_status" "$batch_dir"
      return 0
    fi
    printf 'ERROR: background batch exited during startup (status=%s); inspect %s\n' \
      "$startup_status" "${batch_dir}/batch.log" >&2
    return 1
  fi
  printf 'Started screening batch.\nPID: %s\nLogs: %s\n' "$pid" "$batch_dir"
}

show_status() {
  local batch_dir="" pid="" status="UNKNOWN" active="no"
  local total=0 successes=0 failures=0 row_status

  if [[ -f "$LATEST_FILE" ]]; then
    IFS= read -r batch_dir < "$LATEST_FILE" || true
  fi
  if [[ -z "$batch_dir" || ! -d "$batch_dir" ]]; then
    printf 'No screening batch has been started yet.\n'
    return 0
  fi
  pid="$(read_pid)"
  if pid_is_active "$pid"; then
    active="yes"
  fi
  if [[ -f "${batch_dir}/status" ]]; then
    IFS= read -r status < "${batch_dir}/status" || true
  fi
  if [[ "$status" == "RUNNING" && "$active" == "no" ]]; then
    status="INTERRUPTED_OR_STALE"
  fi
  if [[ -f "${batch_dir}/summary.tsv" ]]; then
    while IFS=$'\t' read -r _ _ row_status _; do
      [[ "$row_status" == "status" ]] && continue
      [[ -z "$row_status" ]] && continue
      total=$((total + 1))
      if [[ "$row_status" == "SUCCESS" ]]; then
        successes=$((successes + 1))
      else
        failures=$((failures + 1))
      fi
    done < "${batch_dir}/summary.tsv"
  fi
  printf 'Batch: %s\nStatus: %s\nActive: %s\n' "$batch_dir" "$status" "$active"
  if [[ -n "$pid" ]]; then
    printf 'PID: %s\n' "$pid"
  fi
  printf 'Completed: %s/%s (success=%s failed=%s)\n' \
    "$total" "${#DATASET_DIRS[@]}" "$successes" "$failures"
  if [[ -f "${batch_dir}/batch.log" ]]; then
    printf 'Batch log: %s\n' "${batch_dir}/batch.log"
  fi
  printf 'Summary: %s\n' "${batch_dir}/summary.tsv"
}

main() {
  local action="${1:-}"
  case "$action" in
    check)
      check_all
      ;;
    run)
      run_foreground
      ;;
    start)
      start_background
      ;;
    status)
      show_status
      ;;
    __worker)
      if [[ $# -ne 2 || ! -d "$2" ]]; then
        printf 'ERROR: invalid internal worker invocation.\n' >&2
        return 2
      fi
      ACTIVE_BATCH_DIR="$2"
      run_batch "$2"
      ;;
    -h|--help|help)
      usage
      ;;
    *)
      usage >&2
      return 2
      ;;
  esac
}

main "$@"
