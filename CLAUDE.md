# Spanishh

Tiny Anki-style flashcard web app for learning Spanish vocabulary. Two ways to run:

## Static (works on any device, GitHub Pages)

Open `index.html` in a browser, or visit the GitHub Pages URL.
State persists in **`localStorage`** — per-browser-per-device, no server needed.
First load bootstraps from `bootstrap.json`, then it's standalone.

## Local server (desktop)

```bash
python3 app.py --serve          # http://localhost:8000
python3 app.py --serve --port 9000
```

State persists to **`data.json`**. No dependencies (Python 3.9+ stdlib only).
Note: server-mode state and Pages-mode state are independent — they don't sync.

## Files

- `index.html` — static single-file app for GitHub Pages / phone.
- `app.py` — desktop HTTP server (same scheduler logic, embedded frontend).
- `seed.json` — Spanish/English pairs with `level` (CEFR A1–C2) and `sublevel` (1–N within each level). The static app falls back to this if `bootstrap.json` is missing.
- `bootstrap.json` — snapshot of `data.json` committed to seed the static app's first load with prior progress.
- `data.json` — desktop server's live state. Gitignored.
- `wordbank.txt` — ~76k Spanish words (xavier-hernandez/spanish-wordlist). Lazy-loaded for Add-form autocomplete. Not part of the review queue.
- `generate_words.py` — script that produced the curated A1–B1 portion of `seed.json`.

## Data model

Each card: `{id, spanish, english, level, sublevel, ease, interval_days, due_ts, reps, lapses}`.
Ratings: `0=Again, 1=Hard, 2=Good, 3=Easy` — applied SM-2 style in `review()` (Python and JS implementations match).

## Scheduling

`pickDue()`/`pick_due()` prioritizes review-due cards (already seen, now due) over introducing new ones. New cards are introduced in CEFR order: A1.1 → A1.2 → … → A2.1 → … Cards with empty `english` are drafts and skipped.

## Adding words

Edit `seed.json` before first run, or use the **Add** form in the UI. The static app's Add form writes to localStorage; the server writes to `data.json`.
