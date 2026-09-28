"""Permanent title catalog — backfill and verification (Phase 1).

Both functions here are strictly read-only with respect to the filesystem:
backfill only recomputes expected library paths from data already in the
`titles` table and inserts SQLite rows; verify only stats/ffprobes files
that already exist. Neither one ever writes, moves, or deletes a media file.
Deletion is Phase 4's job, in a separate, explicitly-named function — not
implemented here. See mediaintel-catalog-plan.md.
"""

import json
import re
import subprocess
from pathlib import Path
from datetime import datetime, timezone

import db
from disc import get_duration
from config import MOVIES_DIR, TV_DIR


def _now():
    return datetime.now(timezone.utc).isoformat()


def backfill_from_completed_jobs():
    """Discovery pass. For every job with status == 'complete', recompute
    each committable title's expected library path (via app._route_library,
    the same function commit already uses) and insert a 'pending' catalog
    row if one doesn't already exist for that (job_id, title_num).

    Idempotent — safe to re-run; titles already catalogued are counted in
    'already_present' and left untouched. Performs zero filesystem I/O
    beyond the SQLite writes — no file is opened, moved, or deleted.

    Returns a summary dict for reporting.
    """
    # Lazy import: app.py imports this module at call time (inside the
    # commit routes), so importing app at module load here would be
    # circular. By the time this function actually runs, app.py has long
    # since finished importing. Same pattern db.py already uses for config.
    from app import _route_library, _cls_label

    summary = {
        'jobs_examined': 0,
        'titles_considered': 0,
        'inserted': 0,
        'already_present': 0,
        'ambiguous': 0,
        'skipped_no_classification': 0,
    }

    jobs = [j for j in db.get_all_jobs() if j['status'] == 'complete']
    summary['jobs_examined'] = len(jobs)

    for job in jobs:
        for t in db.get_titles(job['id']):
            cls = t.get('classification')
            if not cls:
                summary['skipped_no_classification'] += 1
                continue

            ctype = cls.get('type', 'unknown')
            if ctype in ('play_all', 'deleted', 'unknown'):
                continue
            summary['titles_considered'] += 1

            existing = db.get_catalog_row(job['id'], t['title_num'])
            if existing:
                summary['already_present'] += 1
                continue

            library_path = _route_library(cls, t['title_num'])
            if library_path is None:
                summary['ambiguous'] += 1
                continue

            row_id = db.insert_catalog_row(
                job_id=job['id'], title_num=t['title_num'], state='pending',
                type_=ctype, show=cls.get('show'), season=cls.get('season'),
                episode=cls.get('episode'), episode_title=cls.get('episode_title'),
                movie_title=cls.get('movie_title'), year=cls.get('year'),
                label=_cls_label(cls, t['title_num']),
                disc_label=job.get('disc_label'),
                library_path=str(library_path),
                duration_secs=t.get('duration_secs'),
                filesize_bytes=t.get('filesize_bytes'),
                classification=json.dumps(cls),
                # Best available approximation of when this title was
                # actually committed, since commit didn't record it before
                # this catalog existed — the job's updated_at is the last
                # time its status changed, which for a 'complete' job is
                # the completing commit.
                committed_at=job.get('updated_at') or _now(),
            )
            if row_id is None:
                # UNIQUE(library_path) collision — some other row already
                # claims this exact destination path.
                summary['ambiguous'] += 1
            else:
                summary['inserted'] += 1

    return summary


def _check_file(library_path):
    """Read-only check of one file: exists, non-empty, ffprobe-readable, and
    actually decodable at three sample points (start/middle/end) rather than
    just the header/index ffprobe reads. Returns (state, fail_reason) where
    fail_reason is None when verified. Never opens the file for writing,
    never deletes, never moves anything, and never decodes more than ~10s
    per sample -- this is "does it play cleanly", not a full playthrough.
    """
    path = Path(library_path)

    if not path.exists():
        return 'failed', 'missing'
    if path.stat().st_size == 0:
        return 'failed', 'zero_bytes'
    duration = get_duration(path)
    if not duration or duration <= 0:
        return 'failed', 'unreadable'

    reason = _decode_sample_check(path, duration)
    if reason:
        return 'failed', reason
    return 'verified', None


