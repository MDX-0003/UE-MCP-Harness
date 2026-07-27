"""诊断 VisionSubAgent.classify() 纯文本调用 —— 为什么对 MiMo 代理失败。

用法: uv run python tests/diag_classify.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

# 确保仓库根在 sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness.verification.config import load_vision_env
from harness.config import Config
from harness.verification.vision_agent import VisionSubAgent, VISION_CLASSIFY_PROMPT


_TEST_PROPERTIES = [
    {"index": 1, "actor_type": "DirectionalLight", "actor_name": "Light_0",
     "refPath": "/Game/World.World:PersistentLevel.Light_0.LightComponent0",
     "property": "intensity"},
    {"index": 2, "actor_type": "DirectionalLight", "actor_name": "Light_0",
     "refPath": "/Game/World.World:PersistentLevel.Light_0.LightComponent0",
     "property": "lightColor"},
    {"index": 3, "actor_type": "DirectionalLight", "actor_name": "Light_0",
     "refPath": "/Game/World.World:PersistentLevel.Light_0.LightComponent0",
     "property": "temperature"},
    {"index": 4, "actor_type": "SkyAtmosphere", "actor_name": "Sky_0",
     "refPath": "/Game/World.World:PersistentLevel.Sky_0.SkyAtmosphereComponent",
     "property": "rayleighScattering"},
    {"index": 5, "actor_type": "ExponentialHeightFog", "actor_name": "Fog_0",
     "refPath": "/Game/World.World:PersistentLevel.Fog_0.HeightFogComponent0",
     "property": "fogDensity"},
]


def build_prompt(props: list[dict]) -> str:
    """复制 _build_mimo_prompt 的核心逻辑"""
    lines = [
        "以下是从 UE 场景中提取的氛围组件属性，每个属性有一个索引编号 [N]。",
        "请筛选与氛围视觉表现相关的属性，对每个相关属性的索引编号标注其影响的维度：",
        "brightness / contrast / color_temp / color_cast / saturation / haze / shadow_direction / sky。",
        "",
        "## 属性索引",
        "",
    ]
    for p in props:
        lines.append(f"  [{p['index']}] {p['property']}  ({p['actor_type']})")
    lines.append("")
    lines.append(
        "输出格式：一个 JSON 对象，key 为维度名，value 为相关属性的索引编号数组。"
        "只输出 JSON，不要有其他文字。"
    )
    lines.append('{"brightness":[1],"color_temp":[1,3],"haze":[5]}')
    return "\n".join(lines)


async def main() -> None:
    print("=" * 60)
    print("VisionSubAgent.classify() -- pure text diagnosis")
    print("=" * 60)

    # 加载 .vision.env 到 os.environ 并从环境变量构建 Config
    load_vision_env()
    config = Config.from_env()
    api_key_ok = bool(config.vision_api_key and config.vision_api_key != "test-key")
    print(f"\nAPI Key:    {'OK' if api_key_ok else 'MISSING/test-key'}")
    print(f"API URL:    {config.vision_api_base_url}")
    print(f"Model:      {config.vision_model}")

    prompt = build_prompt(_TEST_PROPERTIES)
    print(f"\nClassification prompt ({len(prompt)} chars):")
    print("-" * 40)
    print(prompt)
    print("-" * 40)

    agent = VisionSubAgent(config)
    print(f"\nCalling classify() ...")

    try:
        result = await agent.classify(prompt)
        print(f"\n[OK] classify() succeeded")
        print(f"Return type: {type(result).__name__}")
        print(f"Result: {json.dumps(result, ensure_ascii=False, indent=2)}")
    except ValueError as e:
        print(f"\n[FAIL] classify() raised ValueError")
        print(f"Error: {e}")
    except Exception as e:
        print(f"\n[FAIL] classify() raised {type(e).__name__}")
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(main())
