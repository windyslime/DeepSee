"""Prompt templates for the vision pipeline."""

from __future__ import annotations


def build_vision_prompt(question: str) -> str:
    """Build the question-driven prompt for general image description."""
    return (
        "请仔细查看这张图片,并针对以下问题给出准确、详细的回答。"
        "回答请只描述与问题相关的视觉内容,不要虚构图片中不存在的信息。\n"
        f"问题: {question}"
    )


def build_auto_route_prompt(question: str) -> str:
    """Single-call auto-routing prompt: classify then analyze in one shot."""
    return AUTO_ROUTE_PROMPT + f"\n用户问题: {question}\n只输出 JSON,不要输出任何其他文字。"


def build_ui_analysis_prompt(question: str) -> str:
    """Forced UI-analysis prompt (mode='ui'), skipping classification."""
    return UI_ANALYSIS_PROMPT + f"\n用户问题: {question}\n只输出 JSON,不要输出任何其他文字。"


AUTO_ROUTE_PROMPT = """你是 DeepSee 视觉层。请分析用户提供的图片,严格按以下 JSON 输出:

{
  "is_ui": <true|false>,
  "reason": "<一句话判断依据,如:网页界面截图 / 自然照片 / 截图模糊无法判断>",
  "analysis": <对象或字符串>
}

判断规则:
1. 先判断图片是否为"前端界面截图"(网页、移动 App、桌面软件界面)。
   注意:截图可能是局部区域(只包含某个栏目/按钮),元素可能部分可见或缺少上下文,
   不要因此误判"元素不在图中"。
2. 若 is_ui 为 true,analysis 输出 UI 结构化对象,包含:
   - "ui_type": "web_page" | "mobile_app" | "desktop_app" | "other"
   - "layout": 布局结构描述,按界面复杂度写多句,不要刻意省略
   - "elements": 界面元素数组,最多 15 个,优先列出与用户问题相关的元素。每个元素:
       "id": 数字编号
       "type": "button"|"input"|"text"|"image"|"link"|"icon"|"card"|"menu"|"other"
       "text": 元素文字(无则为空字符串)
       "location": 相对位置描述(如"右上角,紧贴搜索框右侧"),用自然语言,禁止编造像素坐标
       "size": 大致尺寸(如"约 120x40px")
       "style": 尽可能详细的视觉样式:文字颜色与字号、背景色/渐变、边框与圆角、阴影、内边距、hover/选中态变化等;与用户问题相关的元素样式必须写完整
       "state": "normal"|"disabled"|"hover"|"active"(可判断时填写)
   - "target_found": 用户指令指向的目标元素是否出现在这张截图中(元素被裁切但主体可见也算 true)
   - "rescreenshot_advice": target_found 为 false 时,明确告诉用户缺少什么、应重新截图哪个区域;截图模糊/分辨率不足时也在此说明并建议重截;否则为空字符串
   - "answer_to_user": 针对用户问题,明确指出目标元素是谁;若存在多个相似元素,用位置和样式区分;无法确定时请用户进一步明确
3. 若 is_ui 为 false,analysis 必须是字符串:对图片的自然语言描述,回答用户问题;
   若用户问题明显与图片无关(如图片中不存在用户提及的事物),在描述中说明"图片中未找到与问题相关的内容"。
"""

UI_ANALYSIS_PROMPT = """你是 DeepSee 前端 UI 分析器。这张图片是前端界面截图,请严格按以下 JSON 输出:

{
  "ui_type": "web_page" | "mobile_app" | "desktop_app" | "other",
  "layout": "布局结构描述,按界面复杂度写多句,不要刻意省略",
  "elements": [
    {
      "id": 1,
      "type": "button"|"input"|"text"|"image"|"link"|"icon"|"card"|"menu"|"other",
      "text": "元素文字(无则空字符串)",
      "location": "相对位置描述,自然语言,禁止编造像素坐标",
      "size": "大致尺寸",
      "style": "尽可能详细的视觉样式:颜色/字号/背景/边框/圆角/阴影/内边距/交互态;与用户问题相关的元素必须写完整",
      "state": "normal"|"disabled"|"hover"|"active"(可判断时填写)
    }
  ],
  "target_found": true,
  "rescreenshot_advice": "元素不在截图内或截图模糊时,给出明确的重截图指引;正常则为空字符串",
  "answer_to_user": "针对用户问题明确目标元素;多相似元素用位置/样式区分,无法确定时请用户明确"
}

注意:
- 截图可能是局部区域,元素可能部分可见或缺少上下文,不要误判为"元素不在图中"
- 用户指令指向的元素确实不在截图内时,target_found 必须为 false,并在 rescreenshot_advice 说明缺什么、应重截哪个区域
- 截图模糊/分辨率不足时,在 rescreenshot_advice 说明并建议重截
- 元素最多 15 个,优先与用户问题相关;问题相关元素的 style 必须写完整
"""

__all__ = [
    "AUTO_ROUTE_PROMPT",
    "UI_ANALYSIS_PROMPT",
    "build_vision_prompt",
    "build_auto_route_prompt",
    "build_ui_analysis_prompt",
]
