import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import pymupdf
import requests
from playwright.sync_api import (
    sync_playwright,
    TimeoutError as PlaywrightTimeoutError,
)


SOURCE_URL = (
    "https://docs.yandex.ru/view/d/"
    "zQH249qvBK21O7SXl-9z_SPegnqahzm72s0qoIz-cKg6eG1uRGNFdE5adw"
)

BOT_TOKEN = os.environ["BOT_TOKEN"]
STATE_TOKEN = os.environ["STATE_TOKEN"]
STATE_REPO = os.environ["STATE_REPO"]

WORK = Path("work")
WORK.mkdir(exist_ok=True)

STATE_URL = (
    f"https://api.github.com/repos/{STATE_REPO}"
    "/contents/state.json"
)

GH_HEADERS = {
    "Authorization": f"Bearer {STATE_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

TG_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/"


class SafeError(Exception):
    """Ошибка без секретных URL и токенов в сообщении."""


class TelegramError(SafeError):
    def __init__(self, code):
        self.code = code
        super().__init__(f"Telegram API: ошибка {code}")


def telegram(method, data=None, upload=None):
    """Telegram API с ограниченным повтором при превышении лимита."""
    for attempt in range(3):
        files = None
        if upload is not None:
            files = {
                "document": (
                    "raspisanie-10B.png",
                    upload,
                    "image/png",
                )
            }

        response = requests.post(
            TG_URL + method,
            data=data or {},
            files=files,
            timeout=(15, 90),
        )

        try:
            result = response.json()
        except ValueError:
            raise SafeError(
                f"Telegram вернул некорректный ответ: HTTP "
                f"{response.status_code}"
            )

        if result.get("ok"):
            return result["result"]

        code = result.get("error_code", response.status_code)

        if code == 429 and attempt < 2:
            delay = result.get("parameters", {}).get(
                "retry_after", 5
            )
            # Длинное ожидание переносим на следующий запуск.
            if delay <= 60:
                time.sleep(delay + 1)
                continue

        raise TelegramError(code)

    raise SafeError("Не удалось выполнить запрос Telegram")


def say(chat_id, text):
    telegram(
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": text,
        },
    )


def safe_say(chat_id, text):
    # Ошибка ответа на команду не должна отменять подписку/отписку.
    try:
        say(chat_id, text)
    except Exception:
        print("Не удалось отправить ответ на одну из команд.")


def load_state():
    response = requests.get(
        STATE_URL,
        headers=GH_HEADERS,
        timeout=30,
    )

    if response.status_code == 404:
        return {
            "offset": 0,
            "subscribers": {},
            "latest": None,
        }, None

    if response.status_code != 200:
        raise SafeError(
            f"Чтение состояния: HTTP {response.status_code}"
        )

    result = response.json()
    raw = base64.b64decode(result["content"])
    state = json.loads(raw.decode("utf-8"))

    return state, result["sha"]


def save_state(state, sha):
    raw = json.dumps(
        state,
        ensure_ascii=False,
        indent=2,
    ).encode("utf-8")

    payload = {
        "message": "Update bot state",
        "content": base64.b64encode(raw).decode("ascii"),
    }

    if sha:
        payload["sha"] = sha

    response = requests.put(
        STATE_URL,
        headers=GH_HEADERS,
        json=payload,
        timeout=30,
    )

    if response.status_code not in (200, 201):
        raise SafeError(
            f"Сохранение состояния: HTTP {response.status_code}"
        )


