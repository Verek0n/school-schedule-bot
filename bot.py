import base64
import hashlib
import json
import os
import subprocess
import sys
import time
import zipfile
import re
from pathlib import Path

import pymupdf
import requests
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


# ============================================================
# НАСТРОЙКИ
# ============================================================

SOURCE_URL = (
    "https://docs.yandex.ru/view/d/"
    "zQH249qvBK21O7SXl-9z_SPegnqahzm72s0qoIz-cKg6eG1uRGNFdE5adw"
)

BOT_TOKEN = os.environ["BOT_TOKEN"]

# ВАЖНО:
# state.json хранится только в ЭТОМ private-репозитории.
STATE_REPO = "Verek0n/school-schedule-state"

# Отдельный Fine-grained PAT с доступом к private repo.
STATE_TOKEN = os.environ["STATE_TOKEN"]

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


# ============================================================
# КЛАССЫ -> СЛАЙДЫ
# ============================================================

CLASS_PAGE = {
    # Слайд 1
    "5А": 0,
    "5Б": 0,
    "5В": 0,
    "5Г": 0,
    "5Д": 0,
    "5Е": 0,
    "5Ж": 0,
    "5З": 0,
    "6А": 0,
    "6Б": 0,

    # Слайд 2
    "6В": 1,
    "6Г": 1,
    "6Д": 1,
    "6Е": 1,
    "6Ж": 1,
    "6З": 1,
    "7А": 1,
    "7Б": 1,
    "7В": 1,
    "7Г": 1,

    # Слайд 3
    "7Д": 2,
    "7Е": 2,
    "7Ж": 2,
    "7И": 2,
    "8А": 2,
    "8Б": 2,
    "8В": 2,
    "8Г": 2,
    "8Д": 2,
    "8Ж": 2,

    # Слайд 4
    "8З": 3,
    "9А": 3,
    "9Б": 3,
    "9В": 3,
    "9Г": 3,
    "9Д": 3,
    "9Е": 3,
    "10А": 3,
    "10Б": 3,
    "11А": 3,

    # Слайд 5
    "11Б": 4,
}


CLASS_ROWS = [
    ["5А", "5Б", "5В", "5Г"],
    ["5Д", "5Е", "5Ж", "5З"],
    ["6А", "6Б", "6В", "6Г"],
    ["6Д", "6Е", "6Ж", "6З"],
    ["7А", "7Б", "7В", "7Г"],
    ["7Д", "7Е", "7Ж", "7И"],
    ["8А", "8Б", "8В", "8Г"],
    ["8Д", "8Ж", "8З"],
    ["9А", "9Б", "9В", "9Г"],
    ["9Д", "9Е", "10А", "10Б"],
    ["11А", "11Б"],
]


# ============================================================
# ОШИБКИ
# ============================================================

class SafeError(Exception):
    pass


class TelegramError(SafeError):
    def __init__(self, code):
        self.code = code
        super().__init__(f"Telegram API: ошибка {code}")


# ============================================================
# TELEGRAM
# ============================================================

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
                f"Telegram вернул некорректный ответ: "
                f"HTTP {response.status_code}"
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

            time.sleep(
                min(delay, 60) + 1
            )

            continue

        raise TelegramError(code)

    raise SafeError(
        "Не удалось выполнить запрос Telegram."
    )


def say(chat_id, text):
    return telegram(
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": text,
        },
    )


def safe_say(chat_id, text):
    try:
        say(chat_id, text)
    except Exception as error:
        print(
            f"Не удалось отправить сообщение "
            f"{chat_id}: {type(error).__name__}"
        )


def send_photo(chat_id, photo, caption):
    data = {
        "chat_id": chat_id,
        "caption": caption,
    }

    if isinstance(photo, str):
        data["photo"] = photo

        return telegram(
            "sendPhoto",
            data,
        )

    return telegram(
        "sendPhoto",
        data,
        upload=photo,
    )


# ============================================================
# КЛАВИАТУРА
# ============================================================

def class_keyboard():
    return json.dumps(
        {
            "inline_keyboard": [
                [
                    {
                        "text": cls,
                        "callback_data": f"class:{cls}",
                    }
                    for cls in row
                ]
                for row in CLASS_ROWS
            ]
        },
        ensure_ascii=False,
    )


def show_class_selection(chat_id):
    telegram(
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": "Выберите свой класс:",
            "reply_markup": class_keyboard(),
        },
    )


