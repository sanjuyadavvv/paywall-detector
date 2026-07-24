// // content.js — gather page evidence → classify unknowns dynamically (AI-first)

// (function () {
//   let bannerShown = false;
//   let checkStarted = false;
//   let classifyStarted = false;
//   let watcherArmed = false;
//   let lastClassifiedExcerpt = "";
//   let lastClassifyResult = null;
//   let lastBannerSignature = null;

//   const COPY = {
//     FREE: {
//       title: "Free",
//       message: "This website appears to allow free downloads.",
//       level: "ok"
//     },
//     LIMITED_FREE: {
//       title: "Limited Free",
//       message:
//         "Some features require payment. Check export options before spending significant time.",
//       level: "warn"
//     },
//     LIKELY_PAID: {
//       title: "Likely Paid",
//       message:
//         "This site likely requires payment before you can download or export your work.",
//       level: "warn"
//     },
//     PAID_REQUIRED: {
//       title: "Paid Required",
//       message:
//         "This website is known to require payment before completing downloads or exports.",
//       level: "danger"
//     },
//     UNKNOWN: {
//       title: "Analyzing",
//       message: "Checking whether downloads or exports may require payment…",
//       level: "info"
//     }
//   };

//   // Late-appearing paywall / payment language — used to trigger a re-check
//   // after the user has already interacted with the page (e.g. filled a form,
//   // then hit "Apply"/"Submit" and a payment step shows up).
//   const PAYWALL_SIGNAL_RE =
//     /(pay\s?now|payment required|checkout|enter card|credit card required|subscription required|unlock download|pay to download|upgrade to continue|upgrade to submit|upgrade to export|watermark|buy now)/i;

//   // Buttons that commonly precede a late paywall reveal (apply, submit, continue, etc.)
//   const TRIGGER_BUTTON_RE =
//     /(apply|submit|continue|next|finish|complete|proceed|checkout|save profile|create account|sign up|register)/i;

//   function daysAgo(iso) {
//     if (!iso) return null;
//     const t = Date.parse(iso);
//     if (Number.isNaN(t)) return null;
//     return Math.max(0, Math.round((Date.now() - t) / (1000 * 60 * 60 * 24)));
//   }

//   function metaLine(result) {
//     const confidence = Math.round(Number(result?.confidence || 0) * 100);
//     const reports = Number(result?.community_reports || 0);
//     const days = daysAgo(result?.last_verified_at);
//     const source = result?.source ? `Source: ${result.source}` : null;
//     const parts = [`Confidence: ${confidence}%`, `Community reports: ${reports}`];
//     if (days != null) {
//       parts.push(
//         days === 0 ? "Last verified: today" : `Last verified: ${days} day${days === 1 ? "" : "s"} ago`
//       );
//     }
//     if (source) parts.push(source);
//     return parts.join(" · ");
//   }

//   function showBanner(result) {
//     const verdict = String(result?.verdict || "UNKNOWN").toUpperCase().replace(/ /g, "_");
//     const cfg = COPY[verdict] || COPY.UNKNOWN;

//     if (verdict === "FREE") {
//       bannerShown = true;
//       lastBannerSignature = null;
//       const existing = document.getElementById("pwd-banner");
//       if (existing) existing.remove();
//       return;
//     }

//     // Skip re-rendering the exact same result we're already showing — avoids
//     // banner flicker when duplicate classify responses race each other.
//     const signature = `${verdict}|${result?.reason || ""}|${Number(result?.confidence || 0)}`;
//     if (signature === lastBannerSignature && document.getElementById("pwd-banner")) {
//       return;
//     }
//     lastBannerSignature = signature;

//     const existing = document.getElementById("pwd-banner");
//     if (existing) existing.remove();

//     const banner = document.createElement("div");
//     banner.id = "pwd-banner";
//     banner.className = `pwd-banner pwd-${cfg.level}`;
//     banner.innerHTML = `
//       <div class="pwd-main">
//         <span class="pwd-title">${cfg.title}</span>
//         <span class="pwd-text">${cfg.message}</span>
//         <span class="pwd-meta">${metaLine(result)}</span>
//       </div>
//       <button class="pwd-report" id="pwd-report-btn" type="button">Report</button>
//       <button class="pwd-close" id="pwd-close-btn" type="button" aria-label="Close">✕</button>
//     `;
//     document.documentElement.appendChild(banner);
//     bannerShown = verdict !== "UNKNOWN";

