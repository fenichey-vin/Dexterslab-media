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
    """Read-only check of one file: exists, non-empty, ffprobe-readable.
    Returns (state, fail_reason) where fail_reason is None when verified.
    Never opens the file for writing, never deletes, never moves anything.
    """
    path = Path(library_path)

    if not path.exists():
        return 'failed', 'missing'
    if path.stat().st_size == 0:
        return 'failed', 'zero_bytes'
    duration = get_duration(path)
    if not duration or duration <= 0:
        return 'failed', 'unreadable'
    return 'verified', None


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
