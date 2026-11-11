import http.cookiejar
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request

from app.catalogue import load_catalogues
from portal import PortalState, handler_for
from portal import ImageCaptchaStore


class PortalTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.state_path = Path(self.directory.name) / "portal.json"
        self.captchas = ImageCaptchaStore(test_mode=True)
        self.state = PortalState(self.state_path)
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            handler_for(self.state, load_catalogues(), self.captchas,
                        ("demo", "demo12345")))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.root = f"http://127.0.0.1:{self.server.server_port}"
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.directory.cleanup()

    def demo_login(self):
        html = self.opener.open(self.root + "/login").read().decode()
        cid = html.split('name="cid" value="')[1].split('"')[0]
        answer = self.opener.open(self.root + "/test/captcha/" + cid).read().decode()
        body = urllib.parse.urlencode({
            "username": "demo", "password": "demo12345",
            "cid": cid, "captcha": answer,
        }).encode()
        self.opener.open(self.root + "/login", data=body).read()

    def test_login_requires_captcha_and_credentials(self):
        # 错误验证码不能登录
        html = self.opener.open(self.root + "/login").read().decode()
        cid = html.split('name="cid" value="')[1].split('"')[0]
        body = urllib.parse.urlencode({
            "username": "demo", "password": "demo12345",
            "cid": cid, "captcha": "ZZZZ",
        }).encode()
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.opener.open(self.root + "/login", data=body)
        self.assertEqual(error.exception.code, 401)
        # 验证码一次性：用过的 cid 立即失效
        with self.assertRaises(urllib.error.HTTPError):
            self.opener.open(self.root + "/login", data=body)
        # 测试答案接口在非测试模式下不可用
        self.captchas.test_mode = False
        self.assertIsNone(self.captchas.test_answer(cid))

    def test_export_order_session_and_persistence(self):
        self.demo_login()
        self.assertIn(b"iframe", self.opener.open(self.root + "/workspace").read())
        ids = []
        for venue in ["museum-a", "museum-b"]:
            body = urllib.parse.urlencode({"venue": venue, "language": "zh"}).encode()
            ids.append(json.load(self.opener.open(self.root + "/export", data=body))["id"])
        jobs = json.load(self.opener.open(self.root + "/jobs"))
        self.assertEqual([job["state"] for job in jobs], ["pending", "ready"])
        response = self.opener.open(self.root + "/download/" + ids[1])
        self.assertEqual(response.headers["X-Export-ID"], ids[1])
        self.assertEqual(
            response.headers["Content-Disposition"],
            'attachment; filename="catalogue.csv"')
        self.assertIn("museum-b", response.read().decode("utf-8-sig"))
        self.opener.open(self.root + "/expire").read()
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.opener.open(self.root + "/jobs")
        self.assertEqual(error.exception.code, 401)
        # 重新登录后仍能查到先前创建的任务
        self.demo_login()
        self.assertEqual(len(json.load(self.opener.open(self.root + "/jobs"))), 2)
        restored = PortalState(self.state_path)
        self.assertEqual(set(restored.data["jobs"]), set(ids))


if __name__ == "__main__":
    unittest.main()
