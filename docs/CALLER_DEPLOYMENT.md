# acestor-caller — deployment runbook

The **caller** is a small EC2 (t3.nano/small) that runs the DAG scheduler
(`scripts/run_schedules_remote.py`) and, on each cron tick, uses
`acestor.remote` to spawn a bigger compute EC2, run a pipeline, sync
artifacts back, and terminate.

This runbook covers three tasks:

1. **Greenfield AWS setup** (one-time, per account)
2. **Launching the caller EC2** (one-time, manual)
3. **Deployment** (repeated, via GitHub Actions or manual)

Plus troubleshooting and teardown.

---

## 1. Greenfield AWS setup

Run once per AWS account. Sets up IAM roles, security group, keypair, and
the GitHub OIDC identity provider.

```bash
./scripts/bootstrap_infra.sh
```

Interactive — prompts for region, GitHub repo name, deploy branch, etc.
Idempotent — safe to re-run at any time; only creates what's missing.

**Creates:**

| Resource | Purpose |
|---|---|
| IAM role + instance profile `acestor-caller` | Attached to the caller EC2. Grants `ec2:RunInstances` / `Terminate` / `DescribeInstances` for spawning compute, `ssm:GetParameter` for AMI lookup, and `AmazonSSMManagedInstanceCore` so CI/CD can shell into the caller via SSM. |
| Security group `acestor-caller-sg` | Port 22 from your current IP (operator SSH for debugging only — GHA deploys use SSM, no SSH). |
| EC2 keypair `acestor-caller` | The PEM you use to SSH into the caller from your laptop, AND the PEM the caller itself uses to SSH into spawned compute instances. Saved locally to `~/.ssh/acestor-caller.pem`. |
| OIDC provider `token.actions.githubusercontent.com` | Lets GitHub Actions assume AWS roles without long-lived access keys. |
| IAM role `github-actions-deploy-caller` | Assumed by `.github/workflows/deploy-caller.yml` via OIDC. Scoped to this exact repo + the deploy branch. Grants `ec2:DescribeInstances` + `ssm:SendCommand`. |

**Prints at the end:** what to paste into GitHub Actions Secrets + Variables.

### Configuring GitHub

Repo → Settings → Secrets and variables → Actions.

**Secrets** (encrypted at rest, decrypted only in workflow runs):

| Secret | Value |
|---|---|
| `AWS_DEPLOY_ROLE_ARN` | ARN printed by bootstrap script |
| `DASHBOARD_URL` | e.g. `https://apps.artpark.ai/disease-dashboard-staging` |
| `DASHBOARD_CLIENT_ID` | e.g. `admin@dengue.local` |
| `DASHBOARD_CLIENT_SECRET` | the password |

**Variables** (plaintext):

| Variable | Value |
|---|---|
| `AWS_REGION` | e.g. `ap-south-1` |
| `CALLER_INSTANCE_TAG_NAME` | `acestor-caller` (must match the `Name` tag on the caller EC2) |
| `ACESTOR_AWS_KEY_NAME` | e.g. `acestor-caller` (the keypair name) |
| `ACESTOR_AWS_AMI` | (optional) custom AMI id if you baked one |

---

## 2. Launching the caller EC2

Deliberately manual — the caller is a one-per-account persistent box, not
something the CI/CD pipeline should create or replace autonomously.

**Config:**

| Field | Value |
|---|---|
| Instance type | `t3.nano` (orchestration only) or `t3.small` (if you want the option to run prep locally later) |
| AMI | Ubuntu 24.04 LTS amd64 (or the custom tier-2 AMI if baked) |
| Keypair | `acestor-caller` |
| IAM instance profile | `acestor-caller` |
| Security group | `acestor-caller-sg` |
| Storage | 30 GB gp3 (comfortable — repo + logs + prep artifacts) |
| Tags | `Name=acestor-caller` (required — deploy workflow finds it by this tag) |

**After launch — one-time setup on the caller:**

SSH in (from a machine with the PEM):

```bash
ssh -i ~/.ssh/acestor-caller.pem ubuntu@<caller-ip>
```

Add swap (only needed on t3.nano):

```bash
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

Place the compute-instance PEM (same keypair, used *by the caller* to SSH
into spawned compute EC2s):

```bash
# From your laptop:
scp -i ~/.ssh/acestor-caller.pem ~/.ssh/acestor-caller.pem \
    ubuntu@<caller-ip>:~/.ssh/acestor-caller.pem
