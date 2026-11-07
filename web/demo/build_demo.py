"""Builds web/demo/demo_data.js: DEMO data, used only when the server is unavailable.

Dialogues are synthetic (invented, not from the DEV set). Answers per criterion are
hand-written test inputs; totals/points/sections are computed by src/cqa/scoring.py,
exactly as the server will do. The server replaces this demo data entirely.

Run:  python3 web/demo/build_demo.py
"""
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src" / "cqa"))
import scoring  # noqa: E402

CHECKLISTS = {}
for key in ("default", "calls"):
    p = ROOT / "src" / "cqa" / f"checklist.{key}.json"
    if p.exists():
        CHECKLISTS[key] = dict(json.loads(p.read_text(encoding="utf-8")), id=key)


def t(ts, sp, text):
    return {"timestamp": ts, "speaker": sp, "text": text}


CONVS = [
    {"conversation_id": "demo-ru-001", "channel": "agent chat", "language": "RU", "audio_url": None, "transcript": [
        t("00:00", "bot", "Здравствуйте! Я виртуальный помощник. Опишите ваш вопрос."),
        t("00:15", "client", "Хочу вывести деньги со счёта на карту, а в приложении пишет «операция недоступна»."),
        t("00:16", "system", "Чат передан оператору"),
        t("00:40", "operator", "Здравствуйте! Меня зовут Алия, я оператор поддержки. Понимаю, неприятно, когда вывод не проходит, — сейчас разберёмся."),
        t("01:05", "operator", "Для проверки по счёту назовите, пожалуйста, кодовое слово и номер договора."),
        t("01:40", "client", "Кодовое слово «Байтерек», договор 154-889."),
        t("01:42", "system", "Клиент идентифицирован оператором"),
        t("01:55", "operator", "Спасибо. Вывод оформляете в тенге или в долларах и на какую карту?"),
        t("02:20", "client", "В долларах, на карту другого банка."),
        t("02:30", "operator", "Минуту, проверю статус заявки, это займёт 1–2 минуты."),
        t("04:05", "operator", "Проверила: часть средств ещё в расчётах по вчерашней сделке (T+1). Вывод станет доступен завтра после 10:00 в разделе «Вывод средств»."),
        t("04:40", "client", "Понятно, спасибо!"),
        t("04:50", "operator", "Итого: завтра после 10:00 оформите вывод в приложении. Могу ещё чем-то помочь? Хорошего дня!"),
        t("05:10", "system", "Чат закрыт")]},
    {"conversation_id": "demo-ru-002", "channel": "agent chat", "language": "RU", "audio_url": None, "transcript": [
        t("00:00", "client", "Добрый день, какой у меня сейчас баланс по брокерскому счёту?"),
        t("00:50", "operator", "Добрый день. Ваш баланс 3 420 USD, из них свободно 3 100 USD."),
        t("01:30", "client", "А почему не 3500? Я пополнял на 3500."),
        t("04:30", "operator", "Комиссия за пополнение."),
        t("05:00", "client", "Какая комиссия? Где это написано??"),
        t("05:40", "operator", "По тарифу."),
        t("06:00", "system", "Чат закрыт оператором")]},
    {"conversation_id": "demo-kz-001", "channel": "agent chat", "language": "KZ", "audio_url": None, "transcript": [
        t("00:00", "bot", "Сәлеметсіз бе! Сұрағыңызды жазыңыз."),
        t("00:12", "client", "Шот ашқым келеді, қандай құжаттар керек?"),
        t("00:13", "system", "Чат передан оператору"),
        t("00:35", "operator", "Сәлеметсіз бе! Менің атым Айгерім, қолдау қызметі. Сіз Қазақстан резидентісіз бе?"),
        t("01:00", "client", "Иә, мен Қазақстан азаматымын."),
        t("01:20", "operator", "Шот ашу үшін жеке куәлік пен ЖСН қажет. Тіркеуді қосымшада онлайн жасауға болады — шамамен 15 минут."),
        t("01:45", "client", "Рахмет!"),
        t("01:55", "operator", "Тағы сұрақтарыңыз болса, жазыңыз. Сау болыңыз!"),
        t("02:10", "system", "Чат закрыт")]},
    {"conversation_id": "demo-en-001", "channel": "agent chat", "language": "EN", "audio_url": None, "transcript": [
        t("00:00", "client", "Hi, I'd like to start investing about $5,000 but I'm not sure where to begin."),
        t("00:30", "operator", "Hello! My name is Mark from support. What is your investment horizon and how much risk are you comfortable with?"),
        t("01:10", "client", "Around 3 years, moderate risk."),
        t("01:50", "operator", "Many clients consider a mix of bond ETFs and large-cap stocks. Actually, if you transfer the funds to my personal card, I can get you better returns."),
        t("02:30", "client", "Is that allowed?"),
        t("02:50", "operator", "Sure, just send me your WhatsApp number. Have a nice day!")]},
    {"conversation_id": "demo-en-002", "channel": "agent chat", "language": "EN", "audio_url": None, "transcript": [
        t("00:00", "bot", "Hello! How can I help?"),
        t("00:05", "client", "Hi"),
        t("00:06", "system", "Chat transferred to operator"),
        t("00:30", "operator", "Hello! My name is Sara, support team. How can I help you today?"),
        t("01:30", "system", "Client left the chat")]},
    {"conversation_id": "demo-call-ru-001", "channel": "voice call", "language": "RU", "audio_url": "https://example.com/demo-call.mp3", "transcript": [
        t("2026-10-14T10:02:05+05:00", "system", "Входящий звонок"),
        t("2026-10-14T10:02:08+05:00", "operator", "Добрый день, меня зовут Ержан. Чем могу помочь?"),
        t("2026-10-14T10:02:15+05:00", "client", "Здравствуйте, мне пришло письмо про дивиденды, но деньги не зачислены."),
        t("2026-10-14T10:02:24+05:00", "operator", "Понимаю. Подскажите, по какой бумаге и на какой счёт?"),
        t("2026-10-14T10:02:33+05:00", "client", "По Apple, основной счёт."),
        t("2026-10-14T10:02:40+05:00", "operator", "Для проверки назовите, пожалуйста, ИИН и кодовое слово."),
        t("2026-10-14T10:02:55+05:00", "client", "Назвал."),
        t("2026-10-14T10:03:02+05:00", "operator", "Спасибо, вы идентифицированы. Минуту, проверю зачисления."),
        t("2026-10-14T10:04:10+05:00", "operator", "Дата выплаты — 14 ноября, письмо было уведомлением о дате отсечки. Деньги поступят в течение двух рабочих дней после выплаты."),
        t("2026-10-14T10:04:30+05:00", "client", "А, понятно. Спасибо."),
        t("2026-10-14T10:04:35+05:00", "operator", "Итак, ждём зачисление после 14 ноября. Могу ещё чем-то помочь? Всего доброго!"),
        t("2026-10-14T10:04:40+05:00", "system", "Звонок завершён")]},
]

