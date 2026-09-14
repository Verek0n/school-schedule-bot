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


# ============================================================
# CONFIG
# ============================================================

SOURCE_URL = (
    "https://docs.yandex.ru/view/d/"
    "zQH249qvBK21O7SXl-9z_SPegnqahzm72s0qoIz-cKg6eG1uRGNFdE5adw"
)

BOT_TOKEN = os.environ["BOT_TOKEN"]
STATE_TOKEN = os.environ["STATE_TOKEN"]

STATE_REPO = "Verek0n/school-schedule-state"

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
# CLASS -> SLIDE
# ============================================================

CLASS_TO_SLIDE = {
    "5А": 1, "5Б": 1, "5В": 1, "5Г": 1, "5Д": 1,
    "5Е": 1, "5Ж": 1, "5З": 1, "6А": 1, "6Б": 1,

    "6В": 2, "6Г": 2, "6Д": 2, "6Е": 2, "6Ж": 2,
    "6З": 2, "7А": 2, "7Б": 2, "7В": 2, "7Г": 2,

    "7Д": 3, "7Е": 3, "7Ж": 3, "7И": 3, "8А": 3,
    "8Б": 3, "8В": 3, "8Г": 3, "8Д": 3, "8Ж": 3,

    "8З": 4, "9А": 4, "9Б": 4, "9В": 4, "9Г": 4,
    "9Д": 4, "9Е": 4, "10А": 4, "10Б": 4, "11А": 4,

    "11Б": 5,
}


# ============================================================
# ERRORS
# ============================================================

class SafeError(Exception):
    pass


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
            if hasattr(upload, "read"):
                files = {
                    "photo": (
                        "raspisanie.png",
                        upload,
                        "image/png",
                    )
                }
            else:
                path = Path(upload)
                files = {
                    "photo": (
                        path.name,
                        path.open("rb"),
                        "image/png",
                    )
                }

        try:
            response = requests.post(
                TG_URL + method,
                data=data or {},
                files=files,
                timeout=(15, 90),
            )

            result = response.json()

        except ValueError:
            raise SafeError(
                f"Telegram вернул некорректный ответ: HTTP "
                f"{response.status_code}"
            )
        finally:
            if files and not hasattr(upload, "read"):
                try:
                    files["photo"][1].close()
                except Exception:
                    pass

        if result.get("ok"):
            return result["result"]

        code = result.get(
            "error_code",
            response.status_code,
        )

        if code == 429 and attempt < 2:
            delay = result.get(
                "parameters", {}
            ).get("retry_after", 5)

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
    try:
        say(chat_id, text)
    except Exception:
        print(
            "Не удалось отправить ответ "
            "на одну из команд."
        )


# ============================================================
# INLINE CLASS BUTTONS
# ============================================================

def class_inline_keyboard():
    classes = list(CLASS_TO_SLIDE.keys())

    rows = []

    for i in range(0, len(classes), 5):
        row = []

        for class_name in classes[i:i + 5]:
            row.append({
                "text": class_name,
                "callback_data": f"class:{class_name}",
            })

        rows.append(row)

    return {
        "inline_keyboard": rows
    }


def send_class_menu(chat_id):
    telegram(
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": "Выберите свой класс:",
            "reply_markup": json.dumps(
                class_inline_keyboard(),
                ensure_ascii=False,
            ),
        },
    )


def answer_callback(callback_query_id):
    try:
        telegram(
            "answerCallbackQuery",
            {
                "callback_query_id": callback_query_id,
            },
        )
    except Exception:
        pass


# ============================================================
# STATE
# ============================================================

def default_state():
    return {
        "offset": 0,
        "subscribers": {},
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


def normalize_state(state):
    state.setdefault("offset", 0)
    state.setdefault("subscribers", {})
    state.setdefault("latest", {})

    slides = state["latest"].get("slides")

    if not isinstance(slides, list):
        slides = []

    while len(slides) < 5:
        slides.append({
            "hash": None,
            "file_id": None,
        })

    state["latest"]["slides"] = slides[:5]

    return state


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
            f"Чтение состояния: HTTP "
            f"{response.status_code}"
        )

    result = response.json()

    raw = base64.b64decode(
        result["content"]
    )

    state = json.loads(
        raw.decode("utf-8")
    )

    return normalize_state(state), result["sha"]


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
            f"Сохранение состояния: HTTP "
            f"{response.status_code}"
        )

    print("state.json сохранён.")


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
                ["message", "callback_query"]
            ),
        },
    )


def ensure_subscriber(state, chat_id):
    chat_id = str(chat_id)

    if chat_id not in state["subscribers"]:
        state["subscribers"][chat_id] = {
            "class": None,
            "sent": None,
            "want_schedule": False,
        }

    return state["subscribers"][chat_id]


