# Proxmox VE — RealEstateMagnet Deployment Guide

This guide covers everything Proxmox-specific for running a RealEstateMagnet development or staging environment on Proxmox VE 8.x.

---

## Prerequisites

| Requirement | Notes |
|-------------|-------|
| Proxmox VE | 8.0 or later |
| Host RAM | ≥ 16 GB (VM will use 8 GB) |
| Host storage | ≥ 100 GB free on chosen pool |
| Internet access on host | For cloud image download |
| SSH key pair | Generated on your local PC |

Generate an SSH key on your local Windows PC if you don't have one:
```powershell
ssh-keygen -t ed25519 -C "rem-dev"
# Key saved to: C:\Users\<you>\.ssh\id_ed25519
```

---

## Storage pools

The script defaults to `local-lvm`.  Check what's available on your node:

```bash
# On the Proxmox host shell:
pvesm status
```

Common storage names:

| Name | Type | Notes |
|------|------|-------|
| `local-lvm` | LVM-Thin | Fast, good for VM disks |
| `local` | Directory | Stores ISOs/templates; use for cloud image |
| `ceph` / `rbd` | Ceph | High availability — if you have a cluster |
| `nfs-storage` | NFS | Shared storage, good for backups |

If your disk storage is `local`, change `VM_STORAGE="local"` in the script (use `raw` format instead of `qcow2`).

---

## Quick start

```bash
# 1. SSH into your Proxmox host
ssh root@proxmox.local

# 2. Upload or paste the script
nano /tmp/setup-proxmox-vm.sh
# (paste content from Installation/scripts/setup-proxmox-vm.sh)

# 3. Run with your settings
bash /tmp/setup-proxmox-vm.sh \
    --vmid    200            \
    --name    rem-dev        \
    --cores   4              \
    --memory  8192           \
    --disk    50             \
    --storage local-lvm      \
    --bridge  vmbr0          \
    --ip      192.168.1.50/24 \
    --gateway 192.168.1.1    \
    --sshkey  /tmp/id_ed25519.pub
```

To also automatically install Docker and the dev tools on the new VM:
```bash
bash /tmp/setup-proxmox-vm.sh ... --run-setup
```

---

## Network options

### DHCP (easiest)
```bash
bash setup-proxmox-vm.sh --vmid 200
# (omit --ip and --gateway; defaults to dhcp)
```

### Static IP
```bash
bash setup-proxmox-vm.sh \
    --ip      192.168.1.50/24 \
    --gateway 192.168.1.1
```

### VLAN-tagged port
Edit the `--net0` line in the script:
```bash
--net0 "virtio,bridge=vmbr0,tag=100,firewall=1"
```

---

## VM sizing guide

| Use case | vCPUs | RAM | Disk |
|----------|-------|-----|------|
| Single dev (light) | 2 | 4 GB | 30 GB |
| Single dev (recommended) | 4 | 8 GB | 50 GB |
| Team dev server | 8 | 16 GB | 100 GB |
| Staging (with real data) | 4 | 16 GB | 200 GB |

---

## After VM creation

### 1. First login
```bash
ssh ubuntu@192.168.1.50
# (use the IP shown by the script)
```

### 2. Run dev environment setup
```bash
# Upload the setup script
scp Installation/scripts/setup-ubuntu-docker.sh ubuntu@192.168.1.50:/tmp/

# Run on the VM
ssh ubuntu@192.168.1.50 "sudo bash /tmp/setup-ubuntu-docker.sh"
```

### 3. Clone the project
```bash
ssh ubuntu@192.168.1.50
git clone https://github.com/your-org/RealEstateMagnet.git ~/RealEstateMagnet
cp ~/RealEstateMagnet/.env.example ~/RealEstateMagnet/.env
# Edit .env — change DATABASE_URL host from 'db' to 'localhost'
```

### 4. Start the devcontainer stack
```bash
cd ~/RealEstateMagnet
docker compose -f .devcontainer/docker-compose.yml up -d
```

