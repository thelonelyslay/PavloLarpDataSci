/**
 * Freedom Broker CQA (Conversation Quality Analytics) Frontend Application
 * Fully client-side, zero build required. Pure modern JavaScript.
 * Implements exact math and rules from src/cqa/scoring.py and AGENTS.md contract.
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
    appeals: []
  };

  // --- SCORING ENGINE (Exact port of src/cqa/scoring.py) ---
  function computeScore(checklist, answers) {
    const digits = checklist.rounding || 2;
    const scored = checklist.criteria.filter(c => c.type !== 'flag');
    const flags = checklist.criteria.filter(c => c.type === 'flag');

    const norm = {};
    for (const c of checklist.criteria) {
      let v = answers[c.id];
      if (typeof v === 'string') {
        const lower = v.trim().toLowerCase();
        if (lower === 'да' || lower === 'yes' || lower === 'true') v = 'yes';
        else if (lower === 'нет' || lower === 'no' || lower === 'false') v = 'no';
        else if (lower === 'n/a' || lower === 'н/п' || lower === 'na') v = 'na';
      }
      norm[c.id] = v;
    }

    const earned = {};
    const applicable = {};
    let applicableWeight = 0;

    for (const c of scored) {
      const a = norm[c.id];
      if (a === 'na') {
        applicable[c.id] = false;
        earned[c.id] = 0.0;
      } else {
        applicable[c.id] = true;
        applicableWeight += c.weight;
        if (c.type === 'yes_no') {
          earned[c.id] = (a === 'yes') ? Number(c.weight) : 0.0;
        } else if (c.type === 'scale') {
          const maxStep = Math.max(...c.steps.map(Number));
          earned[c.id] = maxStep > 0 ? (Number(a) / maxStep) * c.weight : 0.0;
        }
      }
    }

    let total = null;
    if (applicableWeight > 0) {
      const sumEarned = Object.values(earned).reduce((sum, val) => sum + val, 0);
      total = Math.round((sumEarned / applicableWeight * 100) * 100) / 100;
    }

    const criteriaOut = scored.map(c => {
      const isApp = applicable[c.id];
      const effWeight = (isApp && applicableWeight > 0) ? Math.round((c.weight / applicableWeight * 100) * 100) / 100 : 0.0;
      const points = (isApp && applicableWeight > 0) ? Math.round((earned[c.id] / applicableWeight * 100) * 100) / 100 : 0.0;
      return {
        id: c.id,
        title: c.title,
        section: c.section,
        answer: norm[c.id],
        applicable: isApp,
        weight: c.weight,
        effective_weight: effWeight,
        points: points
      };
    });

    const sections = {};
    for (const c of scored) {
      const sec = c.section || '—';
      if (!sections[sec]) sections[sec] = { earned: 0.0, weight: 0.0 };
      if (applicable[c.id]) {
        sections[sec].earned += earned[c.id];
        sections[sec].weight += c.weight;
      }
    }
    const sectionScores = {};
    for (const sec in sections) {
      sectionScores[sec] = sections[sec].weight > 0 ? Math.round((sections[sec].earned / sections[sec].weight * 100) * 100) / 100 : null;
    }

    const grossErrorIds = flags.filter(c => norm[c.id] === 'yes').map(c => c.id);

    return {
      total,
      criteria: criteriaOut,
      section_scores: sectionScores,
      gross_error: grossErrorIds.length > 0,
      gross_error_ids: grossErrorIds
    };
  }

  // --- INITIALIZATION ---
  function init() {
    // 1. Load Checklists
    if (window.CQA_DEFAULT_CHECKLISTS) {
      state.checklists = JSON.parse(JSON.stringify(window.CQA_DEFAULT_CHECKLISTS));
    }
    const savedChecklist = localStorage.getItem('cqa_custom_checklist');
    if (savedChecklist) {
      try {
        state.checklists['custom'] = JSON.parse(savedChecklist);
        state.activeChecklistKey = 'custom';
      } catch (e) {
        console.error('Failed to parse saved checklist', e);
      }
    }

    // 2. Load Mock Data
    if (window.CQA_MOCK_DATA) {
      state.conversations = JSON.parse(JSON.stringify(window.CQA_MOCK_DATA.conversations));
      state.evaluations = JSON.parse(JSON.stringify(window.CQA_MOCK_DATA.evaluations));
    }

    // Extract initial appeals
    collectAppeals();

    // 3. Set default active conversation
    if (state.conversations.length > 0) {
      state.activeConvId = state.conversations[0].conversation_id;
    }

    // 4. Bind event listeners
    bindEvents();

    // 5. Check if backend server is available
    checkBackendConnection();

    // 6. Initial render
    renderAll();
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
              answer: crit.answer,
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

  async function checkBackendConnection() {
    try {
      const res = await fetch('/api/conversations', { method: 'GET' });
      if (res.ok) {
        const data = await res.json();
        const statusText = document.getElementById('api-status-text');
        const statusDot = document.querySelector('.status-dot');
        if (statusText) statusText.textContent = 'FastAPI сервер онлайн';
        if (statusDot) statusDot.style.background = '#10b981';
      }
    } catch (e) {
      // Backend not running, stay in standalone mock mode
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
      // Channel filter
      if (state.filters.channel === 'chat' && conv.channel !== 'agent chat') return false;
      if (state.filters.channel === 'voice' && conv.channel !== 'voice call') return false;
      // Language filter
      if (state.filters.lang !== 'all' && conv.language !== state.filters.lang) return false;
      // Status filter
      if (state.filters.status === 'error' && !conv.gross_error) return false;
      if (state.filters.status === 'appeal' && !conv.has_appeal) return false;
      // Search
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

      const firstClientMsg = conv.transcript.find(m => m.speaker === 'client') || conv.transcript[0];
      const snippet = firstClientMsg ? firstClientMsg.text : '...';

      let scoreClass = 'score-perfect';
      if (conv.total_score === null) scoreClass = 'score-fair';
      else if (conv.total_score >= 90) scoreClass = 'score-perfect';
      else if (conv.total_score >= 70) scoreClass = 'score-good';
      else if (conv.total_score >= 50) scoreClass = 'score-fair';
      else scoreClass = 'score-poor';

      card.innerHTML = `
        <div class="conv-card-top">
          <span class="conv-id">${conv.conversation_id}</span>
          <div class="conv-badges">
            <span class="badge ${conv.channel === 'voice call' ? 'badge-voice' : 'badge-chat'}">${conv.channel === 'voice call' ? '📞 Звонок' : '💬 Чат'}</span>
            <span class="badge badge-${conv.language.toLowerCase()}">${conv.language}</span>
          </div>
        </div>
        <div class="conv-snippet">${escapeHtml(snippet)}</div>
        <div class="conv-card-bottom">
          <span class="score-pill ${scoreClass}">
            ★ ${conv.total_score !== null ? conv.total_score.toFixed(2) : 'N/A'}
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

  function selectConversation(cid) {
    state.activeConvId = cid;
    renderConversationsList();
    renderTranscript();
    renderEvaluationCard();
  }

  // 2. TRANSCRIPT VIEW
  function renderTranscript() {
    const conv = state.conversations.find(c => c.conversation_id === state.activeConvId);
    if (!conv) return;

    const titleEl = document.getElementById('active-conv-title');
    const subEl = document.getElementById('active-conv-sub');
    const voiceBar = document.getElementById('voice-audio-bar');
    const streamEl = document.getElementById('chat-stream');

    if (titleEl) {
      titleEl.innerHTML = `
        <span>${conv.conversation_id}</span>
        <span class="badge ${conv.channel === 'voice call' ? 'badge-voice' : 'badge-chat'}">${conv.channel}</span>
        <span class="badge badge-${conv.language.toLowerCase()}">${conv.language}</span>
      `;
    }
    if (subEl) {
      subEl.textContent = `Всего реплик: ${conv.transcript.length} · Исходный канал: ${conv.channel}`;
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
      conv.transcript.forEach((msg, idx) => {
        const row = document.createElement('div');
        row.className = `chat-message-row msg-${msg.speaker}`;
        row.id = `msg-${idx}`;

        let speakerLabel = 'Клиент';
        if (msg.speaker === 'operator') speakerLabel = 'Оператор';
        else if (msg.speaker === 'bot') speakerLabel = 'AIM Бот (Freedom)';
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

  // 3. EVALUATION SCORECARD
  function renderEvaluationCard() {
    const ev = state.evaluations[state.activeConvId];
    if (!ev) return;

    // Total Score
    const totalEl = document.getElementById('total-score-display');
    const badgeEl = document.getElementById('score-status-badge');
    const grossBanner = document.getElementById('gross-error-banner');
    const grossText = document.getElementById('gross-error-text');
    const sectionsSummary = document.getElementById('sections-summary');
    const criteriaContainer = document.getElementById('criteria-container');

    if (totalEl) {
      totalEl.textContent = ev.total !== null ? ev.total.toFixed(2) : 'N/A';
    }

    if (badgeEl) {
      if (ev.gross_error) {
        badgeEl.className = 'score-label-badge badge-violation';
        badgeEl.textContent = 'Грубая ошибка';
      } else if (ev.total === null) {
        badgeEl.className = 'score-label-badge badge-warning';
        badgeEl.textContent = 'Не оценивается';
      } else if (ev.total >= 90) {
        badgeEl.className = 'score-label-badge badge-excellent';
        badgeEl.textContent = 'Отлично (90+)';
      } else if (ev.total >= 70) {
        badgeEl.className = 'score-label-badge badge-good';
        badgeEl.textContent = 'Хорошо (70-89)';
      } else {
        badgeEl.className = 'score-label-badge badge-warning';
        badgeEl.textContent = 'Требует внимания';
      }
    }

    // Gross error alert
    if (grossBanner) {
      if (ev.gross_error) {
        grossBanner.style.display = 'flex';
        const grossCrit = ev.criteria.find(c => c.id === 'gross_errors' || (ev.gross_error_ids && ev.gross_error_ids.includes(c.id)));
        if (grossText && grossCrit) {
          grossText.textContent = grossCrit.explanation || 'Выявлено грубое нарушение правил компании!';
        }
      } else {
        grossBanner.style.display = 'none';
      }
    }

    // Sections Progress Bars
    if (sectionsSummary && ev.section_scores) {
      sectionsSummary.innerHTML = '';
      for (const [secName, scoreVal] of Object.entries(ev.section_scores)) {
        const item = document.createElement('div');
        item.className = 'section-bar-item';
        const pct = scoreVal !== null ? scoreVal : 0;
        item.innerHTML = `
          <div class="section-bar-label">
            <span>${escapeHtml(secName)}</span>
            <span><strong>${scoreVal !== null ? scoreVal.toFixed(1) + '%' : 'N/A'}</strong></span>
          </div>
          <div class="bar-track">
            <div class="bar-fill" style="width: ${pct}%;"></div>
          </div>
        `;
        sectionsSummary.appendChild(item);
      }
    }

    // Criteria Breakdown
    if (criteriaContainer && ev.criteria) {
      criteriaContainer.innerHTML = '';
      ev.criteria.forEach((crit, idx) => {
        const card = document.createElement('div');
        card.className = 'criterion-card';

        // Badge class
        let ansClass = 'ans-yes';
        let ansText = String(crit.answer).toUpperCase();
        if (crit.answer === 'no') { ansClass = 'ans-no'; ansText = 'НЕТ'; }
        else if (crit.answer === 'yes') { ansClass = 'ans-yes'; ansText = 'ДА'; }
        else if (crit.answer === 'na' || !crit.applicable) { ansClass = 'ans-na'; ansText = 'N/A'; }
        else if (typeof crit.answer === 'number') { ansClass = 'ans-scale'; ansText = `${crit.answer} б.`; }

        // If flag criterion
        if (crit.weight === 0) {
          if (crit.answer === 'yes') { ansClass = 'ans-flag-active'; ansText = '⚠️ НАРУШЕНИЕ'; }
          else { ansClass = 'ans-flag-clear'; ansText = 'ЧИСТО'; }
        }

        // Evidence button and quotes
        let evidenceHtml = '';
        if (crit.evidence && crit.evidence.length > 0) {
          evidenceHtml = crit.evidence.map(evItem => `
            <div class="evidence-row">
              <div class="evidence-quote">«${escapeHtml(evItem.quote)}»</div>
              <button class="evidence-link-btn" onclick="app.scrollToReplica(${evItem.message_index})">
                <span>📍 Перейти к реплике #${evItem.message_index} ↗</span>
              </button>
            </div>
          `).join('');
        }

        // NA reason block
        let naHtml = '';
        if (crit.answer === 'na' && crit.na_reason) {
          naHtml = `<div class="na-reason-box">ℹ️ ${escapeHtml(crit.na_reason)}</div>`;
        }

        // Appeal status block
        let appealHtml = '';
        if (crit.appeal) {
          appealHtml = `
            <div style="font-size: 11px; background: #fffbeb; border: 1px solid #fde68a; border-radius: 6px; padding: 6px 8px; width: 100%;">
              <div style="display: flex; justify-content: space-between; align-items: center;">
                <span class="appeal-status-badge">⚖️ ${crit.appeal.status === 'open' ? 'На апелляции' : 'Рассмотрено'}</span>
                ${crit.appeal.status === 'open' ? `
                  <button class="btn-primary" style="padding: 2px 8px; font-size: 11px;" onclick="app.openReviewModal('${state.activeConvId}', '${crit.id}')">
                    Рассмотреть (QC)
                  </button>
                ` : ''}
              </div>
              <div style="margin-top: 4px; color: #92400e;"><strong>Оператор:</strong> ${escapeHtml(crit.appeal.comment)}</div>
              ${crit.appeal.decision ? `
                <div style="margin-top: 4px; color: #065f46; border-top: 1px dashed #d1fae5; padding-top: 4px;">
                  <strong>Решение QC:</strong> ${crit.appeal.decision === 'change' ? 'Оценка изменена' : 'Подтверждено ИИ'}. Причина: ${escapeHtml(crit.appeal.reason || '—')}
                </div>
              ` : ''}
            </div>
          `;
        } else {
          appealHtml = `
            <button class="appeal-btn" onclick="app.openAppealModal('${crit.id}', '${crit.title}', '${ansText}')">
              <span>⚖️ Оспорить оценку</span>
            </button>
          `;
        }

        card.innerHTML = `
          <div class="criterion-header">
            <div class="criterion-title-wrap">
              <span class="criterion-section-tag">${escapeHtml(crit.section || '')}</span>
              <span class="criterion-title">${crit.title}</span>
            </div>
            <div class="criterion-badges-wrap">
              <span class="ans-badge ${ansClass}">${ansText}</span>
              <span class="points-pill">+${crit.points.toFixed(2)} б.</span>
            </div>
          </div>

          <div class="weights-row">
            <span>Базовый вес: <strong>${crit.weight}</strong></span>
            <span class="eff-weight-highlight">Эффективный вес: <strong>${crit.effective_weight.toFixed(2)}%</strong></span>
          </div>

          <div class="criterion-explanation">
            ${escapeHtml(crit.explanation || '—')}
          </div>

          ${naHtml}
          ${evidenceHtml}

          <div class="criterion-footer">
            ${appealHtml}
          </div>
        `;
        criteriaContainer.appendChild(card);
      });
    }
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
        <td><a href="javascript:void(0)" onclick="app.selectConversationAndTab('${item.conversation_id}')" style="font-weight: 700; color: var(--primary); text-decoration: none;">${item.conversation_id}</a></td>
        <td><strong>${escapeHtml(item.criterion_title)}</strong></td>
        <td><span class="ans-badge ans-no">${item.answer.toUpperCase()}</span></td>
        <td style="max-width: 320px;">${escapeHtml(item.appeal.comment)}</td>
        <td>
          <span class="badge ${isPending ? 'score-fair' : 'score-perfect'}">${isPending ? 'На рассмотрении' : 'Завершено'}</span>
        </td>
        <td>
          ${isPending ? `
            <button class="btn-primary" style="padding: 4px 10px; font-size: 11px;" onclick="app.openReviewModal('${item.conversation_id}', '${item.criterion_id}')">
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
    if (!tbody) return;

    const checklist = state.checklists[state.activeChecklistKey] || state.checklists['default'];
    if (!checklist || !checklist.criteria) return;

    tbody.innerHTML = '';
    checklist.criteria.forEach((crit, idx) => {
      const tr = document.createElement('tr');
      let typeLabel = 'Да / Нет';
      if (crit.type === 'scale') typeLabel = `Шкала [${crit.steps.join(', ')}]`;
      else if (crit.type === 'flag') typeLabel = 'Флаг (Грубая ошибка)';

      tr.innerHTML = `
        <td>${idx + 1}</td>
        <td><span class="badge badge-chat">${escapeHtml(crit.section || '—')}</span></td>
        <td><strong>${escapeHtml(crit.title)}</strong></td>
        <td>${typeLabel}</td>
        <td><strong>${crit.weight}</strong></td>
        <td>${crit.allow_na ? '<span style="color: var(--success); font-weight: 700;">✓ Да</span>' : '<span style="color: var(--text-muted);">— Нет</span>'}</td>
        <td style="max-width: 300px; font-size: 12px; color: var(--text-muted);">${escapeHtml(crit.instruction || '')}</td>
        <td>
          <div style="display: flex; gap: 6px;">
            <button class="chip-btn" onclick="app.openEditCriterionModal('${crit.id}')">Редактировать</button>
            <button class="chip-btn" style="color: var(--danger);" onclick="app.deleteCriterion('${crit.id}')">Удалить</button>
          </div>
        </td>
      `;
      tbody.appendChild(tr);
    });
  }

  // --- ACTIONS & MODALS ---
  function openAppealModal(critId, critTitle, currentScore) {
    const modal = document.getElementById('appeal-modal');
    document.getElementById('appeal-modal-criterion').value = critTitle;
    document.getElementById('appeal-modal-criterion-id').value = critId;
    document.getElementById('appeal-modal-current-score').value = currentScore;
    document.getElementById('appeal-modal-comment').value = '';
    if (modal) modal.classList.add('active');
  }

  function submitAppeal() {
    const critId = document.getElementById('appeal-modal-criterion-id').value;
    const comment = document.getElementById('appeal-modal-comment').value.trim();

    if (!comment) {
      alert('Пожалуйста, укажите комментарий с аргументацией!');
      return;
    }

    const ev = state.evaluations[state.activeConvId];
    if (ev) {
      const crit = ev.criteria.find(c => c.id === critId);
      if (crit) {
        crit.appeal = {
          comment: comment,
          status: 'open',
          created_at: new Date().toISOString()
        };
      }
    }

    const conv = state.conversations.find(c => c.conversation_id === state.activeConvId);
    if (conv) conv.has_appeal = true;

    closeModal('appeal-modal');
    collectAppeals();
    renderEvaluationCard();
    renderConversationsList();
    renderAppealsTable();
  }

  function openReviewModal(convId, critId) {
    const ev = state.evaluations[convId];
    if (!ev) return;
    const crit = ev.criteria.find(c => c.id === critId);
    if (!crit || !crit.appeal) return;

    document.getElementById('review-modal-target').value = `${convId} — ${crit.title}`;
    document.getElementById('review-modal-conv-id').value = convId;
    document.getElementById('review-modal-criterion-id').value = critId;
    document.getElementById('review-modal-operator-comment').textContent = crit.appeal.comment;
    document.getElementById('review-modal-reason').value = '';

    const modal = document.getElementById('review-modal');
    if (modal) modal.classList.add('active');
  }

  function applyReviewDecision() {
    const convId = document.getElementById('review-modal-conv-id').value;
    const critId = document.getElementById('review-modal-criterion-id').value;
    const decisionType = document.getElementById('review-modal-decision-type').value;
    const newAnswer = document.getElementById('review-modal-new-answer').value;
    const reason = document.getElementById('review-modal-reason').value.trim();

    if (!reason) {
      alert('Пожалуйста, укажите причину решения проверяющего!');
      return;
    }

    const ev = state.evaluations[convId];
    if (!ev) return;
    const crit = ev.criteria.find(c => c.id === critId);
    if (!crit) return;

    crit.appeal.status = 'closed';
    crit.appeal.decision = decisionType;
    crit.appeal.reason = reason;

    if (decisionType === 'change') {
      crit.final_answer = newAnswer;
      crit.answer = newAnswer;

      // Recalculate full evaluation using checklist rules
      const checklist = state.checklists[state.activeChecklistKey] || state.checklists['default'];
      const answersMap = {};
      ev.criteria.forEach(c => answersMap[c.id] = c.answer);
      answersMap[critId] = newAnswer;

      const scored = computeScore(checklist, answersMap);
      ev.total = scored.total;
      ev.section_scores = scored.section_scores;

      // Update points and eff weights in evaluation
      scored.criteria.forEach(scCrit => {
        const target = ev.criteria.find(c => c.id === scCrit.id);
        if (target) {
          target.points = scCrit.points;
          target.effective_weight = scCrit.effective_weight;
          target.applicable = scCrit.applicable;
        }
      });

      // Update conversation summary
      const conv = state.conversations.find(c => c.conversation_id === convId);
      if (conv) conv.total_score = scored.total;
    }

    closeModal('review-modal');
    collectAppeals();
    renderAll();
  }

  // Checklist Editor Actions
  function openEditCriterionModal(critId) {
    const checklist = state.checklists[state.activeChecklistKey] || state.checklists['default'];
    const crit = checklist.criteria.find(c => c.id === critId);
    if (!crit) return;

    document.getElementById('criterion-modal-title').textContent = 'Редактирование критерия';
    document.getElementById('crit-edit-id').value = crit.id;
    document.getElementById('crit-edit-id').readOnly = true;
    document.getElementById('crit-edit-title').value = crit.title;
    document.getElementById('crit-edit-section').value = crit.section || 'Начало и понимание запроса';
    document.getElementById('crit-edit-type').value = crit.type;
    document.getElementById('crit-edit-weight').value = crit.weight;
    document.getElementById('crit-edit-allow-na').checked = !!crit.allow_na;
    document.getElementById('crit-edit-instruction').value = crit.instruction || '';

    const modal = document.getElementById('criterion-modal');
    if (modal) modal.classList.add('active');
  }

  function openAddCriterionModal() {
    document.getElementById('criterion-modal-title').textContent = 'Добавление нового критерия';
    document.getElementById('crit-edit-id').value = 'criterion_' + (Date.now() % 1000);
    document.getElementById('crit-edit-id').readOnly = false;
    document.getElementById('crit-edit-title').value = '';
    document.getElementById('crit-edit-section').value = 'Начало и понимание запроса';
    document.getElementById('crit-edit-type').value = 'yes_no';
    document.getElementById('crit-edit-weight').value = '10';
    document.getElementById('crit-edit-allow-na').checked = true;
    document.getElementById('crit-edit-instruction').value = '';

    const modal = document.getElementById('criterion-modal');
    if (modal) modal.classList.add('active');
  }

  function saveCriterion() {
    const id = document.getElementById('crit-edit-id').value.trim();
    const title = document.getElementById('crit-edit-title').value.trim();
    const section = document.getElementById('crit-edit-section').value;
    const type = document.getElementById('crit-edit-type').value;
    const weight = Number(document.getElementById('crit-edit-weight').value);
    const allowNa = document.getElementById('crit-edit-allow-na').checked;
    const instruction = document.getElementById('crit-edit-instruction').value.trim();

    if (!id || !title) {
      alert('Пожалуйста, укажите ID и название критерия!');
      return;
    }

    let checklist = state.checklists['custom'];
    if (!checklist) {
      checklist = JSON.parse(JSON.stringify(state.checklists['default']));
      checklist.version = (checklist.version || 1) + 1;
      checklist.name = 'Пользовательский чек-лист CQA';
      state.checklists['custom'] = checklist;
      state.activeChecklistKey = 'custom';
    }

    let crit = checklist.criteria.find(c => c.id === id);
    if (crit) {
      crit.title = title;
      crit.section = section;
      crit.type = type;
      crit.weight = weight;
      crit.allow_na = allowNa;
      crit.instruction = instruction;
    } else {
      checklist.criteria.push({
        id,
        title,
        section,
        type,
        weight,
        allow_na: allowNa,
        steps: type === 'scale' ? [20, 15, 10, 5, 0] : undefined,
        instruction
      });
    }

    localStorage.setItem('cqa_custom_checklist', JSON.stringify(checklist));
    closeModal('criterion-modal');
    renderChecklistTable();
    alert(`Критерий «${title}» успешно сохранен в конфигурации!`);
  }

  function deleteCriterion(critId) {
    if (!confirm(`Удалить критерий ${critId} из чек-листа?`)) return;
    let checklist = state.checklists['custom'];
    if (!checklist) {
      checklist = JSON.parse(JSON.stringify(state.checklists['default']));
      state.checklists['custom'] = checklist;
      state.activeChecklistKey = 'custom';
    }
    checklist.criteria = checklist.criteria.filter(c => c.id !== critId);
    localStorage.setItem('cqa_custom_checklist', JSON.stringify(checklist));
    renderChecklistTable();
  }

  function resetChecklist() {
    if (!confirm('Сбросить чек-лист к исходным 8 критериям Freedom Broker?')) return;
    localStorage.removeItem('cqa_custom_checklist');
    delete state.checklists['custom'];
    state.activeChecklistKey = 'default';
    document.getElementById('checklist-picker').value = 'default';
    renderChecklistTable();
    alert('Чек-лист сброшен к заводским настройкам Freedom Broker!');
  }

  function applyChecklistChanges() {
    const checklist = state.checklists[state.activeChecklistKey] || state.checklists['default'];

    // Re-evaluate all dialogues with current checklist math
    state.conversations.forEach(conv => {
      const ev = state.evaluations[conv.conversation_id];
      if (ev) {
        const answersMap = {};
        ev.criteria.forEach(c => answersMap[c.id] = c.answer);
        // Ensure every criteria in checklist has an answer
        checklist.criteria.forEach(c => {
          if (!answersMap[c.id]) answersMap[c.id] = c.type === 'scale' ? c.steps[0] : (c.type === 'flag' ? 'no' : 'yes');
        });

        const scored = computeScore(checklist, answersMap);
        ev.total = scored.total;
        ev.section_scores = scored.section_scores;
        conv.total_score = scored.total;

        // Rebuild criteria array
        ev.criteria = scored.criteria.map(scCrit => {
          const orig = ev.criteria.find(c => c.id === scCrit.id) || {};
          return {
            id: scCrit.id,
            title: scCrit.title,
            section: scCrit.section,
            answer: scCrit.answer,
            applicable: scCrit.applicable,
            weight: scCrit.weight,
            effective_weight: scCrit.effective_weight,
            points: scCrit.points,
            explanation: orig.explanation || `Оценка по обновленному чек-листу: ${scCrit.answer}`,
            evidence: orig.evidence || [],
            na_reason: orig.na_reason || null,
            appeal: orig.appeal || null
          };
        });
      }
    });

    renderAll();
    alert('Обновленный чек-лист успешно применен ко всем диалогам! Баллы пересчитаны на лету.');
  }

  function closeModal(modalId) {
    const modal = document.getElementById(modalId);
    if (modal) modal.classList.remove('active');
  }

  function selectConversationAndTab(cid) {
    selectConversation(cid);
    switchTab('tab-workspace');
  }

  function scrollToReplica(msgIndex) {
    const el = document.getElementById(`msg-${msgIndex}`);
    if (el) {
      el.scrollIntoView({ behavior: 'smooth', block: 'center' });
      el.classList.add('pulse-highlight');
      setTimeout(() => el.classList.remove('pulse-highlight'), 2200);
    }
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
    // Nav tabs
    document.querySelectorAll('.nav-tab-btn').forEach(btn => {
      btn.onclick = () => switchTab(btn.dataset.tab);
    });

    // Checklist selector in top bar
    const chkPicker = document.getElementById('checklist-picker');
    if (chkPicker) {
      chkPicker.onchange = () => {
        state.activeChecklistKey = chkPicker.value;
        renderAll();
      };
    }

    // Search input
    const searchInput = document.getElementById('conv-search-input');
    if (searchInput) {
      searchInput.oninput = (e) => {
        state.filters.search = e.target.value;
        renderConversationsList();
      };
    }

    // Filter chips
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

    // Appeals
    const submitAppealBtn = document.getElementById('btn-submit-appeal');
    if (submitAppealBtn) submitAppealBtn.onclick = submitAppeal;

    const applyReviewBtn = document.getElementById('btn-apply-review-decision');
    if (applyReviewBtn) applyReviewBtn.onclick = applyReviewDecision;

    // Checklist config
    const addCritBtn = document.getElementById('btn-add-criterion');
    if (addCritBtn) addCritBtn.onclick = openAddCriterionModal;

    const saveCritBtn = document.getElementById('btn-save-criterion');
    if (saveCritBtn) saveCritBtn.onclick = saveCriterion;

    const resetChkBtn = document.getElementById('btn-reset-checklist');
    if (resetChkBtn) resetChkBtn.onclick = resetChecklist;

    const applyChkBtn = document.getElementById('btn-apply-checklist-changes');
    if (applyChkBtn) applyChkBtn.onclick = applyChecklistChanges;

    // Analytics export
    const exportCsvBtn = document.getElementById('btn-export-csv');
    if (exportCsvBtn) exportCsvBtn.onclick = exportCSV;

    const exportJsonBtn = document.getElementById('btn-export-json');
    if (exportJsonBtn) exportJsonBtn.onclick = exportJSON;
  }

  // 6. EXECUTIVE ANALYTICS DASHBOARD
  function renderAnalyticsDashboard() {
    const totalConvs = state.conversations.length;
    if (totalConvs === 0) return;

    const validScores = state.conversations.filter(c => c.total_score !== null).map(c => c.total_score);
    const avgScore = validScores.length > 0 ? (validScores.reduce((a, b) => a + b, 0) / validScores.length) : 0;
    const grossErrors = state.conversations.filter(c => c.gross_error).length;
    const appealsCount = state.appeals.length;

    const avgEl = document.getElementById('kpi-avg-score');
    const totalEl = document.getElementById('kpi-total-convs');
    const grossEl = document.getElementById('kpi-gross-errors');
    const appealsEl = document.getElementById('kpi-appeals-count');

    if (avgEl) avgEl.textContent = avgScore.toFixed(1) + '%';
    if (totalEl) totalEl.textContent = totalConvs;
    if (grossEl) grossEl.textContent = grossErrors;
    if (appealsEl) appealsEl.textContent = appealsCount;

    // Grade distribution
    const excellent = state.conversations.filter(c => c.total_score >= 90).length;
    const good = state.conversations.filter(c => c.total_score >= 70 && c.total_score < 90).length;
    const fair = state.conversations.filter(c => c.total_score !== null && c.total_score < 70).length;

    const distribContainer = document.getElementById('distrib-bars-container');
    if (distribContainer) {
      distribContainer.innerHTML = `
        <div class="distrib-bar-row">
          <span class="distrib-bar-label">Отлично (≥ 90%):</span>
          <div class="distrib-bar-track"><div class="distrib-bar-fill" style="width: ${(excellent / totalConvs) * 100}%; background: var(--success);"></div></div>
          <span class="distrib-bar-val">${excellent} (${Math.round((excellent / totalConvs) * 100)}%)</span>
        </div>
        <div class="distrib-bar-row">
          <span class="distrib-bar-label">Хорошо (70–89%):</span>
          <div class="distrib-bar-track"><div class="distrib-bar-fill" style="width: ${(good / totalConvs) * 100}%; background: var(--info);"></div></div>
          <span class="distrib-bar-val">${good} (${Math.round((good / totalConvs) * 100)}%)</span>
        </div>
        <div class="distrib-bar-row">
          <span class="distrib-bar-label">Требует внимания (&lt; 70%):</span>
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

    // Criteria compliance table
    const critTableBody = document.getElementById('analytics-criteria-body');
    if (critTableBody) {
      const checklist = state.checklists[state.activeChecklistKey] || state.checklists['default'];
      if (checklist && checklist.criteria) {
        critTableBody.innerHTML = '';
        checklist.criteria.forEach(crit => {
          let passedCount = 0;
          let applicableCount = 0;

          for (const cid in state.evaluations) {
            const ev = state.evaluations[cid];
            const cItem = ev.criteria && ev.criteria.find(c => c.id === crit.id);
            if (cItem) {
              if (crit.type === 'flag') {
                applicableCount++;
                if (cItem.answer === 'no') passedCount++;
              } else if (cItem.applicable) {
                applicableCount++;
                if (crit.type === 'yes_no' && cItem.answer === 'yes') passedCount++;
                else if (crit.type === 'scale') {
                  const maxStep = Math.max(...(crit.steps || [20]));
                  passedCount += Number(cItem.answer || 0) / maxStep;
                }
              }
            }
          }

          const complianceRate = applicableCount > 0 ? Math.round((passedCount / applicableCount) * 100) : 100;
          let statusBadge = '<span class="badge score-perfect">Норматив выполнен</span>';
          if (complianceRate < 75) statusBadge = '<span class="badge score-poor">Критическая зона</span>';
          else if (complianceRate < 85) statusBadge = '<span class="badge score-fair">Требует коучинга</span>';

          const tr = document.createElement('tr');
          tr.innerHTML = `
            <td><strong>${escapeHtml(crit.title)}</strong></td>
            <td><span class="badge badge-chat">${escapeHtml(crit.section || '—')}</span></td>
            <td><strong>${crit.weight}</strong></td>
            <td>
              <div style="display: flex; align-items: center; gap: 8px;">
                <div style="width: 80px; height: 6px; background: #e2e8f0; border-radius: 3px; overflow: hidden;">
                  <div style="width: ${complianceRate}%; height: 100%; background: ${complianceRate >= 85 ? 'var(--primary)' : (complianceRate >= 75 ? 'var(--warning)' : 'var(--danger)')};"></div>
                </div>
                <strong>${complianceRate}%</strong>
              </div>
            </td>
            <td>${statusBadge}</td>
          `;
          critTableBody.appendChild(tr);
        });
      }
    }
  }

  function exportCSV() {
    const headers = ['ID диалога', 'Канал', 'Язык', 'Итоговый балл CQA', 'Грубая ошибка', 'Апелляция'];
    const rows = state.conversations.map(c => [
      c.conversation_id,
      `"${c.channel}"`,
      c.language,
      c.total_score !== null ? c.total_score : 'N/A',
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
      conversations: state.conversations,
      evaluations: state.evaluations,
      checklist: state.checklists[state.activeChecklistKey] || state.checklists['default']
    }, null, 2));
    const downloadAnchor = document.createElement('a');
    downloadAnchor.setAttribute('href', dataStr);
    downloadAnchor.setAttribute('download', `freedom_broker_cqa_export_${new Date().toISOString().slice(0, 10)}.json`);
    document.body.appendChild(downloadAnchor);
    downloadAnchor.click();
    downloadAnchor.remove();
  }

  // --- UTILS ---
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
    exportCSV,
    exportJSON
  };

  // Run on DOM loaded
  document.addEventListener('DOMContentLoaded', init);

})();