def process_commands(state):
    changed = False

    for _ in range(10):
        updates = get_updates(
            state["offset"]
        )

        if not updates:
            break

        for update in updates:
            state["offset"] = (
                update["update_id"] + 1
            )

            # ------------------------------------------------
            # INLINE BUTTON
            # ------------------------------------------------

            callback = update.get(
                "callback_query"
            )

            if callback:
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

                if (
                    data.startswith("class:")
                    and chat.get("type") == "private"
                ):
                    chat_id = str(chat["id"])
                    selected_class = data[
                        len("class:"):
                    ]

                    if selected_class in CLASS_TO_SLIDE:
                        user = ensure_subscriber(
                            state,
                            chat_id,
                        )

                        user["class"] = (
                            selected_class
                        )
                        user["sent"] = None
                        user["want_schedule"] = True

                        changed = True

                        answer_callback(
                            callback["id"]
                        )

                        safe_say(
                            chat_id,
                            (
                                f"Класс выбран: "
                                f"«{selected_class}».\n"
                                "Расписание будет отправлено "
                                "при ближайшем запуске."
                            ),
                        )

                continue

            # ------------------------------------------------
            # MESSAGE
            # ------------------------------------------------

            message = update.get("message")

            if not message:
                continue

            chat = message.get("chat", {})

            if chat.get("type") != "private":
                continue

            chat_id = str(chat["id"])

            text = (
                message.get("text") or ""
            ).strip()

            if not text:
                continue

            print(
                f"Получено сообщение "
                f"от {chat_id}: {text}"
            )

            # ------------------------------------------------
            # /start
            # ------------------------------------------------

            if text == "/start":
                user = ensure_subscriber(
                    state,
                    chat_id,
                )

                changed = True

                if user.get("class"):
                    safe_say(
                        chat_id,
                        (
                            f"Текущий класс: "
                            f"«{user['class']}»."
                        ),
                    )

                send_class_menu(chat_id)
                continue

            # ------------------------------------------------
            # /class
            # ------------------------------------------------

            if text == "/class":
                ensure_subscriber(
                    state,
                    chat_id,
                )

                send_class_menu(chat_id)
                continue

            # ------------------------------------------------
            # /schedule
            # ------------------------------------------------

            if text == "/schedule":
                user = state["subscribers"].get(
                    chat_id
                )

                if not user:
                    safe_say(
                        chat_id,
                        "Сначала нажмите /start.",
                    )
                elif not user.get("class"):
                    send_class_menu(chat_id)
                else:
                    user["want_schedule"] = True
                    user["sent"] = None
                    changed = True

                    safe_say(
                        chat_id,
                        (
                            "Запрос принят. "
                            "Отправлю расписание "
                            "при ближайшем запуске."
                        ),
                    )

                continue

            # ------------------------------------------------
            # /stop
            # ------------------------------------------------

            if text == "/stop":
                if chat_id in state["subscribers"]:
                    state["subscribers"].pop(
                        chat_id
                    )
                    changed = True

                safe_say(
                    chat_id,
                    (
                        "Вы отписались от расписания.\n"
                        "Чтобы подписаться снова: /start"
                    ),
                )

                continue

            # ------------------------------------------------
            # CLASS TEXT
            # ------------------------------------------------

            if text in CLASS_TO_SLIDE:
                user = ensure_subscriber(
                    state,
                    chat_id,
                )

                user["class"] = text
                user["sent"] = None
                user["want_schedule"] = True

                changed = True

                safe_say(
                    chat_id,
                    (
                        f"Класс выбран: «{text}».\n"
                        "Расписание будет отправлено "
                        "при ближайшем запуске."
                    ),
                )

                continue

            # ------------------------------------------------
            # UNKNOWN
            # ------------------------------------------------

            safe_say(
                chat_id,
                (
                    "Выберите класс кнопкой ниже.\n\n"
                    "/schedule — получить расписание\n"
                    "/class — выбрать другой класс\n"
                    "/stop — отписаться"
                ),
            )

            send_class_menu(chat_id)

        if len(updates) < 100:
            break

    return changed


# ============================================================
# YANDEX DOCX DOWNLOAD
# ============================================================

