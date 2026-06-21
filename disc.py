#!/usr/bin/env python3
"""disc.py — Shared disc utilities for the Media Intel pipeline.

All logic here must remain identical to the riptv CLI's behaviour so that
the web-driven workflow produces the same results as the terminal workflow.
"""

import re
import subprocess
from pathlib import Path

# REVIEW_DIR is imported lazily inside functions to avoid circular-import
# issues when disc.py is imported early.


def detect_drives():
    """Return list of dicts for each /dev/srN that exists.

    Each dict has:
      device    — '/dev/srN'
      has_media — True if a disc is currently in the drive
                  (read from /sys/block/srN/size; 0 = no disc)
    Drives with media sort first so the dropdown defaults to the right one.
    """
    drives = []
    for i in range(4):
        p = Path(f'/dev/sr{i}')
        if not p.exists():
            continue
        try:
            has_media = int(Path(f'/sys/block/sr{i}/size').read_text().strip()) > 0
        except Exception:
            has_media = False
        drives.append({'device': str(p), 'has_media': has_media})
    drives.sort(key=lambda d: (0 if d['has_media'] else 1))
    return drives


# ── MakeMKV parsing ──────────────────────────────────────────────────────────

def scan_disc(device):
    """Run makemkvcon and return (titles, disc_label).

    Tries robot mode first for structured output; if that yields 0 titles
    (common with region-mismatched DVDs), falls back to human-readable mode
    which uses a different code path that handles the region workaround more
    reliably.
    """
    result = subprocess.run(
        ['makemkvcon', '-r', 'info', f'dev:{device}'],
        capture_output=True, text=True
    )
    titles, disc_label = _parse_makemkv_output(result.stdout + result.stderr)

    if not titles:
        # Robot mode produced nothing — try without -r
        result2 = subprocess.run(
            ['makemkvcon', 'info', f'dev:{device}'],
            capture_output=True, text=True
        )
        titles = _parse_plain_output(result2.stdout + result2.stderr)

    # Last-resort label via blkid if robot mode never gave us one
    if disc_label == 'Unknown Disc':
        try:
            r = subprocess.run(
                ['blkid', device, '-o', 'value', '-s', 'LABEL'],
                capture_output=True, text=True
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

def rip_title(device, title_num, output_dir):
    """Run makemkvcon to rip a single title.

    output_dir should be a Path.  Returns Path to the produced MKV, or None
    on failure.
    """
    output_dir = Path(output_dir)
    result = subprocess.run(
        ['makemkvcon', 'mkv', f'dev:{device}', str(title_num), str(output_dir)],
        capture_output=False
    )
    if result.returncode != 0:
        return None

    mkv_files = sorted(output_dir.glob('*.mkv'))
    if not mkv_files:
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
