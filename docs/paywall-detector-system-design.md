# FreeOrNot — Production System Design

> Working name: **FreeOrNot** (formerly Paywall Detector)
> Classification engine for creative/productivity sites that gate download, export, or watermark removal behind payment.

---

## 1. Product Philosophy

**Do not detect paywalls after they appear.**

Warn within seconds of opening a site if finishing the user's likely task (download, export, remove watermark, unlock resolution) will require payment.

| Old model | New model |
|-----------|-----------|
| Scan DOM for "Upgrade" / Stripe | Identify domain → lookup knowledge base |
| React when user clicks Download | Classify *before* they invest time |
| AI on every ambiguous page | AI once per unknown/stale domain |
| Per-user heuristics | Shared, compounding knowledge |

This is closer to **Google Safe Browsing** or **VirusTotal** than to a content script keyword scanner.

---

## 2. Verdict Model

| Verdict | UI | Meaning |
|---------|-----|---------|
| `FREE` | Green | Downloads/exports appear free for the core task |
| `LIMITED_FREE` | Yellow | Free tier exists; export/resolution/watermark may be gated |
| `LIKELY_PAID` | Orange | Strong evidence export requires payment; not 100% confirmed |
| `PAID_REQUIRED` | Red | Confirmed: payment required before completing download/export |
| `UNKNOWN` | Gray | Insufficient evidence |

Every response includes:

```json
{
  "domain": "canva.com",
  "verdict": "LIMITED_FREE",
  "confidence": 0.96,
  "community_reports": 428,
  "last_verified_at": "2026-07-18T12:00:00Z",
  "reason": "Free plan allows limited exports; Pro required for watermark-free / high-res.",
  "features": {
    "free_download": false,
    "limited_free_export": true,
    "watermark": true,
    "locks_high_res": true,
    "requires_subscription_for_export": true,
    "login_only": false
  },
  "capabilities": [
    { "key": "editing", "label": "Editing", "status": "FREE", "is_core_task": false },
    { "key": "png_download", "label": "PNG Download", "status": "FREE", "is_core_task": true },
    { "key": "svg_export", "label": "SVG Export", "status": "LOCKED", "is_core_task": false },
    { "key": "background_remover", "label": "Background Remover", "status": "LOCKED", "is_core_task": false }
  ],
  "version": 14
}
```

### Handling free + paid sites

Most creative tools are hybrid. **Never collapse them to PAID_REQUIRED just because a pricing page exists.**

Decision rule:

1. If core export works free with no watermark → `FREE`
2. If free export exists but watermark / low-res / quota → `LIMITED_FREE`
3. If majority of community + pricing evidence says export needs plan → `LIKELY_PAID` or `PAID_REQUIRED`
4. Upgrade CTAs without export gating (ChatGPT, Claude) → `FREE` (or `UNKNOWN` if unclear)

The **task axis** matters: classify against *download/export completion*, not against "does this company sell subscriptions."

### Per-feature capability breakdown

A single site-level verdict hides useful nuance on hybrid sites. Alongside the top-level verdict, store a **capability matrix**: one row per distinct action a user might come to do, each independently marked `FREE`, `LOCKED`, or `LIMITED`.

```
Canva
  Editing              🟢 Free
  PNG Download         🟢 Free
  SVG Export           🔒 Pro
  Background Remover   🔒 Pro

Community Confidence: 97%
```

The top-level verdict (`LIMITED_FREE` for Canva) is a **rollup** of the capability matrix, not a separate judgment:

- All capabilities `FREE` → `FREE`
- Core task (first-listed / most-searched capability) `FREE`, at least one other `LOCKED` → `LIMITED_FREE`
- Core task `LOCKED` or `LIMITED`, majority of capabilities `LOCKED` → `LIKELY_PAID` / `PAID_REQUIRED`
- No capability data yet → `UNKNOWN`

