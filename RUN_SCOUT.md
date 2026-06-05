# Run the Signal Scout once (push & trigger)

Goal: push the Scout files to GitHub, run the workflow by hand, and confirm it produces a `candidates.json`. **Scout only** — no Ranker, no Writer, no `latest.json`, no publish.

Good news: the repo already exists and is connected to `github.com/kyoheihanaki88/signals-feed` (branch `main`). So you only need to commit, push, and click a button.

---

## 1. Commit the Scout files

Open the Terminal app, then paste these one at a time. (The first line moves into the project folder — adjust if your path differs.)

```bash
cd ~/Documents/Claude/Projects/"Signals app"/signals-feed

git add .github/workflows/scout.yml pipeline/scout.py pipeline/sources.yaml .gitignore RUN_SCOUT.md

git commit -m "Add Signal Scout + daily GitHub Action (Build 1, Step D)"
```

> `candidates.json` and the local `cache/` folder are intentionally **not** committed — they're in `.gitignore`. The Scout's output comes back as a downloadable artifact, not a file in the repo.

## 2. Push to GitHub

```bash
git push origin main
```

If it asks for a username/password, the password is a **GitHub personal access token**, not your account password. (If you've pushed to this repo before, it'll just work.)

## 3. Open GitHub Actions

1. In a browser go to **https://github.com/kyoheihanaki88/signals-feed**
2. Click the **Actions** tab (top of the page).
3. In the left sidebar, click **Signal Scout**.

If you see a one-time banner like *"Workflows aren't being run on this repository"*, click the green **"I understand… enable"** button.

## 4. Run the workflow manually

1. On the **Signal Scout** page, click the **Run workflow** button (right side).
2. Leave the branch as **main**.
3. Click the green **Run workflow**.
4. Wait ~30–60 seconds, then refresh. A new run appears — click it. When the dot turns **green ✓** it's done. (A **red ✗** is fine to inspect too — see step 7.)

## 5. Check the job summary

1. Click into the run, then click the **scout** job.
2. Scroll to the top — there's a **Summary** box titled **"Signal Scout — <date>"** showing:
   - **Candidates collected** (a number)
   - a small table of **candidates per source**
   - **Failed / skipped feeds**
   - **Skipped items**
   - **Cross-source clusters**

That summary is the whole report at a glance.

## 6. Download the candidates artifact

1. On the run page, scroll to the bottom to the **Artifacts** section.
2. Click **candidates-…** to download a `.zip`.
3. Unzip it → you get **`candidates.json`**. Open it (any text editor) to see the real story list.

---

## 7. Confirm it worked

Tick these off:

- [ ] **Candidates count** — the summary shows a number **> 0** (expect roughly **30–60** on a live run; more than the 24 you saw locally, because the runner can also reach Guardian / Verge / Al Jazeera).
- [ ] **Failed feeds** — listed and sensible. `NHK World (Japan)` will show as failed (no URL yet) — that's expected. If BBC or NPR fail, that's worth a look.
- [ ] **Skipped items** — a small number (videos, homepages, stale, dupes). Non-zero is healthy — it means the filters are working.
- [ ] **Clusters found** — at least **1–3** cross-source clusters (the same story from two outlets). This is the importance signal.
- [ ] **candidates.json exists** — you downloaded and opened it, and it lists real article URLs (`bbc.com/news/articles/…`, `npr.org/2026/…`), not homepages.

## 8. Decide: is Scout good enough to move to the Ranker?

**Move on if:** candidates > ~20, BBC + NPR (and ideally Guardian/Verge) succeeded, URLs are real articles, and at least one cluster appeared. That's enough raw material for the Ranker to pick 5.

**Pause and fix first if:** candidates is very low (< 10), the big sources failed, URLs look like homepages, or nothing clustered. Tell me what the summary showed and I'll adjust the Scout — **don't** start the Ranker on a weak feed.

> One known gap either way: **Japan coverage** is empty until NHK's feed is resolved. Not a blocker for testing the Ranker, but worth fixing before launch.

---

*Scout only. This run does not rank, summarize, build `latest.json`, commit results to main, open a PR, or deploy anything.*