def find_download_control(page):
    """
    Тот же подход, который использовался
    в старом рабочем bot.py.
    """

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

    scopes = [page]

    try:
        scopes.extend(page.frames)
    except Exception:
        pass

    for pattern in patterns:
        for scope in scopes:

            locators = []

            try:
                locators.append(
                    scope.get_by_role(
                        "menuitem",
                        name=pattern,
                    )
                )
            except Exception:
                pass

            try:
                locators.append(
                    scope.get_by_role(
                        "button",
                        name=pattern,
                    )
                )
            except Exception:
                pass

            try:
                locators.append(
                    scope.get_by_role(
                        "link",
                        name=pattern,
                    )
                )
            except Exception:
                pass

            try:
                locators.append(
                    scope.get_by_title(
                        pattern
                    )
                )
            except Exception:
                pass

            try:
                locators.append(
                    scope.get_by_label(
                        pattern
                    )
                )
            except Exception:
                pass

            for locator in locators:
                try:
                    count = min(
                        locator.count(),
                        10,
                    )
                except Exception:
                    continue

                for index in range(count):
                    try:
                        element = locator.nth(
                            index
                        )

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

    print("Скачивание расписания...")

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
            print(
                "Открываем страницу расписания..."
            )

            page.goto(
                SOURCE_URL,
                wait_until="domcontentloaded",
                timeout=90000,
            )

            page.wait_for_timeout(12000)

            for attempt in range(4):
                print(
                    f"Поиск кнопки скачивания "
                    f"(попытка {attempt + 1}/4)..."
                )

                control = find_download_control(
                    page
                )

                if control is None:
                    print(
                        "Кнопка скачивания "
                        "пока не найдена."
                    )

                    page.wait_for_timeout(
                        4000
                    )
                    continue

                print(
                    "Кнопка скачивания найдена."
                )

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
                            "об ошибке скачивания"
                        )

                    download.save_as(
                        str(target)
                    )

                    print(
                        f"DOCX скачан: {target}"
                    )

                    break

                except PlaywrightTimeoutError:
                    print(
                        "Ожидание скачивания "
                        "завершилось таймаутом."
                    )

                    page.wait_for_timeout(
                        2000
                    )

            if not target.exists():
                try:
                    page.screenshot(
                        path=str(
                            WORK /
                            "download-error.png"
                        ),
                        full_page=True,
                    )
                except Exception:
                    pass

                raise SafeError(
                    "Не удалось скачать DOCX."
                )

        except Exception:
            try:
                page.screenshot(
                    path=str(
                        WORK /
                        "download-error.png"
                    ),
                    full_page=True,
                )
            except Exception:
                pass

            raise

        finally:
            browser.close()

    if not zipfile.is_zipfile(target):
        raise SafeError(
            "Скачанный файл не является DOCX."
        )

    with zipfile.ZipFile(target) as archive:
        if "word/document.xml" not in archive.namelist():
            raise SafeError(
                "В скачанном архиве нет документа Word."
            )

    print("DOCX успешно проверен.")

    return target


# ============================================================
# DOCX -> PDF -> 5 SLIDES
# ============================================================

def make_schedule(docx_path):
    pdf_path = WORK / "source.pdf"

    if pdf_path.exists():
        pdf_path.unlink()

    print("Конвертация DOCX -> PDF...")

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

    slides = []

    with pymupdf.open(pdf_path) as document:
        if len(document) < 5:
            raise SafeError(
                "В PDF меньше пяти страниц. "
                "Рассылка отменена."
            )

        for number in range(1, 6):
            page = document[number - 1]

            pixmap = page.get_pixmap(
                matrix=pymupdf.Matrix(2, 2),
                colorspace=pymupdf.csRGB,
                alpha=False,
            )

            png_path = (
                WORK /
                f"slide_{number}.png"
            )

            pixmap.save(
                str(png_path)
            )

            digest = hashlib.sha256(
                pixmap.samples
            ).hexdigest()

            slides.append({
                "number": number,
                "path": png_path,
                "hash": digest,
            })

            print(
                f"Слайд {number} готов: "
                f"{png_path.name}"
            )

    return slides


# ============================================================
# SEND / BROADCAST
# ============================================================

def send_photo(
    chat_id,
    photo_path=None,
    file_id=None,
    caption=None,
):
    data = {
        "chat_id": chat_id,
    }

    if caption:
        data["caption"] = caption

    if file_id:
        data["photo"] = file_id

        return telegram(
            "sendPhoto",
            data,
        )

    return telegram(
        "sendPhoto",
        data,
        upload=photo_path,
    )


