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
from disc import detect_drives, scan_disc, flag_titles, rip_title, get_duration, take_screenshots

app = Flask(__name__, static_url_path='/media/static')
app.secret_key = 'mediaintel-change-in-production'
app.config['MAX_CONTENT_LENGTH'] = None  # MKVs can be many GB


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
    review    = [j for j in all_jobs if j['status'] == 'review']
    approved  = [j for j in all_jobs if j['status'] == 'approved']
    encoding  = [j for j in all_jobs if j['status'] == 'encoding']
    staged    = [j for j in all_jobs if j['status'] == 'staged']
    completed = [j for j in all_jobs if j['status'] in ('complete', 'error')]
    total_titles = sum(j['title_count'] for j in all_jobs)
    return render_template('dashboard.html',
                           active=active, review=review,
                           approved=approved, encoding=encoding,
                           staged=staged, completed=completed,
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
    return render_template('job.html', job=job, titles=titles,
                           classified_count=classified, total_count=len(titles))


# ── Classification ────────────────────────────────────────────────────────────

@app.route('/media/job/<job_id>/title/<int:n>/classify', methods=['POST'])
def classify_title(job_id, n):
    data = request.get_json(silent=True)
    if not data or 'type' not in data:
        return jsonify({'error': 'Missing type field'}), 400
    db.save_classification(job_id, n, data)
    # Promote job from ingesting → review once first classification arrives
    job = db.get_job(job_id)
    if job and job['status'] == 'ingesting':
        db.set_job_status(job_id, 'review')
    return jsonify({'ok': True})


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

    # Optional: immediately start encoding when auto_encode=True
    auto_encode = (request.get_json(silent=True) or {}).get('auto_encode', False)
    if auto_encode:
        db.set_job_status(job_id, 'encoding')
        t = threading.Thread(target=_encode_job_bg, args=(job_id,), daemon=True)
        t.start()

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
    """Re-run Claude auto-classification on every unclassified title in the job."""
    if not ANTHROPIC_API_KEY:
        return jsonify({'error': 'ANTHROPIC_API_KEY not configured'}), 503
    job = db.get_job(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    disc_context = job.get('disc_context')
    titles = db.get_titles(job_id)
    to_classify = [t for t in titles if t['classification'] is None]
    if not to_classify:
        return jsonify({'ok': True, 'queued': 0})

    def _bg():
        for t in to_classify:
            try:
                if t['duration_secs'] and t['duration_secs'] < 480:
                    _auto_classify_short(job_id, t['title_num'], disc_context)
                else:
                    _auto_classify_one(job_id, t['title_num'], disc_context)
            except Exception:
                pass

    threading.Thread(target=_bg, daemon=True).start()
    return jsonify({'ok': True, 'queued': len(to_classify)})


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
    db.delete_job(job_id)
    return jsonify({'ok': True})


# ── Encode ───────────────────────────────────────────────────────────────────

@app.route('/media/job/<job_id>/encode', methods=['POST'])
def start_encode(job_id):
    job = db.get_job(job_id)
    if not job:
        return jsonify({'error': 'Not found'}), 404
    if job['status'] == 'encoding':
        return jsonify({'error': 'Already encoding'}), 400
    if job['status'] != 'approved':
        return jsonify({'error': f'Job must be approved first (currently: {job["status"]})'}), 400
    db.set_job_status(job_id, 'encoding')
    t = threading.Thread(target=_encode_job_bg, args=(job_id,), daemon=True)
    t.start()
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

    dest = _unique_path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.move(str(staged), str(dest))
    except OSError as e:
        return jsonify({'error': str(e)}), 500

    db.update_title(job_id, n, staged_path=None)
    _maybe_complete_staged_job(job_id)
    return jsonify({'ok': True, 'dest': str(dest)})


@app.route('/media/job/<job_id>/commit-all', methods=['POST'])
def commit_all(job_id):
    titles = db.get_titles(job_id)
    staged = [t for t in titles if t.get('staged_path')]
    if not staged:
        return jsonify({'error': 'No staged titles to commit'}), 400

    ok_list = []
    errors  = []
    for t in staged:
        n    = t['title_num']
        path = Path(t['staged_path'])
        cls  = t.get('classification')
        if not cls or not path.exists():
            errors.append(f'title_{n}: staged file missing or no classification')
            continue
        dest = _route_library(cls, n)
        if dest is None:
            continue
        dest = _unique_path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(path), str(dest))
            db.update_title(job_id, n, staged_path=None)
            ok_list.append(str(dest))
        except OSError as e:
            errors.append(f'title_{n}: {e}')

    _maybe_complete_staged_job(job_id)
    return jsonify({'ok': True, 'committed': len(ok_list), 'errors': errors})


def _maybe_complete_staged_job(job_id):
    titles = db.get_titles(job_id)
    if not any(t.get('staged_path') for t in titles):
        db.set_job_status(job_id, 'complete')


def _safe_name(name):
    return re.sub(r'[<>:"/\\|?*]', '', str(name)).strip('. ')


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


def _encode_job_bg(job_id):
    """Background thread: HandBrake-encode all titles into the staging folder."""
    job = db.get_job(job_id)
    if not job:
        return

    job_dir  = Path(job['review_path'])
    cls_file = job_dir / 'classification.json'

    if not cls_file.exists():
        db.set_job_status(job_id, 'error', notes='classification.json missing')
        return

    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    log_path = Path('/srv/media/encode_log.txt')

    def _log(msg):
        ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        with open(log_path, 'a') as lf:
            lf.write(f'[{ts}] {msg}\n')

    with open(cls_file) as f:
        classifications = json.load(f)

    _log(f'=== Encode start (staging): {job["name"]} ({job_id}) ===')
    ok = fail = 0

    for key, cls in sorted(classifications.items()):
        m = re.match(r'^title_(\d+)$', key)
        if not m:
            continue
        title_num = int(m.group(1))

        if cls.get('type') in ('play_all', 'deleted', 'unknown'):
            continue

        title_dir = job_dir / f'title_{title_num}'
        mkv_files = sorted(title_dir.glob('*.mkv'))
        if not mkv_files:
            _log(f'FAIL {job_id} {key}: MKV missing')
            fail += 1
            continue

        output_path = _unique_path(_route_staging(cls, title_num))
        if output_path is None:
            continue

        result = subprocess.run([
            'HandBrakeCLI', '-i', str(mkv_files[0]), '-t', '1',
            '-o', str(output_path), '--preset', HB_PRESET,
            *HB_FILTERS, *HB_AUDIO
        ])

        if result.returncode == 0:
            ok += 1
            db.update_title(job_id, title_num, staged_path=str(output_path))
            _log(f'OK   {job_id} {key}: {output_path}')
        else:
            fail += 1
            _log(f'FAIL {job_id} {key}: HandBrakeCLI error')

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

        return (
            f'These are screenshots from a {show}{season_str} episode.\n'
            f'Duration: {dur_str}  |  File size: {size_str}  |  Flags: {flags}'
            f'{ep_list_hint}{position_hint}\n\n'
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

                mkv = rip_title(device, title_num, title_dir)
                if mkv is None:
                    continue

                duration    = get_duration(mkv)
                single      = flag_titles([{'num': title_num, 'dur_secs': duration, 'flags': []}])
                final_flags = single[0]['flags']

                rel_mkv = str(mkv.relative_to(REVIEW_DIR))
                db.add_title(job_id, title_num, rel_mkv,
                             duration, mkv.stat().st_size, [], final_flags)
                ripped.append((title_num, mkv, duration))

            except Exception:
                pass

        # MakeMKV is done — eject immediately and open the job for review
        try:
            subprocess.run(['eject', device], capture_output=True)
        except Exception:
            pass
        db.set_job_status(job_id, 'review')

        # ── Phase 2: screenshots + classification (drive no longer needed) ────
        for title_num, mkv, duration in ripped:
            try:
                title_dir   = job_dir / f'title_{title_num}'
                screenshots = take_screenshots(mkv, title_dir / 'screenshots', duration)
                db.update_title(job_id, title_num, screenshots=screenshots)

                if duration and duration < 480:
                    # Under 8 minutes — auto-classify as Special Features
                    _auto_classify_short(job_id, title_num, disc_context)
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

@app.route('/media/rip')
def rip_page():
    drives = detect_drives()
    return render_template('rip.html', drives=drives)


@app.route('/media/rip/scan', methods=['POST'])
def rip_scan_ajax():
    data   = request.get_json(silent=True) or {}
    device = data.get('device', '')
    if not device:
        return jsonify({'error': 'No device specified'}), 400

    try:
        titles, disc_label = scan_disc(device)
    except Exception as e:
        return jsonify({'error': f'Scan failed: {e}'}), 500

    titles = flag_titles(titles)

    # Add human-readable dur_str to each title
    def _dur(secs):
        if not secs:
            return '?:??:??'
        secs = int(secs)
        h, rem = divmod(secs, 3600)
        m, s   = divmod(rem, 60)
        return f'{h}:{m:02d}:{s:02d}'

    for t in titles:
        t['dur_str'] = _dur(t['dur_secs'])

    disc_id = None
    if ANTHROPIC_API_KEY:
        disc_id = _identify_disc_with_claude(disc_label, titles)

    return jsonify({'disc_label': disc_label, 'titles': titles, 'disc_id': disc_id})


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

    db.create_job(job_id, job_name, job_dir, disc_context=disc_context)

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
    api_key = ANTHROPIC_API_KEY
    if not api_key:
        return jsonify({'error': 'ANTHROPIC_API_KEY is not configured on this server.'}), 503

    title = db.get_title(job_id, n)
    if not title:
        return jsonify({'error': 'Title not found'}), 404

    job        = db.get_job(job_id)
    context    = _get_effective_context(job)
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


# ── Healthcheck ───────────────────────────────────────────────────────────────

@app.route('/media/health')
def health():
    return jsonify({'ok': True, 'db': str(DB_PATH)})


# ── Boot ──────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    db.init_db()
    print(f'Media Intel Dashboard → http://0.0.0.0:{PORT}/media')
    app.run(host='0.0.0.0', port=PORT, debug=False, threaded=True)
