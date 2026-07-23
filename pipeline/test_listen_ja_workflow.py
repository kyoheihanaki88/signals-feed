#!/usr/bin/env python3
"""Checks for .github/workflows/auto-generate-listen-ja.yml — the JA Listen automation.
Guards the safety contract:

  • triggers: workflow_dispatch + workflow_run (EN Listen, success only) + ONE backstop
    cron at 15:15 UTC — never push, never pull_request
  • latest.json.date is the source of truth for the target date (no clock, no dir scan);
    optional dispatch date validated against an existing edition
  • green skip when listen.ja is already 5/5; EN 5/5 is a hard precondition
  • JA never promotes latest.json (explicit gate) — EN-only listen-ready stays intact
  • per-date scratch checkpoint persistence via actions/cache (save even on failure)
  • post-injection verification: JA 5/5, narrator-only, EN byte-identical, no other date
  • failure artifact upload (rejected scripts only — no env/secrets) + failure summary
  • dedicated concurrency group, fully separate from the EN workflow
  • no ElevenLabs anywhere in the JA path; Azure + Anthropic + Cloudflare secrets only
  • the EN workflow file itself is untouched by this feature (structural separation)

Text-based checks always run; structural checks run additionally when PyYAML is
available (CI installs it; a bare local python without yaml still gets full coverage
of the contract via the text checks).
"""
import os
import re
import sys
import textwrap

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
JA_WF = os.path.join(ROOT, ".github", "workflows", "auto-generate-listen-ja.yml")
EN_WF = os.path.join(ROOT, ".github", "workflows", "auto-generate-listen.yml")

FAILURES = []


def check(name, ok):
    if ok:
        print(f"✓ {name}")
    else:
        print(f"✗ {name}")
        FAILURES.append(name)


ja = open(JA_WF, encoding="utf-8").read()
en = open(EN_WF, encoding="utf-8").read()

# ── Triggers: workflow_run + backstop cron + dispatch ───────────────────────────────
check("JA workflow file exists", bool(ja.strip()))
check("workflow_dispatch trigger present", "workflow_dispatch:" in ja)
check("workflow_run trigger targets the EN Listen workflow",
      "workflow_run:" in ja and '["Auto Generate Listen"]' in ja and "types: [completed]" in ja)
check("workflow_run success guard on the probe job",
      "github.event.workflow_run.conclusion == 'success'" in ja)
check("exactly ONE backstop cron at 15:15 UTC",
      ja.count("cron:") == 1 and '"15 15 * * *"' in ja)
check("no push/pull_request triggers (loop-safe)",
      "\n  push:" not in ja and "\n  pull_request:" not in ja)
check("date input exists and is optional",
      bool(re.search(r"date:\n\s+description:.*\n\s+required:\s*false", ja)))
check("latest.json.date is the source of truth (no dir scan, no clock)",
      'latest.get("date")' in ja and "max(eds)" not in ja and "glob.glob" not in ja)
check("explicit date input is validated + must exist on main",
      "invalid date input" in ja and "not found on main" in ja)

# ── workflow_run merge-race wait (Fix 2) ────────────────────────────────────────────
check("wait step exists and is gated to workflow_run ONLY",
      "Wait for EN merge propagation (workflow_run only)" in ja
      and "if: github.event_name == 'workflow_run'" in ja)
check("wait loop: at most 10 checks", "MAX_ATTEMPTS = 10" in ja)
check("wait loop: 60 seconds between checks", "SLEEP_SECONDS = 60" in ja)
check("every attempt logs 'JA probe attempt N/10: expected=… latest=…'",
      "JA probe attempt {attempt}/{MAX_ATTEMPTS}: expected={expected} latest={observed}" in ja)
check("expected date is bound to the TRIGGERING EN run's edition-date artifact",
      "github.event.workflow_run.id" in ja
      and '"edition-date"' in ja
      and 'payload.get("edition_date")' in ja
      and "actions/runs/{run_id}/artifacts" in ja)
check("expected is resolved ONCE before the loop and is immutable while polling",
      "resolved ONCE before the loop" in ja and "immutable during polling" in ja)
check("expected is NEVER derived from main's newest edition or the stale checkout",
      "ls-tree" not in ja and "origin_state" not in ja)
check("missing/invalid artifact → RED failure, no silent fallback",
      "failing instead of guessing" in ja and "NO silent fallback" in ja)
check("latest.json re-fetched from origin/main on EVERY check",
      '"git", "fetch", "--quiet", "origin", "main"' in ja
      and 'git", "show", "origin/main:latest.json' in ja)
