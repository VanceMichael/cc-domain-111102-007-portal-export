import unittest

from app.catalogue import export_csv
from app.verify import VerificationError, read_identity, verify_export


class VerifyTests(unittest.TestCase):
    def _csv(self, venue="museum-a", language="zh", services=("讲解服务",)):
        rows = [{"service": s} for s in services]
        return export_csv(venue, language, rows)

    def test_valid_export_passes(self):
        data = self._csv()
        identity = verify_export(data, "job-1", "job-1", "museum-a", "zh")
        self.assertEqual((identity.venue, identity.language, identity.row_count),
                         ("museum-a", "zh", 1))

    def test_header_mismatch_rejected(self):
        data = self._csv()
        # 同名 catalogue.csv 场景：头里是另一个任务的标识，必须拒绝。
        with self.assertRaises(VerificationError):
            verify_export(data, "job-OTHER", "job-1", "museum-a", "zh")

    def test_missing_header_rejected(self):
        with self.assertRaises(VerificationError):
            verify_export(self._csv(), "", "job-1", "museum-a", "zh")

    def test_csv_venue_mismatch_rejected(self):
        data = self._csv(venue="museum-b")
        with self.assertRaises(VerificationError):
            verify_export(data, "job-1", "job-1", "museum-a", "zh")

    def test_csv_language_mismatch_rejected(self):
        data = self._csv(language="en")
        with self.assertRaises(VerificationError):
            verify_export(data, "job-1", "job-1", "museum-a", "zh")

    def test_half_and_empty_files_rejected(self):
        with self.assertRaises(VerificationError):
            read_identity(b"")
        full = self._csv()
        # 模拟下载中断：截掉尾部，最后一行残缺时必须失败。
        with self.assertRaises(VerificationError):
            read_identity(full[: len(full) - 4])
        # 只有表头也不算完成目录
        header_only = full.split(b"\n", 1)[0] + b"\n"
        with self.assertRaises(VerificationError):
            read_identity(header_only)

    def test_mixed_venue_in_one_file_rejected(self):
        data = b"\xef\xbb\xbfvenue,language,service\nmuseum-a,zh,x\nmuseum-b,zh,y\n"
        with self.assertRaises(VerificationError):
            read_identity(data)


if __name__ == "__main__":
    unittest.main()
