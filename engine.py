#!/usr/bin/env python3
"""
Autoresearch Engine — continuous round-robin experiment runner.
Runs 24/7, discovers projects, cycles through them, never stalls.
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────────────
BASE_DIR   = Path(__file__).parent.resolve()
PROJ_DIR   = BASE_DIR / "projects"
LOGS_DIR   = BASE_DIR / "logs"
STATE_DIR  = BASE_DIR / "state"
ENGINE_LOG = LOGS_DIR / "engine.jsonl"
BUDGET_FILE = STATE_DIR / "budget.json"
GEN_PID    = STATE_DIR / "generator.pid"
STOPPED_FILE = STATE_DIR / "engine_stopped.txt"

MIN_INTERVAL_S = 120   # 2 minutes between runs per project
RUN_TIMEOUT_S  = 2400  # 40-minute subprocess timeout (rate-limited free models need time)
BUDGET_SLEEP_S = 3600  # 1 hour when budget exhausted
PLATEAU_THRESHOLD = 3  # Skip agent after N consecutive no-improvement experiments

# ── Shutdown flag ──────────────────────────────────────────────────────────────
_shutdown = False

def _handle_signal(signum, frame):
    global _shutdown
    _shutdown = True

signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT,  _handle_signal)

# ── Helpers ────────────────────────────────────────────────────────────────────
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def log_event(event: str, project: str = "", **extra):
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    record = {"ts": now_iso(), "event": event, "project": project, **extra}
    with ENGINE_LOG.open("a") as f:
        f.write(json.dumps(record) + "\n")
    print(f"[{record['ts']}] {event} {project} {extra}", flush=True)

def read_score(project_path: Path) -> float:
    score_file = project_path / "best" / "score.txt"
    try:
        return float(score_file.read_text().strip())
    except Exception:
        return 0.0

def read_exp_count(project_path: Path) -> int:
    exp_file = project_path / "experiments.jsonl"
    try:
        with exp_file.open() as f:
            return sum(1 for _ in f)
    except Exception:
        return 0

def last_run_file(project_name: str) -> Path:
    return STATE_DIR / f"last_run_{project_name}.txt"

def seconds_since_last_run(project_name: str) -> float:
    lrf = last_run_file(project_name)
    try:
        ts = float(lrf.read_text().strip())
        return time.time() - ts
    except Exception:
        return float("inf")

def mark_last_run(project_name: str):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    last_run_file(project_name).write_text(str(time.time()))

def budget_ok() -> bool:
    try:
        data = json.loads(BUDGET_FILE.read_text())
        return bool(data.get("ok", True))
    except Exception:
        return True  # assume ok if file missing

def read_project_chars(proj_path: Path) -> int:
    """Read total_chars from meta.json or estimate from system-prompt.md."""
    try:
        meta = json.loads((proj_path / "meta.json").read_text())
        return meta.get("total_chars", 5000)
    except Exception:
        try:
            return len((proj_path / "system-prompt.md").read_text())
        except Exception:
            return 5000

def count_consecutive_no_improve(proj_path: Path) -> int:
    """Count consecutive non-improvement experiments from the end of experiments.jsonl."""
    exp_file = proj_path / "experiments.jsonl"
    if not exp_file.exists():
        return 0
    try:
        lines = exp_file.read_text().strip().split("\n")
        count = 0
        for line in reversed(lines):
            entry = json.loads(line)
            if entry.get("status", "").startswith("keep"):
                break
            if entry.get("status") == "baseline":
                break
            count += 1
        return count
    except Exception:
        return 0

def allocate_experiments(projects: list[tuple[str, Path]], total_budget: int) -> dict[str, int]:
    """Smart experiment allocation: size-proportional with plateau detection.
    
    Strategy:
    - Round 1: 5 experiments per agent (broad coverage, ~80% of gains)
    - Round 2: remaining budget proportional to file size (bigger = more savings)
    - Plateau: skip agents with 3+ consecutive no-improvement
    """
    ROUND1_PER_AGENT = 5
    
    # Filter out plateaued agents
    active = []
    for name, path in projects:
        no_improve = count_consecutive_no_improve(path)
        if no_improve >= PLATEAU_THRESHOLD:
            log_event("plateau_skip", project=name,
                      consecutive_no_improve=no_improve)
            continue
        active.append((name, path))
    
    if not active:
        return {}
    
    # Round 1: minimum allocation
    alloc = {name: ROUND1_PER_AGENT for name, _ in active}
    remaining = total_budget - (len(active) * ROUND1_PER_AGENT)
    
    if remaining > 0:
        # Round 2: proportional to file size
        sizes = {name: read_project_chars(path) for name, path in active}
        total_size = sum(sizes.values())
        for name, _ in active:
            extra = max(3, round(remaining * sizes[name] / total_size))
            alloc[name] += extra
    
    return alloc

def discover_projects() -> list[tuple[str, Path]]:
    """Return [(name, path)] for runnable projects."""
    projects = []
    if not PROJ_DIR.exists():
        return projects
    for entry in sorted(PROJ_DIR.iterdir()):
        if not entry.is_dir():
            continue
        # Support both formats:
        # 1. Full engine mode: agent-codex.py + test-set.json (free LLM router)
        # 2. Simple loop mode: program.md + evaluate.sh (ACP/manual)
        has_engine = (entry / "agent-codex.py").exists() and (entry / "test-set.json").exists()
        has_simple = (entry / "program.md").exists() and (entry / "evaluate.sh").exists()
        if has_engine or has_simple:
            projects.append((entry.name, entry))
    return projects

# ── Budget guard ───────────────────────────────────────────────────────────────
def wait_for_budget():
    while not budget_ok():
        log_event("budget_wait", project="", reason="budget ok=false, sleeping 1h")
        for _ in range(BUDGET_SLEEP_S):
            if _shutdown:
                return
            time.sleep(1)

# ── Generator spawn ────────────────────────────────────────────────────────────
def maybe_spawn_generator():
    gen_script = BASE_DIR / "generator.py"
    if not gen_script.exists():
        return

    # Check existing PID
    if GEN_PID.exists():
        try:
            pid = int(GEN_PID.read_text().strip())
            os.kill(pid, 0)  # raises if not running
            return  # already running
        except (ProcessLookupError, ValueError):
            GEN_PID.unlink(missing_ok=True)

    proc = subprocess.Popen(
        [sys.executable, str(gen_script)],
        cwd=str(BASE_DIR),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    GEN_PID.write_text(str(proc.pid))
    log_event("generator_spawn", project="", pid=proc.pid)

# ── Run a single project ───────────────────────────────────────────────────────
def run_project(name: str, proj_path: Path, max_experiments: int = 10):
    if _shutdown:
        return

    wait_for_budget()
    if _shutdown:
        return

    # Skip plateaued agents
    no_improve = count_consecutive_no_improve(proj_path)
    if no_improve >= PLATEAU_THRESHOLD:
        log_event("plateau_skip", project=name, consecutive_no_improve=no_improve)
        return

    score_before = read_score(proj_path)
    ts_label = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_path = LOGS_DIR / f"{name}-{ts_label}.log"
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    log_event("run_start", project=name, score_before=score_before,
              experiments=read_exp_count(proj_path), max_experiments=max_experiments)

    t0 = time.time()
    try:
        # Pass --resume if a baseline already exists
        has_baseline = any(
            '"status": "baseline"' in line
            for line in (proj_path / "experiments.jsonl").open()
        ) if (proj_path / "experiments.jsonl").exists() else False
        cmd = [sys.executable, "agent-codex.py", "--max", str(max_experiments)]
        if has_baseline:
            cmd.append("--resume")
        with log_path.open("w") as log_fh:
            result = subprocess.run(
                cmd,
                cwd=str(proj_path),
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                timeout=RUN_TIMEOUT_S,
            )
        duration = round(time.time() - t0, 1)
        score_after = read_score(proj_path)
        mark_last_run(name)
        log_event("run_done", project=name,
                  score_before=score_before, score_after=score_after,
                  duration_s=duration, returncode=result.returncode,
                  max_experiments=max_experiments)
    except subprocess.TimeoutExpired:
        duration = round(time.time() - t0, 1)
        log_event("run_error", project=name, reason="timeout",
                  score_before=score_before, score_after=score_before,
                  duration_s=duration)
        mark_last_run(name)
    except Exception as exc:
        duration = round(time.time() - t0, 1)
        log_event("run_error", project=name, reason=str(exc),
                  score_before=score_before, score_after=score_before,
                  duration_s=duration)
        mark_last_run(name)

# ── Main loop ──────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Autoresearch Engine")
    parser.add_argument("--once",    action="store_true", help="Run one full cycle then exit")
    parser.add_argument("--project", metavar="NAME",      help="Only run this project")
    parser.add_argument("--hours",   type=float, default=0, help="Stop after N hours (0=no limit)")
    parser.add_argument("--dry-run", action="store_true", help="Discover and print projects only")
    args = parser.parse_args()
    deadline = time.time() + (args.hours * 3600) if args.hours > 0 else None

    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    projects = discover_projects()

    if not projects:
        print("No runnable projects found in", PROJ_DIR)
        sys.exit(0)

    if args.project:
        projects = [(n, p) for n, p in projects if n == args.project]
        if not projects:
            print(f"Project '{args.project}' not found or not runnable.")
            sys.exit(1)

    if args.dry_run:
        print(f"Discovered {len(projects)} runnable project(s):")
        for name, path in projects:
            score = read_score(path)
            exps  = read_exp_count(path)
            print(f"  {name:30s}  score={score:.3f}  experiments={exps}  path={path}")
        sys.exit(0)

    # Estimate total experiment budget from time limit
    # ~45 sec per experiment on free models
    if args.hours > 0:
        total_budget = int(args.hours * 3600 / 45)
    else:
        total_budget = len(projects) * 30  # default: 30 per project

    # Smart allocation: size-proportional with plateau detection
    alloc = allocate_experiments(projects, total_budget)
    
    log_event("engine_start", project="",
              projects=[n for n, _ in projects], pid=os.getpid(),
              total_budget=total_budget, hours=args.hours or 0,
              allocation={k: v for k, v in sorted(alloc.items(), key=lambda x: -x[1])})

    cycle = 0
    try:
        while not _shutdown:
            cycle += 1
            
            # Check deadline
            if deadline and time.time() > deadline:
                log_event("deadline_reached", project="", hours=args.hours)
                break
            
            # Recalculate allocation each cycle (plateau detection updates)
            if cycle > 1:
                alloc = allocate_experiments(projects, max(total_budget // 3, len(projects) * 5))

            log_event("cycle_start", project="", cycle=cycle,
                      active_agents=len(alloc))

            if not alloc:
                log_event("all_plateaued", project="")
                break

            # Run projects in order of allocation (biggest first = most value)
            proj_map = {n: p for n, p in projects}
            for name in sorted(alloc, key=lambda n: -alloc[n]):
                if _shutdown:
                    break
                if deadline and time.time() > deadline:
                    break
                if name in proj_map:
                    run_project(name, proj_map[name], max_experiments=alloc[name])
                    # Brief cooldown between agents to let rate limits reset
                    if not _shutdown:
                        time.sleep(15)

            if not _shutdown:
                maybe_spawn_generator()

            if args.once:
                break

            if not _shutdown:
                time.sleep(10)

    finally:
        STOPPED_FILE.write_text(now_iso())
        log_event("engine_stop", project="", cycle=cycle)
        print("Engine stopped cleanly.", flush=True)

if __name__ == "__main__":
    main()
