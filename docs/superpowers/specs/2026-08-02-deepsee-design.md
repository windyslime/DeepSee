# DeepSee 设计文档

日期:2026-08-02
状态:已批准(待用户复核)

## 1. 背景与目标

DeepSeek 官方 API 目前是纯文本模型,无法直接处理图片。本项目(DeepSee)为 DeepSeek
提供一个**可插拔的视觉处理层**,使其获得多模态能力——使用者只需要一次调用,心理模型
就是"DeepSeek 会看图了"。

项目长期目标是贡献给 Codewhale 开源项目:让所有 Codewhale 用户都能"为 DeepSeek
配置自己的视觉模型"。本期只做后端数据处理,前端形态(CLI / MCP server / Codewhale
插件 bundle)留接口、不实现,后续阶段再做。

## 2. 范围

### 本期(核心库,后端数据处理)

- 图片预处理(加载 / 压缩 / 编码)
- 三个视觉后端适配器(OpenAI 兼容 / Anthropic / Gemini)
- DeepSeek 组合层(视觉描述 → 上下文注入 → DeepSeek 推理)
- 配置模型(TOML + 环境变量)
- 统一错误处理与重试
- 完整单元测试(mock 全部外部 API)

### 后续阶段(本期不实现)

- CLI 薄封装
- Codewhale 插件 bundle(MCP server + Skill)
- 独立发布(PyPI、GitHub 仓库、文档站)

## 3. 架构

Python ≥ 3.10 单仓库,依赖最小化(httpx + pillow,零官方 SDK 依赖;测试用
pytest + respx)。

```
DeepSee 仓库
├── deepsee/                    ← 核心库(包名: deepsee)
│   ├── backends/               ← 视觉后端适配器
│   │   ├── base.py             ←   VisionBackend 抽象接口 + 工厂
│   │   ├── openai_compat.py    ←   OpenAI 兼容(/chat/completions,base_url 可配)
│   │   ├── anthropic.py        ←   Claude 原生 messages API
│   │   └── gemini.py           ←   Google GenAI 原生 API
│   ├── pipeline/               ← 视觉任务管道
│   │   ├── image.py            ←   图片加载、压缩、base64 编码
│   │   └── prompts.py          ←   提示词模板(问题驱动的视觉提示词)
│   ├── composer/               ← DeepSeek 组合层
│   │   └── deepseek.py         ←   两步串接:VLM 输出 → DeepSeek 推理
│   ├── config/                 ← 配置加载(TOML + 环境变量,${ENV} 引用)
│   └── errors.py               ← 统一异常层次
├── tests/                      ← 单元测试(全部 mock 外部 API)
├── docs/
├── pyproject.toml
└── README.md
```

### 核心抽象:VisionBackend

```python
class VisionBackend(ABC):
    def describe(self, image: ImageInput, prompt: str, **opts) -> str: ...
```

- 输入统一为 `ImageInput`(本地路径 / HTTP(S) URL / bytes / PIL Image),适配器内部
  转成各家协议的图片格式
- 输出统一为字符串,屏蔽三家 API 的响应差异
- `create_backend(config)` 按配置选择适配器;第三方可注册自定义后端(为将来本地
  VLM 留扩展点)

## 4. 核心 API(主角:完整组合)

```python
def ask_with_image(image: ImageInput, question: str, *, stream: bool = False):
    """完整组合:视觉描述 → DeepSeek 推理。

    stream=False 时返回 str(DeepSeek 的完整回答);
    stream=True 时返回 Iterator[str](逐块输出)。
    """
```

内部步骤(复用点,不重点宣传):

```python
def describe_image(image: ImageInput, prompt: str) -> str:
    """组合管线的第 2 步(视觉→文本),单独可调。"""
```

## 5. 数据流

1. **输入归一**:image 接受 本地路径 / HTTP(S) URL / bytes / PIL Image 四种形态
2. **图片预处理**:解码 → 验证格式(JPEG/PNG/WebP)→ 长边压缩到各家上限(如
   OpenAI 兼容一般 ≤ 2048)→ base64 编码 → 组装成对应协议的图片消息块
3. **视觉描述**:用"用户问题驱动的提示词"调 VLM(带着问题看图,描述聚焦)→ 得视觉
   描述 D
4. **DeepSeek 组合**:构造消息——system:"以下是视觉模型对用户图片的描述,请基于
   它回答";user:问题 + D → 调 DeepSeek API(OpenAI 兼容格式)
5. **返回**:DeepSeek 的回答,支持可选流式(stream=True)

## 6. 配置模型

配置文件(TOML)+ 环境变量双通道,环境变量优先;优先级:环境变量 > TOML > 默认值。

```toml
# deepsee.toml(默认从 cwd / ~/.config/deepsee/ 查找,路径可指定)
[deepseek]
api_key = "${DEEPSEEK_API_KEY}"     # 支持 ${ENV} 引用,也可直接写值
base_url = "https://api.deepseek.com"  # 默认值,可改
model = "deepseek-chat"             # 可换 deepseek-reasoner

[vision]
backend = "openai_compatible"       # openai_compatible | anthropic | gemini
api_key = "${VISION_API_KEY}"
base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"  # 仅 openai_compatible 用
model = "qwen-vl-max"
```

启动时严格校验:缺 api_key、backend 非法、base_url 格式错 → 抛 ConfigError,给出
明确修复提示。

## 7. 错误处理

统一异常层次,全部带后端 / 模型 / HTTP 状态码上下文:

