# MediaIntel — Handoff

Last updated: 2026-07-30, end of day. All of today's work is committed and pushed to `origin/main`
at `180dfaa` ("Replace TV episode guessing with TMDb lookup; fix rip workflow papercuts").
**The server was fully rebooted** at the end
of this session (not just `mediaintel.service` — a full `reboot`), at your explicit request, partly
to retest the optical drive issue described below. This session ended immediately when the reboot
was issued, so there was no way to confirm anything post-reboot — check `systemctl status
mediaintel` and `/media/` first thing when you're back, though it should just be up (`enabled` unit).

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

- Branch: `main` only, local and remote in sync (see commit at the very top of this doc).
- Working tree clean — everything from today (`app.py`, `config.py`, `templates/dashboard.html`,
  `templates/job.html`, `templates/rip.html`, `tmdb.py` new, this file) committed and pushed.
- Server was **fully rebooted** at the end of the 2026-07-30 session (not just the service).
  **Verified post-reboot** (2026-07-30 11:4x UTC): came back up clean (`enabled` unit, auto-started,
  PID 5744), `journalctl -b` has zero errors/exceptions, `/media/` and `/media/progress.json` both
  return 200, and the TV job page confirms "Look Up Episode" wording — all four of today's fixes
  (TMDb lookup, one-click classify, season-lock, live-progress polling) are confirmed live, not
  just built. The optical drive issue was retested post-reboot and did **not** improve — see
  "Optical drive hardware issue" above for the definitive (unresolved) result.