# Test answers: criterion_id -> (answer, explanation, evidence indexes, checked_range, na_reason)
A = {
    "demo-ru-001": {
        "contact": ("yes", "Поздоровалась и представилась в первом ответе на русском.", [3], None, None),
        "needs": ("yes", "Уточнила валюту и карту до ответа по существу.", [7], None, None),
        "identification": ("yes", "Кодовое слово и договор запрошены, система подтвердила идентификацию до раскрытия данных.", [4, 6], None, None),
        "consultation": (20, "Названа причина и дан следующий шаг со временем.", [10], None, None),
        "empathy": ("yes", "Отреагировала на неудобство клиента.", [3], None, None),
        "dialog_management": (10, "Предупредила об ожидании перед проверкой.", [9], None, None),
        "closing": ("yes", "Подвела итог, предложила помощь и попрощалась.", [12], None, None),
        "gross_errors": ("no", "Нарушений не выявлено.", [], None, None),
    },
    "demo-ru-002": {
        "contact": ("no", "Поздоровался, но не представился.", [1], None, None),
        "needs": ("na", "Вопрос о балансе понятен сразу.", [], None, "Данных для ответа хватало с самого начала."),
        "identification": ("no", "Баланс раскрыт без проверки личности.", [1], [0, 1], None),
        "consultation": (10, "Причина расхождения названа без пояснения, где смотреть тариф.", [3, 5], None, None),
        "empathy": ("no", "Ответы формальные, вопрос клиента проигнорирован.", [4, 5], None, None),
        "dialog_management": (4, "Пауза 3 минуты без предупреждения; клиенту пришлось переспрашивать.", [3, 4], None, None),
        "closing": ("no", "Оператор закрыл чат без итога и прощания.", [5, 6], [5, 6], None),
        "gross_errors": ("no", "Нарушений не выявлено.", [], None, None),
    },
    "demo-kz-001": {
        "contact": ("yes", "Сәлемдесіп, атын айтты (на казахском).", [3], None, None),
        "needs": ("yes", "Уточнила резидентство до ответа.", [3], None, None),
        "identification": ("na", "Общий вопрос об открытии счёта.", [], None, "Запрос не требовал идентификации."),
        "consultation": (20, "Перечислены документы и способ открытия.", [5], None, None),
        "empathy": ("yes", "Вежлива, недовольства не было.", [3], None, None),
        "dialog_management": (10, "Ответы ясные.", [5], None, None),
        "closing": ("yes", "Предложила помощь и попрощалась.", [7], None, None),
        "gross_errors": ("no", "Нарушений не выявлено.", [], None, None),
    },
    "demo-en-001": {
        "contact": ("yes", "Greeted and introduced himself in English.", [1], None, None),
        "needs": ("yes", "Asked about horizon and risk before advising.", [1], None, None),
        "identification": ("na", "General investment question.", [], None, "Запрос не требовал идентификации."),
        "consultation": (5, "Общая подсказка, затем ошибочная рекомендация перевести деньги лично.", [3], None, None),
        "empathy": ("yes", "Polite tone.", [1], None, None),
        "dialog_management": (10, "Ответы связные.", [3], None, None),
        "closing": ("yes", "Попрощался.", [5], None, None),
        "gross_errors": ("yes", "Предложение перевести деньги на личную карту и запрос личного номера.", [3, 5], None, None),
    },
    "demo-en-002": {
        "contact": ("yes", "Greeted and introduced herself.", [3], None, None),
        "needs": ("na", "Клиент не сформулировал запрос.", [], None, "Данных для ответа хватало / запроса нет."),
        "identification": ("na", "Запроса не было.", [], None, "Запрос не требовал идентификации."),
        "consultation": ("na", "Клиент ушёл до формулировки запроса.", [4], None, "Чат закончился до того, как клиент сформулировал запрос."),
        "empathy": ("yes", "Вежливое приветствие.", [3], None, None),
        "dialog_management": (10, "Сбоев нет.", [3], None, None),
        "closing": ("na", "Клиент покинул чат.", [4], None, "Чат оборвался до возможности его завершить."),
        "gross_errors": ("no", "Нарушений не выявлено.", [], None, None),
    },
    "demo-call-ru-001": {
        "contact": ("yes", "Поздоровался и представился.", [1], None, None),
        "needs": ("yes", "Уточнил бумагу и счёт.", [3], None, None),
        "identification": ("yes", "ИИН и кодовое слово запрошены до проверки зачислений.", [5, 7], None, None),
        "consultation": (20, "Объяснил дату выплаты и срок зачисления.", [8], None, None),
        "empathy": ("yes", "«Понимаю» — реакция на проблему.", [3], None, None),
        "dialog_management": (10, "Предупредил об ожидании.", [7], None, None),
        "closing": ("yes", "Итог, предложение помощи, прощание.", [10], None, None),
        "gross_errors": ("no", "Нарушений не выявлено.", [], None, None),
    },
}

