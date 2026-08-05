#!/usr/bin/env python3
"""disc.py — Shared disc utilities for the Media Intel pipeline.

All logic here must remain identical to the riptv CLI's behaviour so that
the web-driven workflow produces the same results as the terminal workflow.
"""

import re
import sqlite3
import json
import time
import shutil
import select
import threading
import subprocess
from pathlib import Path

# REVIEW_DIR is imported lazily inside functions to avoid circular-import
# issues when disc.py is imported early.


# Friendly names for the two physical drives, matching the persistent udev
# symlinks in /etc/udev/rules.d/99-optical-drives.rules (keyed by each
# drive's ID_SERIAL, so the name follows the physical drive even if the
# kernel assigns it a different /dev/srN across boots).
_FRIENDLY_DRIVE_NAMES = {
    '/dev/lower_opt': 'LOWER OPT',   # ATAPI iHAS124
    '/dev/upper_opt': 'UPPER OPT',   # Plextor PX-891SAF
}


def _friendly_drive_label(device):
    try:
        real = Path(device).resolve()
    except OSError:
        return None
    for symlink, label in _FRIENDLY_DRIVE_NAMES.items():
        p = Path(symlink)
        if p.exists() and p.resolve() == real:
            return label
    return None


_CDROM_ID = '/lib/udev/cdrom_id'


def _has_media(device):
    """True if a disc is actually in the drive right now.

    Uses udev's cdrom_id helper (the authoritative CDROM_DRIVE_STATUS-based
    check) rather than /sys/block/srN/size -- that sysfs value was found to
    go stale after an eject on at least one of these drives, still reporting
    the last disc's size long after the tray was actually empty, which meant
    the UI kept showing "disc detected" on an empty drive.
    """
    try:
        result = subprocess.run([_CDROM_ID, device], capture_output=True, text=True, timeout=10)
        return 'ID_CDROM_MEDIA=1' in result.stdout
    except Exception:
        # cdrom_id missing or errored -- fall back to the old (less reliable
        # but better than nothing) sysfs heuristic rather than crashing.
        try:
            return int(Path(f'/sys/block/{Path(device).name}/size').read_text().strip()) > 0
        except Exception:
            return False


def detect_drives():
    """Return list of dicts for each /dev/srN that exists.

    Each dict has:
      device    — '/dev/srN'
      label     — friendly name ('UPPER OPT' / 'LOWER OPT') if this drive has
                  a persistent udev symlink, else falls back to device
      has_media — True if a disc is currently in the drive (see _has_media)
    Drives with media sort first so the dropdown defaults to the right one.
    """
    drives = []
    for i in range(4):
        p = Path(f'/dev/sr{i}')
        if not p.exists():
            continue
        label = _friendly_drive_label(str(p)) or str(p)
        drives.append({'device': str(p), 'label': label, 'has_media': _has_media(str(p))})
    drives.sort(key=lambda d: (0 if d['has_media'] else 1))
    return drives


# ── MakeMKV parsing ──────────────────────────────────────────────────────────

def check_makemkv_version():
    """Check whether makemkvcon is refusing to run because it's too old.

    Uses 'disc:9999' (a nonexistent virtual disc id) so this never touches a
    real drive -- safe to call anytime, even mid-rip, and doesn't require a
    disc to be present.

    Returns (ok, message). ok is False only when makemkvcon's own version
    gate (MSG:5021) fires; any other failure (missing binary, timeout) is
    treated as "can't tell" rather than a false alarm, so ok stays True.
    """
    try:
        result = subprocess.run(
            ['makemkvcon', '-r', 'info', 'disc:9999'],
            capture_output=True, text=True, timeout=20
        )
    except Exception:
        return True, ''

    output = result.stdout + result.stderr
    if 'MSG:5021' in output:
        return False, ('MakeMKV is too old and is refusing to scan or rip discs. '
                        'Update it to restore ripping.')
    return True, ''


SCAN_IDLE_TIMEOUT = 45    # seconds of ZERO new output before we consider it truly stuck
SCAN_MAX_TIMEOUT  = 600   # seconds -- hard backstop even if it's still actively producing output
SCAN_RETRY_DELAY  = 15    # seconds -- a scan launched right after insert can wedge on a drive that
                          # hasn't finished spinning up/settling yet, even on a perfectly healthy
                          # disc; the identical command reliably succeeds in well under 30s once the
                          # drive has settled, so one retry after a short pause recovers most of these
                          # automatically instead of forcing the user to notice the error and re-click.