_SAMPLE_SECS = 10  # length of each decode sample


# Known-benign ffmpeg stderr chatter that does NOT indicate corruption --
# confirmed harmless on 2026-09-04 by cross-checking against a full-file
# decode (`ffmpeg -f null -`) on 4 freshly re-encoded, definitely-intact
# library files (Sopranos S04E04/E05, Band of Brothers S01E01, SVU S01E10):
# every one of them emits this exact "non monotonically increasing dts"
# warning from the muxer somewhere in the file, with zero actual decode
# errors. Treating ANY non-empty stderr as failure (the original logic)
# produced a false positive on SVU S01E10 immediately after a verified-clean
# re-encode -- the check would have kept re-flagging a genuinely fixed file
# as corrupt forever. Only fail on stderr that actually names a real decode
# problem; an unrecognized-but-nonzero-returncode case is still a hard fail.
_BENIGN_STDERR_PATTERNS = (
    'non monotonically increasing dts',
)


def _is_benign_stderr(stderr_text):
    lines = [ln.strip() for ln in stderr_text.strip().splitlines() if ln.strip()]
    return all(any(p in ln for p in _BENIGN_STDERR_PATTERNS) for ln in lines)


def _decode_sample_check(path, duration):
    """Actually decode ~10s of video at the start, middle, and end of the
    file (not just read metadata) via ffmpeg -> /dev/null. Catches mid-file
    corruption (bad sectors, truncated/interrupted copies with a valid
    header) that ffprobe's metadata-only read would miss, while staying at
    a fixed ~30s total regardless of the file's actual runtime -- this is
    deliberately NOT a full playthrough. Returns a fail_reason string, or
    None if all three samples decoded cleanly (allowing known-benign muxer
    warnings -- see _BENIGN_STDERR_PATTERNS -- which are not corruption).
    """
    # Sample offsets: start (just past any intro black/logo), true middle,
    # and end (backed off far enough that a short file still has room for
    # a full _SAMPLE_SECS-length sample before EOF).
    offsets = sorted({
        max(0.0, 2.0),
        max(0.0, duration / 2 - _SAMPLE_SECS / 2),
        max(0.0, duration - _SAMPLE_SECS - 2.0),
    })

    for offset in offsets:
        try:
            r = subprocess.run(
                ['ffmpeg', '-v', 'error', '-ss', str(offset), '-i', str(path),
                 '-t', str(_SAMPLE_SECS), '-f', 'null', '-'],
                capture_output=True, text=True, timeout=60,
            )
        except subprocess.TimeoutExpired:
            return 'decode_timeout'
        if r.returncode != 0:
            return 'decode_error'
        if r.stderr.strip() and not _is_benign_stderr(r.stderr):
            return 'decode_error'
    return None


def verify_pending_rows():
    """Verification pass. For every catalog row in state 'pending' or
    'committed', check that library_path exists, is non-empty, and is
    readable by ffprobe (duration > 0). Read-only — never deletes or
    modifies the file being checked. Idempotent — re-running only revisits
    rows still sitting in pending/committed; rows already verified or
    failed are left alone.

    Returns a summary dict for reporting.
    """
    summary = {'checked': 0, 'verified': 0, 'missing': 0, 'failed': 0}

    for row in db.get_catalog_rows_by_state(['pending', 'committed']):
        summary['checked'] += 1
        state, reason = _check_file(row['library_path'])

        if state == 'verified':
            db.update_catalog_state(row['id'], 'verified', verified_at=_now())
            summary['verified'] += 1
        elif reason == 'missing':
            db.update_catalog_state(row['id'], 'failed', fail_reason=reason)
            summary['missing'] += 1
        else:
            db.update_catalog_state(row['id'], 'failed', fail_reason=reason)
            summary['failed'] += 1

    return summary


class ReverifyNotAllowed(Exception):
    """Raised by reverify_row() when the row's current state doesn't permit
    reverification. The row is left completely unchanged when this is
    raised — callers must handle it explicitly rather than the guard being
    something a caller could silently skip past.
    """


