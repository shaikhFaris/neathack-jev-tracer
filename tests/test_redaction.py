import json
import unittest
from unittest.mock import patch

from jev_tracer.redaction import MASK, redact, redact_text


class RedactionTests(unittest.TestCase):
    def test_sensitive_keys_and_nested_json_are_masked(self):
        result = redact({"api_key": "arbitrary-key", "result_json": json.dumps({"customer_email": "person@example.com", "status": "accepted"})})
        self.assertEqual(result["api_key"], MASK)
        self.assertEqual(json.loads(result["result_json"])["customer_email"], MASK)
        self.assertEqual(json.loads(result["result_json"])["status"], "accepted")

    def test_url_credentials_and_token_query_are_removed(self):
        value = redact("https://username:password@example.com/path?token=private-value&service=demo")
        self.assertNotIn("username", value)
        self.assertNotIn("password", value)
        self.assertNotIn("private-value", value)
        self.assertIn("service=demo", value)

    def test_free_text_patterns_and_known_environment_secrets(self):
        with patch.dict("os.environ", {"SAMPLE_API_KEY": "unusual-key-value"}):
            value = redact_text("Bearer abcdefghijkl person@example.com 123-45-6789 unusual-key-value sk-examplelongkey12345")
        for private in ["abcdefghijkl", "person@example.com", "123-45-6789", "unusual-key-value", "sk-examplelongkey12345"]:
            self.assertNotIn(private, value)

    def test_normal_health_payload_is_preserved(self):
        value = redact('{"status":"healthy"}')
        self.assertEqual(json.loads(value), {"status": "healthy"})

    def test_private_key_is_masked(self):
        value = redact_text("-----BEGIN PRIVATE KEY-----\nsecret material\n-----END PRIVATE KEY-----")
        self.assertEqual(value, MASK)
