#!/usr/bin/env python3
"""Media Intel Dashboard — web UI for disc ingestion review and classification."""

import os
import sys
import json
import html as html_module
import base64
import re
import shutil
import subprocess
import threading
import queue as _queue
import time
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from flask import (Flask, render_template, request, jsonify,
                   send_from_directory, Response, redirect, url_for)
from config import (REVIEW_DIR, DB_PATH, PORT, ANTHROPIC_API_KEY, ANTHROPIC_MODEL,
                    TV_DIR, MOVIES_DIR, SPECIALS_DIR, STAGING_DIR,
                    HB_PRESET, HB_FILTERS, HB_AUDIO)
import db
import catalog
import tmdb
from disc import (detect_drives, scan_disc, flag_titles, rip_title, get_duration,
                   take_screenshots, check_makemkv_version, _friendly_drive_label)

app = Flask(__name__, static_url_path='/media/static')
app.secret_key = 'mediaintel-change-in-production'
app.config['MAX_CONTENT_LENGTH'] = None  # MKVs can be many GB

# ── Encode queue (one job at a time) ─────────────────────────────────────────
_encode_queue = _queue.Queue()

def _encode_worker():
    while True:
        job_id = _encode_queue.get()
        try:
            _encode_job_bg(job_id)
        except Exception:
            pass
        finally:
            _encode_queue.task_done()

threading.Thread(target=_encode_worker, daemon=True).start()


def _startup_recovery():
    """Recover jobs stuck in 'encoding' from a previous service run.

    With KillMode=process in systemd, HandBrakeCLI survives a Flask restart.
    On boot: if the process is still alive, wait for it then re-queue.
    If it's dead, delete any partial staging files and re-queue immediately.
    """
    all_jobs = db.get_all_jobs()
    stuck = [j for j in all_jobs if j['status'] == 'encoding']
    if not stuck:
        return

    # Collect paths that are legitimately complete (referenced in titles.staged_path)
    legitimate_staged = set()
    for j in all_jobs:
        for t in db.get_titles(j['id']):
            if t.get('staged_path'):
                legitimate_staged.add(t['staged_path'])

    # Identify live orphaned HandBrake processes and the files they're writing
    active_output_files = set()
    live_pid_by_job = {}
    for job in stuck:
        progress = job.get('encode_progress')
        pid = progress.get('pid') if isinstance(progress, dict) else None
        if not pid:
            continue
        try:
            os.kill(pid, 0)
            cmdline = Path(f'/proc/{pid}/cmdline').read_bytes()
            if b'HandBrakeCLI' not in cmdline:
                continue
            parts = cmdline.split(b'\x00')
            for i, part in enumerate(parts):
                if part == b'-o' and i + 1 < len(parts):
                    active_output_files.add(parts[i + 1].decode('utf-8', errors='replace'))
            live_pid_by_job[job['id']] = pid
        except Exception:
            pass

    # Delete partial staging files — not legitimately complete, not actively written
    for p in STAGING_DIR.glob('*.mkv'):
        if str(p) not in legitimate_staged and str(p) not in active_output_files:
            try:
                p.unlink()
                print(f'[recovery] removed partial staging file: {p.name}')
            except Exception:
                pass

    for job in stuck:
        job_id = job['id']
        if job_id in live_pid_by_job:
            pid = live_pid_by_job[job_id]
            print(f'[recovery] job {job_id}: HandBrake PID {pid} still running — monitoring')

            def _watch(jid=job_id, p=pid):
                while True:
                    try:
                        os.kill(p, 0)
                        time.sleep(10)
                    except (ProcessLookupError, PermissionError):
                        break
                print(f'[recovery] PID {p} finished — re-queuing {jid}')
                db.set_encode_progress(jid, None)
                db.set_job_status(jid, 'approved')
                _encode_queue.put(jid)

            threading.Thread(target=_watch, daemon=True).start()
        else:
            print(f'[recovery] job {job_id}: no live HandBrake — re-queuing')
            db.set_encode_progress(job_id, None)
            db.set_job_status(job_id, 'approved')
            _encode_queue.put(job_id)


# ── Template filters ─────────────────────────────────────────────────────────

@app.template_filter('json_attr')
def json_attr(v):
    """JSON-encode and HTML-escape for safe embedding in double-quoted attributes.

    Flask's tojson marks output as Markup (already-safe), so piping through | e
    is a no-op and leaves raw " characters that break HTML attribute parsing.
    We escape manually and wrap in Markup to prevent Jinja auto-escaping from
    double-encoding the &quot; entities.
    """
    from markupsafe import Markup
    return Markup(html_module.escape(json.dumps(v, ensure_ascii=False)))


@app.template_filter('duration')
def fmt_duration(secs):
    if not secs:
        return '?:??:??'
    secs = int(secs)
    h, rem = divmod(secs, 3600)
    m, s   = divmod(rem, 60)
    return f'{h}:{m:02d}:{s:02d}'


@app.template_filter('filesize')
def fmt_size(b):
    if not b:
        return '?'
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if b < 1024:
            return f'{b:.1f} {unit}'
        b /= 1024
    return f'{b:.1f} PB'


@app.template_filter('eta_format')
def fmt_eta(secs):
    if not secs:
        return ''
    secs = int(secs)
    h, rem = divmod(secs, 3600)
    m = rem // 60
    if h:
        return f'{h}h {m:02d}m'
    return f'{m}m'


def _iso_diff_secs(start_iso, end_iso):
    """Seconds between two ISO timestamps, or None if either is missing/
    unparseable. Never fabricates a value -- Phase 3 provenance helper."""
    if not start_iso or not end_iso:
        return None
    try:
        start = datetime.fromisoformat(start_iso)
        end   = datetime.fromisoformat(end_iso)
        return max(0, int((end - start).total_seconds()))
    except Exception:
        return None


@app.template_filter('reltime')
def fmt_reltime(iso):
    try:
        dt = datetime.fromisoformat(iso)
        delta = datetime.now(timezone.utc) - dt
        s = int(delta.total_seconds())
        if s < 60:   return 'just now'
        if s < 3600: return f'{s//60}m ago'
        if s < 86400: return f'{s//3600}h ago'
        return f'{s//86400}d ago'
    except Exception:
        return iso or ''


# ── Dashboard ─────────────────────────────────────────────────────────────────

@app.route('/media')
@app.route('/media/')
def dashboard():
    all_jobs  = db.get_all_jobs()
    active    = [j for j in all_jobs if j['status'] == 'ingesting']
    for j in active:
        j['friendly_drive'] = _friendly_drive_label(j['drive']) if j.get('drive') else None
    review    = [j for j in all_jobs if j['status'] == 'review']
    approved  = [j for j in all_jobs if j['status'] == 'approved']
    enc_queue = [j for j in all_jobs if j['status'] == 'queued']
    encoding  = [j for j in all_jobs if j['status'] == 'encoding']
    staged    = [j for j in all_jobs if j['status'] == 'staged']
    completed = [j for j in all_jobs if j['status'] == 'complete']
    failed    = [j for j in all_jobs if j['status'] == 'error']
    total_titles = sum(j['title_count'] for j in all_jobs)
    return render_template('dashboard.html',
                           active=active, review=review,
                           approved=approved, enc_queue=enc_queue,
                           encoding=encoding, staged=staged,
                           completed=completed, failed=failed,
                           total_titles=total_titles,
                           now=time.time())


# ── Job detail ────────────────────────────────────────────────────────────────

@app.route('/media/job/<job_id>')
def job_detail(job_id):
    job = db.get_job(job_id)
    if not job:
        return 'Job not found', 404
    titles = db.get_titles(job_id)
    classified = sum(1 for t in titles if t['classification'] is not None)
    # Advisory duplicate visibility (Phase 2B) -- read-only, computed fresh on
    # every load so returning to a job later still shows current candidates.
    for t in titles:
        cls = t.get('classification')
        if cls:
            t['duplicates'] = catalog.find_possible_duplicates(
                cls, exclude_job_id=job_id, exclude_title_num=t['title_num'])
    # Per-title provenance milestones (Phase 3) -- derived entirely from
    # existing titles/catalog data, nothing new stored per title.
    for t in titles:
        cls = t.get('classification')
        cat_row = db.get_catalog_row(job_id, t['title_num'])
        t['catalog_row'] = cat_row
        t['is_skip_type'] = bool(cls) and cls.get('type') in ('play_all', 'deleted', 'unknown')
        t['m_classified'] = cls is not None
        t['m_committed']  = cat_row is not None
        t['m_verified']   = cat_row is not None and cat_row['state'] == 'verified'
        t['m_failed']     = cat_row is not None and cat_row['state'] == 'failed'
        t['m_encoded']    = cat_row is not None or bool(t.get('staged_path'))

    summary = _build_job_summary(job, titles)

    # Nudge: TV job, fully classified, ready for Approve, but no title has
    # ever been through transcript verification (identifytv). Screenshot/
    # TMDb-position classification alone has produced wrong or fabricated
    # episode labels before -- surface a reminder rather than relying on the
    # operator to remember to click the button. Purely advisory: never
    # blocks Approve, and disappears permanently once verify-transcript has
    # touched at least one title (checked via classification _source).
    ctx = job.get('disc_context') or _get_effective_context(job)
    is_tv = bool(ctx and ctx.get('type') == 'tv')
    show_verify_nudge = (
        is_tv and job['status'] == 'review'
        and len(titles) > 0 and classified == len(titles)
        and not any((t['classification'] or {}).get('_source') in
                    ('identifytv_transcript', 'whisper_transcript_verified')
                    for t in titles)
    )

    return render_template('job.html', job=job, titles=titles,
                           classified_count=classified, total_count=len(titles),
                           summary=summary, show_verify_nudge=show_verify_nudge)


