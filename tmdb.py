"""tmdb.py — Shared TMDb (themoviedb.org) lookup utility.

Replaces screenshot-based Claude guessing for TV episode *titles*. Season
and episode *number* are already deterministic (operator-provided disc
position, see _build_classify_prompt() in app.py) -- this module only
answers "what is this episode actually called," using TMDb's published
data instead of visual inference.

Every public function here fails soft: on any error (no API key, network
failure, show not found, episode not found) the result is None, never an
exception. Callers treat None as "leave the title blank for manual entry,"
never as something to fall back to guessing for.
"""

import json
import urllib.parse
import urllib.request

TMDB_BASE = 'https://api.themoviedb.org/3'

# In-memory cache: (series_id, season) -> {episode_number: title}. A whole
# season is fetched once and reused for every title on the disc, and again
# if the same show/season is ripped again later in the same process.
_season_cache = {}
_search_cache = {}


def _get(path, params):
    from config import TMDB_API_KEY
    if not TMDB_API_KEY:
        return None
    query = dict(params)
    query['api_key'] = TMDB_API_KEY
    url = f'{TMDB_BASE}{path}?{urllib.parse.urlencode(query)}'
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def search_tv_show(name):
    """Return the best-matching TMDb TV series id for `name`, or None.
    Prefers an exact case-insensitive name match among results; otherwise
    falls back to TMDb's own top-ranked result. No user-facing
    disambiguation -- good enough for the common case, never guesses a
    title, only which show's data to pull from.
    """
    if not name:
        return None
    if name in _search_cache:
        return _search_cache[name]

    data = _get('/search/tv', {'query': name})
    results = (data or {}).get('results') or []
    if not results:
        _search_cache[name] = None
        return None

    exact = next((r for r in results if (r.get('name') or '').strip().lower() == name.strip().lower()), None)
    chosen = exact or results[0]
    series_id = chosen.get('id')
    _search_cache[name] = series_id
    return series_id


def get_season_episodes(series_id, season):
    """Return {episode_number: episode_name} for one season, or {} if
    unavailable. Cached per (series_id, season) for the process lifetime.
    """
    if not series_id or not season:
        return {}
    key = (series_id, season)
    if key in _season_cache:
        return _season_cache[key]

    data = _get(f'/tv/{series_id}/season/{season}', {})
    episodes = (data or {}).get('episodes') or []
    result = {
        ep['episode_number']: ep['name']
        for ep in episodes
        if ep.get('episode_number') is not None and ep.get('name')
    }
    _season_cache[key] = result
    return result


def lookup_episode_title(show, season, episode):
    """The one function callers actually use. Returns the real episode
    title string, or None if the show/season/episode couldn't be resolved
    for any reason. Never raises, never guesses.
    """
    try:
        series_id = search_tv_show(show)
        if not series_id:
            return None
        episodes = get_season_episodes(series_id, season)
        return episodes.get(episode)
    except Exception:
        return None
