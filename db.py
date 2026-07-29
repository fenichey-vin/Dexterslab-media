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
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'ingesting',
    disc_label      TEXT,
    drive           TEXT,
    review_path     TEXT NOT NULL,
    title_count     INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    notes           TEXT,
    disc_context    TEXT,
    rip_progress    TEXT,
    encode_progress TEXT
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
    staged_path     TEXT,
    UNIQUE(job_id, title_num)
);

CREATE INDEX IF NOT EXISTS idx_titles_job ON titles(job_id);

CREATE TABLE IF NOT EXISTS catalog (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id          TEXT,
    title_num       INTEGER NOT NULL,
    state           TEXT NOT NULL DEFAULT 'pending',
    type            TEXT NOT NULL,
    show            TEXT,
    season          INTEGER,
    episode         INTEGER,
    episode_title   TEXT,
    movie_title     TEXT,
    year            INTEGER,
    label           TEXT NOT NULL,
    disc_label      TEXT,
    library_path    TEXT NOT NULL,
    duration_secs   REAL,
    filesize_bytes  INTEGER,
    classification  TEXT NOT NULL,
    committed_at    TEXT NOT NULL,
    verified_at     TEXT,
    cleaned_at      TEXT,
    fail_reason     TEXT,
    source          TEXT NOT NULL DEFAULT 'mediaintel_job',
    UNIQUE(job_id, title_num),
    UNIQUE(library_path)
);

CREATE INDEX IF NOT EXISTS idx_catalog_show  ON catalog(show);
CREATE INDEX IF NOT EXISTS idx_catalog_movie ON catalog(movie_title);
CREATE INDEX IF NOT EXISTS idx_catalog_state ON catalog(state);
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


def _migrate_catalog_job_id_nullable(conn):
    """One-time table rebuild: relax catalog.job_id from NOT NULL to nullable,
    so library_import rows (which have no owning job) can be stored. SQLite
    has no ALTER COLUMN, so this is the standard rebuild-and-swap. Guarded on
    the live column's own notnull flag, so calling init_db() again (or on a
    fresh install where SCHEMA already created job_id nullable) is a no-op.
    """
    cols = conn.execute("PRAGMA table_info(catalog)").fetchall()
    job_id_col = next((c for c in cols if c[1] == 'job_id'), None)
    if job_id_col is None or job_id_col[3] == 0:
        return  # column missing (shouldn't happen) or already nullable

    conn.executescript("""
        CREATE TABLE catalog_new (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id          TEXT,
            title_num       INTEGER NOT NULL,
            state           TEXT NOT NULL DEFAULT 'pending',
            type            TEXT NOT NULL,
            show            TEXT,
            season          INTEGER,
            episode         INTEGER,
            episode_title   TEXT,
            movie_title     TEXT,
            year            INTEGER,
            label           TEXT NOT NULL,
            disc_label      TEXT,
            library_path    TEXT NOT NULL,
            duration_secs   REAL,
            filesize_bytes  INTEGER,
            classification  TEXT NOT NULL,
            committed_at    TEXT NOT NULL,
            verified_at     TEXT,
            cleaned_at      TEXT,
            fail_reason     TEXT,
            source          TEXT NOT NULL DEFAULT 'mediaintel_job',
            UNIQUE(job_id, title_num),
            UNIQUE(library_path)
        );
        INSERT INTO catalog_new
            (id, job_id, title_num, state, type, show, season, episode,
             episode_title, movie_title, year, label, disc_label, library_path,
             duration_secs, filesize_bytes, classification, committed_at,
             verified_at, cleaned_at, fail_reason, source)
        SELECT
            id, job_id, title_num, state, type, show, season, episode,
            episode_title, movie_title, year, label, disc_label, library_path,
            duration_secs, filesize_bytes, classification, committed_at,
            verified_at, cleaned_at, fail_reason, COALESCE(source, 'mediaintel_job')
        FROM catalog;
        DROP TABLE catalog;
        ALTER TABLE catalog_new RENAME TO catalog;
        CREATE INDEX IF NOT EXISTS idx_catalog_show  ON catalog(show);
        CREATE INDEX IF NOT EXISTS idx_catalog_movie ON catalog(movie_title);
        CREATE INDEX IF NOT EXISTS idx_catalog_state ON catalog(state);
        UPDATE sqlite_sequence SET seq = (SELECT COALESCE(MAX(id), 0) FROM catalog)
            WHERE name = 'catalog';
    """)


