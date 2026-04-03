#!/usr/bin/env python3
"""
router.py — Smart model router for autoresearch engine.

Manages a pool of FREE LLM providers only.
Tracks rate limits, auto-switches on exhaustion, enforces daily budget.

Priority: NVIDIA NIM free → OpenRouter free → error

All providers are genuinely free (free tier / free credits / :free models).
For paid providers (xAI, DeepSeek), set env vars and uncomment in MODEL_POOL.
"""
import json
import os
import time
import urllib.request
import urllib.error
from datetime import datetime, date

# ── Keys ─────────────────────────────────────────────────────────────
OPENROUTER_KEY = os.environ.get(
    "OPENROUTER_API_KEY",
    ""
)
DEEPSEEK_KEY = os.environ.get(
    "DEEPSEEK_API_KEY",
    ""
)
XAI_KEY = os.environ.get(
    "XAI_API_KEY",
    ""
)
HF_TOKEN = os.environ.get(
    "HF_TOKEN",
    ""
)
NVIDIA_KEY = os.environ.get(
    "NVIDIA_API_KEY",
    ""
)

# ── Paths ─────────────────────────────────────────────────────────────
STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state")
os.makedirs(STATE_DIR, exist_ok=True)
QUOTA_FILE = os.path.join(STATE_DIR, "quota.json")
BUDGET_FILE = os.path.join(STATE_DIR, "budget.json")

# ── Budget ────────────────────────────────────────────────────────────
DAILY_BUDGET_USD = 0.50

# ── Model Pool ────────────────────────────────────────────────────────
# (provider, model_id, roles, tier, cost_per_1k_output_tokens_usd)
# roles: "mutator" | "evaluator" | "both"
MODEL_POOL = [
    # Tier 0 — NVIDIA NIM: fast evaluator (short prompts, free credits, no shared rate limits)
    ("nvidia", "meta/llama-3.1-8b-instruct",                       "evaluator", 0, 0.0),
    ("nvidia", "meta/llama-3.2-3b-instruct",                       "evaluator", 0, 0.0),
    ("nvidia", "google/gemma-3-12b-it",                            "evaluator", 0, 0.0),
    ("nvidia", "mistralai/mixtral-8x7b-instruct-v0.1",             "evaluator", 0, 0.0),
    # ── Optional paid providers (uncomment if you have API keys) ──────────
    # ("xai", "grok-3-mini-fast",                                   "mutator",   0, 0.00025),
    # ("xai", "grok-3-mini",                                        "both",      1, 0.0005),
    # ("deepseek", "deepseek-chat",                                 "both",      2, 0.00042),
    # ── Free providers ───────────────────────────────────────────────
    # Tier 0 — HuggingFace Inference API: free serverless models
    ("hf", "Qwen/Qwen2.5-72B-Instruct",                            "both",      0, 0.0),
    ("hf", "meta-llama/Llama-3.3-70B-Instruct",                    "both",      0, 0.0),
    ("hf", "mistralai/Mixtral-8x22B-Instruct-v0.1",                "both",      0, 0.0),
    ("hf", "google/gemma-2-27b-it",                                 "evaluator", 0, 0.0),
    # Tier 0 — OpenRouter free: fallback (shared rate limits)
    ("openrouter", "meta-llama/llama-3.3-70b-instruct:free",       "both",      0, 0.0),
    ("openrouter", "google/gemma-3-27b-it:free",                   "both",      0, 0.0),
    ("openrouter", "qwen/qwen3-coder:free",                        "both",      0, 0.0),
    ("openrouter", "nousresearch/hermes-3-llama-3.1-405b:free",    "both",      0, 0.0),
    ("openrouter", "arcee-ai/trinity-large-preview:free",          "both",      0, 0.0),
    ("openrouter", "nvidia/nemotron-3-nano-30b-a3b:free",          "both",      0, 0.0),
    ("openrouter", "nvidia/nemotron-3-super-120b-a12b:free",       "both",      0, 0.0),
    ("openrouter", "google/gemma-3-12b-it:free",                   "both",      0, 0.0),
    ("openrouter", "z-ai/glm-4.5-air:free",                        "both",      0, 0.0),
    ("openrouter", "arcee-ai/trinity-mini:free",                   "evaluator", 0, 0.0),
]

# Models to permanently skip (guardrail/404 on this account)
BLOCKED_MODELS = {
    "openai/gpt-oss-120b:free",
    "openai/gpt-oss-20b:free",
    "minimax/minimax-m2.5:free",
    "stepfun/step-3.5-flash:free",
}

OPENROUTER_BASE = "https://openrouter.ai/api/v1/chat/completions"
DEEPSEEK_BASE   = "https://api.deepseek.com/chat/completions"
XAI_BASE        = "https://api.x.ai/v1/chat/completions"
HF_BASE         = "https://router.huggingface.co/{model}/v1/chat/completions"
NVIDIA_BASE     = "https://integrate.api.nvidia.com/v1/chat/completions"

