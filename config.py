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

# Separate quarantine tree for TV episodes whose episode number/title was
# never independently confirmed by dialogue transcript matching -- only a
# manually-typed guess or disc-position lookup. Point Jellyfin at this as
# its OWN separate library (e.g. "TV Shows (Needs Review)") so unverified
# guesses never sit in the same show/season folder as confirmed episodes.
# Human watches it here, then Confirm & Move to Library on the dashboard
# moves the file into TV_DIR proper once satisfied it's actually correct.
TV_REVIEW_DIR = MEDIA_ROOT / 'tv-needs-review'

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
# NOTE (2026-09-05): explicitly select audio by LANGUAGE, not track position.
# HandBrake's audio-track selection without an explicit --audio flag defaults
# to whatever is FIRST in the source stream order -- fine for most discs, but
# region-coded pressings can put a foreign dub before the English track (The
# Office S02 raw rip: track1/2=Russian AC3, track3=English eac3). Without
# this, every episode on that disc silently encoded with Russian-only audio,
# and the transcript-verification step failed 22/22 titles as a knock-on
# effect (it was transcribing Russian dialogue and matching against English
# --audio-lang-list restricts HandBrake's audio scan to English;
# --first-audio picks that language's first matching track rather than
# grabbing every English track found (some discs tag more than one).
HB_AUDIO    = ['--aencoder', 'copy:ac3', '--audio-fallback', 'av_aac',
               '--audio-lang-list', 'eng', '--first-audio']
