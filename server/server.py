"""
FreeOrNot — dynamic knowledge engine

Flow for every visit:
  1. Return cached classification if present (no AI, no zero-shot)
  2. Else run the zero-shot signal scorer on page signals/text
     - confidence >= ZERO_SHOT_THRESHOLD -> store verdict, done (no AI call)
     - confidence <  ZERO_SHOT_THRESHOLD -> escalate to AI (Gemini)
  3. Store result so every future user gets an instant answer

  force=true bypasses step 1 and re-runs the zero-shot/AI pipeline even if
  a verdict is cached. Used when a page reveals payment language AFTER the
  initial free/unknown classification (e.g. a job-profile form that only
  asks for money once you click "Apply").

  The zero-shot scorer exists to keep AI calls rare and cheap: most sites
  have unambiguous signals (strong payment phrases, or clearly no tool/export
  flow at all) and don't need an LLM to classify correctly. AI is reserved
  for the genuinely ambiguous middle.

Seed file is optional bootstrap only — not the product.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from flask import Flask, jsonify, request
from flask_cors import CORS

import store

# Load server/.env if present (GEMINI_API_KEY)
try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

app = Flask(__name__)
CORS(app)


# Chrome treats chrome-extension:// origins as "public" and localhost as
# "private". Fetches from the extension to this server trigger a Private
# Network Access preflight that requires this header on every response
# (including the OPTIONS preflight) — without it, requests silently fail
# in the browser even though curl / Postman work fine.
@app.after_request
def add_private_network_access_header(response):
    response.headers["Access-Control-Allow-Private-Network"] = "true"
    return response

PORT = int(os.environ.get("PORT", 8787))
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
# Prefer a model that still has free-tier quota; override with GEMINI_MODEL if needed.
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")
GEMINI_FALLBACK_MODELS = [
    m.strip()
    for m in os.environ.get(
        "GEMINI_FALLBACK_MODELS",
        "gemini-flash-latest,gemini-2.0-flash,gemini-2.5-flash,gemini-2.0-flash-lite",
    ).split(",")
    if m.strip()
]

# Zero-shot signal scorer must clear this confidence to short-circuit AI.
# Below it, we escalate to Gemini; if Gemini is unavailable/fails, the
# zero-shot guess is used anyway as a last-resort answer.
ZERO_SHOT_THRESHOLD = float(os.environ.get("ZERO_SHOT_CONFIDENCE_THRESHOLD", "0.65"))

BASE_DIR = Path(__file__).parent
SEED_FILE = BASE_DIR / "seed_sites.json"  # optional bootstrap only

# Unique reporters required before community votes can change a site's status.
COMMUNITY_LIKELY = 3
COMMUNITY_PAID = 5
COMMUNITY_FREE = 3
VOTE_TTL_SECONDS = 60 * 60 * 24 * 30
RATE_LIMIT_WINDOW = 60 * 60
RATE_LIMIT_MAX = 10  # report submissions per IP per hour
RATE_LIMIT_INSTALL_MAX = 8  # reports per extension install per hour
CLASSIFY_RATE_MAX = 40  # classify posts per IP per hour
INSTALL_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")

# Per-verdict freshness. Weak FREE expires fast so late-paywall sites can be rechecked.
TTL_SECONDS = {
    "PAID_REQUIRED": 60 * 60 * 24 * 14,
    "LIKELY_PAID": 60 * 60 * 24 * 7,
    "LIMITED_FREE": 60 * 60 * 24 * 7,
    "FREE": 60 * 60 * 24 * 2,  # strong FREE only (see cache_ttl_seconds)
    "UNKNOWN": 0,  # never treat as fresh knowledge
}

VERDICTS = {"FREE", "LIMITED_FREE", "LIKELY_PAID", "PAID_REQUIRED", "UNKNOWN"}
SITE_TYPES = {"tool", "streaming", "news", "chat", "marketplace", "other"}

# Streaming / membership products whose core use is paid. Matched as an exact
# host or a parent suffix (www.netflix.com, netflix.co.in via token rules).
KNOWN_PAID_DOMAINS = {
    "netflix.com",
    "primevideo.com",
    "amazonprimevideo.com",
    "disneyplus.com",
    "hulu.com",
    "max.com",
    "hbomax.com",
    "play.max.com",
    "paramountplus.com",
    "peacocktv.com",
    "tv.apple.com",
    "hotstar.com",
    "jiohotstar.com",
    "sonyliv.com",
    "zee5.com",
    "jiocinema.com",
    "discoveryplus.com",
    "crunchyroll.com",
    "dazn.com",
}

KNOWN_PAID_HOST_TOKENS = {
    "netflix",
    "primevideo",
    "disneyplus",
    "hulu",
    "hbomax",
    "paramountplus",
    "peacocktv",
    "hotstar",
    "jiohotstar",
    "sonyliv",
    "zee5",
    "jiocinema",
    "discoveryplus",
    "crunchyroll",
}

AMAZON_PRIME_VIDEO_URL_HINTS = (
    "/gp/video",
    "/primevideo",
    "/prime-video",
    "primevideo.",
    "/minitv",
)

ZS_SUBSCRIPTION_PRODUCT = {
    "subscribe to watch": 0.45,
    "start your membership": 0.40,
    "start membership": 0.40,
    "join prime": 0.40,
    "prime membership": 0.35,
    "watch with a subscription": 0.40,
    "subscription required": 0.40,
    "start your free trial": 0.25,
    "monthly subscription": 0.30,
    "annual subscription": 0.30,
    "streaming plan": 0.30,
    "sign up to watch": 0.35,
}

# One AI job per domain at a time
_classify_locks: dict[str, threading.Lock] = {}
_classify_locks_guard = threading.Lock()

SYSTEM_PROMPT = """You are a classifier for a browser extension that protects users from
"create first, pay to download" and "fill out a form, then pay to continue" websites.

