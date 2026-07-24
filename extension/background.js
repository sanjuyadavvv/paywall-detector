// background.js — dynamic knowledge engine
// cache hit → return | miss → analyze once → store for everyone
// force=true → bypass cache shortcut and re-run AI (used for late paywall reveals)

const API_BASE = "http://localhost:8787";
const CACHE_KEY = "freeornot_cache_v4";

const TTL = {
  FREE: 1000 * 60 * 60 * 24 * 2, // strong FREE; weak uses shorter below
  PAID_REQUIRED: 1000 * 60 * 60 * 24 * 14,
  LIMITED_FREE: 1000 * 60 * 60 * 24 * 7,
  LIKELY_PAID: 1000 * 60 * 60 * 24 * 7,
  UNKNOWN: 1000 * 60 * 30 // short — allow retry / classify soon
};

const tabResults = new Map();
const inFlightClassify = new Map(); // domain → Promise

function normalizeDomain(domain) {
  let d = String(domain || "").trim().toLowerCase();
  d = d.replace(/^https?:\/\//, "").split("/")[0];
  return d.replace(/^www\./, "");
}

function ttlFor(result) {
  const verdict = String(result?.verdict || "UNKNOWN");
  const conf = Number(result?.confidence || 0);
  if (verdict === "UNKNOWN") return TTL.UNKNOWN;
  // Weak FREE expires fast so late-paywall sites get re-analyzed
  if (verdict === "FREE" && conf < 0.75) return 1000 * 60 * 60 * 6;
  return TTL[verdict] || TTL.UNKNOWN;
}

function isSolidVerdict(result) {
  if (!result) return false;
  if (result.needs_classification) return false;
  const verdict = String(result.verdict || "UNKNOWN");
  if (!verdict || verdict === "UNKNOWN") return false;
  if (verdict === "FREE" && Number(result.confidence || 0) < 0.7) return false;
  return true;
}

function storageGet(keys) {
  return new Promise((resolve) => chrome.storage.local.get(keys, resolve));
}

function storageSet(obj) {
  return new Promise((resolve) => chrome.storage.local.set(obj, resolve));
}

async function getCache() {
  const data = await storageGet([CACHE_KEY]);
  return data[CACHE_KEY] || {};
}

async function putCache(domain, result) {
  if (!domain || !result) return;
  // Don't cache UNKNOWN — keep domain open for retry
  if (String(result.verdict || "") === "UNKNOWN") return;
  const cache = await getCache();
  cache[domain] = { fetchedAt: Date.now(), result };
  await storageSet({ [CACHE_KEY]: cache });
}

async function fetchBackend(domain) {
  const res = await fetch(`${API_BASE}/v1/sites/${encodeURIComponent(domain)}`);
  if (!res.ok) throw new Error(`lookup ${res.status}`);
  return res.json();
}

async function classifyUnknown(domain, url, signals, pageExcerpt, force) {
  // When forced, don't dedupe against an in-flight job for a *previous*
  // (stale) classification — but do still dedupe concurrent forced calls.
  const key = force ? `${domain}:force` : domain;

  if (inFlightClassify.has(key)) {
    return inFlightClassify.get(key);
  }

  const job = (async () => {
    const res = await fetch(`${API_BASE}/v1/sites/classify`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        domain,
        url,
        signals: signals || {},
        page_excerpt: pageExcerpt || "",
        force: !!force
      })
    });
    if (!res.ok) {
      const body = await res.text().catch(() => "");
      throw new Error(`classify ${res.status}${body ? `: ${body.slice(0, 200)}` : ""}`);
    }
    return res.json();
  })().finally(() => {
    inFlightClassify.delete(key);
  });

  inFlightClassify.set(key, job);
  return job;
}

/**
 * Lookup only — never calls AI.
 * Returns needs_classification: true when the knowledge base has no answer yet.
 */
async function lookupSite(domain) {
  const normalized = normalizeDomain(domain);
  if (!normalized) {
    return { verdict: "UNKNOWN", confidence: 0, reason: "No domain.", domain: "" };
  }

  const cache = await getCache();
  const cached = cache[normalized];
  if (cached?.result) {
    const verdict = cached.result.verdict || "UNKNOWN";
    const age = Date.now() - cached.fetchedAt;
    if (verdict !== "UNKNOWN" && age < ttlFor(cached.result) && isSolidVerdict(cached.result)) {
      return { ...cached.result, from_cache: true };
    }
  }

  try {
    const remote = await fetchBackend(normalized);
    if (remote.verdict && remote.verdict !== "UNKNOWN" && isSolidVerdict(remote)) {
      await putCache(normalized, remote);
    }
    return remote;
  } catch {
    return {
      domain: normalized,
      verdict: "UNKNOWN",
      confidence: 0,
      reason: "Could not reach classification server.",
      community_reports: 0,
      source: "offline",
      needs_classification: true,
      analysis_status: "unavailable"
    };
  }
}

