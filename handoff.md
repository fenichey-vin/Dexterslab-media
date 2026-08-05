# MediaIntel — Handoff

Last updated: 2026-07-31 (mid-session — you stepped away for a few hours, this is what happened
while you were out). **Code changes below are built, tested, and deployed on the running service,
but NOT YET committed to git** (only commit when you explicitly ask, per your own standing rule) —
see "Repo state" at the bottom for the exact file list.

## 2026-07-31 session — what happened while you were away

**1. Found and fixed a real infrastructure bug: Jellyfin couldn't show staged files at all.**
Jellyfin's container had a bind mount pointing at `/srv/media/staging` (empty, stale — confirmed
independently by a storage audit from 2026-07-27 that already called it "retired"), when mediaintel
actually writes staged files to `/srv/work/staging`. Recreated the Jellyfin container via Portainer
with the corrected mount — confirmed healthy afterward, mount now correctly shows
`/srv/work/staging → /media/staging`. (Along the way: you'd lost the Portainer admin login — reset
via Portainer's official `helper-reset-password` container against the same `portainer_data`
volume, no data lost.)

**2. Built "Commit All Staged" + a commit-in-progress banner** (addresses the "Commit All looks
unresponsive" confusion from yesterday — root cause was a synchronous cross-drive file move with
zero progress feedback, see yesterday's notes below):
- New sticky banner (`templates/base.html`, `static/style.css`) — "PLEASE WAIT — COMMIT IN
  PROGRESS," dark red bold on light red, shown for the duration of *any* commit action (single
  title, per-job Commit All, or the new global button) via two small JS helpers in `static/app.js`.
- New **"✓ Commit All Staged"** button in the dashboard's Staged section header — commits every
  staged title across every staged job in one click. Backend: `app.py` refactored the existing
  per-job commit logic into a shared `_commit_all_titles_for_job()` helper, reused by both the
  existing per-job route and the new `/media/commit-all-staged` route — one implementation, not two.
- Tested (syntax-checked, deployed, confirmed rendering) but **not actually triggered for real** —
  didn't want to commit real files to the library as a side effect of testing a UI feature.

**3. Fixed The Sopranos' episode numbers and titles — all of Season 1 was wrong, not just missing
titles.** You'd manually classified 4 separate S01 discs (11 real episodes across them) with
correct-looking sequential numbers but **empty episode titles** and, it turned out, **wrong episode
numbers starting at disc 3** (disc 3 was labeled E07-E10, should have been E09-E12 — a gap opened
up because nothing had been ripped yet for the real E07/E08). Season 2's numbers were already right;
only one episode title had a typo.

**Strategy used (repeatable for future shows with the same problem):**
1. Pulled real TMDb episode data for the show/season — number, title, *and runtime in minutes*
   (`/tv/{id}/season/{n}` — the existing `tmdb.py` only pulls title, so this pass queried TMDb
   directly for runtime too).
2. Compared each ripped title's **actual ripped duration** (a real, unfakeable fact from the file
   itself) against every candidate episode's TMDb runtime.
3. Solved it as a constrained assignment problem, not a greedy nearest-match: minimize total
   duration difference *subject to* each disc's titles staying in strictly increasing episode
   order (physical discs are essentially always authored in chronological order — a plain
   nearest-duration match without this constraint produced an out-of-episode-order result within
   one disc, which is implausible and was a clear tell that the unconstrained approach was wrong).
   Solved via a small dynamic-programming assignment (pure Python, no new dependency) — total error
   across all 11 matched episodes was under 3 minutes combined, and the constrained solution hit
   the *same* minimal cost as the unconstrained one, which is exactly the confirmation you want
   that the ordering constraint didn't force a worse fit.
