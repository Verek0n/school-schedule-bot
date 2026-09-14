```python
import base64
import hashlib
import json
import os
import re
import subprocess
import time
import zipfile
from pathlib import Path

import pymupdf
import requests
from playwright.sync_api import sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError


# ============================================================
# CONFIG
# ============================================================

SOURCE_URL = (
    "https://disk.yandex.ru/i/"
    "https://docs.yandex.ru/view/d/zQH249qvBK21O7SXl-9z_SPegnqahzm72s0qoIz-cKg6eG1uRGNFdE5adw"
)

BOT_TOKEN = os.environ["BOT_TOKEN"]
STATE_TOKEN = os.environ["STATE_TOKEN"]

# Приватный репозиторий состояния
STATE_REPO = "Verek0n/school-schedule-state"

WORK = Path("work")
WORK.mkdir(exist_ok=True)

STATE_URL = (
    f"https://api.github.com/repos/{STATE_REPO}/contents/state.json"
)

GH_HEADERS = {
    "Authorization": f"Bearer {STATE_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


# ============================================================
# CLASS -> SLIDE
# ============================================================

CLASS_TO_SLIDE = {
    # Слайд 1
    "5А": 1,
    "5Б": 1,
    "5В": 1,
    "5Г": 1,
    "5Д": 1,
    "5Е": 1,
    "5Ж": 1,
    "5З": 1,
    "6А": 1,
    "6Б": 1,

    # Слайд 2
    "6В": 2,
    "6Г": 2,
    "6Д": 2,
    "6Е": 2,
    "6Ж": 2,
    "6З": 2,
    "7А": 2,
    "7Б": 2,
    "7В": 2,
    "7Г": 2,

    # Слайд 3
    "7Д": 3,
    "7Е": 3,
    "7Ж": 3,
    "7И": 3,
    "8А": 3,
    "8Б": 3,
    "8В": 3,
    "8Г": 3,
    "8Д": 3,
    "8Ж": 3,

    # Слайд 4
    "8З": 4,
    "9А": 4,
    "9Б": 4,
    "9В": 4,
    "9Г": 4,
    "9Д": 4,
    "9Е": 4,
    "10А": 4,
    "10Б": 4,
    "11А": 4,

    # Слайд 5
    "11Б": 5,
}


# ============================================================
# TELEGRAM
# ============================================================

TG_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"


def telegram(method, data=None, upload=None):
    """
    Вызов Telegram Bot API.
    upload = путь к файлу для multipart upload.
    """
    url = f"{TG_URL}/{method}"

    for attempt in range(5):
        try:
            files = None

            if upload is not None:
                upload_path = Path(upload)

                files = {
                    "photo": (
                        upload_path.name,
                        upload_path.open("rb"),
                        "image/png",
                    )
                }

            response = requests.post(
                url,
                data=data or {},
                files=files,
                timeout=(15, 90),
            )

            if files:
                files["photo"][1].close()

            if response.status_code == 429:
                try:
                    retry_after = (
                        response.json()
                        .get("parameters", {})
                        .get("retry_after", 5)
                    )
                except Exception:
                    retry_after = 5

                print(
                    f"Telegram rate limit, "
                    f"ждём {retry_after} сек."
                )

                time.sleep(retry_after)
                continue

            response.raise_for_status()

            result = response.json()

            if not result.get("ok"):
                raise RuntimeError(
                    f"Telegram API error: {result}"
                )

            return result["result"]

        except Exception:
            if attempt == 4:
                raise

            time.sleep(2)

    raise RuntimeError("Telegram API failed")


def send_message(chat_id, text, keyboard=None):
    data = {
        "chat_id": chat_id,
        "text": text,
    }

    if keyboard is not None:
        data["reply_markup"] = json.dumps(
            keyboard,
            ensure_ascii=False,
        )

    return telegram("sendMessage", data)


def send_photo(chat_id, photo_path=None, file_id=None, caption=None):
    data = {
        "chat_id": chat_id,
    }

    if caption:
        data["caption"] = caption

    if file_id:
        data["photo"] = file_id
        return telegram("sendPhoto", data)

    if photo_path:
        return telegram(
            "sendPhoto",
            data,
            upload=photo_path,
        )

    raise ValueError("Нужно передать photo_path или file_id")


# ============================================================
# KEYBOARD
# ============================================================

def class_keyboard():
    classes = list(CLASS_TO_SLIDE.keys())

    rows = []

    for i in range(0, len(classes), 5):
        rows.append(classes[i:i + 5])

    return {
        "keyboard": rows,
        "resize_keyboard": True,
        "one_time_keyboard": False,
    }


REMOVE_KEYBOARD = {
    "remove_keyboard": True
}


# ============================================================
# STATE
# ============================================================

def default_state():
    return {
        "offset": 0,
        "users": {},
        "latest": {
            "slides": [
                {
                    "hash": None,
                    "file_id": None,
                }
                for _ in range(5)
            ]
        },
    }


def load_state():
    try:
        response = requests.get(
            STATE_URL,
            headers=GH_HEADERS,
            timeout=30,
        )

        if response.status_code == 404:
            print("state.json ещё не существует.")
            return default_state(), None

        response.raise_for_status()

        payload = response.json()

        content = base64.b64decode(
            payload["content"]
        ).decode("utf-8")

        state = json.loads(content)

        # На случай старого / неполного state.json
        state.setdefault("offset", 0)
        state.setdefault("users", {})
        state.setdefault("latest", {})

        if "slides" not in state["latest"]:
            state["latest"]["slides"] = [
                {
                    "hash": None,
                    "file_id": None,
                }
                for _ in range(5)
            ]

        while len(state["latest"]["slides"]) < 5:
            state["latest"]["slides"].append(
                {
                    "hash": None,
                    "file_id": None,
                }
            )

        return state, payload["sha"]

    except Exception as e:
        print(f"Ошибка загрузки state.json: {e}")
        raise


def save_state(state, sha=None):
    content = json.dumps(
        state,
        ensure_ascii=False,
        indent=2,
    )

    encoded = base64.b64encode(
        content.encode("utf-8")
    ).decode("ascii")

    data = {
        "message": "Update state.json",
        "content": encoded,
        "branch": "main",
    }

    if sha:
        data["sha"] = sha

    response = requests.put(
        STATE_URL,
        headers=GH_HEADERS,
        json=data,
        timeout=30,
    )

    response.raise_for_status()

    result = response.json()

    print("state.json сохранён.")

    return result["content"]["sha"]


# ============================================================
# TELEGRAM UPDATES
# ============================================================

def get_updates(offset):
    return telegram(
        "getUpdates",
        {
            "offset": offset,
            "limit": 100,
            "timeout": 0,
            "allowed_updates": json.dumps(
```
