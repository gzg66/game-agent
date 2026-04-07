from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, replace
from datetime import datetime
import json
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from poco_ui_automation import (  # noqa: E402
    AirtestPocoDriver,
    AutomationSession,
    EngineType,
    ProjectProfile,
    ReportBuilder,
    RuleScenario,
)
from poco_ui_automation.models import CoverageStats, IssueRecord, RunSummary  # noqa: E402


def parse_csv(raw_value: str) -> list[str]:
    return [item.strip() for item in raw_value.split(",") if item.strip()]


def device_serial_from_uri(device_uri: str) -> str:
    if ":///" in device_uri:
        return device_uri.split(":///", 1)[1]
    if "://" in device_uri:
        return device_uri.split("://", 1)[1]
    return device_uri


def adb_path() -> Path:
    import airtest

    return (
        Path(airtest.__file__).resolve().parent
        / "core"
        / "android"
        / "static"
        / "adb"
        / "windows"
        / "adb.exe"
    )


def run_adb(device_serial: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(adb_path()), "-s", device_serial, *args],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
    )


def launch_game_package(device_uri: str, package_name: str) -> None:
    device_serial = device_serial_from_uri(device_uri)
    result = run_adb(
        device_serial,
        "shell",
        "monkey",
        "-p",
        package_name,
        "-c",
        "android.intent.category.LAUNCHER",
        "1",
    )
    if result.returncode != 0:
        raise RuntimeError(f"自动启动游戏失败: {result.stderr.strip() or result.stdout.strip()}")


def restart_game_package(device_uri: str, package_name: str, startup_wait_seconds: float) -> None:
    device_serial = device_serial_from_uri(device_uri)
    run_adb(device_serial, "shell", "am", "force-stop", package_name)
    time.sleep(2.0)
    launch_game_package(device_uri, package_name)
    if startup_wait_seconds > 0:
        time.sleep(startup_wait_seconds)


def app_pid(device_serial: str, package_name: str) -> str | None:
    result = run_adb(device_serial, "shell", "pidof", package_name)
    pid = (result.stdout or "").strip()
    return pid or None


def app_is_running(device_serial: str, package_name: str) -> bool:
    return app_pid(device_serial, package_name) is not None


def clear_logcat(device_serial: str) -> None:
    run_adb(device_serial, "logcat", "-c")


def read_logcat_tail(device_serial: str, lines: int) -> list[str]:
    result = run_adb(device_serial, "logcat", "-d", "-t", str(lines))
    raw = result.stdout or result.stderr or ""
    return [line.strip() for line in raw.splitlines() if line.strip()]


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def write_jsonl_line(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"time": now_iso(), **payload}, ensure_ascii=False) + "\n")


@dataclass(slots=True)
class ModulePlan:
    name: str
    goal: str
    max_states: int
    preferred_keywords: list[str]
    blocked_keywords: list[str]
    expected_pages: list[str]


def build_driver(profile: ProjectProfile, args: argparse.Namespace) -> AirtestPocoDriver:
    return AirtestPocoDriver(
        engine_type=profile.engine_type,
        device_uri=args.device_uri,
        package_name=profile.package_name,
        cocos_addr=(args.host, args.port),
    )