SCAN_MAX_ATTEMPTS = 4     # some drive/disc pairings are inconsistent run to run (succeed once, stall
                          # the next, no explanation in dmesg either time) -- a single retry wasn't
                          # reliably enough to absorb that.


def _run_makemkvcon_once(args):
    """Run args, killing the process only if it goes SCAN_IDLE_TIMEOUT seconds
    without producing any new output (with SCAN_MAX_TIMEOUT as a hard backstop
    regardless of activity).

    A flat total-duration timeout was killing perfectly healthy scans on
    structurally complex discs -- one real disc took 130+ seconds and was
    still actively enumerating legitimate content (180+ tiny menu/scene-select
    titles) with zero errors when a flat 120s timeout cut it off. An idle-based
    timeout lets that kind of scan run to completion while still catching a
    genuinely stuck process (observed in practice to produce literally zero
    output for the entire hang) just as fast, often faster.
    """
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    output_chunks = []
    start_time = last_output_time = time.time()

    try:
        while True:
            ready, _, _ = select.select([proc.stdout], [], [], 1.0)
            if ready:
                line = proc.stdout.readline()
                if line:
                    output_chunks.append(line)
                    last_output_time = time.time()

            if proc.poll() is not None:
                remaining = proc.stdout.read()
                if remaining:
                    output_chunks.append(remaining)
                break

            now = time.time()
            if now - last_output_time > SCAN_IDLE_TIMEOUT:
                proc.kill()
                proc.wait()
                raise TimeoutError(
                    f'MakeMKV produced no output for {SCAN_IDLE_TIMEOUT}s -- the disc may be '
                    f'having trouble reading.'
                )
            if now - start_time > SCAN_MAX_TIMEOUT:
                proc.kill()
                proc.wait()
                raise TimeoutError(
                    f'MakeMKV did not finish within {SCAN_MAX_TIMEOUT}s even though it was still '
                    f'producing output -- this disc may have an unusually complex structure.'
                )
    except BaseException:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        raise

    return_code = proc.wait()
    output = ''.join(output_chunks)
    return subprocess.CompletedProcess(args, return_code, stdout=output, stderr='')


def _run_makemkvcon(args):
    """_run_makemkvcon_once(args), retrying up to SCAN_MAX_ATTEMPTS times
    (with SCAN_RETRY_DELAY between tries) if it times out (idle or max).
    Raises RuntimeError only once every attempt has timed out.

    Some drive/disc pairings are genuinely inconsistent run to run -- seen in
    practice succeeding cleanly on one attempt and stalling with zero output
    on the very next, with nothing in dmesg to explain the difference. A
    single retry wasn't enough to reliably absorb that, so this tries several
    times before giving up.
    """
    for attempt in range(1, SCAN_MAX_ATTEMPTS + 1):
        try:
            return _run_makemkvcon_once(args)
        except TimeoutError as e:
            if attempt == SCAN_MAX_ATTEMPTS:
                raise RuntimeError(
                    f'{e} Tried {SCAN_MAX_ATTEMPTS} times -- try ejecting and reinserting the disc.'
                )
            time.sleep(SCAN_RETRY_DELAY)


def scan_disc(device):
    """Run makemkvcon and return (titles, disc_label).

    Tries robot mode first for structured output; if that yields 0 titles
    (common with region-mismatched DVDs), falls back to human-readable mode
    which uses a different code path that handles the region workaround more
    reliably.

    Raises RuntimeError if makemkvcon goes SCAN_IDLE_TIMEOUT seconds with zero
    output twice in a row (see _run_makemkvcon) -- seen in practice with
    certain discs that stall indefinitely producing nothing at all, often just
    from scanning too soon after insert. A disc that's merely slow but still
    actively producing output (e.g. one with hundreds of tiny menu/scene-select
    titles) is not affected by this and gets up to SCAN_MAX_TIMEOUT to finish.
    """
    result = _run_makemkvcon(['makemkvcon', '-r', 'info', f'dev:{device}'])
    titles, disc_label = _parse_makemkv_output(result.stdout + result.stderr)

    if not titles:
        # Robot mode produced nothing — try without -r
        result2 = _run_makemkvcon(['makemkvcon', 'info', f'dev:{device}'])
        titles = _parse_plain_output(result2.stdout + result2.stderr)

    # Last-resort label via blkid if robot mode never gave us one
    if disc_label == 'Unknown Disc':
        try:
            r = subprocess.run(
                ['blkid', device, '-o', 'value', '-s', 'LABEL'],
                capture_output=True, text=True, timeout=15
            )
            label = r.stdout.strip()
            if label:
                disc_label = label
        except Exception:
            pass

    return titles, disc_label


