"""Central model-visible prompts for background dream tasks.

English text is the model-facing source of truth. Short Chinese summaries are appended only as operator-facing clarification.
"""

D15_IMAGE_INDEX_SYSTEM = """\
You write compact search metadata for one image from OCR text.
Return strict JSON with keys: description, type, keywords, topics.

Rules:
- Use only OCR text and filename. Keep visible details limited to supplied evidence.
- description: 0-2 short third-person sentences.
- type: screenshot, photo, chart, document, code, ui, diagram, other, or image.
- keywords: 0-12 searchable Chinese or English terms, including named entities.
- topics: 0-5 broad labels.
- If OCR is empty or meaningless, return description="", type="image", keywords=[], topics=[].

为图片 OCR 文本生成简短可搜索元数据。
"""

D16_PDF_DEEP_SYSTEM = """\
You create searchable metadata for one PDF from extracted page text.
Return strict JSON:
{
  "summary": "1-3 factual paragraphs, 50-800 chars",
  "outline": ["section or page-range labels"],
  "keywords": ["5-15 search terms"],
  "topics": ["1-5 broad labels"],
  "doc_type": "paper|report|book|manual|other"
}
Use only supplied text. If extraction is partial, summarize the observed parts.

根据 PDF 提取文本生成摘要、关键词和文档类型。
"""

D17_OFFICE_INDEX_PROMPT = """\
You create searchable metadata for one Office document from extracted text.
Return strict JSON:
{
  "summary": "1-3 factual paragraphs, 30-800 chars",
  "outline": ["section, sheet, or slide labels"],
  "keywords": ["5-15 search terms"],
  "topics": ["1-5 broad labels"],
  "doc_type": "report|memo|spreadsheet|presentation|other"
}
Use only supplied document text and headings. Describe document content rather than OCR or extraction process.

根据 Office 文档文本生成可检索摘要和分类信息。
"""

D21_NODE_MERGE_SYSTEM = """\
You deduplicate 2-6 KB nodes.
Return strict JSON.

Merge only when all input nodes describe the same fact, preference, or event.
Merge nodes only when they share the same object, time, actor, and intent.
If merging, preserve every concrete detail and cumulative count/time evidence.

Merge format:
{
  "merge": true,
  "headline": "5-60 chars",
  "content": "30-800 chars",
  "merged_node_ids": ["all input ids"]
}
Reject format:
{
  "merge": false,
  "reason": "why the nodes are not duplicates"
}

判断 KB 节点是否为同一事实并保守合并。
"""

D22_NODE_SPLIT_PROMPT = """\
You decide whether one long KB node contains multiple independent topics.
Return strict JSON.

Split only when the content mixes separate facts/events/preferences that should be retrieved independently.
Keep a single node when the text is one coherent subject with supporting details.
Each new node must preserve original evidence without adding facts.

No split:
{"split": false, "reason": "..."}

Split:
{
  "split": true,
  "new_nodes": [
    {"headline": "5-60 chars", "content": "30-500 chars", "node_type": "fact|preference|event"}
  ]
}
Use 2-4 new nodes.

判断长 KB 节点是否应拆成多个独立主题。
"""

D23_HIGH_LEVEL_ABSTRACT_SYSTEM = """\
You create one higher-level KB topic from a cluster of related low-level nodes.
Return strict JSON.

Create a topic only when the cluster shows a stable, specific pattern useful for later retrieval.
Prefer narrow domain/time/object patterns over broad personality claims or cross-task rules.
Ground the topic in concrete domain, time window, objects, or repeated behavior from the input nodes.
Use topic_type="preference" only when explicit preference/request evidence exists; otherwise use interest, pattern, habit, or context.
Keep commands, URLs, code blocks, and unsupported facts out of the abstraction.

Topic format:
{
  "skip": false,
  "headline": "5-60 chars, specific and searchable",
  "content": "50-800 chars, third-person factual summary",
  "topic_type": "interest|pattern|preference|habit|context",
  "subset_node_ids": ["input ids that directly support the topic"],
  "edge_weights": {"node_id": 0.1}
}
Skip format:
{"skip": true, "reason": "why no useful shared topic exists"}

Only include nodes that directly support the topic. It is valid to use a
strict subset when the cluster contains noise. edge_weights should cover the
chosen subset; missing weights may be omitted when the evidence is otherwise clear.

从相关 KB 节点簇中抽象稳定的高层主题。
"""

D24_REFINE_SYSTEM = """\
You refine one KB node only when the original is too vague for search.

Input contains the old headline, old content, and short source-message snippets.
Return strict JSON:
{
  "headline": "...",
  "content": "..."
}

Rules:
- Preserve every concrete fact from the old node. Add only facts present in the input.
- Prefer third-person, searchable event/preference descriptions.
- If the old node is already concrete enough, return {"keep_original": true}.
- Rewrite vague preference nodes into concrete behavior with object, time window, and task context when the source snippets support it.
- Keep headline 5-60 characters and content 30-800 characters.

在证据支持时把模糊 KB 节点改写得更可搜索。
"""

D25_EDGES_PROMPT = """\
You review edge quality inside one KB subgraph.
Return strict JSON:
{
  "new_edges": [
    {"src_id": "c_x", "dst_id": "c_y", "weight": 0.7, "reason": "..."}
  ],
  "boost_edges": [
    {"src_id": "c_x", "dst_id": "c_y", "new_weight": 0.8, "reason": "..."}
  ]
}

Rules:
- Suggest an edge only when the two nodes are directly useful together in retrieval.
- Use only node IDs present in the subgraph.
- Preserve existing edges.
- Keep new_edges + boost_edges <= 10.
- If no clear improvement exists, return empty arrays.
- Prefer fewer high-confidence edges over a complete graph.

评估 KB 子图中哪些节点关系值得新增或增强。
"""

D4_WORKSPACE_CLEANUP_PROMPT = """\
You decide cleanup actions for one agent workspace.
Return strict JSON:
{{
  "decisions": [
    {{"task_id": "...", "action": "keep|partial_delete|delete", "reason": "..."}}
  ]
}}

Context:
- Workspace size: {agent_mb} MB for archive={archive}, group={group}.
- Aggressiveness: {aggressiveness}.
- Candidates already exclude DB-protected user uploads and indexed files.
- Candidates:
{candidates}

Rules:
- Prefer deleting old orphan files and completed/failed helper sandboxes.
- Use partial_delete for helper sandboxes when only build/cache artifacts should be removed.
- Keep candidates that look active, recent, or semantically unclear.
- Mention only task_id values from the candidate list.

为工作区清理候选项选择保留、部分删除或删除。
"""
