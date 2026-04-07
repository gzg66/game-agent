# Agent 单步决策协议

## 1. 文档目的

本文档定义 AI 手游测试 Agent 的单步决策协议，用于约束后续实现中的输入、输出、执行顺序、失败处理和可追踪性要求。

本协议服务于以下目标：

- 让 Agent 每一步的行为可解释
- 让 Agent 每一步的输入和输出结构化
- 让系统先生成候选动作，再由 AI 选择
- 让失败、回退、重试、异常检测都嵌入同一个闭环

后续开发中，如果需要新增 AI 决策能力，默认必须兼容本文档。

## 2. 核心原则

### 2.1 单步决策必须是闭环

每一步都必须包含以下环节：

1. 观察
2. 理解
3. 候选动作生成
4. 动作选择
5. 执行
6. 验证
7. 记忆更新
8. 异常检查
9. 下一步规划

### 2.2 决策前不直接暴露全量原始树给 AI

原始 UI 树可以参与系统分析，但不应直接作为大模型主输入。主输入应当是经过压缩和语义化后的摘要对象。

### 2.3 决策必须保留理由

AI 每步输出必须包含：

- 为什么选这个动作
- 预期会发生什么
- 如果失败准备怎么办

### 2.4 每一步都必须可回放

至少要保留：

- 观察摘要
- 候选动作集合
- AI 选择结果
- 执行结果
- 页面变化
- 异常信号

## 3. 单步决策总流程

单步循环固定为：

1. 拉取当前页面观测数据
2. 识别页面语义和模块语义
3. 从记忆中召回相似页面和历史成功动作
4. 生成候选动作
5. 对候选动作进行风险过滤与语义压缩
6. 构建 Agent 决策输入
7. 调用 AI 选择动作
8. 执行动作
9. 对比执行前后页面差异
10. 判断动作是否达成预期
11. 更新记忆和状态图
12. 检测异常
13. 生成下一步控制指令

## 4. 单步输入协议

## 4.1 输入对象

建议定义为 `AgentStepInput`

建议字段：

- `session_id`
- `run_id`
- `step_index`
- `current_goal`
- `current_module`
- `current_page`
- `page_summary`
- `recent_history`
- `candidate_actions`
- `known_risks`
- `known_constraints`
- `active_issues`
- `memory_hints`

## 4.2 字段解释

### `current_goal`

当前阶段目标，例如：

- 进入大厅
- 推进新手流程
- 进入战斗
- 完成结算
- 探索新模块

### `current_module`

系统当前判断所属模块，例如：

- login
- lobby
- newbie
- battle
- reward
- popup

### `current_page`

建议为结构化对象，而不是字符串。

建议字段：

- `page_signature`
- `page_name`
- `page_type`
- `semantic_tags`
- `dwell_time_ms`
- `confidence`

### `page_summary`

这是给 AI 的核心页面摘要，必须是压缩后的结果。

建议字段：

- `top_texts`
- `top_clickables`
- `dialog_detected`
- `reward_detected`
- `battle_detected`
- `blocking_signals`
- `suspicious_signals`
- `screenshot_caption`

### `recent_history`

最近若干步动作历史，建议保留 3 到 10 步。

每项建议包含：

- `step_index`
- `page_name_before`
- `action_intent`
- `success`
- `page_name_after`
- `summary`

### `candidate_actions`

必须是系统预先生成并排序的候选动作列表。

每项建议包含：

- `action_id`
- `selector_query`
- `semantic_intent`
- `reason`
- `risk_level`
- `priority_score`
- `expected_result`
- `source`

### `memory_hints`

用于告诉 AI 历史经验。

建议包含：

- 当前页面历史成功动作
- 当前模块典型推进路径
- 历史失败动作
- 历史危险动作
- 最近重复页面警告

## 5. 单步输出协议

## 5.1 输出对象

建议定义为 `AgentStepDecision`

建议字段：

- `chosen_action_id`
- `decision_type`
- `reason`
- `expected_outcome`
- `fallback_plan`
- `confidence`
- `page_judgement`
- `module_judgement`
- `risk_assessment`

## 5.2 字段解释

### `decision_type`

允许值建议包括：

- `execute_action`
- `retry_action`
- `back`
- `close_dialog`
- `switch_goal`
- `request_reobserve`
- `stop_run`

### `reason`

必须为可读文本，说明为什么选择这个动作。

### `expected_outcome`

必须说明预期会发生什么，例如：

- 进入大厅
- 关闭弹窗
- 进入战斗准备页
- 完成结算确认

### `fallback_plan`

至少包括一条失败后的备选策略，例如：

- 若点击后页面不变，则尝试返回
- 若进入商店，则立即退出
- 若未关闭弹窗，则尝试点右上角关闭

### `page_judgement`

Agent 对当前页面的主观判断。

建议字段：

- `page_type`
- `semantic_tags`
- `confidence`

### `module_judgement`

Agent 对当前模块阶段的判断。

建议字段：

- `module_name`
- `stage_name`
- `confidence`

### `risk_assessment`