async function reportSite(payload) {
  const res = await fetch(`${API_BASE}/v1/reports`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      ...payload,
      domain: normalizeDomain(payload?.domain)
    })
  });
  if (!res.ok) throw new Error(`report ${res.status}`);
  return res.json();
}

function setTabResult(tabId, result) {
  if (tabId == null) return;
  tabResults.set(tabId, result);
}

function updateBadge(tabId, result) {
  const v = String(result?.verdict || "UNKNOWN");
  const map = {
    FREE: { text: "FREE", color: "#1e7a34" },
    LIMITED_FREE: { text: "LTD", color: "#b78100" },
    LIKELY_PAID: { text: "PAY?", color: "#c05600" },
    PAID_REQUIRED: { text: "PAY", color: "#c53030" },
    UNKNOWN: { text: "…", color: "#718096" }
  };
  const cfg = map[v] || map.UNKNOWN;
  chrome.action.setBadgeText({ tabId, text: cfg.text });
  chrome.action.setBadgeBackgroundColor({ tabId, color: cfg.color });
}

chrome.webNavigation.onCommitted.addListener((details) => {
  if (details.frameId !== 0) return;
  let domain = "";
  try {
    domain = normalizeDomain(new URL(details.url).hostname);
  } catch {
    return;
  }
  // Lookup only here — content script triggers classify if needed (has page text)
  lookupSite(domain)
    .then((result) => {
      setTabResult(details.tabId, result);
      updateBadge(details.tabId, result);
      if (result.verdict && result.verdict !== "UNKNOWN") {
        chrome.tabs.sendMessage(
          details.tabId,
          { type: "SITE_VERDICT", result },
          () => void chrome.runtime.lastError
        );
      }
    })
    .catch(() => {});
});

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  const tabId = sender.tab?.id ?? msg.tabId;

  if (msg.type === "CHECK_DOMAIN") {
    lookupSite(msg.payload?.domain)
      .then((result) => {
        setTabResult(tabId, result);
        if (tabId != null) updateBadge(tabId, result);
        sendResponse({ ok: true, result });
      })
      .catch((err) => sendResponse({ ok: false, error: String(err) }));
    return true;
  }

  if (msg.type === "CLASSIFY_UNKNOWN") {
    const domain = normalizeDomain(msg.payload?.domain);
    const force = !!msg.payload?.force;

    lookupSite(domain)
      .then(async (existing) => {
        // Skip AI only if we already have a solid verdict AND this isn't a
        // forced re-check (forced = page revealed new payment signals).
        if (!force && isSolidVerdict(existing)) {
          setTabResult(tabId, existing);
          if (tabId != null) updateBadge(tabId, existing);
          sendResponse({ ok: true, result: existing, skipped_ai: true });
          return;
        }

        const result = await classifyUnknown(
          domain,
          msg.payload?.url,
          msg.payload?.signals,
          msg.payload?.pageExcerpt || msg.payload?.page_excerpt,
          force
        );
        if (result?.verdict) {
          await putCache(domain, result);
        }
        setTabResult(tabId, result);
        if (tabId != null) updateBadge(tabId, result);
        if (tabId != null) {
          chrome.tabs.sendMessage(
            tabId,
            { type: "SITE_VERDICT", result },
            () => void chrome.runtime.lastError
          );
        }
        sendResponse({ ok: true, result });
      })
      .catch((err) => sendResponse({ ok: false, error: String(err) }));
    return true;
  }

  if (msg.type === "GET_TAB_RESULT") {
    sendResponse({ ok: true, result: tabResults.get(msg.tabId ?? tabId) || null });
    return true;
  }

  if (msg.type === "REPORT_SITE") {
    const domain = normalizeDomain(msg.payload?.domain);
    reportSite(msg.payload)
      .then(async (r) => {
        const result =
          r.result ||
          {
            domain,
            verdict: "LIKELY_PAID",
            confidence: 0.82,
            reason: "Community report: payment required before download/export.",
            source: "community",
            last_verified_at: new Date().toISOString()
          };
        await putCache(domain, result);
        setTabResult(tabId, result);
        if (tabId != null) {
          updateBadge(tabId, result);
          chrome.tabs.sendMessage(
            tabId,
            { type: "SITE_VERDICT", result },
            () => void chrome.runtime.lastError
          );
        }
        sendResponse({ ok: true, result, ...r });
      })
      .catch(async (err) => {
        const fallback = {
          domain,
          verdict: "LIKELY_PAID",
          confidence: 0.8,
          reason: "Local report saved — server unreachable.",
          source: "local_report",
          last_verified_at: new Date().toISOString()
        };
        await putCache(domain, fallback);
        sendResponse({ ok: true, result: fallback, offline: true, error: String(err) });
      });
    return true;
  }
});