Question to answer:
Will this user likely need to PAY (subscription, one-time fee, unlock) before they can
download, export, remove a watermark, submit/complete an application, or unlock the
finished result of work/effort they put into this site?

Focus on the FINAL ACTION the user came for: download/export/submit/apply, OR
accessing a paid product whose core use requires a subscription (streaming,
membership video, paid newsstand apps).

Examples of YES / risk:
- Resume builders that let you edit then block PDF download
- Logo makers that preview then charge for files
- PDF editors that process files then require payment to download
- Video tools that add watermarks unless you pay
- Freemium design tools where free export is heavily limited
- Job/application platforms that let you build a full profile, then charge to
  submit, "boost", or unlock applications to employers
- Streaming / membership products where watching or listening requires paying
  (Netflix, Prime Video, Disney+, Max, Hulu, Hotstar, etc.)

Examples of NO / free for this purpose:
- ChatGPT / Claude / Gemini — Upgrade CTAs exist but chat text is copyable free
- Google Docs — export free with login
- News / social / blogs with no paywall and no create-and-download product
- Tools where the main output is freely downloadable
- Job platforms where applying and submitting is free (optional paid boosts don't count)
- Amazon retail shopping (browse/buy items) — that is not Prime Video

Verdicts:
- FREE — core download/export/submission usable without paying
- LIMITED_FREE — free path exists but watermark, low-res, quota, or key actions gated
- LIKELY_PAID — strong signs payment is needed for the finished result
- PAID_REQUIRED — clear pay-before-completion / subscription gate for the final action
- UNKNOWN — not enough evidence

Respond ONLY with strict JSON (no markdown):
{"verdict":"FREE"|"LIMITED_FREE"|"LIKELY_PAID"|"PAID_REQUIRED"|"UNKNOWN",
 "confidence":0.0-1.0,
 "site_type":"tool"|"streaming"|"news"|"chat"|"marketplace"|"other",
 "reason":"one short sentence",
 "features":{"limited_free_export":bool,"adds_watermark":bool,
 "locks_high_res":bool,"requires_payment_before_download":bool,
 "requires_payment_before_submit":bool}}
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize_domain(domain: str) -> str:
    d = str(domain or "").strip().lower()
    d = re.sub(r"^https?://", "", d)
    d = d.split("/")[0]
    d = re.sub(r"^www\.", "", d)
    return d


def _host_labels(domain: str) -> set[str]:
    return {p for p in normalize_domain(domain).split(".") if p}


def is_amazon_retail(domain: str) -> bool:
    labels = _host_labels(domain)
    return "amazon" in labels or domain.startswith("amzn.") or domain.endswith(".amzn.com")


def is_known_paid_subscription(domain: str, url: str = "") -> bool:
    """True for products whose main use is a paid membership (Netflix, Prime Video)."""
    domain = normalize_domain(domain)
    if not domain:
        return False
    if domain in KNOWN_PAID_DOMAINS or any(
        domain.endswith("." + parent) for parent in KNOWN_PAID_DOMAINS
    ):
        return True
    labels = _host_labels(domain)
    if labels & KNOWN_PAID_HOST_TOKENS:
        return True
    blob = f"{domain} {(url or '').lower()}"
    if is_amazon_retail(domain) and any(hint in blob for hint in AMAZON_PRIME_VIDEO_URL_HINTS):
        return True
    return False


def known_paid_payload(domain: str) -> dict:
    return {
        "verdict": "PAID_REQUIRED",
        "confidence": 0.97,
        "site_type": "streaming",
        "reason": "This is a paid subscription product; access requires a membership.",
        "last_verified_at": now_iso(),
        "features": {
            "requires_payment_before_download": True,
            "requires_subscription_for_access": True,
        },
    }


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def client_ip() -> str:
    forwarded = (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
    return forwarded or (request.remote_addr or "unknown")


def hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def reporter_fingerprint(ip: str, install_id: str) -> str:
    """Stable identity for one reporter. Same extension install counts once
    even if the IP changes; IP is only used when install_id is missing."""
    if install_id:
        return hash_secret(f"install|{install_id}")
    return hash_secret(f"ip|{ip}")


def reporter_aliases(ip: str, install_id: str) -> set[str]:
    """Current + legacy fingerprints so a user cannot vote twice after a format change."""
    aliases = {reporter_fingerprint(ip, install_id)}
    aliases.add(hash_secret(f"{ip}|{install_id}"))
    return aliases


def normalize_install_id(raw) -> str:
    value = str(raw or "").strip()
    if INSTALL_ID_RE.match(value):
        return value
    return ""


def normalize_stance(body: dict) -> str:
    raw = str(body.get("stance") or body.get("report_type") or "").strip().lower()
    if raw in {"free", "false_positive", "now_free", "not_paywall", "this_is_free"}:
        return "free"
    return "paid"


def vote_tally(domain: str) -> dict:
    return store.vote_counts(normalize_domain(domain), time.time(), VOTE_TTL_SECONDS)


def community_confidence(unique_count: int) -> float:
    if unique_count >= 8:
        return 0.90
    if unique_count >= 5:
        return 0.85
    if unique_count >= 3:
        return 0.75
    if unique_count >= 1:
        return 0.65
    return 0.0


def record_report_attempt(ip: str, install_id: str) -> tuple[bool, int]:
    now = time.time()
    allowed_ip, remaining_ip = store.record_rate(
        f"ip:{hash_secret(ip)}", now, RATE_LIMIT_WINDOW, RATE_LIMIT_MAX
    )
    if not allowed_ip:
        return False, 0
    if install_id:
        allowed_install, remaining_install = store.record_rate(
            f"install:{hash_secret(install_id)}",
            now,
            RATE_LIMIT_WINDOW,
            RATE_LIMIT_INSTALL_MAX,
        )
        if not allowed_install:
            return False, 0
        return True, min(remaining_ip, remaining_install)
    return True, remaining_ip


def record_classify_attempt(ip: str) -> bool:
    allowed, _ = store.record_rate(
        f"classify:{hash_secret(ip)}", time.time(), RATE_LIMIT_WINDOW, CLASSIFY_RATE_MAX
    )
    return allowed


def load_seed():
    return load_json(SEED_FILE, {})


def load_classified():
    return store.load_classified()


def report_count_for(domain: str) -> int:
    return vote_tally(domain)["paid"]


def normalize_site_type(value) -> str:
    raw = str(value or "").strip().lower()
    return raw if raw in SITE_TYPES else "other"


def enrich(result: dict, domain: str, source: str) -> dict:
    out = dict(result)
    out["domain"] = domain
    tally = vote_tally(domain) if domain else {"paid": 0, "free": 0, "net_paid": 0, "total": 0}
    out["community_reports"] = tally["paid"]
    out["community_paid"] = tally["paid"]
    out["community_free"] = tally["free"]
    out["source"] = source
    out.setdefault("confidence", 0.0)
    out.setdefault("reason", "")
    out.setdefault("features", {})
    out.setdefault("last_verified_at", now_iso())
    out.setdefault("version", 1)
    out.setdefault("analysis_status", "ready")
    out["site_type"] = normalize_site_type(out.get("site_type"))
    verdict = str(out.get("verdict", "UNKNOWN")).upper().replace(" ", "_")
    if verdict not in VERDICTS:
        verdict = "UNKNOWN"
    out["verdict"] = verdict
    out.pop("skip_ai", None)
    return out


def domain_lock(domain: str) -> threading.Lock:
    with _classify_locks_guard:
        if domain not in _classify_locks:
            _classify_locks[domain] = threading.Lock()
        return _classify_locks[domain]


def community_payload(domain: str, tally: dict) -> dict:
    net = int(tally.get("net_paid") or 0)
    paid = int(tally.get("paid") or 0)
    free = int(tally.get("free") or 0)
    if net <= -COMMUNITY_FREE:
        noun = "reporter" if free == 1 else "reporters"
        return {
            "verdict": "FREE",
            "confidence": community_confidence(free),
            "site_type": "other",
            "reason": f"{free} unique community {noun}: this site does not require payment for the core task.",
            "last_verified_at": now_iso(),
            "features": {},
        }
    verdict = "PAID_REQUIRED" if net >= COMMUNITY_PAID else "LIKELY_PAID"
    noun = "reporter" if paid == 1 else "reporters"
    return {
        "verdict": verdict,
        "confidence": community_confidence(paid),
        "site_type": "other",
        "reason": f"{paid} unique community {noun}: payment required before download/export/access.",
        "last_verified_at": now_iso(),
        "features": {"requires_payment_before_download": True},
    }


def community_verdict(domain: str):
    tally = vote_tally(domain)
    net = int(tally.get("net_paid") or 0)
    if net >= COMMUNITY_PAID or net >= COMMUNITY_LIKELY:
        return enrich(community_payload(domain, tally), domain, "community")
    if net <= -COMMUNITY_FREE:
        return enrich(community_payload(domain, tally), domain, "community")
    return None


def cache_ttl_seconds(result: dict) -> int:
    """How long a stored classification stays fresh."""
    verdict = str(result.get("verdict", "UNKNOWN")).upper().replace(" ", "_")
    if verdict == "UNKNOWN":
        if str(result.get("analysis_status") or "") == "skipped":
            return 60 * 60 * 12
        return 0
    conf = float(result.get("confidence") or 0)
    # Any low-confidence guess (e.g. zero-shot fallback used because AI was
    # unavailable) expires quickly so it gets re-checked with better evidence
    # instead of sticking around for the full verdict TTL.
    if conf < 0.60:
        return 60 * 60 * 6  # 6 hours
    # Weak / provisional FREE expires quickly so AI can re-check with better evidence
    if verdict == "FREE" and conf < 0.75:
        return 60 * 60 * 6  # 6 hours
    return TTL_SECONDS.get(verdict, 60 * 60)


def is_fresh_entry(entry: dict) -> bool:
    result = entry.get("result") or {}
    ttl = cache_ttl_seconds(result)
    if ttl <= 0:
        return False
    return (time.time() - entry.get("classified_at", 0)) < ttl


def is_solid_verdict(existing: dict) -> bool:
    """True when we should skip AI and trust the cached answer."""
    if not existing:
        return False
    if existing.get("needs_classification"):
        return False
    verdict = str(existing.get("verdict", "UNKNOWN")).upper().replace(" ", "_")
    if verdict == "UNKNOWN":
        return str(existing.get("analysis_status") or "") == "skipped"
    # Low-confidence FREE is not solid — often a homepage misread
    if verdict == "FREE" and float(existing.get("confidence") or 0) < 0.7:
        return False
    return True


def lookup_site(domain: str, url: str = "") -> dict:
    """Read-only knowledge lookup. Never calls AI."""
    domain = normalize_domain(domain)
    if not domain:
        return enrich(
            {"verdict": "UNKNOWN", "confidence": 0.0, "reason": "No domain supplied."},
            "",
            "none",
        )

    if is_known_paid_subscription(domain, url):
        return enrich(known_paid_payload(domain), domain, "known_paid")

    # 1) Dynamic cache — results from prior AI / reports (primary knowledge)
    entry = store.get_classified(domain)
    community = community_verdict(domain)

    if community:
        return community

    if entry and is_fresh_entry(entry):
        # Drop old community verdicts that never reached the current quorum.
        if entry.get("source") == "community" and not community:
            pass
        else:
            return enrich(entry["result"], domain, entry.get("source", "cache"))

    # 2) Optional bootstrap seed (not required for the product to work)
    seed = load_seed()
    if domain in seed:
        return enrich(seed[domain], domain, "seed")

    return enrich(
        {
            "verdict": "UNKNOWN",
            "confidence": 0.0,
            "reason": "Not classified yet — analysis needed.",
            "analysis_status": "needed",
            "needs_classification": True,
        },
        domain,
        "none",
    )


def scrub_excerpt(text: str) -> str:
    cleaned = re.sub(r"[\w.+-]+@[\w-]+\.[\w.-]+", "[email]", text or "")
    cleaned = re.sub(r"\b\d{12,19}\b", "[number]", cleaned)
    return cleaned[:2200]


def build_user_prompt(domain, url, signals, page_excerpt):
    excerpt = scrub_excerpt((page_excerpt or "")[:2200])
    return (
        f"Domain: {domain}\n"
        f"URL: {url or 'unknown'}\n"
        f"Page signals: {json.dumps(signals or {}, ensure_ascii=False)}\n"
        f"Visible page text:\n\"\"\"{scrub_excerpt(excerpt)}\"\"\"\n\n"
        "Classify payment risk for completing/downloading/exporting/submitting "
        "what the user came to this site to do."
    )


def parse_model_json(raw_text: str) -> dict:
    cleaned = re.sub(r"```json|```", "", raw_text or "").strip()
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        return {
            "verdict": "UNKNOWN",
            "confidence": 0.3,
            "reason": "Could not parse model output.",
            "features": {},
        }
    verdict = str(data.get("verdict", "UNKNOWN")).upper().replace(" ", "_")
    if verdict not in VERDICTS:
        legacy = {"PAYWALL": "LIKELY_PAID", "FREE": "FREE", "UNCERTAIN": "UNKNOWN"}
        verdict = legacy.get(verdict, "UNKNOWN")
    return {
        "verdict": verdict,
        "confidence": float(data.get("confidence", 0.4)),
        "site_type": normalize_site_type(data.get("site_type")),
        "reason": data.get("reason") or "",
        "features": data.get("features") or {},
        "last_verified_at": now_iso(),
        "version": 1,
    }


def extract_gemini_text(data: dict) -> str:
    """Join all non-thought text parts (Gemini 2.5 may return multiple parts)."""
    candidates = data.get("candidates") or []
    if not candidates:
        feedback = data.get("promptFeedback") or data.get("error") or data
        raise RuntimeError(f"Gemini returned no candidates: {feedback}")

    parts = candidates[0].get("content", {}).get("parts") or []
    texts: list[str] = []
    for part in parts:
        if part.get("thought"):
            continue
        text = part.get("text")
        if text:
            texts.append(text)
    raw = "\n".join(texts).strip()
    if not raw:
        finish = candidates[0].get("finishReason")
        raise RuntimeError(f"Gemini returned empty text (finishReason={finish})")
    return raw


def call_gemini(domain, url, signals, page_excerpt) -> dict:
    if not GEMINI_API_KEY:
        raise RuntimeError("Server missing GEMINI_API_KEY")

    payload = {
        "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [
            {
                "role": "user",
                "parts": [{"text": build_user_prompt(domain, url, signals, page_excerpt)}],
            }
        ],
        "generationConfig": {
            # Keep headroom for JSON; some models also emit thought parts
            "maxOutputTokens": 1024,
            "temperature": 0,
            "responseMimeType": "application/json",
        },
    }

    # Try preferred model first, then fallbacks (handles free-tier 429 per model)
    models: list[str] = []
    for m in [GEMINI_MODEL, *GEMINI_FALLBACK_MODELS]:
        if m and m not in models:
            models.append(m)

    last_err: Exception | None = None
    for model in models:
        url_api = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model}:generateContent"
        )
        try:
            resp = requests.post(
                url_api,
                params={"key": GEMINI_API_KEY},
                headers={"Content-Type": "application/json"},
                data=json.dumps(payload),
                timeout=45,
            )
            if resp.status_code == 429:
                print(f"Gemini quota hit on {model}, trying next fallback...")
                last_err = RuntimeError(f"quota exceeded for {model}")
                continue
            if not resp.ok:
                detail = resp.text[:400]
                print(f"Gemini HTTP {resp.status_code} on {model}: {detail}")
                last_err = RuntimeError(f"HTTP {resp.status_code} on {model}")
                # 404 model-not-found → try next; other errors too
                continue
            data = resp.json()
            raw_text = extract_gemini_text(data)
            parsed = parse_model_json(raw_text)
            parsed["_model"] = model
            try:
                print(f"Gemini OK via {model} for {domain} -> {parsed.get('verdict')}")
            except Exception:
                pass
            return parsed
        except (requests.RequestException, RuntimeError, KeyError, json.JSONDecodeError) as err:
            try:
                print(f"Gemini error on {model}: {err}")
            except Exception:
                pass
            last_err = err
            continue

    raise RuntimeError(f"All Gemini models failed: {last_err}")