//     document.getElementById("pwd-close-btn").addEventListener("click", () => {
//       banner.remove();
//     });
//     document.getElementById("pwd-report-btn").addEventListener("click", () => {
//       chrome.runtime.sendMessage({
//         type: "REPORT_SITE",
//         payload: {
//           domain: location.hostname,
//           url: location.href,
//           note: `User confirmed from banner (${verdict})`,
//           report_type: "paywall_before_export",
//           verdict_claimed: verdict,
//           reportedAt: Date.now()
//         }
//       });
//       const btn = document.getElementById("pwd-report-btn");
//       if (btn) {
//         btn.textContent = "Reported";
//         btn.disabled = true;
//       }
//     });
//   }

//   function collectSignals() {
//     const bodyText = (document.body?.innerText || "").slice(0, 2500);
//     const lower = bodyText.toLowerCase();
//     const buttons = [];
//     document.querySelectorAll("button, a, [role='button'], input[type='submit']").forEach((el) => {
//       const t = (el.innerText || el.value || el.getAttribute("aria-label") || "")
//         .trim()
//         .toLowerCase();
//       if (!t || t.length > 60) return;
//       if (
//         /(download|export|save|convert|upload|edit|merge|compress|unlock|upgrade|pro|premium|subscribe|pricing|buy|apply|submit|continue|checkout|register|sign up)/.test(
//           t
//         ) &&
//         buttons.length < 20
//       ) {
//         buttons.push(t);
//       }
//     });

//     const hasUpload = !!document.querySelector(
//       'input[type="file"], [class*="upload"], [id*="upload"]'
//     );
//     const pricingLink = !!document.querySelector(
//       'a[href*="pricing"], a[href*="plans"], a[href*="subscribe"]'
//     );

//     const toolKeywords = [
//       "pdf",
//       "convert",
//       "resume",
//       "cv builder",
//       "logo maker",
//       "watermark",
//       "export",
//       "download",
//       "video editor",
//       "design",
//       "compress",
//       "merge pdf",
//       "edit pdf",
//       "remove background",
//       "apply now",
//       "job application",
//       "profile"
//     ];
//     const matched_tools = toolKeywords.filter((k) => lower.includes(k));

//     return {
//       title: document.title || "",
//       path: location.pathname,
//       exportish_buttons: buttons,
//       has_file_upload: hasUpload,
//       pricing_link_present: pricingLink,
//       matched_tool_keywords: matched_tools
//     };
//   }

//   // AI-FIRST: no "looks_like_tool" gate. Every page that reaches this point
//   // gets classified. We can reintroduce cost controls later once accuracy
//   // is confirmed — right now correctness matters more than saving AI calls.
//   function requestClassify(showAnalyzing, force, done) {
//     if (classifyStarted && !force) {
//       // Already classified (or classifying) this page — hand back what we
//       // have instead of null, so callers (e.g. the popup) don't treat this
//       // as a failure and kick off a redundant classify of their own.
//       if (typeof done === "function") done(lastClassifyResult);
//       return;
//     }
//     classifyStarted = true;

//     const signals = collectSignals();
//     const excerpt = (document.body?.innerText || "").slice(0, 2200);
//     lastClassifiedExcerpt = excerpt;

//     if (showAnalyzing) {
//       showBanner({
//         verdict: "UNKNOWN",
//         confidence: 0,
//         reason: "Analyzing…",
//         community_reports: 0,
//         source: "live"
//       });
//     }

//     chrome.runtime.sendMessage(
//       {
//         type: "CLASSIFY_UNKNOWN",
//         payload: {
//           domain: location.hostname,
//           url: location.href,
//           signals,
//           pageExcerpt: excerpt,
//           force: !!force
//         }
//       },
//       (response) => {
//         // allow future forced re-checks even if this one failed
//         if (force) classifyStarted = false;

