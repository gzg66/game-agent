"""Set-of-Mark 视觉交互服务。"""
import cv2
import json
import base64
from typing import Any

class SoMVisionService:
    def __init__(self, llm_client: Any):
        self.llm_client = llm_client

    def draw_marks_and_get_base64(self, image_path: str, unknown_nodes: list) -> tuple[str, dict[str, str]]:
        if not image_path:
            return "", {}
            
        img = cv2.imread(image_path)
        if img is None:
            return "", {}

        h, w, _ = img.shape
        node_mapping = {}

        for idx, n_info in enumerate(unknown_nodes):
            mark_id = str(idx + 1)
            node_mapping[mark_id] = n_info.node.path
            
            if n_info.node.pos and len(n_info.node.pos) == 2:
                cx, cy = n_info.node.pos
                px, py = int(cx * w), int(cy * h)
                
                # 画红框和白底黑字标签
                box_half_size = 30
                top_left = (max(0, px - box_half_size), max(0, py - box_half_size))
                bottom_right = (min(w, px + box_half_size), min(h, py + box_half_size))
                cv2.rectangle(img, top_left, bottom_right, (0, 0, 255), 2)
                cv2.rectangle(img, (top_left[0], top_left[1]-20), (top_left[0]+25, top_left[1]), (255, 255, 255), -1)
                cv2.putText(img, mark_id, (top_left[0]+5, top_left[1]-5), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 1)

        _, buffer = cv2.imencode('.jpg', img)
        b64_str = base64.b64encode(buffer).decode('utf-8')
        return b64_str, node_mapping

    def analyze_candidates(self, image_path: str, candidate_nodes: list) -> dict[str, dict[str, Any]]:
        if not candidate_nodes or not self.llm_client:
            return {}

        b64_img, node_mapping = self.draw_marks_and_get_base64(image_path, candidate_nodes)
        if not b64_img:
            return {}

        candidate_summaries: list[str] = []
        for idx, node_info in enumerate(candidate_nodes, start=1):
            node = node_info.node
            candidate_summaries.append(
                f"{idx}. name={node.name!r}, text={node.text!r}, type={node.node_type!r}, "
                f"pos={node.pos}, size={node.size}, rule_role={node_info.role.value}, "
                f"rule_conf={getattr(node_info, 'confidence', 0.0):.2f}, "
                f"candidate_reason={node.candidate_reason!r}"
            )

        prompt = f"""
        这是一张游戏界面截图。我已经用红框和数字标记了若干潜在可操作区域。
        你要判断这些标记是否真的值得自动化点击，并推断它们的语义动作。

        可选的 action_type 只能是：
        close, back, confirm, skip, reward_claim, primary_entry, battle_start, dangerous_action, unknown

        请综合截图视觉信息和以下节点元数据：
        {chr(10).join(candidate_summaries)}

        请严格输出 JSON，格式如下：
        {{
          "nodes": [
            {{
              "id": "1",
              "is_actionable": true,
              "action_type": "primary_entry",
              "confidence": 0.93,
              "reason": "登录主按钮"
            }}
          ]
        }}

        要求：
        1. confidence 取值范围 0 到 1。
        2. 如果该标记只是装饰、标题、背景、纯文本，请设置 is_actionable=false。
        3. 如果不确定动作类型，action_type 填 unknown。
        4. 不要输出 Markdown，不要输出额外解释。
        """
        try:
            response_text = self.llm_client.chat(prompt=prompt, image_base64=b64_img)
            result_json = json.loads(response_text)

            raw_nodes = result_json.get("nodes", [])
            if not isinstance(raw_nodes, list):
                raw_nodes = []

            final_mapping: dict[str, dict[str, Any]] = {}
            for raw_node in raw_nodes:
                if not isinstance(raw_node, dict):
                    continue
                mark_id = str(raw_node.get("id", "")).strip()
                if mark_id not in node_mapping:
                    continue

                action_type = str(raw_node.get("action_type", "unknown")).strip().lower()
                if action_type not in {
                    "close",
                    "back",
                    "confirm",
                    "skip",
                    "reward_claim",
                    "primary_entry",
                    "battle_start",
                    "dangerous_action",
                    "unknown",
                }:
                    action_type = "unknown"

                confidence = raw_node.get("confidence", 0.0)
                if not isinstance(confidence, (int, float)):
                    confidence = 0.0
                confidence = max(0.0, min(1.0, float(confidence)))

                final_mapping[node_mapping[mark_id]] = {
                    "is_actionable": bool(raw_node.get("is_actionable", False)),
                    "action_type": action_type,
                    "confidence": confidence,
                    "reason": str(raw_node.get("reason", "")).strip() or "vision_inferred",
                }
            return final_mapping
        except Exception as e:
            print(f"LLM 视觉分析失败: {e}")
            return {}