# Per-model RPM windows
MODEL_RPM = {
    "openrouter": 8,   # conservative: 429 = skip, not wait
    "hf":         10,
    "deepseek":   30,
    "xai":        60,
    "nvidia":     20,  # NIM free tier: generous but not unlimited
}


def _load_json(path, default):
    if os.path.exists(path):
        try:
            return json.load(open(path))
        except Exception:
            pass
    return default


def _save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


# ── Quota Tracker ────────────────────────────────────────────────────

class QuotaTracker:
    """Tracks per-model request windows to avoid rate limits."""

    def __init__(self):
        self.state = _load_json(QUOTA_FILE, {})
        self._clean_old()

    def _clean_old(self):
        cutoff = time.time() - 120  # 2-min rolling window
        for k in list(self.state.keys()):
            self.state[k] = [t for t in self.state[k] if t > cutoff]
        _save_json(QUOTA_FILE, self.state)

    def record(self, provider, model_id):
        key = f"{provider}/{model_id}"
        self.state.setdefault(key, []).append(time.time())
        _save_json(QUOTA_FILE, self.state)

    def recent_count(self, provider, model_id, window_s=60):
        key = f"{provider}/{model_id}"
        cutoff = time.time() - window_s
        return sum(1 for t in self.state.get(key, []) if t > cutoff)

    def is_available(self, provider, model_id):
        rpm_limit = MODEL_RPM.get(provider, 15)
        recent = self.recent_count(provider, model_id)
        return recent < rpm_limit

    def seconds_until_available(self, provider, model_id):
        key = f"{provider}/{model_id}"
        rpm_limit = MODEL_RPM.get(provider, 15)
        times = sorted(self.state.get(key, []))
        if len(times) < rpm_limit:
            return 0
        # oldest request that fills the window
        oldest = times[-rpm_limit]
        reset_at = oldest + 60
        wait = max(0, reset_at - time.time())
        return wait

    def provider_429_count(self, provider, window_s=120):
        """Count how many distinct models from a provider have full 429 buckets."""
        return sum(
            1 for (p, m, *_) in MODEL_POOL
            if p == provider and self.seconds_until_available(p, m) > 0
        )

    def all_provider_models_blocked(self, provider, window_s=120):
        """True if every model from this provider is currently rate-limited."""
        models = [(p, m) for (p, m, *_) in MODEL_POOL if p == provider]
        if not models:
            return False
        return all(self.seconds_until_available(p, m) > 0 for p, m in models)


# ── Budget Tracker ────────────────────────────────────────────────────

class BudgetTracker:
    def __init__(self):
        self.state = _load_json(BUDGET_FILE, {})

    def today_spend(self):
        return self.state.get(str(date.today()), 0.0)

    def record_cost(self, cost_usd):
        today = str(date.today())
        self.state[today] = self.state.get(today, 0.0) + cost_usd
        _save_json(BUDGET_FILE, self.state)

    def budget_ok(self, tier):
        spend = self.today_spend()
        if spend >= DAILY_BUDGET_USD and tier >= 2:
            return False
        return True

    def budget_status(self):
        spend = self.today_spend()
        return {
            "today_usd": round(spend, 4),
            "cap_usd": DAILY_BUDGET_USD,
            "remaining_usd": round(max(0, DAILY_BUDGET_USD - spend), 4),
            "ok": spend < DAILY_BUDGET_USD,
        }


# ── Main Router ───────────────────────────────────────────────────────

