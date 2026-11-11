"""真实浏览器回归：与本地演示门户端到端联动。

覆盖：
* iframe 内场馆/语言选择与导出；
* 反序完成时按任务标识（而非顺序/文件名）关联；
* 会话失效检测、重新登录后续收，旧会话下载不错挂；
* 进程重启后先查 /jobs 续收，不重复创建；
* 归属校验失败时半成品不进入成品目录。

无浏览器环境（playwright 或 chromium 缺失）时自动跳过。
"""

import json
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

try:
    from playwright.sync_api import sync_playwright
    BROWSER_AVAILABLE = True
except Exception:  # pragma: no cover - 无浏览器环境
    BROWSER_AVAILABLE = False

from app.collector import CollectorConfig, PortalCollector, SessionExpired
from app.verify import VerificationError, verify_export


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@unittest.skipUnless(BROWSER_AVAILABLE, "未安装 Playwright/Chromium")
class CollectorE2ETests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._temp = tempfile.TemporaryDirectory()
        cls.workdir = Path(cls._temp.name)
        cls.port = _free_port()
        cls.base_url = f"http://127.0.0.1:{cls.port}"
        cls.state_path = cls.workdir / "portal.json"
        cls.portal = subprocess.Popen(
            [sys.executable, "-I", str(REPO_ROOT / "portal.py"),
             "--port", str(cls.port), "--state", str(cls.state_path),
             "--test-mode"],
            cwd=REPO_ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                urllib.request.urlopen(cls.base_url + "/login", timeout=1)
                break
            except Exception:
                time.sleep(0.1)
        else:
            raise RuntimeError("门户启动失败")

    @classmethod
    def tearDownClass(cls):
        cls.portal.terminate()
        cls.portal.wait(timeout=10)
        cls._temp.cleanup()

    def _collector(self, name):
        return PortalCollector(CollectorConfig(
            base_url=self.base_url,
            output_root=str(self.workdir / name),
            headless=True,
            demo_user="demo",
            demo_password="demo12345",
            captcha_answer_url=self.base_url + "/test/captcha/{cid}",
            poll_interval_ms=200,
            job_timeout_seconds=60,
        ))

    def test_01_login_iframe_export_reverse_order_and_same_filename(self):
        collector = self._collector("case1")
        collector.start()
        try:
            epoch = collector.login()
            self.assertGreaterEqual(epoch, 1)
            self.assertTrue(collector.session_id)
            records = collector.reconcile([("museum-a", "zh"), ("museum-b", "zh")])
            self.assertEqual([r.venue for r in records], ["museum-a", "museum-b"])
            order = []
            collector.collect_until_done(records, on_progress=lambda r: order.append(r.venue))
            # museum-b 的 ready_after=1，museum-a=4：必须反序完成
            self.assertEqual(order, ["museum-b", "museum-a"])
            for record in collector.store.records.values():
                self.assertEqual(record.state, "verified")
                self.assertEqual(record.export_header, record.job_id)
                self.assertEqual(record.csv_venue, record.venue)
                self.assertEqual(record.csv_language, record.language)
                target = Path(record.output_file)
                self.assertTrue(target.is_absolute() or (collector.store.root / target).exists())
            names = sorted(p.name for p in collector.store.output_dir.iterdir())
            self.assertEqual(names,
                             ["catalogue_museum-a_zh.csv", "catalogue_museum-b_zh.csv"])
            events = [json.loads(line) for line in
                      collector.store.events_path.read_text(encoding="utf-8").splitlines()]
            suggested = {e["suggested_filename"] for e in events
                         if e["event"] == "browser_download_observed"}
            self.assertEqual(suggested, {"catalogue.csv"})
            # 文件内容可独立复核
            for record in collector.store.records.values():
                data = (collector.store.root / record.output_file).read_bytes()
                verify_export(data, record.job_id, record.job_id,
                              record.venue, record.language)
        finally:
            collector.close()

    def test_02_session_expiry_relogin_keeps_affiliation(self):
        collector = self._collector("case2")
        collector.start()
        try:
            epoch_before = collector.login()
            old_session = collector.session_id
            records = collector.reconcile([("museum-a", "en"), ("museum-b", "en")])
            # 主动使会话失效
            request = urllib.request.Request(
                self.base_url + "/expire",
                headers={"Cookie": f"venue_session={old_session}"})
            urllib.request.urlopen(request, timeout=5).read()
            with self.assertRaises(SessionExpired):
                collector.collect_until_done(records)
            epoch_after = collector.login()
            self.assertGreater(epoch_after, epoch_before)
            # 重新登录后对账：任务仍在门户侧，job_id 不变
            again = collector.reconcile([("museum-a", "en"), ("museum-b", "en")])
            self.assertEqual({r.job_id for r in again}, {r.job_id for r in records})
            collector.collect_until_done(again)
            for record in collector.store.records.values():
                self.assertEqual(record.state, "verified")
                self.assertEqual(record.download_session, collector.session_id)
                self.assertEqual(record.epoch, epoch_after)
                self.assertNotEqual(collector.session_id, old_session)
        finally:
            collector.close()

    def test_03_restart_resumes_existing_job_without_duplicate(self):
        out_root = self.workdir / "case3"
        collector = self._collector("case3")
        collector.start()
        try:
            collector.login()
            waiting = collector.reconcile([("museum-a", "zh")])
            self.assertEqual(len(waiting), 1)
            job_id = waiting[0].job_id
        finally:
            collector.close()

        # 门户重启：状态文件保留任务，会话重置
        self.portal.terminate()
        self.portal.wait(timeout=10)
        self.portal = subprocess.Popen(
            [sys.executable, "-I", str(REPO_ROOT / "portal.py"),
             "--port", str(self.port), "--state", str(self.state_path),
             "--test-mode"],
            cwd=REPO_ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                urllib.request.urlopen(self.base_url + "/login", timeout=1)
                break
            except Exception:
                time.sleep(0.1)

        collector = self._collector("case3")
        collector.start()
        try:
            collector.login()
            records = collector.reconcile([("museum-a", "zh")])
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].job_id, job_id, "不得重复创建任务")
            collector.collect_until_done(records)
            record = collector.store.record_for_request("museum-a", "zh")
            self.assertEqual(record.state, "verified")
            ledger = json.loads((out_root / "ledger.json").read_text(encoding="utf-8"))
            matching = [r for r in ledger["records"]
                        if r["venue"] == "museum-a" and r["language"] == "zh"]
            self.assertEqual(len(matching), 1)
        finally:
            collector.close()

    def test_04_failed_verification_keeps_half_file_out_of_output(self):
        collector = self._collector("case4")
        collector.start()
        try:
            collector.login()
            records = collector.reconcile([("museum-a", "zh")])
            record = records[0]
            # 模拟下载中断：暂存区只写入截断的半个文件。
            full = (self.workdir / "sample.csv")
            # 先正常拿到一份完整内容作为截断素材（走门户只读下载接口）。
            collector.collect_until_done([
                collector.store.record_for_request("museum-a", "zh")])
            verified = collector.store.record_for_request("museum-a", "zh")
            full_data = (collector.store.root / verified.output_file).read_bytes()

            # 新建第二个请求的记录，用截断文件走归属校验，必须被拒绝。
            second = collector.reconcile([("museum-b", "zh")])[0]
            staging = collector.store.staging_path(second.job_id)
            staging.write_bytes(full_data[: len(full_data) // 2])
            second.staging_file = str(staging.relative_to(collector.store.root))
            second.export_header = second.job_id
            with self.assertRaises(VerificationError):
                verify_export(staging.read_bytes(), second.job_id, second.job_id,
                              "museum-b", "zh")
            collector.store.discard_staging(second, "模拟截断文件")
            self.assertEqual(second.state, "failed")
            self.assertFalse(staging.exists())
            # 成品目录里只有先前校验通过的 museum-a，半成品没有混入
            names = sorted(p.name for p in collector.store.output_dir.iterdir())
            self.assertEqual(names, ["catalogue_museum-a_zh.csv"])
            # 失败记录可在后续运行中重建并完成
            second.state = "failed"
            collector.store.flush()
            again = collector.reconcile([("museum-b", "zh")])
            collector.collect_until_done(again)
            self.assertEqual(
                collector.store.record_for_request("museum-b", "zh").state, "verified")
        finally:
            collector.close()


if __name__ == "__main__":
    unittest.main()