# Phrase -> weight. Weights are additive evidence toward "payment required",
# tuned so a single strong phrase alone can already clear PAID_REQUIRED, while
# weak marketing words need several hits to move the needle.
ZS_STRONG_PAYMENT = {
    "credit card required": 0.40,
    "payment required": 0.40,
    "pay to download": 0.40,
    "pay to export": 0.40,
    "pay to continue": 0.35,
    "upgrade to download": 0.35,
    "upgrade to export": 0.35,
    "upgrade to submit": 0.35,
    "subscribe to continue": 0.35,
    "unlock download": 0.35,
    "billing information": 0.30,
    "enter card": 0.30,
    "checkout": 0.25,
    "buy now": 0.25,
}

ZS_WEAK_PAYMENT = {
    "pro plan": 0.15,
    "go pro": 0.15,
    "subscribe": 0.12,
    "premium": 0.12,
    "pricing": 0.10,
    "upgrade": 0.10,
}

ZS_STRONG_FREE = {
    "100% free": 0.35,
    "completely free": 0.30,
    "free forever": 0.30,
    "no credit card": 0.30,
    "open source": 0.30,
    "free download": 0.25,
    "free to use": 0.25,
    "no watermark": 0.20,
}

ZS_TOOLISH_KEYWORDS = (
    "pdf",
    "resume builder",
    "cv builder",
    "logo maker",
    "watermark",
    "edit pdf",
    "merge pdf",
    "compress pdf",
    "remove background",
    "try free",
    "start editing",
    "apply now",
    "job application",
)


