#!/usr/bin/env python3
"""Облачный публикатор GigAutopilot: запускается GitHub Actions по расписанию, Mac не нужен.

Публикует из cloud/queue.json всё, чьё время (`at`, UTC) наступило. Только стандартная библиотека.
Логи и очередь этого репозитория публичные: ключи и токены сюда не попадают никогда.
Перед публикацией статус `publishing` уходит в репозиторий: при сбое пост не повторится (лучше пропуск, чем дубль).

Исходник: tools/social/cloud/publish.py в папке компании.
"""
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

IG = "https://graph.facebook.com/v23.0"
TH = "https://graph.threads.net/v1.0"
QUEUE = Path("cloud/queue.json")
MAX_PER_RUN = 3
IG_TOKEN = os.environ.get("IG_TOKEN", "")
IG_USER_ID = os.environ.get("IG_USER_ID", "")
TH_TOKEN = os.environ.get("THREADS_TOKEN", "")


class Fail(Exception):
    pass


def clean(text):
    for secret in (IG_TOKEN, TH_TOKEN):
        if secret:
            text = text.replace(secret, "***")
    return text


def call(method, url, params=None, data=None):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, method=method)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        where = urllib.parse.urlsplit(url)
        raise Fail(clean(f"{method} {where.netloc}{where.path} -> HTTP {e.code}: {e.read().decode()[:400]}"))


def ig_wait(container):
    for _ in range(90):
        st = call("GET", f"{IG}/{container}", {"fields": "status_code,status", "access_token": IG_TOKEN})
        if st.get("status_code") == "FINISHED":
            return
        if st.get("status_code") in ("ERROR", "EXPIRED"):
            raise Fail(f"Instagram rejected the file: {st}")
        time.sleep(5)
    raise Fail("Instagram is processing the video for more than 7 minutes")


def ig_publish(container, first_comment):
    ig_wait(container)
    r = call("POST", f"{IG}/{IG_USER_ID}/media_publish", data={"creation_id": container, "access_token": IG_TOKEN})
    media = call("GET", f"{IG}/{r['id']}", {"fields": "id,permalink", "access_token": IG_TOKEN})
    if first_comment:
        try:
            call("POST", f"{IG}/{r['id']}/comments", data={"message": first_comment, "access_token": IG_TOKEN})
        except Fail as err:                    # пост уже вышел: комментарий не повод считать публикацию ошибкой
            print("first comment failed:", clean(str(err))[:200])
    return media["permalink"]


def ig_reel(it):
    c = call("POST", f"{IG}/{IG_USER_ID}/media", data={
        "media_type": "REELS", "video_url": it["video"], "caption": it["text"], "share_to_feed": "true",
        "access_token": IG_TOKEN})
    return ig_publish(c["id"], it.get("first_comment"))


def ig_carousel(it):
    children = [call("POST", f"{IG}/{IG_USER_ID}/media", data={
        "image_url": url, "is_carousel_item": "true", "access_token": IG_TOKEN})["id"] for url in it["images"]]
    for c in children:
        ig_wait(c)
    c = call("POST", f"{IG}/{IG_USER_ID}/media", data={
        "media_type": "CAROUSEL", "children": ",".join(children), "caption": it["text"], "access_token": IG_TOKEN})
    return ig_publish(c["id"], it.get("first_comment"))


def threads_post(it):
    c = call("POST", f"{TH}/me/threads", data={"media_type": "TEXT", "text": it["text"], "access_token": TH_TOKEN})
    for _ in range(60):
        st = call("GET", f"{TH}/{c['id']}", {"fields": "status,error_message", "access_token": TH_TOKEN})
        if st.get("status") in (None, "FINISHED"):
            break
        if st.get("status") == "ERROR":
            raise Fail(f"Threads rejected the post: {st.get('error_message')}")
        time.sleep(5)
    r = call("POST", f"{TH}/me/threads_publish", data={"creation_id": c["id"], "access_token": TH_TOKEN})
    return call("GET", f"{TH}/{r['id']}", {"fields": "permalink", "access_token": TH_TOKEN})["permalink"]


KINDS = {"ig_reel": ig_reel, "ig_carousel": ig_carousel, "threads_post": threads_post}


def save(items, message):
    """Сохраняет очередь в репозиторий; False, если не удалось (тогда публиковать нельзя)."""
    QUEUE.write_text(json.dumps(items, indent=1, ensure_ascii=False))
    git = lambda *a: subprocess.run(["git", *a], capture_output=True, text=True)
    git("add", str(QUEUE))
    if git("commit", "-m", message).returncode:
        return False
    for _ in range(4):
        if git("push").returncode == 0:
            return True
        git("pull", "--rebase")
        time.sleep(3)
    return False


def check():
    """Проверка без публикации: ключи работают из облака и что стоит на очереди."""
    me = call("GET", f"{IG}/{IG_USER_ID}", {"fields": "username,followers_count", "access_token": IG_TOKEN})
    print("instagram ok:", me.get("username"), "followers", me.get("followers_count"))
    print("threads key length:", len(TH_TOKEN), "fingerprint:", hashlib.sha256(TH_TOKEN.encode()).hexdigest()[:8])
    me = call("GET", f"{TH}/me", {"fields": "username", "access_token": TH_TOKEN})
    print("threads ok:", me.get("username"))
    items = json.loads(QUEUE.read_text())
    print("queue:", {s: sum(i["status"] == s for i in items) for s in {i["status"] for i in items}})


def main():
    if os.environ.get("DRY_RUN") == "true":
        return check()
    items = json.loads(QUEUE.read_text())
    now = datetime.now(timezone.utc)
    due = [i for i in items if i["status"] == "pending"
           and datetime.fromisoformat(i["at"].replace("Z", "+00:00")) <= now][:MAX_PER_RUN]
    print("due:", [i["id"] for i in due])
    for it in due:
        it["status"] = "publishing"
        if not save(items, f"publishing {it['id']}"):
            print("could not save state, stopping:", it["id"])
            sys.exit(1)
        try:
            it.update(status="done", result=KINDS[it["kind"]](it), done_at=datetime.now(timezone.utc).isoformat())
        except Exception as err:               # обрыв сети: пост мог выйти, поэтому без автоповтора
            it.update(status="error", error=clean(str(err))[:300])
        print(it["id"], it["status"], it.get("result") or it.get("error"))
        save(items, f"{it['status']} {it['id']}")


if __name__ == "__main__":
    main()