### 5. Connect via VS Code SSH Remote
On your local PC:
1. Add to `~/.ssh/config`:
   ```
   Host rem-dev
       HostName 192.168.1.50
       User ubuntu
       IdentityFile ~/.ssh/id_ed25519
   ```
2. VS Code: `Ctrl+Shift+P` → `Remote-SSH: Connect to Host` → `rem-dev`
3. Open folder `/home/ubuntu/RealEstateMagnet`
4. VS Code prompts: **"Reopen in Container"** — click it

---

## Proxmox VM management

```bash
# Start / stop
qm start 200
qm stop  200

# Graceful shutdown
qm shutdown 200

# Snapshot before risky changes
qm snapshot 200 clean-install --description "Before Python upgrade"
qm listsnapshot 200
qm rollback 200 clean-install

# Increase disk (cannot shrink)
qm resize 200 scsi0 +20G

# Check VM status
qm status 200
qm config 200

# Console (if SSH is unreachable)
qm terminal 200
# or use Proxmox web UI → VM → Console
```

---

## LXC alternative (advanced)

If you prefer LXC containers (lower overhead than VMs), Docker requires specific kernel features:

```bash
# On Proxmox host — create unprivileged LXC with nesting
pct create 201 local:vztmpl/ubuntu-24.04-standard_24.04-2_amd64.tar.zst \
    --hostname    rem-lxc         \
    --cores       4               \
    --memory      8192            \
    --swap        2048            \
    --rootfs      local-lvm:50   \
    --net0        name=eth0,bridge=vmbr0,ip=dhcp \
    --unprivileged 1              \
    --features    nesting=1,keyctl=1

# Required for Docker-in-LXC: add to /etc/pve/lxc/201.conf
echo "lxc.apparmor.profile: unconfined" >> /etc/pve/lxc/201.conf
echo "lxc.cap.drop:"                    >> /etc/pve/lxc/201.conf

pct start 201
pct exec 201 -- bash -c "apt-get update && apt-get install -y curl"
pct exec 201 -- bash -c "curl -fsSL https://get.docker.com | sh"
```

> **Compatibility note:** Docker-in-unprivileged-LXC works on most Proxmox 8.x hosts using the default kernel (6.x). If you encounter `cgroup` errors, switch to a privileged container (`--unprivileged 0`) or use a VM instead.

---

## Backup & restore

```bash
# Backup VM 200 to 'local' storage
vzdump 200 --storage local --mode snapshot --compress lzo

# List backups
ls /var/lib/vz/dump/

# Restore
qmrestore /var/lib/vz/dump/vzdump-qemu-200-*.vma.lzo 210 \
    --storage local-lvm
```

---

## Firewall (Proxmox datacenter level)

If you enable the Proxmox firewall, open these ports on the VM:

| Port | Protocol | Purpose |
|------|----------|---------|
| 22 | TCP | SSH |
| 8000 | TCP | FastAPI dev server |
| 5050 | TCP | pgAdmin 4 |

Do **not** open `5432` externally — access PostgreSQL via SSH tunnel instead:
```bash
ssh -L 5432:localhost:5432 ubuntu@192.168.1.50
```

---

## Troubleshooting

### Cloud-init doesn't apply SSH key
- Confirm the key file path is correct in `--sshkey`
- After VM starts: `qm cloudinit dump 200 user` to verify cloud-init config
- Re-generate: `qm cloudinit update 200 && qm start 200`

### VM stuck at boot
- Open console in Proxmox web UI: `VM → Console`
- Check if EFI boot order is correct: `qm set 200 --boot order=scsi0`

### Docker fails in LXC (`cgroup: ... permission denied`)
- Enable privileged mode: edit `/etc/pve/lxc/201.conf`, set `unprivileged: 0`
- Or use `overlay` storage driver: `echo '{"storage-driver":"overlay2"}' > /etc/docker/daemon.json`

### Devcontainer build fails on VM (disk full)
```bash
docker system prune -af   # removes unused images/containers
df -h                     # check remaining space
qm resize 200 scsi0 +20G  # expand from Proxmox host
# Then inside VM:
sudo growpart /dev/sda 3
sudo resize2fs /dev/sda3
```
