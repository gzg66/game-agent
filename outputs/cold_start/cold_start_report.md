# 冷启动探索报告

## 概览

| 指标 | 值 |
|------|------|
| 状态 | completed |
| 停止原因 | exploration_completed |
| 总步数 | 0 |
| 发现页面数 | 2 |
| 总页面数 | 2 |
| 转移边数 | 9 |
| 执行动作数 | 9 |
| 崩溃次数 | 0 |
| 开始时间 | 2026-04-07T10:31:01+00:00 |
| 结束时间 | 2026-04-07T10:33:30+00:00 |

## 页面类型分布

- **unknown**: 2

## 主要模块入口

- 未识别到主要模块入口

## 控件语义统计

- **primary_entry**: 1 次
- **back**: 2 次
- **unknown**: 6 次

## 高价值转移路径

| 来源页面 | 目标页面 | 动作 | 角色 | 成功率 |
|----------|----------|------|------|--------|
| Start | basic | btn_start [Start] | primary_entry | 100% |
| basic | Start | btn_back [Back] | back | 100% |
| basic | Start | basic | unknown | 100% |
| basic | Start | drag_and_drop [drag drop] | unknown | 100% |
| basic | Start | list_view [list view] | unknown | 100% |
| basic | Start | local_positioning [local positioning] | unknown | 100% |
| basic | Start | wait_ui [wait UI] | unknown | 100% |
| basic | Start | wait_ui2 [wait UI 2] | unknown | 100% |

## 高风险页面

- 未发现高风险页面

## 崩溃记录

- 无崩溃记录

## 建议

- 发现页面过少，建议增大 max_steps 或调整 action_wait_s
- 未识别类型的页面占比过高，建议丰富 page_type_hints 配置
