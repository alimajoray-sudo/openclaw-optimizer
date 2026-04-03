# 🦞 OpenClaw Optimizer

**Free, autonomous token optimization for OpenClaw workspaces.**

OpenClaw is powerful — but it burns tokens. Your SOUL.md, AGENTS.md, USER.md, TOOLS.md, SKILL.md files get injected into every conversation, every heartbeat, every sub-agent spawn. A typical workspace consumes 15K-30K tokens just in bootstrap context.

This tool **compresses your workspace files autonomously using free LLMs** — no API costs, no quality loss. It runs experiments, measures accuracy, and only keeps compressions that preserve all critical information.

## How It Works

```
┌─────────────────────────────────────────────────────┐
│  Your workspace files (SOUL.md, skills, docs)       │
│  ↓                                                   │
│  Test Set Generator → extracts facts, URLs,          │
│  commands, rules, paths as verification tests        │
│  ↓                                                   │
│  Compression Engine → free LLMs propose rewrites     │
│  ↓                                                   │
│  Evaluator → keyword accuracy × compression ratio    │
│  ↓                                                   │
│  Only keeps improvements (score must beat baseline)  │
│  ↓                                                   │
│  Dashboard → live progress at localhost:8830          │
└─────────────────────────────────────────────────────┘
```

**Scoring formula:** `accuracy × compression_ratio`
- Baseline (uncompressed) = 1.0
- 50% compression + 100% accuracy = 2.0 (better)
- 30% compression + 95% accuracy = 1.33 (still better than baseline)
- Any accuracy drop below threshold = rejected

## Results

From our production OpenClaw instance (14 agents, 21 skills):

| Target | Original | Compressed | Savings | Accuracy |
|--------|----------|-----------|---------|----------|
| Workspace files (SOUL+AGENTS+USER+TOOLS) | 6.7K chars | 5.7K chars | 15% | 100% |
| Skill: delegation-rules | 14K chars | — | Running | — |
| Skill: office-hours | 16K chars | — | Running | — |
| Skill: self-improving-agent | 20K chars | — | Running | — |

**Cost: $0.00** — Uses only free-tier LLMs (OpenRouter free models, HuggingFace Inference API, xAI free tier).

## Quick Start

### 1. Install

```bash
git clone https://github.com/alimajoray-sudo/openclaw-optimizer.git
cd openclaw-optimizer
pip install -r requirements.txt
```

### 2. Point to your OpenClaw workspace

```bash
# Auto-discover and create optimization projects for all workspace + skill files
python discover.py --workspace ~/.openclaw/workspace-main
```

### 3. Run the optimizer

```bash
# Run continuously (free, uses no paid APIs)
python engine.py

# Run for a specific duration
python engine.py --hours 6

# Run a single project
python engine.py --project skill-delegation-rules
```

### 4. Monitor progress

```bash
# Start the dashboard
python dashboard/dashboard-server.py
# Open http://localhost:8830
```

### 5. Apply compressed files back

```bash
# Preview what would change
python apply.py --preview

# Apply best compressions to your workspace
python apply.py --workspace ~/.openclaw/workspace-main
```

## How the Free LLM Router Works

The optimizer includes a smart model router that cycles through free LLM providers:

| Tier | Provider | Models | Cost |
|------|----------|--------|------|
| 0 | OpenRouter | GLM-4, Gemma 27B, Nemotron | $0.00 |
| 1 | HuggingFace | Qwen 72B, Llama 70B, Qwen Coder 32B | $0.00 |
| 2 | xAI | Grok 3 Mini | ~$0.0005/call |
| 3 | DeepSeek | DeepSeek Chat | ~$0.0004/call |

When one provider rate-limits, it automatically falls to the next tier. Tier 0-1 are completely free. Tier 2-3 cost fractions of a cent per call and are only used as fallback.

## Architecture

```
openclaw-optimizer/
├── engine.py              # Main optimization loop
├── router.py              # Free LLM router (multi-provider)
├── discover.py            # Auto-discover OpenClaw files to optimize
├── apply.py               # Apply compressed results back to workspace
├── create-skill-projects.py  # Generate skill compression projects
├── generator.py           # Continuous test case generator
├── dashboard/
│   ├── dashboard-server.py   # FastAPI dashboard backend
│   └── dashboard.html        # Live monitoring UI
└── projects/              # Auto-generated optimization projects
    ├── workspace-main/    # Your workspace files
    ├── skill-*/           # Individual skill compressions
    └── */
        ├── system-prompt.md     # Current version (mutated)
        ├── test-set.json        # Verification test cases
        ├── agent-codex.py       # Optimizer agent
        ├── meta.json            # Project metadata
        ├── best/                # Best result so far
        └── experiments.jsonl    # Experiment history
```

## What Gets Optimized

### Workspace Files
- **SOUL.md** — Agent personality and behavior rules
- **AGENTS.md** — Agent configuration and delegation
- **USER.md** — User profile and preferences
- **TOOLS.md** — Service endpoints and tool configs
- **HEARTBEAT.md** — Health check instructions
- **MEMORY.md** — Core memory index

### Skill Files
- **SKILL.md** — Every skill >5KB gets its own optimization project
- Preserves all commands, URLs, rules, paths, env vars
- Only compresses prose and redundant instructions

### What It Does NOT Touch
- Code files (*.py, *.js, etc.)
- Config files (*.json, *.yaml)
- Memory files (daily logs, active-tasks)

## Token Savings Estimate

| OpenClaw Setup | Bootstrap Tokens | After Optimization | Monthly Savings* |
|----------------|-----------------|-------------------|-----------------|
| Small (3 agents, 5 skills) | ~15K | ~11K | ~120K tokens |
| Medium (10 agents, 15 skills) | ~35K | ~25K | ~300K tokens |
| Large (20+ agents, 30+ skills) | ~60K+ | ~42K | ~540K tokens |

*Based on ~30 conversations/day with heartbeats and sub-agent spawns.

## Environment Variables

```bash
# Optional — only needed for Tier 2+ fallback
export OPENROUTER_API_KEY=sk-or-...     # Free tier available
export XAI_API_KEY=xai-...              # For Grok fallback
export DEEPSEEK_API_KEY=sk-...          # For DeepSeek fallback
export HF_TOKEN=hf_...                  # HuggingFace (500K free/month)
```

No API keys required for basic operation — OpenRouter free tier works without authentication for many models.

## Contributing

1. Fork the repo
2. Add new optimization strategies in `projects/`
3. Submit test results showing accuracy + compression gains
4. PRs welcome for new free LLM provider support

## License

MIT — Free to use, modify, and distribute.

---

Built by [@alimajoray-sudo](https://github.com/alimajoray-sudo) • Powered by [OpenClaw](https://github.com/openclaw/openclaw)
