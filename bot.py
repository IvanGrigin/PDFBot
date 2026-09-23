import asyncio
import os
import io
import re
import shutil
import subprocess
import zipfile
import tempfile
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import (
    BotCommand,
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    FSInputFile, BufferedInputFile, InputMediaPhoto
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
PDF_PREVIEW_MAX_PAGES = 30
PDF_PREVIEW_DPI = 40
PDF_PREVIEW_TIMEOUT_SECONDS = int(os.getenv("PDF_PREVIEW_TIMEOUT_SECONDS", "60"))
PREVIEW_ALBUM_SIZE = 10  # лимит Telegram на фото в одном альбоме
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
        [InlineKeyboardButton(text="🔁 Заменить/вставить страницы", callback_data="menu_editpages")],
        [InlineKeyboardButton(text="📐 Выровнять ширину страниц", callback_data="menu_alignpdf")],
        [InlineKeyboardButton(text="🔲 Обрезать страницы", callback_data="menu_croppages")],
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

def editpages_controls_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Фото больше нет — продолжить", callback_data="editpages_done")],
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

    editpages_wait_pdf = State()
    editpages_collect = State()
    editpages_ask_ops = State()

    align_wait_pdf = State()

    crop_wait_pdf = State()
    crop_ask_ops = State()

@dataclass
class SessionData:
    # for makepdf
    image_file_ids: List[str]
    # for rename/split
    pdf_file_id: Optional[str]
    pdf_bytes: Optional[bytes]
    original_filename: Optional[str]
    # for editpages
    edit_file_ids: List[str] = field(default_factory=list)
    page_count: Optional[int] = None

def _new_session() -> SessionData:
    return SessionData(
        image_file_ids=[], pdf_file_id=None, pdf_bytes=None, original_filename=None,
        edit_file_ids=[], page_count=None,
    )

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


def _edited_pdf_name(original_filename: str | None) -> str:
    stem = _safe_pdf_name(original_filename or "document", "document")
    return f"{stem}_edited.pdf"


_EDIT_OP_RE = re.compile(r"(\d+)\s*([=+])\s*(\d+)")

def _edit_ops_help(page_count: int, photo_count: int) -> str:
    return (
        "Формат — по одной операции в строке или через запятую:\n"
        "• 3=1 — заменить страницу 3 на фото 1\n"
        "• 5=2 — заменить страницу 5 на фото 2\n"
        "• 2+3 — вставить фото 3 после страницы 2\n"
        "• 0+4 — вставить фото 4 в начало документа\n"
        f"• {page_count}+5 — вставить фото 5 в конец документа\n\n"
        "Все номера страниц — из исходного PDF, фото нумеруются в порядке получения, "
        "поэтому замены и вставки можно сочетать как угодно."
    )

def _parse_edit_ops(text: str, page_count: int, photo_count: int) -> List[Tuple[str, int, int]]:
    """Разобрать операции «страница=фото» (замена) и «страница+фото» (вставка после страницы)."""
    ops = [
        (match.group(2), int(match.group(1)), int(match.group(3)))
        for match in _EDIT_OP_RE.finditer(text or "")
    ]
    if not ops:
        raise ValueError("Не нашёл ни одной операции. Пример: 2=1 — заменить страницу 2 на фото 1.")
    for kind, page, photo in ops:
        if kind == "=" and not 1 <= page <= page_count:
            raise ValueError(f"Страницы {page} нет: в PDF страницы от 1 до {page_count}.")
        if kind == "+" and not 0 <= page <= page_count:
            raise ValueError(
                f"Для вставки номер страницы должен быть от 0 (начало документа) до {page_count} (конец)."
            )
        if not 1 <= photo <= photo_count:
            raise ValueError(f"Фото номер {photo} не найдено: вы прислали {photo_count}.")
    return ops

def _build_edited_pdf(raw: bytes, images: List[Image.Image], ops: List[Tuple[str, int, int]]) -> bytes:
    """Заменить и вставить страницы. Номера в ops — по исходному PDF, фото считаются с 1."""
    reader = PdfReader(io.BytesIO(raw))
    items: List = list(reader.pages)

    # Замены не сдвигают страницы, применяем по порядку — последняя операция по странице решает.
    for kind, page, photo in ops:
        if kind == "=":
            items[page - 1] = ("img", photo)

    # Вставки применяем с конца документа, чтобы номера не сдвигались;
    # при равной позиции раньше пишем более позднюю по порядку, сохраняя порядок пользователя.
    inserts = [
        (page, photo, order)
        for order, (kind, page, photo) in enumerate(ops)
        if kind == "+"
    ]
    for page, photo, order in sorted(inserts, key=lambda item: (-item[0], -item[2])):
        items.insert(page, ("img", photo))

    writer = PdfWriter()
    photo_pages: dict[int, object] = {}
    for item in items:
        if isinstance(item, tuple):
            _, photo_index = item
            if photo_index not in photo_pages:
                single = PdfReader(io.BytesIO(_images_to_pdf_bytes([images[photo_index - 1]])))
                photo_pages[photo_index] = single.pages[0]
            writer.add_page(photo_pages[photo_index])
        else:
            writer.add_page(item)

    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def _suffixed_pdf_name(original_filename: str | None, suffix: str) -> str:
    stem = _safe_pdf_name(original_filename or "document", "document")
    return f"{stem}_{suffix}.pdf"


def _align_pdf_widths(raw: bytes) -> Tuple[bytes, int, float]:
    """Масштабировать все страницы к медианной ширине, сохраняя пропорции.

    Возвращает (pdf_bytes, сколько страниц изменилось, медианная ширина).
    Ширина учитывается визуальная: у страниц с /Rotate 90/270 она swaps с высотой.
    """
    reader = PdfReader(io.BytesIO(raw))
    if not reader.pages:
        raise ValueError("no pages")

    def visual_width(page) -> float:
        width, height = float(page.mediabox.width), float(page.mediabox.height)
        rotation = int(page.get("/Rotate", 0) or 0)
        return height if rotation % 180 else width

    widths = sorted(visual_width(page) for page in reader.pages)
    count = len(widths)
    median = widths[count // 2] if count % 2 else (widths[count // 2 - 1] + widths[count // 2]) / 2

    changed = 0
    writer = PdfWriter()
    for page in reader.pages:
        width = visual_width(page)
        if width > 0 and abs(width - median) > 0.1:
            page.scale_by(median / width)
            changed += 1
        writer.add_page(page)

    out = io.BytesIO()
    writer.write(out)
    return out.getvalue(), changed, median


_CROP_OP_RE = re.compile(r"^\s*(?P<pages>\d+\s*(?:-\s*\d+)?|все|all)\s*=\s*(?P<rest>.+?)\s*$", re.IGNORECASE)
_CROP_NUM_RE = re.compile(r"\d+(?:[.,]\d+)?")

def _crop_ops_help(page_count: int) -> str:
    return (
        "Формат — по одной операции в строке:\n"
        "• 2 = 10 0 10 0 — у страницы 2 отрезать 10% сверху, 0% снизу, 10% слева, 0% справа\n"
        "• 1-5 = 15 15 0 0 — то же для страниц с 1 по 5\n"
        "• все = 10 10 10 10 — то же для всех страниц\n\n"
        "Четыре числа — проценты страницы: сверху, снизу, слева, справа. "
        f"Страниц в PDF: {page_count}."
    )

def _parse_crop_ops(text: str, page_count: int) -> List[Tuple[int, int, float, float, float, float]]:
    """Разобрать команды «страницы = верх низ лево право» (проценты на отрезание)."""
    lines = [line for line in re.split(r"[\n;]+", text or "") if line.strip()]
    if not lines:
        raise ValueError("Не нашёл ни одной операции. Пример: все = 10 10 10 10.")

    ops: List[Tuple[int, int, float, float, float, float]] = []
    for line in lines:
        match = _CROP_OP_RE.match(line)
        if not match:
            raise ValueError(
                f"Не понял строку «{line.strip()}». Формат: страницы = верх низ лево право."
            )
        pages = match.group("pages").lower().replace(" ", "")
        if pages in ("все", "all"):
            start, end = 1, page_count
        elif "-" in pages:
            first, last = pages.split("-")
            start, end = int(first), int(last)
            if start > end:
                start, end = end, start
        else:
            start = end = int(pages)
        for page in (start, end):
            if not 1 <= page <= page_count:
                raise ValueError(f"Страницы {page} нет: в PDF страницы от 1 до {page_count}.")

        rest = match.group("rest")
        numbers = [float(value.replace(",", ".")) for value in _CROP_NUM_RE.findall(rest)]
        leftover = _CROP_NUM_RE.sub(" ", rest)
        if leftover.strip(" .,;"):
            raise ValueError(
                f"Не понял часть после «=» в строке «{line.strip()}». Нужно ровно 4 числа: "
                "сверху, снизу, слева, справа (проценты)."
            )
        if len(numbers) != 4:
            raise ValueError(
                f"В строке «{line.strip()}» нужно 4 числа (сверху, снизу, слева, справа), "
                f"а найдено {len(numbers)}."
            )
        top, bottom, left, right = numbers
        if not all(0 <= value <= 95 for value in (top, bottom, left, right)):
            raise ValueError("Поля задаются процентами от 0 до 95.")
        if top + bottom >= 100 or left + right >= 100:
            raise ValueError("Сумма полей сверху+снизу и слева+справа должна быть меньше 100%.")
        ops.append((start, end, top, bottom, left, right))
    return ops

def _crop_pdf_pages(raw: bytes, ops: List[Tuple[int, int, float, float, float, float]]) -> Tuple[bytes, int]:
    """Обрезать поля страниц по плану. Возвращает (pdf_bytes, сколько страниц обрезано)."""
    reader = PdfReader(io.BytesIO(raw))
    if not reader.pages:
        raise ValueError("no pages")

    plan: dict[int, Tuple[float, float, float, float]] = {}
    for start, end, top, bottom, left, right in ops:
        for index in range(start - 1, end):
            plan[index] = (top, bottom, left, right)  # последняя строка по странице решает

    writer = PdfWriter()
    for index, page in enumerate(reader.pages):
        if index in plan:
            top, bottom, left, right = plan[index]
            x0, y0 = float(page.mediabox.left), float(page.mediabox.bottom)
            x1, y1 = float(page.mediabox.right), float(page.mediabox.top)
            new_x0 = x0 + left / 100 * (x1 - x0)
            new_y0 = y0 + bottom / 100 * (y1 - y0)
            new_x1 = x1 - right / 100 * (x1 - x0)
            new_y1 = y1 - top / 100 * (y1 - y0)
            page.mediabox.lower_left = (new_x0, new_y0)
            page.mediabox.upper_right = (new_x1, new_y1)
            page.cropbox.lower_left = (new_x0, new_y0)
            page.cropbox.upper_right = (new_x1, new_y1)
        writer.add_page(page)

    out = io.BytesIO()
    writer.write(out)
    return out.getvalue(), len(plan)


def _render_pdf_previews(source_path: str, output_dir: str, max_pages: int) -> List[Tuple[int, bytes]]:
    """Отрендерить первые страницы PDF в PNG-миниатюры через Ghostscript."""
    executable = shutil.which("gs")
    if not executable:
        raise RuntimeError("Ghostscript is not installed")
    prefix = os.path.join(output_dir, "page")
    command = [
        executable,
        "-dNOPAUSE", "-dBATCH", "-dQUIET", "-dSAFER",
        "-sDEVICE=png16m",
        f"-r{PDF_PREVIEW_DPI}",
        "-dFirstPage=1",
        f"-dLastPage={max_pages}",
        f"-sOutputFile={prefix}-%02d.png",
        source_path,
    ]
    subprocess.run(
        command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        timeout=PDF_PREVIEW_TIMEOUT_SECONDS,
    )
    previews: List[Tuple[int, bytes]] = []
    for number in range(1, max_pages + 1):
        path = f"{prefix}-{number:02d}.png"
        if not os.path.isfile(path):
            break
        with open(path, "rb") as stream:
            previews.append((number, stream.read()))
    if not previews:
        raise RuntimeError("Ghostscript produced no preview images")
    return previews


async def _send_crop_previews(message: Message, raw: bytes, page_count: int) -> str:
    """Прислать миниатюры страниц альбомами. Возвращает примечание, если показаны не все."""
    with tempfile.TemporaryDirectory(prefix="pdf-preview-") as temp_dir:
        source = os.path.join(temp_dir, "source.pdf")
        with open(source, "wb") as stream:
            stream.write(raw)
        previews = await asyncio.to_thread(
            _render_pdf_previews, source, temp_dir, PDF_PREVIEW_MAX_PAGES
        )

    await message.answer(f"🖼 Предпросмотр страниц 1–{len(previews)}:")
    for start in range(0, len(previews), PREVIEW_ALBUM_SIZE):
        chunk = previews[start:start + PREVIEW_ALBUM_SIZE]
        media = [
            InputMediaPhoto(
                media=BufferedInputFile(data, filename=f"page_{number:02d}.png"),
                caption=(
                    f"Страницы {start + 1}–{start + len(chunk)} из {page_count}"
                    if index == 0 else None
                ),
            )
            for index, (number, data) in enumerate(chunk)
        ]
        await message.answer_media_group(media)

    if page_count > len(previews):
        return (
            f"\n\nПредпросмотр ограничен первыми {PDF_PREVIEW_MAX_PAGES} страницами "
            f"из {page_count}."
        )
    return ""


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
        "Создавайте PDF из фотографий без обрезки, переименовывайте файлы, "
        "сжимайте, разделяйте документы, заменяйте страницы фото, выравнивайте "
        "ширину страниц и обрезайте поля.\n\n"
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

@router.callback_query(F.data == "menu_editpages")
async def menu_editpages(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.set_state(Flow.editpages_wait_pdf)
    await state.update_data(sess=_new_session().__dict__)
    await call.message.answer(
        "Пришлите PDF, в котором нужно заменить страницы новыми фото "
        "или вставить новые страницы между старыми.\n"
        f"Ограничение: до {MAX_PDF_DOWNLOAD_MB} МБ.",
        reply_markup=cancel_kb()
    )

@router.callback_query(F.data == "menu_alignpdf")
async def menu_alignpdf(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.set_state(Flow.align_wait_pdf)
    await state.update_data(sess=_new_session().__dict__)
    await call.message.answer(
        "Пришлите PDF — все страницы будут масштабированы к медианной ширине "
        "(пропорции каждой страницы сохранятся, ширина станет одинаковой).\n"
        f"Ограничение: до {MAX_PDF_DOWNLOAD_MB} МБ.",
        reply_markup=cancel_kb()
    )

@router.callback_query(F.data == "menu_croppages")
async def menu_croppages(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.set_state(Flow.crop_wait_pdf)
    await state.update_data(sess=_new_session().__dict__)
    await call.message.answer(
        "Пришлите PDF — обрежу поля у выбранных страниц (содержимое останется, "
        "страница станет меньше).\n"
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

# ====== EDIT PAGES: replace / insert pages ======
@router.message(Flow.editpages_wait_pdf, F.document)
async def editpages_receive_pdf(message: Message, state: FSMContext, bot: Bot):
    doc = message.document
    name = (doc.file_name or "").lower()
    if not name.endswith(".pdf"):
        await message.answer("Это не PDF. Пришлите файл .pdf", reply_markup=cancel_kb())
        return
    if _pdf_too_large(doc.file_size):
        await message.answer(
            f"Файл слишком большой ({(doc.file_size or 0) / (1024*1024):.1f} МБ).\n"
            f"Сейчас бот обрабатывает PDF до {MAX_PDF_DOWNLOAD_MB} МБ.",
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

    try:
        page_count = len(PdfReader(io.BytesIO(raw)).pages)
    except Exception:
        await message.answer(
            "Не удалось прочитать PDF (возможно, файл повреждён или защищён паролем).",
            reply_markup=main_menu_kb()
        )
        await state.clear()
        return
    if page_count <= 0:
        await message.answer("В PDF нет страниц.", reply_markup=main_menu_kb())
        await state.clear()
        return

    data = await state.get_data()
    sess = SessionData(**data["sess"])
    sess.pdf_file_id = doc.file_id
    sess.original_filename = doc.file_name
    sess.page_count = page_count
    await state.update_data(sess=sess.__dict__)

    await state.set_state(Flow.editpages_collect)
    await message.answer(
        f"В PDF {page_count} страниц. Теперь отправьте новые фотографии (до {MAX_PHOTOS}).\n"
        "Порядок важен: фото нумеруются с 1. Когда закончите — нажмите кнопку ниже.",
        reply_markup=editpages_controls_kb()
    )

@router.message(Flow.editpages_collect, F.photo)
async def editpages_collect_photo(message: Message, state: FSMContext):
    data = await state.get_data()
    sess = SessionData(**data["sess"])

    if len(sess.edit_file_ids) >= MAX_PHOTOS:
        await message.answer(
            f"Достигнут лимит {MAX_PHOTOS} фотографий. Нажмите «Фото больше нет — продолжить».",
            reply_markup=editpages_controls_kb()
        )
        return

    sess.edit_file_ids.append(message.photo[-1].file_id)
    await state.update_data(sess=sess.__dict__)

    await message.answer(
        f"Фото принято: {len(sess.edit_file_ids)}/{MAX_PHOTOS}.",
        reply_markup=editpages_controls_kb()
    )

@router.message(Flow.editpages_collect, F.document)
async def editpages_collect_document_image(message: Message, state: FSMContext):
    if not _is_image_document(message):
        await message.answer(
            "Это не похоже на изображение. Пришлите картинку или нажмите «Фото больше нет — продолжить».",
            reply_markup=editpages_controls_kb()
        )
        return

    data = await state.get_data()
    sess = SessionData(**data["sess"])

    if len(sess.edit_file_ids) >= MAX_PHOTOS:
        await message.answer(
            f"Достигнут лимит {MAX_PHOTOS} фотографий. Нажмите «Фото больше нет — продолжить».",
            reply_markup=editpages_controls_kb()
        )
        return

    sess.edit_file_ids.append(message.document.file_id)
    await state.update_data(sess=sess.__dict__)

    await message.answer(
        f"Фото принято: {len(sess.edit_file_ids)}/{MAX_PHOTOS}.",
        reply_markup=editpages_controls_kb()
    )

@router.callback_query(Flow.editpages_collect, F.data == "editpages_done")
async def editpages_done(call: CallbackQuery, state: FSMContext):
    await call.answer()
    data = await state.get_data()
    sess = SessionData(**data["sess"])
    if not sess.edit_file_ids:
        await call.message.answer(
            "Вы ещё не отправили ни одной фотографии.",
            reply_markup=editpages_controls_kb()
        )
        return

    page_count = sess.page_count or 0
    photo_count = len(sess.edit_file_ids)
    await state.set_state(Flow.editpages_ask_ops)
    await call.message.answer(
        f"В PDF {page_count} страниц, фото принято: {photo_count}.\n\n"
        f"Напишите одним сообщением, что сделать.\n\n{_edit_ops_help(page_count, photo_count)}",
        reply_markup=cancel_kb()
    )

@router.message(Flow.editpages_ask_ops, F.text)
async def editpages_receive_ops(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    sess = SessionData(**data["sess"])
    page_count = sess.page_count or 0
    photo_count = len(sess.edit_file_ids)

    try:
        ops = _parse_edit_ops(message.text or "", page_count, photo_count)
    except ValueError as error:
        await message.answer(
            f"⚠️ {error}\n\nПришлите операции ещё раз одним сообщением.\n\n"
            f"{_edit_ops_help(page_count, photo_count)}",
            reply_markup=cancel_kb()
        )
        return

    if not _enough_disk():
        await message.answer("На сервере осталось меньше 5 ГБ. Новые файлы временно не принимаются.")
        await state.clear()
        return

    await message.answer("⏳ Собираю обновлённый PDF…")
    images: List[Image.Image] = []
    try:
        raw = await _download_telegram_file(bot, sess.pdf_file_id)
        for fid in sess.edit_file_ids:
            photo_raw = await _download_telegram_file(bot, fid)
            with Image.open(io.BytesIO(photo_raw)) as im:
                im.load()
                images.append(im.copy())
        pdf_bytes = await asyncio.to_thread(_build_edited_pdf, raw, images, ops)
        if len(pdf_bytes) > MAX_OUTPUT_BYTES:
            raise ValueError("result too large")
    except Exception:
        await message.answer(
            "Ошибка при сборке PDF. Проверьте фото и попробуйте ещё раз.",
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
        await message.answer_document(
            FSInputFile(tmp_path, filename=_edited_pdf_name(sess.original_filename))
        )
        await _show_menu(message, "Готово. Что делаем дальше?")
    finally:
        try:
            os.remove(tmp_path)
        except Exception:
            pass
        await state.clear()

# ====== ALIGN PAGE WIDTHS ======
@router.message(Flow.align_wait_pdf, F.document)
async def align_receive_pdf(message: Message, state: FSMContext, bot: Bot):
    doc = message.document
    name = (doc.file_name or "").lower()
    if not name.endswith(".pdf"):
        await message.answer("Это не PDF. Пришлите файл .pdf", reply_markup=cancel_kb())
        return
    if _pdf_too_large(doc.file_size):
        await message.answer(
            f"Файл слишком большой ({(doc.file_size or 0) / (1024*1024):.1f} МБ).\n"
            f"Сейчас бот обрабатывает PDF до {MAX_PDF_DOWNLOAD_MB} МБ.",
            reply_markup=main_menu_kb()
        )
        await state.clear()
        return
    if not _enough_disk():
        await message.answer("На сервере осталось меньше 5 ГБ. Новые файлы временно не принимаются.")
        await state.clear()
        return

    await message.answer("⏳ Скачиваю PDF и выравниваю ширину страниц…")
    try:
        raw = await _download_telegram_file(bot, doc.file_id)
        pdf_bytes, changed, median = await asyncio.to_thread(_align_pdf_widths, raw)
    except Exception:
        await message.answer(
            "Не удалось обработать PDF. Возможно, файл повреждён или защищён паролем.",
            reply_markup=main_menu_kb()
        )
        await state.clear()
        return

    if changed == 0:
        await _show_menu(
            message,
            f"Все страницы уже одной ширины ({median:.0f} pt) — выравнивать нечего."
        )
        await state.clear()
        return

    if len(pdf_bytes) > MAX_OUTPUT_BYTES:
        await message.answer(
            "Результат получился больше 49 МБ и не проходит по ограничениям Telegram.",
            reply_markup=main_menu_kb()
        )
        await state.clear()
        return

    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        tmp.write(pdf_bytes)
        tmp_path = tmp.name

    try:
        await message.answer_document(
            FSInputFile(tmp_path, filename=_suffixed_pdf_name(doc.file_name, "aligned"))
        )
        await _show_menu(
            message,
            f"Готово: страниц выровнено {changed}, ширина всех страниц теперь {median:.0f} pt."
        )
    finally:
        try:
            os.remove(tmp_path)
        except Exception:
            pass
        await state.clear()

# ====== CROP PAGES ======
@router.message(Flow.crop_wait_pdf, F.document)
async def crop_receive_pdf(message: Message, state: FSMContext, bot: Bot):
    doc = message.document
    name = (doc.file_name or "").lower()
    if not name.endswith(".pdf"):
        await message.answer("Это не PDF. Пришлите файл .pdf", reply_markup=cancel_kb())
        return
    if _pdf_too_large(doc.file_size):
        await message.answer(
            f"Файл слишком большой ({(doc.file_size or 0) / (1024*1024):.1f} МБ).\n"
            f"Сейчас бот обрабатывает PDF до {MAX_PDF_DOWNLOAD_MB} МБ.",
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

    try:
        page_count = len(PdfReader(io.BytesIO(raw)).pages)
    except Exception:
        await message.answer(
            "Не удалось прочитать PDF (возможно, файл повреждён или защищён паролем).",
            reply_markup=main_menu_kb()
        )
        await state.clear()
        return
    if page_count <= 0:
        await message.answer("В PDF нет страниц.", reply_markup=main_menu_kb())
        await state.clear()
        return

    data = await state.get_data()
    sess = SessionData(**data["sess"])
    sess.pdf_file_id = doc.file_id
    sess.original_filename = doc.file_name
    sess.page_count = page_count
    await state.update_data(sess=sess.__dict__)

    await state.set_state(Flow.crop_ask_ops)
    try:
        preview_note = await _send_crop_previews(message, raw, page_count)
    except Exception:
        preview_note = "\n\n⚠️ Предпросмотр сделать не удалось — продолжаем без него."
    await message.answer(
        f"В PDF {page_count} страниц. Напишите, сколько отрезать у каждой группы страниц.\n\n"
        f"{_crop_ops_help(page_count)}{preview_note}",
        reply_markup=cancel_kb()
    )

@router.message(Flow.crop_ask_ops, F.text)
async def crop_receive_ops(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    sess = SessionData(**data["sess"])
    page_count = sess.page_count or 0

    try:
        ops = _parse_crop_ops(message.text or "", page_count)
    except ValueError as error:
        await message.answer(
            f"⚠️ {error}\n\nПришлите операции ещё раз одним сообщением.\n\n"
            f"{_crop_ops_help(page_count)}",
            reply_markup=cancel_kb()
        )
        return

    if not _enough_disk():
        await message.answer("На сервере осталось меньше 5 ГБ. Новые файлы временно не принимаются.")
        await state.clear()
        return

    await message.answer("⏳ Обрезаю страницы…")
    try:
        raw = await _download_telegram_file(bot, sess.pdf_file_id)
        pdf_bytes, cropped = await asyncio.to_thread(_crop_pdf_pages, raw, ops)
        if len(pdf_bytes) > MAX_OUTPUT_BYTES:
            raise ValueError("result too large")
    except Exception:
        await message.answer(
            "Не удалось обработать PDF. Возможно, файл повреждён или защищён паролем.",
            reply_markup=main_menu_kb()
        )
        await state.clear()
        return

    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        tmp.write(pdf_bytes)
        tmp_path = tmp.name

    try:
        await message.answer_document(
            FSInputFile(tmp_path, filename=_suffixed_pdf_name(sess.original_filename, "cropped"))
        )
        await _show_menu(message, f"Готово: обрезано страниц {cropped} из {page_count}.")
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

@router.message(Flow.editpages_wait_pdf)
async def editpages_pdf_other(message: Message):
    await message.answer("Жду PDF-документ.", reply_markup=cancel_kb())

@router.message(Flow.editpages_collect)
async def editpages_collect_other(message: Message):
    await message.answer(
        "Пожалуйста, отправьте фотографию или картинку как документ.\n"
        "Либо нажмите «Фото больше нет — продолжить».",
        reply_markup=editpages_controls_kb()
    )

@router.message(Flow.editpages_ask_ops)
async def editpages_ops_other(message: Message):
    await message.answer(
        "Жду текст с операциями, например: 2=1 (замена) или 3+2 (вставка после страницы 3).",
        reply_markup=cancel_kb()
    )

@router.message(Flow.align_wait_pdf)
async def align_other(message: Message):
    await message.answer("Жду PDF-документ.", reply_markup=cancel_kb())

@router.message(Flow.crop_wait_pdf)
async def crop_other(message: Message):
    await message.answer("Жду PDF-документ.", reply_markup=cancel_kb())

@router.message(Flow.crop_ask_ops)
async def crop_ops_other(message: Message):
    await message.answer(
        "Жду текст с полями, например: все = 10 10 0 0 (проценты: сверху, снизу, слева, справа).",
        reply_markup=cancel_kb()
    )

async def main():
    if not BOT_TOKEN:
        raise RuntimeError("Заполните BOT_TOKEN в .env")
    bot = Bot(BOT_TOKEN)
    await bot.set_my_name("Best PDF Robot")
    await bot.set_my_short_description(
        "Создание и сжатие PDF, переименование, разделение, замена/вставка и обрезка страниц, "
        "выравнивание ширины.")
    await bot.set_my_description(
        "Создаёт PDF из фотографий без обрезки, сжимает, переименовывает, разделяет документы, "
        "заменяет и вставляет страницы, выравнивает ширину страниц и обрезает поля.")
    await bot.set_my_commands([
        BotCommand(command="start", description="Открыть главное меню"),
        BotCommand(command="help", description="Показать возможности бота"),
    ])
    dp = Dispatcher()
    dp.include_router(router)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
