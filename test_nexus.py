"""Offline tests for NEXUS logic. Run: python test_nexus.py   (no Ollama or GPU needed)"""
import base64, json, tempfile, unittest, urllib.error, zlib
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock

import nexus_agent as agent
import nexus_knowledge as kb
import nexus_tools as tools


def fake_embedding(text: str, dims: int = 64):
    v = [0.0] * dims
    for w in text.lower().replace("search_query:", "").replace("search_document:", "").split():
        v[zlib.crc32(w.strip(".,!?").encode()) % dims] += 1.0
    return v


class FakeClient:
    """Scripted chat replies + word-hash embeddings."""
    def __init__(self, replies=None):
        self.replies = list(replies or [])
        self.calls = []
        self.chat = NS(completions=NS(create=self._chat))
        self.embeddings = NS(create=self._embed)

    def _chat(self, **kw):
        self.calls.append(kw)
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return NS(choices=[NS(message=r)])

    def _embed(self, model, input):
        return NS(data=[NS(embedding=fake_embedding(t)) for t in input])


def say(text):
    return NS(content=text, tool_calls=None)


def call(name, args, cid="c1"):
    return NS(content="", tool_calls=[NS(id=cid, function=NS(name=name, arguments=json.dumps(args)))])


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        for target, attr, val in [(kb, "MEMORY_FILE", self.tmp / "mem.json"),
                                  (kb, "KNOWLEDGE_FILE", self.tmp / "kb.json"),
                                  (tools, "IMG_DIR", self.tmp)]:
            p = mock.patch.object(target, attr, val); p.start(); self.addCleanup(p.stop)
        kb._kb_cache = None
        agent._history.clear()
        tools._pending.clear()


class TestMemory(Base):
    def test_rejects_junk_and_dupes(self):
        self.assertIn("short", kb.store_learned_rule("Don"))
        self.assertIn("placeholder", kb.store_learned_rule("<actual_joke_here>"))
        self.assertTrue(kb.store_learned_rule("Call me Captain when greeting me").startswith("Locked in"))
        self.assertIn("Already", kb.store_learned_rule("call me captain, when greeting me!"))
        self.assertEqual(len(kb.load_memory()["learned_rules"]), 1)

    def test_history_capped(self):
        for i in range(80):
            kb.record_command(f"echo {i}")
        self.assertEqual(len(kb.load_memory()["command_history"]), kb.MAX_HISTORY)


class TestKnowledge(Base):
    def test_chunking(self):
        text = "\n\n".join(f"Paragraph {i}. " + "word " * 60 for i in range(10))
        chunks = kb.chunk_text(text, size=500, overlap=50)
        self.assertGreater(len(chunks), 3)
        self.assertTrue(all(len(c) < 700 for c in chunks))
        self.assertEqual(kb.chunk_text("   "), [])
        self.assertTrue(all(len(c) < 700 for c in kb.chunk_text("x" * 5000, size=500)))

    def test_learn_search_replace_forget(self):
        c = FakeClient()
        kb.add_document(c, "pasta", "Boil water, salt it well, cook spaghetti nine minutes, drain and add sauce.")
        kb.add_document(c, "rockets", "Rockets burn propellant and expel exhaust to produce thrust in orbit.")
        top = kb.search(c, "how long to cook spaghetti", min_score=0.1)
        self.assertEqual(top[0]["source"], "pasta")
        kb.add_document(c, "pasta", "Totally new text about gardening and tomato plants.")
        self.assertEqual(sum(1 for s in kb.list_sources() if s["source"] == "pasta"), 1)
        self.assertNotIn("spaghetti", json.dumps(kb.search(c, "spaghetti", min_score=0.0)[:1]))
        self.assertEqual(kb.forget_source("rockets"), 1)
        self.assertTrue(kb.KNOWLEDGE_FILE.exists())


