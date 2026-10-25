"""Spending agent: one phrase in, one row in the spending log out, plus the week's remaining limit.

    python3 spending_agent.py "продукты 908"

The phrase goes to a model on OpenRouter, which turns it into amount, category and description.
The log itself never leaves the computer: the row is written and the week is summed locally.
Without OPENROUTER_API_KEY (or if the model is unavailable) a simple offline parser is used.
"""

import datetime as dt
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.request

log = logging.getLogger("spending_agent")

WEEKLY_LIMIT = int(os.environ.get("SPENDING_WEEKLY_LIMIT", "12000"))
LOG_PATH = os.environ.get("SPENDING_LOG", os.path.join(os.path.dirname(__file__), "examples", "spending-log.example.md"))
# Free models on OpenRouter come and go; several comma-separated ids are tried in turn.
MODELS = os.environ.get(
    "OPENROUTER_MODEL",
    "google/gemma-4-31b-it:free,nvidia/nemotron-3-ultra-550b-a55b:free,google/gemma-4-26b-a4b-it:free,"
    "xingchen-agi/xing4.0-29b-a4b:free",
).split(",")
API_URL = "https://openrouter.ai/api/v1/chat/completions"
RETRY_AFTER_SECONDS = 3

TABLE_HEADER = "| Дата | Сумма, ₽ | Категория | Что |"
CATEGORIES = ["продукты", "Кир", "транспорт", "дом и быт", "связь", "праздники и подарки", "прочее",
              "долги и рассрочки", "подписки", "стоматолог", "перевод"]
# Categories that do not count against the weekly living limit (mandatory payments, debts, transfers).
# Offline parser: words that point to a category when the category name itself is not in the phrase.
KEYWORDS = {"такси": "транспорт", "проезд": "транспорт", "метро": "транспорт", "автобус": "транспорт",
            "магнит": "продукты", "лавка": "продукты", "самокат": "продукты", "пятёрочка": "продукты"}
OUT_OF_LIMIT = {"долги и рассрочки", "подписки", "стоматолог", "перевод"}
OUT_OF_LIMIT_MARK = "вне лимита"

ROW_RE = re.compile(r"^\|\s*(\d{2})\.(\d{2})\s*\|\s*(-?[\d\s]+)\s*\|\s*([^|]+?)\s*\|\s*(.*?)\s*\|\s*$")

SYSTEM_PROMPT = (
    "Ты разбираешь фразу о трате на русском языке. Верни только JSON без пояснений: "
    '{"amount": число в рублях (возврат - отрицательное), "category": одна из '
    f"{CATEGORIES}, \"description\": коротко что купили и с какой карты, если сказано, "
    '"days_ago": 0 если сегодня, 1 если вчера}.'
)


def parse_offline(phrase):
    """Fallback parser for phrases like «продукты 908» or «такси 899 вчера»."""
    m = re.search(r"-?\d[\d\s]*", phrase)
    if not m:
        raise ValueError(f"не нашла сумму во фразе: {phrase!r}")
    amount = int(m.group().replace(" ", ""))
    words = phrase.replace(m.group(), " ").lower()
    days_ago = 1 if "вчера" in words else 0
    words = words.replace("вчера", " ").replace("сегодня", " ")
    description = " ".join(words.split()) or "без описания"
    category = next((c for c in CATEGORIES if c.lower() in description), None) or next(
        (cat for word, cat in KEYWORDS.items() if word in description), "прочее")
    return {"amount": amount, "category": category, "description": description, "days_ago": days_ago}


