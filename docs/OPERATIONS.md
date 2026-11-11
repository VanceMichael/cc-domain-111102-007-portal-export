# 操作手册与排障

## 1. 环境准备

```sh
python3 --version          # 需要 3.12+
pip install -r requirements.txt
python3 -c "from playwright.sync_api import sync_playwright; \
  sync_playwright().start().chromium.launch().close(); print('browser ok')"
```

若最后一步报 `Executable doesn't exist`：

- 受限网络：确认环境预置浏览器目录（如 `/ms-playwright/chromium-1194`），
  并使用 `requirements.txt` 固定的 Playwright 1.56.0；
- 可联网环境：`python3 -m playwright install chromium`。

## 2. 启动演示门户

```sh
python3 -I portal.py --port 8765 --state runtime/portal.json --test-mode
```

常用参数：

| 参数 | 说明 |
| --- | --- |
| `--port / --host` | 监听地址，默认 127.0.0.1:8765 |
| `--state` | 任务/会话状态文件；重启进程后任务仍在，即靠它持久化 |
| `--test-mode` | 开放 `/test/captcha/<id>` 答案接口，仅供自动化回归 |
| `--user/--password` | 演示账号，默认读 `PORTAL_DEMO_USER/PORTAL_DEMO_PASSWORD`，再缺省为 demo/demo12345 |

门户行为：`/login` 要求账号、密码与一次性图形验证码（TTL 5 分钟）；
`/workspace` 内嵌 `/frame` iframe；`museum-a` 任务需 4 次轮询就绪、
`museum-b` 需 1 次，用于制造反序完成；所有下载名均为 `catalogue.csv`，
响应头带 `X-Export-ID`；`/expire` 使当前会话失效。

## 3. 运行采集

```sh
# 自动化演示登录（门户必须 --test-mode）
python3 -I bin/collect \
  --request museum-a:zh,museum-b:zh,museum-a:en,museum-b:en \
  --user demo --password demo12345 --test-captcha \
  --out runtime/run

# 人工登录（真实路径）
DISPLAY=:99 python3 -I bin/collect --request museum-a:zh --headed --out runtime/run
```

退出码：`0` 全部确认；`1` 存在失败任务（详情在 stderr 与账本）；`2` 流程
异常中止（账本已落盘，可重跑恢复）。

中断（Ctrl-C/SIGTERM）：未完成任务标记 `cancelled`，暂存文件不会进入成品
目录；重跑后按对账规则续收或重建。

## 4. 证据怎么读

`ledger.json` 中每条记录的关键字段：

- `job_id`：门户任务标识，归属判断的权威键；
- `venue/language`：请求参数，必须与成品 CSV 内列一致；
- `epoch` / `created_session` / `download_session`：登录世代与会话证据；
- `export_header`：下载响应的 `X-Export-ID`；
- `csv_venue/csv_language`：从文件内容读出的归属；
- `state`：`requested → pending → downloaded → verified`，或
  `failed/cancelled` 终态；
- `history`：创建、对账续收、下载、校验、失败、取消的完整事件史。

`events.ndjson` 额外记录每次浏览器下载观察（含浏览器建议文件名，可核实
「两次同名下载」）与每次登录世代切换。

## 5. 端到端演示

```sh
python3 -I scripts/demo_e2e.py           # 无头真实浏览器，三个场景全自动
DISPLAY=:99 python3 -I scripts/demo_manual_login.py   # 人工登录路径（有头）
```

场景 2 中脚本通过门户 `/expire` 让会话立即失效；可观察到采集器停止业务
动作、重新登录（epoch +1）后在新会话下续收，且 `download_session` 为新
会话值。

## 6. 回归测试

```sh
# 全量（无浏览器环境时 E2E 用例自动跳过）
PYTHONPATH=. python3 -P -m unittest discover -s tests -v

# 只跑不需要浏览器的快速子集
PYTHONPATH=. python3 -P -m unittest \
  tests.test_portal tests.test_captcha tests.test_evidence \
  tests.test_verify tests.test_sources -v
```

测试矩阵：

| 测试文件 | 覆盖内容 |
| --- | --- |
| `test_portal.py` | 验证码一次性、错误凭据拒绝、任务反序、会话失效、状态持久化 |
| `test_captcha.py` | 验证码签发/校验/TTL/测试模式开关/BMP 完整性 |
| `test_evidence.py` | epoch、账本恢复、暂存隔离、原子提交、取消无半成品 |
| `test_verify.py` | 响应头不符、场馆/语言不符、截断/空文件、混合场馆拒绝 |
| `test_collector_e2e.py` | 真实浏览器：iframe 导出、反序+同名、失效重登、重启续收、半成品隔离 |
| `test_sources.py` | 既有 CSV/素材工具回归 |

## 7. 常见问题

**登录后马上又跳回 `/login`。**
会话已失效或验证码过期（一次性、5 分钟 TTL）。人工重新登录即可；账本不会
丢，恢复后自动对账。

**`归属校验失败：响应头任务标识 ... 不一致`。**
下载被错挂到其他任务（同名文件典型事故）。文件已被隔离并标记 failed，
不会进入成品目录；检查是否有其他进程共用同一浏览器用户目录，重跑该请求。

**`等待任务就绪超时`。**
门户任务长时间未 ready（默认 120 秒，`--job-timeout` 可调）。任务保留在
账本，门户恢复后重跑即可续收。

**重跑后任务被重建而不是续收。**
说明门户 `/jobs` 已查不到该 `job_id`（门户状态文件被清理或任务过期）。
旧记录以 failed 留痕，新任务有新标识，归属链仍可追溯。

**有头模式报找不到显示器。**
启动 Xvfb：`Xvfb :99 -screen 0 1280x1024x24 &`，再带 `DISPLAY=:99` 运行。
不要用 `xvfb-run`（部分精简镜像缺少它依赖的 xauth）。

**Playwright 报浏览器版本不匹配。**
不要单独升级 Playwright；按 `requirements.txt` 安装 1.56.0，与预置
chromium-1194 配套。
