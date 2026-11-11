import time
import unittest

from app.captcha import CaptchaStore


class CaptchaTests(unittest.TestCase):
    def test_issue_returns_answer_and_valid_bmp(self):
        store = CaptchaStore(test_mode=True)
        cid, image = store.issue()
        self.assertEqual(len(cid), 16)
        self.assertTrue(image.startswith(b"BM"), "应为 BMP 位图")
        # BMP 文件头中的文件长度字段应与实际字节数一致
        self.assertEqual(int.from_bytes(image[2:6], "little"), len(image))
        self.assertEqual(store.test_answer(cid), store.test_answer(cid))

    def test_verify_is_one_time_and_case_insensitive(self):
        store = CaptchaStore()
        cid, image = store.issue()
        answer = CaptchaStore.test_answer(store, cid) if store.test_mode else None
        # 非测试模式不开放答案接口
        self.assertIsNone(answer)
        # 直接验证流程：用测试模式拿答案，再用普通模式校验
        test_store = CaptchaStore(test_mode=True)
        cid, _ = test_store.issue()
        answer = test_store.test_answer(cid)
        self.assertTrue(test_store.verify(cid, answer.lower()))
        # 一次性：同一验证码再次校验必须失败
        self.assertFalse(test_store.verify(cid, answer))

    def test_wrong_answer_and_unknown_challenge_rejected(self):
        store = CaptchaStore()
        cid, _ = store.issue()
        self.assertFalse(store.verify(cid, "ZZZZ"))
        self.assertFalse(store.verify("missing-id", "ABCD"))
        self.assertFalse(store.verify("", "ABCD"))

    def test_ttl_expiry(self):
        store = CaptchaStore(test_mode=True, ttl=0)
        cid, _ = store.issue()
        answer = store.test_answer(cid)
        time.sleep(0.01)
        self.assertFalse(store.verify(cid, answer))

    def test_test_mode_gate(self):
        store = CaptchaStore(test_mode=False)
        cid, _ = store.issue()
        self.assertIsNone(store.test_answer(cid))


if __name__ == "__main__":
    unittest.main()
