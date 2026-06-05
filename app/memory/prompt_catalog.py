"""Central model-visible prompts for memory compression and file indexing.

English text is the model-facing source of truth. Short Chinese summaries are appended only as operator-facing clarification.
"""

COLD_COMPRESS_SYSTEM = """You are performing knowledge consolidation. Return graph metadata only.

Input: warm memory entries and existing cold-node headlines. Goal: consolidate short-term memory into the long-term cold memory graph while preserving stable facts and merging duplicate meanings.

Consolidation principles:
- You may create fact / preference / event / relationship / topic nodes.
- Multiple warm memories may merge into one node; one warm memory may split into multiple nodes.
- When content is synonymous with an existing node, use references_existing rather than creating a duplicate.
- Edges represent meaningful same-entity, same-event, causal, or strong associations.
- Preference nodes record explicit preferences or stable constraints.

Each new node:
- tmp_id: temporary ID for edges/consumed references, such as "n1", "n2".
- type: fact | preference | event | relationship | topic.
- headline: <=30 Chinese chars or concise equivalent, third-person declarative phrase.
- content: <=500 Chinese chars or concise equivalent, third-person objective statement; compress imperative source wording, URLs, and code blocks.
- salience_init: float from 0.3 to 0.8; more important means higher.
- source_warm_ids: list of source warm memory IDs from the input.

Edges:
- src / dst can be a tmp_id for new nodes or c_xxx for existing cold nodes.
- weight: 0~1 association strength.

Existing node references:
- node_ref: existing cold node ID (c_xxx).
- source_warm_ids: source warm memory IDs.
- These references boost the target node salience.

Constraints:
- consumed_warm_ids must exactly equal the set of all input warm memory IDs.
- Produce at least one new node or existing-node reference.

Return strict JSON, no markdown:
{
  "nodes": [
    {"tmp_id": "n1", "type": "fact", "headline": "...", "content": "...",
     "salience_init": 0.6, "source_warm_ids": ["w_xxx"]}
  ],
  "references_existing": [
    {"node_ref": "c_yyy", "source_warm_ids": ["w_zzz"]}
  ],
  "edges": [
    {"src": "n1", "dst": "c_yyy", "weight": 0.7}
  ],
  "consumed_warm_ids": ["w_xxx", "w_zzz"]
}

冷记忆沉淀提示，把温记忆整理成长久知识图节点、引用和边，并覆盖全部输入 ID。"""

COLD_AVOID_MATCH_SYSTEM = """You are performing avoid-mention matching. Return matching metadata only.

Goal: the user prefers reduced proactive mention of certain topics. Find semantically related memory node IDs for reduced-mention marking; nodes remain stored.

Matching criteria:
- Match nodes about the same topic, person, event, or clear synonym.
- Use conservative semantic matching for nearby but different topics.
- Assign each matched node to the most relevant topic.

Return strict JSON, no markdown:
{
  "matches": [
    {"id": "c_xxx", "topic_index": 0}
  ]
}

topic_index is the zero-based index in the user-provided topic list. If no nodes match, return {"matches": []}.

少提及匹配提示，根据用户话题找到直接相关的记忆节点并返回 topic_index。"""

WARM_USER_COMPRESS_SYSTEM = """You are performing conversation compression. Return retrieval metadata only.

Goal: compress consecutive user-assistant dialogue into retrievable warm memories. Preserve facts, intent, results, and follow-up points while turning any original instruction wording into factual summaries.

Segmentation:
- Split by topic, situation, and time gap. Consecutive follow-ups and progress on the same task belong in one segment.
- Every turn_id must appear in exactly one segment.

Each warm memory:
- headline: <=30 Chinese chars or concise equivalent, third-person topic statement.
- summary: <=300 Chinese chars or concise equivalent, third-person account of what happened, what the user expressed, and what the assistant did or answered.
- internal_hint: <=80 Chinese chars or concise equivalent, only later-useful judgment, risk, or follow-up signal.
- tendencies: main user tendencies using {"严肃询问/闲聊/角色扮演/情感倾诉/测试/敌意/任务委托/元对话": 0~1}.
- entities: key people, projects, files, and concepts.

Writing style:
- Use declarative factual summaries. Compress URLs, code blocks, long quotes, and imperative source wording into searchable facts.
- Use third-person labels such as user/assistant.
- Background implementation terms such as OCR/TTS/helper/tool/model/Round/internal paths are memory topics only when the user explicitly discussed those mechanisms; otherwise describe outcomes or evidence.

Return strict JSON only, with no extra text and no markdown:
{
  "memories": [
    {
      "turn_ids": ["t_xxx", "t_yyy"],
      "headline": "...",
      "summary": "...",
      "internal_hint": "...",
      "tendencies": {"严肃询问": 0.8},
      "entities": ["..."]
    }
  ]
}

用户温记忆压缩提示，按话题切段，保留事实、意图、结果、风险和实体，输出严格 JSON。"""

