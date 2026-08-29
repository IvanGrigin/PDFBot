import asyncio
import os
import io
import re
import shutil
import subprocess
import zipfile
import tempfile
from dataclasses import dataclass
from typing import List, Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import (
    BotCommand,
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    FSInputFile
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.filters import Command, CommandStart

from dotenv import load_dotenv
from PIL import Image, ImageOps
from pypdf import PdfReader, PdfWriter

# --- Optional HEIC/HEIF support ---
HEIF_ENABLED = False
try:
    import pillow_heif  # pip install pillow-heif
    pillow_heif.register_heif_opener()
    HEIF_ENABLED = True
except Exception:
    HEIF_ENABLED = False


ROOT = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(ROOT, ".env"))
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

router = Router()

# ====== SETTINGS ======
MAX_PHOTOS = 15
MAX_PDF_DOWNLOAD_MB = int(os.getenv("MAX_PDF_DOWNLOAD_MB", "20"))
MAX_PDF_DOWNLOAD_BYTES = MAX_PDF_DOWNLOAD_MB * 1024 * 1024
SPLIT_SEPARATE_MAX_PAGES = 30
MIN_FREE_BYTES = int(os.getenv("MIN_FREE_GB", "5")) * 1024**3
MAX_OUTPUT_BYTES = 49 * 1024 * 1024
PDF_COMPRESS_TIMEOUT_SECONDS = int(os.getenv("PDF_COMPRESS_TIMEOUT_SECONDS", "180"))
PAGE_PORTRAIT = (1240, 1754)
PAGE_MARGIN = 56

ALLOWED_IMAGE_EXT = {
    ".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".gif", ".heic", ".heif"
}

# ====== UI ======
def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🖼 Создать PDF", callback_data="menu_makepdf")],
        [InlineKeyboardButton(text="🗜 Сжать PDF", callback_data="menu_compresspdf")],
        [InlineKeyboardButton(text="✏️ Переименовать PDF", callback_data="menu_renamepdf")],
        [InlineKeyboardButton(text="✂️ Разделить PDF", callback_data="menu_splitpdf")],
    ])

def makepdf_controls_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Фото больше нет — создать PDF", callback_data="makepdf_done")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")],
    ])

def cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")]
    ])

def split_mode_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗜 ZIP со всеми страницами", callback_data="split_zip")],
        [InlineKeyboardButton(text="📄 Отдельные PDF — до 30 страниц", callback_data="split_sep")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")],
    ])

# ====== FSM ======
class Flow(StatesGroup):
    makepdf_collect = State()
    makepdf_ask_name = State()

    rename_wait_pdf = State()
    rename_ask_name = State()

    split_wait_pdf = State()
    split_choose_mode = State()

    compress_wait_pdf = State()

@dataclass
class SessionData:
    # for makepdf
    image_file_ids: List[str]
    # for rename/split
    pdf_file_id: Optional[str]
    pdf_bytes: Optional[bytes]
    original_filename: Optional[str]

def _new_session() -> SessionData:
    return SessionData(image_file_ids=[], pdf_file_id=None, pdf_bytes=None, original_filename=None)

# ====== Helpers ======
async def _download_telegram_file(bot: Bot, file_id: str) -> bytes:
    """
    Downloads a Telegram file into memory.
    """
    f = await bot.get_file(file_id)
    bio = io.BytesIO()
    await bot.download_file(f.file_path, destination=bio)
    return bio.getvalue()

