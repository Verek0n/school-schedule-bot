import base64
import hashlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import requests
import fitz
from playwright.sync_api import sync_playwright


# ============================================================
# CONFIG
# ============================================================

SOURCE_URL = (
    "https://docs.yandex.ru/view/d/"
    "zQH249qvBK21O7SXl-9z_SPegnqahzm72s0qoIz-cKg6eG1uRGNFdE5adw"
)

BOT_TOKEN = os.environ["BOT_TOKEN"]

STATE_REPO = "Verek0n/school-schedule-state"
STATE_TOKEN = os.environ["STATE_TOKEN"]

STATE_URL = f"https://api.github.com/repos/{STATE_REPO}/contents/state.json"

TELEGRAM_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"


# ============================================================
# CLASS -> SLIDE
# ============================================================

CLASS_TO_SLIDE = {
    # Slide 1
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

    # Slide 2
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

    # Slide 3
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

    # Slide 4
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

    # Slide 5
    "11Б": 4,
}


CLASSES = list(CLASS_TO_SLIDE.keys())


# ============================================================
# TELEGRAM
# ============================================================

def telegram(method, data=None):
    url = f"{TELEGRAM_URL}/{method}"

    response = requests.post(
        url,
        json=data or {},
        timeout=60,
    )

    response.raise_for_status()

    result = response.json()

    if not result.get("ok"):
        raise RuntimeError(
            f"Telegram API error: {result}"
        )

    return result["result"]


def send_message(chat_id, text, reply_markup=None):
    data = {
        "chat_id": chat_id,
        "text": text,
    }

    if reply_markup:
        data["reply_markup"] = reply_markup

    return telegram("sendMessage", data)


def send_photo(chat_id, photo, caption=None):
    data = {
        "chat_id": chat_id,
        "photo": photo,
    }

    if caption:
        data["caption"] = caption

    return telegram("sendPhoto", data)


def answer_callback(callback_id):
    try:
        telegram(
            "answerCallbackQuery",
            {
                "callback_query_id": callback_id,
            },
        )
    except Exception as e:
        print(f"Не удалось ответить на callback: {e}")


# ============================================================
# KEYBOARD
# ============================================================

def class_keyboard():
    keyboard = []

    row = []

    for class_name in CLASSES:
        # ВАЖНО:
        # теперь кнопка отправляет обычный текст,
        # а не callback_query.
        row.append(
            {
                "text": class_name,
            }
        )

        if len(row) == 5:
            keyboard.append(row)
            row = []

    if row:
        keyboard.append(row)

    return {
        "keyboard": keyboard,
        "resize_keyboard": True,
        "one_time_keyboard": False,
    }


def remove_keyboard():
    return {
        "remove_keyboard": True,
    }


# ============================================================
# STATE
# ============================================================

def default_state():
    return {
        "offset": 0,
        "subscribers": {},
        "latest": {
            "slides": []
        },
    }


