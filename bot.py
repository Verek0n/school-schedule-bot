import base64
import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pymupdf
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from playwright.sync_api import sync_playwright

# ============================================================
# CONFIG
# ============================================================

SOURCE_URL = (
    "https://docs.yandex.ru/view/d/"
    "zQH249qvBK21O7SXl-9z_SPegnqahzm72s0qoIz-cKg6eG1uRGNFdE5adw"
)

BOT_TOKEN = os.environ["BOT_TOKEN"]
STATE_TOKEN = os.environ["STATE_TOKEN"]

# В чат админа отправляем фото для получения file_id, затем удаляем
ADMIN_CHAT_ID = 1334717692

# Интервал проверки Яндекса: 900 секунд = 15 минут
YANDEX_CHECK_INTERVAL = 900

STATE_REPO = "Verek0n/school-schedule-state"
WORK = Path("work")
WORK.mkdir(exist_ok=True)

STATE_URL = f"https://api.github.com/repos/{STATE_REPO}/contents/state.json"

GH_HEADERS = {
    "Authorization": f"Bearer {STATE_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

session = requests.Session()
retries = Retry(total=3, backoff_factor=0.5, status_forcelist=[500, 502, 503, 504])
session.mount("https://", HTTPAdapter(max_retries=retries, pool_connections=10, pool_maxsize=10))

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

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
# TELEGRAM
# ============================================================

TG_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"


def telegram(method, data=None, upload=None):
    url = f"{TG_URL}/{method}"

    for attempt in range(5):
        try:
            files = None
            if upload is not None:
                upload_path = Path(upload)
                mime_type = "image/jpeg" if upload_path.suffix.lower() in (".jpg", ".jpeg") else "image/png"
                files = {
                    "photo": (
                        upload_path.name,
                        upload_path.open("rb"),
                        mime_type,
                    )
                }

            response = session.post(
                url,
                data=data or {},
                files=files,
                timeout=(10, 30),
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
                print(f"Telegram rate limit, ждём {retry_after} сек.")
                time.sleep(retry_after)
                continue

            response.raise_for_status()
            result = response.json()
            if not result.get("ok"):
                raise RuntimeError(f"Telegram API error: {result}")
            return result["result"]

        except Exception:
            if attempt == 4:
                raise
            time.sleep(1)

    raise RuntimeError("Telegram API failed")


def send_message(chat_id, text, keyboard=None):
    data = {"chat_id": chat_id, "text": text}
    if keyboard is not None:
        data["reply_markup"] = json.dumps(keyboard, ensure_ascii=False)
    return telegram("sendMessage", data)


def send_photo(chat_id, photo_path=None, file_id=None, caption=None, keyboard=None):
    data = {"chat_id": chat_id}
    if caption:
        data["caption"] = caption
    if keyboard is not None:
        data["reply_markup"] = json.dumps(keyboard, ensure_ascii=False)
    if file_id:
        data["photo"] = file_id
        return telegram("sendPhoto", data)
    if photo_path:
        return telegram("sendPhoto", data, upload=photo_path)
    raise ValueError("Нужно передать photo_path или file_id")


def delete_message(chat_id, message_id):
    return telegram("deleteMessage", {
        "chat_id": chat_id,
        "message_id": message_id,
    })


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
        "one_time_keyboard": True,  # Сворачивается сразу после выбора
    }

REMOVE_KEYBOARD = {"remove_keyboard": True}


# ============================================================
# STATE
# ============================================================

def default_state():
    return {
        "offset": 0,
        "last_yandex_check": 0,
        "users": {},
        "latest": {
            "slides": [
                {"hash": None, "file_id": None} for _ in range(5)
            ]
        },
    }


def load_state():
    try:
        response = session.get(STATE_URL, headers=GH_HEADERS, timeout=15)

        if response.status_code == 404:
            print("state.json ещё не существует.")
            return default_state(), None

        response.raise_for_status()
        payload = response.json()
        content = base64.b64decode(payload["content"]).decode("utf-8")
        state = json.loads(content)

        state.setdefault("offset", 0)
        state.setdefault("last_yandex_check", 0)
        state.setdefault("users", {})
        state.setdefault("latest", {})

        if "slides" not in state["latest"]:
            state["latest"]["slides"] = [
                {"hash": None, "file_id": None} for _ in range(5)
            ]

        while len(state["latest"]["slides"]) < 5:
            state["latest"]["slides"].append({"hash": None, "file_id": None})

        return state, payload["sha"]

    except Exception as e:
        print(f"Ошибка загрузки state.json: {e}")
        raise


def save_state(state, sha=None):
    content = json.dumps(state, ensure_ascii=False, indent=2)
    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")

    data = {
        "message": "Update state.json",
        "content": encoded,
        "branch": "main",
    }
    if sha:
        data["sha"] = sha

    response = session.put(STATE_URL, headers=GH_HEADERS, json=data, timeout=15)
    response.raise_for_status()
    print("state.json сохранён.")
    return response.json()["content"]["sha"]


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
            "allowed_updates": json.dumps(["message"]),
        },
    )


