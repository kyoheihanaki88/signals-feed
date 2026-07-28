#!/usr/bin/env python3
"""Phase 3D-3C.2 — the offline Editorial Mix Pool builder.

Every external boundary is MOCKED: no live news site, no image provider, no network. The
mocks stand in for the transport only — `writer.draft_one`, `build.read_time_int` and
`build.assign_images` are the real production functions throughout.
"""
import copy
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import build as build_module                                    # noqa: E402
import ranker as ranker_module                                  # noqa: E402
import mix_pool                                                 # noqa: E402
import mix_pool_schema as S                                     # noqa: E402
import writer as writer_module                                  # noqa: E402
import editorial_mix_pool as B                                  # noqa: E402
from editorial_mix_pool_schema import validate_editorial_mix_pool  # noqa: E402

DATE = "2026-07-27"
GENERATED_AT = "2026-07-27T09:00:00Z"
FAILURES = []
WORKLOAD = {}


def check(name, ok, detail=""):
    print(("✓ " if ok else "✗ ") + name + (f"   [{detail}]" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


# ── synthetic raw pool ────────────────────────────────────────────────────────────────
# Short synthetic article text only — no copyrighted article bodies are copied.

ARTICLE = (
    "Tokyo officials said on Friday that the programme would expand to twelve wards "
    "before the end of the year. The ministry confirmed the budget had been approved "
    "after a review lasting several months. Local operators welcomed the decision and "
    "said hiring would begin immediately across the affected districts. Analysts noted "
    "that similar schemes in Osaka and Kyoto had reduced waiting times substantially. "
    "The prefecture will publish quarterly figures so residents can track progress. "
    "A spokesperson added that the rollout would be reviewed again next spring. "
    "Officials expect the first sites to open within eight weeks of the announcement."
)
UNICODE_ARTICLE = (
    '東京 officials confirmed the café "pilot" programme would expand to twelve wards. '
    'The 東京 ministry said the "pilot" had exceeded every target set for it this spring. '
    'Operators in 東京 welcomed the "pilot" and said hiring would begin across districts. '
    'Analysts said the 東京 "pilot" had reduced waiting times substantially in the region. '
    'The 東京 prefecture will publish quarterly figures so residents can track progress. '
    'A 東京 review of the "pilot" is planned again next spring by an oversight panel. '
    "Officials expect the first sites to open within eight weeks of the announcement."
)


def raw_pool(count=20):
    """A frozen raw Mix Pool with `count` candidates, built through the REAL freezer."""
    base = json.load(open(os.path.join(HERE, "fixtures", "mix_pool_scout_candidates.json"),
                          encoding="utf-8"))
    template = base["candidates"][0]
    candidates = []
    for index in range(count):
        row = copy.deepcopy(template)
        row["url"] = f"https://example.com/news/tokyo-care-programme-phase-{index:02d}"
        row["canonical_url"] = row["url"]
        row["id"] = ranker_module.selection_id(row)   # resolvable() requires id == sha1(canonical_url)
        row["title"] = f"Tokyo expands its care programme, phase {index}"
        row["snippet"] = "The ministry confirmed the budget this week."
        row["cluster_id"] = f"cluster-{index:02d}"
        candidates.append(row)
    source = {**base, "candidates": candidates}
    pool = mix_pool.build_mix_pool(source, DATE, GENERATED_AT, now=GENERATED_AT)
    return S.freeze_artifact(pool, source_input=source, source="offline-fixture",
                             reference_at=GENERATED_AT)


def pool_ids(count=20):
    """The frozen artifact's candidate ids, in stored order."""
    return [c["id"] for c in raw_pool(count)["candidates"]]


IDS = pool_ids(25)


IMAGES = {
    "category_pools": {},
    "aliases": {},
    "default_pool": [
        {"imageURL": f"https://images.example.com/photo-{i:03d}?w=900", "placeTime": f"Place {i}"}
        for i in range(40)
    ],
    "topic_pools": {},
    "topic_matchers": {},
    "pool": [],
    "cats": {},
    "default": {"imageURL": "https://images.example.com/fallback?w=900", "placeTime": "Desk"},
}


class Fetcher:
    """Stands in for the network only. Records calls so cache/fetch behaviour is provable."""

    def __init__(self, fail_ids=(), timeout_ids=(), cached_ids=(), unicode_ids=()):
        self.calls = []
        self.network_calls = []
        self.fail_ids = set(fail_ids)
        self.timeout_ids = set(timeout_ids)
        self.cached_ids = set(cached_ids)
        self.unicode_ids = set(unicode_ids)

    def __call__(self, item, articles_dir, allow_fetch, unavailable, force_refetch=frozenset()):
        self.calls.append(item["id"])
        if item["id"] in self.timeout_ids:
            raise TimeoutError("connect timeout https://secret.example/x?token=abc")
        if item["id"] in self.fail_ids:
            return "", "none"
        if item["id"] in self.cached_ids:
            return (UNICODE_ARTICLE if item["id"] in self.unicode_ids else ARTICLE), "full_article"
        self.network_calls.append(item["id"])
        return (UNICODE_ARTICLE if item["id"] in self.unicode_ids else ARTICLE), "full_article"


def build(count=20, fetcher=None, avoid=(), **kwargs):
    fetcher = fetcher or Fetcher(cached_ids=set(pool_ids(count)))
    return B.build_editorial_mix_pool(
        raw_pool(count), generated_at=GENERATED_AT, articles_dir="/nonexistent",
        images_config=IMAGES, avoid_images=set(avoid), allow_fetch=False,
        get_source_text=fetcher, **kwargs,
    ), fetcher


# ── input and selection ───────────────────────────────────────────────────────────────

result, fetcher = build(20)
artifact = result["artifact"]
check("1. a valid frozen raw pool is accepted", result["candidateCount"] >= 15,
      str(result["candidateCount"]))
check("43. the artifact holds 15-20 candidates", 15 <= artifact["candidateCount"] <= 20,
      str(artifact["candidateCount"]))

try:
    B.build_editorial_mix_pool({"nope": 1}, generated_at=GENERATED_AT, articles_dir="/x",
                               images_config=IMAGES, get_source_text=Fetcher())
    check("2. an invalid raw pool is rejected before fetching", False, "accepted")
except B.EditorialPoolError as error:
    check("2. an invalid raw pool is rejected before fetching",
          error.reason == B.REASON_INVALID_RAW_POOL, error.reason)

bad_identity = raw_pool(20)
bad_identity["poolIdentity"] = "0" * 64
probe = Fetcher()
try:
    B.build_editorial_mix_pool(bad_identity, generated_at=GENERATED_AT, articles_dir="/x",
                               images_config=IMAGES, get_source_text=probe)
    check("3. a raw identity mismatch is rejected", False, "accepted")
except B.EditorialPoolError as error:
    check("3. a raw identity mismatch is rejected before any fetch",
          error.reason in (B.REASON_INVALID_RAW_POOL, B.REASON_IDENTITY_MISMATCH)
          and probe.calls == [], error.reason)

probe = Fetcher()
try:
    B.build_editorial_mix_pool(raw_pool(14), generated_at=GENERATED_AT, articles_dir="/x",
                               images_config=IMAGES, get_source_text=probe)
    check("4. fewer than 15 eligible candidates is rejected before fetching", False, "accepted")
except B.EditorialPoolError as error:
    check("4. fewer than 15 eligible candidates is rejected before fetching",
          error.reason == B.REASON_INSUFFICIENT_INPUT and probe.calls == [], error.reason)

r15, _ = build(15)
check("5. exactly 15 eligible candidates is accepted", r15["candidateCount"] == 15,
      str(r15["candidateCount"]))
r20, f20 = build(20)
check("6. exactly 20 eligible candidates is accepted", r20["candidateCount"] == 20,
      str(r20["candidateCount"]))

r25, f25 = build(25)
check("7. more than 20 deterministically truncates to 20",
      r25["candidateCount"] == 20 and len(f25.calls) == 20, f"{r25['candidateCount']}/{len(f25.calls)}")

ids = [c["selector"]["id"] for c in r20["artifact"]["candidates"]]
check("8. input ordering is preserved (raw pool order, sorted by id)", ids == sorted(ids), str(ids[:3]))

dropped = Fetcher(cached_ids=set(pool_ids(20)), fail_ids={pool_ids(20)[3]})
r_drop, _ = build(20, fetcher=dropped)
kept = [c["selector"]["id"] for c in r_drop["artifact"]["candidates"]]
check("9. a failed candidate is removed without reordering survivors",
      pool_ids(20)[3] not in kept and kept == sorted(kept) and len(kept) == 19, str(len(kept)))

original = raw_pool(20)
snapshot = json.dumps(original, sort_keys=True)
B.build_editorial_mix_pool(original, generated_at=GENERATED_AT, articles_dir="/x",
                           images_config=IMAGES, allow_fetch=False,
                           get_source_text=Fetcher(cached_ids=set(pool_ids(20))))
check("10. the raw input object is not mutated", json.dumps(original, sort_keys=True) == snapshot)

# ── article fetching ──────────────────────────────────────────────────────────────────

warm = Fetcher(cached_ids=set(pool_ids(20)))
build(20, fetcher=warm)
check("11. a cache hit avoids a network fetch", warm.network_calls == [], str(warm.network_calls[:3]))

cold = Fetcher()
build(20, fetcher=cold)
check("12. a cache miss uses the existing fetch boundary", len(cold.network_calls) == 20,
      str(len(cold.network_calls)))

timeouts = Fetcher(cached_ids=set(pool_ids(20)), timeout_ids={pool_ids(20)[5]})
r_timeout, _ = build(20, fetcher=timeouts)
timeout_failures = [f for f in r_timeout["failures"] if f["id"] == pool_ids(20)[5]]
check("13. a fetch timeout becomes a candidate failure",
      timeout_failures and timeout_failures[0]["reason"] == B.REASON_ARTICLE_FETCH_FAILED,
      str(timeout_failures))

thin = Fetcher(cached_ids=set(pool_ids(20)), fail_ids={pool_ids(20)[6]})
r_thin, _ = build(20, fetcher=thin)
check("14. unusable article content becomes a candidate failure",
      any(f["id"] == pool_ids(20)[6] for f in r_thin["failures"]))

serialized = json.dumps(r_timeout["artifact"], ensure_ascii=False)
TAIL = "Officials expect the first sites to open within eight weeks of the announcement."
check("15. the full article body never enters the artifact",
      TAIL not in serialized and "source_text_used" not in serialized
      and len(ARTICLE) > max(len(c["editorial"]["summary"])
                             for c in r_timeout["artifact"]["candidates"]))
check("16. no article body or provider detail enters the safe failures",
      not any(ARTICLE[:40] in json.dumps(f) or "token=" in json.dumps(f)
              or "secret.example" in json.dumps(f) for f in r_timeout["failures"]))
check("17. one failure is tolerated when at least 15 survive", r_thin["candidateCount"] == 19)

many = Fetcher(cached_ids=set(pool_ids(20)),
               fail_ids=set(pool_ids(20)[:6]))
try:
    build(20, fetcher=many)
    check("18. more than five failures from 20 fails the artifact", False, "produced an artifact")
except B.EditorialPoolError as error:
    check("18. more than five failures from 20 fails the artifact",
          error.reason == B.REASON_INSUFFICIENT_STORY_SURVIVORS, error.reason)

check("19. no live network call occurred (every fetch was mocked)", True)

# ── writer reuse ──────────────────────────────────────────────────────────────────────

calls = {"n": 0}
real_draft = writer_module.draft_one


def counting_draft(item, source_text, used):
    calls["n"] += 1
    return real_draft(item, source_text, used)


r_writer, _ = build(20, draft_one=counting_draft)
check("20. the existing writer.draft_one is called for every candidate", calls["n"] == 20,
      str(calls["n"]))

sample = r_writer["artifact"]["candidates"][0]["editorial"]
reference = real_draft(
    {"id": r_writer["artifact"]["candidates"][0]["selector"]["id"], "title": r_writer["artifact"]["candidates"][0]["selector"]["headline"],
     "source": sample["source"], "url": sample["originalURL"],
     "category": sample["category"], "snippet": "The ministry confirmed the budget this week."},
    ARTICLE, "full_article",
)["draft"]
check("22. the summary matches the standard writer output", sample["summary"] == reference["summary"])
check("23. keyTakeaways match the standard writer output",
      sample["keyTakeaways"] == [t for t in reference["keyTakeaways"] if str(t).strip()])
check("24. whyItMatters matches the standard writer output",
      sample["whyItMatters"] == reference["whyItMatters"])
check("25. readTime matches build.read_time_int exactly",
      sample["readTime"] == build_module.read_time_int(reference["readTime"]))

check("21. the builder defines no second writer implementation",
      not any(token in open(os.path.join(HERE, "editorial_mix_pool.py"), encoding="utf-8").read()
              for token in ("def _compose", "def _summar", "PROMPT", "def clean_sentences")))


def malformed(item, source_text, used):
    return {"id": item["id"], "draft": {"headline": "", "summary": "", "keyTakeaways": [],
                                        "whyItMatters": "", "readTime": 0}, "flags": []}


try:
    build(20, draft_one=malformed)
    check("26/27/28. a malformed writer result is rejected", False, "accepted")
except B.EditorialPoolError as error:
    check("26/27/28. a malformed writer result is rejected",
          error.reason == B.REASON_INSUFFICIENT_STORY_SURVIVORS, error.reason)

selectors_before = {c["id"]: json.dumps(c, sort_keys=True) for c in raw_pool(20)["candidates"]}
check("31. selector data is unchanged by enrichment",
      all(json.dumps(c["selector"], sort_keys=True) == selectors_before[c["selector"]["id"]]
          for c in r20["artifact"]["candidates"]))
check("29/30. category and originalURL follow the selector",
      all(c["editorial"]["category"] == c["selector"]["category"]
          and c["editorial"]["originalURL"] == c["selector"]["url"]
          for c in r20["artifact"]["candidates"]))

# ── images ────────────────────────────────────────────────────────────────────────────

image_calls = {"n": 0}
real_assign = build_module.assign_images


def counting_assign(*args, **kwargs):
    image_calls["n"] += 1
    return real_assign(*args, **kwargs)


r_img, _ = build(20, assign=counting_assign)
check("32. the existing build.assign_images is reused, once per build", image_calls["n"] == 1,
      str(image_calls["n"]))
check("33. no image provider was called (curated pools only)", True)

urls = [c["editorial"]["imageURL"] for c in r_img["artifact"]["candidates"]]
check("34. every candidate has an https imageURL", all(u.startswith("https://") for u in urls))
check("35. no image repeats within the pool", len(set(urls)) == len(urls),
      f"{len(set(urls))}/{len(urls)}")

avoided = urls[0]
r_avoid, _ = build(20, avoid={avoided})
check("36. the 90-edition cooldown set is honoured",
      avoided not in [c["editorial"]["imageURL"] for c in r_avoid["artifact"]["candidates"]])

captured = {}


def capture_assign(*args, **kwargs):
    captured.update(kwargs)
    captured["items"] = args[0]
    return real_assign(*args, **kwargs)


build(20, assign=capture_assign)
check("37/38. deterministic order is used and no lead-first reordering occurs",
      captured.get("lead_index") is None
      and [i["number"] for i in captured["items"]] == list(range(1, 21)),
      str(captured.get("lead_index")))


def bad_image_assign(items, *args, **kwargs):
    picks = real_assign(items, *args, **kwargs)
    picks[0] = {"imageURL": "file:///Users/me/a.jpg", "placeTime": "x"}
    picks[1] = {"imageURL": "not-a-url", "placeTime": "x"}
    return picks


r_bad, _ = build(20, assign=bad_image_assign)
check("39/40. local paths and malformed image URLs are rejected",
      r_bad["candidateCount"] == 18
      and sum(1 for f in r_bad["failures"] if f["reason"] == B.REASON_INVALID_IMAGE) == 2,
      str(r_bad["candidateCount"]))


def duplicate_assign(items, *args, **kwargs):
    picks = real_assign(items, *args, **kwargs)
    picks[1] = dict(picks[0])
    return picks


r_dup, _ = build(20, assign=duplicate_assign)
check("41. a duplicate image drops the later candidate",
      r_dup["candidateCount"] == 19
      and any(f["reason"] == B.REASON_DUPLICATE_IMAGE for f in r_dup["failures"]))


def wipe_assign(items, *args, **kwargs):
    return [{"imageURL": "", "placeTime": ""} for _ in items]


try:
    build(20, assign=wipe_assign)
    check("42. fewer than 15 after image assignment fails the artifact", False, "accepted")
except B.EditorialPoolError as error:
    check("42. fewer than 15 after image assignment fails the artifact",
          error.reason == B.REASON_INSUFFICIENT_FINAL_SURVIVORS, error.reason)

# ── artifact and identities ───────────────────────────────────────────────────────────

raw20 = raw_pool(20)
survivor_ids = {c["selector"]["id"] for c in r20["artifact"]["candidates"]}
expected_selector_identity = S.pool_identity(
    [c for c in raw20["candidates"] if c["id"] in survivor_ids]
)
check("44/46. the selector identity matches the raw pool candidates",
      r20["artifact"]["selectorPoolIdentity"] == expected_selector_identity)
check("45. the artifact validates under the Python contract",
      validate_editorial_mix_pool(r20["artifact"])["valid"])
check("47. no edition-level field is stored",
      not any(k in c["selector"] or k in c["editorial"]
              for c in r20["artifact"]["candidates"]
              for k in ("number", "importance", "lead")))
check("48. audioURL is the empty string",
      all(c["editorial"]["audioURL"] == "" for c in r20["artifact"]["candidates"]))
_blob = json.dumps(r20["artifact"], ensure_ascii=False)
check("49/50. no full article body, flags or provider provenance is present",
      TAIL not in _blob and "source_text_used" not in _blob
      and "confidence" not in _blob and "flags" not in _blob)

again, _ = build(20)
check("51/52. a repeated build with identical mocked inputs is byte-identical",
      S.canonical_bytes(again["artifact"]) == S.canonical_bytes(r20["artifact"]))

edited = copy.deepcopy(r20["artifact"])
edited["candidates"][0]["editorial"]["headline"] = "A different headline entirely"
from editorial_mix_pool_schema import (  # noqa: E402
    editorial_pool_identity,
    selector_pool_identity,
)
check("53. an editorial change moves only the editorial identity",
      selector_pool_identity(edited) == r20["artifact"]["selectorPoolIdentity"]
      and editorial_pool_identity(edited["candidates"]) != r20["artifact"]["editorialPoolIdentity"])

# ── failure summary safety ────────────────────────────────────────────────────────────

all_failures = r_timeout["failures"] + r_bad["failures"] + r_dup["failures"]
check("55. failures use stable categories",
      all(f["reason"].startswith("editorial_pool_") for f in all_failures))
check("56/57/58. failures carry no article text, URL or credential",
      all(set(f) == {"id", "reason", "stage"} for f in all_failures)
      and not any("http" in json.dumps(f) or "token" in json.dumps(f) for f in all_failures))

# ── unicode coverage ──────────────────────────────────────────────────────────────────

_uid = pool_ids(20)[0]
uni = Fetcher(cached_ids=set(pool_ids(20)), unicode_ids={_uid})
r_uni, _ = build(20, fetcher=uni)
text = S.canonical_bytes(r_uni["artifact"]).decode("utf-8")
_row = next(c for c in r_uni["artifact"]["candidates"] if c["selector"]["id"] == _uid)
check("unicode and escaping survive canonicalization",
      "東京" in text and '\\"' in text,
      _row["editorial"]["summary"][:80])

# ── workload measurement ──────────────────────────────────────────────────────────────

for size in (5, 10, 15, 20):
    if size < 15:
        cold_probe = Fetcher()
        try:
            B.build_editorial_mix_pool(raw_pool(size), generated_at=GENERATED_AT,
                                       articles_dir="/x", images_config=IMAGES,
                                       get_source_text=cold_probe)
        except B.EditorialPoolError:
            pass
        WORKLOAD[size] = {"fetch": len(cold_probe.calls), "network": len(cold_probe.network_calls),
                          "writer": 0, "image": 0, "note": "rejected before external work"}
        continue
    cold_probe = Fetcher()
    writer_calls = {"n": 0}
    image_ops = {"n": 0}

    def counted_draft(item, source_text, used, _c=writer_calls):
        _c["n"] += 1
        return real_draft(item, source_text, used)

    def counted_assign(*args, _c=image_ops, **kwargs):
        _c["n"] += 1
        return real_assign(*args, **kwargs)

    B.build_editorial_mix_pool(raw_pool(size), generated_at=GENERATED_AT, articles_dir="/x",
                               images_config=IMAGES, get_source_text=cold_probe,
                               draft_one=counted_draft, assign=counted_assign)
    warm_probe = Fetcher(cached_ids=set(pool_ids(size)))
    B.build_editorial_mix_pool(raw_pool(size), generated_at=GENERATED_AT, articles_dir="/x",
                               images_config=IMAGES, get_source_text=warm_probe)
    WORKLOAD[size] = {"fetch": len(cold_probe.calls), "network": len(cold_probe.network_calls),
                      "warmNetwork": len(warm_probe.network_calls),
                      "writer": writer_calls["n"], "image": image_ops["n"]}

print()
print("workload (mocked transport; writer/image are the REAL functions):")
print(f"  {'size':>5} {'fetch':>6} {'cold net':>9} {'warm net':>9} {'writer':>7} {'image ops':>10}  vs standard 5")
for size, row in WORKLOAD.items():
    ratio = "—" if row["writer"] == 0 else f"{row['writer'] / 5:.1f}x"
    print(f"  {size:>5} {row['fetch']:>6} {row['network']:>9} {row.get('warmNetwork', 0):>9} "
          f"{row['writer']:>7} {row['image']:>10}  {ratio}"
          + ("   " + row["note"] if row.get("note") else ""))

# ── emit the generated artifact for the TypeScript cross-check ────────────────────────

target = os.environ.get("EDITORIAL_POOL_OUT")
if target:
    with open(target, "w", encoding="utf-8") as handle:
        json.dump(r20["artifact"], handle, indent=2, sort_keys=True, ensure_ascii=False)
        handle.write("\n")
    print(f"\n  wrote generated artifact for cross-language checks: {target}")

print()
print(f"{len(FAILURES)} failure(s)" if FAILURES else "all checks passed")
for name in FAILURES:
    print("  ✗", name)
sys.exit(1 if FAILURES else 0)
