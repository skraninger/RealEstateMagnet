# RealEstateMagnet — Installation Guide

## Overview

RealEstateMagnet uses a **Docker devcontainer** as its primary development environment, ensuring every developer runs identical tooling regardless of host OS.

Three supported deployment paths are documented here:

| Path | Best For | Host OS | Effort |
|------|----------|---------|--------|
| [Path 1: Local PC](#path-1-local-pc-windows) | Daily development | Windows 11 | ~15 min |
| [Path 2: Proxmox VM](#path-2-proxmox-vm) | Persistent dev server, team access | Proxmox PVE 8.x | ~30 min |
| [Path 3: Proxmox LXC](#path-3-proxmox-lxc-advanced) | Lightweight always-on server | Proxmox PVE 8.x | ~20 min |

---

## What the devcontainer provides

When you open the project in VS Code and click **"Reopen in Container"**, Docker automatically builds and starts:

| Service | Purpose | Host Port |
|---------|---------|-----------|
| `app` | Python 3.12 dev container — your workspace | — |
| `db` | PostgreSQL 16 + PostGIS 3.4 | `5432` |
| `pgadmin` | pgAdmin 4 web UI | `5050` |

All Python dependencies, Playwright (Chromium), and VS Code extensions are pre-installed inside the container.

---

## Path 1: Local PC (Windows)

### Prerequisites

| Software | Version | Link |
|----------|---------|------|
| Docker Desktop | 4.x+ | [docker.com/products/docker-desktop](https://www.docker.com/products/docker-desktop/) |
| VS Code | Latest | [code.visualstudio.com](https://code.visualstudio.com/) |
| Git for Windows | Latest | [git-scm.com](https://git-scm.com/download/win) |

### Automated setup

Run the PowerShell script as Administrator to install all prerequisites via `winget`:

```powershell
# Open PowerShell as Administrator, then:
Set-ExecutionPolicy Bypass -Scope Process -Force
.\Installation\scripts\setup-local-windows.ps1
```

### Manual steps

1. **Install Docker Desktop**
   - Download and install from [docker.com](https://www.docker.com/products/docker-desktop/)
   - Enable **WSL 2 backend** during setup (recommended over Hyper-V)
   - Start Docker Desktop and verify: `docker --version`

2. **Install VS Code Dev Containers extension**
   ```
   code --install-extension ms-vscode-remote.remote-containers
   ```

3. **Clone the repository**
   ```bash
   git clone https://github.com/your-org/RealEstateMagnet.git
   cd RealEstateMagnet
   ```

4. **Copy environment file**
   ```bash
   cp .env.example .env
   # Edit .env if needed (defaults work for local dev)
   ```

5. **Open in devcontainer**
   - Open VS Code: `code .`
   - VS Code detects `.devcontainer/` and shows a popup: click **"Reopen in Container"**
   - Or press `Ctrl+Shift+P` → `Dev Containers: Reopen in Container`
   - First build takes ~5 minutes (downloads images, installs packages)

6. **Verify**
   ```bash
   # Inside the container terminal:
   python -m pytest tests/ -v
   python -m modules.discovery.source_discovery --help
   psql -h db -U rem -d realestate_magnet -c "SELECT PostGIS_Version();"
   ```

### Docker Desktop resource recommendations

For this project (PostGIS + Playwright), set in Docker Desktop → Settings → Resources:

| Resource | Minimum | Recommended |
|----------|---------|-------------|
| CPUs | 2 | 4 |
| Memory | 4 GB | 8 GB |
| Disk image | 20 GB | 60 GB |

---

## Path 2: Proxmox VM

This path creates an Ubuntu 24.04 LTS VM on Proxmox, installs Docker CE, and uses **VS Code SSH Remote** to attach to a devcontainer running on the VM.

### Architecture

```
Local PC (Windows)
  └─ VS Code SSH Remote ──→  Proxmox VM (Ubuntu 24.04)
                                └─ Docker
                                     ├─ app container  (Python 3.12)
                                     ├─ db  container  (PostGIS)
                                     └─ pgadmin        (pgAdmin 4)
```

### Step 1 — Create the VM on Proxmox

Run on your Proxmox host (SSH in first):

```bash
# Copy the script to Proxmox host and run it
scp Installation/scripts/setup-proxmox-vm.sh root@proxmox:/tmp/
ssh root@proxmox

# Edit variables at the top of the script, then:
bash /tmp/setup-proxmox-vm.sh
```

The script will:
- Download Ubuntu 24.04 cloud image (if not cached)
- Create a VM with your specified resources
- Configure cloud-init (SSH key, hostname, IP)
- Start the VM and wait for it to be reachable

See [`Installation/proxmox/README.md`](proxmox/README.md) for detailed Proxmox configuration options.

### Step 2 — Install Docker on the VM

SSH into the new VM and run:

```bash
bash /tmp/setup-ubuntu-docker.sh
```

Or run it directly after the VM creation script finishes (it will offer to do so).

### Step 3 — Configure VS Code SSH Remote

1. Install the **Remote - SSH** extension in VS Code:
   ```
   code --install-extension ms-vscode-remote.remote-ssh
   ```

2. Add your VM to `~/.ssh/config`:
   ```
   Host rem-dev
       HostName 192.168.1.XXX    # your VM IP
       User ubuntu
       IdentityFile ~/.ssh/id_ed25519
   ```

3. In VS Code: `Ctrl+Shift+P` → `Remote-SSH: Connect to Host` → `rem-dev`

4. Once connected to the VM, open `/home/ubuntu/RealEstateMagnet` and click **"Reopen in Container"** as normal.

### Step 4 — Port forwarding (optional)

VS Code SSH Remote automatically forwards container ports to your local machine. You can also set up a permanent SSH tunnel:

```bash
# On your local PC — forward Proxmox VM ports locally
ssh -L 8000:localhost:8000 -L 5050:localhost:5050 -L 5432:localhost:5432 -N rem-dev
```

---

## Path 3: Proxmox LXC (Advanced)

LXC containers are lighter than VMs (no hypervisor overhead) but require enabling Docker-in-LXC features.

### Step 1 — Create the LXC container

```bash
# On Proxmox host shell:
# Download Ubuntu template
pveam update
pveam download local ubuntu-24.04-standard_24.04-2_amd64.tar.zst

# Create the container (edit CTID and storage as needed)
pct create 201 local:vztmpl/ubuntu-24.04-standard_24.04-2_amd64.tar.zst \
    --hostname rem-lxc \
    --cores 4 \
    --memory 8192 \
    --swap 2048 \
    --rootfs local-lvm:50 \
    --net0 name=eth0,bridge=vmbr0,ip=dhcp \
    --unprivileged 1 \
    --features nesting=1,keyctl=1

# Enable nesting (required for Docker inside LXC)
# Edit /etc/pve/lxc/201.conf and add:
#   lxc.apparmor.profile: unconfined
#   lxc.cap.drop:

pct start 201
```

### Step 2 — Install Docker inside LXC

```bash
pct exec 201 -- bash -c "$(curl -fsSL https://raw.githubusercontent.com/docker/docker-install/master/install.sh)"
pct exec 201 -- bash /tmp/setup-ubuntu-docker.sh
```

> **Note:** Docker-in-LXC requires `nesting=1` and may require `unprivileged=0` on some Proxmox versions. See [`Installation/proxmox/README.md`](proxmox/README.md) for troubleshooting.

---

## Database initialization

The PostGIS container runs `Installation/scripts/init-database.sh` on first start. It:
- Enables `postgis`, `postgis_topology`, `uuid-ossp` extensions
- Creates `staging` and `raw` schemas
- Sets up the `rem` user with appropriate grants

To reset the database volume:
```bash
# WARNING: destroys all data
docker compose -f .devcontainer/docker-compose.yml down -v
docker compose -f .devcontainer/docker-compose.yml up db
```

---

## Running the application

All commands run **inside the devcontainer terminal**:

```bash
# Run test suite
python -m pytest tests/ -v

# Structured source discovery
python -m modules.discovery.source_discovery \
    --output data/catalogs/florida_sources.json

# Full discovery including web crawl
python -m modules.discovery.source_discovery \
    --include-unstructured \
    --output data/catalogs/florida_all.json

# Crawl a single URL
python -m modules.discovery.web_crawler \
    --url https://www.hoa-usa.com/florida/ \
    --depth 2 \
    --output data/catalogs/hoa_usa.json

# Start FastAPI dev server (Phase 5)
uvicorn modules.api.main:app --host 0.0.0.0 --port 8000 --reload
```

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| `Cannot connect to Docker daemon` | Docker Desktop not running | Start Docker Desktop |
| `port is already allocated` | Host port conflict | Stop conflicting service or change port in `docker-compose.yml` |
| `playwright install` fails in container | Missing system deps | Rebuild container: `Dev Containers: Rebuild Container` |
| `pg_isready` hangs | PostGIS container still starting | Wait 30s, then retry |
| Slow on Windows | Default Docker resource limits | Increase memory/CPU in Docker Desktop settings |
| SSH Remote devcontainer fails | VS Code server mismatch | `Remote-SSH: Kill VS Code Server on Host`, reconnect |