def reverify_row(catalog_id):
    """Re-check one specific catalog row's file — but only when its current
    state is 'failed' or 'verified'. Rows in 'pending', 'committed', or
    'cleaned' raise ReverifyNotAllowed instead, and are left completely
    unchanged: 'pending'/'committed' belong to the backfill/verify pipeline
    (not yet reconciled with disk at all), and 'cleaned' means the working
    files this check would otherwise be confirming are already gone by
    design, not by accident.

    Permitted transitions: failed->verified, failed->failed,
    verified->verified, verified->failed (e.g. a previously-good library
    file that has since gone missing or corrupted).

    Read-only with respect to the file itself (only stat + ffprobe); only
    writes the state/fail_reason/verified_at of this one catalog row. Never
    touches job status, never deletes or moves anything, never runs cleanup.

    Returns the updated row dict, or None if catalog_id doesn't exist.
    """
    row = db.get_catalog_row_by_id(catalog_id)
    if not row:
        return None

    if row['state'] not in ('failed', 'verified'):
        raise ReverifyNotAllowed(
            f"Catalog rows in state '{row['state']}' cannot be reverified"
        )

    state, reason = _check_file(row['library_path'])
    if state == 'verified':
        db.update_catalog_state(catalog_id, 'verified', verified_at=_now(), fail_reason=None)
    else:
        db.update_catalog_state(catalog_id, 'failed', fail_reason=reason)

    return db.get_catalog_row_by_id(catalog_id)


def find_uncataloged_conflicts():
    """Read-only. For every committable title belonging to a 'complete' job
    that has no catalog row yet, recompute its expected library path (same
    routing logic commit uses) and check whether that exact path is already
    claimed by a *different* (job_id, title_num) already in the catalog —
    i.e. the UNIQUE(library_path) collisions backfill silently skipped
    rather than guess about. Also reports titles whose classification
    couldn't be routed to any path at all, as a separate bucket. Performs
    no writes of any kind — pure recomputation and lookup.
    """
    # Lazy import — see the note in backfill_from_completed_jobs().
    from app import _route_library, _cls_label

    rows = db.get_all_catalog_rows()
    by_path = {r['library_path']: (r['job_id'], r['title_num']) for r in rows}
    cataloged_keys = {(r['job_id'], r['title_num']) for r in rows}

    conflicts   = []
    unroutable  = []

    for job in db.get_all_jobs():
        if job['status'] != 'complete':
            continue
        for t in db.get_titles(job['id']):
            cls = t.get('classification')
            if not cls:
                continue
            ctype = cls.get('type', 'unknown')
            if ctype in ('play_all', 'deleted', 'unknown'):
                continue

            key = (job['id'], t['title_num'])
            if key in cataloged_keys:
                continue

            label = _cls_label(cls, t['title_num'])
            library_path = _route_library(cls, t['title_num'])
            if library_path is None:
                unroutable.append({
                    'job_id': job['id'], 'title_num': t['title_num'], 'label': label,
                })
                continue

            holder = by_path.get(str(library_path))
            if holder:
                conflicts.append({
                    'job_id':          job['id'],
                    'title_num':       t['title_num'],
                    'label':           label,
                    'library_path':    str(library_path),
                    'holder_job_id':   holder[0],
                    'holder_title_num': holder[1],
                })

    return {'conflicts': conflicts, 'unroutable': unroutable}


# ── Library import (Phase 2A) ────────────────────────────────────────────────
# Catalogs media that already exists under MOVIES_DIR/TV_DIR but was never
# committed through a mediaintel job (source='library_import'). Strictly
# read-only with respect to the filesystem in BOTH dry_run and live modes —
# only os.listdir/Path.stat-equivalent reads, never a write, move, rename, or
# delete of any media file. dry_run additionally guarantees zero database
# writes (already-present is checked via db.get_catalog_row_by_path, a plain
# SELECT). Rows are always inserted as state='pending' — never auto-verified;
# bin/catalog-verify (unchanged) is what promotes them.