//         if (chrome.runtime.lastError || !response?.ok) {
//           const b = document.getElementById("pwd-banner");
//           if (b && b.querySelector(".pwd-title")?.textContent === "Analyzing") b.remove();
//           if (typeof done === "function") done(null);
//           return;
//         }
//         const result = response.result;
//         if (result) {
//           lastClassifyResult = result;
//           showBanner(result);
//         }
//         if (typeof done === "function") done(result || null);
//       }
//     );
//   }

//   function looksToolish(signals) {
//     return (
//       (signals.matched_tool_keywords && signals.matched_tool_keywords.length > 0) ||
//       signals.has_file_upload ||
//       (signals.exportish_buttons && signals.exportish_buttons.length > 0) ||
//       signals.pricing_link_present
//     );
//   }

//   function classifyCurrentSite() {
//     if (checkStarted) return;
//     checkStarted = true;

//     chrome.runtime.sendMessage(
//       {
//         type: "CHECK_DOMAIN",
//         payload: { domain: location.hostname, url: location.href }
//       },
//       (response) => {
//         if (chrome.runtime.lastError || !response?.ok) {
//           // Server down — still try classify
//           requestClassify(true);
//           return;
//         }
//         const result = response.result || {};
//         const verdict = String(result.verdict || "UNKNOWN").toUpperCase();
//         const conf = Number(result.confidence || 0);

//         // Strong FREE → quiet. Weak FREE or tool-like page → re-analyze.
//         if (verdict === "FREE") {
//           if (conf >= 0.75 && !result.needs_classification && !looksToolish(collectSignals())) {
//             return;
//           }
//           requestClassify(false, conf < 0.7);
//           return;
//         }

//         if (verdict !== "UNKNOWN" && !result.needs_classification) {
//           showBanner(result);
//           return;
//         }

//         // Not in cache → dynamic analysis (AI), then stored for everyone
//         requestClassify(true);
//       }
//     );
//   }

//   // Re-check when payment language appears on the page AFTER the initial
//   // classification (e.g. profile-building flow that only reveals a paywall
//   // once you click "Apply"). This is the core fix for late-reveal paywalls.
//   function armLateChangeWatcher() {
//     if (watcherArmed) return;
//     watcherArmed = true;

//     let debounceTimer = null;
//     const tryForceCheck = () => {
//       const text = (document.body?.innerText || "").slice(0, 2500);
//       if (PAYWALL_SIGNAL_RE.test(text) && text !== lastClassifiedExcerpt) {
//         lastClassifiedExcerpt = text;
//         requestClassify(true, /* force */ true);
//       }
//     };

//     const observer = new MutationObserver(() => {
//       clearTimeout(debounceTimer);
//       debounceTimer = setTimeout(tryForceCheck, 1200); // let DOM settle
//     });
//     observer.observe(document.body, { childList: true, subtree: true, characterData: true });

//     // Also force a check right when the user clicks a likely "reveal" button
//     // (Apply, Submit, Continue, Checkout, ...) — catches it faster than
//     // waiting for the mutation debounce alone.
//     document.addEventListener(
//       "click",
//       (e) => {
//         const el = e.target.closest("button, a, [role='button'], input[type='submit']");
//         if (!el) return;
//         const label = (el.innerText || el.value || el.getAttribute("aria-label") || "").trim();
//         if (TRIGGER_BUTTON_RE.test(label)) {
//           // Give the page a moment to render whatever comes next, then check.
//           setTimeout(tryForceCheck, 900);
//         }
//       },
//       true
//     );
//   }

//   chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
//     if (msg.type === "SITE_VERDICT" && msg.result) {
//       const v = String(msg.result.verdict || "").toUpperCase();
//       if (v && v !== "FREE" && v !== "UNKNOWN") showBanner(msg.result);
//       return;
//     }

//     // Popup (or background) asks this tab to run live classification
//     if (msg.type === "CLASSIFY_FOR_POPUP") {
//       requestClassify(true, !!msg.force, (result) => {
//         sendResponse({ ok: !!result, result });
//       });
//       return true;
//     }
//   });

//   function init() {
//     classifyCurrentSite();
//     armLateChangeWatcher();
//   }

//   if (document.readyState === "loading") {
//     document.addEventListener("DOMContentLoaded", init);
//   } else {
//     init();
//   }
// })();


