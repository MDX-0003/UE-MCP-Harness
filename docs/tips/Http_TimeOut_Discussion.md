线程安全假说解释不了直连 vs Harness 的差异——两者调同一个 UE Server、同一个工具、同一个 SSE 机制。

真正的差异在这里：

直连测试：1 个 SSE MultipleWriteStream

initialize → JSON 响应（非 SSE）
initialized → 202（非 SSE）
load_toolset → SSE（MultipleWriteStream，瞬间完成）
CaptureAssetImage → SSE（MultipleWriteStream，1-90s）
直连在 CaptureAssetImage 之前只开了 1 个 MultipleWriteStream（load_toolset）。

Harness：50+ 个 SSE MultipleWriteStream

initialize → JSON（非 SSE）
initialized → 202（非 SSE）
list_toolsets → SSE（MultipleWriteStream，瞬间完成）
load_toolset × 50 → SSE（MultipleWriteStream × 50，瞬间完成）
list_tools → JSON（非 SSE）
get_current_level → SSE（MultipleWriteStream，可能失败）
find_actors → SSE（MultipleWriteStream，可能返回大量数据）
GetSelectedActors → SSE（MultipleWriteStream）
...
CaptureAssetImage → SSE（MultipleWriteStream，1-90s）
Harness 在 CaptureAssetImage 之前开了 50+ 个 MultipleWriteStream。

关键差异：我们的 _rpc 如何处理 SSE
_rpc 用 post() + response.content——阻塞等待连接关闭才返回。每个 load_toolset 的 MultipleWriteStream 流程是：


Server: 第1次 OnComplete（空 header + HasAdditionalWrites）→ 数据到达 httpx
Server: 第2次 OnComplete（SSE result，无 HasAdditionalWrites）→ 数据到达 httpx
Server: HTTP Server 检测到没有 HasAdditionalWrites → 关闭连接
httpx: response.content 读完 → 返回
每个都完整消费了。

但 call_tool（用于 CaptureAssetImage）不同：


Server: 第1次 OnComplete（空 header + HasAdditionalWrites）
httpx: stream() 返回 → aiter_lines() 等待
Server: 第2次 OnComplete（SSE result）
httpx: 读到 result → aclose() → 客户端关连接
Server: ？？？客户端先关了
_rpc 等服务器关连接。call_tool 主动关连接。

如果 UE HTTP Server 对 MultipleWriteStream 的客户端主动关闭处理有 bug——比如第一次 aclose() 后 server 端的连接对象未正确复位——那么后续在这个 session 上的 MultipleWriteStream 就会异常。50 个 _rpc 调用都等服务器关，不会触发这个问题。但第一个 CaptureAssetImage 的 aclose() 触发后，第二次就坏了。

这解释了：为什么第一次有时成功、第二次必定失败。 不是概率问题，是状态累积——第一次 call_tool（aclose()）破坏了 server 端状态，第二次直接完蛋。

你怎么看？要不要试把 call_tool 的成功路径也改为等服务器关连接，而不是主动 aclose()？