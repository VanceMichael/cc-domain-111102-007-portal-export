"""人工登录路径演练（有头模式）。

不使用采集器的测试验证码通道（captcha_answer_url 保持 None），而是在真实
人工登录代码路径上，通过同线程登录钩子模拟操作员在页面填写演示账号、密码
与验证码。真实使用时不传钩子，由人在浏览器窗口完成同样动作；采集器自身
始终不读取答案、不识别或绕过验证码。

无显示器的 CI 环境用 Xvfb 提供虚拟显示：
    Xvfb :99 -screen 0 1280x1024x24 &
    DISPLAY=:99 python3 -I scripts/demo_manual_login.py
"""

from __future__ import annotations

import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from app.collector import CollectorConfig, PortalCollector  # noqa: E402


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="portal-manual-"))
    port = free_port()
    base_url = f"http://127.0.0.1:{port}"
    portal = subprocess.Popen(
        [sys.executable, "-I", str(REPO_ROOT / "portal.py"),
         "--port", str(port), "--state", str(workdir / "portal.json"),
         "--test-mode"],
        cwd=REPO_ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                urllib.request.urlopen(base_url + "/login", timeout=1)
                break
            except Exception:
                time.sleep(0.1)

        def operator_hook(page) -> None:
            # 与真人操作一致的同线程动作；验证码答案在此演示中由测试接口
            # 代读，真实场景是人眼读图后手工键入，采集器不经手答案。
            cid = page.locator("input[name='cid']").input_value()
            answer = urllib.request.urlopen(
                base_url + "/test/captcha/" + cid).read().decode()
            page.fill("input[name='username']", "demo")
            page.fill("input[name='password']", "demo12345")
            page.fill("input[name='captcha']", answer)
            page.click("form[action='/login'] button")

        config = CollectorConfig(
            base_url=base_url,
            output_root=str(workdir / "evidence"),
            headless=False,            # 人工登录必须有头
            demo_user="",              # 留空 -> 走人工登录路径
            demo_password="",
            captcha_answer_url=None,   # 采集器不获取验证码答案
            manual_login_hook=operator_hook,
            poll_interval_ms=300,
        )
        collector = PortalCollector(config)
        result = collector.run([("museum-b", "en")])
        assert not result["failed"], result
        record = collector.store.record_for_request("museum-b", "en")
        assert record.state == "verified"
        print(f"人工登录路径采集成功：{record.output_file}")
        return 0
    finally:
        portal.terminate()
        portal.wait(timeout=10)


if __name__ == "__main__":
    raise SystemExit(main())
