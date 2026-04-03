#!/usr/bin/env python3
"""
discover.py — Auto-discover OpenClaw workspace files and create optimization projects.

Usage:
    python discover.py --workspace ~/.openclaw/workspace-main
    python discover.py --workspace ~/.openclaw/workspace-main --skills-only
    python discover.py --workspace ~/.openclaw/workspace-main --workspace-only
"""
import argparse
import json
import os
import re
import shutil
from pathlib import Path
from datetime import datetime

PROJECTS_DIR = Path(__file__).parent / "projects"
TEMPLATE_DIR = Path(__file__).parent

# Workspace files to optimize (injected into every conversation)
WORKSPACE_FILES = [
    "SOUL.md", "AGENTS.md", "USER.md", "TOOLS.md",
    "HEARTBEAT.md", "IDENTITY.md", "MEMORY.md"
]

MIN_SKILL_SIZE = 3000  # Only optimize skills > 3KB


def extract_test_cases(content: str, source_name: str) -> list:
    """Extract verification test cases from file content."""
    tests = []
    test_id = 0

    # 1. URLs
    urls = re.findall(r'https?://[^\s\)\"\'>`]+', content)
    for url in urls[:3]:
        test_id += 1
        tests.append({
            "id": f"Q{test_id}",
            "question": f"What URL is referenced for {url.split('/')[2]}?",
            "expected_keywords": [url[:60]],
            "category": "urls"
        })

    # 2. Code commands
    code_blocks = re.findall(r'```(?:bash|sh|shell)?\n(.*?)```', content, re.DOTALL)
    for block in code_blocks[:4]:
        lines = [l.strip() for l in block.strip().split('\n')
                 if l.strip() and not l.strip().startswith('#')]
        for line in lines[:1]:
            cmd_parts = line.split()[:3]
            if cmd_parts:
                test_id += 1
                tests.append({
                    "id": f"Q{test_id}",
                    "question": f"What command: {line[:50]}?",
                    "expected_keywords": cmd_parts,
                    "category": "commands"
                })

    # 3. Port numbers
    ports = re.findall(r'(?:port|PORT)\s*[:=]?\s*(\d{4,5})', content, re.IGNORECASE)
    ports += re.findall(r'localhost:(\d{4,5})', content)
    for port in set(list(ports)[:3]):
        test_id += 1
        tests.append({
            "id": f"Q{test_id}",
            "question": f"What runs on port {port}?",
            "expected_keywords": [port],
            "category": "config"
        })

    # 4. Rules (MUST/NEVER/ALWAYS/CRITICAL)
    rules = re.findall(
        r'^.*(?:MUST|NEVER|ALWAYS|DO NOT|CRITICAL|WARNING).*$',
        content, re.MULTILINE | re.IGNORECASE
    )
    for rule in rules[:4]:
        words = [w for w in rule.split() if len(w) > 3 and
                 w.upper() not in ('MUST', 'NEVER', 'ALWAYS', 'THAT', 'THIS', 'WITH', 'FROM')]
        if len(words) >= 2:
            test_id += 1
            tests.append({
                "id": f"Q{test_id}",
                "question": f"Rule: {rule.strip()[:60]}?",
                "expected_keywords": words[:5],
                "category": "rules"
            })

    # 5. File paths
    paths = re.findall(r'[`"]([~/][a-zA-Z0-9_./-]{10,})[`"]', content)
    for p in paths[:3]:
        test_id += 1
        tests.append({
            "id": f"Q{test_id}",
            "question": f"Path for {os.path.basename(p)}?",
            "expected_keywords": [p],
            "category": "paths"
        })

    # 6. Section headers
    headers = re.findall(r'^#{1,3}\s+(.+)$', content, re.MULTILINE)
    for h in headers[:5]:
        words = [w for w in h.split() if len(w) > 3]
        if words:
            test_id += 1
            tests.append({
                "id": f"Q{test_id}",
                "question": f"What does '{h.strip()}' cover?",
                "expected_keywords": words[:4],
                "category": "coverage"
            })

    # 7. Named entities (emails, names in bold/backticks)
    entities = re.findall(r'\*\*([^*]{3,40})\*\*', content)
    for ent in entities[:3]:
        words = ent.split()
        if len(words) <= 4:
            test_id += 1
            tests.append({
                "id": f"Q{test_id}",
                "question": f"What is {ent}?",
                "expected_keywords": words,
                "category": "entities"
            })

    # Deduplicate
    seen = set()
    unique = []
    for t in tests:
        key = t["question"][:30]
        if key not in seen:
            seen.add(key)
            unique.append(t)

    return unique[:25]


