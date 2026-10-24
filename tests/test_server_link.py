"""Laptop → server brain link without a network: the HTTP opener is mocked."""
import io
import json
import os
import unittest
import urllib.error
from unittest import mock

os.environ["JARVIS_SERVER_BRAIN"] = "0"  # tests never talk to the real server brain
from core import server_link


def reply(payload):
    return mock.MagicMock(__enter__=lambda self: io.BytesIO(json.dumps(payload).encode()),
                          __exit__=lambda self, *a: False)


class ServerLinkTests(unittest.TestCase):
    def test_ask_sends_token_and_splits_off_the_cost(self):
        with mock.patch.object(server_link, "TOKEN", "secret"), \
             mock.patch.object(server_link._opener, "open",
                               return_value=reply({"answer": "Готово, открыл.\n\n($0.02)"})) as opened:
            answer, cost = server_link.ask("открой почту")
        self.assertEqual((answer, cost), ("Готово, открыл.", 0.02))
        request = opened.call_args.args[0]
        self.assertEqual(request.full_url, server_link.BASE + "/ask")
        self.assertEqual(request.get_header("Authorization"), "Bearer secret")
        self.assertEqual(json.loads(request.data), {"text": "открой почту"})

    def test_cheap_answers_have_no_cost(self):
        with mock.patch.object(server_link._opener, "open", return_value=reply({"answer": "Привет."})):
            self.assertEqual(server_link.ask("привет"), ("Привет.", None))

    def test_unreachable_server_is_one_clear_error(self):
        refused = urllib.error.URLError(ConnectionRefusedError("refused"))
        with mock.patch.object(server_link._opener, "open", side_effect=refused):
            with self.assertRaises(server_link.ServerUnavailable):
                server_link.ask("привет")
            self.assertFalse(server_link.stop())

    def test_switch_and_token_both_needed(self):
        with mock.patch.object(server_link, "TOKEN", ""), mock.patch.dict(os.environ, {"JARVIS_SERVER_BRAIN": "1"}):
            self.assertFalse(server_link.enabled())
        with mock.patch.object(server_link, "TOKEN", "t"), mock.patch.dict(os.environ, {"JARVIS_SERVER_BRAIN": "1"}):
            self.assertTrue(server_link.enabled())
        with mock.patch.object(server_link, "TOKEN", "t"), mock.patch.dict(os.environ, {"JARVIS_SERVER_BRAIN": "0"}):
            self.assertFalse(server_link.enabled())


if __name__ == "__main__":
    unittest.main()
