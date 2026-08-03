# DeepSee

为 DeepSeek 官方 API 提供可插拔的视觉处理层,让 DeepSeek 获得多模态能力:
一次 `ask_with_image()` 调用,完成"视觉模型看图 → DeepSeek 推理回答"。

## 安装

```bash
pip install deepsee
```

## 快速开始

```python
from deepsee import ask_with_image

answer = ask_with_image("photo.jpg", "这张图里有什么?")
print(answer)
```

## 配置

配置文件 `deepsee.toml`(放在当前目录或 `~/.config/deepsee/`),也可以只用环境变量
(`DeepSee_DEEPSEEK_API_KEY` 等)。`${ENV}` 可引用环境变量:

```toml
[deepseek]
api_key = "${DEEPSEEK_API_KEY}"

[vision]
backend = "openai_compatible"   # openai_compatible | anthropic | gemini
api_key = "${VISION_API_KEY}"
base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
model = "qwen-vl-max"
```

切换视觉后端只需修改 `backend` / `base_url` / `model` 三个字段。

用环境变量覆盖 `VISION_BACKEND` 切换后端时,TOML 中的 `base_url` / `api_key`
/ `model` 不会沿用(它们属于旧后端:base_url 指向旧主机,key 属于旧供应商,
会被发给错误的主机/供应商)。`base_url` 回落到新后端的官方默认主机 ——
Anthropic 和 Gemini 有默认主机;OpenAI-compatible 没有默认值,必须显式设置
`VISION_BASE_URL`。`api_key` 与 `model` 必须由环境变量显式提供,否则报错。
环境变量与 TOML 中相同的 `VISION_BACKEND` 不算切换,TOML 配置原样保留
(自定义代理 / 审计 / 数据驻留场景)。

## 支持的后端

- **openai_compatible**: Qwen-VL、GPT-4o、GLM-4V、Moonshot 等任意 OpenAI 兼容服务
- **anthropic**: Claude 系列(原生 API)
- **gemini**: Google Gemini(原生 API)

## 流式输出

```python
for chunk in ask_with_image("photo.jpg", "讲个故事", stream=True):
    print(chunk, end="", flush=True)
```

## 许可证

MIT