"""下载文件的归属校验。

门户对每个下载响应都带 ``X-Export-ID`` 头，且 CSV 每一行都包含场馆与语言
列。只靠浏览器下载事件的顺序、文件名（全部同名 catalogue.csv）或“最后一次
点击”都无法证明归属，因此这里做三重比对：

1. 响应头 X-Export-ID 必须等于我们记录的任务标识；
2. CSV 内 venue 列必须与任务场馆一致（逐行）；
3. CSV 内 language 列必须与任务语言一致（逐行）；
4. 文件必须完整可读、至少含表头与一行数据，禁止半个文件通过。
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass

UTF8_BOM = b"\xef\xbb\xbf"


@dataclass(frozen=True)
class CsvIdentity:
    venue: str
    language: str
    row_count: int


class VerificationError(Exception):
    """文件归属或完整性校验失败。"""


def read_identity(data: bytes) -> CsvIdentity:
    if not data:
        raise VerificationError("文件为空，疑似未完成的下载")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise VerificationError(f"文件不是合法的 UTF-8 文本：{exc}") from exc
    reader = csv.DictReader(io.StringIO(text))
    required = {"venue", "language", "service"}
    if reader.fieldnames is None or not required.issubset(reader.fieldnames):
        raise VerificationError(f"缺少必需列，实际表头：{reader.fieldnames}")
    venues: set[str] = set()
    languages: set[str] = set()
    row_count = 0
    for line, row in enumerate(reader, start=2):
        venue = (row.get("venue") or "").strip()
        language = (row.get("language") or "").strip()
        service = (row.get("service") or "").strip()
        if not venue or not language or not service:
            raise VerificationError(f"第 {line} 行存在空字段，文件可能被截断")
        venues.add(venue)
        languages.add(language)
        row_count += 1
    if row_count == 0:
        raise VerificationError("只有表头没有数据行，不接受为空目录")
    if len(venues) != 1 or len(languages) != 1:
        raise VerificationError(
            f"文件内场馆/语言不唯一：venues={sorted(venues)} languages={sorted(languages)}")
    return CsvIdentity(venue=venues.pop(), language=languages.pop(), row_count=row_count)


def verify_export(data: bytes, header_export_id: str, expected_job_id: str,
                  expected_venue: str, expected_language: str) -> CsvIdentity:
    """完整归属校验，失败即抛 VerificationError，调用方必须保留或隔离该文件。"""
    if not header_export_id:
        raise VerificationError("下载响应缺少 X-Export-ID 头，无法关联任务")
    if header_export_id != expected_job_id:
        raise VerificationError(
            f"响应头任务标识 {header_export_id} 与期望 {expected_job_id} 不一致，"
            "存在同名文件错挂风险，拒绝接收")
    identity = read_identity(data)
    if identity.venue != expected_venue:
        raise VerificationError(
            f"文件内场馆 {identity.venue} 与任务场馆 {expected_venue} 不一致")
    if identity.language != expected_language:
        raise VerificationError(
            f"文件内语言 {identity.language} 与任务语言 {expected_language} 不一致")
    return identity