def _score_phrases(text: str, weighted: dict[str, float]) -> tuple[float, list[str]]:
    hits = [phrase for phrase in weighted if phrase in text]
    return sum(weighted[phrase] for phrase in hits), hits


def zero_shot_classify(signals: dict, page_excerpt: str, domain: str = "", url: str = "") -> dict:
    """Weighted keyword/signal scorer — the "zero-shot" first pass.

    Runs on every unknown/stale domain before AI. It always returns a
    verdict + confidence (never None); the caller decides whether that
    confidence is high enough to skip AI entirely (see ZERO_SHOT_THRESHOLD).
    """
    if is_known_paid_subscription(domain, url):
        return known_paid_payload(domain)

    text = f"{json.dumps(signals)} {(page_excerpt or '')} {(url or '')}".lower()
    toolish = any(k in text for k in ZS_TOOLISH_KEYWORDS) or bool(
        (signals or {}).get("exportish_buttons")
        or (signals or {}).get("has_file_upload")
        or (signals or {}).get("matched_tool_keywords")
    )

    sub_signal, sub_hits = _score_phrases(text, ZS_SUBSCRIPTION_PRODUCT)

    if not toolish:
        if sub_signal >= 0.35:
            return {
                "verdict": "PAID_REQUIRED",
                "confidence": min(0.92, 0.74 + sub_signal * 0.2),
                "site_type": "streaming",
                "reason": f"Membership/subscription required to use this product ({sub_hits[0]}).",
                "features": {"requires_subscription_for_access": True},
                "last_verified_at": now_iso(),
            }
        return {
            "verdict": "UNKNOWN",
            "confidence": 0.35,
            "site_type": "other",
            "reason": "Not enough evidence that this site gates a finished result behind payment.",
            "features": {},
            "last_verified_at": now_iso(),
            "skip_ai": True,
            "needs_classification": False,
        }

    strong_paid, strong_hits = _score_phrases(text, ZS_STRONG_PAYMENT)
    weak_paid, weak_hits = _score_phrases(text, ZS_WEAK_PAYMENT)
    free_signal, free_hits = _score_phrases(text, ZS_STRONG_FREE)

    raw = max(0.0, min(1.0, strong_paid + weak_paid * 0.6 + sub_signal - free_signal))
    evidence_count = len(strong_hits) + len(weak_hits) + len(free_hits) + len(sub_hits)

    if raw >= 0.70:
        verdict, base_confidence = "PAID_REQUIRED", 0.72
    elif raw >= 0.45:
        verdict, base_confidence = "LIKELY_PAID", 0.62
    elif raw >= 0.25:
        verdict, base_confidence = "LIMITED_FREE", 0.50
    else:
        verdict, base_confidence = "UNKNOWN", 0.45

    confidence = min(0.93, base_confidence + evidence_count * 0.05)
    site_type = "streaming" if sub_hits and not strong_hits else "tool"

    if sub_hits and not strong_hits:
        reason = f"Membership/subscription required to use this product ({sub_hits[0]})."
    elif strong_hits:
        reason = f"Payment phrase found before task completion ({strong_hits[0]})."
    elif free_hits:
        reason = f"Free-usage language found, tool-like page ({free_hits[0]})."
        verdict = "LIMITED_FREE" if verdict == "UNKNOWN" else verdict
        site_type = "tool"
    elif weak_hits:
        reason = "Tool-like page with generic upgrade language, no direct payment gate found."
    else:
        reason = "Tool-like page with no strong payment or free signals."

    return {
        "verdict": verdict,
        "confidence": confidence,
        "site_type": site_type,
        "reason": reason,
        "features": {"requires_payment_before_download": bool(strong_hits or sub_hits)},
        "last_verified_at": now_iso(),
    }


