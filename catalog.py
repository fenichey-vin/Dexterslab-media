"""Permanent title catalog — backfill and verification (Phase 1).

Both functions here are strictly read-only with respect to the filesystem:
backfill only recomputes expected library paths from data already in the
`titles` table and inserts SQLite rows; verify only stats/ffprobes files
that already exist. Neither one ever writes, moves, or deletes a media file.
Deletion is Phase 4's job, in a separate, explicitly-named function — not
implemented here. See mediaintel-catalog-plan.md.
"""

import json
from pathlib import Path
from datetime import datetime, timezone

import db
from disc import get_duration


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
        path = Path(row['library_path'])

        if not path.exists():
            db.update_catalog_state(row['id'], 'failed', fail_reason='missing')
            summary['missing'] += 1
            continue

        if path.stat().st_size == 0:
            db.update_catalog_state(row['id'], 'failed', fail_reason='zero_bytes')
            summary['failed'] += 1
            continue

        duration = get_duration(path)
        if not duration or duration <= 0:
            db.update_catalog_state(row['id'], 'failed', fail_reason='unreadable')
            summary['failed'] += 1
            continue

        db.update_catalog_state(row['id'], 'verified', verified_at=_now())
        summary['verified'] += 1

    return summary
