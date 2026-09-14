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
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


SOURCE_URL = (
    "https://docs.yandex.ru/view/d/"
    "zQH249qvBK21O7SXl-9z_SPegnqahzm72s0qoIz-cKg6eG1uRGNFdE5adw"
)

BOT_TOKEN = os.environ["BOT_TOKEN"]
STATE_TOKEN = os.environ.get("STATE_TOKEN") or os.environ["GITHUB_TOKEN"]
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


CLASS_PAGE = {
    "5А": 1, "5Б": 1, "5В": 1, "5Г": 1,
    "5Д": 1, "5Е": 1, "5Ж": 1, "5З": 1,
    "6А": 1, "6Б": 1,

    "6В": 2, "6Г": 2, "6Д": 2, "6Е": 2,
    "6Ж": 2, "6З": 2,
    "7А": 2, "7Б": 2, "7В": 2, "7Г": 2,

    "7Д": 3, "7Е": 3, "7Ж": 3, "7И": 3,
    "8А": 3, "8Б": 3, "8В": 3, "8Г": 3,
    "8Д": 3, "8Ж": 3,

    "8З": 4,
    "9А": 4, "9Б": 4, "9В": 4,
    "9Г": 4, "9Д": 4, "9Е": 4,
    "10А": 4, "10Б": 4,
    "11А": 4,

    "11Б": 5,
}


CLASS_GROUPS = [
    ["5А", "5Б", "5В", "5Г"],
    ["5Д", "5Е", "5Ж", "5З"],
    ["6А", "6Б", "6В", "6Г"],
    ["6Д", "6Е", "6Ж", "6З"],
    ["7А", "7Б", "7В", "7Г"],
    ["7Д", "7Е", "7Ж", "7И"],
    ["8А", "8Б", "8В", "8Г"],
    ["8Д", "8Ж", "8З", "9А"],
    ["9Б", "9В", "9Г", "9Д"],
    ["9Е", "10А", "10Б", "11А"],
    ["11Б"],
]


class SafeError(Exception):
    pass


class TelegramError(SafeError):
    def __init__(self, code):
        self.code = code
        super().__init__(f"Telegram API: ошибка {code}")


def telegram(method, data=None, upload=None):
    for attempt in range(3):
        files = None

        if upload is not None:
            files = {
                "photo": (
                    "raspisanie.png",
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
                f"Telegram вернул HTTP {response.status_code}"
            )

        if result.get("ok"):
            return result["result"]

        code = result.get(
            "error_code",
            response.status_code,
        )

        if code == 429 and attempt < 2:
            delay = result.get(
                "parameters",
                {},
            ).get(
                "retry_after",
                5,
            )

            if delay <= 60:
                time.sleep(delay + 1)
                continue

        raise TelegramError(code)

    raise SafeError("Не удалось выполнить запрос Telegram")


def safe_say(chat_id, text):
    try:
        telegram(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": text,
            },
        )
    except Exception:
        print(
            f"Не удалось отправить сообщение {chat_id}"
        )


def answer_callback(callback_id):
    try:
        telegram(
            "answerCallbackQuery",
            {
                "callback_query_id": callback_id,
            },
        )
    except Exception:
        pass


def default_state():
    return {
        "offset": 0,
        "subscribers": {},
        "latest": {
            "slides": {}
        },
    }


def load_state():
    response = requests.get(
        STATE_URL,
        headers=GH_HEADERS,
        timeout=30,
    )

    if response.status_code == 404:
        return default_state(), None

    if response.status_code != 200:
        raise SafeError(
            f"Чтение состояния: HTTP {response.status_code}"
        )

    result = response.json()

    raw = base64.b64decode(
        result["content"]
    )

    state = json.loads(
        raw.decode("utf-8")
    )

    state.setdefault("offset", 0)
    state.setdefault("subscribers", {})
    state.setdefault(
        "latest",
        {"slides": {}},
    )

    state["latest"].setdefault(
        "slides",
        {},
    )

    for subscriber in state["subscribers"].values():
        subscriber.setdefault(
            "class",
            None,
        )
        subscriber.setdefault(
            "sent",
            None,
        )

    return state, result["sha"]


