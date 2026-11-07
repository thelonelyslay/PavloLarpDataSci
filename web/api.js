/**
 * CQA Unified API Client (web/api.js)
 * 
 * ПОДКЛЮЧЕНИЕ К API АЛИХАНА:
 * Чтобы переключить сайт на реальный сервер Алихана, достаточно раскомментировать строку:
 *   API_BASE_URL: 'http://localhost:8000/api',
 * Или изменить её на актуальный адрес.
 * 
 * По умолчанию (когда API_BASE_URL = null) сайт берет данные из единого файла-заглушки mock_data.json,
 * полностью соответствующего формату из AGENTS.md.
 */

window.CQA_API_CONFIG = {
  // Для подключения реального сервера Алихана укажите URL (например: 'http://localhost:8000/api' или '/api')
  API_BASE_URL: null,

  // Единый файл-заглушка с ответом по спецификации AGENTS.md
  MOCK_DATA_URL: 'mock_data.json',

  // Таймаут сетевых запросов (мс)
  TIMEOUT_MS: 5000
};

window.CQA_API = (function () {
  'use strict';

  let cachedMockData = null;

  async function loadMockData() {
    if (cachedMockData) return cachedMockData;
    if (window.CQA_MOCK_DATA) {
      cachedMockData = window.CQA_MOCK_DATA;
      return cachedMockData;
    }
    const res = await fetch(window.CQA_API_CONFIG.MOCK_DATA_URL);
    if (!res.ok) throw new Error(`Не удалось загрузить файл-заглушку ${window.CQA_API_CONFIG.MOCK_DATA_URL}`);
    cachedMockData = await res.json();
    return cachedMockData;
  }

  return {
    isLiveApi() {
      return Boolean(window.CQA_API_CONFIG.API_BASE_URL);
    },

    setBaseUrl(url) {
      window.CQA_API_CONFIG.API_BASE_URL = url ? url.trim() : null;
    },

    getBaseUrl() {
      return window.CQA_API_CONFIG.API_BASE_URL;
    },

    /**
     * Получить список всех диалогов
     */
    async getConversations() {
      if (this.isLiveApi()) {
        const url = `${window.CQA_API_CONFIG.API_BASE_URL}/conversations`;
        const res = await fetch(url);
        if (!res.ok) throw new Error(`Ошибка API (${res.status}): ${res.statusText}`);
        const data = await res.json();
        // Сервер Алихана возвращает массив [{id, channel, language}]
        return data.map(item => ({
          conversation_id: item.id || item.conversation_id,
          channel: item.channel,
          language: item.language,
          total_score: item.total_score !== undefined ? item.total_score : null,
          gross_error: Boolean(item.gross_error),
          has_appeal: Boolean(item.has_appeal)
        }));
      } else {
        const mock = await loadMockData();
        return mock.conversations;
      }
    },

    /**
     * Получить транскрипт конкретного диалога
     */
    async getConversation(id) {
      if (this.isLiveApi()) {
        const url = `${window.CQA_API_CONFIG.API_BASE_URL}/conversations/${encodeURIComponent(id)}`;
        const res = await fetch(url);
        if (!res.ok) throw new Error(`Ошибка API (${res.status}): ${res.statusText}`);
        const data = await res.json();
        return {
          conversation_id: data.id || data.conversation_id,
          channel: data.channel,
          language: data.language,
          transcript: typeof data.transcript === 'string' ? JSON.parse(data.transcript) : data.transcript,
          audio_url: data.audio_url || null
        };
      } else {
        const mock = await loadMockData();
        const conv = mock.conversations.find(c => c.conversation_id === id);
        if (!conv) throw new Error(`Диалог ${id} не найден`);
        return conv;
      }
    },

    /**
     * Получить результат оценки диалога
     */
    async getEvaluation(id) {
      if (this.isLiveApi()) {
        try {
          const url = `${window.CQA_API_CONFIG.API_BASE_URL}/evaluations/${encodeURIComponent(id)}`;
          const res = await fetch(url);
          if (res.ok) {
            return await res.json();
          }
        } catch (e) {
          // Если на сервере оценка еще не создана, fallback к расчету или mock
        }
      }
      const mock = await loadMockData();
      return mock.evaluations[id] || null;
    },

    /**
     * Проверка доступности API (health check)
     */
    async checkHealth() {
      if (!this.isLiveApi()) return { status: 'mock', message: 'Файл-заглушка (mock_data.json)' };
      const url = `${window.CQA_API_CONFIG.API_BASE_URL}/health`;
      const res = await fetch(url, { signal: AbortSignal.timeout(3000) });
      if (!res.ok) throw new Error(`Сервер недоступен (${res.status})`);
      return { status: 'online', message: 'FastAPI сервер Алихана онлайн' };
    }
  };
})();
