'use strict';

// Per-title state: which form is open, which Claude suggestion is stored
const _state = {};

// ── Utilities ──────────────────────────────────────────────────────────────────

function qs(id)    { return document.getElementById(id); }
function show(el)  { el && el.classList.remove('hidden'); }
function hide(el)  { el && el.classList.add('hidden'); }

// Commit is a synchronous file move (often across drives) with no progress
// stream, unlike rip/encode -- this banner is the only feedback the user
// gets while it's running. Every commit action (single-title, per-job, or
// the all-staged-jobs button) must show/hide it.
function showCommitBanner() { show(qs('commit-banner')); }
function hideCommitBanner() { hide(qs('commit-banner')); }

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
  const existingRaw = wrap && wrap.dataset.cls;

  if (sugg && sugg.type === type) {
    _prefillForm(activeFieldset, sugg);
  } else if (existingRaw) {
    try { _prefillForm(activeFieldset, JSON.parse(existingRaw)); } catch(e) {}
  } else {
    // Nothing title-specific yet — fall back to what the disc context
    // already knows (e.g. a single-movie disc knows its title/year for
    // every title on it), so the user only has to verify, not retype.
    _prefillFromDiscContext(activeFieldset, type);
  }

  wrap.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function _prefillFromDiscContext(container, type) {
  if (typeof DISC_CONTEXT === 'undefined' || !DISC_CONTEXT || !container) return;
  if (type === 'movie' && DISC_CONTEXT.type === 'movie') {
    _prefillForm(container, { movie_title: DISC_CONTEXT.title, year: DISC_CONTEXT.year });
  } else if (type === 'tv_episode' && DISC_CONTEXT.type === 'tv') {
    _prefillForm(container, { show: DISC_CONTEXT.show, season: DISC_CONTEXT.season });
  }
}

function hideForm(n) {
  hide(qs(`cls-form-${n}`));
}

