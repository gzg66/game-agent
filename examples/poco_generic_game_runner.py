from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import airtest
from airtest.core.api import connect_device
from poco.drivers.unity3d import UnityPoco


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def log_event(log_path: Path, payload: dict[str, Any]) -> None:
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"time": now_iso(), **payload}, ensure_ascii=False) + "\n")


def adb_cmd(device_serial: str, *args: str) -> subprocess.CompletedProcess[str]:
    runtime_adb = Path(airtest.__file__).resolve().parent / "core" / "android" / "static" / "adb" / "windows" / "adb.exe"
    adb = str(runtime_adb)
    command = [adb, "-s", device_serial, *args]
    return subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
    )


def force_stop_app(device_serial: str, package_name: str) -> None:
    adb_cmd(device_serial, "shell", "am", "force-stop", package_name)


def start_app(device_serial: str, activity_name: str) -> None:
    adb_cmd(device_serial, "shell", "am", "start", "-n", activity_name)


def press_back(device_serial: str) -> None:
    adb_cmd(device_serial, "shell", "input", "keyevent", "4")


def app_pid(device_serial: str, package_name: str) -> str | None:
    result = adb_cmd(device_serial, "shell", "pidof", package_name)
    pid = result.stdout.strip()
    return pid or None


def app_is_running(device_serial: str, package_name: str) -> bool:
    return app_pid(device_serial, package_name) is not None


def get_screen_size(device_serial: str) -> tuple[int, int]:
    result = adb_cmd(device_serial, "shell", "wm", "size")
    output = result.stdout.strip()
    for token in output.replace("Physical size:", "").split():
        if "x" in token:
            width, height = token.split("x", 1)
            if width.isdigit() and height.isdigit():
                return int(width), int(height)
    return 1440, 2560


def connect_runtime(device_uri: str, host: str, port: int) -> UnityPoco:
    device = connect_device(device_uri)
    return UnityPoco((host, port), device=device)


def schedule_async_force_stop(
    device_serial: str,
    package_name: str,
    delay_s: float,
    log_path: Path,
) -> None:
    def worker() -> None:
        time.sleep(delay_s)
        force_stop_app(device_serial, package_name)
        log_event(log_path, {"kind": "external_kill", "delay_s": delay_s})

    threading.Thread(target=worker, daemon=True).start()


def dump_hierarchy(poco: UnityPoco, retries: int = 3, wait_s: float = 1.0) -> dict[str, Any]:
    last_error: Exception | None = None
    for _ in range(retries):
        try:
            return poco.freeze().agent.hierarchy.dump()
        except Exception as exc:
            last_error = exc
            time.sleep(wait_s)
    if last_error:
        raise last_error
    raise RuntimeError("dump_hierarchy failed")


def safe_id(raw: str) -> str:
    return "n_" + "".join(ch if ch.isalnum() else "_" for ch in raw)


def nested_text(node: dict[str, Any]) -> str:
    payload = node.get("payload", {}) or {}
    text = str(payload.get("text") or "").strip()
    if text:
        return text
    for child in node.get("children", []) or []:
        if isinstance(child, dict):
            child_text = nested_text(child)
            if child_text:
                return child_text
    return ""


def build_signature(hierarchy: dict[str, Any]) -> str:
    names: list[str] = []

    def walk(node: dict[str, Any]) -> None:
        payload = node.get("payload", {}) or {}
        name = str(node.get("name") or payload.get("name") or "")
        text = str(payload.get("text") or "")
        clickable = str(payload.get("clickable", False))
        if name or text:
            names.append(f"{name}|{text}|{clickable}")
        for child in node.get("children", []) or []:
            if isinstance(child, dict):
                walk(child)

    walk(hierarchy)
    joined = "\n".join(names[:80]).encode("utf-8", errors="ignore")
    return hashlib.sha1(joined).hexdigest()[:16]


def tree_nodes(hierarchy: dict[str, Any]) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []

    def walk(node: dict[str, Any], path: str = "root", depth: int = 0, parent_graph_id: str | None = None) -> None:
        payload = node.get("payload", {}) or {}
        name = str(node.get("name") or payload.get("name") or f"node_{len(nodes)}")
        graph_id = safe_id(f"{path}_{depth}_{name}_{len(nodes)}")
        entry = {
            "graph_id": graph_id,
            "parent_graph_id": parent_graph_id,
            "name": name,
            "type": payload.get("type"),
            "text": nested_text(node),
            "clickable": payload.get("clickable", False),
            "components": payload.get("components", []) or [],
            "depth": depth,
            "path": path,
            "pos": payload.get("pos"),
            "size": payload.get("size"),
            "visible": payload.get("visible", True),
        }
        entry["interactive_candidate"] = bool(
            entry["clickable"]
            or entry["type"] in {"Button", "InputField", "Toggle", "Slider", "Dropdown"}
            or any(comp in {"Button", "InputField", "StrongFeedback"} for comp in entry["components"])
        )
        nodes.append(entry)
        for index, child in enumerate(node.get("children", []) or []):
            if isinstance(child, dict):
                walk(child, f"{path}_{index}_{name}", depth + 1, graph_id)

    walk(hierarchy)
    return nodes


