#!/usr/bin/env bash
# bootstrap_infra.sh — one-time greenfield setup for the acestor-caller stack.
#
# Idempotent. Safe to re-run — each resource is created only if missing.
# Interactive: prompts for anything account-specific (region, GitHub repo,
# whether to bake a custom AMI, whether to launch the caller EC2 now).
#
# Creates (only what's missing):
#   1. IAM role + inline policy + instance profile 'acestor-caller'
#      (attached to the caller EC2; grants ec2:RunInstances etc.)
#   2. Security group 'acestor-caller-sg' with SSH from your current IP
#   3. EC2 keypair 'acestor-caller' (PEM saved locally)
#   4. GitHub OIDC identity provider (if not present)
#   5. IAM role 'github-actions-deploy-caller' with trust policy scoped to
#      the acestor repo — .github/workflows/deploy-caller.yml assumes it
#      at deploy time (no long-lived AWS keys in GitHub)
#
# Dashboard creds + the PEM live in GitHub Actions Secrets — this script
# just prints a Summary block telling you exactly what to paste where.
#
# Deliberately NOT automated (user should do manually per docs):
#   * Baking the tier-2 custom AMI (~30 min one-time task)
#   * Launching the caller EC2 (small, one-time; instructions in Summary)

set -euo pipefail

# ── Colour helpers ────────────────────────────────────────────────────────────
if [[ -t 1 ]]; then
  C_OK=$'\033[32m'; C_WARN=$'\033[33m'; C_ERR=$'\033[31m'; C_DIM=$'\033[2m'; C_R=$'\033[0m'
else
  C_OK=""; C_WARN=""; C_ERR=""; C_DIM=""; C_R=""
fi
say() { echo -e "${C_DIM}[$(date -u +%FT%TZ)]${C_R} $*"; }
ok()  { echo -e "  ${C_OK}✓${C_R} $*"; }
warn(){ echo -e "  ${C_WARN}!${C_R} $*"; }
err() { echo -e "  ${C_ERR}✗${C_R} $*" >&2; }
prompt() { local q="$1" default="${2:-}"; local a; read -rp "$q [${default}]: " a; echo "${a:-$default}"; }
confirm() { local q="$1" default="${2:-n}"; local a; read -rp "$q [y/N]: " a; a="${a:-$default}"; [[ "${a,,}" =~ ^y ]]; }

# ── Pre-flight ────────────────────────────────────────────────────────────────
command -v aws >/dev/null || { err "aws CLI not found; install it first."; exit 1; }
command -v jq  >/dev/null || { err "jq not found (needed for parsing AWS output)."; exit 1; }

if ! aws sts get-caller-identity >/dev/null 2>&1; then
  err "AWS credentials not configured. Run 'aws configure' or 'aws sso login' first."
  exit 1
fi
CALLER_ID=$(aws sts get-caller-identity --query Arn --output text)
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
say "Running as: ${CALLER_ID}"

# ── Interactive config ────────────────────────────────────────────────────────
REGION=$(prompt "AWS region" "ap-south-1")
GITHUB_REPO=$(prompt "GitHub repo (org/name)" "dsih-artpark/acestor")
GITHUB_BRANCH=$(prompt "Deploy branch (trust policy scope)" "production")
KEYPAIR_NAME=$(prompt "EC2 keypair name" "acestor-caller")
PEM_LOCAL_PATH=$(prompt "Where to save the PEM locally" "$HOME/.ssh/${KEYPAIR_NAME}.pem")
CALLER_SG_NAME=$(prompt "Security group name" "acestor-caller-sg")
CALLER_IAM_ROLE=$(prompt "Caller IAM role name" "acestor-caller")
DEPLOY_IAM_ROLE=$(prompt "GitHub Actions deploy IAM role name" "github-actions-deploy-caller")

echo
say "Will create in ${C_OK}${REGION}${C_R} for account ${C_OK}${ACCOUNT_ID}${C_R}."
confirm "Proceed?" "y" || { warn "Aborted."; exit 0; }

MY_IP=$(curl -fsS ifconfig.me 2>/dev/null || echo "")
[[ -z "$MY_IP" ]] && MY_IP=$(prompt "Couldn't detect your public IP. Enter manually" "0.0.0.0")

# ── 1. Caller IAM role + policy + instance profile ────────────────────────────
say "1/5  Caller IAM role '${CALLER_IAM_ROLE}'"
if aws iam get-role --role-name "$CALLER_IAM_ROLE" >/dev/null 2>&1; then
  ok "role exists — skipping create"
else
  aws iam create-role --role-name "$CALLER_IAM_ROLE" \
    --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]}' \
    >/dev/null
  ok "created"
