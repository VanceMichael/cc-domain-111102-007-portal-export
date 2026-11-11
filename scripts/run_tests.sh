# 自动化回归
#
# 纯标准库单元测试（门户、验证码、账本、归属校验，无需浏览器）：
#   PYTHONPATH=. python3 -P -m unittest tests.test_portal tests.test_captcha \
#       tests.test_evidence tests.test_verify tests.test_sources -v
#
# 真实浏览器端到端回归（自动启动本地演示门户，需要 Chromium）：
#   PYTHONPATH=. python3 -P -m unittest tests.test_collector_e2e -v
#
# 一键全量：
#   PYTHONPATH=. python3 -P -m unittest discover -s tests -v
#
# 说明：
# - -P 屏蔽 cwd 隐式 sys.path，避免下载目录中 planted 的同名模块被导入；
#   源码路径由 PYTHONPATH 显式提供。不要用 -I，它会同时忽略 PYTHONPATH。
# - 无 Playwright/Chromium 的环境中，浏览器用例自动跳过，其余用例照常运行。
