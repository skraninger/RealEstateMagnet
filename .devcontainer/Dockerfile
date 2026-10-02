FROM python:3.12-slim-bookworm

# ── Environment ─────────────────────────────────────────────────────────────
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# ── System packages ──────────────────────────────────────────────────────────
# libpq-dev + gcc   → psycopg2 / asyncpg build deps
# nodejs            → Playwright browser manager
# postgresql-client → psql CLI inside container
RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl wget sudo gnupg2 ca-certificates \
    libpq-dev gcc g++ \
    postgresql-client \
    nodejs npm \
    && rm -rf /var/lib/apt/lists/*

# ── Non-root vscode user (VS Code devcontainer convention) ───────────────────
ARG USERNAME=vscode
ARG USER_UID=1000
ARG USER_GID=1000

RUN groupadd --gid ${USER_GID} ${USERNAME} \
    && useradd  --uid ${USER_UID} --gid ${USER_GID} -m ${USERNAME} \
    && echo "${USERNAME} ALL=(root) NOPASSWD:ALL" > /etc/sudoers.d/${USERNAME} \
    && chmod 0440 /etc/sudoers.d/${USERNAME}

# ── Python dependencies (cached layer) ──────────────────────────────────────
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# ── Playwright: install Chromium + all system deps (runs as root) ────────────
RUN playwright install chromium --with-deps

# ── Switch to non-root user for all devcontainer runtime ────────────────────
USER ${USERNAME}
WORKDIR /workspace