// content.js — gather page evidence → classify unknowns dynamically (AI-first)
// NOTE: Auto-run on page load has been disabled. This script now only acts
// when the popup explicitly asks it to (via CLASSIFY_FOR_POPUP), and does so
// silently — no banner is injected into the page.

(function () {
  console.log("FreeOrNot content.js loaded — v2 (silent popup mode)");
  let bannerShown = false;
  let checkStarted = false;
  let classifyStarted = false;
  let watcherArmed = false;
  let lastClassifiedExcerpt = "";
  let lastClassifyResult = null;
  let lastBannerSignature = null;

  const COPY = {
    FREE: {
      title: "Free",
      message: "This website appears to allow free downloads.",
      level: "ok"
    },
    LIMITED_FREE: {
      title: "Limited Free",
      message:
        "Some features require payment. Check export options before spending significant time.",
      level: "warn"
    },
    LIKELY_PAID: {
      title: "Likely Paid",
      message:
        "This site likely requires payment before you can download or export your work.",
      level: "warn"
    },
    PAID_REQUIRED: {
      title: "Paid Required",
      message:
        "This website is known to require payment before completing downloads or exports.",
      level: "danger"
    },
    UNKNOWN: {
      title: "Analyzing",
      message: "Checking whether downloads or exports may require payment…",
      level: "info"
    }
  };

  // Late-appearing paywall / payment language — used to trigger a re-check
  // after the user has already interacted with the page (e.g. filled a form,
  // then hit "Apply"/"Submit" and a payment step shows up).
  const PAYWALL_SIGNAL_RE =
    /(pay\s?now|payment required|checkout|enter card|credit card required|subscription required|unlock download|pay to download|upgrade to continue|upgrade to submit|upgrade to export|watermark|buy now)/i;

  // Buttons that commonly precede a late paywall reveal (apply, submit, continue, etc.)
  const TRIGGER_BUTTON_RE =
    /(apply|submit|continue|next|finish|complete|proceed|checkout|save profile|create account|sign up|register)/i;

  function daysAgo(iso) {
    if (!iso) return null;
    const t = Date.parse(iso);
    if (Number.isNaN(t)) return null;
    return Math.max(0, Math.round((Date.now() - t) / (1000 * 60 * 60 * 24)));
  }

  function metaLine(result) {
    const confidence = Math.round(Number(result?.confidence || 0) * 100);
    const reports = Number(result?.community_reports || 0);
    const days = daysAgo(result?.last_verified_at);
    const source = result?.source ? `Source: ${result.source}` : null;
    const parts = [`Confidence: ${confidence}%`, `Community reports: ${reports}`];
    if (days != null) {
      parts.push(
        days === 0 ? "Last verified: today" : `Last verified: ${days} day${days === 1 ? "" : "s"} ago`
      );
    }
    if (source) parts.push(source);
    return parts.join(" · ");
  }

  function showBanner(result) {
    const verdict = String(result?.verdict || "UNKNOWN").toUpperCase().replace(/ /g, "_");
    const cfg = COPY[verdict] || COPY.UNKNOWN;

    if (verdict === "FREE") {
      bannerShown = true;
      lastBannerSignature = null;
      const existing = document.getElementById("pwd-banner");
      if (existing) existing.remove();
      return;
    }

    // Skip re-rendering the exact same result we're already showing — avoids
    // banner flicker when duplicate classify responses race each other.
    const signature = `${verdict}|${result?.reason || ""}|${Number(result?.confidence || 0)}`;
    if (signature === lastBannerSignature && document.getElementById("pwd-banner")) {
      return;
    }
    lastBannerSignature = signature;

    const existing = document.getElementById("pwd-banner");
    if (existing) existing.remove();

    const banner = document.createElement("div");
    banner.id = "pwd-banner";
    banner.className = `pwd-banner pwd-${cfg.level}`;
    banner.innerHTML = `
      <div class="pwd-main">
        <span class="pwd-title">${cfg.title}</span>
        <span class="pwd-text">${cfg.message}</span>
        <span class="pwd-meta">${metaLine(result)}</span>
      </div>
      <button class="pwd-report" id="pwd-report-btn" type="button">Report</button>
      <button class="pwd-close" id="pwd-close-btn" type="button" aria-label="Close">✕</button>
    `;
    document.documentElement.appendChild(banner);
    bannerShown = verdict !== "UNKNOWN";

    document.getElementById("pwd-close-btn").addEventListener("click", () => {
      banner.remove();
    });
    document.getElementById("pwd-report-btn").addEventListener("click", () => {
      chrome.runtime.sendMessage({
        type: "REPORT_SITE",
        payload: {
          domain: location.hostname,
          url: location.href,
          note: `User confirmed from banner (${verdict})`,
          report_type: "paywall_before_export",
          verdict_claimed: verdict,
          reportedAt: Date.now()
        }
      });
      const btn = document.getElementById("pwd-report-btn");
      if (btn) {
        btn.textContent = "Reported";
        btn.disabled = true;
      }
    });
  }

  function collectSignals() {
    const bodyText = (document.body?.innerText || "").slice(0, 2500);
    const lower = bodyText.toLowerCase();
    const buttons = [];
    document.querySelectorAll("button, a, [role='button'], input[type='submit']").forEach((el) => {
      const t = (el.innerText || el.value || el.getAttribute("aria-label") || "")
        .trim()
        .toLowerCase();
      if (!t || t.length > 60) return;
      if (
        /(download|export|save|convert|upload|edit|merge|compress|unlock|upgrade|pro|premium|subscribe|pricing|buy|apply|submit|continue|checkout|register|sign up)/.test(
          t
        ) &&
        buttons.length < 20
      ) {
        buttons.push(t);
      }
    });

    const hasUpload = !!document.querySelector(
      'input[type="file"], [class*="upload"], [id*="upload"]'
    );
    const pricingLink = !!document.querySelector(
      'a[href*="pricing"], a[href*="plans"], a[href*="subscribe"]'
    );

    const toolKeywords = [
      "pdf",
      "convert",
      "resume",
      "cv builder",
      "logo maker",
      "watermark",
      "export",
      "download",
      "video editor",
      "design",
      "compress",
      "merge pdf",
      "edit pdf",
      "remove background",
      "apply now",
      "job application",
      "profile"
    ];
    const matched_tools = toolKeywords.filter((k) => lower.includes(k));

    return {
      title: document.title || "",
      path: location.pathname,
      exportish_buttons: buttons,
      has_file_upload: hasUpload,
      pricing_link_present: pricingLink,
      matched_tool_keywords: matched_tools
    };
  }

  // AI-FIRST: no "looks_like_tool" gate. Every page that reaches this point
  // gets classified. We can reintroduce cost controls later once accuracy
  // is confirmed — right now correctness matters more than saving AI calls.
  //
  // `silent`: when true, never touches the DOM/banner — used when the popup
  // is the one asking, since the popup renders its own UI from the result.
  function requestClassify(showAnalyzing, force, done, silent) {
    if (classifyStarted && !force) {
      // Already classified (or classifying) this page — hand back what we
      // have instead of null, so callers (e.g. the popup) don't treat this
      // as a failure and kick off a redundant classify of their own.
      if (typeof done === "function") done(lastClassifyResult);
      return;
    }
    classifyStarted = true;

    const signals = collectSignals();
    const excerpt = (document.body?.innerText || "").slice(0, 2200);
    lastClassifiedExcerpt = excerpt;

    if (showAnalyzing && !silent) {
      showBanner({
        verdict: "UNKNOWN",
        confidence: 0,
        reason: "Analyzing…",
        community_reports: 0,
        source: "live"
      });
    }

    chrome.runtime.sendMessage(
      {
        type: "CLASSIFY_UNKNOWN",
        payload: {
          domain: location.hostname,
          url: location.href,
          signals,
          pageExcerpt: excerpt,
          force: !!force
        }
      },
      (response) => {
        // allow future forced re-checks even if this one failed
        if (force) classifyStarted = false;

        if (chrome.runtime.lastError || !response?.ok) {
          if (!silent) {
            const b = document.getElementById("pwd-banner");
            if (b && b.querySelector(".pwd-title")?.textContent === "Analyzing") b.remove();
          }
          if (typeof done === "function") done(null);
          return;
        }
        const result = response.result;
        if (result) {
          lastClassifyResult = result;
          if (!silent) showBanner(result);
        }
        if (typeof done === "function") done(result || null);
      }
    );
  }

  function looksToolish(signals) {
    return (
      (signals.matched_tool_keywords && signals.matched_tool_keywords.length > 0) ||
      signals.has_file_upload ||
      (signals.exportish_buttons && signals.exportish_buttons.length > 0) ||
      signals.pricing_link_present
    );
  }

  function classifyCurrentSite() {
    if (checkStarted) return;
    checkStarted = true;

    chrome.runtime.sendMessage(
      {
        type: "CHECK_DOMAIN",
        payload: { domain: location.hostname, url: location.href }
      },
      (response) => {
        if (chrome.runtime.lastError || !response?.ok) {
          // Server down — still try classify
          requestClassify(true);
          return;
        }
        const result = response.result || {};
        const verdict = String(result.verdict || "UNKNOWN").toUpperCase();
        const conf = Number(result.confidence || 0);

        // Strong FREE → quiet. Weak FREE or tool-like page → re-analyze.
        if (verdict === "FREE") {
          if (conf >= 0.75 && !result.needs_classification && !looksToolish(collectSignals())) {
            return;
          }
          requestClassify(false, conf < 0.7);
          return;
        }

        if (verdict !== "UNKNOWN" && !result.needs_classification) {
          showBanner(result);
          return;
        }

        // Not in cache → dynamic analysis (AI), then stored for everyone
        requestClassify(true);
      }
    );
  }

  // Re-check when payment language appears on the page AFTER the initial
  // classification (e.g. profile-building flow that only reveals a paywall
  // once you click "Apply"). This is the core fix for late-reveal paywalls.
  //
  // NOTE: this whole watcher is only armed if something explicitly calls
  // armLateChangeWatcher() — it is no longer wired up automatically.
  function armLateChangeWatcher() {
    if (watcherArmed) return;
    watcherArmed = true;

    let debounceTimer = null;
    const tryForceCheck = () => {
      const text = (document.body?.innerText || "").slice(0, 2500);
      if (PAYWALL_SIGNAL_RE.test(text) && text !== lastClassifiedExcerpt) {
        lastClassifiedExcerpt = text;
        requestClassify(true, /* force */ true);
      }
    };

    const observer = new MutationObserver(() => {
      clearTimeout(debounceTimer);
      debounceTimer = setTimeout(tryForceCheck, 1200); // let DOM settle
    });
    observer.observe(document.body, { childList: true, subtree: true, characterData: true });

    // Also force a check right when the user clicks a likely "reveal" button
    // (Apply, Submit, Continue, Checkout, ...) — catches it faster than
    // waiting for the mutation debounce alone.
    document.addEventListener(
      "click",
      (e) => {
        const el = e.target.closest("button, a, [role='button'], input[type='submit']");
        if (!el) return;
        const label = (el.innerText || el.value || el.getAttribute("aria-label") || "").trim();
        if (TRIGGER_BUTTON_RE.test(label)) {
          // Give the page a moment to render whatever comes next, then check.
          setTimeout(tryForceCheck, 900);
        }
      },
      true
    );
  }

  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (msg.type === "SITE_VERDICT" && msg.result) {
      // No longer auto-displayed — content script stays silent unless the
      // popup is actively asking for a classification (see below).
      return;
    }

    // Popup asks this tab to run live classification.
    // Always handled silently: the popup renders the result itself, so we
    // never touch the page DOM or inject a banner here.
    if (msg.type === "CLASSIFY_FOR_POPUP") {
      requestClassify(true, !!msg.force, (result) => {
        sendResponse({ ok: !!result, result });
      }, /* silent */ true);
      return true;
    }
  });

  // Auto-run on page load is intentionally disabled.
  // Previously this called classifyCurrentSite() + armLateChangeWatcher()
  // on every page, which is what injected the banner across every site.
  // Now the content script does nothing until the popup messages it via
  // CLASSIFY_FOR_POPUP, so the box only ever appears when you click the
  // extension icon.
  //
  // If you ever want the old always-on banner behavior back, uncomment:
  //
  // function init() {
  //   classifyCurrentSite();
  //   armLateChangeWatcher();
  // }
  // if (document.readyState === "loading") {
  //   document.addEventListener("DOMContentLoaded", init);
  // } else {
  //   init();
  // }
})();