import os
from pathlib import Path

BASE_DIR = Path(os.path.dirname(os.path.abspath(__file__)))
MEDIA_ROOT = Path('/srv/media')

REVIEW_DIR   = MEDIA_ROOT / 'review'
INBOX_DIR    = MEDIA_ROOT / 'inbox'
TV_DIR       = MEDIA_ROOT / 'tv'
MOVIES_DIR   = MEDIA_ROOT / 'movies'
SPECIALS_DIR = MEDIA_ROOT / 'specials'
QUEUE_DIR    = MEDIA_ROOT / 'queue'
STAGING_DIR  = MEDIA_ROOT / 'staging'

DB_PATH = BASE_DIR / 'mediaintel.db'
PORT    = 8088

ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')
ANTHROPIC_MODEL   = 'claude-haiku-4-5-20251001'

# HandBrake preset used for all encodes
HB_PRESET   = 'HQ 480p30 Surround'
HB_FILTERS  = ['--comb-detect', '--decomb']
HB_AUDIO    = ['--aencoder', 'copy:ac3', '--audio-fallback', 'av_aac']