# ============================================================
# STATE
# ============================================================

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
            f"Чтение состояния из private repo: "
            f"HTTP {response.status_code}"
        )

    result = response.json()

    try:
        raw = base64.b64decode(
            result["content"]
        )

        state = json.loads(
            raw.decode("utf-8")
        )

    except Exception as error:
        raise SafeError(
            "Не удалось прочитать state.json: "
            f"{type(error).__name__}"
        )

    state.setdefault(
        "offset",
        0,
    )

    state.setdefault(
        "subscribers",
        {},
    )

    state.setdefault(
        "latest",
        None,
    )

    for chat_id, subscriber in list(
        state["subscribers"].items()
    ):
        if not isinstance(
            subscriber,
            dict,
        ):
            state["subscribers"][chat_id] = {
                "class": None,
                "sent": None,
                "want_schedule": False,
            }

            continue

        subscriber.setdefault(
            "class",
            None,
        )

        subscriber.setdefault(
            "sent",
            None,
        )

        subscriber.setdefault(
            "want_schedule",
            False,
        )

    if state["latest"]:
        if "slides" not in state["latest"]:
            state["latest"] = None

    return (
        state,
        result["sha"],
    )


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

    if response.status_code not in (
        200,
        201,
    ):
        raise SafeError(
            f"Сохранение state.json в "
            f"{STATE_REPO}: "
            f"HTTP {response.status_code} "
            f"{response.text[:300]}"
        )


# ============================================================
# ПОЛЬЗОВАТЕЛИ
# ============================================================

def ensure_subscriber(state, chat_id):
    subscribers = state["subscribers"]

    if chat_id not in subscribers:
        subscribers[chat_id] = {
            "class": None,
            "sent": None,
            "want_schedule": False,
        }

    subscriber = subscribers[chat_id]

    subscriber.setdefault(
        "class",
        None,
    )

    subscriber.setdefault(
        "sent",
        None,
    )

    subscriber.setdefault(
        "want_schedule",
        False,
    )

    return subscriber


# ============================================================
# TELEGRAM КОМАНДЫ
# ============================================================

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

            # ------------------------------------------------
            # MESSAGE
            # ------------------------------------------------

            message = update.get("message")

            if message:
                chat = message.get(
                    "chat",
                    {},
                )

                if chat.get("type") == "private":
                    chat_id = str(
                        chat["id"]
                    )

                    text = message.get(
                        "text",
                        "",
                    ).strip()

                    command = ""

                    if text:
                        command = (
                            text.split()[0]
                            .split("@")[0]
                            .lower()
                        )

                    # ----------------------------------------
                    # /start
                    # ----------------------------------------

                    if command == "/start":

                        subscriber = ensure_subscriber(
                            state,
                            chat_id,
                        )

                        if subscriber.get("class"):
                            safe_say(
                                chat_id,
                                "Бот уже запущен.\n\n"
                                f'Ваш класс: '
                                f'"{subscriber["class"]}"\n\n'
                                "Изменить класс: /class\n"
                                "Получить расписание: /schedule",
                            )
                        else:
                            show_class_selection(
                                chat_id
                            )

                    # ----------------------------------------
                    # /class
                    # ----------------------------------------

                    elif command == "/class":

                        ensure_subscriber(
                            state,
                            chat_id,
                        )

                        show_class_selection(
                            chat_id
                        )

                    # ----------------------------------------
                    # /schedule
                    # ----------------------------------------

                    elif command == "/schedule":

                        subscriber = ensure_subscriber(
                            state,
                            chat_id,
                        )

                        if not subscriber.get("class"):
                            show_class_selection(
                                chat_id
                            )
                        else:
                            subscriber[
                                "want_schedule"
                            ] = True

                    # ----------------------------------------
                    # /stop
                    # ----------------------------------------

                    elif command == "/stop":

                        state["subscribers"].pop(
                            chat_id,
                            None,
                        )

                        safe_say(
                            chat_id,
                            "Вы отписались от "
                            "автоматической рассылки.\n\n"
                            "Чтобы подписаться снова: "
                            "/start",
                        )

                    # ----------------------------------------
                    # Неизвестная команда
                    # ----------------------------------------

                    elif text.startswith("/"):
                        safe_say(
                            chat_id,
                            "/start — запустить бота\n"
                            "/class — изменить класс\n"
                            "/schedule — получить расписание\n"
                            "/stop — отключить рассылку",
                        )

            # ------------------------------------------------
            # CALLBACK
            # ------------------------------------------------

            callback = update.get(
                "callback_query"
            )

            if callback:
                callback_id = callback.get("id")

                callback_data = callback.get(
                    "data",
                    "",
                )

                callback_message = callback.get(
                    "message",
                    {},
                )

                callback_chat = callback_message.get(
                    "chat",
                    {},
                )

                chat_id = str(
                    callback_chat.get(
                        "id",
                        "",
                    )
                )

                if callback_data.startswith("class:"):

                    selected_class = callback_data.split(
                        ":",
                        1,
                    )[1]

                    if selected_class not in CLASS_PAGE:
                        try:
                            telegram(
                                "answerCallbackQuery",
                                {
                                    "callback_query_id":
                                        callback_id,
                                    "text":
                                        "Неизвестный класс.",
                                    "show_alert":
                                        True,
                                },
                            )
                        except Exception:
                            pass

                    else:

                        subscriber = ensure_subscriber(
                            state,
                            chat_id,
                        )

                        subscriber["class"] = selected_class

                        # При выборе класса обязательно
                        # отправляем его текущее расписание.
                        subscriber[
                            "sent"
                        ] = None

                        subscriber[
                            "want_schedule"
                        ] = True

                        try:
                            telegram(
                                "answerCallbackQuery",
                                {
                                    "callback_query_id":
                                        callback_id,
                                    "text":
                                        f"Выбран класс "
                                        f"{selected_class}",
                                },
                            )
                        except Exception:
                            pass

                        safe_say(
                            chat_id,
                            f'Выбран класс '
                            f'"{selected_class}"',
                        )

            state["offset"] = (
                update["update_id"] + 1
            )

        if len(updates) < 100:
            break


