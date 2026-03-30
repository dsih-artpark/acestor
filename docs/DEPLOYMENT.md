# Deployment Guide

How to deploy the acestor dengue pipeline in production — both Docker-only and on AWS EC2.

---

## Option A: Docker (local or any Linux server)

### 1. Build the image

```bash
docker build -t acestor .
```

### 2. Prepare your config and secrets

Copy your config (e.g. `configs/gba_stage1_s3.yaml`) into a directory the container can mount.
Create a `.env` file for secrets:

```env
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
AWS_REGION=ap-south-1
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_USER=...
SMTP_PASS=...
```

### 3. Run a single pipeline execution

```bash
docker run --rm \
  --env-file .env \
  -v $(pwd)/configs:/app/configs:ro \
  -v $(pwd)/artifacts:/app/artifacts \
  acestor \
  python -m acestor.run \
    --pipeline pipelines.gba_dengue.pipeline:build_pipeline \
    --config /app/configs/gba_stage1_s3.yaml \
    --run-id $(date +%Y%m%d)
```

### 4. Run on a schedule (APScheduler mode)

The container can run `scripts/run_schedules.py` to keep a long-lived scheduler process alive:

```bash
docker run -d \
  --name acestor-scheduler \
  --restart unless-stopped \
  --env-file .env \
  -v $(pwd)/configs:/app/configs:ro \
  -v $(pwd)/artifacts:/app/artifacts \
  -v $(pwd)/logs:/app/logs \
  acestor \
  python scripts/run_schedules.py
```

Logs per run are written to `logs/{pipeline-name}/{run-id}.log` inside the mounted volume.

---

## Option B: AWS EC2

### Recommended setup

| Component | Choice |
|---|---|
| Instance | `t3.medium` (2 vCPU, 4 GB RAM) — sufficient for district-level runs |
| OS | Amazon Linux 2023 or Ubuntu 22.04 LTS |
| Storage | 30 GB gp3 root volume (artifacts + logs) |
| IAM Role | Attach an instance role with S3 read/write access — no static keys needed |
| Security group | No inbound ports required (outbound HTTPS for S3 + SMTP only) |

### 1. Provision the instance

```bash
# Launch via AWS console or CLI, then SSH in
ssh -i your-key.pem ec2-user@<instance-ip>
```

### 2. Install Docker

```bash
# Amazon Linux 2023
sudo dnf install -y docker
sudo systemctl enable --now docker
sudo usermod -aG docker ec2-user
# Log out and back in for group to take effect
```

```bash
# Ubuntu 22.04
sudo apt update && sudo apt install -y docker.io
sudo systemctl enable --now docker
sudo usermod -aG docker ubuntu
```

### 3. Clone the repo and build

```bash
git clone https://github.com/your-org/acestor-v2.git
cd acestor-v2
docker build -t acestor .
```

### 4. Configure secrets

If using an IAM instance role for S3, you don't need `AWS_*` keys — the SDK picks them up automatically. Only add SMTP credentials:

```bash
cat > .env <<EOF
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_USER=alerts@example.com
SMTP_PASS=yourpassword
EOF
chmod 600 .env
```

### 5. Start the scheduler as a systemd service

Create `/etc/systemd/system/acestor.service`:

```ini
[Unit]
Description=Acestor Dengue Pipeline Scheduler
After=docker.service
Requires=docker.service

[Service]
Restart=on-failure
RestartSec=30
WorkingDirectory=/home/ec2-user/acestor-v2
ExecStart=docker run --rm \
  --name acestor-scheduler \
  --env-file /home/ec2-user/acestor-v2/.env \
  -v /home/ec2-user/acestor-v2/configs:/app/configs:ro \
  -v /home/ec2-user/acestor-v2/artifacts:/app/artifacts \
  -v /home/ec2-user/acestor-v2/logs:/app/logs \
  acestor python scripts/run_schedules.py
ExecStop=docker stop acestor-scheduler

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now acestor
sudo systemctl status acestor
```

### 6. Tail live logs

```bash
# Scheduler process log
sudo journalctl -u acestor -f

# Per-run pipeline logs
tail -f logs/gba-weekly/run-$(date +%Y%m%d)*.log
```

### 7. Artifact storage

By default artifacts are written to the local volume. For persistence across instance replacements, point the config to S3:

```yaml
storages:
  artifacts:
    kind: s3
    s3:
      bucket: your-bucket
      base_prefix: artifacts/
```

---

## Updating the pipeline

```bash
cd acestor-v2
git pull
docker build -t acestor .
sudo systemctl restart acestor
```

---

## Health check

There is no HTTP endpoint. Monitor via:
- `sudo systemctl status acestor` — confirms the scheduler process is running
- `ls -lt logs/gba-weekly/` — confirms runs are executing on schedule
- Email notifications (configure `email:` in the pipeline config) — confirms end-to-end success
