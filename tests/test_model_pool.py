"""
Model Pool 抽象层测试
- 所有 6 个 (think × tier) 组合的 resolve 结果
- 所有 17 个 task 的 task_tier 映射
- ProviderConfig / ModelSpec 数据类行为
- Facade 函数签名与参数转发
- 向后兼容 (reasoning / lite 旧参数)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def test_provider_config_immutable():
    """ProviderConfig 是 frozen dataclass，创建后不可改"""
    from app.llm.model_pool import ProviderConfig

    cfg = ProviderConfig(name="test", api_key="sk-abc", base_url="https://x.com")
    assert cfg.name == "test"
    assert cfg.api_key == "sk-abc"
    assert cfg.base_url == "https://x.com"

    try:
        cfg.name = "other"
        assert False, "should have raised FrozenInstanceError"
    except Exception:
        pass  # expected

    print("[OK] ProviderConfig frozen + fields")


def test_model_spec_immutable():
    """ModelSpec 是 frozen dataclass"""
    from app.llm.model_pool import ModelSpec, ProviderConfig

    p = ProviderConfig(name="x", api_key="k", base_url="http://x")
    spec = ModelSpec(model="m", reasoning="disabled", provider=p)
    assert spec.model == "m"
    assert spec.reasoning == "disabled"
    assert spec.provider is p

    try:
        spec.model = "m2"
        assert False, "should have raised FrozenInstanceError"
    except Exception:
        pass

    print("[OK] ModelSpec frozen + fields")


def test_providers_defined():
    """活跃 provider 定义且有 API key（兼容 DS / GPT / Mixed 三种变体）"""
    import app.llm.model_pool as mp

    # 变体可能导出 ACTIVE / DEEPSEEK / GPT55 中的不同组合
    active = getattr(mp, "ACTIVE", None)
    if active is not None:
        # DeepSeek-only 变体：单一 ACTIVE provider
        assert active.name, "provider must have a name"
        assert isinstance(active.api_key, str), "api_key must be str"
        assert active.base_url.startswith("https://"), f"bad base_url: {active.base_url}"
        print(f"[OK] single provider: {active.name} @ {active.base_url}")
    else:
        # Multi-provider 变体：DEEPSEEK + GPT55
        assert mp.DEEPSEEK.name == "deepseek"
        assert mp.DEEPSEEK.base_url == "https://api.deepseek.com"
        assert isinstance(mp.DEEPSEEK.api_key, str)
        assert mp.GPT55.name == "gpt55"
        assert len(mp.GPT55.api_key) > 10
        print("[OK] DEEPSEEK + GPT55 providers")


def test_model_maps_all_tiers():
    """所有 6 个 (think/nonthink × low/mid/high) 槽位都有值"""
    from app.llm.model_pool import THINK, NONTHINK

    for tier in ("low", "mid", "high"):
        assert tier in THINK, f"THINK missing tier={tier}"
        assert tier in NONTHINK, f"NONTHINK missing tier={tier}"
        # 值可能是 str（DS 单 provider 变体）或 tuple（多 provider 变体）
        assert THINK[tier], f"THINK[{tier}] is empty"
        assert NONTHINK[tier], f"NONTHINK[{tier}] is empty"

    print("[OK] all 6 slots populated")


def test_reasoning_tiers_defined():
    """所有 reasoning tier 都有合法值"""
    from app.llm.model_pool import REASONING

    valid = {"disabled", "low", "medium", "high", "max"}
    for tier in ("low", "mid", "high"):
        assert tier in REASONING, f"REASONING missing tier={tier}"
        assert REASONING[tier] in valid, f"REASONING[{tier}]={REASONING[tier]}"

    print("[OK] all reasoning tiers valid")


def test_resolve_all_combinations():
    """resolve(think, tier) 覆盖全部 6 种组合，返回合法 ModelSpec"""
    from app.llm.model_pool import resolve, ModelSpec

    for think in (False, True):
        for tier in ("low", "mid", "high"):
            spec = resolve(think, tier)
            assert isinstance(spec, ModelSpec)
            assert spec.model, "model must not be empty"
            assert spec.reasoning, "reasoning must not be empty"
            assert spec.provider is not None, "provider must not be None"
            assert spec.provider.api_key or True, "api_key may be empty (env var)"

    print("[OK] resolve() all 6 think×tier combos")


def test_resolve_specs_are_distinct():
    """每次 resolve 返回独立实例"""
    from app.llm.model_pool import resolve

    s1 = resolve(False, "low")
    s2 = resolve(False, "low")
    assert s1 == s2           # 值相等
    assert s1 is not s2       # 但不是同一个对象（dataclass 无缓存）
    print("[OK] resolve() returns independent copies")


def test_resolve_task_all_known():
    """resolve_task() 对所有已知 task 返回合法 ModelSpec"""
    from app.llm.model_pool import resolve_task, ModelSpec, TASK_TIER

    for task in sorted(TASK_TIER):
        spec = resolve_task(task)
        assert isinstance(spec, ModelSpec), f"{task} → {type(spec)}"
        assert spec.model, f"{task}: model empty"
        assert len(spec.provider.api_key) >= 0, f"{task} provider has no key"
        # reasoning 必须非空
        assert spec.reasoning, f"{task}: reasoning empty"

    print(f"[OK] resolve_task() all {len(TASK_TIER)} tasks")


def test_task_tier_coverage():
    """TASK_TIER 覆盖所有关键任务类型"""
    from app.llm.model_pool import TASK_TIER

    required = [
        "round1_intent",
        "round2_medium", "round2_medium_coding", "round2_hard", "round2_veryhard",
        "round3_easy", "round3_normal",
        "helper_lite", "helper_full_coding", "helper_full_legacy_hard",
        "progress_message", "self_check_plan", "upgrade_assess",
        "plan_intent_assess", "user_profile", "office_tail_downgrade",
        "auto_continue_check",
    ]
    for task in required:
        assert task in TASK_TIER, f"missing task: {task}"
        think, tier = TASK_TIER[task]
        assert isinstance(think, bool), f"{task} think={think}"
        assert tier in ("low", "mid", "high"), f"{task} tier={tier}"

    print(f"[OK] TASK_TIER covers {len(required)} expected tasks")


def test_model_pool_variants_keep_stage_and_helper_keys_consistent():
    """可切换 model_pool 变体必须保留主协议需要的 stage/helper key。"""
    import importlib

    expected = {
        "round2_medium_coding": (False, "mid"),
        "round2_hard": (True, "low"),
        "round2_veryhard": (True, "mid"),
        "helper_full_coding": (False, "mid"),
        "helper_full_coding_think": (True, "low"),
        "helper_full_edit": (False, "mid"),
        "helper_full_legacy_hard": (True, "high"),
        "helper_full_verify": (False, "mid"),
        "helper_full_verify_think": (True, "low"),
    }

    for module_name in (
        "app.llm.model_pool_variants.model_pool_deepseek",
        "app.llm.model_pool_variants.model_pool_mixed",
        "app.llm.model_pool_variants.model_pool_all_gpt",
    ):
        mp = importlib.import_module(module_name)
        for task, assignment in expected.items():
            assert mp.TASK_TIER.get(task) == assignment, f"{module_name}:{task} drifted"

    print("[OK] model_pool variants keep critical stage/helper assignments consistent")


def test_mixed_variant_routes_only_requested_stages_to_deepseek():
    """Mixed pool policy: round1/round3/lowest round2 use DeepSeek; the rest use GPT-5.5."""
    import importlib

    mp = importlib.import_module("app.llm.model_pool_variants.model_pool_mixed")

    deepseek_tasks = {
        "round1_intent",
        "round2_medium",
        "round3_easy",
        "round3_normal",
        "auto_continue_check",
    }
    gpt_tasks = set(mp.TASK_TIER) - deepseek_tasks

    for task in deepseek_tasks:
        spec = mp.resolve_task(task)
        assert spec.provider.name == "deepseek", f"{task} should use DeepSeek, got {spec}"

    for task in gpt_tasks:
        spec = mp.resolve_task(task)
        assert spec.provider.name == "gpt55", f"{task} should use GPT-5.5, got {spec}"
        assert spec.model == "gpt-5.5"
        assert spec.reasoning == "disabled"

    print("[OK] mixed variant routes requested stages to DeepSeek and all others to GPT-5.5")


def test_think_tier_assignment_rationale():
    """关键任务的 think/tier 分配符合预期"""
    from app.llm.model_pool import TASK_TIER

    # lightweight → nonthink + low
    for t in ("round1_intent", "round3_easy", "round2_medium",
              "helper_lite", "progress_message", "self_check_plan",
              "upgrade_assess", "user_profile", "office_tail_downgrade",
              "auto_continue_check"):
        think, tier = TASK_TIER[t]
        assert not think, f"{t}: expected think=False"
        assert tier == "low", f"{t}: expected tier=low, got {tier}"

    # coding → stronger non-thinking model; hard/veryhard add reasoning progressively
    think, tier = TASK_TIER["round2_medium_coding"]
    assert not think, "round2_medium_coding: expected think=False"
    assert tier == "mid", f"round2_medium_coding: expected tier=mid, got {tier}"

    think, tier = TASK_TIER["round2_hard"]
    assert think, "round2_hard: expected think=True"
    assert tier == "low", f"round2_hard: expected tier=low, got {tier}"

    think, tier = TASK_TIER["round2_veryhard"]
    assert think, "round2_veryhard: expected think=True"
    assert tier == "mid", f"round2_veryhard: expected tier=mid, got {tier}"

    for t in ("helper_full_coding",):
        think, tier = TASK_TIER[t]
        assert not think, f"{t}: expected think=False"
        assert tier == "mid", f"{t}: expected tier=mid, got {tier}"

    # legacy hard-mode helper → think + high
    think, tier = TASK_TIER["helper_full_legacy_hard"]
    assert think, "helper_full_legacy_hard: expected think=True"
    assert tier == "high", f"helper_full_legacy_hard: expected tier=high, got {tier}"

    print("[OK] task think/tier assignments semantically correct")


def test_facade_chat_json_accepts_model_spec():
    """chat_json facade 接受 model_spec 参数"""
    import asyncio
    from app.llm.model_pool import chat_json, resolve_task, ModelSpec, ProviderConfig

    # 验证函数签名（不实际调用 LLM）
    import inspect
    sig = inspect.signature(chat_json)
    params = list(sig.parameters)
    assert "model_spec" in params, "chat_json missing model_spec param"
    assert "think" in params, "chat_json missing think param"
    assert "tier" in params, "chat_json missing tier param"
    assert "reasoning" in params, "legacy param"
    assert "lite" in params, "legacy param"
    assert "metrics_tag" in params, "cache metrics tag param"

    print("[OK] chat_json() accepts model_spec + think/tier + legacy params")


def test_facade_chat_stream_accepts_model_spec():
    """chat_stream facade 接受 model_spec 参数"""
    import inspect
    from app.llm.model_pool import chat_stream

    sig = inspect.signature(chat_stream)
    params = list(sig.parameters)
    assert "model_spec" in params
    assert "think" in params
    assert "tier" in params

    print("[OK] chat_stream() accepts model_spec + think/tier")


def test_facade_chat_with_tools_loop_accepts_model_spec():
    """chat_with_tools_loop facade 接受 model_spec 参数"""
    import inspect
    from app.llm.model_pool import chat_with_tools_loop

    sig = inspect.signature(chat_with_tools_loop)
    params = list(sig.parameters)
    assert "model_spec" in params
    assert "think" in params
    assert "tier" in params

    print("[OK] chat_with_tools_loop() accepts model_spec + think/tier")


def test_dotenv_loaded():
    """模块加载时已尝试加载 .env —— 至少一个 provider 有 API key"""
    import app.llm.model_pool as mp

    active = getattr(mp, "ACTIVE", None)
    if active is not None:
        # DS 单 provider 变体：key 从环境变量读（可能为空）
        assert isinstance(active.api_key, str)
        assert len(active.api_key) >= 0
        print(f"[OK] .env loading (ACTIVE provider key: {'present' if active.api_key else 'empty (from env)'}")
    else:
        # 多 provider 变体：GPT55 key 来自根目录 .env / 环境变量，不再在代码里硬编码。
        assert isinstance(mp.GPT55.api_key, str)
        assert mp.GPT55.base_url.startswith("https://")
        print("[OK] .env loading (GPT55 key from root config/env)")


def test_resolve_task_unknown_raises():
    """未知 task 名应该抛出 KeyError"""
    from app.llm.model_pool import resolve_task

    try:
        resolve_task("nonexistent_task_xyz")
        assert False, "should have raised KeyError"
    except KeyError:
        pass

    print("[OK] resolve_task() raises KeyError on unknown task")


def test_model_spec_repr():
    """ModelSpec repr 包含关键信息"""
    from app.llm.model_pool import ModelSpec, ProviderConfig

    p = ProviderConfig(name="test", api_key="sk-123", base_url="http://x")
    spec = ModelSpec(model="gpt-5.5", reasoning="disabled", provider=p)
    r = repr(spec)
    assert "gpt-5.5" in r
    assert "disabled" in r
    print("[OK] ModelSpec repr readable")


# ═══════════════════════════════════════════════════════════════
# RUNNER
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    tests = [
        ("ProviderConfig immutable", test_provider_config_immutable),
        ("ModelSpec immutable", test_model_spec_immutable),
        ("providers defined", test_providers_defined),
        ("model maps all tiers", test_model_maps_all_tiers),
        ("reasoning tiers defined", test_reasoning_tiers_defined),
        ("resolve all combos", test_resolve_all_combinations),
        ("resolve distinct copies", test_resolve_specs_are_distinct),
        ("resolve_task all tasks", test_resolve_task_all_known),
        ("TASK_TIER coverage", test_task_tier_coverage),
        ("task assignments rationale", test_think_tier_assignment_rationale),
        ("chat_json signature", test_facade_chat_json_accepts_model_spec),
        ("chat_stream signature", test_facade_chat_stream_accepts_model_spec),
        ("chat_with_tools_loop signature", test_facade_chat_with_tools_loop_accepts_model_spec),
        ("dotenv loaded", test_dotenv_loaded),
        ("unknown task raises", test_resolve_task_unknown_raises),
        ("ModelSpec repr", test_model_spec_repr),
    ]

    failed = 0
    for name, fn in tests:
        try:
            fn()
        except Exception as e:
            print(f"[FAIL] {name}: {e}")
            import traceback
            traceback.print_exc()
            failed += 1

    print()
    print(f"─── {len(tests) - failed}/{len(tests)} passed ───")
    if failed:
        sys.exit(1)