Which capability is "core" is domain-specific (e.g. for Canva it's PNG download, not SVG); this mapping is itself stored per-site (see `site_capabilities.is_core_task`) so the rollup rule stays mechanical rather than re-litigated per domain.

---

## 3. System Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                     Chrome Extension (MV3)                       │
│  Content Script ──► Service Worker ──► Local Cache (chrome.storage)│
│       │                    │                                     │
│       │                    ▼                                     │
│       │            Classification Client                         │
└───────┼────────────────────┼─────────────────────────────────────┘
        │                    │ HTTPS
        ▼                    ▼
┌─────────────────────────────────────────────────────────────────┐
│                         Edge / CDN                               │
│              (TLS, WAF, rate limit, geo routing)                 │
└────────────────────────────┬────────────────────────────────────┘
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│                      API Gateway (stateless)                     │
│   GET /v1/sites/:domain   POST /v1/reports   POST /v1/classify  │
└───────┬────────────────────┬────────────────────┬───────────────┘
        │                    │                    │
        ▼                    ▼                    ▼
┌───────────────┐   ┌────────────────┐   ┌────────────────────┐
│ Redis Hot     │   │ Site Lookup +  │   │ Report Ingest      │
│ Cache         │   │ Risk Scoring   │   │ (trust-weighted)   │
└───────┬───────┘   └───────┬────────┘   └─────────┬──────────┘
        │                   │                      │
        │                   ▼                      ▼
        │           ┌───────────────┐      ┌───────────────┐
        └──────────►│   Postgres    │◄─────│ Moderation Q  │
                    │  (source of   │      └───────────────┘
                    │   truth)      │
                    └───────┬───────┘
                            │
              ┌─────────────┼─────────────┐
              ▼             ▼             ▼
       ┌────────────┐ ┌──────────┐ ┌────────────────┐
       │ AI Worker  │ │ Refresh  │ │ Object Store   │
       │ (queue)    │ │ Scheduler│ │ (snapshots)    │
       └────────────┘ └──────────┘ └────────────────┘
```

### Components

| Layer | Role |
|-------|------|
| Extension | Domain ID, local cache, UI, privacy-safe signals, report UI |
| API | Lookup, classify enqueue, reports, health |
| Redis | Sub-50ms hot path for popular domains |
| Postgres | Sites, features, reports, scores, AI history, versions |
| Queue + AI workers | Analyze unknowns/stale only |
| Object store | HTML fingerprints, pricing snapshots (no PII) |
| Scheduler | Re-analysis when stale or drift detected |

---

## 4. Data Flow

### Fast path (≈95%+ of visits after warmup)

```
User opens canva.com
  → SW normalizes domain
  → chrome.storage hit? → show verdict (<50ms)
  → miss → GET /v1/sites/canva.com
  → Redis hit → return
  → Redis miss → Postgres → warm Redis → return
```

### Cold path (unknown domain)

```
Unknown domain
  → API returns UNKNOWN + analysis_queued: true
  → Extension shows "Checking…" briefly
  → Job enqueued (deduped by domain)
  → Worker: fetch public pages + pricing + signals
  → Risk engine + optional AI
  → Write sites + version + cache
  → Extension polls or receives push via next lookup
```

### Stale / drift path

```
last_verified_at older than TTL
  OR content fingerprint changed
  OR report spike
  → enqueue re-analysis (version n+1)
  → serve previous verdict until new one commits
```

---

## 5. Folder Structure

```text
freeornot/
├── extension/
│   ├── manifest.json
│   ├── src/
│   │   ├── background/
│   │   │   ├── service-worker.ts
│   │   │   ├── cache.ts
│   │   │   └── api-client.ts
│   │   ├── content/
│   │   │   ├── content.ts
│   │   │   ├── signals.ts          # light DOM signals only
│   │   │   └── banner.ts
│   │   ├── popup/
│   │   │   ├── popup.html
│   │   │   ├── popup.ts
│   │   │   └── popup.css
│   │   └── shared/
│   │       ├── types.ts
│   │       ├── domain.ts            # eTLD+1 normalization
│   │       └── verdict.ts
│   └── icons/
├── backend/
│   ├── cmd/api/main.go              # or Python FastAPI — pick one stack
│   ├── internal/
│   │   ├── handlers/
│   │   ├── services/
│   │   │   ├── lookup.go
│   │   │   ├── scoring.go
│   │   │   ├── community.go
│   │   │   └── cache.go
│   │   ├── workers/
│   │   │   ├── ai_analyzer.go
│   │   │   └── refresh.go
│   │   └── models/
│   ├── migrations/
│   └── Dockerfile
├── db/
│   └── schema.sql
├── packages/
│   └── shared-types/                # OpenAPI-generated client types
├── infra/
│   ├── terraform/
│   ├── k8s/
│   └── redis/
├── docs/
│   └── paywall-detector-system-design.md
└── README.md
```

---

## 6. Backend API

Base: `https://api.freeornot.app/v1`  
Auth: extension API key + optional anonymous install ID (UUID, not PII)

### `GET /v1/sites/{domain}`

Lookup classification. Always fast; never blocks on AI.

**Response 200**

```json
{
  "domain": "canva.com",
  "verdict": "LIMITED_FREE",
  "confidence": 0.96,
  "community_reports": 428,
  "last_verified_at": "2026-07-18T12:00:00Z",
  "reason": "…",
  "features": { },
  "capabilities": [
    { "key": "editing", "label": "Editing", "status": "FREE", "is_core_task": false },
    { "key": "png_download", "label": "PNG Download", "status": "FREE", "is_core_task": true },
    { "key": "svg_export", "label": "SVG Export", "status": "LOCKED", "is_core_task": false },
    { "key": "background_remover", "label": "Background Remover", "status": "LOCKED", "is_core_task": false }
  ],
  "version": 14,
  "analysis_status": "ready"
}
```

**Response 200 (unknown)**

```json
{
  "domain": "newtool.io",
  "verdict": "UNKNOWN",
  "confidence": 0.0,
  "analysis_status": "queued",
  "job_id": "…"
}
```

### `POST /v1/sites/classify`

Request analysis for unknown/stale. Idempotent per domain.

```json
{
  "domain": "newtool.io",
  "url": "https://newtool.io/editor",
  "signals": {
    "pricing_link_present": true,
    "export_buttons": ["Download", "Export PNG"],
    "watermark_hint": false,
    "payment_sdk_hosts": [],
    "page_title": "NewTool Editor"
  }
}
```

Privacy: **no full HTML, no cookies, no file contents, no account data.**

### `POST /v1/reports`

```json
{
  "domain": "canva.com",
  "report_type": "watermark_on_export",
  "verdict_claimed": "LIMITED_FREE",
  "note": "optional short note",
  "install_id": "uuid"
}
```

### `GET /v1/sites/{domain}/status`

Poll analysis job (for UNKNOWN → ready transition).

### `POST /v1/internal/refresh` (admin / scheduler)

Force re-analysis.

### API principles

- Read path p99 < 100ms (cached)
- Classify is async; never stall the extension on LLM
- Rate limit: lookups generous; reports and classify strict
- Version field for client cache coherence

---

## 7. Database Schema

```sql
-- Canonical site record
CREATE TABLE sites (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  domain            TEXT NOT NULL,
  normalized_domain TEXT NOT NULL UNIQUE,  -- eTLD+1 lowercased
  verdict           TEXT NOT NULL CHECK (verdict IN (
                      'FREE','LIMITED_FREE','LIKELY_PAID','PAID_REQUIRED','UNKNOWN')),
  confidence        NUMERIC(5,4) NOT NULL DEFAULT 0,
  community_report_count INT NOT NULL DEFAULT 0,
  last_verified_at  TIMESTAMPTZ,
  last_analyzed_at  TIMESTAMPTZ,
  content_fingerprint TEXT,
  version           INT NOT NULL DEFAULT 1,
  reason            TEXT,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_sites_verdict ON sites(verdict);
CREATE INDEX idx_sites_verified ON sites(last_verified_at);

-- Structured product capabilities (answers the "backend questions")
CREATE TABLE pricing_features (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  site_id           UUID NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
  free_download     BOOLEAN,
  limited_free_export BOOLEAN,
  requires_payment_before_download BOOLEAN,
  requires_payment_before_export BOOLEAN,
  adds_watermark    BOOLEAN,
  locks_high_res    BOOLEAN,
  requires_subscription_after_create BOOLEAN,
  login_only        BOOLEAN,
  has_free_and_paid BOOLEAN,
  pricing_page_url  TEXT,
  evidence          JSONB NOT NULL DEFAULT '{}',
  version           INT NOT NULL DEFAULT 1,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (site_id, version)
);

-- Per-feature capability matrix (Editing / PNG Download / SVG Export / …)
CREATE TABLE site_capabilities (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  site_id           UUID NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
  key               TEXT NOT NULL,           -- stable slug, e.g. 'svg_export'
  label             TEXT NOT NULL,           -- display text, e.g. 'SVG Export'
  status            TEXT NOT NULL CHECK (status IN ('FREE','LIMITED','LOCKED')),
  is_core_task      BOOLEAN NOT NULL DEFAULT false,  -- drives verdict rollup
  evidence          JSONB NOT NULL DEFAULT '{}',
  version           INT NOT NULL DEFAULT 1,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (site_id, key, version)
);

CREATE INDEX idx_capabilities_site ON site_capabilities(site_id);

-- Community reports
CREATE TABLE community_reports (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  site_id           UUID NOT NULL REFERENCES sites(id),
  install_id_hash   TEXT,              -- hashed, not raw UUID in analytics joins
  report_type       TEXT NOT NULL,
  verdict_claimed   TEXT,
  note              TEXT,
  trust_weight      NUMERIC(5,4) NOT NULL DEFAULT 0.5,
  moderation_state  TEXT NOT NULL DEFAULT 'pending'
                      CHECK (moderation_state IN
                        ('pending','accepted','rejected','flagged')),
  ip_hash           TEXT,              -- salted hash for abuse only
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_reports_site ON community_reports(site_id, created_at DESC);
CREATE UNIQUE INDEX idx_reports_dedupe
  ON community_reports(site_id, install_id_hash, report_type, (created_at::date));

-- Explainable score snapshots
CREATE TABLE confidence_scores (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  site_id           UUID NOT NULL REFERENCES sites(id),
  score             NUMERIC(5,4) NOT NULL,
  signal_breakdown  JSONB NOT NULL,
  model_version     TEXT,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- AI runs (cost tracking + audit)
CREATE TABLE ai_analysis_history (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  site_id           UUID REFERENCES sites(id),
  domain            TEXT NOT NULL,
  prompt_hash       TEXT,
  model_name        TEXT,
  status            TEXT NOT NULL,  -- queued|running|succeeded|failed
  input_snapshot    JSONB,
  output_snapshot   JSONB,
  cost_usd          NUMERIC(10,6),
  latency_ms        INT,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Materialized API payloads (optional; Redis is primary hot cache)
CREATE TABLE cached_results (
  site_id           UUID PRIMARY KEY REFERENCES sites(id),
  response_payload  JSONB NOT NULL,
  expires_at        TIMESTAMPTZ NOT NULL,
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Version history for re-analysis
CREATE TABLE site_versions (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  site_id           UUID NOT NULL REFERENCES sites(id),
  version           INT NOT NULL,
  verdict           TEXT NOT NULL,
  confidence        NUMERIC(5,4),
  evidence          JSONB,
  changed_fields    JSONB,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (site_id, version)
);

-- Reporter reputation
CREATE TABLE reporter_trust (
  install_id_hash   TEXT PRIMARY KEY,
  trust_score       NUMERIC(5,4) NOT NULL DEFAULT 0.5,
  accepted_count    INT NOT NULL DEFAULT 0,
  rejected_count    INT NOT NULL DEFAULT 0,
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Analysis job queue mirror (if not using Redis Streams alone)
CREATE TABLE analysis_jobs (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  domain            TEXT NOT NULL,
  status            TEXT NOT NULL DEFAULT 'queued',
  priority          INT NOT NULL DEFAULT 100,
  attempts          INT NOT NULL DEFAULT 0,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  started_at        TIMESTAMPTZ,
  finished_at       TIMESTAMPTZ
);

CREATE UNIQUE INDEX idx_jobs_active_domain
  ON analysis_jobs(domain) WHERE status IN ('queued','running');
```

---

## 8. Risk Scoring Engine

**Never let keywords alone decide.**

### Signal set

| Signal | Weight | Notes |
|--------|-------:|-------|
| Known DB classification (prior version) | 0.30 | Strong prior |
| Community consensus (trust-weighted) | 0.22 | Needs N effective reports |
| Pricing page / plan matrix analysis | 0.18 | Structured features |
| Export/download flow analysis | 0.12 | CTA + disabled state |
| Watermark indicators | 0.08 | Text/UI patterns + AI |
| Payment SDK presence | 0.05 | Weak alone (Stripe ≠ paywall) |
| DOM keyword heuristics | 0.03 | Lowest weight; FP prone |
| AI structured reasoning | 0.02 | Tie-breaker / cold start |

Weights sum ≈ 1.0; unused signals redistribute or leave score lower → `UNKNOWN`.

### Formula

```
raw = Σ (w_i * s_i)   // s_i ∈ [0,1], oriented toward "payment required for export"
raw = raw - false_positive_penalties

// Penalties examples:
// - Upgrade CTA but no export gating evidence: -0.25
// - Chat/productivity SaaS with no download product: -0.35
// - Login wall only: -0.20
```

### Verdict mapping (payment-risk axis)

| Raw score | Verdict |
|-----------|---------|
| ≥ 0.85 | `PAID_REQUIRED` |
| 0.65–0.85 | `LIKELY_PAID` |
| 0.40–0.65 | `LIMITED_FREE` |
| 0.15–0.40 | `FREE` (if positive free-export evidence) else `UNKNOWN` |
| < 0.15 | `UNKNOWN` |

### Confidence

Separate from verdict:

```
confidence = evidence_coverage * agreement * freshness

evidence_coverage = fraction of high-weight signals present
agreement = 1 - variance(community, AI, pricing)
freshness = decay(days_since_verified)
```

Show `confidence` as percent in UI (e.g. 96%).

### Anti-false-positive rules (hard)

1. Keyword "Upgrade" / "Free trial" alone → **never** raise above `UNKNOWN`.
2. Stripe/PayPal script alone → **never** raise above `LIMITED_FREE`.
3. If `login_only` and free export after login → `FREE`.
4. If product is chat/Q&A with no downloadable artifact → default `FREE`.
5. Hybrid freemium with clear free export path → prefer `LIMITED_FREE` over `PAID_REQUIRED`.

---

## 9. AI Workflow

AI runs **only when**:

- domain never classified, or
- classification older than freshness TTL, or
- fingerprint/pricing drift, or
- community strongly disagrees with stored verdict

### Pipeline

1. Deduplicate job by `normalized_domain`
2. Fetch: homepage, `/pricing`, editor URL (server-side crawl; respect robots where required)
3. Extract structured facts (plans, limits, watermark, export)
4. Merge extension-provided light signals
5. Call LLM with **structured JSON schema** output
6. Feed AI output as *one signal* into risk engine (not the sole decider)
7. Persist `ai_analysis_history`, `pricing_features`, `site_versions`, update `sites`
8. Invalidate Redis + bump version

### Cost controls

- Domain-level lock (one AI job at a time)
- Negative cache for failures (1–6h)
- Prefer cheaper model for triage; escalate only on low confidence
- Cap tokens; no full HTML dumps
- Pre-seed top 500 creative tools manually / semi-manually at launch

---

## 10. Chrome Extension Workflow

### On navigation (`webNavigation.onCommitted` / tab update)

1. Normalize hostname → eTLD+1 (`canva.com`, not `www.canva.com`)
2. Skip internal browsers pages, localhost (configurable)
3. Read local cache by domain
4. On hit + fresh → paint badge/banner
5. On miss → `GET /v1/sites/{domain}`
6. Store result locally with TTL by verdict
7. If `UNKNOWN` + queued → show checking state; re-fetch after delay / on focus

### Content script role (narrowed)

- Collect **light** signals only when backend asks or for classify POST
- Do **not** drive verdict from keywords alone
- Optional: detect "user entered editor" to prioritize classify priority

### UI

| Verdict | Copy |
|---------|------|
| FREE | "This website appears to allow free downloads." |
| LIMITED_FREE | "Some features require payment. Check export options before spending significant time." |
| LIKELY_PAID | "Users frequently report that exporting or downloading requires a paid plan." |
| PAID_REQUIRED | "This website is known to require payment before completing downloads or exports." |
| UNKNOWN | "Not enough data yet. We're analyzing this site." |

Also show: Confidence, Community Reports, Last Verified.

### Popup: capability matrix (when available)

For sites with a stored `capabilities` array, the popup shows the feature-level breakdown above the summary copy, so users see *which* action is safe before reading anything else:

```
Canva
  Editing              🟢 Free
  PNG Download          🟢 Free
  SVG Export            🔒 Pro
  Background Remover    🔒 Pro

Confidence: 96%   Community Reports: 428   Last Verified: 3 days ago
```

Sites with no capability data fall back to the single-verdict banner only — the matrix is additive, never blocking.

---

## 11. Caching Strategy

| Layer | TTL examples | Purpose |
|-------|--------------|---------|
| Extension `chrome.storage` | FREE/PAID: 7–30d; LIMITED/LIKELY: 3–7d; UNKNOWN: 1–2h | Instant UX |
| Redis | Same bands + version key | Million-user hot path |
| Postgres | Durable | Source of truth |
| CDN (optional) | Public verdict JSON for top domains | Extreme scale |

**Invalidation:** version bump on re-analysis; clients discard if `version` newer than cached.

**Seed list:** ship extension with baked-in JSON of top ~200 domains for first-paint offline.

---

## 12. Community Moderation

### Report types

- `paywall_before_export`
- `watermark_on_export`
- `subscription_after_create`
- `limited_free`
- `false_positive` (site wrongly flagged)
- `now_free` / `policy_changed`

### Trust

```
effective_weight = reporter.trust_score * report_quality
consensus = sum(weights) for accepted reports / threshold
```

- New installs start at 0.5
- Correct reports (agree with later verified) ↑ trust
- Rejected / contradicted ↓ trust
- Rate limit: 5 reports/day/install; burst → captcha or hold

### Anti-malicious

- Deduplicate per install/domain/day
- Detect coordinated campaigns (same IP cluster, same timing)
- Require quorum before community alone flips verdict (e.g. effective weight ≥ 8)
- Human mod queue for high-traffic domain flips
- `false_positive` reports can *lower* payment risk score

---

## 13. Security & Privacy

### Privacy

- No full page HTML by default
- No user content, designs, resumes, or files
- Install ID is random UUID; hash at rest for reports
- Clear privacy policy: domain lookups only
- Opt-in for richer diagnostics

### Security

- API keys rotated; abuse keys revoked
- WAF + rate limits on classify/report
- Separate internal worker credentials
- Do not expose prompts or raw model output to clients
- Parameterized SQL; validate domain (no SSRF to internal IPs in crawler)
- Crawler URL allowlist schemes `http/https` only; block link-local/metadata IPs

---

## 14. Scalability (millions of users)

| Concern | Approach |
|---------|----------|
| Read QPS | Redis + optional CDN; stateless API replicas |
| Write/report QPS | Async ingest; batch trust updates |
| AI cost | Analyze once; serve forever until stale |
| Domains | Most traffic hits top 10k domains — pre-warm |
| DB | Postgres primary + read replicas; partition reports by time |
| Queue | Redis Streams / SQS; autoscaling workers |
| Multi-region | Regional Redis; global Postgres or regional read replicas |

**SLO targets**

- Lookup p50 < 30ms (cached), p99 < 100ms
- Extension time-to-banner < 2s on cold miss
- AI cold-start < 60s for new domain (async OK)

---

## 15. Re-analysis & Versioning

Triggers:

- Age > TTL for verdict class
- `content_fingerprint` delta above threshold
- Pricing URL change
- Community spike
- Manual admin refresh

Lifecycle: serve `version n` → compute `n+1` → atomic swap → invalidate caches.

---

## 16. Worked Examples

### Canva

Pricing + watermark + community → `LIMITED_FREE` or `LIKELY_PAID`, high confidence.

### ChatGPT / Claude

Upgrade upsell, but chat output copyable free → `FREE`. Keywords ignored via hard rules.

### Stripe-powered SaaS with free CSV export

SDK present, free export confirmed → `FREE` or `LIMITED_FREE`, SDK weight alone insufficient.

### Obscure logo maker, first visitor

`UNKNOWN` → queue AI → later users get instant `PAID_REQUIRED`.

---

## 16b. Current Implementation Status

What's live in `server/server.py` + `extension/` today vs. what above is still design-only:

| Piece | Status |
|-------|--------|
| Domain-cache-first flow (`lookup → cache → classify`) | ✅ Built |
| AI-on-unknown-only, persist-forever | ✅ Built |
| 5-verdict model + confidence + `last_verified_at` | ✅ Built |
| Community reports write into the knowledge base | ✅ Built, but as a **report-count threshold** (`≥1` → `LIKELY_PAID`, `≥3` → `PAID_REQUIRED`), not the trust-weighted consensus in §12 |
| Anti-FP hard rules (§8 rule 1–5) | ⚠️ Partial — enforced only via prompt wording, not as a post-scoring guard the code applies |
| Weighted multi-signal risk engine (§8 formula) | ❌ Not built — verdict currently comes straight from one AI call or the report threshold, no weighted blend |
| Per-feature capability matrix (§2, §10) | ❌ Not built — `features` is a loose AI-provided bag of booleans, not a structured `site_capabilities` table with rollup |
| Redis hot cache | ❌ Not built — `classified_sites.json` is the only store |
| Postgres | ❌ Not built — flat JSON files (`classified_sites.json`, `reports.json`) |
| Versioning / `site_versions` | ❌ Not built — `version` field exists but always `1` |
| Reporter trust / anti-abuse | ❌ Not built — no dedupe, no rate limit, no trust score |
| AI cost tracking (`ai_analysis_history`) | ❌ Not built — no cost/latency logging |

This is the honest gap between "design" and "shipped." The MVP correctly proves the *philosophy* (cache-first, AI-once, community-teaches-the-network); the production hardening in §7–§9 and §12 is the next build phase, not yet code.

---

## 17. Roadmap

| Phase | Ship |
|-------|------|
| **P0 MVP** | Domain lookup API, Postgres, extension cache, seed top 200, basic UI, reports |
| **P1** | Risk scoring, Redis, trust weights, freshness TTLs |
| **P2** | AI worker + queue, versioning, fingerprint drift |
| **P3** | Moderation tools, abuse detection, multi-region cache |
| **P4** | Category intents (PDF, video, resume), public API, partnerships |

---

## 18. What to Delete from Current Extension

Current code scans keywords, Stripe, and calls Gemini on uncertainty — that causes ChatGPT/Claude FPs.

Keep temporarily only as **low-weight signals** fed to backend classify, then remove as primary decision path.

New primary path: **cache → API → (rare) AI worker**.

---

## 19. Summary

Build a **domain classification knowledge engine**:

1. Identify site immediately  
2. Cache locally + Redis + Postgres  
3. AI only for unknown/stale  
4. Weighted multi-signal scoring with hard anti-FP rules  
5. Community reports with trust and moderation  
6. Versioned re-analysis as sites change  

That is the production architecture for millions of users — not a smarter DOM scraper.
