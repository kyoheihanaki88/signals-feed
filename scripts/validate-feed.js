#!/usr/bin/env node
/**
 * validate-feed.js — validates a Signals feed file against the SignalsFeed schema
 * used by the iOS app. Run BEFORE deploying latest.json.
 *
 *   node scripts/validate-feed.js            # validates ./latest.json
 *   node scripts/validate-feed.js path.json  # validates a specific file
 *
 * Exit code 0 = valid, 1 = invalid. No network. No dependencies.
 */

const fs = require("fs");
const path = require("path");

const file = process.argv[2] || path.join(__dirname, "..", "latest.json");

const REQUIRED_SIGNAL_FIELDS = [
  "number", "lead", "category", "source", "headline", "summary",
  "keyTakeaways", "whyItMatters", "originalURL", "readTime", "imageURL",
];
// placeTime and audioURL are optional (may be null / absent).

const errors = [];
const fail = (msg) => errors.push(msg);

// 1. valid JSON
let raw;
try {
  raw = fs.readFileSync(file, "utf8");
} catch (e) {
  console.error(`✖ Cannot read file: ${file}\n  ${e.message}`);
  process.exit(1);
}

let feed;
try {
  feed = JSON.parse(raw);
} catch (e) {
  console.error(`✖ Invalid JSON in ${file}\n  ${e.message}`);
  process.exit(1);
}

// top-level
if (typeof feed.date !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(feed.date)) {
  fail(`date must be a "YYYY-MM-DD" string (got: ${JSON.stringify(feed.date)})`);
}
if (feed.focus !== "MIXED") {
  fail(`focus must be "MIXED" (Free = MIXED only) (got: ${JSON.stringify(feed.focus)})`);
}
if (!Number.isInteger(feed.version)) {
  fail(`version must be an integer (got: ${JSON.stringify(feed.version)})`);
}
if (!Array.isArray(feed.signals)) {
  fail("signals must be an array");
}

if (Array.isArray(feed.signals)) {
  // exactly five signals
  if (feed.signals.length !== 5) {
    fail(`signals must contain exactly 5 items (got: ${feed.signals.length})`);
  }

  const numbers = [];
  let leadCount = 0;

  feed.signals.forEach((s, i) => {
    const at = `signals[${i}]`;

    // required fields present
    REQUIRED_SIGNAL_FIELDS.forEach((f) => {
      if (!(f in s)) fail(`${at}: missing required field "${f}"`);
    });

    // number 1–5
    if (!Number.isInteger(s.number) || s.number < 1 || s.number > 5) {
      fail(`${at}: number must be an integer 1–5 (got: ${JSON.stringify(s.number)})`);
    } else {
      numbers.push(s.number);
    }

    // lead boolean + count
    if (typeof s.lead !== "boolean") {
      fail(`${at}: lead must be a boolean (got: ${JSON.stringify(s.lead)})`);
    } else if (s.lead === true) {
      leadCount++;
    }

    // keyTakeaways: exactly 3 strings
    if (!Array.isArray(s.keyTakeaways) || s.keyTakeaways.length !== 3) {
      fail(`${at}: keyTakeaways must have exactly 3 items (got: ${
        Array.isArray(s.keyTakeaways) ? s.keyTakeaways.length : typeof s.keyTakeaways
      })`);
    }

    // imageURL present + non-empty string
    if (typeof s.imageURL !== "string" || s.imageURL.trim() === "") {
      fail(`${at}: imageURL must be a non-empty string`);
    }

    // originalURL present + non-empty string
    if (typeof s.originalURL !== "string" || s.originalURL.trim() === "") {
      fail(`${at}: originalURL must be a non-empty string`);
    }

    // readTime integer
    if (!Number.isInteger(s.readTime)) {
      fail(`${at}: readTime must be an integer (got: ${JSON.stringify(s.readTime)})`);
    }

    // audioURL may be null, absent, or a string
    if ("audioURL" in s && s.audioURL !== null && typeof s.audioURL !== "string") {
      fail(`${at}: audioURL must be a string or null`);
    }
  });

  // exactly one lead
  if (feed.signals.length === 5 && leadCount !== 1) {
    fail(`exactly one signal must have lead:true (got: ${leadCount})`);
  }

  // numbers should be the set 1..5 (unique, complete)
  const unique = new Set(numbers);
  if (numbers.length === 5 && (unique.size !== 5 || [1, 2, 3, 4, 5].some((n) => !unique.has(n)))) {
    fail(`signal numbers must be exactly 1,2,3,4,5 with no duplicates (got: ${numbers.join(", ")})`);
  }
}

if (errors.length) {
  console.error(`✖ INVALID — ${file}`);
  errors.forEach((e) => console.error(`  • ${e}`));
  process.exit(1);
}

console.log(`✓ VALID — ${file}`);
console.log(`  date=${feed.date}  focus=${feed.focus}  version=${feed.version}  signals=${feed.signals.length}`);
process.exit(0);
