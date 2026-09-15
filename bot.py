import base64
import hashlib
import json
import os
import re
import time
from pathlib import Path

import pymupdf
import requests
from playwright.sync_api import sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError


# ============================================================
# CONFIG
# ============================================================

SOURCE_URL = (
    "https://docs.yandex.ru/view/d/"
    "zQH249qvBK21O7SXl-9z_SPegnqahzm72s0qoIz-cKg6eG1uRGNFdE5adw"
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

        except Exception as e:
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
                ["message"]
            ),
        },
    )


def process_commands(state):
    """
    Обрабатывает входящие сообщения Telegram.

    Возвращает:
        changed = True/False
    """

    changed = False

    for batch_number in range(10):
        updates = get_updates(state["offset"])

        if not updates:
            break

        for update in updates:
            state["offset"] = (
                update["update_id"] + 1
            )

            message = update.get("message")

            if not message:
                continue

            chat = message.get("chat", {})
            chat_id = chat.get("id")

            if chat.get("type") != "private":
                continue

            text = (
                message.get("text") or ""
            ).strip()

            if not text:
                continue

            print(
                f"Получено сообщение "
                f"от {chat_id}: {text}"
            )

            user_key = str(chat_id)

            # ------------------------------------------------
            # /start
            # ------------------------------------------------

            if text == "/start":
                if user_key not in state["users"]:
                    state["users"][user_key] = {
                        "class": None,
                        "sent": None,
                        "want_schedule": False,
                    }
                    changed = True

                welcome_msg = (
                    "Добро пожаловать в бота для получения расписания школы №9!\n\n"
                    "Бот обновляется раз в 5 минут, поэтому возможны небольшие задержки. "
                    "Если бот не отвечает более 10 минут — напишите @Verek0n\n\n"
                )

                current_class = (
                    state["users"][user_key]
                    .get("class")
                )

                if current_class:
                    send_message(
                        chat_id,
                        welcome_msg + f"Текущий класс: {current_class}",
                        class_keyboard(),
                    )
                else:
                    send_message(
                        chat_id,
                        welcome_msg + "Выберите свой класс на клавиатуре ниже:",
                        class_keyboard(),
                    )

                continue

            # ------------------------------------------------
            # /stop
            # ------------------------------------------------

            if text == "/stop":
                if user_key in state["users"]:
                    del state["users"][user_key]
                    changed = True

                send_message(
                    chat_id,
                    "Вы отписались от рассылки расписания.\nЧтобы вернуться, отправьте /start.",
                    REMOVE_KEYBOARD,
                )

                continue

            # ------------------------------------------------
            # /class
            # ------------------------------------------------

            if text == "/class":
                send_message(
                    chat_id,
                    "Выберите класс:",
                    class_keyboard(),
                )
                continue

            # ------------------------------------------------
            # /schedule
            # ------------------------------------------------

            if text == "/schedule":
                if user_key not in state["users"]:
                    state["users"][user_key] = {
                        "class": None,
                        "sent": None,
                        "want_schedule": False,
                    }
                    changed = True

                user = state["users"][user_key]

                if not user.get("class"):
                    send_message(
                        chat_id,
                        "Сначала выберите свой класс на клавиатуре:",
                        class_keyboard(),
                    )
                else:
                    user["want_schedule"] = True
                    changed = True

                    send_message(
                        chat_id,
                        "Запрос принят! Расписание отправляется...",
                    )

                continue

            # ------------------------------------------------
            # /stats (Только для администратора)
            # ------------------------------------------------

            if text == "/stats":
                if user_key == "1334717692":
                    subscribers = state.get("users", {})

                    total = len(subscribers)

                    with_class = sum(
                        1
                        for user in subscribers.values()
                        if user.get("class")
                    )

                    without_class = total - with_class

                    lines = [
                        "Статистика бота",
                        "",
                        f"Всего пользователей: {total}",
                        f"С выбранным классом: {with_class}",
                        f"Без класса: {without_class}",
                    ]

                    send_message(chat_id, "\n".join(lines))
                    continue

            # ------------------------------------------------
            # CLASS BUTTON
            # ------------------------------------------------

            if text in CLASS_TO_SLIDE:
                if user_key not in state["users"]:
                    state["users"][user_key] = {
                        "class": None,
                        "sent": None,
                        "want_schedule": False,
                    }

                user = state["users"][user_key]

                old_class = user.get("class")

                user["class"] = text

                # Обязательно отправить новое расписание
                user["sent"] = None
                user["want_schedule"] = True

                changed = True

                print(
                    f"Пользователь {chat_id} "
                    f"выбрал класс {text} "
                    f"(было: {old_class})"
                )

                send_message(
                    chat_id,
                    (
                        f"Класс выбран: «{text}»\n"
                        "Расписание отправляется..."
                    ),
                    class_keyboard(),
                )

                continue

            # ------------------------------------------------
            # UNKNOWN
            # ------------------------------------------------

            send_message(
                chat_id,
                (
                    "Это конечно прикольно, но я умею только:\n"
                    "/schedule - Получить расписание\n"
                    "/class - Изменить свой класс\n"
                    "/start - Подписаться на расписание\n"
                    "/stop - Отключить рассылку"
                ),
                class_keyboard(),
            )

    return changed