def build_profile(args: argparse.Namespace) -> ProjectProfile:
    if args.profile:
        profile = ProjectProfile.load(args.profile)
        if args.package:
            profile.package_name = args.package
        if args.project_name:
            profile.project_name = args.project_name
        if args.version_tag:
            profile.version_tags = list(dict.fromkeys([*profile.version_tags, args.version_tag]))
        return profile

    if not args.package:
        raise SystemExit("未提供 --package，且没有传入 --profile。至少需要提供其中一种。")

    return ProjectProfile(
        project_name=args.project_name,
        engine_type=EngineType.COCOS2DX_JS,
        package_name=args.package,
        ui_root_candidates=parse_csv(args.ui_roots) or ["root", "Canvas", "UIRoot", "MainScene"],
        startup_wait_seconds=int(args.startup_wait),
        popup_blacklist=parse_csv(args.popup_blacklist),
        critical_pages=parse_csv(args.critical_pages) or ["login", "lobby", "battle"],
        metric_sampling={"fps": True, "stutter": True, "memory": False},
        selector_aliases={
            "close": ["close", "关闭", "跳过", "skip"],
            "back": ["back", "返回"],
            "confirm": ["确认", "确定", "ok", "confirm"],
        },
        dangerous_actions=parse_csv(args.dangerous_actions) or ["充值", "支付", "购买", "删除账号"],
        page_signatures={
            "login": ["开始游戏", "游客登录", "账号登录"],
            "lobby": ["冒险", "背包", "任务", "邮件"],
            "battle": ["自动", "暂停", "跳过"],
        },
        module_scenarios=[
            {
                "name": "newbie",
                "goal": "大厅",
                "max_states": int(args.max_states),
                "preferred_keywords": ["开始", "进入", "下一步", "跳过", "领取", "确认"],
                "blocked_keywords": parse_csv(args.blocked_keywords),
                "expected_pages": ["login", "lobby"],
            },
            {
                "name": "battle",
                "goal": "战斗",
                "max_states": int(args.max_states),
                "preferred_keywords": ["冒险", "出战", "挑战", "自动", "结算", "再次挑战"],
                "blocked_keywords": parse_csv(args.blocked_keywords),
                "expected_pages": ["battle"],
            },
        ],
        runtime_guard={
            "cruise_minutes": 0,
            "max_rounds": 1,
            "restart_on_crash": True,
            "stall_repeat_rounds": 3,
        },
        issue_detection={
            "enable_numeric_check": True,
            "enable_resource_check": True,
            "enable_log_error_check": True,
        },
        report_preferences={"emit_markdown": True},
        log_keywords=[" error ", " exception", " assert", " fatal "],
        version_tags=[args.version_tag] if args.version_tag else [],
    )


def build_scenario(args: argparse.Namespace) -> RuleScenario | None:
    preferred_keywords = parse_csv(args.preferred_keywords)
    blocked_keywords = parse_csv(args.blocked_keywords)
    expected_pages = parse_csv(args.expected_pages)
    if not preferred_keywords and not blocked_keywords and not expected_pages:
        return None
    return RuleScenario(
        name="xingtu_tiancheng_mumu",
        preferred_keywords=preferred_keywords,
        blocked_keywords=blocked_keywords,
        expected_pages=expected_pages,
    )


def summarize_nodes(session: AutomationSession, limit: int = 12) -> dict[str, object]:
    snapshot = session.refresh_snapshot("preflight")
    preview = []
    for node in snapshot.nodes[:limit]:
        label = node.text or node.name
        if not label:
            continue
        preview.append(
            {
                "label": label,
                "name": node.name,
                "text": node.text,
                "clickable": node.clickable,
            }
        )
    return {
        "page_name": snapshot.page_name,
        "signature": snapshot.signature,
        "node_count": len(snapshot.nodes),
        "preview_nodes": preview,
    }


def build_module_plans(profile: ProjectProfile, args: argparse.Namespace) -> list[ModulePlan]:
    configured = profile.module_scenarios or []
    selected = set(parse_csv(args.modules)) if args.modules else set()
    plans: list[ModulePlan] = []
    for index, item in enumerate(configured, start=1):
        name = str(item.get("name") or f"module_{index}")
        if selected and name not in selected:
            continue
        plans.append(
            ModulePlan(
                name=name,
                goal=str(item.get("goal") or args.goal),
                max_states=int(item.get("max_states") or args.max_states),
                preferred_keywords=[str(value) for value in item.get("preferred_keywords", []) if str(value).strip()],
                blocked_keywords=[str(value) for value in item.get("blocked_keywords", []) if str(value).strip()],
                expected_pages=[str(value) for value in item.get("expected_pages", []) if str(value).strip()],
            )
        )
    if plans:
        return plans

    fallback = build_scenario(args)
    return [
        ModulePlan(
            name="default",
            goal=args.goal,
            max_states=args.max_states,
            preferred_keywords=fallback.preferred_keywords if fallback else [],
            blocked_keywords=fallback.blocked_keywords if fallback else [],
            expected_pages=fallback.expected_pages if fallback else [],
        )
    ]


