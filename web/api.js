/**
 * CQA API client (web/api.js). Endpoints follow the contract in AGENTS.md.
 *
 * Two modes:
 *  - "live": the FastAPI server answers GET {base}/health. All data, evaluations,
 *    totals, model and cost come from the server. Nothing is mixed with demo data.
 *  - "demo": the server is unavailable. Read-only DEMO data from demo/demo_data.js
 *    (synthetic dialogues). The UI marks this mode clearly; evaluation is disabled.
 *
 * Server address: saved in the UI (Источник данных), or ?api=http://host:8000/api,
 * or same origin /api when the page is served by the server, else http://localhost:8000/api.
 */
window.CQA_API = (function () {
  'use strict';

  const TIMEOUT_MS = 8000;
  const EVAL_TIMEOUT_MS = 180000; // evaluation runs an LLM on the server
  const LS_KEY = 'cqa_api_url';

  function defaultBase() {
    const q = new URLSearchParams(location.search).get('api');
    if (q) return q;
    try { const saved = localStorage.getItem(LS_KEY); if (saved) return saved; } catch (e) { /* storage off */ }
    if (location.protocol.startsWith('http')) return location.origin + '/api';
    return 'http://localhost:8000/api';
  }

  let base = defaultBase().replace(/\/$/, '');
  let mode = 'demo';
  const demo = window.CQA_DEMO_DATA || { demo: true, checklists: {}, conversations: [], evaluations: {} };
  const clone = o => JSON.parse(JSON.stringify(o));

  async function req(method, path, body, timeout) {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeout || TIMEOUT_MS);
    try {
      const res = await fetch(base + path, {
        method,
        headers: body ? { 'Content-Type': 'application/json', Accept: 'application/json' } : { Accept: 'application/json' },
        body: body ? JSON.stringify(body) : undefined,
        signal: ctrl.signal,
      });
      const text = await res.text();
      let data = null;
      try { data = text ? JSON.parse(text) : null; } catch (e) { data = text; }
      if (!res.ok) {
        const msg = (data && (data.detail || data.error || data.message)) || res.statusText;
        const err = new Error(`${method} ${path}: ${res.status} ${typeof msg === 'string' ? msg : JSON.stringify(msg)}`);
        err.status = res.status;
        throw err;
      }
      return data;
    } catch (e) {
      if (e.name === 'AbortError') throw new Error(`${method} ${path}: нет ответа за ${(timeout || TIMEOUT_MS) / 1000} с`);
      throw e;
    } finally { clearTimeout(timer); }
  }

  function demoOnly(action) {
    const e = new Error(`${action} работает только с сервером. Сейчас открыты демо-данные.`);
    e.demo = true;
    throw e;
  }

  // Server returns {id,...}; UI uses conversation_id everywhere.
  function normConvItem(x) {
    return {
      conversation_id: x.conversation_id || x.id,
      channel: x.channel,
      language: x.language,
      total_score: x.total !== undefined ? x.total : (x.total_score !== undefined ? x.total_score : null),
      gross_error: Boolean(x.gross_error),
      has_appeal: Boolean(x.has_appeal),
      last_evaluation_id: x.last_evaluation_id || x.evaluation_id || null,
      evaluation_status: x.status || x.evaluation_status || null,
    };
  }

  return {
    get mode() { return mode; },
    isLive() { return mode === 'live'; },
    getBaseUrl() { return base; },
    setBaseUrl(url) {
      base = (url || '').trim().replace(/\/$/, '');
      try { if (base) localStorage.setItem(LS_KEY, base); else localStorage.removeItem(LS_KEY); } catch (e) { /* storage off */ }
    },

    /** Decide the mode: live if GET /health answers, otherwise demo. */
    async connect() {
      try {
        await req('GET', '/health', null, 3000);
        mode = 'live';
        return { status: 'online', message: 'Сервер: ' + base };
      } catch (e) {
        mode = 'demo';
        return { status: 'demo', message: 'ДЕМО-ДАННЫЕ (сервер недоступен)', error: e.message };
      }
    },

    async getConversations() {
      if (mode === 'live') return (await req('GET', '/conversations')).map(normConvItem);
      return demo.conversations.map(c => {
        const ev = demo.evaluations[c.conversation_id];
        return Object.assign(normConvItem({ conversation_id: c.conversation_id, channel: c.channel, language: c.language,
          total: ev ? ev.total : null, gross_error: ev ? ev.gross_error : false, last_evaluation_id: ev ? ev.id : null }),
          { transcript: clone(c.transcript), audio_url: c.audio_url });
      });
    },

    async getConversation(id) {
      if (mode === 'live') {
        const d = await req('GET', '/conversations/' + encodeURIComponent(id));
        return {
          conversation_id: d.conversation_id || d.id, channel: d.channel, language: d.language,
          transcript: typeof d.transcript === 'string' ? JSON.parse(d.transcript) : d.transcript,
          audio_url: d.audio_url || null,
          last_evaluation_id: d.last_evaluation_id || d.evaluation_id || null,
        };
      }
      const c = demo.conversations.find(x => x.conversation_id === id);
      if (!c) throw new Error(`Диалог ${id} не найден`);
      return clone(c);
    },

    /** Latest evaluation of a conversation, or null if it was not evaluated yet. */
    async getLatestEvaluation(conv) {
      if (mode === 'live') {
        if (conv.last_evaluation_id) return req('GET', '/evaluations/' + encodeURIComponent(conv.last_evaluation_id));
        try {
          const r = await req('GET', '/evaluations?conversation_id=' + encodeURIComponent(conv.conversation_id));
          const list = Array.isArray(r) ? r : (r ? [r] : []);
          return list.length ? list[list.length - 1] : null;
        } catch (e) { if (e.status === 404) return null; throw e; }
      }
      const ev = demo.evaluations[conv.conversation_id];
      return ev ? clone(ev) : null;
    },

    async getEvaluation(id) {
      if (mode === 'live') return req('GET', '/evaluations/' + encodeURIComponent(id));
      const ev = Object.values(demo.evaluations).find(e => e.id === id);
      return ev ? clone(ev) : null;
    },

    /** POST /api/evaluations {conversation_id, checklist_id} */
    async evaluate(conversationId, checklistId) {
      if (mode !== 'live') demoOnly('Оценка диалога');
      return req('POST', '/evaluations', { conversation_id: conversationId, checklist_id: checklistId }, EVAL_TIMEOUT_MS);
    },

    /** POST /api/evaluations/batch {conversation_ids, checklist_id} */
    async evaluateBatch(conversationIds, checklistId) {
      if (mode !== 'live') demoOnly('Пакетная оценка');
      return req('POST', '/evaluations/batch', { conversation_ids: conversationIds, checklist_id: checklistId }, EVAL_TIMEOUT_MS);
    },

    async getChecklists() {
      if (mode === 'live') {
        const r = await req('GET', '/checklists');
        const list = Array.isArray(r) ? r : Object.values(r || {});
        const out = {};
        for (const item of list) {
          const full = item.criteria ? item : await req('GET', '/checklists/' + encodeURIComponent(item.id));
          out[full.id] = full;
        }
        return out;
      }
      return clone(demo.checklists);
    },

    /** POST /api/checklists: save a new version; returns the saved checklist (with id). */
    async saveChecklist(checklist) {
      if (mode !== 'live') demoOnly('Сохранение чек-листа на сервере');
      return req('POST', '/checklists', checklist);
    },

    async activateChecklist(id) {
      if (mode !== 'live') demoOnly('Активация чек-листа');
      return req('POST', '/checklists/' + encodeURIComponent(id) + '/activate', {});
    },

    /** POST /api/evaluations/{id}/appeals {criterion_id, comment} */
    async createAppeal(evaluationId, criterionId, comment) {
      if (mode !== 'live') demoOnly('Подача апелляции');
      return req('POST', '/evaluations/' + encodeURIComponent(evaluationId) + '/appeals', { criterion_id: criterionId, comment });
    },

    async getAppeals(status) {
      if (mode !== 'live') return [];
      return req('GET', '/appeals' + (status ? '?status=' + encodeURIComponent(status) : ''));
    },

    /** POST /api/appeals/{id}/decision {decision, new_answer?, reason} */
    async decideAppeal(appealId, decision, newAnswer, reason) {
      if (mode !== 'live') demoOnly('Решение по апелляции');
      const body = { decision, reason };
      if (decision === 'change') body.new_answer = newAnswer;
      return req('POST', '/appeals/' + encodeURIComponent(appealId) + '/decision', body);
    },
  };
})();
