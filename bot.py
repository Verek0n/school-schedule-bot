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

# ============================================================
# НАСТРОЙКИ
# ============================================================

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

# ============================================================
# КЛАССЫ И СООТВЕТСТВИЕ СЛАЙДАМ
# Номера слайдов здесь начинаются с 1
# ============================================================

CLASS_LETTERS = {
    "5": ["А", "Б", "В", "Г", "Д", "Е", "Ж", "З"],
    "6": ["А", "Б", "В", "Г", "Д", "Е", "Ж", "З"],
    "7": ["А", "Б", "В", "Г", "Д", "Е", "Ж", "И"],
    "8": ["А", "Б", "В", "Г", "Д", "Ж", "З"],
    "9": ["А", "Б", "В", "Г", "Д", "Е"],
    "10": ["А", "Б"],
    "11": ["А", "Б"],
}

CLASS_TO_SLIDE = {
    # Слайд 1
    "5А": 1, "5Б": 1, "5В": 1, "5Г": 1,
    "5Д": 1, "5Е": 1, "5Ж": 1, "5З": 1,
    "6А": 1, "6Б": 1,

    # Слайд 2
    "6В": 2, "6Г": 2, "6Д": 2, "6Е": 2,
    "6Ж": 2, "6З": 2,
    "7А": 2, "7Б": 2, "7В": 2, "7Г": 2,

    # Слайд 3
    "7Д": 3, "7Е": 3, "7Ж": 3, "7И": 3,
    "8А": 3, "8Б": 3, "8В": 3, "8Г": 3,
    "8Д": 3, "8Ж": 3,

    # Слайд 4
    "8З": 4,
    "9А": 4, "9Б": 4, "9В": 4,
    "9Г": 4, "9Д": 4, "9Е": 4,
    "10А": 4, "10Б": 4,
    "11А": 4,

    # Слайд 5
    "11Б": 5,
}

# Тексты намеренно без смайликов и точек в конце
TEXT_CHOOSE_CLASS = "Выберите класс"
TEXT_CHOOSE_LETTER = "Выберите букву"
TEXT_SETTINGS = "Настройки"
TEXT_SUBSCRIBED = "Подписка на расписание включена"
TEXT_UNSUBSCRIBED = "Вы отписались от расписания"
TEXT_NEED_CLASS = "Сначала выберите класс"
TEXT_NO_SCHEDULE = "Расписание пока недоступно"
TEXT_NEW_SCHEDULE = "Доступно новое расписание"

# ============================================================
# ОШИБКИ
# ============================================================

class SafeError(Exception):
    """Ошибка без секретных URL и токенов в сообщении"""


class TelegramError(SafeError):
    def __init__(self, code):
        self.code = code
        super().__init__(f"Telegram API: ошибка {code}")


# ============================================================
# TELEGRAM API
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

            if delay <= 60:
                time.sleep(delay + 1)
                continue

        raise TelegramError(code)

    raise SafeError("Не удалось выполнить запрос Telegram")


def send_text(chat_id, text, reply_markup=None):
    data = {
        "chat_id": chat_id,
        "text": text,
    }

    if reply_markup is not None:
        data["reply_markup"] = json.dumps(
            reply_markup,
            ensure_ascii=False,
        )

    telegram("sendMessage", data)


def edit_text(chat_id, message_id, text, reply_markup=None):
    data = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
    }

    if reply_markup is not None:
        data["reply_markup"] = json.dumps(
            reply_markup,
            ensure_ascii=False,
        )

    try:
        telegram("editMessageText", data)
    except TelegramError:
        pass


def answer_callback(callback_id):
    try:
        telegram(
            "answerCallbackQuery",
            {"callback_query_id": callback_id},
        )
    except Exception:
        pass


# ============================================================
# КНОПКИ
# ============================================================

def class_keyboard():
    buttons = []

    for class_number in CLASS_LETTERS:
        buttons.append(
            {
                "text": class_number,
                "callback_data": f"class:{class_number}",
            }
        )

    return {
        "inline_keyboard": [
            buttons[:4],
            buttons[4:],
        ]
    }


def letter_keyboard(class_number):
    letters = CLASS_LETTERS[class_number]

    rows = []
    current = []

    for letter in letters:
        current.append(
            {
                "text": letter,
                "callback_data": f"letter:{class_number}:{letter}",
            }
        )

        if len(current) == 4:
            rows.append(current)
            current = []

    if current:
        rows.append(current)

    rows.append(
        [
            {
                "text": "Назад",
                "callback_data": "back:classes",
            }
        ]
    )

    return {"inline_keyboard": rows}