def parse_with_model(phrase, api_key, model):
    body = json.dumps({
        "model": model,
        # Instructions go into the user message: some free providers (e.g. Gemma) reject a system role.
        "messages": [{"role": "user", "content": f"{SYSTEM_PROMPT}\n\nФраза: {phrase}"}],
        "temperature": 0,
    }).encode()
    req = urllib.request.Request(API_URL, data=body, headers={
        "Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        content = json.load(resp)["choices"][0]["message"]["content"]
    data = json.loads(content[content.index("{"):content.rindex("}") + 1])
    if data.get("category") not in CATEGORIES:
        data["category"] = "прочее"
    return {"amount": int(data["amount"]), "category": data["category"],
            "description": str(data.get("description") or "без описания"), "days_ago": int(data.get("days_ago") or 0)}


def http_error_detail(exc):
    try:
        error = json.loads(exc.read().decode()).get("error", {})
    except (ValueError, AttributeError):
        return str(exc)
    raw = str(error.get("metadata", {}).get("raw", ""))[:200]
    return " | ".join(x for x in (str(exc), error.get("message", ""), raw) if x)


def parse(phrase):
    """Try each model in turn; free endpoints go down often, so one failure must not hide the others."""
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if api_key:
        for model in MODELS:
            for attempt in (1, 2):
                try:
                    return parse_with_model(phrase, api_key, model), model
                except urllib.error.HTTPError as exc:
                    log.warning("%s unavailable: %s", model, http_error_detail(exc))
                    if exc.code != 429 or attempt == 2:
                        break
                    time.sleep(RETRY_AFTER_SECONDS)  # free pools are rate-limited in short bursts
                except (urllib.error.URLError, KeyError, ValueError) as exc:
                    log.warning("%s unavailable: %s", model, exc)
                    break
        log.warning("no model answered - pick working ones at https://openrouter.ai/models?q=free; "
                    "using offline parser")
    return parse_offline(phrase), "без модели"


def format_amount(n):
    return f"{n:,}".replace(",", " ")


def add_row(text, day, entry):
    """Insert the entry as the first row of the first spending table (the log keeps newest rows on top)."""
    row = f"| {day:%d.%m} | {format_amount(entry['amount'])} | {entry['category']} | {entry['description']} |"
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if line.strip() == TABLE_HEADER:
            lines.insert(i + 2, row)  # after header and separator
            return "\n".join(lines), row
    raise ValueError(f"в учёте не нашлась таблица с заголовком {TABLE_HEADER}")


def week_spent(text, today):
    """Sum of this week's (Mon-Sun) rows that count against the living limit."""
    monday = today - dt.timedelta(days=today.weekday())
    total = 0
    for line in text.split("\n"):
        m = ROW_RE.match(line)
        if not m:
            continue
        day = dt.date(today.year, int(m.group(2)), int(m.group(1)))
        category, description = m.group(4).strip(), m.group(5)
        if not monday <= day <= monday + dt.timedelta(days=6):
            continue
        if category in OUT_OF_LIMIT or OUT_OF_LIMIT_MARK in description:
            continue
        total += int(m.group(3).replace(" ", ""))
    return total


def main(argv):
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    phrase = " ".join(argv[1:]).strip()
    if not phrase:
        print('Как пользоваться: python3 spending_agent.py "продукты 908"')
        return 2
    entry, source = parse(phrase)
    if entry["amount"] == 0:
        print(f"Не поняла сумму во фразе «{phrase}» - ничего не записала. Попробуйте так: «продукты 908».")
        return 1
    today = dt.date.today()
    day = today - dt.timedelta(days=entry["days_ago"])
    with open(LOG_PATH, encoding="utf-8") as f:
        text = f.read()
    text, row = add_row(text, day, entry)
    with open(LOG_PATH, "w", encoding="utf-8") as f:
        f.write(text)
    left = WEEKLY_LIMIT - week_spent(text, today)
    print(f"Записала ({source}): {day:%d.%m} · {entry['category']} · {format_amount(entry['amount'])} ₽ · {entry['description']}")
    if left >= 0:
        print(f"Остаток недели: {format_amount(left)} из {format_amount(WEEKLY_LIMIT)}")
    else:
        print(f"Лимит недели превышен на {format_amount(-left)} (лимит {format_amount(WEEKLY_LIMIT)})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
