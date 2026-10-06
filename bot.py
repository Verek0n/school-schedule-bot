import base64
import hashlib
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pymupdf
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ============================================================
# CONFIG
# ============================================================

# PDF теперь лежит на сайте школы и скачивается напрямую, без браузера.
# Число в конце ссылки (?1790944044) — метка времени для обхода кэша,
# поэтому при скачивании подставляем текущее время.
SOURCE_URL = "https://school9kirov.gosuslugi.ru/netcat_files/30/69/ZAMENY.pdf"

BOT_TOKEN = os.environ["BOT_TOKEN"]
STATE_TOKEN = os.environ["STATE_TOKEN"]

# В чат админа отправляем фото для получения file_id, затем удаляем
ADMIN_CHAT_ID = 1334717692

# Интервал проверки сайта с расписанием: 1800 секунд = 30 минут
SOURCE_CHECK_INTERVAL = 1800

# Часовой пояс Москвы (UTC+3)
MSK_TZ = timezone(timedelta(hours=3))

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
                files = {
                    "photo": (
                        upload_path.name,
                        upload_path.open("rb"),
                        "image/jpeg",
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

        except Exception as e:
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
        "one_time_keyboard": True,  # Клавиатура скроется после одного нажатия
    }

REMOVE_KEYBOARD = {"remove_keyboard": True}


# ============================================================
# STATE
# ============================================================

def default_state():
    return {
        "offset": 0,
        "last_source_check": 0,
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

        # Миграция со старого state.json: раньше ключ назывался last_yandex_check
        if "last_source_check" not in state:
            state["last_source_check"] = state.pop("last_yandex_check", 0)
        else:
            state.pop("last_yandex_check", None)

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
                # АДМИН-РАССЫЛКА: /broadcast <текст> или /объявление <текст>
                is_admin = (user_key == str(ADMIN_CHAT_ID))
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
                                time.sleep(0.05)  # небольшая задержка от лимитов спама
                            except requests.HTTPError as e:
                                if e.response is not None and e.response.status_code in (403, 400):
                                    print(f"Удаляем пользователя {sub_id} (заблокировал бота во время рассылки).")
                                    if sub_id in state["users"]:
                                        del state["users"][sub_id]
                                        changed = True
                                    fail_count += 1
                                else:
                                    print(f"Не удалось отправить пользователю {sub_id}: {e}")
                                    fail_count += 1
                            except Exception as e:
                                print(f"Ошибка отправки пользователю {sub_id}: {e}")
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
                    if user_key == str(ADMIN_CHAT_ID):
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

                # КНОПКА ВЫБОРА КЛАССА
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

                # НЕИЗВЕСТНАЯ КОМАНДА
                send_message(
                    chat_id,
                    (
                        "Я умею только выполнять команды:\n"
                        "/schedule - Получить расписание\n"
                        "/class - Изменить свой класс\n"
                        "/start - Подписаться на расписание\n"
                        "/stop - Отключить рассылку"
                    ),
                    class_keyboard(),
                )

            except requests.HTTPError as e:
                # Если пользователь заблокировал бота, удаляем его из БД
                if e.response is not None and e.response.status_code in (403, 400):
                    print(f"Пользователь {chat_id} заблокировал бота (403/400). Удаляем из состояния.")
                    if user_key in state["users"]:
                        del state["users"][user_key]
                        changed = True
                else:
                    print(f"HTTP ошибка при обработке сообщения от {chat_id}: {e}")
            except Exception as e:
                print(f"Ошибка при обработке сообщения от {chat_id}: {e}")

    return changed


# ============================================================
# PDF DOWNLOAD (ПРЯМАЯ ССЫЛКА, БЕЗ БРАУЗЕРА)
# ============================================================

def download_pdf():
    target = WORK / "source.pdf"
    if target.exists():
        target.unlink()

    print("Скачивание расписания в PDF напрямую с сайта школы...")

    # Метка времени в конце ссылки обходит кэш (как ?1790944044 на сайте)
    url = f"{SOURCE_URL}?{int(time.time())}"

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "application/pdf,*/*",
        "Referer": "https://school9kirov.gosuslugi.ru/",
    }

    last_error = None

    for attempt in range(5):
        try:
            with session.get(url, headers=headers, timeout=(10, 60), stream=True) as response:
                response.raise_for_status()
                with target.open("wb") as f:
                    for chunk in response.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)

            data = target.read_bytes()
            if len(data) < 1000:
                raise RuntimeError("Скачанный PDF слишком мал.")
            if not data.startswith(b"%PDF"):
                raise RuntimeError("Скачанный файл не является PDF.")

            print(f"PDF скачан: {target} ({len(data)} байт)")
            print("PDF успешно проверен.")
            return target

        except Exception as e:
            last_error = e
            print(f"Попытка скачивания {attempt + 1}/5 не удалась: {e}")
            if target.exists():
                target.unlink()
            time.sleep(2)

    raise RuntimeError(f"Не удалось скачать PDF: {last_error}")