function _prefillForm(container, data) {
  if (!container) return;
  const set = f => { const el = container.querySelector(`[name="${f}"]`); if (el && data[f] != null) el.value = data[f]; };
  set('show'); set('season'); set('episode'); set('episode_title');
  set('movie_title'); set('year'); set('title'); set('imdb_id');

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
      data[name] = isNaN(v) || name === 'show' || name === 'episode_title' || name === 'movie_title' || name === 'title' || name === 'imdb_id'
        ? v : Number(v);
    }
  };
  ['show','season','episode','episode_title','movie_title','year','title','imdb_id'].forEach(collect);

  apiPost(`/media/job/${JOB_ID}/title/${n}/classify`, data)
    .then(r => {
      if (r.ok) {
        _updateDisplay(n, data);
        _renderDuplicates(n, r.duplicates);
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
        _renderDuplicates(n, r.duplicates);
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

  const imdbLink = cls.imdb_id
    ? `<a class="imdb-link" href="https://www.imdb.com/title/${cls.imdb_id}/" target="_blank" rel="noopener">${cls.imdb_id} ↗</a>`
    : '';
  display.innerHTML = `<div class="cls-badge cls-${cls.type}">${text}</div>${imdbLink}`;

  // Mark card as classified
  const card = qs(`title-card-${n}`);
  if (card) card.classList.add('classified');
}

// ── Possible-duplicate panel (Phase 2B, advisory only, no page reload) ────────
// Mirrors the Jinja-rendered panel in job.html so a live classify (no reload)
// looks identical to what a fresh page load would render for the same data.

function _escapeHtml(s) {
  const div = document.createElement('div');
  div.textContent = s == null ? '' : String(s);
  return div.innerHTML;
}

function _renderDuplicates(n, duplicates) {
  const wrap = qs(`dup-panel-${n}`);
  if (!wrap) return;
  if (!duplicates || !duplicates.length) {
    wrap.innerHTML = '';
    return;
  }

  const cards = duplicates.map(d => {
    const href = d.catalog_id ? `/media/catalog/${d.catalog_id}` : `/media/job/${d.job_id}`;
    const reasons = (d.reasons || []).map(r => `<li>${_escapeHtml(r)}</li>`).join('');
    const pathLine = d.library_path
      ? `<div class="dup-meta" style="font-family:'Courier New',monospace;font-size:0.75rem">${_escapeHtml(d.library_path)}</div>`
      : '';
    const noRowNote = d.has_catalog_row ? '' : '&nbsp;·&nbsp; job history only, no catalog row';
    return `
      <div class="dup-card">
        <div class="dup-card-header">
          <span class="dup-conf dup-conf-${String(d.confidence).toLowerCase()}">${_escapeHtml(d.confidence)}</span>
          <a class="dup-title-link" href="${href}">${_escapeHtml(d.label)}</a>
        </div>
        <div class="dup-meta">
          Source: ${_escapeHtml((d.source || '').replace(/_/g, ' '))}
          &nbsp;·&nbsp; State: ${_escapeHtml(d.state)}
          &nbsp;·&nbsp; ${d.verified ? 'Verified' : 'Not verified'}
          ${noRowNote}
        </div>
        ${pathLine}
        <ul class="dup-reasons">${reasons}</ul>
        <div class="dup-actions">
          <a class="btn btn-ghost" href="${href}">View Existing</a>
        </div>
      </div>`;
  }).join('');

  wrap.innerHTML = `<div class="dup-panel"><div class="dup-panel-header">Possible Duplicate</div>${cards}</div>`;
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
  apiPost(`/media/job/${jobId}/approve`, {})
    .then(r => {
      if (r.ok) {
        window.location.href = '/media/';
      } else {
        alert('Error: ' + (r.error || 'unknown'));
      }
    })
    .catch(e => alert('Network error: ' + e));
}

function approveAndEncode(jobId) {
  apiPost(`/media/job/${jobId}/approve`, { auto_encode: true })
    .then(r => {
      if (r.ok) {
        // Any failure past this point happens asynchronously during the actual
        // encode -- that shows up on the job's card in the dashboard's Failed
        // section (with a Retry Encode button) once/if it happens, not here.
        window.location.href = '/media/';
      } else {
        alert('Error: ' + (r.error || 'unknown'));
      }
    })
    .catch(e => alert('Network error: ' + e));
}

function encodeJob(jobId, event) {
  if (event) { event.preventDefault(); event.stopPropagation(); }
  if (!confirm('Add this job to the encoding queue?')) return;
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

// ── Global "already ripped?" search ──────────────────────────────────────────

(function () {
  const input = qs('global-search-input');
  const panel = qs('global-search-results');
  if (!input || !panel) return;

  const STATUS_LABEL = {
    ingesting: 'ripping', review: 'in review', approved: 'approved',
    queued: 'queued', encoding: 'encoding', staged: 'staged',
    complete: 'in library', error: 'error'
  };

  let debounceTimer;
  input.addEventListener('input', () => {
    clearTimeout(debounceTimer);
    const q = input.value.trim();
    if (q.length < 2) { hide(panel); panel.innerHTML = ''; return; }
    debounceTimer = setTimeout(() => runSearch(q), 250);
  });

  input.addEventListener('focus', () => {
    if (panel.innerHTML.trim()) show(panel);
  });

  document.addEventListener('click', e => {
    if (e.target !== input && !panel.contains(e.target)) hide(panel);
  });

  function runSearch(q) {
    fetch(`/media/search.json?q=${encodeURIComponent(q)}`)
      .then(r => r.json())
      .then(d => renderResults(d.results || []))
      .catch(() => {});
  }

  function renderResults(results) {
    if (!results.length) {
      panel.innerHTML = '<div class="search-empty">No matches — probably not ripped yet</div>';
      show(panel);
      return;
    }
    panel.innerHTML = results.map(r => {
      const statusText = r.discarded ? 'discarded' : (r.staged ? 'staged' : (STATUS_LABEL[r.job_status] || r.job_status));
      return `
        <a class="search-result" href="/media/job/${r.job_id}">
          <span class="search-result-label">${_esc(r.label)}</span>
          <span class="search-result-meta">
            <span class="badge ${r.discarded ? 'error' : r.job_status}">${_esc(statusText)}</span>
            ${r.disc_label ? `<span class="search-result-disc">${_esc(r.disc_label)}</span>` : ''}
          </span>
        </a>`;
    }).join('');
    show(panel);
  }

  function _esc(s) {
    const d = document.createElement('div');
    d.textContent = s;
    return d.innerHTML;
  }
})();

// ── MakeMKV health banner ─────────────────────────────────────────────────────

(function () {
  const banner = qs('makemkv-banner');
  if (!banner) return;

  fetch('/media/makemkv/status')
    .then(r => r.json())
    .then(d => {
      if (!d.ok) {
        qs('makemkv-banner-text').textContent = d.message || 'MakeMKV needs an update.';
        show(banner);
      }
    })
    .catch(() => {});
})();

function updateMakemkv() {
  const btn = qs('makemkv-update-btn');
  const text = qs('makemkv-banner-text');
  btn.disabled = true;
  btn.textContent = 'Updating…';
  const prevText = text.textContent;
  text.textContent = 'Updating MakeMKV — this can take a minute…';

  apiPost('/media/makemkv/update', {})
    .then(r => {
      if (r.ok) {
        text.textContent = 'MakeMKV updated successfully.';
        btn.remove();
        setTimeout(() => hide(qs('makemkv-banner')), 4000);
      } else {
        text.textContent = 'Update failed — see console for details.';
        console.error('MakeMKV update failed:', r.output);
        btn.disabled = false;
        btn.textContent = 'Update MakeMKV';
      }
    })
    .catch(e => {
      text.textContent = prevText;
      btn.disabled = false;
      btn.textContent = 'Update MakeMKV';
      alert('Network error: ' + e);
    });
}
