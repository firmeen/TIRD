"""Regression coverage for compatibility with Textual 8.x."""
from pathlib import Path
from types import SimpleNamespace
import unittest

from tird_ui import TIRDApp


class TextualStartupTests(unittest.TestCase):
    def test_textual_internal_logger_is_preserved(self):
        args = SimpleNamespace(output=Path("TIRD_Newsletter"))
        app = TIRDApp(args, SimpleNamespace())
        self.assertTrue(hasattr(app.log, "system"))
        self.assertTrue(callable(app.write_log))


if __name__ == "__main__":
    unittest.main()
