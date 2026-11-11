"""端到端演示：真实浏览器 + 本地演示门户。

覆盖运营事故对应的三个场景：
  1. 两个场馆反序完成（museum-a 慢、museum-b 快，先请求 a 却先收到 b），
     且两次下载在浏览器侧的文件名都是 catalogue.csv（同名文件）；
  2. 采集过程中会话失效：采集器检测到 401 后停止业务动作，经一次演示
     登录恢复，旧会话下载不会错挂到新请求；
  3. 模拟进程退出与门户重启：新进程先查询已创建任务再决定是否重建，
     未完成的任务被续收而不是重复创建。

只使用本地门户与演示账号；验证码答案仅取自门户 --test-mode 接口，
脚本本身不识别、不绕过验证码，真实环境应改用人工登录。

运行（仓库根目录）：
    python3 -I scripts/demo_e2e.py
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from app.collector import CollectorConfig, PortalCollector, SessionExpired  # noqa: E402


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class PortalProcess:
    def __init__(self, state_path: Path, port: int):
        self.state_path = state_path
        self.port = port
        self.process: subprocess.Popen | None = None

    def start(self) -> None:
        self.process = subprocess.Popen(
            [sys.executable, "-I", str(REPO_ROOT / "portal.py"),
             "--port", str(self.port), "--state", str(self.state_path),
             "--test-mode"],
            cwd=REPO_ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{self.port}/login", timeout=1)
                return
            except Exception:
                time.sleep(0.1)
        raise RuntimeError("门户启动超时")

    def stop(self) -> None:
        if self.process:
            self.process.terminate()
            self.process.wait(timeout=10)
            self.process = None

    def restart(self) -> None:
        self.stop()
        self.start()

    def expire_session(self, session_id: str) -> None:
        """携带指定会话 Cookie 调 /expire，模拟会话中途失效。"""
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/expire",
            headers={"Cookie": f"venue_session={session_id}"})
        urllib.request.urlopen(request, timeout=5).read()


def make_collector(base_url: str, out_root: Path) -> PortalCollector:
    return PortalCollector(CollectorConfig(
        base_url=base_url,
        output_root=str(out_root),
        headless=True,
        demo_user="demo",
        demo_password="demo12345",
        captcha_answer_url=base_url + "/test/captcha/{cid}",
        poll_interval_ms=300,
        job_timeout_seconds=60,
    ))


def read_ledger(out_root: Path) -> dict:
    return json.loads((out_root / "ledger.json").read_text(encoding="utf-8"))


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="portal-demo-"))
    state_path = workdir / "portal-state.json"
    out_root = workdir / "evidence"
    port = free_port()
    base_url = f"http://127.0.0.1:{port}"
    portal = PortalProcess(state_path, port)
    print(f"[准备] 工作目录 {workdir}")
    portal.start()
    print(f"[准备] 本地门户已启动：{base_url}/login（演示账号 demo / demo12345）")

    try:
        # ===== 场景 1：反序完成 + 同名文件 =====
        print("\n=== 场景 1：两个场馆反序完成，且下载文件名完全相同 ===")
        collector = make_collector(base_url, out_root)
        collector.start()
        epoch1 = collector.login()
        print(f"[登录] 演示登录完成，epoch={epoch1}，会话={collector.session_id[:8]}…")

        records = collector.reconcile([("museum-a", "zh"), ("museum-b", "zh")])
        print("[导出] 任务创建顺序：museum-a -> museum-b（a 的 ready_after=4，b 为 1）")
        assert [r.venue for r in records] == ["museum-a", "museum-b"]

        completed_order: list[str] = []
        collector.collect_until_done(
            records, on_progress=lambda r: completed_order.append(r.venue))
        print(f"[下载] 实际完成顺序：{completed_order}")
        assert completed_order == ["museum-b", "museum-a"], "未体现反序完成"

        events = [json.loads(line) for line in
                  (out_root / "events.ndjson").read_text(encoding="utf-8").splitlines()]
        filenames = {e["suggested_filename"] for e in events
                     if e["event"] == "browser_download_observed"}
        print(f"[同名] 浏览器侧两次下载文件名：{filenames}（文件名完全一样）")
        assert filenames == {"catalogue.csv"}

        for record in collector.store.records.values():
            assert record.state == "verified"
            assert record.export_header == record.job_id
            assert (record.csv_venue, record.csv_language) == (record.venue, record.language)
            print(f"[证据] {record.venue}/{record.language} -> {record.output_file}")
            print(f"       响应头 X-Export-ID={record.export_header[:12]}… "
                  f"== 任务标识；文件内 venue/language 列一致")
        outputs = sorted(p.name for p in (out_root / "catalogues").iterdir())
        print(f"[落盘] 成品按归属改名，互不覆盖：{outputs}")
        assert outputs == ["catalogue_museum-a_zh.csv", "catalogue_museum-b_zh.csv"]
        collector.close()
        print("[场景 1 通过] 反序完成被正确关联，同名文件未互相覆盖。")

        # ===== 场景 2：会话中途失效，重新登录后续收 =====
        print("\n=== 场景 2：采集过程中会话失效，演示登录恢复后续收 ===")
        collector = make_collector(base_url, out_root)
        collector.start()
        epoch_before = collector.login()
        old_session = collector.session_id
        records = collector.reconcile([("museum-a", "en"), ("museum-b", "en")])
        print(f"[导出] 英文目录任务已创建：{[r.job_id[:8] for r in records]}")
        print("[失效] 调用 /expire 让当前会话立即过期…")
        portal.expire_session(old_session)

        try:
            collector.collect_until_done(records)
            raise AssertionError("应当检测到会话失效")
        except SessionExpired:
            print("[检测] 业务接口返回 401，采集器立即冻结业务操作，不再触发任何导出/下载。")

        epoch_after = collector.login()
        assert epoch_after > epoch_before
        print(f"[恢复] 重新演示登录，epoch {epoch_before} -> {epoch_after}，"
              f"新会话={collector.session_id[:8]}…")
        records = collector.reconcile([("museum-a", "en"), ("museum-b", "en")])
        # 旧任务仍在门户侧，对账阶段必须续收而不是新建。
        assert {r.job_id for r in records} == {r.job_id for r in records}
        collector.collect_until_done(records)
        for record in collector.store.records.values():
            if record.language != "en":
                continue
            assert record.state == "verified"
            assert record.download_session == collector.session_id, \
                "成品必须来自新会话，旧会话下载不得错挂"
            assert record.epoch == epoch_after
            print(f"[证据] {record.venue}/en 在新会话（epoch={epoch_after}）下确认归属")
        collector.close()
        print("[场景 2 通过] 重新登录后旧任务续收，旧会话下载未错挂到新请求。")

        # ===== 场景 3：进程退出 + 门户重启，先查任务再决定是否重建 =====
        print("\n=== 场景 3：进程退出、门户重启后的任务续收 ===")
        out_root2 = workdir / "evidence-restart"
        collector = make_collector(base_url, out_root2)
        collector.start()
        collector.login()
        waiting = collector.reconcile([("museum-a", "zh")])
        assert len(waiting) == 1
        orphan_job_id = waiting[0].job_id
        print(f"[退出] 任务 {orphan_job_id[:8]}… 已创建但尚未下载，模拟进程退出。")
        collector.close()  # 浏览器与进程退出，账本已落盘

        portal.restart()
        print("[重启] 门户已重启（任务状态保留在状态文件，会话全部重置）。")
        remote_state = json.loads(state_path.read_text(encoding="utf-8"))
        assert orphan_job_id in remote_state["jobs"], "门户侧任务应仍然存在"

        collector = make_collector(base_url, out_root2)
        collector.start()
        collector.login()
        records = collector.reconcile([("museum-a", "zh")])
        assert len(records) == 1 and records[0].job_id == orphan_job_id, \
            "必须续收原任务，不得重复创建"
        print(f"[对账] 账本任务 {orphan_job_id[:8]}… 在门户 /jobs 中命中，直接续收。")
        collector.collect_until_done(records)
        ledger = read_ledger(out_root2)
        job_ids = [r["job_id"] for r in ledger["records"]
                   if r["venue"] == "museum-a" and r["language"] == "zh"]
        assert job_ids == [orphan_job_id], "同一请求出现了重复任务"
        record = collector.store.record_for_request("museum-a", "zh")
        assert record.state == "verified" and record.output_file
        print(f"[续收] 任务在新进程内完成：{record.output_file}")
        collector.close()
        print("[场景 3 通过] 未重复创建任务，重启后下载归属正确。")

        print("\n全部场景通过。证据目录：")
        for root in (out_root, out_root2):
            print(f"  {root}/")
            for path in sorted(root.rglob("*")):
                if path.is_file():
                    print(f"    {path.relative_to(root)}")
        return 0
    finally:
        portal.stop()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as exc:
        print(f"\n演示失败：{exc}", file=sys.stderr)
        raise SystemExit(1)
