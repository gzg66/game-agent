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

    def analyze_unknown_nodes(self, image_path: str, unknown_nodes: list) -> dict[str, str]:
        if not unknown_nodes or not self.llm_client:
            return {}

        b64_img, node_mapping = self.draw_marks_and_get_base64(image_path, unknown_nodes)
        if not b64_img:
            return {}

        prompt = """
        这是一张游戏界面截图。我已经使用红框标记了几个未知的可点击按钮，并标注了数字编号。
        请你扮演游戏自动化测试专家，分析这些编号按钮的语义功能。
        可选的语义标签为：close, back, confirm, skip, reward_claim, primary_entry, battle_start, shop, unknown。
        请严格输出 JSON 格式，键为数字编号，值为推断的语义标签。例如：{"1": "close", "2": "unknown"}
        """
        try:
            # TODO: 替换为你实际的 LLM 调用方法
            response_text = self.llm_client.chat(prompt=prompt, image_base64=b64_img)
            result_json = json.loads(response_text)
            
            final_mapping = {}
            for mark_id, role_str in result_json.items():
                if mark_id in node_mapping:
                    final_mapping[node_mapping[mark_id]] = role_str
            return final_mapping
        except Exception as e:
            print(f"LLM 视觉分析失败: {e}")
            return {}