# REGRESSION (2026-07-23): the wait heredoc's stdout is redirected into $GITHUB_OUTPUT,
# so every subprocess run inside it that could emit stdout MUST capture/redirect it —
# the uncaptured `git reset` printed "HEAD is now at …" into GITHUB_OUTPUT and failed
# the probe as malformed output immediately after a correct proceed decision.
check("reset is captured (git stdout can never leak into GITHUB_OUTPUT)",
      re.search(r'subprocess\.run\(\["git", "reset", "--hard", "origin/main"\],\s*\n\s*'
                r'check=True, capture_output=True, text=True\)', ja) is not None)
_wait_block = re.search(r"Wait for EN merge propagation \(workflow_run only\)(.*?)- name: Resolve date", ja, re.S)
check("wait heredoc extractable", _wait_block is not None)
if _wait_block:
    _calls = re.findall(r"subprocess\.run\((?:[^()]|\([^()]*\))*\)", _wait_block.group(1))
    check("EVERY subprocess call inside the wait heredoc captures its output",
          len(_calls) >= 2 and all("capture_output=True" in c for c in _calls))

# The polling loop itself must not reassign `expected` — extract the for-block and prove it.
_loop = re.search(r"for attempt in range\(1, MAX_ATTEMPTS \+ 1\):(.*?)\n\s*else:", ja, re.S)
check("polling loop extractable", _loop is not None)
check("polling loop never reassigns expected (cannot move mid-wait)",
      _loop is not None
      and re.search(r"^\s+expected\s*=[^=]", _loop.group(1), re.M) is None)
check("older latest → retry (explicitly NOT complete / NOT stale)",
      "still older than expected" in ja and "merge not propagated yet" in ja)
check("equal → proceed into the existing generation path (checkout synced first)",
      "decision=proceed" in ja and '"git", "reset", "--hard", "origin/main"' in ja)
check("newer → green-skip with reason=stale_workflow_run",
      "decision=stale" in ja and "reason=stale_workflow_run" in ja
      and "a newer edition is already served" in ja)
check("stale run never consumes the newer edition (skip happens before any gate/generation)",
      "must never consume it" in ja)
check("timeout emits the merge-propagation error and fails RED (nonzero), never green-skips",
      "merge-propagation timeout" in ja
      and "the EN PR may not have merged" in ja
      and re.search(r"merge-propagation timeout[^\n]*\n\s*sys\.exit\(1\)", ja) is not None)
check("summary reports event + expected/observed/checks/decision",
      "steps.wait.outputs.decision" in ja and "steps.wait.outputs.attempts" in ja
      and "event: \\`${{ github.event_name }}\\`" in ja)
check("stale_workflow_run has its own summary line", "stale workflow_run: a newer edition" in ja)

# Script-level unit test of the pure decision core, extracted from the workflow heredoc —
# the three terminal semantics are exercised as CODE, not just as YAML text.
_m = re.search(r"def decide\(latest_date, expected_date\):.*?return \"wait\"", ja, re.S)
check("decide() extractable for unit testing", _m is not None)
if _m:
    _ns = {}
    exec(textwrap.dedent(_m.group(0)), _ns)
    _d = _ns["decide"]
    check("unit: latest == expected → proceed", _d("2026-07-22", "2026-07-22") == "proceed")
    check("unit: latest NEWER than expected → stale (green-skip)", _d("2026-07-23", "2026-07-22") == "stale")
    check("unit: latest OLDER than expected → wait (retry, not complete/stale)",
          _d("2026-07-21", "2026-07-22") == "wait")
    # Review fixture: triggering EN run processed 07-22; Daily Auto Publish then merged
    # editions/2026-07-23.json and EN promoted latest to 07-23 before this old event's
    # probe ran. Expected stays bound to the TRIGGERING run (07-22, from its artifact),
    # so the old run green-skips as stale — it can NEVER generate or consume 07-23.
    check("fixture: old run (expected=07-22) vs newest-edition world (latest=07-23) → stale_workflow_run, never generation",
          _d("2026-07-23", "2026-07-22") == "stale")

# The JA TTS preset and audio architecture are untouched by this workflow change.
_lg = open(os.path.join(HERE, "listen_generate.py"), encoding="utf-8").read()
check("TTS preset unchanged: narrator ja-JP-NanamiNeural",
      'AZURE_VOICE_JA_LISTENER = "ja-JP-NanamiNeural"' in _lg)
check("TTS preset unchanged: customerservice style",
      'AZURE_TTS_STYLE_JA_NARRATOR = "customerservice"' in _lg)
check("TTS preset unchanged: rate +12%", 'AZURE_TTS_RATE_JA_LISTENER = "+12%"' in _lg)
check("audio architecture unchanged: per-line SSML + raw per-line concatenation",
      "def _azure_ssml" in _lg and "RAW byte concatenation of per-line MP3s" in _lg)