def runtime_guard(profile: ProjectProfile, args: argparse.Namespace) -> dict[str, Any]:
    guard = dict(profile.runtime_guard or {})
    guard.setdefault("cruise_minutes", float(args.cruise_minutes))
    guard.setdefault("max_rounds", int(args.max_rounds))
    guard.setdefault("restart_on_crash", bool(args.restart_on_crash))
    guard.setdefault("stall_repeat_rounds", int(args.stall_repeat_rounds))
    guard.setdefault("module_pause_seconds", float(args.module_pause_seconds))
    return guard


def issue_detection_config(profile: ProjectProfile, args: argparse.Namespace) -> dict[str, Any]:
    config = dict(profile.issue_detection or {})
    config.setdefault("enable_log_error_check", True)
    config.setdefault("enable_numeric_check", True)
    config.setdefault("enable_resource_check", True)
    config.setdefault("logcat_lines", int(args.logcat_lines))
    return config


def build_rule_scenario(plan: ModulePlan) -> RuleScenario | None:
    if not plan.preferred_keywords and not plan.blocked_keywords and not plan.expected_pages:
        return None
    return RuleScenario(
        name=plan.name,
        preferred_keywords=plan.preferred_keywords,
        blocked_keywords=plan.blocked_keywords,
        expected_pages=plan.expected_pages,
    )


def action_trace(actions: list[Any], limit: int = 6) -> list[str]:
    trace: list[str] = []
    for action in actions[-limit:]:
        trace.append(f"{action.action_type}:{action.selector_key}:{action.outcome}")
    return trace


def tail_signature(actions: list[Any]) -> str | None:
    for action in reversed(actions):
        if action.page_signature_after:
            return action.page_signature_after
    return None


def detect_block_issue(
    plan: ModulePlan,
    before_signature: str,
    module_summary: RunSummary,
    stagnant_rounds: int,
    threshold: int,
) -> IssueRecord | None:
    if not module_summary.actions:
        return IssueRecord(
            category="阻塞",
            severity="high",
            title=f"模块 {plan.name} 未产生有效动作",
            page_name=plan.goal,
            page_signature=before_signature,
            reproduction_path=[],
            details={"module": plan.name, "reason": "无动作产出"},
        )
    success_count = sum(1 for item in module_summary.actions if item.outcome == "success")
    unique_targets = {item.page_signature_after for item in module_summary.actions if item.page_signature_after}
    if success_count == 0:
        return IssueRecord(
            category="阻塞",
            severity="high",
            title=f"模块 {plan.name} 全部动作失败",
            page_name=plan.goal,
            page_signature=before_signature,
            action_index=module_summary.actions[-1].index,
            action_label=module_summary.actions[-1].selector_key,
            reproduction_path=action_trace(module_summary.actions),
            details={"module": plan.name, "reason": "全部点击失败"},
        )
    if unique_targets == {before_signature} or stagnant_rounds >= threshold:
        return IssueRecord(
            category="阻塞",
            severity="medium",
            title=f"模块 {plan.name} 连续停留在同一页面",
            page_name=plan.goal,
            page_signature=before_signature,
            action_index=module_summary.actions[-1].index,
            action_label=module_summary.actions[-1].selector_key,
            reproduction_path=action_trace(module_summary.actions),
            details={"module": plan.name, "stagnant_rounds": stagnant_rounds, "target_count": len(unique_targets)},
        )
    return None


