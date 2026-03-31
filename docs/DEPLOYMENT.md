# Deployment Guide

Three supported deployment scenarios:

| Scenario | Where to run | Files |
|---|---|---|
| [A — Docker (any server)](#option-a-docker-any-server) | Any Linux machine with Docker | `Dockerfile` |
| [B — Docker on AWS EC2](#option-b-docker-on-aws-ec2) | EC2 instance | `deployment/ec2/` |
| [C — Native on AWS EC2](#option-c-native-on-aws-ec2-no-docker) | EC2 instance | `deployment/ec2-native/` |

In all scenarios the pipeline runs as a **long-lived scheduler process** (`scripts/run_schedules.py` using APScheduler). The schedule is defined inside that file — edit it before deploying:

```python
# scripts/run_schedules.py
PIPELINES = [
    {
        "name":     "ap-weekly",
        "cron":     "30 0 * * 2",   # every Tuesday 00:30 UTC (06:00 IST)
        "pipeline": "pipelines.gba_dengue.pipeline:build_pipeline",
        "config":   "configs/ap_district.yaml",
    },
]
```

---

## Option A: Docker (any server)

### Prerequisites

- Docker installed on the host

### 1. Configure the schedule

Edit `scripts/run_schedules.py` — set the correct `cron` expression and `config` path for your pipeline.

### 2. Build the image

```bash
git clone https://github.com/your-org/acestor-v2.git
cd acestor-v2
docker build -t acestor:latest .
```

### 3. Configure secrets

```bash
cp deployment/ec2/env.example .env
chmod 600 .env
# Edit .env — fill in CONFIG_PATH, SMTP credentials, S3 bucket names if applicable
```

### 4. Start the scheduler

```bash
docker run -d \
  --name acestor-scheduler \
  --restart unless-stopped \
  --env-file .env \
  -v $(pwd)/configs:/app/configs:ro \
  -v $(pwd)/artifacts:/app/artifacts \
  -v $(pwd)/logs:/app/logs \
  acestor:latest \
  uv run python scripts/run_schedules.py
```

The container restarts automatically on crash or reboot (`--restart unless-stopped`).

### 5. Monitor

```bash
docker logs -f acestor-scheduler
ls -lt logs/
```

---

## Option B: Docker on AWS EC2

### Recommended instance

| Component | Choice |
|---|---|
| Instance | `t3.medium` (2 vCPU, 4 GB RAM) |
| OS | Amazon Linux 2023 or Ubuntu 22.04 LTS |
| Storage | 30 GB gp3 root volume |
| IAM Role | Instance role with S3 read/write — no static keys needed |
| Security group | Outbound HTTPS only (S3 + SMTP) |

### 1. Provision and SSH in

```bash
ssh -i your-key.pem ec2-user@<instance-ip>
```

### 2. Clone the repo and install Docker

```bash
git clone https://github.com/your-org/acestor-v2.git /opt/acestor
cd /opt/acestor
sudo bash deployment/ec2/setup.sh
# Log out and back in for docker group membership to take effect
```

### 3. Configure the schedule

Edit `scripts/run_schedules.py` with your pipeline's cron expression and config path.

### 4. Build the image

```bash
cd /opt/acestor
docker build -t acestor:latest .
```

### 5. Configure secrets

```bash
cp deployment/ec2/env.example .env
chmod 600 .env
# Edit .env — set CONFIG_PATH and SMTP credentials
# AWS credentials not needed if using IAM instance role
```

### 6. Test a manual start

```bash
bash deployment/ec2/deploy.sh
docker logs -f acestor-scheduler
```

### 7. Manage with systemd (recommended for production)

systemd will restart the scheduler container on crash or EC2 reboot:

```bash
sudo cp deployment/ec2/acestor.service /etc/systemd/system/
# If repo is not at /opt/acestor, edit the paths in the service file first
sudo systemctl daemon-reload
sudo systemctl enable --now acestor
sudo systemctl status acestor
```

### 8. Monitor

```bash
# Scheduler process
sudo journalctl -u acestor -f

# Per-run logs written by run_schedules.py
ls -lt /opt/acestor/logs/
```

### 9. Update the pipeline

```bash
cd /opt/acestor
git pull
docker build -t acestor:latest .
sudo systemctl restart acestor
```

---

## Option C: Native on AWS EC2 (no Docker)

Use this when Docker is unavailable or you prefer a direct Python environment.

### 1. Provision and SSH in

```bash
ssh -i your-key.pem ec2-user@<instance-ip>
```

### 2. Clone the repo

```bash
git clone https://github.com/your-org/acestor-v2.git /opt/acestor
cd /opt/acestor
```

### 3. Install all dependencies

```bash
sudo bash deployment/ec2-native/setup.sh
```

This installs on the host:

| Dependency | Purpose |
|---|---|
| Python 3.12 | Runtime |
| [uv](https://github.com/astral-sh/uv) | Python env management |
| GDAL, GEOS, PROJ, spatialindex | Geospatial processing (geopandas) |
| texlive (latex-extra) | PDF report generation |
| All Python packages | Via `uv sync` from `uv.lock` |

### 4. Configure the schedule

Edit `scripts/run_schedules.py` with your cron expression and config path.

### 5. Configure secrets

```bash
cp deployment/ec2-native/env.example .env
chmod 600 .env
# Edit .env
```

### 6. Test a manual start

```bash
bash deployment/ec2-native/run.sh
# Press Ctrl+C to stop
```

### 7. Manage with systemd

```bash
# If the deploy user is not ec2-user, edit User= in acestor.service first
sudo cp deployment/ec2-native/acestor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now acestor
sudo systemctl status acestor
```

### 8. Monitor

```bash
sudo journalctl -u acestor -f
ls -lt /opt/acestor/logs/
```

### 9. Update the pipeline

```bash
cd /opt/acestor
git pull
~/.local/bin/uv sync --frozen --extra dengue --extra cds --extra s3
sudo systemctl restart acestor
```

---

## Artifact storage on S3

For persistence across instance replacements, configure S3 artifact storage in your pipeline YAML:

```yaml
storages:
  artifacts:
    kind: s3
    s3:
      bucket: your-bucket
      base_prefix: acestor/artifacts/
```

With an IAM instance role attached to the EC2 instance, no credentials are needed in `.env`.

---

## Health check

There is no HTTP endpoint. Monitor via:

- `sudo systemctl status acestor` — confirms the scheduler process is running
- `sudo journalctl -u acestor -n 50` — last 50 log lines
- `ls -lt logs/` — confirms runs are executing on schedule
- Email notifications — configure `email:` in your pipeline YAML for end-to-end confirmation
