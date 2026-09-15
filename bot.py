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
    "5А": 1, "5Б": 1, "5В": 1, "5Г": 1,
    "5Д": 1, "5Е": 1, "5Ж": 1, "5З": 1,
    "6А": 1, "6Б": 1,

    # Слайд 2
    "6В": 2, "6Г": 2, "6Д": 2, "6Е": 2,
    "6Ж": 2, "6З": 2, "7А": 2, "7Б": 2,
    "7В": 2, "7Г": 2,

    # Слайд 3
    "7Д": 3, "7Е": 3, "7Ж": 3, "7И": 3,
    "8А": 3, "8Б": 3, "8В": 3, "8Г": 3,
    "8Д": 3, "8Ж": 3,

    # Слайд 4
    "8З": 4, "9А": 4, "9Б": 4, "9В": 4,
    "9Г": 4, "9Д": 4, "9Е": 4, "10А": 4,
    "10Б": 4, "11А": 4,

    # Слайд 5
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
                    retry_after = response.json().get("parameters", {}).get("retry_after", 5)
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
            time.sleep(2)

    raise RuntimeError("Telegram API failed")


def send_message(chat_id, text, keyboard=None):
    data = {
        "chat_id": chat_id,
        "text": text,
    }
    if keyboard is not None:
        data["reply_markup"] = json.dumps(keyboard, ensure_ascii=False)
    return telegram("sendMessage", data)


def send_photo(chat_id, photo_path=None, file_id=None, caption=None):
    data = {"chat_id": chat_id}
    if caption:
        data["caption"] = caption

    if file_id:
        data["photo"] = file_id
        return telegram("sendPhoto", data)

    if photo_path:
        return telegram("sendPhoto", data, upload=photo_path)

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


REMOVE_KEYBOARD = {"remove_keyboard": True}


# ============================================================
# STATE
# ============================================================

def default_state():
    return {
        "offset": 0,
        "users": {},
        "latest": {
            "slides": [{"hash": None, "file_id": None} for _ in range(5)]
        },
    }


def load_state():
    try:
        response = requests.get(STATE_URL, headers=GH_HEADERS, timeout=30)
        if response.status_code == 404:
            print("state.json ещё не существует.")
            return default_state(), None

        response.raise_for_status()
        payload = response.json()

        content = base64.b64decode(payload["content"]).decode("utf-8")
        state = json.loads(content)

        state.setdefault("offset", 0)
        state.setdefault("users", {})
        state.setdefault("latest", {})

        if "slides" not in state["latest"]:
            state["latest"]["slides"] = [{"hash": None, "file_id": None} for _ in range(5)]

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

    response = requests.put(STATE_URL, headers=GH_HEADERS, json=data, timeout=30)
    response.raise_for_status()

    print("state.json сохранён.")
    return response.json()["content"]["sha"]


# ============================================================
# TELEGRAM UPDATES
# ============================================================

def process_commands(state):
    changed = False

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

            print(f"Получено сообщение от {chat_id}: {text}")
            user_key = str(chat_id)

            if text == "/start":
                if user_key not in state["users"]:
                    state["users"][user_key] = {"class": None, "sent": None, "want_schedule": False}
                    changed = True

                welcome_msg = (
                    "Добро пожаловать в бота для получения расписания школы №9!\n\n"
                    "Бот обновляется раз в 5 минут.\n"
                )
                current_class = state["users"][user_key].get("class")
                if current_class:
                    send_message(chat_id, welcome_msg + f"Текущий класс: {current_class}", class_keyboard())
                else:
                    send_message(chat_id, welcome_msg + "Выберите свой класс на клавиатуре:", class_keyboard())
                continue

            if text == "/stop":
                if user_key in state["users"]:
                    del state["users"][user_key]
                    changed = True
                send_message(chat_id, "Вы отписались от рассылки.", REMOVE_KEYBOARD)
                continue

            if text == "/class":
                send_message(chat_id, "Выберите класс:", class_keyboard())
                continue

            if text == "/schedule":
                if user_key not in state["users"]:
                    state["users"][user_key] = {"class": None, "sent": None, "want_schedule": False}
                    changed = True

                user = state["users"][user_key]
                if not user.get("class"):
                    send_message(chat_id, "Сначала выберите класс:", class_keyboard())
                else:
                    user["want_schedule"] = True
                    changed = True
                    send_message(chat_id, "Запрос принят! Расписание будет отправлено в течение 5 минут.")
                continue

            if text == "/stats":
                if user_key == "1334717692":
                    subs = state.get("users", {})
                    total = len(subs)
                    with_class = sum(1 for u in subs.values() if u.get("class"))
                    send_message(chat_id, f"Всего: {total}\nС классом: {with_class}\nБез класса: {total - with_class}")
                continue

            if text in CLASS_TO_SLIDE:
                if user_key not in state["users"]:
                    state["users"][user_key] = {"class": None, "sent": None, "want_schedule": False}

                user = state["users"][user_key]
                user["class"] = text
                user["sent"] = None
                user["want_schedule"] = True
                changed = True

                send_message(chat_id, f"Класс выбран: «{text}»\nРасписание отправляется...", class_keyboard())
                continue

            send_message(
                chat_id,
                "Команды:\n/schedule - Получить расписание\n/class - Изменить класс\n/start - Подписка\n/stop - Отписка",
                class_keyboard(),
            )

    return changed


