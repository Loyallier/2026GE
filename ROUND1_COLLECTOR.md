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