def process_commands(state):
    changed = False

    for _ in range(10):
        try:
            updates = get_updates(state["offset"])
        except Exception as e:
            print(f"Ошибка получения обновлений Telegram: {e}")
            break

        if not updates:
            break

        for update in updates:
            state["offset"] = update["update_id"] + 1
            message = update.get("message")
            if not message:
                continue

            chat = message.get("chat", {})
            chat_id = chat.get("id")
            if chat.get("type") != "private":
                continue

            text = (message.get("text") or "").strip()
            if not text:
                continue

            user_key = str(chat_id)
            print(f"Получено сообщение от {chat_id}: {text}")

            try:
                is_admin = (user_key == str(ADMIN_CHAT_ID))

                # АДМИН-РАССЫЛКА: /broadcast <текст> или /объявление <текст>
                if is_admin and (text.startswith("/broadcast ") or text.startswith("/объявление ")):
                    parts = text.split(maxsplit=1)
                    if len(parts) > 1:
                        broadcast_text = parts[1]
                        subscribers = list(state.get("users", {}).keys())
                        send_message(chat_id, f"Начинаю рассылку для {len(subscribers)} пользователей...")

                        success_count = 0
                        fail_count = 0

                        for sub_id in subscribers:
                            try:
                                send_message(int(sub_id), broadcast_text)
                                success_count += 1
                                time.sleep(0.03)
                            except requests.HTTPError as e:
                                if e.response is not None and e.response.status_code in (403, 400):
                                    print(f"Удаляем пользователя {sub_id} (заблокировал бота).")
                                    if sub_id in state["users"]:
                                        del state["users"][sub_id]
                                        changed = True
                                    fail_count += 1
                                else:
                                    print(f"Не удалось отправить {sub_id}: {e}")
                                    fail_count += 1
                            except Exception as e:
                                print(f"Ошибка отправки {sub_id}: {e}")
                                fail_count += 1

                        send_message(
                            chat_id,
                            f"Рассылка завершена.\nУспешно: {success_count}\nОшибок/Удалено: {fail_count}"
                        )
                    else:
                        send_message(chat_id, "Использование: /broadcast <текст уведомления>")
                    continue

                # /start
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

                    current_class = state["users"][user_key].get("class")
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

                # /stop
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

                # /class
                if text == "/class":
                    send_message(chat_id, "Выберите класс:", class_keyboard())
                    continue

                # /schedule
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
                    continue

                # /stats
                if text == "/stats":
                    if user_key == "1334717692":
                        subscribers = state.get("users", {})
                        total = len(subscribers)
                        with_class = sum(1 for u in subscribers.values() if u.get("class"))
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

                # CLASS BUTTON
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
                    user["sent"] = None
                    user["want_schedule"] = True
                    changed = True

                    print(f"Пользователь {chat_id} выбрал класс {text} (было: {old_class})")
                    continue

                # UNKNOWN
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

            except requests.HTTPError as e:
                if e.response is not None and e.response.status_code in (403, 400):
                    print(f"Пользователь {chat_id} заблокировал бота. Удаляем из состояния.")
                    if user_key in state["users"]:
                        del state["users"][user_key]
                        changed = True
                else:
                    print(f"HTTP ошибка при обработке сообщения от {chat_id}: {e}")
            except Exception as e:
                print(f"Ошибка при обработке сообщения от {chat_id}: {e}")

    return changed


# ============================================================
# YANDEX PDF DOWNLOAD (TARGETED IFRAME WAIT & CLICK)
# ============================================================