VIDEO_EXTS = {'.mkv', '.mp4', '.avi', '.m4v', '.mov', '.wmv'}
_MOVIE_RE  = re.compile(r'^(.+) \((\d{4})\)$')
_SEASON_RE = re.compile(r'^Season\s+(\d+)$', re.IGNORECASE)


def import_library_content(dry_run=True):
    """Scan MOVIES_DIR and TV_DIR for pre-existing library content with no
    catalog row yet. Returns a summary dict with one list per bucket
    (would_insert, already_present, ambiguous, unparsed, errors,
    known_skips) plus an 'inserted' count. Safe to re-run: already-catalogued
    paths (any source) are reported, never re-inserted or modified.
    """
    result = {
        'would_insert': [], 'already_present': [], 'ambiguous': [],
        'unparsed': [], 'errors': [], 'known_skips': [], 'inserted': 0,
    }

    _scan_movies(result)
    _scan_tv(result)

    if not dry_run:
        now = _now()
        for item in list(result['would_insert']):
            cls = item['classification']
            row_id = db.insert_catalog_row(
                job_id=None, title_num=0, state='pending',
                type_=cls['type'], show=cls.get('show'), season=cls.get('season'),
                episode=cls.get('episode'), episode_title=cls.get('episode_title'),
                movie_title=cls.get('movie_title'), year=cls.get('year'),
                label=item['label'], disc_label=None,
                library_path=item['library_path'], duration_secs=None,
                filesize_bytes=item['filesize_bytes'],
                classification=json.dumps(cls), committed_at=now,
                source='library_import',
            )
            if row_id is None:
                # Collided with a row that appeared since the scan (e.g. a
                # concurrent commit claimed this path) -- report it as
                # already-present rather than silently dropping it.
                result['would_insert'].remove(item)
                result['already_present'].append(item)
            else:
                result['inserted'] += 1

    return result


def _record_candidate(result, path, cls):
    library_path = str(path)
    try:
        filesize_bytes = path.stat().st_size
    except OSError as e:
        result['errors'].append({'path': library_path, 'reason': str(e)})
        return

    from app import _cls_label
    item = {
        'library_path': library_path,
        'label': _cls_label(cls, 0),
        'classification': cls,
        'filesize_bytes': filesize_bytes,
    }
    existing = db.get_catalog_row_by_path(library_path)
    if existing:
        item['existing_state']  = existing['state']
        item['existing_source'] = existing['source']
        result['already_present'].append(item)
    else:
        result['would_insert'].append(item)


def _scan_movies(result):
    if not MOVIES_DIR.is_dir():
        result['errors'].append({'path': str(MOVIES_DIR), 'reason': 'movies root not found'})
        return
    try:
        entries = sorted(MOVIES_DIR.iterdir())
    except OSError as e:
        result['errors'].append({'path': str(MOVIES_DIR), 'reason': str(e)})
        return

    for entry in entries:
        try:
            if entry.is_file():
                result['unparsed'].append({
                    'path': str(entry),
                    'reason': 'stray file directly in movies root, not inside a title folder',
                })
            elif entry.is_dir():
                _classify_movie_folder(entry, result)
        except OSError as e:
            result['errors'].append({'path': str(entry), 'reason': str(e)})


def _classify_movie_folder(folder, result):
    try:
        files = sorted(f for f in folder.iterdir() if f.is_file())
    except OSError as e:
        result['errors'].append({'path': str(folder), 'reason': str(e)})
        return

    if not files:
        result['known_skips'].append({'path': str(folder), 'reason': 'empty directory'})
        return

    videos = [f for f in files if f.suffix.lower() in VIDEO_EXTS]
    if len(videos) > 1:
        result['ambiguous'].append({
            'path': str(folder),
            'reason': f'{len(videos)} video files in one folder',
            'candidates': [str(v) for v in videos],
        })
        return
    if not videos:
        result['unparsed'].append({'path': str(folder), 'reason': 'no recognized video file in folder'})
        return

    video = videos[0]
    m = _MOVIE_RE.match(folder.name)
    if not m:
        result['unparsed'].append({'path': str(folder), 'reason': "folder name doesn't match 'Title (Year)'"})
        return
    if video.stem != folder.name:
        result['unparsed'].append({
            'path': str(folder),
            'reason': f"video filename '{video.name}' doesn't match folder name",
        })
        return

    title, year = m.group(1), int(m.group(2))
    _record_candidate(result, video, {'type': 'movie', 'movie_title': title, 'year': year})


