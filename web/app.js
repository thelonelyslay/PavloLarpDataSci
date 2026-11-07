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
  function toast(msg) { showError(msg); setTimeout(hideError, 5000); }

  // --- INITIALIZATION ---
  async function init() {
    bindEvents();
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
        state.activeChecklistKey = keys.find(k => (state.checklists[k].applies_to || 'agent chat') === 'agent chat') || keys[0] || 'draft';
      }
      renderChecklistPicker();

      state.conversations = await api().getConversations();
      if (state.conversations.length > 0 && !state.conversations.find(c => c.conversation_id === state.activeConvId)) {
        state.activeConvId = state.conversations[0].conversation_id;
      }
      if (state.activeConvId) await selectConversation(state.activeConvId, true);

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

  async function ensureEvaluationLoaded(cid, force) {
    if (state.evaluations[cid] && !force) return;
    const conv = state.conversations.find(c => c.conversation_id === cid);
    if (!conv) return;
    try {
      const ev = await api().getLatestEvaluation(conv);
      if (ev) { state.evaluations[cid] = ev; syncConvFromEval(conv, ev); }
      else delete state.evaluations[cid];
    } catch (e) {
      console.warn('Не удалось загрузить оценку для', cid, e);
    }
  }

  function syncConvFromEval(conv, ev) {
    conv.total_score = ev.total;
    conv.gross_error = Boolean(ev.gross_error);
    conv.last_evaluation_id = ev.id;
    conv.has_appeal = (ev.criteria || []).some(c => c.appeal && c.appeal.status === 'open');
  }

  function renderChecklistPicker() {
    const sel = document.getElementById('checklist-picker');
    if (!sel) return;
    sel.innerHTML = Object.entries(state.checklists).map(([k, c]) => {
      const n = (c.criteria || []).length;
      const label = k === 'draft' ? `Черновик (не сохранён): ${c.name || ''}` : `${c.name || k} v${c.version || 1}`;
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
    for (const cid in state.evaluations) {
      const ev = state.evaluations[cid];
      if (ev && ev.criteria) {
        for (const crit of ev.criteria) {
          if (crit.appeal) {
            state.appeals.push({
              conversation_id: cid,
              criterion_id: crit.id,
              criterion_title: crit.title,
              answer: finalAnswer(crit),
              view: answerView(critDef(ev, crit.id), crit),
              appeal: crit.appeal
            });
          }
        }
      }
    }
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

  // 1. CONVERSATIONS SIDEBAR
  function renderConversationsList() {
    const listEl = document.getElementById('conversations-list');
    if (!listEl) return;

    let items = state.conversations.filter(conv => {
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

      const lvl = scoreLevel(conv.total_score);
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
        <div class="conv-card-bottom">
          <span class="score-pill ${scoreClass}">
            ★ ${conv.total_score !== null && conv.total_score !== undefined ? fmt2(conv.total_score) : (conv.last_evaluation_id ? 'итог не рассчитан' : 'не оценён')}
          </span>
          <div style="display: flex; gap: 6px;">
            ${conv.gross_error ? '<span class="alert-icon-pill" title="Грубая ошибка!">⚠️ Нарушение</span>' : ''}
            ${conv.has_appeal ? '<span style="font-size: 11px; color: var(--warning); font-weight: 700;" title="Подана апелляция">⚖️ Апелляция</span>' : ''}
          </div>
        </div>
      `;
      listEl.appendChild(card);
    });
  }

  async function selectConversation(cid, quiet) {
    state.activeConvId = cid;
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
    const cl = activeChecklist();
    if (!cid || !cl) return;
    if (!isLive()) { toast('Оценка работает только с сервером. Сейчас открыты демо-данные — подключите сервер (кнопка статуса справа сверху).'); return; }
    if (state.activeChecklistKey === 'draft') { toast('Чек-лист — несохранённый черновик. Сначала нажмите «Сохранить и применить» во вкладке чек-листа.'); return; }
    state.evaluating = cid;
    renderTranscriptActions();
    try {
      const ev = await api().evaluate(cid, cl.id || state.activeChecklistKey);
      state.evaluations[cid] = ev;
      const conv = state.conversations.find(c => c.conversation_id === cid);
      if (conv) syncConvFromEval(conv, ev);
      if (ev.status === 'error') toast('Оценка завершилась ошибкой: ' + (ev.error || 'см. сервер'));
    } catch (e) {
      toast('Не удалось оценить диалог: ' + e.message);
    } finally {
      state.evaluating = null;
      collectAppeals();
      renderAll();
    }
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

    if (titleEl) {
      titleEl.innerHTML = `
        <span>${escapeHtml(conv.conversation_id)}</span>
        <span class="badge ${conv.channel === 'voice call' ? 'badge-voice' : 'badge-chat'}">${escapeHtml(conv.channel)}</span>
        <span class="badge badge-${String(conv.language || '').toLowerCase()}">${escapeHtml(conv.language)}</span>
      `;
    }
    if (subEl) {
      const msgCount = conv.transcript ? conv.transcript.length : 0;
      subEl.textContent = `Всего реплик: ${msgCount} · Исходный канал: ${conv.channel}`;
    }

    // Audio Bar for Voice Calls
    if (voiceBar) {
      if (conv.channel === 'voice call' && conv.audio_url) {
        voiceBar.style.display = 'flex';
        const extLink = document.getElementById('audio-external-link');
        if (extLink) extLink.href = conv.audio_url;
        setupVoiceAudio(conv.audio_url);
      } else {
        voiceBar.style.display = 'none';
      }
    }

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

        row.innerHTML = `
          <div class="msg-header">
            <span class="replica-badge">#${idx}</span>
            <span class="speaker-tag speaker-${msg.speaker}">${speakerLabel}</span>
            <span class="msg-time">${formatTimestamp(msg.timestamp)}</span>
          </div>
          <div class="msg-bubble">${escapeHtml(msg.text)}</div>
        `;
        streamEl.appendChild(row);
      });
    }
  }

  function setupVoiceAudio(audioUrl) {
    const playBtn = document.getElementById('audio-play-btn');
    const seekInput = document.getElementById('audio-seek');
    const curTime = document.getElementById('audio-time-current');
    const durTime = document.getElementById('audio-time-duration');
    const speedSel = document.getElementById('audio-speed');
    const audio = document.getElementById('html5-audio-element');

    if (!audio) return;
    audio.src = audioUrl;

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
      if (audio.duration) {
        const pct = (audio.currentTime / audio.duration) * 100;
        if (seekInput) seekInput.value = pct;
        if (curTime) curTime.textContent = formatSec(audio.currentTime);
        if (durTime) durTime.textContent = formatSec(audio.duration);
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

    if (!ev) {
      if (totalEl) totalEl.textContent = '—';
      if (badgeEl) { badgeEl.className = 'score-label-badge badge-warning'; badgeEl.textContent = 'Не оценён'; }
      if (grossBanner) grossBanner.style.display = 'none';
      if (sectionsSummary) sectionsSummary.innerHTML = '';
      setText('eval-model-tag', 'Модель: —');
      setText('eval-cost-tag', 'Стоимость: —');
      setText('eval-version-tag', 'Чек-лист: —');
      if (demoTag) demoTag.style.display = 'none';
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

    if (totalEl) totalEl.textContent = ev.total !== null && ev.total !== undefined ? fmt2(ev.total) : '—';

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
      origEl.textContent = changed ? `исходно ${fmt2(ev.original_total)}` : '';
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

    if (!criteriaContainer) return;
    criteriaContainer.innerHTML = '';
    if (ev.status === 'error') {
      criteriaContainer.innerHTML = `<div class="empty-eval">Сервер не смог оценить диалог: ${escapeHtml(ev.error || ev.message || 'неизвестная ошибка')}.<br>Нажмите «↻ Переоценить».</div>`;
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
      card.className = 'criterion-card';

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
              ${open ? `<button class="btn-primary" style="padding: 2px 8px; font-size: 11px;" onclick="app.openReviewModal('${escapeHtml(state.activeConvId)}', '${escapeHtml(crit.id)}')">Рассмотреть</button>` : ''}
            </div>
            <div style="margin-top: 4px; color: #92400e;"><strong>Апелляция:</strong> ${escapeHtml(crit.appeal.comment)}</div>
            ${crit.appeal.decision ? `
              <div style="margin-top: 4px; color: #065f46; border-top: 1px dashed #d1fae5; padding-top: 4px;">
                <strong>Решение:</strong> ${crit.appeal.decision === 'change' ? 'оценка изменена' : 'оценка подтверждена'}. Причина: ${escapeHtml(crit.appeal.reason || '—')}
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
        <div class="criterion-explanation">${escapeHtml(crit.explanation || '—')}</div>
        ${rulesHtml}
        ${naHtml}
        ${evidenceHtml}
        ${rangeHtml}
        <div class="criterion-footer">${appealHtml}</div>`;
      criteriaContainer.appendChild(card);
    });
  }

  // 4. APPEALS VIEW TABLE
  function renderAppealsTable() {
    const tbody = document.getElementById('appeals-table-body');
    if (!tbody) return;

    tbody.innerHTML = '';
    if (state.appeals.length === 0) {
      tbody.innerHTML = '<tr><td colspan="6" style="text-align: center; color: var(--text-muted); padding: 30px;">Нет активных апелляций</td></tr>';
      return;
    }

    state.appeals.forEach(item => {
      const tr = document.createElement('tr');
      const isPending = item.appeal.status === 'open';
      tr.innerHTML = `
        <td><a href="javascript:void(0)" onclick="app.selectConversationAndTab('${escapeHtml(item.conversation_id)}')" style="font-weight: 700; color: var(--primary); text-decoration: none;">${escapeHtml(item.conversation_id)}</a></td>
        <td><strong>${escapeHtml(item.criterion_title)}</strong></td>
        <td><span class="ans-badge ${item.view.cls}">${item.view.text}</span></td>
        <td style="max-width: 320px;">${escapeHtml(item.appeal.comment)}</td>
        <td>
          <span class="badge ${isPending ? 'score-fair' : 'score-perfect'}">${isPending ? 'На рассмотрении' : 'Завершено'}</span>
        </td>
        <td>
          ${isPending ? `
            <button class="btn-primary" style="padding: 4px 10px; font-size: 11px;" onclick="app.openReviewModal('${escapeHtml(item.conversation_id)}', '${escapeHtml(item.criterion_id)}')">
              Принять решение
            </button>
          ` : `
            <span style="font-size: 11px; color: var(--text-muted);">Решение принято</span>
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
        indicatorEl.innerHTML = `⚠️ Внимание: Сумма базовых весов равна <strong>${sumWeights} / 100</strong>! (Рекомендуется 100)`;
      }
    }

    tbody.innerHTML = '';
    checklist.criteria.forEach((crit, idx) => {
      const tr = document.createElement('tr');
      let typeLabel = 'Да / Нет';
      if (crit.type === 'scale') typeLabel = `Шкала [${(crit.steps || []).join(', ')}]`;
      else if (crit.type === 'flag') typeLabel = 'Флаг (Грубая ошибка)';

      tr.innerHTML = `
        <td>${idx + 1}</td>
        <td><span class="badge badge-chat">${escapeHtml(crit.section || '—')}</span></td>
        <td><strong>${escapeHtml(crit.title)}</strong>${crit.instruction ? `<div style="font-size:11px;color:var(--text-muted);margin-top:3px">${escapeHtml(crit.instruction)}</div>` : ''}</td>
        <td>${typeLabel}${crit.allow_na && crit.na_condition ? `<div style="font-size:11px;color:var(--text-muted);margin-top:3px">${escapeHtml(crit.na_condition)}</div>` : ''}</td>
        <td><strong>${crit.weight}</strong></td>
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
    } catch (e) {
      toast('Апелляция не отправлена: ' + e.message);
    }
  }

  async function refreshEvaluation(cid, evId) {
    const fresh = await api().getEvaluation(evId);
    if (fresh) {
      state.evaluations[cid] = fresh;
      const conv = state.conversations.find(c => c.conversation_id === cid);
      if (conv) syncConvFromEval(conv, fresh);
    }
    collectAppeals();
    renderAll();
  }

  // Review decision: POST /api/appeals/{id}/decision {decision, new_answer?, reason}
  function openReviewModal(convId, critId) {
    const ev = state.evaluations[convId];
    if (!ev) return;
    const crit = (ev.criteria || []).find(c => c.id === critId);
    if (!crit || !crit.appeal) return;
    const def = critDef(ev, critId) || {};
    state.review = { convId, critId };

    document.getElementById('review-modal-target').value = `${convId} — ${crit.title} (сейчас: ${answerView(def, crit).text})`;
    document.getElementById('review-modal-conv-id').value = convId;
    document.getElementById('review-modal-criterion-id').value = critId;
    document.getElementById('review-modal-operator-comment').textContent = crit.appeal.comment;
    document.getElementById('review-modal-reason').value = '';

    // New answer options come from the criterion type in the checklist
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
    const decSel = document.getElementById('review-modal-decision-type');
    const grp = document.getElementById('review-new-answer-group');
    if (decSel && grp) {
      decSel.value = 'confirm';
      grp.style.display = 'none';
      decSel.onchange = () => { grp.style.display = decSel.value === 'change' ? 'block' : 'none'; };
    }
    const modal = document.getElementById('review-modal');
    if (modal) modal.classList.add('active');
  }

  async function findAppealId(ev, critId) {
    const crit = (ev.criteria || []).find(c => c.id === critId);
    if (crit && crit.appeal && (crit.appeal.id || crit.appeal.appeal_id)) return crit.appeal.id || crit.appeal.appeal_id;
    const open = await api().getAppeals('open');
    const list = Array.isArray(open) ? open : (open && open.appeals) || [];
    const hit = list.find(a => (a.evaluation_id === ev.id || a.conversation_id === ev.conversation_id) && a.criterion_id === critId);
    if (!hit) throw new Error('не найден id апелляции на сервере');
    return hit.id;
  }

  async function applyReviewDecision() {
    const convId = document.getElementById('review-modal-conv-id').value;
    const critId = document.getElementById('review-modal-criterion-id').value;
    const decisionType = document.getElementById('review-modal-decision-type').value;
    let newAnswer = document.getElementById('review-modal-new-answer').value;
    const reason = document.getElementById('review-modal-reason').value.trim();
    if (!reason) { toast('Укажите причину решения'); return; }
    if (!isLive()) { toast('Решение по апелляции сохраняет сервер. Сейчас открыты демо-данные.'); return; }

    const ev = state.evaluations[convId];
    if (!ev) return;
    const def = critDef(ev, critId) || {};
    if (decisionType === 'change' && def.type === 'scale' && newAnswer !== 'na') newAnswer = Number(newAnswer);
    try {
      const appealId = await findAppealId(ev, critId);
      await api().decideAppeal(appealId, decisionType, decisionType === 'change' ? newAnswer : undefined, reason);
      closeModal('review-modal');
      await refreshEvaluation(convId, ev.id); // new total comes from the server (scoring.py)
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

  async function applyChecklistChanges() {
    if (!isLive()) { toast('Сохранение чек-листа и пересчёт оценок выполняет сервер. Сейчас открыты демо-данные; черновик сохранён в браузере.'); return; }
    const btn = document.getElementById('btn-apply-checklist-changes');
    if (btn) { btn.disabled = true; btn.textContent = '⏳ Сохраняем и оцениваем…'; }
    try {
      let checklistId = activeChecklist() && activeChecklist().id;
      if (state.activeChecklistKey === 'draft') {
        const draft = Object.assign({}, state.checklists['draft']);
        delete draft.based_on;
        const saved = await api().saveChecklist(draft);
        checklistId = saved && (saved.id || saved.checklist_id);
        if (!checklistId) throw new Error('сервер не вернул id чек-листа');
        await api().activateChecklist(checklistId);
        delete state.checklists['draft'];
        try { localStorage.removeItem('cqa_checklist_draft'); } catch (e) { /* storage off */ }
        state.checklists = await api().getChecklists();
        state.activeChecklistKey = checklistId;
        renderChecklistPicker();
      }
      const ids = state.conversations.filter(c => (activeChecklist().applies_to || 'agent chat') === c.channel).map(c => c.conversation_id);
      if (!ids.length) { toast('Нет диалогов для канала ' + (activeChecklist().applies_to || 'agent chat')); return; }
      if (!confirm(`Оценить ${ids.length} диалог(ов) по чек-листу «${activeChecklist().name}» v${activeChecklist().version}? Это вызовет модель для каждого диалога.`)) return;
      await api().evaluateBatch(ids, checklistId);
      state.conversations = await api().getConversations();
      state.evaluations = {};
      if (state.activeConvId) await ensureEvaluationLoaded(state.activeConvId, true);
      collectAppeals();
      renderAll();
      toast(`Готово: ${ids.length} диалог(ов) отправлено на оценку по новой версии`);
    } catch (e) {
      toast('Не удалось применить чек-лист: ' + e.message);
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = 'Сохранить и применить к диалогам'; }
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

  function exportCSV() {
    const headers = ['ID диалога', 'Канал', 'Язык', 'Итоговый балл CQA', 'Грубая ошибка', 'Апелляция'];
    const rows = state.conversations.map(c => [
      c.conversation_id,
      `"${c.channel}"`,
      c.language,
      c.total_score !== null && c.total_score !== undefined ? c.total_score : '',
      c.gross_error ? 'ДА' : 'НЕТ',
      c.has_appeal ? 'ДА' : 'НЕТ'
    ]);

    let csvContent = 'data:text/csv;charset=utf-8,\uFEFF'
      + [headers.join(';'), ...rows.map(e => e.join(';'))].join('\n');

    const encodedUri = encodeURI(csvContent);
    const link = document.createElement('a');
    link.setAttribute('href', encodedUri);
    link.setAttribute('download', `freedom_broker_cqa_report_${new Date().toISOString().slice(0, 10)}.csv`);
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
  }

  function exportJSON() {
    const dataStr = 'data:text/json;charset=utf-8,' + encodeURIComponent(JSON.stringify({
      exported_at: new Date().toISOString(),
      source: isLive() ? api().getBaseUrl() : 'demo',
      evaluations: state.evaluations,
      checklist: activeChecklist()
    }, null, 2));
    const downloadAnchor = document.createElement('a');
    downloadAnchor.setAttribute('href', dataStr);
    downloadAnchor.setAttribute('download', `freedom_broker_cqa_export_${new Date().toISOString().slice(0, 10)}.json`);
    document.body.appendChild(downloadAnchor);
    downloadAnchor.click();
    downloadAnchor.remove();
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

  function switchTab(tabId) {
    document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.nav-tab-btn').forEach(b => b.classList.remove('active'));

    const tab = document.getElementById(tabId);
    if (tab) tab.classList.add('active');

    const btn = document.querySelector(`[data-tab="${tabId}"]`);
    if (btn) btn.classList.add('active');
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
        renderConversationsList();
      };
    }

    document.querySelectorAll('.filter-chips button[data-filter]').forEach(btn => {
      btn.onclick = () => {
        document.querySelectorAll('.filter-chips button[data-filter]').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        state.filters.channel = btn.dataset.filter === 'chat' || btn.dataset.filter === 'voice' ? btn.dataset.filter : 'all';
        state.filters.status = btn.dataset.filter === 'error' || btn.dataset.filter === 'appeal' ? btn.dataset.filter : 'all';
        renderConversationsList();
      };
    });

    document.querySelectorAll('.filter-chips button[data-lang]').forEach(btn => {
      btn.onclick = () => {
        document.querySelectorAll('.filter-chips button[data-lang]').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        state.filters.lang = btn.dataset.lang;
        renderConversationsList();
      };
    });

    const submitAppealBtn = document.getElementById('btn-submit-appeal');
    if (submitAppealBtn) submitAppealBtn.onclick = submitAppeal;

    const applyReviewBtn = document.getElementById('btn-apply-review-decision');
    if (applyReviewBtn) applyReviewBtn.onclick = applyReviewDecision;

    const addCritBtn = document.getElementById('btn-add-criterion');
    if (addCritBtn) addCritBtn.onclick = openAddCriterionModal;

    const saveCritBtn = document.getElementById('btn-save-criterion');
    if (saveCritBtn) saveCritBtn.onclick = saveCriterion;

    const resetChkBtn = document.getElementById('btn-reset-checklist');
    if (resetChkBtn) resetChkBtn.onclick = resetChecklist;

    const applyChkBtn = document.getElementById('btn-apply-checklist-changes');
    if (applyChkBtn) applyChkBtn.onclick = applyChecklistChanges;

    const exportCsvBtn = document.getElementById('btn-export-csv');
    if (exportCsvBtn) exportCsvBtn.onclick = exportCSV;

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

  function formatTimestamp(ts) {
    if (!ts) return '';
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

})();