def detect_numeric_anomalies(session: AutomationSession, module_name: str) -> list[IssueRecord]:
    issues: list[IssueRecord] = []
    try:
        snapshot = session.refresh_snapshot("numeric_check")
    except Exception:
        return issues
    suspect_labels: list[str] = []
    for node in snapshot.nodes:
        label = (node.text or node.name).strip()
        if not label:
            continue
        lower = label.lower()
        if any(keyword in lower for keyword in ("奖励", "金币", "钻石", "体力", "exp", "coin", "gold", "gem")):
            if re.search(r"(-\d+|nan|inf|\?\?\?)", lower):
                suspect_labels.append(label)
    if suspect_labels:
        issues.append(
            IssueRecord(
                category="数值错误",
                severity="medium",
                title=f"模块 {module_name} 发现疑似数值异常",
                page_name=snapshot.page_name,
                page_signature=snapshot.signature,
                reproduction_path=suspect_labels[:6],
                details={"suspect_values": suspect_labels[:10]},
            )
        )
    return issues


def detect_resource_gaps(session: AutomationSession, module_name: str) -> list[IssueRecord]:
    issues: list[IssueRecord] = []
    try:
        snapshot = session.refresh_snapshot("resource_check")
    except Exception:
        return issues
    suspect_labels: list[str] = []
    for node in snapshot.nodes:
        label = (node.text or node.name).strip()
        lower = label.lower()
        if any(keyword in lower for keyword in ("missing", "null", "none", "todo", "placeholder", "default", "未命名", "image_")):
            suspect_labels.append(label)
        if node.clickable and not node.text and node.name.lower().startswith(("btn_", "icon_", "img_")):
            suspect_labels.append(node.name)
    if suspect_labels:
        issues.append(
            IssueRecord(
                category="资源缺失",
                severity="medium",
                title=f"模块 {module_name} 发现疑似资源缺失",
                page_name=snapshot.page_name,
                page_signature=snapshot.signature,
                reproduction_path=suspect_labels[:6],
                details={"suspect_nodes": suspect_labels[:10]},
            )
        )
    return issues


def filter_error_logs(lines: list[str], keywords: list[str], seen: set[str]) -> list[str]:
    matched: list[str] = []
    normalized_keywords = [item.lower() for item in keywords if item.strip()]
    for line in lines:
        lower = line.lower()
        if normalized_keywords and not any(keyword in lower for keyword in normalized_keywords):
            continue
        if line in seen:
            continue
        seen.add(line)
        matched.append(line)
    return matched


def aggregate_coverage(profile: ProjectProfile, session: AutomationSession, module_runs: list[RunSummary]) -> CoverageStats:
    covered_pages = sorted({node.page_name for node in session.memory.nodes.values() if node.page_name})
    critical_hit = sorted(set(profile.critical_pages).intersection(covered_pages))
    module_counter: dict[str, int] = {}
    action_count = 0
    successful_actions = 0
    for item in module_runs:
        module_name = str(item.metadata.get("module_name") or "default")
        module_counter[module_name] = module_counter.get(module_name, 0) + 1
        action_count += len(item.actions)
        successful_actions += sum(1 for action in item.actions if action.outcome == "success")
    return CoverageStats(
        visited_pages=len(covered_pages),
        visited_signatures=len(session.memory.nodes),
        modules_run=len(module_runs),
        critical_pages_total=len(profile.critical_pages),
        critical_pages_covered=len(critical_hit),
        path_count=len(session.memory.edges),
        action_count=action_count,
        successful_actions=successful_actions,
        module_coverage=module_counter,
        covered_pages=covered_pages,
    )