def _scan_tv(result):
    if not TV_DIR.is_dir():
        result['errors'].append({'path': str(TV_DIR), 'reason': 'tv root not found'})
        return
    try:
        show_entries = sorted(TV_DIR.iterdir())
    except OSError as e:
        result['errors'].append({'path': str(TV_DIR), 'reason': str(e)})
        return

    for show_dir in show_entries:
        try:
            if show_dir.is_file():
                result['unparsed'].append({
                    'path': str(show_dir),
                    'reason': 'stray file directly in tv root, not inside a show folder',
                })
            elif show_dir.is_dir():
                _scan_show(show_dir, result)
        except OSError as e:
            result['errors'].append({'path': str(show_dir), 'reason': str(e)})


def _scan_show(show_dir, result):
    show = show_dir.name
    try:
        entries = sorted(show_dir.iterdir())
    except OSError as e:
        result['errors'].append({'path': str(show_dir), 'reason': str(e)})
        return

    if not entries:
        result['known_skips'].append({'path': str(show_dir), 'reason': 'empty show directory'})
        return

    for entry in entries:
        if entry.is_file():
            result['unparsed'].append({
                'path': str(entry),
                'reason': f"stray file directly in '{show}' folder, not inside a Season folder",
            })
            continue
        if not entry.is_dir():
            continue
        m = _SEASON_RE.match(entry.name)
        if not m:
            result['unparsed'].append({'path': str(entry), 'reason': "folder name doesn't match 'Season NN'"})
            continue
        _scan_season(show, int(m.group(1)), entry, result)


def _scan_season(show, season, season_dir, result):
    try:
        files = sorted(f for f in season_dir.iterdir() if f.is_file())
    except OSError as e:
        result['errors'].append({'path': str(season_dir), 'reason': str(e)})
        return

    if not files:
        result['known_skips'].append({'path': str(season_dir), 'reason': 'empty season folder'})
        return

    ep_re = re.compile(rf'^{re.escape(show)} - S(\d+)E(\d+)\.mkv$')
    for f in files:
        if f.suffix.lower() != '.mkv':
            result['unparsed'].append({'path': str(f), 'reason': 'not a .mkv file'})
            continue
        m = ep_re.match(f.name)
        if not m:
            result['unparsed'].append({
                'path': str(f),
                'reason': f"filename doesn't match '{show} - S##E##.mkv'",
            })
            continue
        ep_season, episode = int(m.group(1)), int(m.group(2))
        if ep_season != season:
            result['unparsed'].append({
                'path': str(f),
                'reason': f'season in filename (S{ep_season:02d}) does not match folder (Season {season:02d})',
            })
            continue
        _record_candidate(result, f, {'type': 'tv_episode', 'show': show, 'season': season, 'episode': episode})


# ── Duplicate visibility (Phase 2B) ──────────────────────────────────────────
# Read-only, advisory-only duplicate detection. Never inserts, updates, or
# deletes a catalog row or job record, never touches the filesystem, never
# blocks a caller. Deterministic only -- no fuzzy/similarity matching, no AI,
# no external APIs. Reuses the same classification dict shape already used
# everywhere else in this app (titles.classification / catalog.classification)
# -- there is no second title parser here, only a comparison over that same
# structured data.

_SKIP_TYPES = ('play_all', 'deleted', 'unknown')
_DUP_CATALOG_STATES = ('verified', 'pending', 'committed', 'failed')
_ACTIVE_JOB_STATUSES = ('ingesting', 'review', 'approved', 'queued', 'encoding')

_CONFIDENCE_RANK = {'HIGH': 0, 'MEDIUM': 1, 'LOW': 2}
_STATE_RELEVANCE = {
    'verified': 0, 'committed': 1, 'pending': 2,
    'staged_job': 3, 'active_job': 4, 'uncataloged_completed_job': 5,
    'failed': 6,
}


