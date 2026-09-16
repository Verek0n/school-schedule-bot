import base64
import hashlib
import json
import os
import re
import time
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

ADMIN_CHAT_ID = 1334717692
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
                mime_type = "image/jpeg" if upload_path.suffix == ".jpg" else "image/png"
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
        "one_time_keyboard": False,
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
                is_admin = user_key == str(ADMIN_CHAT_ID)

                # /broadcast или /объявление
                if is_admin and (text.startswith("/broadcast ") or text.startswith("/объявление ")):
                    parts = text.split(maxsplit=1)
                    if len(parts) < 2:
                        send_message(chat_id, "Использование: /broadcast <текст уведомления>")
                        continue

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
                                print(f"Удаляем пользователя {sub_id} (blocked).")
                                state["users"].pop(sub_id, None)
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
                        f"Рассылка завершена.\nУспешно: {success_count}\nОшибок/Удалено: {fail_count}",
                    )
                    continue

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

                if text == "/class":
                    send_message(chat_id, "Выберите класс:", class_keyboard())
                    continue

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

                if text == "/stats" and is_admin:
                    subscribers = state.get("users", {})
                    total = len(subscribers)
                    with_class = sum(1 for u in subscribers.values() if u.get("class"))
                    without_class = total - with_class
                    send_message(
                        chat_id,
                        "\n".join([
                            "Статистика бота",
                            "",
                            f"Всего пользователей: {total}",
                            f"С выбранным классом: {with_class}",
                            f"Без класса: {without_class}",
                        ]),
                    )
                    continue

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
                if e.response is not None and e.response.status_code in (403, 400):
                    print(f"Пользователь {chat_id} заблокировал бота. Удаляем.")
                    if user_key in state["users"]:
                        del state["users"][user_key]
                        changed = True
                else:
                    print(f"HTTP ошибка от {chat_id}: {e}")
            except Exception as e:
                print(f"Ошибка обработки {chat_id}: {e}")

    return changed


# ============================================================
# YANDEX PDF DOWNLOAD (FRAME-AWARE SEARCH)
# ============================================================

def download_pdf():
    target = WORK / "source.pdf"
    if target.exists():
        target.unlink()

    print("Скачивание PDF через браузер...")
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
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1920, "height": 1080},
        )

        page = context.new_page()

        def route_filter(route):
            url = route.request.url
            if any(x in url for x in ["mc.yandex.ru", "yandex.ru/clck", "metrika", "an.yandex.ru"]):
                return route.abort()
            return route.continue_()

        page.route("**/*", route_filter)

        try:
            page.goto(SOURCE_URL, wait_until="domcontentloaded", timeout=35000)
        except Exception as e:
            browser.close()
            raise RuntimeError(f"Не удалось открыть страницу Яндекса: {e}")

        def find_element_in_frames(pattern, exact=False, timeout_sec=20):
            start = time.time()
            while time.time() - start < timeout_sec:
                for root in [page] + list(page.frames):
                    try:
                        loc = root.get_by_text(pattern, exact=exact)
                        if loc.count() > 0 and loc.first.is_visible():
                            return loc.first
                    except Exception:
                        pass
                page.wait_for_timeout(400)
            return None

        # 1. Поиск меню "Файл" по всем фреймам
        print("Поиск меню 'Файл' во фреймах...")
        file_btn = find_element_in_frames(re.compile(r"^Файл$", re.I), timeout_sec=20)
        if not file_btn:
            file_btn = find_element_in_frames("Файл", timeout_sec=5)

        if not file_btn:
            browser.close()
            raise RuntimeError("Кнопка 'Файл' не найдена во фреймах редактора.")

        file_btn.click(force=True)
        page.wait_for_timeout(300)

        # 2. Поиск пункта "Скачать"
        print("Поиск пункта 'Скачать'...")
        download_menu = find_element_in_frames(re.compile(r"^Скачать$", re.I), timeout_sec=5)
        if not download_menu:
            download_menu = find_element_in_frames("Скачать", timeout_sec=5)

        if not download_menu:
            browser.close()
            raise RuntimeError("Пункт 'Скачать' не найден в меню.")

        try:
            download_menu.hover(timeout=2000)
        except Exception:
            pass
        try:
            download_menu.click(force=True, timeout=2000)
        except Exception:
            pass
        page.wait_for_timeout(300)

        # 3. Поиск пункта "Документ PDF" и скачивание
        print("Поиск формата 'PDF'...")
        pdf_item = find_element_in_frames(re.compile(r"Документ PDF|\.pdf", re.I), timeout_sec=5)
        if not pdf_item:
            pdf_item = find_element_in_frames("PDF", timeout_sec=5)

        if not pdf_item:
            browser.close()
            raise RuntimeError("Пункт 'Документ PDF' не найден.")

        print("Клик по 'PDF' и скачивание...")
        with page.expect_download(timeout=25000) as download_info:
            pdf_item.click(force=True)

        download = download_info.value
        download.save_as(target)
        browser.close()

    if not target.exists() or target.stat().st_size < 1000:
        raise RuntimeError("Скачанный PDF не существует или пуст.")

    print(f"PDF скачан за {time.time() - t0:.1f}с ({target.stat().st_size} байт).")
    return target


