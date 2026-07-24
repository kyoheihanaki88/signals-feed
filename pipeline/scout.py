#!/usr/bin/env python3
"""
Signals — Signal Scout (Build 1, Step B).

Reads sources.yaml, fetches each RSS feed, normalizes items into candidate objects,
filters (homepage / video / stale), de-duplicates exact URLs, lightly clusters the same
story across sources, and writes candidates.json.

Scope: collection only. NO summaries, NO ranking, NO lead selection, NO latest.json.
Stdlib only (xml.etree + email.utils) + pyyaml — no feedparser dependency.

Usage:
  python3 scout.py                      # fetch live feeds (for the runner)
  python3 scout.py --cache-dir cache    # parse pre-saved <slug>.xml files (offline/testing)
  python3 scout.py --max-age-hours 36 --out candidates.json
"""
import sys, os, re, json, argparse, html, datetime, hashlib
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
import yaml

STOPWORDS = set("a an the of to in on for and or with from at by as is are was were this that "
                "after over into out new his her their its it he she they we you".split())
TRACKING_PREFIXES = ("utm_", "at_medium", "at_campaign", "at_", "ito", "cmpid", "ns_")
VIDEO_RE = re.compile(r"/(videos?|watch|av/)", re.I)

def slug(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")

def publisher_key(name):
    """Publisher identity for corroboration counting: the source name minus any section
    suffix — "BBC News (World)" and "BBC News (Technology)" are ONE publisher. Section
    feeds widen coverage; they must never count as independent corroboration."""
    return re.sub(r"\s*\(.*?\)\s*$", "", name or "").strip()

def canonical_url(url):
    p = urlsplit(url)
    keep = [(k, v) for k, v in parse_qsl(p.query)
            if not any(k.lower().startswith(t) for t in TRACKING_PREFIXES)]
    return urlunsplit((p.scheme, p.netloc.lower(), p.path.rstrip("/") or "/", urlencode(keep), ""))

def stable_id(url):
    """Deterministic 6-char candidate id from the (canonical) article URL — stable across Scout
    re-runs. Identical formula to selection.py's short_id, so candidates.json, review.md, and the
    selection/build step all agree on the same id (e.g. 7ddbf6)."""
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:6]

def is_homepage(url):
    return urlsplit(url).path.strip("/") == ""

def clean_text(s):
    return html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()

def to_iso(date_str):
    try:
        return parsedate_to_datetime(date_str).astimezone(datetime.timezone.utc).isoformat()
    except Exception:
        return None

def reliability(status):
    return {"verified": "high", "known": "high", "reachable": "medium", "flagged": "low"}.get(status, "unknown")

def _stem(w):
    return w[:-1] if w.endswith("s") and len(w) > 4 else w  # drones→drone, strikes→strike

def title_tokens(title):
    return {_stem(w) for w in re.findall(r"[a-z0-9]+", (title or "").lower())
            if w not in STOPWORDS and len(w) > 2}

def jaccard(a, b):
    return len(a & b) / len(a | b) if (a or b) else 0.0

def parse_feed(xml_bytes):
    """Return a list of raw item dicts from RSS 2.0 (and basic Atom)."""
    items = []
    root = ET.fromstring(xml_bytes)
    for it in root.iter("item"):  # RSS 2.0
        items.append({
            "title": (it.findtext("title") or "").strip(),
            "link": (it.findtext("link") or "").strip(),
            "desc": it.findtext("description") or "",
            "date": it.findtext("pubDate") or "",
        })
    if not items:  # Atom fallback
        ns = "{http://www.w3.org/2005/Atom}"
        for it in root.iter(f"{ns}entry"):
            link = ""
            for l in it.findall(f"{ns}link"):
                if l.get("rel", "alternate") == "alternate":
                    link = l.get("href", "")
            items.append({
                "title": (it.findtext(f"{ns}title") or "").strip(),
                "link": link,
                "desc": it.findtext(f"{ns}summary") or "",
                "date": it.findtext(f"{ns}updated") or it.findtext(f"{ns}published") or "",
            })
    return items