# ============================================================
# YANDEX PDF DOWNLOAD (DIRECT MENU CLICK)
# ============================================================

def _click_first_visible(page, candidates, timeout=3000):
    for cand in candidates:
        try:
            loc = page.get_by_text(cand, exact=False) if isinstance(cand, str) else cand
            count = loc.count()
            for i in range(count):
                item = loc.nth(i)
                if item.is_visible():
                    item.click(timeout=timeout)
                    return True
        except Exception:
            continue
    return False

def download_pdf():
    """
    Скачивает сразу PDF из Яндекс.Документов:
    Файл → Скачать → Документ PDF (.pdf)
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
                viewport={"width": 1600, "height": 1100},
            )

            page = context.new_page()
            page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

            print("Открываем страницу расписания...")
            page.goto(SOURCE_URL, wait_until="domcontentloaded", timeout=90000)
            page.wait_for_timeout(8000)

            try:
                page.get_by_text("Файл", exact=True).first.wait_for(state="visible", timeout=30000)
            except Exception:
                print("Кнопка «Файл» не появилась сразу, продолжаем...")

            download_ok = False

            for attempt in range(5):
                print(f"Попытка скачать через меню ({attempt + 1}/5)...")
                try:
                    # 1. Открываем «Файл»
                    if not _click_first_visible(page, [
                        page.get_by_role("button", name=re.compile(r"^Файл$", re.I)),
                        page.get_by_text("Файл", exact=True)
                    ]):
                        page.wait_for_timeout(2000)
                        continue

                    page.wait_for_timeout(1000)

                    # 2. Ищем и наводим на «Скачать»
                    download_opened = False
                    for cand in [page.get_by_role("menuitem", name=re.compile(r"Скачать", re.I)), page.get_by_text("Скачать", exact=False)]:
                        try:
                            for i in range(cand.count()):
                                item = cand.nth(i)
                                if item.is_visible():
                                    item.hover(timeout=2000)
                                    page.wait_for_timeout(800)
                                    try: item.click(timeout=2000)
                                    except Exception: pass
                                    download_opened = True
                                    break
                            if download_opened: break
                        except Exception: pass

                    if not download_opened:
                        page.mouse.click(10, 10)
                        page.wait_for_timeout(1500)
                        continue

                    page.wait_for_timeout(1000)

                    # 3. Кликаем «Документ PDF (.pdf)»
                    pdf_candidates = [
                        page.get_by_role("menuitem", name=re.compile(r"Документ PDF|\.pdf|PDF", re.I)),
                        page.get_by_text("Документ PDF (.pdf)", exact=False),
                        page.get_by_text(".pdf", exact=False),
                    ]

                    clicked_pdf = False
                    with page.expect_download(timeout=30000) as download_info:
                        for cand in pdf_candidates:
                            try:
                                for i in range(cand.count()):
                                    item = cand.nth(i)
                                    if item.is_visible():
                                        item.click(timeout=3000)
                                        clicked_pdf = True
                                        break
                                if clicked_pdf: break
                            except Exception: pass

                        if not clicked_pdf:
                            raise RuntimeError("Пункт Документ PDF (.pdf) не найден")

                    download = download_info.value
                    if download.failure():
                        raise RuntimeError(f"Ошибка скачивания: {download.failure()}")

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
                    page.wait_for_timeout(500)
                    page.mouse.click(10, 10)
                except Exception:
                    pass
                page.wait_for_timeout(2000)

            if not download_ok or not target.exists():
                try:
                    page.screenshot(path=str(WORK / "download-error.png"), full_page=True)
                except Exception: pass
                raise RuntimeError("Не удалось скачать PDF.")

        finally:
            if browser is not None:
                browser.close()

    # Базовая проверка
    if target.stat().st_size < 1000:
        raise RuntimeError("Скачанный PDF слишком мал (возможно, пустой).")

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
        print(f"В PDF найдено страниц: {page_count}")

        if page_count < 5:
            raise RuntimeError(f"Ожидалось минимум 5 страниц, получено {page_count}.")

        for slide_number in range(1, 6):
            page_index = slide_number - 1
            page = doc[page_index]

            # Увеличиваем разрешение для четкости (DPI ~ 150)
            matrix = pymupdf.Matrix(150 / 72, 150 / 72)
            pix = page.get_pixmap(matrix=matrix, alpha=False, colorspace=pymupdf.csRGB)

            png_path = WORK / f"slide_{slide_number}.png"
            pix.save(str(png_path))

            image_bytes = png_path.read_bytes()
            image_hash = hashlib.sha256(image_bytes).hexdigest()

            slides.append({
                "number": slide_number,
                "path": png_path,
                "hash": image_hash,
            })

            print(f"Слайд {slide_number} создан: hash={image_hash[:12]}")

        return slides
    finally:
        doc.close()


# ============================================================
# BROADCAST
# ============================================================

def send_slide(chat_id, slide_number, slide_path, file_id=None, caption=None):
    if file_id:
        return send_photo(chat_id, file_id=file_id, caption=caption)
    else:
        return send_photo(chat_id, photo_path=slide_path, caption=caption)


def broadcast(state, slides, schedule_changed):
    users = state["users"]
    old_slides = state["latest"]["slides"]

    for slide in slides:
        index = slide["number"] - 1
        old = old_slides[index]
        if old.get("hash") != slide["hash"]:
            old["hash"] = slide["hash"]
            old["file_id"] = None

    for user_key, user in list(users.items()):
        try:
            chat_id = int(user_key)
            selected_class = user.get("class")

            if not selected_class:
                continue

            slide_number = CLASS_TO_SLIDE.get(selected_class)
            if not slide_number:
                continue

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
            caption = f"Доступно новое расписание «{selected_class}»!" if is_new_alert else f"Расписание «{selected_class}»"

            if cached_file_id:
                send_slide(chat_id, slide_number, current_slide["path"], file_id=cached_file_id, caption=caption)
            else:
                result = send_slide(chat_id, slide_number, current_slide["path"], file_id=None, caption=caption)
                try:
                    photo = result.get("photo", [])
                    if photo:
                        state["latest"]["slides"][index]["file_id"] = photo[-1]["file_id"]
                except Exception as e:
                    print(f"Не удалось сохранить file_id: {e}")

            user["sent"] = current_hash
            user["want_schedule"] = False

            print(f"Отправлено пользователю {chat_id} (класс {selected_class}).")

        except requests.HTTPError as e:
            if getattr(e, "response", None) and e.response.status_code == 403:
                print(f"Пользователь {user_key} заблокировал бота. Удаляем.")
                users.pop(user_key, None)
        except Exception as e:
            print(f"Ошибка отправки пользователю {user_key}: {e}")


# ============================================================
# MAIN
# ============================================================

def main():
    print("=" * 60)
    print("School Schedule Bot (Direct PDF Mode)")
    print("=" * 60)

    state, state_sha = load_state()
    state_changed = process_commands(state)

    slides = None
    schedule_changed = False

    try:
        pdf_path = download_pdf()
        slides = make_schedule(pdf_path)

        old_slides = state["latest"]["slides"]
        for slide in slides:
            if old_slides[slide["number"] - 1].get("hash") != slide["hash"]:
                schedule_changed = True

        print("Расписание изменилось." if schedule_changed else "Расписание не изменилось.")

    except Exception as e:
        print(f"Ошибка расписания: {e}")
        slides = []
        for i in range(5):
            slides.append({
                "number": i + 1,
                "path": None,
                "hash": state["latest"]["slides"][i].get("hash"),
            })

    if slides:
        users = state["users"]
        for user_key, user in list(users.items()):
            selected_class = user.get("class")
            if not selected_class: continue

            slide_number = CLASS_TO_SLIDE.get(selected_class)
            if not slide_number: continue

            index = slide_number - 1
            current_slide = slides[index]

            if current_slide["path"] is None:
                if not user.get("want_schedule"): continue

                old_file_id = state["latest"]["slides"][index].get("file_id")
                old_hash = state["latest"]["slides"][index].get("hash")

                if not old_file_id: continue

                try:
                    send_slide(int(user_key), slide_number, None, file_id=old_file_id, caption=f"Расписание «{selected_class}»")
                    user["sent"] = old_hash
                    user["want_schedule"] = False
                    state_changed = True
                except Exception as e:
                    print(f"Ошибка отправки старого расписания {user_key}: {e}")

        if any(slide["path"] is not None for slide in slides):
            broadcast(state, slides, schedule_changed)
            state_changed = True

    if state_changed:
        save_state(state, state_sha)
    else:
        print("Изменений состояния нет.")

    print("Готово.")

if __name__ == "__main__":
    main()
