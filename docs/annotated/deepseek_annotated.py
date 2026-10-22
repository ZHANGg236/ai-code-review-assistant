# =============================================================================
# services/deepseek.py 逐行注释学习版
#
# 说明：
#   1. 这是学习版，生产代码请看 D:\FastAPI\services\deepseek.py。
#   2. 这个文件只负责一件事：把 DeepSeek 的 SSE 流转成 Python 异步字符串流。
#   3. 多行字符串内部不强行加 #，否则会改变字符串内容。
# =============================================================================

"""DeepSeek 流式调用封装。

把 HTTP 细节放在这里，主应用只负责鉴权、校验和组装 SSE 事件。
"""

from __future__ import annotations  # 让类型注解延迟求值，兼容更多写法

import json  # 标准库：解析 DeepSeek 返回的 JSON
from typing import AsyncIterator  # 类型提示：表示异步迭代器

import httpx  # 第三方：异步 HTTP 客户端


class DeepSeekStreamError(Exception):  # 自定义异常：表示上游流式调用失败
    """DeepSeek 在流式调用中返回了错误。"""  # 文档字符串

    def __init__(self, message: str, status_code: int | None = None) -> None:  # 构造函数
        super().__init__(message)  # 把错误信息交给父类 Exception
        self.status_code = status_code  # 保存 HTTP 状态码，方便主应用映射提示


async def stream_review(  # 定义异步生成器函数
    *,  # 后面的参数必须用关键字传参，避免顺序传错
    api_url: str,  # DeepSeek 接口地址
    api_key: str,  # DeepSeek API Key
    model: str,  # 模型名
    system_prompt: str,  # 系统提示词
    user_prompt: str,  # 用户提示词
    max_tokens: int,  # 最大输出 token 数
    timeout: float,  # 读取超时时间
    client: httpx.AsyncClient | None = None,  # 可选的 HTTP 客户端，测试时注入
) -> AsyncIterator[str]:  # 返回类型：异步产出字符串
    """调用 DeepSeek 流式接口，逐段产出文本。

    Args:
        client: 可选的自定义 AsyncClient，主要用于测试时注入 MockTransport。
    """
    payload = {  # 构造发给 DeepSeek 的请求体
        "model": model,  # 模型名
        "messages": [  # 对话消息列表
            {"role": "system", "content": system_prompt},  # 系统消息
            {"role": "user", "content": user_prompt},  # 用户消息
        ],
        "temperature": 0.3,  # 温度，越低越稳定
        "max_tokens": max_tokens,  # 最大输出 token
        "stream": True,  # 关键：开启流式返回
    }
    headers = {  # 请求头
        "Authorization": f"Bearer {api_key}",  # Bearer Token 认证
        "Content-Type": "application/json",  # 请求体是 JSON
        "Accept": "text/event-stream",  # 告诉服务端我要 SSE
    }

    owns_client = client is None  # 记录 client 是不是自己创建的
    if client is None:  # 如果没有传入 client
        client = httpx.AsyncClient(  # 自己创建一个异步客户端
            timeout=httpx.Timeout(timeout, connect=10.0),  # 连接 10 秒，读取 timeout 秒
        )

    try:  # 确保最后能关闭自己创建的 client
        async with client.stream(  # 建立流式连接
            "POST",  # HTTP 方法
            api_url,  # 请求地址
            headers=headers,  # 请求头
            json=payload,  # 请求体
        ) as response:  # response 是流式响应对象
            if response.status_code >= 400:  # 如果上游返回 4xx/5xx
                body = (await response.aread()).decode(  # 读取响应体并解码
                    "utf-8",  # 按 UTF-8 解码
                    errors="replace",  # 解码失败时用替换字符
                )
                raise DeepSeekStreamError(body[:1000], response.status_code)  # 抛自定义异常

            async for line in response.aiter_lines():  # 逐行读取 SSE
                line = line.strip()  # 去掉首尾空白
                if not line or not line.startswith("data:"):  # 跳过空行和非 data 行
                    continue  # 继续下一行

                data = line[5:].strip()  # 去掉 "data:" 前缀
                if data == "[DONE]":  # 如果是结束标记
                    break  # 结束循环

                try:  # 尝试解析 JSON
                    event = json.loads(data)  # 把 JSON 字符串转成字典
                except json.JSONDecodeError:  # 如果 JSON 不合法
                    continue  # 跳过这一行

                if not isinstance(event, dict):  # 如果解析结果不是字典
                    continue  # 跳过

                if event.get("error"):  # 如果事件里有 error 字段
                    raise DeepSeekStreamError(str(event["error"]), None)  # 抛异常

                choices = event.get("choices") or []  # 取出 choices 列表
                if not choices or not isinstance(choices[0], dict):  # 如果没有 choices 或格式不对
                    continue  # 跳过

                delta = choices[0].get("delta") or {}  # 取出 delta
                if not isinstance(delta, dict):  # 如果 delta 不是字典
                    continue  # 跳过

                content = delta.get("content")  # 取出增量文本
                if isinstance(content, str) and content:  # 如果是非空字符串
                    yield content  # 产出一段文本
    finally:  # 无论成功还是异常都会执行
        if owns_client:  # 如果 client 是自己创建的
            await client.aclose()  # 关闭连接，释放资源
