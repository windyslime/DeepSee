"""Prompt templates for the vision pipeline."""


def build_vision_prompt(question: str) -> str:
    """Build the question-driven prompt sent to the vision model.

    The vision model sees the user's question while looking at the image so
    its description focuses on what the user actually wants to know.
    """
    return (
        "请仔细查看这张图片,并针对以下问题给出准确、详细的回答。"
        "回答请只描述与问题相关的视觉内容,不要虚构图片中不存在的信息。\n"
        f"问题: {question}"
    )