def _normalize_title(s):
    """Deterministic string canonicalization for comparison -- NOT a parser
    and NOT fuzzy matching. Lowercases, folds common filename separators
    (./_) to spaces, strips punctuation, collapses whitespace."""
    if not s:
        return ''
    s = str(s).lower()
    s = re.sub(r'[._]+', ' ', s)
    s = re.sub(r'[^a-z0-9 ]+', '', s)
    return re.sub(r'\s+', ' ', s).strip()


def _movie_match(cls, other):
    """Movie/documentary/special matching. Returns (confidence, reasons) or
    None. Type must match exactly; specials additionally require the show
    field to match (two different shows' "Special Features 1" must not
    collide). No similarity/edit-distance tier -- exact normalized-title
    equality is required for any confidence level."""
    ctype = cls.get('type')
    if not ctype or other.get('type') != ctype:
        return None

    title1 = _normalize_title(cls.get('movie_title') or cls.get('title'))
    title2 = _normalize_title(other.get('movie_title') or other.get('title'))
    if not title1 or title1 != title2:
        return None

    if ctype == 'special':
        show1 = _normalize_title(cls.get('show'))
        show2 = _normalize_title(other.get('show'))
        if show1 != show2:
            return None

    reasons = ['Same normalized title', 'Same media type']
    year1, year2 = cls.get('year'), other.get('year')
    if year1 and year2:
        if year1 == year2:
            reasons.append('Same year')
            return 'HIGH', reasons
        reasons.append(f'Year differs ({year1} vs {year2})')
        reasons.append('Possible remake or alternate edition')
        return 'LOW', reasons

    reasons.append('Year missing on one side — cannot confirm or rule out')
    return 'MEDIUM', reasons


def _tv_match(cls, other):
    """TV episode matching. Returns (confidence, reasons) or None. Same show
    with differing season or episode (both sides present) is a different
    item, never a match. Same show alone, with neither season nor episode
    confirmed equal, is too weak to be a candidate at all."""
    if cls.get('type') != 'tv_episode' or other.get('type') != 'tv_episode':
        return None

    show1 = _normalize_title(cls.get('show'))
    show2 = _normalize_title(other.get('show'))
    if not show1 or show1 != show2:
        return None

    season1, season2 = cls.get('season'), other.get('season')
    ep1, ep2 = cls.get('episode'), other.get('episode')

    if season1 and season2 and season1 != season2:
        return None
    if ep1 and ep2 and ep1 != ep2:
        return None

    reasons = ['Same normalized series title', 'Same media type']
    have_season = bool(season1 and season2)
    have_ep = bool(ep1 and ep2)

    if have_season and have_ep:
        reasons.append('Same season and episode')
        et1, et2 = cls.get('episode_title'), other.get('episode_title')
        if et1 and et2 and _normalize_title(et1) == _normalize_title(et2):
            reasons.append('Same episode title')
        return 'HIGH', reasons

    if have_season or have_ep:
        reasons.append('Same season/episode identity, but episode metadata is incomplete on one side')
        return 'MEDIUM', reasons

    return None  # same series alone is never enough