def settings_keyboard():
    return {
        "inline_keyboard": [
            [
                {
                    "text": "Изменить класс",
                    "callback_data": "settings:class",
                }
            ],
            [
                {
                    "text": "Получить расписание",
                    "callback_data": "settings:schedule",
                }
            ],
        ]
    }


# ============================================================
# СОСТОЯНИЕ В GITHUB
# ============================================================

def empty_state():
    return {
        "offset": 0,
        "subscribers": {},
        "pending": [],
        "latest": {
            "hashes": {},
            "file_ids": {},
        },
    }


def load_state():
    response = requests.get(
        STATE_URL,
        headers=GH_HEADERS,
        timeout=30,
    )

    if response.status_code == 404:
        return empty_state(), None

    if response.status_code != 200:
        raise SafeError(
            f"Чтение состояния: HTTP {response.status_code}"
        )

    result = response.json()
    raw = base64.b64decode(result["content"])
    state = json.loads(raw.decode("utf-8"))

    # Совместимость со старым state.json
    if "subscribers" not in state:
        state["subscribers"] = {}

    if "pending" not in state:
        state["pending"] = []

    if "offset" not in state:
        state["offset"] = 0

    latest = state.get("latest")

    if not isinstance(latest, dict):
        state["latest"] = {
            "hashes": {},
            "file_ids": {},
        }
    else:
        state["latest"].setdefault("hashes", {})
        state["latest"].setdefault("file_ids", {})

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


# ============================================================
# РАБОТА С ПОЛЬЗОВАТЕЛЯМИ
# ============================================================

def show_classes(chat_id, message_id=None):
    if message_id is None:
        send_text(
            chat_id,
            TEXT_CHOOSE_CLASS,
            class_keyboard(),
        )
    else:
        edit_text(
            chat_id,
            message_id,
            TEXT_CHOOSE_CLASS,
            class_keyboard(),
        )


def show_letters(chat_id, message_id, class_number):
    edit_text(
        chat_id,
        message_id,
        f"{TEXT_CHOOSE_LETTER} {class_number}",
        letter_keyboard(class_number),
    )


def get_class_for_user(state, chat_id):
    subscriber = state["subscribers"].get(chat_id)

    if not subscriber:
        return None

    return subscriber.get("class")


def request_schedule(state, chat_id):
    """Запоминает запрос, чтобы после обновления расписания отправить его"""
    if chat_id not in state["pending"]:
        state["pending"].append(chat_id)


def send_schedule_to_user(state, chat_id, caption=None):
    class_name = get_class_for_user(state, chat_id)

    if not class_name:
        send_text(
            chat_id,
            TEXT_NEED_CLASS,
            class_keyboard(),
        )
        return False

    slide = CLASS_TO_SLIDE.get(class_name)

    if not slide:
        send_text(chat_id, TEXT_NO_SCHEDULE)
        return False

    latest = state.get("latest", {})
    file_ids = latest.get("file_ids", {})
    file_id = file_ids.get(str(slide))

    if not file_id:
        request_schedule(state, chat_id)
        return False

    data = {
        "chat_id": chat_id,
        "photo": file_id,
    }

    if caption:
        data["caption"] = caption

    try:
        telegram("sendPhoto", data)
        return True
    except TelegramError as error:
        if error.code == 403:
            state["subscribers"].pop(chat_id, None)
            return False
        raise



