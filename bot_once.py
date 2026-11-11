"""
نسخه‌ی GitHub Actions ربات اخبار گیم.
هر بار که اجرا می‌شود: دکمه‌های زده‌شده را پردازش می‌کند، خبر جدید می‌گیرد،
پیش‌نویس‌ها را به پیوی ادمین می‌فرستد و تمام می‌شود. وضعیت در state.json ذخیره می‌شود.
"""
import json
import logging
import os
import time
import uuid

import feedparser
import requests
from anthropic import Anthropic

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("gamebot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_ID"])
CHANNEL_ID = os.environ["CHANNEL_ID"]
MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5")
MAX_PER_CHECK = int(os.getenv("MAX_PER_CHECK", "40"))
PENDING_TTL = 3 * 24 * 3600  # پیش‌نویس‌های تاییدنشده بعد از ۳ روز پاک می‌شوند

API = f"https://api.telegram.org/bot{BOT_TOKEN}"
STATE_FILE = "state.json"

FEEDS = [
    "https://www.gamespot.com/feeds/news/",
    "https://feeds.feedburner.com/ign/games-all",
    "https://www.pcgamer.com/rss/",
    "https://www.eurogamer.net/feed",
]

SYSTEM_PROMPT = (
    "تو ویراستار یک کانال تلگرامی فارسی درباره اخبار بازی‌های ویدیویی هستی. "
    "از خبر داده‌شده یک پست کوتاه، جذاب و روان بنویس. کل پست (عنوان، متن و هشتگ‌ها) باید کاملاً فارسی باشد، حتی اگر خبر انگلیسی است. "
    "قوانین: عنوان گیرا با یک ایموجی مرتبط در خط اول؛ ۳ تا ۵ جمله خلاصه دقیق بدون اضافه‌کردن اطلاعات ساختگی؛ "
    "فقط نام بازی‌ها و شرکت‌ها به انگلیسی بماند؛ در پایان ۲ تا ۴ هشتگ فارسی. "
    "لینک و منبع را خودت ننویس. فقط متن پست را برگردان."
)

client = Anthropic()


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
            [{"text": "✅ تایید و ارسال به کانال", "callback_data": f"ok:{pid}"}],
            [
                {"text": "🔄 بازنویسی", "callback_data": f"re:{pid}"},
                {"text": "❌ رد", "callback_data": f"no:{pid}"},
            ],
        ]
    }


def generate_post(item):
    prompt = f"عنوان: {item['title']}\n\nخلاصه: {item['summary']}"
    resp = client.messages.create(
        model=MODEL,
        max_tokens=800,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    text = resp.content[0].text.strip()
    return f"{text}\n\n🔗 منبع: {item['link']}"[:4000]


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
        elif action == "re":
            try:
                entry["text"] = generate_post(entry["item"])
            except Exception as e:
                edit(chat_id, mid, f"خطا در بازنویسی: {e}", keyboard(pid))
                continue
            edit(chat_id, mid, f"📝 پیش‌نویس بازنویسی‌شده:\n\n{entry['text']}", keyboard(pid))


def fetch_new_items(seen):
    seen_set = set(seen)
    items = []
    for url in FEEDS:
        try:
            feed = feedparser.parse(url)
        except Exception as e:
            log.warning("feed error %s: %s", url, e)
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
    items = fetch_new_items(state["seen"])
    sent = 0
    for item in items:
        if sent >= MAX_PER_CHECK:
            break
        try:
            text = generate_post(item)
        except Exception as e:
            log.error("generation failed: %s", e)
            continue
        pid = uuid.uuid4().hex[:8]
        r = tg(
            "sendMessage",
            chat_id=ADMIN_ID,
            text=f"📝 پیش‌نویس جدید:\n\n{text}",
            reply_markup=keyboard(pid),
            disable_web_page_preview=True,
        )
        if not r.get("ok"):
            # اگر پیام نرسید (مثلاً /start نزده‌ای)، خبر را «دیده‌شده» نکن
            break
        state["pending"][pid] = {"text": text, "item": item, "ts": int(time.time())}
        state["seen"].append(item["link"])
        sent += 1
    log.info("drafts sent: %d", sent)


def main():
    state = load_state()
    now = int(time.time())
    state["pending"] = {
        k: v for k, v in state["pending"].items() if now - v.get("ts", now) < PENDING_TTL
    }
    handle_buttons(state)
    save_state(state)  # دکمه‌ها ذخیره شوند حتی اگر ادامه کار خطا بدهد
    send_drafts(state)
    save_state(state)


if __name__ == "__main__":
    main()