def download_pdf():
    target = WORK / "source.pdf"
    if target.exists():
        target.unlink()

    print("Скачивание расписания в формате PDF через браузер...")
    t0 = time.time()

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )

        context = browser.new_context(
            accept_downloads=True,
            locale="ru-RU",
            user_agent=UA,
            viewport={"width": 1920, "height": 1080},
        )

        page = context.new_page()

        # Маскировка под обычный Chrome
        page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            window.chrome = { runtime: {} };
        """)

        def route_filter(route):
            url = route.request.url
            if any(x in url for x in ["mc.yandex.ru", "yandex.ru/clck", "metrika", "an.yandex.ru"]):
                return route.abort()
            return route.continue_()

        page.route("**/*", route_filter)

        print("Открытие страницы Яндекса...")
        try:
            page.goto(SOURCE_URL, wait_until="domcontentloaded", timeout=45000)
        except Exception as e:
            print(f"Предупреждение при переходе по URL: {e}")

        # Цикл ожидания монтирования фрейма редактора и появление меню "Файл" (до 30 сек)
        target_frame = None
        file_element = None

        print("Ожидание полной загрузки редактора Яндекса...")
        start_wait = time.time()
        while time.time() - start_wait < 30:
            for frame in page.frames:
                try:
                    loc = frame.get_by_text("Файл", exact=True)
                    if loc.count() > 0:
                        target_frame = frame
                        file_element = loc.first
                        print(f"Редактор прогружен! Найден фрейм с меню 'Файл': {frame.url}")
                        break
                    loc_reg = frame.get_by_text(re.compile(r"^Файл$", re.I))
                    if loc_reg.count() > 0:
                        target_frame = frame
                        file_element = loc_reg.first
                        print(f"Редактор прогружен! Найден фрейм (regex): {frame.url}")
                        break
                except Exception:
                    pass
            if file_element:
                break
            page.wait_for_timeout(1500)

        if not target_frame or not file_element:
            browser.close()
            raise RuntimeError("Редактор Яндекса не загрузился за 30 секунд (меню 'Файл' не найдено).")

        download_ok = False

        try:
            # 1. Клик по "Файл"
            print("1. Клик по меню 'Файл'...")
            file_element.click(force=True, timeout=5000)
            page.wait_for_timeout(1000)

            # 2. Поиск и наведение на "Скачать"
            print("2. Поиск пункта 'Скачать'...")
            dl_element = None
            for frame in page.frames:
                try:
                    loc = frame.get_by_text(re.compile(r"Скачать", re.I))
                    if loc.count() > 0:
                        dl_element = loc.first
                        break
                except Exception:
                    pass

            if dl_element:
                try:
                    dl_element.hover(force=True, timeout=2000)
                except Exception:
                    pass
                try:
                    dl_element.click(force=True, timeout=2000)
                except Exception:
                    pass

            page.wait_for_timeout(1000)

            # 3. Поиск пункта "Документ PDF" и перехват скачивания
            print("3. Поиск пункта 'Документ PDF' и запуск скачивания...")
            pdf_element = None
            for frame in page.frames:
                try:
                    loc = frame.get_by_text(re.compile(r"Документ PDF|\.pdf", re.I))
                    if loc.count() > 0:
                        pdf_element = loc.first
                        break
                except Exception:
                    pass

            if not pdf_element:
                raise RuntimeError("Пункт меню 'Документ PDF' не появился во фреймах.")

            with page.expect_download(timeout=35000) as download_info:
                pdf_element.click(force=True, timeout=5000)

            download = download_info.value
            if download.failure():
                raise RuntimeError(f"Ошибка при скачивании файла: {download.failure()}")

            download.save_as(target)
            download_ok = True
            print("PDF успешно скачан из Яндекса!")

        except Exception as e:
            print(f"Ошибка скачивания через UI: {e}")

        browser.close()

        if not download_ok or not target.exists():
            raise RuntimeError("Не удалось скачать PDF через интерфейс Яндекса.")

    if target.stat().st_size < 5000:
        raise RuntimeError(f"Скачанный PDF слишком мал ({target.stat().st_size} байт).")

    print(f"PDF успешно скачан и проверен ({target.stat().st_size} байт, за {time.time() - t0:.1f}с).")
    return target


# ============================================================
# PDF -> 5 JPG (HIGH QUALITY 200 DPI)
# ============================================================

def make_schedule(pdf_path):
    print("Рендер PDF в JPG (200 DPI)...")
    doc = pymupdf.open(pdf_path)
    slides = []

    try:
        page_count = len(doc)
        print(f"В PDF найдено страниц: {page_count}")

        if page_count < 5:
            raise RuntimeError(f"Скачанный PDF содержит только {page_count} страниц (ожидалось минимум 5).")

        matrix = pymupdf.Matrix(200 / 72, 200 / 72)

        for slide_number in range(1, 6):
            page = doc[slide_number - 1]
            pix = page.get_pixmap(
                matrix=matrix,
                alpha=False,
                colorspace=pymupdf.csRGB,
            )

            jpg_path = WORK / f"slide_{slide_number}.jpg"
            pix.save(str(jpg_path), jpg_quality=95)

            image_hash = hashlib.sha256(jpg_path.read_bytes()).hexdigest()
            slides.append({
                "number": slide_number,
                "path": jpg_path,
                "hash": image_hash,
            })

            print(f"Слайд {slide_number}: {jpg_path.name}, hash={image_hash[:12]}")

        return slides

    finally:
        doc.close()


# ============================================================
# WARMUP CACHE — загружаем все 5 слайдов в Telegram сразу
# ============================================================

def warmup_cache(state, slides):
    changed = False

    for slide in slides:
        idx = slide["number"] - 1
        old = state["latest"]["slides"][idx]

        if old.get("hash") == slide["hash"] and old.get("file_id"):
            continue

        try:
            result = send_photo(
                ADMIN_CHAT_ID,
                photo_path=slide["path"],
            )

            message_id = result.get("message_id")
            photos = result.get("photo", [])

            if photos:
                new_file_id = photos[-1]["file_id"]
                state["latest"]["slides"][idx]["file_id"] = new_file_id
                state["latest"]["slides"][idx]["hash"] = slide["hash"]
                changed = True
                print(f"Кэш: слайд {slide['number']} file_id сохранён.")
            else:
                print(f"Кэш: слайд {slide['number']} — не удалось получить file_id.")

            if message_id:
                try:
                    delete_message(ADMIN_CHAT_ID, message_id)
                except Exception:
                    pass

        except Exception as e:
            print(f"Кэш: ошибка загрузки слайда {slide['number']}: {e}")

    return changed


# ============================================================
# SEND ONE SLIDE
# ============================================================

def send_slide(chat_id, slide_number, slide_path, file_id=None, caption=None, keyboard=None):
    if file_id:
        return send_photo(chat_id, file_id=file_id, caption=caption, keyboard=keyboard)
    else:
        return send_photo(chat_id, photo_path=slide_path, caption=caption, keyboard=keyboard)


# ============================================================
# FAST USER REQUEST FULFILLMENT (FROM CACHE)
# ============================================================

def fulfill_user_requests(state):
    changed = False
    users = state["users"]
    slides = state["latest"]["slides"]

    for user_key, user in list(users.items()):
        if not user.get("want_schedule"):
            continue

        selected_class = user.get("class")
        if not selected_class or selected_class not in CLASS_TO_SLIDE:
            user["want_schedule"] = False
            changed = True
            continue

        slide_num = CLASS_TO_SLIDE[selected_class]
        cached = slides[slide_num - 1]
        file_id = cached.get("file_id")

        if not file_id:
            continue

        try:
            send_slide(
                int(user_key),
                slide_num,
                None,
                file_id=file_id,
                caption=f"Расписание «{selected_class}»",
                keyboard=REMOVE_KEYBOARD,
            )
            user["sent"] = cached.get("hash")
            user["want_schedule"] = False
            changed = True
            print(f"Мгновенно отправлено из кэша для {user_key} ({selected_class})")
        except requests.HTTPError as e:
            if getattr(e.response, "status_code", None) in (403, 400):
                print(f"Удаляем пользователя {user_key}: бот заблокирован.")
                users.pop(user_key, None)
                changed = True
        except Exception as e:
            print(f"Ошибка мгновенной отправки {user_key}: {e}")

    return changed


# ============================================================
# BROADCAST
# ============================================================

def broadcast(state, slides, schedule_changed):
    users = state["users"]

    for user_key, user in list(users.items()):
        try:
            chat_id = int(user_key)
            selected_class = user.get("class")
            if not selected_class or selected_class not in CLASS_TO_SLIDE:
                user["want_schedule"] = False
                continue

            slide_number = CLASS_TO_SLIDE[selected_class]
            index = slide_number - 1
            current_slide = slides[index]
            current_hash = current_slide["hash"]
            old_user_hash = user.get("sent")
            want_schedule = bool(user.get("want_schedule"))

            should_send = False
            is_new_alert = False

            if want_schedule:
                should_send = True
            elif schedule_changed and old_user_hash != current_hash:
                should_send = True
                is_new_alert = True

            if not should_send:
                continue

            cached_file_id = state["latest"]["slides"][index].get("file_id")

            if is_new_alert:
                caption = f"Доступно новое расписание «{selected_class}»!"
            else:
                caption = f"Расписание «{selected_class}»"

            send_slide(
                chat_id,
                slide_number,
                current_slide["path"],
                file_id=cached_file_id,
                caption=caption,
                keyboard=REMOVE_KEYBOARD,
            )

            user["sent"] = current_hash
            user["want_schedule"] = False

            print(
                f"Расписание слайда {slide_number} "
                f"отправлено пользователю {chat_id} "
                f"(класс {selected_class})."
            )

        except requests.HTTPError as e:
            print(f"Ошибка Telegram для {user_key}: {e}")
            response = getattr(e, "response", None)
            if response is not None and response.status_code in (403, 400):
                print(f"Удаляем пользователя {user_key}: бот заблокирован.")
                users.pop(user_key, None)

        except Exception as e:
            print(f"Ошибка отправки пользователю {user_key}: {e}")


# ============================================================
# MAIN
# ============================================================

def main():
    print("=" * 60)
    print("School Schedule Bot")
    print("=" * 60)

    state, state_sha = load_state()

    # 1. Забираем входящие сообщения
    print("Получение новых сообщений Telegram...")
    state_changed = process_commands(state)

    # 2. Быстро отвечаем из кэша
    if fulfill_user_requests(state):
        state_changed = True

    # 3. Решаем, идти ли на Яндекс (проверка с 12:00 до 03:00 МСК)
    now = time.time()
    time_since_last_check = now - state.get("last_yandex_check", 0)

    # Часовой пояс Москвы (UTC+3)
    msk_tz = timezone(timedelta(hours=3))
    now_msk = datetime.now(msk_tz)

    # Активные часы: с 12:00 дня до 03:00 ночи МСК (т.е. часы 12..23 и 0, 1, 2)
    is_active_hours = (now_msk.hour >= 12 or now_msk.hour < 3)

    needs_yandex = False
    for user in state["users"].values():
        if user.get("want_schedule"):
            cls = user.get("class")
            if cls and cls in CLASS_TO_SLIDE:
                idx = CLASS_TO_SLIDE[cls] - 1
                if not state["latest"]["slides"][idx].get("file_id"):
                    needs_yandex = True
                    break
            else:
                user["want_schedule"] = False
                state_changed = True

    should_fetch_yandex = is_active_hours and (
        time_since_last_check >= YANDEX_CHECK_INTERVAL
        or needs_yandex
    )

    if not is_active_hours:
        print(
            f"Пропуск скачивания с Яндекса: сейчас {now_msk.strftime('%H:%M')} МСК "
            f"(проверка работает только с 12:00 до 03:00 МСК)."
        )
    elif should_fetch_yandex:
        print(f"Идем проверять Яндекс (прошло {int(time_since_last_check)}с, время {now_msk.strftime('%H:%M')} МСК)...")
        schedule_changed = False

        try:
            pdf_path = download_pdf()
            slides = make_schedule(pdf_path)

            for slide in slides:
                idx = slide["number"] - 1
                if state["latest"]["slides"][idx].get("hash") != slide["hash"]:
                    schedule_changed = True

            if schedule_changed:
                print("Расписание изменилось.")
            else:
                print("Расписание не изменилось.")

            if warmup_cache(state, slides):
                state_changed = True

            broadcast(state, slides, schedule_changed)

            state["last_yandex_check"] = now
            state_changed = True

        except Exception as e:
            print(f"Ошибка расписания: {e}")
            print("Старое расписание сохранено.")
    else:
        print(f"Пропуск скачивания с Яндекса (прошло {int(time_since_last_check)}с из {YANDEX_CHECK_INTERVAL}с)")

    # 4. Сохраняем состояние
    if state_changed:
        save_state(state, state_sha)
    else:
        print("Изменений состояния нет.")

    print("=" * 60)
    print("Готово.")
    print("=" * 60)


if __name__ == "__main__":
    main()