def _build_job_summary(job, titles):
    """Job Summary + Timeline data (Phase 3) -- read-only, computed fresh on
    every load from existing jobs/titles/catalog fields plus the 3 new
    provenance timestamps. Never writes anything, never fabricates a
    timestamp that isn't actually stored.
    """
    committed = [t for t in titles if t['catalog_row']]
    failed    = [t for t in titles if t['catalog_row'] and t['catalog_row']['state'] == 'failed']
    skipped   = [t for t in titles if t['is_skip_type']]
    not_committed = [
        t for t in titles
        if t['classification'] and not t['is_skip_type'] and not t['catalog_row']
    ]
    dup_flagged = [t for t in titles if t.get('duplicates')]

    all_committed_verified = bool(committed) and all(
        t['catalog_row']['state'] == 'verified' for t in committed)

    rip_secs      = _iso_diff_secs(job.get('created_at'), job.get('rip_completed_at'))
    encode_secs   = _iso_diff_secs(job.get('encode_started_at'), job.get('encode_completed_at'))
    # Job Lifetime: overall elapsed wall-clock span from job creation to the
    # last recorded activity -- NOT a processing duration. For older/idle
    # jobs this includes time spent waiting in review/staging, not just
    # active work; that's why it's named "lifetime," not "processing time."
    lifetime_secs = _iso_diff_secs(job.get('created_at'), job.get('updated_at')) if job['status'] == 'complete' else None

    latest_classified_at = max(
        (t['classified_at'] for t in titles if t.get('classified_at')), default=None)
    latest_verified_at = max(
        (t['catalog_row']['verified_at'] for t in titles
         if t['catalog_row'] and t['catalog_row'].get('verified_at')), default=None)
    latest_committed_at = max(
        (t['catalog_row']['committed_at'] for t in titles if t['catalog_row']), default=None)

    final_files = [
        {'label': t['catalog_row']['label'], 'library_path': t['catalog_row']['library_path'],
         'catalog_id': t['catalog_row']['id']}
        for t in committed
    ]

    return {
        'processed': len(titles), 'committed': len(committed), 'failed': len(failed),
        'skipped': len(skipped), 'not_committed': len(not_committed),
        'dup_flagged_count': len(dup_flagged),
        'all_committed_verified': all_committed_verified,
        'rip_secs': rip_secs, 'encode_secs': encode_secs, 'lifetime_secs': lifetime_secs,
        'latest_classified_at': latest_classified_at,
        'latest_verified_at': latest_verified_at,
        'latest_committed_at': latest_committed_at,
        'final_files': final_files,
    }


# ── Classification ────────────────────────────────────────────────────────────

@app.route('/media/job/<job_id>/title/<int:n>/classify', methods=['POST'])
def classify_title(job_id, n):
    data = request.get_json(silent=True)
    if not data or 'type' not in data:
        return jsonify({'error': 'Missing type field'}), 400
    # Advisory duplicate check runs against the posted metadata *before* the
    # classification is committed -- read-only, never blocks the save below.
    duplicates = catalog.find_possible_duplicates(data, exclude_job_id=job_id, exclude_title_num=n)
    db.save_classification(job_id, n, data)
    # Promote job from ingesting → review once first classification arrives
    job = db.get_job(job_id)
    if job and job['status'] == 'ingesting':
        db.set_job_status(job_id, 'review')
    return jsonify({'ok': True, 'duplicates': duplicates})


@app.route('/media/job/<job_id>/title/<int:n>/unclassify', methods=['POST'])
def unclassify_title(job_id, n):
    db.save_classification(job_id, n, None)
    return jsonify({'ok': True})


# ── Job approval (writes classification.json) ─────────────────────────────────

@app.route('/media/job/<job_id>/approve', methods=['POST'])
def approve_job(job_id):
    job = db.get_job(job_id)
    if not job:
        return jsonify({'error': 'Not found'}), 404
    classification = db.get_classification_export(job_id)
    out_path = Path(job['review_path']) / 'classification.json'
    try:
        out_path.write_text(json.dumps(classification, indent=2))
    except OSError as e:
        return jsonify({'error': str(e)}), 500
    db.set_job_status(job_id, 'approved')

    # Optional: immediately queue encoding when auto_encode=True
    auto_encode = (request.get_json(silent=True) or {}).get('auto_encode', False)
    if auto_encode:
        db.set_job_status(job_id, 'queued')
        _encode_queue.put(job_id)

    return jsonify({'ok': True, 'path': str(out_path)})


# ── Disc context (save to existing job) ──────────────────────────────────────

@app.route('/media/job/<job_id>/context', methods=['POST'])
def save_disc_context(job_id):
    data = request.get_json(silent=True) or {}
    ctx  = data.get('disc_context')
    if not ctx:
        return jsonify({'error': 'No disc_context provided'}), 400
    db.set_disc_context(job_id, ctx)
    return jsonify({'ok': True})