def persist_classification(domain: str, result: dict, source: str) -> dict:
    enriched = enrich(result, domain, source)
    # Do not pollute the knowledge base with UNKNOWN — leave domain open for retry
    if enriched.get("verdict") == "UNKNOWN" and enriched.get("analysis_status") != "skipped":
        enriched["needs_classification"] = True
        enriched["analysis_status"] = enriched.get("analysis_status") or "failed"
        return enriched

    # Never let a later FREE guess overwrite a known paid subscription product.
    if is_known_paid_subscription(domain) and source != "known_paid":
        paid = enrich(known_paid_payload(domain), domain, "known_paid")
        store.upsert_classified(domain, "known_paid", paid)
        return paid

    store.upsert_classified(domain, source, enriched)
    return enriched


@app.get("/health")
def health():
    return jsonify(
        {
            "ok": True,
            "ai_enabled": bool(GEMINI_API_KEY),
            "gemini_model": GEMINI_MODEL,
            "classified_domains": store.classified_count(),
            "seed_domains": len(load_seed()),
        }
    )


@app.get("/v1/sites/<domain>")
def get_site(domain):
    url = request.args.get("url") or ""
    return jsonify(lookup_site(domain, url))


@app.post("/v1/sites/classify")
def classify_site():
    """
    Analyze once if unknown; otherwise return cache — UNLESS force=true,
    in which case AI is re-run even if a verdict is already cached. This is
    how late-appearing paywalls (e.g. "pay to submit your application") get
    caught after an initial FREE/UNKNOWN verdict was already stored.
    """
    body = request.get_json(silent=True) or {}
    domain = normalize_domain(body.get("domain"))
    if not domain:
        return jsonify({"error": "domain is required"}), 400

    force = bool(body.get("force"))
    url = body.get("url") or ""

    with domain_lock(domain):
        if is_known_paid_subscription(domain, url):
            return jsonify(persist_classification(domain, known_paid_payload(domain), "known_paid"))

        existing = lookup_site(domain, url)
        if not force and is_solid_verdict(existing):
            return jsonify(existing)

        if not record_classify_attempt(client_ip()):
            if is_solid_verdict(existing):
                return jsonify(existing)
            return jsonify({"error": "rate_limited", "message": "Too many classification requests."}), 429

        page_excerpt = body.get("page_excerpt") or body.get("pageExcerpt") or ""
        signals = body.get("signals") or {}

        if not page_excerpt and not signals:
            return jsonify({"error": "page_excerpt or signals required"}), 400

        # Zero-shot first: cheap, instant, no API cost. Only escalate to AI
        # when it isn't confident enough to trust on its own.
        zero_shot = zero_shot_classify(signals, page_excerpt, domain, url)
        if zero_shot.get("skip_ai"):
            zero_shot["analysis_status"] = "skipped"
            zero_shot["needs_classification"] = False
            return jsonify(persist_classification(domain, zero_shot, "zero_shot"))
        if zero_shot["confidence"] >= ZERO_SHOT_THRESHOLD:
            source = "zero_shot_force" if force else "zero_shot"
            return jsonify(persist_classification(domain, zero_shot, source))

        if GEMINI_API_KEY:
            try:
                parsed = call_gemini(domain, url, signals, page_excerpt)
                source = "ai_force" if force else "ai"
                return jsonify(persist_classification(domain, parsed, source))
            except (requests.RequestException, RuntimeError, KeyError, json.JSONDecodeError) as err:
                print(f"Gemini classify error for {domain}: {err}")
                # fall through to the low-confidence zero-shot guess below

        # AI unavailable or failed — use the zero-shot guess anyway rather
        # than giving up; it's still better than nothing, just lower confidence.
        source = "zero_shot_low_conf_force" if force else "zero_shot_low_conf"
        return jsonify(persist_classification(domain, zero_shot, source))