class TestTools(Base):
    def test_command_safety(self):
        for ok in ["Get-Date", "ipconfig /all", "whoami", "Get-Process"]:
            self.assertTrue(tools.is_safe_command(ok), ok)
        for bad in ["Get-Date; Remove-Item x", "Remove-Item C:\\x", "echo hi > a.txt",
                    "Get-Process | Stop-Process", "echo $(calc)", "", "curl http://x"]:
            self.assertFalse(tools.is_safe_command(bad), bad)

    def test_approval_flow(self):
        ctx = {}
        out = tools.run_command("Remove-Item C:\\tmp -Recurse", ctx)
        self.assertIn("NOT RUN YET", out)
        pending = tools.pending_list()
        self.assertEqual(len(pending), 1)
        with mock.patch.object(tools, "_exec_powershell", return_value="ran") as ex:
            self.assertEqual(tools.resolve_approval(pending[0]["id"], True), "ran")
            ex.assert_called_once_with("Remove-Item C:\\tmp -Recurse")
        self.assertEqual(tools.pending_list(), [])
        pid = tools.queue_approval("run_command", {"command": "x"}, "x")
        self.assertIn("Denied", tools.resolve_approval(pid, False))
        self.assertIn("expired", tools.resolve_approval(pid, True))

    def test_open_browser_blocks_bad_schemes_and_injection(self):
        with mock.patch.object(tools.webbrowser, "open") as wb:
            self.assertIn("Refused", tools.open_browser("javascript:alert(1)"))
            self.assertIn("Refused", tools.open_browser("file:///C:/Windows/System32/calc.exe"))
            wb.assert_not_called()
            tools.open_browser("example.com/a'; calc; '")   # old code ran this in PowerShell
            self.assertTrue(wb.call_args[0][0].startswith("https://example.com"))

    def test_ssrf_blocked(self):
        for u in ["http://127.0.0.1:8000/", "http://localhost/", "http://169.254.169.254/latest",
                  "http://192.168.1.1/", "http://10.0.0.5/", "file:///etc/passwd", "ftp://x.com"]:
            self.assertIsNotNone(tools.check_public_url(u), u)

    def test_ddg_parser(self):
        page = '''<div class="result"><a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa%3Fx%3D1&amp;rut=abc">First &amp; <b>Best</b></a>
        <a class="result__snippet" href="x">A <b>great</b> snippet</a></div>
        <div><a class="result__a" href="https://two.org/">Second</a>
        <a class="result__snippet">Second snippet</a></div>'''
        r = tools.parse_ddg_results(page)
        self.assertEqual(r[0]["url"], "https://example.com/a?x=1")
        self.assertEqual(r[0]["title"], "First & Best")
        self.assertEqual(r[0]["snippet"], "A great snippet")
        self.assertEqual(r[1]["url"], "https://two.org/")

    def test_html_to_text(self):
        title, text = tools.html_to_text("<html><title>Hi</title><script>evil()</script><body><p>One</p><p>Two &amp; three</p></body>")
        self.assertEqual(title, "Hi"); self.assertNotIn("evil", text); self.assertIn("Two & three", text)

    def test_generate_image(self):
        png = b"\x89PNG\r\n\x1a\n" + b"0" * 20
        resp = mock.MagicMock(); resp.__enter__.return_value.read.return_value = json.dumps(
            {"images": [base64.b64encode(png).decode()]}).encode()
        ctx = {}
        with mock.patch.object(tools.urllib.request, "urlopen", return_value=resp) as uo:
            out = tools.generate_image("a red fox", ctx, width=700, height=5000, steps=999)
        body = json.loads(uo.call_args[0][0].data)
        self.assertEqual((body["width"], body["height"], body["steps"]), (704, 1024, 60))
        self.assertIn("generated", out)
        self.assertEqual(len(ctx["images"]), 1)
        self.assertEqual((self.tmp / ctx["images"][0].split("/")[-1]).read_bytes(), png)
        with mock.patch.object(tools.urllib.request, "urlopen", side_effect=urllib.error.URLError("refused")):
            self.assertIn("--api", tools.generate_image("x", {}))

    def test_learn_requires_user_supplied_url(self):
        c = FakeClient()
        page = ("Example", "Python was created by Guido van Rossum. " * 5)
        with mock.patch.object(tools, "fetch_page_text", return_value=page):
            self.assertIn("Refused", tools.learn_from_url("https://evil.test/x", {"prompt": "tell me about python", "client": c}))
            ok = tools.learn_from_url("https://example.com/py", {"prompt": "learn this: example.com/py please", "client": c})
        self.assertIn("Learned", ok)
        self.assertEqual(len(kb.list_sources()), 1)

    def test_unknown_tool_and_bad_args(self):
        self.assertIn("no tool", tools.execute_tool("nope", {}, {}))
        self.assertIn("bad arguments", tools.execute_tool("web_search", {"wrong": 1}, {}))


