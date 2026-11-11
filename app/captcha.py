"""本地演示门户的图形验证码。

验证码答案只保存在门户进程内存里，图片本身不携带答案（DOM 里也没有）。
采集器不读取答案接口：恢复登录由人工在本地页面完成，验证码正是留给人工
处理的环节。test_mode 下开放的答案接口只服务自动化回归，采集器从不调用。
"""

from __future__ import annotations

import random
import secrets
import threading
import time

# 极简 3x5 点阵字体，仅用于本地演示图片，不追求可读性工程质量。
_GLYPHS = {
    "0": ["111", "101", "101", "101", "111"],
    "1": ["010", "110", "010", "010", "111"],
    "2": ["111", "001", "111", "100", "111"],
    "3": ["111", "001", "111", "001", "111"],
    "4": ["101", "101", "111", "001", "001"],
    "5": ["111", "100", "111", "001", "111"],
    "6": ["111", "100", "111", "101", "111"],
    "7": ["111", "001", "010", "010", "010"],
    "8": ["111", "101", "111", "101", "111"],
    "9": ["111", "101", "111", "001", "111"],
    "A": ["010", "101", "111", "101", "101"],
    "B": ["110", "101", "110", "101", "110"],
    "C": ["111", "100", "100", "100", "111"],
    "D": ["110", "101", "101", "101", "110"],
    "E": ["111", "100", "110", "100", "111"],
    "F": ["111", "100", "110", "100", "100"],
}

_ALPHABET = "0123456789ABCDEF"
_SCALE = 6
_PADDING = 10
_GLYPH_W = 3 * _SCALE
_GLYPH_H = 5 * _SCALE
_SPACING = 2 * _SCALE
_WIDTH = _PADDING * 2 + 4 * _GLYPH_W + 3 * _SPACING
_HEIGHT = _PADDING * 2 + _GLYPH_H
_TTL_SECONDS = 300


def _bmp(width: int, height: int, pixels: list[list[tuple[int, int, int]]]) -> bytes:
    row_size = (width * 3 + 3) & ~3
    pixel_bytes = bytearray()
    for y in range(height - 1, -1, -1):  # BMP 自底向上
        row = bytearray()
        for x in range(width):
            r, g, b = pixels[y][x]
            row += bytes((b, g, r))
        row += b"\x00" * (row_size - len(row))
        pixel_bytes += row
    file_size = 54 + len(pixel_bytes)
    header = b"BM"
    header += file_size.to_bytes(4, "little")
    header += b"\x00\x00\x00\x00"
    header += (54).to_bytes(4, "little")
    info = (40).to_bytes(4, "little")
    info += width.to_bytes(4, "little")
    info += height.to_bytes(4, "little")
    info += (1).to_bytes(2, "little")
    info += (24).to_bytes(2, "little")
    info += b"\x00" * 24
    return header + info + bytes(pixel_bytes)


def _render(text: str) -> bytes:
    rng = random.Random()
    pixels = [[(245, 245, 245) for _ in range(_WIDTH)] for _ in range(_HEIGHT)]

    def put(x: int, y: int, color: tuple[int, int, int]) -> None:
        if 0 <= x < _WIDTH and 0 <= y < _HEIGHT:
            pixels[y][x] = color

    # 干扰点与干扰线，模拟真实门户图片验证码的基本噪声。
    for _ in range(_WIDTH * _HEIGHT // 14):
        put(rng.randrange(_WIDTH), rng.randrange(_HEIGHT),
            (rng.randrange(90, 200), rng.randrange(90, 200), rng.randrange(90, 200)))
    for _ in range(5):
        x1, y1 = rng.randrange(_WIDTH), rng.randrange(_HEIGHT)
        x2, y2 = rng.randrange(_WIDTH), rng.randrange(_HEIGHT)
        steps = max(abs(x2 - x1), abs(y2 - y1), 1)
        for i in range(steps + 1):
            put(x1 + (x2 - x1) * i // steps, y1 + (y2 - y1) * i // steps,
                (rng.randrange(60, 160), rng.randrange(60, 160), rng.randrange(60, 160)))

    for index, char in enumerate(text):
        origin_x = _PADDING + index * (_GLYPH_W + _SPACING)
        origin_y = _PADDING + rng.randrange(-2, 3)
        color = (rng.randrange(0, 90), rng.randrange(0, 90), rng.randrange(0, 90))
        for gy, line in enumerate(_GLYPHS[char]):
            for gx, mark in enumerate(line):
                if mark != "1":
                    continue
                for dy in range(_SCALE):
                    for dx in range(_SCALE):
                        put(origin_x + gx * _SCALE + dx, origin_y + gy * _SCALE + dy, color)
    return _bmp(_WIDTH, _HEIGHT, pixels)


class CaptchaStore:
    """进程内验证码存储：一次性使用、带 TTL，答案不进入页面。"""

    def __init__(self, test_mode: bool = False, ttl: int = _TTL_SECONDS) -> None:
        self.test_mode = test_mode
        self.ttl = ttl
        self._lock = threading.Lock()
        self._challenges: dict[str, tuple[str, float]] = {}

    def issue(self) -> tuple[str, bytes]:
        challenge_id = secrets.token_hex(8)
        answer = "".join(secrets.choice(_ALPHABET) for _ in range(4))
        with self._lock:
            self._challenges[challenge_id] = (answer, time.monotonic() + self.ttl)
        return challenge_id, _render(answer)

    def verify(self, challenge_id: str, guess: str) -> bool:
        if not challenge_id or not guess:
            return False
        with self._lock:
            record = self._challenges.pop(challenge_id, None)
        if record is None:
            return False
        answer, expires = record
        return time.monotonic() <= expires and secrets.compare_digest(answer, guess.upper().strip())

    def test_answer(self, challenge_id: str) -> str | None:
        """仅 test_mode 可用：回归脚本据此代替人工读图，采集器不使用。"""
        if not self.test_mode:
            return None
        with self._lock:
            record = self._challenges.get(challenge_id)
        return record[0] if record else None
