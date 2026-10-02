#!/usr/bin/env bash
# =============================================================================
# RealEstateMagnet — Proxmox VE 8.x: Create Ubuntu 24.04 Dev VM
# =============================================================================
# Run this script on the Proxmox host shell (or via SSH as root).
#
# Usage:
#   bash setup-proxmox-vm.sh [options]
#
# Options (or edit the CONFIGURATION section below):
#   --vmid       VM ID          (default: 200)
#   --name       VM hostname    (default: rem-dev)
#   --ip         Static IP/CIDR (default: dhcp)
#   --gateway    Gateway IP     (required for static IP)
#   --cores      vCPU count     (default: 4)
#   --memory     RAM in MB      (default: 8192)
#   --disk       Disk in GB     (default: 50)
#   --storage    Proxmox storage (default: local-lvm)
#   --bridge     Network bridge (default: vmbr0)
#   --sshkey     SSH public key file (default: ~/.ssh/id_ed25519.pub)
#   --run-setup  Also SSH in and run setup-ubuntu-docker.sh
#
# Example (static IP):
#   bash setup-proxmox-vm.sh --vmid 200 --ip 192.168.1.50/24 --gateway 192.168.1.1
#
# Example (DHCP):
#   bash setup-proxmox-vm.sh --vmid 200
# =============================================================================

set -euo pipefail

# ── Colour helpers ────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; NC='\033[0m'
step() { echo -e "\n${CYAN}▶  $*${NC}"; }
ok()   { echo -e "   ${GREEN}✓  $*${NC}"; }
warn() { echo -e "   ${YELLOW}⚠  $*${NC}"; }
fail() { echo -e "   ${RED}✗  $*${NC}"; exit 1; }

# ── CONFIGURATION (edit here or use CLI flags) ─────────────────────────────
VMID=200
VM_NAME="rem-dev"
VM_CORES=4
VM_MEMORY=8192           # MB
VM_DISK_GB=50
VM_STORAGE="local-lvm"   # Proxmox storage pool
VM_BRIDGE="vmbr0"
VM_IP="dhcp"             # e.g. "192.168.1.50/24" for static
VM_GATEWAY=""            # e.g. "192.168.1.1" — only needed for static IP
VM_DNS="8.8.8.8,1.1.1.1"
VM_SSHKEY="${HOME}/.ssh/id_ed25519.pub"
VM_USER="ubuntu"
RUN_SETUP=false

# Cloud image source (Ubuntu 24.04 LTS)
CLOUD_IMAGE_URL="https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img"
CLOUD_IMAGE_FILE="/var/lib/vz/template/iso/noble-server-cloudimg-amd64.img"

# ── Parse CLI arguments ────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case $1 in
        --vmid)      VMID="$2";       shift 2 ;;
        --name)      VM_NAME="$2";    shift 2 ;;
        --ip)        VM_IP="$2";      shift 2 ;;
        --gateway)   VM_GATEWAY="$2"; shift 2 ;;
        --cores)     VM_CORES="$2";   shift 2 ;;
        --memory)    VM_MEMORY="$2";  shift 2 ;;
        --disk)      VM_DISK_GB="$2"; shift 2 ;;
        --storage)   VM_STORAGE="$2"; shift 2 ;;
        --bridge)    VM_BRIDGE="$2";  shift 2 ;;
        --sshkey)    VM_SSHKEY="$2";  shift 2 ;;
        --run-setup) RUN_SETUP=true;  shift   ;;
        *) warn "Unknown option: $1"; shift ;;
    esac
done

# ── Sanity checks ─────────────────────────────────────────────────────────────
[[ "$EUID" -ne 0 ]] && fail "Must run as root on the Proxmox host."
command -v qm   &>/dev/null || fail "'qm' not found. Is this a Proxmox host?"
command -v pvesh &>/dev/null || fail "'pvesh' not found."

# Check VMID not already in use
if qm status "$VMID" &>/dev/null; then
    fail "VM $VMID already exists. Choose a different --vmid."
fi

# SSH key
if [[ ! -f "$VM_SSHKEY" ]]; then
    warn "SSH public key not found at $VM_SSHKEY"
    echo "  Generate one with:  ssh-keygen -t ed25519 -C 'rem-dev'"
    read -rp "  Enter path to your SSH public key: " VM_SSHKEY
fi
SSH_KEY_CONTENT="$(cat "$VM_SSHKEY")"

