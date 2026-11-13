import unittest

from telegram_release import chunks, render_notes, utf16_len


class ReleaseNotesTests(unittest.TestCase):
    def test_russian_section_and_links(self):
        result = render_notes(
            "### Русский\n\n**Новое:**\n\n"
            "- Обновлён [сайт](https://example.com) и имя_файла\n"
            "- Исправлена ошибка 😀\n\n### English\n\n- Updated site"
        )
        self.assertEqual(result.value, "- Обновлён сайт и имя_файла\n- Исправлена ошибка 😀\n\nОригинальное приложение | GitHub")
        self.assertEqual(sum(e[2]["type"] == "italic" for e in result.entities), 2)
        self.assertTrue(any(e[2].get("url") == "https://example.com" for e in result.entities))

    def test_reject_english_only(self):
        with self.assertRaises(ValueError):
            render_notes("### English\n\n- Updated site")

    def test_bold_language_labels(self):
        result = render_notes("**Русский**\n\n- Исправлена ошибка\n\n**English**\n\n- Fixed a bug")
        self.assertTrue(result.value.startswith("- Исправлена ошибка\n\n"))
        self.assertNotIn("Fixed a bug", result.value)

    def test_long_text_and_emoji_are_not_lost(self):
        result = render_notes("- Изменение 😀 " * 1500)
        parts = list(chunks(result))
        self.assertEqual("".join(part for part, _ in parts), result.value)
        for index, (part, entities) in enumerate(parts):
            self.assertLessEqual(utf16_len(part), 1024 if index == 0 else 4096)
            for entity in entities:
                self.assertLessEqual(entity["offset"] + entity["length"], utf16_len(part))


if __name__ == "__main__":
    unittest.main()