def init_db():
    with _db() as conn:
        conn.executescript(SCHEMA)
        for col, definition in [
            ('disc_context', 'TEXT'),
            ('staged_path',  'TEXT'),
        ]:
            try:
                conn.execute(f'ALTER TABLE titles ADD COLUMN {col} {definition}')
            except Exception:
                pass
        try:
            conn.execute('ALTER TABLE jobs ADD COLUMN disc_context TEXT')
        except Exception:
            pass
        try:
            conn.execute('ALTER TABLE jobs ADD COLUMN rip_progress TEXT')
        except Exception:
            pass
        try:
            conn.execute('ALTER TABLE jobs ADD COLUMN encode_progress TEXT')
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE catalog ADD COLUMN source TEXT NOT NULL DEFAULT 'mediaintel_job'")
        except Exception:
            pass
        _migrate_catalog_job_id_nullable(conn)
        for col in ('rip_completed_at', 'encode_started_at', 'encode_completed_at'):
            try:
                conn.execute(f'ALTER TABLE jobs ADD COLUMN {col} TEXT')
            except Exception:
                pass


def _now():
    return datetime.now(timezone.utc).isoformat()


# ── Jobs ────────────────────────────────────────────────────────────────────

def create_job(job_id, name, review_path, drive='', disc_label='', disc_context=None):
    now = _now()
    with _db() as conn:
        conn.execute(
            '''INSERT OR IGNORE INTO jobs
               (id, name, status, disc_label, drive, review_path, title_count,
                created_at, updated_at, disc_context)
               VALUES (?,?,?,?,?,?,0,?,?,?)''',
            (job_id, name, 'ingesting', disc_label, drive, str(review_path), now, now,
             json.dumps(disc_context) if disc_context else None)
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


_JOB_TIMESTAMP_FIELDS = ('rip_completed_at', 'encode_started_at', 'encode_completed_at')


def set_job_timestamp(job_id, field):
    """Permanently record when a job reached one of the three provenance
    milestones not already covered by created_at/updated_at (Phase 3). Field
    must be one of _JOB_TIMESTAMP_FIELDS -- whitelisted since it's
    interpolated into the SQL column list. Never overwritten afterward by
    set_job_status(), unlike updated_at -- this is what makes rip/encode
    duration recoverable after a job moves on to later statuses."""
    if field not in _JOB_TIMESTAMP_FIELDS:
        raise ValueError(f'invalid job timestamp field: {field}')
    with _db() as conn:
        conn.execute(f'UPDATE jobs SET {field}=? WHERE id=?', (_now(), job_id))


def increment_title_count(job_id):
    with _db() as conn:
        conn.execute(
            'UPDATE jobs SET title_count = title_count + 1, updated_at=? WHERE id=?',
            (_now(), job_id)
        )


def _parse_job(row):
    j = dict(row)
    if j.get('disc_context'):
        try:
            j['disc_context'] = json.loads(j['disc_context'])
        except Exception:
            j['disc_context'] = None
    if j.get('rip_progress'):
        try:
            j['rip_progress'] = json.loads(j['rip_progress'])
        except Exception:
            j['rip_progress'] = None
    if j.get('encode_progress'):
        try:
            j['encode_progress'] = json.loads(j['encode_progress'])
        except Exception:
            j['encode_progress'] = None
    return j


def get_job(job_id):
    with _db() as conn:
        row = conn.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        return _parse_job(row) if row else None


def get_all_jobs():
    with _db() as conn:
        rows = conn.execute('SELECT * FROM jobs ORDER BY created_at DESC').fetchall()
        return [_parse_job(r) for r in rows]


def set_rip_progress(job_id, progress):
    """Write or clear rip progress. progress=dict or None to clear."""
    with _db() as conn:
        conn.execute(
            'UPDATE jobs SET rip_progress=? WHERE id=?',
            (json.dumps(progress) if progress else None, job_id)
        )


def set_encode_progress(job_id, progress):
    """Write or clear encode progress. progress=dict or None to clear."""
    with _db() as conn:
        conn.execute(
            'UPDATE jobs SET encode_progress=? WHERE id=?',
            (json.dumps(progress) if progress else None, job_id)
        )


def set_disc_context(job_id, disc_context):
    with _db() as conn:
        conn.execute(
            'UPDATE jobs SET disc_context=?, updated_at=? WHERE id=?',
            (json.dumps(disc_context) if disc_context else None, _now(), job_id)
        )


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
    allowed = {'duration_secs', 'filesize_bytes', 'screenshots', 'flags', 'filepath', 'staged_path'}
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


# ── Catalog ──────────────────────────────────────────────────────────────────
# Permanent record of committed titles. Deliberately has no foreign key to
# jobs/titles — it's meant to outlive those rows (and the review-dir files
# they point at) once cleanup ever prunes them. See mediaintel-catalog-plan.md.

def insert_catalog_row(job_id, title_num, state, type_, show, season, episode,
                        episode_title, movie_title, year, label, disc_label,
                        library_path, duration_secs, filesize_bytes,
                        classification, committed_at, source='mediaintel_job'):
    """Insert one catalog row. Returns the new row id, or None if a row for
    this (job_id, title_num) or this library_path already exists — callers
    should treat None as 'already catalogued', not as an error.

    source defaults to 'mediaintel_job' so every existing caller (Phase 1
    backfill, app.py's _catalog_commit) is unaffected; the library importer
    is the only caller that passes source='library_import'.
    """
    with _db() as conn:
        try:
            cur = conn.execute(
                '''INSERT INTO catalog
                   (job_id, title_num, state, type, show, season, episode,
                    episode_title, movie_title, year, label, disc_label,
                    library_path, duration_secs, filesize_bytes,
                    classification, committed_at, source)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (job_id, title_num, state, type_, show, season, episode,
                 episode_title, movie_title, year, label, disc_label,
                 library_path, duration_secs, filesize_bytes,
                 classification, committed_at, source)
            )
            return cur.lastrowid
        except sqlite3.IntegrityError:
            return None


def get_catalog_row(job_id, title_num):
    with _db() as conn:
        row = conn.execute(
            'SELECT * FROM catalog WHERE job_id=? AND title_num=?',
            (job_id, title_num)
        ).fetchone()
        return dict(row) if row else None


def get_catalog_rows_by_state(states):
    """states: list of state strings, e.g. ['pending', 'committed']."""
    placeholders = ','.join('?' for _ in states)
    with _db() as conn:
        rows = conn.execute(
            f'SELECT * FROM catalog WHERE state IN ({placeholders})', states
        ).fetchall()
        return [dict(r) for r in rows]


def update_catalog_state(catalog_id, state, verified_at=None, cleaned_at=None, fail_reason=None):
    with _db() as conn:
        conn.execute(
            '''UPDATE catalog
               SET state=?, verified_at=COALESCE(?, verified_at),
                   cleaned_at=COALESCE(?, cleaned_at), fail_reason=?
               WHERE id=?''',
            (state, verified_at, cleaned_at, fail_reason, catalog_id)
        )


def get_all_catalog_rows():
    with _db() as conn:
        rows = conn.execute('SELECT * FROM catalog ORDER BY committed_at DESC').fetchall()
        return [dict(r) for r in rows]


def get_catalog_row_by_id(catalog_id):
    with _db() as conn:
        row = conn.execute('SELECT * FROM catalog WHERE id=?', (catalog_id,)).fetchone()
        return dict(row) if row else None


def get_catalog_row_by_path(library_path):
    """Look up a catalog row by its library_path (UNIQUE), regardless of
    source or state. Used by the library importer to decide already-present
    vs would-insert without inserting-and-catching — so dry-run mode can
    answer this without writing anything."""
    with _db() as conn:
        row = conn.execute(
            'SELECT * FROM catalog WHERE library_path=?', (str(library_path),)
        ).fetchone()
        return dict(row) if row else None


def get_catalog_source_counts():
    """Return {source: count} for every source present in the catalog table."""
    with _db() as conn:
        rows = conn.execute('SELECT source, COUNT(*) AS n FROM catalog GROUP BY source').fetchall()
        return {r['source']: r['n'] for r in rows}


def get_catalog_state_counts():
    """Return {state: count} for every state present in the catalog table."""
    with _db() as conn:
        rows = conn.execute('SELECT state, COUNT(*) AS n FROM catalog GROUP BY state').fetchall()
        return {r['state']: r['n'] for r in rows}
