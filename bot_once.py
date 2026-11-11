"""
ربات اخبار گیم (بدون هوش مصنوعی) برای GitHub Actions.
"""
import html
import json
import logging
import os
import re
import time
import uuid

import feedparser
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("gamebot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_ID"])
CHANNEL_ID = os.environ["CHANNEL_ID"]
MAX_PER_CHECK = int(os.getenv("MAX_PER_CHECK", "10"))
SUMMARY_CHARS = int(os.getenv("SUMMARY_CHARS", "350"))
PENDING_TTL = 3 * 24 * 3600

FEEDS = [u.strip() for u in re.split(r"[\n,]+", os.getenv("FEEDS", "")) if u.strip()]

API = f"https://api.telegram.org/bot{BOT_TOKEN}"
STATE_FILE = "state.json"


def tg(method, **params):
    try:
        r = requests.post(f"{API}/{method}", json=params, timeout=30)
        data = r.json()
    except Exception as e:
        log.error("telegram %s failed: %s", method, e)
        return {"ok": False, "description": str(e)}
    if not data.get("ok"):
        log.warning("telegram %s: %s", method, data.get("description"))
    return data


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            s = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        s = {}
    s.setdefault("offset", 0)
    s.setdefault("seen", [])
    s.setdefault("pending", {})
    return s


def save_state(s):
    s["seen"] = s["seen"][-3000:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=1)


def keyboard(pid):
    return {
        "inline_keyboard": [
            [
                {"text": "✅ تایید و ارسال به کانال", "callback_data": f"ok:{pid}"},
                {"text": "❌ رد", "callback_data": f"no:{pid}"},
            ]
        ]
    }


def clean(text):
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def format_post(item):
    title = clean(item["title"])
    summary = clean(item["summary"])
    if len(summary) > SUMMARY_CHARS:
        summary = summary[:SUMMARY_CHARS].rsplit(" ", 1)[0] + "…"
    parts = [f"🎮 {title}"]
    if summary:
        parts.append(summary)
    parts.append(f"🔗 منبع: {item['link']}")
    return "\n\n".join(parts)[:4000]


def edit(chat_id, message_id, text, markup=None):
    params = dict(
        chat_id=chat_id,
        message_id=message_id,
        text=text[:4000],
        disable_web_page_preview=True,
    )
    if markup:
        params["reply_markup"] = markup
    tg("editMessageText", **params)


def handle_buttons(state):
    res = tg(
        "getUpdates",
        offset=state["offset"],
        timeout=0,
        allowed_updates=["callback_query"],
    )
    for u in res.get("result", []):
        state["offset"] = u["update_id"] + 1
        cq = u.get("callback_query")
        if not cq:
            continue
        tg("answerCallbackQuery", callback_query_id=cq["id"])
        if cq["from"]["id"] != ADMIN_ID:
            continue
        try:
            action, pid = cq["data"].split(":", 1)
        except ValueError:
            continue
        chat_id = cq["message"]["chat"]["id"]
        mid = cq["message"]["message_id"]
        entry = state["pending"].get(pid)
        if not entry:
            edit(chat_id, mid, "این پیش‌نویس دیگر موجود نیست.")
            continue

        if action == "ok":
            r = tg(
                "sendMessage",
                chat_id=CHANNEL_ID,
                text=entry["text"],
                disable_web_page_preview=False,
            )
            if r.get("ok"):
                state["pending"].pop(pid, None)
                edit(chat_id, mid, "✅ به کانال ارسال شد.")
            else:
                edit(
                    chat_id,
                    mid,
                    f"⚠️ ارسال به کانال ناموفق بود: {r.get('description')}\n\n{entry['text']}",
                    keyboard(pid),
                )
        elif action == "no":
            state["pending"].pop(pid, None)
            edit(chat_id, mid, "❌ رد شد.")


def fetch_new_items(seen):
    seen_set = set(seen)
    items = []
    for url in FEEDS:
        try:
            feed = feedparser.parse(url)
        except Exception as e:
            log.warning("feed error %s: %s", url, e)
            continue
        if not feed.entries:
            log.warning("feed has no entries (check the URL): %s", url)
            continue
        for e in feed.entries[:10]:
            link = e.get("link")
            if not link or link in seen_set:
                continue
            seen_set.add(link)
            items.append(
                {
                    "title": e.get("title", ""),
                    "summary": e.get("summary", ""),
                    "link": link,
                }
            )
    return items


def send_drafts(state):
    if not FEEDS:
        log.error("FEEDS is empty: set the FEEDS secret with your RSS URLs")
        return
    items = fetch_new_items(state["seen"])
    sent = 0
    for item in items:
        if sent >= MAX_PER_CHECK:
            break
        text = format_post(item)
        pid = uuid.uuid4().hex[:8]
        r = tg(
            "sendMessage",
            chat_id=ADMIN_ID,
            text=f"📝 پیش‌نویس جدید:\n\n{text}",
            reply_markup=keyboard(pid),
            disable_web_page_preview=True,
        )
        if not r.get("ok"):
            break
        state["pending"][pid] = {"text": text, "item": item, "ts": int(time.time())}
        state["seen"].append(item["link"])
        sent += 1
        time.sleep(1)
    log.info("drafts sent: %d", sent)


def main():
    state = load_state()
    now = int(time.time())
    state["pending"] = {
        k: v for k, v in state["pending"].items() if now - v.get("ts", now) < PENDING_TTL
    }
    handle_buttons(state)
    save_state(state)
    send_drafts(state)
    save_state(state)


if __name__ == "__main__":
    main()
