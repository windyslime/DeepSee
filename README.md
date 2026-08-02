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