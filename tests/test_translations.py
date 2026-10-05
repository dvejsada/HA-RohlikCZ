"""Offline translation key and interpolation checks, also runnable with unittest."""

import json
from pathlib import Path
from string import Formatter
import unittest


ROOT = Path(__file__).resolve().parents[1] / "custom_components/rohlikcz/translations"


def strings(document, path=()):
    if isinstance(document, dict):
        for key, value in document.items():
            yield from strings(value, (*path, key))
    else:
        yield path, document


class TestGermanTranslation(unittest.TestCase):
    def test_complete_string_tree(self):
        english = dict(strings(json.loads((ROOT / "en.json").read_text(encoding="utf-8"))))
        german = dict(strings(json.loads((ROOT / "de.json").read_text(encoding="utf-8"))))
        self.assertEqual(english.keys(), german.keys())
        for path, value in german.items():
            with self.subTest(path=path):
                self.assertIsInstance(value, str)
                self.assertTrue(value.strip())

    def test_placeholders_preserved(self):
        formatter = Formatter()
        english = dict(strings(json.loads((ROOT / "en.json").read_text(encoding="utf-8"))))
        german = dict(strings(json.loads((ROOT / "de.json").read_text(encoding="utf-8"))))
        for path, value in english.items():
            with self.subTest(path=path):
                original = [(field, spec, conversion) for _, field, spec, conversion in formatter.parse(value) if field]
                translated = [(field, spec, conversion) for _, field, spec, conversion in formatter.parse(german[path]) if field]
                self.assertEqual(original, translated)