def interactive_nodes(hierarchy: dict[str, Any]) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []

    def walk(node: dict[str, Any], depth: int = 0, path: str = "root") -> None:
        payload = node.get("payload", {}) or {}
        components = payload.get("components", []) or []
        item = {
            "name": str(node.get("name") or payload.get("name") or ""),
            "type": payload.get("type"),
            "text": nested_text(node),
            "clickable": payload.get("clickable", False),
            "components": components,
            "depth": depth,
            "path": path,
            "pos": payload.get("pos"),
            "size": payload.get("size"),
            "visible": payload.get("visible", True),
        }
        item["interactive_candidate"] = bool(
            item["clickable"]
            or item["type"] in {"Button", "InputField", "Toggle", "Slider", "Dropdown"}
            or any(comp in {"Button", "InputField", "StrongFeedback"} for comp in components)
        )
        if item["interactive_candidate"]:
            nodes.append(item)
        for index, child in enumerate(node.get("children", []) or []):
            if isinstance(child, dict):
                child_name = str(child.get("name") or (child.get("payload", {}) or {}).get("name") or index)
                walk(child, depth + 1, f"{path}/{index}:{child_name}")

    walk(hierarchy)
    return nodes


def first_text(hierarchy: dict[str, Any]) -> str:
    result = ""

    def walk(node: dict[str, Any]) -> bool:
        nonlocal result
        payload = node.get("payload", {}) or {}
        text = str(payload.get("text") or "")
        if text:
            result = text
            return True
        for child in node.get("children", []) or []:
            if isinstance(child, dict) and walk(child):
                return True
        return False

    walk(hierarchy)
    return result


def page_title(hierarchy: dict[str, Any], fallback: str) -> str:
    return first_text(hierarchy) or fallback


def action_key(action: dict[str, Any]) -> str:
    return "|".join(
        [
            str(action.get("path") or ""),
            str(action.get("name") or ""),
            str(action.get("text") or ""),
            str(action.get("type") or ""),
        ]
    )


def action_label(action: dict[str, Any]) -> str:
    name = str(action.get("name") or "").strip()
    text = str(action.get("text") or "").strip()
    if name and text and text.lower() != name.lower():
        return f"{name} [{text}]"
    return text or name


def normalize_action(action: dict[str, Any]) -> dict[str, Any]:
    return {
        "key": action_key(action),
        "label": action_label(action),
        "name": action.get("name") or "",
        "text": action.get("text") or "",
        "path": action.get("path") or "",
        "type": action.get("type") or "",
        "depth": action.get("depth"),
        "components": action.get("components") or [],
        "pos": action.get("pos"),
        "size": action.get("size"),
        "visible": action.get("visible", True),
        "clickable": action.get("clickable", False),
    }


def is_back_like(action: dict[str, Any]) -> bool:
    tokens = " ".join(
        [
            str(action.get("name") or "").lower(),
            str(action.get("text") or "").lower(),
            str(action.get("path") or "").lower(),
        ]
    )
    return any(keyword in tokens for keyword in ["btn_back", "back", "return", "close", "cancel"])


def is_valid_pos(action: dict[str, Any]) -> bool:
    pos = action.get("pos")
    if not isinstance(pos, list) or len(pos) != 2:
        return False
    x, y = pos
    return isinstance(x, (int, float)) and isinstance(y, (int, float)) and 0.0 <= x <= 1.0 and 0.0 <= y <= 1.0


def choose_actions(hierarchy: dict[str, Any], include_back: bool = False) -> list[dict[str, Any]]:
    candidates = [normalize_action(item) for item in interactive_nodes(hierarchy)]
    filtered: list[dict[str, Any]] = []
    seen: set[str] = set()
    for action in candidates:
        if not action["visible"]:
            continue
        if not is_valid_pos(action):
            continue
        if not include_back and is_back_like(action):
            continue
        if action["key"] in seen:
            continue
        seen.add(action["key"])
        filtered.append(action)
    filtered.sort(key=lambda item: (item["depth"] or 0, item["path"]))
    return filtered


def click_action(poco: UnityPoco, device_serial: str, screen_size: tuple[int, int], action: dict[str, Any]) -> tuple[bool, str]:
    action_name = str(action.get("name") or "")
    if action_name:
        try:
            node = poco(action_name)
            if node.exists():
                node.click()
                return True, f"poco:{action_name}"
        except Exception:
            pass
    if not is_valid_pos(action):
        return False, "invalid_pos"
    pos = action["pos"]
    x = max(1, min(screen_size[0] - 1, int(pos[0] * screen_size[0])))
    y = max(1, min(screen_size[1] - 1, int(pos[1] * screen_size[1])))
    result = adb_cmd(device_serial, "shell", "input", "tap", str(x), str(y))
    if result.returncode != 0:
        return False, result.stderr.strip() or "adb_tap_failed"
    return True, f"{x},{y}"