@app.route('/media/job/<job_id>/classify-all', methods=['POST'])
def classify_all(job_id):
    """Classify every unclassified title in the job. TV titles are resolved
    deterministically (disc position -> episode number, TMDb -> episode
    title) -- no screenshots, no Claude. Movie/special/documentary titles
    still use Claude auto-classification from screenshots, unchanged.
    """
    job = db.get_job(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    disc_context = job.get('disc_context')
    is_tv = bool(disc_context and disc_context.get('type') == 'tv')
    if not is_tv and not ANTHROPIC_API_KEY:
        return jsonify({'error': 'ANTHROPIC_API_KEY not configured'}), 503

    titles = db.get_titles(job_id)
    to_classify = [t for t in titles if t['classification'] is None]
    if not to_classify:
        return jsonify({'ok': True, 'queued': 0})

    def _bg():
        for t in to_classify:
            try:
                if t['duration_secs'] and t['duration_secs'] < 480:
                    _auto_classify_short(job_id, t['title_num'], disc_context)
                elif is_tv:
                    _auto_classify_tv_from_lookup(job_id, t['title_num'], disc_context, titles)
                else:
                    _auto_classify_one(job_id, t['title_num'], disc_context)
            except Exception:
                pass

    threading.Thread(target=_bg, daemon=True).start()
    return jsonify({'ok': True, 'queued': len(to_classify)})


@app.route('/media/job/<job_id>/verify-transcript', methods=['POST'])
def verify_transcript(job_id):
    """Identify every title by what's actually SAID in it (local whisper +
    full-script n-gram match against the real episode list), independent of
    disc position or screenshots. This is the strongest available signal --
    stronger than TMDb lookup-by-position, which still trusts disc order.

    Runs bin/identifytv as a background subprocess with --apply, which only
    writes a suggestion when confidence is high/medium and the runtime looks
    like a real episode (see identifytv's confidence_script() thresholds).
    Anything it can't confidently identify is left for manual review rather
    than guessed. Safe to run on a job mid-rip or already classified --
    identifytv only reads MKVs, never touches the drive.
    """
    job = db.get_job(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    ctx = job.get('disc_context') or _get_effective_context(job)
    if not ctx or ctx.get('type') != 'tv' or not ctx.get('show'):
        return jsonify({'error': 'Set Disc Context (Show + Season) first — '
                                  'transcript matching needs to know what show '
                                  'to check dialogue against.'}), 400

    db.set_job_status(job_id, job['status'], notes='Verifying via transcript…')

    def _bg():
        try:
            cmd = ['/home/vinnie/mediaintel/bin/identifytv', job_id,
                   '--show', ctx['show'], '--apply']
            if ctx.get('season'):
                cmd += ['--season', str(ctx['season'])]
            subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        except Exception as e:
            db.set_job_status(job_id, job['status'], notes=f'Transcript verify failed: {e}')
            return
        report = Path(job['review_path']) / 'identification.json'
        note = 'Transcript verification complete — check title cards for any left Unclassified.'
        if report.exists():
            try:
                data = json.loads(report.read_text())
                low = [r['title_num'] for r in data.get('results', [])
                       if r.get('confidence') not in ('high', 'medium')]
                # Also check what identifytv actually wrote to the DB for a
                # mismatch flag (set when the transcript match disagreed with
                # whatever "Look Up All Episodes" or a manual entry had) --
                # this is the real answer to "does the disc match my input".
                mismatched = [t['title_num'] for t in db.get_titles(job_id)
                             if (t.get('classification') or {}).get('mismatch_with_prior')]
                parts = []
                if mismatched:
                    parts.append(f'⚠ {len(mismatched)} title(s) DISAGREE with what you entered '
                                 f'(title_{", title_".join(map(str, mismatched))}) — check those cards')
                if low:
                    parts.append(f'{len(low)} title(s) could not be confidently matched '
                                 f'(title_{", title_".join(map(str, low))})')
                if parts:
                    note = 'Transcript verification complete — ' + '; '.join(parts) + '.'
                else:
                    note = 'Transcript verification complete — every title agrees with what you entered.'
            except Exception:
                pass
        db.set_job_status(job_id, job['status'], notes=note)

    threading.Thread(target=_bg, daemon=True).start()
    return jsonify({'ok': True})


# ── JSON export ───────────────────────────────────────────────────────────────

@app.route('/media/job/<job_id>/export.json')
def export_json(job_id):
    classification = db.get_classification_export(job_id)
    return Response(
        json.dumps(classification, indent=2),
        mimetype='application/json',
        headers={'Content-Disposition': 'attachment; filename=classification.json'}
    )


# ── Job deletion ──────────────────────────────────────────────────────────────

@app.route('/media/job/<job_id>/delete', methods=['POST'])
def delete_job(job_id):
    job = db.get_job(job_id)
    if job:
        titles = db.get_titles(job_id)
        review_path = Path(job['review_path'])
        db.delete_job(job_id)
        if not titles and review_path.exists():
            try:
                shutil.rmtree(str(review_path))
            except Exception:
                pass
    else:
        db.delete_job(job_id)
    return jsonify({'ok': True})


# ── Raw review-footage cleanup ───────────────────────────────────────────────
#
# A completed job's raw rip (in review/) is a separate, earlier-stage copy
# from the encoded file actually committed to the library -- commit only
# ever moves the encoded copy out of staging/, nothing has ever cleaned up
# the raw source once a job is done with it. This is that missing cleanup
# step, gated on independent verification (never on commit alone) so it can
# never delete the only good copy of something.

def _job_raw_cleanup_eligible(job_id):
    rows = db.get_catalog_rows_for_job(job_id)
    return bool(rows) and all(r['state'] == 'verified' for r in rows)


def _cleanup_job_raw(job_id, job=None):
    """Delete one job's raw review-directory rip, but only once every one of
    its catalog rows is independently verified against disk (not just
    committed). Marks those rows 'cleaned' so this is never silently
    re-attempted and never confused with 'still has a backing raw file'.
    Returns bytes freed, or None if not eligible / nothing to clean.
    """
    job = job or db.get_job(job_id)
    if not job or job['status'] != 'complete':
        return None
    if not _job_raw_cleanup_eligible(job_id):
        return None
    review_path = Path(job['review_path'])
    if not review_path.exists():
        return None

    freed = sum(f.stat().st_size for f in review_path.rglob('*') if f.is_file())
    shutil.rmtree(str(review_path))

    now = datetime.now(timezone.utc).isoformat()
    for row in db.get_catalog_rows_for_job(job_id):
        db.update_catalog_state(row['id'], 'cleaned', cleaned_at=now)
    return freed


@app.route('/media/job/<job_id>/cleanup-raw', methods=['POST'])
def cleanup_job_raw(job_id):
    job = db.get_job(job_id)
    if not job:
        return jsonify({'error': 'Not found'}), 404
    if job['status'] != 'complete':
        return jsonify({'error': f'Job is not complete (currently: {job["status"]})'}), 400
    if not _job_raw_cleanup_eligible(job_id):
        return jsonify({'error': 'Not every title for this job is independently verified yet'}), 409
    freed = _cleanup_job_raw(job_id, job)
    if freed is None:
        return jsonify({'error': 'Nothing to clean up (already cleaned, or raw path missing)'}), 404
    return jsonify({'ok': True, 'freed_bytes': freed})


@app.route('/media/cleanup-all-verified', methods=['POST'])
def cleanup_all_verified():
    """Bulk version of the above -- sweeps every 'complete' job and cleans
    up any whose titles are all independently verified. Safe to run
    repeatedly: jobs that aren't eligible yet (or already cleaned) are
    silently skipped, not treated as errors.
    """
    complete = [j for j in db.get_all_jobs() if j['status'] == 'complete']
    cleaned_ids = []
    total_freed = 0
    for job in complete:
        freed = _cleanup_job_raw(job['id'], job)
        if freed is not None:
            cleaned_ids.append(job['id'])
            total_freed += freed
    return jsonify({'ok': True, 'cleaned': len(cleaned_ids), 'freed_bytes': total_freed, 'job_ids': cleaned_ids})


# ── Encode ───────────────────────────────────────────────────────────────────

@app.route('/media/encode-all', methods=['POST'])
def encode_all():
    all_jobs = db.get_all_jobs()
    approved = [j for j in all_jobs if j['status'] == 'approved']
    for job in approved:
        db.set_job_status(job['id'], 'queued')
        _encode_queue.put(job['id'])
    return jsonify({'ok': True, 'queued': len(approved)})


@app.route('/media/job/<job_id>/encode', methods=['POST'])
def start_encode(job_id):
    job = db.get_job(job_id)
    if not job:
        return jsonify({'error': 'Not found'}), 404
    if job['status'] in ('encoding', 'queued'):
        return jsonify({'error': 'Already encoding or queued'}), 400
    if job['status'] != 'approved':
        return jsonify({'error': f'Job must be approved first (currently: {job["status"]})'}), 400
    db.set_job_status(job_id, 'queued')
    _encode_queue.put(job_id)
    return jsonify({'ok': True})


@app.route('/media/job/<job_id>/retry-encode', methods=['POST'])
def retry_encode(job_id):
    """Re-queue a failed job's encode. The rip itself already succeeded --
    only the encode write failed (most commonly seen: the work drive ran out
    of space) -- so this just re-runs the existing encode pipeline against
    the raw source still sitting in review/, no re-rip needed.
    """
    job = db.get_job(job_id)
    if not job:
        return jsonify({'error': 'Not found'}), 404
    if job['status'] != 'error':
        return jsonify({'error': f'Job is not in error status (currently: {job["status"]})'}), 400
    db.set_job_status(job_id, 'queued', notes='')
    _encode_queue.put(job_id)
    return jsonify({'ok': True})


# ── Commit staged titles to Jellyfin library ─────────────────────────────────

@app.route('/media/job/<job_id>/title/<int:n>/commit', methods=['POST'])
def commit_title(job_id, n):
    title = db.get_title(job_id, n)
    if not title:
        return jsonify({'error': 'Title not found'}), 404

    staged_path = title.get('staged_path')
    if not staged_path:
        return jsonify({'error': 'Title has no staged file'}), 400

    staged = Path(staged_path)
    if not staged.exists():
        return jsonify({'error': f'Staged file missing: {staged.name}'}), 404

    cls = title.get('classification')
    if not cls:
        return jsonify({'error': 'No classification — set one before committing'}), 400

    dest = _route_library(cls, n)
    if dest is None:
        return jsonify({'error': 'This type is skipped (play_all/deleted/unknown)'}), 400

    if dest.exists():
        return jsonify({'error': f'File already exists in library: {dest.name} — delete it first if you want to replace it'}), 409

    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.move(str(staged), str(dest))
    except OSError as e:
        return jsonify({'error': str(e)}), 500

    _catalog_commit(job_id, n, title, cls, dest)
    db.update_title(job_id, n, staged_path=None)
    _maybe_complete_staged_job(job_id)
    return jsonify({'ok': True, 'dest': str(dest)})


def _commit_all_titles_for_job(job_id):
    """Commit every currently-staged title in one job. Returns (ok_list,
    errors) -- shared by the per-job commit-all route and the
    commit-all-staged-jobs route so there's one implementation of the
    actual file-move logic, not two.
    """
    ok_list = []
    errors  = []
    for t in db.get_titles(job_id):
        if not t.get('staged_path'):
            continue
        n    = t['title_num']
        path = Path(t['staged_path'])
        cls  = t.get('classification')
        if not cls or not path.exists():
            errors.append(f'{job_id}/title_{n}: staged file missing or no classification')
            continue
        dest = _route_library(cls, n)
        if dest is None:
            continue
        if dest.exists():
            errors.append(f'{job_id}/title_{n}: already in library ({dest.name}) — delete it first')
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(path), str(dest))
            _catalog_commit(job_id, n, t, cls, dest)
            db.update_title(job_id, n, staged_path=None)
            ok_list.append(str(dest))
        except OSError as e:
            errors.append(f'{job_id}/title_{n}: {e}')

    _maybe_complete_staged_job(job_id)
    return ok_list, errors


@app.route('/media/job/<job_id>/commit-all', methods=['POST'])
def commit_all(job_id):
    if not any(t.get('staged_path') for t in db.get_titles(job_id)):
        return jsonify({'error': 'No staged titles to commit'}), 400
    ok_list, errors = _commit_all_titles_for_job(job_id)
    return jsonify({'ok': True, 'committed': len(ok_list), 'errors': errors})


@app.route('/media/commit-all-staged', methods=['POST'])
def commit_all_staged():
    """Commit every staged title across every 'staged' job in one action --
    backs the dashboard's top-level "Commit All Staged" button. Reuses the
    exact same per-job commit logic as the individual per-job button.
    """
    staged_jobs = [j for j in db.get_all_jobs() if j['status'] == 'staged']
    total_committed = 0
    all_errors = []
    for job in staged_jobs:
        ok_list, errors = _commit_all_titles_for_job(job['id'])
        total_committed += len(ok_list)
        all_errors.extend(errors)
    return jsonify({'ok': True, 'committed': total_committed, 'errors': all_errors, 'jobs': len(staged_jobs)})


def _catalog_commit(job_id, n, title, cls, dest):
    """Insert a permanent catalog row for a title that was just committed to
    the library. Best-effort: a catalog failure must never break commit's
    existing behavior, so this only logs and swallows errors.
    """
    try:
        job = db.get_job(job_id)
        db.insert_catalog_row(
            job_id=job_id, title_num=n, state='committed',
            type_=cls.get('type', 'unknown'),
            show=cls.get('show'), season=cls.get('season'),
            episode=cls.get('episode'), episode_title=cls.get('episode_title'),
            movie_title=cls.get('movie_title'), year=cls.get('year'),
            label=_cls_label(cls, n), disc_label=(job or {}).get('disc_label'),
            library_path=str(dest),
            duration_secs=title.get('duration_secs'),
            filesize_bytes=title.get('filesize_bytes'),
            classification=json.dumps(cls),
            committed_at=datetime.now(timezone.utc).isoformat(),
        )
    except Exception:
        pass


def _maybe_complete_staged_job(job_id):
    titles = db.get_titles(job_id)
    if not any(t.get('staged_path') for t in titles):
        db.set_job_status(job_id, 'complete')


def _is_within(path, root):
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


# ── Discard staged files (encoded but not committed to the library) ──────────

@app.route('/media/job/<job_id>/title/<int:n>/discard', methods=['POST'])
def discard_staged_title(job_id, n):
    title = db.get_title(job_id, n)
    if not title:
        return jsonify({'error': 'Title not found'}), 404

    staged_path = title.get('staged_path')
    if not staged_path:
        return jsonify({'error': 'Title has no staged file'}), 400

    p = Path(staged_path)
    if not _is_within(p, STAGING_DIR):
        return jsonify({'error': 'Refusing to delete a file outside the staging dir'}), 400

    try:
        if p.exists():
            p.unlink()
    except OSError as e:
        return jsonify({'error': str(e)}), 500

    flags = title.get('flags') or []
    if 'discarded' not in flags:
        flags = flags + ['discarded']
    db.update_title(job_id, n, staged_path=None, flags=flags)
    _maybe_complete_staged_job(job_id)
    return jsonify({'ok': True})


@app.route('/media/job/<job_id>/discard-staged', methods=['POST'])
def discard_all_staged(job_id):
    titles = db.get_titles(job_id)
    staged = [t for t in titles if t.get('staged_path')]
    if not staged:
        return jsonify({'error': 'No staged titles to discard'}), 400

    ok_count = 0
    errors   = []
    for t in staged:
        n = t['title_num']
        p = Path(t['staged_path'])
        if not _is_within(p, STAGING_DIR):
            errors.append(f'title_{n}: refusing to delete file outside staging dir')
            continue
        try:
            if p.exists():
                p.unlink()
            flags = t.get('flags') or []
            if 'discarded' not in flags:
                flags = flags + ['discarded']
            db.update_title(job_id, n, staged_path=None, flags=flags)
            ok_count += 1
        except OSError as e:
            errors.append(f'title_{n}: {e}')

    _maybe_complete_staged_job(job_id)
    return jsonify({'ok': True, 'discarded': ok_count, 'errors': errors})


def _safe_name(name):
    return re.sub(r'[<>:"/\\|?*]', '', str(name)).strip('. ')


def _cls_label(cls, title_num):
    """Human-readable one-liner for a classification, used in search results."""
    if not cls:
        return f'title_{title_num}'
    t = cls.get('type', 'unknown')
    if t == 'tv_episode':
        label = cls.get('show') or 'Unknown Show'
        season, ep = cls.get('season'), cls.get('episode')
        if season:
            label += f' S{int(season):02d}'
        if ep:
            label += f'E{int(ep):02d}'
        if cls.get('episode_title'):
            label += f' — {cls["episode_title"]}'
        return label
    if t in ('movie', 'documentary'):
        label = cls.get('movie_title') or cls.get('title') or 'Unknown Title'
        return f'{label} ({cls["year"]})' if cls.get('year') else label
    if t == 'special':
        show  = cls.get('show') or 'Misc'
        title = cls.get('title') or f'title_{title_num}'
        return f'{show} — {title}'
    return t.replace('_', ' ').title()


# ── Search (avoid re-ripping something already in the library) ──────────────

@app.route('/media/search.json')
def search_json():
    q = (request.args.get('q') or '').strip().lower()
    if len(q) < 2:
        return jsonify({'results': []})

    results = []
    for job in db.get_all_jobs():
        for t in db.get_titles(job['id']):
            cls = t.get('classification')
            if not cls:
                continue
            label = _cls_label(cls, t['title_num'])
            haystack = ' '.join(str(v) for v in [
                job.get('name') or '', job.get('disc_label') or '',
                cls.get('show') or '', cls.get('movie_title') or '',
                cls.get('title') or '', cls.get('episode_title') or '',
                label,
            ]).lower()
            if q not in haystack:
                continue
            results.append({
                'job_id':      job['id'],
                'title_num':   t['title_num'],
                'label':       label,
                'type':        cls.get('type'),
                'job_status':  job['status'],
                'disc_label':  job.get('disc_label'),
                'discarded':   'discarded' in (t.get('flags') or []),
                'staged':      bool(t.get('staged_path')),
            })

    results.sort(key=lambda r: r['label'])
    return jsonify({'results': results[:40]})


def _route_library(cls, title_num):
    """Build the final Jellyfin library path for a classification."""
    t = cls.get('type', 'unknown')
    if t in ('play_all', 'deleted', 'unknown'):
        return None
    if t == 'tv_episode':
        show   = _safe_name(cls.get('show') or 'Unknown Show')
        season = int(cls.get('season') or 1)
        ep     = int(cls.get('episode') or 0)
        etitle = _safe_name(cls.get('episode_title') or '')
        fname  = f'{show} - S{season:02d}{"E"+str(ep).zfill(2) if ep else ""}'
        if etitle:
            fname += f' - {etitle}'
        return TV_DIR / show / f'Season {season:02d}' / (fname + '.mkv')
    if t in ('movie', 'documentary'):
        title  = _safe_name(cls.get('movie_title') or cls.get('title') or 'Unknown Title')
        year   = cls.get('year')
        folder = f'{title} ({year})' if year else title
        return MOVIES_DIR / folder / f'{folder}.mkv'
    if t == 'special':
        show  = _safe_name(cls.get('show') or 'Misc')
        title = _safe_name(cls.get('title') or f'title_{title_num}')
        return SPECIALS_DIR / show / f'{title}.mkv'
    return None


def _route_staging(cls, title_num):
    """Build a flat staging path — descriptive name, no nested folders."""
    t = cls.get('type', 'unknown')
    if t in ('play_all', 'deleted', 'unknown'):
        return None
    if t == 'tv_episode':
        show   = _safe_name(cls.get('show') or 'Unknown Show')
        season = int(cls.get('season') or 1)
        ep     = int(cls.get('episode') or 0)
        etitle = _safe_name(cls.get('episode_title') or '')
        fname  = f'{show} - S{season:02d}{"E"+str(ep).zfill(2) if ep else ""}'
        if etitle:
            fname += f' - {etitle}'
        return STAGING_DIR / (fname + '.mkv')
    if t in ('movie', 'documentary'):
        title = _safe_name(cls.get('movie_title') or cls.get('title') or 'Unknown Title')
        year  = cls.get('year')
        fname = f'{title} ({year})' if year else title
        return STAGING_DIR / (fname + '.mkv')
    if t == 'special':
        show  = _safe_name(cls.get('show') or 'Misc')
        title = _safe_name(cls.get('title') or f'title_{title_num}')
        fname = f'{show} - {title}'
        return STAGING_DIR / (fname + '.mkv')
    return None


def _unique_path(path):
    """Return path, appending _2 _3 etc. if a file already exists there."""
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    i = 2
    while True:
        candidate = path.parent / f'{stem}_{i}{suffix}'
        if not candidate.exists():
            return candidate
        i += 1


def _run_handbrake(cmd, job_id, title_name, title_idx, title_total):
    """Run HandBrakeCLI and stream progress from stderr into encode_progress."""
    pct_re = re.compile(rb'(\d+\.\d+)\s*%')
    fps_re = re.compile(rb'avg\s+([\d.]+)\s*fps')
    eta_re = re.compile(rb'ETA\s+(\d+)h(\d+)m(\d+)s')

    proc = subprocess.Popen(
        cmd, stderr=subprocess.PIPE, stdout=subprocess.DEVNULL,
        start_new_session=True,  # detach from Flask — survives service restart
    )
    # Store PID immediately so startup recovery can find this process
    db.set_encode_progress(job_id, {
        'pct': 0.0, 'fps': 0.0, 'eta_secs': 0,
        'title': title_name, 'idx': title_idx, 'total': title_total,
        'ts': time.time(), 'pid': proc.pid,
    })
    last_save = [0.0]

    def _reader():
        buf = b''
        while True:
            try:
                chunk = proc.stderr.read1(4096)
            except Exception:
                break
            if not chunk:
                break
            buf = (buf + chunk)[-2048:]
            now = time.time()
            if now - last_save[0] < 3.0:
                continue
            m_pct = pct_re.search(buf)
            if not m_pct:
                continue
            pct = float(m_pct.group(1))
            m_fps = fps_re.search(buf)
            m_eta = eta_re.search(buf)
            fps = float(m_fps.group(1)) if m_fps else 0.0
            eta = (int(m_eta.group(1)) * 3600 + int(m_eta.group(2)) * 60 + int(m_eta.group(3))) if m_eta else 0
            db.set_encode_progress(job_id, {
                'pct': round(pct, 1), 'fps': round(fps, 1), 'eta_secs': eta,
                'title': title_name, 'idx': title_idx, 'total': title_total,
                'ts': now, 'pid': proc.pid,
            })
            last_save[0] = now

    t = threading.Thread(target=_reader, daemon=True)
    t.start()
    proc.wait()
    t.join(timeout=5)
    return proc.returncode


def _encode_job_bg(job_id):
    """Encode all titles for one job into staging, one at a time with progress."""
    job = db.get_job(job_id)
    if not job:
        return

    db.set_job_status(job_id, 'encoding')
    db.set_job_timestamp(job_id, 'encode_started_at')

    job_dir  = Path(job['review_path'])
    cls_file = job_dir / 'classification.json'

    if not cls_file.exists():
        db.set_job_status(job_id, 'error', notes='classification.json missing')
        return

    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    log_path = Path('/srv/work/logs/encode_log.txt')

    def _log(msg):
        ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        with open(log_path, 'a') as lf:
            lf.write(f'[{ts}] {msg}\n')

    with open(cls_file) as f:
        classifications = json.load(f)

    # ---- DB is the source of truth for classification, not the JSON file ----
    # classification.json is written once by the auto-classifier. When a human
    # corrects a title in the dashboard, only titles.classification is updated,
    # so the file goes stale. Encoding from the stale file is what silently
    # produced wrongly-numbered episodes (Sopranos S01, Seinfeld S02).
    # Overlay the DB rows on top of the file, and rewrite the file so review
    # dir and DB agree from here on.
    _db_titles = {t['title_num']: t for t in db.get_titles(job_id)}
    _overridden = []
    for _n, _t in _db_titles.items():
        _cls = _t.get('classification')
        if not _cls:
            continue
        _key = f'title_{_n}'
        if classifications.get(_key) != _cls:
            classifications[_key] = _cls
            _overridden.append(_key)
    if _overridden:
        try:
            with open(cls_file, 'w') as f:
                json.dump(classifications, f, indent=2)
        except OSError:
            pass

    # Build list of encodable titles up front for total count
    encodable = []
    for key, cls in sorted(classifications.items()):
        m = re.match(r'^title_(\d+)$', key)
        if not m:
            continue
        if cls.get('type') in ('play_all', 'deleted', 'unknown'):
            continue
        encodable.append((int(m.group(1)), cls))

    _log(f'=== Encode start (staging): {job["name"]} ({job_id}) — {len(encodable)} titles ===')
    if _overridden:
        _log(f'INFO {job_id}: classification.json was stale; used DB values for '
             f'{", ".join(sorted(_overridden))}')

    # ---- runtime sanity gate -------------------------------------------
    # A "TV episode" that runs 112 minutes is a feature film, and a "movie"
    # that runs 4 minutes is a bonus feature. Both mistakes reached the
    # library before (TMNT films filed as cartoon episodes, Seinfeld
    # featurettes numbered as episodes). Refuse to stage anything whose
    # runtime is wildly inconsistent with its declared type.
    _titles_by_num = {t['title_num']: t for t in db.get_titles(job_id)}
    _gated = []
    for _n, _cls in list(encodable):
        _secs = (_titles_by_num.get(_n) or {}).get('duration_secs') or 0
        _mins = _secs / 60.0
        _t = _cls.get('type')
        _why = None
        if _t == 'tv_episode':
            if _mins > 75:
                _why = (f'{_mins:.0f}min is too long for a TV episode — '
                        f'likely a feature film or a play-all title')
            elif 0 < _mins < 12:
                _why = (f'{_mins:.0f}min is too short for a TV episode — '
                        f'likely a bonus feature or menu clip')
        elif _t in ('movie', 'documentary') and 0 < _mins < 40:
            _why = f'{_mins:.0f}min is too short for a feature — likely an extra'
        if _why:
            _gated.append((_n, _why))
    if _gated:
        for _n, _why in _gated:
            _log(f'BLOCKED {job_id} title_{_n}: {_why}')
        encodable = [(n, c) for n, c in encodable
                     if n not in {g[0] for g in _gated}]
        _note = '; '.join(f'title_{n}: {w}' for n, w in _gated)
        db.set_job_status(job_id, 'review',
                          notes=f'Runtime check flagged {len(_gated)} title(s) '
                                f'— reclassify before encoding. {_note}')
        if not encodable:
            _log(f'=== Encode aborted: all titles failed the runtime check ===')
            return

    ok = fail = 0

    for title_idx, (title_num, cls) in enumerate(encodable):
        title_dir = job_dir / f'title_{title_num}'
        mkv_files = sorted(title_dir.glob('*.mkv'))
        if not mkv_files:
            _log(f'FAIL {job_id} title_{title_num}: MKV missing')
            fail += 1
            continue

        staging_path = _route_staging(cls, title_num)
        if staging_path is None:
            continue

        # Skip — don't create _2 duplicates — if this filename already landed
        # in staging from a different job for the same content.
        if staging_path.exists():
            _log(f'SKIP {job_id} title_{title_num}: staging file already exists ({staging_path.name})')
            ok += 1
            db.update_title(job_id, title_num, staged_path=str(staging_path))
            continue

        title_name = staging_path.stem
        db.set_encode_progress(job_id, {
            'pct': 0.0, 'fps': 0.0, 'eta_secs': 0,
            'title': title_name, 'idx': title_idx + 1, 'total': len(encodable),
            'ts': time.time(),
        })

        cmd = [
            'HandBrakeCLI', '-i', str(mkv_files[0]), '-t', '1',
            '-o', str(staging_path), '--preset', HB_PRESET,
            *HB_FILTERS, *HB_AUDIO,
        ]
        rc = _run_handbrake(cmd, job_id, title_name, title_idx + 1, len(encodable))

        if rc == 0:
            ok += 1
            db.update_title(job_id, title_num, staged_path=str(staging_path))
            _log(f'OK   {job_id} title_{title_num}: {staging_path}')
        else:
            fail += 1
            _log(f'FAIL {job_id} title_{title_num}: HandBrakeCLI error (rc={rc})')

    db.set_encode_progress(job_id, None)
    db.set_job_timestamp(job_id, 'encode_completed_at')

    if fail == 0:
        db.set_job_status(job_id, 'staged')
    else:
        db.set_job_status(job_id, 'error', notes=f'{fail} encode failure(s)')
    _log(f'=== Encode {"staged" if fail == 0 else "error"}: {job["name"]} ok={ok} fail={fail} ===')


# ── Context helpers ──────────────────────────────────────────────────────────

def _get_effective_context(job):
    """Return disc_context if set, otherwise parse show/season from job name."""
    ctx = (job or {}).get('disc_context')
    if ctx:
        return ctx
    name = (job or {}).get('name', '')
    if not name:
        return None
    # TV: "Show S01", "Show Season 1", "Show S01 Disc2", "Show - S01", etc.
    m = re.match(r'^(.+?)\s*[-–]?\s+[Ss](?:eason\s*)?0*(\d{1,2})(?:\b|$)', name)
    if m:
        return {'type': 'tv', 'show': m.group(1).strip(' -–'), 'season': int(m.group(2)), 'episodes': []}
    # Movie: "Title (Year)"
    m = re.match(r'^(.+?)\s+\((\d{4})\)\s*$', name)
    if m:
        return {'type': 'movie', 'title': m.group(1).strip(), 'year': int(m.group(2))}
    return None


def _build_classify_prompt(title_num, all_titles, context, dur_str, size_str, flags):
    """Build the classification prompt text. Focused when show/movie context is known."""
    if context and context.get('type') == 'tv':
        show     = context.get('show', '')
        season   = context.get('season')
        episodes = context.get('episodes') or []

        # Which Nth main-episode title is this on the disc?
        ep_nums = sorted(
            t['title_num'] for t in all_titles
            if 'play_all' not in (t['flags'] or [])
            and (t['duration_secs'] or 0) > 900
        )
        position_hint = ''
        if title_num in ep_nums and episodes:
            pos = ep_nums.index(title_num)
            if pos < len(episodes):
                ep_num = episodes[pos]
                sn = f'S{season:02d}' if isinstance(season, int) else ''
                position_hint = (
                    f'\nThis is the #{pos+1} episode title on the disc'
                    + (f' — most likely {sn}E{ep_num:02d}.' if sn else f' — most likely episode {ep_num}.')
                )

        season_str   = f' Season {season}' if season else ''
        ep_list_hint = (f' Episodes on this disc: {", ".join(str(e) for e in episodes)}.' if episodes else '')
        season_val   = season if isinstance(season, int) else 0
        # The operator has already confirmed the season from the disc case --
        # Claude's job is to place the episode within it, not to second-guess
        # the season itself (shows with seasons split across multiple discs,
        # or with content Claude misremembers, otherwise invite it to
        # "correct" a season number that is actually already right).
        season_lock = (
            f'\nThe season number above ({season_val}) is operator-confirmed from the '
            f'disc case -- always return it unchanged in your reply. Only determine the '
            f'episode number and title within that season.'
        ) if season_val else ''

        return (
            f'These are screenshots from a {show}{season_str} episode.\n'
            f'Duration: {dur_str}  |  File size: {size_str}  |  Flags: {flags}'
            f'{ep_list_hint}{position_hint}{season_lock}\n\n'
            f'Identify which specific episode this is. Examine on-screen text (title cards,\n'
            f'subtitles, signage), character names and faces, locations, and plot moments.\n'
            f'You MUST provide the episode number and title — do not leave them blank.\n'
            f'If uncertain between two episodes, pick the most likely and note it in reasoning.\n\n'
            f'Reply ONLY with valid JSON — no markdown, no explanation:\n'
            f'{{"type":"tv_episode","show":"{show}","season":{season_val},'
            f'"episode":0,"episode_title":"...","confidence":"high|medium|low","reasoning":"..."}}'
        )

    elif context and context.get('type') == 'movie':
        mtitle   = context.get('title', '')
        year     = context.get('year', '')
        year_str = f' ({year})' if year else ''
        year_val = year if year else 0
        return (
            f'These are screenshots from the movie "{mtitle}"{year_str}.\n'
            f'Duration: {dur_str}  |  File size: {size_str}  |  Flags: {flags}\n\n'
            f'Confirm whether this is the main feature or a bonus/special feature.\n'
            f'If it is the main feature, type is "movie". If it is a short bonus, type is "special".\n'
            f'If it appears to be a combined play-all title, type is "play_all".\n\n'
            f'Reply ONLY with valid JSON:\n'
            f'{{"type":"movie","movie_title":"{mtitle}","year":{year_val},'
            f'"confidence":"high|medium|low","reasoning":"..."}}'
        )

    else:
        return (
            f'These are screenshots sampled evenly across DVD title {title_num}.\n'
            f'Duration: {dur_str}  |  File size: {size_str}  |  Flags: {flags}\n\n'
            f'Classify this as one of: tv_episode, movie, special, documentary, play_all, or unknown.\n'
            f'- play_all means a combined "play all episodes" title — skip it.\n'
            f'- If tv_episode: identify the show, season, and specific episode number and title.\n'
            f'- If movie: provide title and year.\n\n'
            f'Reply ONLY with valid JSON:\n'
            f'{{"type":"...","show":"...","season":0,"episode":0,"episode_title":"...",'
            f'"movie_title":"...","year":0,"confidence":"high|medium|low","reasoning":"..."}}'
        )


# ── Disc identification (text-only Claude call) ───────────────────────────────

def _identify_disc_with_claude(disc_label, titles):
    """Ask Claude (text only) what disc this is. Returns parsed dict or None."""
    api_key = ANTHROPIC_API_KEY
    if not api_key:
        return None
    try:
        def _dur(secs):
            if not secs:
                return '?:??:??'
            secs = int(secs)
            h, rem = divmod(secs, 3600)
            m, s   = divmod(rem, 60)
            return f'{h}:{m:02d}:{s:02d}'

        title_lines = '\n'.join(
            f'  Title {t["num"]}: {_dur(t["dur_secs"])}'
            + (f'  [{", ".join(t["flags"])}]' if t.get('flags') else '')
            for t in titles
        )

        prompt = (
            f'I have a DVD disc with the label: {disc_label!r}\n\n'
            f'It contains {len(titles)} title(s):\n{title_lines}\n\n'
            f'Based on the disc label and title structure, is this a TV show or a movie?\n'
            f'If TV: which episodes are the main episodes (exclude play-all and shorts)?\n\n'
            f'Reply ONLY with valid JSON — no markdown, no explanation.\n'
            f'For a TV disc:    {{"type":"tv","show":"...","season":3,"episodes":[1,2,3,4],"specials":[]}}\n'
            f'For a movie disc: {{"type":"movie","title":"...","year":1994,"main_title":1}}\n'
        )

        payload = json.dumps({
            'model': ANTHROPIC_MODEL,
            'max_tokens': 300,
            'messages': [{'role': 'user', 'content': prompt}]
        }).encode()

        req = urllib.request.Request(
            'https://api.anthropic.com/v1/messages',
            data=payload,
            headers={
                'x-api-key': api_key,
                'anthropic-version': '2023-06-01',
                'content-type': 'application/json',
            }
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read())
        text = body['content'][0]['text'].strip()
        m = re.search(r'\{.*\}', text, re.DOTALL)
        if m:
            return json.loads(m.group())
        return None
    except Exception:
        return None


# ── Auto-classify short titles as Special Features ───────────────────────────

def _auto_classify_short(job_id, title_num, disc_context):
    """Titles under 8 minutes are auto-classified as Special Features.

    Name comes from context (disc_context or job name) for sequential numbering.
    """
    job     = db.get_job(job_id)
    context = disc_context or _get_effective_context(job)

    name = ''
    if context:
        if context.get('type') == 'tv':
            show   = context.get('show', '')
            season = context.get('season')
            name   = f'{show} - Season {season}' if show and season else show
        elif context.get('type') == 'movie':
            t = context.get('title', '')
            y = context.get('year', '')
            name = f'{t} ({y})' if y else t
    if not name:
        name = (job or {}).get('name', 'Unknown')

    # Count already-auto-classified shorts on this job for sequential numbering
    existing = sum(
        1 for t in db.get_titles(job_id)
        if (t['classification'] or {}).get('_source') == 'auto_short'
    )
    num = existing + 1

    db.save_classification(job_id, title_num, {
        'type':       'special',
        'show':       name,
        'title':      f'{name} - Special Features {num}',
        '_source':    'auto_short',
        'confidence': 'high',
        'reasoning':  'Auto-classified: duration under 8 minutes.',
    })


# ── TV episode identification via TMDb lookup (not screenshot guessing) ──────

def _disc_episode_number(title_num, all_titles, episodes):
    """Map a title's position among eligible (non-play_all, >15min) titles on
    the disc to the operator-provided 'episodes on disc' list. Returns None
    if the title isn't eligible or its position falls outside that list --
    this only reads off the operator's own input, it never guesses a number.
    """
    ep_nums = sorted(
        t['title_num'] for t in all_titles
        if 'play_all' not in (t['flags'] or [])
        and (t['duration_secs'] or 0) > 900
    )
    if title_num not in ep_nums or not episodes:
        return None
    pos = ep_nums.index(title_num)
    if pos >= len(episodes):
        return None
    return episodes[pos]


def _auto_classify_tv_from_lookup(job_id, title_num, context, all_titles):
    """Classify one TV title using the operator-confirmed show/season/disc
    position (already fully deterministic, see _disc_episode_number) plus a
    TMDb lookup for the episode *title* -- no screenshots, no Claude, no
    guessing. If TMDb has no key configured or can't resolve the episode,
    the season/episode number are still saved (they never depended on TMDb
    to begin with); only episode_title is left blank with confidence 'low'.
    Never falls back to a guessed title.
    """
    show   = context.get('show', '')
    season = context.get('season')
    episode = _disc_episode_number(title_num, all_titles, context.get('episodes') or [])
    if episode is None:
        return False

    title_text = tmdb.lookup_episode_title(show, season, episode)
    classification = {
        'type': 'tv_episode', 'show': show, 'season': season, 'episode': episode,
        'episode_title': title_text or '',
        'confidence': 'high' if title_text else 'low',
        'reasoning': (
            f'Looked up from TMDb: {show} S{season:02d}E{episode:02d}.' if title_text
            else f'TMDb lookup found no title for {show} S{season:02d}E{episode:02d} -- '
                 f'season/episode number are from disc position, title needs manual entry.'
        ),
        '_source': 'tmdb_lookup',
    }
    db.save_classification(job_id, title_num, classification)
    return True


# ── Auto-classify a single title in the background ────────────────────────────

def _auto_classify_one(job_id, title_num, disc_context):
    """Call Claude to classify one title; save result with _source=claude_auto."""
    api_key = ANTHROPIC_API_KEY
    if not api_key:
        return False
    try:
        title = db.get_title(job_id, title_num)
        if not title:
            return False

        job        = db.get_job(job_id)
        context    = disc_context or _get_effective_context(job)
        all_titles = db.get_titles(job_id)

        content = []

        # Attach up to 3 screenshots as base64
        for spath in (title['screenshots'] or [])[:3]:
            full = REVIEW_DIR / spath
            if full.exists():
                img_b64 = base64.b64encode(full.read_bytes()).decode()
                content.append({
                    'type': 'image',
                    'source': {'type': 'base64', 'media_type': 'image/jpeg', 'data': img_b64}
                })

        dur_str  = fmt_duration(title['duration_secs'])
        size_str = fmt_size(title['filesize_bytes'])
        flags    = ', '.join(title['flags']) if title['flags'] else 'none'

        content.append({
            'type': 'text',
            'text': _build_classify_prompt(title_num, all_titles, context, dur_str, size_str, flags)
        })

        payload = json.dumps({
            'model': ANTHROPIC_MODEL,
            'max_tokens': 600,
            'messages': [{'role': 'user', 'content': content}]
        }).encode()

        req = urllib.request.Request(
            'https://api.anthropic.com/v1/messages',
            data=payload,
            headers={
                'x-api-key': api_key,
                'anthropic-version': '2023-06-01',
                'content-type': 'application/json',
            }
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = json.loads(resp.read())
        text = body['content'][0]['text'].strip()
        m = re.search(r'\{.*\}', text, re.DOTALL)
        if not m:
            return False
        classification = json.loads(m.group())
        classification['_source'] = 'claude_auto'
        db.save_classification(job_id, title_num, classification)
        return True
    except Exception:
        return False


# ── Background ripping thread ─────────────────────────────────────────────────

def _rip_disc_bg(job_id, device, title_nums, disc_context):
    """Background thread: rip selected titles one by one, then auto-classify.

    Phase 1 — MakeMKV: rip each title, register in DB with basic metadata.
    Phase 2 — post-processing: screenshots + Claude classification, done after
               the disc is ejected so the drive is free for the next job.
    """
    try:
        job = db.get_job(job_id)
        if not job:
            return
        job_dir = Path(job['review_path'])

        # ── Phase 1: rip ──────────────────────────────────────────────────────
        ripped = []
        for title_num in title_nums:
            try:
                title_dir = job_dir / f'title_{title_num}'
                title_dir.mkdir(parents=True, exist_ok=True)

                mkv = rip_title(device, title_num, title_dir, job_id=job_id)
                if mkv is None:
                    continue

                db.set_rip_progress(job_id, {
                    'title_num': title_num, 'pct': 100,
                    'op': 'Post-processing…', 'ts': time.time()
                })

                duration    = get_duration(mkv)
                single      = flag_titles([{'num': title_num, 'dur_secs': duration, 'flags': []}])
                final_flags = single[0]['flags']

                rel_mkv = str(mkv.relative_to(REVIEW_DIR))
                db.add_title(job_id, title_num, rel_mkv,
                             duration, mkv.stat().st_size, [], final_flags)
                db.set_rip_progress(job_id, None)
                ripped.append((title_num, mkv, duration))

            except Exception:
                pass

        # MakeMKV is done — eject immediately and open the job for review
        try:
            subprocess.run(['eject', device], capture_output=True)
        except Exception:
            pass
        db.set_job_timestamp(job_id, 'rip_completed_at')
        db.set_job_status(job_id, 'review')

        # ── Phase 2: screenshots + classification (drive no longer needed) ────
        # TV titles are NEVER auto-classified with Claude here. Screenshot-only
        # guessing on TV episodes has repeatedly produced confident-wrong or
        # fabricated episode titles (see media-library-integrity skill) --
        # disc position/order is not a reliable signal and 3 static frames
        # aren't enough to distinguish episodes of the same show. TV titles
        # are left Unclassified so the operator must fill in Disc Context
        # (real episode numbers off the sleeve) and either run
        # "Look Up All Episodes" (deterministic TMDb lookup) or
        # "Verify via Transcript" (dialogue match) -- both far stronger
        # signals than screenshots. Movies/specials still use Claude since a
        # single feature-length title is easy to identify visually and has
        # no sibling episodes to confuse it with.
        effective_ctx = disc_context or _get_effective_context(job)
        is_tv_job = bool(effective_ctx and effective_ctx.get('type') == 'tv')

        for title_num, mkv, duration in ripped:
            try:
                title_dir   = job_dir / f'title_{title_num}'
                screenshots = take_screenshots(mkv, title_dir / 'screenshots', duration)
                db.update_title(job_id, title_num, screenshots=screenshots)

                if duration and duration < 480:
                    # Under 8 minutes — auto-classify as Special Features
                    _auto_classify_short(job_id, title_num, disc_context)
                elif is_tv_job:
                    pass  # leave unclassified — see comment above
                elif ANTHROPIC_API_KEY:
                    _auto_classify_one(job_id, title_num, disc_context)

            except Exception:
                pass

    except Exception:
        db.set_job_status(job_id, 'error', notes='Background rip failed unexpectedly')
        try:
            subprocess.run(['eject', device], capture_output=True)
        except Exception:
            pass


# ── New Rip page ──────────────────────────────────────────────────────────────

# ── MakeMKV health check ─────────────────────────────────────────────────────

_makemkv_status_cache = {'checked_at': 0, 'ok': True, 'message': ''}
_MAKEMKV_STATUS_TTL = 120  # seconds -- avoid spawning makemkvcon on every page load


@app.route('/media/makemkv/status')
def makemkv_status():
    now = time.time()
    if now - _makemkv_status_cache['checked_at'] > _MAKEMKV_STATUS_TTL:
        ok, message = check_makemkv_version()
        _makemkv_status_cache.update(checked_at=now, ok=ok, message=message)
    return jsonify({'ok': _makemkv_status_cache['ok'], 'message': _makemkv_status_cache['message']})


@app.route('/media/makemkv/update', methods=['POST'])
def makemkv_update():
    try:
        result = subprocess.run(
            ['sudo', '-n', '/usr/local/sbin/mediaintel-update-makemkv'],
            capture_output=True, text=True, timeout=180
        )
    except subprocess.TimeoutExpired:
        return jsonify({'ok': False, 'output': 'Update timed out after 3 minutes.'}), 500

    output = (result.stdout or '') + (result.stderr or '')
    if result.returncode != 0:
        return jsonify({'ok': False, 'output': output}), 500

    # Force a fresh check next time status is polled instead of waiting out the TTL
    _makemkv_status_cache.update(checked_at=0)
    return jsonify({'ok': True, 'output': output})


@app.route('/media/rip')
def rip_page():
    drives = detect_drives()
    # Flag a drive as busy if an active rip job is using it, so the New Disc
    # Rip page can warn against starting a second job on the same drive
    # instead of just showing "disc detected" with no hint it's mid-rip.
    active_by_drive = {j['drive']: j for j in db.get_all_jobs()
                       if j['status'] == 'ingesting' and j.get('drive')}
    for d in drives:
        busy_job = active_by_drive.get(d['device'])
        d['busy_job'] = busy_job['name'] if busy_job else None
    return render_template('rip.html', drives=drives)


# ── Disc scan (background thread + polled status) ────────────────────────────
#
# A scan is a slow (up to SCAN_TIMEOUT seconds) makemkvcon call. Running it
# inline in the request handler meant navigating away lost the result
# entirely (the server kept scanning with nobody listening) and a second
# click on the same drive spawned a second, overlapping makemkvcon process
# with nothing preventing it. Tracking state here per-device instead means
# the rip page can poll and recover its result even after a navigation, and
# a second scan request for a drive already scanning is rejected outright.

_scan_state = {}       # device -> {'status': 'running'|'done'|'error', ...}
_scan_lock  = threading.Lock()


def _dur_str(secs):
    if not secs:
        return '?:??:??'
    secs = int(secs)
    h, rem = divmod(secs, 3600)
    m, s   = divmod(rem, 60)
    return f'{h}:{m:02d}:{s:02d}'


def _last_tv_context():
    """Return {'show','season'} from the most recently created TV job, so the
    rip page can pre-fill the show name (rarely changes disc to disc) while
    leaving season and episode numbers for the operator to type off the
    sleeve. This replaces a Claude text-guess that inferred season/episode
    from disc label + title durations alone -- with no real signal to work
    from (disc labels are usually blank), it fabricated plausible-looking
    but wrong episode ranges every time. Duration/order carry no identity
    information; only the person holding the disc knows which one it is.
    """
    try:
        for j in db.get_all_jobs():
            ctx = j.get('disc_context')
            if ctx and ctx.get('type') == 'tv' and ctx.get('show'):
                return {'type': 'tv', 'show': ctx['show'], 'season': ctx.get('season')}
    except Exception:
        pass
    return None


def _infer_from_disc_label(show, disc_label):
    """If the disc's own volume label carries a literal disc number (e.g.
    "LAW_ORDER_SVU_DISC6"), suggest a season/episode range for it by
    combining that REAL, physically-burned number with the operator's own
    established per-disc episode count from prior rips of the same show
    (e.g. 4 episodes/disc, confirmed by hand on discs 2-4).

    This is fundamentally different from the banned duration/order-based
    guessing: the disc number itself isn't inferred, it's literally printed
    on the disc by the studio and read via blkid -- as reliable as reading
    the sleeve. The episode range built from it is still just a SUGGESTION
    the operator must confirm/correct before ripping (pre-filled, editable,
    never silently applied) -- e.g. a show's last disc in a season is often
    a half-disc (fewer episodes), which this can't know about.

    Returns None (leaving fields blank as before) when there's no disc
    number to read, or no prior per-disc pattern to reuse.
    """
    if not show or not disc_label:
        return None
    m = re.search(r'DISC\D{0,3}(\d{1,2})\b', disc_label, re.I)
    if not m:
        return None
    disc_num = int(m.group(1))

    jobs = [j for j in db.get_all_jobs()
            if (j.get('disc_context') or {}).get('type') == 'tv'
            and (j['disc_context'].get('show') or '').strip().lower() == show.strip().lower()
            and j['disc_context'].get('episodes')]
    if not jobs:
        return None

    from collections import Counter
    per_disc_counts = Counter(len(j['disc_context']['episodes']) for j in jobs)
    per_disc = per_disc_counts.most_common(1)[0][0]
    if per_disc <= 0:
        return None
    season_counts = Counter(j['disc_context'].get('season') for j in jobs
                            if j['disc_context'].get('season'))
    season = season_counts.most_common(1)[0][0] if season_counts else None

    start = (disc_num - 1) * per_disc + 1
    end = disc_num * per_disc
    return {
        'type': 'tv', 'show': show, 'season': season,
        'episodes': list(range(start, end + 1)),
        '_source': 'disc_label_pattern', '_disc_num': disc_num, '_per_disc': per_disc,
    }


def _scan_disc_bg(device):
    try:
        titles, disc_label = scan_disc(device)
        titles = flag_titles(titles)
        for t in titles:
            t['dur_str'] = _dur_str(t['dur_secs'])

        # No LLM guessing here. Two tiers of real signal only:
        #  1. The disc's own volume label, when it carries a literal disc
        #     number (e.g. "LAW_ORDER_SVU_DISC6") -- combined with the
        #     operator's own established per-disc episode count from prior
        #     rips of the same show, to suggest a season/episode range.
        #  2. Falls back to just the show name carried over from the last
        #     TV job if no disc-number pattern is found.
        # Either way this is a pre-filled SUGGESTION the operator confirms/
        # edits before ripping -- never silently trusted.
        last_ctx = _last_tv_context()
        disc_id = None
        if last_ctx and last_ctx.get('show'):
            disc_id = _infer_from_disc_label(last_ctx['show'], disc_label)
        if not disc_id:
            disc_id = last_ctx

        _scan_state[device] = {
            'status': 'done',
            'finished_at': time.time(),
            'result': {'disc_label': disc_label, 'titles': titles, 'disc_id': disc_id},
        }
    except Exception as e:
        _scan_state[device] = {'status': 'error', 'finished_at': time.time(), 'message': str(e)}


@app.route('/media/rip/scan', methods=['POST'])
def rip_scan_ajax():
    data   = request.get_json(silent=True) or {}
    device = data.get('device', '')
    if not device:
        return jsonify({'error': 'No device specified'}), 400

    with _scan_lock:
        if _scan_state.get(device, {}).get('status') == 'running':
            return jsonify({'error': 'A scan is already in progress for this drive', 'busy': True}), 409
        _scan_state[device] = {'status': 'running', 'started_at': time.time()}

    threading.Thread(target=_scan_disc_bg, args=(device,), daemon=True).start()
    return jsonify({'ok': True})


@app.route('/media/rip/scan-status')
def rip_scan_status():
    device = request.args.get('device', '')
    state  = _scan_state.get(device, {'status': 'idle'})
    return jsonify(state)


@app.route('/media/rip/start', methods=['POST'])
def rip_start_ajax():
    data         = request.get_json(silent=True) or {}
    job_name     = data.get('job_name', '').strip()
    device       = data.get('device', '')
    title_nums   = data.get('title_nums', [])
    disc_context = data.get('disc_context') or None  # user-confirmed context dict

    if not job_name:
        return jsonify({'error': 'Job name is required'}), 400
    if not title_nums:
        return jsonify({'error': 'No titles selected'}), 400

    safe_name = re.sub(r'[^\w\s-]', '', job_name).strip()
    safe_name = re.sub(r'\s+', '-', safe_name)
    timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
    job_id    = f'{safe_name}-{timestamp}'
    job_dir   = REVIEW_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    db.create_job(job_id, job_name, job_dir, drive=device, disc_context=disc_context)
    _scan_state.pop(device, None)  # stale scan result for this device no longer applies

    t = threading.Thread(
        target=_rip_disc_bg,
        args=(job_id, device, title_nums, disc_context),
        daemon=True
    )
    t.start()

    return jsonify({'ok': True, 'job_id': job_id})


# ── MKV Upload ───────────────────────────────────────────────────────────────

@app.route('/media/upload')
def upload_page():
    return render_template('upload.html')


@app.route('/media/upload', methods=['POST'])
def upload_start():
    job_name = request.form.get('job_name', '').strip()
    if not job_name:
        return jsonify({'error': 'Job name is required'}), 400

    files = request.files.getlist('files')
    mkv_files = [f for f in files if f.filename.lower().endswith('.mkv')]
    if not mkv_files:
        return jsonify({'error': 'No MKV files found in upload'}), 400

    safe_name = re.sub(r'[^\w\s-]', '', job_name).strip()
    safe_name = re.sub(r'\s+', '-', safe_name)
    timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
    job_id    = f'{safe_name}-{timestamp}'
    job_dir   = REVIEW_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    db.create_job(job_id, job_name, job_dir)

    title_paths = []
    for i, f in enumerate(mkv_files):
        title_dir = job_dir / f'title_{i}'
        title_dir.mkdir(parents=True, exist_ok=True)
        dest = title_dir / f'title_t{i:02d}.mkv'
        f.save(str(dest))
        title_paths.append(str(dest))

    t = threading.Thread(
        target=_import_mkv_bg,
        args=(job_id, title_paths),
        daemon=True
    )
    t.start()

    return jsonify({'ok': True, 'job_id': job_id})


def _import_mkv_bg(job_id, title_paths):
    """Background thread: take screenshots, flag, and auto-classify uploaded MKVs."""
    try:
        job = db.get_job(job_id)
        if not job:
            return

        raw_titles = [
            {'num': i, 'dur_secs': get_duration(p), 'flags': []}
            for i, p in enumerate(title_paths)
        ]
        flagged = flag_titles(raw_titles)

        for i, mkv_path in enumerate(title_paths):
            try:
                mkv_path   = Path(mkv_path)
                duration   = flagged[i]['dur_secs']
                screenshots = take_screenshots(mkv_path, mkv_path.parent / 'screenshots', duration)
                final_flags = flagged[i]['flags']
                rel_mkv     = str(mkv_path.relative_to(REVIEW_DIR))
                db.add_title(job_id, i, rel_mkv,
                             duration, mkv_path.stat().st_size, screenshots, final_flags)
                if ANTHROPIC_API_KEY:
                    _auto_classify_one(job_id, i, None)
            except Exception:
                pass

        db.set_job_status(job_id, 'review')
    except Exception:
        db.set_job_status(job_id, 'error', notes='MKV import processing failed')


# ── Claude AI suggestion ──────────────────────────────────────────────────────

@app.route('/media/job/<job_id>/title/<int:n>/ask-claude', methods=['POST'])
def ask_claude(job_id, n):
    title = db.get_title(job_id, n)
    if not title:
        return jsonify({'error': 'Title not found'}), 404

    job        = db.get_job(job_id)
    context    = _get_effective_context(job)
    all_titles = db.get_titles(job_id)

    # TV: deterministic disc position + TMDb lookup, no screenshots, no
    # Claude call at all -- same response shape the UI already expects.
    if context and context.get('type') == 'tv':
        show    = context.get('show', '')
        season  = context.get('season')
        episode = _disc_episode_number(n, all_titles, context.get('episodes') or [])
        if episode is None:
            return jsonify({
                'type': 'tv_episode', 'show': show, 'season': season, 'episode': 0,
                'episode_title': '', 'confidence': 'low',
                'reasoning': 'Could not determine this title\'s disc position or episode '
                             'number from "Episodes on disc" -- check that list is complete.',
            })
        title_text = tmdb.lookup_episode_title(show, season, episode)
        return jsonify({
            'type': 'tv_episode', 'show': show, 'season': season, 'episode': episode,
            'episode_title': title_text or '',
            'confidence': 'high' if title_text else 'low',
            'reasoning': (
                f'Looked up from TMDb: {show} S{season:02d}E{episode:02d}.' if title_text
                else f'TMDb lookup found no title for {show} S{season:02d}E{episode:02d} -- '
                     f'season/episode number are from disc position, title needs manual entry.'
            ),
        })

    api_key = ANTHROPIC_API_KEY
    if not api_key:
        return jsonify({'error': 'ANTHROPIC_API_KEY is not configured on this server.'}), 503

    content = []

    # Attach up to 3 screenshots as base64
    for spath in (title['screenshots'] or [])[:3]:
        full = REVIEW_DIR / spath
        if full.exists():
            img_b64 = base64.b64encode(full.read_bytes()).decode()
            content.append({
                'type': 'image',
                'source': {'type': 'base64', 'media_type': 'image/jpeg', 'data': img_b64}
            })

    dur_str  = fmt_duration(title['duration_secs'])
    size_str = fmt_size(title['filesize_bytes'])
    flags    = ', '.join(title['flags']) if title['flags'] else 'none'

    content.append({
        'type': 'text',
        'text': _build_classify_prompt(n, all_titles, context, dur_str, size_str, flags)
    })

    payload = json.dumps({
        'model': ANTHROPIC_MODEL,
        'max_tokens': 600,
        'messages': [{'role': 'user', 'content': content}]
    }).encode()

    req = urllib.request.Request(
        'https://api.anthropic.com/v1/messages',
        data=payload,
        headers={
            'x-api-key': api_key,
            'anthropic-version': '2023-06-01',
            'content-type': 'application/json',
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read())
        text = body['content'][0]['text'].strip()
        # Extract JSON from response
        m = re.search(r'\{.*\}', text, re.DOTALL)
        suggestion = json.loads(m.group()) if m else {'type': 'unknown', 'reasoning': text}
        return jsonify(suggestion)
    except urllib.error.HTTPError as e:
        err = e.read().decode()
        return jsonify({'error': f'Anthropic API error {e.code}: {err}'}), 502
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── On-demand screenshot generation ──────────────────────────────────────────

@app.route('/media/job/<job_id>/title/<int:n>/screenshots', methods=['POST'])
def retake_screenshots(job_id, n):
    title = db.get_title(job_id, n)
    if not title:
        return jsonify({'error': 'Title not found'}), 404

    mkv_path = REVIEW_DIR / title['filepath']
    if not mkv_path.exists():
        return jsonify({'error': 'MKV file not found on disk'}), 404

    duration    = get_duration(mkv_path)
    thumb_dir   = mkv_path.parent / 'screenshots'
    screenshots = take_screenshots(mkv_path, thumb_dir, duration)

    if not screenshots:
        return jsonify({'error': 'ffmpeg produced no frames — check the MKV file'}), 500

    db.update_title(job_id, n, screenshots=screenshots)
    urls = [f'/media/screenshots/{s}' for s in screenshots]
    return jsonify({'ok': True, 'urls': urls})


# ── Screenshot serving ────────────────────────────────────────────────────────

@app.route('/media/screenshots/<path:filepath>')
def screenshot(filepath):
    full = REVIEW_DIR / filepath
    if not full.exists():
        return 'Not found', 404
    return send_from_directory(str(full.parent), full.name)


# ── Catalog status (read-only window into the catalog table) ─────────────────

@app.route('/media/catalog-status')
def catalog_status():
    counts        = db.get_catalog_state_counts()
    source_counts = db.get_catalog_source_counts()
    all_rows    = db.get_all_catalog_rows()
    failed_rows = [r for r in all_rows if r['state'] == 'failed']

    # Recorded (at commit time) vs actual current size — read-only stat, no
    # writes, no repair. A mismatch here is exactly what caught Man on Fire.
    for r in failed_rows:
        p = Path(r['library_path'])
        try:
            r['actual_size'] = p.stat().st_size if p.exists() else None
        except OSError:
            r['actual_size'] = None

    conflict_data = catalog.find_uncataloged_conflicts()

    return render_template('catalog_status.html',
                           counts=counts,
                           source_counts=source_counts,
                           total_rows=len(all_rows),
                           failed_rows=failed_rows,
                           conflicts=conflict_data['conflicts'],
                           unroutable=conflict_data['unroutable'])


@app.route('/media/catalog/verify-pending', methods=['POST'])
def catalog_verify_pending():
    """Bulk-run catalog.verify_pending_rows() -- checks every row still in
    'pending' or 'committed' against disk (read-only: stat + ffprobe only)
    and promotes each to 'verified' or 'failed'. Idempotent.
    """
    summary = catalog.verify_pending_rows()
    return jsonify({'ok': True, **summary})


@app.route('/media/catalog/<int:catalog_id>')
def catalog_detail(catalog_id):
    """Read-only detail view of a single catalog row (Phase 2B). Used as the
    'View Existing' / clickable-title destination for duplicate candidates
    that have their own catalog row. One on-demand stat() for the actual
    file size -- same pattern catalog_status() already uses for failed rows,
    done here for exactly one row, not a scan.
    """
    row = db.get_catalog_row_by_id(catalog_id)
    if not row:
        return 'Catalog row not found', 404
    path = Path(row['library_path'])
    try:
        actual_size = path.stat().st_size if path.exists() else None
    except OSError:
        actual_size = None
    return render_template('catalog_detail.html', row=row, actual_size=actual_size,
                           file_exists=path.exists())


@app.route('/media/catalog/<int:catalog_id>/reverify', methods=['POST'])
def catalog_reverify(catalog_id):
    """Re-check one catalog row's file against disk. Read-only w.r.t. the
    file (stat + ffprobe only) — never deletes, moves, or fixes anything,
    never touches job status. Only this one row's state may change, and
    only when its current state is 'failed' or 'verified' — see the guard
    in catalog.reverify_row() itself.
    """
    try:
        row = catalog.reverify_row(catalog_id)
    except catalog.ReverifyNotAllowed as e:
        return jsonify({'error': str(e)}), 409
    if row is None:
        return jsonify({'error': 'Catalog row not found'}), 404
    return jsonify({'ok': True, 'state': row['state'], 'fail_reason': row['fail_reason']})


# ── Live dashboard progress (polling, no page reload) ─────────────────────────

@app.route('/media/progress.json')
def progress_json():
    """Lightweight read-only polling endpoint for the dashboard. Returns
    job-status buckets (so the client can detect when a job has moved
    sections and a real reload is actually needed) plus the raw rip/encode
    progress dicts (so the client can update progress bars in place the rest
    of the time). No rendering, no writes -- safe to poll every couple of
    seconds.
    """
    jobs = db.get_all_jobs()
    buckets = {}
    for j in jobs:
        buckets.setdefault(j['status'], []).append(j['id'])
    rip    = {j['id']: j['rip_progress']    for j in jobs if j.get('rip_progress')}
    encode = {j['id']: j['encode_progress'] for j in jobs if j.get('encode_progress')}
    return jsonify({'buckets': buckets, 'rip': rip, 'encode': encode, 'now': time.time()})


# ── Healthcheck ───────────────────────────────────────────────────────────────

@app.route('/media/health')
def health():
    return jsonify({'ok': True, 'db': str(DB_PATH)})


# ── Boot ──────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    db.init_db()
    _startup_recovery()
    print(f'Media Intel Dashboard → http://0.0.0.0:{PORT}/media')
    app.run(host='0.0.0.0', port=PORT, debug=False, threaded=True)
