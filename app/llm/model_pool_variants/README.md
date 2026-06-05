# Model Pool 变体 — 切换指南

三个模型包，`model_pool.py` 是唯一对外接口。业务代码不改，只换这一个文件。

## 当前激活

**纯 DeepSeek** (`model_pool.py`) — 全链路 DeepSeek，think 用 v4-pro + reasoning_effort 分级。

## 三个变体

| 文件 | 说明 | think high | nonthink low |
|------|------|------------|--------------|
| `model_pool.py` (当前) | 纯 DeepSeek | deepseek-v4-pro | deepseek-v4-flash |
| `model_pool_variants/model_pool_all_gpt.py` | 全 GPT-5.5 | gpt-5.5 | gpt-5.5 |
| `model_pool_variants/model_pool_mixed.py` | DeepSeek + GPT-5.5 混合 | gpt-5.5 | deepseek-v4-flash |

## 变体详细对比

### model_pool.py — 纯 DeepSeek（当前）

```
           low                mid                high
THINK:     deepseek-v4-pro    deepseek-v4-pro    deepseek-v4-pro
           (reasoning=low)    (reasoning=high)   (reasoning=max)
NONTHINK:  deepseek-v4-flash  deepseek-v4-pro    deepseek-v4-pro
```

### model_pool_all_gpt.py — 全 GPT-5.5

```
           low       mid       high
THINK:     gpt-5.5   gpt-5.5   gpt-5.5    (全部 reasoning=disabled)
NONTHINK:  gpt-5.5   gpt-5.5   gpt-5.5
```

### model_pool_mixed.py — 混合

```
           low                mid                high
THINK:     deepseek-v4-pro    deepseek-v4-pro    gpt-5.5
           (reasoning=low)    (reasoning=high)   (reasoning=disabled)
NONTHINK:  deepseek-v4-flash  deepseek-v4-pro    deepseek-v4-pro
```

只有 `round2_veryhard` 和 `helper_full_final` 两个最高难度任务走 GPT-5.5，其余全部 DeepSeek。

## 切换方法

```powershell
cd f:\chatbot\app\llm

# 切换到全 GPT-5.5
copy model_pool_variants\model_pool_all_gpt.py model_pool.py

# 切换到混合
copy model_pool_variants\model_pool_mixed.py model_pool.py

# 切回纯 DeepSeek
copy model_pool_variants\model_pool_deepseek.py model_pool.py
```

切换后无需改任何其他文件 — `client.py` / `orchestrator.py` / `delegate.py` 都通过 `model_pool.py` 的 facade 调用，接口不变。

## 添加新变体

1. 复制当前 `model_pool.py` 到 `model_pool_variants/`
2. 修改 provider 配置和 model maps
3. 更新本 README
4. 用 `python tests\test_model_pool.py` 验证
