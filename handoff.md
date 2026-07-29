# MediaIntel — Handoff

Last updated: 2026-07-29. Current `main` HEAD: `3f5ab53` ("Refine job lifetime reporting"),
pushed to `origin/main`. Single-branch repo — no other local or remote branches exist, nothing
outstanding to merge.

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

## Current data snapshot (at last restart, 2026-07-29 12:46 UTC)

- Catalog: **240 rows** — 81 `mediaintel_job`, 159 `library_import`; 239 `verified`, 1 `failed`
  (the known Man on Fire (2004) row — recorded FAILED/unreadable, not a bug, not yet repaired).
- Jobs: 95 total — 82 `complete`, 8 `staged`, 5 `review`. No `error`-status jobs currently exist.
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
- **Duplicate detection is deliberately non-fuzzy** — exact-normalized-title match required for
  any confidence tier; a title/year mismatch on an otherwise-exact title match is surfaced as LOW
  ("possible remake"), never silently dropped. See `catalog.py`'s `_movie_match`/`_tv_match` for
  the exact rules if extending this.
- **`bin/encodetv`** is a second, legacy encode path (writes straight to the final library,
  bypassing staging) that still exists and is still wired for provenance timestamps — if it's
  used, jobs processed through it get `encode_started_at`/`encode_completed_at` too, and rely on
  `bin/catalog-backfill` to eventually get a catalog row (it doesn't insert one itself).

## Known, accepted gaps (not bugs — deliberately out of scope so far)

- No demonstrated `complete` TV-episode job or `error`-status job exists in real data yet (all
  current TV content came in via `library_import`, not the live rip pipeline). Worth re-checking
  once a real TV disc goes through end to end.
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

## Repo state

- Branch: `main` only, local and remote in sync at `3f5ab53`.
- Working tree clean, nothing uncommitted.
- Service running (PID recorded above at last restart) — not restarted since, so ripping now
  will not lose any in-progress state.
