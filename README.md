# FreeOrNot — Paywall Detector

Warns you early if a site is likely to charge **after** you create work (download / export / watermark removal).

## How it works (dynamic — not a hardcoded list)

```
Visit site
  → check local cache          → hit? show verdict (no AI)
  → check backend knowledge    → hit? show verdict (no AI)
  → unknown?
       → collect page signals
       → analyze once (AI)
       → store in knowledge base
       → every future visitor gets instant answer
```

Hardcoded seed domains are **optional bootstrap only**. The product learns from live analysis + community reports.

## Setup

```powershell
cd server
pip install -r requirements.txt
# Put GEMINI_API_KEY in server/.env (required for first-time analysis of unknown sites)
python server.py
```

Then Chrome → `chrome://extensions` → Load unpacked → `extension/` folder → **Reload** after updates.

Default model is `gemini-flash-latest` (falls back if a model hits free-tier quota). Override with `GEMINI_MODEL` in `.env` if needed.

## Try an unknown site (e.g. pdfleader.com)

1. Open the site
2. First visit: brief **Analyzing…** then a verdict (AI runs once)
3. Reload / second visit: instant answer from cache — **no AI call**
4. Other users hitting the same domain also get the cached answer from the server

## Report button

If detection is wrong or late, **Report** writes into the shared knowledge base so the next visitors are warned sooner.
