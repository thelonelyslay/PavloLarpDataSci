/**
 * Freedom Broker CQA (Conversation Quality Analytics) Frontend Application
 * Fully client-side, zero build required. Pure modern JavaScript.
 * Totals, points, model and cost come from the server (src/cqa/scoring.py). Nothing is recalculated here.
 */

(function () {
  'use strict';

  // --- STATE ---
  const state = {
    conversations: [],
    evaluations: {},
    activeConvId: null,
    activeChecklistKey: 'default',
    checklists: {},
    filters: {
      search: '',
      channel: 'all',
      lang: 'all',
      status: 'all'
    },
    appeals: [],
    appealRecords: null,
    evalById: {},
    batch: null,
    isLoading: false,
    errorMessage: null
  };

  // --- SETTINGS (display thresholds only; they never change scores) ---
  const SETTINGS_KEY = 'cqa_display_settings';
  const DEFAULT_SETTINGS = { good: 70, excellent: 90, target: 85 };
  function loadSettings() {
    try { return Object.assign({}, DEFAULT_SETTINGS, JSON.parse(localStorage.getItem(SETTINGS_KEY) || '{}')); }
    catch (e) { return Object.assign({}, DEFAULT_SETTINGS); }
  }
  state.settings = loadSettings();
  function saveSettings() { try { localStorage.setItem(SETTINGS_KEY, JSON.stringify(state.settings)); } catch (e) { /* storage off */ } }
  function scoreLevel(total) {
    if (total === null || total === undefined) return 'none';
    if (total >= state.settings.excellent) return 'excellent';
    if (total >= state.settings.good) return 'good';
    return 'low';
  }

  // --- HELPERS ---
  const api = () => window.CQA_API;
  const isLive = () => api() && api().isLive();
  function activeChecklist() {
    return state.checklists[state.activeChecklistKey] || Object.values(state.checklists)[0] || null;
  }
  function checklistOf(ev) {
    return (ev && state.checklists[ev.checklist_id]) || activeChecklist();
  }
  function critDef(ev, critId) {
    const cl = checklistOf(ev);
    return cl && cl.criteria ? cl.criteria.find(c => c.id === critId) : null;
  }
  function finalAnswer(crit) {
    return crit.final_answer !== undefined && crit.final_answer !== null ? crit.final_answer : crit.answer;
  }
  function answerView(def, crit, a) {
    if (a === undefined) a = finalAnswer(crit);
    const isFlag = (def && def.type === 'flag') || (!def && crit.weight === 0);
    if (isFlag) return a === 'yes' ? { cls: 'ans-flag-active', text: '⚠️ НАРУШЕНИЕ' } : { cls: 'ans-flag-clear', text: 'НЕТ НАРУШЕНИЙ' };
    if (a === 'na') return { cls: 'ans-na', text: 'N/A' };
    if (a === 'yes') return { cls: 'ans-yes', text: 'ДА' };
    if (a === 'no') return { cls: 'ans-no', text: 'НЕТ' };
    if (typeof a === 'number' || /^\d+(\.\d+)?$/.test(String(a))) {
      const max = def && def.steps ? Math.max(...def.steps.map(Number)) : null;
      return { cls: 'ans-scale', text: `${a}${max ? ' из ' + max : ''}` };
    }
    return { cls: 'ans-na', text: '—' };
  }
  function fmt2(n) { return n === null || n === undefined || isNaN(n) ? '—' : Number(n).toFixed(2); }
  // Floating notice: red for problems, green for done
  function toast(msg, kind) {
    const bad = kind === 'error' || (kind === undefined && /не удалось|ошибк|не сохранен|не отправлен|^укажите|^выберите|^проверьте|только с сервером|недоступ|нет диалогов|сначала|изменений нет|не принимает/i.test(msg));
    let box = document.getElementById('toast-box');
    if (!box) { box = document.createElement('div'); box.id = 'toast-box'; document.body.appendChild(box); }
    const t = document.createElement('div');
    t.className = 'toast ' + (bad ? 'toast-bad' : 'toast-ok');
    t.textContent = msg;
    t.onclick = () => t.remove();
    box.appendChild(t);
    setTimeout(() => t.remove(), bad ? 7000 : 4500);
  }

  // --- INITIALIZATION ---
  async function init() {
    bindEvents();
    restoreUiState();
    await loadData();
  }

  async function loadData() {
    showLoading(true);
    hideError();
    state.conversations = [];
    state.evaluations = {};
    try {
      const health = await api().connect();
      updateApiStatusUI(health);

      // Checklists: from the server (live) or from demo data (generated from src/cqa/checklist.*.json)
      state.checklists = await api().getChecklists();
      try {
        const draft = JSON.parse(localStorage.getItem('cqa_checklist_draft') || 'null');
        if (draft && draft.criteria) state.checklists['draft'] = draft;
      } catch (e) { /* no draft */ }
      if (!state.checklists[state.activeChecklistKey]) {
        const keys = Object.keys(state.checklists).filter(k => k !== 'draft');
        state.activeChecklistKey = keys.find(k => state.checklists[k].active && (state.checklists[k].applies_to || 'agent chat') === 'agent chat')
          || keys.find(k => (state.checklists[k].applies_to || 'agent chat') === 'agent chat') || keys[0] || 'draft';
      }
      renderChecklistPicker();

      state.conversations = await api().getConversations();
      if (state.conversations.length > 0 && !state.conversations.find(c => c.conversation_id === state.activeConvId)) {
        state.activeConvId = state.conversations[0].conversation_id;
      }
      if (state.activeConvId) await selectConversation(state.activeConvId, true);
      if (isLive()) { await preloadEvaluations(); resumeJob(); loadTranscriptsInBackground(); }

      collectAppeals();
      renderAll();
    } catch (err) {
      console.error('Ошибка загрузки данных:', err);
      showError(`Не удалось загрузить данные: ${err.message}`);
      renderAll();
    } finally {
      showLoading(false);
    }
  }

  // Live mode: fill totals, gross-error reasons and appeals. One GET /api/evaluations (newest 100),
  // then the latest evaluation of every conversation (last_evaluation_id) and the evaluations behind open appeals.
  async function preloadEvaluations() {
    try {
      const recent = await api().getRecentEvaluations();
      recent.slice().reverse().forEach(ev => { if (ev && ev.id) state.evalById[ev.id] = stamp(ev); });
    } catch (e) { /* older server: no list */ }
    const want = new Set();
    state.conversations.forEach(c => { if (c.last_evaluation_id && !state.evalById[c.last_evaluation_id]) want.add(c.last_evaluation_id); });
    try {
      const list = await api().getAppeals();
      state.appealRecords = Array.isArray(list) ? list : [];
      state.appealRecords.forEach(a => { if (!state.evalById[a.evaluation_id]) want.add(a.evaluation_id); });
    } catch (e) { state.appealRecords = null; /* older server: use evaluation snapshots */ }
    const ids = [...want];
    for (let i = 0; i < ids.length; i += 6) {
      await Promise.all(ids.slice(i, i + 6).map(async id => {
        try { const ev = await api().getEvaluation(id); if (ev) state.evalById[ev.id] = ev; } catch (e) { /* skip */ }
      }));
    }
    // the conversation's latest evaluation is the one the server names; otherwise the newest we know
    state.conversations.forEach(c => {
      const ev = (c.last_evaluation_id && state.evalById[c.last_evaluation_id])
        || Object.values(state.evalById).filter(e => e.conversation_id === c.conversation_id).sort((x, y) => (y._seq || 0) - (x._seq || 0))[0];
      if (ev) { state.evaluations[c.conversation_id] = ev; syncConvFromEval(c, ev); }
    });
  }

  // The server list has no texts: fetch transcripts quietly for previews and text search
  async function loadTranscriptsInBackground() {
    const todo = state.conversations.filter(c => !c.transcript);
    for (let i = 0; i < todo.length; i += 6) {
      await Promise.all(todo.slice(i, i + 6).map(async c => {
        try { const full = await api().getConversation(c.conversation_id); c.transcript = full.transcript; c.audio_url = full.audio_url; } catch (e) { /* skip */ }
      }));
      renderConversationsList();
    }
  }

  async function ensureEvaluationLoaded(cid, force) {
    if (state.evaluations[cid] && !force) return;
    const conv = state.conversations.find(c => c.conversation_id === cid);
    if (!conv) return;
    try {
      const ev = await api().getLatestEvaluation(conv);
      if (ev) storeEval(ev);
      else delete state.evaluations[cid];
    } catch (e) {
      console.warn('Не удалось загрузить оценку для', cid, e);
    }
  }

  function storeEval(ev) {
    if (!ev || !ev.id) return;
    state.evalById[ev.id] = ev;
    const cur = state.evaluations[ev.conversation_id];
    if (!cur || cur.id === ev.id || !state.evalById[cur.id] || (ev._seq || 0) >= (cur._seq || 0)) state.evaluations[ev.conversation_id] = ev;
    const conv = state.conversations.find(c => c.conversation_id === ev.conversation_id);
    if (conv && state.evaluations[ev.conversation_id] === ev) syncConvFromEval(conv, ev);
  }
  let evalSeq = 0;
  const stamp = ev => { if (ev) ev._seq = ++evalSeq; return ev; };

  // Checklist to use for a conversation: the picked one if it is for this channel, otherwise the server's active one
  function checklistIdFor(conv) {
    const cl = activeChecklist();
    if (state.activeChecklistKey === 'draft' || !cl || !cl.id) return null;
    return (cl.applies_to || 'agent chat') === conv.channel ? cl.id : null;
  }

  function syncConvFromEval(conv, ev) {
    conv.total_score = ev.total;
    conv.evaluation_status = ev.status;
    conv.gross_error = Boolean(ev.gross_error);
    conv.last_evaluation_id = ev.id;
    conv.has_appeal = (ev.criteria || []).some(c => c.appeal && c.appeal.status === 'open');
  }

  function renderChecklistPicker() {
    const sel = document.getElementById('checklist-picker');
    if (!sel) return;
    sel.innerHTML = Object.entries(state.checklists).map(([k, c]) => {
      const n = (c.criteria || []).length;
      const label = k === 'draft' ? `Черновик (не сохранён): ${c.name || ''}` : `${c.active ? '★ ' : ''}${c.name || k} v${c.version || 1}`;
      return `<option value="${escapeHtml(k)}" ${k === state.activeChecklistKey ? 'selected' : ''}>${escapeHtml(label)} · ${n} крит.</option>`;
    }).join('');
  }

  function updateApiStatusUI(health) {
    const statusText = document.getElementById('api-status-text');
    const statusDot = document.querySelector('.status-dot');
    const demoBanner = document.getElementById('demo-mode-banner');
    if (statusText) statusText.textContent = health.message;
    if (statusDot) {
      const color = health.status === 'online' ? '#10b981' : '#f59e0b';
      statusDot.style.background = color;
      statusDot.style.boxShadow = `0 0 6px ${color}`;
    }
    if (demoBanner) demoBanner.style.display = health.status === 'online' ? 'none' : 'flex';
    document.body.classList.toggle('is-demo', health.status !== 'online');
  }

  function showLoading(show) {
    state.isLoading = show;
  }

  function showError(msg) {
    state.errorMessage = msg;
    const banner = document.getElementById('global-error-banner');
    const textEl = document.getElementById('global-error-text');
    if (banner && textEl) {
      textEl.textContent = msg;
      banner.style.display = 'flex';
    }
  }

  function hideError() {
    state.errorMessage = null;
    const banner = document.getElementById('global-error-banner');
    if (banner) banner.style.display = 'none';
  }

  function collectAppeals() {
    state.appeals = [];
    if (isLive() && Array.isArray(state.appealRecords)) {
      for (const row of state.appealRecords) {
        const ev = state.evalById[row.evaluation_id];
        const crit = ev && (ev.criteria || []).find(c => c.id === row.criterion_id);
        const answer = row.decision === 'change' && row.new_answer !== null ? row.new_answer
          : row.final_answer !== undefined ? row.final_answer : crit ? finalAnswer(crit) : null;
        state.appeals.push({
          evaluation_id: row.evaluation_id,
          conversation_id: row.conversation_id || (ev && ev.conversation_id),
          criterion_id: row.criterion_id,
          criterion_title: row.criterion_title || (crit && crit.title) || row.criterion_id,
          answer,
          view: answerView(critDef(ev, row.criterion_id), crit || {}, answer),
          appeal: { id: row.id, comment: row.comment, status: row.status,
            decision: row.decision, reason: row.reason, new_answer: row.new_answer }
        });
      }
      state.appeals.sort((x, y) => (x.appeal.status === 'open' ? 0 : 1) - (y.appeal.status === 'open' ? 0 : 1));
      updateAppealsBadge();
      return;
    }
    const evs = Object.values(state.evalById).length ? Object.values(state.evalById) : Object.values(state.evaluations);
    const seen = new Set();
    for (const ev of evs) {
      if (!ev || !ev.criteria || seen.has(ev.id)) continue;
      seen.add(ev.id);
      for (const crit of ev.criteria) {
        if (!crit.appeal) continue;
        state.appeals.push({
          evaluation_id: ev.id,
          conversation_id: ev.conversation_id,
          criterion_id: crit.id,
          criterion_title: crit.title,
          answer: finalAnswer(crit),
          view: answerView(critDef(ev, crit.id), crit),
          appeal: crit.appeal
        });
      }
    }
    state.appeals.sort((x, y) => (x.appeal.status === 'open' ? 0 : 1) - (y.appeal.status === 'open' ? 0 : 1));
    updateAppealsBadge();
  }

  function updateAppealsBadge() {
    const badge = document.getElementById('appeals-badge-count');
    if (badge) {
      const openCount = state.appeals.filter(a => a.appeal.status === 'open').length;
      badge.textContent = openCount;
      badge.style.display = openCount > 0 ? 'inline-block' : 'none';
    }
  }

  // --- RENDERING FUNCTIONS ---
  function renderAll() {
    renderConversationsList();
    renderTranscript();
    renderEvaluationCard();
    renderAppealsTable();
    renderChecklistTable();
    renderAnalyticsDashboard();
  }

  function visibleConversations() {
    return state.conversations.filter(conv => {
      if (state.filters.channel === 'chat' && conv.channel !== 'agent chat') return false;
      if (state.filters.channel === 'voice' && conv.channel !== 'voice call') return false;
      if (state.filters.lang !== 'all' && conv.language !== state.filters.lang) return false;
      if (state.filters.status === 'error' && !conv.gross_error) return false;
      if (state.filters.status === 'appeal' && !conv.has_appeal) return false;
      if (state.filters.search) {
        const q = state.filters.search.toLowerCase();
        const idMatch = conv.conversation_id.toLowerCase().includes(q);
        const textMatch = conv.transcript && conv.transcript.some(m => m.text && m.text.toLowerCase().includes(q));
        if (!idMatch && !textMatch) return false;
      }
      return true;
    });
  }

  // Why a conversation is flagged: the explanation of the triggered flag criterion
  function grossReason(cid) {
    const ev = state.evaluations[cid];
    if (!ev || !ev.gross_error) return '';
    const c = (ev.criteria || []).find(x => { const d = critDef(ev, x.id); return d && d.type === 'flag' && finalAnswer(x) === 'yes'; });
    return c ? c.explanation || c.title : 'Отмечена грубая ошибка';
  }

  // 1. CONVERSATIONS SIDEBAR
  function renderConversationsList() {
    const listEl = document.getElementById('conversations-list');
    if (!listEl) return;

    renderBatchBar();
    const items = visibleConversations();

    listEl.innerHTML = '';
    if (items.length === 0) {
      listEl.innerHTML = '<div style="padding: 20px; text-align: center; color: var(--text-muted); font-size: 12px;">Диалогов не найдено</div>';
      return;
    }

    items.forEach(conv => {
      const card = document.createElement('div');
      card.className = `conv-card ${conv.conversation_id === state.activeConvId ? 'active' : ''}`;
      card.onclick = () => selectConversation(conv.conversation_id);
      const lang = String(conv.language || '').toLowerCase();

      const firstClientMsg = (conv.transcript && conv.transcript.find(m => m.speaker === 'client')) || (conv.transcript && conv.transcript[0]);
      const snippet = firstClientMsg ? firstClientMsg.text : '';

      const lvl = conv.evaluation_status === 'error' ? 'low' : scoreLevel(conv.total_score);
      const scoreClass = { excellent: 'score-perfect', good: 'score-good', low: 'score-poor', none: 'score-fair' }[lvl];

      card.innerHTML = `
        <div class="conv-card-top">
          <span class="conv-id">${escapeHtml(conv.conversation_id)}</span>
          <div class="conv-badges">
            <span class="badge ${conv.channel === 'voice call' ? 'badge-voice' : 'badge-chat'}">${conv.channel === 'voice call' ? '📞 Звонок' : '💬 Чат'}</span>
            <span class="badge badge-${lang}">${escapeHtml(conv.language)}</span>
          </div>
        </div>
        <div class="conv-snippet">${escapeHtml(snippet)}</div>
        ${conv.gross_error && grossReason(conv.conversation_id) ? `<div class="gross-reason">⚠️ ${escapeHtml(grossReason(conv.conversation_id))}</div>` : ''}
        <div class="conv-card-bottom">
          <span class="score-pill ${scoreClass}">
            ${conv.evaluation_status === 'error' ? '✕ ошибка оценки'
              : '★ ' + (conv.total_score !== null && conv.total_score !== undefined ? fmtShort(conv.total_score) : (conv.last_evaluation_id ? 'итог не рассчитан' : 'не оценён'))}
          </span>
          <div style="display: flex; gap: 6px;">
            ${state.evaluating === conv.conversation_id ? '<span class="busy-pill">⏳ оценка…</span>' : ''}
            ${state.evalError && state.evalError.cid === conv.conversation_id ? '<span class="err-pill" title="' + escapeHtml(state.evalError.message) + '">ошибка</span>' : ''}
            ${conv.gross_error ? '<span class="alert-icon-pill" title="Грубая ошибка">⚠️ Нарушение</span>' : ''}
            ${conv.has_appeal ? '<span style="font-size: 11px; color: var(--warning); font-weight: 700;" title="Подана апелляция">⚖️ Апелляция</span>' : ''}
          </div>
        </div>
      `;
      listEl.appendChild(card);
    });
  }

  async function selectConversation(cid, quiet) {
    if (state.activeConvId !== cid) state.selCrit = null;
    state.activeConvId = cid;
    writeHash();
    if (!quiet) renderConversationsList();

    let conv = state.conversations.find(c => c.conversation_id === cid);
    if (conv && !conv.transcript && window.CQA_API) {
      try {
        const full = await window.CQA_API.getConversation(cid);
        Object.assign(conv, full);
      } catch (e) {
        console.warn('Не удалось догрузить транскрипт', e);
      }
    }

    await ensureEvaluationLoaded(cid);
    if (quiet) return;
    renderConversationsList();
    renderTranscript();
    renderEvaluationCard();
  }

  // Evaluate the active conversation on the server: POST /api/evaluations
  async function evaluateActive() {
    const cid = state.activeConvId;
    if (!cid) return;
    if (!isLive()) { toast('Оценка работает только с сервером. Сейчас открыты демо-данные.'); return; }
    if (state.activeChecklistKey === 'draft') { toast('Выбран несохранённый черновик чек-листа. Сохраните версию во вкладке «Чек-листы» или выберите сохранённый чек-лист.'); return; }
    await evaluateOne(cid);
    collectAppeals();
    renderAll();
  }

  async function evaluateOne(cid) {
    const conv = state.conversations.find(c => c.conversation_id === cid);
    if (!conv) return null;
    state.evaluating = cid;
    state.evalError = null;
    if (cid === state.activeConvId) { renderTranscriptActions(); renderEvaluationCard(); }
    try {
      const ev = stamp(await api().evaluate(cid, checklistIdFor(conv)));
      storeEval(ev);
      if (ev.status === 'error') state.evalError = { cid, message: ev.error || 'модель не ответила' };
      return ev;
    } catch (e) {
      state.evalError = { cid, message: e.message };
      return null;
    } finally {
      state.evaluating = null;
    }
  }

  // "Оценить все": background job on the server (POST /api/evaluations/batch/start), progress by polling
  const JOB_KEY = 'cqa_batch_job';
  async function evaluateAll(ids, label) {
    if (!isLive()) { toast('Пакетная оценка работает только с сервером.'); return; }
    if (state.batch) { toast('Пакетная оценка уже идёт'); return; }
    if (!ids.length) { toast('Нет диалогов для оценки'); return; }
    // one checklist for the whole job only if every conversation is of the picked checklist's channel
    const convs = ids.map(id => state.conversations.find(c => c.conversation_id === id)).filter(Boolean);
    const clIds = [...new Set(convs.map(c => checklistIdFor(c)))];
    const checklistId = clIds.length === 1 ? clIds[0] : null;
    try {
      const job = await api().startBatch(ids, checklistId);
      await followJob(job, label || 'Оценка');
    } catch (e) {
      toast('Не удалось запустить пакетную оценку: ' + e.message);
    }
  }

  async function followJob(job, label) {
    state.batch = { id: job.id, total: job.total || 0, done: job.completed || 0, errors: 0, label, seen: 0 };
    try { localStorage.setItem(JOB_KEY, JSON.stringify({ id: job.id, label })); } catch (e) { /* storage off */ }
    renderBatchBar();
    let cur = job;
    while (true) {
      const fresh = (cur.results || []).slice(state.batch.seen);
      state.batch.seen += fresh.length;
      for (const r of fresh) {
        if (r.evaluation && r.evaluation.id) { storeEval(stamp(r.evaluation)); if (r.evaluation.status === 'error') state.batch.errors++; }
        else { state.batch.errors++; if (r.conversation_id === state.activeConvId) state.evalError = { cid: r.conversation_id, message: r.error || 'ошибка оценки' }; }
        const conv = state.conversations.find(c => c.conversation_id === r.conversation_id);
        if (conv && !r.evaluation) conv.evaluation_status = 'error';
      }
      state.batch.done = cur.completed || state.batch.seen;
      state.batch.total = cur.total || state.batch.total;
      renderBatchBar();
      if (fresh.length) {
        renderConversationsList();
        if (fresh.some(r => r.conversation_id === state.activeConvId)) { renderTranscript(); renderEvaluationCard(); }
      }
      if (cur.status === 'done') break;
      await new Promise(r => setTimeout(r, 1500));
      try { cur = await api().getBatch(state.batch.id); }
      catch (e) {
        if (e.status === 404) { toast('Пакетная задача не найдена на сервере (сервер перезапускали?)'); break; }
        await new Promise(r => setTimeout(r, 3000)); // network hiccup: keep polling
      }
    }
    const b = state.batch;
    state.batch = null;
    try { localStorage.removeItem(JOB_KEY); } catch (e) { /* storage off */ }
    collectAppeals();
    renderAll();
    toast(`${b.label}: готово ${b.done - b.errors} из ${b.total}${b.errors ? `, ошибок: ${b.errors}` : ''}`, b.errors ? 'error' : 'ok');
  }

  // After a page reload: continue showing a job that is still running on the server
  async function resumeJob() {
    let saved = null;
    try { saved = JSON.parse(localStorage.getItem(JOB_KEY) || 'null'); } catch (e) { /* storage off */ }
    if (!saved || !saved.id || state.batch) return;
    try { const job = await api().getBatch(saved.id); followJob(job, saved.label || 'Оценка'); }
    catch (e) { try { localStorage.removeItem(JOB_KEY); } catch (x) { /* storage off */ } }
  }

  function renderBatchBar() {
    const g = document.getElementById('global-progress');
    if (g) {
      const b = state.batch;
      g.style.display = b ? 'block' : 'none';
      if (b) g.innerHTML = `<div class="gp-text">⏳ ${escapeHtml(b.label)}: ${b.done} / ${b.total}${b.errors ? ` · ошибок ${b.errors}` : ''}</div>
        <div class="gp-track"><div class="gp-fill" style="width:${b.total ? b.done / b.total * 100 : 0}%"></div></div>`;
    }
    const box = document.getElementById('batch-bar');
    if (!box) return;
    const b = state.batch;
    if (!b) {
      box.innerHTML = `<button class="btn-primary batch-btn" id="btn-evaluate-all" ${isLive() ? '' : 'disabled title="Только с сервером"'}>▶ Оценить все (${visibleConversations().length})</button>`;
      const btn = document.getElementById('btn-evaluate-all');
      if (btn) btn.onclick = () => evaluateAll(visibleConversations().map(c => c.conversation_id), 'Оценка');
      return;
    }
    box.innerHTML = `<div class="batch-progress" id="batch-progress">
        <div class="batch-text"><b>${escapeHtml(b.label)}: ${b.done} / ${b.total}</b>${b.errors ? ` · ошибок ${b.errors}` : ''}</div>
        <div class="bar-track"><div class="bar-fill" style="width:${b.total ? b.done / b.total * 100 : 0}%"></div></div>
        <div class="batch-hint">Идёт на сервере; можно открывать диалоги и перезагружать страницу.</div>
      </div>`;
  }

  function renderTranscriptActions() {
    const box = document.getElementById('transcript-actions');
    if (!box) return;
    const ev = state.evaluations[state.activeConvId];
    const busy = state.evaluating === state.activeConvId;
    const live = isLive();
    box.innerHTML = `
      <button class="btn-primary" id="btn-evaluate" ${busy || !live ? 'disabled' : ''}
        title="${live ? 'POST /api/evaluations: оценить диалог по активному чек-листу' : 'Доступно только при подключённом сервере'}">
        ${busy ? '⏳ Оцениваем…' : (ev ? '↻ Переоценить' : '▶ Оценить')}
      </button>`;
    const btn = document.getElementById('btn-evaluate');
    if (btn) btn.onclick = evaluateActive;
  }

  // 2. TRANSCRIPT VIEW WITH REPLICA NUMBERS (#0, #1, ...)
  function renderTranscript() {
    const conv = state.conversations.find(c => c.conversation_id === state.activeConvId);
    renderTranscriptActions();
    if (!conv) return;

    const titleEl = document.getElementById('active-conv-title');
    const subEl = document.getElementById('active-conv-sub');
    const voiceBar = document.getElementById('voice-audio-bar');
    const streamEl = document.getElementById('chat-stream');

    const isVoice = conv.channel === 'voice call';
    const LANG = { RU: 'Русский', KZ: 'Казахский', EN: 'English' };
    const offs = messageOffsets(conv);
    const dur = conversationDuration(conv, offs);
    if (subEl) {
      subEl.textContent = `${isVoice ? 'Звонок' : 'Чат'} ${conv.conversation_id} · ${LANG[conv.language] || conv.language || '—'}` +
        (dur ? ` · ${formatSec(dur)}` : '') + ` · ${conv.transcript ? conv.transcript.length : 0} реплик`;
    }
    if (titleEl) titleEl.innerHTML = '';
    document.body.classList.toggle('is-chat', !isVoice);
    const tlCard = document.querySelector('.timeline-card');
    if (tlCard) tlCard.style.display = isVoice ? '' : 'none';

    // Player: real audio for calls; for chats only the timeline
    const audio = document.getElementById('html5-audio-element');
    const playBtn = document.getElementById('audio-play-btn');
    const extLink = document.getElementById('audio-external-link');
    if (isVoice && conv.audio_url) {
      if (extLink) extLink.href = conv.audio_url;
      setupVoiceAudio(conv.audio_url, dur);
      if (playBtn) playBtn.disabled = false;
    } else {
      if (audio) { audio.pause(); audio.removeAttribute('src'); }
      if (playBtn) { playBtn.disabled = true; playBtn.textContent = '▶'; }
      setText('audio-time-current', '00:00');
      setText('audio-time-duration', formatSec(dur || 0));
      const seek = document.getElementById('audio-seek'); if (seek) seek.value = 0;
    }
    renderTimeline(conv);

    // Render messages with replica numbers (#0, #1, #2, ...)
    if (streamEl) {
      streamEl.innerHTML = '';
      if (!conv.transcript || conv.transcript.length === 0) {
        streamEl.innerHTML = '<div style="padding: 30px; text-align: center; color: var(--text-muted);">Транскрипт загружается или пуст</div>';
        return;
      }
      conv.transcript.forEach((msg, idx) => {
        const row = document.createElement('div');
        row.className = `chat-message-row msg-${msg.speaker}`;
        row.id = `msg-${idx}`;

        let speakerLabel = 'Клиент';
        if (msg.speaker === 'operator') speakerLabel = 'Оператор';
        else if (msg.speaker === 'bot') speakerLabel = 'Бот';
        else if (msg.speaker === 'system') speakerLabel = 'Система';

        row.dataset.i = idx;
        row.onclick = () => { seekAudioToReplica(idx); highlightTimeline([idx, idx]); };
        row.innerHTML = `
          <div class="msg-header">
            <span class="msg-time">${escapeHtml(formatTimestamp(msg.timestamp, offs[idx]))}</span>
            <span class="speaker-tag speaker-${escapeHtml(msg.speaker)}">${speakerLabel}</span>
            <span class="replica-badge">#${idx}</span>
          </div>
          <div class="msg-bubble">${escapeHtml(msg.text)}</div>
        `;
        streamEl.appendChild(row);
      });
    }
  }

  function setupVoiceAudio(audioUrl, fallbackDur) {
    const playBtn = document.getElementById('audio-play-btn');
    const seekInput = document.getElementById('audio-seek');
    const curTime = document.getElementById('audio-time-current');
    const durTime = document.getElementById('audio-time-duration');
    const speedSel = document.getElementById('audio-speed');
    const audio = document.getElementById('html5-audio-element');

    if (!audio) return;
    if (audio.getAttribute('src') !== audioUrl) audio.src = audioUrl;
    if (playBtn) playBtn.textContent = audio.paused ? '▶' : '⏸';
    if (durTime) durTime.textContent = formatSec(fallbackDur || 0);
    audio.onended = () => { if (playBtn) playBtn.textContent = '▶'; };
    audio.onerror = () => { const n = document.getElementById('tl-note'); if (n) n.textContent = 'Запись не воспроизводится в браузере — откройте её по ссылке ↗. Полосы построены по меткам времени реплик.'; };

    if (playBtn) {
      playBtn.onclick = () => {
        if (audio.paused) {
          audio.play().catch(() => {});
          playBtn.textContent = '⏸';
        } else {
          audio.pause();
          playBtn.textContent = '▶';
        }
      };
    }

    audio.ontimeupdate = () => {
      const total = audio.duration || fallbackDur;
      if (total) {
        const pct = (audio.currentTime / total) * 100;
        if (seekInput) seekInput.value = pct;
        if (curTime) curTime.textContent = formatSec(audio.currentTime);
        if (durTime) durTime.textContent = formatSec(total);
        const ph = document.getElementById('tl-playhead');
        const lanes = document.getElementById('lane-client');
        if (ph && lanes) { ph.style.display = 'block'; ph.style.left = (lanes.offsetLeft + lanes.offsetWidth * Math.min(1, pct / 100)) + 'px'; }
      }
    };

    if (seekInput) {
      seekInput.oninput = () => {
        if (audio.duration) {
          audio.currentTime = (seekInput.value / 100) * audio.duration;
        }
      };
    }

    if (speedSel) {
      speedSel.onchange = () => {
        audio.playbackRate = Number(speedSel.value);
      };
    }
  }

  // --- TIMELINE: who spoke when (built from replica timestamps) ---
  function tsToSec(ts) {
    if (!ts) return null;
    if (/^\d{1,2}:\d{2}(:\d{2})?$/.test(ts)) { const p = ts.split(':').map(Number); return p.length === 3 ? p[0] * 3600 + p[1] * 60 + p[2] : p[0] * 60 + p[1]; }
    const d = Date.parse(ts); return isNaN(d) ? null : d / 1000;
  }
  function messageOffsets(conv) {
    const t = (conv.transcript || []).map(m => tsToSec(m.timestamp));
    const base = t.find(x => x !== null);
    return t.map(x => (x === null || base === undefined) ? null : Math.max(0, x - base));
  }
  // Estimated speaking time of a replica: ~15 characters per second, 1.5 s minimum
  const speakSec = m => Math.max(1.5, (m.text || '').length / 15);
  function conversationDuration(conv, offs) {
    const tr = conv.transcript || [];
    let end = 0;
    tr.forEach((m, i) => { if (offs[i] !== null) end = Math.max(end, offs[i] + (m.speaker === 'system' ? 0 : speakSec(m))); });
    return end;
  }
  function messageSpans(conv) {
    const offs = messageOffsets(conv);
    const tr = conv.transcript || [];
    return tr.map((m, i) => {
      if (offs[i] === null) return null;
      let next = null;
      for (let k = i + 1; k < tr.length; k++) if (offs[k] !== null && offs[k] > offs[i]) { next = offs[k]; break; }
      const end = offs[i] + speakSec(m);
      return [offs[i], next !== null ? Math.min(end, next) : end];
    });
  }
  // On a long chat a short replica would be a hairline: give every replica at least ~1.5% of the width
  function visualSpans(spans, dur) {
    const minW = dur * 0.015;
    return spans.map(sp => sp ? [sp[0], Math.max(sp[1], sp[0] + minW)] : null);
  }
  const BAR_COUNT = 140;
  function renderTimeline(conv) {
    const spans = messageSpans(conv);
    const offs = messageOffsets(conv);
    const dur = conversationDuration(conv, offs) || 1;
    const tr = conv.transcript || [];
    const vis = visualSpans(spans, dur);
    const lanes = { client: document.getElementById('lane-client'), operator: document.getElementById('lane-operator') };
    for (const role of ['client', 'operator']) {
      const el = lanes[role];
      if (!el) continue;
      let html = '';
      for (let k = 0; k < BAR_COUNT; k++) {
        const t0 = k / BAR_COUNT * dur, t1 = (k + 1) / BAR_COUNT * dur;
        const i = tr.findIndex((m, j) => m.speaker === role && vis[j] && vis[j][0] < t1 && vis[j][1] > t0);
        if (i >= 0) {
          // bar height varies deterministically with the replica text, only for visual rhythm
          const code = (tr[i].text.charCodeAt((k * 7) % tr[i].text.length) || 60) + k * 13 + i * 31;
          const h = 30 + (code % 70);
          html += `<i class="on" data-i="${i}" style="height:${h}%"></i>`;
        } else html += '<i></i>';
      }
      el.innerHTML = html;
      el.onclick = (e) => {
        const rect = el.getBoundingClientRect();
        const t = (e.clientX - rect.left) / rect.width * dur;
        let best = -1, bd = Infinity;
        tr.forEach((m, j) => { if (spans[j] && m.speaker !== 'system') { const d = Math.abs(spans[j][0] - t); if (d < bd) { bd = d; best = j; } } });
        if (best >= 0) scrollToReplica(best);
      };
    }
    const ph = document.getElementById('tl-playhead'); if (ph) ph.style.display = 'none';
    state.timeline = { dur, spans: vis };
    applyFocus();
  }
  function highlightTimeline(range) {
    const hl = document.getElementById('tl-highlight');
    const lane = document.getElementById('lane-client');
    const tl = state.timeline;
    if (!hl || !lane || !tl || !range) { if (hl) hl.style.display = 'none'; return; }
    const a = tl.spans[range[0]], b = tl.spans[range[1]] || a;
    if (!a) { hl.style.display = 'none'; return; }
    const x0 = a[0] / tl.dur, x1 = Math.max(b[1], a[0]) / tl.dur;
    hl.style.display = 'block';
    hl.style.left = (lane.offsetLeft + lane.offsetWidth * x0 - 3) + 'px';
    hl.style.width = Math.max(10, lane.offsetWidth * (x1 - x0) + 6) + 'px';
  }

  function formatSec(s) {
    const mins = Math.floor(s / 60);
    const secs = Math.floor(s % 60);
    return `${String(mins).padStart(2, '0')}:${String(secs).padStart(2, '0')}`;
  }

  // 3. EVALUATION SCORECARD (everything comes from the evaluation JSON, AGENTS.md)
  function setText(id, text) { const el = document.getElementById(id); if (el) el.textContent = text; }

  function renderEvaluationCard() {
    const ev = state.evaluations[state.activeConvId];
    const totalEl = document.getElementById('total-score-display');
    const badgeEl = document.getElementById('score-status-badge');
    const grossBanner = document.getElementById('gross-error-banner');
    const grossText = document.getElementById('gross-error-text');
    const sectionsSummary = document.getElementById('sections-summary');
    const criteriaContainer = document.getElementById('criteria-container');
    const demoTag = document.getElementById('eval-demo-tag');

    const busy = state.evaluating === state.activeConvId;
    const err = state.evalError && state.evalError.cid === state.activeConvId && (!ev || ev.status === 'error') ? state.evalError.message : null;
    if (busy || (!ev && err)) {
      if (totalEl) totalEl.textContent = busy ? '…' : '—';
      if (badgeEl) { badgeEl.className = 'score-label-badge ' + (busy ? 'badge-good' : 'badge-violation'); badgeEl.textContent = busy ? 'Оцениваем' : 'Ошибка оценки'; }
      if (grossBanner) grossBanner.style.display = 'none';
      if (sectionsSummary) sectionsSummary.innerHTML = '';
      renderQualitySummary(null);
      const whyT = document.getElementById('why-title'); const whyX = document.getElementById('why-text');
      if (whyT) whyT.textContent = busy ? 'Идёт оценка' : 'Ошибка оценки';
      if (whyX) whyX.textContent = busy ? 'Сервер вызывает модель и считает баллы через scoring.py. Обычно 10–60 секунд.' : err;
      if (criteriaContainer) criteriaContainer.innerHTML = busy
        ? '<div class="empty-eval"><div class="spinner"></div>Оцениваем диалог…</div>'
        : `<div class="empty-eval err">Не удалось оценить: ${escapeHtml(err)}<br>Нажмите «↻ Переоценить», чтобы повторить.</div>`;
      return;
    }
    if (!ev) {
      if (totalEl) totalEl.textContent = '—';
      if (badgeEl) { badgeEl.className = 'score-label-badge badge-warning'; badgeEl.textContent = 'Не оценён'; }
      if (grossBanner) grossBanner.style.display = 'none';
      if (sectionsSummary) sectionsSummary.innerHTML = '';
      setText('eval-model-tag', 'Модель: —');
      setText('eval-cost-tag', 'Стоимость: —');
      setText('eval-version-tag', 'Чек-лист: —');
      if (demoTag) demoTag.style.display = 'none';
      renderQualitySummary(null);
      if (criteriaContainer) {
        criteriaContainer.innerHTML = `<div class="empty-eval">Диалог ещё не оценён.<br>${isLive()
          ? 'Нажмите «▶ Оценить» над транскриптом — оценку выполнит сервер.'
          : 'Оценка доступна только при подключённом сервере.'}</div>`;
      }
      return;
    }

    const isDemo = !isLive();
    if (demoTag) demoTag.style.display = isDemo ? 'inline-block' : 'none';
    const cl = checklistOf(ev);

    if (totalEl) totalEl.textContent = ev.total !== null && ev.total !== undefined ? fmtShort(ev.total) : '—';

    if (badgeEl) {
      const lvl = scoreLevel(ev.total);
      if (ev.status === 'error') { badgeEl.className = 'score-label-badge badge-violation'; badgeEl.textContent = 'Ошибка оценки'; }
      else if (ev.gross_error) { badgeEl.className = 'score-label-badge badge-violation'; badgeEl.textContent = 'Грубая ошибка'; }
      else if (lvl === 'none') { badgeEl.className = 'score-label-badge badge-warning'; badgeEl.textContent = 'Итог не рассчитан'; }
      else if (lvl === 'excellent') { badgeEl.className = 'score-label-badge badge-excellent'; badgeEl.textContent = `≥ ${state.settings.excellent} (настройка)`; }
      else if (lvl === 'good') { badgeEl.className = 'score-label-badge badge-good'; badgeEl.textContent = `≥ ${state.settings.good} (настройка)`; }
      else { badgeEl.className = 'score-label-badge badge-warning'; badgeEl.textContent = `< ${state.settings.good} (настройка)`; }
    }

    // Original total (before appeals) if it differs
    const origEl = document.getElementById('total-score-original');
    if (origEl) {
      const changed = ev.original_total !== undefined && ev.original_total !== null && ev.original_total !== ev.total;
      origEl.style.display = changed ? 'inline' : 'none';
      origEl.textContent = changed ? `исходно ${fmtShort(ev.original_total)}` : '';
    }

    if (grossBanner) {
      if (ev.gross_error) {
        grossBanner.style.display = 'flex';
        const flagCrit = (ev.criteria || []).find(c => { const d = critDef(ev, c.id); return d && d.type === 'flag' && finalAnswer(c) === 'yes'; });
        if (grossText) grossText.textContent = (flagCrit && flagCrit.explanation) || 'Выявлена грубая ошибка';
      } else {
        grossBanner.style.display = 'none';
      }
    }

    if (sectionsSummary) {
      sectionsSummary.innerHTML = '';
      for (const [secName, scoreVal] of Object.entries(ev.section_scores || {})) {
        const item = document.createElement('div');
        item.className = 'section-bar-item';
        const pct = scoreVal !== null ? scoreVal : 0;
        item.innerHTML = `
          <div class="section-bar-label">
            <span>${escapeHtml(secName)}</span>
            <span><strong>${scoreVal !== null ? fmt2(scoreVal) + '%' : 'N/A'}</strong></span>
          </div>
          <div class="bar-track"><div class="bar-fill" style="width: ${pct}%;"></div></div>`;
        sectionsSummary.appendChild(item);
      }
    }

    // Model, cost, checklist version: from the evaluation JSON only
    setText('eval-model-tag', 'Модель: ' + (ev.model || '—'));
    setText('eval-cost-tag', 'Стоимость: ' + (ev.cost_usd !== null && ev.cost_usd !== undefined ? '$' + Number(ev.cost_usd).toFixed(4) : '—'));
    setText('eval-version-tag', `Чек-лист: ${cl && cl.name ? cl.name + ' ' : ''}v${ev.checklist_version !== undefined ? ev.checklist_version : '—'}`);

    renderQualitySummary(ev);
    if (!criteriaContainer) return;
    criteriaContainer.innerHTML = '';
    if (ev.status === 'error') {
      criteriaContainer.innerHTML = `<div class="empty-eval err">Сервер не смог оценить диалог: ${escapeHtml(ev.error || ev.message || 'неизвестная ошибка')}.<br>Нажмите «↻ Переоценить».</div>`;
      const whyT = document.getElementById('why-title'); const whyX = document.getElementById('why-text');
      if (whyT) whyT.textContent = 'Ошибка оценки';
      if (whyX) whyX.textContent = ev.error || 'Модель не ответила';
      return;
    }
    const transcript = (state.conversations.find(c => c.conversation_id === state.activeConvId) || {}).transcript || [];

    (ev.criteria || []).forEach((crit) => {
      const def = critDef(ev, crit.id);
      const fin = finalAnswer(crit);
      const orig = crit.original_answer !== undefined && crit.original_answer !== null ? crit.original_answer : crit.answer;
      const view = answerView(def, crit, fin);
      const origView = answerView(def, crit, orig);
      const isFlag = def ? def.type === 'flag' : crit.weight === 0;
      const card = document.createElement('div');
      card.className = 'criterion-card' + (state.selCrit === crit.id ? ' sel' : '');
      card.dataset.crit = crit.id;
      card.onclick = (e) => { if (!e.target.closest('button')) selectCriterion(crit.id); };

      const evidenceHtml = (crit.evidence || []).map(e => `
          <div class="evidence-row">
            <div class="evidence-quote">«${escapeHtml(e.quote || (transcript[e.message_index] || {}).text || '')}»</div>
            <button class="evidence-link-btn" onclick="app.scrollToReplica(${Number(e.message_index)})">
              <span>📍 Перейти к реплике #${Number(e.message_index)} ↗</span>
            </button>
          </div>`).join('');

      const rangeHtml = Array.isArray(crit.checked_range) && crit.checked_range.length === 2 ? `
          <div class="evidence-row">
            <div class="evidence-quote">Проверен участок #${Number(crit.checked_range[0])}–#${Number(crit.checked_range[1])}: здесь ожидалось действие</div>
            <button class="evidence-link-btn" onclick="app.scrollToReplica(${Number(crit.checked_range[0])}, ${Number(crit.checked_range[1])})">
              <span>📍 Показать участок ↗</span>
            </button>
          </div>` : '';

      const naHtml = fin === 'na' ? `<div class="na-reason-box">ℹ️ Почему N/A: ${escapeHtml(crit.na_reason || (def && def.na_condition) || '—')}</div>` : '';
      const rules = (def && def.rules) || crit.rules;
      const rulesHtml = rules ? `<div class="rules-box"><strong>Правило:</strong> ${escapeHtml(rules)}</div>` : '';

      let appealHtml;
      if (crit.appeal) {
        const open = crit.appeal.status === 'open';
        appealHtml = `
          <div style="font-size: 11px; background: #fffbeb; border: 1px solid #fde68a; border-radius: 6px; padding: 6px 8px; width: 100%;">
            <div style="display: flex; justify-content: space-between; align-items: center;">
              <span class="appeal-status-badge">⚖️ ${open ? 'На апелляции' : 'Рассмотрено'}</span>
              ${open ? `<button class="btn-primary" style="padding: 2px 8px; font-size: 11px;" onclick="app.openReviewModal('${escapeHtml(ev.id)}', '${escapeHtml(crit.id)}')">Рассмотреть</button>` : ''}
            </div>
            <div style="margin-top: 4px; color: #92400e;"><strong>Апелляция:</strong> ${escapeHtml(crit.appeal.comment)}</div>
            ${crit.appeal.decision ? `
              <div style="margin-top: 4px; color: #065f46; border-top: 1px dashed #d1fae5; padding-top: 4px;">
                <strong>Решение:</strong> ${crit.appeal.decision === 'change' ? 'апелляция принята, оценка изменена' : 'апелляция отклонена, оценка оставлена'}. Причина: ${escapeHtml(crit.appeal.reason || '—')}
              </div>` : ''}
          </div>`;
      } else {
        appealHtml = `<button class="appeal-btn" onclick="app.openAppealModal('${escapeHtml(crit.id)}')"><span>⚖️ Оспорить оценку</span></button>`;
      }

      const pointsHtml = isFlag ? '<span class="points-pill">без баллов</span>'
        : crit.applicable ? `<span class="points-pill">${fmt2(crit.points)} б.</span>` : '<span class="points-pill">—</span>';
      const weightsHtml = isFlag ? '<span>Флаг: на итог не влияет, отмечается отдельно</span>'
        : `<span>Вес: <strong>${crit.weight}</strong></span>
           <span class="eff-weight-highlight">${crit.applicable ? `После перераспределения: <strong>${fmt2(crit.effective_weight)}</strong>` : 'N/A: вес перераспределён'}</span>`;

      card.innerHTML = `
        <div class="criterion-header">
          <div class="criterion-title-wrap">
            <span class="criterion-section-tag">${escapeHtml((def && def.section) || crit.section || '')}</span>
            <span class="criterion-title">${escapeHtml(crit.title)}</span>
          </div>
          <div class="criterion-badges-wrap">
            ${String(orig) !== String(fin) ? `<span class="ans-badge ${origView.cls}" style="text-decoration: line-through; opacity: .55;" title="Исходная оценка">${origView.text}</span>` : ''}
            <span class="ans-badge ${view.cls}">${view.text}</span>
            ${pointsHtml}
          </div>
        </div>
        <div class="weights-row">${weightsHtml}</div>
        <div class="criterion-explanation">${crit.appeal && crit.appeal.status !== 'open' && crit.appeal.decision ? '<span class="expl-label">Модель:</span> ' : ''}${escapeHtml(crit.explanation || '—')}</div>
        ${rulesHtml}
        ${naHtml}
        ${evidenceHtml}
        ${rangeHtml}
        <div class="criterion-footer">${appealHtml}</div>`;
      criteriaContainer.appendChild(card);
    });
  }

  // --- QUALITY SUMMARY (ring, short criteria list, "why", actions) ---
  function critStatus(def, crit) {
    const a = finalAnswer(crit);
    const isFlag = def ? def.type === 'flag' : crit.weight === 0;
    if (isFlag) return a === 'yes' ? 'flag' : 'ok';
    if (a === 'na' || !crit.applicable) return 'na';
    const pts = Number(crit.points) || 0, w = Number(crit.effective_weight) || 0;
    if (w > 0 && pts >= w - 0.005) return 'ok';
    return pts > 0.005 ? 'part' : 'bad';
  }
  function focusRange(crit) {
    if (!crit) return null;
    const idx = (crit.evidence || []).map(e => Number(e.message_index)).filter(n => !isNaN(n));
    if (idx.length) return [Math.min(...idx), Math.max(...idx)];
    // the checked range only matters when the action was missing (no/partial/flag), not for passed or N/A criteria
    const ev = state.evaluations[state.activeConvId];
    const stt = ev ? critStatus(critDef(ev, crit.id), crit) : 'bad';
    if (['bad', 'part', 'flag'].includes(stt) && Array.isArray(crit.checked_range) && crit.checked_range.length === 2) return [Number(crit.checked_range[0]), Number(crit.checked_range[1])];
    return null;
  }
  function selectedCrit() {
    const ev = state.evaluations[state.activeConvId];
    return ev && (ev.criteria || []).find(c => c.id === state.selCrit) || null;
  }

  function renderQualitySummary(ev) {
    const ring = document.getElementById('score-ring');
    const list = document.getElementById('criteria-summary');
    const C = 2 * Math.PI * 50;
    if (ring) {
      const t = ev ? ev.total : null;
      ring.style.strokeDashoffset = (t === null || t === undefined) ? C : C * (1 - Math.max(0, Math.min(100, t)) / 100);
      ring.setAttribute('class', 'ring-fg lvl-' + (ev && ev.gross_error ? 'low' : scoreLevel(t)));
    }
    if (!ev || ev.status === 'error') {
      if (list) list.innerHTML = '';
      state.selCrit = null;
      applyFocus();
      return;
    }
    const crits = ev.criteria || [];
    // default selection: the first criterion that lost points (or a gross error flag)
    if (!crits.find(c => c.id === state.selCrit)) {
      const st = c => critStatus(critDef(ev, c.id), c);
      const pick = crits.find(c => st(c) === 'flag') || crits.find(c => ['bad', 'part'].includes(st(c)));
      state.selCrit = pick ? pick.id : null; // nothing lost: no criterion is preselected
    }
    const ICON = { ok: '✓', part: '≈', bad: '✕', na: 'N/A', flag: '!' };
    if (list) {
      list.innerHTML = crits.map(c => {
        const def = critDef(ev, c.id);
        const st = critStatus(def, c);
        const isFlag = def ? def.type === 'flag' : c.weight === 0;
        const pts = isFlag ? (finalAnswer(c) === 'yes' ? '<b>нарушение</b>' : 'нет')
          : st === 'na' ? 'N/A' : `<b>${fmtShort(c.points)}</b> / ${fmtShort(c.effective_weight)}`;
        return `<div class="cs-row cs-${st} ${c.id === state.selCrit ? 'sel' : ''}" data-crit="${escapeHtml(c.id)}" title="${escapeHtml(c.explanation || '')}">
          <span class="cs-ico">${ICON[st]}</span><span class="cs-name">${escapeHtml(c.title)}</span><span class="cs-pts">${pts}</span></div>`;
      }).join('');
      list.querySelectorAll('.cs-row').forEach(r => { r.onclick = () => selectCriterion(r.dataset.crit); });
    }
    applyFocus();
  }
  function fmtShort(n) {
    if (n === null || n === undefined || isNaN(n)) return '—';
    return Number(n).toLocaleString('ru-RU', { maximumFractionDigits: 2 });
  }

  function selectCriterion(id) {
    state.selCrit = id;
    document.querySelectorAll('.cs-row').forEach(r => r.classList.toggle('sel', r.dataset.crit === id));
    document.querySelectorAll('.criterion-card[data-crit]').forEach(r => r.classList.toggle('sel', r.dataset.crit === id));
    applyFocus();
    const range = focusRange(selectedCrit());
    if (range) { const el = document.getElementById(`msg-${range[0]}`); if (el) el.scrollIntoView({ behavior: 'smooth', block: 'nearest' }); }
  }

  // Highlights the selected criterion's evidence in the transcript and on the timeline, fills "why" and the buttons
  function applyFocus() {
    const ev = state.evaluations[state.activeConvId];
    const crit = selectedCrit();
    const range = focusRange(crit);
    document.querySelectorAll('.chat-message-row.focus').forEach(r => r.classList.remove('focus'));
    if (crit) {
      const idx = new Set((crit.evidence || []).map(e => Number(e.message_index)));
      if (!idx.size && range) for (let i = range[0]; i <= range[1]; i++) idx.add(i);
      idx.forEach(i => { const r = document.getElementById(`msg-${i}`); if (r) r.classList.add('focus'); });
    }
    highlightTimeline(range);

    const whyT = document.getElementById('why-title');
    const whyX = document.getElementById('why-text');
    const go = document.getElementById('btn-goto-fragment');
    const dis = document.getElementById('btn-dispute');
    if (!ev || !crit) {
      if (whyT) whyT.textContent = ev ? 'Оценка не снижена' : 'Почему снижена оценка';
      if (whyX) whyX.textContent = ev ? 'Все применимые критерии выполнены. Нажмите на критерий, чтобы увидеть обоснование и реплики.' : 'Диалог ещё не оценён.';
      if (go) go.disabled = true;
      if (dis) dis.disabled = true;
      return;
    }
    const def = critDef(ev, crit.id);
    const st = critStatus(def, crit);
    const lostAny = (ev.criteria || []).some(c => ['bad', 'part', 'flag'].includes(critStatus(critDef(ev, c.id), c)));
    const decided = crit.appeal && crit.appeal.status !== 'open' && crit.appeal.decision;
    if (decided) {
      // After the reviewer's decision the reviewer's reason replaces the model's explanation
      const accepted = crit.appeal.decision === 'change';
      const orig = crit.original_answer !== undefined && crit.original_answer !== null ? crit.original_answer : crit.answer;
      if (whyT) whyT.textContent = accepted ? 'Решение проверяющего: оценка изменена' : 'Решение проверяющего: оценка оставлена';
      if (whyX) whyX.innerHTML = `<span class="why-crit">${escapeHtml(crit.title)}:</span> ${escapeHtml(crit.appeal.reason || '—')}`
        + (accepted ? `<div class="why-sub">Было: ${answerView(def, crit, orig).text} → стало: ${answerView(def, crit).text}</div>` : '');
    } else {
      if (whyT) whyT.textContent = st === 'ok' ? (lostAny ? 'Критерий выполнен' : 'Оценка не снижена') : st === 'na' ? 'Почему N/A' : st === 'flag' ? 'Грубая ошибка' : 'Почему снижена оценка';
      if (whyX) {
        const extra = st === 'na' ? (crit.na_reason || (def && def.na_condition) || '') : '';
        whyX.innerHTML = `<span class="why-crit">${escapeHtml(crit.title)}:</span> ${escapeHtml(extra || crit.explanation || '—')}`;
      }
    }
    if (go) { go.disabled = !range; go.onclick = () => range && scrollToReplica(range[0], range[1]); }
    if (dis) {
      const hasAppeal = Boolean(crit.appeal);
      dis.disabled = hasAppeal || !isLive();
      dis.textContent = hasAppeal ? (crit.appeal.status === 'open' ? 'Апелляция подана' : 'Апелляция рассмотрена') : 'Оспорить оценку';
      dis.title = isLive() ? '' : 'Апелляции работают только с сервером';
      dis.onclick = () => openAppealModal(crit.id);
    }
  }

  // 4. APPEALS VIEW TABLE
  function renderAppealsTable() {
    const tbody = document.getElementById('appeals-table-body');
    if (!tbody) return;

    tbody.innerHTML = '';
    if (state.appeals.length === 0) {
      tbody.innerHTML = '<tr><td colspan="6" style="text-align: center; color: var(--text-muted); padding: 30px;">Апелляций пока нет. Оспорить оценку можно в карточке диалога: кнопка «Оспорить оценку».</td></tr>';
      return;
    }

    state.appeals.forEach(item => {
      const tr = document.createElement('tr');
      const isPending = item.appeal.status === 'open';
      const ev = state.evalById[item.evaluation_id] || {};
      const crit = (ev.criteria || []).find(c => c.id === item.criterion_id) || {};
      const def = critDef(ev, item.criterion_id);
      const orig = crit.original_answer !== undefined && crit.original_answer !== null ? crit.original_answer : crit.answer;
      const accepted = item.appeal.decision === 'change';
      tr.innerHTML = `
        <td><a href="javascript:void(0)" onclick="app.selectConversationAndTab('${escapeHtml(item.conversation_id)}')" style="font-weight: 700; color: var(--primary); text-decoration: none;">${escapeHtml(item.conversation_id)}</a></td>
        <td><strong>${escapeHtml(item.criterion_title)}</strong></td>
        <td><span class="ans-badge ${item.view.cls}">${item.view.text}</span></td>
        <td style="max-width: 320px;">${escapeHtml(item.appeal.comment)}</td>
        <td>
          <span class="badge ${isPending ? 'score-fair' : 'score-perfect'}">${isPending ? 'На рассмотрении' : 'Решено'}</span>
        </td>
        <td>
          ${isPending ? `
            <button class="btn-primary" style="padding: 4px 10px; font-size: 11px;" onclick="app.openReviewModal('${escapeHtml(item.evaluation_id)}', '${escapeHtml(item.criterion_id)}')">
              Принять решение
            </button>
          ` : `
            <div class="ap-decision ${accepted ? 'acc' : 'rej'}">${accepted ? 'Принята' : 'Отклонена'}${accepted ? `: ${answerView(def, crit, orig).text} → ${answerView(def, crit, item.answer).text}` : ''}</div>
            ${accepted && ev.original_total !== ev.total ? `<div class="ap-total">Итог ${fmtShort(ev.original_total)} → <b>${fmtShort(ev.total)}</b></div>` : ''}
            <div class="ap-reason">${escapeHtml(item.appeal.reason || '')}</div>
            <button class="chip-btn" onclick="app.openReviewModal('${escapeHtml(item.evaluation_id)}', '${escapeHtml(item.criterion_id)}')">Открыть</button>
          `}
        </td>
      `;
      tbody.appendChild(tr);
    });
  }

  // 5. CHECKLIST BUILDER TABLE
  function renderChecklistTable() {
    const tbody = document.getElementById('checklist-table-body');
    const indicatorEl = document.getElementById('checklist-weight-indicator');
    const channelSelect = document.getElementById('checklist-channel-select');
    if (!tbody) return;

    renderChecklistActions();
    const checklist = activeChecklist();
    if (!checklist || !checklist.criteria) { tbody.innerHTML = '<tr><td colspan="8" style="text-align:center;padding:24px;color:var(--text-muted)">Чек-листы не загружены</td></tr>'; return; }

    if (channelSelect) {
      channelSelect.value = checklist.applies_to || 'agent chat';
      channelSelect.onchange = (e) => {
        editableChecklist().applies_to = e.target.value;
        renderChecklistPicker();
        renderChecklistTable();
      };
    }

    // Calculate sum of weights for non-flag criteria
    const sumWeights = checklist.criteria.filter(c => c.type !== 'flag').reduce((sum, c) => sum + (Number(c.weight) || 0), 0);
    if (indicatorEl) {
      if (sumWeights === 100) {
        indicatorEl.className = 'weight-indicator-badge weight-indicator-ok';
        indicatorEl.innerHTML = '✓ Сумма весов: <strong>100 / 100</strong> (Сбалансировано)';
      } else {
        indicatorEl.className = 'weight-indicator-badge weight-indicator-warn';
        indicatorEl.innerHTML = `Сумма весов: <strong>${sumWeights}</strong> — итог всё равно приводится к 100 (вес × 100 / сумма применимых)`;
      }
    }

    tbody.innerHTML = '';
    // Draft: mark new / changed criteria against the version it is based on
    const base = state.activeChecklistKey === 'draft' ? state.checklists[checklist.based_on] : null;
    const FIELDS = { title: 'название', section: 'секция', type: 'тип', weight: 'вес', allow_na: 'N/A', na_condition: 'условие N/A', steps: 'шкала', instruction: 'инструкция', rules: 'правила' };
    const diffOf = crit => {
      if (!base) return null;
      const old = (base.criteria || []).find(c => c.id === crit.id);
      if (!old) return { isNew: true };
      const changed = Object.keys(FIELDS).filter(k => JSON.stringify(old[k] ?? null) !== JSON.stringify(crit[k] ?? null));
      return changed.length ? { changed, old } : null;
    };
    checklist.criteria.forEach((crit, idx) => {
      const tr = document.createElement('tr');
      const diff = diffOf(crit);
      if (diff) tr.className = diff.isNew ? 'row-new' : 'row-changed';
      const diffBadge = !diff ? '' : diff.isNew ? '<span class="diff-badge new">новый</span>'
        : `<span class="diff-badge chg" title="${escapeHtml(diff.changed.map(k => FIELDS[k]).join(', '))}">изменён: ${escapeHtml(diff.changed.map(k => FIELDS[k]).join(', '))}</span>`;
      const weightCell = diff && !diff.isNew && diff.changed.includes('weight') ? `<span class="old-val">${diff.old.weight}</span> → <strong>${crit.weight}</strong>` : `<strong>${crit.weight}</strong>`;
      let typeLabel = 'Да / Нет';
      if (crit.type === 'scale') typeLabel = `Шкала [${(crit.steps || []).join(', ')}]`;
      else if (crit.type === 'flag') typeLabel = 'Флаг (Грубая ошибка)';

      tr.innerHTML = `
        <td>${idx + 1}</td>
        <td><span class="badge badge-chat">${escapeHtml(crit.section || '—')}</span></td>
        <td><strong>${escapeHtml(crit.title)}</strong> ${diffBadge}${crit.instruction ? `<div style="font-size:11px;color:var(--text-muted);margin-top:3px">${escapeHtml(crit.instruction)}</div>` : ''}</td>
        <td>${typeLabel}${crit.allow_na && crit.na_condition ? `<div style="font-size:11px;color:var(--text-muted);margin-top:3px">${escapeHtml(crit.na_condition)}</div>` : ''}</td>
        <td>${crit.type === 'flag' ? '—' : weightCell}</td>
        <td>${crit.allow_na ? '<span style="color: var(--success); font-weight: 700;">✓ Да</span>' : '<span style="color: var(--text-muted);">— Нет</span>'}</td>
        <td style="max-width: 280px; font-size: 12px; color: #334155;">
          ${crit.rules ? `<div class="rules-box" style="margin: 0;">${escapeHtml(crit.rules)}</div>` : '<span style="color: var(--text-muted);">—</span>'}
        </td>
        <td>
          <div style="display: flex; gap: 6px;">
            <button class="chip-btn" onclick="app.openEditCriterionModal('${escapeHtml(crit.id)}')">Редактировать</button>
            <button class="chip-btn" style="color: var(--danger);" onclick="app.deleteCriterion('${escapeHtml(crit.id)}')">Удалить</button>
          </div>
        </td>
      `;
      tbody.appendChild(tr);
    });
    if (base) {
      const removed = (base.criteria || []).filter(c => !checklist.criteria.find(x => x.id === c.id));
      if (removed.length) {
        const tr = document.createElement('tr');
        tr.className = 'row-removed';
        tr.innerHTML = `<td colspan="8">Удалены из черновика: ${removed.map(c => `<s>${escapeHtml(c.title)}</s>`).join(', ')}</td>`;
        tbody.appendChild(tr);
      }
    }
  }

  // 6. EXECUTIVE ANALYTICS DASHBOARD
  function renderAnalyticsDashboard() {
    const totalConvs = state.conversations.length;
    if (totalConvs === 0) return;

    const validScores = state.conversations.filter(c => c.total_score !== null && c.total_score !== undefined).map(c => c.total_score);
    const avgScore = validScores.length > 0 ? (validScores.reduce((a, b) => a + b, 0) / validScores.length) : 0;
    const grossErrors = state.conversations.filter(c => c.gross_error).length;
    const appealsCount = state.appeals.length;

    const avgEl = document.getElementById('kpi-avg-score');
    const totalEl = document.getElementById('kpi-total-convs');
    const grossEl = document.getElementById('kpi-gross-errors');
    const appealsEl = document.getElementById('kpi-appeals-count');

    if (avgEl) avgEl.textContent = validScores.length ? avgScore.toFixed(2) : '—';
    if (totalEl) totalEl.textContent = totalConvs;
    if (grossEl) grossEl.textContent = grossErrors;
    if (appealsEl) appealsEl.textContent = appealsCount;

    const st = state.settings;
    const scoredConvs = state.conversations.filter(c => c.total_score !== null && c.total_score !== undefined);
    const excellent = scoredConvs.filter(c => c.total_score >= st.excellent).length;
    const good = scoredConvs.filter(c => c.total_score >= st.good && c.total_score < st.excellent).length;
    const fair = scoredConvs.filter(c => c.total_score < st.good).length;
    const targetEl = document.getElementById('kpi-target-sub');
    if (targetEl) targetEl.textContent = `Целевой уровень (настройка): ≥ ${st.target}%`;
    const totalSub = document.getElementById('kpi-total-sub');
    if (totalSub) totalSub.textContent = `оценено ${scoredConvs.length} · чатов ${state.conversations.filter(c => c.channel === 'agent chat').length} · звонков ${state.conversations.filter(c => c.channel === 'voice call').length}`;

    const distribContainer = document.getElementById('distrib-bars-container');
    if (distribContainer) {
      distribContainer.innerHTML = `
        <div class="distrib-bar-row">
          <span class="distrib-bar-label">≥ ${st.excellent} (настройка):</span>
          <div class="distrib-bar-track"><div class="distrib-bar-fill" style="width: ${(excellent / totalConvs) * 100}%; background: var(--success);"></div></div>
          <span class="distrib-bar-val">${excellent} (${Math.round((excellent / totalConvs) * 100)}%)</span>
        </div>
        <div class="distrib-bar-row">
          <span class="distrib-bar-label">${st.good}–${st.excellent} (настройка):</span>
          <div class="distrib-bar-track"><div class="distrib-bar-fill" style="width: ${(good / totalConvs) * 100}%; background: var(--info);"></div></div>
          <span class="distrib-bar-val">${good} (${Math.round((good / totalConvs) * 100)}%)</span>
        </div>
        <div class="distrib-bar-row">
          <span class="distrib-bar-label">&lt; ${st.good} (настройка):</span>
          <div class="distrib-bar-track"><div class="distrib-bar-fill" style="width: ${(fair / totalConvs) * 100}%; background: var(--warning);"></div></div>
          <span class="distrib-bar-val">${fair} (${Math.round((fair / totalConvs) * 100)}%)</span>
        </div>
        <div class="distrib-bar-row">
          <span class="distrib-bar-label">Грубые ошибки (Комплаенс):</span>
          <div class="distrib-bar-track"><div class="distrib-bar-fill" style="width: ${(grossErrors / totalConvs) * 100}%; background: var(--danger);"></div></div>
          <span class="distrib-bar-val" style="color: var(--danger);">${grossErrors} (${Math.round((grossErrors / totalConvs) * 100)}%)</span>
        </div>
      `;
    }

    updateExportButton();
    const grid = document.getElementById('lang-channel-grid');
    if (grid) {
      const n = totalConvs;
      const row = (label, k) => `<div class="stat-pill-row"><span>${label}</span><strong>${k} (${n ? Math.round(k / n * 100) : 0}%)</strong></div>`;
      const langs = [...new Set(state.conversations.map(c => c.language))];
      grid.innerHTML = row('💬 Чаты (agent chat):', state.conversations.filter(c => c.channel === 'agent chat').length)
        + row('📞 Звонки (voice call):', state.conversations.filter(c => c.channel === 'voice call').length)
        + langs.map(l => row(`${escapeHtml(l)}:`, state.conversations.filter(c => c.language === l).length)).join('');
    }

    const critTableBody = document.getElementById('analytics-criteria-body');
    if (critTableBody) {
      const checklist = activeChecklist();
      if (checklist && checklist.criteria) {
        critTableBody.innerHTML = '';
        checklist.criteria.forEach(crit => {
          let passedCount = 0;
          let applicableCount = 0;

          for (const cid in state.evaluations) {
            const ev = state.evaluations[cid];
            const cItem = ev.criteria && ev.criteria.find(c => c.id === crit.id);
            if (cItem) {
              const a = finalAnswer(cItem);
              if (crit.type === 'flag') {
                applicableCount++;
                if (a === 'no') passedCount++;
              } else if (a !== 'na') {
                applicableCount++;
                if (crit.type === 'yes_no' && a === 'yes') passedCount++;
                else if (crit.type === 'scale' && crit.steps && crit.steps.length) {
                  const maxStep = Math.max(...crit.steps.map(Number));
                  if (maxStep > 0) passedCount += Number(a || 0) / maxStep;
                }
              }
            }
          }

          const complianceRate = applicableCount > 0 ? Math.round((passedCount / applicableCount) * 100) : null;
          let statusBadge = '<span class="badge score-fair">нет данных</span>';
          if (complianceRate !== null) {
            statusBadge = complianceRate >= st.target ? `<span class="badge score-perfect">≥ ${st.target}% (настройка)</span>`
              : complianceRate >= st.good ? `<span class="badge score-fair">${st.good}–${st.target}% (настройка)</span>`
              : `<span class="badge score-poor">&lt; ${st.good}% (настройка)</span>`;
          }

          const tr = document.createElement('tr');
          tr.innerHTML = `
            <td><strong>${escapeHtml(crit.title)}</strong></td>
            <td><span class="badge badge-chat">${escapeHtml(crit.section || '—')}</span></td>
            <td><strong>${crit.type === 'flag' ? 'флаг' : crit.weight}</strong></td>
            <td>
              <div style="display: flex; align-items: center; gap: 8px;">
                <div style="width: 80px; height: 6px; background: #e2e8f0; border-radius: 3px; overflow: hidden;">
                  <div style="width: ${complianceRate || 0}%; height: 100%; background: ${complianceRate === null ? '#cbd5e1' : complianceRate >= st.target ? 'var(--primary)' : (complianceRate >= st.good ? 'var(--warning)' : 'var(--danger)')};"></div>
                </div>
                <strong>${complianceRate === null ? '—' : complianceRate + '%'}</strong>
              </div>
            </td>
            <td>${statusBadge}</td>
          `;
          critTableBody.appendChild(tr);
        });
      }
    }
  }

  // --- ACTIONS & MODALS ---
  // Appeal: POST /api/evaluations/{id}/appeals {criterion_id, comment}
  function openAppealModal(critId) {
    const ev = state.evaluations[state.activeConvId];
    if (!ev) return;
    const crit = (ev.criteria || []).find(c => c.id === critId);
    if (!crit) return;
    if (!isLive()) { toast('Апелляции сохраняются на сервере. Сейчас открыты демо-данные — подключите сервер.'); return; }
    document.getElementById('appeal-modal-criterion').value = crit.title;
    document.getElementById('appeal-modal-criterion-id').value = critId;
    document.getElementById('appeal-modal-current-score').value = answerView(critDef(ev, critId), crit).text;
    document.getElementById('appeal-modal-comment').value = '';
    const modal = document.getElementById('appeal-modal');
    if (modal) modal.classList.add('active');
  }

  async function submitAppeal() {
    const critId = document.getElementById('appeal-modal-criterion-id').value;
    const comment = document.getElementById('appeal-modal-comment').value.trim();
    if (!comment) { toast('Укажите комментарий с аргументацией'); return; }
    const ev = state.evaluations[state.activeConvId];
    if (!ev) return;
    try {
      await api().createAppeal(ev.id, critId, comment);
      closeModal('appeal-modal');
      await refreshEvaluation(state.activeConvId, ev.id);
      toast('Апелляция подана. Её видит проверяющий во вкладке «Апелляции».');
    } catch (e) {
      toast('Апелляция не отправлена: ' + e.message);
    }
  }

  async function refreshEvaluation(cid, evId) {
    const fresh = await api().getEvaluation(evId);
    if (fresh) {
      if (state.evalById[evId]) fresh._seq = state.evalById[evId]._seq;
      storeEval(fresh);
    }
    if (isLive()) {
      try { state.appealRecords = await api().getAppeals(); } catch (e) { state.appealRecords = null; }
    }
    collectAppeals();
    renderAll();
  }

  // Review decision: POST /api/appeals/{id}/decision {decision: confirm|change, new_answer?, reason}
  function openReviewModal(evalId, critId) {
    const ev = state.evalById[evalId] || Object.values(state.evaluations).find(e => e.id === evalId);
    if (!ev) return;
    const crit = (ev.criteria || []).find(c => c.id === critId);
    if (!crit || !crit.appeal) return;
    const def = critDef(ev, critId) || {};
    document.getElementById('review-modal-conv-id').value = ev.conversation_id;
    document.getElementById('review-modal-eval-id').value = ev.id;
    document.getElementById('review-modal-criterion-id').value = critId;
    setText('review-modal-target', `${ev.conversation_id} — ${crit.title}`);
    document.getElementById('review-modal-current').innerHTML = `<span class="ans-badge ${answerView(def, crit).cls}">${answerView(def, crit).text}</span>`;
    setText('review-modal-explanation', crit.explanation || '—');
    setText('review-modal-operator-comment', crit.appeal.comment || '');
    document.getElementById('review-modal-reason').value = '';

    // fragment: evidence ±2 replicas (transcript of this conversation)
    const frag = document.getElementById('review-modal-fragment');
    const conv = state.conversations.find(c => c.conversation_id === ev.conversation_id);
    const tr = (conv && conv.transcript) || [];
    const range = focusRange(crit);
    if (frag) {
      if (!tr.length) frag.innerHTML = '<div class="hint-muted">Откройте диалог, чтобы увидеть фрагмент.</div>';
      else {
        const lo = range ? Math.max(0, range[0] - 2) : 0, hi = range ? Math.min(tr.length - 1, range[1] + 2) : Math.min(tr.length - 1, 5);
        const evIdx = new Set((crit.evidence || []).map(e => Number(e.message_index)));
        frag.innerHTML = tr.slice(lo, hi + 1).map((m, k) => {
          const i = lo + k;
          return `<div class="frag-row ${evIdx.has(i) ? 'ev' : ''}"><span>#${i} · ${escapeHtml(m.speaker)}</span>${escapeHtml(m.text)}</div>`;
        }).join('');
      }
    }

    // new answer options come from the criterion type in the checklist
    const sel = document.getElementById('review-modal-new-answer');
    if (sel) {
      let opts = [];
      if (def.type === 'scale') opts = (def.steps || []).map(v => [String(v), `${v} из ${Math.max(...def.steps)}${def.step_descriptions && def.step_descriptions[String(v)] ? ' — ' + def.step_descriptions[String(v)] : ''}`]);
      else if (def.type === 'flag') opts = [['yes', 'Нарушение есть'], ['no', 'Нарушения нет']];
      else opts = [['yes', 'Да'], ['no', 'Нет']];
      if (def.allow_na) opts.push(['na', 'N/A']);
      const cur = String(finalAnswer(crit));
      sel.innerHTML = opts.filter(([v]) => v !== cur).map(([v, l]) => `<option value="${escapeHtml(v)}">${escapeHtml(l)}</option>`).join('');
    }
    const decided = crit.appeal.status !== 'open';
    ['btn-review-accept', 'btn-review-reject'].forEach(id => { const b = document.getElementById(id); if (b) { b.disabled = decided || !isLive(); b.style.display = decided ? 'none' : ''; } });
    const grp = document.getElementById('review-new-answer-group'); if (grp) grp.style.display = decided ? 'none' : '';
    const rs = document.getElementById('review-modal-reason');
    if (rs) { rs.value = decided ? (crit.appeal.reason || '') : ''; rs.readOnly = decided; }
    setText('review-modal-status', decided ? (crit.appeal.decision === 'change' ? 'Апелляция принята, оценка изменена.' : 'Апелляция отклонена, оценка оставлена.') : '');
    const modal = document.getElementById('review-modal');
    if (modal) modal.classList.add('active');
  }

  async function findAppealId(ev, critId) {
    const crit = (ev.criteria || []).find(c => c.id === critId);
    if (crit && crit.appeal && (crit.appeal.id || crit.appeal.appeal_id)) return crit.appeal.id || crit.appeal.appeal_id;
    const open = await api().getAppeals('open');
    const list = Array.isArray(open) ? open : (open && open.appeals) || [];
    const hit = list.find(a => a.evaluation_id === ev.id && a.criterion_id === critId);
    if (!hit) throw new Error('открытая апелляция не найдена на сервере');
    return hit.id;
  }

  async function applyReviewDecision(decisionType) {
    const evalId = document.getElementById('review-modal-eval-id').value;
    const critId = document.getElementById('review-modal-criterion-id').value;
    let newAnswer = document.getElementById('review-modal-new-answer').value;
    const reason = document.getElementById('review-modal-reason').value.trim();
    if (!reason) { toast('Укажите причину решения'); return; }
    if (!isLive()) { toast('Решение по апелляции сохраняет сервер.'); return; }
    const ev = state.evalById[evalId];
    if (!ev) return;
    const def = critDef(ev, critId) || {};
    if (decisionType === 'change') {
      if (newAnswer === '') { toast('Выберите новую оценку'); return; }
      if (def.type === 'scale' && newAnswer !== 'na') newAnswer = Number(newAnswer);
    }
    const before = ev.total;
    try {
      const appealId = await findAppealId(ev, critId);
      const updated = await api().decideAppeal(appealId, decisionType, decisionType === 'change' ? newAnswer : undefined, reason);
      closeModal('review-modal');
      if (updated && updated.id) {
        updated._seq = ev._seq;
        storeEval(updated);
        try { state.appealRecords = await api().getAppeals(); } catch (e) { state.appealRecords = null; }
        collectAppeals();
        renderAll();
      }
      else await refreshEvaluation(ev.conversation_id, ev.id);
      const after = (state.evalById[evalId] || {}).total;
      toast(decisionType === 'change' ? `Апелляция принята. Итог: ${fmtShort(before)} → ${fmtShort(after)} (пересчитал сервер)` : 'Апелляция отклонена, оценка оставлена');
    } catch (e) {
      toast('Решение не сохранено: ' + e.message);
    }
  }

  // --- CHECKLIST EDITOR ---
  // Edits go to a local draft; "Сохранить и применить" sends it to the server (POST /api/checklists),
  // activates it and re-evaluates conversations (POST /api/evaluations/batch).
  function editableChecklist() {
    if (state.activeChecklistKey === 'draft' && state.checklists['draft']) return state.checklists['draft'];
    const base = activeChecklist();
    const draft = JSON.parse(JSON.stringify(base || { name: 'Новый чек-лист', version: 0, rounding: 2, criteria: [] }));
    draft.based_on = base && base.id ? base.id : state.activeChecklistKey;
    delete draft.id;
    delete draft.active;
    draft.version = (Number(draft.version) || 0) + 1;
    state.checklists['draft'] = draft;
    state.activeChecklistKey = 'draft';
    saveDraft();
    renderChecklistPicker();
    return draft;
  }
  function saveDraft() {
    try { localStorage.setItem('cqa_checklist_draft', JSON.stringify(state.checklists['draft'])); } catch (e) { /* storage off */ }
  }
  function sectionsOf(cl) {
    return [...new Set((cl && cl.criteria || []).map(c => c.section).filter(Boolean))];
  }
  function fillSectionOptions(current) {
    const sel = document.getElementById('crit-edit-section');
    const list = document.getElementById('crit-edit-section-list');
    const secs = sectionsOf(activeChecklist());
    if (list) list.innerHTML = secs.map(x => `<option value="${escapeHtml(x)}"></option>`).join('');
    if (sel) sel.value = current || '';
  }
  function toggleTypeFields() {
    const type = document.getElementById('crit-edit-type').value;
    const steps = document.getElementById('crit-edit-steps-group');
    const w = document.getElementById('crit-edit-weight-group');
    if (steps) steps.style.display = type === 'scale' ? 'block' : 'none';
    if (w) w.style.display = type === 'flag' ? 'none' : 'block';
    const naChk = document.getElementById('crit-edit-allow-na');
    const naGrp = document.getElementById('crit-edit-na-group');
    if (naGrp && naChk) naGrp.style.display = naChk.checked ? 'block' : 'none';
  }

  function fillCriterionModal(crit, isNew) {
    document.getElementById('criterion-modal-title').textContent = isNew ? 'Новый критерий' : 'Редактирование критерия';
    document.getElementById('crit-edit-id').value = crit.id || '';
    document.getElementById('crit-edit-id').readOnly = !isNew;
    document.getElementById('crit-edit-title').value = crit.title || '';
    fillSectionOptions(crit.section);
    document.getElementById('crit-edit-type').value = crit.type || 'yes_no';
    document.getElementById('crit-edit-weight').value = crit.weight !== undefined ? crit.weight : '';
    document.getElementById('crit-edit-allow-na').checked = !!crit.allow_na;
    document.getElementById('crit-edit-na-condition').value = crit.na_condition || '';
    document.getElementById('crit-edit-steps').value = (crit.steps || []).join(', ');
    document.getElementById('crit-edit-instruction').value = crit.instruction || '';
    document.getElementById('crit-edit-rules').value = crit.rules || '';
    toggleTypeFields();
    const modal = document.getElementById('criterion-modal');
    if (modal) modal.classList.add('active');
  }

  function openEditCriterionModal(critId) {
    const crit = (activeChecklist().criteria || []).find(c => c.id === critId);
    if (crit) fillCriterionModal(crit, false);
  }

  function openAddCriterionModal() {
    fillCriterionModal({ id: '', type: 'yes_no', allow_na: false, section: sectionsOf(activeChecklist())[0] || '' }, true);
  }

  function saveCriterion() {
    const id = document.getElementById('crit-edit-id').value.trim();
    const title = document.getElementById('crit-edit-title').value.trim();
    const section = document.getElementById('crit-edit-section').value.trim();
    const type = document.getElementById('crit-edit-type').value;
    const weight = type === 'flag' ? 0 : Number(document.getElementById('crit-edit-weight').value);
    const allowNa = document.getElementById('crit-edit-allow-na').checked;
    const naCondition = document.getElementById('crit-edit-na-condition').value.trim();
    const steps = document.getElementById('crit-edit-steps').value.split(/[,;\s]+/).filter(Boolean).map(Number);
    const instruction = document.getElementById('crit-edit-instruction').value.trim();
    const rules = document.getElementById('crit-edit-rules').value.trim();

    if (!/^[a-z0-9_]+$/i.test(id)) { toast('ID: латиница, цифры и _ (например, closing)'); return; }
    if (!title) { toast('Укажите название критерия'); return; }
    if (!instruction) { toast('Укажите инструкцию по оценке — по ней оценивает модель'); return; }
    if (type !== 'flag' && !(weight >= 0)) { toast('Вес должен быть числом ≥ 0'); return; }
    if (type === 'scale' && (steps.length < 2 || steps.some(isNaN) || steps.some(v => v < 0) || Math.max(...steps) <= 0)) { toast('Шкала: минимум 2 неотрицательные ступени, верхняя > 0'); return; }
    if (allowNa && !naCondition) { toast('Укажите условие N/A'); return; }

    const isNew = !document.getElementById('crit-edit-id').readOnly;
    const draft = editableChecklist();
    if (isNew && draft.criteria.some(c => c.id === id)) { toast('Критерий с таким ID уже есть'); return; }
    let crit = draft.criteria.find(c => c.id === id);
    if (!crit) {
      crit = { id, order: Math.max(0, ...draft.criteria.map(c => c.order || 0)) + 1 };
      draft.criteria.push(crit);
    }
    Object.assign(crit, { title, section, type, weight, allow_na: allowNa, na_condition: allowNa ? naCondition : null, instruction, rules });
    if (type === 'scale') {
      const old = crit.step_descriptions || {};
      crit.steps = steps.sort((x, y) => y - x);
      crit.step_descriptions = Object.fromEntries(crit.steps.map(v => [String(v), old[String(v)] || '']));
    } else { delete crit.steps; delete crit.step_descriptions; }

    saveDraft();
    closeModal('criterion-modal');
    renderChecklistPicker();
    renderChecklistTable();
  }

  function deleteCriterion(critId) {
    if (!confirm(`Удалить критерий ${critId} из чек-листа? Изменение попадёт в черновик.`)) return;
    const draft = editableChecklist();
    draft.criteria = draft.criteria.filter(c => c.id !== critId);
    saveDraft();
    renderChecklistPicker();
    renderChecklistTable();
  }

  function resetChecklist() {
    if (!state.checklists['draft']) { toast('Несохранённых изменений нет'); return; }
    if (!confirm('Удалить черновик и вернуться к сохранённому чек-листу?')) return;
    const basedOn = state.checklists['draft'].based_on;
    delete state.checklists['draft'];
    try { localStorage.removeItem('cqa_checklist_draft'); } catch (e) { /* storage off */ }
    state.activeChecklistKey = state.checklists[basedOn] ? basedOn : Object.keys(state.checklists)[0];
    renderChecklistPicker();
    renderAll();
  }

  // Save the draft as a new version on the server and make it active for its channel
  async function saveChecklistVersion() {
    if (!isLive()) { toast('Сохранение чек-листа выполняет сервер. Сейчас открыты демо-данные; черновик сохранён в браузере.'); return; }
    const draft = state.checklists['draft'];
    if (!draft) { toast('Изменений нет: отредактируйте или добавьте критерий'); return; }
    const btn = document.getElementById('btn-save-checklist');
    if (btn) { btn.disabled = true; btn.textContent = '⏳ Сохраняем…'; }
    try {
      const body = Object.assign({}, draft);
      const base = state.checklists[draft.based_on];
      delete body.based_on; delete body.active; delete body.id;
      if (base && base.name === body.name && base.applies_to === body.applies_to) body.base_id = base.id;
      const saved = await api().saveChecklist(body);
      const newId = saved && (saved.id || saved.checklist_id);
      if (!newId) throw new Error('сервер не вернул id чек-листа');
      await api().activateChecklist(newId);
      delete state.checklists['draft'];
      try { localStorage.removeItem('cqa_checklist_draft'); } catch (e) { /* storage off */ }
      state.checklists = await api().getChecklists();
      state.activeChecklistKey = newId;
      renderChecklistPicker();
      renderChecklistTable();
      toast(`Сохранено: «${saved.name}» v${saved.version}, активна для канала «${saved.applies_to}». Теперь нажмите «Применить к диалогам и пересчитать».`);
    } catch (e) {
      toast('Не удалось сохранить чек-лист: ' + e.message);
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = 'Сохранить версию'; }
      renderChecklistActions();
    }
  }

  // Re-evaluate all conversations of the checklist's channel with the saved version (progress in the list)
  async function applyChecklistToDialogs() {
    if (!isLive()) { toast('Пересчёт оценок выполняет сервер. Сейчас открыты демо-данные.'); return; }
    if (state.activeChecklistKey === 'draft') { toast('Сначала нажмите «Сохранить версию»'); return; }
    const cl = activeChecklist();
    const channel = cl.applies_to || 'agent chat';
    const ids = state.conversations.filter(c => c.channel === channel).map(c => c.conversation_id);
    if (!ids.length) { toast('Нет диалогов канала ' + channel); return; }
    if (!confirm(`Пересчитать ${ids.length} диалог(ов) канала «${channel}» по чек-листу «${cl.name}» v${cl.version}? Для каждого диалога сервер вызовет модель.`)) return;
    if (state.batch) { toast('Пакетная оценка уже идёт'); return; }
    try {
      const job = await api().recalculateChecklist(cl.id);
      toast(`Пересчёт запущен на сервере: ${job.total || ids.length} диалог(ов). Прогресс — в меню слева; можно работать дальше.`, 'ok');
      await followJob(job, `Пересчёт по v${cl.version}`);
    } catch (e) {
      toast('Не удалось запустить пересчёт: ' + e.message);
    }
  }

  function renderChecklistActions() {
    const save = document.getElementById('btn-save-checklist');
    const apply = document.getElementById('btn-apply-checklist-changes');
    const hasDraft = state.activeChecklistKey === 'draft';
    if (save) save.disabled = !hasDraft;
    if (apply) apply.disabled = hasDraft || !isLive();
    const note = document.getElementById('checklist-draft-note');
    if (note) {
      const cl = activeChecklist();
      note.textContent = hasDraft ? 'Есть несохранённые изменения (черновик в браузере). Нажмите «Сохранить версию».'
        : cl ? `Версия v${cl.version}${cl.active ? ' — активна для канала' : ''}. Изменения создадут новую версию.` : '';
    }
  }

  // --- NEW CONVERSATION FROM JSON (POST /api/conversations) ---
  function parseRecords(text) {
    let data;
    try { data = JSON.parse(text); }
    catch (e) {
      const m = String(e.message).match(/line (\d+) column (\d+)/);
      throw new Error('это не JSON: проверьте скобки, кавычки и запятые' + (m ? ` (строка ${m[1]}, символ ${m[2]})` : ''));
    }
    let recs;
    if (Array.isArray(data)) recs = data;
    else if (data && data.data && Array.isArray(data.data.records)) recs = data.data.records;
    else if (data && Array.isArray(data.records)) recs = data.records;
    else if (data && data.conversation_id) recs = [data];
    else throw new Error('не найден conversation_id или data.records');
    recs.forEach((r, i) => {
      const where = `запись ${i + 1}`;
      if (!r.conversation_id) throw new Error(`${where}: нет conversation_id`);
      if (!['agent chat', 'voice call'].includes(r.channel)) throw new Error(`${where}: channel должен быть "agent chat" или "voice call"`);
      if (!Array.isArray(r.transcript) || !r.transcript.length) throw new Error(`${where}: пустой transcript`);
      r.transcript.forEach((m, k) => {
        if (!m || typeof m.text !== 'string' || !m.speaker) throw new Error(`${where}, реплика #${k}: нужны speaker и text`);
        if (!['client', 'operator', 'bot', 'system'].includes(m.speaker)) throw new Error(`${where}, реплика #${k}: speaker «${m.speaker}» не из client/operator/bot/system`);
      });
      r.language = String(r.language || '').toUpperCase();
      if (!['RU', 'KZ', 'EN'].includes(r.language)) throw new Error(`${where}: language должен быть RU, KZ или EN`);
    });
    return recs;
  }
  function openUploadModal() {
    document.getElementById('upload-text').value = '';
    document.getElementById('upload-file').value = '';
    setText('upload-check', isLive() ? '' : 'Сейчас открыты демо-данные: загрузка работает только с сервером.');
    const m = document.getElementById('upload-modal');
    if (m) m.classList.add('active');
  }
  function checkUploadText() {
    const box = document.getElementById('upload-check');
    const txt = document.getElementById('upload-text').value.trim();
    if (!txt) { box.textContent = ''; return null; }
    try {
      const recs = parseRecords(txt);
      box.className = 'upload-check ok';
      const dup = recs.filter(r => state.conversations.some(c => c.conversation_id === String(r.conversation_id)));
      box.textContent = `✓ ${recs.length} диалог(ов): ` + recs.map(r => `${r.conversation_id} (${r.channel}, ${r.language || '—'}, ${r.transcript.length} реплик)`).join('; ')
        + (dup.length ? ` — внимание: ${dup.map(r => r.conversation_id).join(', ')} уже есть, диалог будет заменён` : '');
      return recs;
    } catch (e) {
      box.className = 'upload-check bad';
      box.textContent = '✕ ' + e.message;
      return null;
    }
  }
  async function submitUpload() {
    const recs = checkUploadText();
    if (!recs) { toast('Проверьте JSON'); return; }
    if (!isLive()) { toast('Загрузка нового диалога работает только с сервером.'); return; }
    const btn = document.getElementById('btn-upload-submit');
    if (btn) { btn.disabled = true; btn.textContent = '⏳ Загружаем…'; }
    try {
      await api().addConversations(recs);
      closeModal('upload-modal');
      state.conversations = await api().getConversations();
      loadTranscriptsInBackground();
      const ids = recs.map(r => String(r.conversation_id));
      state.activeConvId = ids[0];
      await selectConversation(ids[0]);
      renderAll();
      if (document.getElementById('upload-evaluate').checked) await evaluateAll(ids, 'Оценка новых');
    } catch (e) {
      toast('Не удалось загрузить: ' + e.message);
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = 'Загрузить на сервер'; }
    }
  }

  // Display thresholds (settings, not part of the task rules)
  function openSettingsModal() {
    document.getElementById('set-good').value = state.settings.good;
    document.getElementById('set-excellent').value = state.settings.excellent;
    document.getElementById('set-target').value = state.settings.target;
    const m = document.getElementById('settings-modal');
    if (m) m.classList.add('active');
  }
  function saveSettingsModal() {
    const g = Number(document.getElementById('set-good').value);
    const e = Number(document.getElementById('set-excellent').value);
    const t = Number(document.getElementById('set-target').value);
    if (![g, e, t].every(v => v >= 0 && v <= 100) || g > e) { toast('Пороги: числа 0–100, «хорошо» ≤ «отлично»'); return; }
    state.settings = { good: g, excellent: e, target: t };
    saveSettings();
    closeModal('settings-modal');
    renderAll();
  }

  // API Configuration Modal
  function openApiModal() {
    const urlInput = document.getElementById('api-url-input');
    if (urlInput) urlInput.value = api().getBaseUrl();
    const st = document.getElementById('api-modal-state');
    if (st) st.textContent = isLive() ? 'Подключено к серверу.' : 'Сервер не отвечает — показаны демо-данные.';
    const modal = document.getElementById('api-modal');
    if (modal) modal.classList.add('active');
  }

  function saveApiConfig() {
    const urlInput = document.getElementById('api-url-input');
    api().setBaseUrl(urlInput ? urlInput.value : '');
    closeModal('api-modal');
    loadData();
  }

  function download(filename, text, mime) {
    const blob = new Blob([text], { type: mime });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url; link.download = filename;
    document.body.appendChild(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  function exportRows() {
    return state.conversations.map(c => ({ conv: c, ev: state.evaluations[c.conversation_id] || null }));
  }
  function exportCSV() {
    const critIds = [];
    exportRows().forEach(({ ev }) => (ev && ev.criteria || []).forEach(k => { if (!critIds.find(x => x.id === k.id)) critIds.push({ id: k.id, title: k.title }); }));
    const head = ['conversation_id', 'channel', 'language', 'status', 'total', 'original_total', 'gross_error', 'gross_reason', 'model', 'cost_usd', 'checklist_id', 'checklist_version', 'open_appeals']
      .concat(critIds.map(k => `${k.title} (${k.id})`));
    const esc = v => { const t = v === null || v === undefined ? '' : String(v); return /[";\n]/.test(t) ? '"' + t.replace(/"/g, '""') + '"' : t; };
    const lines = [head.map(esc).join(';')];
    exportRows().forEach(({ conv, ev }) => {
      const row = [conv.conversation_id, conv.channel, conv.language, ev ? ev.status : 'не оценён', ev ? ev.total : '', ev ? ev.original_total : '',
        ev ? (ev.gross_error ? 'да' : 'нет') : '', ev && ev.gross_error ? grossReason(conv.conversation_id) : '', ev ? ev.model : '', ev ? ev.cost_usd : '',
        ev ? ev.checklist_id : '', ev ? ev.checklist_version : '', ev ? (ev.criteria || []).filter(k => k.appeal && k.appeal.status === 'open').length : ''];
      critIds.forEach(k => { const c = ev && (ev.criteria || []).find(x => x.id === k.id); row.push(c ? finalAnswer(c) : ''); });
      lines.push(row.map(esc).join(';'));
    });
    download(`cqa_results_${new Date().toISOString().slice(0, 10)}.csv`, '\uFEFF' + lines.join('\n'), 'text/csv;charset=utf-8');
  }

  // "Экспорт результатов (JSON)": the server builds the file (GET /api/export). Disabled until the route exists.
  async function updateExportButton() {
    const btn = document.getElementById('btn-export-server');
    if (!btn) return;
    if (state.exportAvailable === undefined && isLive()) state.exportAvailable = await api().hasRoute(EXPORT_PATH);
    const on = isLive() && state.exportAvailable === true;
    btn.disabled = !on;
    btn.title = !isLive() ? 'Только с сервером' : on ? 'Скачать все результаты с сервера' : 'Маршрут экспорта на сервере ещё не готов (GET /api' + EXPORT_PATH + ')';
  }
  async function exportFromServer() {
    const btn = document.getElementById('btn-export-server');
    if (btn) { btn.disabled = true; btn.textContent = '⏳ Готовим файл…'; }
    try {
      const data = await api().exportResults();
      download(`cqa_export_${new Date().toISOString().slice(0, 10)}.json`, JSON.stringify(data, null, 2), 'application/json');
    } catch (e) {
      toast('Экспорт не удался: ' + e.message);
    } finally {
      if (btn) { btn.textContent = '💾 Экспорт результатов (JSON)'; }
      updateExportButton();
    }
  }

  function exportJSON() {
    const payload = {
      exported_at: new Date().toISOString(),
      source: isLive() ? api().getBaseUrl() : 'demo',
      checklist: activeChecklist(),
      results: exportRows().map(({ conv, ev }) => ({ conversation_id: conv.conversation_id, channel: conv.channel, language: conv.language, evaluation: ev ? Object.fromEntries(Object.entries(ev).filter(([k]) => k !== '_seq')) : null })),
    };
    download(`cqa_results_${new Date().toISOString().slice(0, 10)}.json`, JSON.stringify(payload, null, 2), 'application/json');
  }

  function closeModal(modalId) {
    const modal = document.getElementById(modalId);
    if (modal) modal.classList.remove('active');
  }

  function selectConversationAndTab(cid) {
    selectConversation(cid);
    switchTab('tab-workspace');
  }

  function scrollToReplica(msgIndex, toIndex) {
    const last = toIndex !== undefined ? toIndex : msgIndex;
    for (let i = msgIndex; i <= last; i++) {
      const m = document.getElementById(`msg-${i}`);
      if (m) { m.classList.add('pulse-highlight'); setTimeout(() => m.classList.remove('pulse-highlight'), 2200); }
    }
    const el = document.getElementById(`msg-${msgIndex}`);
    if (el) el.scrollIntoView({ behavior: 'smooth', block: 'center' });
    highlightTimeline([msgIndex, last]);
    seekAudioToReplica(msgIndex);
  }

  // For voice calls: jump the recording to the replica time (ISO timestamps, offset from the first one)
  function seekAudioToReplica(i) {
    const conv = state.conversations.find(c => c.conversation_id === state.activeConvId);
    const audio = document.getElementById('html5-audio-element');
    if (!conv || !audio || conv.channel !== 'voice call' || !conv.transcript || !conv.transcript[i]) return;
    const t = ts => { const d = Date.parse(ts); return isNaN(d) ? null : d / 1000; };
    const first = conv.transcript.map(m => t(m.timestamp)).find(x => x !== null);
    const cur = t(conv.transcript[i].timestamp);
    if (first === undefined || cur === null) return;
    try { audio.currentTime = Math.max(0, cur - first); } catch (e) { /* not loaded yet */ }
    const link = document.getElementById('audio-external-link');
    if (link && conv.audio_url) link.href = conv.audio_url.split('#')[0] + '#t=' + Math.floor(Math.max(0, cur - first));
  }

  // --- UI STATE THAT SURVIVES A RELOAD (data itself always comes from the server) ---
  const TAB_HASH = { 'tab-workspace': 'dialogs', 'tab-checklist': 'checklists', 'tab-appeals': 'appeals', 'tab-analytics': 'analytics' };
  function writeHash() {
    const tab = (document.querySelector('.nav-tab-btn.active') || {}).dataset;
    const name = TAB_HASH[(tab && tab.tab) || 'tab-workspace'] || 'dialogs';
    const h = '#' + name + (name === 'dialogs' && state.activeConvId ? '/' + encodeURIComponent(state.activeConvId) : '');
    if (location.hash !== h) history.replaceState(null, '', h);
  }
  function readHash() {
    const [name, id] = location.hash.replace(/^#/, '').split('/');
    const tab = Object.keys(TAB_HASH).find(k => TAB_HASH[k] === name);
    return { tab: tab || null, convId: id ? decodeURIComponent(id) : null };
  }
  function saveUiState() {
    try { sessionStorage.setItem('cqa_ui', JSON.stringify({ filters: state.filters, checklist: state.activeChecklistKey })); } catch (e) { /* storage off */ }
  }
  function restoreUiState() {
    try {
      const ui = JSON.parse(sessionStorage.getItem('cqa_ui') || 'null');
      if (ui && ui.filters) Object.assign(state.filters, ui.filters);
      if (ui && ui.checklist) state.activeChecklistKey = ui.checklist;
    } catch (e) { /* storage off */ }
    const f = state.filters;
    const chip = f.status !== 'all' ? f.status : (f.channel !== 'all' ? f.channel : 'all');
    document.querySelectorAll('.filter-chips button[data-filter]').forEach(b => b.classList.toggle('active', b.dataset.filter === chip));
    document.querySelectorAll('.filter-chips button[data-lang]').forEach(b => b.classList.toggle('active', b.dataset.lang === f.lang));
    const si = document.getElementById('conv-search-input'); if (si) si.value = f.search || '';
    const h = readHash();
    if (h.convId) state.activeConvId = h.convId;
    if (h.tab) switchTab(h.tab);
  }

  function switchTab(tabId) {
    document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.nav-tab-btn').forEach(b => b.classList.remove('active'));

    const tab = document.getElementById(tabId);
    if (tab) tab.classList.add('active');

    const btn = document.querySelector(`[data-tab="${tabId}"]`);
    if (btn) btn.classList.add('active');
    writeHash();
  }

  // --- BIND EVENTS ---
  function bindEvents() {
    document.querySelectorAll('.nav-tab-btn').forEach(btn => {
      btn.onclick = () => switchTab(btn.dataset.tab);
    });

    const chkPicker = document.getElementById('checklist-picker');
    if (chkPicker) {
      chkPicker.onchange = () => {
        state.activeChecklistKey = chkPicker.value;
        saveUiState();
        renderAll();
      };
    }
    const typeSel = document.getElementById('crit-edit-type');
    if (typeSel) typeSel.onchange = toggleTypeFields;
    const naChk = document.getElementById('crit-edit-allow-na');
    if (naChk) naChk.onchange = toggleTypeFields;
    const setBtn = document.getElementById('btn-open-settings');
    if (setBtn) setBtn.onclick = openSettingsModal;
    const setSave = document.getElementById('btn-save-settings');
    if (setSave) setSave.onclick = saveSettingsModal;

    const searchInput = document.getElementById('conv-search-input');
    if (searchInput) {
      searchInput.oninput = (e) => {
        state.filters.search = e.target.value;
        saveUiState();
        renderConversationsList();
      };
    }

    document.querySelectorAll('.filter-chips button[data-filter]').forEach(btn => {
      btn.onclick = () => {
        document.querySelectorAll('.filter-chips button[data-filter]').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        state.filters.channel = btn.dataset.filter === 'chat' || btn.dataset.filter === 'voice' ? btn.dataset.filter : 'all';
        state.filters.status = btn.dataset.filter === 'error' || btn.dataset.filter === 'appeal' ? btn.dataset.filter : 'all';
        saveUiState();
        renderConversationsList();
      };
    });

    document.querySelectorAll('.filter-chips button[data-lang]').forEach(btn => {
      btn.onclick = () => {
        document.querySelectorAll('.filter-chips button[data-lang]').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        state.filters.lang = btn.dataset.lang;
        saveUiState();
        renderConversationsList();
      };
    });

    const submitAppealBtn = document.getElementById('btn-submit-appeal');
    if (submitAppealBtn) submitAppealBtn.onclick = submitAppeal;

    const acceptBtn = document.getElementById('btn-review-accept');
    if (acceptBtn) acceptBtn.onclick = () => applyReviewDecision('change');
    const rejectBtn = document.getElementById('btn-review-reject');
    if (rejectBtn) rejectBtn.onclick = () => applyReviewDecision('confirm');

    const addCritBtn = document.getElementById('btn-add-criterion');
    if (addCritBtn) addCritBtn.onclick = openAddCriterionModal;

    const saveCritBtn = document.getElementById('btn-save-criterion');
    if (saveCritBtn) saveCritBtn.onclick = saveCriterion;

    const upBtn = document.getElementById('btn-open-upload');
    if (upBtn) upBtn.onclick = openUploadModal;
    const upText = document.getElementById('upload-text');
    if (upText) upText.oninput = checkUploadText;
    const upFile = document.getElementById('upload-file');
    if (upFile) upFile.onchange = () => {
      const f = upFile.files[0]; if (!f) return;
      const rd = new FileReader();
      rd.onload = () => { upText.value = rd.result; checkUploadText(); };
      rd.readAsText(f);
    };
    const upSubmit = document.getElementById('btn-upload-submit');
    if (upSubmit) upSubmit.onclick = submitUpload;
    const saveChkBtn = document.getElementById('btn-save-checklist');
    if (saveChkBtn) saveChkBtn.onclick = saveChecklistVersion;
    const resetChkBtn = document.getElementById('btn-reset-checklist');
    if (resetChkBtn) resetChkBtn.onclick = resetChecklist;

    const applyChkBtn = document.getElementById('btn-apply-checklist-changes');
    if (applyChkBtn) applyChkBtn.onclick = applyChecklistToDialogs;

    const exportCsvBtn = document.getElementById('btn-export-csv');
    if (exportCsvBtn) exportCsvBtn.onclick = exportCSV;

    const exportServerBtn = document.getElementById('btn-export-server');
    if (exportServerBtn) exportServerBtn.onclick = exportFromServer;
    const exportJsonBtn = document.getElementById('btn-export-json');
    if (exportJsonBtn) exportJsonBtn.onclick = exportJSON;

    const saveApiBtn = document.getElementById('btn-save-api-config');
    if (saveApiBtn) saveApiBtn.onclick = saveApiConfig;
  }

  function escapeHtml(str) {
    if (!str) return '';
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#039;');
  }

  function formatTimestamp(ts, offset) {
    if (!ts) return '';
    if (ts.includes('T') && offset !== undefined && offset !== null) return formatSec(offset);
    if (ts.includes('T')) {
      try {
        const d = new Date(ts);
        return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
      } catch (e) {
        return ts;
      }
    }
    return ts;
  }

  // Global exports for inline onclick handlers
  window.app = {
    selectConversation,
    selectConversationAndTab,
    scrollToReplica,
    openAppealModal,
    openReviewModal,
    openEditCriterionModal,
    deleteCriterion,
    closeModal,
    openApiModal,
    saveApiConfig,
    openSettingsModal,
    evaluateActive,
    reloadConversations: loadData,
    exportCSV,
    exportJSON
  };

  document.addEventListener('DOMContentLoaded', init);
  window.addEventListener('resize', () => applyFocus());

})();
