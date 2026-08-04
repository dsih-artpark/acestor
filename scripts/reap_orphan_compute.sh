#!/usr/bin/env bash
# reap_orphan_compute.sh — kill EC2s tagged as acestor.remote spawns
# that have been running longer than $ORPHAN_MAX_AGE_MIN (default 60m).
#
# Runs on system cron (installed by scripts/deploy_caller.sh). Idempotent,
# safe to run at any cadence. Independent of the scheduler process itself
# so it catches orphans from every failure mode:
#   * scheduler restart killed the acestor.remote subprocess before it
#     could terminate its compute box
#   * spot reclamation raced with our poll → we never learned about it
#   * acestor.remote crashed before provision returned
#   * genuine bug in provider.terminate() call
#
# Env vars (with defaults):
#   AWS_REGION              — required (no default)
#   ORPHAN_MAX_AGE_MIN      — 60
#   REAP_TAG_KEY            — "acestor:remote-runner"
#   REAP_TAG_VALUE          — "true"
#   REAP_LOG_PATH           — /home/ubuntu/acestor-work/logs/orphan-reaper.log
#
# Exit 0 unless something unexpected went wrong (e.g. bad creds).

set -euo pipefail

: "${AWS_REGION:?AWS_REGION is required}"
: "${ORPHAN_MAX_AGE_MIN:=60}"
: "${REAP_TAG_KEY:=acestor:remote-runner}"
: "${REAP_TAG_VALUE:=true}"
: "${REAP_LOG_PATH:=/home/ubuntu/acestor-work/logs/orphan-reaper.log}"

mkdir -p "$(dirname "$REAP_LOG_PATH")"

_log() { echo "[$(date -u +%FT%TZ)] reaper: $*" | tee -a "$REAP_LOG_PATH" >&2; }

now_epoch=$(date -u +%s)
cutoff_epoch=$(( now_epoch - ORPHAN_MAX_AGE_MIN * 60 ))

# Enumerate tagged instances that are alive (running / pending / stopping).
# For each: (id, instance_type, launch_time_iso).
mapfile -t rows < <(
  aws ec2 describe-instances --region "$AWS_REGION" \
    --filters "Name=tag:${REAP_TAG_KEY},Values=${REAP_TAG_VALUE}" \
              "Name=instance-state-name,Values=running,pending,stopping" \
    --query 'Reservations[].Instances[].[InstanceId,InstanceType,LaunchTime]' \
    --output text 2>/dev/null || true
)

total="${#rows[@]}"
if (( total == 0 )); then
  _log "no tagged running instances found — nothing to check"
  exit 0
fi

killed=()
for row in "${rows[@]}"; do
  [[ -z "$row" ]] && continue
  # awk out the three fields (whitespace-separated)
  read -r iid itype launch <<< "$row"
  # LaunchTime is ISO 8601 with '+00:00' — convert to epoch
  launch_epoch=$(date -u -d "$launch" +%s 2>/dev/null || \
                 date -u -j -f "%Y-%m-%dT%H:%M:%S+00:00" "$launch" +%s 2>/dev/null || \
                 echo 0)
  age_min=$(( (now_epoch - launch_epoch) / 60 ))
  if (( launch_epoch > 0 && launch_epoch < cutoff_epoch )); then
    _log "TERMINATE ${iid} (${itype}, age ${age_min}m > ${ORPHAN_MAX_AGE_MIN}m)"
    if aws ec2 terminate-instances --region "$AWS_REGION" \
         --instance-ids "$iid" >/dev/null 2>&1; then
      killed+=("$iid")
    else
      _log "FAILED to terminate ${iid}"
    fi
  fi
done

_log "reaped ${#killed[@]} of ${total} tagged instance(s)"