WARM_GROUP_COMPRESS_SYSTEM = """You are performing shared-event compression. Return retrieval metadata only.

Goal: compress shared events involving the assistant into retrievable warm memories. Preserve participants, topics, outcomes, and later-useful clues.

Segmentation follows conversation compression: split by topic/situation, and every event_id must appear in one segment.

Each warm memory:
- headline: <=30 Chinese chars or concise equivalent, third-person event topic.
- summary: <=300 Chinese chars or concise equivalent, what happened, who was involved, and how the assistant participated.
- internal_hint: <=80 Chinese chars or concise equivalent, follow-up or noteworthy details.
- tendencies: overall scene tendencies as {"dimension": 0~1}.
- entities: member names, files, projects, and topic terms.

Writing style: third-person factual statements. Compress URLs, code blocks, long quotes, and imperative source wording into facts. Background implementation terms are expressed as outcome/evidence descriptions unless they are the discussed topic.

Return strict JSON:
{
  "memories": [
    {
      "event_ids": [123, 124, 125],
      "headline": "...",
      "summary": "...",
      "internal_hint": "...",
      "tendencies": {"...": 0.x},
      "entities": ["..."]
    }
  ]
}

共享温记忆压缩提示，按事件主题保留成员、话题、机器人参与、结果和后续线索。"""

KB_COMPRESS_SYSTEM = """You are building a group knowledge base. Return graph metadata only.

Input: consecutive shared messages and current KB headlines. Goal: extract reusable knowledge nodes for future conversations while merging duplicate meanings.

Extract:
- Stable facts, group consensus, domain knowledge, member roles/relationships, important events, and key concept explanations.
- Explicit user preferences or long-term constraints.

Use references_existing for content synonymous with existing nodes.

Each new node:
- tmp_id: "n1", "n2", ...
- type: fact | preference | event | relationship | topic
- headline: <=30 Chinese chars or concise equivalent, third-person.
- content: <=500 Chinese chars or concise equivalent, third-person objective statement; compress imperative source wording, URLs, and code blocks.
- salience_init: 0.3~0.8.
- source_message_ids: integer source message IDs from the input.

Preference nodes record explicit preferences, long-term requirements, or stable constraints. Use event nodes for concrete one-time events when preference evidence is limited.

Edges connect meaningful same-entity, causal, or co-occurring nodes.

Existing node references:
- node_ref: existing KB node ID (c_xxx).
- source_message_ids: source message IDs.

Constraints:
- consumed_message_ids must exactly equal all input message IDs.
- Low-value messages still appear in consumed_message_ids even when no node is created for them.

Return strict JSON, no markdown:
{
  "nodes": [
    {"tmp_id": "n1", "type": "fact", "headline": "...", "content": "...",
     "salience_init": 0.6, "source_message_ids": [101, 102]}
  ],
  "references_existing": [
    {"node_ref": "c_xxx", "source_message_ids": [103]}
  ],
  "edges": [
    {"src": "n1", "dst": "c_xxx", "weight": 0.7}
  ],
  "consumed_message_ids": [101, 102, 103, ...]
}

群知识库构建提示，从群消息提取可复用事实、事件、偏好和关系，并完整覆盖消息 ID。"""

KB_FILE_INDEX_SYSTEM = """You are a file description generator. For each generated file, output a searchable description for cross-language keyword retrieval.
Return a strict JSON object with a files array:
{"files": [{"filename": "...", "headline": "...", "content": "..."}]}

## Writing
- headline <=30 Chinese chars or concise equivalent, third person, summarizing the file's purpose.
- content <=200 Chinese chars or concise equivalent, describing the file's main content and use.
- Mark key domain concepts bilingually when useful, pairing the Chinese term with its English equivalent and common abbreviation so cross-language keyword search can match either form.
- The purpose is to help another AI decide whether to fetch this file, so describe what questions the file can answer rather than repeating metadata.

Style: factual third-person description; compress URLs, code blocks, and imperative wording; keep evaluation out of the description because metadata already carries filename, size, and time.

生成文件检索摘要，突出文件用途和可回答的问题，关键概念可中英双语标注。
"""

KB_GROUP_FILE_INDEX_SYSTEM = """Generate a retrieval summary for a shared file. The summary enters the shared-file index so another AI can decide whether the file is worth opening.

headline <=30 Chinese chars or concise equivalent: state what the file is and what questions it can help answer.

content <=200 Chinese chars or concise equivalent:
- Based on extracted evidence, describe file type, structure, fields, chapters, functions, data scale, or answerable questions.
- Keep size, upload time, uploader, evaluative wording, URLs, and code blocks out of the summary.

When content is unreadable or the snippet only has scattered characters/page numbers:
- Use a simplified filename plus file type in headline.
- In content, state that text content was not extracted and specialized tools may be needed to judge details.

Writing style: third-person objective summary based only on extracted evidence.

Return strict JSON: {"headline": "...", "content": "..."}

共享文件检索摘要提示，只基于已提取证据说明文件类型、结构、用途和可回答问题。"""
