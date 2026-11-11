# 授权门户的跨页导出 RPA 采集

面向「仅提供网页的授权门户」的 Python 浏览器自动化采集流程：在真实浏览器里
经过 iframe 选择场馆与语言、触发异步导出、轮询任务状态、接收下载，并为每个
文件保留可追查归属的证据。**只做公开目录的只读导出，不执行预约、付款或任何
业务写入；不识别、不绕过验证码，也不获取真实凭据。**

## 它解决的具体问题

| 门户/运营现象 | 本项目的应对 |
| --- | --- |
| 导出是异步任务，完成顺序与请求顺序不同（museum-a 慢、museum-b 快） | 以门户返回的任务标识登记，轮询 `/jobs` 后按标识匹配，谁先 ready 先收谁 |
| 两个场馆的下载文件名完全相同（都是 `catalogue.csv`） | 不用文件名关联；以响应头 `X-Export-ID` + 文件内 `venue/language` 列双重核对，通过后按归属改名 |
| 同事凭“最后一次点击”关联，收到错场馆文件 | 下载锚点按 `data-job-id` 定位，点击顺序/文件名均不参与归属判断 |
| 页面局部重绘，长期持有的节点脱离文档 | 全程使用 Playwright 定位器即时求值，不缓存 ElementHandle |
| 登录中途过期 | 响应监听发现 401 立即冻结业务操作；支持人工在本地页面完成一次演示登录后恢复 |
| 重新登录后旧会话下载错挂到新请求 | 每次登录递增 epoch 并记录会话 cookie，只接收当前会话产生的下载 |
| 进程退出后任务是否要重建 | 启动先查账本、再查门户 `/jobs`，任务仍在就续收，确认缺失才重建 |
| 失败/取消产生半个文件 | 下载先进 `.staging` 暂存区，校验通过才原子改名进入成品目录 |
| 靠固定睡眠猜下载完成 | 全部由 `expect_response` / `expect_download` / 轮询响应事件驱动，睡眠只用于降低轮询频率 |

## 目录结构

```
portal.py              本地演示门户（标准库，含图形验证码登录、iframe、异步任务）
app/
  captcha.py           进程内图形验证码（答案不进页面；仅 --test-mode 开放答案接口）
  catalogue.py         合成场馆目录加载与 CSV 导出
  collector.py         Playwright 采集器：登录/对账/轮询/下载/归属校验/恢复
  evidence.py          证据账本、epoch 世代、暂存区与原子提交
  verify.py            X-Export-ID 与文件内场馆/语言的归属校验
  cli.py               采集器命令行
  wire.py / inspect.py 既有素材读取工具（回归保留）
bin/collect            支持 python -I 从任意目录运行的入口
scripts/
  demo_e2e.py          真实浏览器端到端演示（反序完成/会话失效/重启续收/同名文件）
  demo_manual_login.py 人工登录路径演练（有头 + Xvfb，同线程登录钩子）
  run_tests.sh         回归命令速查
tests/                 27 个测试：纯单元 + 真实浏览器 E2E（无浏览器自动跳过）
fixtures/catalogues.json  自造的两馆双语公开目录
```

## 快速开始

需要 Python 3.12+。浏览器自动化依赖与浏览器版本必须配套：

```sh
pip install -r requirements.txt          # playwright==1.56.0
# 浏览器已由环境预置（chromium-1194）；全新环境再执行：
python -m playwright install chromium
```

### 1. 启动本地演示门户

```sh
python3 -I portal.py --port 8765 --state runtime/portal.json --test-mode
# 演示账号 demo / demo12345（只属于本地演示，可用 --user/--password 或环境变量覆盖）
```

`--test-mode` 仅开放 `/test/captcha/<id>` 答案接口供自动化回归；采集器在真实
人工登录场景不使用它。去掉该参数即与真实门户行为一致：只能人眼识别验证码。

### 2. 运行端到端演示（真实浏览器，无头）

```sh
python3 -I scripts/demo_e2e.py
```

依次演示：两个场馆反序完成且同名下载、会话中途失效后重新登录续收、进程退出
与门户重启后的任务续收。每个场景都有断言，结束后打印证据目录清单。

### 3. 执行采集

自动化演示登录（仅本地，验证码答案取自门户测试接口）：

```sh
python3 -I bin/collect \
  --url http://127.0.0.1:8765 \
  --out runtime/run \
  --request museum-a:zh,museum-b:zh,museum-a:en \
  --user demo --password demo12345 --test-captcha
```

真实使用方式——人工登录（有头；无显示器的机器用 Xvfb）：

```sh
Xvfb :99 -screen 0 1280x1024x24 &
DISPLAY=:99 python3 -I bin/collect --request museum-a:zh --headed
# 终端打印提示后，在浏览器窗口输入演示账号、密码与图形验证码；
# 进入 /workspace 后流程自动继续，采集器不读取或绕过验证码。
```

中断后原样重跑同一条命令即可：已 `verified` 的请求跳过，未完成任务先查门户
再决定续收或重建。

### 4. 回归测试

```sh
PYTHONPATH=. python3 -P -m unittest discover -s tests -v
```

## 证据与产物

每次运行的 `--out` 目录：

```
ledger.json     任务账本（任务标识、场馆/语言、epoch、创建/下载会话、校验结果、事件史）
events.ndjson   流式事件（登录世代、导出创建、浏览器下载观察、校验通过/失败）
catalogues/     校验通过的成品，按 catalogue_<场馆>_<语言>.csv 命名
.staging/       下载暂存区；失败/取消的半个文件不会进入 catalogues/
```

更详细的设计取舍、资源占用与误关联风险见 [docs/DESIGN.md](docs/DESIGN.md)，
运维与排障步骤见 [docs/OPERATIONS.md](docs/OPERATIONS.md)。

## 边界与免责

- 账号 `demo/demo12345` 与场馆目录均为本仓库自造，仅用于本地演示；
  不含任何第三方站点登录材料或真实单位业务记录。
- 流程只调用门户自身的导出/查询/下载能力，不做预约、付款等业务写入。
- 验证码与凭据环节留给人工；仓库不包含、也不会尝试获取真实凭据。
