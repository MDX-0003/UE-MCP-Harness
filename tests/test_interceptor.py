"""测试 harness.interceptor 模块 — ToolCallInterceptor 接口 + ToolCallCompleted。"""

from harness.interceptor import (
    DebugPreCallInterceptor,
    ToolCallCompleted,
    ToolCallInterceptor,
)


# ---- ToolCallCompleted ----

class TestToolCallCompleted:
    """测试 ToolCallCompleted 数据类。"""

    def test_basic_fields(self) -> None:
        event = ToolCallCompleted(
            name="SceneTools.find_actors",
            args={"glob": "Light*"},
            raw_result={"content": [{"type": "text", "text": "Light_0"}]},
            parsed_text="Light_0",
            duration_ms=42.5,
        )
        assert event.name == "SceneTools.find_actors"
        assert event.args == {"glob": "Light*"}
        assert event.parsed_text == "Light_0"
        assert event.error is None
        assert event.duration_ms == 42.5

    def test_error_field(self) -> None:
        err = RuntimeError("连接超时")
        event = ToolCallCompleted(
            name="SceneTools.find_actors",
            args={},
            error=err,
            duration_ms=1200.0,
        )
        assert event.error is err
        assert event.raw_result is None
        assert event.parsed_text is None

    def test_defaults(self) -> None:
        """未传的参数应有合理默认值。"""
        event = ToolCallCompleted(name="test", args={})
        assert event.raw_result is None
        assert event.parsed_text is None
        assert event.error is None
        assert event.duration_ms == 0.0


# ---- ToolCallInterceptor 基类 ----

class TestToolCallInterceptor:
    """测试基类的默认行为。"""

    async def test_pre_call_passthrough(self) -> None:
        ic = ToolCallInterceptor()
        args = {"x": 1}
        result = await ic.pre_call("test", args)
        assert result is args  # 默认透传——返回同一个 dict
        assert result == {"x": 1}

    async def test_post_call_noop(self) -> None:
        ic = ToolCallInterceptor()
        event = ToolCallCompleted(name="test", args={})
        # 不抛异常即通过
        await ic.post_call(event)

    async def test_custom_interceptor(self) -> None:
        """子类可以覆盖 pre_call 和 post_call。"""
        calls: list[str] = []

        class TestInterceptor(ToolCallInterceptor):
            async def pre_call(self, name, args):
                calls.append(f"pre:{name}")
                return args

            async def post_call(self, event):
                calls.append(f"post:{event.name}")

        ic = TestInterceptor()
        await ic.pre_call("find_actors", {})
        await ic.post_call(ToolCallCompleted(name="find_actors", args={}))
        assert calls == ["pre:find_actors", "post:find_actors"]


# ---- DebugPreCallInterceptor ----

class TestDebugPreCallInterceptor:
    """测试调试拦截器的 pre_call 行为。"""

    async def test_pre_call_passthrough(self) -> None:
        ic = DebugPreCallInterceptor()
        args = {"glob": "Light*"}
        result = await ic.pre_call("SceneTools.find_actors", args)
        assert result is args  # 透传
        assert result == {"glob": "Light*"}

    async def test_pre_call_does_not_modify_args(self) -> None:
        ic = DebugPreCallInterceptor()
        original = {"actor": {"name": "Light_0"}, "xform": {"location": {"x": 0}}}
        result = await ic.pre_call("ActorTools.set_actor_transform", dict(original))
        assert result == original
