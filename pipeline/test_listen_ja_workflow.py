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
check("permissions are contents+PR write only",
      "contents: write" in ja and "pull-requests: write" in ja
      and "packages:" not in ja and "id-token:" not in ja)

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
    en_wf = yaml.safe_load(en)
    check("EN concurrency group differs from JA",
          en_wf["concurrency"]["group"] != wf["concurrency"]["group"])

print()
if FAILURES:
    print(f"{len(FAILURES)} CHECK(S) FAILED")
    sys.exit(1)
print("ALL PASS")
