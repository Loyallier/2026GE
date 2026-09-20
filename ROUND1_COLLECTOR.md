# 第一轮全量采集器

目标只有一个：**先完整保存第一轮页面，分析以后再做。**

这个采集器不会提交选课、退课或其他状态修改请求。它只会：

1. 打开可见 Chromium；
2. 由你手动登录 AC Online；
3. 你进入第一轮选课页面后，程序自动识别 `c=Xk`；
4. 持续刷新当前页面；
5. 每一轮保存完整 DOM，并通用提取页面中所有 `<table>`。

它不依赖 `table[2]`、`data_table`、课程列位置或固定页码，因此即使已选课程出现在页面上方，也不会影响原始数据留存。

## 安装

```bash
pip install -r requirements-collector.txt
playwright install chromium
```

## 运行

## 推荐：正式采集使用直接 URL 模式

如果已经知道目标选课页面的真实 URL，推荐直接：

```powershell
python round1_collector.py --url "https://ac.xmu.edu.my/student/index.php?c=Xk&a=view&id=531"
```

流程会变成：

1. 打开 Chromium；
2. 你手动登录；
3. 程序检测登录页消失；
4. 自动进入 `--url` 指定页面；
5. 立即开始连续采集。

这个模式不依赖首页菜单、iframe、AJAX 导航或 PowerShell Enter 交互，是正式第一轮最推荐的运行方式。


```bash
python round1_collector.py
```

浏览器打开后：

- 手动登录；
- 手动进入第一轮选课课程列表页；
- 检测到 `c=Xk` 页面后程序会自动开始采集。

默认行为是：每次页面加载完成后等待 1 秒，再刷新下一轮。实际采样间隔还包含服务器页面加载时间。

如需修改：

```bash
python round1_collector.py --interval 2
```

停止：

```text
Ctrl+C
```

> Windows / PowerShell：请确保焦点在 PowerShell 窗口里再按 Ctrl+C。新版会直接退出，不再等待下一轮。
>
> 如果你直接手动关闭 Chromium，新版也会检测到并自动结束 Python 采集进程。

如果旧版本已经出现“浏览器关了但 Python 还在”的情况，可以在 PowerShell 中定位并强制停止：

```powershell
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Select-Object ProcessId, CommandLine

Stop-Process -Id <PID> -Force
```

页面窗口现在使用 Chromium 原生 viewport，可以自由缩放窗口；页面缩放使用：

```text
Ctrl+-   缩小
Ctrl+=   放大
Ctrl+0   恢复 100%
```

## 数据

每次运行创建一个独立目录：

```text
collector_data/
  round1_YYYYMMDD_HHMMSS/
    snapshots.sqlite3
    timeline.jsonl
    errors/
```

### snapshots.sqlite3

每个 snapshot 都保存：

- 精确本地时间；
- 页面 URL；
- HTTP 状态；
-服务器 Date header（存在时）；
- 页面加载耗时；
- 完整 DOM HTML（gzip 压缩）；
- 页面全部 HTML table 的所有行/单元格文本；
- 异常信息。

完整 HTML 按 SHA-256 去重保存，但 snapshot 时间线不会去重：**即使页面连续多次完全相同，也会留下每一次采样记录。**

### timeline.jsonl

作为 SQLite 之外的第二份追加式时间线备份，每次 snapshot 写完都会 flush + fsync。

### errors/

出现“页面没有 table / 行数骤降 / 保存异常”等情况时，额外保存截图，但程序不会因为解析异常而丢弃原始 HTML。

## 设计原则

采集阶段不做：

- 周期预测；
- 人数变化判断；
- 课程筛选；
- 自动选课；
- 固定表格索引解析。

如果页面结构今年发生变化，最坏情况也应该只是“后续需要重新解析保存下来的 HTML”，而不是当场丢数据。


## 实时查看

采集器会先保存原始快照，然后再尝试解析当前 `data_table` 作为实时视图。实时解析失败不会影响原始采集。

首次识别成功时，PowerShell 会打印当前全部课程；之后只有申请人数发生变化时才打印，例如：

```text
[LIVE] 当前课程人数 / 变化
  G0111    Elementary Number Theory ...              申请  132 / 86    Δ +1
```

每轮还会更新：

```text
collector_data/round1_.../live_latest.csv
```

它是“当前最新状态”，完整历史仍以 `snapshots.sqlite3` 为准。

Windows 上不要长期用 Excel 打开 `live_latest.csv`，Excel 可能锁文件导致实时 CSV 暂时无法替换；PowerShell 中的 LIVE 输出和 SQLite 原始采集不受影响。


## 旧协议保底采集器

`legacy_protocol_collector.py` 保留去年 `ac_sniper_V2.py / ac_checkpoints_v2.py` 的核心协议：

```text
GET Random 页面
→ 提取 __VIEWSTATE
→ POST $All
→ 解析第 1 页
→ 更新 __VIEWSTATE
→ POST $Page / $2
→ 解析第 2 页
```

改动只有登录、存储和调度：

- 登录：弹 Chromium 手动登录，再把浏览器 Cookie 注入 `requests.Session`
- 存储：每轮课程数据 + GET/$All/$Page 原始 HTML 全部保存到 SQLite
- 调度：固定高频循环，不做周期预测

正式开放后，确认本轮 Random URL，例如：

```powershell
python legacy_protocol_collector.py --url "https://ac.xmu.edu.my/student/index.php?c=Xk&a=Random&id=1402" --pages 2 --interval 3
```

`id=1402` 是旧值，正式运行前必须确认当轮 ID。

它使用独立的 `.legacy_protocol_profile/`，因此可以和 `round1_collector.py` 同时运行，但需要各自完成一次浏览器登录。

注意：旧逻辑会 POST `$All`，也就是切到 All Courses 视图。这是刻意保留的旧行为。
