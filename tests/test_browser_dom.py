"""Opt-in Camoufox checks with a fresh temporary profile and local files only.

Run with DTT_TEST_BROWSER=1 and DTT's virtualenv Python to exercise its installed
Notte/Camoufox dependencies. No account, model, remote page or saved profile is used.
"""

import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest

from test_browser_sessions import load_browser_code


@unittest.skipUnless(os.environ.get("DTT_TEST_BROWSER") == "1", "set DTT_TEST_BROWSER=1 for local Camoufox checks")
class BrowserDomTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dtt-browser-dom-", dir="/tmp")
        self.root = Path(self.temp.name)
        code = load_browser_code()
        code["BASE"] = self.root
        self.browser = code["Browser"](profile_dir=self.root / "profile")
        self.fixture = self.root / "fixture.html"
        self.fixture.write_text('''<!doctype html><html><head><meta charset="utf-8"></head>
        <body><div id="editor" contenteditable="true"></div>
        <input id="upload" type="file" multiple>
        <button id="increment" onclick="window.clicks++">Increment</button>
        <main><button id="trusted" onclick="if(event.isTrusted){window.trustedClicks++}"><span>Publish</span></button></main>
        <div role="dialog"><button id="dialog-trusted" onclick="if(event.isTrusted){window.dialogTrustedClicks++}"><span>Publish</span></button></div>
        <script>window.events=[];window.clicks=0;window.trustedClicks=0;window.dialogTrustedClicks=0;
        document.querySelector('#upload').addEventListener('change', event => {
          window.events.push([...event.target.files].map(file => ({name:file.name,size:file.size})));
        });</script></body></html>''')

    async def asyncTearDown(self):
        await self.browser.close()
        self.temp.cleanup()

    async def test_new_tab_returns_before_a_script_allows_domcontentloaded(self):
        session = await self.browser._ensure()
        context = session.window.page.context
        script_requested = asyncio.Event()
        release_script = asyncio.Event()
        async def document(route):
            await route.fulfill(content_type="text/html", body=
                '<!doctype html><script src="/slow.js"></script><div id="ready">Ready</div>')
        async def slow_script(route):
            script_requested.set()
            await release_script.wait()
            await route.fulfill(content_type="application/javascript", body="window.scriptComplete=true")
        # Route all fixture requests locally. No server or network is used.
        await context.route("http://127.0.0.1/fixture.html", document)
        await context.route("http://127.0.0.1/slow.js", slow_script)
        try:
            tab = await asyncio.wait_for(self.browser.act("tab_new", url="http://127.0.0.1/fixture.html"), timeout=5)
            await asyncio.wait_for(script_requested.wait(), timeout=5)
            self.assertEqual(tab["url"], "http://127.0.0.1/fixture.html")
            state = await self.browser.act("evaluate", tab_id=tab["tab_id"], code="document.readyState")
            self.assertEqual(state, {"value": "loading"})
            self.assertIn(tab["tab_id"], [item["tab_id"] for item in (await self.browser.act("tabs"))["tabs"]])
        finally:
            release_script.set()
        ready = await self.browser.act("wait_for", tab_id=tab["tab_id"], selector="#ready", timeout_ms=5000)
        self.assertTrue(ready["found"])
        self.assertEqual(await self.browser.act("evaluate", tab_id=tab["tab_id"], code="window.scriptComplete"), {"value": True})

    async def test_script_completion_large_values_and_tab_isolation(self):
        first = (await self.browser.act("tabs"))["tabs"][0]["tab_id"]
        second = await self.browser.act("tab_new", url=self.fixture.as_uri())
        await self.browser.act("goto", tab_id=first, url=self.fixture.as_uri())
        payload = "māori 🦕" * 20000
        script = "window.payload=" + json.dumps(payload, ensure_ascii=False) + "; /*" + "x" * 100000 + "*/window.payload"
        self.assertGreater(len(script), 180000)
        result = await self.browser.act("evaluate", tab_id=first, code=script)
        self.assertEqual(result, {"value": payload})
        self.assertEqual(await self.browser.act("evaluate", code="typeof window.payload"), {"value": "undefined"})
        self.assertEqual(await self.browser.act("evaluate", code="window.foo='';'ok'"), {"value": "ok"})
        value = await self.browser.act("evaluate", code="({string:'māori 🦕',number:42,yes:true,no:false,nil:null,array:[1,'two']})")
        self.assertEqual(value["value"], {"string": "māori 🦕", "number": 42, "yes": True, "no": False, "nil": None, "array": [1, "two"]})
        self.assertEqual(await self.browser.act("evaluate", code="Promise.resolve({done:true})"), {"value": {"done": True}})
        self.assertEqual(await self.browser.act("evaluate", code="undefined"), {"value": None})
        tabs = (await self.browser.act("tabs"))["tabs"]
        self.assertEqual([tab["tab_id"] for tab in tabs], [first, second["tab_id"]])
        self.assertEqual([tab["active"] for tab in tabs], [False, True])
        await self.browser.act("tab_select", tab_id=first)
        self.assertEqual(await self.browser.act("evaluate", code="window.payload.length"), {"value": len(payload.encode("utf-16-le")) // 2})
        await self.browser.act("tab_close", tab_id=first)
        with self.assertRaisesRegex(ValueError, "Unknown or closed"):
            await self.browser.act("evaluate", tab_id=first, code="42")
        self.assertEqual((await self.browser.act("tabs"))["tabs"][0]["tab_id"], second["tab_id"])

    async def test_notte_element_ids_target_the_observed_tab(self):
        first = await self.browser.act("tab_new", url=self.fixture.as_uri())
        observed = await self.browser.act("observe")
        button = next(element for element in observed["elements"] if "<button" in element["description"])
        second = await self.browser.act("tab_new", url=self.fixture.as_uri())
        await self.browser.act("observe")
        result = await self.browser.act("click", tab_id=first["tab_id"], id=button["id"])
        self.assertTrue(result["success"], result)
        self.assertEqual(await self.browser.act("evaluate", tab_id=first["tab_id"], code="window.clicks"), {"value": 1})
        self.assertEqual(await self.browser.act("evaluate", tab_id=second["tab_id"], code="window.clicks"), {"value": 0})

    async def test_selector_click_requires_a_trusted_event_and_one_target(self):
        original = (await self.browser.act("tabs"))["tabs"][0]["tab_id"]
        await self.browser.act("goto", tab_id=original, url=self.fixture.as_uri())
        target = await self.browser.act("tab_new", url=self.fixture.as_uri())
        tab_id = target["tab_id"]
        await self.browser.act("wait_for", tab_id=tab_id, selector="#trusted", timeout_ms=3000)
        await self.browser.act("tab_select", tab_id=original)
        synthetic = await self.browser.act("evaluate", tab_id=tab_id,
            code="document.querySelector('#trusted').click();window.trustedClicks")
        self.assertEqual(synthetic, {"value": 0})
        clicked = await self.browser.act("click_selector", tab_id=tab_id,
            selector='main button:has(:text-is("Publish"))', timeout_ms=3000)
        self.assertTrue(clicked["clicked"])
        self.assertEqual(await self.browser.act("evaluate", tab_id=tab_id, code="window.trustedClicks"), {"value": 1})
        with self.assertRaisesRegex(Exception, "strict mode violation"):
            await self.browser.act("click_selector", tab_id=tab_id,
                selector='button:has(:text-is("Publish"))', timeout_ms=3000)
        self.assertEqual(await self.browser.act("evaluate", tab_id=tab_id, code="window.trustedClicks"), {"value": 1})
        self.assertEqual(await self.browser.act("evaluate", tab_id=tab_id, code="window.dialogTrustedClicks"), {"value": 0})
        await self.browser.act("click_selector", tab_id=tab_id,
            selector='[role=dialog] button:has(:text-is("Publish"))', timeout_ms=3000)
        self.assertEqual(await self.browser.act("evaluate", tab_id=tab_id, code="window.dialogTrustedClicks"), {"value": 1})
        self.assertEqual(await self.browser.act("evaluate", tab_id=original,
            code="[window.trustedClicks, window.dialogTrustedClicks]"), {"value": [0, 0]})
        tabs = (await self.browser.act("tabs"))["tabs"]
        self.assertEqual(next(tab["tab_id"] for tab in tabs if tab["active"]), original)

    async def test_native_upload_and_contenteditable_preserve_unicode(self):
        initial = (await self.browser.act("tabs"))["tabs"][0]["tab_id"]
        target = await self.browser.act("tab_new", url=self.fixture.as_uri())
        await self.browser.act("tab_select", tab_id=initial)
        tab_id = target["tab_id"]
        await self.browser.act("wait_for", tab_id=tab_id, selector="#editor", timeout_ms=3000)
        text = "A mandate's scope includes māori 🦕."
        inserted = await self.browser.act("evaluate", tab_id=tab_id, code='''
          document.querySelector('#editor').focus();
          document.execCommand('insertText', false, ''' + json.dumps(text) + ''');
          document.querySelector('#editor').innerText
        ''')
        self.assertEqual(inserted, {"value": text})
        file = self.root / "media māori.txt"
        file.write_text("native upload 🦕", encoding="utf-8")
        uploaded = await self.browser.act("upload_files", tab_id=tab_id, selector="#upload", paths=[str(file)])
        self.assertEqual(uploaded["uploaded"], [str(file)])
        value = await self.browser.act("evaluate", tab_id=tab_id, code="window.events")
        self.assertEqual(value["value"], [[{"name": file.name, "size": file.stat().st_size}]])
        readback = await self.browser.act("evaluate", tab_id=tab_id, code="document.querySelector('#upload').files[0].text()")
        self.assertEqual(readback, {"value": "native upload 🦕"})
        self.assertEqual((await self.browser.act("tabs"))["tabs"][0]["active"], True)
        with self.assertRaises(Exception):
            await self.browser.act("wait_for", tab_id=tab_id, selector="#missing", timeout_ms=50)
        self.assertEqual(await self.browser.act("evaluate", tab_id=tab_id, code="'still usable'"), {"value": "still usable"})

    async def test_kept_tab_reopens_live_and_saved_windows_stay_cleared(self):
        import lz4.block

        class Pages(BaseHTTPRequestHandler):
            def do_GET(self):
                body = f"<title>{self.path}</title><p>{self.path}</p>".encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                if self.path == "/login":
                    self.send_header("Set-Cookie", "sid=signed-in; Path=/")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Pages)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        profile = self.root / "profile"

        def saved_tab_urls():
            state = json.loads(lz4.block.decompress((profile / "sessionstore.jsonlz4").read_bytes()[8:]))
            return sorted(tab["entries"][tab["index"] - 1]["url"] if tab.get("entries") else "about:blank"
                          for window in state["windows"] for tab in window["tabs"])

        try:
            inbox = await self.browser.act("tab_new", url=base + "/login")
            await self.browser.act("wait_for", tab_id=inbox["tab_id"], selector="p", timeout_ms=5000)
            await self.browser.act("tab_new", url=base + "/article")
            result = await self.browser.control("close", keep_tabs=[inbox["tab_id"]])
            self.assertEqual(result["kept_tabs"], [base + "/login"])
            for _ in range(2):
                browser = type(self.browser)(profile_dir=profile)
                try:
                    tabs = (await browser.act("tabs"))["tabs"]
                    self.assertEqual([(tab["url"], tab["active"], tab["keep"]) for tab in tabs],
                                     [("about:blank", True, False), (base + "/login", False, True)])
                    kept = tabs[1]["tab_id"]
                    await browser.act("wait_for", tab_id=kept, selector="p", timeout_ms=5000)
                    self.assertEqual(await browser.act("evaluate", tab_id=kept, code="document.title"),
                                     {"value": "/login"})
                    # Firefox restores the session cookie from its saved session.
                    self.assertEqual(await browser.act("evaluate", tab_id=kept, code="document.cookie"),
                                     {"value": "sid=signed-in"})
                finally:
                    await browser.close()
                # Firefox saves this session's tabs; the next launch clears them first.
                self.assertEqual(saved_tab_urls(), ["about:blank", base + "/login"])
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
