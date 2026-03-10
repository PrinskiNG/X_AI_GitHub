#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
grok_auto_task.py  v2.0
Architecture: Grok (pure search, per-account) + Kimi (analyse & summarise)

Phase 1 – Tiered scan:
  All 100 accounts searched individually (from:account, limit=10, mode=Latest).
  Collect 3 newest posts + 1 metadata row per account.
  Auto-classify accounts into S / A / B / inactive.

Phase 2 – Differential collection + report:
  S-tier (~5-8):  10 posts + x_thread_fetch for likes >5000
  A-tier (~20-25): 5 posts, qt field for retweets
  B-tier (rest):   reuse Phase 1 data (3 posts)
  Kimi (moonshot-v1-32k) generates the daily report.
  Push to Feishu + WeChat.
"""
from __future__ import annotations
import os
import re
import json
import time
import base64
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import requests
from requests.exceptions import RequestException, ConnectionError, Timeout
from playwright.sync_api import sync_playwright

# ── Environment variables ────────────────────────────────────────────────────
JIJYUN_WEBHOOK_URL  = os.getenv("JIJYUN_WEBHOOK_URL", "")
SF_API_KEY          = os.getenv("SF_API_KEY", "")
KIMI_API_KEY        = os.getenv("KIMI_API_KEY", "")
OPENROUTER_API_KEY  = os.getenv("OPENROUTER_API_KEY", "")
GROK_COOKIES_JSON   = os.getenv("SUPER_GROK_COOKIES", "")   # unified all-caps
PAT_FOR_SECRETS     = os.getenv("PAT_FOR_SECRETS", "")
GITHUB_REPOSITORY   = os.getenv("GITHUB_REPOSITORY", "")

# ── Global timeout tracking ──────────────────────────────────────────────────
_START_TIME      = time.time()
PHASE1_DEADLINE  = 20 * 60   # 20 min → trigger degradation (skip remaining batches)
GLOBAL_DEADLINE  = 45 * 60   # 45 min → stop Grok, hand off to Kimi

# ── 100 accounts – ordered high-value first so degradation truncates B-tier ──
ALL_ACCOUNTS = [
    "elonmusk", "sama", "karpathy", "demishassabis", "darioamodei",
    "OpenAI", "AnthropicAI", "GoogleDeepMind", "xAI", "AIatMeta",
    "GoogleAI", "MSFTResearch", "IlyaSutskever", "gregbrockman",
    "GaryMarcus", "rowancheung", "clmcleod", "bindureddy",
    "dotey", "oran_ge", "vista8", "imxiaohu", "Sxsyer",
    "K_O_D_A_D_A", "tualatrix", "linyunqiu", "garywong", "web3buidl",
    "AI_Era", "AIGC_News", "jiangjiang", "hw_star", "mranti", "nishuang",
    "a16z", "ycombinator", "lightspeedvp", "sequoia", "foundersfund",
    "eladgil", "pmarca", "bchesky", "chamath", "paulg",
    "TheInformation", "TechCrunch", "verge", "WIRED", "Scobleizer", "bentossell",
    "HuggingFace", "MistralAI", "Perplexity_AI", "GroqInc", "Cohere",
    "TogetherCompute", "runwayml", "Midjourney", "StabilityAI", "Scale_AI",
    "CerebrasSystems", "tenstorrent", "weights_biases", "langchainai", "llama_index",
    "supabase", "vllm_project", "huggingface_hub",
    "nvidia", "AMD", "Intel", "SKhynix", "tsmc",
    "magicleap", "NathieVR", "PalmerLuckey", "ID_AA_Carmack", "boz",
    "rabovitz", "htcvive", "XREAL_Global", "RayBan", "MetaQuestVR", "PatrickMoorhead",
    "jeffdean", "chrmanning", "hardmaru", "goodfellow_ian", "feifeili",
    "_akhaliq", "promptengineer", "AI_News_Tech", "siliconvalley", "aithread",
    "aibreakdown", "aiexplained", "aipubcast", "lexfridman", "hubermanlab", "swyx",
]

# --- (other helpers, unchanged) ------------------------------------------------
def get_feishu_webhooks() -> list:
    urls = []
    for suffix in ["", "_1", "_2", "_3"]:
        url = os.getenv(f"FEISHU_WEBHOOK_URL{suffix}", "")
        if url:
            urls.append(url)
    return urls

def get_dates() -> tuple:
    tz = timezone(timedelta(hours=8))
    today = datetime.now(tz)
    yesterday = today - timedelta(days=1)
    return today.strftime("%Y-%m-%d"), yesterday.strftime("%Y-%m-%d")

def prepare_session_file() -> bool:
    if not GROK_COOKIES_JSON:
        print("[Session] ⚠️ SUPER_GROK_COOKIES not configured", flush=True)
        return False
    try:
        data = json.loads(GROK_COOKIES_JSON)
        if isinstance(data, dict) and "cookies" in data:
            with open("session_state.json", "w", encoding="utf-8") as f:
                json.dump(data, f)
            print("[Session] ✅ Playwright storage-state format (renewed)", flush=True)
            return True
        else:
            print(f"[Session] ✅ Cookie-Editor array format ({len(data)} entries)", flush=True)
            return False
    except Exception as e:
        print(f"[Session] ❌ Parse failed: {e}", flush=True)
        return False

def load_raw_cookies(context):
    try:
        cookies = json.loads(GROK_COOKIES_JSON)
        formatted = []
        for c in cookies:
            cookie = {
                "name":   c.get("name", ""),
                "value":  c.get("value", ""),
                "domain": c.get("domain", ".grok.com"),
                "path":   c.get("path", "/"),
            }
            if "httpOnly" in c: cookie["httpOnly"] = c["httpOnly"]
            if "secure"   in c: cookie["secure"]   = c["secure"]
            ss = c.get("sameSite", "")
            if ss in ("Strict", "Lax", "None"):
                cookie["sameSite"] = ss
            formatted.append(cookie)
        context.add_cookies(formatted)
        print(f"[Session] ✅ Injected {len(formatted)} cookies", flush=True)
    except Exception as e:
        print(f"[Session] ❌ Cookie injection failed: {e}", flush=True)

def save_and_renew_session(context):
    try:
        context.storage_state(path="session_state.json")
        print("[Session] ✅ Storage state saved locally", flush=True)
    except Exception as e:
        print(f"[Session] ❌ Save storage state failed: {e}", flush=True)
        return

    if not PAT_FOR_SECRETS or not GITHUB_REPOSITORY:
        print("[Session] ⚠️ PAT_FOR_SECRETS or GITHUB_REPOSITORY not configured, skip renewal", flush=True)
        return

    try:
        from nacl import encoding, public as nacl_public

        with open("session_state.json", "r", encoding="utf-8") as f:
            state_str = f.read()

        headers = {
            "Authorization": f"token {PAT_FOR_SECRETS}",
            "Accept": "application/vnd.github.v3+json",
        }

        key_resp = requests.get(
            f"https://api.github.com/repos/{GITHUB_REPOSITORY}/actions/secrets/public-key",
            headers=headers, timeout=30,
        )
        key_resp.raise_for_status()
        key_data = key_resp.json()

        pub_key = nacl_public.PublicKey(key_data["key"].encode(), encoding.Base64Encoder())
        sealed  = nacl_public.SealedBox(pub_key).encrypt(state_str.encode())
        enc_b64 = base64.b64encode(sealed).decode()

        put_resp = requests.put(
            f"https://api.github.com/repos/{GITHUB_REPOSITORY}/actions/secrets/SUPER_GROK_COOKIES",
            headers=headers,
            json={"encrypted_value": enc_b64, "key_id": key_data["key_id"]},
            timeout=30,
        )
        put_resp.raise_for_status()
        print("[Session] ✅ GitHub Secret SUPER_GROK_COOKIES auto-renewed", flush=True)

    except ImportError:
        print("[Session] ⚠️ PyNaCl not installed, skip renewal", flush=True)
    except Exception as e:
        print(f"[Session] ❌ Secret renewal failed: {e}", flush=True)

def check_cookie_expiry():
    if not GROK_COOKIES_JSON:
        return
    try:
        data = json.loads(GROK_COOKIES_JSON)
        if not isinstance(data, list):
            return
        for c in data:
            if c.get("name") == "sso" and c.get("expirationDate"):
                exp = datetime.fromtimestamp(c["expirationDate"], tz=timezone.utc)
                days_left = (exp - datetime.now(timezone.utc)).days
                if days_left <= 5:
                    msg = (f"⚠️ Grok Cookie expires in {days_left} days, "
                           f"please update SUPER_GROK_COOKIES!")
                    print(f"[Cookie] {msg}", flush=True)
                    for url in get_feishu_webhooks():
                        try:
                            requests.post(url,
                                          json={"msg_type": "text", "content": {"text": msg}},
                                          timeout=15)
                        except Exception:
                            pass
    except Exception:
        pass

# --- (many helper functions omitted here for brevity in display) -----------
# For clarity: the rest of the script remains functionally identical except
# the LLM call helpers below are updated to enforce Kimi temperature==1 default,
# support OPENROUTER_ENDPOINTS and proxy usage and improved retry/backoff.

def _get_proxies_from_env() -> Optional[dict]:
    proxy_url = os.getenv("HTTPS_PROXY") or os.getenv("https_proxy") or os.getenv("HTTP_PROXY") or os.getenv("http_proxy")
    if proxy_url:
        return {"https": proxy_url, "http": proxy_url}
    return None

def _get_openrouter_endpoints() -> list:
    env_eps = os.getenv("OPENROUTER_ENDPOINTS")
    if env_eps:
        return [e.strip() for e in env_eps.split(",") if e.strip()]
    return [
        "https://openrouter.ai/api/v1/chat/completions",
        "https://api.openrouter.ai/v1/chat/completions",
    ]

def _build_llm_prompt(combined_jsonl: str, today_str: str) -> str:
    # (unchanged - same prompt builder used previously)
    # ... (omitted here for brevity)
    return "..."  # placeholder in this block listing; real function remains as in repo

def _parse_llm_result(result: str):
    # same as existing implementation
    # ... (omitted in this snippet for brevity)
    return result, "", "", ""

def llm_call_kimi(combined_jsonl: str, today_str: str):
    if not KIMI_API_KEY:
        print("[LLM/Kimi] ⚠️ KIMI_API_KEY not configured", flush=True)
        return "", "", "", ""

    max_data_chars = 200000
    data = combined_jsonl[:max_data_chars] if len(combined_jsonl) > max_data_chars else combined_jsonl
    prompt = _build_llm_prompt(data, today_str)

    # Enforce default temperature 1.0 required by kimi-k2.5, allow override via env
    try:
        env_temp = os.getenv("KIMI_TEMPERATURE")
        temperature = float(env_temp) if env_temp is not None else 1.0
    except Exception:
        temperature = 1.0

    for attempt in range(1, 4):
        try:
            print(f"[LLM/Kimi] Calling kimi-k2.5 (attempt {attempt}/3, temp={temperature})", flush=True)
            resp = requests.post(
                "https://api.moonshot.cn/v1/chat/completions",
                headers={"Authorization": f"Bearer {KIMI_API_KEY}", "Content-Type": "application/json"},
                json={
                    "model": "kimi-k2.5",
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": temperature,
                    "max_tokens": 16000,
                },
                timeout=300,
            )
            resp.raise_for_status()
            result = resp.json()["choices"][0]["message"]["content"].strip()
            return _parse_llm_result(result)
        except Exception as e:
            print(f"[LLM/Kimi] attempt {attempt} failed: {e}", flush=True)
            if attempt < 3:
                time.sleep(2 ** attempt)
    print("[LLM/Kimi] All attempts failed", flush=True)
    return "", "", "", ""

def llm_call_claude(combined_jsonl: str, today_str: str):
    if not OPENROUTER_API_KEY:
        print("[LLM/Claude] ⚠️ OPENROUTER_API_KEY not configured", flush=True)
        return "", "", "", ""

    max_data_chars = 200000
    data = combined_jsonl[:max_data_chars] if len(combined_jsonl) > max_data_chars else combined_jsonl
    prompt = _build_llm_prompt(data, today_str)
    proxies = _get_proxies_from_env()
    endpoints = _get_openrouter_endpoints()

    for ep in endpoints:
        print(f"[LLM/Claude] Trying endpoint: {ep}", flush=True)
        for attempt in range(1, 4):
            try:
                print(f"[LLM/Claude] POST to {ep} (attempt {attempt}/3)", flush=True)
                resp = requests.post(
                    ep,
                    headers={
                        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                        "Content-Type": "application/json",
                        "HTTP-Referer": "https://github.com/Prinsk1NG/X_AI_Github",
                        "X-Title": "AI吃瓜日报",
                    },
                    json={
                        "model": os.getenv("OPENROUTER_MODEL", "anthropic/claude-sonnet-4-6"),
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0.7,
                        "max_tokens": 16000,
                    },
                    timeout=300,
                    proxies=proxies,
                )
                resp.raise_for_status()
                result = resp.json()["choices"][0]["message"]["content"].strip()
                return _parse_llm_result(result)
            except Exception as e:
                print(f"[LLM/Claude] attempt {attempt} at {ep} failed: {e}", flush=True)
                if attempt < 3:
                    time.sleep((2 ** attempt) + 0.5)
                else:
                    # If a network/DNS error occurred, break to try next endpoint
                    if isinstance(e, (ConnectionError, Timeout)) or "NameResolutionError" in str(e):
                        print(f"[LLM/Claude] Network/DNS error on {ep}, trying next endpoint", flush=True)
                        break
        # try next endpoint
    print("[LLM/Claude] All endpoints/attempts failed", flush=True)
    return "", "", "", ""

# (rest of file remains functionally identical: fallback, feishu push, saving, main() etc.)
if __name__ == "__main__":
    main()
