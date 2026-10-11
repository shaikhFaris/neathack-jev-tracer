import io
import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jev_tracer.runtime import configure_runtime


class RuntimeTests(unittest.TestCase):
    def tearDown(self):
        logger = logging.getLogger("jev_tracer")
        logger.handlers.clear()
        logger.setLevel(logging.NOTSET)
        logger.propagate = True

    def test_loads_project_env_without_overriding_process(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            root = Path(directory)
            (root / ".env").write_text("TEST_VALUE=from-file\nTEST_OTHER=loaded\n", encoding="utf-8")
            os.environ["TEST_VALUE"] = "from-process"
            configure_runtime(root, stream=io.StringIO())
            self.assertEqual(os.environ["TEST_VALUE"], "from-process")
            self.assertEqual(os.environ["TEST_OTHER"], "loaded")

    def test_progress_redacts_secrets_and_escapes_newlines(self):
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"NEATLOGS_API_KEY": "test-private-key-123"}):
            configure_runtime(Path(directory), stream=output)
            logging.getLogger("jev_tracer.monitor").info("Progress %s\nforged entry", os.environ["NEATLOGS_API_KEY"])
        self.assertNotIn("test-private-key-123", output.getvalue())
        self.assertIn("[REDACTED]", output.getvalue())
        self.assertEqual(len(output.getvalue().splitlines()), 1)

    def test_repeated_configuration_does_not_duplicate_output(self):
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            configure_runtime(Path(directory), stream=output)
            configure_runtime(Path(directory), stream=output)
            logging.getLogger("jev_tracer.monitor").info("One progress event")
        self.assertEqual(output.getvalue().count("One progress event"), 1)
