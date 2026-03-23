# Deployment

This project keeps deployment simple with two options:

1. Docker (local / any VM)
2. EC2 (Docker-based)

## Docker deployment

Build image:

```bash
docker build -t acestor:latest .
```

Run pipeline:

```bash
docker run --rm \
  -v "$(pwd)/configs:/app/configs" \
  -v "$(pwd)/geojsons:/app/geojsons" \
  -v "$(pwd)/artifacts:/app/artifacts" \
  --env-file .env \
  acestor:latest \
  --pipeline pipelines.gba_dengue.pipeline:build_pipeline \
  --config configs/dengue_s3_env.yaml \
  --run-id docker-run-001
```

## EC2 deployment (Docker)

Use files in `deployment/ec2/`:

- `env.example` - environment variables for runtime
- `deploy.sh` - build + run helper
- `acestor.service` - optional systemd service template

Typical flow on EC2:

```bash
cp deployment/ec2/env.example .env
# edit .env
bash deployment/ec2/deploy.sh
```
