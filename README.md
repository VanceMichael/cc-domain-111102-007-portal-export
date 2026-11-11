# 授权门户的跨页导出 RPA 采集

这个 Python 项目保存采集侧的来源读取工具和合成资料。代码位于 app，示例输入位于 fixtures，现有读取工具的回归位于 tests。示例不代表任何真实单位的业务记录。

## 数据与接口

portal.py 是独立本地门户，运行 python3 portal.py --port 8765 --state /tmp/venue-portal.json。打开 /login 后点击演示登录，/workspace 嵌入 /frame，表单可选择场馆与语言。提交后得到 job_id，任务列表查询次数达到阈值后生成文件；museum-a 慢于 museum-b。/expire 可主动使当前会话失效。状态文件保存导出任务，Cookie 仅用于本地演示，所有下载名称均为 catalogue.csv。

## 本地使用

使用 Python 3.12 或更新版本，当前工具仅依赖标准库。从项目根目录执行：

```sh
python3 -m app.inspect
python3 -m unittest discover -s tests -v
python3 -m compileall -q app tests portal.py
```

读取命令输出当前素材的结构或单次读取结果。运行过程中产生的文件放在 runtime 中，与原始素材分开。公开示例仅使用 .invalid 域名，网络适配时应替换为已授权的来源。
