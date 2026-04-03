#!/usr/bin/env python3
"""
apply.py — Apply optimized compressions back to your OpenClaw workspace.

Usage:
    python apply.py --preview                              # Show what would change
    python apply.py --workspace ~/.openclaw/workspace-main # Apply changes
    python apply.py --workspace ~/.openclaw/workspace-main --min-score 1.2  # Only apply if 20%+ better
"""
import argparse
import json
import os
import shutil
from pathlib import Path
from datetime import datetime

PROJECTS_DIR = Path(__file__).parent / "projects"
BACKUP_DIR = Path(__file__).parent / "backups"


def get_projects_with_results() -> list:
    """Find projects that have improvements."""
    results = []
    for proj_dir in sorted(PROJECTS_DIR.iterdir()):
        if not proj_dir.is_dir():
            continue
        best_score_file = proj_dir / "best" / "score.txt"
        best_prompt_file = proj_dir / "best" / "system-prompt.md"
        meta_file = proj_dir / "meta.json"

        if not all(f.exists() for f in [best_score_file, best_prompt_file, meta_file]):
            continue

        try:
            score = float(best_score_file.read_text().strip())
        except:
            continue

        if score <= 1.0:  # No improvement over baseline
            continue

        try:
            meta = json.loads(meta_file.read_text())
        except:
            continue

        original_chars = meta.get("original_chars", 0)
        best_chars = len(best_prompt_file.read_text())
        compression = round((1 - best_chars / original_chars) * 100, 1) if original_chars > 0 else 0

        results.append({
            "name": proj_dir.name,
            "source": meta.get("source", ""),
            "score": score,
            "original_chars": original_chars,
            "best_chars": best_chars,
            "compression_pct": compression,
            "best_file": best_prompt_file,
        })

    return results


def preview(results: list):
    """Show what would be applied."""
    if not results:
        print("No improvements found (all scores ≤ 1.0).")
        print("Run the optimizer longer: python engine.py")
        return

    print(f"{'Project':<35} {'Score':>6} {'Original':>9} {'Compressed':>11} {'Savings':>8}")
    print("-" * 75)

    total_original = 0
    total_compressed = 0

    for r in results:
        total_original += r["original_chars"]
        total_compressed += r["best_chars"]
        print(f"{r['name']:<35} {r['score']:>6.2f} {r['original_chars']:>8,} {r['best_chars']:>10,} {r['compression_pct']:>7.1f}%")

    total_savings = round((1 - total_compressed / total_original) * 100, 1) if total_original > 0 else 0
    print("-" * 75)
    print(f"{'TOTAL':<35} {'':>6} {total_original:>8,} {total_compressed:>10,} {total_savings:>7.1f}%")
    print(f"\nEstimated token savings per conversation: ~{(total_original - total_compressed) // 4} tokens")


def apply(results: list, workspace: Path, min_score: float, dry_run: bool = False):
    """Apply compressed files back to workspace."""
    if not results:
        print("Nothing to apply.")
        return

    # Filter by min score
    applicable = [r for r in results if r["score"] >= min_score]
    if not applicable:
        print(f"No projects meet minimum score threshold ({min_score}).")
        return

    # Create backup
    backup_dir = BACKUP_DIR / datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir.mkdir(parents=True, exist_ok=True)

    applied = 0
    for r in applicable:
        source_path = Path(r["source"])
        if not source_path.exists():
            print(f"  SKIP {r['name']}: source not found ({r['source']})")
            continue

        # Backup original
        backup_file = backup_dir / source_path.name
        if not dry_run:
            shutil.copy2(source_path, backup_file)

        # Apply compressed version
        best_content = r["best_file"].read_text()
        if not dry_run:
            source_path.write_text(best_content)

        print(f"  ✓ {r['name']}: {r['original_chars']:,} → {r['best_chars']:,} chars ({r['compression_pct']}% saved)")
        applied += 1

    print(f"\nApplied {applied} optimizations. Backups in: {backup_dir}")
    if not dry_run:
        print("Restart OpenClaw to use the optimized files.")


def main():
    parser = argparse.ArgumentParser(description="Apply optimized compressions to OpenClaw workspace")
    parser.add_argument("--workspace", help="Path to OpenClaw workspace (required to apply)")
    parser.add_argument("--preview", action="store_true", help="Preview changes without applying")
    parser.add_argument("--min-score", type=float, default=1.1, help="Minimum score to apply (default: 1.1 = 10%+ improvement)")
    parser.add_argument("--dry-run", action="store_true", help="Show what would happen without writing")
    args = parser.parse_args()

    results = get_projects_with_results()

    if args.preview or not args.workspace:
        preview(results)
        if not args.workspace and not args.preview:
            print("\nTo apply: python apply.py --workspace ~/.openclaw/workspace-main")
        return

    workspace = Path(args.workspace).expanduser()
    if not workspace.exists():
        print(f"ERROR: Workspace not found: {workspace}")
        return

    apply(results, workspace, args.min_score, args.dry_run)


if __name__ == "__main__":
    main()
