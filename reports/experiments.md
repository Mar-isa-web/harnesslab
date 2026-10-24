# HarnessLab 遥测复盘报告

> 数据源: `data/bench.db` ｜ 生成时间: 2026-10-24 13:23

## 1. 总览

- 轮次总数: **74**（completed 54，error 20）
- 耗时: 中位 3.6s ｜ P90 6.9s ｜ 最长 78.9s
- 累计 tokens: 输入 231.3k ｜ 输出 18.1k

## 2. 工具画像

| 工具 | 调用次数 | 成功率 | 平均耗时 |
|---|---|---|---|
| run_shell | 120 | 99% | 0.6s |
| read_file | 67 | 99% | 0.0s |
| write_file | 49 | 100% | 0.0s |
| list_dir | 25 | 96% | 0.0s |
| flaky_task | 3 | 33% | 0.0s |

## 3. 模型请求与失败信号

- 模型请求共 332 次，其中失败 61 次
  - `http_429`: 41 次
  - `http_401`: 20 次

## 4. 基准通过率（按轮次）

| run_label | 模型 | 通过 | 任务数 | 通过率 | 总耗时 |
|---|---|---|---|---|---|
| baseline | deepseek-chat | 10 | 10 | 100% | 43.3s |
| ab-backoff | deepseek-chat | 9 | 10 | 90% | 36.5s |
| ab-sliding | deepseek-chat | 9 | 10 | 90% | 39.7s |
| fault-immediate | deepseek-chat | 0 | 10 | 0% | 0.1s |
| fault-backoff | deepseek-chat | 10 | 10 | 100% | 125.6s |
| reasoner-probe | deepseek-reasoner | 2 | 2 | 100% | 8.9s |
| model-reasoner | deepseek-reasoner | 9 | 10 | 90% | 77.5s |

失败任务明细: count_md(判分不过)，merge_sorted(判分不过)，count_lines(http_429)，sum_numbers(http_429)，top_word(http_429)，find_largest(http_429)，count_md(http_429)，find_keyword(http_429)，merge_sorted(http_429)，uppercase(http_429)，add_two_files(http_429)，char_count(http_429)，count_md(判分不过)

## 5. 模型对比

| 模型 | 请求数 | 失败 | 平均耗时 | 平均输出token |
|---|---|---|---|---|
| deepseek-chat | 248 | 61 | 0.7s | 42 |
| deepseek-reasoner | 84 | 0 | 1.0s | 91 |