def load_feed(source, cache_dir):
    """Return (xml_bytes, error). Uses cache file if cache_dir is set; else fetches."""
    if cache_dir:
        # feed_id disambiguates multiple feeds of one publisher in offline cache mode
        path = os.path.join(cache_dir, slug(source.get("feed_id") or source["name"]) + ".xml")
        if not os.path.exists(path):
            return None, "no-cache-file"
        return open(path, "rb").read(), None
    # live fetch (runner only)
    import urllib.request
    try:
        req = urllib.request.Request(source["url"], headers={"User-Agent": "SignalsScout/1.0"})
        return urllib.request.urlopen(req, timeout=10).read(), None
    except Exception as e:
        return None, f"fetch-error: {e}"

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sources", default=os.path.join(os.path.dirname(__file__), "sources.yaml"))
    ap.add_argument("--cache-dir", default=None)
    ap.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "candidates.json"))
    ap.add_argument("--max-age-hours", type=int, default=36)
    ap.add_argument("--summary-file", default=None, help="also write the report as markdown (for CI job summary)")
    args = ap.parse_args()

    sources = yaml.safe_load(open(args.sources))["sources"]
    now = datetime.datetime.now(datetime.timezone.utc)
    cutoff = now - datetime.timedelta(hours=args.max_age_hours)

    candidates, failed, skipped = [], [], {"homepage": 0, "video": 0, "stale": 0, "no-link": 0, "dup": 0}
    seen = set()

    for src in sources:
        if not src.get("url"):
            failed.append((src["name"], "no-url-configured")); continue
        xml_bytes, err = load_feed(src, args.cache_dir)
        if err:
            failed.append((src["name"], err)); continue
        try:
            items = parse_feed(xml_bytes)
        except Exception as e:
            failed.append((src["name"], f"parse-error: {e}")); continue
        if not items:
            failed.append((src["name"], "no-items")); continue

        for it in items:
            url = it["link"]
            if not url or not it["title"]:
                skipped["no-link"] += 1; continue
            if is_homepage(url):
                skipped["homepage"] += 1; continue
            if VIDEO_RE.search(url):
                skipped["video"] += 1; continue
            iso = to_iso(it["date"])
            if iso and datetime.datetime.fromisoformat(iso) < cutoff:
                skipped["stale"] += 1; continue
            cu = canonical_url(url)
            if cu in seen:
                skipped["dup"] += 1; continue
            seen.add(cu)
            candidates.append({
                "id": stable_id(cu or url),       # stable, canonical-URL-based id (selection.py needs c["id"])
                "title": clean_text(it["title"]),
                "source": src["name"],
                "publisher": publisher_key(src["name"]),   # section feeds share one publisher
                "category": src.get("category", "OTHER"),
                "url": url,
                "canonical_url": cu,
                "published_at": iso,
                "snippet": clean_text(it["desc"])[:400],
                "paywalled": bool(src.get("paywalled", False)),
                "source_reliability": reliability(src.get("status", "")),
            })

    # light clustering: greedy token-Jaccard on titles (same story across sources)
    toks = [title_tokens(c["title"]) for c in candidates]
    cluster_id = [-1] * len(candidates)
    nid = 0
    for i in range(len(candidates)):
        if cluster_id[i] != -1:
            continue
        cluster_id[i] = nid
        for j in range(i + 1, len(candidates)):
            if cluster_id[j] == -1 and jaccard(toks[i], toks[j]) >= 0.30:
                cluster_id[j] = nid
        nid += 1
    sizes = {cid: cluster_id.count(cid) for cid in set(cluster_id)}
    # cluster_sources: DISTINCT PUBLISHERS per cluster. Corroboration must count
    # publishers, not feed entries — three stories about one launch from three feeds of
    # the same publisher are still ONE publisher's coverage (section-feed safety).
    pubs_per_cluster = {}
    for k, c in enumerate(candidates):
        pubs_per_cluster.setdefault(cluster_id[k], set()).add(c.get("publisher") or c["source"])
    for k, c in enumerate(candidates):
        c["cluster_id"] = cluster_id[k]
        c["cluster_size"] = sizes[cluster_id[k]]
        c["cluster_sources"] = len(pubs_per_cluster[cluster_id[k]])

    out = {
        "generated_at": now.isoformat(),
        "source_count": len(sources),
        "candidate_count": len(candidates),
        "candidates": candidates,
    }
    json.dump(out, open(args.out, "w"), ensure_ascii=False, indent=2)

    # ---- report ----
    per_src = {}
    for c in candidates:
        per_src[c["source"]] = per_src.get(c["source"], 0) + 1
    multi = sorted({cid for cid, sz in sizes.items() if sz > 1})

    lines = []  # plain-text report (stdout)
    md = []     # markdown report (CI job summary)
    def emit(text="", md_text=None):
        lines.append(text)
        md.append(text if md_text is None else md_text)

    emit(f"=== Signal Scout report ({now.date()}) ===", f"## Signal Scout — {now.date()}")
    emit(f"candidates collected : {len(candidates)}", f"**Candidates collected:** {len(candidates)}")
    emit("", "")
    emit("", "| Source | Candidates |\n|---|---|")
    for s, n in per_src.items():
        emit(f"  {s:<22} {n}", f"| {s} | {n} |")
    emit("", "")
    emit(f"failed feeds         : {len(failed)}", f"**Failed / skipped feeds:** {len(failed)}")
    for name, why in failed:
        emit(f"  - {name}: {why}", f"- `{name}` — {why}")
    emit("", "")
    emit(f"skipped items        : {sum(skipped.values())}  {skipped}",
         f"**Skipped items:** {sum(skipped.values())} — {skipped}")
    emit("", "")
    emit(f"cross-source clusters: {len(multi)} (size>1 = importance signal)",
         f"**Cross-source clusters:** {len(multi)} (size > 1 = importance signal)")
    for cid in multi:
        members = [c for c in candidates if c["cluster_id"] == cid]
        srcs = ", ".join(sorted({m["source"].split()[0] for m in members}))
        emit(f"  cluster {cid} (size {members[0]['cluster_size']}): {members[0]['title'][:60]} … [{srcs}]",
             f"- cluster {cid} (size {members[0]['cluster_size']}): {members[0]['title'][:70]} — _{srcs}_")
    emit("", "")
    emit(f"wrote {args.out}", f"\n_Artifact: `{os.path.basename(args.out)}`_")

    print("\n" + "\n".join(lines))
    if args.summary_file:
        with open(args.summary_file, "w") as f:
            f.write("\n".join(md) + "\n")

    # Non-zero exit if nothing was collected, so the CI run flags a dry morning.
    if not candidates:
        sys.exit("ERROR: zero candidates collected — downstream would hold last valid feed.")

if __name__ == "__main__":
    main()