# ============================================================
# СКАЧИВАНИЕ DOCX
# ============================================================

def find_download_control(page):
    patterns = [
        re.compile(
            r"docx|word",
            re.IGNORECASE,
        ),
        re.compile(
            r"скачать|download|"
            r"загрузить на компьютер",
            re.IGNORECASE,
        ),
    ]

    scopes = [page] + list(page.frames)

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
                scope.get_by_title(pattern),
                scope.get_by_label(pattern),
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
                        continue

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

            page.wait_for_timeout(8000)

            for _ in range(8):

                control = find_download_control(
                    page
                )

                if control is None:
                    page.wait_for_timeout(3000)
                    continue

                try:

                    with page.expect_download(
                        timeout=30000
                    ) as event:

                        control.click(
                            timeout=10000
                        )

                    download = event.value

                    if download.failure():
                        raise SafeError(
                            "Браузер сообщил "
                            "об ошибке скачивания."
                        )

                    download.save_as(
                        str(target)
                    )

                    break

                except PlaywrightTimeoutError:
                    page.wait_for_timeout(2000)

            if not target.exists():
                raise SafeError(
                    "Не удалось скачать DOCX."
                )

            if not zipfile.is_zipfile(target):
                raise SafeError(
                    "Скачанный файл "
                    "не является DOCX."
                )

            with zipfile.ZipFile(target) as archive:

                if (
                    "word/document.xml"
                    not in archive.namelist()
                ):
                    raise SafeError(
                        "В скачанном архиве "
                        "нет документа Word."
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


# ============================================================
# РЕНДЕР 5 СЛАЙДОВ
# ============================================================

def make_schedules(docx_path):

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
            "LibreOffice не смог "
            "создать PDF."
        )

    with pymupdf.open(pdf_path) as document:

        if len(document) < 5:
            raise SafeError(
                "В PDF меньше 5 страниц. "
                "Рассылка отменена."
            )

        slides = []

        for page_index in range(5):

            page = document[page_index]

            pixmap = page.get_pixmap(
                matrix=pymupdf.Matrix(
                    2,
                    2,
                ),
                colorspace=pymupdf.csRGB,
                alpha=False,
            )

            png = pixmap.tobytes("png")

            digest = hashlib.sha256(
                png
            ).hexdigest()

            output_path = (
                WORK
                / f"slide-{page_index + 1}.png"
            )

            output_path.write_bytes(png)

            slides.append(
                {
                    "hash": digest,
                    "png": png,
                }
            )

    return slides


# ============================================================
# РАССЫЛКА
# ============================================================