def broadcast(state, slides, source_ok):
    changed = False

    latest_slides = state[
        "latest"
    ]["slides"]

    # --------------------------------------------------------
    # Обновляем хэши и Telegram file_id
    # --------------------------------------------------------

    for slide in slides:
        index = slide["number"] - 1

        old = latest_slides[index]

        if old.get("hash") != slide["hash"]:
            old["hash"] = slide["hash"]
            old["file_id"] = None

            changed = True

    # --------------------------------------------------------
    # Отправляем нужный слайд каждому пользователю
    # --------------------------------------------------------

    for chat_id, user in list(
        state["subscribers"].items()
    ):
        selected_class = user.get("class")

        if not selected_class:
            continue

        slide_number = CLASS_TO_SLIDE.get(
            selected_class
        )

        if not slide_number:
            continue

        index = slide_number - 1

        current = slides[index]

        current_hash = current["hash"]

        need_send = (
            user.get("want_schedule", False)
            or user.get("sent") != current_hash
        )

        # При автоматической рассылке отправляем
        # только если источник действительно обновился.
        if (
            not user.get("want_schedule", False)
            and not source_ok
        ):
            continue

        if not user.get("want_schedule", False):
            # Для автоматической рассылки нужно,
            # чтобы слайд реально изменился.
            old_hash = user.get("sent")

            if old_hash == current_hash:
                continue

        file_id = latest_slides[index].get(
            "file_id"
        )

        try:
            result = send_photo(
                chat_id,
                photo_path=current["path"],
                file_id=file_id,
                caption=(
                    f"Расписание «{selected_class}»"
                ),
            )

            new_file_id = result["photo"][-1][
                "file_id"
            ]

            latest_slides[index][
                "file_id"
            ] = new_file_id

            user["sent"] = current_hash
            user["want_schedule"] = False

            changed = True

            print(
                f"Слайд {slide_number} отправлен "
                f"{chat_id} "
                f"(класс {selected_class})."
            )

            time.sleep(0.1)

        except TelegramError as error:
            if error.code == 403:
                print(
                    f"Пользователь {chat_id} "
                    "заблокировал бота."
                )

                state["subscribers"].pop(
                    chat_id,
                    None,
                )

                changed = True

            else:
                print(
                    f"Ошибка отправки "
                    f"{chat_id}: код {error.code}"
                )

        except Exception as error:
            print(
                f"Ошибка отправки "
                f"{chat_id}: "
                f"{type(error).__name__}"
            )

    return changed


# ============================================================
# MAIN
# ============================================================

def main():
    print("=" * 60)
    print("School Schedule Bot")
    print("=" * 60)

    state, sha = load_state()

    state_changed = False

    # --------------------------------------------------------
    # 1. Telegram
    # --------------------------------------------------------

    print(
        "Получение новых сообщений Telegram..."
    )

    try:
        if process_commands(state):
            state_changed = True

    except Exception as error:
        print(
            "Ошибка получения команд Telegram: "
            f"{type(error).__name__}"
        )

    # --------------------------------------------------------
    # 2. Download + convert
    # --------------------------------------------------------

    slides = None
    source_ok = False

    try:
        docx_path = download_docx()

        slides = make_schedule(
            docx_path
        )

        source_ok = True

    except Exception as error:
        print(
            f"Ошибка расписания: {error}"
        )

        print(
            "Старое расписание сохранено."
        )

    # --------------------------------------------------------
    # 3. Рассылка
    # --------------------------------------------------------

    if source_ok:
        if broadcast(
            state,
            slides,
            True,
        ):
            state_changed = True

    else:
        # Если DOCX временно не скачался,
        # можно всё равно отправить пользователю
        # последнюю сохранённую версию по file_id.
        for chat_id, user in list(
            state["subscribers"].items()
        ):
            if not user.get("want_schedule"):
                continue

            selected_class = user.get("class")

            if not selected_class:
                continue

            slide_number = CLASS_TO_SLIDE.get(
                selected_class
            )

            if not slide_number:
                continue

            index = slide_number - 1

            cached = state[
                "latest"
            ]["slides"][index]

            file_id = cached.get(
                "file_id"
            )

            cached_hash = cached.get(
                "hash"
            )

            if not file_id:
                print(
                    f"Нет сохранённого file_id "
                    f"для класса {selected_class}."
                )
                continue

            try:
                send_photo(
                    chat_id,
                    file_id=file_id,
                    caption=(
                        f"Последняя доступная версия "
                        f"расписания «{selected_class}»"
                    ),
                )

                user["sent"] = cached_hash
                user["want_schedule"] = False

                state_changed = True

                print(
                    f"Старая версия слайда "
                    f"{slide_number} отправлена "
                    f"{chat_id}."
                )

            except Exception as error:
                print(
                    f"Не удалось отправить "
                    f"старое расписание "
                    f"{chat_id}: "
                    f"{type(error).__name__}"
                )

    # --------------------------------------------------------
    # 4. Save
    # --------------------------------------------------------

    if state_changed:
        save_state(
            state,
            sha,
        )
    else:
        print(
            "Изменений состояния нет."
        )

    print("=" * 60)
    print("Готово.")
    print("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(
            "Ошибка выполнения: "
            f"{type(error).__name__}"
        )
        sys.exit(1)
