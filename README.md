# Laya on ROCm — Docker Setup

Runs the [Laya](https://laya.convaiinnovations.com/) non-autoregressive decision model from [Convai Innovations](https://huggingface.co/convaiinnovations/laya) on an AMD GPU via ROCm inside Docker, exposing:

* `GET  /v1/models` — list available checkpoints (no auth)

* `POST /v1/systemone` — single-state decision (**Jev-compatible**, drop-in replacement)

* `POST /v1/systemone/batch` — batch decisions (Jev extension)

* `/mcp` — **Streamable HTTP MCP server** (tools: `system_one` and `system_one_batch` with progress streaming)

* `GET /health` — GPU probe (no auth)

---

## Prerequisites

| Requirement | Notes |
| --- | --- |
| AMD GPU | RX 5000+, RX 6000+, RX 7000+ (RDNA), or MI-series (CDNA) |
| ROCm driver on host | [Install guide](https://rocm.docs.amd.com/en/latest/deploy/linux/index.html) — verify with `rocminfo` |
| Docker ≥ 24 | With `--device` passthrough support |
| Docker Compose v2 | `docker compose version` |
| \~2 GB disk | For the multilingual model weights (cached in a named volume) |

---

## Quick start

```bash
git clone <this-repo>
cd laya-rocm

cp .env.example .env
# Edit .env — at minimum set a strong LAYA_API_KEY

docker compose up -d --build
docker compose logs -f laya   # watch weight download + startup
```

The first startup downloads model weights (\~700 MB for multilingual). Subsequent starts are instant because the weights are cached in the `laya-models` Docker volume.

Once you see `Laya router ready.` in the logs, the service is up.

---

## Environment variables

| Variable | Default | Description |
| --- | --- | --- |
| `LAYA_API_KEY` | _(auto-generated)_ | Bearer token clients must send. Auto-generated if blank — copy from logs. |
| `LAYA_MODEL` | `laya-multilingual` | Checkpoint: `laya-multilingual` \| `laya-english` \| `laya-typed-decisions` |
| `UVICORN_WORKERS` | `2` | Uvicorn worker processes. Raise to 4 on high-core hosts. |
| `HF_HOME` | `/models` | Weight cache path inside the container. |
| `HSA_OVERRIDE_GFX_VERSION` | _(unset)_ | Set to `10.3.0` (RDNA2) or `11.0.0` (RDNA3) for unsupported consumer GPUs. |

---

## Available checkpoints

| `LAYA_MODEL` value | Encoder | Params | Context | Best for |
| --- | --- | --- | --- | --- |
| `laya-multilingual` | mmBERT-base | 322 M | 1 024 tok | Most use cases — 100+ languages |
| `laya-english` | ModernBERT-large | 421 M | 512 tok | English-only, larger encoder |
| `laya-typed-decisions` | ModernBERT-large | 421 M | 1 024 tok | Invoices, tickets, agent traces |

---

## Question types

| Type | Returns | Use it for |
| --- | --- | --- |
| `choice` | Most likely option + probability per option + confidence | Routing, classification |
| `score` | Expected level (float) on ordered scale + distribution | Urgency, severity, ratings |
| `noul` | P(true) — 0.0 … 1.0 | Yes/no propositions |

---

## REST API

All endpoints except `/health` and `/v1/models` require:

```
Authorization: Bearer <LAYA_API_KEY>
```

### TypeSafe SDK drop-in

Existing integrations using the TypeSafe SDK need **zero code changes** — just set two env vars:

```bash
export TYPESAFE_BASE_URL=http://localhost:8000
export TYPESAFE_API_KEY=<LAYA_API_KEY>
```

### Health check

```bash
curl http://localhost:8000/health
```

```json
{
  "status": "ok",
  "default_model": "laya-multilingual",
  "gpu_available": true,
  "device": "AMD Radeon RX 7900 XTX",
  "rocm_hip": "6.1.40091-4f163c0f",
  "torch": "2.3.0+rocm6.1"
}
```

If `gpu_available` is `false`, see [Troubleshooting](#troubleshooting).

### List models — `GET /v1/models`

```bash
curl http://localhost:8000/v1/models
```

```json
{
  "object": "list",
  "data": [
    {"id": "laya-multilingual",    "object": "model", "description": "..."},
    {"id": "laya-english",         "object": "model", "description": "..."},
    {"id": "laya-typed-decisions", "object": "model", "description": "..."}
  ]
}
```

### Single decision — `POST /v1/systemone`

Jev-compatible. The `model` field is optional — omit it to use the server's `LAYA_MODEL`.

```bash
curl -X POST http://localhost:8000/v1/systemone \
  -H "Authorization: Bearer $LAYA_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "laya-multilingual",
    "state": {
      "subject": "Duplicate charge",
      "body": "We were billed twice for March invoice #4411. Refund the duplicate today or we will cancel."
    },
    "questions": {
      "team": {
        "type": "choice",
        "instructions": "Which team should handle this request?",
        "criteria": {
          "billing":   "invoices, payments, refunds",
          "technical": "bugs, outages, errors",
          "sales":     "pricing, new plans",
          "other":     "everything else"
        }
      },
      "churn": {
        "type": "noul",
        "instructions": "Does the customer explicitly threaten to cancel?"
      },
      "urgency": {
        "type": "score",
        "instructions": "How urgent is this request?",
        "criteria": ["not urgent", "soon", "today"]
      }
    }
  }'
```

Response (Jev-compatible shape):

```json
{
  "model": "laya-multilingual",
  "answers": {
    "team":   {"type":"choice","choice":"billing","confidence":1.0,"probabilities":{"billing":1.0,"technical":0.0,"sales":0.0,"other":0.0}},
    "churn":  {"type":"noul","noul":0.9794},
    "urgency":{"type":"score","score":1.9,"confidence":0.698,"legend":{"0":"not urgent","1":"soon","2":"today"},"probabilities":{"0":0.004,"1":0.092,"2":0.904}}
  },
  "usage": {"input_tokens":172,"output_tokens":0}
}
```

### Batch decisions — `POST /v1/systemone/batch`

Jev extension (Jev has no batch endpoint). Same `model` field, takes `items` array.

```bash
curl -X POST http://localhost:8000/v1/systemone/batch \
  -H "Authorization: Bearer $LAYA_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "laya-multilingual",
    "items": [
      "The app keeps crashing on startup since the last update.",
      "I need to upgrade my plan to include more seats.",
      "Charged twice in March — please issue a refund."
    ],
    "questions": {
      "team": {
        "type": "choice",
        "instructions": "Which team owns this issue?",
        "criteria": {
          "billing":   "payments, refunds",
          "technical": "bugs, outages",
          "sales":     "plans, upgrades"
        }
      }
    }
  }'
```

Returns an array of `{ model, answers, usage }` objects in the same order as `items`.

---

## MCP server

The MCP server is mounted at `/mcp` and uses the **Streamable HTTP** transport (stateless per request — no persistent WebSocket needed).

### Connect from Claude Desktop / Cursor

Add this to your MCP client config (e.g., `~/.config/claude/mcp.json`):

```json
{
  "mcpServers": {
    "laya": {
      "type": "http",
      "url": "http://localhost:8000/mcp",
      "headers": {
        "Authorization": "Bearer <LAYA_API_KEY>"
      }
    }
  }
}
```

### Inspect with MCP Inspector

```bash
npx @modelcontextprotocol/inspector
# → URL: http://localhost:8000/mcp
# → Add header: Authorization: Bearer <LAYA_API_KEY>
```

### Available MCP tools

Both tools accept an optional `model` field identical to the REST API.

#### `system_one`

Run typed decisions over a single state. Mirrors `POST /v1/systemone`.

```json
{
  "model": "laya-multilingual",
  "state": { "subject": "...", "body": "..." },
  "questions": {
    "team":   { "type": "choice", "instructions": "...", "criteria": { "billing": "..." } },
    "urgent": { "type": "noul",   "instructions": "..." }
  }
}
```

#### `system_one_batch`

Run the same questions over many states. Mirrors `POST /v1/systemone/batch`.\
Reports progress via MCP `progress` notifications as each item completes — your MCP client will see `1/N`, `2/N`, … streaming events.

```json
{
  "model": "laya-multilingual",
  "items": ["ticket text 1", "ticket text 2", { "subject": "..." }],
  "questions": { "team": { "type": "choice", ... } }
}
```

---

## Multi-worker notes

The Dockerfile runs `UVICORN_WORKERS` independent worker processes. Each worker holds its own copy of the Laya model in memory:

* **2 workers** (default): \~1.4 GB VRAM/RAM for multilingual

* **4 workers**: \~2.8 GB — use when you need high parallel throughput

Workers are stateless, so sessions between REST calls and MCP requests are not shared — which is fine for Laya since every prediction is independent.

---

## Troubleshooting

### `gpu_available: false` in `/health`

1. Check host driver: `rocminfo | grep gfx`

2. Check device passthrough: `docker exec laya ls /dev/kfd /dev/dri`

3. Confirm group membership: `docker exec laya id` — should include `video` and `render`

4. For unsupported consumer GPUs, set `HSA_OVERRIDE_GFX_VERSION` in `.env`:

  * RDNA2 (RX 6000): `10.3.0`

  * RDNA3 (RX 7000): `11.0.0`

### Torch is CPU / CUDA, not ROCm

The ROCm base image ships a ROCm torch. If `pip install laya` replaced it:

```bash
# Inside container or during build, check:
python -c "import torch; print(torch.version.hip)"
# Should print a ROCm HIP version, not None
```

`requirements.txt` uses `laya --no-deps` to prevent this. If a transitive dep still pulls CUDA torch, pin it:

```
torch @ https://download.pytorch.org/whl/rocm6.1/torch-2.3.0%2Brocm6.1-cp310-cp310-linux_x86_64.whl
```

Get the exact URL from [PyTorch's ROCm wheel index](https://download.pytorch.org/whl/rocm6.1/).

### Weight download fails

Weights are downloaded from HuggingFace on first run. If the container is air-gapped:

```bash
# On a machine with internet access:
pip install huggingface_hub
python -c "from huggingface_hub import snapshot_download; snapshot_download('convaiinnovations/laya', local_dir='./laya-weights')"

# Copy the folder into the container or bind-mount it:
# volumes:
#   - ./laya-weights:/models/hub/models--convaiinnovations--laya/snapshots/main
```

### MCP client gets 401

The MCP endpoint requires the same `Authorization: Bearer <LAYA_API_KEY>` header as the REST endpoints. Check that your MCP client config includes the header (see example above).

---

## File layout

```
laya-rocm/
├── app.py               # FastAPI + MCP server
├── Dockerfile           # ROCm PyTorch base, multi-worker Uvicorn
├── requirements.txt     # Python deps (preserves ROCm torch)
├── docker-compose.yml   # ROCm device passthrough, volumes, env
├── .env.example         # Copy to .env and fill in
└── README.md            # This file
```

---

## License

Model weights: Apache 2.0 (Convai Innovations).\
This configuration: MIT.
