const LABELS = {
  FREE: "Free",
  LIMITED_FREE: "Limited Free",
  LIKELY_PAID: "Likely Paid",
  PAID_REQUIRED: "Paid Required",
  UNKNOWN: "Unknown"
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
  const reports = Number(result?.community_reports || 0);
  document.getElementById("meta").innerHTML = `
    <div>Confidence: ${confidence}%</div>
    <div>Community reports: ${reports}</div>
    <div>Last verified: ${daysAgo(result?.last_verified_at)}</div>
    <div>Source: ${result?.source || "—"}</div>
  `;
  document.getElementById("domain").textContent = domain;
}

function needsLiveClassify(result) {
  if (!result) return true;
  const verdict = String(result.verdict || "UNKNOWN").toUpperCase();
  if (result.needs_classification) return true;
  if (verdict === "UNKNOWN") return true;
  if (verdict === "FREE" && Number(result.confidence || 0) < 0.7) return true;
  return false;
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
          // Content script may not be injected (chrome:// etc.) — try background with empty signals
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

  document.getElementById("report-btn").addEventListener("click", () => {
    chrome.runtime.sendMessage(
      {
        type: "REPORT_SITE",
        tabId: tab.id,
        payload: {
          domain,
          url: tab.url,
          note: "Manually reported via popup",
          report_type: "paywall_before_export",
          reportedAt: Date.now()
        }
      },
      (res) => {
        const btn = document.getElementById("report-btn");
        if (res && res.ok) {
          btn.textContent = "Reported — thanks";
          btn.disabled = true;
          if (res.result) render(res.result, domain);
        } else {
          btn.textContent = "Report failed — is the server running?";
        }
      }
    );
  });
});