def state_headers():
    return {
        "Authorization": f"Bearer {STATE_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def load_state():
    response = requests.get(
        STATE_URL,
        headers=state_headers(),
        timeout=30,
    )

    if response.status_code == 404:
        print("state.json ещё не существует.")
        return default_state(), None

    response.raise_for_status()

    data = response.json()

    content = base64.b64decode(
        data["content"]
    ).decode("utf-8")

    state = json.loads(content)

    return state, data["sha"]


def save_state(state, sha):
    content = json.dumps(
        state,
        ensure_ascii=False,
        indent=2,
    )

    encoded = base64.b64encode(
        content.encode("utf-8")
    ).decode("ascii")

    data = {
        "message": "Update bot state",
        "content": encoded,
    }

    if sha:
        data["sha"] = sha

    response = requests.put(
        STATE_URL,
        headers=state_headers(),
        json=data,
        timeout=30,
    )

    response.raise_for_status()

    return response.json()["content"]["sha"]


# ============================================================
# DOWNLOAD DOCX
# ============================================================

def download_docx():
    temp_dir = tempfile.mkdtemp(prefix="schedule_")

    download_dir = Path(temp_dir)

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True
            )

            page = browser.new_page(
                accept_downloads=True
            )

            page.goto(
                SOURCE_URL,
                wait_until="domcontentloaded",
                timeout=120000,
            )

            page.wait_for_timeout(5000)

            download = None

            # Пытаемся найти кнопку/ссылку скачивания
            selectors = [
                'a[download]',
                'button:has-text("Скачать")',
                'text=Скачать',
                '[aria-label*="Скачать"]',
            ]

            for selector in selectors:
                try:
                    locator = page.locator(selector)

                    if locator.count() == 0:
                        continue

                    with page.expect_download(
                        timeout=15000
                    ) as download_info:
                        locator.first.click()

                    download = download_info.value
                    break

                except Exception:
                    continue

            if download is None:
                # Дополнительная попытка через ссылки
                links = page.locator("a")

                for i in range(min(links.count(), 100)):
                    try:
                        link = links.nth(i)

                        text = (
                            link.inner_text()
                            .strip()
                            .lower()
                        )

                        href = link.get_attribute("href")

                        if (
                            "скач" in text
                            or "download" in text
                        ):
                            with page.expect_download(
                                timeout=15000
                            ) as download_info:
                                link.click()

                            download = (
                                download_info.value
                            )
                            break

                    except Exception:
                        continue

            if download is None:
                browser.close()
                raise RuntimeError(
                    "Не удалось скачать DOCX."
                )

            file_path = (
                download_dir
                / "schedule.docx"
            )

            download.save_as(
                str(file_path)
            )

            browser.close()

            return file_path, temp_dir

    except Exception:
        shutil.rmtree(
            temp_dir,
            ignore_errors=True
        )
        raise


# ============================================================
# DOCX -> PNG
# ============================================================

