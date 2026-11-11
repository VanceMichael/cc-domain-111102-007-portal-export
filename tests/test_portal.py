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


class PortalTests(unittest.TestCase):
    def test_export_order_session_and_persistence(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "portal.json"
            state = PortalState(state_path)
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(state, load_catalogues()))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                root = f"http://127.0.0.1:{server.server_port}"
                opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
                opener.open(root + "/login", data=b"").read()
                self.assertIn(b"iframe", opener.open(root + "/workspace").read())
                ids = []
                for venue in ["museum-a", "museum-b"]:
                    body = urllib.parse.urlencode({"venue": venue, "language": "zh"}).encode()
                    ids.append(json.load(opener.open(root + "/export", data=body))["id"])
                jobs = json.load(opener.open(root + "/jobs"))
                self.assertEqual([job["state"] for job in jobs], ["pending", "ready"])
                response = opener.open(root + "/download/" + ids[1])
                self.assertEqual(response.headers["X-Export-ID"], ids[1])
                self.assertIn("museum-b", response.read().decode("utf-8-sig"))
                opener.open(root + "/expire").read()
                with self.assertRaises(urllib.error.HTTPError) as error:
                    opener.open(root + "/jobs")
                self.assertEqual(error.exception.code, 401)
                opener.open(root + "/login", data=b"").read()
                self.assertEqual(len(json.load(opener.open(root + "/jobs"))), 2)
                restored = PortalState(state_path)
                self.assertEqual(set(restored.data["jobs"]), set(ids))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
