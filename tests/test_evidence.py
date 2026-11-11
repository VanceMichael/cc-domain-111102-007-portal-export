import json
import tempfile
import unittest
from pathlib import Path

from app.evidence import (
    CANCELLED,
    FAILED,
    VERIFIED,
    EvidenceStore,
)


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.store = EvidenceStore(self.root)

    def tearDown(self):
        self.directory.cleanup()

    def test_epoch_increments_and_persists(self):
        e1 = self.store.start_epoch("session-1")
        e2 = self.store.start_epoch("session-2")
        self.assertEqual((e1, e2), (1, 2))
        reopened = EvidenceStore(self.root)
        self.assertEqual(reopened.epoch, 2)

    def test_ledger_restoration_and_no_duplicate_requests(self):
        record = self.store.create_record("j1", "museum-a", "zh", 1, "s1")
        self.store.create_record("j2", "museum-b", "en", 1, "s1")
        reopened = EvidenceStore(self.root)
        self.assertEqual(set(reopened.records), {"j1", "j2"})
        self.assertEqual(reopened.record_for_request("museum-a", "zh").job_id, "j1")
        # 已 verified 的请求不再出现在待办里
        record.state = VERIFIED
        self.store.upsert(record)
        reopened = EvidenceStore(self.root)
        self.assertEqual(reopened.open_requests(), [("museum-b", "en")])

    def test_staging_never_becomes_output_without_commit(self):
        record = self.store.create_record("j1", "museum-a", "zh", 1, "s1")
        staging = self.store.staging_path("j1")
        staging.write_bytes(b"partial")
        record.staging_file = str(staging.relative_to(self.root))
        # 失败时暂存文件被清理，成品目录保持为空
        self.store.discard_staging(record, "校验失败")
        self.assertEqual(record.state, FAILED)
        self.assertEqual(list(self.store.output_dir.iterdir()), [])
        self.assertFalse(staging.exists())

    def test_commit_is_atomic_and_named_by_identity(self):
        record = self.store.create_record("j1", "museum-a", "zh", 1, "s1")
        staging = self.store.staging_path("j1")
        staging.write_bytes(b"\xef\xbb\xbfvenue,language,service\nmuseum-a,zh,x\n")
        record.staging_file = str(staging.relative_to(self.root))
        record.export_header = "j1"
        record.csv_venue, record.csv_language = "museum-a", "zh"
        target = self.store.commit(record)
        self.assertEqual(target.name, "catalogue_museum-a_zh.csv")
        self.assertTrue(target.exists())
        self.assertFalse(staging.exists())
        self.assertEqual(record.state, VERIFIED)

    def test_cancel_leaves_no_half_output(self):
        record = self.store.create_record("j1", "museum-a", "zh", 1, "s1")
        staging = self.store.staging_path("j1")
        staging.write_bytes(b"x")
        record.staging_file = str(staging.relative_to(self.root))
        record.state = CANCELLED
        record.error = "进程被人工取消"
        self.store.flush()
        self.assertEqual(list(self.store.output_dir.iterdir()), [])
        # 账本 JSON 完整可读
        data = json.loads((self.root / "ledger.json").read_text(encoding="utf-8"))
        self.assertEqual(data["records"][0]["state"], CANCELLED)


if __name__ == "__main__":
    unittest.main()
