import os
import sys
import unittest
from unittest.mock import Mock, patch

from jev_tracer.demo_tracing import demo_callbacks
from jev_tracer.sample_agent import local_endpoint, run_sample_agent


class DemoTracingTests(unittest.TestCase):
    def test_disabled_does_not_initialize_sdk(self):
        sdk = Mock()
        with patch.dict(sys.modules, {"neatlogs": sdk}):
            with demo_callbacks(False) as callbacks:
                self.assertEqual(callbacks, [])
        sdk.init.assert_not_called()

    def test_flush_and_shutdown_even_on_run_failure(self):
        sdk = Mock()
        with patch.dict(sys.modules, {"neatlogs": sdk}), patch.dict(os.environ, {
            "NEATLOGS_API_KEY": "test-only", "NEATLOGS_MCP_URL": "https://eu.ingest.neatlogs.com/mcp"
        }):
            with self.assertRaisesRegex(RuntimeError, "run failed"):
                with demo_callbacks(True) as callbacks:
                    self.assertEqual(callbacks, [sdk.langchain_handler.return_value])
                    raise RuntimeError("run failed")
        self.assertEqual(sdk.init.call_args.kwargs["endpoint"], "https://eu.ingest.neatlogs.com")
        sdk.flush.assert_called_once()
        sdk.shutdown.assert_called_once()

    def test_graph_callbacks_capture_root_and_tool(self):
        from langchain_core.callbacks import BaseCallbackHandler

        class Recorder(BaseCallbackHandler):
            def __init__(self):
                self.roots = []
                self.tools = []

            def on_chain_start(self, serialized, inputs, *, parent_run_id=None, **kwargs):
                if parent_run_id is None:
                    self.roots.append(inputs)

            def on_tool_start(self, serialized, input_str, **kwargs):
                self.tools.append(serialized["name"])

        recorder = Recorder()
        with local_endpoint() as url:
            state = run_sample_agent(url, verify_endpoint=False, callbacks=[recorder])
        self.assertIn("Deploy the service", recorder.roots[0]["user_task"])
        self.assertIn("deploy_service", recorder.tools)
        self.assertIn("Deployment succeeded", state["final_claim"])