def save_state(state, sha):
    raw = json.dumps(
        state,
        ensure_ascii=False,
        indent=2,
    ).encode("utf-8")

    payload = {
        "message": "Update bot state",
        "content": base64.b64encode(
            raw
        ).decode("ascii"),
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


def class_keyboard():
    return {
        "inline_keyboard": [
            [
                {
                    "text": class_name,
                    "callback_data": f"class:{class_name}",
                }
                for class_name in group
            ]
            for group in CLASS_GROUPS
        ]
    }


def send_class_selection(chat_id, text):
    telegram(
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": text,
            "reply_markup": json.dumps(
                class_keyboard(),
                ensure_ascii=False,
            ),
        },
    )


def send_latest_to_user(
    state,
    chat_id,
):
    subscriber = state["subscribers"].get(
        chat_id
    )

    if not subscriber:
        safe_say(
            chat_id,
            "Сначала выберите класс командой /start.",
        )
        return

    class_name = subscriber.get("class")

    if not class_name:
        send_class_selection(
            chat_id,
            "Сначала выберите свой класс:",
        )
        return

    page = CLASS_PAGE[class_name]

    slide = state["latest"]["slides"].get(
        str(page)
    )

    if not slide or not slide.get("file_id"):
        safe_say(
            chat_id,
            "Расписание ещё не загружено. "
            "Попробуйте немного позже.",
        )
        return

    try:
        telegram(
            "sendPhoto",
            {
                "chat_id": chat_id,
                "photo": slide["file_id"],
                "caption": f"Расписание {class_name}",
            },
        )

        subscriber["sent"] = slide["hash"]

    except TelegramError as error:
        if error.code == 403:
            state["subscribers"].pop(
                chat_id,
                None,
            )
        else:
            raise


def process_commands(state):
    for _ in range(10):
        updates = telegram(
            "getUpdates",
            {
                "offset": state["offset"],
                "limit": 100,
                "timeout": 0,
                "allowed_updates": json.dumps(
                    [
                        "message",
                        "callback_query",
                    ]
                ),
            },
        )

        if not updates:
            break

        for update in updates:
            try:
                process_update(
                    state,
                    update,
                )
            except Exception as error:
                print(
                    "Ошибка обработки update: "
                    f"{type(error).__name__}"
                )

            state["offset"] = (
                update["update_id"] + 1
            )

        if len(updates) < 100:
            break


def process_update(state, update):
    callback = update.get(
        "callback_query"
    )

    if callback:
        answer_callback(
            callback["id"]
        )

        data = callback.get(
            "data",
            "",
        )

        message = callback.get(
            "message",
            {},
        )

        chat = message.get(
            "chat",
            {},
        )

        if chat.get("type") != "private":
            return

        chat_id = str(
            chat["id"]
        )

        if not data.startswith("class:"):
            return

        class_name = data.split(
            ":",
            1,
        )[1]

        if class_name not in CLASS_PAGE:
            safe_say(
                chat_id,
                "Неизвестный класс.",
            )
            return

        state["subscribers"][chat_id] = {
            "class": class_name,
            "sent": None,
        }

        safe_say(
            chat_id,
            (
                f"Выбран класс: {class_name}\n\n"
                "/schedule — получить расписание\n"
                "/class — изменить класс\n"
                "/stop — отключить рассылку"
            ),
        )

        send_latest_to_user(
            state,
            chat_id,
        )

        return

    message = update.get(
        "message",
        {},
    )

    chat = message.get(
        "chat",
        {},
    )

    if chat.get("type") != "private":
        return

    text = message.get(
        "text",
        "",
    ).strip()

    if not text:
        return

    chat_id = str(
        chat["id"]
    )

    command = (
        text.split()[0]
        .split("@")[0]
        .lower()
    )

    if command == "/start":
        if chat_id not in state["subscribers"]:
            state["subscribers"][chat_id] = {
                "class": None,
                "sent": None,
            }

        if not state["subscribers"][chat_id].get(
            "class"
        ):
            send_class_selection(
                chat_id,
                "Выберите свой класс:",
            )
        else:
            safe_say(
                chat_id,
                (
                    f"Ваш класс: "
                    f"{state['subscribers'][chat_id]['class']}\n\n"
                    "/schedule — получить расписание\n"
                    "/class — изменить класс\n"
                    "/stop — отключить рассылку"
                ),
            )

            send_latest_to_user(
                state,
                chat_id,
            )

    elif command == "/class":
        if chat_id not in state["subscribers"]:
            state["subscribers"][chat_id] = {
                "class": None,
                "sent": None,
            }

        send_class_selection(
            chat_id,
            "Выберите новый класс:",
        )

    elif command == "/schedule":
        send_latest_to_user(
            state,
            chat_id,
        )

    elif command == "/stop":
        state["subscribers"].pop(
            chat_id,
            None,
        )

        safe_say(
            chat_id,
            (
                "Рассылка отключена.\n"
                "Чтобы подключиться снова: /start"
            ),
        )

    else:
        safe_say(
            chat_id,
            (
                "/start — выбрать класс\n"
                "/schedule — получить расписание\n"
                "/class — изменить класс\n"
                "/stop — отключить рассылку"
            ),
        )


