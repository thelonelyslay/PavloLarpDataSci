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

Open `http://127.0.0.1:8000/` for the site, `http://127.0.0.1:8000/api/health` for health, or `/docs` for API documentation. For manual labeling, open `/api/conversations/{id}/view` to see numbered messages. To import again without restarting, call `POST /api/conversations/sync`. The API keeps transcript order and original timestamps; the zero-based array position is `message_index`.

The browser API base URL is `http://127.0.0.1:8000/api` when the web page runs on the same computer. CORS is enabled for the prototype's HTML/JS interface.

`server.scoring.score_answers(checklist, answers)` calls `src/cqa/scoring.py` directly. `POST /api/evaluations` calls the merged `src/cqa/evaluator.py`, which obtains answers and evidence from a real model before the shared scoring function computes points. Set `EVAL_MODELS` in `.env` to choose the model chain. Model and checklist failures return a saved result with `status: "error"` and an appropriate HTTP error status.

The server also creates SQLite tables for checklists, evaluations, appeals, and decisions. On first startup it imports the chat and call checklists from `src/cqa/`. Use `GET /api/checklists` for versions and `GET /api/checklists/{id}` for a complete checklist. Send an edited complete checklist JSON to `POST /api/checklists` to save a new immutable version; its `id` can identify the version being copied. The new version stays inactive until `POST /api/checklists/{id}/activate`. Each channel has its own active checklist.

Send `{"conversation_id":"..."}` to `POST /api/evaluations` to use the channel's active checklist, or add `checklist_id` to choose a specific version. To evaluate a new JSON dialogue, send `{"conversation": {"conversation_id":"...","channel":"agent chat","language":"RU","transcript":[...]}}`; the server saves it first. `POST /api/conversations` also saves a new dialogue without evaluating it. Read results with `GET /api/evaluations/{id}`; the conversation list includes the latest evaluation ID, total, gross-error flag, and appeal flag. Filter gross errors with `GET /api/conversations?gross_error=true`.

`POST /api/evaluations/batch` accepts `{"conversation_ids":["..."],"checklist_id":"..."}` and returns a result or an error for each ID. For live progress, start with `POST /api/evaluations/batch/start` using the same body, then poll `GET /api/evaluations/batch/{job_id}` for `completed`, `total`, and per-dialogue results. `POST /api/checklists/{id}/recalculate` starts this kind of job for every dialogue matching that checklist's channel. `POST /api/evaluations/{id}/appeals` accepts `criterion_id` and `comment`; `GET /api/appeals?status=open` lists pending cases. `POST /api/appeals/{id}/decision` accepts `decision`, `reason`, and `new_answer` for a change. Scores are recalculated by `src/cqa/scoring.py`.

`GET /api/export?set=dev` builds, validates, and downloads `results/dev/results.json` using `src/cqa/export_results.py` and the server evaluation cache. `GET /api/export?set=prod` uses `data/raw/prod.json`, or `PROD_GROUP_ID` from `.env` with `DATA_API_URL`. Add `only_cached=true` to avoid new model calls; the request returns an error if any required evaluation is missing. Commit the validated files under `results/` before the stop-code deadline; this directory is intentionally tracked by Git.
