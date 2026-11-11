"""采集证据与任务账本。

设计要点
--------
1. 每个导出任务以「任务标识 + 场馆 + 语言」为主键记录，进程重启后先读账本，
   再向门户 /jobs 查询，已存在的任务绝不重复创建。
2. 每次浏览器会话（登录）分配递增 epoch；下载证据必须记录其 epoch，恢复登录
   后不会把旧会话产生的下载挂到新请求上。
3. 文件先写入暂存区 ``.staging``，校验通过后才原子改名进入输出目录；失败、
   取消一律保留在暂存区或清理掉，绝不会出现半个文件冒充成品。
4. 账本写入采用临时文件 + 原子替换，避免进程被杀死时写坏 JSON。
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

# 任务状态机
REQUESTED = "requested"      # 已在页面提交导出，拿到任务标识
PENDING = "pending"          # 门户任务仍在生成
DOWNLOADED = "downloaded"    # 文件已落到暂存区，等待/正在校验
VERIFIED = "verified"        # 任务标识与文件内场馆、语言全部核对通过（终态）
FAILED = "failed"            # 校验失败或门户报错（终态，需人工介入）
CANCELLED = "cancelled"      # 本地主动放弃（终态）

TERMINAL_STATES = {VERIFIED, FAILED, CANCELLED}


@dataclass
class JobRecord:
    job_id: str
    venue: str
    language: str
    state: str = REQUESTED
    epoch: int = 0
    created_session: str = ""          # 创建该任务时的会话标识（cookie 值）
    download_session: str = ""         # 实际下载时的会话标识
    output_file: str = ""              # 校验通过后的成品相对路径
    staging_file: str = ""             # 暂存文件相对路径
    export_header: str = ""            # 下载响应 X-Export-ID
    csv_venue: str = ""
    csv_language: str = ""
    polls: int = 0
    attempts: int = 0
    error: str = ""
    history: list[dict] = field(default_factory=list)

    def add_event(self, event: str, **detail) -> None:
        entry = {"event": event, **detail}
        self.history.append(entry)
        if len(self.history) > 200:  # 防止长期轮询把账本撑爆
            self.history = self.history[-200:]


class EvidenceStore:
    """任务账本 + 输出目录 + 暂存区的统一管理者。"""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.output_dir = self.root / "catalogues"
        self.staging_dir = self.root / ".staging"
        self.ledger_path = self.root / "ledger.json"
        self.events_path = self.root / "events.ndjson"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.staging_dir.mkdir(parents=True, exist_ok=True)
        self._lock_path = self.root / ".ledger.lock"
        self.records: dict[str, JobRecord] = {}
        self.epoch: int = 0
        self._load()

    # ---------- 持久化 ----------
    def _load(self) -> None:
        if self.ledger_path.exists():
            data = json.loads(self.ledger_path.read_text(encoding="utf-8"))
            self.epoch = int(data.get("epoch", 0))
            for raw in data.get("records", []):
                record = JobRecord(**raw)
                self.records[record.job_id] = record

    def _snapshot(self) -> dict:
        return {
            "epoch": self.epoch,
            "records": [asdict(record) for record in self.records.values()],
        }

    def flush(self) -> None:
        """原子写账本：同目录临时文件 + os.replace，崩溃不会留下半截 JSON。"""
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.ledger_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._snapshot(), ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, self.ledger_path)

    def append_event(self, line: dict) -> None:
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(line, ensure_ascii=False) + "\n")

    # ---------- 会话（登录）世代 ----------
    def start_epoch(self, session_id: str) -> int:
        self.epoch += 1
        self.append_event({"event": "epoch_start", "epoch": self.epoch,
                           "session": session_id})
        self.flush()
        return self.epoch

    # ---------- 任务记录 ----------
    def open_requests(self) -> list[tuple[str, str]]:
        """仍需取得成品的 (场馆, 语言) 请求；已终态的不再重复。"""
        done = {(r.venue, r.language) for r in self.records.values()
                if r.state == VERIFIED}
        return [(v, l) for (v, l) in self._request_order() if (v, l) not in done]

    def _request_order(self) -> list[tuple[str, str]]:
        seen: list[tuple[str, str]] = []
        for record in self.records.values():
            key = (record.venue, record.language)
            if key not in seen:
                seen.append(key)
        return seen

    def record_for_request(self, venue: str, language: str) -> Optional[JobRecord]:
        # 同一请求可能因门户侧任务丢失而重建，存在多条历史记录时返回最新一条
        # （字典按插入顺序保存，最新的在最后），失败记录仍保留用于追溯。
        match = None
        for record in self.records.values():
            if record.venue == venue and record.language == language:
                match = record
        return match

    def upsert(self, record: JobRecord) -> JobRecord:
        self.records[record.job_id] = record
        self.flush()
        return record

    def create_record(self, job_id: str, venue: str, language: str,
                      epoch: int, session_id: str) -> JobRecord:
        record = JobRecord(job_id=job_id, venue=venue, language=language,
                           epoch=epoch, created_session=session_id)
        record.add_event("created", epoch=epoch, session=session_id)
        return self.upsert(record)

    # ---------- 暂存与成品 ----------
    def staging_path(self, job_id: str) -> Path:
        return self.staging_dir / f"{job_id}.part"

    def _rel(self, path: Path) -> str:
        return str(path.resolve().relative_to(self.root.resolve()))

    def output_path(self, venue: str, language: str) -> Path:
        # 门户下发的文件名全部是 catalogue.csv，本地必须按归属改名，
        # 否则两个场馆的文件会互相覆盖。
        return self.output_dir / f"catalogue_{venue}_{language}.csv"

    def commit(self, record: JobRecord) -> Path:
        """暂存文件校验通过后原子改名到输出目录。"""
        staging = self.root / record.staging_file
        target = self.output_path(record.venue, record.language)
        os.replace(staging, target)
        record.output_file = self._rel(target)
        record.staging_file = ""
        record.state = VERIFIED
        record.add_event("verified", output=record.output_file,
                         header=record.export_header,
                         csv_venue=record.csv_venue, csv_language=record.csv_language)
        self.upsert(record)
        return target

    def discard_staging(self, record: JobRecord, reason: str) -> None:
        if record.staging_file:
            path = self.root / record.staging_file
            if path.exists():
                path.unlink()
        record.staging_file = ""
        record.state = FAILED
        record.error = reason
        record.add_event("failed", reason=reason)
        self.upsert(record)