fi

CALLER_POLICY=$(cat <<'POLICY'
{
  "Version": "2012-10-17",
  "Statement": [
    {"Sid":"EC2Lifecycle","Effect":"Allow","Action":[
      "ec2:RunInstances","ec2:DescribeInstances","ec2:DescribeImages",
      "ec2:TerminateInstances","ec2:CreateTags"],
      "Resource":"*"},
    {"Sid":"SSMAmiLookup","Effect":"Allow","Action":"ssm:GetParameter",
      "Resource":"arn:aws:ssm:*::parameter/aws/service/canonical/ubuntu/*"}
  ]
}
POLICY
)
aws iam put-role-policy --role-name "$CALLER_IAM_ROLE" \
  --policy-name "$CALLER_IAM_ROLE" --policy-document "$CALLER_POLICY"
ok "inline policy updated"

# Attach AWS-managed SSM policy so the deploy workflow can run commands on
# the caller via `aws ssm send-command` — no SSH port + no PEM in GitHub.
aws iam attach-role-policy --role-name "$CALLER_IAM_ROLE" \
  --policy-arn "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore" >/dev/null
ok "SSM managed policy attached (enables aws ssm send-command from CI/CD)"

if aws iam get-instance-profile --instance-profile-name "$CALLER_IAM_ROLE" >/dev/null 2>&1; then
  ok "instance profile exists"
else
  aws iam create-instance-profile --instance-profile-name "$CALLER_IAM_ROLE" >/dev/null
  aws iam add-role-to-instance-profile --instance-profile-name "$CALLER_IAM_ROLE" --role-name "$CALLER_IAM_ROLE"
  ok "instance profile created + role attached"
fi

# ── 2. Security group ─────────────────────────────────────────────────────────
say "2/5  Security group '${CALLER_SG_NAME}' (SSH from ${MY_IP})"
VPC=$(aws ec2 describe-vpcs --region "$REGION" --filters Name=isDefault,Values=true \
      --query 'Vpcs[0].VpcId' --output text)
if [[ "$VPC" == "None" || -z "$VPC" ]]; then
  err "No default VPC in ${REGION}. Create one manually or edit this script."
  exit 1
fi
SG_ID=$(aws ec2 describe-security-groups --region "$REGION" \
        --filters "Name=group-name,Values=${CALLER_SG_NAME}" "Name=vpc-id,Values=${VPC}" \
        --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null || echo "None")
if [[ "$SG_ID" == "None" || -z "$SG_ID" ]]; then
  SG_ID=$(aws ec2 create-security-group --region "$REGION" \
          --group-name "$CALLER_SG_NAME" \
          --description "SSH from operator to acestor caller box" \
          --vpc-id "$VPC" --query GroupId --output text)
  ok "created ${SG_ID}"
else
  ok "exists ${SG_ID}"
fi
aws ec2 authorize-security-group-ingress --region "$REGION" --group-id "$SG_ID" \
  --protocol tcp --port 22 --cidr "${MY_IP}/32" >/dev/null 2>&1 && ok "SSH rule added" \
  || ok "SSH rule already present (or duplicate)"

# ── 3. Keypair ────────────────────────────────────────────────────────────────
say "3/5  EC2 keypair '${KEYPAIR_NAME}'"
if aws ec2 describe-key-pairs --region "$REGION" --key-names "$KEYPAIR_NAME" >/dev/null 2>&1; then
  warn "keypair exists in AWS. Place the PEM at ${PEM_LOCAL_PATH} if you have it."
  warn "If lost, delete + re-create: aws ec2 delete-key-pair --key-name ${KEYPAIR_NAME}"
else
  mkdir -p "$(dirname "$PEM_LOCAL_PATH")"
  aws ec2 create-key-pair --region "$REGION" --key-name "$KEYPAIR_NAME" \
    --query KeyMaterial --output text > "$PEM_LOCAL_PATH"
  chmod 600 "$PEM_LOCAL_PATH"
  ok "created + saved to ${PEM_LOCAL_PATH}"
fi

# ── 4. GitHub OIDC identity provider ──────────────────────────────────────────
say "4/5  GitHub OIDC identity provider"
OIDC_ARN="arn:aws:iam::${ACCOUNT_ID}:oidc-provider/token.actions.githubusercontent.com"
if aws iam get-open-id-connect-provider --open-id-connect-provider-arn "$OIDC_ARN" >/dev/null 2>&1; then
  ok "OIDC provider exists"
else
  aws iam create-open-id-connect-provider \
    --url "https://token.actions.githubusercontent.com" \
    --client-id-list "sts.amazonaws.com" \
    --thumbprint-list "6938fd4d98bab03faadb97b34396831e3780aea1" >/dev/null
  ok "created (thumbprint = GitHub's SHA1; AWS also validates via CA chain post-2023)"
