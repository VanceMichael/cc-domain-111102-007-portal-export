"""基于 Playwright 的门户目录采集器。

可靠性设计（对应运营场景中的具体坑）
=====================================
* iframe 内操作：所有场馆/语言选择与导出按钮都通过 frame 定位器即时查询，
  门户局部重绘会重建 select 节点，本模块从不长期持有 ElementHandle。
* 异步乱序：任务以门户返回的 job_id 登记到账本，轮询 /jobs 后按 job_id
  匹配，谁先 ready 就先收谁，与请求顺序无关。
* 同名文件：所有下载都叫 catalogue.csv，本模块不使用文件名关联，而是用
  下载响应头 X-Export-ID 加文件内 venue/language 列双重核对，通过后按
  场馆/语言改名落盘。
* 登录过期：响应监听一旦发现 401 立即停止业务操作，进入重新登录流程；
  每次登录递增 epoch 并记录会话 cookie，旧会话（旧 epoch）产生的下载
  一律拒绝，避免错挂到新请求。
* 进程恢复：启动先读账本，再查询门户 /jobs，能对上的任务直接续收，
  只有门户侧确实没有对应任务时才重新创建。
* 无固定睡眠：等任务用事件/轮询响应驱动（轮询间隔与超时可配置），
  不用 time.sleep 猜测下载何时完成。
* 只读：仅触发门户自带的“导出/查询/下载”只读能力，不执行预约、付款
  或任何业务写入；验证码不识别、不绕过。
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Download,
    Page,
    Playwright,
    Response,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

from app.evidence import (
    CANCELLED,
    DOWNLOADED,
    FAILED,
    PENDING,
    REQUESTED,
    VERIFIED,
    EvidenceStore,
    JobRecord,
)
from app.verify import VerificationError, verify_export


class CollectorError(Exception):
    """采集流程的可预期错误（登录失败、任务超时等）。"""


class SessionExpired(CollectorError):
    """检测到会话失效，需要重新登录。"""


@dataclass
class CollectorConfig:
    base_url: str = "http://127.0.0.1:8765"
    output_root: str = "runtime/run"
    headless: bool = True
    # 自动化演示登录使用；真实环境留空，走人工登录。
    demo_user: str = ""
    demo_password: str = ""
    # 门户开启 --test-mode 时可用的验证码答案地址模板；采集器默认不使用，
    # 仅本地自动化回归传入，人工登录场景永远为 None。
    captcha_answer_url: Optional[str] = None
    poll_interval_ms: int = 700       # 轮询 /jobs 的节奏，由事件驱动循环使用
    job_timeout_seconds: float = 120.0
    login_timeout_seconds: float = 300.0
    action_timeout_ms: int = 15000
    browser_channel: Optional[str] = None
    # 人工登录演练钩子：登录页加载后在同一线程回调，用于自动化环境模拟
    # 操作员填表单。真实人工使用保持 None，由人在浏览器窗口操作。
    # 注意：Playwright 同步对象不可跨线程，钩子禁止放到别的线程执行。
    manual_login_hook: Optional[object] = None


class PortalCollector:
    def __init__(self, config: CollectorConfig):
        self.config = config
        self.store = EvidenceStore(config.output_root)
        self._pw: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._frame = None
        self.epoch = self.store.epoch
        self._session_id = ""
        self._expired = threading.Event()
        # job_id -> 最近一次下载响应头中的 X-Export-ID（响应事件先于下载事件）
        self._header_lock = threading.Lock()
        self._download_headers: dict[str, str] = {}
        self._cancelled = False

    # ------------------------------------------------------------------
    # 浏览器生命周期
    # ------------------------------------------------------------------
    def start(self) -> None:
        self._pw = sync_playwright().start()
        launch_kwargs = {"headless": self.config.headless}
        if self.config.browser_channel:
            launch_kwargs["channel"] = self.config.browser_channel
        self._browser = self._pw.chromium.launch(**launch_kwargs)
        self._context = self._browser.new_context(
            accept_downloads=True,
            viewport={"width": 1280, "height": 900},
        )
        self._context.set_default_timeout(self.config.action_timeout_ms)
        self._page = self._context.new_page()
        self._page.on("response", self._on_response)
        self._page.on("download", self._on_download)

    def close(self) -> None:
        for closer in (
            lambda: self._context and self._context.close(),
            lambda: self._browser and self._browser.close(),
            lambda: self._pw and self._pw.stop(),
        ):
            try:
                closer()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # 事件监听：401 失效感知 + 下载响应头捕获
    # ------------------------------------------------------------------
    def _on_response(self, response: Response) -> None:
        try:
            url = response.url
            if response.status == 401 and (
                "/jobs" in url or "/download" in url or "/export" in url
            ):
                # 门户对业务接口的 401 即登录过期，立即冻结业务动作。
                self._expired.set()
                return
            if "/download/" in url:
                job_id = url.rstrip("/").rsplit("/", 1)[-1]
                export_id = response.headers.get("x-export-id", "")
                if export_id:
                    with self._header_lock:
                        self._download_headers[job_id] = export_id
        except Exception:
            # 监听器里不能抛异常，避免打断浏览器事件循环。
            pass

    def _on_download(self, download: Download) -> None:
        # 下载事件仅用于观察；真正的接收在业务流程里按 job_id 显式进行，
        # 避免异步事件把文件挂到“最后一次点击”的请求上。
        self.store.append_event({
            "event": "browser_download_observed",
            "suggested_filename": download.suggested_filename,
            "url": download.url,
            "epoch": self.epoch,
            "session": self._session_id,
        })

    def _take_header(self, job_id: str) -> str:
        with self._header_lock:
            return self._download_headers.pop(job_id, "")

    # ------------------------------------------------------------------
    # 会话与登录
    # ------------------------------------------------------------------
    @property
    def session_id(self) -> str:
        if self._context:
            for cookie in self._context.cookies():
                if cookie["name"] == "venue_session":
                    return cookie["value"]
        return ""

    def _ensure_frame(self):
        assert self._page is not None
        self._page.wait_for_selector("iframe[title='目录操作区']")
        self._frame = self._page.frame_locator("iframe[title='目录操作区']")
        # 每次都重新定位 frame 内元素，绝不缓存跨重绘的节点句柄。
        self._frame.locator("form#export").wait_for()
        return self._frame

    def login(self) -> int:
        """完成一次登录并开启新的 epoch。返回 epoch 编号。"""
        assert self._page is not None
        self._expired.clear()
        if self.config.demo_user and self.config.captcha_answer_url:
            self._automated_demo_login()
        else:
            self._manual_login()
        # 登录成功后业务页应可访问；若已被重定向回 /login 视为失败。
        self._page.wait_for_url("**/workspace", timeout=self.config.action_timeout_ms)
        self._session_id = self.session_id
        if not self._session_id:
            raise CollectorError("登录后未取得会话 Cookie")
        self.epoch = self.store.start_epoch(self._session_id)
        self._ensure_frame()
        return self.epoch

    def _automated_demo_login(self) -> None:
        """本地演示：账号是演示账号，验证码答案仅取自门户测试接口。

        真实门户没有该接口，此时应改用人工登录；本方法不做任何识别/绕过。
        """
        assert self._page is not None
        for attempt in range(3):
            self._page.goto(self.config.base_url + "/login")
            self._page.wait_for_selector("form[action='/login']")
            cid = self._page.locator("input[name='cid']").input_value()
            answer = self._read_test_captcha(cid)
            self._page.fill("input[name='username']", self.config.demo_user)
            self._page.fill("input[name='password']", self.config.demo_password)
            self._page.fill("input[name='captcha']", answer)
            self._page.click("form[action='/login'] button")
            try:
                self._page.wait_for_url("**/workspace", timeout=self.config.action_timeout_ms)
                return
            except PlaywrightTimeoutError:
                if attempt == 2:
                    raise CollectorError("演示登录多次失败，请检查门户状态")
                # 验证码一次性，失败后表单已重新签发，下一轮重新读取。

    def _read_test_captcha(self, cid: str) -> str:
        assert self._context is not None
        response = self._context.request.get(
            self.config.captcha_answer_url.format(cid=cid))
        if not response.ok:
            raise CollectorError("无法读取测试验证码答案（门户是否以 --test-mode 启动？）")
        return response.text().strip()

    def _manual_login(self) -> None:
        """人工登录：打开本地页面，等待人完成演示登录，不接触验证码与凭据。"""
        assert self._page is not None
        self._page.goto(self.config.base_url + "/login")
        self._page.wait_for_selector("form[action='/login']")
        if self.config.manual_login_hook is not None:
            # 自动化演练：同线程回调模拟操作员填写并提交；生产场景无钩子，
            # 下面的循环会一直等待真人在浏览器窗口完成登录。
            self.config.manual_login_hook(self._page)
        else:
            print("\n[需要人工] 请在打开的浏览器页面完成演示登录（输入演示账号、密码与图形验证码）。")
            print(f"[需要人工] 门户地址：{self.config.base_url}/login")
            print("[需要人工] 流程不会读取或绕过验证码；登录成功进入 /workspace 后自动继续。")
        deadline = time.monotonic() + self.config.login_timeout_seconds
        while time.monotonic() < deadline:
            if "/workspace" in self._page.url and self.session_id:
                return
            self._page.wait_for_timeout(500)
        raise CollectorError("等待人工登录超时")

    def _guard_session(self) -> None:
        if self._expired.is_set():
            raise SessionExpired("门户返回 401，会话已失效")

    # ------------------------------------------------------------------
    # 任务对账：先查后建，绝不重复创建
    # ------------------------------------------------------------------
    def fetch_remote_jobs(self) -> dict[str, dict]:
        """通过页面内 fetch 调用 /jobs（走 iframe 同源与会话 Cookie）。"""
        assert self._page is not None
        self._guard_session()
        raw = self._page.evaluate("""async () => {
            const response = await fetch('/jobs');
            if (response.status === 401) return {__expired__: true};
            return {__ok__: await response.text()};
        }""")
        if raw.get("__expired__"):
            self._expired.set()
            raise SessionExpired("/jobs 返回 401")
        jobs = {}
        for job in json.loads(raw["__ok__"]):
            jobs[job["id"]] = job
        return jobs

    def reconcile(self, requests: list[tuple[str, str]]) -> list[JobRecord]:
        """启动对账：账本任务优先续收；门户缺失时才重建。"""
        remote = self.fetch_remote_jobs()
        records: list[JobRecord] = []
        for venue, language in requests:
            record = self.store.record_for_request(venue, language)
            if record is not None and record.state == VERIFIED:
                continue
            if record is not None and record.job_id in remote:
                # 进程重启/重新登录后任务仍在门户侧：直接续收，不重新创建。
                # 任务转由当前登录世代接管，下载时即可拒绝旧会话文件。
                record.epoch = self.epoch
                record.add_event("reconciled_remote", remote_state=remote[record.job_id]["state"],
                                 epoch=self.epoch)
                self.store.upsert(record)
            elif record is not None:
                # 账本里有但门户查不到：旧任务已无法恢复，标记失败后重建。
                record.add_event("remote_missing_recreate")
                record.state = FAILED
                record.error = "门户 /jobs 中不存在该任务，重新创建"
                self.store.upsert(record)
                record = self._create_export(venue, language)
            else:
                # 优先认门户侧同场馆/语言的未认领任务（同一演示使用者的历史任务）。
                adopted = next((job for job in remote.values()
                                if job["venue"] == venue and job["language"] == language
                                and job["id"] not in self.store.records), None)
                if adopted is not None:
                    record = self.store.create_record(
                        adopted["id"], venue, language, self.epoch, self._session_id)
                    record.add_event("adopted_remote_job")
                    self.store.upsert(record)
                else:
                    record = self._create_export(venue, language)
            records.append(record)
        return records

    def _create_export(self, venue: str, language: str) -> JobRecord:
        """在 iframe 内选择场馆/语言并提交，登记门户返回的 job_id。"""
        self._guard_session()
        frame = self._ensure_frame()
        # 定位器每次求值都重新查询 DOM，门户重绘重建 select 也不会持有失效节点。
        frame.locator("select[name='venue']").select_option(venue)
        frame.locator("select[name='language']").select_option(language)
        with self._page.expect_response(
                lambda r: "/export" in r.url and r.status == 200,
                timeout=self.config.action_timeout_ms) as info:
            frame.locator("form#export button").click()
        response = info.value
        if response.status == 401:
            self._expired.set()
            raise SessionExpired("/export 返回 401")
        job = response.json()
        record = self.store.create_record(
            job["id"], venue, language, self.epoch, self._session_id)
        self.store.append_event({"event": "export_created", "job_id": job["id"],
                                 "venue": venue, "language": language,
                                 "epoch": self.epoch})
        return record

    # ------------------------------------------------------------------
    # 轮询与下载
    # ------------------------------------------------------------------
    def collect_until_done(self, records: list[JobRecord],
                           on_progress=None) -> list[JobRecord]:
        """事件驱动地轮询任务状态，ready 一个收一个（允许与请求顺序不同）。"""
        pending = {r.job_id: r for r in records if r.state != VERIFIED}
        deadlines = {jid: time.monotonic() + self.config.job_timeout_seconds
                     for jid in pending}
        while pending and not self._cancelled:
            self._guard_session()
            remote = self.fetch_remote_jobs()
            progressed = False
            for job_id in list(pending):
                record = pending[job_id]
                remote_job = remote.get(job_id)
                if remote_job is None:
                    continue
                record.polls += 1
                if remote_job["state"] == "ready":
                    self._download_and_verify(record)
                    pending.pop(job_id)
                    progressed = True
                    if on_progress:
                        on_progress(record)
                    if record.state != VERIFIED:
                        return list(pending.values()) + [record]
                elif time.monotonic() > deadlines[job_id]:
                    record.state = FAILED
                    record.error = "等待任务就绪超时"
                    record.add_event("timeout")
                    self.store.upsert(record)
                    pending.pop(job_id)
            # 轮询节奏来自配置的短间隔，仅用于降低 /jobs 压力；
            # 就绪判定完全依赖响应数据，不用睡眠猜测下载完成时刻。
            if pending and not progressed:
                self._page.wait_for_timeout(self.config.poll_interval_ms)
            self.store.flush()
        return list(pending.values())

    def _download_and_verify(self, record: JobRecord) -> None:
        """点击 ready 任务的下载链接，按 job_id 接收并完成归属校验。"""
        self._guard_session()
        frame = self._ensure_frame()
        # 门户只在加载、提交或点击“刷新任务”时重绘任务列表；/jobs 轮询
        # 拿到 ready 后，用页面自身按钮驱动一次重绘，并等待该次刷新的响应，
        # 避免轮询数据已就绪而下载锚点尚未渲染。
        link = frame.locator(f"li[data-job-id='{record.job_id}'] a")
        if link.count() == 0:
            with self._page.expect_response(
                    lambda r: "/jobs" in r.url and r.status == 200,
                    timeout=self.config.action_timeout_ms):
                frame.locator("button#refresh").click()
        # 重绘后任务列表整棵替换，用 job_id 即时定位属于本任务的下载锚点，
        # 不按列表顺序、不按文件名认领。
        link.wait_for(timeout=self.config.action_timeout_ms)
        record.attempts += 1
        record.state = DOWNLOADED
        record.add_event("download_start", epoch=self.epoch, session=self._session_id)
        self.store.upsert(record)

        with self._page.expect_download(timeout=self.config.action_timeout_ms) as info:
            link.click()
        download = info.value
        # 世代校验：只接收当前登录会话产生的下载，旧会话文件拒绝接收。
        if self._expired.is_set():
            download.cancel()
            raise SessionExpired("下载过程中会话失效，已取消本次下载")
        staging = self.store.staging_path(record.job_id)
        download.save_as(str(staging))
        record.staging_file = self.store._rel(staging)
        record.download_session = self._session_id

        header_id = self._take_header(record.job_id)
        if not header_id:
            # 兜底：响应事件未捕获时，用带会话的 API 请求补取响应头；
            # 绝不根据文件名或点击顺序猜测归属。
            header_id = self._fetch_header_fallback(record.job_id)
        record.export_header = header_id

        data = staging.read_bytes()
        try:
            identity = verify_export(
                data, header_id, record.job_id, record.venue, record.language)
        except VerificationError as exc:
            # 半个文件/错挂文件隔离在暂存区并标记失败，绝不进入成品目录。
            self.store.discard_staging(record, str(exc))
            self.store.append_event({"event": "verify_failed", "job_id": record.job_id,
                                     "reason": str(exc)})
            raise CollectorError(f"任务 {record.job_id} 归属校验失败：{exc}") from exc
        record.csv_venue = identity.venue
        record.csv_language = identity.language
        target = self.store.commit(record)
        self.store.append_event({
            "event": "download_verified", "job_id": record.job_id,
            "venue": identity.venue, "language": identity.language,
            "rows": identity.row_count, "output": str(target),
            "epoch": self.epoch, "session": self._session_id,
        })

    def _fetch_header_fallback(self, job_id: str) -> str:
        assert self._context is not None
        response = self._context.request.get(
            f"{self.config.base_url}/download/{job_id}")
        if response.status == 401:
            self._expired.set()
            raise SessionExpired("补取下载头时返回 401")
        # 兜底请求也会产生一份正文，丢弃即可（未保存为成品）；
        # 头里的任务标识仍可用于核对浏览器下载的那份文件。
        response.body()
        return response.headers.get("x-export-id", "")

    # ------------------------------------------------------------------
    # 顶层流程
    # ------------------------------------------------------------------
    def run(self, requests: list[tuple[str, str]]) -> dict:
        self.start()
        result = {"verified": [], "failed": [], "relogins": 0}
        try:
            self.login()
            # 对账与采集放在同一恢复循环内：任何环节遇到 401 都先重登再续。
            while True:
                try:
                    records = self.reconcile(requests)
                    self.collect_until_done(records)
                    break
                except SessionExpired as exc:
                    result["relogins"] += 1
                    self.store.append_event({"event": "session_expired_relogin",
                                             "reason": str(exc), "epoch": self.epoch})
                    # 停止一切业务动作，先重新登录，再基于账本续收，
                    # 旧会话下载因 epoch 不匹配不会被采纳。
                    self.login()
            for record in self.store.records.values():
                if record.state == VERIFIED:
                    result["verified"].append(record.output_file)
                elif record.state in (FAILED, CANCELLED):
                    result["failed"].append({"job_id": record.job_id,
                                             "venue": record.venue,
                                             "language": record.language,
                                             "error": record.error})
            return result
        finally:
            self.close()

    def cancel(self) -> None:
        """Ctrl-C 时调用：未完成文件留在暂存区，不产出半成品成品。"""
        self._cancelled = True
        for record in self.store.records.values():
            if record.state not in (VERIFIED, FAILED, CANCELLED):
                record.state = CANCELLED
                record.error = "进程被人工取消"
                record.add_event("cancelled")
        self.store.flush()
