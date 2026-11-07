# CQA server

From the repository root, use either the private `data/raw/dev.json` file from the team or put the DEV endpoint in the ignored `.env` file as `DATA_API_URL=...`. The server prefers the local file, imports records on startup, and stores them in the ignored `data/server.sqlite3`. If the source is unavailable, previously imported records remain accessible. No API URL or dialogue content is logged.

Install dependencies once:

```sh
python3 -m venv .venv && .venv/bin/python -m pip install -r server/requirements.txt
```

Start with one command:

```sh
.venv/bin/python -m uvicorn server.app:app --reload
```

Open `http://127.0.0.1:8000/api/health`, `/api/conversations`, or `/docs`. For manual labeling, open `/api/conversations/{id}/view` to see numbered messages. To import again without restarting, call `POST /api/conversations/sync`. The list returns `total: null` until an evaluation is saved. The API keeps transcript order and original timestamps; the zero-based array position is `message_index`.

The browser API base URL is `http://127.0.0.1:8000/api` when the web page runs on the same computer. CORS is enabled for the prototype's HTML/JS interface.

`server.scoring.score_answers(checklist, answers)` calls `src/cqa/scoring.py` directly. `POST /api/evaluations` calls the merged `src/cqa/evaluator.py`, which obtains answers and evidence from a real model before the shared scoring function computes points. Set `EVAL_MODELS` in `.env` to choose the model chain. Model and checklist failures return a saved result with `status: "error"` and an appropriate HTTP error status.

The server also creates SQLite tables for checklists, evaluations, appeals, and decisions. On first startup it imports the chat and call checklists from `src/cqa/`. Use `GET /api/checklists` for versions and `GET /api/checklists/{id}` for a complete checklist. Send an edited complete checklist JSON to `POST /api/checklists` to save a new immutable version; its `id` can identify the version being copied. The new version stays inactive until `POST /api/checklists/{id}/activate`. Each channel has its own active checklist.

Send `{"conversation_id":"..."}` to `POST /api/evaluations` to use the channel's active checklist, or add `checklist_id` to choose a specific version. Read it later with `GET /api/evaluations/{id}`. `POST /api/evaluations/batch` accepts `{"conversation_ids":["..."],"checklist_id":"..."}` and returns a result or an error for each ID. `POST /api/evaluations/{id}/appeals` accepts `criterion_id` and `comment`; `GET /api/appeals?status=open` lists pending cases. `POST /api/appeals/{id}/decision` accepts `decision`, `reason`, and `new_answer` for a change. Scores are recalculated by `src/cqa/scoring.py`.
