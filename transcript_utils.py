"""Safe helpers for transcript-based media identification."""
import re


def canonical_tv_show_name(show):
    """Remove rip-label season/disc suffixes from a TV series name."""
    value = (show or '').strip()
    # Rip labels commonly repeat season tokens and append a disc number:
    # "The Sopranos S06 disc4 S06". Remove only recognized trailing tokens,
    # leaving numbered series names such as "24" untouched.
    previous = None
    while value and value != previous:
        previous = value
        value = re.sub(r'\s+(?:disc\s*\d+|s\d{1,2}|season\s+\d+)\s*$', '', value,
                       flags=re.IGNORECASE).strip()
    return value


def identifytv_failure_note(returncode, output):
    """Return a concise, non-secret operator message for identifytv failure."""
    safe = (output or '').lower()
    if 'no reference episodes' in safe or 'could not reach tvmaze' in safe:
        return ('Transcript verification failed: TVmaze found no episodes. '
                'Use only the series title in Show (for example, “Homeland”), '
                'with the season in the separate Season field.')
    return (f'Transcript verification failed (identifytv exit {int(returncode)}). '
            'Check the service log for the safe failure classification.')
