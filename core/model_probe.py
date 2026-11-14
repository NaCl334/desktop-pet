"""Bounded model checks using draft settings and no conversation history."""
import base64
import io
import random
import re
import time

from PIL import Image, ImageDraw

from core.conversation import ConversationClient, ConversationError, ConversationCancelled


class ModelProbeError(RuntimeError):
    pass


def probe_model(config, vision=False, request_is_current=None, status_callback=None):
    client = ConversationClient(dict(config), request_is_current=request_is_current,
                                status_callback=status_callback)
    if not client.configured:
        raise ModelProbeError("请先填写接口地址和模型名称。")
    content = "请用一句简短的话确认你可以正常回复。"
    expected = None
    if vision:
        colors = [("红", "red", "#e02020"), ("绿", "green", "#20b040"),
                  ("蓝", "blue", "#2050e0"), ("黄", "yellow", "#f0d020")]
        expected = random.sample(colors, 2)
        image = Image.new("RGB", (192, 96))
        draw = ImageDraw.Draw(image)
        for index, color in enumerate(expected):
            draw.rectangle((index * 96, 0, index * 96 + 95, 95), fill=color[2])
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        content = [{"type": "text", "text": "这张图片左右各有一个色块。按从左到右顺序，只说出两个颜色名称。"},
                   {"type": "image_url", "image_url": {"url": "data:image/png;base64,"
                    + base64.b64encode(buffer.getvalue()).decode("ascii")}}]
    started = time.monotonic()
    try:
        reply = client._complete_messages(
            [{"role": "user", "content": content}], temperature=0,
            max_tokens=client._integer("max_tokens", 1024))
    except ConversationCancelled:
        raise
    except ConversationError as exc:
        message = str(exc)
        status = re.search(r"HTTP\s+(\d{3})", message)
        if status:
            raise ModelProbeError("接口返回 HTTP {}，请检查地址、型号和密钥权限。".format(status.group(1))) from None
        if re.search(r"timeout|timed out|超时", message, re.I):
            raise ModelProbeError("连接超时，请检查网络和接口地址。") from None
        if re.search(r"10054|reset|重置", message, re.I):
            raise ModelProbeError("连接被服务器或网络设备重置，请检查网络后重试。") from None
        if "finish_reason=length" in message:
            raise ModelProbeError("模型生成预算耗尽但没有正文，请增加生成预算后重试。") from None
        if "有效 JSON" in message:
            raise ModelProbeError("接口未返回有效JSON，请检查接口地址是否兼容聊天协议。") from None
        if "没有返回可显示" in message:
            raise ModelProbeError("接口已响应，但没有可显示的回复正文。") from None
        raise ModelProbeError("未取得有效回复，请检查网络、接口配置和模型可用性。") from None
    if not reply.strip():
        raise ModelProbeError("接口没有返回可显示的文本。")
    if expected:
        positions = [min((reply.lower().find(name) for name in color[:2]
                          if name in reply.lower()), default=-1) for color in expected]
        if positions[0] < 0 or positions[1] <= positions[0]:
            raise ModelProbeError("接口已回复，但未通过图片识别校验，请确认该型号支持视觉输入。")
    return {"seconds": round(time.monotonic() - started, 2),
            "reply": re.sub(r"\s+", " ", reply).strip()[:120]}
