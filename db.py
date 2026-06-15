import sqlite3
import json
from pathlib import Path
from datetime import datetime, timezone
from contextlib import contextmanager

# Avoid circular import — load DB_PATH lazily
def _db_path():
    from config import DB_PATH
    return DB_PATH

SCHEMA = """
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'ingesting',
    disc_label  TEXT,
    drive       TEXT,
    review_path TEXT NOT NULL,
    title_count INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    notes       TEXT
);

CREATE TABLE IF NOT EXISTS titles (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id          TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    title_num       INTEGER NOT NULL,
    filepath        TEXT NOT NULL,
    duration_secs   REAL,
    filesize_bytes  INTEGER,
    screenshots     TEXT NOT NULL DEFAULT '[]',
    flags           TEXT NOT NULL DEFAULT '[]',
    classification  TEXT,
    classified_at   TEXT,
    UNIQUE(job_id, title_num)
);

CREATE INDEX IF NOT EXISTS idx_titles_job ON titles(job_id);
"""


def _connect():
    conn = sqlite3.connect(str(_db_path()))
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    conn.execute('PRAGMA journal_mode = WAL')
    return conn


@contextmanager
def _db():
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with _db() as conn:
        conn.executescript(SCHEMA)


def _now():
    return datetime.now(timezone.utc).isoformat()


# ── Jobs ────────────────────────────────────────────────────────────────────

def create_job(job_id, name, review_path, drive='', disc_label=''):
    now = _now()
    with _db() as conn:
        conn.execute(
            '''INSERT OR IGNORE INTO jobs
               (id, name, status, disc_label, drive, review_path, title_count, created_at, updated_at)
               VALUES (?,?,?,?,?,?,0,?,?)''',
            (job_id, name, 'ingesting', disc_label, drive, str(review_path), now, now)
        )


def set_job_status(job_id, status, notes=None):
    now = _now()
    with _db() as conn:
        if notes is not None:
            conn.execute(
                'UPDATE jobs SET status=?, updated_at=?, notes=? WHERE id=?',
                (status, now, notes, job_id)
            )
        else:
            conn.execute(
                'UPDATE jobs SET status=?, updated_at=? WHERE id=?',
                (status, now, job_id)
            )


def increment_title_count(job_id):
    with _db() as conn:
        conn.execute(
            'UPDATE jobs SET title_count = title_count + 1, updated_at=? WHERE id=?',
            (_now(), job_id)
        )


def get_job(job_id):
    with _db() as conn:
        row = conn.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        return dict(row) if row else None


def get_all_jobs():
    with _db() as conn:
        rows = conn.execute('SELECT * FROM jobs ORDER BY created_at DESC').fetchall()
        return [dict(r) for r in rows]


def delete_job(job_id):
    with _db() as conn:
        conn.execute('DELETE FROM jobs WHERE id=?', (job_id,))


# ── Titles ───────────────────────────────────────────────────────────────────

def add_title(job_id, title_num, filepath, duration_secs=None,
              filesize_bytes=None, screenshots=None, flags=None):
    screenshots = screenshots or []
    flags       = flags or []
    with _db() as conn:
        conn.execute(
            '''INSERT OR REPLACE INTO titles
               (job_id, title_num, filepath, duration_secs, filesize_bytes,
                screenshots, flags)
               VALUES (?,?,?,?,?,?,?)''',
            (job_id, title_num, str(filepath), duration_secs, filesize_bytes,
             json.dumps(screenshots), json.dumps(flags))
        )
    increment_title_count(job_id)


def update_title(job_id, title_num, **kwargs):
    """Update specific fields on a title."""
    allowed = {'duration_secs', 'filesize_bytes', 'screenshots', 'flags', 'filepath'}
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    if not fields:
        return
    # JSON-encode list fields
    for k in ('screenshots', 'flags'):
        if k in fields and isinstance(fields[k], list):
            fields[k] = json.dumps(fields[k])
    sets = ', '.join(f'{k}=?' for k in fields)
    vals = list(fields.values()) + [job_id, title_num]
    with _db() as conn:
        conn.execute(
            f'UPDATE titles SET {sets} WHERE job_id=? AND title_num=?', vals
        )


def get_titles(job_id):
    with _db() as conn:
        rows = conn.execute(
            'SELECT * FROM titles WHERE job_id=? ORDER BY title_num', (job_id,)
        ).fetchall()
    result = []
    for r in rows:
        t = dict(r)
        t['screenshots']    = json.loads(t['screenshots'])
        t['flags']          = json.loads(t['flags'])
        t['classification'] = json.loads(t['classification']) if t['classification'] else None
        result.append(t)
    return result


def get_title(job_id, title_num):
    with _db() as conn:
        row = conn.execute(
            'SELECT * FROM titles WHERE job_id=? AND title_num=?',
            (job_id, title_num)
        ).fetchone()
    if not row:
        return None
    t = dict(row)
    t['screenshots']    = json.loads(t['screenshots'])
    t['flags']          = json.loads(t['flags'])
    t['classification'] = json.loads(t['classification']) if t['classification'] else None
    return t


def save_classification(job_id, title_num, classification):
    now = _now() if classification else None
    val = json.dumps(classification) if classification else None
    with _db() as conn:
        conn.execute(
            '''UPDATE titles SET classification=?, classified_at=?
               WHERE job_id=? AND title_num=?''',
            (val, now, job_id, title_num)
        )


def get_classification_export(job_id):
    """Return the classification JSON dict suitable for export."""
    titles = get_titles(job_id)
    out = {}
    for t in titles:
        key = f"title_{t['title_num']}"
        out[key] = t['classification'] or {'type': 'unknown'}
    return out