def process_commands(state):
    # Обрабатываем до 1000 накопившихся updates
    for _ in range(10):
        updates = telegram(
            "getUpdates",
            {
                "offset": state["offset"],
                "limit": 100,
                "timeout": 0,
                "allowed_updates": json.dumps(
                    ["message", "callback_query"]
                ),
            },
        )

        if not updates:
            break

        for update in updates:
            message = update.get("message", {})
            callback = update.get("callback_query")

            # Обычные сообщения и команды
            if message:
                chat = message.get("chat", {})

                if chat.get("type") == "private":
                    chat_id = str(chat["id"])
                    text = message.get("text", "").strip()

                    command = (
                        text.split()[0].split("@")[0].lower()
                        if text
                        else ""
                    )

                    if command == "/start":
                        state["subscribers"].setdefault(
                            chat_id,
                            {
                                "class": None,
                                "sent": {},
                            },
                        )

                        subscriber = state["subscribers"][chat_id]

                        if not subscriber.get("class"):
                            show_classes(chat_id)
                        else:
                            request_schedule(state, chat_id)

                    elif command == "/stop":
                        state["subscribers"].pop(chat_id, None)

                        # Отписка без лишнего текста
                        try:
                            send_text(
                                chat_id,
                                TEXT_UNSUBSCRIBED,
                            )
                        except Exception:
                            pass

                    elif command == "/schedule":
                        if chat_id in state["subscribers"]:
                            request_schedule(state, chat_id)
                        else:
                            send_text(
                                chat_id,
                                TEXT_NEED_CLASS,
                                class_keyboard(),
                            )

                    elif command == "/settings":
                        if chat_id not in state["subscribers"]:
                            state["subscribers"][chat_id] = {
                                "class": None,
                                "sent": {},
                            }

                        send_text(
                            chat_id,
                            TEXT_SETTINGS,
                            settings_keyboard(),
                        )

                    # Любой другой текст не обрабатываем

            # Inline-кнопки
            if callback:
                callback_id = callback["id"]
                answer_callback(callback_id)

                callback_message = callback.get("message", {})
                callback_chat = callback_message.get("chat", {})

                if callback_chat.get("type") != "private":
                    state["offset"] = update["update_id"] + 1
                    continue

                chat_id = str(callback_chat["id"])
                message_id = callback_message["message_id"]
                data = callback.get("data", "")

                if data == "back:classes":
                    show_classes(chat_id, message_id)

                elif data.startswith("class:"):
                    class_number = data.split(":", 1)[1]

                    if class_number in CLASS_LETTERS:
                        # Временное сохранение выбранного класса
                        state["subscribers"].setdefault(
                            chat_id,
                            {
                                "class": None,
                                "sent": {},
                            },
                        )

                        show_letters(
                            chat_id,
                            message_id,
                            class_number,
                        )

                elif data.startswith("letter:"):
                    parts = data.split(":")

                    if len(parts) != 3:
                        state["offset"] = update["update_id"] + 1
                        continue

                    class_number = parts[1]
                    letter = parts[2]
                    class_name = f"{class_number}{letter}"

                    if (
                        class_number not in CLASS_LETTERS
                        or letter not in CLASS_LETTERS[class_number]
                        or class_name not in CLASS_TO_SLIDE
                    ):
                        state["offset"] = update["update_id"] + 1
                        continue

                    state["subscribers"].setdefault(
                        chat_id,
                        {
                            "class": None,
                            "sent": {},
                        },
                    )

                    state["subscribers"][chat_id]["class"] = class_name
                    state["subscribers"][chat_id]["sent"] = {}

                    # Выбор класса одновременно включает подписку
                    edit_text(
                        chat_id,
                        message_id,
                        f"Расписание {class_name}",
                    )

                    # Отправка произойдёт после проверки источника
                    request_schedule(state, chat_id)

                elif data == "settings:class":
                    show_classes(chat_id, message_id)

                elif data == "settings:schedule":
                    request_schedule(state, chat_id)

            state["offset"] = update["update_id"] + 1

        if len(updates) < 100:
            break


# ============================================================
# СКАЧИВАНИЕ DOCX ИЗ ЯНДЕКСА
# ============================================================

def find_download_control(page):
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
                    page.wait_for_timeout(2000)

            if not target.exists():
                raise SafeError(
                    "Не удалось скачать DOCX. "
                    "Нужно проверить кнопки скачивания."
                )

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


# ============================================================
# СОЗДАНИЕ 5 КАРТИНОК ИЗ PDF
# ============================================================

def make_schedule(docx_path):
    pdf_path = WORK / "source.pdf"

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

    pages = {}

    with pymupdf.open(pdf_path) as document:
        if len(document) < 5:
            raise SafeError(
                "В PDF меньше пяти страниц"
            )

        for slide_number in range(1, 6):
            page = document[slide_number - 1]

            pixmap = page.get_pixmap(
                matrix=pymupdf.Matrix(2, 2),
                colorspace=pymupdf.csRGB,
                alpha=False,
            )

            digest = hashlib.sha256()
            digest.update(
                f"{pixmap.width}:{pixmap.height}:".encode("ascii")
            )
            digest.update(pixmap.samples)

            png = pixmap.tobytes("png")

            filename = WORK / f"schedule_{slide_number}.png"
            filename.write_bytes(png)

            pages[slide_number] = {
                "hash": digest.hexdigest(),
                "png": png,
            }

    return pages


# ============================================================
# РАССЫЛКА НОВОГО РАСПИСАНИЯ
# ============================================================