def build_ai_summary(
    profile: ProjectProfile,
    coverage: CoverageStats,
    issues: list[IssueRecord],
    module_runs: list[RunSummary],
) -> dict[str, Any]:
    critical_ratio = 1.0
    if coverage.critical_pages_total > 0:
        critical_ratio = coverage.critical_pages_covered / coverage.critical_pages_total
    issue_counter: dict[str, int] = {}
    for issue in issues:
        issue_counter[issue.category] = issue_counter.get(issue.category, 0) + 1
    top_issue = max(issue_counter.items(), key=lambda item: item[1])[0] if issue_counter else "无明显异常"
    coverage_assessment = (
        "关键页面覆盖良好"
        if critical_ratio >= 0.8
        else "关键页面覆盖一般，仍需补充模块入口或页面签名"
    )
    risk_assessment = (
        "高风险，建议优先处理阻塞/崩溃/日志报错"
        if any(issue.category in {"崩溃", "阻塞", "项目报错"} for issue in issues)
        else "当前未发现高优先级稳定性问题"
    )
    recommendations = [
        "补充更多 page_signatures 提高跨版本页面识别稳定性",
        "为未覆盖关键页面配置模块入口关键字与 expected_pages",
        "将 crash/block/error 三类问题接入告警平台做长时巡航守护",
    ]
    if top_issue != "无明显异常":
        recommendations.insert(0, f"优先分析 {top_issue} 类问题的高频触发路径")
    return {
        "conclusion": (
            f"{profile.project_name} 已完成 {len(module_runs)} 轮模块巡航，"
            f"累计覆盖 {coverage.visited_pages} 个页面、{coverage.path_count} 条路径。"
        ),
        "coverage_assessment": coverage_assessment,
        "risk_assessment": risk_assessment,
        "top_issue_category": top_issue,
        "issue_breakdown": issue_counter,
        "recommendations": recommendations[:4],
    }


def write_markdown_summary(
    output_path: Path,
    aggregate: RunSummary,
    report_paths: dict[str, str],
) -> None:
    coverage = aggregate.coverage
    lines = [
        f"# {aggregate.project_name} AI 自动化测试报告",
        "",
        f"- 目标模块: {aggregate.goal}",
        f"- 运行状态: {aggregate.status}",
        f"- 动作总数: {len(aggregate.actions)}",
        f"- 异常总数: {len(aggregate.issues)}",
        "",
        "## 覆盖情况",
        "",
        f"- 页面覆盖: {coverage.visited_pages if coverage else 0}",
        f"- 关键页面覆盖: {coverage.critical_pages_covered if coverage else 0} / {coverage.critical_pages_total if coverage else 0}",
        f"- 路径覆盖: {coverage.path_count if coverage else 0}",
        "",
        "## 问题摘要",
        "",
    ]
    if aggregate.issues:
        for issue in aggregate.issues[:20]:
            lines.append(
                f"- [{issue.category}/{issue.severity}] {issue.title} | 页面={issue.page_name} | 路径={' -> '.join(issue.reproduction_path) or '-'}"
            )
    else:
        lines.append("- 未发现异常问题")
    lines.extend(
        [
            "",
            "## AI 结论",
            "",
            f"- {aggregate.ai_summary.get('conclusion', '-')}",
            f"- 覆盖判断: {aggregate.ai_summary.get('coverage_assessment', '-')}",
            f"- 风险判断: {aggregate.ai_summary.get('risk_assessment', '-')}",
            "",
            "## 报告文件",
            "",
            f"- JSON: {report_paths.get('json', '-')}",
            f"- HTML: {report_paths.get('html', '-')}",
            f"- Mermaid: {report_paths.get('mermaid', '-')}",
        ]
    )
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def clone_issue(issue: IssueRecord) -> IssueRecord:
    return replace(
        issue,
        reproduction_path=list(issue.reproduction_path),
        related_logs=list(issue.related_logs),
        details=dict(issue.details),
    )


def append_module_summary(aggregate: RunSummary, module_summary: RunSummary) -> None:
    for action in module_summary.actions:
        aggregate.actions.append(replace(action, index=len(aggregate.actions) + 1, metrics=dict(action.metrics)))
    for metric in module_summary.business_metrics:
        aggregate.business_metrics.append(replace(metric))
    for sample in module_summary.performance_samples:
        aggregate.performance_samples.append(replace(sample, raw=dict(sample.raw)))


def record_issue(
    aggregate: RunSummary,
    issue: IssueRecord,
    trace_path: Path,
    module_name: str,
    round_index: int,
) -> None:
    aggregate.issues.append(issue)
    write_jsonl_line(
        trace_path,
        {
            "kind": "issue",
            "module": module_name,
            "round": round_index,
            "category": issue.category,
            "severity": issue.severity,
            "title": issue.title,
            "page": issue.page_name,
            "details": issue.details,
        },
    )