def find_download_control(page):
    patterns = [
        re.compile(
            r"docx|word",
            re.IGNORECASE,
        ),
        re.compile(
            r"скачать|download|загрузить на компьютер",
            re.IGNORECASE,
        ),
    ]

    scopes = [
        page
    ] + list(page.frames)

    for pattern in patterns:
        for scope in scopes:
            locators = [
                scope.get_by_role(
                    "menuitem",
                    name=pattern,
                ),
                scope.get_by_role(
                    "button",
                    name=pattern,
                ),
                scope.get_by_role(
                    "link",
                    name=pattern,
                ),
                scope.get_by_title(
                    pattern,
                ),
                scope.get_by_label(
                    pattern,
                ),
            ]

            for locator in locators:
                try:
                    count = min(
                        locator.count(),
                        10,
                    )
                except Exception:
                    continue

                for index in range(count):
                    element = locator.nth(index)

                    try:
                        if (
                            element.is_visible()
                            and element.is_enabled()
                        ):
                            return element
                    except Exception:
                        pass

    return None


def download_docx():
    target = WORK / "source.docx"

    if target.exists():
        target.unlink()

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True
        )

        context = browser.new_context(
            accept_downloads=True,
            locale="ru-RU",
            viewport={
                "width": 1600,
                "height": 1100,
            },
        )

        page = context.new_page()

        try:
            page.goto(
                SOURCE_URL,
                wait_until="domcontentloaded",
                timeout=90000,
            )

            # Не ждём фиксированные 12 секунд.
            # Ищем кнопку скачивания с интервалом 1 сек.
            for _ in range(12):
                control = find_download_control(
                    page
                )

                if control is None:
                    page.wait_for_timeout(1000)
                    continue

                try:
                    with page.expect_download(
                        timeout=10000
                    ) as event:
                        control.click(
                            timeout=5000
                        )

                    download = event.value

                    if download.failure():
                        raise SafeError(
                            "Ошибка скачивания DOCX."
                        )

                    download.save_as(
                        str(target)
                    )

                    break

                except PlaywrightTimeoutError:
                    page.wait_for_timeout(1000)

            if not target.exists():
                raise SafeError(
                    "Не удалось скачать DOCX."
                )

            if not zipfile.is_zipfile(
                target
            ):
                raise SafeError(
                    "Скачанный файл не является DOCX."
                )

            with zipfile.ZipFile(
                target
            ) as archive:
                if "word/document.xml" not in archive.namelist():
                    raise SafeError(
                        "В DOCX отсутствует document.xml."
                    )

            return target

        except Exception:
            try:
                page.screenshot(
                    path=str(
                        WORK / "download-error.png"
                    ),
                    full_page=True,
                )
            except Exception:
                pass

            raise

        finally:
            browser.close()


