"""Проверки безопасной сборки PDF и интерфейса."""
from __future__ import annotations

import io
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

    def test_menu_contains_all_three_operations(self) -> None:
        labels = [row[0].text for row in bot.main_menu_kb().inline_keyboard]
        self.assertEqual(labels, ["🖼 Создать PDF", "✏️ Переименовать PDF", "✂️ Разделить PDF"])


if __name__ == "__main__":
    unittest.main()