fi

# ── 5. GitHub Actions deploy IAM role ─────────────────────────────────────────
say "5/5  GitHub Actions deploy role '${DEPLOY_IAM_ROLE}'"
# Trust scope: this exact repo, from branch=<GITHUB_BRANCH> OR workflow_dispatch.
DEPLOY_TRUST=$(cat <<TRUST
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"Federated": "${OIDC_ARN}"},
    "Action": "sts:AssumeRoleWithWebIdentity",
    "Condition": {
      "StringEquals": {"token.actions.githubusercontent.com:aud": "sts.amazonaws.com"},
      "StringLike":   {"token.actions.githubusercontent.com:sub": [
        "repo:${GITHUB_REPO}:ref:refs/heads/${GITHUB_BRANCH}",
        "repo:${GITHUB_REPO}:environment:production"
      ]}
    }
  }]
}
TRUST
)
if aws iam get-role --role-name "$DEPLOY_IAM_ROLE" >/dev/null 2>&1; then
  aws iam update-assume-role-policy --role-name "$DEPLOY_IAM_ROLE" \
    --policy-document "$DEPLOY_TRUST" >/dev/null
  ok "role exists — trust policy updated"
else
  aws iam create-role --role-name "$DEPLOY_IAM_ROLE" \
    --assume-role-policy-document "$DEPLOY_TRUST" >/dev/null
  ok "created"
fi
# What the deploy workflow needs to be allowed:
#   1. find the caller EC2 by tag (DescribeInstances)
#   2. run bash on it via SSM (SendCommand / GetCommandInvocation)
# No SSH — SSM is the transport. Dashboard creds arrive as env vars
# baked into the SendCommand payload from GitHub Secrets, never persisted.
DEPLOY_POLICY=$(cat <<'POLICY'
{
  "Version": "2012-10-17",
  "Statement": [
    {"Sid":"FindCaller","Effect":"Allow","Action":[
      "ec2:DescribeInstances","ec2:DescribeSecurityGroups","ec2:DescribeTags"],
      "Resource":"*"},
    {"Sid":"SSMRunBashOnCaller","Effect":"Allow","Action":[
      "ssm:SendCommand","ssm:GetCommandInvocation","ssm:ListCommandInvocations"],
      "Resource":"*"}
  ]
}
POLICY
)
aws iam put-role-policy --role-name "$DEPLOY_IAM_ROLE" \
  --policy-name "$DEPLOY_IAM_ROLE" --policy-document "$DEPLOY_POLICY"
ok "inline policy updated"

# ── Summary ───────────────────────────────────────────────────────────────────
DEPLOY_ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${DEPLOY_IAM_ROLE}"
echo
echo "════════════════════════════════════════════════════════════════════════"
echo " AWS-side setup complete. Two things left to do manually:"
echo "════════════════════════════════════════════════════════════════════════"
echo
echo " (1) Launch the caller EC2 instance:"
echo "     Instance type:      t3.nano  (or t3.small if you want to run prep locally)"
echo "     AMI:                Ubuntu 24.04 LTS amd64 (or your custom tier-2 AMI)"
echo "     Keypair:            ${KEYPAIR_NAME}"
echo "     IAM instance profile: ${CALLER_IAM_ROLE}"
echo "     Security group:     ${SG_ID}"
echo "     Tag:                Name=acestor-caller  (deploy workflow finds it by this tag)"
echo
echo " (2) In GitHub → Settings → Secrets and variables → Actions,"
echo "     add these SECRETS (encrypted, deploy transport is SSM so no SSH key):"
echo
echo "       AWS_DEPLOY_ROLE_ARN         = ${DEPLOY_ROLE_ARN}"
echo "       DASHBOARD_URL               = https://apps.artpark.ai/disease-dashboard-staging"
echo "       DASHBOARD_CLIENT_ID         = admin@dengue.local"
echo "       DASHBOARD_CLIENT_SECRET     = <the password>"
echo
echo "     and these VARIABLES (plaintext):"
echo
echo "       AWS_REGION                  = ${REGION}"
echo "       CALLER_INSTANCE_TAG_NAME    = acestor-caller"
echo "       ACESTOR_AWS_KEY_NAME        = ${KEYPAIR_NAME}"
echo "       ACESTOR_AWS_AMI             = <ami-id if you baked a custom one; leave unset otherwise>"
echo
echo " Once both are done, push to '${GITHUB_BRANCH}' (or run 'deploy-caller' manually)."
echo "════════════════════════════════════════════════════════════════════════"