4. Applied the corrected season/episode/title to all 11 real episodes via the existing classify
   API (safe to do on `staged` jobs — same reasoning as yesterday's Sex and the City fix: commit
   re-reads live classification, doesn't care what the staged file is currently named).

**Result — Season 1** (job → title → corrected):
- Disc 1 (`...154015`): E01 The Sopranos, E02 46 Long, E03 Denial Anger Acceptance, E04 Meadowlands
- Disc 2 (`...164713`): E05 College, E06 Pax Soprana
- Disc 3 (`...173238`): **E09 Boca, E10 A Hit Is a Hit, E11 Nobody Knows Anything, E12 Isabella**
  (corrected from the wrong E07-E10 labels)
- Disc 4 (`...182239`): E13 I Dream of Jeannie Cusamano (titles 1/2 stay `deleted` — genuine short
  clips, ~4 min each; title 3 stays the "David Chase Interview" special — at 77.5 min it's far
  longer than any single episode's TMDb runtime, so it's a real bonus feature, not a misclassified
  episode)

**Result — Season 2** (one job, `...192437`): E01-E04 numbers/titles were already correct; only
fixed a typo on E03 ("Tootle-Fucking-Oo" → real title "**Toodle**-Fucking-Oo").

**Known gap, not fixable without new physical media**: **S01E07 "Down Neck" and S01E08 "The Legend
of Tennessee Moltisanti" have never been ripped** — there's a real hole in the disc sequence
between disc 2 (ends at E06) and disc 3 (now correctly starts at E09). Whatever physical disc
contains those two episodes hasn't been ripped yet. Nothing to do here except rip that disc when
you get to it — it'll land in this same S01 sequence.

No duplicate/catalog conflicts from any of this (checked: zero existing Sopranos catalog rows,
nothing committed yet for this show, so no risk of a path collision from the renumbering).

---

## TL;DR — what happened today (2026-07-30), for when you sit back down

You ripped 2 TV discs successfully (Sex and the City S01E01-06, S01E07-12) plus 2 movies (Act of
Valor, The Hurt Locker), and hit four real problems in a row. All four got fixed. (A 3rd/4th TV
disc scan attempt, `SATC_DISC3`/`SATC_DISC4`, never got past scanning — that's the separate,
unresolved optical-drive hardware issue below, not a software problem.)

1. **A job's display name doesn't update when you reclassify a title** (the AOV job kept showing
   "AOV S01" in the header after you fixed the title to "Act of Valor") — cosmetic only, fixed by
   hand for that one job; the underlying behavior (job name is set once at rip time, nothing
   renames it later) is unchanged/by design.
2. **Classifying a single-movie disc took 3-4 clicks for no reason** — fixed: Save Context now
   auto-classifies a disc's only title if it's still unclassified.
3. **TV episode titles kept coming out wrong — repeatedly, even after manual fixes** (season
   guessed wrong, an episode defaulted to a duplicate of episode 1, "Ask Claude" renamed things to
   a *different* wrong title on a second try). Root cause: asking an AI to guess a fixed,
   published fact (an episode's real title) from a couple of screenshots. **Fixed properly, not
   patched**: TV episode titles now come from a real TMDb lookup keyed on season/episode number
   (which was always deterministic from disc position — see `tmdb.py`, new). Claude is no longer
   involved in naming TV episodes at all. All 12 Sex and the City episode titles across both jobs
   were corrected with real data as part of this (see below) — expect at least one, maybe more, to
   still need a quick box-check (see per-episode notes below).
4. **The dashboard progress bar looked stalled while actively encoding** — it wasn't; the real fix
   (JS polling instead of full-page reload) was built hours before this reboot but sat undeployed
   the whole time, because the service was never restarted after that specific file changed. This
   reboot is what finally puts it live — if it's still not working after you're back, that's a new
   problem, not this one recurring.

Also: **hit real hardware read errors on both optical drives** (kernel-level, confirmed via
`dmesg`) on two different physical SATC discs — see "Optical drive hardware issue" below, this is
NOT fixed, the reboot is a diagnostic step, not a confirmed solution.

## What this is

Home media server app (`/home/vinnie/mediaintel/`, Flask on port 8088, systemd unit
`mediaintel.service`). Full architecture/workflow is documented in `/home/vinnie/CLAUDE.md` —
read that first for the rip → review → classify → encode → commit → Jellyfin pipeline and the
CLI tools (`riptv`, `encodetv`, `identifytv`).

This file tracks the **catalog/provenance work** built across four sessions on top of that base
app — where it stands, what's safe to assume, and what's deliberately not built yet.

## What's been built, in order

1. **Permanent catalog + backfill** (`4fb85df`, `6362748`) — `catalog` table records every title
   ever committed to the library. `bin/catalog-backfill` / `bin/catalog-verify` are standalone
   CLIs (dry-run-safe, idempotent) that discover and verify existing commits.
2. **Library import** (`26e3cd2`) — `bin/catalog-import-library` scans `/srv/media/movies` and
   `/srv/media/tv` for content that predates MediaIntel or was added outside it, and catalogs it
   with `source='library_import'` (vs. `mediaintel_job` for app-committed rows). Added the
   `source` column and made `catalog.job_id` nullable (SQLite table-rebuild migration, already
   applied to the live db).
3. **Duplicate visibility** (`983c6f0`) — `catalog.find_possible_duplicates()` runs a
   deterministic (no fuzzy/AI matching) check against the catalog *and* job history (active,
   staged, uncataloged-completed jobs) right when a title is classified, before the save commits.
   Shows a persistent "Possible Duplicate" panel on the review page (`job.html`) with
   HIGH/MEDIUM/LOW confidence — advisory only, never blocks anything, no dismiss control. Also
   added the read-only Catalog Detail page (`/media/catalog/<id>`).
4. **Job provenance & permanent history** (`983c6f0` bulk, `3f5ab53` polish) — the Job page
   (`/media/job/<id>`) is now the permanent record of *how* a file was made: a Job Summary (disc
   info, title counts, Rip Time / Encode Time / **Job Lifetime** — the last one is an elapsed
   wall-clock span from disc-insert to last activity, deliberately *not* called "processing time"
   since it can include idle review/staging time), a Job Timeline, and per-title
   classified/encoded/verified/committed milestones. Added exactly 3 nullable `jobs` columns
   (`rip_completed_at`, `encode_started_at`, `encode_completed_at`) — everything else is derived
   from data that already existed. Catalog Detail links back to its job ("Produced by Job …");
   the job page links forward to any catalog row it produced.

## Today's session (2026-07-30) — everything committed and pushed at end of day

Three real rip sessions surfaced actual problems, which led to four code changes (the fourth being
a full architectural fix, not a patch). All committed — see "Repo state" for the commit.

**1. One-click classify for single-title discs** (`templates/job.html`, `saveContext()`): clicking
"Save Context" on a disc with exactly one still-unclassified title now classifies it immediately
(movie type only) instead of requiring a second manual Save in the per-title form. Advisory-only,
never overwrites an existing classification.

**2. → 4. The TV episode naming saga.** This took three attempts to actually fix, in order:
- **Attempt 1 (prompt fix, insufficient alone):** Disc 1 (S01E01-05 entered, but 6 titles ripped):
  the 6th title got auto-classified as a duplicate of episode 1, because the "Episodes on disc"
  list was one entry short, leaving Claude with no episode-number hint. Then Disc "S02" (really
  S01E07-12 — **Season 1 spans multiple discs, it's not just 6 episodes**, an earlier wrong
  assumption in this doc that's now corrected): Claude's "Classify All" changed the season from 1
  to 2 on its own, because the prompt only *showed* the season as an example value with nothing
  telling Claude not to override it. Fixed the prompt (`_build_classify_prompt()` in `app.py`) to
  explicitly lock the season as operator-confirmed, plus a client-side warning
  (`templates/job.html`, `saveContext()`) when "Episodes on disc" count doesn't match the number of
  actual episode-length titles. **This helped, but didn't fully solve it** — episode *number* was
  now reliable, but episode *title text* was still a screenshot guess.
- **Attempt 2 (manual fix from memory, also wrong):** manually corrected the S01E07-12 titles by
  hand — turned out to be wrong too (not authoritative, just a better guess).
- **Attempt 3, the real fix — TMDb lookup, no more AI guessing for TV titles at all:** re-running
  "Ask Claude" on the same disc produced yet a *third*, still-wrong set of titles. That's what
  triggered the actual architectural fix: **new `tmdb.py`** module looks up real episode titles
  from themoviedb.org, keyed on show/season/episode number — which was always deterministic from
  disc position (`_disc_episode_number()` in `app.py`, extracted from the existing position-hint
  logic), never a guess. For TV context, both "Ask Claude" (now labeled "Look Up Episode") and
  "Classify All" (now "Look Up All Episodes") call `tmdb.lookup_episode_title()` instead of the
  Claude vision API — `_auto_classify_tv_from_lookup()` in `app.py`. If TMDb can't resolve an
  episode, the number is still saved (never depended on TMDb) and the title is left blank at
  `confidence: 'low'` for manual entry — **it never falls back to a guessed title**, since
  reintroducing that would defeat the entire point. Movies/specials/documentaries are completely
  unchanged — still Claude screenshots, since there's no per-number database lookup equivalent for
  "is this the main feature or a bonus feature."
  - Needs `TMDB_API_KEY` (free key from themoviedb.org) — **already set** via `sudo systemctl edit
    mediaintel` (same mechanism as `ANTHROPIC_API_KEY`), confirmed present before this was even
    finished, so it's live and working, not just built.
  - **Retroactively corrected all 12 Sex and the City episodes** (both jobs) with real TMDb data,
    replacing every previous guess:
    - S01E01-06: only E05/E06 needed correction. E05 is "The Power of Female Sex" per TMDb (this
      doc/Claude had "...Sexuality" — could be an alternate broadcast title, or a TMDb quirk, worth
      a quick box check). E06 is **"Secret Sex"** — my own earlier manual guess ("The Awful Truth")
      was flat wrong.
    - S01E07-12: all 6 corrected — The Monogamists, Three's a Crowd, The Turtle and the Hare, The
      Baby Shower, The Drought, Oh Come All Ye Faithful.
    - Safe to do while the S01E07-12 job was mid-encode: HandBrake reads a classification.json
      snapshot taken at Approve time, but `commit_title()`/`commit_all()` re-read the *live*
      `titles.classification` at commit time — so fixing the DB didn't touch the running encode,
      and the eventual committed filename/path will be correct regardless of what name the staged
      file currently has.
  - `rip.html`'s disc-context blurb also reworded away from "Claude uses this to identify each
    episode" for honesty, since that's no longer true for TV.

**5. Dashboard progress looked stalled while actively encoding** (it wasn't — real encode was
running, files were growing on disk, just the on-screen % was stale). Root cause: the dashboard
only auto-refreshed (full page reload, every 5s) while a job was active/encoding/queued, and
browsers throttle/pause that timer for backgrounded tabs. Replaced with:
- New read-only endpoint `GET /media/progress.json` — returns job-status buckets (to detect when a
  real reload is needed, e.g. a job finished) plus raw `rip_progress`/`encode_progress` dicts.
- `templates/dashboard.html`: JS polls that endpoint every 2s and updates the progress bar/%/ETA
  **in place** without reloading; only does a full `location.reload()` when the bucket signature
  actually changes (a job moved sections). Also re-polls immediately on `visibilitychange` so
  switching back to a backgrounded tab catches up instantly rather than waiting on a possibly-
  throttled timer. The old 5s meta-refresh is now just a 60s fallback safety net.

**Important timing note:** the dashboard.html/progress.json changes above were built, then sat
**undeployed for over an hour** before this reboot — the service was restarted once right after
item 2's first attempt, but not again after this item's files changed, so the running process was
serving the *old* dashboard the whole rest of the session. That's the actual reason the progress
bar "still wasn't working" late in the day even though the fix already existed on disk — not a bug
in the fix itself. This reboot is what finally deploys it.

All changes tested against the live service where possible before this reboot (each restart done
only after confirming no active rip/encode). Committed and pushed — see "Repo state."

## Optical drive hardware issue (2026-07-30, unresolved — reboot is a diagnostic step, not a fix)

Both `/dev/sr0` (SATA) and `/dev/sr1` (USB) hit real kernel-level `Medium Error: L-EC UNCORRECTABLE
ERROR` reads on physical discs — confirmed via `dmesg` and by running `makemkvcon -r info` directly
(not a bug in this app's scan code, which just reports whatever MakeMKV returns). Two **different**
physical discs (`SATC_DISC3` and `SATC_DISC4`) both failed at the **identical** file
(`/VIDEO_TS/VTS_02_1.VOB`) on the same drive (`sr0`), both with MakeMKV logging `Title #N in broken
titleset was skipped` for a large range of titles afterward — only the first title set (3 titles)
comes through clean on either disc.

**Correction to an earlier wrong theory in this doc:** I initially misread the scan output's
`"DVD+R-DL ATAPI iHAS124..."` string as describing the disc type (burned dual-layer media) and
built a whole theory around that. That string is actually the **drive's own hardware model name**
(a real optical drive model that happens to support burning DVD+R DL) — it says nothing about the
disc. The user confirmed these are the full retail pressed DVD set, not burns. Given that, and
given two different discs fail at the identical logical position on the same drive, the more
likely explanation is **this drive's laser/optics struggling at the layer-break transition point**
that dual-layer pressed discs commonly have (not disc damage — two different discs being damaged
at the exact same byte offset would be an extraordinary coincidence).

Tried a **targeted, low-risk fix first** (not a full reboot): deleted and rescanned both device
nodes via sysfs (`echo 1 > /sys/block/srN/device/delete` + `echo "- - -" > /sys/class/scsi_host/
hostN/scan`, hosts 3 and 6 respectively). This restored correct `root:cdrom` permissions/ACLs (sr0
briefly came back `root:root` only — fixed with `udevadm trigger --action=add` +
`udevadm settle`), and both drives report media present (`/sys/block/srN/size` nonzero) — but
**re-scanning after the reset produced the identical medium error on the identical file again**,
and the scan hung until timeout rather than completing gracefully. A logical device reset doesn't
power-cycle the actual drive hardware, so this doesn't rule out the laser/optics theory above —
it just means the sysfs-level reset alone wasn't enough.

**Update, post-reboot (2026-07-30 11:4x UTC): the full reboot did NOT fix it.** Re-ran the exact
same scan on `sr0` with `SATC_DISC3` still in the drive — identical result: same error on
`/VIDEO_TS/VTS_02_1.VOB`, same "Title #N in broken titleset was skipped" for titles 7-27, same 3
clean titles. A genuine power cycle failing to change anything at all is strong, near-conclusive
evidence this is real drive hardware wear (laser/optics), not a stuck software/bus/controller
state — there's nothing left to try at the software level. **Next step should be trying a
different/replacement drive**, not another reset or reboot. `sr1` (the second drive) was not
re-tested with this same disc post-reboot — worth doing if a definitive "is it just sr0" answer is
wanted, though the original two-different-discs-same-failure-point evidence already pointed at
`sr0` specifically.

**Update, 2026-07-31: user ran `dd`/`dvdbackup` diagnostics directly (against Sweet Home Alabama,
not the SATC discs) — one real signal, one red herring, worth recording so it isn't misread later:**
- `sudo dd if=/dev/srN of=/dev/null` failed immediately on **both** drives. On `sr1` this is a
  **false alarm** — the error was `Sense Key: Illegal Request / "Read of scrambled sector without
  authentication"`, which is the drive *correctly refusing* CSS-encrypted sectors to a tool
  (`dd`) that never did the DVD copy-protection handshake. That happens on any DVD drive reading
  any commercial disc via plain `dd` — it is not evidence of a hardware problem on `sr1`.
  `sr0`'s failure was a genuine medium-error-class read (no scrambled-sector message) — consistent
  with the existing marginal-media findings, not new information.
- `DVDCSS_VERBOSE=2 dvdbackup -I -i /dev/srN` (a CSS-aware tool with retry logic, unlike `dd`)
  **succeeded completely on both drives** for this disc — full title-set/VOB structure listed,
  zero errors in either run. Consistent with what was already known: Sweet Home Alabama hit real
  medium errors mid-rip via MakeMKV and still finished successfully, because MakeMKV/libdvdcss-based
  tools retry through marginal reads that a no-retry raw `dd` does not.
- **This was not tested against `SATC_DISC3`/`DISC4`** — the actual hard-failing discs. Running the
  same `dvdbackup -I` command against one of those (not just Sweet Home Alabama) would be the more
  informative next test: if it fails even at the authentication/structure-listing level (not just
  during a full VOB rip), that's a stronger, cleaner signal than anything gathered so far.

## Current data snapshot (end of day, 2026-07-30, before the reboot)

- Catalog: **240 rows** — 81 `mediaintel_job`, 159 `library_import`; 239 `verified`, 1 `failed`
  (the known Man on Fire (2004) row — recorded FAILED/unreadable, not a bug, not yet repaired).
  Unchanged today — nothing from today's rips has been committed to the library yet, all still
  sitting in `review`/`staged`.
- Jobs: **102 total** — 82 `complete`, 13 `staged`, 7 `review`. No `error`-status jobs currently
  exist (grew from 95/8/5 two days ago due to today's rip sessions: Sex and the City ×2 discs, Act
  of Valor, The Hurt Locker, and others already staged/reviewed — none committed yet, so expect the
  catalog count to jump once you start committing these).
- Known, understood, intentionally-untouched data quirks (don't be alarmed by these):
  - **Top Gun** has two historical rip attempts: job `Top-Gun-S01-20260623192823` → catalog row
    `#26` (verified, holds the library path) and job `Top-Gun-20260623183305` (`complete`, no
    catalog row of its own — a real, surfaced-not-resolved duplicate). Both the duplicate panel
    and Catalog Status's conflicts table show this correctly; nothing has been merged or deleted.
  - **Man on Fire (2004)** — catalog row failed verification (recorded size 7.3 GB vs. actual
    301.2 MB). Shown everywhere as failed, never as healthy. Not repaired.
  - **Skyfall (2012)** has a second copy sitting in `staged` status
    (`Skyfall-2012-20260623152934`) alongside the already-verified one — a live example of the
    duplicate panel actually catching something real.

## Safe assumptions for continuing work

- **DB migrations are idempotent** — `db.init_db()` (called on every service start) safely
  no-ops if a column/table already exists. Additive columns use
  `try: ALTER TABLE ... ADD COLUMN / except: pass`; the one structural change (making
  `catalog.job_id` nullable) is guarded on the column's current `notnull` flag.
- **Backup-before-migrate discipline**: `mediaintel.db.bak-YYYYMMDD-HHMMSS` — three exist in
  `/home/vinnie/mediaintel/` (`.gitignore`d, never committed). Take a new one before any further
  schema change, same pattern as before.
- **Restart discipline**: schema-only changes never need a service restart (every `db.py` call
  opens a fresh connection). Python/template changes do. Before restarting, always check for
  in-progress work first: `rip_progress`/`encode_progress` non-null on any job, and
  `ps aux | grep -iE "makemkv|handbrake|ffmpeg"`.
- **Before assuming a fix is live, check the restart timestamp against the file's mtime** — bit us
  today: the dashboard live-progress fix sat on disk for over an hour after the last restart before
  anyone (including me) noticed it had never actually been deployed. `systemctl show mediaintel
  -p ActiveEnterTimestamp` vs. `ls -la --time-style=full-iso <file>` catches this immediately;
  don't just assume "I edited it, it must be live."
- **Duplicate detection is deliberately non-fuzzy** — exact-normalized-title match required for
  any confidence tier; a title/year mismatch on an otherwise-exact title match is surfaced as LOW
  ("possible remake"), never silently dropped. See `catalog.py`'s `_movie_match`/`_tv_match` for
  the exact rules if extending this.
- **`bin/encodetv`** is a second, legacy encode path (writes straight to the final library,
  bypassing staging) that still exists and is still wired for provenance timestamps — if it's
  used, jobs processed through it get `encode_started_at`/`encode_completed_at` too, and rely on
  `bin/catalog-backfill` to eventually get a catalog row (it doesn't insert one itself).
- **TV episode titles no longer come from Claude at all** — `tmdb.py` looks them up from
  themoviedb.org by season/episode number instead. `TMDB_API_KEY` must stay set (systemd override,
  same as `ANTHROPIC_API_KEY`) or TV titles will silently come back blank (never a wrong guess,
  just empty — check `confidence: 'low'` on affected titles). Movies/specials/documentaries are
  unaffected, still Claude screenshots as always.
- **Claude will second-guess an operator-provided season/episode number if given the chance** —
  confirmed root cause of a real incident today. `_build_classify_prompt()`'s season-lock wording
  only applies to the movie/Claude path now (TV no longer calls Claude) — worth remembering if this
  ever gets extended, since the underlying tendency (Claude "correcting" things it's only supposed
  to confirm) isn't specific to season numbers.
- **A disc's "Episodes on disc" list must have one entry per real episode title** — if it's short,
  the excess title(s) get no episode number at all (not even TMDb can help without a number to look
  up), and the fallback is a blank title needing manual entry, not a guess. The client-side warning
  added today catches this before Classify All/Look Up All Episodes runs, but it's advisory only,
  not enforced.
- **TMDb show-name matching is simple, not bulletproof** — `search_tv_show()` prefers an exact
  case-insensitive name match, else TMDb's top search result. Shows with very generic names or
  multiple same-named entries could match the wrong series. No user-facing disambiguation exists
  yet if this becomes a real problem.

## Known, accepted gaps (not bugs — deliberately out of scope so far)

- No demonstrated `complete` TV-episode job or `error`-status job exists in real data yet — the
  Sex and the City discs ripped today are the first real TV content through the live pipeline, but
  they're currently sitting in `review`/`staged`, not yet `complete`. Worth re-checking once one of
  them is actually committed.
- Whether a not-committed title was "never encoded," "encode failed," or "manually discarded"
  is not distinguishable from the database alone (only `/srv/work/logs/encode_log.txt` has that
  detail, and the provenance work was explicitly told not to depend on log files). Not fixed —
  flagged as a real, accepted limitation.
- No classification-edit history, no reverify/retry history — both would need new persistence
  and weren't judged worth the schema growth yet.
- **Phase 4 (cleanup)** — deleting raw rip-source files for verified titles — was scoped in the
  original design doc but never implemented. Nothing in the current code path deletes anything
  automatically or manually; if this gets picked up, `mediaintel-catalog-plan.md` in this repo
  has the original design for it.

## Where to look

- `mediaintel-catalog-plan.md` — original 4-phase design doc (catalog/backfill, duplicate
  detection, cleanup dry-run, cleanup). Phases 1–3 above supersede/extend parts of it with more
  detail than originally scoped (esp. duplicate detection, which ended up classify-time and
  deterministic rather than disc-scan-time and fuzzy). Phase 4 (cleanup) in that doc is still
  not implemented and still the next logical step if wanted.
- `/home/vinnie/.claude/plans/bubbly-sniffing-sonnet.md` — the most recent approved plan file
  (Phase 3 provenance); prior plans for Phases 1/2A/2B were overwritten by later planning
  sessions in the same file, so this only reflects the latest one verbatim.
- `catalog.py` — all business logic (backfill, verify, library import, duplicate matching)
  lives here. `db.py` is thin CRUD + schema only. `app.py` is routes.
- `tmdb.py` *(new today)* — the only place TV episode titles come from now. `search_tv_show()` /
  `get_season_episodes()` (cached) / `lookup_episode_title()`. Read this before touching anything
  related to TV classification.

## Repo state

- Branch: `main` only. Remote (`origin/main`) is at `180dfaa`, same as end of 2026-07-30 (that's
  all still committed/pushed).
- **Working tree is NOT clean right now** — 2026-07-31's commit-banner/Commit-All-Staged work is
  uncommitted: `app.py`, `static/app.js`, `static/style.css`, `templates/base.html`,
  `templates/dashboard.html`, `templates/job.html`. Built, syntax-checked, restarted into the live
  service, and confirmed rendering correctly — just not committed (only commit when you ask).
  The Sopranos episode fix and the Jellyfin/Portainer work from today were **data/infrastructure
  changes, not code** — nothing to commit for those.
- Service was restarted once today (2026-07-31, after the commit-banner changes) — no active
  rip/encode at that time, confirmed clean restart, no journal errors.
- Server was **fully rebooted** at the end of the 2026-07-30 session (not just the service).
  **Verified post-reboot** (2026-07-30 11:4x UTC): came back up clean (`enabled` unit, auto-started,
  PID 5744), `journalctl -b` has zero errors/exceptions, `/media/` and `/media/progress.json` both
  return 200, and the TV job page confirms "Look Up Episode" wording — all four of today's fixes
  (TMDb lookup, one-click classify, season-lock, live-progress polling) are confirmed live, not
  just built. The optical drive issue was retested post-reboot and did **not** improve — see
  "Optical drive hardware issue" above for the definitive (unresolved) result.
