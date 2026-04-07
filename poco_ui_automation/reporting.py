from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
from typing import Any

from jinja2 import Template

from .ai_strategy import StateGraphMemory
from .models import RunSummary


SUMMARY_TEMPLATE = Template(
    """
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <title>{{ summary.project_name }} UI 探索报告</title>
  <style>
    body { font-family: "Microsoft YaHei", sans-serif; margin: 24px; background: #0f172a; color: #e2e8f0; }
    h1, h2 { color: #f8fafc; }
    .card { background: #111827; border: 1px solid #334155; border-radius: 12px; padding: 16px; margin-bottom: 16px; }
    table { width: 100%; border-collapse: collapse; }
    th, td { border-bottom: 1px solid #334155; padding: 8px; text-align: left; vertical-align: top; }
    .ok { color: #4ade80; }
    .bad { color: #f87171; }
    code { background: #1e293b; padding: 2px 6px; border-radius: 6px; }
  </style>
</head>
<body>
  <h1>{{ summary.project_name }} UI 探索报告</h1>
  <div class="card">
    <p>目标：{{ summary.goal }}</p>
    <p>状态：<strong>{{ summary.status }}</strong></p>
    <p>动作数：{{ summary.actions|length }}，状态节点：{{ state_nodes|length }}，状态边：{{ state_edges|length }}</p>
  </div>

  <div class="card">
    <h2>覆盖摘要</h2>
    {% if summary.coverage %}
      <p>页面覆盖：{{ summary.coverage.visited_pages }} / 关键页面覆盖：{{ summary.coverage.critical_pages_covered }} / {{ summary.coverage.critical_pages_total }}</p>
      <p>路径数：{{ summary.coverage.path_count }}，成功动作：{{ summary.coverage.successful_actions }} / {{ summary.coverage.action_count }}</p>
      <p>已覆盖页面：{{ summary.coverage.covered_pages|join('、') or '-' }}</p>
    {% else %}
      <p>-</p>
    {% endif %}
  </div>

  <div class="card">
    <h2>动作流水</h2>
    <table>
      <thead>
        <tr>
          <th>#</th>
          <th>动作</th>
          <th>Selector</th>
          <th>结果</th>
          <th>耗时(ms)</th>
          <th>页面变化</th>
        </tr>
      </thead>
      <tbody>
      {% for action in summary.actions %}
        <tr>
          <td>{{ action.index }}</td>
          <td>{{ action.action_type }}</td>
          <td><code>{{ action.selector_key }}</code></td>
          <td class="{{ 'ok' if action.outcome == 'success' else 'bad' }}">{{ action.outcome }}</td>
          <td>{{ action.duration_ms }}</td>
          <td>{{ action.page_signature_before }} → {{ action.page_signature_after or '-' }}</td>
        </tr>
      {% endfor %}
      </tbody>
    </table>
  </div>

  <div class="card">
    <h2>异常问题</h2>
    <table>
      <thead>
        <tr>
          <th>类别</th>
          <th>级别</th>
          <th>标题</th>
          <th>页面</th>
          <th>复现路径</th>
        </tr>
      </thead>
      <tbody>
      {% for issue in summary.issues %}
        <tr>
          <td>{{ issue.category }}</td>
          <td>{{ issue.severity }}</td>
          <td>{{ issue.title }}</td>
          <td>{{ issue.page_name }}</td>
          <td>{{ issue.reproduction_path|join(' -> ') or '-' }}</td>
        </tr>
      {% else %}
        <tr>
          <td colspan="5">未记录到异常问题</td>
        </tr>
      {% endfor %}
      </tbody>
    </table>
  </div>

  <div class="card">
    <h2>性能采样</h2>
    <table>
      <thead>
        <tr>
          <th>范围</th>
          <th>平均帧时长</th>
          <th>P95</th>
          <th>P99</th>
          <th>Jank 比例</th>
        </tr>
      </thead>
      <tbody>
      {% for sample in summary.performance_samples %}
        <tr>
          <td>{{ sample.scope }}</td>
          <td>{{ sample.avg_frame_ms or '-' }}</td>
          <td>{{ sample.p95_frame_ms or '-' }}</td>
          <td>{{ sample.p99_frame_ms or '-' }}</td>
          <td>{{ sample.jank_ratio or '-' }}</td>
        </tr>
      {% endfor %}
      </tbody>
    </table>
  </div>

  <div class="card">
    <h2>AI 分析摘要</h2>
    {% if summary.ai_summary %}
      <p><strong>结论：</strong>{{ summary.ai_summary.get('conclusion', '-') }}</p>
      <p><strong>覆盖判断：</strong>{{ summary.ai_summary.get('coverage_assessment', '-') }}</p>
      <p><strong>风险判断：</strong>{{ summary.ai_summary.get('risk_assessment', '-') }}</p>
      <p><strong>建议：</strong>{{ summary.ai_summary.get('recommendations', [])|join('；') or '-' }}</p>
    {% else %}
      <p>-</p>
    {% endif %}
  </div>
</body>
</html>
"""
)


class ReportBuilder:
    def __init__(self, output_dir: str | Path) -> None:
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def build(
        self,
        summary: RunSummary,
        memory: StateGraphMemory,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, str]:
        payload = {
            "summary": asdict(summary),
            "state_nodes": [asdict(node) for node in memory.nodes.values()],
            "state_edges": [asdict(edge) for edge in memory.edges.values()],
            "metadata": metadata or {},
        }
        json_path = self.output_dir / "result.json"
        html_path = self.output_dir / "summary.html"
        mermaid_path = self.output_dir / "ui_graph.mmd"

        json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        html_path.write_text(
            SUMMARY_TEMPLATE.render(
                summary=payload["summary"],
                state_nodes=payload["state_nodes"],
                state_edges=payload["state_edges"],
            ),
            encoding="utf-8",
        )
        mermaid_path.write_text(self._build_mermaid(memory), encoding="utf-8")
        return {
            "json": str(json_path),
            "html": str(html_path),
            "mermaid": str(mermaid_path),
        }

    def _build_mermaid(self, memory: StateGraphMemory) -> str:
        lines = ["flowchart TD"]
        for node in memory.nodes.values():
            node_id = _safe_id(node.signature)
            lines.append(f'    {node_id}["{node.page_name}"]')
        for edge in memory.edges.values():
            from_id = _safe_id(edge.from_signature)
            to_id = _safe_id(edge.to_signature)
            label = f"{edge.selector_key} ({edge.action_type})"
            lines.append(f'    {from_id} -->|"{label}"| {to_id}')
        return "\n".join(lines) + "\n"


def _safe_id(raw: str) -> str:
    return "n_" + "".join(ch if ch.isalnum() else "_" for ch in raw)