def broadcast(state, slides_png):

    latest = state.get("latest")

    if not latest:
        return

    slides = latest.get(
        "slides",
        [],
    )

    if len(slides) != 5:
        return

    for chat_id, subscriber in list(
        state["subscribers"].items()
    ):

        selected_class = subscriber.get(
            "class"
        )

        if (
            not selected_class
            or selected_class not in CLASS_PAGE
        ):
            continue

        slide_index = CLASS_PAGE[
            selected_class
        ]

        slide = slides[slide_index]

        current_hash = slide["hash"]

        old_sent_hash = subscriber.get(
            "sent"
        )

        want_schedule = subscriber.get(
            "want_schedule",
            False,
        )

        # Автоматическая новая рассылка
        # только если hash изменился.
        is_new_schedule = (
            old_sent_hash is not None
            and old_sent_hash != current_hash
        )

        should_send = (
            want_schedule
            or is_new_schedule
        )

        if not should_send:
            continue

        if is_new_schedule:
            caption = (
                f'Доступно новое расписание '
                f'"{selected_class}"!'
            )
        else:
            caption = (
                f'Расписание '
                f'"{selected_class}"'
            )

        try:

            file_id = slide.get(
                "file_id"
            )

            if file_id:

                send_photo(
                    chat_id,
                    file_id,
                    caption,
                )

            else:

                if (
                    not slides_png
                    or slide_index >= len(slides_png)
                ):
                    continue

                result = send_photo(
                    chat_id,
                    slides_png[slide_index],
                    caption,
                )

                try:
                    slide["file_id"] = (
                        result["photo"][-1]["file_id"]
                    )
                except Exception:
                    pass

            subscriber["sent"] = current_hash
            subscriber["want_schedule"] = False

            time.sleep(0.1)

        except TelegramError as error:

            if error.code == 403:

                state["subscribers"].pop(
                    chat_id,
                    None,
                )

            else:

                print(
                    f"Ошибка отправки "
                    f"{chat_id}: Telegram "
                    f"{error.code}"
                )

        except Exception as error:

            print(
                f"Ошибка отправки "
                f"{chat_id}: "
                f"{type(error).__name__}"
            )


# ============================================================
# MAIN
# ============================================================

def main():

    state, sha = load_state()

    had_error = False
    rendered_slides = None

    # --------------------------------------------------------
    # 1. Получаем команды Telegram
    # --------------------------------------------------------

    try:
        process_commands(state)

    except Exception as error:

        had_error = True

        print(
            "Ошибка получения команд Telegram: "
            f"{type(error).__name__}"
        )

    # --------------------------------------------------------
    # 2. Скачиваем и обрабатываем расписание
    # --------------------------------------------------------

    try:

        docx_path = download_docx()

        rendered_slides = make_schedules(
            docx_path
        )

        old_latest = state.get(
            "latest"
        )

        old_slides = []

        if old_latest:
            old_slides = old_latest.get(
                "slides",
                [],
            )

        new_slides = []

        for index, rendered in enumerate(
            rendered_slides
        ):

            old_slide = (
                old_slides[index]
                if index < len(old_slides)
                else None
            )

            if (
                old_slide
                and old_slide.get("hash")
                == rendered["hash"]
            ):

                new_slides.append(
                    {
                        "hash":
                            rendered["hash"],
                        "file_id":
                            old_slide.get(
                                "file_id"
                            ),
                    }
                )

            else:

                print(
                    f"Слайд {index + 1} изменился."
                )

                new_slides.append(
                    {
                        "hash":
                            rendered["hash"],
                        "file_id":
                            None,
                    }
                )

        state["latest"] = {
            "slides": new_slides,
        }

    except Exception as error:

        had_error = True

        if isinstance(error, SafeError):
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

    # --------------------------------------------------------
    # 3. Рассылаем
    # --------------------------------------------------------

    try:

        if rendered_slides:

            slides_png = [
                item["png"]
                for item in rendered_slides
            ]

            broadcast(
                state,
                slides_png,
            )

    except Exception as error:

        had_error = True

        print(
            "Ошибка рассылки: "
            f"{type(error).__name__}"
        )

    # --------------------------------------------------------
    # 4. Сохраняем state.json
    #    ТОЛЬКО В PRIVATE REPO
    # --------------------------------------------------------

    try:

        save_state(
            state,
            sha,
        )

        print(
            f"Состояние сохранено в "
            f"private repo: {STATE_REPO}"
        )

    except Exception as error:

        had_error = True

        print(
            f"Сохранение состояния: {error}"
        )

    # --------------------------------------------------------
    # 5. Завершение
    # --------------------------------------------------------

    if had_error:
        raise SafeError(
            "Запуск завершён с ошибкой."
        )

    print(
        "Проверка расписания завершена."
    )


if __name__ == "__main__":

    try:
        main()

    except Exception as error:

        print(
            f"Ошибка: {error}"
        )

        sys.exit(1)