建议输出：

- `low`
- `medium`
- `high`

## 6. 执行协议

## 6.1 执行对象

建议定义为 `ActionExecutionPlan`

建议字段：

- `step_index`
- `action_id`
- `selector_query`
- `semantic_intent`
- `decision_reason`
- `expected_outcome`
- `timeout_ms`
- `retry_limit`

## 6.2 执行要求

- 执行前记录当前页面签名
- 执行后必须重新观测页面
- 必须记录执行耗时
- 必须记录是否触发页面变化
- 必须允许超时
- 必须支持失败回传

## 7. 执行结果协议

建议定义为 `AgentStepResult`

建议字段：

- `step_index`
- `decision`
- `execution`
- `page_before`
- `page_after`
- `success`
- `state_changed`
- `goal_progressed`
- `diff_summary`
- `issues_detected`
- `memory_updates`
- `control_signal`

## 7.1 成功判定

动作是否成功，不能只看 click 是否成功，还应结合：

- 页面签名是否变化
- 页面语义是否变化
- 模块阶段是否推进
- 目标是否更接近

## 7.2 `control_signal`

用于决定下一轮循环。

建议取值：

- `continue`
- `retry`
- `backtrack`
- `reobserve`
- `switch_module`
- `stop`

## 8. 候选动作生成协议

候选动作生成必须发生在 AI 决策之前。

建议执行顺序：

1. 收集当前页面全部可点击节点
2. 召回历史成功动作
3. 补充通用动作模板
4. 打上语义标签
5. 计算优先级
6. 过滤危险动作
7. 保留少量探索动作
8. 输出前 N 个候选动作

建议控制候选动作数量：

- 默认 5 到 20 个
- 不要无限扩展

## 9. 风险过滤协议

高风险动作默认降权或阻断，例如：

- 充值
- 支付
- 购买
- 删除账号
- 领取付费礼包
- 跳转外部页面

风险动作可以存在于候选列表，但默认不应被直接高优先选择。

## 10. 重试与回退协议

### 10.1 可以重试的情况

- 目标控件短暂未加载
- 页面轻微卡顿
- 预期弹窗未完全展开

### 10.2 不应盲目重试的情况

- 连续多次点击无变化
- 页面已明显循环
- 已进入危险区域
- 已命中异常信号

### 10.3 回退触发条件

- 页面进入错误模块
- 页面进入商业化区域
- 页面与目标明显偏离
- 连续多步无推进

## 11. 异常检测挂接点

异常检测必须至少挂在以下时机：

1. 执行动作前
2. 执行动作后
3. 页面长时间停留时
4. 模块切换时
5. 运行结束时

建议每步输出 `issues_detected`，即使为空也应明确写出。

## 12. 记忆更新协议

每一步结束后都需要更新记忆。

建议更新内容：

- 页面签名与页面语义映射
- 控件与语义角色映射
- 动作成功率
- 动作失败率
- 页面转移边
- 模块推进路径
- 新发现异常

## 13. 推荐的 AI 提示词结构

如果后续引入 LLM，建议输入结构如下：

1. 当前测试目标
2. 当前页面摘要
3. 最近历史
4. 候选动作
5. 风险提示
6. 历史经验提示
7. 输出格式约束

禁止：

- 直接塞全量原始 UI 树
- 不限制输出格式
- 不限制候选动作选择范围

## 14. 输出格式约束建议

建议要求 AI 严格输出 JSON，字段固定，例如：

```json
{
  "chosen_action_id": "action_3",
  "decision_type": "execute_action",
  "reason": "当前页面更像登录后的大厅弹窗，优先关闭后继续主流程。",
  "expected_outcome": "关闭弹窗并回到大厅主界面",
  "fallback_plan": "若页面无变化，则尝试点击返回或右上角关闭按钮",
  "confidence": 0.82,
  "page_judgement": {
    "page_type": "popup",
    "semantic_tags": ["大厅弹窗", "奖励提示"],
    "confidence": 0.76
  },
  "module_judgement": {
    "module_name": "lobby",
    "stage_name": "popup_cleanup",
    "confidence": 0.74
  },
  "risk_assessment": "low"
}
```

## 15. 开发约束

后续实现 Agent 单步决策时，必须遵守：

1. 不允许 AI 直接从全量原始控件中自由选择任何字符串。
2. 不允许没有候选动作生成层就直接接 LLM。
3. 不允许只记录最终点击结果，不记录决策理由。
4. 不允许把异常检测放到整轮结束后再统一分析。
5. 不允许把失败处理全塞进 runner，必须进入统一协议。

## 16. 验收标准

满足以下条件时，认为单步决策协议落地正确：

- 每一步都有结构化输入对象
- 每一步都有结构化输出对象
- 每一步都能回放“为什么这样选”
- 每一步都能判断是否推进目标
- 每一步都能输出异常信号
- 失败、重试、回退都进入统一控制流

## 17. 一句话总结

Agent 单步决策不是“点一下按钮”，而是“观察、理解、选择、执行、验证、学习”的完整闭环。
