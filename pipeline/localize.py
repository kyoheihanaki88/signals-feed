#!/usr/bin/env python3
"""
Signals — Japanese localizer (Increment F).

Adds an OPTIONAL `localized.ja` block to each signal of a BUILT feed, generated from that signal's
*final* English fields (headline / summary / keyTakeaways / whyItMatters). The English stays the
source of truth; Japanese is purely additive. iOS already falls back to English when `localized.ja`
is missing (Increment E), so this layer is safe to skip entirely.

  localized: {
    ja: { headline: str, summary: str, keyTakeaways: [str, ...], whyItMatters: str }
  }

Design / safety:
  - Runs on the gitignored build draft (pipeline/generated/latest.draft.json) BETWEEN build.py and
    publish.py. It never touches latest.json or editions/ directly — publish.py writes those, so the
    Japanese rides into the edition through the normal promotion path.
  - Best-effort by default: a per-signal generation/validation failure OMITS localized.ja for that
    signal (the edition still ships, English-only for that one). With --strict, any failure is fatal.
  - No new dependencies: talks to the Anthropic Messages API over stdlib urllib. If
    ANTHROPIC_API_KEY is unset, it cleanly skips all signals (English-only edition) unless --strict.
  - Natural, calm, editorial Japanese — NOT machine translation (see SYSTEM_PROMPT). The model is
    instructed never to invent facts beyond the English content.

Usage:
  python3 pipeline/localize.py <feed.json> [--out OUT] [--strict] [--model NAME] [--limit N]
      (default: rewrites <feed.json> in place)

Env:
  ANTHROPIC_API_KEY   required to actually generate (absent → skip, unless --strict)
  SIGNALS_JA_MODEL    overrides the default model (else --model, else DEFAULT_MODEL)
"""
import sys, os, re, json, argparse, urllib.request, urllib.error

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
# Override per-environment with SIGNALS_JA_MODEL / --model. Kept as a plain alias so ops can point
# it at whatever current model they prefer without touching code.
DEFAULT_MODEL = "claude-3-5-sonnet-latest"
MAX_TOKENS = 1024
TIMEOUT = 60

SYSTEM_PROMPT = (
    "You are a bilingual editor for 'Signals', a calm morning-reading app. You localize short "
    "English news briefs into natural, calm, editorial Japanese that a Japanese reader reads on "
    "their phone in the morning.\n\n"
    "Rules:\n"
    "- Translate MEANING and TONE, never word for word. The Japanese must read as if originally "
    "written by a thoughtful Japanese editor — never like machine translation.\n"
    "- Register: calm, literary, concise editorial Japanese (常体 — だ・である、または体言止め — を自然な範囲で). "
    "Do NOT use a chatty です・ます blog tone. Do NOT use stiff newspaper headline-ese.\n"
    "- Be concise. Short, clear sentences that read easily on a small screen.\n"
    "- Use only standard Japanese punctuation (、。「」). No exclamation marks, no emoji, no "
    "half-width katakana, no romaji, no unusual symbols.\n"
    "- No excessive honorifics, no slang.\n"
    "- Do NOT add facts, numbers, names, or analysis that are not in the English. Do not drop key "
    "meaning. If the English is uncertain, keep the Japanese equally measured.\n"
    "- headline: a short, calm Japanese headline (not a literal translation).\n"
    "- summary: a few concise sentences carrying the English summary's meaning and tone.\n"
    "- keyTakeaways: 2–3 short bullet phrases, each one calm sentence or体言止め. Mirror the number "
    "of English takeaways (max 3).\n"
    "- whyItMatters: exactly ONE clear, quiet sentence.\n\n"
    "Return ONLY a JSON object — no prose, no markdown, no code fences — with exactly these keys:\n"
    '{"headline": string, "summary": string, "keyTakeaways": [string, ...], "whyItMatters": string}'
)


class LocalizeError(Exception):
    pass


def model_name(cli_model=None):
    return os.environ.get("SIGNALS_JA_MODEL") or cli_model or DEFAULT_MODEL


def _english_payload(sig):
    """The final English fields for one signal, as the user message."""
    return {
        "headline": sig.get("headline", ""),
        "summary": sig.get("summary", ""),
        "keyTakeaways": sig.get("keyTakeaways", []),
        "whyItMatters": sig.get("whyItMatters", ""),
    }