def render_slides(docx_path):
    pdf_path = WORK / "source.pdf"

    if pdf_path.exists():
        pdf_path.unlink()

    result = subprocess.run(
        [
            "libreoffice",
            "-env:UserInstallation="
            "file:///tmp/school-lo-profile",
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

    if (
        result.returncode != 0
        or not pdf_path.exists()
    ):
        raise SafeError(
            "LibreOffice не смог создать PDF."
        )

    slides = {}

    with pymupdf.open(
        pdf_path
    ) as document:
        if len(document) < 5:
            raise SafeError(
                f"В PDF только {len(document)} страниц, "
                "а нужно минимум 5."
            )

        for page_number in range(1, 6):
            page = document[
                page_number - 1
            ]

            pixmap = page.get_pixmap(
                matrix=pymupdf.Matrix(
                    2,
                    2,
                ),
                colorspace=pymupdf.csRGB,
                alpha=False,
            )

            digest = hashlib.sha256()

            digest.update(
                (
                    f"{pixmap.width}:"
                    f"{pixmap.height}:"
                ).encode("ascii")
            )

            digest.update(
                pixmap.samples
            )

            png = pixmap.tobytes(
                "png"
            )

            slides[
                str(page_number)
            ] = {
                "hash": digest.hexdigest(),
                "png": png,
            }

            (
                WORK / f"slide-{page_number}.png"
            ).write_bytes(png)

    return slides


def broadcast(state, slides):
    latest = state["latest"]["slides"]

    for chat_id, subscriber in list(
        state["subscribers"].items()
    ):
        class_name = subscriber.get(
            "class"
        )

        if not class_name:
            continue

        page_number = CLASS_PAGE.get(
            class_name
        )

        if not page_number:
            continue

        page_key = str(
            page_number
        )

        slide = slides.get(
            page_key
        )

        stored = latest.get(
            page_key
        )

        if not slide or not stored:
            continue

        # Пользователь уже получил эту версию.
        if (
            subscriber.get("sent")
            == stored["hash"]
        ):
            continue

        try:
            file_id = stored.get(
                "file_id"
            )

            if file_id:
                message = telegram(
                    "sendPhoto",
                    {
                        "chat_id": chat_id,
                        "photo": file_id,
                        "caption": (
                            f"Новое расписание "
                            f"{class_name}"
                        ),
                    },
                )

            else:
                message = telegram(
                    "sendPhoto",
                    {
                        "chat_id": chat_id,
                        "caption": (
                            f"Новое расписание "
                            f"{class_name}"
                        ),
                    },
                    upload=slide["png"],
                )

                file_id = message[
                    "photo"
                ][-1]["file_id"]

                stored["file_id"] = file_id

            subscriber["sent"] = stored["hash"]

            time.sleep(0.05)

        except TelegramError as error:
            if error.code == 403:
                state["subscribers"].pop(
                    chat_id,
                    None,
                )
            else:
                print(
                    f"Telegram error {error.code} "
                    f"для {chat_id}"
                )

        except Exception as error:
            print(
                "Ошибка отправки: "
                f"{type(error).__name__}"
            )


def main():
    state, sha = load_state()

    had_error = False
    slides = {}

    # Сначала команды.
    try:
        process_commands(
            state
        )
    except Exception as error:
        had_error = True
        print(
            "Ошибка Telegram: "
            f"{type(error).__name__}"
        )

    # Затем обновление расписания.
    try:
        docx_path = download_docx()

        slides = render_slides(
            docx_path
        )

        latest = state[
            "latest"
        ]["slides"]

        for page_number, slide in slides.items():
            old = latest.get(
                page_number
            )

            if (
                not old
                or old.get("hash")
                != slide["hash"]
            ):
                latest[
                    page_number
                ] = {
                    "hash": slide["hash"],
                    "file_id": None,
                }

                print(
                    f"Слайд {page_number} изменился."
                )
            else:
                print(
                    f"Слайд {page_number} "
                    "не изменился."
                )

    except Exception as error:
        had_error = True

        if isinstance(
            error,
            SafeError,
        ):
            print(
                f"Ошибка расписания: {error}"
            )
        else:
            print(
                "Ошибка обработки расписания: "
                f"{type(error).__name__}"
            )

        print(
            "Старое расписание сохранено."
        )

    # Рассылка только если расписание
    # удалось получить в этом запуске.
    if slides:
        broadcast(
            state,
            slides,
        )

    save_state(
        state,
        sha,
    )

    if had_error:
        raise SafeError(
            "Запуск завершён с ошибкой. "
            "Состояние сохранено."
        )

    print(
        "Проверка завершена."
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(
            error
        )
        sys.exit(1)
