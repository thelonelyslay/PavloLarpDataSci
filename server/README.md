# CQA server

From the repository root, put the DEV endpoint in the ignored `.env` file as `DATA_API_URL=...`. The server fetches records on startup and stores them in the ignored `data/server.sqlite3`. If the endpoint is unavailable, previously imported records remain accessible. No API URL or dialogue content is logged.

Install dependencies once:

```sh
python3 -m venv .venv && .venv/bin/python -m pip install -r server/requirements.txt
```

Start with one command:

```sh
.venv/bin/python -m uvicorn server.app:app --reload
```

Open `http://127.0.0.1:8000/api/health`, `/api/conversations`, or `/docs`. To import again without restarting, call `POST /api/conversations/sync`. The list returns `total: null` until an evaluation is saved. The API keeps transcript order and original timestamps; the zero-based array position is `message_index`.

`server.scoring.score_answers(checklist, answers)` calls `src/cqa/scoring.py` directly. Automatic evaluation awaits the team's model-answer module and the final API contract.