@app.post("/classify")
def classify_legacy():
    body = request.get_json(silent=True) or {}
    domain = normalize_domain(body.get("domain"))
    force = bool(body.get("force"))
    url = body.get("url") or ""
    if is_known_paid_subscription(domain, url):
        result = persist_classification(domain, known_paid_payload(domain), "known_paid")
        return jsonify(
            {
                "verdict": "paywall",
                "confidence": result.get("confidence", 0),
                "reason": result.get("reason", ""),
            }
        )
    existing = lookup_site(domain, url)
    if not force and is_solid_verdict(existing):
        legacy_map = {
            "PAID_REQUIRED": "paywall",
            "LIKELY_PAID": "paywall",
            "LIMITED_FREE": "paywall",
            "FREE": "free",
            "UNKNOWN": "uncertain",
        }
        return jsonify(
            {
                "verdict": legacy_map.get(existing["verdict"], "uncertain"),
                "confidence": existing.get("confidence", 0),
                "reason": existing.get("reason", ""),
            }
        )

    page_excerpt = body.get("pageExcerpt") or body.get("page_excerpt") or ""
    if not page_excerpt:
        return jsonify({"error": "pageExcerpt is required"}), 400

    with domain_lock(domain):
        signals = {"buttonLabel": body.get("buttonLabel")}
        zero_shot = zero_shot_classify(signals, page_excerpt, domain, url)
        if zero_shot["confidence"] >= ZERO_SHOT_THRESHOLD:
            result = persist_classification(domain, zero_shot, "zero_shot_force" if force else "zero_shot")
        elif GEMINI_API_KEY:
            try:
                parsed = call_gemini(domain, body.get("url"), signals, page_excerpt)
                result = persist_classification(domain, parsed, "ai_force" if force else "ai")
            except Exception as err:
                print(err)
                return jsonify({"verdict": "uncertain", "confidence": 0, "reason": "Classifier error."}), 500
        else:
            source = "zero_shot_low_conf_force" if force else "zero_shot_low_conf"
            result = persist_classification(domain, zero_shot, source)

    legacy_map = {
        "PAID_REQUIRED": "paywall",
        "LIKELY_PAID": "paywall",
        "LIMITED_FREE": "paywall",
        "FREE": "free",
        "UNKNOWN": "uncertain",
    }
    return jsonify(
        {
            "verdict": legacy_map.get(result["verdict"], "uncertain"),
            "confidence": result.get("confidence", 0),
            "reason": result.get("reason", ""),
        }
    )