check("pronunciation dictionary unchanged",
      '("赤と金の", "赤ときんの")' in _lg)

# ── Idempotence + EN precondition ───────────────────────────────────────────────────
check("green skip when listen.ja already 5/5", "reason=complete" in ja and "ja == 5" in ja)
check("completed historical dates are never regenerated automatically",
      "NEVER regenerated automatically" in ja)
check("EN 5/5 is a precondition (skip when EN pending)",
      "reason=en_pending" in ja and "en < 5" in ja)
check("hard gate: edition must have listen.en 5/5", "en != 5" in ja)
check("hard gate: latest.json must ALREADY point at DATE (JA never promotes)",
      "l==os.environ['DATE']" in ja and "JA never promotes" in ja)

# ── Generation path ─────────────────────────────────────────────────────────────────
check("generation runs listen_generate.py with --lang ja",
      re.search(r"listen_generate\.py\s+\"\$DATE\"\s+--lang ja", ja) is not None)
check("injection runs listen_inject_edition.py", "listen_inject_edition.py" in ja)
check("Azure secrets wired (key + region)",
      "secrets.AZURE_SPEECH_KEY" in ja and "secrets.AZURE_SPEECH_REGION" in ja)
check("Anthropic + model secrets wired",
      "secrets.ANTHROPIC_API_KEY" in ja and "secrets.SIGNALS_LISTEN_MODEL" in ja)
check("Cloudflare secrets wired",
      "secrets.CLOUDFLARE_API_TOKEN" in ja and "secrets.CLOUDFLARE_ACCOUNT_ID" in ja)
check("NO ElevenLabs anywhere in the JA workflow", "ELEVENLABS" not in ja)
check("ffmpeg installed (decoded-duration drift)", "ffmpeg" in ja)
check("wrangler installed (R2 upload)", "wrangler" in ja)

# ── Checkpoint persistence via cache ────────────────────────────────────────────────
check("cache restore step present (actions/cache/restore)", "actions/cache/restore@" in ja)
check("cache save step present (actions/cache/save)", "actions/cache/save@" in ja)
check("cache path is scratch", re.search(r"path:\s*scratch", ja) is not None)
check("cache key is per-date", "listen-ja-scratch-${{ needs.probe.outputs.date }}" in ja)
check("restore-keys fall back to newest cache for the date",
      "restore-keys" in ja and "listen-ja-scratch-${{ needs.probe.outputs.date }}-\n" in ja)
check("cache SAVED even when generation fails (resume is for failures)",
      "always()" in ja and "steps.gen.outcome == 'failure'" in ja)
_save_pos = ja.find("actions/cache/save@")
_rm_pos = ja.find("rm -rf scratch")
check("cache save happens BEFORE scratch cleanup", 0 < _save_pos < _rm_pos)

# ── Post-injection verification ─────────────────────────────────────────────────────
check("post-injection verify step exists",
      "Verify injection" in ja and "JA coverage is not 5/5" in ja)
check("verify: narrator-only captions asserted", 'spk == {"narrator"}' in ja)
check("verify: EN blocks byte-identical to pre-injection HEAD snapshot",
      "EN blocks changed by injection" in ja and 'git", "show", f"HEAD:' in ja)
check("verify: manifest==edition captions + no other date changed",
      "another date changed" in ja and "manifest/edition ja captions differ" in ja)
_verify_pos = ja.find("Verify injection")
_guard_pos = ja.find("Guard — only feed metadata files changed")
check("verify runs after injection and before the commit guard",
      ja.find("listen_inject_edition.py") < _verify_pos < _guard_pos)

# ── Failure observability ───────────────────────────────────────────────────────────
check("failure artifact upload step exists (rejected scripts only)",
      "actions/upload-artifact@" in ja and "scratch/failed_ja_dialogue_*.json" in ja
      and "if-no-files-found: ignore" in ja)
check("artifact step runs on failure with a retention limit",
      "retention-days:" in ja and re.search(r"Upload JA failure artifacts[\s\S]{0,80}if: failure\(\)", ja) is not None)
check("artifact never includes env dumps or secrets",
      "env |" not in ja and "printenv" not in ja and "toJSON(secrets" not in ja)
check("failure summary step exists with recovery guidance",
      re.search(r"Failure summary\n\s+if: failure\(\)", ja) is not None
      and "GITHUB_STEP_SUMMARY" in ja and "Recovery:" in ja and "checkpoint cache" in ja)

# ── Isolation from EN ───────────────────────────────────────────────────────────────
check("dedicated concurrency group auto-generate-listen-ja",
      "group: auto-generate-listen-ja" in ja)
