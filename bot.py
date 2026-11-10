#!/usr/bin/env python3
"""Бот: бере зі сторінки УЗ графік відключень для підгрупи (за замовчуванням 5.1)
і надсилає його в Telegram. Пише при появі нової дати та при зміні вже відомого графіка.

Налаштування через змінні оточення:
  TG_TOKEN       токен бота від @BotFather
  TG_CHAT_ID     id чату/каналу, куди слати
  QUEUE          черга (за замовчуванням 5)
  SUBQUEUE       підчерга (за замовчуванням 1)
  SCHEDULE_URL   адреса сторінки з графіком
  STATE_FILE     файл стану (за замовчуванням state.json)

Запуск:
  python bot.py                      одна перевірка (для cron)
  python bot.py --loop 600           перевіряти кожні 600 с
  python bot.py --file sample.html --dry-run   перевірка парсера без Telegram
"""
import argparse
import json
import os
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

URL = os.getenv(
    "SCHEDULE_URL",
    "https://www.uz.gov.ua/about/activity/electropostachannia/electro_consumers/"
    "temporary_shutdown/grafikiobmezhen/622296/",
)
QUEUE = int(os.getenv("QUEUE", "5"))
SUBQUEUE = int(os.getenv("SUBQUEUE", "1"))
TG_TOKEN = os.getenv("TG_TOKEN")
TG_CHAT_ID = os.getenv("TG_CHAT_ID")
STATE_FILE = Path(os.getenv("STATE_FILE", "state.json"))
try:
    TZ = ZoneInfo("Europe/Kyiv")
except Exception:
    try:
        TZ = ZoneInfo("Europe/Kiev")  # стара назва в застарілих базах часових поясів
    except Exception:
        TZ = None  # крайній випадок: беремо час комп'ютера

MONTHS = {
    "січня": 1, "лютого": 2, "березня": 3, "квітня": 4, "травня": 5, "червня": 6,
    "липня": 7, "серпня": 8, "вересня": 9, "жовтня": 10, "листопада": 11, "грудня": 12,
}
WEEKDAYS = ["понеділок", "вівторок", "середа", "четвер", "пʼятниця", "субота", "неділя"]
DATE_RE = re.compile(r"(\d{1,2})\s+(" + "|".join(MONTHS) + r")", re.I)
TIME_RE = re.compile(r"(\d{1,2})\s*[-–:.]\s*(\d{2})")
QUEUE_RE = re.compile(r"(\d+)\s*\(([\d\s,]+)\)")


# ---------- парсинг ----------

def resolve_date(day: int, month: int, today: date) -> date:
    year = today.year
    if today.month == 12 and month == 1:      # перехід через Новий рік
        year += 1
    elif today.month == 1 and month == 12:
        year -= 1
    return date(year, month, day)


def merge(intervals):
    out = []
    for s, e in sorted(intervals):
        if out and out[-1][1] == s:
            out[-1][1] = e
        else:
            out.append([s, e])
    return out


def parse_table(table):
    """Повертає злиті інтервали відключень (в хвилинах від 00:00) для нашої підгрупи."""
    hours_cells = queues_cells = None
    for tr in table.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        if not cells:
            continue
        label = cells[0].get_text(" ", strip=True).lower()
        if "години" in label:
            hours_cells = cells[1:]
        elif "черги" in label:
            queues_cells = cells[1:]
    if not hours_cells or not queues_cells:
        return None

    off = []
    for h_cell, q_cell in zip(hours_cells, queues_cells):
        times = TIME_RE.findall(h_cell.get_text(" ", strip=True))
        if len(times) < 2:
            continue
        start = int(times[0][0]) * 60 + int(times[0][1])
        end = int(times[1][0]) * 60 + int(times[1][1])
        for q, subs in QUEUE_RE.findall(q_cell.get_text(" ", strip=True)):
            if int(q) == QUEUE and str(SUBQUEUE) in re.findall(r"\d", subs):
                off.append((start, end))
    return merge(off)


def parse_page(html: str, today: date) -> dict:
    """{'2026-11-10': [[0, 360], [570, 990], ...]} — по кожній даті на сторінці."""
    soup = BeautifulSoup(html, "html.parser")
    result = {}
    current = None
    for el in soup.find_all(["p", "table"]):
        if el.name == "p":
            if el.find_parent("table"):
                continue
            text = el.get_text(" ", strip=True)
            m = DATE_RE.search(text)
            if m and "діят" in text.lower():
                current = resolve_date(int(m.group(1)), MONTHS[m.group(2).lower()], today)
        elif el.name == "table" and current:
            intervals = parse_table(el)
            if intervals is not None:
                result[current.isoformat()] = intervals
                current = None
    return result


# ---------- повідомлення ----------

def fmt_time(m: int) -> str:
    return f"{m // 60:02d}:{m % 60:02d}"


