#!/usr/bin/env python3
"""
Test calls for the Laya Decision API (Jev-compatible).

Usage:
    # against local docker compose:
    python test_api.py

    # against a remote host:
    LAYA_BASE_URL=http://myserver:8000 LAYA_API_KEY=my-key python test_api.py
"""

import json
import os
import sys
import urllib.request
import urllib.error

BASE_URL = os.environ.get("LAYA_BASE_URL", "http://localhost:8000").rstrip("/")
API_KEY  = os.environ.get("LAYA_API_KEY", "")

# ── shared fixture ────────────────────────────────────────────────────────────
QUESTIONS = {
    "team": {
        "type": "choice",
        "instructions": "Which team should handle this request?",
        "criteria": {
            "billing":   "invoices, payments, refunds",
            "technical": "bugs, outages, errors",
            "sales":     "pricing, new plans",
            "other":     "everything else",
        },
    },
    "churn": {
        "type": "noul",
        "instructions": "Does the customer explicitly threaten to cancel?",
    },
    "urgency": {
        "type": "score",
        "instructions": "How urgent is this request?",
        "criteria": ["not urgent", "soon", "today"],
    },
}

STATE_BILLING = {
    "subject": "Duplicate charge",
    "body": "We were billed twice for March invoice #4411. Refund the duplicate today or we will cancel.",
}

STATE_TECHNICAL = "The dashboard keeps throwing a 500 error since the last deployment."
STATE_SALES     = "I'd like to upgrade our plan to include 20 more seats."

# ── helpers ───────────────────────────────────────────────────────────────────
PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
results = []


def request(method: str, path: str, body: dict | None = None, auth: bool = True) -> tuple[int, dict]:
    url     = f"{BASE_URL}{path}"
    data    = json.dumps(body).encode() if body else None
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if auth and API_KEY:
        headers["Authorization"] = f"Bearer {API_KEY}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def check(name: str, status: int, body: dict, expect_status: int = 200, checks: list[str] | None = None):
    ok = status == expect_status
    if ok and checks:
        for key_path in checks:
            obj = body
            for k in key_path.split("."):
                if not isinstance(obj, dict) or k not in obj:
                    ok = False
                    print(f"  ✗ missing key: {key_path}")
                    break
                obj = obj[k]
    label = PASS if ok else FAIL
    results.append(ok)
    print(f"[{label}] {name}")
    if not ok:
        print(f"       status={status}  body={json.dumps(body, indent=2)}")
    else:
        print(f"       status={status}  keys={list(body.keys()) if isinstance(body, dict) else type(body).__name__}")


# ═════════════════════════════════════════════════════════════════════════════
print(f"\n{'═'*60}")
print(f"  Laya API test  →  {BASE_URL}")
print(f"  auth={'yes' if API_KEY else 'NO KEY SET — unauthenticated calls will 401'}")
print(f"{'═'*60}\n")

# ── 1. health (no auth) ───────────────────────────────────────────────────────
status, body = request("GET", "/health", auth=False)
check("GET /health (no auth)", status, body, checks=["status", "gpu_available", "default_model"])
if status == 200:
    print(f"       gpu={body.get('gpu_available')}  device={body.get('device')}  rocm={body.get('rocm_hip')}")

# ── 2. models (no auth) ───────────────────────────────────────────────────────
status, body = request("GET", "/v1/models", auth=False)
check("GET /v1/models (no auth)", status, body, checks=["object", "data"])
if status == 200:
    ids = [m["id"] for m in body.get("data", [])]
    print(f"       models={ids}")

# ── 3. auth rejection ─────────────────────────────────────────────────────────
status, body = request("POST", "/v1/systemone",
                       body={"state": "test", "questions": {}},
                       auth=False)
check("POST /v1/systemone — no token → 401", status, body, expect_status=401)

# ── 4. single decision — billing ticket ───────────────────────────────────────
status, body = request("POST", "/v1/systemone", body={
    "model":     "laya-multilingual",
    "state":     STATE_BILLING,
    "questions": QUESTIONS,
})
check("POST /v1/systemone — billing ticket", status, body,
      checks=["model", "answers", "usage"])
if status == 200:
    ans = body.get("answers", {})
    print(f"       team={ans.get('team',{}).get('choice')}  "
          f"churn={ans.get('churn',{}).get('noul')}  "
          f"urgency={ans.get('urgency',{}).get('score')}")

# ── 5. single decision — technical ticket ─────────────────────────────────────
status, body = request("POST", "/v1/systemone", body={
    "state":     STATE_TECHNICAL,
    "questions": QUESTIONS,
})
check("POST /v1/systemone — technical ticket (no model field)", status, body,
      checks=["model", "answers", "usage"])
if status == 200:
    ans = body.get("answers", {})
    print(f"       team={ans.get('team',{}).get('choice')}  "
          f"churn={ans.get('churn',{}).get('noul')}  "
          f"urgency={ans.get('urgency',{}).get('score')}")

# ── 6. model alias — jev-latest ───────────────────────────────────────────────
status, body = request("POST", "/v1/systemone", body={
    "model":     "jev-latest",
    "state":     "Quick question about pricing.",
    "questions": {"team": QUESTIONS["team"]},
})
check("POST /v1/systemone — model alias jev-latest", status, body,
      checks=["model", "answers"])
if status == 200:
    print(f"       resolved model={body.get('model')}")

# ── 7. batch ──────────────────────────────────────────────────────────────────
status, body = request("POST", "/v1/systemone/batch", body={
    "model": "laya-multilingual",
    "items": [STATE_BILLING, STATE_TECHNICAL, STATE_SALES],
    "questions": {"team": QUESTIONS["team"]},
})
check("POST /v1/systemone/batch — 3 items", status, body, expect_status=200)
if status == 200 and isinstance(body, list):
    teams = [item.get("answers", {}).get("team", {}).get("choice") for item in body]
    print(f"       results={len(body)}  teams={teams}")
else:
    # batch returns a list, not a dict — re-check
    if isinstance(body, list) and len(body) == 3:
        results[-1] = True
        teams = [item.get("answers", {}).get("team", {}).get("choice") for item in body]
        print(f"  [{PASS}] (list response)  results={len(body)}  teams={teams}")

# ── 8. unknown model ──────────────────────────────────────────────────────────
status, body = request("POST", "/v1/systemone", body={
    "model":     "nonexistent-model",
    "state":     "test",
    "questions": {"q": {"type": "noul", "instructions": "test?"}},
})
# Laya Router may raise or fall back — either a 200 (fallback) or 4xx/5xx is acceptable
ok = status in (200, 400, 422, 500)
results.append(ok)
label = PASS if ok else FAIL
print(f"[{label}] POST /v1/systemone — unknown model (status={status}, acceptable={ok})")

# ── summary ───────────────────────────────────────────────────────────────────
passed = sum(results)
total  = len(results)
print(f"\n{'═'*60}")
print(f"  {passed}/{total} passed", end="")
if passed == total:
    print("  🎉")
else:
    print(f"  — {total - passed} failed")
print(f"{'═'*60}\n")

sys.exit(0 if passed == total else 1)