# ============================================================
# PDF -> 5 JPG (HIGH QUALITY 200 DPI)
# ============================================================

def make_schedule(pdf_path):
    print("Рендер PDF в JPG высокого качества (200 DPI)...")
    doc = pymupdf.open(pdf_path)
    slides = []

    try:
        page_count = len(doc)
        print(f"В PDF найдено страниц: {page_count}")

        if page_count < 5:
            raise RuntimeError(f"Ожидалось минимум 5 страниц, получено {page_count}.")

        # 200 DPI обеспечивает высокой четкости текст без размытий
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
# WARMUP CACHE
# ============================================================

def warmup_cache(state, slides):
    changed = False

    for slide in slides:
        idx = slide["number"] - 1
        old = state["latest"]["slides"][idx]

        if old.get("hash") == slide["hash"] and old.get("file_id"):
            continue

        try:
            result = send_photo(ADMIN_CHAT_ID, photo_path=slide["path"])
            message_id = result.get("message_id")
            photos = result.get("photo", [])

            if photos:
                state["latest"]["slides"][idx]["file_id"] = photos[-1]["file_id"]
                state["latest"]["slides"][idx]["hash"] = slide["hash"]
                changed = True
                print(f"Кэш: слайд {slide['number']} file_id сохранён.")
            else:
                print(f"Кэш: слайд {slide['number']} — file_id не получен.")

            if message_id:
                try:
                    delete_message(ADMIN_CHAT_ID, message_id)
                except Exception:
                    pass
        except Exception as e:
            print(f"Кэш: ошибка слайда {slide['number']}: {e}")

    return changed


def send_slide(chat_id, slide_number, slide_path, file_id=None, caption=None, keyboard=None):
    if file_id:
        return send_photo(chat_id, file_id=file_id, caption=caption, keyboard=keyboard)
    return send_photo(chat_id, photo_path=slide_path, caption=caption, keyboard=keyboard)


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
                keyboard=class_keyboard(),
            )
            user["sent"] = cached.get("hash")
            user["want_schedule"] = False
            changed = True
            print(f"Мгновенно из кэша: {user_key} ({selected_class})")
        except requests.HTTPError as e:
            if getattr(e.response, "status_code", None) in (403, 400):
                print(f"Удаляем {user_key}: бот заблокирован.")
                users.pop(user_key, None)
                changed = True
        except Exception as e:
            print(f"Ошибка мгновенной отправки {user_key}: {e}")

    return changed


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
            caption = (
                f"Доступно новое расписание «{selected_class}»!"
                if is_new_alert
                else f"Расписание «{selected_class}»"
            )

            send_slide(
                chat_id,
                slide_number,
                current_slide["path"],
                file_id=cached_file_id,
                caption=caption,
                keyboard=class_keyboard(),
            )

            user["sent"] = current_hash
            user["want_schedule"] = False
            print(
                f"Слайд {slide_number} отправлен {chat_id} (класс {selected_class})."
            )

        except requests.HTTPError as e:
            print(f"Ошибка Telegram для {user_key}: {e}")
            response = getattr(e, "response", None)
            if response is not None and response.status_code in (403, 400):
                print(f"Удаляем {user_key}: бот заблокирован.")
                users.pop(user_key, None)
        except Exception as e:
            print(f"Ошибка отправки {user_key}: {e}")


# ============================================================
# MAIN
# ============================================================

def main():
    print("=" * 60)
    print("School Schedule Bot")
    print("=" * 60)

    state, state_sha = load_state()

    print("Получение новых сообщений Telegram...")
    state_changed = process_commands(state)

    if fulfill_user_requests(state):
        state_changed = True

    now = time.time()
    time_since_last_check = now - state.get("last_yandex_check", 0)

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

    should_fetch_yandex = (
        time_since_last_check >= YANDEX_CHECK_INTERVAL
        or needs_yandex
    )

    if should_fetch_yandex:
        print(f"Идем проверять Яндекс (прошло {int(time_since_last_check)}с)...")
        schedule_changed = False

        try:
            pdf_path = download_pdf()
            slides = make_schedule(pdf_path)

            for slide in slides:
                idx = slide["number"] - 1
                if state["latest"]["slides"][idx].get("hash") != slide["hash"]:
                    schedule_changed = True

            print("Расписание изменилось." if schedule_changed else "Расписание не изменилось.")

            if warmup_cache(state, slides):
                state_changed = True

            broadcast(state, slides, schedule_changed)

            state["last_yandex_check"] = now
            state_changed = True

        except Exception as e:
            print(f"Ошибка расписания: {e}")
            print("Старое расписание сохранено.")
    else:
        print(
            f"Пропуск Яндекса (прошло {int(time_since_last_check)}с из {YANDEX_CHECK_INTERVAL}с)"
        )

    if state_changed:
        save_state(state, state_sha)
    else:
        print("Изменений состояния нет.")

    print("=" * 60)
    print("Готово.")
    print("=" * 60)


if __name__ == "__main__":
    main()
