"""冷启动探索报告生成。

根据设计文档，冷启动报告应包含：
- 新发现页面数 / 已归并页面数
- 主要模块入口
- 高频控件语义标签
- 高价值转移路径
- 高风险页面与动作
- 冷启动阶段异常
- 建议进入自主游玩的初始目标
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from .explorer import ColdStartResult
from .state_graph import ExplorationGraph


class ColdStartReportBuilder:
    """生成冷启动探索的验收报告。"""

    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def build(self, result: ColdStartResult) -> dict[str, str]:
        """生成报告文件，返回文件路径字典。"""
        report_data = self._compile_report(result)

        # JSON 报告
        json_path = self.output_dir / "cold_start_report.json"
        json_path.write_text(
            json.dumps(report_data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        # Markdown 报告
        md_path = self.output_dir / "cold_start_report.md"
        md_path.write_text(self._render_markdown(report_data), encoding="utf-8")

        return {
            "json": str(json_path),
            "markdown": str(md_path),
        }

    def _compile_report(self, result: ColdStartResult) -> dict[str, Any]:
        """编译报告数据。"""
        graph = result.graph

        # 1. 页面统计
        page_categories = Counter(
            p.category for p in graph.pages.values()
        )
        module_entries = [
            {"page_id": p.page_id, "title": p.title, "category": p.category}
            for p in graph.pages.values()
            if p.category in {"lobby", "shop", "battle_prepare", "battle_running"}
        ]

        # 2. 控件语义统计
        role_counter: Counter[str] = Counter()
        for exec_ in result.executions:
            role_counter[exec_.action.role.value] += 1

        # 3. 高价值转移路径（页面变化的成功转移）
        valuable_edges = [
            {
                "from": graph.pages.get(e.from_signature, None),
                "to": graph.pages.get(e.to_signature, None),
                "action": e.action_label,
                "role": e.action_role,
                "success_rate": f"{e.success_rate:.0%}",
            }
            for e in graph.edges
            if e.is_page_changed and e.success_count > 0
        ]
        # 格式化
        formatted_edges = []
        for ve in valuable_edges:
            from_p = ve["from"]
            to_p = ve["to"]
            formatted_edges.append({
                "from_page": from_p.title if from_p else "unknown",
                "from_category": from_p.category if from_p else "unknown",
                "to_page": to_p.title if to_p else "unknown",
                "to_category": to_p.category if to_p else "unknown",
                "action": ve["action"],
                "role": ve["role"],
                "success_rate": ve["success_rate"],
            })

        # 4. 高风险页面与动作
        risk_pages = [
            {"page_id": p.page_id, "title": p.title, "category": p.category}
            for p in graph.high_risk_pages()
        ]
        risk_actions = [
            e.to_dict() for e in result.executions
            if e.action.risk_level >= 2
        ]

        # 5. 高频页面
        freq_pages = [
            {"page_id": p.page_id, "title": p.title, "visits": p.visit_count}
            for p in graph.high_frequency_pages(min_visits=2)
        ]

        # 6. 弹窗页面
        popup_list = [
            {"page_id": p.page_id, "title": p.title}
            for p in graph.popup_pages()
        ]

        # 7. 建议初始目标
        suggestions = self._generate_suggestions(graph, result)
        semantic_stats = dict(result.semantic_stats)

        return {
            "overview": {
                "status": result.status,
                "stop_reason": result.stop_reason,
                "total_steps": result.total_steps,
                "new_pages_found": result.new_pages_found,
                "total_pages": graph.page_count,
                "total_edges": graph.edge_count,
                "total_executions": len(result.executions),
                "crash_count": len(result.crashes),
                "started_at": result.started_at,
                "finished_at": result.finished_at,
            },
            "page_categories": dict(page_categories),
            "module_entries": module_entries,
            "control_role_stats": dict(role_counter),
            "high_value_transitions": formatted_edges[:20],
            "high_frequency_pages": freq_pages,
            "popup_pages": popup_list,
            "high_risk_pages": risk_pages,
            "high_risk_actions": risk_actions[:10],
            "crashes": result.crashes,
            "semantic_stats": semantic_stats,
            "suggestions": suggestions,
        }

    def _generate_suggestions(
        self,
        graph: ExplorationGraph,
        result: ColdStartResult,
    ) -> list[str]:
        """根据探索结果生成建议。"""
        suggestions: list[str] = []

        if graph.page_count < 3:
            suggestions.append("发现页面过少，建议增大 max_steps 或调整 action_wait_s")

        if result.stop_reason == "ui_tree_not_exposed_android_uiautomation":
            suggestions.append("当前构建仅暴露了 Android 原生外壳层级，游戏内画布控件未进入 UI 树")
            suggestions.append("建议使用接入 Unity/Cocos Poco SDK 的测试包，或补充基于截图/OCR 的点击兜底")

        if result.crashes:
            suggestions.append(f"探索中发生 {len(result.crashes)} 次崩溃，建议检查游戏稳定性")

        lobby_pages = [p for p in graph.pages.values() if p.category == "lobby"]
        if lobby_pages:
            suggestions.append(f"已识别到大厅页面，建议从 {lobby_pages[0].title} 开始自主游玩")

        unknown_pages = [p for p in graph.pages.values() if p.category == "unknown"]
        if len(unknown_pages) > graph.page_count * 0.5:
            suggestions.append("未识别类型的页面占比过高，建议丰富 page_type_hints 配置")

        if not suggestions:
            suggestions.append("冷启动探索正常完成，可进入下一阶段自主游玩")

        return suggestions

    def _render_markdown(self, data: dict[str, Any]) -> str:
        """渲染 Markdown 报告。"""
        lines: list[str] = []
        ov = data["overview"]

        lines.append("# 冷启动探索报告\n")
        lines.append("## 概览\n")
        lines.append(f"| 指标 | 值 |")
        lines.append(f"|------|------|")
        lines.append(f"| 状态 | {ov['status']} |")
        lines.append(f"| 停止原因 | {ov['stop_reason']} |")
        lines.append(f"| 总步数 | {ov['total_steps']} |")
        lines.append(f"| 发现页面数 | {ov['new_pages_found']} |")
        lines.append(f"| 总页面数 | {ov['total_pages']} |")
        lines.append(f"| 转移边数 | {ov['total_edges']} |")
        lines.append(f"| 执行动作数 | {ov['total_executions']} |")
        lines.append(f"| 崩溃次数 | {ov['crash_count']} |")
        lines.append(f"| 开始时间 | {ov['started_at']} |")
        lines.append(f"| 结束时间 | {ov['finished_at']} |")
        lines.append("")

        lines.append("## 页面类型分布\n")
        for cat, count in data["page_categories"].items():
            lines.append(f"- **{cat}**: {count}")
        lines.append("")

        lines.append("## 主要模块入口\n")
        for entry in data["module_entries"]:
            lines.append(f"- [{entry['category']}] {entry['title']} ({entry['page_id']})")
        if not data["module_entries"]:
            lines.append("- 未识别到主要模块入口")
        lines.append("")

        lines.append("## 控件语义统计\n")
        for role, count in data["control_role_stats"].items():
            lines.append(f"- **{role}**: {count} 次")
        lines.append("")

        lines.append("## 语义缓存与 LLM 收益\n")
        semantic_stats = data.get("semantic_stats", {})
        if semantic_stats:
            lines.append(f"- 页面分析次数: {semantic_stats.get('pages_analyzed', 0)}")
            lines.append(f"- 缓存命中页面: {semantic_stats.get('cache_hit_pages', 0)}")
            lines.append(f"- 缓存未命中页面: {semantic_stats.get('cache_miss_pages', 0)}")
            lines.append(f"- 缓存命中率: {semantic_stats.get('cache_hit_rate', 0.0):.2%}")
            lines.append(f"- LLM 提交页面: {semantic_stats.get('llm_submitted_pages', 0)}")
            lines.append(f"- LLM 完成页面: {semantic_stats.get('llm_completed_pages', 0)}")
            lines.append(f"- LLM 候选节点: {semantic_stats.get('llm_candidate_nodes', 0)}")
            lines.append(f"- LLM 增强节点: {semantic_stats.get('llm_enriched_nodes', 0)}")
            lines.append(f"- 缓存节省的 LLM 调用: {semantic_stats.get('llm_calls_saved_by_cache', 0)}")
            lines.append(f"- 平均 LLM 延迟: {semantic_stats.get('avg_llm_latency_ms', 0.0)} ms")
        else:
            lines.append("- 暂无语义缓存 / LLM 统计")
        lines.append("")

        lines.append("## 高价值转移路径\n")
        if data["high_value_transitions"]:
            lines.append("| 来源页面 | 目标页面 | 动作 | 角色 | 成功率 |")
            lines.append("|----------|----------|------|------|--------|")
            for t in data["high_value_transitions"][:15]:
                lines.append(
                    f"| {t['from_page']} | {t['to_page']} | {t['action']} "
                    f"| {t['role']} | {t['success_rate']} |"
                )
        else:
            lines.append("暂无高价值转移路径")
        lines.append("")

        lines.append("## 高风险页面\n")
        for rp in data["high_risk_pages"]:
            lines.append(f"- ⚠ {rp['title']} ({rp['page_id']})")
        if not data["high_risk_pages"]:
            lines.append("- 未发现高风险页面")
        lines.append("")

        lines.append("## 崩溃记录\n")
        if data["crashes"]:
            for crash in data["crashes"]:
                lines.append(f"- 步骤 {crash.get('step', '?')}: {crash.get('crash_type', 'unknown')}")
        else:
            lines.append("- 无崩溃记录")
        lines.append("")

        lines.append("## 建议\n")
        for s in data["suggestions"]:
            lines.append(f"- {s}")
        lines.append("")

        return "\n".join(lines)
