# Deployment

See **[docs/DEPLOYMENT.md](../docs/DEPLOYMENT.md)** for the full deployment guide covering:

- **Option A** — Docker on any Linux server
- **Option B** — Docker on AWS EC2 (`deployment/ec2/`)
- **Option C** — Native Python on AWS EC2, no Docker (`deployment/ec2-native/`)

## Quick reference

| File | Purpose |
|---|---|
| `ec2/setup.sh` | Install Docker on EC2 (AL2023 or Ubuntu) |
| `ec2/deploy.sh` | Build image + start scheduler container |
| `ec2/acestor.service` | systemd service — keeps scheduler container alive |
| `ec2/env.example` | Environment variable template |
| `ec2-native/setup.sh` | Install Python, GDAL, texlive, uv natively |
| `ec2-native/run.sh` | Start scheduler natively (no Docker) |
| `ec2-native/acestor.service` | systemd service — keeps scheduler process alive |
| `ec2-native/env.example` | Environment variable template |
