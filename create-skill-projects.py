#!/usr/bin/env python3
"""
Create autoresearch projects for compressing large SKILL.md files.
Generates test sets by extracting key facts, commands, URLs, and rules from each skill.
"""
import json
import os
import re
import shutil
from pathlib import Path
from datetime import datetime

SKILLS_DIR = Path.home() / ".openclaw/workspace-main/skills"
PROJECTS_DIR = Path(__file__).parent / "projects"
TEMPLATE_CODEX = Path(__file__).parent / "projects/system-prompt-compression/agent-codex.py"

# Skills worth compressing (>5KB, active, not archived)
MIN_SIZE = 5000

def extract_test_cases(content: str, skill_name: str) -> list:
    """Extract hard test cases from skill content — specific facts, not vague questions."""
    tests = []
    
    # 1. Extract all URLs
    urls = re.findall(r'https?://[^\s\)\"\'>`]+', content)
    for url in urls[:3]:
        tests.append({
            "question": f"What URL is mentioned for {url.split('/')[2]}?",
            "expected_keywords": [url[:60]],
            "category": "urls"
        })
    
    # 2. Extract all code commands (```bash blocks)
    code_blocks = re.findall(r'```(?:bash|sh|shell)?\n(.*?)```', content, re.DOTALL)
    for block in code_blocks[:4]:
        lines = [l.strip() for l in block.strip().split('\n') if l.strip() and not l.strip().startswith('#')]
        for line in lines[:1]:
            # Extract the command name
            cmd = line.split()[0] if line.split() else ''
            if cmd and len(cmd) > 2:
                tests.append({
                    "question": f"What command/script should be used for: {line[:50]}?",
                    "expected_keywords": [cmd] + line.split()[:3],
                    "category": "commands"
                })
    
    # 3. Extract port numbers
    ports = re.findall(r'(?:port|PORT)\s*[:=]?\s*(\d{4,5})', content, re.IGNORECASE)
    ports += re.findall(r'localhost:(\d{4,5})', content)
    for port in set(ports):
        tests.append({
            "question": f"What service runs on port {port}?",
            "expected_keywords": [port],
            "category": "config"
        })
    
    # 4. Extract specific rules/constraints (lines with MUST, NEVER, ALWAYS, DO NOT)
    rules = re.findall(r'^.*(?:MUST|NEVER|ALWAYS|DO NOT|IMPORTANT|CRITICAL|WARNING).*$', content, re.MULTILINE | re.IGNORECASE)
    for rule in rules[:3]:
        # Extract key words from the rule
        words = [w for w in rule.split() if len(w) > 3 and w.upper() not in ('MUST','NEVER','ALWAYS','THAT','THIS','WITH','FROM','HAVE')]
        if len(words) >= 2:
            tests.append({
                "question": f"What rule says: {rule.strip()[:60]}?",
                "expected_keywords": words[:5],
                "category": "rules"
            })
    
    # 5. Extract file paths
    paths = re.findall(r'[`"]([~/][a-zA-Z0-9_./-]{10,})[`"]', content)
    for p in paths[:3]:
        tests.append({
            "question": f"What is the path for {os.path.basename(p)}?",
            "expected_keywords": [p],
            "category": "paths"
        })
    
    # 6. Extract env vars
    env_vars = re.findall(r'\b([A-Z][A-Z0-9_]{3,})\b', content)
    env_vars = [v for v in set(env_vars) if v not in ('SKILL', 'HTTP', 'POST', 'JSON', 'TRUE', 'FALSE', 'NULL', 'NONE', 'TODO', 'NOTE')]
    for var in env_vars[:3]:
        tests.append({
            "question": f"What is {var} used for?",
            "expected_keywords": [var],
            "category": "config"
        })
    
    # 7. Extract section headers as topic coverage tests
    headers = re.findall(r'^#{1,3}\s+(.+)$', content, re.MULTILINE)
    for h in headers[:5]:
        words = [w for w in h.split() if len(w) > 3]
        if words:
            tests.append({
                "question": f"What does the '{h.strip()}' section cover?",
                "expected_keywords": words[:4],
                "category": "coverage"
            })
    
    # Deduplicate and limit
    seen = set()
    unique = []
    for t in tests:
        key = t["question"][:40]
        if key not in seen:
            seen.add(key)
            unique.append(t)
    
    return unique[:25]  # max 25 tests per skill


def create_project(skill_name: str, skill_path: Path):
    """Create a compression project for a skill."""
    content = skill_path.read_text()
    if len(content) < MIN_SIZE:
        return None
    
    proj_dir = PROJECTS_DIR / f"skill-{skill_name}"
    proj_dir.mkdir(parents=True, exist_ok=True)
    
    # Copy skill content as target
    (proj_dir / "system-prompt.md").write_text(content)
    
    # Generate test cases
    tests = extract_test_cases(content, skill_name)
    if len(tests) < 5:
        print(f"  Skipping {skill_name}: only {len(tests)} test cases extracted")
        shutil.rmtree(proj_dir)
        return None
    
    (proj_dir / "test-set.json").write_text(json.dumps(tests, indent=2))
    
    # Create meta
    meta = {
        "skill": skill_name,
        "source": str(skill_path),
        "original_chars": len(content),
        "test_cases": len(tests),
        "created": datetime.now().isoformat()
    }
    (proj_dir / "meta.json").write_text(json.dumps(meta, indent=2))
    
    # Copy and adapt agent-codex.py
    codex_src = TEMPLATE_CODEX.read_text()
    # Fix STATUS_FILE
    codex_adapted = re.sub(
        r'STATUS_FILE\s*=\s*f?["\'].*?["\']',
        f'STATUS_FILE = f"/tmp/skill-{skill_name}-status.json"',
        codex_src
    )
    (proj_dir / "agent-codex.py").write_text(codex_adapted)
    
    # Create best dir with initial copy
    best_dir = proj_dir / "best"
    best_dir.mkdir(exist_ok=True)
    (best_dir / "system-prompt.md").write_text(content)
    (best_dir / "score.txt").write_text("0.0000\n")
    
    # Empty experiments log
    (proj_dir / "experiments.jsonl").write_text("")
    
    return {
        "name": skill_name,
        "chars": len(content),
        "tests": len(tests)
    }


def main():
    created = []
    
    for skill_dir in sorted(SKILLS_DIR.iterdir()):
        if not skill_dir.is_dir():
            continue
        if skill_dir.name.startswith('_'):  # skip _archived
            continue
        
        skill_file = skill_dir / "SKILL.md"
        if not skill_file.exists():
            continue
        
        content = skill_file.read_text()
        if len(content) < MIN_SIZE:
            continue
        
        # Skip if project already exists
        proj_dir = PROJECTS_DIR / f"skill-{skill_dir.name}"
        if proj_dir.exists():
            print(f"  Skip {skill_dir.name}: project exists")
            continue
        
        print(f"Creating project for: {skill_dir.name} ({len(content)} chars)")
        result = create_project(skill_dir.name, skill_file)
        if result:
            created.append(result)
            print(f"  ✓ {result['tests']} tests, {result['chars']} chars")
    
    print(f"\n{'='*50}")
    print(f"Created {len(created)} new skill projects:")
    for c in created:
        print(f"  {c['name']}: {c['chars']} chars, {c['tests']} tests")
    
    total_chars = sum(c['chars'] for c in created)
    print(f"\nTotal chars to compress: {total_chars:,}")


if __name__ == "__main__":
    main()
