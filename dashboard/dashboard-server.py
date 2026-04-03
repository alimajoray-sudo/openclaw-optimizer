#!/usr/bin/env python3
"""FastAPI dashboard server for autoresearch. Serves dashboard.html and live API."""
import json
import os
from pathlib import Path
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
import uvicorn

PORT = 8830
PROJECT_DIR = Path(__file__).parent
AUTORESEARCH_DIR = PROJECT_DIR.parent  # free-autoresearch/

EMPTY_STATUS = lambda: {
    "phase": "waiting", "experiment": 0, "max_experiments": 0,
    "best_score": 0, "baseline_score": 0, "improvements": 0,
    "regressions": 0, "skips": 0, "elapsed_seconds": 0,
    "codex_calls": 0, "model": "unknown", "rate_limited": False,
    "eta_minutes": 0, "current_question": "", "current_question_id": 0,
    "total_questions": 0, "question_scores": [], "experiments_log": []
}


def discover_projects():
    """Auto-discover projects from projects/ directory."""
    projects = {}
    proj_dir = AUTORESEARCH_DIR / "projects"
    if proj_dir.exists():
        for p in sorted(proj_dir.iterdir()):
            if p.is_dir() and (p / "test-set.json").exists():
                projects[p.name] = {
                    "name": p.name.replace("-", " ").title(),
                    "path": p,
                    "status_file": Path(f"/tmp/{p.name}-status.json"),
                    "experiments_file": p / "experiments.jsonl",
                    "best_score_file": p / "best" / "score.txt",
                }
    return projects


def read_best_score(path: Path) -> float:
    try:
        return float(path.read_text().strip())
    except Exception:
        return 0.0


def read_experiment_count(path: Path) -> int:
    try:
        return sum(1 for l in path.read_text().splitlines() if l.strip())
    except Exception:
        return 0


app = FastAPI(title="Autoresearch Dashboard")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.get("/")
async def index():
    from fastapi.responses import Response
    html = (PROJECT_DIR / "dashboard.html").read_text()
    return Response(content=html, media_type="text/html", headers={
        "Cache-Control": "no-store, no-cache, must-revalidate",
        "Pragma": "no-cache"
    })


@app.get("/api/projects")
async def all_projects():
    PROJECTS = discover_projects()
    result = {}
    for key, proj in PROJECTS.items():
        try:
            data = json.loads(proj["status_file"].read_text())
        except Exception:
            data = EMPTY_STATUS()
        data["project_name"] = proj["name"]
        data["project_key"] = key
        data["best_score"] = read_best_score(proj["best_score_file"])
        data["experiment_count"] = read_experiment_count(proj["experiments_file"])
        # Count improvements from experiments.jsonl (status="kept" means improvement)
        try:
            improvements = 0
            baseline_score = 0
            for line in proj["experiments_file"].read_text().splitlines():
                if not line.strip(): continue
                entry = json.loads(line)
                if entry.get("status") in ("kept", "keep") or entry.get("improved"): improvements += 1
                if entry.get("experiment") == 0 or entry.get("status") == "baseline":
                    baseline_score = entry.get("score", 0)
            data["improvements"] = improvements
            if not data.get("baseline_score"): data["baseline_score"] = baseline_score
        except Exception:
            pass
        # Add compression metrics
        meta_file = proj["path"] / "meta.json"
        best_file = proj["path"] / "best" / "system-prompt.md"
        target_file = proj["path"] / "system-prompt.md"
        try:
            meta = json.loads(meta_file.read_text())
            original_chars = meta.get("total_chars", 0)
        except Exception:
            try:
                original_chars = len(target_file.read_text())
            except Exception:
                original_chars = 0
        try:
            best_chars = len(best_file.read_text())
        except Exception:
            best_chars = original_chars
        data["original_chars"] = original_chars
        data["best_chars"] = best_chars
        data["compression_pct"] = round((1 - best_chars / original_chars) * 100, 1) if original_chars > 0 else 0
        result[key] = data
    return JSONResponse(result)


@app.get("/api/status/{project_key}")
async def project_status(project_key: str):
    PROJECTS = discover_projects()
    if project_key not in PROJECTS:
        return JSONResponse({"error": "unknown project"}, status_code=404)
    proj = PROJECTS[project_key]
    try:
        data = json.loads(proj["status_file"].read_text())
    except Exception:
        data = EMPTY_STATUS()
    data["best_score"] = read_best_score(proj["best_score_file"])
    data["experiment_count"] = read_experiment_count(proj["experiments_file"])
    return JSONResponse(data)