def _image_to_page(image: Image.Image) -> Image.Image:
    """Вписать изображение в страницу без обрезки и с учётом EXIF-поворота."""
    oriented = ImageOps.exif_transpose(image)
    page_size = PAGE_PORTRAIT if oriented.height >= oriented.width else PAGE_PORTRAIT[::-1]
    content_size = (page_size[0] - 2 * PAGE_MARGIN, page_size[1] - 2 * PAGE_MARGIN)
    fitted = ImageOps.contain(oriented.convert("RGB"), content_size, Image.Resampling.LANCZOS)
    page = Image.new("RGB", page_size, "white")
    page.paste(fitted, ((page.width - fitted.width) // 2, (page.height - fitted.height) // 2))
    return page


def _images_to_pdf_bytes(images: List[Image.Image]) -> bytes:
    rgb = [_image_to_page(image) for image in images]

    if not rgb:
        raise ValueError("No images")

    out = io.BytesIO()
    first, rest = rgb[0], rgb[1:]
    first.save(out, format="PDF", save_all=True, append_images=rest, resolution=150)
    for page in rgb:
        page.close()
    return out.getvalue()


def _safe_pdf_name(value: str, fallback: str = "document") -> str:
    name = re.sub(r"\.pdf$", "", value.strip(), flags=re.IGNORECASE)
    name = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", name)
    return name[:100].strip(" ._") or fallback


def _compressed_pdf_name(original_filename: str | None) -> str:
    """Добавить запрошенный суффикс без удвоения расширения PDF."""
    stem = _safe_pdf_name(original_filename or "document", "document")
    if not stem.casefold().endswith("_compresed"):
        stem += "_compresed"
    return f"{stem}.pdf"


def _compress_pdf_file(source: str, target: str) -> None:
    """Сжать PDF через Ghostscript без shell и проверить созданный файл."""
    executable = shutil.which("gs")
    if not executable:
        raise RuntimeError("Ghostscript is not installed")
    command = [
        executable,
        "-sDEVICE=pdfwrite",
        "-dCompatibilityLevel=1.6",
        "-dPDFSETTINGS=/ebook",
        "-dNOPAUSE",
        "-dBATCH",
        "-dQUIET",
        "-dSAFER",
        "-dDetectDuplicateImages=true",
        "-dCompressFonts=true",
        "-dSubsetFonts=true",
        "-dDownsampleColorImages=true",
        "-dColorImageResolution=144",
        "-dDownsampleGrayImages=true",
        "-dGrayImageResolution=144",
        "-dDownsampleMonoImages=true",
        "-dMonoImageResolution=300",
        f"-sOutputFile={target}",
        source,
    ]
    subprocess.run(
        command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        timeout=PDF_COMPRESS_TIMEOUT_SECONDS,
    )
    if not os.path.isfile(target) or os.path.getsize(target) == 0:
        raise RuntimeError("Ghostscript created an empty file")
    PdfReader(target)


def _enough_disk() -> bool:
    return shutil.disk_usage(ROOT).free >= MIN_FREE_BYTES

def _is_image_document(message: Message) -> bool:
    doc = message.document
    if not doc:
        return False
    mime = (doc.mime_type or "").lower()
    name = (doc.file_name or "").lower()

    if mime.startswith("image/"):
        return True
    for ext in ALLOWED_IMAGE_EXT:
        if name.endswith(ext):
            return True
    return False

def _pdf_too_large(doc_size: Optional[int]) -> bool:
    return (doc_size or 0) > MAX_PDF_DOWNLOAD_BYTES

async def _show_menu(message: Message, text: str = "Выберите действие:"):
    await message.answer(text, reply_markup=main_menu_kb())

async def _show_menu_edit(call: CallbackQuery, text: str = "Выберите действие:"):
    # safer: send new message instead of edit (to avoid edit errors)
    await call.message.answer(text, reply_markup=main_menu_kb())

# ====== Common ======
@router.message(CommandStart())
@router.message(Command("help"))
async def start(message: Message, state: FSMContext):
    await state.clear()
    heic_note = "" if HEIF_ENABLED else "\nHEIC/HEIF: для поддержки установите pillow-heif."
    await message.answer(
        "📄 <b>Best PDF Robot</b>\n\n"
        "Создавайте PDF из фотографий без обрезки, переименовывайте файлы "
        "сжимайте и разделяйте документы на отдельные страницы.\n\n"
        "• До 15 изображений в одном PDF\n"
        f"• Обработка PDF до {MAX_PDF_DOWNLOAD_MB} МБ\n"
        "• Временные файлы удаляются после отправки\n"
        f"{heic_note}",
        reply_markup=main_menu_kb(), parse_mode="HTML"
    )

@router.callback_query(F.data == "cancel")
async def cancel(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.clear()
    await _show_menu_edit(call, "Отменено. Выберите действие:")

# ====== Menu callbacks ======
@router.callback_query(F.data == "menu_makepdf")
async def menu_makepdf(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.set_state(Flow.makepdf_collect)
    await state.update_data(sess=_new_session().__dict__)
    await call.message.answer(
        f"Отправляйте фото или изображения-документы (до {MAX_PHOTOS}).\n"
        "Когда закончите — нажмите кнопку ниже.",
        reply_markup=makepdf_controls_kb()
    )

@router.callback_query(F.data == "menu_renamepdf")
async def menu_renamepdf(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.set_state(Flow.rename_wait_pdf)
    await state.update_data(sess=_new_session().__dict__)
    await call.message.answer(
        "Пришлите PDF-файл для переименования.\n"
        f"Ограничение: до {MAX_PDF_DOWNLOAD_MB} МБ.",
        reply_markup=cancel_kb()
    )


@router.callback_query(F.data == "menu_compresspdf")
async def menu_compresspdf(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.set_state(Flow.compress_wait_pdf)
    await state.update_data(sess=_new_session().__dict__)
    await call.message.answer(
        "Пришлите PDF для сжатия. Изображения будут оптимизированы для просмотра на экране.\n"
        f"Ограничение: до {MAX_PDF_DOWNLOAD_MB} МБ.",
        reply_markup=cancel_kb(),
    )

@router.callback_query(F.data == "menu_splitpdf")
async def menu_splitpdf(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.set_state(Flow.split_wait_pdf)
    await state.update_data(sess=_new_session().__dict__)
    await call.message.answer(
        "Пришлите PDF-файл для разделения.\n"
        f"Ограничение: до {MAX_PDF_DOWNLOAD_MB} МБ.",
        reply_markup=cancel_kb()
    )

# ====== MAKE PDF: collect images ======
@router.message(Flow.makepdf_collect, F.photo)
async def makepdf_collect_photo(message: Message, state: FSMContext):
    data = await state.get_data()
    sess = SessionData(**data["sess"])

    if len(sess.image_file_ids) >= MAX_PHOTOS:
        await message.answer(
            f"Достигнут лимит {MAX_PHOTOS} изображений. Нажмите «Фото больше нет — создать PDF».",
            reply_markup=makepdf_controls_kb()
        )
        return

    file_id = message.photo[-1].file_id
    sess.image_file_ids.append(file_id)
    await state.update_data(sess=sess.__dict__)

    await message.answer(
        f"Принято: {len(sess.image_file_ids)}/{MAX_PHOTOS}.",
        reply_markup=makepdf_controls_kb()
    )

@router.message(Flow.makepdf_collect, F.document)
async def makepdf_collect_document_image(message: Message, state: FSMContext):
    if not _is_image_document(message):
        await message.answer(
            "Это не похоже на изображение. Пришлите картинку или нажмите «Фото больше нет — создать PDF».",
            reply_markup=makepdf_controls_kb()
        )
        return

    data = await state.get_data()
    sess = SessionData(**data["sess"])

    if len(sess.image_file_ids) >= MAX_PHOTOS:
        await message.answer(
            f"Достигнут лимит {MAX_PHOTOS} изображений. Нажмите «Фото больше нет — создать PDF».",
            reply_markup=makepdf_controls_kb()
        )
        return

    sess.image_file_ids.append(message.document.file_id)
    await state.update_data(sess=sess.__dict__)

    await message.answer(
        f"Принято: {len(sess.image_file_ids)}/{MAX_PHOTOS}.",
        reply_markup=makepdf_controls_kb()
    )

@router.callback_query(Flow.makepdf_collect, F.data == "makepdf_done")
async def makepdf_done(call: CallbackQuery, state: FSMContext):
    await call.answer()
    data = await state.get_data()
    sess = SessionData(**data["sess"])
    if not sess.image_file_ids:
        await call.message.answer(
            "Вы ещё не отправили ни одного изображения.",
            reply_markup=makepdf_controls_kb()
        )
        return

    await state.set_state(Flow.makepdf_ask_name)
    await call.message.answer(
        "Напишите название будущего файла (без .pdf в конце).",
        reply_markup=cancel_kb()
    )

@router.message(Flow.makepdf_ask_name, F.text)
async def makepdf_create_and_send(message: Message, state: FSMContext, bot: Bot):
    filename = _safe_pdf_name(message.text or "", "")
    if not filename:
        await message.answer("Название не должно быть пустым. Введите ещё раз.", reply_markup=cancel_kb())
        return

    data = await state.get_data()
    sess = SessionData(**data["sess"])

    if not _enough_disk():
        await message.answer("На сервере осталось меньше 5 ГБ. Новые файлы временно не принимаются.")
        await state.clear()
        return
    await message.answer("⏳ Собираю PDF без обрезки изображений…")

    images: List[Image.Image] = []
    try:
        for fid in sess.image_file_ids:
            raw = await _download_telegram_file(bot, fid)
            try:
                with Image.open(io.BytesIO(raw)) as im:
                    im.load()
                    images.append(im.copy())
            except Exception:
                # likely unsupported format (e.g. HEIC without plugin) or corrupted file
                if not HEIF_ENABLED:
                    await message.answer(
                        "Не удалось открыть одно из изображений. Если это HEIC/HEIF — установите pillow-heif:\n"
                        "pip install pillow-heif\n"
                        "И повторите отправку.",
                        reply_markup=main_menu_kb()
                    )
                else:
                    await message.answer(
                        "Не удалось открыть одно из изображений (формат не поддержан или файл повреждён).",
                        reply_markup=main_menu_kb()
                    )
                await state.clear()
                return

        pdf_bytes = _images_to_pdf_bytes(images)
        if len(pdf_bytes) > MAX_OUTPUT_BYTES:
            raise ValueError("result too large")
    except Exception:
        await message.answer(
            "Ошибка при создании PDF. Проверьте, что изображения корректные и попробуйте снова.",
            reply_markup=main_menu_kb()
        )
        await state.clear()
        return
    finally:
        for im in images:
            try:
                im.close()
            except Exception:
                pass

    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        tmp.write(pdf_bytes)
        tmp_path = tmp.name

    try:
        await message.answer_document(FSInputFile(tmp_path, filename=f"{filename}.pdf"))
        await _show_menu(message, "Готово. Что делаем дальше?")
    finally:
        try:
            os.remove(tmp_path)
        except Exception:
            pass
        await state.clear()

# ====== RENAME PDF ======
@router.message(Flow.rename_wait_pdf, F.document)
async def rename_receive_pdf(message: Message, state: FSMContext):
    doc = message.document
    name = (doc.file_name or "").lower()
    if not name.endswith(".pdf"):
        await message.answer("Это не PDF. Пришлите файл .pdf", reply_markup=cancel_kb())
        return

    if _pdf_too_large(doc.file_size):
        await message.answer(
            f"Файл слишком большой ({doc.file_size / (1024*1024):.1f} МБ).\n"
            f"Через стандартный Bot API бот обрабатывает PDF до {MAX_PDF_DOWNLOAD_MB} МБ.\n"
            "Сожмите PDF или используйте меньший файл.",
            reply_markup=main_menu_kb()
        )
        await state.clear()
        return

    data = await state.get_data()
    sess = SessionData(**data["sess"])
    sess.pdf_file_id = doc.file_id
    sess.original_filename = doc.file_name
    await state.update_data(sess=sess.__dict__)

    await state.set_state(Flow.rename_ask_name)
    await message.answer("Напишите новое имя (без .pdf).", reply_markup=cancel_kb())


# ====== COMPRESS PDF ======
@router.message(Flow.compress_wait_pdf, F.document)
async def compress_receive_pdf(message: Message, state: FSMContext, bot: Bot):
    doc = message.document
    name = (doc.file_name or "").lower()
    if not name.endswith(".pdf"):
        await message.answer("Это не PDF. Пришлите файл .pdf", reply_markup=cancel_kb())
        return
    if _pdf_too_large(doc.file_size):
        await message.answer(
            f"Файл слишком большой ({(doc.file_size or 0) / (1024*1024):.1f} МБ).\n"
            f"Сейчас бот обрабатывает PDF до {MAX_PDF_DOWNLOAD_MB} МБ.",
            reply_markup=main_menu_kb(),
        )
        await state.clear()
        return
    if not _enough_disk():
        await message.answer("На сервере осталось меньше 5 ГБ. Новые файлы временно не принимаются.")
        await state.clear()
        return

    await message.answer("⏳ Скачиваю и сжимаю PDF…")
    try:
        raw = await _download_telegram_file(bot, doc.file_id)
        with tempfile.TemporaryDirectory(prefix="pdf-compress-") as temp_dir:
            source = os.path.join(temp_dir, "source.pdf")
            compressed = os.path.join(temp_dir, "compressed.pdf")
            with open(source, "wb") as stream:
                stream.write(raw)
            await asyncio.to_thread(_compress_pdf_file, source, compressed)
            result = compressed if os.path.getsize(compressed) < len(raw) else source
            result_size = os.path.getsize(result)
            if result_size > MAX_OUTPUT_BYTES:
                raise ValueError("result too large")
            await message.answer_document(
                FSInputFile(result, filename=_compressed_pdf_name(doc.file_name))
            )
        saved = max(0, len(raw) - result_size)
        if saved:
            percent = saved * 100 / len(raw)
            status = f"Готово: файл уменьшен на {percent:.0f}%."
        else:
            status = "Готово. Этот PDF уже был хорошо оптимизирован, поэтому меньшей версии не получилось."
        await _show_menu(message, status)
    except subprocess.TimeoutExpired:
        await message.answer(
            "Сжатие заняло слишком много времени. Попробуйте PDF меньшего размера.",
            reply_markup=main_menu_kb(),
        )
    except Exception:
        await message.answer(
            "Не удалось сжать PDF. Возможно, файл повреждён или защищён паролем.",
            reply_markup=main_menu_kb(),
        )
    finally:
        await state.clear()

@router.message(Flow.rename_ask_name, F.text)
async def rename_send(message: Message, state: FSMContext, bot: Bot):
    new_name = _safe_pdf_name(message.text or "", "")
    if not new_name:
        await message.answer("Имя не должно быть пустым. Введите ещё раз.", reply_markup=cancel_kb())
        return
    if not _enough_disk():
        await message.answer("На сервере осталось меньше 5 ГБ. Новые файлы временно не принимаются.")
        await state.clear()
        return

    data = await state.get_data()
    sess = SessionData(**data["sess"])

    await message.answer("Скачиваю PDF и переименовываю…")

    try:
        raw = await _download_telegram_file(bot, sess.pdf_file_id)
    except Exception:
        await message.answer(
            "Не удалось скачать PDF (ошибка Telegram/сети). Попробуйте ещё раз.",
            reply_markup=main_menu_kb()
        )
        await state.clear()
        return

    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        tmp.write(raw)
        tmp_path = tmp.name

    try:
        await message.answer_document(FSInputFile(tmp_path, filename=f"{new_name}.pdf"))
        await _show_menu(message, "Готово. Что делаем дальше?")
    finally:
        try:
            os.remove(tmp_path)
        except Exception:
            pass
        await state.clear()

# ====== SPLIT PDF ======
@router.message(Flow.split_wait_pdf, F.document)
async def split_receive_pdf(message: Message, state: FSMContext, bot: Bot):
    doc = message.document
    name = (doc.file_name or "").lower()
    if not name.endswith(".pdf"):
        await message.answer("Это не PDF. Пришлите файл .pdf", reply_markup=cancel_kb())
        return
    if not _enough_disk():
        await message.answer("На сервере осталось меньше 5 ГБ. Новые файлы временно не принимаются.")
        await state.clear()
        return

    if _pdf_too_large(doc.file_size):
        await message.answer(
            f"Файл слишком большой ({doc.file_size / (1024*1024):.1f} МБ).\n"
            f"Через стандартный Bot API бот обрабатывает PDF до {MAX_PDF_DOWNLOAD_MB} МБ.\n"
            "Сожмите PDF или используйте меньший файл.",
            reply_markup=main_menu_kb()
        )
        await state.clear()
        return

    await message.answer("Скачиваю PDF…")

    try:
        raw = await _download_telegram_file(bot, doc.file_id)
    except Exception:
        await message.answer(
            "Не удалось скачать PDF (ошибка Telegram/сети). Попробуйте ещё раз.",
            reply_markup=main_menu_kb()
        )
        await state.clear()
        return

    data = await state.get_data()
    sess = SessionData(**data["sess"])
    sess.pdf_file_id = doc.file_id
    sess.original_filename = doc.file_name
    sess.pdf_bytes = raw
    await state.update_data(sess=sess.__dict__)

    await state.set_state(Flow.split_choose_mode)
    await message.answer("Как выдавать страницы?", reply_markup=split_mode_kb())

@router.callback_query(Flow.split_choose_mode, F.data.in_({"split_zip", "split_sep"}))
async def split_do(call: CallbackQuery, state: FSMContext):
    await call.answer()
    mode = call.data

    data = await state.get_data()
    sess = SessionData(**data["sess"])
    raw = sess.pdf_bytes or b""

    try:
        reader = PdfReader(io.BytesIO(raw))
        n = len(reader.pages)
    except Exception:
        await call.message.answer(
            "Не удалось прочитать PDF (возможно, файл повреждён или защищён паролем).",
            reply_markup=main_menu_kb()
        )
        await state.clear()
        return

    if n <= 0:
        await call.message.answer("В PDF нет страниц.", reply_markup=main_menu_kb())
        await state.clear()
        return

    if mode == "split_sep" and n > SPLIT_SEPARATE_MAX_PAGES:
        await call.message.answer(
            f"В PDF {n} страниц. Отдельно бот отправляет максимум {SPLIT_SEPARATE_MAX_PAGES}, "
            "чтобы не заспамить чат. Выберите ZIP.",
            reply_markup=split_mode_kb()
        )
        return

    if mode == "split_sep":
        await call.message.answer(f"Страниц: {n}. Отправляю отдельными PDF…")
        for i in range(n):
            w = PdfWriter()
            w.add_page(reader.pages[i])
            out = io.BytesIO()
            w.write(out)

            with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
                tmp.write(out.getvalue())
                tmp_path = tmp.name

            try:
                await call.message.answer_document(FSInputFile(tmp_path, filename=f"page_{i+1}.pdf"))
            finally:
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass

        await call.message.answer("Готово.", reply_markup=main_menu_kb())
        await state.clear()
        return

    # ZIP mode
    await call.message.answer(f"Страниц: {n}. Собираю ZIP…")

    zip_buf = io.BytesIO()
    try:
        with zipfile.ZipFile(zip_buf, "w", compression=zipfile.ZIP_DEFLATED) as z:
            for i in range(n):
                w = PdfWriter()
                w.add_page(reader.pages[i])
                out = io.BytesIO()
                w.write(out)
                z.writestr(f"page_{i+1}.pdf", out.getvalue())
    except Exception:
        await call.message.answer("Ошибка при сборке ZIP.", reply_markup=main_menu_kb())
        await state.clear()
        return

    with tempfile.NamedTemporaryFile(delete=False, suffix=".zip") as tmp:
        tmp.write(zip_buf.getvalue())
        tmp_path = tmp.name

    try:
        await call.message.answer_document(FSInputFile(tmp_path, filename="split_pages.zip"))
        await call.message.answer("Готово.", reply_markup=main_menu_kb())
    finally:
        try:
            os.remove(tmp_path)
        except Exception:
            pass
        await state.clear()

# ====== Fallbacks / nicer errors ======
@router.message(Flow.makepdf_collect)
async def makepdf_other(message: Message):
    await message.answer(
        "Пожалуйста, отправьте изображение (photo) или картинку как документ.\n"
        "Либо нажмите «Фото больше нет — создать PDF».",
        reply_markup=makepdf_controls_kb()
    )

@router.message(Flow.rename_wait_pdf)
async def rename_other(message: Message):
    await message.answer("Жду PDF-документ.", reply_markup=cancel_kb())

@router.message(Flow.split_wait_pdf)
async def split_other(message: Message):
    await message.answer("Жду PDF-документ.", reply_markup=cancel_kb())


@router.message(Flow.compress_wait_pdf)
async def compress_other(message: Message):
    await message.answer("Жду PDF-документ.", reply_markup=cancel_kb())

async def main():
    if not BOT_TOKEN:
        raise RuntimeError("Заполните BOT_TOKEN в .env")
    bot = Bot(BOT_TOKEN)
    await bot.set_my_name("Best PDF Robot")
    await bot.set_my_short_description(
        "Создание и сжатие PDF, переименование и разделение документов.")
    await bot.set_my_description(
        "Создаёт PDF из фотографий без обрезки, сжимает, переименовывает и разделяет документы.")
    await bot.set_my_commands([
        BotCommand(command="start", description="Открыть главное меню"),
        BotCommand(command="help", description="Показать возможности бота"),
    ])
    dp = Dispatcher()
    dp.include_router(router)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
