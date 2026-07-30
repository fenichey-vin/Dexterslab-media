import os
from pathlib import Path

BASE_DIR = Path(os.path.dirname(os.path.abspath(__file__)))

MEDIA_ROOT = Path('/srv/media')
WORK_ROOT  = Path('/srv/work')

REVIEW_DIR   = WORK_ROOT / 'review'
INBOX_DIR    = WORK_ROOT / 'inbox'
QUEUE_DIR    = WORK_ROOT / 'queue'
STAGING_DIR  = WORK_ROOT / 'staging'

TV_DIR       = MEDIA_ROOT / 'tv'
MOVIES_DIR   = MEDIA_ROOT / 'movies'
SPECIALS_DIR = MEDIA_ROOT / 'specials'

DB_PATH = BASE_DIR / 'mediaintel.db'
PORT    = 8088

ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')
ANTHROPIC_MODEL   = 'claude-haiku-4-5-20251001'

# Used to look up real TV episode titles by season/episode number instead of
# guessing them from screenshots (see tmdb.py). Free key from themoviedb.org.
TMDB_API_KEY = os.environ.get('TMDB_API_KEY', '')

# HandBrake preset used for all encodes
HB_PRESET   = 'HQ 480p30 Surround'
HB_FILTERS  = ['--comb-detect', '--decomb']
HB_AUDIO    = ['--aencoder', 'copy:ac3', '--audio-fallback', 'av_aac']
