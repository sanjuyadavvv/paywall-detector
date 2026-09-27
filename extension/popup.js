const LABELS = {
  FREE: "Free",
  LIMITED_FREE: "Limited Free",
  LIKELY_PAID: "Likely Paid",
  PAID_REQUIRED: "Paid Required",
  UNKNOWN: "Unknown"
};

const SITE_TYPE_LABELS = {
  tool: "Creative / download tool",
  streaming: "Paid streaming / membership",
  news: "News / articles",
  chat: "Chat / AI assistant",
  marketplace: "Store / marketplace",
  other: "Other"
};

function daysAgo(iso) {
  if (!iso) return "—";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return "—";
  const d = Math.max(0, Math.round((Date.now() - t) / (1000 * 60 * 60 * 24)));
  if (d === 0) return "today";
  if (d === 1) return "1 day ago";
  return `${d} days ago`;
}

function render(result, domain) {
  const verdict = String(result?.verdict || "UNKNOWN").toUpperCase().replace(/ /g, "_");
  const card = document.getElementById("card");
  card.className = `card ${verdict in LABELS ? verdict : "UNKNOWN"}`;
  document.getElementById("verdict").textContent = LABELS[verdict] || "Unknown";
  document.getElementById("reason").textContent =
    result?.reason || "No classification yet for this domain.";
  const confidence = Math.round(Number(result?.confidence || 0) * 100);
  const paid = Number(result?.community_paid ?? result?.community_reports ?? 0);
  const free = Number(result?.community_free || 0);
  const siteType = SITE_TYPE_LABELS[result?.site_type] || SITE_TYPE_LABELS.other;
  document.getElementById("meta").innerHTML = `
    <div>Type: ${siteType}</div>
    <div>Confidence: ${confidence}%</div>
    <div>Paid reports: ${paid} · Free reports: ${free}</div>
    <div>Last verified: ${daysAgo(result?.last_verified_at)}</div>
    <div>Source: ${result?.source || "—"}</div>
  `;
  document.getElementById("domain").textContent = domain;
}

function needsLiveClassify(result) {
  if (!result) return true;
  const verdict = String(result.verdict || "UNKNOWN").toUpperCase();
  if (result.analysis_status === "skipped") return false;
  if (result.needs_classification) return true;
  if (verdict === "UNKNOWN") return true;
  if (verdict === "FREE" && Number(result.confidence || 0) < 0.7) return true;
  return false;
}

function applyReportResponse(res, stance, domain) {
  const paidBtn = document.getElementById("report-paid-btn");
  const freeBtn = document.getElementById("report-free-btn");
  const btn = stance === "free" ? freeBtn : paidBtn;
  const other = stance === "free" ? paidBtn : freeBtn;
  if (res && res.ok) {
    if (res.already_reported) {
      btn.textContent = "Already reported";
    } else if (res.status_updated) {
      btn.textContent = "Reported — status updated";
    } else {
      const needed = Number(res.reports_needed || 3);
      const have = stance === "free" ? Number(res.community_free || 0) : Number(res.uniqueReporters || 0);
      btn.textContent = `Reported (${have}/${needed} needed)`;
    }
    btn.disabled = true;
    other.disabled = true;
    if (res.result) render(res.result, domain);
  } else if (res?.rate_limited) {
    btn.textContent = "Too many reports — try later";
  } else {
    btn.textContent = "Report failed — is the server running?";
  }
}

document.addEventListener("DOMContentLoaded", async () => {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab?.url) {
    render({ verdict: "UNKNOWN", reason: "No active page." }, "—");
    return;
  }

  let domain = "—";
  try {
    domain = new URL(tab.url).hostname;
  } catch {
    render({ verdict: "UNKNOWN", reason: "This page cannot be classified." }, tab.url);
    return;
  }

  chrome.runtime.sendMessage({ type: "GET_MUTED" }, (muteRes) => {
    const box = document.getElementById("mute-banner");
    const host = domain.replace(/^www\./, "");
    box.checked = !!(muteRes?.muted && muteRes.muted[host]);
  });

  document.getElementById("mute-banner").addEventListener("change", (e) => {
    chrome.runtime.sendMessage({
      type: "SET_MUTED",
      payload: { domain, muted: e.target.checked }
    });
  });

  chrome.runtime.sendMessage(
    { type: "CHECK_DOMAIN", payload: { domain, url: tab.url }, tabId: tab.id },
    (response) => {
      if (chrome.runtime.lastError || !response?.ok) {
        render(
          { verdict: "UNKNOWN", reason: "Lookup failed. Is the local server running?" },
          domain
        );
        return;
      }

      const result = response.result;
      render(result, domain);

      if (!needsLiveClassify(result) || tab.id == null) return;

      document.getElementById("verdict").textContent = "Analyzing…";
      document.getElementById("reason").textContent =
        "Running live classification for this page…";

      chrome.tabs.sendMessage(tab.id, { type: "CLASSIFY_FOR_POPUP" }, (classifyRes) => {
        if (chrome.runtime.lastError || !classifyRes?.ok) {
          chrome.runtime.sendMessage(
            {
              type: "CLASSIFY_UNKNOWN",
              tabId: tab.id,
              payload: {
                domain,
                url: tab.url,
                signals: { title: domain, path: "/" },
                pageExcerpt: `Page on ${domain}`,
                force: false
              }
            },
            (fallback) => {
              if (fallback?.ok && fallback.result) render(fallback.result, domain);
              else render(result, domain);
            }
          );
          return;
        }
        if (classifyRes.result) render(classifyRes.result, domain);
      });
    }
  );

  function sendReport(stance) {
    chrome.runtime.sendMessage(
      {
        type: "REPORT_SITE",
        tabId: tab.id,
        payload: {
          domain,
          url: tab.url,
          stance,
          note: stance === "free" ? "User says this site is free" : "Manually reported via popup",
          report_type: stance === "free" ? "false_positive" : "paywall_before_export",
          reportedAt: Date.now()
        }
      },
      (res) => applyReportResponse(res, stance, domain)
    );
  }

  document.getElementById("report-paid-btn").addEventListener("click", () => sendReport("paid"));
  document.getElementById("report-free-btn").addEventListener("click", () => sendReport("free"));
});