check("cancel-in-progress false", "cancel-in-progress: false" in ja)
check("EN workflow keeps its own separate group", "group: auto-generate-listen\n" in en)
check("EN workflow has NO --lang ja (JA lives only in the new workflow)",
      "--lang ja" not in en)
check("EN workflow still EN-voiced (ElevenLabs untouched)", "ELEVENLABS_API_KEY" in en)

# ── Output hygiene ──────────────────────────────────────────────────────────────────
check("metadata-only guard present (no MP3s / stray files can be committed)",
      "git status --porcelain" in ja and "unexpected change" in ja)
check("guard allowlist is exactly the 3 feed metadata files",
      '"editions/${DATE}.json"|latest.json|pipeline/listen_manifest.json' in ja)
check("PR branch is listen/ja-<date>", 'branch: "listen/ja-${{ needs.probe.outputs.date }}"' in ja)
check("commit message matches manual convention",
      'commit-message: "Add JA solo Listen for ${{ needs.probe.outputs.date }}"' in ja)
check("merge-or-fail-loudly step present",
      "gh pr merge" in ja and "Failing loudly" in ja)
check("permissions are contents+PR write + actions:read (artifact of the triggering run)",
      "contents: write" in ja and "pull-requests: write" in ja and "actions: read" in ja
      and "packages:" not in ja and "id-token:" not in ja)

# ── EN side of the binding: the EN probe exposes its edition date ───────────────────
check("EN probe uploads the edition-date artifact (both generate and green-skip runs)",
      "Upload edition-date artifact" in en and "name: edition-date" in en
      and "every probe outcome" in en)
check("EN artifact payload is the date ONLY (no secrets, no env)",
      '\'{"edition_date": "%s"}\\n\'' in en
      and "secrets." not in en.split("edition-date artifact payload")[1].split("Upload edition-date")[0])
check("EN artifact has a short retention", re.search(r"name: edition-date\n\s+path: edition-date\.json\n\s+retention-days: 3", en) is not None)

# ── Structural checks (when PyYAML is available) ────────────────────────────────────
try:
    import yaml  # type: ignore
except ImportError:
    print("~ PyYAML not installed — structural checks skipped (text checks above cover the contract)")
else:
    wf = yaml.safe_load(ja)
    trig = wf.get("on", wf.get(True))  # YAML 1.1 parses bare `on` as boolean True
    check("YAML parses", isinstance(wf, dict))
    check("triggers are exactly {workflow_dispatch, workflow_run, schedule}",
          set(trig.keys()) == {"workflow_dispatch", "workflow_run", "schedule"})
    check("workflow_run structurally targets Auto Generate Listen on completed",
          trig["workflow_run"] == {"workflows": ["Auto Generate Listen"], "types": ["completed"]})
    check("schedule is a single 15:15 UTC cron",
          trig["schedule"] == [{"cron": "15 15 * * *"}])
    check("dispatch date input default is blank",
          trig["workflow_dispatch"]["inputs"]["date"].get("default", "") == "")
    check("two jobs: probe + listen-ja", set(wf["jobs"].keys()) == {"probe", "listen-ja"})
    check("probe carries the workflow_run success guard",
          "workflow_run.conclusion == 'success'" in wf["jobs"]["probe"]["if"])
    check("listen-ja depends on probe and is gated on needed",
          wf["jobs"]["listen-ja"]["needs"] == "probe"
          and "needed == 'true'" in wf["jobs"]["listen-ja"]["if"])
    check("concurrency block structurally correct",
          wf["concurrency"] == {"group": "auto-generate-listen-ja", "cancel-in-progress": False})
    check("probe timeout-minutes covers the ~10-minute wait window",
          wf["jobs"]["probe"]["timeout-minutes"] == 15)
    _steps = wf["jobs"]["probe"]["steps"]
    _wait = [s for s in _steps if s.get("id") == "wait"]
    check("wait step structurally gated to workflow_run only",
          bool(_wait) and _wait[0].get("if") == "github.event_name == 'workflow_run'")
    _names = [s.get("name", "") for s in _steps]
    check("wait runs after checkout and before the resolve step",
          _names.index("Wait for EN merge propagation (workflow_run only)")
          < _names.index("Resolve date from latest.json — JA coverage + EN precondition"))
    en_wf = yaml.safe_load(en)
    check("EN concurrency group differs from JA",
          en_wf["concurrency"]["group"] != wf["concurrency"]["group"])

print()
if FAILURES:
    print(f"{len(FAILURES)} CHECK(S) FAILED")
    sys.exit(1)
print("ALL PASS")
