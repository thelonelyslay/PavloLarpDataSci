# CQA: оценка качества диалогов операторов

Прототип для Freedom Broker. Система читает диалог (чат или звонок), по чек-листу оценивает работу **оператора**
и для каждого критерия показывает ответ, баллы, короткое объяснение и реплику-доказательство.
Ответы даёт нейросеть, а итоговый балл считает обычный код (`src/cqa/scoring.py`), чтобы его можно было проверить.
Чек-листы хранятся в JSON, а не в коде: критерии можно менять без программиста.

Диалоги на русском, казахском и английском. Оценивается только оператор; реплики бота и системы это контекст.

## Быстрый старт (5 команд)

Нужен Python 3.9 или новее и ключ хотя бы одной нейросети (Claude или Gemini).

```bash
git clone https://github.com/thelonelyslay/PavloLarpDataSci.git && cd PavloLarpDataSci
python3 -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                     # затем откройте .env и впишите ключи
python3 src/cqa/fetch_data.py && python3 src/cqa/evaluator.py --id p1-ru-001
```

Последняя команда скачивает диалоги в `data/raw/dev.json` и печатает оценку одного диалога в JSON:
итог, баллы по разделам и по каждому критерию объяснение и номер реплики-доказательства.

Что вписать в `.env`:
- `DATA_API_URL`: ссылка на API с диалогами (выдают организаторы);
- `ANTHROPIC_API_KEY` и/или `GEMINI_API_KEY` (для Gemini ещё `GEMINI_MODEL`);
- `EVAL_MODELS`: какие модели пробовать по очереди, если первая не ответила.

## Другие команды

| Что сделать | Команда |
|---|---|
| Оценить один диалог другой моделью | `python3 src/cqa/evaluator.py --id p1-ru-001 --model claude-opus-5-5` |
| Оценить по своему чек-листу | `python3 src/cqa/evaluator.py --id p1-ru-001 --checklist мой.json` |
| Сравнить модели: точность, время, цена | `python3 src/cqa/compare_models.py --show-answers` |
| Собрать ручную разметку из `docs/labels_*.csv` | `python3 src/cqa/labels_from_csv.py` |
| Запустить тесты | `cd src/cqa && python3 -m unittest -v` |

## Где что лежит

- `src/cqa/evaluator.py`: оценка одного диалога (`evaluate()`), выбор модели, запасные модели.
- `src/cqa/scoring.py`: подсчёт баллов (N/A, перераспределение весов, итог). Покрыт тестами.
- `src/cqa/prompting.py`: текст задания для нейросети и проверка её ответа.
- `src/cqa/llm.py`: обращение к Claude, Gemini, OpenAI.
- `src/cqa/checklist.default.json` (чаты) и `src/cqa/checklist.calls.json` (звонки): чек-листы.
- `AGENTS.md`: общие договорённости команды и формат ответа API.

## Сервер и сайт

_Дописать позже:_ как запустить сервер (`server/`) и веб-интерфейс (`web/`): список диалогов,
страница оценки с переходом к реплике, апелляции, редактор чек-листов.

## Правила

- Ключи и ссылка на данные только в `.env`. Этот файл в Git не попадает.
- Диалоги и результаты (`data/`) в Git не попадают.