# ============================================================
# YANDEX PDF DOWNLOAD (FRAME-AWARE)
# ============================================================

def _find_element(page, candidates):
    """
    Ищет элемент во всех фреймах страницы (включая iframe просмотрщика).
    """
    roots = [page] + list(page.frames)
    for root in roots:
        for item in candidates:
            try:
                if isinstance(item, tuple):
                    role, pattern = item
                    loc = root.get_by_role(role, name=pattern)
                elif isinstance(item, str):
                    loc = root.get_by_text(item, exact=False)
                else:
                    loc = root.get_by_text(item)

                for i in range(loc.count()):
                    el = loc.nth(i)
                    if el.is_visible():
                        return el
            except Exception:
                continue
    return None


def download_pdf():
    """
    Скачивает сразу PDF из Яндекс.Документов через поиск по iframe.
    """
    target = WORK / "source.pdf"

    try:
        target.unlink()
    except FileNotFoundError:
        pass

    print("Скачивание расписания в формате PDF...")

    with sync_playwright() as p:
        browser = None
        try:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-blink-features=AutomationControlled",
                ],
            )

            context = browser.new_context(
                accept_downloads=True,
                locale="ru-RU",
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1920, "height": 1080},
            )

            page = context.new_page()
            page.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )

            print("Открываем страницу расписания...")
            page.goto(SOURCE_URL, wait_until="domcontentloaded", timeout=90000)
            
            # Даем интерфейсу редактора догрузиться
            page.wait_for_timeout(10000)

            download_ok = False

            for attempt in range(5):
                print(f"Попытка скачать через меню ({attempt + 1}/5)...")
                try:
                    # 1. Поиск «Файл» во всех фреймах
                    file_btn = _find_element(page, [
                        ("button", re.compile(r"^Файл$", re.I)),
                        ("menuitem", re.compile(r"^Файл$", re.I)),
                        re.compile(r"^Файл$", re.I),
                        "Файл",
                    ])

                    if not file_btn:
                        print("Кнопка «Файл» пока не найдена во фреймах...")
                        page.wait_for_timeout(3000)
                        continue

                    print("Нажатие на «Файл»...")
                    file_btn.click(timeout=5000)
                    page.wait_for_timeout(1000)

                    # 2. Поиск «Скачать» во всех фреймах
                    download_btn = _find_element(page, [
                        ("menuitem", re.compile(r"Скачать", re.I)),
                        ("button", re.compile(r"Скачать", re.I)),
                        re.compile(r"Скачать", re.I),
                        "Скачать",
                    ])

                    if not download_btn:
                        print("Пункт «Скачать» не найден, сбрасываем...")
                        page.keyboard.press("Escape")
                        page.wait_for_timeout(1500)
                        continue

                    print("Наведение и клик по «Скачать»...")
                    try:
                        download_btn.hover(timeout=3000)
                        page.wait_for_timeout(500)
                    except Exception:
                        pass

                    try:
                        download_btn.click(timeout=3000)
                    except Exception:
                        pass

                    page.wait_for_timeout(1000)

                    # 3. Поиск «Документ PDF (.pdf)» во всех фреймах
                    pdf_btn = _find_element(page, [
                        ("menuitem", re.compile(r"PDF|\.pdf", re.I)),
                        ("button", re.compile(r"PDF|\.pdf", re.I)),
                        re.compile(r"Документ PDF|\.pdf|PDF", re.I),
                        ".pdf",
                    ])

                    if not pdf_btn:
                        print("Пункт «Документ PDF» не найден.")
                        page.keyboard.press("Escape")
                        page.wait_for_timeout(1500)
                        continue

                    print("Ожидание скачивания PDF...")
                    with page.expect_download(timeout=40000) as download_info:
                        pdf_btn.click(timeout=5000, force=True)

                    download = download_info.value
                    if download.failure():
                        raise RuntimeError(
                            f"Ошибка скачивания: {download.failure()}"
                        )

                    download.save_as(target)
                    print(f"PDF скачан: {target}")
                    download_ok = True
                    break

                except PlaywrightTimeoutError:
                    print("Таймаут ожидания скачивания.")
                except Exception as e:
                    print(f"Ошибка попытки: {e}")

                try:
                    page.keyboard.press("Escape")
                    page.wait_for_timeout(1000)
                except Exception:
                    pass

            if not download_ok or not target.exists():
                try:
                    page.screenshot(
                        path=str(WORK / "download-error.png"), full_page=True
                    )
                    print("Скриншот ошибки сохранён в work/download-error.png")
                except Exception:
                    pass
                raise RuntimeError("Не удалось скачать PDF.")

        finally:
            if browser is not None:
                browser.close()

    # Проверка файла
    if target.stat().st_size < 1000:
        raise RuntimeError("Скачанный PDF слишком мал (возможно, пустой).")

    print("PDF успешно проверен.")
    return target


