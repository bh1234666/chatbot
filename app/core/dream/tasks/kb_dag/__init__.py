"""
KB DAG 整理任务 (D21-D26).

核心:
- D23 ⭐ 高级抽象节点 (从低级 fact → 提炼 topic, 用 main+max)
- D21 节点合并 (减冗余)
- D22 节点拆分 (单节点 content > 400 字 + 多主题)
- D24 Headline/content 精炼 (老 lite 写的 → main+max 重写)
- D25 Edge 重组
- D26 Salience 衰减 (附带做, 不独立)
"""

# 注册 (会触发 @register_dream_task)
from app.core.dream.tasks.kb_dag import d23_high_level_abstract  # noqa: F401
from app.core.dream.tasks.kb_dag import d21_node_merge  # noqa: F401
from app.core.dream.tasks.kb_dag import d22_node_split  # noqa: F401
from app.core.dream.tasks.kb_dag import d24_refine  # noqa: F401
from app.core.dream.tasks.kb_dag import d25_edges  # noqa: F401
from app.core.dream.tasks.kb_dag import d26_salience_decay  # noqa: F401
from app.core.dream.tasks.kb_dag import d8_kb_ref_cleanup  # noqa: F401