ssh -i ~/.ssh/acestor-caller.pem ubuntu@<caller-ip> 'chmod 600 ~/.ssh/acestor-caller.pem'
```

That's it. **You don't install anything else** — the deploy workflow does
`apt install`, `uv sync`, git clone, systemd setup on first deploy.

---

## 3. Deployment

Two paths, both use the same script + same SSM transport under the hood.

### 3a. From GitHub Actions (recommended)

The workflow at `.github/workflows/deploy-caller.yml` fires on:

- push to `production` that touches `scripts/run_schedules_remote.py`,
  `scripts/deploy_caller.sh`, or the workflow itself
- manual `workflow_dispatch` (Actions tab → "Deploy acestor-caller" → Run)

The workflow:

1. Assumes `AWS_DEPLOY_ROLE_ARN` via OIDC (no long-lived keys).
2. Runs `scripts/deploy_caller.sh`, which uses `aws ssm send-command` to
   ship a bash payload to the caller EC2.
3. Polls the SSM invocation until Success / Failed / Cancelled / TimedOut.
4. Fails the job if the caller reported anything other than Success.

Concurrency guard: only one deploy runs at a time.

### 3b. From your laptop

Same script, ambient AWS creds (`aws sso login` first):

```bash
export AWS_REGION=ap-south-1
export CALLER_INSTANCE_TAG_NAME=acestor-caller
export DASHBOARD_URL=...
export DASHBOARD_CLIENT_ID=...
export DASHBOARD_CLIENT_SECRET=...
./scripts/deploy_caller.sh
```

### What the deploy does on the caller

`deploy_caller.sh` ships a self-contained bash payload that:

1. `apt install` git, python3.12, build tools, uv (idempotent)
2. `git clone`/`git pull` the acestor repo into `~/acestor-work`
3. `uv sync` scheduler deps into `~/acestor-work/.venv-remote`
4. Write `~/.env` with dashboard creds (chmod 600)
5. Install/update `/etc/systemd/system/acestor-scheduler.service`
6. `systemctl daemon-reload && systemctl enable --now acestor-scheduler`
7. Verify with `systemctl is-active acestor-scheduler`

The systemd unit:
- Runs as user `ubuntu`.
- WorkingDirectory `/home/ubuntu/acestor-work`.
- Restart=on-failure + RestartSec=30s.
- Logs to `/home/ubuntu/acestor-work/logs/scheduler.log`.

---

## Troubleshooting

**Deploy workflow fails at "Assume AWS role via OIDC"**  
Trust policy scope didn't match. Re-run `bootstrap_infra.sh` with the
correct branch name.

**Deploy fails "no running EC2 in <region> with tag Name=..."**  
Caller not launched yet, or `Name` tag doesn't match `CALLER_INSTANCE_TAG_NAME`
variable.

**Deploy hangs / TimedOut**  
Caller lost its SSM connection (SSM agent stopped or IAM policy detached).
On the caller: `sudo systemctl status amazon-ssm-agent`.

**Scheduler not running after deploy**  
On the caller: `sudo systemctl status acestor-scheduler` and
`tail /home/ubuntu/acestor-work/logs/scheduler.log`.

**Dashboard auth fails at pipeline runtime**  
`~/.env` was written but creds are wrong. Update the GitHub Secret, re-run
the deploy workflow.

---

## Teardown

If you need to fully remove the acestor-caller stack from an account:

```bash
# Terminate the caller EC2
aws ec2 terminate-instances --instance-ids <caller-instance-id>

# Delete IAM roles + policies (in this order)
aws iam remove-role-from-instance-profile --instance-profile-name acestor-caller --role-name acestor-caller
aws iam delete-instance-profile --instance-profile-name acestor-caller
aws iam delete-role-policy --role-name acestor-caller --policy-name acestor-caller
aws iam detach-role-policy --role-name acestor-caller --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore
aws iam delete-role --role-name acestor-caller

aws iam delete-role-policy --role-name github-actions-deploy-caller --policy-name github-actions-deploy-caller
aws iam delete-role --role-name github-actions-deploy-caller

# Security group + keypair
aws ec2 delete-security-group --group-name acestor-caller-sg
aws ec2 delete-key-pair --key-name acestor-caller

# OIDC provider — only if no other GitHub-Actions-using role in the account depends on it
aws iam delete-open-id-connect-provider --open-id-connect-provider-arn arn:aws:iam::<acct>:oidc-provider/token.actions.githubusercontent.com
```