def process_commands(state):
    # За один запуск обрабатываем до 1000 накопившихся сообщений.
    for _ in range(10):
        updates = telegram(
            "getUpdates",
            {
                "offset": state["offset"],
                "limit": 100,
                "timeout": 0,
                "allowed_updates": json.dumps(["message"]),
            },
        )

        if not updates:
            break

        for update in updates:
            message = update.get("message", {})
            chat = message.get("chat", {})

            # Работаем только в личной переписке с ботом.
            if chat.get("type") == "private":
                text = message.get("text", "").strip()
                command = (
                    text.split()[0].split("@")[0].lower()
                    if text else ""
                )
                chat_id = str(chat["id"])
                subscribers = state["subscribers"]

                if command == "/start":
                    subscribers[chat_id] = {"sent": None}
                    safe_say(
                        chat_id,
                        "Подписка на расписание 10Б включена\n\n"
                        "Проверки проходят примерно раз в 15 минут, "
                        "возможны задержки\n\n"
                    )

                elif command == "/stop":
                    subscribers.pop(chat_id, None)
                    safe_say(
                        chat_id,
                        "Вы отписались от расписания.\n"
                        "Чтобы подписаться снова: /start",
                    )

                elif command == "/schedule":
                    if chat_id in subscribers:
                        subscribers[chat_id]["sent"] = None
                        safe_say(
                            chat_id,
                            "Запрос принят. Отправлю последнее "
                            "доступное расписание.",
                        )
                    else:
                        safe_say(
                            chat_id,
                            "Сначала подпишитесь командой /start.",
                        )

                elif text:
                    safe_say(
                        chat_id,
                        "/start — подписаться\n"
                        "/schedule — получить расписание\n"
                        "/stop — отписаться",
                    )

            state["offset"] = update["update_id"] + 1

        if len(updates) < 100:
            break


def find_download_control(page):
    """
    Поиск стандартных элементов скачивания.

    Это адаптер интерфейса Яндекса. Если подписи кнопок
    на сайте другие, менять нужно прежде всего эту функцию.
    """
    patterns = [
        re.compile(r"docx|word", re.IGNORECASE),
        re.compile(
            r"скачать|download|загрузить на компьютер",
            re.IGNORECASE,
        ),
    ]

    scopes = [page] + list(page.frames)

    for pattern in patterns:
        for scope in scopes:
            locators = [
                scope.get_by_role("menuitem", name=pattern),
                scope.get_by_role("button", name=pattern),
                scope.get_by_role("link", name=pattern),
                scope.get_by_title(pattern),
                scope.get_by_label(pattern),
            ]

            for locator in locators:
                for index in range(min(locator.count(), 10)):
                    element = locator.nth(index)
                    if element.is_visible() and element.is_enabled():
                        return element

    return None


def download_docx():
    target = WORK / "source.docx"

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(
            accept_downloads=True,
            locale="ru-RU",
            viewport={"width": 1600, "height": 1100},
        )
        page = context.new_page()

        try:
            page.goto(
                SOURCE_URL,
                wait_until="domcontentloaded",
                timeout=90000,
            )
            page.wait_for_timeout(12000)

            # Первое нажатие может открыть меню форматов,
            # второе — непосредственно скачать DOCX.
            for _ in range(4):
                control = find_download_control(page)

                if control is None:
                    page.wait_for_timeout(4000)
                    continue

                try:
                    with page.expect_download(timeout=30000) as event:
                        control.click(timeout=10000)

                    download = event.value
                    if download.failure():
                        raise SafeError(
                            "Браузер сообщил об ошибке скачивания"
                        )

                    download.save_as(str(target))
                    break

                except PlaywrightTimeoutError:
                    # Возможно, открылось меню выбора формата.
                    page.wait_for_timeout(2000)

            if not target.exists():
                raise SafeError(
                    "Не удалось скачать DOCX. "
                    "Нужно проверить кнопки скачивания."
                )

            # Не принимаем HTML ошибки или иной файл за DOCX.
            if not zipfile.is_zipfile(target):
                raise SafeError(
                    "Скачанный файл не является DOCX"
                )

            with zipfile.ZipFile(target) as archive:
                if "word/document.xml" not in archive.namelist():
                    raise SafeError(
                        "В скачанном архиве нет документа Word"
                    )

            return target

        except Exception:
            try:
                page.screenshot(
                    path=str(WORK / "download-error.png"),
                    full_page=True,
                )
            except Exception:
                pass
            raise

        finally:
            browser.close()