# ============================================================
# PDF -> 5 PNG
# ============================================================

def make_schedule(pdf_path):
    """
    Открывает скачанный PDF напрямую (без конвертации LibreOffice)
    и сохраняет 5 первых страниц как слайды PNG.
    """
    print("Рендер PDF в PNG...")

    doc = pymupdf.open(pdf_path)
    slides = []

    try:
        page_count = len(doc)

        print(
            f"В PDF найдено страниц: {page_count}"
        )

        if page_count < 5:
            raise RuntimeError(
                f"Ожидалось минимум 5 страниц, "
                f"получено {page_count}."
            )

        for slide_number in range(1, 6):
            page_index = slide_number - 1

            page = doc[page_index]

            # DPI примерно 150.
            matrix = pymupdf.Matrix(
                150 / 72,
                150 / 72,
            )

            pix = page.get_pixmap(
                matrix=matrix,
                alpha=False,
                colorspace=pymupdf.csRGB,
            )

            png_path = (
                WORK /
                f"slide_{slide_number}.png"
            )

            pix.save(str(png_path))

            image_bytes = png_path.read_bytes()

            image_hash = hashlib.sha256(
                image_bytes
            ).hexdigest()

            slides.append(
                {
                    "number": slide_number,
                    "path": png_path,
                    "hash": image_hash,
                }
            )

            print(
                f"Слайд {slide_number}: "
                f"{png_path.name}, "
                f"hash={image_hash[:12]}"
            )

        return slides

    finally:
        doc.close()


# ============================================================
# SEND ONE SLIDE
# ============================================================

def send_slide(
    chat_id,
    slide_number,
    slide_path,
    file_id=None,
    caption=None,
):
    """
    Если есть Telegram file_id — повторно загружать PNG
    не нужно.
    """

    if file_id:
        result = send_photo(
            chat_id,
            file_id=file_id,
            caption=caption,
        )
    else:
        result = send_photo(
            chat_id,
            photo_path=slide_path,
            caption=caption,
        )

    return result


# ============================================================
# BROADCAST
# ============================================================

def broadcast(state, slides, schedule_changed):
    """
    Рассылает расписание только нужным пользователям.

    Если пользователь выбрал класс:
        want_schedule=True
        -> получает расписание.

    Если расписание изменилось:
        пользователи, которым уже отправлялся этот слайд,
        получают новую версию.

    Telegram file_id кешируется в state.json.
    """

    users = state["users"]

    old_slides = state["latest"]["slides"]

    # --------------------------------------------------------
    # Обновляем информацию о 5 слайдах
    # --------------------------------------------------------

    for slide in slides:
        number = slide["number"]
        index = number - 1

        old = old_slides[index]

        if old.get("hash") == slide["hash"]:
            # Слайд не изменился.
            # Старый Telegram file_id сохраняем.
            continue

        # Слайд новый/изменённый.
        old["hash"] = slide["hash"]

        # Старый file_id относится к старой картинке.
        old["file_id"] = None

    # --------------------------------------------------------
    # Отправка пользователям
    # --------------------------------------------------------

    for user_key, user in list(users.items()):
        try:
            chat_id = int(user_key)

            selected_class = user.get("class")

            if not selected_class:
                continue

            slide_number = CLASS_TO_SLIDE.get(
                selected_class
            )

            if not slide_number:
                continue

            index = slide_number - 1

            current_slide = slides[index]

            current_hash = current_slide["hash"]

            old_user_hash = user.get("sent")

            want_schedule = bool(
                user.get("want_schedule")
            )

            # ------------------------------------------------
            # Нужно ли отправлять?
            # ------------------------------------------------

            should_send = False
            is_new_alert = False

            # Пользователь только выбрал класс
            # или запросил /schedule.
            if want_schedule:
                should_send = True

            # Слайд изменился после предыдущей отправки.
            elif (
                schedule_changed
                and old_user_hash != current_hash
            ):
                should_send = True
                is_new_alert = True  # Это автоматическое уведомление о новом расписании

            if not should_send:
                continue

            cached_file_id = (
                state["latest"]["slides"][index]
                .get("file_id")
            )

            # Подпись к фото с динамическим классом
            if is_new_alert:
                caption = f"Доступно новое расписание «{selected_class}»!"
            else:
                caption = f"Расписание «{selected_class}»"

            # ------------------------------------------------
            # Если есть file_id — отправляем его.
            # Если нет — загружаем PNG.
            # ------------------------------------------------

            if cached_file_id:
                result = send_slide(
                    chat_id,
                    slide_number,
                    current_slide["path"],
                    file_id=cached_file_id,
                    caption=caption,
                )

            else:
                result = send_slide(
                    chat_id,
                    slide_number,
                    current_slide["path"],
                    file_id=None,
                    caption=caption,
                )

                # Получаем file_id из Telegram.
                try:
                    photo = result.get("photo", [])

                    if photo:
                        new_file_id = photo[-1]["file_id"]

                        state["latest"]["slides"][
                            index
                        ]["file_id"] = new_file_id

                        print(
                            f"Слайд {slide_number}: "
                            f"Telegram file_id сохранён."
                        )

                except Exception as e:
                    print(
                        f"Не удалось получить "
                        f"file_id: {e}"
                    )

            user["sent"] = current_hash
            user["want_schedule"] = False

            print(
                f"Расписание слайда {slide_number} "
                f"отправлено пользователю {chat_id} "
                f"(класс {selected_class})."
            )

        except requests.HTTPError as e:
            # Пользователь мог заблокировать бота.
            print(
                f"Ошибка Telegram для "
                f"{user_key}: {e}"
            )

            response = getattr(e, "response", None)

            if response is not None:
                if response.status_code == 403:
                    print(
                        f"Удаляем пользователя "
                        f"{user_key}: бот заблокирован."
                    )

                    users.pop(user_key, None)

        except Exception as e:
            print(
                f"Ошибка отправки пользователю "
                f"{user_key}: {e}"
            )