@app.post("/v1/reports")
@app.post("/report")
def report():
    body = request.get_json(silent=True) or {}
    domain = normalize_domain(body.get("domain"))
    if not domain:
        return jsonify({"error": "domain is required"}), 400

    ip = client_ip()
    install_id = normalize_install_id(body.get("install_id"))
    if not install_id:
        return jsonify({
            "error": "install_id_required",
            "message": "A valid install_id is required to report a site.",
        }), 400

    stance = normalize_stance(body)
    report_type = body.get("report_type") or (
        "false_positive" if stance == "free" else "paywall_before_export"
    )
    fingerprint = reporter_fingerprint(ip, install_id)
    now = time.time()
    url = body.get("url") or ""

    existing = store.find_report(domain, reporter_aliases(ip, install_id), now, VOTE_TTL_SECONDS)
    if existing and existing.get("stance") == stance:
        tally = vote_tally(domain)
        current = lookup_site(domain, url)
        return jsonify({
            "ok": True,
            "already_reported": True,
            "status_updated": False,
            "stance": stance,
            "totalReportsForDomain": tally["paid"],
            "uniqueReporters": tally["paid"],
            "community_paid": tally["paid"],
            "community_free": tally["free"],
            "reports_needed": COMMUNITY_LIKELY,
            "refreshed": True,
            "domain": domain,
            "result": current,
            "message": "You have already submitted this report for this site.",
        })

    allowed, remaining = record_report_attempt(ip, install_id)
    if not allowed:
        return jsonify({
            "error": "rate_limited",
            "message": "Too many reports from this install or network. Try again later.",
        }), 429

    store.upsert_report(
        domain,
        fingerprint,
        stance,
        report_type,
        url,
        body.get("note"),
        now,
    )
    tally = vote_tally(domain)
    community = community_verdict(domain)
    status_updated = community is not None
    if status_updated:
        result = persist_classification(domain, community_payload(domain, tally), "community")
    else:
        result = lookup_site(domain, url)

    needed = COMMUNITY_FREE if stance == "free" else COMMUNITY_LIKELY
    have = tally["free"] if stance == "free" else tally["paid"]
    return jsonify({
        "ok": True,
        "already_reported": False,
        "status_updated": status_updated,
        "stance": stance,
        "totalReportsForDomain": tally["paid"],
        "uniqueReporters": tally["paid"],
        "community_paid": tally["paid"],
        "community_free": tally["free"],
        "reports_needed": needed,
        "refreshed": bool(existing),
        "rateLimitRemaining": remaining,
        "domain": domain,
        "result": result,
        "message": (
            f"{have} unique {stance} reports so far. Status changes after {needed}."
            if not status_updated
            else f"Community status updated from {tally['paid']} paid / {tally['free']} free reports."
        ),
    })


@app.get("/v1/reports/<domain>")
@app.get("/reports/<domain>")
def get_reports(domain):
    domain = normalize_domain(domain)
    tally = vote_tally(domain)
    now = time.time()
    rows = [
        {
            "domain": r.get("domain"),
            "stance": r.get("stance"),
            "report_type": r.get("report_type"),
            "last_reported_at": r.get("last_reported_at"),
        }
        for r in store.load_fresh_reports(now, VOTE_TTL_SECONDS)
        if normalize_domain(r.get("domain")) == domain
    ]
    return jsonify({
        "domain": domain,
        "count": tally["paid"],
        "community_paid": tally["paid"],
        "community_free": tally["free"],
        "reports": rows,
    })


store.init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)