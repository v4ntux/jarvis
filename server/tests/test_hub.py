"""The laptop's /ask and /stop on the hub, through a real aiohttp server on a free port."""
import unittest

import aiohttp
from aiohttp import web

from jarvis.hub import Hub


class HubAskTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.hub = Hub("token")
        self.asked = []

        async def on_ask(text):
            self.asked.append(text)
            return "Ответ на «%s»" % text

        self.hub.on_ask, self.hub.on_stop = on_ask, lambda: True
        app = web.Application()
        app.router.add_post("/ask", self.hub.ask)
        app.router.add_post("/stop", self.hub.stop)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.url = "http://127.0.0.1:%d" % site._server.sockets[0].getsockname()[1]
        self.http = aiohttp.ClientSession()

    async def asyncTearDown(self):
        await self.http.close()
        await self.runner.cleanup()

    async def post(self, path, payload, token="token"):
        async with self.http.post(self.url + path, json=payload,
                                  headers={"Authorization": "Bearer " + token}) as r:
            return r.status, (await r.json() if r.status == 200 else None)

    async def test_ask_returns_the_brain_answer(self):
        self.assertEqual(await self.post("/ask", {"text": " привет "}), (200, {"answer": "Ответ на «привет»"}))
        self.assertEqual(self.asked, ["привет"])

    async def test_wrong_token_never_reaches_the_brain(self):
        self.assertEqual((await self.post("/ask", {"text": "x"}, token="nope"))[0], 401)
        self.assertEqual((await self.post("/stop", {}, token="nope"))[0], 401)
        self.assertEqual(self.asked, [])

    async def test_empty_question_is_rejected(self):
        self.assertEqual((await self.post("/ask", {"text": "  "}))[0], 400)

    async def test_stop(self):
        self.assertEqual(await self.post("/stop", {}), (200, {"stopped": True}))


if __name__ == "__main__":
    unittest.main()