# ============================================================
# MAIN
# ============================================================

def main():
    print("=" * 60)
    print("School Schedule Bot")
    print("=" * 60)

    state, state_sha = load_state()

    # --------------------------------------------------------
    # 1. Сначала забираем новые сообщения Telegram.
    # --------------------------------------------------------

    print("Получение новых сообщений Telegram...")

    state_changed = process_commands(state)

    if state_changed:
        print(
            "Есть изменения состояния "
            "после обработки Telegram."
        )
    else:
        print(
            "Новых изменений состояния нет."
        )

    # --------------------------------------------------------
    # 2. Скачиваем расписание (в формате PDF).
    # --------------------------------------------------------

    slides = None
    schedule_changed = False

    try:
        pdf_path = download_pdf()

        slides = make_schedule(pdf_path)

        old_slides = state["latest"]["slides"]

        for slide in slides:
            index = slide["number"] - 1

            old_hash = old_slides[index].get(
                "hash"
            )

            if old_hash != slide["hash"]:
                schedule_changed = True

        if schedule_changed:
            print(
                "Расписание изменилось."
            )
        else:
            print(
                "Расписание не изменилось."
            )

    except Exception as e:
        print(
            f"Ошибка расписания: {e}"
        )

        print(
            "Старое расписание сохранено."
        )

        slides = []

        for i in range(5):
            slides.append(
                {
                    "number": i + 1,
                    "path": None,
                    "hash": state["latest"][
                        "slides"
                    ][i].get("hash"),
                }
            )

    # --------------------------------------------------------
    # 3. Рассылка пользователям.
    # --------------------------------------------------------

    if slides:
        users = state["users"]

        for user_key, user in list(
            users.items()
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

            current_slide = slides[index]

            if current_slide["path"] is None:
                if not user.get("want_schedule"):
                    continue

                old_file_id = state[
                    "latest"
                ]["slides"][index].get(
                    "file_id"
                )

                old_hash = state[
                    "latest"
                ]["slides"][index].get(
                    "hash"
                )

                if not old_file_id:
                    print(
                        f"Нет кешированного расписания "
                        f"для пользователя {user_key}."
                    )
                    continue

                try:
                    send_slide(
                        int(user_key),
                        slide_number,
                        None,
                        file_id=old_file_id,
                        caption=f"Расписание «{selected_class}»",
                    )

                    user["sent"] = old_hash
                    user["want_schedule"] = False

                    print(
                        f"Старая версия расписания "
                        f"отправлена {user_key}."
                    )

                    state_changed = True

                except Exception as e:
                    print(
                        f"Ошибка отправки старого "
                        f"расписания {user_key}: {e}"
                    )

        if any(
            slide["path"] is not None
            for slide in slides
        ):
            broadcast(
                state,
                slides,
                schedule_changed,
            )

            state_changed = True

    # --------------------------------------------------------
    # 4. Сохраняем state.json.
    # --------------------------------------------------------

    if state_changed:
        save_state(
            state,
            state_sha,
        )
    else:
        print(
            "Изменений состояния нет."
        )

    print("=" * 60)
    print("Готово.")
    print("=" * 60)


if __name__ == "__main__":
    main()