def ensure_file_ids(state, pages):
    """
    Загружает изменившиеся или ещё не загруженные слайды в Telegram
    через первого подходящего подписчика

    Telegram file_id после этого сохраняется в GitHub state.json
    """
    latest = state["latest"]
    old_hashes = latest.get("hashes", {})
    old_file_ids = latest.get("file_ids", {})

    changed_slides = []

    for slide_number, page_data in pages.items():
        key = str(slide_number)
        page_hash = page_data["hash"]

        if old_hashes.get(key) != page_hash:
            changed_slides.append(slide_number)

        if old_hashes.get(key) == page_hash and old_file_ids.get(key):
            continue

        latest["file_ids"].pop(key, None)

    # Для каждого слайда нужен хотя бы один чат, куда его можно загрузить
    # Если подписчиков ещё нет, file_id будет создан позже при первом запросе
    candidates = list(state["subscribers"].keys())

    for slide_number in range(1, 6):
        key = str(slide_number)

        if latest["file_ids"].get(key):
            continue

        class_candidates = [
            chat_id
            for chat_id in candidates
            if CLASS_TO_SLIDE.get(
                state["subscribers"].get(chat_id, {}).get("class")
            ) == slide_number
        ]

        if not class_candidates:
            continue

        chat_id = class_candidates[0]

        try:
            message = telegram(
                "sendPhoto",
                {"chat_id": chat_id},
                upload=pages[slide_number]["png"],
            )

            latest["file_ids"][key] = message["photo"][-1]["file_id"]

            # Удаляем пробное сообщение, чтобы пользователь не получил
            # дубликат: основной broadcast/request отправит его отдельно
            try:
                telegram(
                    "deleteMessage",
                    {
                        "chat_id": chat_id,
                        "message_id": message["message_id"],
                    },
                )
            except Exception:
                pass

        except TelegramError as error:
            if error.code == 403:
                state["subscribers"].pop(chat_id, None)
            else:
                print(
                    f"Не удалось загрузить слайд {slide_number}: "
                    f"код {error.code}"
                )

    latest["hashes"] = {
        str(slide_number): pages[slide_number]["hash"]
        for slide_number in pages
    }

    return changed_slides


def send_pending_requests(state):
    """Отправляет расписание пользователям, которые запросили /schedule"""
    pending = list(state.get("pending", []))
    state["pending"] = []

    for chat_id in pending:
        if chat_id not in state["subscribers"]:
            continue

        try:
            sent = send_schedule_to_user(state, chat_id)

            if not sent:
                # Если file_id пока отсутствует, попробуем в следующий запуск
                if chat_id not in state["pending"]:
                    state["pending"].append(chat_id)

        except Exception:
            if chat_id not in state["pending"]:
                state["pending"].append(chat_id)


def broadcast(state, changed_slides):
    if not changed_slides:
        return

    changed = set(changed_slides)

    for chat_id, subscriber in list(
        state["subscribers"].items()
    ):
        class_name = subscriber.get("class")

        if not class_name:
            continue

        slide = CLASS_TO_SLIDE.get(class_name)

        if slide not in changed:
            continue

        file_id = state["latest"]["file_ids"].get(str(slide))

        if not file_id:
            continue

        try:
            telegram(
                "sendPhoto",
                {
                    "chat_id": chat_id,
                    "photo": file_id,
                    "caption": TEXT_NEW_SCHEDULE,
                },
            )

            subscriber.setdefault("sent", {})[str(slide)] = (
                state["latest"]["hashes"][str(slide)]
            )

            time.sleep(0.1)

        except TelegramError as error:
            if error.code == 403:
                state["subscribers"].pop(chat_id, None)
            else:
                print(
                    f"Расписание не доставлено подписчику: "
                    f"код {error.code}"
                )

        except Exception:
            print(
                "Временная ошибка отправки расписания"
            )



# ============================================================
# MAIN
# ============================================================

def main():
    state, sha = load_state()
    had_error = False

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
        pages = make_schedule(docx_path)

        changed_slides = ensure_file_ids(
            state,
            pages,
        )

        if changed_slides:
            print(
                "Изменились слайды: "
                + ", ".join(map(str, changed_slides))
            )
        else:
            print("Расписание не изменилось")

        broadcast(state, changed_slides)
        send_pending_requests(state)

    except Exception as error:
        had_error = True

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

    save_state(state, sha)

    if had_error:
        raise SafeError(
            "Запуск завершён с ошибкой. Состояние сохранено."
        )

    print("Проверка завершена")


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