def convert_to_images(docx_path, temp_dir):
    output_dir = (
        Path(temp_dir) / "converted"
    )

    output_dir.mkdir(
        parents=True,
        exist_ok=True
    )

    subprocess.run(
        [
            "libreoffice",
            "--headless",
            "--convert-to",
            "pdf",
            "--outdir",
            str(output_dir),
            str(docx_path),
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=120,
    )

    pdf_path = (
        output_dir
        / f"{docx_path.stem}.pdf"
    )

    if not pdf_path.exists():
        raise RuntimeError(
            "LibreOffice не создал PDF."
        )

    pdf = fitz.open(str(pdf_path))

    images = []

    try:
        for index, page in enumerate(pdf):
            if index >= 5:
                break

            pix = page.get_pixmap(
                matrix=fitz.Matrix(2, 2),
                alpha=False,
            )

            image_path = (
                output_dir
                / f"slide_{index + 1}.png"
            )

            pix.save(str(image_path))

            images.append(image_path)

    finally:
        pdf.close()

    if len(images) == 0:
        raise RuntimeError(
            "PDF не содержит страниц."
        )

    return images


# ============================================================
# IMAGE HASH
# ============================================================

def file_hash(path):
    sha = hashlib.sha256()

    with open(path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)

            if not chunk:
                break

            sha.update(chunk)

    return sha.hexdigest()


# ============================================================
# TELEGRAM FILE ID
# ============================================================

def upload_photo(chat_id, image_path, caption=None):
    url = f"{TELEGRAM_URL}/sendPhoto"

    with open(image_path, "rb") as photo:
        files = {
            "photo": (
                image_path.name,
                photo,
                "image/png",
            )
        }

        data = {
            "chat_id": chat_id,
        }

        if caption:
            data["caption"] = caption

        response = requests.post(
            url,
            data=data,
            files=files,
            timeout=120,
        )

    response.raise_for_status()

    result = response.json()

    if not result.get("ok"):
        raise RuntimeError(
            f"Telegram API error: {result}"
        )

    message = result["result"]

    photo_sizes = message.get("photo", [])

    if not photo_sizes:
        raise RuntimeError(
            "Telegram не вернул photo."
        )

    return photo_sizes[-1]["file_id"]


# ============================================================
# SCHEDULE PROCESSING
# ============================================================

def build_latest_slides(image_paths):
    slides = []

    for image_path in image_paths:
        slides.append(
            {
                "hash": file_hash(image_path),
                "file_id": None,
            }
        )

    return slides


def send_slide(
    chat_id,
    slide_index,
    class_name,
    image_paths,
    latest_slides,
    caption,
):
    slide = latest_slides[slide_index]

    file_id = slide.get("file_id")

    if file_id:
        try:
            send_photo(
                chat_id,
                file_id,
                caption=caption,
            )

            return

        except Exception as e:
            print(
                f"Не удалось отправить file_id: {e}"
            )

    image_path = image_paths[slide_index]

    new_file_id = upload_photo(
        chat_id,
        image_path,
        caption=caption,
    )

    slide["file_id"] = new_file_id


# ============================================================
# CLASS SELECTION
# ============================================================

def handle_class_selection(
    chat_id,
    class_name,
    state,
):
    if class_name not in CLASS_TO_SLIDE:
        return False

    chat_key = str(chat_id)

    subscriber = state["subscribers"].get(
        chat_key,
        {}
    )

    subscriber["class"] = class_name

    # Важно:
    # sent сбрасываем, чтобы следующий запуск
    # обязательно отправил расписание выбранного класса.
    subscriber["sent"] = None

    subscriber["want_schedule"] = True

    state["subscribers"][chat_key] = subscriber

    send_message(
        chat_id,
        f'Класс выбран: "{class_name}".\n'
        "Расписание будет отправлено при ближайшем запуске бота.",
    )

    return True


# ============================================================
# COMMANDS / MESSAGES
# ============================================================

def process_message(message, state):
    chat = message.get("chat")

    if not chat:
        return

    chat_id = chat["id"]

    text = message.get("text", "").strip()

    if not text:
        return

    chat_key = str(chat_id)

    # --------------------------------------------------------
    # /start
    # --------------------------------------------------------

    if text == "/start":
        subscriber = state["subscribers"].get(
            chat_key
        )

        if subscriber and subscriber.get("class"):
            class_name = subscriber["class"]

            send_message(
                chat_id,
                f'Бот уже настроен на класс "{class_name}".\n\n'
                "Команды:\n"
                "/class — выбрать класс\n"
                "/schedule — получить расписание\n"
                "/stop — отключить рассылку",
            )

        else:
            send_message(
                chat_id,
                "Выберите свой класс:",
                reply_markup=class_keyboard(),
            )

        return

    # --------------------------------------------------------
    # /class
    # --------------------------------------------------------

    if text == "/class":
        send_message(
            chat_id,
            "Выберите свой класс:",
            reply_markup=class_keyboard(),
        )

        return

    # --------------------------------------------------------
    # /schedule
    # --------------------------------------------------------

    if text == "/schedule":
        subscriber = state["subscribers"].get(
            chat_key
        )

        if not subscriber or not subscriber.get(
            "class"
        ):
            send_message(
                chat_id,
                "Сначала выберите класс:",
                reply_markup=class_keyboard(),
            )

            return

        subscriber["want_schedule"] = True

        send_message(
            chat_id,
            "Расписание будет отправлено при ближайшем запуске.",
        )

        return

    # --------------------------------------------------------
    # /stop
    # --------------------------------------------------------

    if text == "/stop":
        if chat_key in state["subscribers"]:
            del state["subscribers"][chat_key]

        send_message(
            chat_id,
            "Рассылка отключена.",
            reply_markup=remove_keyboard(),
        )

        return

    # --------------------------------------------------------
    # CLASS BUTTON
    # --------------------------------------------------------

    if text in CLASS_TO_SLIDE:
        handle_class_selection(
            chat_id,
            text,
            state,
        )

        return


# ============================================================
# GET UPDATES
# ============================================================

def get_updates(offset):
    return telegram(
        "getUpdates",
        {
            "offset": offset,
            "timeout": 1,
            "allowed_updates": [
                "message",
            ],
        },
    )


# ============================================================
# MAIN
# ============================================================

def main():
    state, state_sha = load_state()

    if "subscribers" not in state:
        state["subscribers"] = {}

    if "latest" not in state:
        state["latest"] = {
            "slides": []
        }

    offset = state.get("offset", 0)

    print("Получение новых сообщений Telegram...")

    updates = get_updates(offset)

    changed = False

    for update in updates:
        update_id = update["update_id"]

        state["offset"] = update_id + 1
        offset = update_id + 1

        changed = True

        message = update.get("message")

        if message:
            try:
                process_message(
                    message,
                    state,
                )

            except Exception as e:
                print(
                    f"Ошибка обработки сообщения: {e}"
                )

    # --------------------------------------------------------
    # Скачиваем актуальное расписание
    # --------------------------------------------------------

    temp_dir = None

    try:
        print("Скачивание расписания...")

        docx_path, temp_dir = download_docx()

        print(
            f"DOCX скачан: {docx_path}"
        )

        image_paths = convert_to_images(
            docx_path,
            temp_dir,
        )

        print(
            f"Получено страниц: {len(image_paths)}"
        )

        new_slides = build_latest_slides(
            image_paths
        )

        old_slides = state["latest"].get(
            "slides",
            []
        )

        # ----------------------------------------------------
        # Определяем, изменилось ли расписание
        # ----------------------------------------------------

        schedule_changed = (
            len(old_slides) != len(new_slides)
            or any(
                old_slides[i].get("hash")
                != new_slides[i].get("hash")
                for i in range(
                    min(
                        len(old_slides),
                        len(new_slides),
                    )
                )
            )
        )

        # Сохраняем старые file_id,
        # если соответствующая страница не изменилась.
        for i in range(len(new_slides)):
            if (
                i < len(old_slides)
                and old_slides[i].get("hash")
                == new_slides[i].get("hash")
            ):
                new_slides[i]["file_id"] = (
                    old_slides[i].get("file_id")
                )

        state["latest"]["slides"] = new_slides

        # ----------------------------------------------------
        # Отправка расписания пользователям
        # ----------------------------------------------------

        for chat_key, subscriber in list(
            state["subscribers"].items()
        ):
            class_name = subscriber.get("class")

            if class_name not in CLASS_TO_SLIDE:
                continue

            slide_index = CLASS_TO_SLIDE[
                class_name
            ]

            if slide_index >= len(new_slides):
                continue

            want_schedule = subscriber.get(
                "want_schedule",
                False,
            )

            previous_sent = subscriber.get(
                "sent"
            )

            current_hash = new_slides[
                slide_index
            ]["hash"]

            # Отправляем:
            #
            # 1. если пользователь только выбрал класс;
            # 2. если попросил /schedule;
            # 3. если расписание изменилось и
            #    он уже получал предыдущую версию.

            should_send = (
                want_schedule
                or (
                    schedule_changed
                    and previous_sent
                    and previous_sent
                    != current_hash
                )
            )

            if not should_send:
                continue

            if want_schedule:
                caption = (
                    f'Расписание "{class_name}"'
                )
            else:
                caption = (
                    f'Доступно новое расписание '
                    f'"{class_name}"!'
                )

            try:
                send_slide(
                    int(chat_key),
                    slide_index,
                    class_name,
                    image_paths,
                    new_slides,
                    caption,
                )

                subscriber["sent"] = current_hash
                subscriber["want_schedule"] = False

                changed = True

                print(
                    f"Отправлено расписание "
                    f"{class_name} -> {chat_key}"
                )

            except Exception as e:
                print(
                    f"Ошибка отправки "
                    f"{class_name} -> {chat_key}: {e}"
                )

    except Exception as e:
        print(
            f"Ошибка расписания: {e}"
        )

        # Старое расписание не удаляем.
        print(
            "Старое расписание сохранено."
        )

    finally:
        if temp_dir:
            shutil.rmtree(
                temp_dir,
                ignore_errors=True
            )

    # --------------------------------------------------------
    # Сохраняем состояние
    # --------------------------------------------------------

    if changed:
        print("Сохранение состояния...")

        try:
            save_state(
                state,
                state_sha,
            )

            print(
                "Состояние успешно сохранено."
            )

        except Exception as e:
            print(
                f"Ошибка сохранения состояния: {e}"
            )
            raise

    else:
        print(
            "Изменений состояния нет."
        )


if __name__ == "__main__":
    main()