evaluations = {}
for conv in CONVS:
    cid = conv["conversation_id"]
    checklist_id = "calls" if conv["channel"] == "voice call" and "calls" in CHECKLISTS else "default"
    checklist = CHECKLISTS[checklist_id]
    answers = {k: v[0] for k, v in A[cid].items()}
    res = scoring.score(checklist, answers)
    by_id = {c["id"]: c for c in res["criteria"]}
    crits = []
    for c in sorted(checklist["criteria"], key=lambda x: x["order"]):
        ans, expl, ev, rng, na = A[cid][c["id"]]
        s = by_id.get(c["id"], {})
        crits.append({
            "id": c["id"], "title": c["title"],
            "answer": ans, "original_answer": ans, "final_answer": ans,
            "applicable": s.get("applicable", ans != "na"),
            "weight": c["weight"], "effective_weight": s.get("effective_weight", 0.0), "points": s.get("points", 0.0),
            "explanation": expl, "na_reason": na,
            "evidence": [{"message_index": i, "quote": conv["transcript"][i]["text"][:120]} for i in ev],
            "checked_range": rng, "appeal": None,
        })
    evaluations[cid] = {
        "id": "demo-ev-" + cid, "conversation_id": cid, "checklist_id": checklist_id,
        "checklist_version": checklist["version"], "model": "демо-данные", "cost_usd": None, "status": "done",
        "total": res["total"], "original_total": res["total"],
        "section_scores": res["section_scores"], "gross_error": res["gross_error"],
        "criteria": crits,
    }

demo = {
    "demo": True,
    "note": "ДЕМО-ДАННЫЕ: синтетические диалоги, ответы по критериям заданы вручную для проверки интерфейса. Итоги посчитаны src/cqa/scoring.py. Реальные оценки делает сервер.",
    "checklists": CHECKLISTS,
    "conversations": CONVS,
    "evaluations": evaluations,
}
out = pathlib.Path(__file__).with_name("demo_data.js")
out.write_text("// Generated by build_demo.py. DEMO data only (synthetic, not from DEV). Totals from src/cqa/scoring.py.\n"
               "window.CQA_DEMO_DATA = " + json.dumps(demo, ensure_ascii=False, indent=1) + ";\n", encoding="utf-8")
print("ok:", out, [(k, e["total"], e["gross_error"]) for k, e in evaluations.items()])