echo ""
echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${CYAN}  Creating VM $VMID ($VM_NAME)${NC}"
echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo "  vCPUs   : $VM_CORES"
echo "  RAM     : ${VM_MEMORY} MB"
echo "  Disk    : ${VM_DISK_GB} GB on $VM_STORAGE"
echo "  Network : $VM_BRIDGE  IP: $VM_IP"
echo "  SSH key : $VM_SSHKEY"
echo ""
read -rp "Proceed? [y/N] " confirm
[[ "$confirm" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 0; }

# ── 1. Download Ubuntu cloud image ─────────────────────────────────────────
step "Checking Ubuntu 24.04 cloud image"
if [[ -f "$CLOUD_IMAGE_FILE" ]]; then
    ok "Cloud image already cached: $CLOUD_IMAGE_FILE"
else
    echo "   Downloading from $CLOUD_IMAGE_URL ..."
    wget -q --show-progress -O "$CLOUD_IMAGE_FILE" "$CLOUD_IMAGE_URL"
    ok "Downloaded: $CLOUD_IMAGE_FILE"
fi

# ── 2. Create VM ─────────────────────────────────────────────────────────────
step "Creating VM $VMID"
qm create "$VMID" \
    --name       "$VM_NAME" \
    --cores      "$VM_CORES" \
    --memory     "$VM_MEMORY" \
    --machine    q35 \
    --bios       ovmf \
    --cpu        host \
    --numa       0 \
    --ostype     l26 \
    --scsihw     virtio-scsi-pci \
    --agent      1 \
    --serial0    socket \
    --vga        serial0 \
    --net0       "virtio,bridge=${VM_BRIDGE},firewall=1"

ok "VM $VMID created"

# ── 3. Import cloud image as primary disk ────────────────────────────────────
step "Importing cloud image as disk"
qm importdisk "$VMID" "$CLOUD_IMAGE_FILE" "$VM_STORAGE" --format qcow2 >/dev/null
qm set "$VMID" \
    --scsi0  "${VM_STORAGE}:vm-${VMID}-disk-0,discard=on,ssd=1" \
    --boot   "c;order=scsi0" \
    --efidisk0 "${VM_STORAGE}:0,efitype=4m,pre-enrolled-keys=1"

ok "Primary disk attached"

# ── 4. Resize disk ────────────────────────────────────────────────────────────
step "Resizing disk to ${VM_DISK_GB}G"
qm resize "$VMID" scsi0 "${VM_DISK_GB}G"
ok "Disk resized"

# ── 5. Cloud-init drive ───────────────────────────────────────────────────────
step "Configuring cloud-init"
qm set "$VMID" --ide2 "${VM_STORAGE}:cloudinit"

# Network config
if [[ "$VM_IP" == "dhcp" ]]; then
    qm set "$VMID" --ipconfig0 "ip=dhcp"
else
    IPCONFIG="ip=${VM_IP}"
    [[ -n "$VM_GATEWAY" ]] && IPCONFIG="${IPCONFIG},gw=${VM_GATEWAY}"
    qm set "$VMID" --ipconfig0 "$IPCONFIG"
fi

# User / SSH key
qm set "$VMID" \
    --ciuser     "$VM_USER" \
    --sshkeys    <(echo "$SSH_KEY_CONTENT") \
    --nameserver "$VM_DNS" \
    --ciupgrade  1

ok "Cloud-init configured (user: $VM_USER)"

# ── 6. Start VM ───────────────────────────────────────────────────────────────
step "Starting VM $VMID"
qm start "$VMID"
ok "VM started"

# ── 7. Wait for IP / SSH ──────────────────────────────────────────────────────
step "Waiting for VM to become reachable (up to 120s)"
VM_IP_RESOLVED=""
for i in $(seq 1 24); do
    sleep 5
    VM_IP_RESOLVED=$(qm guest cmd "$VMID" network-get-interfaces 2>/dev/null \
        | python3 -c "
import sys, json
ifaces = json.load(sys.stdin)
for iface in ifaces:
    if iface.get('name') != 'lo':
        for addr in iface.get('ip-addresses', []):
            if addr.get('ip-address-type') == 'ipv4':
                print(addr['ip-address'])
                sys.exit()
" 2>/dev/null || true)

    if [[ -n "$VM_IP_RESOLVED" ]]; then
        ok "VM IP: $VM_IP_RESOLVED"
        break
    fi
    echo -n "   . "
done

if [[ -z "$VM_IP_RESOLVED" ]]; then
    warn "Could not auto-detect VM IP. Check Proxmox UI for the assigned address."
    VM_IP_RESOLVED="<VM_IP>"
fi

# ── 8. Optional: run dev setup on VM ─────────────────────────────────────────
if [[ "$RUN_SETUP" == "true" ]] && [[ "$VM_IP_RESOLVED" != "<VM_IP>" ]]; then
    step "Running setup-ubuntu-docker.sh on the VM"
    SETUP_SCRIPT="$(dirname "$0")/setup-ubuntu-docker.sh"
    if [[ -f "$SETUP_SCRIPT" ]]; then
        # Wait a bit more for SSH to be ready
        sleep 15
        scp -o StrictHostKeyChecking=no "$SETUP_SCRIPT" \
            "${VM_USER}@${VM_IP_RESOLVED}:/tmp/setup-ubuntu-docker.sh"
        ssh -o StrictHostKeyChecking=no \
            "${VM_USER}@${VM_IP_RESOLVED}" \
            "sudo bash /tmp/setup-ubuntu-docker.sh"
        ok "Dev environment setup complete on VM"
    else
        warn "setup-ubuntu-docker.sh not found — skipping remote setup"
    fi
fi

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${CYAN}  VM $VMID ($VM_NAME) is ready!${NC}"
echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo "  VM IP   : $VM_IP_RESOLVED"
echo "  SSH     : ssh ${VM_USER}@${VM_IP_RESOLVED}"
echo ""
echo "  Next steps:"
echo "  1. SSH into the VM and run the dev setup:"
echo "       ssh ${VM_USER}@${VM_IP_RESOLVED}"
echo "       sudo bash /tmp/setup-ubuntu-docker.sh"
echo ""
echo "  2. Add to your local ~/.ssh/config:"
echo "       Host rem-dev"
echo "           HostName ${VM_IP_RESOLVED}"
echo "           User ${VM_USER}"
echo "           IdentityFile ${VM_SSHKEY%.pub}"
echo ""
echo "  3. In VS Code:"
echo "       Ctrl+Shift+P → Remote-SSH: Connect to Host → rem-dev"
echo "       Then open /home/${VM_USER}/RealEstateMagnet and reopen in container"
echo ""