@app.get("/api/experiments/{project_key}")
async def project_experiments(project_key: str):
    PROJECTS = discover_projects()
    if project_key not in PROJECTS:
        return JSONResponse({"error": "unknown project"}, status_code=404)
    entries = []
    try:
        for line in PROJECTS[project_key]["experiments_file"].read_text().strip().splitlines():
            if line.strip():
                entries.append(json.loads(line))
    except Exception:
        pass
    return JSONResponse(entries[-100:])  # last 100


@app.get("/api/engine")
async def engine_events():
    """Recent engine.jsonl events — last 50."""
    log = AUTORESEARCH_DIR / "logs/engine.jsonl"
    entries = []
    try:
        for line in log.read_text().strip().splitlines():
            if line.strip():
                entries.append(json.loads(line))
    except Exception:
        pass
    return JSONResponse(entries[-50:])


@app.get("/api/generator")
async def generator_events():
    """Recent generator.jsonl events — last 30."""
    log = AUTORESEARCH_DIR / "state/generator.jsonl"
    entries = []
    try:
        for line in log.read_text().strip().splitlines():
            if line.strip():
                entries.append(json.loads(line))
    except Exception:
        pass
    return JSONResponse(entries[-30:])


@app.get("/api/budget")
async def budget():
    """Current budget status from router state."""
    try:
        data = json.loads((AUTORESEARCH_DIR / "state/budget.json").read_text())
        return JSONResponse(data)
    except Exception:
        return JSONResponse({"today_usd": 0.0, "cap_usd": 0.5, "remaining_usd": 0.5, "ok": True})


@app.get("/api/router")
async def router_state():
    """Live router quota + model pool state."""
    quota_file = AUTORESEARCH_DIR / "state/quota.json"
    try:
        quota = json.loads(quota_file.read_text())
    except Exception:
        quota = {}
    # Build model pool summary from known config
    pool = [
        {"id": "qwen/qwen3-coder:free",                 "tier": 0, "provider": "openrouter", "role": "mutator",   "cost": 0.0},
        {"id": "z-ai/glm-4.5-air:free",                 "tier": 0, "provider": "openrouter", "role": "mutator",   "cost": 0.0},
        {"id": "google/gemma-3-27b-it:free",             "tier": 0, "provider": "openrouter", "role": "both",      "cost": 0.0},
        {"id": "nvidia/nemotron-3-nano-30b-a3b:free",    "tier": 0, "provider": "openrouter", "role": "both",      "cost": 0.0},
        {"id": "meta-llama/llama-3.2-3b-instruct:free",  "tier": 0, "provider": "openrouter", "role": "evaluator", "cost": 0.0},
        {"id": "Qwen/Qwen2.5-72B-Instruct",              "tier": 1, "provider": "huggingface","role": "both",      "cost": 0.0},
        {"id": "meta-llama/Llama-3.3-70B-Instruct",      "tier": 1, "provider": "huggingface","role": "both",      "cost": 0.0},
        {"id": "Qwen/Qwen2.5-Coder-32B-Instruct",        "tier": 1, "provider": "huggingface","role": "mutator",   "cost": 0.0},
        {"id": "Qwen/Qwen3-4B-Thinking-2507",            "tier": 1, "provider": "huggingface","role": "evaluator", "cost": 0.0},
        {"id": "grok-3-mini",                            "tier": 2, "provider": "xai",        "role": "both",      "cost": 0.0005},
        {"id": "grok-3-mini-fast",                       "tier": 2, "provider": "xai",        "role": "evaluator", "cost": 0.00025},
        {"id": "deepseek-chat",                          "tier": 3, "provider": "deepseek",   "role": "both",      "cost": 0.00042},
    ]
    for m in pool:
        key = f"{m['provider']}/{m['id']}"
        m["quota_entries"] = quota.get(key, [])
        m["exhausted"] = len(m["quota_entries"]) > 0  # has recent rate-limit entries
    return JSONResponse({"models": pool, "quota_raw": quota})


@app.get("/api/model_log")
async def model_log():
    """Aggregate model_log entries from all active project status files — last 100 calls."""
    PROJECTS = discover_projects()
    entries = []
    for key, proj in PROJECTS.items():
        try:
            data = json.loads(proj["status_file"].read_text())
            log = data.get("model_log", [])
            for e in log:
                e["project"] = key
                entries.append(e)
        except Exception:
            pass
    return JSONResponse(entries[-100:])


@app.get("/api/test_counts")
async def test_counts():
    """How many test cases each project has."""
    PROJECTS = discover_projects()
    result = {}
    for key, proj in PROJECTS.items():
        test_file = proj["experiments_file"].parent / "test-set.json"
        try:
            tests = json.loads(test_file.read_text())
            result[key] = len(tests)
        except Exception:
            result[key] = 0
    return JSONResponse(result)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
