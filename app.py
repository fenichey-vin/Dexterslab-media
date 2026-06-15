#!/usr/bin/env python3
"""Media Intel Dashboard — web UI for disc ingestion review and classification."""

import os
import sys
import json
import base64
import re
import subprocess
import threading
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from flask import (Flask, render_template, request, jsonify,
                   send_from_directory, Response, redirect, url_for)
from config import (REVIEW_DIR, DB_PATH, PORT, ANTHROPIC_API_KEY, ANTHROPIC_MODEL,
                    TV_DIR, MOVIES_DIR, SPECIALS_DIR, HB_PRESET, HB_FILTERS, HB_AUDIO)
import db
from disc import detect_drives, scan_disc, flag_titles, rip_title, get_duration, take_screenshots

app = Flask(__name__, static_url_path='/media/static')
app.secret_key = 'mediaintel-change-in-production'


# ── Template filters ─────────────────────────────────────────────────────────

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
    completed = [j for j in all_jobs if j['status'] in ('complete', 'error')]
    total_titles = sum(j['title_count'] for j in all_jobs)
    return render_template('dashboard.html',
                           active=active, review=review,
                           approved=approved, encoding=encoding,
                           completed=completed, total_titles=total_titles)


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


def _encode_job_bg(job_id):
    """Background thread: HandBrake-encode all classified titles for one job."""
    job = db.get_job(job_id)
    if not job:
        return

    job_dir  = Path(job['review_path'])
    cls_file = job_dir / 'classification.json'

    if not cls_file.exists():
        db.set_job_status(job_id, 'error', notes='classification.json missing')
        return

    log_path = Path('/srv/media/encode_log.txt')

    def _log(msg):
        ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        with open(log_path, 'a') as lf:
            lf.write(f'[{ts}] {msg}\n')

    def _safe(name):
        return re.sub(r'[<>:"/\\|?*]', '', str(name)).strip('. ')

    def _route(cls, title_num):
        t = cls.get('type', 'unknown')
        if t in ('play_all', 'deleted', 'unknown'):
            return None
        if t == 'tv_episode':
            show   = _safe(cls.get('show') or 'Unknown Show')
            season = int(cls.get('season') or 1)
            ep     = int(cls.get('episode') or 0)
            etitle = _safe(cls.get('episode_title') or '')
            fname  = f'{show} - S{season:02d}{"E"+str(ep).zfill(2) if ep else ""}'
            if etitle:
                fname += f' - {etitle}'
            return TV_DIR / show / f'Season {season:02d}' / (fname + '.mkv')
        if t in ('movie', 'documentary'):
            title  = _safe(cls.get('movie_title') or cls.get('title') or 'Unknown Title')
            year   = cls.get('year')
            folder = f'{title} ({year})' if year else title
            return MOVIES_DIR / folder / f'{folder}.mkv'
        if t == 'special':
            show  = _safe(cls.get('show') or 'Misc')
            title = _safe(cls.get('title') or f'title_{title_num}')
            return SPECIALS_DIR / show / f'{title}.mkv'
        return None

    with open(cls_file) as f:
        classifications = json.load(f)

    _log(f'=== Encode start: {job["name"]} ({job_id}) ===')
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

        output_path = _route(cls, title_num)
        if output_path is None:
            continue
        if output_path.exists():
            _log(f'SKIP {job_id} {key}: already exists')
            ok += 1
            continue

        output_path.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run([
            'HandBrakeCLI', '-i', str(mkv_files[0]), '-t', '1',
            '-o', str(output_path), '--preset', HB_PRESET,
            *HB_FILTERS, *HB_AUDIO
        ])

        if result.returncode == 0:
            ok += 1
            _log(f'OK   {job_id} {key}: {output_path}')
        else:
            fail += 1
            _log(f'FAIL {job_id} {key}: HandBrakeCLI error')

    if fail == 0:
        db.set_job_status(job_id, 'complete')
    else:
        db.set_job_status(job_id, 'error', notes=f'{fail} encode failure(s)')
    _log(f'=== Encode {"complete" if fail == 0 else "error"}: {job["name"]} ok={ok} fail={fail} ===')


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

        def _dur(secs):
            if not secs:
                return '?:??:??'
            secs = int(secs)
            h, rem = divmod(secs, 3600)
            m, s   = divmod(rem, 60)
            return f'{h}:{m:02d}:{s:02d}'

        dur_str  = _dur(title['duration_secs'])
        size_str = fmt_size(title['filesize_bytes'])
        flags    = ', '.join(title['flags']) if title['flags'] else 'none'

        context_hint = ''
        if disc_context:
            if disc_context.get('type') == 'tv':
                context_hint = (
                    f'\nDisc context hint: This appears to be {disc_context.get("show","")} '
                    f'Season {disc_context.get("season","")}. '
                    f'Main episode title numbers are likely: {disc_context.get("episodes",[])}.'
                )
            elif disc_context.get('type') == 'movie':
                context_hint = (
                    f'\nDisc context hint: This appears to be the movie '
                    f'{disc_context.get("title","")} ({disc_context.get("year","")}).'
                )

        content.append({
            'type': 'text',
            'text': (
                f'These are screenshots from DVD title {title_num} I need to classify.\n'
                f'Duration: {dur_str}  |  File size: {size_str}  |  Flags: {flags}'
                f'{context_hint}\n\n'
                f'Classify this as one of: tv_episode, movie, special, documentary, play_all, or unknown.\n'
                f'- If tv_episode: identify show name and episode title/number if possible.\n'
                f'- If movie: provide title and year if visible.\n'
                f'- play_all means this is a "play all episodes" combined title to be skipped.\n\n'
                f'Reply ONLY with valid JSON:\n'
                f'{{"type":"...","show":"...","season":0,"episode":0,"episode_title":"...",'
                f'"movie_title":"...","year":0,"confidence":"high|medium|low","reasoning":"..."}}'
            )
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
    """Background thread: rip selected titles one by one, then auto-classify."""
    try:
        job = db.get_job(job_id)
        if not job:
            return
        job_dir = Path(job['review_path'])

        for title_num in title_nums:
            try:
                title_dir = job_dir / f'title_{title_num}'
                title_dir.mkdir(parents=True, exist_ok=True)

                mkv = rip_title(device, title_num, title_dir)
                if mkv is None:
                    continue

                duration    = get_duration(mkv)
                screenshots = take_screenshots(mkv, title_dir / 'screenshots', duration)

                # Recompute flags with accurate duration
                single      = flag_titles([{'num': title_num, 'dur_secs': duration, 'flags': []}])
                final_flags = single[0]['flags']

                rel_mkv = str(mkv.relative_to(REVIEW_DIR))
                db.add_title(job_id, title_num, rel_mkv,
                             duration, mkv.stat().st_size, screenshots, final_flags)

                if ANTHROPIC_API_KEY:
                    _auto_classify_one(job_id, title_num, disc_context)

            except Exception:
                pass  # log silently — never abort the whole rip

        db.set_job_status(job_id, 'review')
    except Exception:
        db.set_job_status(job_id, 'error', notes='Background rip failed unexpectedly')
    finally:
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
    data       = request.get_json(silent=True) or {}
    job_name   = data.get('job_name', '').strip()
    device     = data.get('device', '')
    title_nums = data.get('title_nums', [])
    disc_id    = data.get('disc_id')

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

    db.create_job(job_id, job_name, job_dir)

    t = threading.Thread(
        target=_rip_disc_bg,
        args=(job_id, device, title_nums, disc_id),
        daemon=True
    )
    t.start()

    return jsonify({'ok': True, 'job_id': job_id})


# ── Claude AI suggestion ──────────────────────────────────────────────────────

@app.route('/media/job/<job_id>/title/<int:n>/ask-claude', methods=['POST'])
def ask_claude(job_id, n):
    api_key = ANTHROPIC_API_KEY
    if not api_key:
        return jsonify({'error': 'ANTHROPIC_API_KEY is not configured on this server.'}), 503

    title = db.get_title(job_id, n)
    if not title:
        return jsonify({'error': 'Title not found'}), 404

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
        'text': (
            f'These are screenshots from a DVD title I need to classify.\n'
            f'Duration: {dur_str}  |  File size: {size_str}  |  Flags: {flags}\n\n'
            f'Classify this as one of: tv_episode, movie, special, documentary, play_all, or unknown.\n'
            f'- If tv_episode: identify show name and episode title/number if possible.\n'
            f'- If movie: provide title and year if visible.\n'
            f'- play_all means this is a "play all episodes" combined title to be skipped.\n\n'
            f'Reply ONLY with valid JSON:\n'
            f'{{"type":"...","show":"...","season":0,"episode":0,"episode_title":"...",'
            f'"movie_title":"...","year":0,"confidence":"high|medium|low","reasoning":"..."}}'
        )
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
