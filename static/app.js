'use strict';

// Per-title state: which form is open, which Claude suggestion is stored
const _state = {};

// ── Utilities ──────────────────────────────────────────────────────────────────

function qs(id)    { return document.getElementById(id); }
function show(el)  { el && el.classList.remove('hidden'); }
function hide(el)  { el && el.classList.add('hidden'); }

function apiPost(url, body) {
  return fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body)
  }).then(r => r.json());
}

// ── Classification form ────────────────────────────────────────────────────────

function showForm(n, type) {
  const wrap = qs(`cls-form-${n}`);
  if (!wrap) return;

  // Set hidden type field
  const typeInput = qs(`cls-type-${n}`);
  if (typeInput) typeInput.value = type;

  // Show appropriate field set, hide others
  const sets = { tv_episode: 'tv', movie: 'movie', special: 'special', documentary: 'special' };
  ['tv', 'movie', 'special'].forEach(f => {
    const el = qs(`fields-${f}-${n}`);
    if (el) el.style.display = (sets[type] === f) ? 'flex' : 'none';
  });

  show(wrap);
  hide(qs(`claude-panel-${n}`));

  // Scope prefill to the active fieldset only — multiple fieldsets share the
  // same input names (e.g. both TV and Special have name="show"), so querying
  // the whole form returns the first match, which is always the TV fieldset.
  const activeFieldset = qs(`fields-${sets[type]}-${n}`);
  const sugg = (_state[n] || {}).claudeSuggestion;
  if (sugg && sugg.type === type) {
    _prefillForm(activeFieldset, sugg);
  } else {
    const existingRaw = wrap && wrap.dataset.cls;
    if (existingRaw) {
      try { _prefillForm(activeFieldset, JSON.parse(existingRaw)); } catch(e) {}
    }
  }

  wrap.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function hideForm(n) {
  hide(qs(`cls-form-${n}`));
}

function _prefillForm(container, data) {
  if (!container) return;
  const set = f => { const el = container.querySelector(`[name="${f}"]`); if (el && data[f] != null) el.value = data[f]; };
  set('show'); set('season'); set('episode'); set('episode_title');
  set('movie_title'); set('year'); set('title');

  // For specials saved before 'show' was included: derive it from the title
  // by stripping the trailing "- Special Features N" suffix.
  const showEl = container.querySelector('[name="show"]');
  if (showEl && !showEl.value && data.title) {
    const m = data.title.match(/^(.+?)\s*-\s*Special Features\s*\d*\s*$/i);
    if (m) showEl.value = m[1].trim();
  }
}

function submitClassification(event, n) {
  event.preventDefault();
  const form  = event.target;
  const type  = (qs(`cls-type-${n}`) || {}).value;
  if (!type) return;

  const data = { type };
  const collect = name => {
    const el = form.querySelector(`[name="${name}"]`);
    if (el && el.value.trim()) {
      const v = el.value.trim();
      data[name] = isNaN(v) || name === 'show' || name === 'episode_title' || name === 'movie_title' || name === 'title'
        ? v : Number(v);
    }
  };
  ['show','season','episode','episode_title','movie_title','year','title'].forEach(collect);

  apiPost(`/media/job/${JOB_ID}/title/${n}/classify`, data)
    .then(r => {
      if (r.ok) {
        _updateDisplay(n, data);
        hideForm(n);
        _updateProgress();
        const wrap = qs(`cls-form-${n}`);
        if (wrap) wrap.dataset.cls = JSON.stringify(data);
      } else {
        alert('Save failed: ' + (r.error || 'unknown error'));
      }
    })
    .catch(e => alert('Network error: ' + e));
}

// Quick one-click types that need no extra fields
function quickClassify(n, type) {
  const data = { type };
  apiPost(`/media/job/${JOB_ID}/title/${n}/classify`, data)
    .then(r => {
      if (r.ok) {
        _updateDisplay(n, data);
        hideForm(n);
        hide(qs(`claude-panel-${n}`));
        _updateProgress();
      }
    })
    .catch(e => alert('Network error: ' + e));
}

// ── Claude integration ─────────────────────────────────────────────────────────

function askClaude(n) {
  const spinner = qs(`claude-spinner-${n}`);
  const panel   = qs(`claude-panel-${n}`);
  const display = qs(`claude-suggestion-${n}`);

  show(spinner);
  hide(panel);

  apiPost(`/media/job/${JOB_ID}/title/${n}/ask-claude`, {})
    .then(data => {
      hide(spinner);
      if (data.error) {
        alert('Claude error: ' + data.error);
        return;
      }

      _state[n] = { ..._state[n], claudeSuggestion: data };

      // Format suggestion for display
      const lines = [];
      lines.push(`Type: ${data.type}`);
      if (data.show)           lines.push(`Show: ${data.show}`);
      if (data.season)         lines.push(`Season: ${data.season}`);
      if (data.episode)        lines.push(`Episode: ${data.episode}`);
      if (data.episode_title)  lines.push(`Episode Title: ${data.episode_title}`);
      if (data.movie_title)    lines.push(`Movie: ${data.movie_title}`);
      if (data.year)           lines.push(`Year: ${data.year}`);
      if (data.confidence)     lines.push(`Confidence: ${data.confidence}`);
      if (data.reasoning)      lines.push(`\n${data.reasoning}`);

      display.textContent = lines.join('\n');
      show(panel);
    })
    .catch(e => {
      hide(spinner);
      alert('Request failed: ' + e);
    });
}

function applySuggestion(n) {
  const sugg = (_state[n] || {}).claudeSuggestion;
  if (!sugg) return;

  if (['play_all', 'deleted', 'unknown'].includes(sugg.type)) {
    quickClassify(n, sugg.type);
    hide(qs(`claude-panel-${n}`));
    return;
  }

  showForm(n, sugg.type);
  hide(qs(`claude-panel-${n}`));
}

function dismissClaude(n) {
  hide(qs(`claude-panel-${n}`));
}

// ── Display update (no page reload) ───────────────────────────────────────────

const TYPE_LABEL = {
  tv_episode: 'TV Episode', movie: 'Movie', special: 'Special',
  documentary: 'Documentary', play_all: 'Play-All', deleted: 'Delete',
  unknown: 'Unknown'
};

function _updateDisplay(n, cls) {
  const display = qs(`cls-display-${n}`);
  if (!display) return;

  let text = TYPE_LABEL[cls.type] || cls.type;
  if (cls.type === 'tv_episode') {
    if (cls.show) text += ` — ${cls.show}`;
    if (cls.season)  text += ` S${String(cls.season).padStart(2,'0')}`;
    if (cls.episode) text += `E${String(cls.episode).padStart(2,'0')}`;
    if (cls.episode_title) text += `: ${cls.episode_title}`;
  } else if (cls.type === 'movie' && cls.movie_title) {
    text += ` — ${cls.movie_title}`;
    if (cls.year) text += ` (${cls.year})`;
  } else if ((cls.type === 'special' || cls.type === 'documentary') && cls.title) {
    text += ` — ${cls.title}`;
  }

  display.innerHTML = `<div class="cls-badge cls-${cls.type}">${text}</div>`;

  // Mark card as classified
  const card = qs(`title-card-${n}`);
  if (card) card.classList.add('classified');
}

function _updateProgress() {
  const cards      = document.querySelectorAll('.title-card');
  const classified = document.querySelectorAll('.title-card.classified');
  const fill       = document.querySelector('.progress-bar-fill');
  const label      = document.querySelector('.progress-label');
  if (!fill || !label) return;

  const total = cards.length;
  const done  = classified.length;
  const pct   = total ? Math.round((done / total) * 100) : 0;
  fill.style.width = pct + '%';
  label.textContent = `${done}/${total} classified`;
}

// ── Lightbox ───────────────────────────────────────────────────────────────────

function openLightbox(src) {
  const lb  = qs('lightbox');
  const img = qs('lightbox-img');
  if (!lb || !img) return;
  img.src = src;
  show(lb);
  document.body.style.overflow = 'hidden';
}

function closeLightbox() {
  hide(qs('lightbox'));
  document.body.style.overflow = '';
}

document.addEventListener('keydown', e => {
  if (e.key === 'Escape') closeLightbox();
});

// ── Job approval ───────────────────────────────────────────────────────────────

function approveJob(jobId) {
  if (!confirm('Mark all titles approved and write classification.json?')) return;
  apiPost(`/media/job/${jobId}/approve`, {})
    .then(r => {
      if (r.ok) {
        location.reload();
      } else {
        alert('Error: ' + (r.error || 'unknown'));
      }
    })
    .catch(e => alert('Network error: ' + e));
}

function approveAndEncode(jobId) {
  if (!confirm('Approve all classifications and start encoding immediately?')) return;
  apiPost(`/media/job/${jobId}/approve`, { auto_encode: true })
    .then(r => {
      if (r.ok) location.reload();
      else alert('Error: ' + (r.error || 'unknown'));
    })
    .catch(e => alert('Network error: ' + e));
}

function encodeJob(jobId, event) {
  if (event) { event.preventDefault(); event.stopPropagation(); }
  if (!confirm('Start HandBrake encoding for this job? It will run in the background.')) return;
  apiPost(`/media/job/${jobId}/encode`, {})
    .then(r => {
      if (r.ok) {
        location.reload();
      } else {
        alert('Error: ' + (r.error || 'unknown'));
      }
    })
    .catch(e => alert('Network error: ' + e));
}
