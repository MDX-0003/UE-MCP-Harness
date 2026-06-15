"""工具调用拦截器基类 — Contract 1。

Contract 1 定义了两个接口：
  - ToolCallCompleted: 每次工具调用结束后传递的完整数据
  - ToolCallInterceptor: pre/post 双钩子，子类可选覆盖

涉及的模块：
  - 003 可观测性: ToolCallLogger 实现 post_call 写 JSONL
  - 008 State Cache: StateCacheInterceptor 实现 post_call 更新缓存
  - 011 安全护栏（未来）: 实现 pre_call 做规则拦截
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("harness.interceptor")


@dataclass
class ToolCallCompleted:
    """一次完整的工具调用所携带的全部信息。

    raw_result / parsed_text 的分工：
      - raw_result:   完整 JSON-RPC result dict/list/str，需要原始结构时用（如图片 base64）
      - parsed_text:  已剥离 MCP content array 外层的纯文本结果，handler 可直读
                      等价于 raw_result["content"][0]["text"] 的提取值。
                      如果 result 不是 MCP content array 格式，则 parsed_text = str(raw_result)
    """

    name: str
    args: dict
    raw_result: Any = None          # JsonRpcResponse.result — 完整原始结果
    parsed_text: str | None = None  # 已提取的 content[0].text，handler 可直读
    error: Exception | None = None
    duration_ms: float = 0.0


class ToolCallInterceptor:
    """工具调用拦截器基类。

    pre_call 和 post_call 都是可选的 —— 子类只覆盖需要的方法。
    当前 003 和 008 都只需 post_call；pre_call 保留给未来 #011 Safety Guardrails 使用。

    设计约束：
      - 拦截器之间独立，不读取其他拦截器的状态
      - post_call 返回 None，不能改变 CallToolResult
      - pre_call 可以拒绝调用（抛异常），args 修改向下传递
      - post_call 中抛异常不阻断主链路
    """

    async def pre_call(self, name: str, args: dict) -> dict:
        """在转发到 UE 之前调用。可修改 args（返回修改后的）。默认透传。"""
        return args

    async def post_call(self, event: ToolCallCompleted) -> None:
        """在 UE 返回结果之后调用。默认空操作。"""
        pass


class DebugPreCallInterceptor(ToolCallInterceptor):
    """验证 pre_call 链路的临时拦截器。

    后续有真正需要 pre 的模块（#011 Safety）时替换掉。
    """

    async def pre_call(self, name: str, args: dict) -> dict:
        logger.debug("[pre_call] 工具: %s, args keys: %s", name, list(args.keys()))
        return args
