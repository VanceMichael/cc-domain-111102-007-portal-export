"""采集器命令行入口。

示例
----
自动化演示（门户以 --test-mode 启动，仅本地演示验证码答案接口）：

    python3 -m app.cli --request museum-a:zh,museum-b:zh \\
        --user demo --password demo12345 --test-captcha

人工登录（真实使用方式，有头浏览器；无头环境用 xvfb-run 包裹）：

    xvfb-run -a python3 -m app.cli --request museum-a:zh --headed
"""

from __future__ import annotations

import argparse
import signal
import sys

from app.collector import CollectorConfig, PortalCollector


def parse_requests(text: str) -> list[tuple[str, str]]:
    requests: list[tuple[str, str]] = []
    for chunk in text.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if ":" not in chunk:
            raise argparse.ArgumentTypeError(
                f"请求项格式应为 场馆:语言，收到：{chunk}")
        venue, language = chunk.split(":", 1)
        requests.append((venue.strip(), language.strip()))
    if not requests:
        raise argparse.ArgumentTypeError("至少需要一个 场馆:语言 请求")
    return requests


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="门户公开服务目录采集器（只读、可恢复）")
    parser.add_argument("--url", default="http://127.0.0.1:8765", help="门户基础地址")
    parser.add_argument("--out", default="runtime/run", help="证据与成品输出目录")
    parser.add_argument("--request", dest="requests", required=True,
                        type=parse_requests, help="逗号分隔的 场馆:语言 列表")
    parser.add_argument("--headed", action="store_true",
                        help="有头模式（人工登录需要；无头机器请配合 xvfb-run）")
    parser.add_argument("--user", default="", help="本地演示账号（真实环境留空走人工登录）")
    parser.add_argument("--password", default="", help="本地演示密码")
    parser.add_argument("--test-captcha", action="store_true",
                        help="允许使用门户 --test-mode 的验证码答案接口（仅本地回归）")
    parser.add_argument("--poll-ms", type=int, default=700)
    parser.add_argument("--job-timeout", type=float, default=120.0)
    parser.add_argument("--login-timeout", type=float, default=300.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = CollectorConfig(
        base_url=args.url.rstrip("/"),
        output_root=args.out,
        headless=not args.headed,
        demo_user=args.user,
        demo_password=args.password,
        captcha_answer_url=(args.url.rstrip("/") + "/test/captcha/{cid}")
        if args.test_captcha else None,
        poll_interval_ms=args.poll_ms,
        job_timeout_seconds=args.job_timeout,
        login_timeout_seconds=args.login_timeout,
    )
    collector = PortalCollector(config)

    def _handle_sigint(signum, frame):
        collector.cancel()

    signal.signal(signal.SIGINT, _handle_sigint)
    signal.signal(signal.SIGTERM, _handle_sigint)

    try:
        result = collector.run(args.requests)
    except KeyboardInterrupt:
        collector.cancel()
        print("已取消：未完成文件保留在暂存区，重跑同一命令可续收。", file=sys.stderr)
        return 130
    except Exception as exc:  # 顶层兜底：账本已在每个步骤落盘，可再次运行恢复
        print(f"采集中止：{exc}", file=sys.stderr)
        return 2
    print("\n采集结果：")
    for path in result["verified"]:
        print(f"  已确认归属：{path}")
    for failure in result["failed"]:
        print(f"  失败：{failure['venue']}/{failure['language']} "
              f"({failure['job_id']}) {failure['error']}", file=sys.stderr)
    if result["relogins"]:
        print(f"  期间会话失效并重新登录 {result['relogins']} 次")
    return 0 if not result["failed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