def find_possible_duplicates(cls, exclude_job_id=None, exclude_title_num=None):
    """Deterministic, read-only advisory duplicate check for a single
    classification dict (the same shape used everywhere else in this app).
    Compares against catalog rows (both sources, states verified/pending/
    committed/failed) and job history (active/staged/uncataloged-complete
    jobs), excluding only the exact (job_id, title_num) being checked.
    Never writes to the database, never touches the filesystem, never
    blocks the caller. Returns a sorted list of candidate dicts, or [] if
    none -- callers should treat [] as "no panel", not an error.
    """
    ctype = cls.get('type')
    if not ctype or ctype in _SKIP_TYPES:
        return []

    matcher = _tv_match if ctype == 'tv_episode' else _movie_match
    candidates = []

    # 1. Catalog rows -- both sources, four informational states.
    all_rows = db.get_all_catalog_rows()
    by_path = {row['library_path']: row for row in all_rows}

    for row in all_rows:
        if row['state'] not in _DUP_CATALOG_STATES:
            continue
        if row['job_id'] == exclude_job_id and row['title_num'] == exclude_title_num:
            continue

        other = {
            'type': row['type'], 'movie_title': row['movie_title'], 'year': row['year'],
            'show': row['show'], 'season': row['season'], 'episode': row['episode'],
            'episode_title': row['episode_title'],
        }
        m = matcher(cls, other)
        if not m:
            continue
        confidence, reasons = m[0], list(m[1])

        if row['state'] == 'verified':
            reasons.append('Existing verified catalog entry')
        elif row['state'] == 'committed':
            reasons.append('Existing committed catalog entry (not yet verified)')
        elif row['state'] == 'pending':
            reasons.append('Existing catalog entry not yet verified')
        elif row['state'] == 'failed':
            reasons.append('Existing catalog entry FAILED verification — file may be missing or unreadable')

        candidates.append({
            'confidence': confidence, 'reasons': reasons,
            'label': row['label'], 'type': row['type'],
            'movie_title': row['movie_title'], 'year': row['year'],
            'show': row['show'], 'season': row['season'], 'episode': row['episode'],
            'episode_title': row['episode_title'],
            'source': row['source'], 'state': row['state'],
            'library_path': row['library_path'],
            'job_id': row['job_id'], 'catalog_id': row['id'],
            'verified': row['state'] == 'verified',
            'has_catalog_row': True,
        })

    # 2. Job history -- active / staged / uncataloged-complete.
    from app import _route_library, _cls_label

    for job in db.get_all_jobs():
        jid, status = job['id'], job['status']
        if status == 'error':
            continue
        if status in _ACTIVE_JOB_STATUSES:
            job_source = 'active_job'
        elif status == 'staged':
            job_source = 'staged_job'
        elif status == 'complete':
            job_source = 'uncataloged_completed_job'
        else:
            continue

        for t in db.get_titles(jid):
            if jid == exclude_job_id and t['title_num'] == exclude_title_num:
                continue
            other = t.get('classification')
            if not other or other.get('type') in _SKIP_TYPES:
                continue

            if job_source == 'uncataloged_completed_job':
                # Only titles with no catalog row of their own belong here --
                # ones that do are already covered by the catalog-row pass
                # above (as a mediaintel_job / library_import candidate).
                if db.get_catalog_row(jid, t['title_num']):
                    continue

            m = matcher(cls, other)
            if not m:
                continue
            confidence, reasons = m[0], list(m[1])

            if job_source == 'active_job':
                reasons.append(f'Another copy is in progress — job status: {status}')
            elif job_source == 'staged_job':
                reasons.append('Encoded and staged, not yet committed to the library')
            else:
                reasons.append('Job completed but has no catalog record of its own')
                expected_path = _route_library(other, t['title_num'])
                if expected_path and str(expected_path) in by_path:
                    holder = by_path[str(expected_path)]
                    reasons.append(
                        f"Expected library path conflicts with existing catalog row "
                        f"#{holder['id']} ({holder['label']})"
                    )

            candidates.append({
                'confidence': confidence, 'reasons': reasons,
                'label': _cls_label(other, t['title_num']), 'type': other.get('type'),
                'movie_title': other.get('movie_title'), 'year': other.get('year'),
                'show': other.get('show'), 'season': other.get('season'),
                'episode': other.get('episode'), 'episode_title': other.get('episode_title'),
                'source': job_source, 'state': status,
                'library_path': None,
                'job_id': jid, 'catalog_id': None,
                'verified': False,
                'has_catalog_row': False,
            })

    def _sort_key(c):
        relevance_key = c['state'] if c['source'] in ('mediaintel_job', 'library_import') else c['source']
        return (
            _CONFIDENCE_RANK.get(c['confidence'], 9),
            _STATE_RELEVANCE.get(relevance_key, 9),
            (c['label'] or '').lower(),
            c['year'] or 0,
        )

    candidates.sort(key=_sort_key)
    return candidates
