#!/usr/bin/env bash
# deploy_caller.sh — idempotent deploy of acestor-scheduler onto an existing caller EC2.
#
# Transport is AWS SSM (aws ssm send-command). No SSH keys, no port 22 access
# needed for the deployer. Auth is via IAM (OIDC when run from GitHub Actions,
# your ambient creds when run from a laptop).
#
# Preconditions (script errors out with a clear message if any is missing):
#   * Caller EC2 exists with tag Name=<CALLER_INSTANCE_TAG_NAME>
#   * Caller has SSM agent + AmazonSSMManagedInstanceCore attached
#     (both true if you launched via bootstrap_infra.sh + Ubuntu 24.04)
#   * Caller is in state=running
#
# Env vars (provided by the GHA workflow or set locally):
#   AWS_REGION                (required)
#   CALLER_INSTANCE_TAG_NAME  (required, e.g. 'acestor-caller')
#   ACESTOR_REPO_URL          (optional, default: https://github.com/dsih-artpark/acestor.git)
#   ACESTOR_REPO_REF          (optional, default: production)
#   ACESTOR_AWS_KEY_NAME      (optional, only if scheduler needs it)
#   ACESTOR_AWS_AMI           (optional, only if using a custom AMI)
#   DASHBOARD_URL             (required)
#   DASHBOARD_CLIENT_ID       (required)
#   DASHBOARD_CLIENT_SECRET   (required)
#
# What runs on the caller:
#   1. git clone (first-time) or git pull (subsequent) ~/acestor-work
#   2. uv sync scheduler deps into ~/acestor-work/.venv-remote
#   3. Write ~/.env from DASHBOARD_* env vars (chmod 600)
#   4. Install / update /etc/systemd/system/acestor-scheduler.service
#   5. systemctl daemon-reload && systemctl enable --now acestor-scheduler
#   6. systemctl is-active acestor-scheduler   (health check)

set -euo pipefail

_required() {
  local name="$1"
  if [[ -z "${!name:-}" ]]; then
    echo "deploy_caller.sh: required env var ${name} is not set" >&2
    exit 2
  fi
}
_required AWS_REGION
_required CALLER_INSTANCE_TAG_NAME
_required DASHBOARD_URL
_required DASHBOARD_CLIENT_ID
_required DASHBOARD_CLIENT_SECRET

REPO_URL="${ACESTOR_REPO_URL:-https://github.com/dsih-artpark/acestor.git}"
REPO_REF="${ACESTOR_REPO_REF:-production}"

echo "==> Finding caller EC2 tagged Name=${CALLER_INSTANCE_TAG_NAME} in ${AWS_REGION}"
INSTANCE_ID=$(aws ec2 describe-instances --region "$AWS_REGION" \
  --filters "Name=tag:Name,Values=${CALLER_INSTANCE_TAG_NAME}" \
            "Name=instance-state-name,Values=running" \
  --query 'Reservations[0].Instances[0].InstanceId' --output text 2>/dev/null || echo "None")
if [[ "$INSTANCE_ID" == "None" || -z "$INSTANCE_ID" ]]; then
  echo "deploy_caller.sh: no running EC2 in ${AWS_REGION} with tag Name=${CALLER_INSTANCE_TAG_NAME}" >&2
  echo "  Create it manually with IAM profile + SG + keypair from bootstrap_infra.sh." >&2
  exit 3
fi
echo "  found: ${INSTANCE_ID}"