def _parse_makemkv_output(output):
    titles     = {}
    disc_label = 'Unknown Disc'

    for line in output.splitlines():
        # Disc label from DRV line (format: DRV:idx,status,flags,drives,"drive","label","path")
        m = re.match(r'^DRV:\d+,\d+,\d+,\d+,"[^"]*","([^"]+)"', line)
        if m and m.group(1).strip():
            disc_label = m.group(1).strip()
            continue

        # Disc label from CINFO (type 2 = label)
        m = re.match(r'^CINFO:2,0,"([^"]+)"', line)
        if m and m.group(1).strip() not in ('', 'DVD', 'DVD_VIDEO'):
            disc_label = m.group(1).strip()
            continue

        # Title attributes
        m = re.match(r'^TINFO:(\d+),(\d+),(\d+),"(.*)"$', line)
        if m:
            tnum = int(m.group(1))
            attr = int(m.group(2))
            val  = m.group(4)
            if tnum not in titles:
                titles[tnum] = {}
            titles[tnum][attr] = val

    result = []
    for tnum in sorted(titles.keys()):
        attrs = titles[tnum]

        # Duration: find any attr value matching H:MM:SS
        dur_str  = None
        dur_secs = 0
        for val in attrs.values():
            mm = re.match(r'^(\d+):(\d{2}):(\d{2})$', val)
            if mm:
                h, m_, s = int(mm.group(1)), int(mm.group(2)), int(mm.group(3))
                dur_secs = h * 3600 + m_ * 60 + s
                dur_str  = val
                break

        result.append({
            'num':      tnum,
            'dur_str':  dur_str or '?:??:??',
            'dur_secs': dur_secs,
            'flags':    [],
        })

    return result, disc_label


def _parse_plain_output(output):
    """Parse human-readable makemkvcon output (no -r flag).

    Extracts titles from lines like:
      Title #1 was added (39 cell(s), 2:06:34)

    MakeMKV reports title numbers 1-indexed in human mode but indexes them
    0-based in the TINFO/rip commands, so we subtract 1 to stay consistent.
    """
    result = []
    for line in output.splitlines():
        m = re.match(r'^Title #(\d+) was added \(.*?(\d+:\d{2}:\d{2})\)', line)
        if m:
            tnum = int(m.group(1)) - 1   # convert to 0-indexed
            h, mi, s = (int(x) for x in m.group(2).split(':'))
            result.append({
                'num':      tnum,
                'dur_str':  m.group(2),
                'dur_secs': h * 3600 + mi * 60 + s,
                'flags':    [],
            })
    return result


# ── Flagging ─────────────────────────────────────────────────────────────────