- `ImageError` —— 文件不存在、格式不支持、URL 下载失败、尺寸超限
- `VisionBackendError` —— 三家 VLM 任一失败(内部映射各家错误)
- `ComposeError` —— DeepSeek 调用失败
- `ConfigError` —— 配置缺失 / 非法

对 429 / 5xx 自动指数退避重试(次数可配,默认 2)。

## 8. 工程骨架

- Python ≥ 3.10,包名 `deepsee`,仓库名 DeepSee,LICENSE:MIT
- 依赖:httpx、pillow(运行时);pytest、respx(开发)
- pyproject.toml(hatchling)

## 9. 测试策略(本期重点:全是数据管道)

- **图片处理**:PIL 生成测试图 → 验证压缩尺寸、格式校验、base64 编码、URL 下载(mock)
- **配置**:TOML 解析、${ENV} 引用、环境变量优先级、非法配置报错
- **三个适配器**:respx 模拟各家 API 请求/响应,断言请求 URL/头/体格式、响应解析、
  错误映射——不花真实 API 钱
- **组合层**:mock 视觉后端 + mock DeepSeek,验证"问题驱动的提示词 → 描述注入 →
  最终回答"全链路逻辑
- 真实 API 冒烟测试留作可选手动步骤(需要 key)

## 10. 成功标准(本期)

1. `ask_with_image` 全链路(图片 → 三家任一 VLM → DeepSeek)在 mock 环境下
   100% 覆盖并通过
2. 配置三种后端只需改 TOML 四个字段,无代码改动
3. 错误信息可定位(哪个后端、哪个模型、什么状态码)
4. 零官方 SDK 依赖,`pip install deepsee` 即可用

---

## 11. UI 分析增强(截图识图 + 修改前端能力)

### 11.1 目标

在保持"全部接入外部 API"的前提下,让 DeepSeek 最大程度做到:
- 理解用户发来的任意截图(不限于前端)
- 对前端截图,能**精准定位元素**(如"把那个按钮往右移"所指的按钮),从而正确执行修改指令
- 用户指令所指元素不在截图内、或截图质量不足时,明确引导用户重新截图

### 11.2 自动路由(单次调用)

不再分两阶段(检测 + 分支),而是**一次 VLM 调用**完成分类与输出:

- 提示词要求 VLM 先判断"是否为前端界面截图"
- 是 → `analysis` 输出 UI 结构化对象;否 → `analysis` 输出自然语言描述
- 固定 JSON 外壳:`{"is_ui": bool, "reason": str, "analysis": object|string}`
- 所有图片类型都只需 1 次 VLM 调用(相对两阶段省 50% 请求)

### 11.3 UI 分析 schema

```json
{
  "ui_type": "web_page | mobile_app | desktop_app | other",
  "layout": "布局结构描述,按复杂度写多句,不省略",
  "elements": [
    {
      "id": 1,
      "type": "button | input | text | image | link | icon | card | menu | other",
      "text": "元素文字(无则空)",
      "location": "相对位置描述(自然语言,禁止编造像素坐标)",
      "size": "大致尺寸",
      "style": "详细样式:颜色/字号/背景/边框/圆角/阴影/内边距/交互态",
      "state": "normal | disabled | hover | active(可判断时填写)"
    }
  ],
  "target_found": true,
  "rescreenshot_advice": "元素不在截图内或截图模糊时的重截图指引;正常则空字符串",
  "answer_to_user": "针对用户指令明确目标元素;多相似元素时用位置/样式区分,必要时反问"
}
```

- 元素最多 15 个,优先与用户问题相关;问题相关元素的 style 必须写完整
- `location` 用自然语言相对定位(截图来源任意,VLM 给不出可靠像素)

### 11.4 图片处理:按真实输入尺寸

- 默认**不压缩**,原尺寸传给 VLM(用户可能只截单个按钮/栏目,细节必须保留)
- 仅保护性阈值:长边 > 8192 时才等比缩放(规避各家 API 硬限制)
- 格式仍统一转 JPEG
- 注:此行为变更影响现有 image 测试(3000×1500 不再压到 2048)

### 11.5 提示词关键体验设计(审核补齐)

审核发现并补齐 6 处"未明确的关键体验设计":

1. **局部截图**:截图可能是局部区域,元素可能部分可见/缺上下文 —— 不得误判为"元素不在图中"
2. **截图质量**:模糊/分辨率不足时,VLM 应主动说明并建议重截
3. **元素不存在**:用户指令指向的元素不在截图内 → 必须 `target_found=false` + 重截图建议,不许编造
4. **多相似元素歧义**:用位置/样式区分,无法确定时反问用户
5. **通用分支题图不符**:图与问题无关时,描述中说明"未找到与问题相关的内容"
6. **reason 字段语义**:明确为"一句话判断依据",供 DeepSeek 参考

### 11.6 mode 开关

- `auto`(默认):自动路由(单次调用)
- `ui`:强制 UI 结构化输出(跳过分类)
- `general`:强制通用描述(复用原 `build_vision_prompt`)

### 11.7 降级策略

JSON 解析失败(围栏/杂质/坏格式)→ 把 VLM 原始输出标注"未能结构化解析"注入
DeepSeek,由 DeepSeek 自行判断,流程不中断。

### 11.8 测试策略

- 路由:mock VLM 返回 is_ui true/false → 断言对应分支与注入内容
- JSON 容错:围栏、杂文本、非法 JSON → 提取成功或降级
- 可见性:target_found=false → DeepSeek 消息含重截图建议
- 真实尺寸:常规图保持原尺寸;超阈值才缩放
- mode 开关:auto/ui/general 各自行为
- 回归:既有 46 个测试保持通过(通用路径默认行为不变,受影响的 image/composer 测试同步更新)