def try_initial_actions(session: AutomationSession, profile: ProjectProfile) -> None:
    for selector in profile.initial_actions:
        try:
            session.driver.click(selector)
            time.sleep(1.0)
        except Exception:
            continue


def safe_refresh(session: AutomationSession, reason: str) -> tuple[str | None, str]:
    try:
        snapshot = session.refresh_snapshot(reason)
        return snapshot.signature, snapshot.page_name
    except Exception as exc:
        return None, f"refresh_failed:{exc}"


def main() -> None:
    parser = argparse.ArgumentParser(description="MuMu 上的 星途天城(poco) cocos-js 通用巡航 runner")
    parser.add_argument(
        "--profile",
        default=str(ROOT / "examples" / "xingtu_tiancheng_poco_profile.yaml"),
        help="项目配置文件路径；默认使用 examples/xingtu_tiancheng_poco_profile.yaml",
    )
    parser.add_argument("--project-name", default="星途天城(poco)")
    parser.add_argument(
        "--device-uri",
        default="Android:///127.0.0.1:16384",
        help="MuMu 常见连接方式之一；如果你的实例端口不同，请改为自己的 ADB 端口",
    )
    parser.add_argument("--package", default="", help="游戏包名；如果 profile 中已配置可不传")
    parser.add_argument("--host", default="127.0.0.1", help="本地 Poco 代理地址")
    parser.add_argument("--port", type=int, default=5003, help="cocos-js 默认 Poco 端口")
    parser.add_argument("--output", default=str(ROOT / "outputs" / "xingtu_tiancheng_mumu"))
    parser.add_argument("--goal", default="大厅")
    parser.add_argument("--max-states", type=int, default=30)
    parser.add_argument("--startup-wait", type=float, default=10.0)
    parser.add_argument("--ui-roots", default="root,Canvas,UIRoot,MainScene")
    parser.add_argument("--popup-blacklist", default="更新公告,用户协议,实名认证")
    parser.add_argument("--critical-pages", default="login,lobby,battle")
    parser.add_argument("--dangerous-actions", default="充值,支付,购买,删除账号")
    parser.add_argument("--preferred-keywords", default="开始,进入,确认,大厅,冒险")
    parser.add_argument("--blocked-keywords", default="充值,支付,购买")
    parser.add_argument("--expected-pages", default="login,lobby")
    parser.add_argument("--modules", default="", help="只运行指定模块，逗号分隔")
    parser.add_argument("--cruise-minutes", type=float, default=0.0, help="持续巡航时长；0 表示按 max-rounds 控制")
    parser.add_argument("--max-rounds", type=int, default=1, help="模块轮转轮数")
    parser.add_argument("--stall-repeat-rounds", type=int, default=3, help="连续停留同页达到该轮数时判定阻塞")
    parser.add_argument("--module-pause-seconds", type=float, default=2.0, help="每个模块执行后的暂停时间")
    parser.add_argument("--logcat-lines", type=int, default=150, help="每轮采集的 logcat 尾部行数")
    parser.add_argument("--version-tag", default="", help="当前测试版本标识，可用于跨版本巡航记录")
    parser.add_argument("--restart-on-crash", action="store_true", help="崩溃后自动重启游戏并继续巡航")
    parser.add_argument("--preview-only", action="store_true", help="只验证是否能拿到 Poco UI 树，不执行自动探索")
    args = parser.parse_args()

    profile = build_profile(args)
    profile.validate()
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    trace_path = output_dir / "run_trace.jsonl"
    aggregate_dir = output_dir / "aggregate"
    aggregate_dir.mkdir(parents=True, exist_ok=True)

    device_serial = device_serial_from_uri(args.device_uri)
    launch_game_package(args.device_uri, profile.package_name)
    driver = build_driver(profile, args)

    if profile.startup_wait_seconds > 0:
        time.sleep(profile.startup_wait_seconds)

    session = AutomationSession(profile=profile, driver=driver, output_dir=output_dir)
    preflight = summarize_nodes(session)
    print(json.dumps({"preflight": preflight}, ensure_ascii=False, indent=2))

    if args.preview_only:
        return

    guard = runtime_guard(profile, args)
    issue_config = issue_detection_config(profile, args)
    module_plans = build_module_plans(profile, args)
    if not module_plans:
        raise SystemExit("没有可执行的模块计划，请检查 --modules 或 profile.module_scenarios 配置。")

    aggregate = RunSummary(
        project_name=profile.project_name,
        goal="、".join(plan.name for plan in module_plans),
    )
    aggregate.metadata.update(
        {
            "device_uri": args.device_uri,
            "package_name": profile.package_name,
            "version_tags": profile.version_tags,
            "module_plan_count": len(module_plans),
        }
    )

    module_runs: list[RunSummary] = []
    seen_error_logs: set[str] = set()
    clear_logcat(device_serial)
    started_at = time.monotonic()
    round_index = 0
    stagnant_rounds = 0
    last_signature: str | None = None

    while True:
        round_index += 1
        if guard["max_rounds"] > 0 and round_index > int(guard["max_rounds"]):
            break
        if guard["cruise_minutes"] > 0 and (time.monotonic() - started_at) >= float(guard["cruise_minutes"]) * 60:
            break

        for plan in module_plans:
            if guard["cruise_minutes"] > 0 and (time.monotonic() - started_at) >= float(guard["cruise_minutes"]) * 60:
                break

            if not app_is_running(device_serial, profile.package_name):
                crash_issue = IssueRecord(
                    category="崩溃",
                    severity="critical",
                    title=f"模块 {plan.name} 开始前检测到进程退出",
                    page_name=plan.goal,
                    details={"module": plan.name, "phase": "before_run"},
                )
                record_issue(aggregate, crash_issue, trace_path, plan.name, round_index)
                if guard["restart_on_crash"]:
                    restart_game_package(args.device_uri, profile.package_name, profile.startup_wait_seconds)
                    session.driver = build_driver(profile, args)
                else:
                    continue

            try_initial_actions(session, profile)
            before_signature, before_page_name = safe_refresh(session, "module_start")
            if before_signature and before_signature == last_signature:
                stagnant_rounds += 1
            else:
                stagnant_rounds = 0
            if before_signature:
                last_signature = before_signature

            scenario = build_rule_scenario(plan)
            write_jsonl_line(
                trace_path,
                {
                    "kind": "module_start",
                    "module": plan.name,
                    "round": round_index,
                    "goal": plan.goal,
                    "page_signature": before_signature,
                    "page_name": before_page_name,
                },
            )

            try:
                artifacts = session.crawl_ui_graph(
                    goal=plan.goal,
                    max_states=plan.max_states,
                    scenario=scenario,
                    metadata={
                        "module_name": plan.name,
                        "round_index": round_index,
                        "version_tags": profile.version_tags,
                    },
                )
                module_summary = artifacts.summary
            except Exception as exc:
                crash_category = "崩溃" if not app_is_running(device_serial, profile.package_name) else "阻塞"
                issue = IssueRecord(
                    category=crash_category,
                    severity="critical" if crash_category == "崩溃" else "high",
                    title=f"模块 {plan.name} 执行异常",
                    page_name=before_page_name,
                    page_signature=before_signature,
                    reproduction_path=[],
                    details={"module": plan.name, "error": str(exc)},
                )
                record_issue(aggregate, issue, trace_path, plan.name, round_index)
                if crash_category == "崩溃" and guard["restart_on_crash"]:
                    restart_game_package(args.device_uri, profile.package_name, profile.startup_wait_seconds)
                    session.driver = build_driver(profile, args)
                continue

            block_issue = detect_block_issue(
                plan=plan,
                before_signature=before_signature or "",
                module_summary=module_summary,
                stagnant_rounds=stagnant_rounds,
                threshold=int(guard["stall_repeat_rounds"]),
            )
            if block_issue:
                module_summary.issues.append(block_issue)

            if issue_config["enable_numeric_check"]:
                module_summary.issues.extend(detect_numeric_anomalies(session, plan.name))
            if issue_config["enable_resource_check"]:
                module_summary.issues.extend(detect_resource_gaps(session, plan.name))

            if issue_config["enable_log_error_check"]:
                matched_logs = filter_error_logs(
                    read_logcat_tail(device_serial, int(issue_config["logcat_lines"])),
                    profile.log_keywords or [" error ", "exception", "assert", "fatal"],
                    seen_error_logs,
                )
                if matched_logs:
                    module_summary.issues.append(
                        IssueRecord(
                            category="项目报错",
                            severity="high",
                            title=f"模块 {plan.name} 捕获到客户端异常日志",
                            page_name=before_page_name,
                            page_signature=tail_signature(module_summary.actions),
                            action_index=module_summary.actions[-1].index if module_summary.actions else None,
                            action_label=module_summary.actions[-1].selector_key if module_summary.actions else None,
                            reproduction_path=action_trace(module_summary.actions),
                            related_logs=matched_logs[:10],
                            details={"module": plan.name, "log_count": len(matched_logs)},
                        )
                    )

            append_module_summary(aggregate, module_summary)
            for issue in module_summary.issues:
                record_issue(aggregate, clone_issue(issue), trace_path, plan.name, round_index)
            module_runs.append(module_summary)

            write_jsonl_line(
                trace_path,
                {
                    "kind": "module_end",
                    "module": plan.name,
                    "round": round_index,
                    "actions": len(module_summary.actions),
                    "issues": len(module_summary.issues),
                    "state_count": len(session.memory.nodes),
                    "edge_count": len(session.memory.edges),
                },
            )

            if not app_is_running(device_serial, profile.package_name):
                crash_issue = IssueRecord(
                    category="崩溃",
                    severity="critical",
                    title=f"模块 {plan.name} 结束后检测到进程退出",
                    page_name=plan.goal,
                    details={"module": plan.name, "phase": "after_run"},
                )
                record_issue(aggregate, crash_issue, trace_path, plan.name, round_index)
                if guard["restart_on_crash"]:
                    restart_game_package(args.device_uri, profile.package_name, profile.startup_wait_seconds)
                    session.driver = build_driver(profile, args)

            time.sleep(float(guard["module_pause_seconds"]))

    aggregate.coverage = aggregate_coverage(profile, session, module_runs)
    aggregate.ai_summary = build_ai_summary(profile, aggregate.coverage, aggregate.issues, module_runs)
    aggregate.metadata["module_reports"] = [
        {
            "module_name": item.metadata.get("module_name"),
            "round_index": item.metadata.get("round_index"),
            "actions": len(item.actions),
            "issues": len(item.issues),
        }
        for item in module_runs
    ]
    aggregate.metadata["trace_path"] = str(trace_path)
    aggregate.complete("completed")

    report_paths = ReportBuilder(aggregate_dir).build(aggregate, session.memory, metadata=aggregate.metadata)
    write_markdown_summary(aggregate_dir / "summary.md", aggregate, report_paths)
    (aggregate_dir / "module_runs.json").write_text(
        json.dumps([asdict(item) for item in module_runs], ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    print(
        json.dumps(
            {
                "project_name": profile.project_name,
                "output": str(aggregate_dir),
                "state_count": len(session.memory.nodes),
                "edge_count": len(session.memory.edges),
                "coverage": asdict(aggregate.coverage),
                "issue_count": len(aggregate.issues),
                "reports": report_paths,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        raise SystemExit(
            "接入失败，请确认 MuMu ADB 已连通、游戏已接入 Poco SDK，"
            "并且 AirtestIDE 的 Cocos-Js 模式可以看到 UI 树。"
            f"\n原始异常: {exc}"
        ) from exc