def make_schedule(docx_path):
    pdf_path = WORK / "source.pdf"

    # Не используем PDF, оставшийся после неудачной конвертации.
    if pdf_path.exists():
        pdf_path.unlink()

    result = subprocess.run(
        [
            "libreoffice",
            "-env:UserInstallation=file:///tmp/school-lo-profile",
            "--headless",
            "--convert-to",
            "pdf",
            "--outdir",
            str(WORK),
            str(docx_path),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )

    if result.returncode != 0 or not pdf_path.exists():
        raise SafeError("LibreOffice не смог создать PDF")

    with pymupdf.open(pdf_path) as document:
        if len(document) < 4:
            raise SafeError(
                "В PDF меньше четырёх страниц. Рассылка отменена."
            )

        page = document[3]

        pixmap = page.get_pixmap(
            matrix=pymupdf.Matrix(2, 2),
            colorspace=pymupdf.csRGB,
            alpha=False,
        )

        # Сравниваем отрисованную страницу, а не метаданные DOCX/PDF.
        digest = hashlib.sha256()
        digest.update(
            f"{pixmap.width}:{pixmap.height}:".encode("ascii")
        )
        digest.update(pixmap.samples)

        png = pixmap.tobytes("png")

    (WORK / "schedule.png").write_bytes(png)

    return digest.hexdigest(), png


def broadcast(state, png):
    latest = state.get("latest")
    if not latest:
        return

    for chat_id, subscriber in list(
        state["subscribers"].items()
    ):
        if subscriber.get("sent") == latest["hash"]:
            continue

        data = {
            "chat_id": chat_id,
            "caption": (
                "Последняя обнаруженная версия расписания 10Б\n\n"
            ),
        }

        try:
            if latest.get("file_id"):
                data["document"] = latest["file_id"]
                message = telegram("sendDocument", data)
            elif png is not None:
                message = telegram(
                    "sendDocument",
                    data,
                    upload=png,
                )
            else:
                continue

            latest["file_id"] = message["document"]["file_id"]
            subscriber["sent"] = latest["hash"]

            # Небольшая пауза между получателями.
            time.sleep(0.1)

        except TelegramError as error:
            if error.code == 403:
                # Пользователь заблокировал бота.
                state["subscribers"].pop(chat_id, None)
            else:
                print(
                    "Расписание не доставлено одному из "
                    f"подписчиков: код {error.code}. "
                    "Повторим при следующем запуске."
                )

        except Exception:
            print(
                "Временная ошибка отправки. "
                "Повторим при следующем запуске."
            )


def main():
    state, sha = load_state()
    had_error = False
    png = None

    try:
        process_commands(state)
    except Exception:
        had_error = True
        print(
            "Ошибка получения команд Telegram. "
            "Проверьте BOT_TOKEN и отсутствие другого "
            "процесса или webhook у этого бота."
        )

    try:
        docx_path = download_docx()
        version, png = make_schedule(docx_path)

        latest = state.get("latest")

        if not latest or latest["hash"] != version:
            state["latest"] = {
                "hash": version,
                "file_id": None,
            }
            print("Обнаружена новая версия 4-й страницы.")
        else:
            print("4-я страница не изменилась.")

    except Exception as error:
        had_error = True
        # Не печатаем произвольные ошибки библиотек:
        # в них могут оказаться чувствительные URL.
        if isinstance(error, SafeError):
            print(f"Ошибка источника: {error}")
        else:
            print(
                "Ошибка скачивания или преобразования: "
                f"{type(error).__name__}"
            )
        print(
            "Предыдущее расписание сохранено. "
            "Повторим проверку при следующем запуске."
        )

    # При ошибке источника можно повторно выдать ранее
    # загруженный в Telegram файл по команде /schedule.
    broadcast(state, png)
    save_state(state, sha)

    if had_error:
        raise SafeError(
            "Запуск завершён с ошибкой. Состояние сохранено."
        )

    print("Проверка завершена.")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        if isinstance(error, SafeError):
            print(error)
        else:
            print(
                "Ошибка выполнения: "
                f"{type(error).__name__}. "
                "Проверьте секреты и доступ к хранилищу."
            )
        sys.exit(1)