def flag_titles(titles):
    """Add play_all / short / no_duration / possible_duplicate flags.

    Thresholds are identical to the riptv CLI.
    """
    valid = [t['dur_secs'] for t in titles if t['dur_secs'] > 0]
    if not valid:
        return titles

    median = sorted(valid)[len(valid) // 2]

    for t in titles:
        d = t['dur_secs']
        flags = []

        # Play-All: > 1.5× median and > 20 min
        if d > 1.5 * median and d > 1200:
            flags.append('play_all')

        # Short: < 15 minutes (likely bonus/trailer)
        if 0 < d < 900:
            flags.append('short')

        if d == 0:
            flags.append('no_duration')

        # Near-duplicate duration
        dupes = sum(
            1 for t2 in titles
            if t2['num'] != t['num'] and abs(t2['dur_secs'] - d) < 30
        )
        if dupes:
            flags.append('possible_duplicate')

        t['flags'] = flags

    return titles


# ── Ripping ──────────────────────────────────────────────────────────────────

def _write_rip_progress(job_id, title_num, pct, op):
    try:
        from config import DB_PATH
        data = json.dumps({'title_num': title_num, 'pct': pct, 'op': op, 'ts': time.time()})
        c = sqlite3.connect(str(DB_PATH))
        c.execute('PRAGMA journal_mode = WAL')
        c.execute('UPDATE jobs SET rip_progress=? WHERE id=?', (data, job_id))
        c.commit()
        c.close()
    except Exception:
        pass


def _clear_rip_progress(job_id):
    try:
        from config import DB_PATH
        c = sqlite3.connect(str(DB_PATH))
        c.execute('PRAGMA journal_mode = WAL')
        c.execute('UPDATE jobs SET rip_progress=NULL WHERE id=?', (job_id,))
        c.commit()
        c.close()
    except Exception:
        pass


def rip_title(device, title_num, output_dir, job_id=None):
    """Run makemkvcon to rip a single title.

    output_dir should be a Path.  Returns Path to the produced MKV, or None
    on failure.  Pass job_id to enable live progress tracking in the dashboard.
    """
    output_dir = Path(output_dir)

    # stdbuf forces line-buffering on makemkvcon so PRGV lines arrive promptly
    cmd = ['makemkvcon', '-r', 'mkv', f'dev:{device}', str(title_num), str(output_dir)]
    if shutil.which('stdbuf'):
        cmd = ['stdbuf', '-oL'] + cmd

    if job_id:
        _write_rip_progress(job_id, title_num, 0, 'Starting…')

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1
    )

    prgv_seen   = [False]
    stop_event  = threading.Event()
    start_time  = time.time()

    # Fallback: poll the growing output file so the dashboard always shows something
    def _size_poll():
        ESTIMATE = 25 * 60  # ~25 min cap; bar stops advancing at 95%
        while not stop_event.wait(2):
            if prgv_seen[0]:
                return  # PRGV is working — hand off
            try:
                mkv_files = sorted(output_dir.glob('*.mkv'))
                if not mkv_files:
                    continue
                size_gb = mkv_files[0].stat().st_size / (1024 ** 3)
                elapsed = time.time() - start_time
                pct     = min(int(elapsed / ESTIMATE * 100), 95)
                _write_rip_progress(job_id, title_num, pct,
                                    f'Ripping… {size_gb:.1f} GB written')
            except Exception:
                pass

    if job_id:
        threading.Thread(target=_size_poll, daemon=True).start()

    last_write = 0.0
    pct = 0
    op  = ''

    for raw in proc.stdout:
        line = raw.rstrip()

        m = re.match(r'^PRGV:(\d+),\d+,(\d+)', line)
        if m:
            cur, mx = int(m.group(1)), int(m.group(2))
            pct = int(cur * 100 / mx) if mx else 0
            prgv_seen[0] = True
            if job_id:
                now = time.time()
                if now - last_write >= 3:
                    _write_rip_progress(job_id, title_num, pct, op)
                    last_write = now
            continue

        m = re.match(r'^PRG[CT]:\d+,\d+,"([^"]*)"', line)
        if m:
            op = m.group(1)

    proc.wait()
    stop_event.set()

    if proc.returncode != 0:
        if job_id:
            _clear_rip_progress(job_id)
        return None

    mkv_files = sorted(output_dir.glob('*.mkv'))
    if not mkv_files:
        if job_id:
            _clear_rip_progress(job_id)
        return None

    return mkv_files[0]


# ── Post-processing ──────────────────────────────────────────────────────────

def get_duration(mkv_path):
    """Return ffprobe-measured duration in seconds (float)."""
    r = subprocess.run(
        ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
         '-of', 'csv=p=0', str(mkv_path)],
        capture_output=True, text=True
    )
    try:
        return float(r.stdout.strip())
    except (ValueError, AttributeError):
        return 0.0


def take_screenshots(mkv_path, output_dir, duration_secs):
    """Generate 5 screenshots evenly distributed across the episode.

    Timestamps are spread from 10% to 90% of duration so we sample the
    whole episode rather than just the first 15 minutes. This gives Claude
    much better coverage for episode identification.

    Returns a list of path strings relative to REVIEW_DIR.
    """
    from config import REVIEW_DIR  # lazy import — avoids circular deps

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = []

    if not duration_secs or duration_secs < 60:
        return paths

    # 5 evenly-spaced samples from 10% to 90% of the runtime
    count = 5
    timestamps = [
        int(duration_secs * (i + 1) / (count + 1))
        for i in range(count)
    ]

    for ts in timestamps:
        out = output_dir / f'thumb_{ts}.jpg'
        r = subprocess.run(
            ['ffmpeg', '-y', '-ss', str(ts), '-i', str(mkv_path),
             '-frames:v', '1', '-q:v', '2', str(out)],
            capture_output=True
        )
        if r.returncode == 0 and out.exists():
            paths.append(str(out.relative_to(REVIEW_DIR)))

    return paths
