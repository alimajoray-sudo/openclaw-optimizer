#!/usr/bin/env python3
"""
generator.py — Continuous test-case generator for autoresearch projects.
Generates new test cases using ModelRouter and appends them to each project's test-set.json.
"""

import sys
import os
import json
import re
import time
import argparse
import signal
import traceback
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from router import ModelRouter, AllModelsExhausted

BASE_DIR = Path(__file__).parent
PROJECTS_DIR = BASE_DIR / "projects"
STATE_DIR = BASE_DIR / "state"
BUDGET_FILE = STATE_DIR / "budget.json"
LOG_FILE = STATE_DIR / "generator.jsonl"
PID_FILE = STATE_DIR / "generator.pid"

N_GENERATE = 5
SLEEP_CYCLE = 1800   # 30 min between full passes
SLEEP_BUDGET = 3600  # 1 hr when budget exhausted


def write_pid():
    STATE_DIR.mkdir(exist_ok=True)
    PID_FILE.write_text(str(os.getpid()))


def remove_pid():
    try:
        PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def budget_ok() -> bool:
    try:
        if BUDGET_FILE.exists():
            data = json.loads(BUDGET_FILE.read_text())
            return data.get("ok", True)
    except Exception:
        pass
    return True


def log_event(project: str, added: int, model: str, cost: float, error: str = ""):
    STATE_DIR.mkdir(exist_ok=True)
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "project": project,
        "added": added,
        "model": model,
        "cost": cost,
    }
    if error:
        entry["error"] = error
    with open(LOG_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")


def discover_projects() -> list[Path]:
    projects = []
    if not PROJECTS_DIR.exists():
        return projects
    for p in sorted(PROJECTS_DIR.iterdir()):
        if p.is_dir():
            if (p / "test-set.json").exists() and (p / "system-prompt.md").exists():
                projects.append(p)
    return projects


def read_text(path: Path, max_chars: int = None) -> str:
    try:
        text = path.read_text(encoding="utf-8")
        if max_chars:
            text = text[:max_chars]
        return text
    except Exception:
        return ""


def load_test_set(path: Path) -> list:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []


def extract_json_array(text: str):
    """Find first [...] block in text and parse it."""
    match = re.search(r'\[.*\]', text, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def build_prompt(system_purpose: str, examples: list, n: int) -> str:
    examples_json = json.dumps(examples, indent=2, ensure_ascii=False)
    return f"""You are a test case generator for an AI system.

SYSTEM PURPOSE: {system_purpose}

EXISTING TEST CASES (examples of the format):
{examples_json}

Generate {n} NEW diverse test cases in the exact same JSON format.
- Cover edge cases, error conditions, unusual inputs
- Vary complexity (simple to complex)
- Do NOT repeat existing inputs
- Output ONLY a valid JSON array, no explanation"""


def next_id(existing: list) -> int:
    """Get next numeric id (used only if ids are ints)."""
    int_ids = [t.get("id") for t in existing if isinstance(t.get("id"), int)]
    return max(int_ids, default=0) + 1


def process_project(project_dir: Path, dry_run: bool = False) -> tuple[int, str, float]:
    """Process one project. Returns (added, model_used, cost)."""
    name = project_dir.name
    test_set_path = project_dir / "test-set.json"
    system_prompt_path = project_dir / "system-prompt.md"
    program_path = project_dir / "program.md"

    system_purpose = read_text(system_prompt_path, max_chars=500)
    existing = load_test_set(test_set_path)

    if not system_purpose:
        raise ValueError(f"Empty system-prompt.md for {name}")

    # Pick 2 examples for few-shot
    examples = existing[:2] if len(existing) >= 2 else existing

    prompt = build_prompt(system_purpose, examples, N_GENERATE)

    if dry_run:
        print(f"\n[dry-run] Project: {name}")
        print(f"  Existing tests: {len(existing)}")
        print(f"  Would send prompt ({len(prompt)} chars) to generate {N_GENERATE} new cases")
        print(f"  Prompt preview:\n{prompt[:300]}...")
        return 0, "dry-run", 0.0

    router = ModelRouter(verbose=False)
    text, model_used, cost = router.complete(prompt, role="mutator", max_tokens=1500)

    new_cases = extract_json_array(text)
    if new_cases is None:
        raise ValueError(f"Failed to parse JSON array from LLM response for {name}")

    if not isinstance(new_cases, list):
        raise ValueError(f"LLM returned non-list for {name}")

    # Deduplicate by input/question field
    existing_inputs = set()
    for t in existing:
        for field in ("input", "question"):
            val = t.get(field)
            if val:
                existing_inputs.add(str(val).strip())

    unique_new = []
    for case in new_cases:
        if not isinstance(case, dict):
            continue
        key = None
        for field in ("input", "question"):
            val = case.get(field)
            if val:
                key = str(val).strip()
                break
        if key and key in existing_inputs:
            continue
        # Assign sequential id if existing ones are ints
        if existing and isinstance(existing[0].get("id"), int):
            case["id"] = next_id(existing) + len(unique_new)
        unique_new.append(case)
        if key:
            existing_inputs.add(key)

    if unique_new:
        updated = existing + unique_new
        test_set_path.write_text(
            json.dumps(updated, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8"
        )

    return len(unique_new), model_used, cost


def run_pass(projects: list[Path], dry_run: bool = False, project_filter: str = None):
    print(f"[generator] Starting pass over {len(projects)} project(s)")
    for project_dir in projects:
        name = project_dir.name
        if project_filter and name != project_filter:
            continue

        # Budget check
        if not budget_ok():
            print(f"[generator] Budget exhausted, sleeping {SLEEP_BUDGET}s")
            time.sleep(SLEEP_BUDGET)

        try:
            added, model, cost = process_project(project_dir, dry_run=dry_run)
            if not dry_run:
                log_event(name, added, model, cost)
                print(f"[generator] {name}: +{added} tests | model={model} | cost=${cost:.4f}")
        except AllModelsExhausted as e:
            msg = f"All models exhausted: {e}"
            print(f"[generator] {name}: ERROR — {msg}")
            if not dry_run:
                log_event(name, 0, "none", 0.0, error=msg)
        except Exception as e:
            msg = traceback.format_exc()
            print(f"[generator] {name}: ERROR — {e}")
            if not dry_run:
                log_event(name, 0, "none", 0.0, error=str(e))


def main():
    parser = argparse.ArgumentParser(description="Continuous test-case generator")
    parser.add_argument("--once", action="store_true", help="One pass then exit")
    parser.add_argument("--project", type=str, help="Only generate for this project name")
    parser.add_argument("--dry-run", action="store_true", dest="dry_run",
                        help="Print what would be generated, don't write")
    args = parser.parse_args()

    projects = discover_projects()
    if not projects:
        print("[generator] No projects found")
        sys.exit(1)

    print(f"[generator] Discovered {len(projects)} project(s): {[p.name for p in projects]}")

    if not args.dry_run:
        write_pid()
        def _cleanup(sig, frame):
            remove_pid()
            sys.exit(0)
        signal.signal(signal.SIGTERM, _cleanup)
        signal.signal(signal.SIGINT, _cleanup)

    try:
        while True:
            run_pass(projects, dry_run=args.dry_run, project_filter=args.project)
            if args.once or args.dry_run:
                break
            print(f"[generator] Pass complete. Sleeping {SLEEP_CYCLE}s")
            time.sleep(SLEEP_CYCLE)
    finally:
        if not args.dry_run:
            remove_pid()


if __name__ == "__main__":
    main()
