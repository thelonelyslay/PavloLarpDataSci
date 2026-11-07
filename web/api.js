/**
 * CQA API client (web/api.js). Endpoints follow the contract in AGENTS.md.
 *
 * Two modes:
 *  - "live": the FastAPI server answers GET {base}/health. All data, evaluations,
 *    totals, model and cost come from the server. Nothing is mixed with demo data.
 *  - "demo": the server is unavailable. Read-only DEMO data from demo/demo_data.js
 *    (synthetic dialogues). The UI marks this mode clearly; evaluation is disabled.
 *
 * Server address: API_BASE ('/api', relative: the server serves web/ at http://127.0.0.1:8000/).
 * Overrides for development: ?api=http://127.0.0.1:8000/api or the address in the "Источник данных" dialog.
 */
const API_BASE = '/api';
// Server export of all results (Alikhan's endpoint). The button turns on when the server lists this route in /openapi.json.
const EXPORT_PATH = '/export';
window.CQA_API = (function () {
  'use strict';

  const TIMEOUT_MS = 8000;
  const EVAL_TIMEOUT_MS = 180000; // evaluation runs an LLM on the server
  const LS_KEY = 'cqa_api_url';

  function defaultBase() {
    const q = new URLSearchParams(location.search).get('api');
    if (q) return q;
    try { const saved = localStorage.getItem(LS_KEY); if (saved) return saved; } catch (e) { /* storage off */ }
    return API_BASE;
  }

  let base = defaultBase().replace(/\/$/, '');
  let mode = 'demo';
  const demo = window.CQA_DEMO_DATA || { demo: true, checklists: {}, conversations: [], evaluations: {} };
  const clone = o => JSON.parse(JSON.stringify(o));
  const EV_KEY = 'cqa_last_evaluations';
  function rememberEval(ev) {
    if (!ev || !ev.id || !ev.conversation_id) return;
    try { const m = JSON.parse(localStorage.getItem(EV_KEY) || '{}'); m[base + '|' + ev.conversation_id] = ev.id; localStorage.setItem(EV_KEY, JSON.stringify(m)); } catch (e) { /* storage off */ }
  }
  function rememberedEvalId(cid) {
    try { return (JSON.parse(localStorage.getItem(EV_KEY) || '{}'))[base + '|' + cid] || null; } catch (e) { return null; }
  }

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
        const err = new Error(`${res.status}: ${typeof msg === 'string' ? msg : JSON.stringify(msg)}`);
        err.status = res.status;
        err.data = data; // e.g. a saved evaluation with status "error"
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
      if (!/^https?:/.test(base) && !location.protocol.startsWith('http')) {
        mode = 'demo';
        return { status: 'demo', message: 'ДЕМО-ДАННЫЕ (страница открыта как файл)' };
      }
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
        const id = conv.last_evaluation_id || rememberedEvalId(conv.conversation_id);
        if (!id) return null;
        try { return await req('GET', '/evaluations/' + encodeURIComponent(id)); }
        catch (e) { if (e.status === 404) return null; throw e; }
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
      const body = { conversation_id: conversationId };
      if (checklistId) body.checklist_id = checklistId; // otherwise the server uses the channel's active checklist
      try {
        const ev = await req('POST', '/evaluations', body, EVAL_TIMEOUT_MS);
        rememberEval(ev);
        return ev;
      } catch (e) {
        // A failed model call is saved by the server as an evaluation with status "error" (it has id and conversation_id).
        // Other errors ({status: "error", detail}) are plain request errors.
        if (e.data && e.data.status === 'error' && e.data.id && e.data.conversation_id) { rememberEval(e.data); return e.data; }
        throw e;
      }
    },

    /** Background batch: POST /api/evaluations/batch/start → {id, status, completed, total, results} */
    async startBatch(conversationIds, checklistId) {
      if (mode !== 'live') demoOnly('Пакетная оценка');
      const body = { conversation_ids: conversationIds };
      if (checklistId) body.checklist_id = checklistId;
      return req('POST', '/evaluations/batch/start', body);
    },
    /** GET /api/evaluations/batch/{job_id}: progress and results so far */
    async getBatch(jobId) {
      return req('GET', '/evaluations/batch/' + encodeURIComponent(jobId));
    },
    /** POST /api/checklists/{id}/recalculate: re-evaluate every conversation of the checklist's channel (background job) */
    async recalculateChecklist(checklistId) {
      if (mode !== 'live') demoOnly('Пересчёт по чек-листу');
      return req('POST', '/checklists/' + encodeURIComponent(checklistId) + '/recalculate', {});
    },
    /** GET /api/evaluations: up to 100 newest evaluations (to fill the list in one request) */
    async getRecentEvaluations() {
      if (mode !== 'live') return Object.values(clone(demo.evaluations));
      const r = await req('GET', '/evaluations');
      return Array.isArray(r) ? r : [];
    },

    async getChecklists() {
      if (mode === 'live') {
        const r = await req('GET', '/checklists');
        const list = Array.isArray(r) ? r : Object.values(r || {});
        const out = {};
        for (const item of list) {
          const full = item.criteria ? item : await req('GET', '/checklists/' + encodeURIComponent(item.id));
          out[full.id] = Object.assign({}, full, { id: item.id, active: Boolean(item.active) });
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

    /** Add new conversations: POST /api/conversations, one record per request (same format as the data API) */
    async addConversations(records) {
      if (mode !== 'live') demoOnly('Загрузка нового диалога');
      const saved = [];
      for (const r of records) {
        try { saved.push(await req('POST', '/conversations', r)); }
        catch (e) {
          if (e.status === 405) throw new Error('сервер не принимает новые диалоги (нет POST /api/conversations)');
          throw new Error(`${r.conversation_id}: ${e.message}`);
        }
      }
      return saved;
    },

    /** Does the server have this route? Reads FastAPI's /openapi.json (served next to /api). */
    async hasRoute(path) {
      if (mode !== 'live') return false;
      try {
        const root = base.replace(/\/api$/, '');
        const res = await fetch(root + '/openapi.json', { headers: { Accept: 'application/json' } });
        if (!res.ok) return false;
        const spec = await res.json();
        return Object.keys(spec.paths || {}).some(p => p === '/api' + path || p.startsWith('/api' + path + '/') || p.startsWith('/api' + path + '?'));
      } catch (e) { return false; }
    },

    /** GET /api/export: all results as JSON from the server */
    async exportResults() {
      if (mode !== 'live') demoOnly('Экспорт результатов с сервера');
      // The server may evaluate missing dialogues first (up to an hour). Keep its file byte-for-byte.
      const ctrl = new AbortController();
      const timer = setTimeout(() => ctrl.abort(), 65 * 60 * 1000);
      try {
        const res = await fetch(base + EXPORT_PATH, { signal: ctrl.signal });
        const text = await res.text();
        if (!res.ok) {
          let msg = res.statusText;
          try { const j = JSON.parse(text); msg = j.detail || j.error || msg; } catch (e) { /* not JSON */ }
          throw new Error(res.status === 409 ? 'выгрузка уже идёт, дождитесь файла' : `${res.status}: ${msg}`);
        }
        const cd = res.headers.get('Content-Disposition') || '';
        const m = cd.match(/filename\*?=(?:UTF-8'')?"?([^";]+)"?/i);
        return { text, filename: m ? decodeURIComponent(m[1]) : null };
      } catch (e) {
        if (e.name === 'AbortError') throw new Error('сервер не ответил за 65 минут');
        throw e;
      } finally { clearTimeout(timer); }
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