def capture_page(hierarchy: dict[str, Any], page_id: str | None = None, path_actions: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    signature = build_signature(hierarchy)
    return {
        "page_id": page_id or signature,
        "signature": signature,
        "title": page_title(hierarchy, page_id or signature),
        "hierarchy": hierarchy,
        "tree_nodes": tree_nodes(hierarchy),
        "actions": choose_actions(hierarchy),
        "path_actions": list(path_actions or []),
    }


def add_or_update_page(
    pages_by_signature: dict[str, dict[str, Any]],
    page_order: list[str],
    page: dict[str, Any],
    path_actions: list[dict[str, Any]],
) -> dict[str, Any]:
    signature = page["signature"]
    existing = pages_by_signature.get(signature)
    if existing:
        existing["title"] = page["title"]
        existing["hierarchy"] = page["hierarchy"]
        existing["tree_nodes"] = page["tree_nodes"]
        existing["actions"] = page["actions"]
        if not existing.get("path_actions"):
            existing["path_actions"] = list(path_actions)
        return existing
    page["page_id"] = f"page_{len(page_order):03d}"
    page["path_actions"] = list(path_actions)
    pages_by_signature[signature] = page
    page_order.append(signature)
    return page


def dump_with_reconnect(
    poco: UnityPoco,
    device_serial: str,
    package_name: str,
    device_uri: str,
    host: str,
    port: int,
    log_path: Path,
) -> tuple[UnityPoco, dict[str, Any] | None]:
    try:
        return poco, dump_hierarchy(poco, retries=2, wait_s=0.5)
    except Exception as exc:
        if not app_is_running(device_serial, package_name):
            log_event(log_path, {"kind": "back_attempt_failed", "reason": f"dump_app_not_running:{exc}"})
            return poco, None
        try:
            poco = connect_runtime(device_uri, host, port)
            hierarchy = dump_hierarchy(poco, retries=3, wait_s=0.5)
            log_event(log_path, {"kind": "poco_reconnected", "reason": "back_navigation"})
            return poco, hierarchy
        except Exception as reconnect_exc:
            log_event(log_path, {"kind": "back_attempt_failed", "reason": f"dump:{exc}|reconnect:{reconnect_exc}"})
            return poco, None


def try_go_back(
    poco: UnityPoco,
    device_serial: str,
    package_name: str,
    device_uri: str,
    host: str,
    port: int,
    action_wait_s: float,
    screen_size: tuple[int, int],
    expected_signature: str,
    log_path: Path,
) -> tuple[bool, UnityPoco]:
    for _ in range(3):
        poco, hierarchy = dump_with_reconnect(poco, device_serial, package_name, device_uri, host, port, log_path)
        if hierarchy is None:
            return False, poco
        if build_signature(hierarchy) == expected_signature:
            return True, poco
        for action in choose_actions(hierarchy, include_back=True):
            if not is_back_like(action):
                continue
            ok, click_info = click_action(poco, device_serial, screen_size, action)
            log_event(log_path, {"kind": "back_action", "action_key": action["key"], "click": click_info, "ok": ok})
            if not ok:
                continue
            time.sleep(action_wait_s)
            poco, after_hierarchy = dump_with_reconnect(
                poco, device_serial, package_name, device_uri, host, port, log_path
            )
            if after_hierarchy is None:
                continue
            if build_signature(after_hierarchy) == expected_signature:
                return True, poco
        press_back(device_serial)
        log_event(log_path, {"kind": "back_action", "action_key": "android_back", "click": "keyevent_4", "ok": True})
        time.sleep(action_wait_s)
        poco, after_hierarchy = dump_with_reconnect(
            poco, device_serial, package_name, device_uri, host, port, log_path
        )
        if after_hierarchy is None:
            continue
        if build_signature(after_hierarchy) == expected_signature:
            return True, poco
    return False, poco


def restore_path_in_session(
    poco: UnityPoco,
    path_actions: list[dict[str, Any]],
    root_signature: str,
    expected_signature: str,
    device_serial: str,
    package_name: str,
    device_uri: str,
    host: str,
    port: int,
    screen_size: tuple[int, int],
    action_wait_s: float,
    log_path: Path,
) -> tuple[bool, UnityPoco]:
    poco, hierarchy = dump_with_reconnect(poco, device_serial, package_name, device_uri, host, port, log_path)
    if hierarchy is None:
        return False, poco
    current_signature = build_signature(hierarchy)
    if current_signature == expected_signature:
        return True, poco

    for _ in range(max(2, len(path_actions) + 1)):
        if current_signature == root_signature:
            break
        press_back(device_serial)
        log_event(log_path, {"kind": "path_restore_back", "click": "keyevent_4"})
        time.sleep(action_wait_s)
        poco, hierarchy = dump_with_reconnect(poco, device_serial, package_name, device_uri, host, port, log_path)
        if hierarchy is None:
            return False, poco
        current_signature = build_signature(hierarchy)
        if current_signature == expected_signature:
            return True, poco

    if current_signature != root_signature:
        log_event(
            log_path,
            {
                "kind": "path_restore_failed",
                "reason": "not_at_root",
                "current_signature": current_signature,
                "expected_signature": expected_signature,
            },
        )
        return False, poco

    for action in path_actions:
        ok, click_info = click_action(poco, device_serial, screen_size, action)
        log_event(
            log_path,
            {
                "kind": "path_restore_action",
                "action_key": action["key"],
                "action_label": action["label"],
                "action_text": action["text"],
                "click": click_info,
                "ok": ok,
            },
        )
        if not ok:
            return False, poco
        time.sleep(action_wait_s)
        poco, hierarchy = dump_with_reconnect(poco, device_serial, package_name, device_uri, host, port, log_path)
        if hierarchy is None:
            return False, poco
        current_signature = build_signature(hierarchy)

    return current_signature == expected_signature, poco


def build_graph_td(pages: list[dict[str, Any]], relations: list[dict[str, Any]]) -> str:
    lines = ["graph TD"]
    root_by_signature: dict[str, str] = {}
    for page in pages:
        subgraph_id = safe_id(f"page_{page['page_id']}")
        title = str(page["title"]).replace('"', "'")
        lines.append(f'    subgraph {subgraph_id}["{title}"]')
        for node in page["tree_nodes"]:
            label = str(node.get("name") or "node").replace('"', "'")
            if node.get("text"):
                text_label = str(node["text"]).replace('"', "'")
                label = f"{label}\\n{text_label}"
            if node.get("interactive_candidate"):
                label = f"{label}\\n[interactive]"
            lines.append(f'        {node["graph_id"]}["{label}"]')
        for node in page["tree_nodes"]:
            if node.get("parent_graph_id"):
                lines.append(f'        {node["parent_graph_id"]} --> {node["graph_id"]}')
        lines.append("    end")
        if page["tree_nodes"]:
            root_by_signature[page["signature"]] = page["tree_nodes"][0]["graph_id"]
    for relation in relations:
        from_id = root_by_signature.get(relation["from_signature"])
        to_id = root_by_signature.get(relation["to_signature"])
        if not from_id or not to_id:
            continue
        label = str(relation["selector"]).replace('"', "'")
        lines.append(f'    {from_id} -->|"{label}"| {to_id}')
    return "\n".join(lines) + "\n"


def write_outputs(
    output_dir: Path,
    pages_by_signature: dict[str, dict[str, Any]],
    page_order: list[str],
    relations: list[dict[str, Any]],
    summary: dict[str, Any],
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    logs_dir = output_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    pages_dir = output_dir / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)

    ordered_pages = [pages_by_signature[sig] for sig in page_order if sig in pages_by_signature]
    map_payload = {
        "page_order": page_order,
        "pages": [
            {
                "page_id": page["page_id"],
                "signature": page["signature"],
                "title": page["title"],
                "actions": page["actions"],
                "path_actions": page.get("path_actions", []),
                "tree_nodes": page["tree_nodes"],
                "interactive_candidates": page["actions"],
            }
            for page in ordered_pages
        ],
        "relations": relations,
        "root_signature": summary["root_signature"],
        "root_title": summary["root_title"],
    }

    for page in ordered_pages:
        hierarchy = page.get("hierarchy")
        if hierarchy is None:
            continue
        (pages_dir / f"{page['page_id']}.json").write_text(
            json.dumps(hierarchy, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    (output_dir / "map.json").write_text(json.dumps(map_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_dir / "relations.json").write_text(json.dumps(map_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    graph_td = build_graph_td(ordered_pages, relations)
    (output_dir / "graph_td.mmd").write_text(graph_td, encoding="utf-8")
    (output_dir / "relations.mmd").write_text(graph_td, encoding="utf-8")
    (output_dir / "test_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


def summarize_actions_for_log(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "action_key": action["key"],
            "action_label": action["label"],
            "action_name": action["name"],
            "action_text": action["text"],
        }
        for action in actions
    ]


def discover_page(
    poco: UnityPoco,
    current_page: dict[str, Any],
    path_actions: list[dict[str, Any]],
    stack: list[str],
    pages_by_signature: dict[str, dict[str, Any]],
    page_order: list[str],
    relations: list[dict[str, Any]],
    summary: dict[str, Any],
    discovered_action_keys: set[str],
    attempted_action_keys: set[str],
    log_path: Path,
    device_uri: str,
    host: str,
    port: int,
    device_serial: str,
    package_name: str,
    screen_size: tuple[int, int],
    action_wait: float,
    max_pages: int,
    max_actions_per_page: int,
) -> tuple[bool, UnityPoco]:
    current_page = add_or_update_page(pages_by_signature, page_order, current_page, path_actions)
    current_signature = current_page["signature"]
    current_title = current_page["title"]
    current_actions = current_page["actions"][:max_actions_per_page]
    if current_signature not in summary["visited_pages"]:
        summary["visited_pages"].append(current_signature)

    page_log = {
        "page_signature": current_signature,
        "page_title": current_title,
        "status": "ok",
        "discovered_action_count": len(current_actions),
        "actions": [],
    }
    log_event(
        log_path,
        {
            "kind": "page_scan",
            "page_signature": current_signature,
            "page_title": current_title,
            "discovered_action_count": len(current_actions),
            "discovered_actions": summarize_actions_for_log(current_actions),
        },
    )
    for action in current_actions:
        discovered_action_keys.add(f"{current_signature}::{action['key']}")

    for action in current_actions:
        scope = f"{current_signature}::{action['key']}"
        if scope in attempted_action_keys:
            continue
        attempted_action_keys.add(scope)
        pid_before = app_pid(device_serial, package_name)
        ok, click_info = click_action(poco, device_serial, screen_size, action)
        log_event(
            log_path,
            {
                "kind": "action_start",
                "page_signature": current_signature,
                "page_title": current_title,
                "action_key": action["key"],
                "action_label": action["label"],
                "action_name": action["name"],
                "action_text": action["text"],
                "click": click_info,
                "pid_before": pid_before,
            },
        )
        if not ok:
            page_log["actions"].append(
                {
                    "action_key": action["key"],
                    "action_label": action["label"],
                    "action_text": action["text"],
                    "status": "tap_failed",
                    "reason": click_info,
                }
            )
            continue

        time.sleep(action_wait)
        if not app_is_running(device_serial, package_name):
            crash_info = {
                "page_signature": current_signature,
                "page_title": current_title,
                "action_key": action["key"],
                "crash_type": "process_disappeared",
            }
            summary["crashes"].append(crash_info)
            page_log["status"] = "crashed"
            page_log["actions"].append(
                {"action_key": action["key"], "action_label": action["label"], "action_text": action["text"], "status": "crash"}
            )
            log_event(log_path, {"kind": "app_crash", **crash_info, "pid_after": None})
            summary["page_runs"].append(_finalize_page_log(page_log, current_actions))
            summary["stop_reason"] = "app_crash"
            return False, poco

        try:
            after_hierarchy = dump_hierarchy(poco)
        except Exception as exc:
            crash_info = {
                "page_signature": current_signature,
                "page_title": current_title,
                "action_key": action["key"],
                "crash_type": f"rpc_broken:{exc}",
            }
            summary["crashes"].append(crash_info)
            page_log["status"] = "crashed"
            page_log["actions"].append(
                {
                    "action_key": action["key"],
                    "action_label": action["label"],
                    "action_text": action["text"],
                    "status": "rpc_broken",
                }
            )
            log_event(log_path, {"kind": "app_crash", **crash_info})
            summary["page_runs"].append(_finalize_page_log(page_log, current_actions))
            summary["stop_reason"] = "rpc_broken"
            return False, poco

        after_page = capture_page(after_hierarchy, path_actions=[*path_actions, action])
        after_signature = after_page["signature"]
        changed = after_signature != current_signature
        relations.append(
            {
                "selector": action["key"],
                "status": "ok" if changed else "no_change",
                "from_signature": current_signature,
                "to_signature": after_signature,
                "from_page_id": current_page["page_id"],
                "to_page_id": pages_by_signature.get(after_signature, {"page_id": f"page_{len(page_order):03d}"})["page_id"],
                "first_text": after_page["title"],
                "interactive_candidates": after_page["actions"],
            }
        )
        page_log["actions"].append(
            {
                "action_key": action["key"],
                "action_label": action["label"],
                "action_text": action["text"],
                "status": "ok" if changed else "no_change",
                "to_signature": after_signature,
                "changed": changed,
            }
        )
        log_event(
            log_path,
            {
                "kind": "action_end",
                "page_signature": current_signature,
                "page_title": current_title,
                "action_key": action["key"],
                "action_label": action["label"],
                "to_signature": after_signature,
                "changed": changed,
            },
        )

        if not changed:
            continue

        child_exists = after_signature in pages_by_signature
        child_page = add_or_update_page(pages_by_signature, page_order, after_page, [*path_actions, action])
        if not child_exists and len(pages_by_signature) > max_pages:
            page_log["actions"].append(
                {
                    "action_key": action["key"],
                    "action_label": action["label"],
                    "action_text": action["text"],
                    "status": "max_pages_reached",
                }
            )
            continue
        if child_page["signature"] not in stack:
            ok, poco = discover_page(
                poco,
                child_page,
                [*path_actions, action],
                [*stack, current_signature],
                pages_by_signature,
                page_order,
                relations,
                summary,
                discovered_action_keys,
                attempted_action_keys,
                log_path,
                device_uri,
                host,
                port,
                device_serial,
                package_name,
                screen_size,
                action_wait,
                max_pages,
                max_actions_per_page,
            )
            if not ok:
                summary["page_runs"].append(_finalize_page_log(page_log, current_actions))
                return False, poco

        returned, poco = try_go_back(
            poco,
            device_serial,
            package_name,
            device_uri,
            host,
            port,
            action_wait,
            screen_size,
            current_signature,
            log_path,
        )
        if not returned:
            returned, poco = restore_path_in_session(
                poco,
                path_actions,
                summary["root_signature"],
                current_signature,
                device_serial,
                package_name,
                device_uri,
                host,
                port,
                screen_size,
                action_wait,
                log_path,
            )
        if not returned:
            page_log["status"] = "return_failed"
            page_log["actions"].append(
                {
                    "action_key": action["key"],
                    "action_label": action["label"],
                    "action_text": action["text"],
                    "status": "return_failed",
                }
            )
            summary["page_runs"].append(_finalize_page_log(page_log, current_actions))
            summary["stop_reason"] = "return_failed"
            return False, poco

        try:
            current_page = capture_page(dump_hierarchy(poco), page_id=current_page["page_id"], path_actions=path_actions)
            add_or_update_page(pages_by_signature, page_order, current_page, path_actions)
        except Exception as exc:
            page_log["status"] = "refresh_failed"
            page_log["actions"].append(
                {
                    "action_key": action["key"],
                    "action_label": action["label"],
                    "action_text": action["text"],
                    "status": f"refresh_failed:{exc}",
                }
            )
            summary["page_runs"].append(_finalize_page_log(page_log, current_actions))
            summary["stop_reason"] = "refresh_failed"
            return False, poco

    summary["page_runs"].append(_finalize_page_log(page_log, current_actions))
    return True, poco


def _finalize_page_log(page_log: dict[str, Any], current_actions: list[dict[str, Any]]) -> dict[str, Any]:
    attempted_keys = {item["action_key"] for item in page_log["actions"]}
    page_log["attempted_action_count"] = len(attempted_keys)
    page_log["remaining_action_count"] = max(0, len(current_actions) - len(attempted_keys))
    page_log["coverage_ratio"] = round((len(attempted_keys) / len(current_actions)) if current_actions else 1.0, 4)
    return page_log


def load_map(map_file: Path) -> tuple[list[str], dict[str, dict[str, Any]], dict[tuple[str, str], str], list[dict[str, Any]]]:
    payload = json.loads(map_file.read_text(encoding="utf-8"))
    page_order = payload.get("page_order") or [page["signature"] for page in payload.get("pages", [])]
    pages = {page["signature"]: page for page in payload.get("pages", [])}
    relation_lookup = {
        (relation["from_signature"], relation["selector"]): relation["to_signature"]
        for relation in payload.get("relations", [])
    }
    return page_order, pages, relation_lookup, payload.get("relations", [])


def replay_page(
    poco: UnityPoco,
    page_signature: str,
    map_pages: dict[str, dict[str, Any]],
    relation_lookup: dict[tuple[str, str], str],
    stack: list[str],
    summary: dict[str, Any],
    discovered_action_keys: set[str],
    attempted_action_keys: set[str],
    log_path: Path,
    device_uri: str,
    host: str,
    port: int,
    device_serial: str,
    package_name: str,
    screen_size: tuple[int, int],
    action_wait: float,
    max_actions_per_page: int,
) -> tuple[bool, UnityPoco]:
    map_page = map_pages[page_signature]
    current_page = capture_page(dump_hierarchy(poco), page_id=map_page["page_id"], path_actions=map_page.get("path_actions", []))
    current_signature = current_page["signature"]
    current_title = current_page["title"]
    planned_actions = (map_page.get("actions") or [])[:max_actions_per_page]
    if page_signature not in summary["visited_pages"]:
        summary["visited_pages"].append(page_signature)

    page_log = {
        "page_signature": current_signature,
        "page_title": current_title,
        "status": "ok",
        "discovered_action_count": len(planned_actions),
        "actions": [],
    }
    log_event(
        log_path,
        {
            "kind": "page_scan",
            "page_signature": current_signature,
            "page_title": current_title,
            "discovered_action_count": len(planned_actions),
            "discovered_actions": summarize_actions_for_log(planned_actions),
        },
    )
    for action in planned_actions:
        discovered_action_keys.add(f"{page_signature}::{action['key']}")

    for action in planned_actions:
        scope = f"{page_signature}::{action['key']}"
        if scope in attempted_action_keys:
            continue
        attempted_action_keys.add(scope)
        pid_before = app_pid(device_serial, package_name)
        ok, click_info = click_action(poco, device_serial, screen_size, action)
        log_event(
            log_path,
            {
                "kind": "action_start",
                "page_signature": current_signature,
                "page_title": current_title,
                "action_key": action["key"],
                "action_label": action["label"],
                "action_name": action["name"],
                "action_text": action["text"],
                "click": click_info,
                "pid_before": pid_before,
            },
        )
        if not ok:
            page_log["actions"].append(
                {
                    "action_key": action["key"],
                    "action_label": action["label"],
                    "action_text": action["text"],
                    "status": "tap_failed",
                    "reason": click_info,
                }
            )
            continue

        time.sleep(action_wait)
        if not app_is_running(device_serial, package_name):
            crash_info = {
                "page_signature": current_signature,
                "page_title": current_title,
                "action_key": action["key"],
                "crash_type": "process_disappeared",
            }
            summary["crashes"].append(crash_info)
            page_log["status"] = "crashed"
            page_log["actions"].append(
                {"action_key": action["key"], "action_label": action["label"], "action_text": action["text"], "status": "crash"}
            )
            log_event(log_path, {"kind": "app_crash", **crash_info, "pid_after": None})
            summary["page_runs"].append(_finalize_page_log(page_log, planned_actions))
            summary["stop_reason"] = "app_crash"
            return False, poco

        try:
            after_hierarchy = dump_hierarchy(poco)
        except Exception as exc:
            crash_info = {
                "page_signature": current_signature,
                "page_title": current_title,
                "action_key": action["key"],
                "crash_type": f"rpc_broken:{exc}",
            }
            summary["crashes"].append(crash_info)
            page_log["status"] = "crashed"
            page_log["actions"].append(
                {
                    "action_key": action["key"],
                    "action_label": action["label"],
                    "action_text": action["text"],
                    "status": "rpc_broken",
                }
            )
            log_event(log_path, {"kind": "app_crash", **crash_info})
            summary["page_runs"].append(_finalize_page_log(page_log, planned_actions))
            summary["stop_reason"] = "rpc_broken"
            return False, poco

        after_page = capture_page(after_hierarchy)
        after_signature = after_page["signature"]
        changed = after_signature != current_signature
        expected_to = relation_lookup.get((page_signature, action["key"]))
        page_log["actions"].append(
            {
                "action_key": action["key"],
                "action_label": action["label"],
                "action_text": action["text"],
                "status": "ok" if changed else "no_change",
                "to_signature": after_signature,
                "expected_to_signature": expected_to,
                "changed": changed,
            }
        )
        log_event(
            log_path,
            {
                "kind": "action_end",
                "page_signature": current_signature,
                "page_title": current_title,
                "action_key": action["key"],
                "action_label": action["label"],
                "to_signature": after_signature,
                "expected_to_signature": expected_to,
                "changed": changed,
            },
        )

        if not changed or not expected_to or expected_to in stack:
            continue
        if expected_to not in map_pages:
            continue
        ok, poco = replay_page(
            poco,
            expected_to,
            map_pages,
            relation_lookup,
            [*stack, page_signature],
            summary,
            discovered_action_keys,
            attempted_action_keys,
            log_path,
            device_uri,
            host,
            port,
            device_serial,
            package_name,
            screen_size,
            action_wait,
            max_actions_per_page,
        )
        if not ok:
            summary["page_runs"].append(_finalize_page_log(page_log, planned_actions))
            return False, poco
        returned, poco = try_go_back(
            poco,
            device_serial,
            package_name,
            device_uri,
            host,
            port,
            action_wait,
            screen_size,
            current_signature,
            log_path,
        )
        if not returned:
            returned, poco = restore_path_in_session(
                poco,
                map_page.get("path_actions", []),
                summary["root_signature"],
                current_signature,
                device_serial,
                package_name,
                device_uri,
                host,
                port,
                screen_size,
                action_wait,
                log_path,
            )
        if not returned:
            page_log["status"] = "return_failed"
            page_log["actions"].append(
                {
                    "action_key": action["key"],
                    "action_label": action["label"],
                    "action_text": action["text"],
                    "status": "return_failed",
                }
            )
            summary["page_runs"].append(_finalize_page_log(page_log, planned_actions))
            summary["stop_reason"] = "return_failed"
            return False, poco

    summary["page_runs"].append(_finalize_page_log(page_log, planned_actions))
    return True, poco


def build_summary(mode: str, root_page: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "running",
        "mode": mode,
        "root_signature": root_page["signature"],
        "root_title": root_page["title"],
        "visited_pages": [],
        "map_updates": [],
        "crashes": [],
        "page_runs": [],
        "coverage": {
            "discovered_page_count": 0,
            "discovered_action_count": 0,
            "attempted_action_count": 0,
            "coverage_ratio": 0.0,
        },
        "stop_reason": "completed",
    }


def finalize_summary(
    summary: dict[str, Any],
    pages_by_signature: dict[str, dict[str, Any]],
    relations: list[dict[str, Any]],
    discovered_action_keys: set[str],
    attempted_action_keys: set[str],
) -> None:
    summary["status"] = "completed" if summary.get("stop_reason") == "completed" else "interrupted"
    summary["page_count"] = len(pages_by_signature)
    summary["edge_count"] = len(relations)
    summary["coverage"] = {
        "discovered_page_count": len(pages_by_signature),
        "discovered_action_count": len(discovered_action_keys),
        "attempted_action_count": len(attempted_action_keys),
        "coverage_ratio": round(
            (len(attempted_action_keys) / len(discovered_action_keys)) if discovered_action_keys else 1.0,
            4,
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Poco 通用在线建图与地图回放测试器")
    parser.add_argument("--mode", choices=["discover", "replay"], default="discover")
    parser.add_argument("--device-uri", default="Android:///emulator-5554")
    parser.add_argument("--device-serial", default="emulator-5554")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=5001)
    parser.add_argument("--package", default="com.NetEase")
    parser.add_argument("--activity", default="com.NetEase/com.unity3d.player.UnityPlayerActivity")
    parser.add_argument("--output", default="outputs/generic_game_run")
    parser.add_argument("--map-file", default=None)
    parser.add_argument("--boot-wait", type=float, default=8.0)
    parser.add_argument("--action-wait", type=float, default=2.0)
    parser.add_argument("--max-pages", type=int, default=20)
    parser.add_argument("--max-actions-per-page", type=int, default=20)
    parser.add_argument("--kill-after-seconds", type=float, default=None)
    args = parser.parse_args()

    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)
    logs_dir = out_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / "execution.jsonl"
    if log_path.exists():
        log_path.unlink()

    screen_size = get_screen_size(args.device_serial)
    force_stop_app(args.device_serial, args.package)
    time.sleep(1)
    start_app(args.device_serial, args.activity)
    time.sleep(args.boot_wait)
    poco = connect_runtime(args.device_uri, args.host, args.port)
    root_hierarchy = dump_hierarchy(poco)
    root_page = capture_page(root_hierarchy, page_id="page_000", path_actions=[])
    log_event(
        log_path,
        {
            "kind": "root_page_ready",
            "root_signature": root_page["signature"],
            "title": root_page["title"],
            "action_count": len(root_page["actions"]),
            "mode": args.mode,
        },
    )

    if args.kill_after_seconds:
        log_event(log_path, {"kind": "global_kill_scheduled", "delay_s": args.kill_after_seconds})
        schedule_async_force_stop(args.device_serial, args.package, args.kill_after_seconds, log_path)

    if args.mode == "discover":
        pages_by_signature: dict[str, dict[str, Any]] = {}
        page_order: list[str] = []
        relations: list[dict[str, Any]] = []
        discovered_action_keys: set[str] = set()
        attempted_action_keys: set[str] = set()
        summary = build_summary("discover", root_page)
        add_or_update_page(pages_by_signature, page_order, root_page, [])
        _, poco = discover_page(
            poco,
            root_page,
            [],
            [],
            pages_by_signature,
            page_order,
            relations,
            summary,
            discovered_action_keys,
            attempted_action_keys,
            log_path,
            args.device_uri,
            args.host,
            args.port,
            args.device_serial,
            args.package,
            screen_size,
            args.action_wait,
            args.max_pages,
            args.max_actions_per_page,
        )
        finalize_summary(summary, pages_by_signature, relations, discovered_action_keys, attempted_action_keys)
        write_outputs(out_dir, pages_by_signature, page_order, relations, summary)
        return

    if not args.map_file:
        raise SystemExit("--mode replay 需要提供 --map-file")

    page_order, map_pages, relation_lookup, relations = load_map(Path(args.map_file))
    discovered_action_keys = {
        f"{page['signature']}::{action['key']}"
        for page in map_pages.values()
        for action in (page.get("actions") or [])[: args.max_actions_per_page]
    }
    attempted_action_keys: set[str] = set()
    summary = build_summary("replay", root_page)
    _, poco = replay_page(
        poco,
        page_order[0],
        map_pages,
        relation_lookup,
        [],
        summary,
        discovered_action_keys,
        attempted_action_keys,
        log_path,
        args.device_uri,
        args.host,
        args.port,
        args.device_serial,
        args.package,
        screen_size,
        args.action_wait,
        args.max_actions_per_page,
    )
    finalize_summary(summary, map_pages, relations, discovered_action_keys, attempted_action_keys)
    write_outputs(out_dir, map_pages, page_order, relations, summary)


if __name__ == "__main__":
    main()
