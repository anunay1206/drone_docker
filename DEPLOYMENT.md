# Tree-Crown Workstation Deployment

This repo does not include detector model weights. Keep the `.pth` files in a
separate folder on the workstation and point Docker Compose to that folder.

## 1. Install Docker

Ubuntu/Debian:

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl gnupg
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker "$USER"
```

Log out and back in after adding your user to the `docker` group.

## 2. Prepare Models

Create or choose a model folder outside the repo:

```bash
mkdir -p /opt/treecrown-models
```

Place these files there:

```text
urban_trees_Cambridge_20230630.pth
220723_withParacouUAV.pth
230103_randresize_full.pth
```

The model filenames are configured in `code/models.yaml`. If your filenames are
different, edit `code/models.yaml`; do not move weights into the Git repo.

## 3. Configure

```bash
cp .env.example .env
```

Edit `.env`:

```text
HOST_MODELS_DIR=/opt/treecrown-models
TCP_MODELS_DIR=/models
TCP_MODELS_MANIFEST=/code/models.yaml
TCP_STORAGE_ROOT=/data/storage
TCP_DATABASE_URL=sqlite:////data/treecrown.db
```

If Airflow is not being used yet, leave this blank:

```text
TCP_AIRFLOW_BASE_URL=
```

## 4. Run From Source

```bash
docker compose build
docker compose up -d
docker compose logs -f api
```

Open:

```text
Frontend: http://localhost:8200
API docs: http://localhost:8123/docs
Health:   http://localhost:8123/livez
```

## 5. Run With Prebuilt Images

Set image names in `.env`:

```text
IMAGE_API=uavforaliens/treecrown-workstation:latest
IMAGE_FRONTEND=uavforaliens/treecrown-frontend:latest
HOST_MODELS_DIR=/opt/treecrown-models
```

Then:

```bash
docker compose -f docker-compose.hub.yml pull
docker compose -f docker-compose.hub.yml up -d
```

## 6. Verify Model Mount

```bash
docker compose exec api ls -lh /models
curl http://localhost:8123/api/v1/detectors
```

Each detector should show:

```json
"available": true
```

If `available` is false, check `HOST_MODELS_DIR` and `code/models.yaml`.