def _call_anthropic(user_text, model, api_key):
    """One Messages API call → raw assistant text. Raises LocalizeError on transport/HTTP failure."""
    body = json.dumps({
        "model": model,
        "max_tokens": MAX_TOKENS,
        "temperature": 0.4,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user_text}],
    }).encode("utf-8")
    req = urllib.request.Request(API_URL, data=body, method="POST", headers={
        "x-api-key": api_key,
        "anthropic-version": API_VERSION,
        "content-type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")[:300]
        raise LocalizeError(f"HTTP {e.code}: {detail}")
    except Exception as e:
        raise LocalizeError(str(e))
    parts = data.get("content", [])
    text = "".join(p.get("text", "") for p in parts if p.get("type") == "text").strip()
    if not text:
        raise LocalizeError("empty model response")
    return text


def _parse_json_object(text):
    """Extract the JSON object from the model text (tolerates accidental code fences / prose)."""
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", t).strip()
    a, b = t.find("{"), t.rfind("}")
    if a == -1 or b == -1 or b <= a:
        raise LocalizeError("no JSON object in response")
    return json.loads(t[a:b + 1])


def _clean_str(v):
    return v.strip() if isinstance(v, str) else ""


def validate_ja(obj):
    """Shape + content check on a generated ja block. Returns a normalized dict or raises
    LocalizeError. Mirrors what validate_feed.py will accept and what iOS expects."""
    if not isinstance(obj, dict):
        raise LocalizeError("response is not a JSON object")
    headline = _clean_str(obj.get("headline"))
    summary = _clean_str(obj.get("summary"))
    why = _clean_str(obj.get("whyItMatters"))
    raw_kt = obj.get("keyTakeaways")
    if not headline:
        raise LocalizeError("missing headline")
    if not summary:
        raise LocalizeError("missing summary")
    if not why:
        raise LocalizeError("missing whyItMatters")
    if not isinstance(raw_kt, list):
        raise LocalizeError("keyTakeaways not a list")
    takeaways = [s.strip() for s in raw_kt if isinstance(s, str) and s.strip()][:3]
    if not takeaways:
        raise LocalizeError("keyTakeaways empty")
    # Guard against punctuation/charset artifacts that would look wrong on the phone.
    blob = headline + summary + why + "".join(takeaways)
    if any(ch in blob for ch in ("!", "！", "�")) or re.search(r"[｡-ﾟ]", blob):
        raise LocalizeError("disallowed punctuation/half-width characters")
    return {"headline": headline, "summary": summary,
            "keyTakeaways": takeaways, "whyItMatters": why}


def localize_signal(sig, model, api_key):
    """Generate + validate the ja block for one signal. Returns a dict or raises LocalizeError."""
    user_text = (
        "Localize this Signal into Japanese per the rules. English fields:\n\n"
        + json.dumps(_english_payload(sig), ensure_ascii=False, indent=2)
    )
    return validate_ja(_parse_json_object(_call_anthropic(user_text, model, api_key)))


def localize_feed(feed, *, model, api_key, strict=False, limit=None, log=print):
    """Attach localized.ja to each signal in `feed` (best-effort). Returns (feed, stats).
    Never raises in non-strict mode — a signal that fails is simply left English-only."""
    signals = feed.get("signals", [])
    stats = {"total": len(signals), "localized": 0, "skipped": 0, "failed": 0}

    if not api_key:
        msg = "ANTHROPIC_API_KEY not set — leaving edition English-only (localized.ja omitted)."
        if strict:
            raise LocalizeError(msg)
        log(f"  ⚠ {msg}")
        stats["skipped"] = len(signals)
        return feed, stats

    for i, sig in enumerate(signals):
        if limit is not None and stats["localized"] >= limit:
            break
        num = sig.get("number", i + 1)
        try:
            ja = localize_signal(sig, model, api_key)
            sig["localized"] = {"ja": ja}            # additive; English fields untouched
            stats["localized"] += 1
            log(f"  ✓ #{num} localized.ja  「{ja['headline'][:30]}」")
        except LocalizeError as e:
            stats["failed"] += 1
            sig.pop("localized", None)               # ensure no partial/garbage block remains
            log(f"  ✗ #{num} skipped (English-only): {e}")
            if strict:
                raise
    return feed, stats


def main():
    ap = argparse.ArgumentParser(description="Add optional localized.ja to a built Signals feed.")
    ap.add_argument("feed", help="path to a built feed JSON (e.g. pipeline/generated/latest.draft.json)")
    ap.add_argument("--out", default=None, help="output path (default: rewrite the input in place)")
    ap.add_argument("--model", default=None, help="model name (else $SIGNALS_JA_MODEL, else default)")
    ap.add_argument("--strict", action="store_true",
                    help="fail the run if any signal can't be localized (default: skip it)")
    ap.add_argument("--limit", type=int, default=None, help="localize at most N signals (testing)")
    args = ap.parse_args()

    if not os.path.exists(args.feed):
        print(f"❌ feed not found: {args.feed}")
        sys.exit(1)
    try:
        feed = json.load(open(args.feed))
    except Exception as e:
        print(f"❌ feed is not valid JSON: {e}")
        sys.exit(1)

    model = model_name(args.model)
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    print(f"=== localize.py — Japanese (model={model}, strict={args.strict}) ===")

    try:
        feed, stats = localize_feed(feed, model=model, api_key=api_key, strict=args.strict,
                                    limit=args.limit)
    except LocalizeError as e:
        print(f"❌ strict localization failed: {e}")
        sys.exit(1)

    out = args.out or args.feed
    with open(out, "w") as f:
        json.dump(feed, f, ensure_ascii=False, indent=2)
    print(f"\n  {stats['localized']}/{stats['total']} localized, "
          f"{stats['failed']} failed, {stats['skipped']} skipped → {os.path.relpath(out)}")
    print("  (English fields unchanged; localized.ja is additive and optional.)")


if __name__ == "__main__":
    main()
