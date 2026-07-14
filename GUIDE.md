# Tree-Crown — Run Guide (CPU & GPU)

How to run the Tree-Crown backend on your machine, either CPU-only (default,
works anywhere) or GPU-accelerated (NVIDIA only, much faster inference).

The Docker **image** ships the app + Python deps. It does **not** ship the host
NVIDIA driver or the container GPU bridge — each machine sets those up once
(GPU section below).

---

## 0. Prerequisites (everyone)

- Docker Engine + Docker Compose v2 (`docker compose`, not the old `docker-compose`).
- This repo cloned locally.
- A `.env` file. Copy the template and edit the machine-specific paths:
  ```bash
  cp env .env
  ```
  Set at minimum:
  - `HOST_MODELS_DIR` — absolute path to the detector-weights folder on **this** machine.
  - `IMAGE_API` / `IMAGE_FRONTEND` — leave defaults unless told otherwise.

Ports once running: API `http://localhost:8123`, frontend `http://localhost:8200`,
filebrowser `http://localhost:8097`.

---

## 1. CPU run (default, any machine)

No GPU, no driver, no toolkit needed. Pulls prebuilt images from Docker Hub.

```bash
docker compose -f docker-compose.hub.yml pull
docker compose -f docker-compose.hub.yml up -d
```

Verify it's up:
```bash
docker compose -f docker-compose.hub.yml ps
curl http://localhost:8123/livez
```

The pipeline will log `Device: cpu`. That's expected here.

---

## 2. GPU run (NVIDIA only)

Faster inference. Requires an NVIDIA GPU + three host steps, then a GPU image.

### 2a. Host GPU setup (once per machine)

**Step 1 — NVIDIA driver.** Must be recent enough for CUDA 12.8
(Linux driver >= 525). Check:
```bash
nvidia-smi
```
A GPU table = good. No table = install/upgrade the NVIDIA driver first.

**Step 2 — NVIDIA Container Toolkit** (lets Docker see the GPU):
```bash
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | \
  sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | \
  sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
  sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
sudo apt-get update && sudo apt-get install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker      # REQUIRED — without this the config isn't live
```

**Step 3 — confirm Docker sees the GPU:**
```bash
docker run --rm --gpus all nvidia/cuda:12.4.0-base-ubuntu22.04 nvidia-smi
```
Prints a GPU table = ready.

> WSL2 note: use a recent Windows NVIDIA driver, then run steps 2–3 inside the
> WSL distro. If step 3 still fails, enable your distro under
> Docker Desktop → Settings → Resources → WSL Integration.

### 2b. Get the GPU image + run

In `.env` set:
```
IMAGE_API=uavforaliens/treecrown-workstation:cu128
```

Then pull and start:
```bash
docker compose -f docker-compose.hub.yml pull
docker compose -f docker-compose.hub.yml up -d
```

### 2c. Build the GPU image yourself (instead of pulling)

Only if you can't pull the `cu128` image. Set `TORCH_INDEX=https://download.pytorch.org/whl/cu128` in `.env`, then:
```bash
docker compose build --no-cache api
docker compose up -d
```

### 2d. Verify GPU is actually used

```bash
docker compose -f docker-compose.hub.yml exec api python -c \
  "import torch; print(torch.version.cuda, torch.cuda.is_available(), torch.cuda.get_device_name(0))"
```
Want: `12.8 True <your-gpu-name>`.

Live proof during a run:
```bash
watch -n1 nvidia-smi
```
GPU-Util jumps above 0% = GPU crunching. Pipeline log also shows `Device: cuda`:
```bash
docker compose -f docker-compose.hub.yml logs -f api | grep -i device
```

---

## 3. Which command am I running? (common trap)

- `docker compose up -d` — your **locally built** image (`treecrown-workstation:latest`).
- `docker compose -f docker-compose.hub.yml up -d` — **pulled** image (`IMAGE_API` from `.env`).

Check the running image:
```bash
docker ps --format '{{.Image}}'
```
A `:cu128` tag = GPU. A `:latest` tag = CPU.

---

## 4. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `could not select device driver "nvidia"` | Toolkit missing or Docker not restarted | Do 2a step 2, incl. `systemctl restart docker` |
| `torch.cuda.is_available()` = `False` on GPU box | Running a CPU image | Pull/run the `cu128` image or build it (2c) |
| `CUDA driver version is insufficient` | Host driver too old for CUDA 12.8 | Update NVIDIA driver (>= 525) |
| CPU box, don't want GPU | — | Use section 1; GPU block is ignored without a GPU |

---

## 5. Stop / restart

```bash
docker compose -f docker-compose.hub.yml down
docker compose -f docker-compose.hub.yml up -d
```