def get_codex_template() -> str:
    """Read the agent-codex.py template."""
    # Try from existing project
    for proj in PROJECTS_DIR.iterdir() if PROJECTS_DIR.exists() else []:
        codex = proj / "agent-codex.py"
        if codex.exists():
            return codex.read_text()
    # Fallback: check system-prompt-compression
    fallback = PROJECTS_DIR / "system-prompt-compression" / "agent-codex.py"
    if fallback.exists():
        return fallback.read_text()
    print("ERROR: No agent-codex.py template found. Run with an existing project first.")
    return ""


def create_project(name: str, content: str, source_path: str, template: str) -> dict | None:
    """Create an optimization project."""
    proj_dir = PROJECTS_DIR / name
    if proj_dir.exists():
        return None  # Already exists

    tests = extract_test_cases(content, name)
    if len(tests) < 5:
        print(f"  Skip {name}: only {len(tests)} test cases")
        return None

    proj_dir.mkdir(parents=True, exist_ok=True)

    # Target file
    (proj_dir / "system-prompt.md").write_text(content)

    # Test set
    (proj_dir / "test-set.json").write_text(json.dumps(tests, indent=2))

    # Meta
    meta = {
        "name": name,
        "source": source_path,
        "original_chars": len(content),
        "test_cases": len(tests),
        "created": datetime.now().isoformat()
    }
    (proj_dir / "meta.json").write_text(json.dumps(meta, indent=2))

    # Agent codex (adapted template)
    codex = re.sub(
        r'STATUS_FILE\s*=\s*f?["\'].*?["\']',
        f'STATUS_FILE = f"/tmp/{name}-status.json"',
        template
    )
    (proj_dir / "agent-codex.py").write_text(codex)

    # Best dir
    best_dir = proj_dir / "best"
    best_dir.mkdir(exist_ok=True)
    (best_dir / "system-prompt.md").write_text(content)
    (best_dir / "score.txt").write_text("0.0000\n")

    # Empty experiments
    (proj_dir / "experiments.jsonl").write_text("")

    return {"name": name, "chars": len(content), "tests": len(tests)}


def discover_workspace(workspace: Path) -> list:
    """Discover workspace-level files."""
    results = []
    for fname in WORKSPACE_FILES:
        fpath = workspace / fname
        if fpath.exists():
            content = fpath.read_text()
            if len(content) > 500:
                results.append((f"workspace-{fname.replace('.md','').lower()}", content, str(fpath)))
    return results


def discover_skills(workspace: Path) -> list:
    """Discover skill files."""
    results = []
    skills_dir = workspace / "skills"
    if not skills_dir.exists():
        return results

    for skill_dir in sorted(skills_dir.iterdir()):
        if not skill_dir.is_dir() or skill_dir.name.startswith('_'):
            continue
        skill_file = skill_dir / "SKILL.md"
        if skill_file.exists():
            content = skill_file.read_text()
            if len(content) >= MIN_SKILL_SIZE:
                results.append((f"skill-{skill_dir.name}", content, str(skill_file)))

    return results


def main():
    parser = argparse.ArgumentParser(description="Discover OpenClaw files to optimize")
    parser.add_argument("--workspace", required=True, help="Path to OpenClaw workspace")
    parser.add_argument("--skills-only", action="store_true", help="Only discover skills")
    parser.add_argument("--workspace-only", action="store_true", help="Only discover workspace files")
    parser.add_argument("--min-size", type=int, default=MIN_SKILL_SIZE, help="Min skill size in chars")
    args = parser.parse_args()

    workspace = Path(args.workspace).expanduser()
    if not workspace.exists():
        print(f"ERROR: Workspace not found: {workspace}")
        return

    template = get_codex_template()
    if not template:
        return

    targets = []
    if not args.skills_only:
        targets += discover_workspace(workspace)
    if not args.workspace_only:
        targets += discover_skills(workspace)

    print(f"Discovered {len(targets)} files to optimize\n")

    created = []
    for name, content, source in targets:
        print(f"  {name}: {len(content)} chars", end="")
        result = create_project(name, content, source, template)
        if result:
            created.append(result)
            print(f" → {result['tests']} tests ✓")
        else:
            print(" → skip (exists or too few tests)")

    print(f"\n{'='*50}")
    print(f"Created {len(created)} projects")
    total = sum(c['chars'] for c in created)
    print(f"Total chars to optimize: {total:,}")
    print(f"\nRun: python engine.py")


if __name__ == "__main__":
    main()