def fmt_intervals(intervals) -> list:
    if not intervals:
        return ["✅ Відключень не заплановано, світло є цілу добу"]
    segments = []  # (початок, кінець, чи є світло)
    pos = 0
    for s, e in intervals:
        if s > pos:
            segments.append((pos, s, True))
        segments.append((s, e, False))
        pos = e
    if pos < 1440:
        segments.append((pos, 1440, True))
    lines = [
        f"{'🟢' if on else '🔴'} {fmt_time(s)}–{fmt_time(e)} — {'світло є' if on else 'світла немає'}"
        for s, e, on in segments
    ]
    off_total = sum(e - s for s, e in intervals)
    on_total = 1440 - off_total
    lines.append("")
    lines.append(f"Без світла: {off_total // 60} год {off_total % 60:02d} хв")
    lines.append(f"Зі світлом: {on_total // 60} год {on_total % 60:02d} хв")
    return lines


def build_message(d_iso: str, intervals, old=None) -> str:
    d = date.fromisoformat(d_iso)
    wd = WEEKDAYS[d.weekday()]
    title = "🔄 Графік ЗМІНЕНО" if old is not None else "⚡ Графік"
    lines = [f"{title} для підгрупи {QUEUE}.{SUBQUEUE} на {d:%d.%m.%Y} ({wd})", ""]
    lines += fmt_intervals(intervals)
    if old is not None:
        lines += ["", "Було:"] + fmt_intervals(old)
    return "\n".join(lines)


def send_telegram(text: str):
    if not TG_TOKEN or not TG_CHAT_ID:
        raise RuntimeError("Задайте TG_TOKEN і TG_CHAT_ID")
    # можна вказати кілька отримувачів через кому: 123456789,987654321
    chat_ids = [c for c in re.split(r"[,;\s]+", TG_CHAT_ID) if c]
    ok = 0
    for chat_id in chat_ids:
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                data={"chat_id": chat_id, "text": text},
                timeout=15,
            )
        except requests.RequestException as e:
            # не друкуємо текст помилки повністю: в ньому є адреса з токеном
            print(f"Чат {chat_id}: помилка мережі ({type(e).__name__})", file=sys.stderr)
            continue
        if r.ok:
            ok += 1
        else:
            print(f"Чат {chat_id}: Telegram відповів {r.status_code}: {r.text}", file=sys.stderr)
    if ok == 0:
        raise RuntimeError("Повідомлення не надіслано жодному отримувачу")


# ---------- стан і основний цикл ----------

def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def fetch_html() -> str:
    last_error = None
    for attempt in range(1, 4):  # до 3 спроб: сайт іноді відповідає не з першого разу
        try:
            r = requests.get(
                URL,
                headers={"User-Agent": "Mozilla/5.0 (compatible; uz-schedule-bot/1.0; personal use)"},
                timeout=(20, 40),
            )
            r.raise_for_status()
            break
        except requests.RequestException as e:
            last_error = e
            print(f"Спроба {attempt}/3 не вдалася: {type(e).__name__}", file=sys.stderr)
            if attempt < 3:
                time.sleep(10)
    else:
        raise last_error
    r.encoding = r.apparent_encoding if r.encoding in (None, "ISO-8859-1") else r.encoding
    return r.text


def run_once(html: str, dry_run: bool) -> int:
    today = (datetime.now(TZ) if TZ else datetime.now()).date()
    schedules = parse_page(html, today)
    if not schedules:
        print("Не знайдено жодного графіка — можливо, змінилась розмітка сторінки.", file=sys.stderr)
        return 1

    state = load_state()
    for d_iso, intervals in sorted(schedules.items()):
        old = state.get(d_iso)
        if old is None:
            msg = build_message(d_iso, intervals)
        elif old != intervals:
            msg = build_message(d_iso, intervals, old)
        else:
            continue
        if dry_run:
            print(msg, "\n---")
        else:
            send_telegram(msg)
            print(f"Надіслано: {d_iso}")
            state[d_iso] = intervals
            save_state(state)

    if not dry_run:  # прибираємо старі дати
        keep = {k: v for k, v in state.items() if (today - date.fromisoformat(k)).days <= 7}
        if keep != state:
            save_state(keep)
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", help="взяти HTML з локального файлу замість сайту")
    ap.add_argument("--dry-run", action="store_true", help="друкувати, не слати і не зберігати стан")
    ap.add_argument("--loop", type=int, metavar="SEC", help="повторювати кожні SEC секунд")
    args = ap.parse_args()

    while True:
        try:
            html = Path(args.file).read_text(encoding="utf-8") if args.file else fetch_html()
            code = run_once(html, args.dry_run)
        except Exception as e:  # мережа лягла — не падаємо в режимі циклу
            print(f"Помилка: {e}", file=sys.stderr)
            code = 1
        if not args.loop:
            sys.exit(code)
        time.sleep(args.loop)


if __name__ == "__main__":
    main()
