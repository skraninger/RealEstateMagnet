#!/usr/bin/env bash
# =============================================================================
# RealEstateMagnet — Ubuntu 24.04 LTS Development Environment Setup
# =============================================================================
# Installs Docker CE, Python 3.12, Node.js LTS, and configures the dev env.
#
# Usage:
#   # As root or with sudo:
#   bash setup-ubuntu-docker.sh
#
#   # Optionally clone & open the project at the end:
#   REPO_URL=https://github.com/your-org/RealEstateMagnet.git \
#   bash setup-ubuntu-docker.sh
#
# Tested on: Ubuntu 24.04 LTS (Noble Numbat)
# =============================================================================

set -euo pipefail

# ── Colour helpers ────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; NC='\033[0m'
step()  { echo -e "\n${CYAN}▶  $*${NC}"; }
ok()    { echo -e "   ${GREEN}✓  $*${NC}"; }
warn()  { echo -e "   ${YELLOW}⚠  $*${NC}"; }
fail()  { echo -e "   ${RED}✗  $*${NC}"; exit 1; }

# ── Variables (override via environment) ─────────────────────────────────────
INSTALL_USER="${SUDO_USER:-ubuntu}"           # non-root user to configure
REPO_URL="${REPO_URL:-}"                      # set to clone the project
PROJECT_DIR="${PROJECT_DIR:-/home/${INSTALL_USER}/RealEstateMagnet}"
NODE_MAJOR=20                                 # Node.js LTS major version

# ── Must be root ──────────────────────────────────────────────────────────────
if [[ "$EUID" -ne 0 ]]; then
    fail "Run as root or with sudo:  sudo bash $0"
fi

# ── 1. System update ──────────────────────────────────────────────────────────
step "Updating system packages"
apt-get update -qq
apt-get upgrade -y -qq
ok "System packages updated"

# ── 2. Essential tools ────────────────────────────────────────────────────────
step "Installing essential tools"
apt-get install -y -qq \
    git curl wget ca-certificates gnupg lsb-release \
    build-essential libpq-dev \
    postgresql-client \
    htop unzip jq
ok "Essential tools installed"

# ── 3. Docker CE ──────────────────────────────────────────────────────────────
step "Installing Docker CE"
if command -v docker &>/dev/null; then
    ok "Docker already installed: $(docker --version)"
else
    # Add Docker's official GPG key & repository
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
        | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    chmod a+r /etc/apt/keyrings/docker.gpg

    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" \
        > /etc/apt/sources.list.d/docker.list

    apt-get update -qq
    apt-get install -y -qq docker-ce docker-ce-cli containerd.io \
        docker-buildx-plugin docker-compose-plugin

    systemctl enable docker
    systemctl start docker
    ok "Docker CE installed: $(docker --version)"
fi

# Add install user to docker group so sudo isn't needed for docker commands
if id "$INSTALL_USER" &>/dev/null; then
    usermod -aG docker "$INSTALL_USER"
    ok "User $INSTALL_USER added to 'docker' group (re-login required)"
fi

# ── 4. Python 3.12 ────────────────────────────────────────────────────────────
step "Verifying Python 3.12"
# Ubuntu 24.04 ships Python 3.12 by default
if python3.12 --version &>/dev/null; then
    ok "Python $(python3.12 --version)"
else
    apt-get install -y -qq python3.12 python3.12-venv python3.12-dev python3-pip
    ok "Python 3.12 installed"
fi

# Ensure pip is up to date
python3.12 -m pip install --upgrade pip --quiet
ok "pip upgraded: $(python3.12 -m pip --version)"

# ── 5. Node.js LTS ────────────────────────────────────────────────────────────
step "Installing Node.js $NODE_MAJOR LTS (required by Playwright)"
if node --version 2>/dev/null | grep -q "v${NODE_MAJOR}"; then
    ok "Node.js $(node --version) already installed"
else
    curl -fsSL https://deb.nodesource.com/setup_${NODE_MAJOR}.x | bash -
    apt-get install -y -qq nodejs
    ok "Node.js $(node --version) installed"
fi

# ── 6. Clone project (optional) ───────────────────────────────────────────────
if [[ -n "$REPO_URL" ]]; then
    step "Cloning RealEstateMagnet"
    if [[ -d "$PROJECT_DIR/.git" ]]; then
        ok "Repository already exists at $PROJECT_DIR"
        git -C "$PROJECT_DIR" pull
    else
        sudo -u "$INSTALL_USER" git clone "$REPO_URL" "$PROJECT_DIR"
        ok "Cloned to $PROJECT_DIR"
    fi
fi

# ── 7. Python dependencies ────────────────────────────────────────────────────
if [[ -f "$PROJECT_DIR/requirements.txt" ]]; then
    step "Installing Python dependencies"
    sudo -u "$INSTALL_USER" python3.12 -m pip install \
        -r "$PROJECT_DIR/requirements.txt" --quiet
    ok "Python dependencies installed"

    step "Installing Playwright Chromium"
    sudo -u "$INSTALL_USER" python3.12 -m playwright install chromium --with-deps
    ok "Playwright Chromium installed"
fi

# ── 8. Environment file ───────────────────────────────────────────────────────
if [[ -f "$PROJECT_DIR/.env.example" ]] && [[ ! -f "$PROJECT_DIR/.env" ]]; then
    step "Creating .env from template"
    cp "$PROJECT_DIR/.env.example" "$PROJECT_DIR/.env"
    chown "$INSTALL_USER:$INSTALL_USER" "$PROJECT_DIR/.env"
    # On a VM, db is localhost (not the Docker service name "db")
    sed -i 's|@db:|@localhost:|g' "$PROJECT_DIR/.env"
    ok ".env created (DATABASE_URL updated to use localhost)"
fi

# ── 9. UFW firewall rules (if active) ────────────────────────────────────────
if command -v ufw &>/dev/null && ufw status | grep -q "Status: active"; then
    step "Configuring UFW firewall"
    ufw allow 22/tcp  comment 'SSH'    quiet
    ufw allow 8000/tcp comment 'FastAPI' quiet
    ufw allow 5050/tcp comment 'pgAdmin' quiet
    # PostgreSQL (5432) intentionally not opened — use SSH tunnel
    ok "Firewall rules added (SSH:22, FastAPI:8000, pgAdmin:5050)"
fi

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${CYAN} Setup complete!${NC}"
echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo "  VM IP address:  $(hostname -I | awk '{print $1}')"
echo "  Project dir:    $PROJECT_DIR"
echo ""
echo "  Next steps:"
echo "  1. Log out and back in (applies docker group membership)"
echo "  2. Start devcontainer:"
echo "       cd $PROJECT_DIR"
echo "       docker compose -f .devcontainer/docker-compose.yml up -d"
echo ""
echo "  Or connect via VS Code SSH Remote:"
echo "    Host:  $(hostname -I | awk '{print $1}')"
echo "    User:  $INSTALL_USER"
echo "    Path:  $PROJECT_DIR"
echo ""
warn "Remember to set a strong POSTGRES_PASSWORD in .env before production use!"
