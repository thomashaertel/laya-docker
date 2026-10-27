# ─── Laya Decision Model on ROCm ────────────────────────────────────────────
# Base: official ROCm PyTorch image (ships a ROCm-built torch — do NOT let pip
# replace it with a CUDA or CPU wheel).
#
# Build:
#   docker build -t laya-rocm .
#
# Pinned to ROCm 7.2.4 + Ubuntu 24.04 + Python 3.12 + PyTorch 2.9.1.
# This is the newest stack and the first to include gfx1151 (RDNA 4) kernels.
# ─────────────────────────────────────────────────────────────────────────────
FROM rocm/pytorch:rocm7.2.4_ubuntu24.04_py3.12_pytorch_release_2.9.1

# ── System deps ───────────────────────────────────────────────────────────────
# git  : some HuggingFace snapshot_download paths need it
# curl : healthcheck probe in compose
RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ── Python deps ───────────────────────────────────────────────────────────────
# Install WITHOUT touching torch (--no-deps on laya, then add remaining deps).
# This preserves the ROCm-built torch that ships in the base image.
COPY requirements.txt .
# Step 1: install everything except laya (torch is already present from the base image)
RUN pip install --no-cache-dir -r requirements.txt
# Step 2: install laya WITHOUT its declared dependencies so pip cannot
#         swap the ROCm torch for a CPU or CUDA wheel
RUN pip install --no-cache-dir --no-deps laya

# Smoke-test: confirm ROCm torch survived
RUN python -c "import torch; v=torch.__version__; hip=getattr(torch.version,'hip','none'); \
    print(f'torch={v}  HIP={hip}'); assert hip!='none', 'ROCm torch not found!'"

# ── Render group ──────────────────────────────────────────────────────────────
# Name only — entrypoint.sh reads the real GID from /dev/dri/renderD* at
# startup and grants it to the app process via setpriv (no fixed GID here).
RUN getent group render || groupadd --system render

# ── Application ───────────────────────────────────────────────────────────────
COPY app.py .
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Model weights cache (mount a volume here to persist across rebuilds)
ENV HF_HOME=/models

# Expose REST + MCP port
EXPOSE 8000

# ── Entrypoint ────────────────────────────────────────────────────────────────
# --workers: one Uvicorn process per CPU core is a good starting point.
#   For a GPU-bound model like Laya, 2–4 workers usually saturates the GPU
#   without wasting memory.  Override with UVICORN_WORKERS env var.
#
# --timeout-keep-alive: raised to 75 s so slow MCP streaming sessions don't
#   drop while waiting for a large batch.
#
# Gunicorn is NOT used here because the MCP session manager uses asyncio state
# that cannot be shared across processes via a pre-fork model.  Each Uvicorn
# worker is independent, which is fine for stateless HTTP MCP.
ENTRYPOINT ["/entrypoint.sh"]
CMD uvicorn app:app \
    --host 0.0.0.0 \
    --port ${UVICORN_PORT:-8000} \
    --workers ${UVICORN_WORKERS:-1} \
    --timeout-keep-alive 75 \
    --access-log