# ============================================================
# PDF -> 5 JPG (ВЫСОКОЕ КАЧЕСТВО)
# ============================================================

def make_schedule(pdf_path):
    print("Рендер PDF в высококачественный JPG (300 DPI)...")
    doc = pymupdf.open(pdf_path)
    slides = []

    try:
        page_count = len(doc)
        print(f"В PDF найдено страниц: {page_count}")

        if page_count < 5:
            raise RuntimeError(f"Ожидалось минимум 5 страниц, получено {page_count}.")

        # 300 DPI обеспечивает отличную читаемость шрифтов
        matrix = pymupdf.Matrix(300 / 72, 300 / 72)

        for slide_number in range(1, 6):
            page = doc[slide_number - 1]
            pix = page.get_pixmap(
                matrix=matrix,
                alpha=False,
                colorspace=pymupdf.csRGB,
            )

            jpg_path = WORK / f"slide_{slide_number}.jpg"
            # Сохранение в JPG с качеством 95%
            pix.save(str(jpg_path), output="jpg", jpg_quality=95)

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
    """
    Для каждого слайда, у которого hash изменился (или file_id пуст),
    отправляет JPG в чат админа, получает file_id, удаляет сообщение.
    """
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
                delete_message(ADMIN_CHAT_ID, message_id)

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
            # Отправляем расписание и убираем кнопки клавиатуры
            send_slide(
                int(user_key),
                slide_num,
                None,
                file_id=file_id,
                caption=f"Расписание «{selected_class}»",
                keyboard=REMOVE_KEYBOARD
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

            # При любой отправке расписания прячем кнопки
            if cached_file_id:
                send_slide(
                    chat_id,
                    slide_number,
                    current_slide["path"],
                    file_id=cached_file_id,
                    caption=caption,
                    keyboard=REMOVE_KEYBOARD,
                )
            else:
                send_slide(
                    chat_id,
                    slide_number,
                    current_slide["path"],
                    file_id=None,
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

    # 2. Быстро отвечаем из имеющегося кэша
    if fulfill_user_requests(state):
        state_changed = True

    # 3. Решаем, идти ли на сайт за новым расписанием
    now = time.time()
    time_since_last_check = now - state.get("last_source_check", 0)

    # Проверяем, есть ли запросы на слайды, которых вообще нет в кэше
    needs_source = False
    for user in state["users"].values():
        if user.get("want_schedule"):
            cls = user.get("class")
            if cls and cls in CLASS_TO_SLIDE:
                idx = CLASS_TO_SLIDE[cls] - 1
                if not state["latest"]["slides"][idx].get("file_id"):
                    needs_source = True
                    break
            else:
                user["want_schedule"] = False
                state_changed = True

    # Вычисляем текущее московское время (UTC+3)
    msk_now = datetime.now(MSK_TZ)
    # Проверяем диапазон: строго от 12:00 до 00:00 (час >= 12)
    is_active_hours = (12 <= msk_now.hour < 24)

    # СТРОГИЕ ПРАВИЛА:
    # Скачивание происходит ТОЛЬКО в активные часы (12:00 - 00:00 МСК) И:
    # либо прошло 30 минут (1800 сек), либо слайда вообще нет в кэше.
    should_fetch_source = is_active_hours and (
        time_since_last_check >= SOURCE_CHECK_INTERVAL or needs_source
    )

    if should_fetch_source:
        print(f"Идем проверять источник расписания (МСК: {msk_now.strftime('%H:%M:%S')}, прошло {int(time_since_last_check)}с)...")
        schedule_changed = False

        try:
            pdf_path = download_pdf()
            slides = make_schedule(pdf_path)

            # Проверяем, изменилось ли расписание
            for slide in slides:
                idx = slide["number"] - 1
                if state["latest"]["slides"][idx].get("hash") != slide["hash"]:
                    schedule_changed = True

            if schedule_changed:
                print("Расписание изменилось.")
            else:
                print("Расписание не изменилось.")

            # Загружаем все 5 слайдов в кэш Telegram (в высоком JPG)
            if warmup_cache(state, slides):
                state_changed = True

            # Рассылаем тем, кому нужно новое расписание
            broadcast(state, slides, schedule_changed)

            state["last_source_check"] = now
            state_changed = True

        except Exception as e:
            print(f"Ошибка расписания: {e}")
            print("Старое расписание сохранено.")
    else:
        if not is_active_hours:
            reason = f"не входит в диапазон 12:00-00:00 МСК ({msk_now.strftime('%H:%M')})"
        else:
            reason = f"прошло всего {int(time_since_last_check)}с из требуемых {SOURCE_CHECK_INTERVAL}с"
        print(f"Пропуск скачивания расписания ({reason})")

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