# Build the deploy script that will run on the caller as user 'ubuntu'.
# Kept as a heredoc so it survives one round-trip through SendCommand's
# JSON parameter surface (which is finicky about escaping).
# Systemd unit is written inline so this script is self-contained.
DEPLOY_SCRIPT=$(cat <<REMOTE_SCRIPT
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
export HOME=/home/ubuntu
cd "\$HOME"

echo "== Ensuring base tooling"
sudo apt-get -qq update
sudo apt-get -qq install -y git python3.12 python3.12-venv build-essential curl rsync >/dev/null

if ! command -v uv >/dev/null 2>&1 && [[ ! -x "\$HOME/.local/bin/uv" ]]; then
  echo "== Installing uv"
  curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null 2>&1
fi
export PATH="\$HOME/.local/bin:\$PATH"

echo "== Sync repo (${REPO_URL} @ ${REPO_REF})"
if [[ -d "\$HOME/acestor-work/.git" ]]; then
  git -C "\$HOME/acestor-work" fetch --quiet origin "${REPO_REF}"
  git -C "\$HOME/acestor-work" reset --hard "origin/${REPO_REF}"
else
  git clone --depth 20 --branch "${REPO_REF}" "${REPO_URL}" "\$HOME/acestor-work"
fi
cd "\$HOME/acestor-work"

echo "== Sync venv"
if [[ ! -d .venv-remote ]]; then
  uv venv --python 3.12 .venv-remote
fi
uv pip install --python .venv-remote/bin/python \\
  boto3 pyyaml python-dotenv rich apscheduler >/dev/null

echo "== Write ~/.env (dashboard creds)"
umask 077
cat > "\$HOME/.env" <<ENV
DASHBOARD_URL=${DASHBOARD_URL}
DASHBOARD_CLIENT_ID=${DASHBOARD_CLIENT_ID}
DASHBOARD_CLIENT_SECRET=${DASHBOARD_CLIENT_SECRET}
ENV

echo "== Install systemd unit"
sudo tee /etc/systemd/system/acestor-scheduler.service >/dev/null <<UNIT
[Unit]
Description=acestor remote-run scheduler (DAG-per-state cron)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/acestor-work
Environment=PATH=/home/ubuntu/acestor-work/.venv-remote/bin:/home/ubuntu/.local/bin:/usr/local/bin:/usr/bin:/bin
Environment=PYTHONPATH=/home/ubuntu/acestor-work
${ACESTOR_AWS_KEY_NAME:+Environment=ACESTOR_AWS_KEY_NAME=${ACESTOR_AWS_KEY_NAME}}
${ACESTOR_AWS_KEY_NAME:+Environment=ACESTOR_AWS_KEY_PATH=/home/ubuntu/.ssh/${ACESTOR_AWS_KEY_NAME}.pem}
${ACESTOR_AWS_AMI:+Environment=ACESTOR_AWS_AMI=${ACESTOR_AWS_AMI}}
ExecStart=/home/ubuntu/acestor-work/.venv-remote/bin/python /home/ubuntu/acestor-work/scripts/run_schedules_remote.py
Restart=on-failure
RestartSec=30s
StandardOutput=append:/home/ubuntu/acestor-work/logs/scheduler.log
StandardError=append:/home/ubuntu/acestor-work/logs/scheduler.log

[Install]
WantedBy=multi-user.target
UNIT
mkdir -p "\$HOME/acestor-work/logs"

echo "== Restart scheduler"
sudo systemctl daemon-reload
sudo systemctl enable acestor-scheduler >/dev/null
sudo systemctl restart acestor-scheduler
sleep 2
sudo systemctl is-active acestor-scheduler
echo "== DONE"
REMOTE_SCRIPT
)

echo "==> Submitting SSM SendCommand"
# AWS-RunShellScript runs the joined commands via /bin/sh (dash on Ubuntu),
# but our payload uses bash-only syntax ([[ ]], arrays, ${:+…}). Base64-
# encode it and decode+exec via bash on the target — bulletproof against
# shell escaping AND makes the CloudTrail entry a single opaque blob
# rather than an easily-grepped script.
if base64 --version 2>/dev/null | grep -q GNU; then
  DEPLOY_SCRIPT_B64=$(base64 -w0 <<< "$DEPLOY_SCRIPT")
else
  DEPLOY_SCRIPT_B64=$(base64 <<< "$DEPLOY_SCRIPT" | tr -d '\n')
fi
# SSM agent runs as root; our payload needs to operate on the ubuntu-owned
# repo (~/acestor-work) and end up with ubuntu-owned files. Git 2.35+ refuses
# to touch a repo whose owner differs from the current user — hence we run
# the whole payload as user ubuntu via sudo -u. Inside the payload, `sudo`
# still works for apt/systemctl because ubuntu has NOPASSWD in the default
# cloud-init sudoers.
CMD_ID=$(aws ssm send-command --region "$AWS_REGION" \
  --instance-ids "$INSTANCE_ID" \
  --document-name "AWS-RunShellScript" \
  --comment "acestor deploy $(date -u +%FT%TZ)" \
  --timeout-seconds 900 \
  --parameters "commands=[\"echo ${DEPLOY_SCRIPT_B64} | base64 -d | sudo -u ubuntu -H bash\"]" \
  --query 'Command.CommandId' --output text)
echo "  command id: ${CMD_ID}"

echo "==> Polling command status (up to 15 min)"
DEADLINE=$(( $(date +%s) + 900 ))
STATUS=""
while (( $(date +%s) < DEADLINE )); do
  STATUS=$(aws ssm get-command-invocation --region "$AWS_REGION" \
    --command-id "$CMD_ID" --instance-id "$INSTANCE_ID" \
    --query 'Status' --output text 2>/dev/null || echo "Pending")
  case "$STATUS" in
    Success|Failed|Cancelled|TimedOut) break ;;
    *) sleep 5 ;;
  esac
done

echo "==> Final status: ${STATUS}"
aws ssm get-command-invocation --region "$AWS_REGION" \
  --command-id "$CMD_ID" --instance-id "$INSTANCE_ID" \
  --query 'StandardOutputContent' --output text | tail -40

if [[ "$STATUS" != "Success" ]]; then
  echo "==> STDERR from remote:"
  aws ssm get-command-invocation --region "$AWS_REGION" \
    --command-id "$CMD_ID" --instance-id "$INSTANCE_ID" \
    --query 'StandardErrorContent' --output text | tail -40
  exit 1
fi

echo "==> Deploy OK"