class ModelRouter:
    def __init__(self, verbose=True):
        self.quota = QuotaTracker()
        self.budget = BudgetTracker()
        self.verbose = verbose

    def _log(self, msg):
        if self.verbose:
            ts = datetime.now().strftime("%H:%M:%S")
            print(f"[{ts}] [router] {msg}", flush=True)

    def get_model(self, role="both"):
        """Return (provider, model_id) for available model matching role."""
        for provider, model_id, roles, tier, cost in MODEL_POOL:
            if role != "both" and roles != "both" and roles != role:
                continue
            if not self.budget.budget_ok(tier):
                self._log(f"Budget cap hit — skipping tier {tier} ({model_id})")
                continue
            if self.quota.is_available(provider, model_id):
                return provider, model_id
        return None, None

    def get_wait_seconds(self):
        """How long until any model is available (if all rate-limited)."""
        waits = []
        for provider, model_id, roles, tier, cost in MODEL_POOL:
            if not self.budget.budget_ok(tier):
                continue
            w = self.quota.seconds_until_available(provider, model_id)
            waits.append(w)
        return min(waits) if waits else 60

    def complete(self, messages, role="both", max_tokens=2048, temperature=0.8,
                 system=None, timeout=90,
                 or_timeout=30,
                 nvidia_timeout=120):
        """
        Call LLM with automatic provider selection + fallback.

        Args:
            messages: list of {role, content} dicts, OR a str (user prompt)
            role: "mutator" | "evaluator" | "both"
            system: optional system prompt string (prepended if messages is list)
            ...

        Returns:
            (text: str, model_used: str, cost_usd: float)
        """
        if isinstance(messages, str):
            msgs = [{"role": "user", "content": messages}]
        else:
            msgs = list(messages)

        if system:
            msgs = [{"role": "system", "content": system}] + msgs

        # Track which providers are fully saturated (skip all their models at once)
        _blocked_providers = set()

        # Try each model in pool order
        tried = []
        for provider, model_id, roles, tier, cost_per_1k in MODEL_POOL:
            if role != "both" and roles != "both" and roles != role:
                continue
            if not self.budget.budget_ok(tier):
                continue

            # Skip permanently blocked models (guardrail 404s etc)
            if model_id in BLOCKED_MODELS:
                continue

            # If entire provider is rate-limited, skip all its remaining models fast
            if provider in _blocked_providers:
                tried.append(f"{model_id}(provider_blocked)")
                continue

            # Skip immediately if rate-limited — don't wait, fall through to next model
            wait = self.quota.seconds_until_available(provider, model_id)
            if wait > 0:
                tried.append(f"{model_id}(wait={wait:.0f}s)")
                # If all models on this provider are now blocked, mark provider
                if self.quota.all_provider_models_blocked(provider):
                    _blocked_providers.add(provider)
                    self._log(f"Provider fully rate-limited: {provider} — skipping remaining")
                continue

            try:
                if provider == "openrouter":
                    _timeout = or_timeout
                elif provider == "nvidia":
                    _timeout = nvidia_timeout
                else:
                    _timeout = timeout
                text, actual_tokens = self._call(
                    provider, model_id, msgs, max_tokens, temperature, _timeout
                )
                cost = (actual_tokens / 1000) * cost_per_1k
                self.budget.record_cost(cost)
                self.quota.record(provider, model_id)
                self._log(f"✓ {model_id} ({actual_tokens} tok, ${cost:.5f})")
                return text, model_id, cost

            except RateLimitError as e:
                self._log(f"Rate limited: {model_id} — {e}")
                # Mark as exhausted for ~1 min (fill RPM bucket with fake timestamps)
                for _ in range(MODEL_RPM.get(provider, 15)):
                    self.quota.record(provider, model_id)
                tried.append(f"{model_id}(429)")
                # Check if entire provider is now blocked
                if self.quota.all_provider_models_blocked(provider):
                    _blocked_providers.add(provider)
                    self._log(f"Provider fully rate-limited: {provider} — skipping remaining")
                continue

            except ModelError as e:
                err_str = str(e)
                self._log(f"Model error: {model_id} — {e}")
                tried.append(f"{model_id}(err)")
                # On 404: permanently block this model for the session
                if "404" in err_str or "No endpoints" in err_str or "guardrail" in err_str:
                    BLOCKED_MODELS.add(model_id)
                # On timeout: apply same cooldown as 429 so we don't retry immediately
                elif "timeout" in err_str.lower():
                    for _ in range(MODEL_RPM.get(provider, 15)):
                        self.quota.record(provider, model_id)
                    if self.quota.all_provider_models_blocked(provider):
                        _blocked_providers.add(provider)
                        self._log(f"Provider fully rate-limited (timeout): {provider}")
                continue

        raise AllModelsExhausted(f"All models tried: {tried}")

    def _call(self, provider, model_id, messages, max_tokens, temperature, timeout):
        if provider == "openrouter":
            return self._call_openrouter(model_id, messages, max_tokens, temperature, timeout)
        elif provider == "hf":
            return self._call_hf(model_id, messages, max_tokens, temperature, timeout)
        elif provider == "xai":
            return self._call_xai(model_id, messages, max_tokens, temperature, timeout)
        elif provider == "deepseek":
            return self._call_deepseek(model_id, messages, max_tokens, temperature, timeout)
        elif provider == "nvidia":
            return self._call_nvidia(model_id, messages, max_tokens, temperature, timeout)
        raise ModelError(f"Unknown provider: {provider}")

    def _call_openrouter(self, model_id, messages, max_tokens, temperature, timeout):
        payload = json.dumps({
            "model": model_id,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }).encode()

        req = urllib.request.Request(
            OPENROUTER_BASE,
            data=payload,
            headers={
                "Authorization": f"Bearer {OPENROUTER_KEY}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/alimajoray-sudo/free-autoresearch",
                "X-Title": "Free-Autoresearch",
            },
            method="POST",
        )
        return self._http_call(req, timeout)

    def _call_hf(self, model_id, messages, max_tokens, temperature, timeout):
        try:
            from huggingface_hub import InferenceClient as _HFClient
        except ImportError:
            raise ModelError("huggingface_hub not installed; run: pip install huggingface-hub")

        import threading
        client = _HFClient(api_key=HF_TOKEN)
        result_holder = [None]
        error_holder = [None]

        def _hf_call():
            try:
                result_holder[0] = client.chat.completions.create(
                    model=model_id,
                    messages=messages,
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
            except Exception as e:
                error_holder[0] = e

        t = threading.Thread(target=_hf_call, daemon=True)
        t.start()
        t.join(timeout=timeout)
        if t.is_alive():
            raise ModelError(f"HuggingFace timeout after {timeout}s on {model_id}")
        if error_holder[0] is not None:
            raise error_holder[0]
        result = result_holder[0]
        try:
            text = result.choices[0].message.content or ""
            tokens = getattr(result.usage, "completion_tokens", max(1, len(text) // 4))
            return text, tokens
        except Exception as e:
            err = str(e)
            if "429" in err or "rate" in err.lower():
                raise RateLimitError(err)
            raise ModelError(err)

    def _call_xai(self, model_id, messages, max_tokens, temperature, timeout):
        payload = json.dumps({
            "model": model_id,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }).encode()
        req = urllib.request.Request(
            XAI_BASE,
            data=payload,
            headers={
                "Authorization": f"Bearer {XAI_KEY}",
                "Content-Type": "application/json",
                "User-Agent": "python-httpx/0.27.0",
            },
            method="POST",
        )
        return self._http_call(req, timeout)

    def _call_deepseek(self, model_id, messages, max_tokens, temperature, timeout):
        payload = json.dumps({
            "model": model_id,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }).encode()

        req = urllib.request.Request(
            DEEPSEEK_BASE,
            data=payload,
            headers={
                "Authorization": f"Bearer {DEEPSEEK_KEY}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        return self._http_call(req, timeout)

    def _call_nvidia(self, model_id, messages, max_tokens, temperature, timeout):
        payload = json.dumps({
            "model": model_id,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }).encode()
        req = urllib.request.Request(
            NVIDIA_BASE,
            data=payload,
            headers={
                "Authorization": f"Bearer {NVIDIA_KEY}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        return self._http_call(req, timeout)

    def _http_call(self, req, timeout):
        import threading as _threading
        result_holder = [None]
        error_holder = [None]

        def _do_call():
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    result_holder[0] = json.load(resp)
            except urllib.error.HTTPError as e:
                error_holder[0] = e
            except Exception as e:
                error_holder[0] = e

        t = _threading.Thread(target=_do_call, daemon=True)
        t.start()
        t.join(timeout=timeout + 5)  # +5s grace over socket timeout
        if t.is_alive():
            raise ModelError(f"HTTP timeout after {timeout}s")

        if error_holder[0] is not None:
            e = error_holder[0]
            if isinstance(e, urllib.error.HTTPError):
                body = e.read().decode(errors="replace")
                if e.code == 429:
                    raise RateLimitError(f"429: {body[:200]}")
                raise ModelError(f"HTTP {e.code}: {body[:300]}")
            raise ModelError(str(e))

        data = result_holder[0]

        # Parse OpenAI-compatible response
        try:
            msg = data["choices"][0]["message"]
            text = msg.get("content") or ""
            # Some models (GLM, thinking models) put output in reasoning field
            if not text.strip():
                text = msg.get("reasoning_content") or msg.get("reasoning") or text
            tokens = data.get("usage", {}).get("completion_tokens", max(1, len(text) // 4))
            return text, tokens
        except (KeyError, IndexError) as e:
            raise ModelError(f"Bad response shape: {e} | {str(data)[:200]}")


# ── Exceptions ────────────────────────────────────────────────────────

class RateLimitError(Exception):
    pass

class ModelError(Exception):
    pass

class AllModelsExhausted(Exception):
    pass


# ── CLI Test ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    router = ModelRouter(verbose=True)

    print("=== Budget Status ===")
    print(json.dumps(router.budget.budget_status(), indent=2))

    print("\n=== Model Availability ===")
    for provider, model_id, roles, tier, cost in MODEL_POOL:
        avail = router.quota.is_available(provider, model_id)
        wait = router.quota.seconds_until_available(provider, model_id)
        print(f"  {'✓' if avail else '✗'} [{tier}] {model_id:<50} wait={wait:.0f}s")

    if "--test" in sys.argv:
        print("\n=== Live Test Call ===")
        text, model, cost = router.complete(
            "Say 'ROUTER_OK' and nothing else.",
            role="both",
            max_tokens=20,
            temperature=0.1
        )
        print(f"Response: {text!r}")
        print(f"Model: {model} | Cost: ${cost:.5f}")
