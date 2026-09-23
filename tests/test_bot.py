"""Проверки безопасной сборки PDF и интерфейса."""
from __future__ import annotations

import io
import os
import shutil
import tempfile
import unittest

from PIL import Image
from pypdf import PdfReader

import bot


class PdfBotTest(unittest.TestCase):
    def test_wide_image_is_contained_on_landscape_page(self) -> None:
        source = Image.new("RGB", (2400, 600), "red")
        page = bot._image_to_page(source)
        self.assertEqual(page.size, bot.PAGE_PORTRAIT[::-1])
        self.assertEqual(page.getpixel((0, 0)), (255, 255, 255))
        self.assertEqual(page.getpixel((page.width // 2, page.height // 2)), (255, 0, 0))

    def test_created_pdf_keeps_one_page_per_image(self) -> None:
        images = [Image.new("RGB", (300, 900), "blue"), Image.new("RGB", (900, 300), "green")]
        result = bot._images_to_pdf_bytes(images)
        reader = PdfReader(io.BytesIO(result))
        self.assertEqual(len(reader.pages), 2)
        first = reader.pages[0].mediabox
        second = reader.pages[1].mediabox
        self.assertGreater(float(first.height), float(first.width))
        self.assertGreater(float(second.width), float(second.height))

    def test_filename_is_safe_and_has_no_duplicate_extension(self) -> None:
        self.assertEqual(bot._safe_pdf_name("  report.pdf  "), "report")
        self.assertEqual(bot._safe_pdf_name('../bad:name?.pdf'), "bad_name")

    def test_menu_contains_all_operations(self) -> None:
        labels = [row[0].text for row in bot.main_menu_kb().inline_keyboard]
        self.assertEqual(labels, [
            "🖼 Создать PDF", "🗜 Сжать PDF", "✏️ Переименовать PDF", "✂️ Разделить PDF",
            "🔁 Заменить/вставить страницы", "📐 Выровнять ширину страниц", "🔲 Обрезать страницы",
        ])

    def test_compressed_filename_has_requested_suffix(self) -> None:
        self.assertEqual(bot._compressed_pdf_name("Report.pdf"), "Report_compresed.pdf")
        self.assertEqual(bot._compressed_pdf_name("Report_compresed.pdf"), "Report_compresed.pdf")
        self.assertEqual(bot._compressed_pdf_name("../bad:name.PDF"), "bad_name_compresed.pdf")

    def test_parse_edit_ops_reads_replaces_and_inserts(self) -> None:
        ops = bot._parse_edit_ops("2=1, 3+2\n0+1", page_count=5, photo_count=3)
        self.assertEqual(ops, [("=", 2, 1), ("+", 3, 2), ("+", 0, 1)])

    def test_parse_edit_ops_allows_insert_at_edges_only(self) -> None:
        self.assertEqual(bot._parse_edit_ops("0+1", 5, 1), [("+", 0, 1)])
        self.assertEqual(bot._parse_edit_ops("5+1", 5, 1), [("+", 5, 1)])
        with self.assertRaises(ValueError):
            bot._parse_edit_ops("6+1", 5, 1)
        with self.assertRaises(ValueError):
            bot._parse_edit_ops("6=1", 5, 1)

    def test_parse_edit_ops_rejects_unknown_pages_photos_and_gibberish(self) -> None:
        with self.assertRaises(ValueError):
            bot._parse_edit_ops("9=1", 5, 2)
        with self.assertRaises(ValueError):
            bot._parse_edit_ops("2=7", 5, 2)
        with self.assertRaises(ValueError):
            bot._parse_edit_ops("замени вторую страницу", 5, 2)

    def test_edited_pdf_replaces_and_inserts_pages(self) -> None:
        tall = Image.new("RGB", (300, 900), "blue")    # портретная страница
        wide = Image.new("RGB", (900, 300), "green")   # альбомная страница
        source = bot._images_to_pdf_bytes([tall, wide])
        photos = [
            Image.new("RGB", (900, 300), "red"),       # стр. 1 фото — альбомная
            Image.new("RGB", (300, 900), "purple"),    # стр. 2 фото — портретная
        ]
        ops = [("=", 2, 1), ("+", 1, 2)]

        result = bot._build_edited_pdf(source, photos, ops)

        reader = PdfReader(io.BytesIO(result))
        self.assertEqual(len(reader.pages), 3)
        orientations = [
            "portrait" if float(page.mediabox.height) > float(page.mediabox.width) else "landscape"
            for page in reader.pages
        ]
        # Исходник [портрет, альбом]: страницу 2 заменило фото 1, фото 2 вставлено после страницы 1.
        self.assertEqual(orientations, ["portrait", "portrait", "landscape"])

    def test_edited_pdf_can_only_insert_without_deleting(self) -> None:
        source = bot._images_to_pdf_bytes([Image.new("RGB", (300, 900), "blue")])
        photos = [Image.new("RGB", (900, 300), "red")]
        result = bot._build_edited_pdf(source, photos, [("+", 0, 1), ("+", 1, 1)])
        self.assertEqual(len(PdfReader(io.BytesIO(result)).pages), 3)

    def test_align_pdf_widths_scales_pages_to_median_width(self) -> None:
        source = bot._images_to_pdf_bytes([
            Image.new("RGB", (300, 900), "blue"),   # портретная страница
            Image.new("RGB", (900, 300), "green"),  # альбомная страница
            Image.new("RGB", (300, 900), "red"),    # портретная страница
        ])

        result, changed, median = bot._align_pdf_widths(source)

        reader = PdfReader(io.BytesIO(result))
        self.assertEqual(len(reader.pages), 3)
        widths = [float(page.mediabox.width) for page in reader.pages]
        self.assertEqual(changed, 1)  # только альбомная страница масштабировалась
        for width in widths:
            self.assertAlmostEqual(width, median, delta=0.5)
        # Пропорции альбомной страницы сохранились: она осталась шире, чем выше.
        scaled = reader.pages[1]
        self.assertGreater(float(scaled.mediabox.width), float(scaled.mediabox.height))

    def test_parse_crop_ops_reads_ranges_and_all(self) -> None:
        ops = bot._parse_crop_ops("2 = 10 0 10,5 0\nвсе = 15 15 0 0", page_count=3)
        self.assertEqual(ops, [
            (2, 2, 10.0, 0.0, 10.5, 0.0),
            (1, 3, 15.0, 15.0, 0.0, 0.0),
        ])
        self.assertEqual(bot._parse_crop_ops("1-3 = 5 5 5 5", 5), [(1, 3, 5.0, 5.0, 5.0, 5.0)])

    def test_parse_crop_ops_rejects_bad_lines(self) -> None:
        with self.assertRaises(ValueError):
            bot._parse_crop_ops("9 = 10 10 10 10", 5)          # нет такой страницы
        with self.assertRaises(ValueError):
            bot._parse_crop_ops("2 = 10 10 10", 5)             # не 4 числа
        with self.assertRaises(ValueError):
            bot._parse_crop_ops("2 = 50 60 0 0", 5)            # сумма >= 100%
        with self.assertRaises(ValueError):
            bot._parse_crop_ops("обрежь всё", 5)               # не операция

    def test_crop_pdf_pages_shrinks_only_selected_pages(self) -> None:
        source = bot._images_to_pdf_bytes([
            Image.new("RGB", (300, 900), "blue"),
            Image.new("RGB", (300, 900), "green"),
        ])

        result, cropped = bot._crop_pdf_pages(source, [(1, 1, 10, 0, 10, 10)])

        self.assertEqual(cropped, 1)
        reader = PdfReader(io.BytesIO(result))
        first, second = reader.pages[0].mediabox, reader.pages[1].mediabox
        original = PdfReader(io.BytesIO(source)).pages[0].mediabox
        # сверху 10% и слева+справа по 10%
        self.assertAlmostEqual(float(first.width), float(original.width) * 0.8, delta=0.01)
        self.assertAlmostEqual(float(first.height), float(original.height) * 0.9, delta=0.01)
        self.assertAlmostEqual(float(second.width), float(original.width), delta=0.01)
        self.assertAlmostEqual(float(second.height), float(original.height), delta=0.01)

    @unittest.skipUnless(shutil.which("gs"), "ghostscript не установлен")
    def test_render_pdf_previews_limits_page_count(self) -> None:
        source = bot._images_to_pdf_bytes([
            Image.new("RGB", (300, 900), "blue"),
            Image.new("RGB", (900, 300), "green"),
            Image.new("RGB", (300, 900), "red"),
        ])
        with tempfile.TemporaryDirectory(prefix="pdf-preview-test-") as temp_dir:
            source_path = os.path.join(temp_dir, "source.pdf")
            with open(source_path, "wb") as stream:
                stream.write(source)

            previews = bot._render_pdf_previews(source_path, temp_dir, 2)

        self.assertEqual([number for number, _ in previews], [1, 2])
        for _, data in previews:
            self.assertTrue(data.startswith(b"\x89PNG"))
            self.assertGreater(len(data), 0)


if __name__ == "__main__":
    unittest.main()
