# Listen — Conversational ("dialogue") feed schema

Status: **Phase 1 (schema + optional validation only)** — no daily-generation or publish change yet.

## Goal

Serve the two-person conversational Listen format (audio + timed captions) from the feed, so iOS can
stop hardcoding `mockDialogueSignal1`. Must be **backward compatible**: absent → existing
article/TTS behavior is unchanged; old iOS ignores the new fields.

## Shape — top-level `signal.listen` (optional)

```jsonc
{
  "number": 1,
  // …existing article fields + audioURL + localized are UNCHANGED…
  "listen": {                         // optional; absent → current behavior
    "format": "dialogue",             // optional string (future: "monologue", etc.)
    "en": {                           // optional per-language track
      "audioURL": "https://…/audio/<DATE>/signal-01-dialogue-en.mp3",
      "gap": 0.0,                     // optional number ≥ 0 (added after each line; 0 for direct concat)
      "captions": [                   // optional list
        { "speaker": "listener",  "text": "So there's real news on Iran this morning?", "duration": 2.168163 },
        { "speaker": "explainer", "text": "Yeah. The U.S. and Iran reached an early peace deal.", "duration": 3.004082 }
      ]
    },
    "ja": { "audioURL": "https://…/audio/<DATE>/signal-01-dialogue-ja.mp3", "gap": 0.0, "captions": [] }
  }
}
```

Caption = `{ speaker (str), text (str), duration (seconds, number > 0) }`.

## Placement decision

- **Top-level `signal.listen`** (chosen): discoverable, decoupled from article text and from the
  existing single-voice `audioURL`; language-keyed for EN/JA and future languages.
- Not `localized.en` (EN article is top-level; `localized` is for added translations).
- Not reusing `audioURL` (that stays the plain single-voice narration / fallback source).

## Interaction with existing `audioURL`

`audioURL` and `localized.ja.audioURL` are **unchanged**. iOS Listen selection per effective language:

1. `listen.<lang>` with `audioURL` + `captions` → conversational audio + timed captions (new).
2. else single-voice `audioURL` (EN) / `localized.ja.audioURL` (JA) → current behavior + article karaoke.
3. else → on-device TTS + article karaoke.

Language is never crossed (JA with no `listen.ja` falls back to JA behavior, not EN dialogue).

## Validation (Phase 1, implemented)

`validate_feed.py` → `listen_errors(signal)` (mirrors `localized_errors`): `listen` optional; if
present must be an object; `format` if present must be a string; each language track if present must
be an object; `audioURL` if present non-empty string; `gap` if present number ≥ 0; `captions` if
present a list; each caption needs non-empty `speaker`, non-empty `text`, `duration` > 0. Unknown
extra fields tolerated. **Never required; never weakens an existing check.**

## Migration plan

1. **Feed first** — schema + optional validation (this phase). Later: emit `listen` from
   build/publish + audio manifest (best-effort), date-scoped immutable dialogue audio keys.
2. **iOS reader second** — optional Codable models; play `listen.<lang>` when present, else current
   behavior.
3. **Remove mock third** — once the feed serves Signal 1 dialogue and iOS reads it, delete
   `mockDialogueSignal1` and the hardcoded path.

## Risks / edge cases

- Backward compat: Swift synthesized Codable ignores unknown keys; optionals decode nil when absent.
- Caption/audio drift: `duration` reflects the final MP3 per-line seconds (direct concat → `gap` 0);
  iOS holds the last line until the audio/Signal ends. A warn-only total-vs-sum check can be added.
- Immutability: dialogue audio = date-scoped immutable keys; captions live in the immutable edition;
  `latest.json` stays a byte-identical copy of the newest edition.
- Partial data: missing `duration` → iOS may fall back to a text-length estimate; unknown `speaker`
  treated leniently.
- Validation must NOT require `listen`; missing is the normal case.
