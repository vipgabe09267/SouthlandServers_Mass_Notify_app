import json
import tempfile
import unittest
from pathlib import Path

import sls_diagnostics as diagnostics


class DiagnosticsTests(unittest.TestCase):
    def test_credentials_in_common_log_formats_are_redacted(self):
        cases = [
            ("Authorization: Basic abc123", "abc123"),
            ("authorization=Bearer bearer-secret", "bearer-secret"),
            ('{"password": "quoted secret with spaces"}', "quoted secret with spaces"),
            ("{'token': 'secret-token'}", "secret-token"),
            ('password="secret with spaces"', "secret with spaces"),
            ("https://username:my-password@pbx.example/api?token=query-secret", "my-password"),
            ("https://pbx.example/api?token=query-secret", "query-secret"),
        ]
        for message, secret in cases:
            with self.subTest(message=message):
                redacted = diagnostics.redact(message)
                self.assertNotIn(secret, redacted)
                self.assertIn("redacted", redacted)

    def test_lines_and_size_are_bounded(self):
        result = diagnostics.redact("line1\r\nforged second line" + "x" * 5000)
        self.assertNotIn("\n", result)
        self.assertNotIn("\r", result)
        self.assertLessEqual(len(result), 1500)

    def test_export_contains_health_without_private_error_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "diagnostics.json"
            diagnostics.export_diagnostics(destination, version="test", statuses={0: ("AUTHENTICATED", "private-user")},
                counts={"pending": 3}, update_error="request failed https://pbx.private.example/api?username=private-user&token=private-token")
            content = destination.read_text()
            exported = json.loads(content)
            self.assertEqual(3, exported["inbox_counts"]["pending"])
            self.assertEqual("AUTHENTICATED", exported["profiles"][0]["state"])
            for private in ("pbx.private.example", "private-user", "private-token"):
                self.assertNotIn(private, content)

    def test_logs_rotate_in_temporary_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "app.log"
            diagnostics.write_log(path, "starting")
            logger = diagnostics._LOGGERS[str(path)]
            try:
                # Exercise the actual rotating handler using a small byte budget.
                handler = logger.handlers[0]
                self.assertEqual(4, handler.backupCount)
                handler.maxBytes = 128
                for index in range(30):
                    diagnostics.write_log(path, f"entry {index} password=private-secret " + "x" * 50)
                self.assertLessEqual(len(list(Path(temporary).iterdir())), 5)
                for log in Path(temporary).iterdir():
                    self.assertNotIn("private-secret", log.read_text())
            finally:
                for handler in logger.handlers[:]:
                    handler.close()
                    logger.removeHandler(handler)
                diagnostics._LOGGERS.pop(str(path), None)


if __name__ == "__main__":
    unittest.main()
