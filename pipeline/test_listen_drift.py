#!/usr/bin/env python3
"""Focused tests for the decoded-duration drift gate (listen_generate).

The Listen final clip is a RAW byte concatenation of per-line MP3s, each carrying
its own ID3v2 tag + LAME/"Info" header. ffprobe's format=duration on that blob is a
size*8/bitrate ESTIMATE that counts the embedded header bytes as audio and OVER-reports
(the historical false-positive drift, e.g. 2026-07-14 signal 1: format≈64.474s while the
real audio ≈64.157s). decoded_duration() decodes the stream to PCM and derives the length
from the actual sample count, which matches the sum of the parts — and still catches a
genuinely missing or duplicated segment.

Needs ffmpeg + an MP3 encoder; skips cleanly (exit 0) if unavailable so CI without ffmpeg
stays green."""
import os, sys, shutil, subprocess, tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import listen_generate as lg

failures = 0
def check(name, cond):
    global failures
    print(("✓" if cond else "✗"), name)
    if not cond:
        failures += 1

if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
    print("SKIP: ffmpeg/ffprobe not available")
    sys.exit(0)

_enc = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
if "libmp3lame" not in _enc and "mp3 " not in _enc:
    print("SKIP: no mp3 encoder available")
    sys.exit(0)
MP3_ENC = "libmp3lame" if "libmp3lame" in _enc else "mp3"

tmp = tempfile.mkdtemp(prefix="drift_test_")

def make_mp3(path, seconds, freq):
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi",
                    "-i", f"sine=frequency={freq}:duration={seconds}:sample_rate=44100",
                    "-c:a", MP3_ENC, "-b:a", "128k", "-write_id3v2", "1", path], check=True)

def concat(dest, parts):
    with open(dest, "wb") as o:
        for p in parts:
            o.write(open(p, "rb").read())
    return dest

# 12 segments (like the real signal-1 JA clip) so the repeated ID3/Info headers
# meaningfully inflate the container/bitrate estimate.
segs, durs = [], []
for i in range(1, 13):
    p = os.path.join(tmp, f"seg{i:02d}.mp3")
    make_mp3(p, 0.6 + 0.05 * i, 200 + 20 * i)
    segs.append(p)
    durs.append(lg.ffprobe_duration(p))
sum_parts = round(sum(durs), 6)

final = concat(os.path.join(tmp, "final.mp3"), segs)
blob = open(final, "rb").read()
check("concatenated blob carries repeated ID3 headers (>=2)", blob.count(b"ID3") >= 2)

est = lg.ffprobe_duration(final)      # OLD gate input: container size/bitrate estimate
dec = lg.decoded_duration(final)      # NEW gate input: decoded PCM sample count
old_drift = abs(est - sum_parts)
new_drift = abs(dec - sum_parts)
print(f"  sum_parts={sum_parts:.4f}  format_estimate={est:.4f}  decoded={dec:.4f}  "
      f"old_drift={old_drift:.4f}  new_drift={new_drift:.4f}")

check("format=duration estimate differs from decoded (inflated by embedded headers)",
      abs(est - dec) > 1e-3)
check("decoded duration matches sum of parts within tolerance", new_drift < 0.25)
check("decoded drift is smaller than the old format-estimate drift", new_drift <= old_drift + 1e-3)
check("decoded drift is under DRIFT_THRESHOLD (valid clip passes)", new_drift <= lg.DRIFT_THRESHOLD)

# a genuinely TRUNCATED / missing final segment must still fail the gate
trunc = concat(os.path.join(tmp, "trunc.mp3"), segs[:-1])
check("missing segment -> decoded drift exceeds threshold (still fails)",
      abs(lg.decoded_duration(trunc) - sum_parts) > lg.DRIFT_THRESHOLD)

# a DUPLICATED / extra final segment must still fail the gate
extra = concat(os.path.join(tmp, "extra.mp3"), segs + [segs[-1]])
check("extra/duplicate segment -> decoded drift exceeds threshold (still fails)",
      abs(lg.decoded_duration(extra) - sum_parts) > lg.DRIFT_THRESHOLD)

# fail-closed: an undecodable final raises rather than silently skipping the check
bad = os.path.join(tmp, "bad.mp3")
open(bad, "wb").write(b"this is not audio at all")
raised = False
try:
    lg.decoded_duration(bad)
except Exception:
    raised = True
check("undecodable final raises (fail-closed, not skipped)", raised)

check("DRIFT_THRESHOLD unchanged at 0.5", lg.DRIFT_THRESHOLD == 0.5)

shutil.rmtree(tmp, ignore_errors=True)
print("ALL PASS" if failures == 0 else f"{failures} CHECK(S) FAILED")
sys.exit(1 if failures else 0)
