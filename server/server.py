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
REPORTS_FILE = BASE_DIR / "reports.json"
SEED_FILE = BASE_DIR / "seed_sites.json"  # optional bootstrap only
CLASSIFIED_FILE = BASE_DIR / "classified_sites.json"

COMMUNITY_LIKELY = 1  # first report already teaches the network
COMMUNITY_PAID = 3

# Per-verdict freshness. Weak FREE expires fast so late-paywall sites can be rechecked.
TTL_SECONDS = {
    "PAID_REQUIRED": 60 * 60 * 24 * 14,
    "LIKELY_PAID": 60 * 60 * 24 * 7,
    "LIMITED_FREE": 60 * 60 * 24 * 7,
    "FREE": 60 * 60 * 24 * 2,  # strong FREE only (see cache_ttl_seconds)
    "UNKNOWN": 0,  # never treat as fresh knowledge
}

VERDICTS = {"FREE", "LIMITED_FREE", "LIKELY_PAID", "PAID_REQUIRED", "UNKNOWN"}

# One AI job per domain at a time
_classify_locks: dict[str, threading.Lock] = {}
_classify_locks_guard = threading.Lock()

SYSTEM_PROMPT = """You are a classifier for a browser extension that protects users from
"create first, pay to download" and "fill out a form, then pay to continue" websites.

Question to answer:
Will this user likely need to PAY (subscription, one-time fee, unlock) before they can
download, export, remove a watermark, submit/complete an application, or unlock the
finished result of work/effort they put into this site?

Focus on the FINAL ACTION (download/export/submit/apply), NOT whether the company sells
a Pro plan somewhere on the site.

Examples of YES / risk:
- Resume builders that let you edit then block PDF download
- Logo makers that preview then charge for files
- PDF editors that process files then require payment to download
- Video tools that add watermarks unless you pay
- Freemium design tools where free export is heavily limited
- Job/application platforms that let you build a full profile, then charge to
  submit, "boost", or unlock applications to employers

Examples of NO / free for this purpose:
- ChatGPT / Claude / Gemini — Upgrade CTAs exist but chat text is copyable free
- Google Docs — export free with login
- News / social / blogs with no create-and-download product
- Tools where the main output is freely downloadable
- Job platforms where applying and submitting is free (optional paid boosts don't count)

Verdicts:
- FREE — core download/export/submission usable without paying
- LIMITED_FREE — free path exists but watermark, low-res, quota, or key actions gated
- LIKELY_PAID — strong signs payment is needed for the finished result
- PAID_REQUIRED — clear pay-before-completion / subscription gate for the final action
- UNKNOWN — not enough evidence

Respond ONLY with strict JSON (no markdown):
{"verdict":"FREE"|"LIMITED_FREE"|"LIKELY_PAID"|"PAID_REQUIRED"|"UNKNOWN",
 "confidence":0.0-1.0,
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


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_reports():
    return load_json(REPORTS_FILE, [])


def load_seed():
    return load_json(SEED_FILE, {})


def load_classified():
    return load_json(CLASSIFIED_FILE, {})


def save_classified(data) -> None:
    save_json(CLASSIFIED_FILE, data)


def report_count_for(domain: str) -> int:
    return sum(1 for r in load_reports() if normalize_domain(r.get("domain")) == domain)


def enrich(result: dict, domain: str, source: str) -> dict:
    out = dict(result)
    out["domain"] = domain
    out["community_reports"] = report_count_for(domain)
    out["source"] = source
    out.setdefault("confidence", 0.0)
    out.setdefault("reason", "")
    out.setdefault("features", {})
    out.setdefault("last_verified_at", now_iso())
    out.setdefault("version", 1)
    out.setdefault("analysis_status", "ready")
    verdict = str(out.get("verdict", "UNKNOWN")).upper().replace(" ", "_")
    if verdict not in VERDICTS:
        verdict = "UNKNOWN"
    out["verdict"] = verdict
    return out


def domain_lock(domain: str) -> threading.Lock:
    with _classify_locks_guard:
        if domain not in _classify_locks:
            _classify_locks[domain] = threading.Lock()
        return _classify_locks[domain]


def community_verdict(domain: str):
    count = report_count_for(domain)
    if count >= COMMUNITY_PAID:
        return enrich(
            {
                "verdict": "PAID_REQUIRED",
                "confidence": min(0.80 + count * 0.02, 0.95),
                "reason": f"{count} community reports: payment required before download/export.",
                "last_verified_at": now_iso(),
                "features": {"requires_payment_before_download": True},
            },
            domain,
            "community",
        )
    if count >= COMMUNITY_LIKELY:
        return enrich(
            {
                "verdict": "LIKELY_PAID",
                "confidence": 0.75,
                "reason": "Community report suggests paid gating on download/export.",
                "last_verified_at": now_iso(),
                "features": {"requires_payment_before_download": True},
            },
            domain,
            "community",
        )
    return None


def cache_ttl_seconds(result: dict) -> int:
    """How long a stored classification stays fresh."""
    verdict = str(result.get("verdict", "UNKNOWN")).upper().replace(" ", "_")
    if verdict == "UNKNOWN":
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
        return False
    # Low-confidence FREE is not solid — often a homepage misread
    if verdict == "FREE" and float(existing.get("confidence") or 0) < 0.7:
        return False
    return True


def lookup_site(domain: str) -> dict:
    """Read-only knowledge lookup. Never calls AI."""
    domain = normalize_domain(domain)
    if not domain:
        return enrich(
            {"verdict": "UNKNOWN", "confidence": 0.0, "reason": "No domain supplied."},
            "",
            "none",
        )

    # 1) Dynamic cache — results from prior AI / reports (primary knowledge)
    classified = load_classified()
    entry = classified.get(domain)
    if entry and is_fresh_entry(entry):
        return enrich(entry["result"], domain, entry.get("source", "cache"))

    # 2) Community quorum
    community = community_verdict(domain)
    if community:
        return community

    # 3) Optional bootstrap seed (not required for the product to work)
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


def build_user_prompt(domain, url, signals, page_excerpt):
    excerpt = (page_excerpt or "")[:2200]
    return (
        f"Domain: {domain}\n"
        f"URL: {url or 'unknown'}\n"
        f"Page signals: {json.dumps(signals or {}, ensure_ascii=False)}\n"
        f"Visible page text:\n\"\"\"{excerpt}\"\"\"\n\n"
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


def zero_shot_classify(signals: dict, page_excerpt: str) -> dict:
    """Weighted keyword/signal scorer — the "zero-shot" first pass.

    Runs on every unknown/stale domain before AI. It always returns a
    verdict + confidence (never None); the caller decides whether that
    confidence is high enough to skip AI entirely (see ZERO_SHOT_THRESHOLD).
    """
    text = f"{json.dumps(signals)} {(page_excerpt or '')}".lower()
    toolish = any(k in text for k in ZS_TOOLISH_KEYWORDS) or bool(
        (signals or {}).get("exportish_buttons")
        or (signals or {}).get("has_file_upload")
        or (signals or {}).get("matched_tool_keywords")
    )

    if not toolish:
        # Anti-FP rule: chat/content/blog pages with no create-and-download
        # flow default FREE with solid (not certain) confidence.
        return {
            "verdict": "FREE",
            "confidence": 0.72,
            "reason": "Page does not look like a create-and-download or apply-and-pay flow.",
            "features": {},
            "last_verified_at": now_iso(),
        }

    strong_paid, strong_hits = _score_phrases(text, ZS_STRONG_PAYMENT)
    weak_paid, weak_hits = _score_phrases(text, ZS_WEAK_PAYMENT)
    free_signal, free_hits = _score_phrases(text, ZS_STRONG_FREE)

    raw = max(0.0, min(1.0, strong_paid + weak_paid * 0.6 - free_signal))
    evidence_count = len(strong_hits) + len(weak_hits) + len(free_hits)

    if raw >= 0.70:
        verdict, base_confidence = "PAID_REQUIRED", 0.72
    elif raw >= 0.45:
        verdict, base_confidence = "LIKELY_PAID", 0.62
    elif raw >= 0.25:
        verdict, base_confidence = "LIMITED_FREE", 0.50
    else:
        verdict, base_confidence = "FREE", 0.55

    confidence = min(0.93, base_confidence + evidence_count * 0.05)

    if strong_hits:
        reason = f"Payment phrase found before task completion ({strong_hits[0]})."
    elif free_hits:
        reason = f"Free-usage language found, tool-like page ({free_hits[0]})."
    elif weak_hits:
        reason = "Tool-like page with generic upgrade language, no direct payment gate found."
    else:
        reason = "Tool-like page with no strong payment or free signals."

    return {
        "verdict": verdict,
        "confidence": confidence,
        "reason": reason,
        "features": {"requires_payment_before_download": bool(strong_hits)},
        "last_verified_at": now_iso(),
    }


def persist_classification(domain: str, result: dict, source: str) -> dict:
    enriched = enrich(result, domain, source)
    # Do not pollute the knowledge base with UNKNOWN — leave domain open for retry
    if enriched.get("verdict") == "UNKNOWN":
        enriched["needs_classification"] = True
        enriched["analysis_status"] = enriched.get("analysis_status") or "failed"
        return enriched

    classified = load_classified()
    classified[domain] = {
        "classified_at": time.time(),
        "source": source,
        "result": enriched,
    }
    save_classified(classified)
    return enriched


@app.get("/health")
def health():
    return jsonify(
        {
            "ok": True,
            "ai_enabled": bool(GEMINI_API_KEY),
            "gemini_model": GEMINI_MODEL,
            "classified_domains": len(load_classified()),
            "seed_domains": len(load_seed()),
        }
    )


@app.get("/v1/sites/<domain>")
def get_site(domain):
    return jsonify(lookup_site(domain))


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

    with domain_lock(domain):
        existing = lookup_site(domain)
        if not force and is_solid_verdict(existing):
            return jsonify(existing)

        page_excerpt = body.get("page_excerpt") or body.get("pageExcerpt") or ""
        signals = body.get("signals") or {}
        url = body.get("url")

        if not page_excerpt and not signals:
            return jsonify({"error": "page_excerpt or signals required"}), 400

        # Zero-shot first: cheap, instant, no API cost. Only escalate to AI
        # when it isn't confident enough to trust on its own.
        zero_shot = zero_shot_classify(signals, page_excerpt)
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
    existing = lookup_site(domain)
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
        zero_shot = zero_shot_classify(signals, page_excerpt)
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

    reports = load_reports()
    reports.append(
        {
            "domain": domain,
            "url": body.get("url"),
            "note": body.get("note"),
            "report_type": body.get("report_type") or "paywall_before_export",
            "verdict_claimed": body.get("verdict_claimed"),
            "reportedAt": body.get("reportedAt", int(time.time() * 1000)),
            "install_id": body.get("install_id"),
        }
    )
    save_json(REPORTS_FILE, reports)
    total = report_count_for(domain)

    # Reports write into the shared knowledge base immediately
    verdict = "PAID_REQUIRED" if total >= COMMUNITY_PAID else "LIKELY_PAID"
    result = persist_classification(
        domain,
        {
            "verdict": verdict,
            "confidence": 0.82 if verdict == "LIKELY_PAID" else 0.92,
            "reason": "Community report: payment required before download/export/submission.",
            "last_verified_at": now_iso(),
            "features": {"requires_payment_before_download": True},
        },
        "community",
    )
    return jsonify({"ok": True, "totalReportsForDomain": total, "domain": domain, "result": result})


@app.get("/v1/reports/<domain>")
@app.get("/reports/<domain>")
def get_reports(domain):
    domain = normalize_domain(domain)
    reports = [r for r in load_reports() if normalize_domain(r.get("domain")) == domain]
    return jsonify({"domain": domain, "count": len(reports), "reports": reports})


if __name__ == "__main__":
    print(f"AI enabled: {bool(GEMINI_API_KEY)}")
    print(f"Classified domains: {len(load_classified())}")
    print(f"Optional seed domains: {len(load_seed())}")
    app.run(port=PORT, debug=True)