class TestAgent(Base):
    def test_tool_round_then_answer_and_history(self):
        c = FakeClient([call("get_system_stats", {}), say("CPU is chill.")])
        with mock.patch.object(tools, "get_system_stats", return_value="CPU: 5% | RAM: 40%"):
            r = agent.run_agent(c, "how's my pc?", "s1")
        self.assertEqual(r["ai_response"], "CPU is chill.")
        self.assertEqual(c.calls[1]["messages"][-1]["role"], "tool")
        c2 = FakeClient([say("Still here.")])
        agent.run_agent(c2, "and now?", "s1")
        roles = [m["role"] for m in c2.calls[0]["messages"]]
        self.assertEqual(roles, ["system", "user", "assistant", "user"])  # remembered the last turn

    def test_inline_json_tool_call_fallback(self):
        c = FakeClient([say('{"name": "get_system_stats", "parameters": {}}'), say("All good.")])
        with mock.patch.object(tools, "get_system_stats", return_value="CPU: 1%"):
            r = agent.run_agent(c, "stats", "s2")
        self.assertEqual(r["ai_response"], "All good.")

    def test_tools_unsupported_retries_plain(self):
        c = FakeClient([Exception("model does not support tools"), say("It's a cat.")])
        r = agent.run_agent(c, "what is this", "s3", image_bytes=b"\xff\xd8", image_mime="image/jpeg")
        self.assertEqual(r["ai_response"], "It's a cat.")
        self.assertEqual(r["model"], agent.VISION_MODEL)
        self.assertNotIn("tools", c.calls[1])

    def test_runaway_tool_loop_is_stopped(self):
        c = FakeClient([call("get_system_stats", {}, f"c{i}") for i in range(agent.MAX_TOOL_ROUNDS)] + [say("Enough.")])
        with mock.patch.object(tools, "get_system_stats", return_value="x"):
            r = agent.run_agent(c, "loop", "s4")
        self.assertEqual(r["ai_response"], "Enough.")
        self.assertNotIn("tools", c.calls[-1])

    def test_risky_command_surfaces_approval_and_image_returned(self):
        c = FakeClient([call("run_command", {"command": "Remove-Item C:\\x"}), say("Waiting on your OK.")])
        r = agent.run_agent(c, "delete x", "s5")
        self.assertEqual(len(r["pending"]), 1)
        png = base64.b64encode(b"\x89PNG....").decode()
        resp = mock.MagicMock(); resp.__enter__.return_value.read.return_value = json.dumps({"images": [png]}).encode()
        c = FakeClient([call("generate_image", {"prompt": "a castle"}), say("Here you go!")])
        with mock.patch.object(tools.urllib.request, "urlopen", return_value=resp):
            r = agent.run_agent(c, "draw a castle", "s6")
        self.assertEqual(len(r["images"]), 1)

    def test_knowledge_is_injected_into_prompt(self):
        c = FakeClient([say("ok")])
        kb.add_document(c, "manual", "The flux capacitor requires 1.21 gigawatts of power.")
        with mock.patch.object(kb, "RAG_MIN_SCORE", 0.1):
            agent.run_agent(c, "how much power does the flux capacitor need", "s7")
        self.assertIn("1.21 gigawatts", c.calls[0]["messages"][0]["content"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
