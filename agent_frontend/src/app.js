const STORAGE_KEY = "bot-agent-state-v2";
const DB_NAME = "bot-agent-frontend";
const DB_STORE = "kv";
const DB_VERSION = 1;
const CLEANUP_MARKER_URL = ".cleanup_state.json";
const CLEANUP_MARKER_LOCAL_KEY = "bot-agent-cleanup-id";
const LARGE_FILE_WARN_BYTES = 50 * 1024 * 1024;
const LONG_MESSAGE_FILE_THRESHOLD = 7000;
const DEFAULT_MAX_WORKFLOW_RUNS = 80;

const PROMPT_TEMPLATES = [
  {
    id: "project_scan",
    group: "工程",
    title: "梳理工程",
    text: "请先快速梳理当前项目结构、关键入口、运行方式和潜在风险，给出简明结论和下一步建议。",
  },
  {
    id: "project_tests",
    group: "工程",
    title: "运行测试",
    text: "请检查当前项目可用的测试/构建命令，选择合适的最小验证集运行，并根据结果修复显著问题。",
  },
  {
    id: "project_diff",
    group: "工程",
    title: "检查改动",
    text: "请检查当前项目最近改动，重点找行为回归、遗漏测试、死代码和可以低风险优化的点。",
  },
  {
    id: "continue_task",
    group: "工程",
    title: "继续任务",
    text: "请基于当前对话和项目状态继续推进未完成任务，先确认当前进度，再完成下一步并验证。",
  },
  {
    id: "file_summary",
    group: "文件",
    title: "分析附件",
    text: "请分析本轮附加的 bot 文件区文件，提取关键信息、异常点和可执行建议。",
  },
  {
    id: "report",
    group: "文件",
    title: "生成报告",
    text: "请基于当前材料生成一份结构清晰的报告，区分事实、推断、风险和建议。",
  },
  {
    id: "chat_summary",
    group: "对话",
    title: "总结上下文",
    text: "请总结当前对话中已经确定的结论、待办事项、阻塞点和下一步优先级。",
  },
  {
    id: "ask_precise",
    group: "对话",
    title: "精确回答",
    text: "请直接回答上一个问题，优先给结论，再列必要依据；不展开无关背景。",
  },
];

const defaultState = {
  settings: {
    backendUrl: "http://127.0.0.1:8000",
    monitorEnabled: true,
    insertMode: "inject_only",
    maxWorkflowRuns: DEFAULT_MAX_WORKFLOW_RUNS,
    reduceConfirmations: false,
    logTailLines: 200,
    theme: "light",
    fontSize: 14,
    codeFont: "Consolas, Courier New, monospace",
    autoRunQueue: true,
    autoContinue: false,
    autoContinueMaxSec: 900,
    defaultSendMode: "normal",
    workflowOpen: true,
    sidebarOpen: true,
  },
  accounts: [],
  activeAccountId: "",
  archives: [],
  activeArchiveId: "",
  conversations: {},
  files: {},
  artifacts: {},
  ui: {
    filePanelOpen: false,
    artifactPanelOpen: false,
    fileSearch: "",
    settingsOpen: false,
    previewOpen: false,
    previewFileId: "",
    previewText: "",
    previewStatus: "idle",
    projectPanelOpen: false,
    projectTree: [],
    projectTreePath: ".",
    projectTreeStatus: "idle",
    projectPreviewPath: "",
    projectPreviewText: "",
    projectPreviewStatus: "idle",
    projectSearch: "",
    projectSearchResults: [],
    projectCommand: "",
    projectCommandOutput: "",
    projectDiffPath: "",
    projectDiffComparePath: "",
    projectDiffText: "",
    workflowFilter: "all",
    workflowSearch: "",
    workflowViewMode: "detailed",
    transcriptSearch: "",
    workflowOpenNodeIds: [],
    selectedNodeId: "",
    archiveSearch: "",
    notifications: [],
  },
};

let state = loadState();
let activeController = null;
let monitorController = null;
let startupRecovered = false;
let attachedFileIds = [];
let currentDraft = "";
let draftHistoryIndex = -1;
let stateChannel = null;
let idbReady = false;
let uploadProgress = {};
let workflowScrollRestoreSeq = 0;
let workflowRenderTimer = null;
let workflowRenderOptions = null;
let workflowLastScrollSnapshot = null;
let workflowPointerActive = false;
let transcriptLastScrollSnapshot = null;
let tooltipEl = null;
let tooltipTarget = null;
const sendingArchiveIds = new Set();

const $ = (selector) => document.querySelector(selector);

function cssEscape(value) {
  if (globalThis.CSS?.escape) return CSS.escape(String(value));
  return String(value).replace(/["\\]/g, "\\$&");
}

function uid(prefix = "id") {
  if (globalThis.crypto?.randomUUID) return `${prefix}_${crypto.randomUUID()}`;
  return `${prefix}_${Date.now()}_${Math.random().toString(16).slice(2)}`;
}

function nowIso() {
  return new Date().toISOString();
}

function fmtTime(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString();
}

function fmtSize(size) {
  const n = Number(size || 0);
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

function downloadBlob(filename, content, type = "text/plain;charset=utf-8") {
  const blob = content instanceof Blob ? content : new Blob([content], { type });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

function setDraft(text, append = false) {
  currentDraft = append && currentDraft ? `${currentDraft.trimEnd()}\n\n${text}` : text;
  syncDraftToConversation();
  saveState();
  render();
  requestAnimationFrame(() => {
    const input = $("#draftInput");
    if (input) {
      input.focus();
      input.selectionStart = input.selectionEnd = input.value.length;
    }
  });
}

function addDraftText(text) {
  setDraft(text, false);
}

function syncDraftToConversation() {
  const conv = activeConversation();
  if (!conv) return;
  conv.draft = currentDraft;
  conv.draftAttachments = attachedFileIds.slice();
}

function loadDraftFromConversation() {
  const conv = activeConversation();
  currentDraft = conv?.draft || "";
  attachedFileIds = Array.isArray(conv?.draftAttachments) ? conv.draftAttachments.slice() : [];
  draftHistoryIndex = -1;
}

function pushInputHistory(text) {
  const conv = activeConversation();
  const trimmed = (text || "").trim();
  if (!conv || !trimmed) return;
  conv.inputHistory = (conv.inputHistory || []).filter((item) => item !== trimmed);
  conv.inputHistory.push(trimmed);
  conv.inputHistory = conv.inputHistory.slice(-100);
}

function notify(title, detail = "", kind = "info", nodeId = "") {
  state.ui.notifications = [
    {
      id: uid("notice"),
      title,
      detail,
      kind,
      nodeId,
      createdAt: nowIso(),
    },
    ...(state.ui.notifications || []),
  ].slice(0, 8);
  saveState();
  renderNotificationsOnly();
}

function dismissNotification(id) {
  state.ui.notifications = (state.ui.notifications || []).filter((item) => item.id !== id);
  saveState();
  renderNotificationsOnly();
}

function queueTemplateText(text) {
  const conv = activeConversation();
  if (!conv || !text.trim()) return;
  conv.queue.push({ id: uid("queue"), text: text.trim(), attachments: attachedFileIds.slice(), createdAt: nowIso(), fromTemplate: true });
  saveState();
  render();
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

const BUTTON_TOOLTIPS = {
  newAccountBtn: "新建一个本地账号。账号用于隔离存档、文件区、对话状态和项目目录配置。",
  renameAccountBtn: "修改当前本地账号显示名。只影响前端显示，不改变后端用户标识。",
  deleteAccountBtn: "删除当前账号在本前端保存的本地状态。会移除该账号下的本地存档和对话记录。",
  newArchiveBtn: "为当前账号新建一个存档。每个存档可以绑定一个项目目录，也可以留空作为普通 bot 对话。",
  duplicateTabBtn: "复制当前存档为新的本地对话窗口，用于并行维护同一项目的不同任务线。",
  toggleSidebarBtn: "显示或隐藏左侧栏。左侧栏包含账号、存档列表、对话搜索和本地状态。",
  toggleWorkflowBtn: "显示或隐藏右侧工作流面板。工作流面板展示主进程、helper、工具调用和命令进度。",
  recoverBtn: "从后端恢复当前账号/存档的项目映射、活跃任务和最近监控事件。",
  syncArchiveBtn: "把当前存档与后端 archive/group/project 映射同步，确保前端和后端指向同一会话。",
  renameArchiveBtn: "修改当前存档标题。只改变前端显示名称，不影响项目目录。",
  editDirBtn: "设置或清空当前存档绑定的项目目录。目录为空时作为普通 bot 对话使用。",
  pinArchiveBtn: "固定或取消固定当前存档。固定存档会在左侧存档列表中靠前显示。",
  archiveArchiveBtn: "归档或取消归档当前存档。归档后默认隐藏，可在存档搜索中输入 archived 查看。",
  projectPanelBtn: "打开项目目录面板。可查看目录树、搜索项目文件、预览文件、运行命令和查看 diff。",
  filePanelBtn: "打开 bot 文件区。这里管理上传给 bot 的输入文件，可附加到消息、预览、下载或删除。",
  artifactPanelBtn: "打开 bot 产物区。这里显示 bot 生成并可下载的报告、图片、音频、压缩包等产物。",
  settingsBtn: "打开前端设置。可配置后端地址、监控流、队列、自动继续、字号和本地数据导入导出。",
  abortBtn: "请求中断当前正在运行的任务。会保留现有对话、队列和已产生的工作流记录。",
  abortClearBtn: "请求中断当前任务并清空当前窗口本地队列。适合要立即停止后续自动任务时使用。",
  uploadBtn: "选择文件上传到 bot 文件区。上传后可在本次消息发送前附加或撤回。",
  clearDraftBtn: "清空当前输入框内容，并撤回本次消息尚未发送的附件。",
  queueBtn: "把当前输入加入当前存档的本地队列。队列自动开启时会在当前任务结束后继续发送。",
  insertBtn: "在当前任务运行中插入一条用户输入，进入中途介入流程；任务结束后按设置决定是否继续下一轮。",
  paletteBtn: "打开命令面板，快速填入常用工程、文件或对话任务模板。快捷键 Ctrl+K。",
  toggleQueueAutoBtn: "切换当前对话队列的自动执行状态。自动时任务结束后继续发送下一条队列消息。",
  sendBtn: "发送当前输入和已附加文件。Enter 也会发送，Shift+Enter 换行。",
  exportWorkflowJsonBtn: "导出当前工作流原始 JSON，便于调试、复盘或提交问题。",
  exportWorkflowMdBtn: "导出当前工作流 Markdown 摘要，便于阅读和分享。",
  refreshRunBtn: "按当前 trace 从后端刷新最近监控历史，补回断线或页面刷新期间的工作流事件。",
  refreshFilesBtn: "刷新 bot 文件区列表和上传状态。",
  closeFilePanelBtn: "关闭 bot 文件区面板。",
  refreshArtifactsBtn: "刷新 bot 产物区列表，只显示 bot 推送或生成的可交付文件。",
  closeArtifactPanelBtn: "关闭 bot 产物区面板。",
  refreshProjectTreeBtn: "刷新当前项目目录树。使用上方相对目录作为浏览起点。",
  closeProjectPanelBtn: "关闭项目目录面板。",
  projectSearchBtn: "在绑定项目目录中搜索文件内容或路径，结果可点击预览。",
  projectRunBtn: "在绑定项目目录中运行命令。命令会经过后端风险策略处理。",
  projectDiffBtn: "读取指定项目文件的 diff；可选填写对比文件。",
  projectDiffExplainBtn: "把当前 diff 内容填入输入框，请 bot 解释改动、风险和验证建议。",
  closeSettingsBtn: "关闭设置面板。",
  saveSettingsBtn: "保存当前前端设置，并立即应用后端地址、监控、队列、字号等配置。",
  clearTranscriptBtn: "清空当前存档的本地 transcript 显示记录。不会删除后端记忆或文件。",
  exportBtn: "把当前 transcript 导出为 Markdown 文件。",
  exportStateBtn: "导出整个前端本地状态 JSON，便于备份或迁移。",
  importStateBtn: "从 JSON 导入前端本地状态，会覆盖当前本地状态。",
  cleanupBtn: "清理前端本地缓存和过期状态，保留必要配置。",
  deleteArchiveBtn: "删除当前本地存档及其前端状态。不会直接删除真实项目目录。",
  closePreviewBtn: "关闭文件预览面板。",
  closeNodeDetailsBtn: "关闭工作流节点详情面板。",
};

const BUTTON_TEXT_TOOLTIPS = {
  "新建": "新建当前区域对应的对象，例如账号或存档。",
  "改名": "修改当前选中对象的显示名称。",
  "删除": "删除当前条目。删除前会按场景进行确认。",
  "关闭": "关闭当前面板、通知或详情视图。",
  "刷新": "重新从后端或本地状态读取最新内容。",
  "预览": "在右侧预览面板打开该文件；支持文本、图片、PDF、音频和视频等常见类型。",
  "下载": "在浏览器中打开或下载该文件。",
  "复制": "复制当前代码块内容到剪贴板。",
  "复制 URL": "复制该文件的后端下载地址到剪贴板。",
  "复制路径": "复制该文件在工作区或项目中的路径。",
  "附加": "把该文件附加到下一条将发送给 bot 的消息。",
  "总结": "把该文件加入输入框，请 bot 总结内容和要点。",
  "提取": "把该文件加入输入框，请 bot 提取结构化内容或关键文字。",
  "报告": "把该文件加入输入框，请 bot 基于文件生成报告。",
  "搜索": "执行当前输入的搜索。",
  "运行": "执行当前输入的命令。",
  "Diff": "查看当前文件路径对应的差异内容。",
  "解释 diff": "把当前 diff 交给 bot 分析。",
  "详细": "切换到详细工作流视图，按主进程和 helper 分层展示事件。",
  "简洁": "切换到简洁工作流视图，每行显示最新动作和状态。",
  "展开": "展开当前工作流节点，查看子步骤、进度或流式内容。",
  "收起": "收起当前工作流节点，减少工作流面板占用空间。",
  "发送": "按当前发送模式提交输入。",
  "队列自动": "当前队列会在任务结束后自动继续发送下一条。",
  "队列手动": "当前队列不会自动继续，需要手动发送或切回自动。",
  "清空": "清空当前列表或队列内容。",
  "上移": "把该队列项向前移动，优先执行。",
  "下移": "把该队列项向后移动，降低执行优先级。",
  "编辑": "编辑该队列项内容。",
};

function buttonTooltip(el) {
  if (!el) return "";
  if (el.id && BUTTON_TOOLTIPS[el.id]) return BUTTON_TOOLTIPS[el.id];
  const text = (el.textContent || "").replace(/\s+/g, " ").trim();
  if (el.dataset.templateId) {
    const item = PROMPT_TEMPLATES.find((tpl) => tpl.id === el.dataset.templateId);
    return item ? `左键填入输入框；右键加入队列。\n\n模板内容：${item.text}` : "快捷任务模板。左键填入输入框，右键加入队列。";
  }
  if (el.dataset.dismissNotice) return "关闭这条前端通知。";
  if (el.dataset.detachFile) return "从本次待发送消息中撤回这个附件；不会删除文件区中的原文件。";
  if (el.dataset.clearQueue) return "清空当前对话的本地消息队列。";
  if (el.dataset.moveQueue) return el.dataset.dir === "-1" ? "把这条队列消息上移一位。" : "把这条队列消息下移一位。";
  if (el.dataset.editQueue) return "编辑这条队列消息的文本。";
  if (el.dataset.removeQueue) return "从当前队列中删除这条尚未发送的消息。";
  if (el.dataset.workflowView) return el.dataset.workflowView === "compact"
    ? "切换到简洁工作流视图，只按时间显示一行一条的最新动作。"
    : "切换到详细工作流视图，按主进程和 helper 分层展示事件。";
  if (el.dataset.toggleNode) return text === "收起"
    ? "收起当前工作流节点的详情和子步骤。"
    : "展开当前工作流节点的详情、子步骤和流式内容。";
  if (el.dataset.copyCode !== undefined) return "复制这个代码块的全部内容到剪贴板。";
  if (el.dataset.attachFile) return "把该文件加入当前输入的附件列表，随下一条消息发送给 bot。";
  if (el.dataset.previewFile) return "在预览面板打开该文件。";
  if (el.dataset.downloadFile) return "打开该文件的下载地址。";
  if (el.dataset.copyFilePath) return "复制该文件在 bot 文件区中的工作区路径。";
  if (el.dataset.copyFileUrl) return "复制该文件的下载 URL。";
  if (el.dataset.fileAction === "summary") return "把这个文件生成“总结文件内容”的请求填入输入框。";
  if (el.dataset.fileAction === "extract") return "把这个文件生成“提取关键内容”的请求填入输入框。";
  if (el.dataset.fileAction === "report") return "把这个文件生成“写报告”的请求填入输入框。";
  if (el.dataset.deleteFile) return "从 bot 文件区删除这个文件记录和可管理文件。";
  if (el.dataset.previewArtifact) return "在预览面板打开这个 bot 生成产物。";
  if (el.dataset.downloadArtifact) return "打开这个 bot 生成产物的下载地址。";
  if (el.dataset.copyArtifactUrl) return "复制这个产物的下载 URL。";
  if (el.dataset.copyArtifactPath) return "复制这个产物在工作区中的路径。";
  if (BUTTON_TEXT_TOOLTIPS[text]) return BUTTON_TEXT_TOOLTIPS[text];
  return text ? `${text}：执行当前按钮对应操作。` : "执行当前按钮对应操作。";
}

function enhanceButtonTooltips(root = document) {
  const scope = root?.querySelectorAll ? root : document;
  scope.querySelectorAll("button").forEach((button) => {
    const tip = buttonTooltip(button);
    if (!tip) return;
    button.setAttribute("title", tip);
    button.dataset.tooltip = tip;
    if (!button.getAttribute("aria-label")) button.setAttribute("aria-label", tip.split("\n")[0]);
  });
}

function ensureTooltipEl() {
  if (tooltipEl) return tooltipEl;
  tooltipEl = document.createElement("div");
  tooltipEl.className = "button-tooltip";
  tooltipEl.setAttribute("role", "tooltip");
  document.body.appendChild(tooltipEl);
  return tooltipEl;
}

function positionTooltip(target) {
  if (!tooltipEl || !target) return;
  const rect = target.getBoundingClientRect();
  tooltipEl.style.maxWidth = `${Math.min(420, Math.max(240, window.innerWidth - 32))}px`;
  const tipRect = tooltipEl.getBoundingClientRect();
  let left = rect.left + rect.width / 2 - tipRect.width / 2;
  left = Math.min(Math.max(12, left), window.innerWidth - tipRect.width - 12);
  let top = rect.bottom + 9;
  if (top + tipRect.height > window.innerHeight - 12) top = rect.top - tipRect.height - 9;
  tooltipEl.style.left = `${Math.max(12, left)}px`;
  tooltipEl.style.top = `${Math.max(12, top)}px`;
}

function showButtonTooltip(target) {
  const tip = target?.dataset?.tooltip;
  if (!tip) return;
  tooltipTarget = target;
  const el = ensureTooltipEl();
  el.textContent = tip;
  el.classList.add("open");
  positionTooltip(target);
}

function hideButtonTooltip(target = null) {
  if (target && tooltipTarget && target !== tooltipTarget) return;
  tooltipTarget = null;
  tooltipEl?.classList.remove("open");
}

function loadState() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY) || localStorage.getItem("bot-agent-state-v1");
    if (!raw) return seedState();
    return normalizeState({ ...defaultState, ...JSON.parse(raw) });
  } catch {
    return seedState();
  }
}

async function clearIndexedDbState() {
  if (!("indexedDB" in window)) return;
  try {
    await new Promise((resolve) => {
      const req = indexedDB.deleteDatabase(DB_NAME);
      req.onsuccess = () => resolve();
      req.onerror = () => resolve();
      req.onblocked = () => resolve();
    });
  } catch {
    // Browser storage cleanup is best-effort.
  }
}

async function applyMaintenanceCleanupMarker() {
  try {
    const resp = await fetch(`${CLEANUP_MARKER_URL}?t=${Date.now()}`, { cache: "no-store" });
    if (!resp.ok) return false;
    const marker = await resp.json();
    const cleanupId = String(marker?.cleanup_id || "");
    if (!cleanupId) return false;
    if (localStorage.getItem(CLEANUP_MARKER_LOCAL_KEY) === cleanupId) return false;

    localStorage.removeItem(STORAGE_KEY);
    localStorage.removeItem("bot-agent-state-v1");
    localStorage.setItem(CLEANUP_MARKER_LOCAL_KEY, cleanupId);
    await clearIndexedDbState();
    state = seedState();
    attachedFileIds = [];
    currentDraft = "";
    idbReady = false;
    startupRecovered = false;
    saveState();
    return true;
  } catch {
    return false;
  }
}

function openDb() {
  return new Promise((resolve, reject) => {
    if (!("indexedDB" in window)) {
      reject(new Error("IndexedDB is not available"));
      return;
    }
    const req = indexedDB.open(DB_NAME, DB_VERSION);
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains(DB_STORE)) db.createObjectStore(DB_STORE);
    };
    req.onerror = () => reject(req.error || new Error("open IndexedDB failed"));
    req.onsuccess = () => resolve(req.result);
  });
}

async function idbGet(key) {
  const db = await openDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(DB_STORE, "readonly");
    const req = tx.objectStore(DB_STORE).get(key);
    req.onerror = () => reject(req.error || new Error("IndexedDB get failed"));
    req.onsuccess = () => resolve(req.result);
    tx.oncomplete = () => db.close();
  });
}

async function idbSet(key, value) {
  const db = await openDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(DB_STORE, "readwrite");
    tx.objectStore(DB_STORE).put(value, key);
    tx.onerror = () => reject(tx.error || new Error("IndexedDB set failed"));
    tx.oncomplete = () => {
      db.close();
      resolve();
    };
  });
}

async function hydrateFromIndexedDb() {
  try {
    const saved = await idbGet(STORAGE_KEY);
    if (saved) {
      state = normalizeState({ ...defaultState, ...saved });
      idbReady = true;
      render();
    }
  } catch {
    idbReady = false;
  }
}

function seedState() {
  const account = {
    userId: "local-user",
    displayName: "本地用户",
    createdAt: nowIso(),
    lastUsedAt: nowIso(),
  };
  const archive = makeLocalArchive(account.userId, "默认对话", "");
  return normalizeState({
    ...defaultState,
    accounts: [account],
    activeAccountId: account.userId,
    archives: [archive],
    activeArchiveId: archive.id,
    conversations: { [archive.id]: emptyConversation(archive.id) },
    files: { [archive.id]: [] },
  });
}

function normalizeState(input) {
  const s = structuredClone(input);
  s.settings = { ...defaultState.settings, ...(s.settings || {}) };
  s.ui = { ...defaultState.ui, ...(s.ui || {}) };
  s.settings.maxWorkflowRuns = Math.max(1, Number(s.settings.maxWorkflowRuns || DEFAULT_MAX_WORKFLOW_RUNS));
  s.settings.autoContinueMaxSec = Math.max(1, Math.min(86400, Number(s.settings.autoContinueMaxSec || 900)));
  s.accounts = Array.isArray(s.accounts) ? s.accounts : [];
  s.archives = Array.isArray(s.archives) ? s.archives : [];
  s.conversations = s.conversations || {};
  s.files = s.files || {};
  s.artifacts = s.artifacts || {};
  if (!s.accounts.length) return seedState();
  if (!s.activeAccountId || !s.accounts.some((a) => a.userId === s.activeAccountId)) {
    s.activeAccountId = s.accounts[0].userId;
  }
  const accountArchives = s.archives.filter((a) => a.userId === s.activeAccountId);
  if (!s.activeArchiveId || !accountArchives.some((a) => a.id === s.activeArchiveId)) {
    s.activeArchiveId = accountArchives[0]?.id || "";
  }
  for (const archive of s.archives) {
    archive.archiveId = archive.archiveId || "";
    archive.windowId = archive.windowId || archive.id;
    if (archive.archiveId && archive.id === archive.archiveId) {
      const nextId = uid("archive_window");
      if (s.conversations[archive.id] && !s.conversations[nextId]) {
        s.conversations[nextId] = s.conversations[archive.id];
        s.conversations[nextId].archiveId = nextId;
        delete s.conversations[archive.id];
      }
      if (s.files[archive.id] && !s.files[nextId]) {
        s.files[nextId] = s.files[archive.id];
        delete s.files[archive.id];
      }
      if (s.artifacts[archive.id] && !s.artifacts[nextId]) {
        s.artifacts[nextId] = s.artifacts[archive.id];
        delete s.artifacts[archive.id];
      }
      archive.id = nextId;
      archive.windowId = nextId;
    }
    archive.groupId = archive.groupId || `env_user_${archive.userId || s.activeAccountId}`;
    archive.projectId = archive.projectId || archive.projectKey || uid("project");
    archive.currentDir = archive.currentDir || "";
    if (!s.conversations[archive.id]) s.conversations[archive.id] = emptyConversation(archive.id);
    if (!s.files[archive.id]) s.files[archive.id] = [];
    if (!s.artifacts[archive.id]) s.artifacts[archive.id] = [];
    s.conversations[archive.id].workflowRuns = Array.isArray(s.conversations[archive.id].workflowRuns)
      ? s.conversations[archive.id].workflowRuns.slice(0, s.settings.maxWorkflowRuns)
      : [];
    s.conversations[archive.id].messages = Array.isArray(s.conversations[archive.id].messages)
      ? s.conversations[archive.id].messages
      : [];
    normalizeConversationMessages(s.conversations[archive.id]);
    s.conversations[archive.id].queue = Array.isArray(s.conversations[archive.id].queue)
      ? s.conversations[archive.id].queue
      : [];
    s.conversations[archive.id].draft = s.conversations[archive.id].draft || "";
    s.conversations[archive.id].draftAttachments = Array.isArray(s.conversations[archive.id].draftAttachments)
      ? s.conversations[archive.id].draftAttachments
      : [];
    s.conversations[archive.id].inputHistory = Array.isArray(s.conversations[archive.id].inputHistory)
      ? s.conversations[archive.id].inputHistory.slice(-100)
      : [];
    s.conversations[archive.id].autoRunQueue = s.conversations[archive.id].autoRunQueue ?? s.settings.autoRunQueue;
    s.conversations[archive.id].autoContinueStartedAt = s.conversations[archive.id].autoContinueStartedAt || "";
    s.conversations[archive.id].pendingInsertedMessages = Array.isArray(s.conversations[archive.id].pendingInsertedMessages)
      ? s.conversations[archive.id].pendingInsertedMessages.slice(-5)
      : [];
  }
  return s;
}

function normalizeConversationMessages(conv) {
  if (!conv || !Array.isArray(conv.messages)) return;
  const next = [];
  for (const msg of conv.messages) {
    if (!msg || typeof msg !== "object") continue;
    const text = String(msg.text || "");
    const hasAttachments = Array.isArray(msg.attachments) && msg.attachments.length > 0;
    const hasPreview = Array.isArray(msg.round2PreviewItems) && msg.round2PreviewItems.length > 0;
    const hasAction = Boolean(msg.currentActionText);
    if (msg.role === "assistant" && !text.trim() && !hasAttachments && !hasPreview && !hasAction) {
      if (msg.status !== "streaming") continue;
    }
    next.push(msg);
  }
  conv.messages = next;
}

function saveState() {
  trimWorkflowRuns();
  localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
  if ("indexedDB" in window) {
    idbSet(STORAGE_KEY, state)
      .then(() => { idbReady = true; })
      .catch(() => { idbReady = false; });
  }
  broadcastState();
}

function emptyConversation(archiveId) {
  return {
    id: uid("conv"),
    archiveId,
    status: "idle",
    messages: [],
    workflowRuns: [],
    queue: [],
    draft: "",
    draftAttachments: [],
    inputHistory: [],
    autoRunQueue: true,
    autoContinueStartedAt: "",
    pendingInsertedMessages: [],
  };
}

function trimWorkflowRuns() {
  const limit = Math.max(1, Number(state.settings?.maxWorkflowRuns || DEFAULT_MAX_WORKFLOW_RUNS));
  for (const conv of Object.values(state.conversations || {})) {
    if (Array.isArray(conv.workflowRuns) && conv.workflowRuns.length > limit) {
      conv.workflowRuns = conv.workflowRuns.slice(0, limit);
    }
  }
}

function makeLocalArchive(userId, title, currentDir) {
  return {
    id: uid("local_archive"),
    archiveId: "",
    userId,
    title: title || "新存档",
    groupId: `env_user_${userId}`,
    currentDir: currentDir || "",
    projectId: uid("project"),
    createdAt: nowIso(),
    lastUsedAt: nowIso(),
    localOnly: true,
  };
}

function activeAccount() {
  return state.accounts.find((a) => a.userId === state.activeAccountId) || state.accounts[0] || null;
}

function activeArchive() {
  return state.archives.find((a) => a.id === state.activeArchiveId) || null;
}

function activeConversation() {
  const archive = activeArchive();
  if (!archive) return null;
  if (!state.conversations[archive.id]) state.conversations[archive.id] = emptyConversation(archive.id);
  return state.conversations[archive.id];
}

function lockKeyFor(archive, account) {
  if (!archive || !account) return "";
  const archiveId = archive.archiveId || archive.id;
  return `${archiveId}:${archive.groupId || ""}:${account.userId}`;
}

function hasOtherRunningWindow(archive, account) {
  const key = lockKeyFor(archive, account);
  if (!key) return false;
  return state.archives.some((item) => {
    if (item.id === archive.id) return false;
    if (item.userId !== account.userId) return false;
    if (lockKeyFor(item, account) !== key) return false;
    return state.conversations[item.id]?.status === "running";
  });
}

function backend(path) {
  const base = (state.settings.backendUrl || "http://127.0.0.1:8000").replace(/\/$/, "");
  if (location.protocol.startsWith("http") && location.port === "8765") return path;
  return `${base}${path}`;
}

async function fetchJson(path, options = {}) {
  const resp = await fetch(backend(path), options);
  if (!resp.ok) throw new Error(`${resp.status}: ${await safeText(resp)}`);
  return resp.json();
}

function makeTextFile(name, content) {
  const blob = new Blob([content], { type: "text/plain;charset=utf-8" });
  return new File([blob], name, { type: "text/plain;charset=utf-8" });
}

async function safeText(resp) {
  try {
    return await resp.text();
  } catch {
    return "";
  }
}

async function parseSseStream(response, onEvent) {
  if (!response.body) throw new Error("当前浏览器不支持流式响应");
  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let match;
    while ((match = buffer.match(/\r?\n\r?\n/))) {
      const raw = buffer.slice(0, match.index);
      buffer = buffer.slice((match.index || 0) + match[0].length);
      const evt = parseSseEvent(raw);
      if (evt) onEvent(evt);
    }
  }
  if (buffer.trim()) {
    const evt = parseSseEvent(buffer);
    if (evt) onEvent(evt);
  }
}

function parseSseEvent(raw) {
  const lines = raw.split(/\r?\n/);
  let event = "message";
  const dataLines = [];
  for (const line of lines) {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    if (line.startsWith("data:")) dataLines.push(line.slice(5).trimStart());
  }
  if (!dataLines.length) return null;
  const rawData = dataLines.join("\n");
  let data = rawData;
  try {
    data = JSON.parse(rawData);
  } catch {
    data = { text: rawData };
  }
  return { event, data };
}

function addMessage(message) {
  const conv = activeConversation();
  if (!conv) return "";
  const id = message.id || uid("msg");
  conv.messages.push({
    id,
    role: "system",
    text: "",
    createdAt: nowIso(),
    status: "done",
    attachments: [],
    ...message,
    id,
  });
  saveState();
  render();
  scrollTranscript();
  return id;
}

function updateMessage(id, patch, archiveId = state.activeArchiveId) {
  const conv = state.conversations[archiveId] || activeConversation();
  const msg = conv?.messages.find((m) => m.id === id);
  if (!msg) return;
  Object.assign(msg, patch);
  saveState();
  if (archiveId === state.activeArchiveId) {
    renderTranscript();
    scrollTranscript();
  }
}

function addWorkflowNode(run, node) {
  run.nodes.push({
    id: uid("node"),
    kind: "workflow",
    title: "事件",
    status: "done",
    detail: "",
    depth: 0,
    createdAt: nowIso(),
    ...node,
  });
}

function workflowNodeMergeKey(node) {
  if (!node) return "";
  if (node.mergeKey) return node.mergeKey;
  if (node.streamKey) return `stream:${node.streamKey}`;
  if (node.commandId) return `command:${node.commandId}`;
  if (node.helperId) return `helper:${node.helperId}`;
  if (node.historyKey) return `history:${node.historyKey}`;
  return "";
}

function workflowEventId(data = {}, event = "") {
  if (!data || typeof data !== "object") return "";
  return data.event_id || data.id || data.node_id || data.sequence || data.seq || data.index || data.ts || data.timestamp || data.created_at || data.createdAt || "";
}

function workflowUniqueMergeKey(prefix = "event") {
  return `${prefix}:${uid("wf")}`;
}

function workflowEventMergeKey(data = {}, event = "workflow", { reusable = false } = {}) {
  if (data?.mergeKey) return data.mergeKey;
  const eventId = workflowEventId(data, event);
  const kind = data?.kind || event || "workflow";
  if (eventId) return `${kind}:${eventId}`;
  if (reusable) {
    const helperId = data?.helperId || data?.helper_id || data?.proc_id || data?.process_id || data?.task_id || "";
    const commandId = data?.command_id || data?.commandId || "";
    return `${kind}:${commandId || helperId || data?.tool || data?.message || event}`;
  }
  return workflowUniqueMergeKey(kind);
}

function isHelperWorkflowKind(kind) {
  const text = String(kind || "");
  return text === "helper" || text.startsWith("helper:") || text.startsWith("helper_");
}

function parsedNodeDetail(node) {
  if (!node || !node.detail || typeof node.detail !== "string") return null;
  const text = node.detail.trim();
  if (!text.startsWith("{") || !text.endsWith("}")) return null;
  try {
    const value = JSON.parse(text);
    return value && typeof value === "object" ? value : null;
  } catch {
    return null;
  }
}

function normalizedWorkflowNode(node) {
  const detail = parsedNodeDetail(node) || {};
  const helperId = node?.helperId || node?.helper_id || node?.proc_id || node?.process_id || node?.task_id
    || detail.helperId || detail.helper_id || detail.proc_id || detail.process_id || detail.task_id || "";
  const taskId = node?.taskId || node?.task_id || detail.task_id || detail.helper_task_id || "";
  const helperKind = node?.helperKind || node?.helper_kind || detail.helper_kind || "";
  let kind = node?.kind || detail.kind || "";
  if (helperId && String(kind || "") === "workflow" && detail.kind) kind = detail.kind;
  return {
    ...node,
    kind,
    helperId,
    taskId,
    helperKind,
    title: node?.title || detail.title || detail.message || kind || "事件",
  };
}

function helperNodeId(node) {
  const normalized = normalizedWorkflowNode(node);
  return normalized.helperId || "";
}

function helperGroupId(node) {
  const normalized = normalizedWorkflowNode(node);
  return normalized.taskId || normalized.helperId || "";
}

function helperTaskId(node) {
  const normalized = normalizedWorkflowNode(node);
  return normalized.taskId || "";
}

function isHelperProgressNode(node) {
  return String(normalizedWorkflowNode(node).kind || "") === "helper_progress";
}

function helperProgressOwnerId(node) {
  const normalized = normalizedWorkflowNode(node);
  return normalized.helperId || normalized.taskId || helperGroupId(normalized) || "";
}

function completePriorHelperProgress(run, ownerId, currentKey = "") {
  if (!run?.nodes || !ownerId) return;
  for (const node of run.nodes) {
    if (!isHelperProgressNode(node)) continue;
    if (helperProgressOwnerId(node) !== ownerId) continue;
    if (currentKey && workflowNodeMergeKey(node) === currentKey) continue;
    if (!node.status || node.status === "running") {
      node.status = "done";
    }
  }
}

function completeHelperProgress(run, ownerId) {
  if (!run?.nodes || !ownerId) return;
  for (const node of run.nodes) {
    if (!isHelperProgressNode(node)) continue;
    if (helperProgressOwnerId(node) !== ownerId) continue;
    if (!node.status || node.status === "running") {
      node.status = "done";
      node.updatedAt = node.updatedAt || nowIso();
    }
  }
}

function helperKindLabel(kind) {
  const raw = String(kind || "helper");
  return raw.startsWith("helper:") ? raw.slice("helper:".length) || "helper" : raw || "helper";
}

function helperRootTitle(node, helperId = "") {
  const normalized = normalizedWorkflowNode(node);
  const kind = helperKindLabel(normalized.helperKind || normalized.kind || "helper");
  const task = normalized.taskId || helperTaskId(normalized) || helperId;
  return `${kind} helper${task ? ` · ${task}` : ""}`;
}

function helperRootDetail(node, helperId = "") {
  const normalized = normalizedWorkflowNode(node);
  const detail = parsedNodeDetail(normalized) || {};
  const lines = [];
  const kind = helperKindLabel(normalized.helperKind || normalized.kind || "helper");
  const task = normalized.taskId || detail.task_id || detail.helper_task_id || "";
  lines.push(`类型: ${kind}`);
  if (task) lines.push(`任务: ${task}`);
  if (helperId) lines.push(`进程: ${helperId}`);
  if (normalized.status || detail.status) lines.push(`状态: ${normalized.status || detail.status}`);
  if (detail.last_iter != null) lines.push(`迭代: ${detail.last_iter}`);
  if (Array.isArray(detail.recent_tools) && detail.recent_tools.length) {
    lines.push(`最近工具: ${detail.recent_tools.slice(-5).join(", ")}`);
  }
  if (detail.progress_summary) lines.push(`摘要: ${detail.progress_summary}`);
  if (detail.last_thought_preview) lines.push(`最近进展: ${detail.last_thought_preview}`);
  return lines.join("\n");
}

function helperProgressMergeKey(data, event = "workflow") {
  const helperId = data?.helperId || data?.helper_id || data?.proc_id || data?.process_id || data?.task_id || "";
  if (!helperId) return `${data?.kind || event}:${data?.message || event}`;
  const iter = data?.iter ?? data?.last_iter ?? "";
  const tool = Array.isArray(data?.recent_tools) && data.recent_tools.length ? data.recent_tools[data.recent_tools.length - 1] : "";
  const phase = data?.kind || event || "helper_progress";
  if (phase === "helper_progress") return `helper_progress:${helperId}:iter:${iter || "?"}:tool:${tool || "stream"}`;
  return `${phase}:${helperId}`;
}

function helperProgressTitle(data, event = "workflow") {
  const kind = data?.kind || event || "helper_progress";
  if (kind === "helper_progress") {
    const iter = data?.iter ?? data?.last_iter ?? "";
    const tool = Array.isArray(data?.recent_tools) && data.recent_tools.length ? data.recent_tools[data.recent_tools.length - 1] : "";
    return `${iter ? `iter ${iter}` : "helper 进度"}${tool ? ` · ${tool}` : ""}`;
  }
  if (kind === "helper_start") return "helper 启动";
  if (kind === "helper_blocked") return "helper 被守卫拦截";
  if (kind === "helper_registry_done") return "helper 进程已退出";
  return data?.title || data?.message || kind;
}

function markHelperLifecycleExited(run, ownerId) {
  if (!run?.nodes || !ownerId) return;
  for (const node of run.nodes) {
    if (!isHelperWorkflowKind(node.kind)) continue;
    if (helperProgressOwnerId(node) !== ownerId) continue;
    if (!node.status || node.status === "running") {
      node.status = "exited";
      node.updatedAt = node.updatedAt || nowIso();
    }
  }
}

function workflowDetailText(data, event = "workflow") {
  if (typeof data === "string") return data;
  if (!data || typeof data !== "object") return String(data ?? "");
  const kind = data.kind || event || "";
  if (String(kind).startsWith("helper")) {
    const lines = [];
    if (data.reason) lines.push(`原因: ${data.reason}`);
    if (data.error) lines.push(`类型: ${data.error}`);
    if (Array.isArray(data.blocked_tasks) && data.blocked_tasks.length) {
      lines.push(`被拦截任务: ${data.blocked_tasks.map((item) => item.task_id || item.helper_kind || "helper").join(", ")}`);
    }
    if (data.what_doing) lines.push(`正在: ${data.what_doing}`);
    if (data.last_thought) lines.push(`进展: ${data.last_thought}`);
    if (data.last_note) lines.push(`状态: ${data.last_note}`);
    if (data.progress_summary) lines.push(`摘要: ${data.progress_summary}`);
    if (Array.isArray(data.recent_tools) && data.recent_tools.length) {
      lines.push(`最近工具: ${data.recent_tools.slice(-6).join(" -> ")}`);
    }
    if (data.elapsed_seconds != null) lines.push(`耗时: ${Number(data.elapsed_seconds).toFixed(1)}s`);
    if (data.heartbeat_status) lines.push(`心跳: ${data.heartbeat_status}`);
    return lines.join("\n") || helperRootDetail(data, data.proc_id || data.process_id || data.task_id || "");
  }
  return JSON.stringify(data, null, 2);
}

function mainToolTitle(data = {}, event = "workflow") {
  const tool = data.tool || data.name || "工具";
  const iter = data.iteration ? `iter ${data.iteration}` : "";
  const suffix = iter ? ` (${iter})` : "";
  if (data.kind === "main_tool_start") return `主进程调用 ${tool}${suffix}`;
  if (data.kind === "main_tool_done") {
    const elapsed = data.elapsed_seconds != null ? `，${data.elapsed_seconds}s` : "";
    return `主进程完成 ${tool}${suffix}${elapsed}`;
  }
  return data.title || data.message || event;
}

function workflowNodeStatus(status, kind = "", event = "", data = {}) {
  const raw = String(status || data?.status || "").toLowerCase();
  const tag = String(kind || data?.kind || event || "").toLowerCase();
  const eventName = String(event || "").toLowerCase();
  if (tag === "main_tool_start") return "running";
  if (tag === "main_tool_done") return data?.ok === false ? "error" : "done";
  if (tag === "helper_registry_done") return "exited";
  if (data?.ok === false) return "error";
  if (["helper_blocked", "blocked"].includes(tag) || raw === "blocked") return "error";
  if (["error", "failed", "fail", "exception"].includes(raw) || ["error", "failed", "fail", "exception"].includes(tag)) return "error";
  if (["interrupted", "aborted", "cancelled", "canceled"].includes(raw) || ["interrupted", "aborted", "cancelled", "canceled"].includes(tag)) return "interrupted";
  if (["done", "complete", "completed", "success", "ok"].includes(raw)) return "done";
  if (["exited", "exit"].includes(raw)) return "exited";
  if (["done", "complete", "completed", "tool_done"].includes(tag)) return "done";
  if (["done", "complete"].includes(eventName)) return "done";

  // These monitor payloads are point-in-time log events. Active commands and
  // helpers are represented by monitor snapshots, so these rows should not
  // stay visually "running" after they have been recorded.
  if (["start", "pid", "tool_start", "tool_done"].includes(tag)) return "done";
  if (["start", "pid", "tool_start", "tool_done"].includes(eventName)) return "done";
  return raw || "running";
}

function normalizeWorkflowNodeForRun(node, run) {
  const normalized = normalizedWorkflowNode(node);
  let status = workflowNodeStatus(normalized.status, normalized.kind, "", normalized);
  if (run?.status && run.status !== "running" && status === "running") {
    status = run.status === "error" ? "error" : run.status === "interrupted" ? "interrupted" : "done";
  }
  return { ...normalized, status };
}

function finalizeWorkflowRun(run, status = "done") {
  if (!run?.nodes) return;
  const finalStatus = status === "done" ? "done" : (status || "done");
  for (const node of run.nodes) {
    const current = workflowNodeStatus(node.status, node.kind, "", node);
    if (current === "error") {
      node.status = "error";
      continue;
    }
    if (current === "interrupted") {
      node.status = "interrupted";
      continue;
    }
    if (!node.status || node.status === "running") {
      node.status = finalStatus;
      node.updatedAt = node.updatedAt || nowIso();
    }
  }
  for (const node of run.nodes) {
    const ownerId = helperProgressOwnerId(node);
    if (ownerId) completeHelperProgress(run, ownerId);
  }
  run.commandCount = 0;
  run.helperCount = 0;
}

function upsertWorkflowNode(run, node, options = {}) {
  if (!run) return null;
  const key = workflowNodeMergeKey(node);
  const existing = key ? run.nodes.find((item) => workflowNodeMergeKey(item) === key) : null;
  if (!existing) {
    addWorkflowNode(run, node);
    return run.nodes[run.nodes.length - 1] || null;
  }
  const nextDetail = node.detail ?? existing.detail ?? "";
  Object.assign(existing, {
    ...node,
    id: existing.id,
    createdAt: existing.createdAt,
    detail: options.appendDetail ? `${existing.detail || ""}${nextDetail}` : nextDetail,
    updatedAt: nowIso(),
  });
  return existing;
}

function appendWorkflowStream(run, streamKey, text, title = "LLM 返回流") {
  if (!run || !text) return null;
  return upsertWorkflowNode(run, {
    streamKey,
    mergeKey: `stream:${streamKey}`,
    kind: "stream",
    title,
    status: "running",
    detail: text,
  }, { appendDetail: true });
}

function streamTextFromData(data) {
  if (data == null) return "";
  if (typeof data === "string") return data;
  if (typeof data !== "object") return String(data);
  for (const key of ["text", "content", "delta", "reasoning_content", "reasoning", "think"]) {
    const value = data[key];
    if (typeof value === "string") return value;
  }
  return "";
}

function assistantMessageById(id, archiveId = state.activeArchiveId) {
  const conv = state.conversations[archiveId] || activeConversation();
  return conv?.messages.find((m) => m.id === id && m.role === "assistant") || null;
}

function currentStreamingAssistantMessage(conv = activeConversation()) {
  if (!conv?.messages?.length) return null;
  for (let i = conv.messages.length - 1; i >= 0; i -= 1) {
    const msg = conv.messages[i];
    if (msg.role === "assistant" && msg.status === "streaming") return msg;
  }
  return null;
}

function compactStatusText(text, limit = 220) {
  const value = String(text || "").replace(/\s+/g, " ").trim();
  if (!value) return "";
  return value.length > limit ? `${value.slice(0, limit - 1)}…` : value;
}

function workflowLabelText(kind = "") {
  const raw = String(kind || "").trim();
  const lower = raw.toLowerCase();
  if (!raw) return "进度";
  if (lower.includes("plan") || lower.includes("planning")) return "计划";
  if (lower.includes("intent")) return "意图";
  if (lower.includes("memory")) return "记忆";
  if (lower.includes("tool")) return "工具";
  if (lower.includes("helper")) return "helper";
  if (lower.includes("command")) return "命令";
  if (lower.includes("reply") || lower.includes("composing")) return "回复";
  return raw;
}

function intermediateReplyLabel(event = "") {
  const key = String(event || "").toLowerCase();
  if (key === "helper_blocked") return "分工调整";
  if (key === "helper_start") return "分工开始";
  if (key === "helper_done") return "分工结果";
  if (key === "helper_exit") return "进程退出";
  if (key === "milestone") return "里程碑";
  if (key === "long_silence") return "状态更新";
  if (key === "stuck") return "阻力处理";
  if (key === "breakthrough") return "突破进展";
  return "中途反馈";
}

function appendAssistantRound2Content(assistantId, item) {
  const msg = assistantMessageById(assistantId);
  if (!msg || msg.status !== "streaming" || msg.text) return false;
  const text = String(item?.text || item?.message || item?.title || "").trim();
  if (!text) return false;
  const key = item?.key || `round2:${Date.now()}`;
  const nextItem = {
    key,
    label: compactStatusText(item?.label || "进度", 24),
    text,
    ts: nowIso(),
  };
  const items = Array.isArray(msg.round2PreviewItems) ? msg.round2PreviewItems.slice() : [];
  const last = items[items.length - 1];
  if (last && last.text === nextItem.text && last.label === nextItem.label) return false;
  items.push(nextItem);
  msg.round2PreviewItems = items;
  return true;
}

function latestWorkflowActionText(run) {
  const rows = workflowCompactRows(run).filter((node) => String(node.kind || "") !== "stream");
  if (!rows.length) return "";
  const latest = rows[rows.length - 1];
  const latestTime = Date.parse(latest?.updatedAt || latest?.createdAt || "") || 0;
  const running = [...rows].reverse().find((node) => {
    if (workflowNodeStatus(node.status, node.kind, "", node) !== "running") return false;
    const nodeTime = Date.parse(node.updatedAt || node.createdAt || "") || 0;
    return !latestTime || latestTime - nodeTime < 15000;
  });
  const node = running || latest;
  const label = helperGroupId(node) ? helperKindLabel(node.helperKind || node.kind || "helper") : workflowLabelText(node.kind || "workflow");
  const text = compactStatusText(compactNodeText(node), 180);
  return text ? `${label}: ${text}` : "";
}

function updateAssistantCurrentAction(assistantId, run) {
  const msg = assistantMessageById(assistantId);
  if (!msg || msg.status !== "streaming") return false;
  const text = latestWorkflowActionText(run);
  if (!text || msg.currentActionText === text) return false;
  msg.currentActionText = text;
  return true;
}

function updateStreamingAssistantCurrentActionFromRun(run) {
  const msg = currentStreamingAssistantMessage();
  if (!msg) return false;
  const text = latestWorkflowActionText(run);
  if (!text || msg.currentActionText === text) return false;
  msg.currentActionText = text;
  return true;
}

function updateStreamingAssistantRound2PreviewFromRun(run) {
  return updateStreamingAssistantCurrentActionFromRun(run);
}

function clearAssistantRound2Preview(assistantId) {
  const msg = assistantMessageById(assistantId);
  if (!msg) return false;
  let changed = false;
  if (msg.round2PreviewItems) {
    delete msg.round2PreviewItems;
    changed = true;
  }
  if (!msg.round3Started) {
    msg.round3Started = true;
    changed = true;
  }
  return changed;
}

function isThinkStreamEvent(event, data) {
  const ev = String(event || "").toLowerCase();
  if (ev === "think" || ev === "reasoning" || ev === "reasoning_token") return true;
  if (data && typeof data === "object") {
    const type = String(data.type || data.kind || data.channel || "").toLowerCase();
    if (type === "think" || type === "reasoning") return true;
    if (typeof data.reasoning_content === "string" && !data.text && !data.content) return true;
  }
  return false;
}

function isTextStreamEvent(event, data) {
  const ev = String(event || "").toLowerCase();
  if (["token", "text", "content", "delta", "message"].includes(ev)) return true;
  return isThinkStreamEvent(event, data);
}

function moveQueueItem(queueId, direction) {
  const conv = activeConversation();
  if (!conv) return;
  const idx = conv.queue.findIndex((q) => q.id === queueId);
  if (idx < 0) return;
  const nextIdx = idx + direction;
  if (nextIdx < 0 || nextIdx >= conv.queue.length) return;
  const [item] = conv.queue.splice(idx, 1);
  conv.queue.splice(nextIdx, 0, item);
  saveState();
  render();
}

function editQueueItem(queueId) {
  const conv = activeConversation();
  const item = conv?.queue.find((q) => q.id === queueId);
  if (!item) return;
  const next = prompt("编辑队列消息", item.text);
  if (next === null) return;
  const trimmed = next.trim();
  if (!trimmed) return;
  item.text = trimmed;
  saveState();
  render();
}

function clearQueue() {
  const conv = activeConversation();
  if (!conv?.queue.length) return;
  if (!confirm("清空当前存档的本地队列？")) return;
  conv.queue = [];
  saveState();
  render();
}

function projectToArchive(item, userId) {
  const id = uid("archive_window");
  return {
    id,
    windowId: id,
    archiveId: item.archive_id,
    userId,
    title: item.project_name || "bot",
    groupId: item.group_id,
    currentDir: item.root_dir || "",
    projectId: item.project_key,
    createdAt: item.created_at || nowIso(),
    lastUsedAt: item.last_seen_at || nowIso(),
    localOnly: false,
  };
}

function mergeArchiveFromBackend(item, userId) {
  if (!item?.archive_id) return null;
  let archive = state.archives.find(
    (a) => a.userId === userId && a.archiveId === item.archive_id && a.projectId === item.project_key
  );
  if (!archive) {
    archive = projectToArchive(item, userId);
    state.archives.push(archive);
  } else {
    archive.archiveId = item.archive_id;
    archive.groupId = item.group_id || archive.groupId;
    archive.projectId = item.project_key || archive.projectId;
    archive.currentDir = item.root_dir || archive.currentDir || "";
    archive.title = archive.title || item.project_name || "bot";
    archive.localOnly = false;
    archive.lastUsedAt = item.last_seen_at || nowIso();
  }
  if (!state.conversations[archive.id]) state.conversations[archive.id] = emptyConversation(archive.id);
  if (!state.files[archive.id]) state.files[archive.id] = [];
  if (!state.artifacts[archive.id]) state.artifacts[archive.id] = [];
  return archive;
}

function removeArchiveLocalState(archiveId) {
  delete state.conversations[archiveId];
  delete state.files[archiveId];
  delete state.artifacts[archiveId];
}

function resetArchivesForAccount(userId) {
  const kept = [];
  let removed = 0;
  for (const archive of state.archives || []) {
    if (archive.userId === userId) {
      removeArchiveLocalState(archive.id);
      removed += 1;
    } else {
      kept.push(archive);
    }
  }
  const archive = makeLocalArchive(userId, "默认对话", "");
  kept.push(archive);
  state.archives = kept;
  state.conversations[archive.id] = emptyConversation(archive.id);
  state.files[archive.id] = [];
  state.artifacts[archive.id] = [];
  if (state.activeAccountId === userId) state.activeArchiveId = archive.id;
  return removed;
}

function pruneArchivesAfterBackendSync(userId, projects) {
  const backendProjects = Array.isArray(projects) ? projects : [];
  const valid = new Set(
    backendProjects
      .filter((item) => item?.archive_id)
      .map((item) => `${item.archive_id}:${item.project_key || ""}`)
  );
  const next = [];
  let removed = 0;
  for (const archive of state.archives || []) {
    const belongsToUser = archive.userId === userId;
    const backendBound = Boolean(archive.archiveId) && archive.localOnly === false;
    const key = `${archive.archiveId || ""}:${archive.projectId || ""}`;
    if (belongsToUser && backendBound && (backendProjects.length === 0 || !valid.has(key))) {
      removeArchiveLocalState(archive.id);
      removed += 1;
      continue;
    }
    next.push(archive);
  }
  state.archives = next;
  if (!state.archives.some((archive) => archive.id === state.activeArchiveId)) {
    const replacement = state.archives.find((archive) => archive.userId === userId);
    if (replacement) {
      state.activeArchiveId = replacement.id;
    } else {
      const archive = makeLocalArchive(userId, "默认对话", "");
      state.archives.push(archive);
      state.conversations[archive.id] = emptyConversation(archive.id);
      state.files[archive.id] = [];
      state.artifacts[archive.id] = [];
      state.activeArchiveId = archive.id;
    }
  }
  return removed;
}


async function ensureBackendArchive(archive, account) {
  if (!archive || !account) throw new Error("未选择账号或存档");
  if (archive.archiveId && !archive.localOnly) return archive;

  const body = {
    user_id: account.userId,
    title: archive.title || "bot chat",
    current_dir: archive.currentDir || "",
    project_id: archive.projectId || "",
    archive_id: archive.archiveId || "",
    group_id: archive.groupId || "",
  };
  const data = await fetchJson("/v1/agent/projects", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  adoptProject(archive, data);
  return archive;
}

function adoptProject(archive, data) {
  archive.archiveId = data.archive_id;
  archive.groupId = data.group_id;
  archive.projectId = data.project_key;
  archive.currentDir = data.root_dir || archive.currentDir || "";
  archive.title = archive.title || data.project_name || "bot";
  archive.localOnly = false;
  archive.lastUsedAt = nowIso();

  if (!state.conversations[archive.id]) state.conversations[archive.id] = emptyConversation(archive.id);
  if (!state.files[archive.id]) state.files[archive.id] = [];
  if (!state.artifacts[archive.id]) state.artifacts[archive.id] = [];
  state.activeArchiveId = archive.id;
  saveState();
}

function buildChatRequest(message, archive, account, clientMsgId, fileIds) {
  return {
    archive_id: archive.archiveId || archive.id,
    group_id: archive.groupId,
    user_id: account.userId,
    user_name: account.displayName,
    message,
    client_msg_id: clientMsgId,
    current_dir: archive.currentDir || "",
    project_id: archive.projectId || "",
    persona_id: "environment",
    attached_file_ids: fileIds,
  };
}

function longInputFilename() {
  const stamp = new Date().toISOString().replace(/[-:]/g, "").replace(/\..+$/, "").replace("T", "_");
  return `long_input_${stamp}.txt`;
}

async function prepareMessageForSend(text, fileIds, { queuedItem = null, mode = "normal" } = {}) {
  if (!text || text.length <= LONG_MESSAGE_FILE_THRESHOLD) {
    return { text, fileIds, converted: false };
  }
  if (queuedItem?.longInputConverted) {
    return { text, fileIds, converted: false };
  }
  const ok = confirmAction(
    `当前输入约 ${text.length.toLocaleString()} 个字符，直接发送会挤占主进程上下文并可能失败。\n\n` +
    `是否将其转换为 bot 文件区 txt 附件，并只发送一条短指令让 bot 读取该文件？`
  );
  if (!ok) {
    notify("已取消发送", "请把超长内容保存为文件后上传，或确认自动转为文件。", "warn");
    return null;
  }
  const file = makeTextFile(longInputFilename(), text);
  const savedFile = await uploadSingleBotFile(file, {
    autoAttach: true,
    notifySuccess: true,
    sourceLabel: "超长输入",
  });
  const nextIds = [...new Set([...(fileIds || []), savedFile.id])];
  const shortText = [
    `用户需求已转换为 bot 文件区附件：${savedFile.name}`,
    "",
    "请先读取并完整理解该附件中的用户原始需求，再按其中要求执行。若内容较长，请使用分段读取、摘要和 helper 处理，避免把全文一次性塞入主进程上下文。",
  ].join("\n");
  return { text: shortText, fileIds: nextIds, converted: true, file: savedFile };
}

async function sendMessage(mode = "normal", queuedItem = null) {
  const archive = activeArchive();
  const account = activeAccount();
  const conv = activeConversation();
  if (!archive || !account || !conv) return;
  const sendLockId = archive.id;
  if (mode === "normal" && !queuedItem && sendingArchiveIds.has(sendLockId)) {
    notify("正在发送", "上一条消息仍在准备或提交，已忽略重复点击。", "info");
    return;
  }
  let text = (queuedItem?.text ?? currentDraft).trim();
  if (!text) return;
  if (mode === "normal" && !queuedItem) sendingArchiveIds.add(sendLockId);
  let assistantId = "";
  let run = null;

  try {
    let fileIds = (queuedItem?.attachments || attachedFileIds).slice();
    if (mode === "insert" && !queuedItem && text.length > LONG_MESSAGE_FILE_THRESHOLD) {
      const preparedInsert = await prepareMessageForSend(text, fileIds, { queuedItem, mode });
      if (!preparedInsert) return;
      conv.queue.push({
        id: uid("queue"),
        text: preparedInsert.text,
        attachments: preparedInsert.fileIds,
        createdAt: nowIso(),
        longInputConverted: preparedInsert.converted,
        reason: "long_insert_as_followup",
      });
      pushInputHistory(preparedInsert.text);
      currentDraft = "";
      attachedFileIds = [];
      syncDraftToConversation();
      saveState();
      render();
      notify("超长插入已转为下一轮队列", "运行中插入接口不携带附件；已上传为文件并排入下一轮。", "info");
      return;
    }
    const prepared = await prepareMessageForSend(text, fileIds, { queuedItem, mode });
    if (!prepared) return;
    text = prepared.text;
    fileIds = prepared.fileIds;
    if (mode === "queue") {
      conv.queue.push({ id: uid("queue"), text, attachments: fileIds, createdAt: nowIso(), longInputConverted: prepared.converted });
      pushInputHistory(text);
      currentDraft = "";
      attachedFileIds = [];
      syncDraftToConversation();
      saveState();
      render();
      return;
    }
    if (mode === "insert") {
      await insertIntoRun(text);
      return;
    }
    if (conv.status === "running" && !queuedItem) {
      conv.queue.push({ id: uid("queue"), text, attachments: fileIds, createdAt: nowIso(), longInputConverted: prepared.converted });
      pushInputHistory(text);
      currentDraft = "";
      attachedFileIds = [];
      syncDraftToConversation();
      saveState();
      render();
      notify("已加入队列", text.slice(0, 80), "info");
      return;
    }
    if (!queuedItem && hasOtherRunningWindow(archive, account)) {
      conv.queue.push({ id: uid("queue"), text, attachments: fileIds, createdAt: nowIso(), reason: "other_window_running", longInputConverted: prepared.converted });
      pushInputHistory(text);
      currentDraft = "";
      attachedFileIds = [];
      syncDraftToConversation();
      saveState();
      render();
      notify("同锁窗口运行中", "消息已在本窗口排队", "info");
      return;
    }

    try {
      await ensureBackendArchive(archive, account);
    } catch (err) {
      addMessage({ role: "system_status", text: `创建后端存档失败：${err.message || err}`, status: "failed" });
      return;
    }

    addMessage({ role: "user", text, attachments: fileIds, sendMode: queuedItem ? "queue" : "normal" });
    if (!queuedItem?.autoContinue) conv.autoContinueStartedAt = "";
    pushInputHistory(text);
    currentDraft = "";
    if (!queuedItem) attachedFileIds = [];
    syncDraftToConversation();

    assistantId = addMessage({
      role: "assistant",
      text: "",
      status: "streaming",
      round2PreviewItems: [{
        key: "request:submitted",
        label: "状态",
        text: "请求已提交，等待后端开始处理。",
        ts: nowIso(),
      }],
      currentActionText: "连接后端并准备工作流",
    });
    run = {
      id: uid("run"),
      traceId: "",
      status: "running",
      startedAt: nowIso(),
      nodes: [],
    };
    conv.workflowRuns.unshift(run);
    trimWorkflowRuns();
    conv.status = "running";
    activeController = new AbortController();
    saveState();
    render();

    const request = buildChatRequest(text, archive, account, uid("client"), fileIds);
    const response = await fetch(backend("/v1/chat/stream"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(request),
      signal: activeController.signal,
    });
    if (response.status === 409) {
      conv.queue.unshift({ id: uid("queue"), text, attachments: fileIds, createdAt: nowIso() });
      notify("后端忙", "消息已放回队列", "warn");
      throw new Error("后端已有同一用户任务在运行，本条消息已放回队列");
    }
    if (!response.ok) throw new Error(`请求失败 ${response.status}: ${await safeText(response)}`);
    await parseSseStream(response, (evt) => handleSseEvent(evt, assistantId, run));
    finishRun(run, assistantId, "done", true, archive.id);
  } catch (err) {
    if (!assistantId || !run) {
      addMessage({ role: "system_status", text: `发送失败：${err.message || err}`, status: "failed" });
      notify("发送失败", err.message || String(err), "error");
      return;
    }
    if (err?.name === "AbortError") {
      finishRun(run, assistantId, "interrupted", true, archive.id);
      notify("任务已中断", run.traceId || "", "warn");
    } else {
      addWorkflowNode(run, {
        kind: "error",
        title: "请求失败",
        status: "error",
        detail: err.message || String(err),
      });
      updateMessage(assistantId, {
        status: "failed",
        text: currentAssistantText(assistantId, archive.id) || `出错：${err.message || err}`,
      }, archive.id);
      finishRun(run, assistantId, "error", false, archive.id);
      notify("任务失败", err.message || String(err), "error");
    }
  } finally {
    sendingArchiveIds.delete(sendLockId);
    activeController = null;
    saveState();
    render();
  }
}

function currentAssistantText(id, archiveId = state.activeArchiveId) {
  return (state.conversations[archiveId] || activeConversation())?.messages.find((m) => m.id === id)?.text || "";
}

async function recoverFromBackend() {
  const account = activeAccount();
  if (!account || startupRecovered) return;
  startupRecovered = true;
  try {
    const projects = await fetchJson(`/v1/agent/projects?user_id=${encodeURIComponent(account.userId)}`);
    const removed = pruneArchivesAfterBackendSync(account.userId, projects);
    for (const item of projects) mergeArchiveFromBackend(item, account.userId);
    if (removed) {
      notify("已清理本地存档", `后端已无对应项目，已移除 ${removed} 个本地缓存存档。`, "info");
    }
    saveState();
    render();
  } catch (err) {
    addPassiveWorkflow("recover", "后端项目列表恢复失败", "error", err.message || String(err));
  }
  await refreshActiveState();
}

function setupStateBroadcast() {
  if (!("BroadcastChannel" in window)) return;
  stateChannel = new BroadcastChannel("bot-agent-state");
  stateChannel.addEventListener("message", (event) => {
    const msg = event.data || {};
    if (msg.type !== "state-updated" || !msg.state) return;
    if (msg.origin === window.name) return;
    const incoming = normalizeState({ ...defaultState, ...msg.state });
    state = incoming;
    attachedFileIds = attachedFileIds.filter((id) => fileRefs([id]).length);
    render();
  });
}

function broadcastState() {
  if (!stateChannel) return;
  try {
    stateChannel.postMessage({
      type: "state-updated",
      origin: window.name,
      state,
    });
  } catch {
    // Best-effort local multi-window sync.
  }
}

async function refreshActiveState() {
  const account = activeAccount();
  if (!account) return;
  try {
    const active = await fetchJson("/v1/chat/active");
    const mine = (active.items || []).filter((item) => item.user_id === account.userId);
    for (const item of mine) {
      const archive = state.archives.find((a) => (a.archiveId || a.id) === item.archive_id && a.groupId === item.group_id);
      if (!archive) continue;
      const conv = state.conversations[archive.id] || emptyConversation(archive.id);
      state.conversations[archive.id] = conv;
      if (conv.status !== "running") {
        conv.status = "running";
        conv.workflowRuns.unshift({
          id: uid("run"),
          traceId: item.trace_id || "",
          status: "running",
          startedAt: nowIso(),
          nodes: [{
            id: uid("node"),
            kind: "recover",
            title: "恢复后端活跃任务",
            status: "running",
            detail: JSON.stringify(item, null, 2),
            createdAt: nowIso(),
          }],
        });
      }
    }
    saveState();
    render();
    startMonitor();
    await loadMonitorHistory();
  } catch (err) {
    addPassiveWorkflow("recover", "活跃任务恢复失败", "error", err.message || String(err));
  }
}

async function refreshRunSnapshot() {
  const archive = activeArchive();
  const account = activeAccount();
  const conv = activeConversation();
  const run = conv?.workflowRuns?.[0];
  if (!archive || !account || !run?.traceId) {
    alert("当前没有可恢复的 trace。");
    return;
  }
  try {
    const query = new URLSearchParams({
      archive_id: archive.archiveId || archive.id,
      group_id: archive.groupId || "",
      user_id: account.userId,
      limit: "500",
    });
    const data = await fetchJson(`/v1/chat/runs/${encodeURIComponent(run.traceId)}?${query}`);
    for (const item of data.items || []) {
      const payload = item.payload || {};
      const unique = `run:${item.event || "event"}:${payload.kind || ""}:${payload.command_id || payload.task_id || payload.message || item.ts || ""}`;
      if (run.nodes.some((node) => node.historyKey === unique)) continue;
      upsertWorkflowNode(run, {
        mergeKey: unique,
        kind: payload.kind || item.event || "history",
        title: payload.title || payload.message || payload.kind || item.event || "历史事件",
        status: payload.status || "done",
        commandId: payload.command_id || "",
        helperId: payload.proc_id || payload.process_id || payload.task_id || "",
        taskId: payload.task_id || "",
        helperKind: payload.helper_kind || "",
        historyKey: unique,
        detail: JSON.stringify(payload, null, 2),
      });
    }
    if (data.active) mergeMonitorSnapshot(data.active);
    saveState();
    renderWorkflowOnly();
  } catch (err) {
    addPassiveWorkflow("history", "Run 快照加载失败", "error", err.message || String(err));
  }
}

async function loadMonitorHistory() {
  const archive = activeArchive();
  const account = activeAccount();
  const conv = activeConversation();
  const run = conv?.workflowRuns?.[0];
  if (!archive?.archiveId || !account || !run) return;
  try {
    const query = new URLSearchParams({
      archive_id: archive.archiveId || archive.id,
      group_id: archive.groupId || "",
      user_id: account.userId,
      trace_id: run.traceId || "",
      limit: "200",
    });
    const data = await fetchJson(`/v1/chat/monitor/history?${query}`);
    for (const item of data.items || []) {
      const payload = item.payload || {};
      const unique = `${item.event || "event"}:${payload.kind || ""}:${payload.command_id || payload.task_id || payload.message || item.ts || ""}`;
      if (run.nodes.some((node) => node.historyKey === unique)) continue;
      upsertWorkflowNode(run, {
        mergeKey: unique,
        kind: payload.kind || item.event || "history",
        title: payload.title || payload.message || payload.kind || item.event || "历史事件",
        status: payload.status || "done",
        commandId: payload.command_id || "",
        helperId: payload.proc_id || payload.process_id || payload.task_id || "",
        taskId: payload.task_id || "",
        helperKind: payload.helper_kind || "",
        historyKey: unique,
        detail: JSON.stringify(payload, null, 2),
      });
    }
    saveState();
    renderWorkflowOnly();
  } catch (err) {
    addPassiveWorkflow("history", "监控历史加载失败", "error", err.message || String(err));
  }
}

function handleSseEvent(evt, assistantId, run) {
  const { event, data } = evt;
  if (event === "meta") {
    run.traceId = data.trace_id || run.traceId;
    if (run.traceId) startMonitor();
    const env = data.environment;
    if (env) {
      const archive = activeArchive();
      if (archive) adoptProject(archive, env);
    }
    addWorkflowNode(run, { kind: "run", title: "开始运行", status: "running", detail: JSON.stringify(data, null, 2) });
  } else if (isTextStreamEvent(event, data)) {
    const text = streamTextFromData(data);
    if (!text) return;
    if (isThinkStreamEvent(event, data)) {
      appendWorkflowStream(run, "round3_think", text, "思考流");
    } else {
      const msg = activeConversation()?.messages.find((m) => m.id === assistantId);
      if (msg) {
        clearAssistantRound2Preview(assistantId);
        msg.text += text;
      }
      appendWorkflowStream(run, "round3_reply", text, "Round3 LLM 返回");
    }
  } else if (event === "intermediate_reply") {
    const text = String(data.message || "").trim();
    if (text) {
      appendAssistantRound2Content(assistantId, {
        key: `intermediate:${Date.now()}`,
        label: intermediateReplyLabel(data.event),
        text,
      });
    }
    upsertWorkflowNode(run, {
      mergeKey: workflowEventMergeKey(data, `intermediate_reply:${Date.now()}`),
      kind: "intermediate_reply",
      title: text || "中途反馈",
      status: "done",
      detail: JSON.stringify(data, null, 2),
    });
  } else if (event === "progress") {
    upsertWorkflowNode(run, {
      mergeKey: workflowEventMergeKey(data, data.kind || data.round || "progress"),
      kind: data.kind || data.round || "progress",
      title: data.message || data.kind || data.round || "进度",
      status: workflowNodeStatus(data.status, data.kind || data.round || "progress", event, data),
      detail: JSON.stringify(data, null, 2),
    });
    updateAssistantCurrentAction(assistantId, run);
  } else if (event === "done" || event === "complete") {
    if (event === "done" && Array.isArray(data.files) && data.files.length) {
      rememberArtifacts(data.files);
    }
    upsertWorkflowNode(run, {
      mergeKey: `event:${event}`,
      kind: event,
      title: event === "done" ? "回复生成完成" : "任务完成",
      status: "done",
      detail: JSON.stringify(data, null, 2),
    });
    const streamNode = run.nodes.find((node) => node.mergeKey === "stream:round3_reply");
    if (streamNode) streamNode.status = "done";
  } else if (event === "error") {
    addWorkflowNode(run, {
      kind: "error",
      title: data.message || "后端错误",
      status: "error",
      detail: JSON.stringify(data, null, 2),
    });
  } else {
    const helperId = data.helperId || data.helper_id || data.proc_id || data.process_id || data.task_id || "";
    upsertWorkflowNode(run, {
      mergeKey: workflowEventMergeKey(data, data.kind || event, { reusable: Boolean(data.command_id || helperId) }),
      kind: data.kind || event,
      title: data.title || data.message || event,
      status: workflowNodeStatus(data.status || "done", data.kind || event, event, data),
      commandId: data.command_id || "",
      helperId,
      taskId: data.task_id || "",
      helperKind: data.helper_kind || "",
      detail: typeof data === "string" ? data : JSON.stringify(data, null, 2),
    });
    updateAssistantCurrentAction(assistantId, run);
  }
  updateAssistantCurrentAction(assistantId, run);
  updateStreamingAssistantRound2PreviewFromRun(run);
  saveState();
  renderTranscript();
  scheduleWorkflowRender();
  renderArtifactPanelOnly();
  renderNotificationsOnly();
  scrollTranscript({ onlyIfNearBottom: true });
}

function finishRun(run, assistantId, status, drain = true, archiveId = state.activeArchiveId) {
  const conv = state.conversations[archiveId] || activeConversation();
  if (!conv) return;
  finalizeWorkflowRun(run, status);
  run.status = status;
  run.endedAt = nowIso();
  conv.status = status === "error" ? "error" : "idle";
  const assistant = conv.messages.find((m) => m.id === assistantId && m.role === "assistant");
  const nextStatus = status === "error" ? "failed" : (status === "interrupted" ? "interrupted" : "done");
  if (assistant && status === "interrupted" && !String(assistant.text || "").trim()) {
    conv.messages = conv.messages.filter((m) => m.id !== assistantId);
    if (archiveId === state.activeArchiveId) {
      saveState();
      renderTranscript();
    }
  } else {
    updateMessage(assistantId, { status: nextStatus }, archiveId);
  }
  if (status === "done") notify("任务完成", run.traceId || "", "done");
  if (drain && status === "done") setTimeout(() => checkAutoContinueIfNeeded(archiveId, assistantId), 250);
  if (drain && conv.autoRunQueue !== false && state.settings.autoRunQueue !== false) setTimeout(() => drainQueueIfIdle(archiveId), 500);
}

function lastUserMessageBefore(conv, assistantId) {
  if (!conv) return null;
  if (Array.isArray(conv.pendingInsertedMessages) && conv.pendingInsertedMessages.length) {
    const inserted = conv.pendingInsertedMessages[conv.pendingInsertedMessages.length - 1];
    return {
      role: "inserted_user",
      text: `用户在任务运行中补充/修正：${inserted.text || ""}`,
      createdAt: inserted.createdAt || nowIso(),
    };
  }
  const idx = conv.messages.findIndex((m) => m.id === assistantId);
  const end = idx >= 0 ? idx : conv.messages.length;
  for (let i = end - 1; i >= 0; i -= 1) {
    if (conv.messages[i]?.role === "user") return conv.messages[i];
  }
  return null;
}

function clearPendingInsertedMessages(conv) {
  if (conv && Array.isArray(conv.pendingInsertedMessages) && conv.pendingInsertedMessages.length) {
    conv.pendingInsertedMessages = [];
  }
}

function recentConversationText(conv, limit = 6000) {
  if (!conv) return "";
  const text = conv.messages
    .slice(-10)
    .map((m) => `${roleLabel(m.role)}: ${m.text || ""}`)
    .join("\n");
  return text.length > limit ? text.slice(-limit) : text;
}

async function checkAutoContinueIfNeeded(archiveId, assistantId) {
  const conv = state.conversations[archiveId];
  const assistant = conv?.messages.find((m) => m.id === assistantId);
  const user = lastUserMessageBefore(conv, assistantId);
  if (!conv || !assistant || !user) return;
  if (!state.settings.autoContinue) return;
  if (conv.status === "running" || conv.queue.length) return;
  if (!assistant.text?.trim() || !user.text?.trim()) return;

  const startedAt = conv.autoContinueStartedAt || nowIso();
  conv.autoContinueStartedAt = startedAt;
  const elapsedSec = Math.max(0, (Date.now() - new Date(startedAt).getTime()) / 1000);
  const maxSec = Math.max(1, Number(state.settings.autoContinueMaxSec || 900));
  if (elapsedSec >= maxSec) {
    notify("自动继续停止", "已达到时间上限", "info");
    clearPendingInsertedMessages(conv);
    saveState();
    return;
  }

  try {
    const result = await fetchJson("/v1/chat/auto-continue/check", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        user_message: user.text,
        assistant_reply: assistant.text,
        recent_context: recentConversationText(conv),
        auto_continue_elapsed_sec: elapsedSec,
        max_auto_continue_sec: maxSec,
      }),
    });
    if (!result.should_continue) {
      conv.autoContinueStartedAt = "";
      clearPendingInsertedMessages(conv);
      saveState();
      return;
    }
    const text = (result.continue_message || "继续").trim() || "继续";
    conv.queue.push({
      id: uid("queue"),
      text,
      attachments: [],
      createdAt: nowIso(),
      autoContinue: true,
    });
    notify("自动继续", result.reason || text, "info");
    clearPendingInsertedMessages(conv);
    saveState();
    setTimeout(() => drainQueueIfIdle(archiveId), 100);
  } catch (err) {
    notify("自动继续判定失败", err.message || String(err), "warn");
  }
}

function drainQueueIfIdle(archiveId = state.activeArchiveId) {
  const conv = state.conversations[archiveId];
  if (!conv || conv.status === "running" || !conv.queue.length) return;
  if (archiveId !== state.activeArchiveId) return;
  const item = conv.queue.shift();
  notify("队列开始执行", item.text.slice(0, 80), "info");
  saveState();
  currentDraft = "";
  attachedFileIds = item.attachments || [];
  sendMessage("normal", item);
}

async function insertIntoRun(text) {
  const archive = activeArchive();
  const account = activeAccount();
  const conv = activeConversation();
  if (!archive || !account || !conv) return;
  if (conv.status !== "running") {
    conv.queue.push({ id: uid("queue"), text, attachments: attachedFileIds.slice(), createdAt: nowIso() });
    currentDraft = "";
    attachedFileIds = [];
    syncDraftToConversation();
    saveState();
    render();
    return;
  }
  const body = {
    archive_id: archive.archiveId || archive.id,
    group_id: archive.groupId,
    user_id: account.userId,
    message: text,
    client_msg_id: uid("insert"),
    current_dir: archive.currentDir || "",
    project_id: archive.projectId || "",
  };
  try {
    const result = await fetchJson("/v1/chat/interrupt_message", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    let abortResult = { ok: false };
    if (result.ok) {
      abortResult = await fetchJson("/v1/chat/abort", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          archive_id: archive.archiveId || archive.id,
          group_id: archive.groupId,
          user_id: account.userId,
          current_dir: archive.currentDir || "",
          project_id: archive.projectId || "",
        }),
      });
    }
    const ok = Boolean(result.ok && abortResult.ok);
    addMessage({ role: "inserted_user", text, sendMode: state.settings.insertMode, status: ok ? "done" : "failed" });
    if (ok) {
      conv.pendingInsertedMessages = Array.isArray(conv.pendingInsertedMessages) ? conv.pendingInsertedMessages : [];
      conv.pendingInsertedMessages.push({
        text,
        createdAt: nowIso(),
        mode: state.settings.insertMode,
      });
      conv.pendingInsertedMessages = conv.pendingInsertedMessages.slice(-5);
    }
    if (result.ok && state.settings.insertMode === "inject_then_followup") {
      conv.queue.push({
        id: uid("queue"),
        text,
        attachments: attachedFileIds.slice(),
        createdAt: nowIso(),
        fromInsert: true,
      });
    }
    notify(
      ok ? "插入已触发" : "插入未生效",
      ok ? "已进入中途介入流程，等待当前任务收束。" : (abortResult.reason || "当前任务未进入可中途介入状态。"),
      ok ? "done" : "warn",
    );
  } catch (err) {
    addMessage({ role: "system_status", text: `插入失败：${err.message || err}`, status: "failed" });
    clearPendingInsertedMessages(conv);
  } finally {
    currentDraft = "";
    attachedFileIds = [];
    syncDraftToConversation();
  }
}

async function abortRun() {
  const archive = activeArchive();
  const account = activeAccount();
  const conv = activeConversation();
  if (!archive || !account) return;
  try {
    await fetchJson("/v1/chat/abort", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        archive_id: archive.archiveId || archive.id,
        group_id: archive.groupId,
        user_id: account.userId,
      }),
    });
  } catch {
    // Local abort still matters if the backend is unreachable.
  }
  if (activeController) activeController.abort();
  if (conv) conv.status = "interrupted";
  saveState();
  render();
}

function startMonitor() {
  if (!state.settings.monitorEnabled) return;
  const archive = activeArchive();
  const account = activeAccount();
  const run = activeConversation()?.workflowRuns?.[0];
  if (!archive?.archiveId || !account || !run?.traceId) return;
  stopMonitor();
  monitorController = new AbortController();
  const query = new URLSearchParams({
    archive_id: archive.archiveId || archive.id,
    group_id: archive.groupId || "",
    user_id: account.userId,
    trace_id: run?.traceId || "",
    heartbeat_sec: "5",
  });
  fetch(backend(`/v1/chat/monitor?${query}`), { signal: monitorController.signal })
    .then((resp) => {
      if (!resp.ok) throw new Error(`monitor ${resp.status}`);
      return parseSseStream(resp, handleMonitorEvent);
    })
    .catch((err) => {
      if (err?.name !== "AbortError") {
        addPassiveWorkflow("monitor", "监控流断开", "error", err.message || String(err));
      }
    });
}

function stopMonitor() {
  if (monitorController) monitorController.abort();
  monitorController = null;
}

function handleMonitorEvent(evt) {
  const conv = activeConversation();
  const run = conv?.workflowRuns?.[0];
  if (!run || run.status !== "running") return;
  const payloadTrace = evt.data?.trace_id || "";
  if (run.traceId && payloadTrace && payloadTrace !== run.traceId) return;
  if (run.traceId && !payloadTrace && evt.event !== "heartbeat" && evt.event !== "snapshot") return;
  if (evt.event === "heartbeat" || evt.event === "snapshot") {
    mergeMonitorSnapshot(evt.data || {});
    saveState();
    scheduleWorkflowRender();
    renderTranscript();
    return;
  }
  const data = evt.data || {};
  const helperId = data.helperId || data.helper_id || data.process_id || data.proc_id || data.task_id || "";
  const commandId = data.command_id || data.commandId || "";
  const mergeKey = helperId
    ? helperProgressMergeKey(data, evt.event)
    : (commandId ? `command:${commandId}` : workflowEventMergeKey(data, data.kind || evt.event));
  const kind = data.kind || evt.event;
  if (helperId && String(kind || "") === "helper_progress") {
    completePriorHelperProgress(run, helperId, mergeKey);
  }
  if (helperId && workflowNodeStatus(data.status, kind, evt.event, data) === "done") {
    completeHelperProgress(run, helperId);
  }
  upsertWorkflowNode(run, {
    mergeKey,
    kind,
    title: helperId ? helperProgressTitle(data, evt.event) : mainToolTitle(data, evt.event),
    status: workflowNodeStatus(data.status, kind, evt.event, data),
    commandId,
    helperId,
    taskId: data.task_id || "",
    helperKind: data.helper_kind || "",
    detail: workflowDetailText(data, evt.event),
  });
  updateStreamingAssistantCurrentActionFromRun(run);
  updateStreamingAssistantRound2PreviewFromRun(run);
  saveState();
  scheduleWorkflowRender();
  renderTranscript();
}

function mergeMonitorSnapshot(snapshot) {
  const conv = activeConversation();
  const run = conv?.workflowRuns?.[0];
  if (!run) return;
  const commands = snapshot.active_commands || [];
  const helpers = snapshot.active_helpers || [];
  const activeCommandIds = new Set();
  const activeHelperIds = new Set();
  run.commandCount = snapshot.active_command_count || commands.length || 0;
  run.helperCount = snapshot.active_helper_count || helpers.length || 0;
  for (const command of commands) {
    const commandId = command.command_id || command.id;
    if (!commandId) continue;
    activeCommandIds.add(commandId);
    upsertWorkflowNode(run, {
      kind: "command",
      title: command.title || command.command || "运行命令",
      status: "running",
      commandId,
      detail: JSON.stringify(command, null, 2),
    });
  }
  for (const helper of helpers) {
    const helperId = helper.proc_id || helper.process_id || helper.task_id || helper.helper_task_id;
    if (!helperId) continue;
    activeHelperIds.add(helperId);
    upsertWorkflowNode(run, {
      kind: `helper:${helper.helper_kind || "helper"}`,
      title: helperRootTitle(helper, helperId),
      status: "running",
      helperId,
      taskId: helper.task_id || helper.helper_task_id || "",
      helperKind: helper.helper_kind || "",
      detail: JSON.stringify(helper, null, 2),
    });
  }
  if (Array.isArray(snapshot.active_commands)) {
    for (const node of run.nodes || []) {
      if (node.commandId && !activeCommandIds.has(node.commandId) && node.status === "running") {
        node.status = "done";
        node.updatedAt = nowIso();
      }
    }
  }
  if (Array.isArray(snapshot.active_helpers)) {
    for (const node of run.nodes || []) {
      if (node.helperId && !activeHelperIds.has(node.helperId) && node.status === "running" && isHelperWorkflowKind(node.kind)) {
        node.status = "exited";
        node.updatedAt = nowIso();
        markHelperLifecycleExited(run, node.helperId);
      }
    }
  }
  updateStreamingAssistantCurrentActionFromRun(run);
  updateStreamingAssistantRound2PreviewFromRun(run);
}

async function abortCommand(commandId) {
  if (!commandId) return;
  try {
    const result = await fetchJson(`/v1/chat/commands/${encodeURIComponent(commandId)}/abort`, {
      method: "POST",
    });
    addPassiveWorkflow("command", "命令中断请求已发送", result.ok ? "done" : "error", JSON.stringify(result, null, 2));
  } catch (err) {
    addPassiveWorkflow("command", "命令中断失败", "error", err.message || String(err));
  }
}

function addPassiveWorkflow(kind, title, status, detail) {
  const conv = activeConversation();
  if (!conv) return;
  const run = conv.workflowRuns[0] || {
    id: uid("run"),
    traceId: "",
    status: "idle",
    startedAt: nowIso(),
    nodes: [],
  };
  if (!conv.workflowRuns.length) conv.workflowRuns.unshift(run);
  addWorkflowNode(run, { kind, title, status, detail });
  saveState();
  scheduleWorkflowRender();
}

async function refreshFiles() {
  const archive = activeArchive();
  if (!archive?.archiveId) return;
  try {
    const data = await fetchJson(`/v1/chat/files/${encodeURIComponent(archive.archiveId)}/${encodeURIComponent(archive.groupId)}`);
    state.files[archive.id] = data.items || [];
    saveState();
    render();
  } catch (err) {
    addMessage({ role: "system_status", text: `刷新文件失败：${err.message || err}`, status: "failed" });
  }
}

async function refreshArtifacts() {
  const archive = activeArchive();
  if (!archive?.archiveId) return;
  try {
    const data = await fetchJson(`/v1/chat/artifacts/${encodeURIComponent(archive.archiveId)}/${encodeURIComponent(archive.groupId)}`);
    state.artifacts[archive.id] = data.items || [];
    saveState();
    render();
  } catch (err) {
    addMessage({ role: "system_status", text: `刷新产物失败：${err.message || err}`, status: "failed" });
  }
}

function rememberArtifacts(files) {
  const archive = activeArchive();
  if (!archive || !Array.isArray(files) || !files.length) return;
  const existing = new Map((state.artifacts[archive.id] || []).map((item) => [item.download_url || item.url || item.rel_path || item.name, item]));
  for (const file of files) {
    const rel = file.rel_path || file.name || "";
    const url = file.download_url || file.url || "";
    const key = url || rel;
    if (!key) continue;
    existing.set(key, {
      id: file.id || `artifact:${rel || key}`,
      name: file.name || rel || key,
      rel_path: rel,
      workspace_path: rel,
      size: file.size || 0,
      status: "ready",
      kind: "artifact",
      download_url: url,
      local_path: file.local_path || "",
      created_at: Date.now() / 1000,
    });
  }
  state.artifacts[archive.id] = [...existing.values()].sort((a, b) => (b.created_at || 0) - (a.created_at || 0));
  state.ui.artifactPanelOpen = true;
}

async function uploadFiles(files) {
  const archive = activeArchive();
  const account = activeAccount();
  if (!archive || !account || !files?.length) return;
  try {
    await ensureBackendArchive(archive, account);
  } catch (err) {
    addMessage({ role: "system_status", text: `创建后端存档失败：${err.message || err}`, status: "failed" });
    return;
  }
  for (const file of files) {
    try {
      await uploadSingleBotFile(file, { autoAttach: true, notifySuccess: true });
    } catch {
      // uploadSingleBotFile already records a visible failed item or cancellation notice.
    }
  }
  saveState();
  render();
}

async function uploadSingleBotFile(file, { autoAttach = true, notifySuccess = true, sourceLabel = "文件" } = {}) {
  const archive = activeArchive();
  const account = activeAccount();
  if (!archive || !account || !file) throw new Error("缺少当前账号或存档");
  if (!archive.archiveId) await ensureBackendArchive(archive, account);
  if (file.size > LARGE_FILE_WARN_BYTES) {
    const ok = confirmAction(`${file.name} 大小为 ${fmtSize(file.size)}，上传和索引可能较慢。继续上传？`);
    if (!ok) throw new Error("用户取消上传");
  }
  const url =
    `/v1/chat/files/${encodeURIComponent(archive.archiveId)}/${encodeURIComponent(archive.groupId)}/upload` +
    `?filename=${encodeURIComponent(file.name)}` +
    `&user_id=${encodeURIComponent(account.userId)}` +
    `&user_name=${encodeURIComponent(account.displayName)}`;
  try {
    const uploadId = uid("upload");
    uploadProgress[uploadId] = { name: file.name, status: "uploading", percent: 0 };
    renderFilePanelOnly();
    const data = await uploadFileWithProgress(backend(url), file, uploadId);
    const nextFile = data.file || {};
    const savedFile = {
      ...nextFile,
      id: nextFile.id || uid("file"),
      name: nextFile.name || nextFile.file_name || file.name,
      size: nextFile.size ?? file.size,
      mime: nextFile.mime || file.type || "application/octet-stream",
      status: nextFile.status || "ready",
      local_preview_type: previewTypeForFile(nextFile.name ? nextFile : file),
    };
    state.files[archive.id] = [savedFile, ...(state.files[archive.id] || [])];
    if (autoAttach && !attachedFileIds.includes(savedFile.id)) attachedFileIds.push(savedFile.id);
    state.ui.filePanelOpen = true;
    syncDraftToConversation();
    delete uploadProgress[uploadId];
    renderFilePanelOnly();
    if (notifySuccess) notify(`${sourceLabel}已上传并附加到本次消息`, file.name, "done");
    return savedFile;
  } catch (err) {
    const failedId = uid("failed_file");
    state.files[archive.id] = [{
      id: failedId,
      name: file.name,
      size: file.size,
      mime: file.type || "application/octet-stream",
      status: "failed",
      uploadedAt: nowIso(),
      error: err.message || String(err),
    }, ...(state.files[archive.id] || [])];
    addMessage({ role: "system_status", text: `上传失败：${file.name}\n${err.message || err}`, status: "failed" });
    notify("文件上传失败", file.name, "error");
    for (const [id, item] of Object.entries(uploadProgress)) {
      if (item.name === file.name) delete uploadProgress[id];
    }
    renderFilePanelOnly();
    throw err;
  }
}

function uploadFileWithProgress(url, file, uploadId) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", url);
    xhr.setRequestHeader("Content-Type", file.type || "application/octet-stream");
    xhr.upload.onprogress = (event) => {
      if (!event.lengthComputable) return;
      uploadProgress[uploadId] = {
        name: file.name,
        status: "uploading",
        percent: Math.round((event.loaded / event.total) * 100),
      };
      renderFilePanelOnly();
    };
    xhr.onload = () => {
      if (xhr.status < 200 || xhr.status >= 300) {
        reject(new Error(xhr.responseText || `upload failed: ${xhr.status}`));
        return;
      }
      try {
        resolve(JSON.parse(xhr.responseText));
      } catch (err) {
        reject(err);
      }
    };
    xhr.onerror = () => reject(new Error("upload network error"));
    xhr.send(file);
  });
}

function previewTypeForFile(file) {
  const name = (file.name || file.file_name || "").toLowerCase();
  const mime = (file.mime || file.type || "").toLowerCase();
  if (mime.startsWith("image/") || /\.(png|jpg|jpeg|gif|webp|bmp)$/i.test(name)) return "image";
  if (mime === "application/pdf" || name.endsWith(".pdf")) return "pdf";
  if (mime.startsWith("audio/") || /\.(mp3|wav|ogg|m4a|flac)$/i.test(name)) return "audio";
  if (mime.startsWith("video/") || /\.(mp4|webm|mov)$/i.test(name)) return "video";
  if (/\.(txt|md|json|csv|tsv|log|py|js|ts|tsx|jsx|html|css|xml|yaml|yml|toml|ini)$/i.test(name)) return "text";
  return "download";
}

async function previewFile(fileId) {
  const archive = activeArchive();
  const file = (state.files[archive?.id] || []).find((f) => f.id === fileId);
  if (!file) return;
  state.ui.previewOpen = true;
  state.ui.previewFileId = fileId;
  state.ui.previewText = "";
  state.ui.previewStatus = "loading";
  saveState();
  render();
  const type = previewTypeForFile(file);
  if (type !== "text") {
    state.ui.previewStatus = "ready";
    saveState();
    render();
    return;
  }
  try {
    const resp = await fetch(backend(file.download_url));
    if (!resp.ok) throw new Error(`${resp.status}: ${await safeText(resp)}`);
    const text = await resp.text();
    state.ui.previewText = text.slice(0, 300000);
    state.ui.previewStatus = text.length > 300000 ? "truncated" : "ready";
  } catch (err) {
    state.ui.previewText = err.message || String(err);
    state.ui.previewStatus = "error";
  }
  saveState();
  render();
}

function closePreview() {
  state.ui.previewOpen = false;
  state.ui.previewFileId = "";
  state.ui.previewText = "";
  state.ui.previewStatus = "idle";
  saveState();
  render();
}

async function deleteFile(fileId) {
  const archive = activeArchive();
  const file = (state.files[archive?.id] || []).find((f) => f.id === fileId);
  if (!archive || !file) return;
  if (!confirmAction(`删除 bot 文件区文件：${file.name}？`)) return;
  try {
    if (archive.archiveId) {
      await fetchJson(`/v1/chat/files/${encodeURIComponent(archive.archiveId)}/${encodeURIComponent(archive.groupId)}/${encodeURIComponent(fileId)}`, {
        method: "DELETE",
      });
    }
    state.files[archive.id] = (state.files[archive.id] || []).filter((f) => f.id !== fileId);
    attachedFileIds = attachedFileIds.filter((id) => id !== fileId);
    saveState();
    render();
  } catch (err) {
    addMessage({ role: "system_status", text: `删除文件失败：${err.message || err}`, status: "failed" });
  }
}

function attachFile(fileId) {
  if (!attachedFileIds.includes(fileId)) attachedFileIds.push(fileId);
  syncDraftToConversation();
  saveState();
  render();
}

function detachFile(fileId) {
  attachedFileIds = attachedFileIds.filter((id) => id !== fileId);
  syncDraftToConversation();
  saveState();
  render();
}

function artifactRefs() {
  const archive = activeArchive();
  return archive ? (state.artifacts[archive.id] || []) : [];
}

async function refreshProjectTree(path = state.ui.projectTreePath || ".") {
  const archive = activeArchive();
  const account = activeAccount();
  if (!archive?.currentDir || !archive.projectId || !account) {
    state.ui.projectTreeStatus = "当前存档未绑定项目目录";
    state.ui.projectTree = [];
    saveState();
    render();
    return;
  }
  state.ui.projectPanelOpen = true;
  state.ui.projectTreeStatus = "loading";
  state.ui.projectTreePath = path || ".";
  saveState();
  render();
  try {
    const query = new URLSearchParams({
      user_id: account.userId,
      path: state.ui.projectTreePath,
      max_depth: "4",
      limit: "800",
    });
    const data = await fetchJson(`/v1/agent/projects/${encodeURIComponent(archive.projectId)}/tree?${query}`);
    state.ui.projectTree = data.items || [];
    state.ui.projectTreeStatus = data.truncated ? "truncated" : "ready";
  } catch (err) {
    state.ui.projectTreeStatus = err.message || String(err);
    state.ui.projectTree = [];
  }
  saveState();
  render();
}

async function previewProjectFile(path) {
  const archive = activeArchive();
  const account = activeAccount();
  if (!archive?.projectId || !account || !path) return;
  state.ui.projectPreviewPath = path;
  state.ui.projectPreviewText = "";
  state.ui.projectPreviewStatus = "loading";
  saveState();
  render();
  try {
    const query = new URLSearchParams({ user_id: account.userId, path, max_chars: "180000" });
    const data = await fetchJson(`/v1/agent/projects/${encodeURIComponent(archive.projectId)}/file?${query}`);
    state.ui.projectPreviewText = data.content || "";
    state.ui.projectPreviewStatus = data.ok ? `${data.type || "file"}${data.truncated ? " truncated" : ""}` : (data.error || "failed");
  } catch (err) {
    state.ui.projectPreviewText = err.message || String(err);
    state.ui.projectPreviewStatus = "error";
  }
  saveState();
  render();
}

async function searchProject() {
  const archive = activeArchive();
  const account = activeAccount();
  const queryText = (state.ui.projectSearch || "").trim();
  if (!archive?.projectId || !account || !queryText) return;
  state.ui.projectPreviewStatus = "searching";
  saveState();
  render();
  try {
    const query = new URLSearchParams({
      user_id: account.userId,
      query: queryText,
      path: state.ui.projectTreePath || ".",
      limit: "200",
    });
    const data = await fetchJson(`/v1/agent/projects/${encodeURIComponent(archive.projectId)}/search?${query}`);
    state.ui.projectSearchResults = data.matches || [];
    state.ui.projectPreviewStatus = data.truncated ? "search truncated" : "search ready";
  } catch (err) {
    state.ui.projectSearchResults = [];
    state.ui.projectPreviewStatus = err.message || String(err);
  }
  saveState();
  render();
}

async function runProjectCommand() {
  const archive = activeArchive();
  const account = activeAccount();
  const command = (state.ui.projectCommand || "").trim();
  if (!archive?.projectId || !account || !command) return;
  state.ui.projectCommandOutput = "running...";
  saveState();
  render();
  try {
    const data = await fetchJson(`/v1/agent/projects/${encodeURIComponent(archive.projectId)}/run?user_id=${encodeURIComponent(account.userId)}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ command, cwd: state.ui.projectTreePath || ".", timeout_sec: 120 }),
    });
    state.ui.projectCommandOutput = [
      `$ ${data.command}`,
      `cwd=${data.cwd} returncode=${data.returncode} elapsed=${data.elapsed_sec}s timeout=${data.timed_out}`,
      "",
      data.stdout || "",
      data.stderr ? `\n[stderr]\n${data.stderr}` : "",
    ].join("\n");
  } catch (err) {
    state.ui.projectCommandOutput = err.message || String(err);
  }
  saveState();
  render();
}

async function loadProjectDiff() {
  const archive = activeArchive();
  const account = activeAccount();
  const path = (state.ui.projectDiffPath || state.ui.projectPreviewPath || "").trim();
  const comparePath = (state.ui.projectDiffComparePath || "").trim();
  if (!archive?.projectId || !account || !path) return;
  state.ui.projectDiffText = "loading diff...";
  saveState();
  render();
  try {
    const query = new URLSearchParams({
      user_id: account.userId,
      path,
      compare_path: comparePath,
      max_chars: "120000",
    });
    const data = await fetchJson(`/v1/agent/projects/${encodeURIComponent(archive.projectId)}/diff?${query}`);
    state.ui.projectDiffText = data.binary
      ? `binary changed=${data.changed}`
      : (data.diff || (data.changed ? "changed, but diff is empty" : "no changes"));
    state.ui.projectPreviewText = state.ui.projectDiffText;
    state.ui.projectPreviewPath = `diff: ${path}${comparePath ? ` -> ${comparePath}` : ""}`;
    state.ui.projectPreviewStatus = data.truncated ? "diff truncated" : "diff ready";
  } catch (err) {
    state.ui.projectDiffText = err.message || String(err);
    state.ui.projectPreviewText = state.ui.projectDiffText;
    state.ui.projectPreviewStatus = "diff error";
  }
  saveState();
  render();
}

function explainCurrentDiff() {
  const text = state.ui.projectDiffText || state.ui.projectPreviewText || "";
  if (!text.trim()) return;
  addDraftText(`请解释这组 diff 的目的、风险和需要验证的地方：\n\n${text.slice(0, 6000)}`);
}

async function abortAndClearQueue() {
  const conv = activeConversation();
  if (conv) conv.queue = [];
  await abortRun();
  saveState();
  render();
}

function fileRefs(ids) {
  const archive = activeArchive();
  const files = state.files[archive?.id] || [];
  return ids.map((id) => files.find((f) => f.id === id)).filter(Boolean);
}

function findTemplate(templateId) {
  return PROMPT_TEMPLATES.find((item) => item.id === templateId);
}

function applyTemplate(templateId, action = "draft") {
  const template = findTemplate(templateId);
  if (!template) return;
  if (action === "queue") queueTemplateText(template.text);
  else if (action === "send") {
    currentDraft = template.text;
    sendMessage("normal");
  } else {
    addDraftText(template.text);
  }
}

function fileQuickAction(fileId, action) {
  const file = fileRefs([fileId])[0];
  if (!file) return;
  attachFile(fileId);
  const prompts = {
    summary: `请总结这个文件「${file.name}」，列出关键点、异常点和需要注意的问题。`,
    extract: `请提取这个文件「${file.name}」里的关键数据，必要时整理成表格。`,
    report: `请基于这个文件「${file.name}」生成结构清晰的报告，区分事实、推断和建议。`,
    log: `请分析这个日志/错误文件「${file.name}」，按严重程度归类问题并给出修复建议。`,
  };
  addDraftText(prompts[action] || prompts.summary);
}

function nodeQuickAction(nodeId, action = "fix") {
  const conv = activeConversation();
  const node = conv?.workflowRuns?.[0]?.nodes?.find((item) => item.id === nodeId);
  if (!node) return;
  const text = action === "explain"
    ? `请解释这个工作流节点的含义、风险和下一步：\n${node.title}\n${node.detail || ""}`
    : `刚才这个工作流节点显示了问题，请基于其输出定位原因并修复或给出下一步：\n${node.title}\n${node.detail || ""}`;
  addDraftText(text.slice(0, 6000));
}

function copyText(text) {
  navigator.clipboard?.writeText(text || "").then(
    () => notify("已复制", "", "done"),
    () => notify("复制失败", "浏览器拒绝剪贴板访问", "warn")
  );
}

function confirmAction(message) {
  if (state.settings.reduceConfirmations) return true;
  return confirm(message);
}

function detailTail(detail) {
  const text = String(detail || "");
  const lines = text.split(/\r?\n/);
  const limit = Math.max(20, Math.min(2000, Number(state.settings.logTailLines || 200)));
  if (lines.length <= limit) return text;
  return `[tail ${limit} / ${lines.length} lines]\n${lines.slice(-limit).join("\n")}`;
}

function setQueueAutoRun(enabled) {
  const conv = activeConversation();
  if (!conv) return;
  conv.autoRunQueue = enabled;
  saveState();
  render();
}

function createAccount() {
  const displayName = prompt("账号名称", "本地用户");
  if (!displayName) return;
  const userId = prompt("用户 ID", `local-${Date.now()}`);
  if (!userId) return;
  state.accounts.push({ userId, displayName, createdAt: nowIso(), lastUsedAt: nowIso() });
  state.activeAccountId = userId;
  const archive = makeLocalArchive(userId, "默认对话", "");
  state.archives.push(archive);
  state.conversations[archive.id] = emptyConversation(archive.id);
  state.files[archive.id] = [];
  state.artifacts[archive.id] = [];
  state.activeArchiveId = archive.id;
  saveState();
  render();
}

function renameAccount() {
  const account = activeAccount();
  if (!account) return;
  const next = prompt("账号名称", account.displayName);
  if (!next) return;
  account.displayName = next;
  account.lastUsedAt = nowIso();
  saveState();
  render();
}

function deleteLocalAccount() {
  const account = activeAccount();
  if (!account) return;
  if (state.accounts.length <= 1) {
    alert("至少保留一个账号。");
    return;
  }
  if (!confirmAction(`删除本地账号：${account.displayName}？后端存档不会被删除。`)) return;
  const archiveIds = state.archives.filter((a) => a.userId === account.userId).map((a) => a.id);
  state.accounts = state.accounts.filter((a) => a.userId !== account.userId);
  state.archives = state.archives.filter((a) => a.userId !== account.userId);
  for (const id of archiveIds) {
    delete state.conversations[id];
    delete state.files[id];
    delete state.artifacts[id];
  }
  state.activeAccountId = state.accounts[0].userId;
  const next = state.archives.find((a) => a.userId === state.activeAccountId);
  state.activeArchiveId = next?.id || "";
  startupRecovered = false;
  saveState();
  render();
  recoverFromBackend();
}

function createArchive() {
  const account = activeAccount();
  if (!account) return;
  const title = prompt("存档名称", "新存档");
  if (!title) return;
  const currentDir = prompt("项目目录路径，可为空", "") || "";
  const archive = makeLocalArchive(account.userId, title, currentDir);
  state.archives.push(archive);
  state.conversations[archive.id] = emptyConversation(archive.id);
  state.files[archive.id] = [];
  state.artifacts[archive.id] = [];
  state.activeArchiveId = archive.id;
  saveState();
  render();
}

function duplicateConversationTab() {
  const archive = activeArchive();
  if (!archive) return;
  const copy = makeLocalArchive(archive.userId, `${archive.title} 副本`, archive.currentDir);
  copy.archiveId = archive.archiveId;
  copy.groupId = archive.groupId;
  copy.projectId = `${archive.projectId || "project"}_${Date.now()}`;
  copy.localOnly = archive.localOnly;
  state.archives.push(copy);
  state.conversations[copy.id] = emptyConversation(copy.id);
  state.files[copy.id] = [...(state.files[archive.id] || [])];
  state.artifacts[copy.id] = [...(state.artifacts[archive.id] || [])];
  state.activeArchiveId = copy.id;
  saveState();
  render();
}

async function syncArchive() {
  const archive = activeArchive();
  const account = activeAccount();
  if (!archive || !account) return;
  try {
    await ensureBackendArchive(archive, account);
    await refreshFiles();
    await refreshArtifacts();
  } catch (err) {
    addMessage({ role: "system_status", text: `同步失败：${err.message || err}`, status: "failed" });
  }
}

async function renameArchive() {
  const archive = activeArchive();
  if (!archive) return;
  const next = prompt("存档名称", archive.title);
  if (!next) return;
  archive.title = next;
  archive.lastUsedAt = nowIso();
  saveState();
  render();
  if (!archive.localOnly) await updateBackendProject();
}

async function editArchiveDir() {
  const archive = activeArchive();
  if (!archive) return;
  const next = prompt("项目目录路径，可为空", archive.currentDir || "");
  if (next === null) return;
  archive.currentDir = next;
  archive.localOnly = true;
  archive.archiveId = "";
  saveState();
  render();
}

async function updateBackendProject() {
  const archive = activeArchive();
  const account = activeAccount();
  if (!archive?.projectId || !account || archive.localOnly) return;
  try {
    const data = await fetchJson(`/v1/agent/projects/${encodeURIComponent(archive.projectId)}?user_id=${encodeURIComponent(account.userId)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: archive.title, current_dir: archive.currentDir }),
    });
    adoptProject(archive, data);
  } catch {
    // Local metadata can still be used; chat will resync on send.
  }
}

function deleteLocalArchive() {
  const archive = activeArchive();
  if (!archive) return;
  if (!confirmAction(`仅删除本地前端记录：${archive.title}？后端存档不会被删除。`)) return;
  state.archives = state.archives.filter((a) => a.id !== archive.id);
  delete state.conversations[archive.id];
  delete state.files[archive.id];
  delete state.artifacts[archive.id];
  const next = state.archives.find((a) => a.userId === state.activeAccountId);
  state.activeArchiveId = next?.id || "";
  if (!next) {
    const account = activeAccount();
    if (account) {
      const newArchive = makeLocalArchive(account.userId, "默认对话", "");
      state.archives.push(newArchive);
      state.conversations[newArchive.id] = emptyConversation(newArchive.id);
      state.files[newArchive.id] = [];
      state.artifacts[newArchive.id] = [];
      state.activeArchiveId = newArchive.id;
    }
  }
  saveState();
  render();
}

function archiveLocalOnly(action) {
  const archive = activeArchive();
  if (!archive) return;
  if (action === "toggle_pin") archive.pinned = !archive.pinned;
  if (action === "toggle_archive") archive.archived = !archive.archived;
  archive.lastUsedAt = nowIso();
  saveState();
  render();
}

function clearTranscript() {
  const conv = activeConversation();
  if (!conv || !confirmAction("清空当前前端 transcript？后端记忆不会删除。")) return;
  conv.messages = [];
  conv.workflowRuns = [];
  conv.queue = [];
  conv.status = "idle";
  saveState();
  render();
}

function exportMarkdown() {
  const archive = activeArchive();
  const conv = activeConversation();
  if (!archive || !conv) return;
  const lines = [`# ${archive.title}`, "", `- 导出时间：${fmtTime(nowIso())}`, `- 项目目录：${archive.currentDir || "空"}`, ""];
  for (const msg of conv.messages) {
    lines.push(`## ${roleLabel(msg.role)} ${fmtTime(msg.createdAt)}`, "");
    lines.push(msg.text || "");
    if (msg.attachments?.length) {
      lines.push("", `附件：${fileRefs(msg.attachments).map((f) => f.name).join(", ")}`);
    }
    lines.push("");
  }
  downloadBlob(`${archive.title || "bot"}-${Date.now()}.md`, lines.join("\n"), "text/markdown;charset=utf-8");
}

function exportWorkflow(format = "json") {
  const archive = activeArchive();
  const conv = activeConversation();
  const run = conv?.workflowRuns?.[0];
  if (!archive || !run) {
    alert("当前没有可导出的工作流。");
    return;
  }
  if (format === "md") {
    const lines = [
      `# ${archive.title} 工作流`,
      "",
      `- 导出时间：${fmtTime(nowIso())}`,
      `- trace：${run.traceId || "无"}`,
      `- 状态：${run.status || "unknown"}`,
      "",
    ];
    for (const node of run.nodes || []) {
      lines.push(`## ${node.title || node.kind || "事件"}`);
      lines.push("");
      lines.push(`- 类型：${node.kind || ""}`);
      lines.push(`- 状态：${node.status || ""}`);
      lines.push(`- 时间：${fmtTime(node.createdAt)}`);
      if (node.detail) {
        lines.push("", "```json", String(node.detail), "```");
      }
      lines.push("");
    }
    downloadBlob(`${archive.title || "bot"}-workflow-${Date.now()}.md`, lines.join("\n"), "text/markdown;charset=utf-8");
    return;
  }
  downloadBlob(
    `${archive.title || "bot"}-workflow-${Date.now()}.json`,
    JSON.stringify({ archive, run }, null, 2),
    "application/json;charset=utf-8"
  );
}

function exportStateJson() {
  downloadBlob(`bot-agent-state-${Date.now()}.json`, JSON.stringify(state, null, 2), "application/json;charset=utf-8");
}

function importStateJson(file) {
  if (!file) return;
  const reader = new FileReader();
  reader.onload = () => {
    try {
      const parsed = JSON.parse(String(reader.result || "{}"));
      const next = normalizeState({ ...defaultState, ...parsed });
      if (!confirmAction("导入会替换当前前端本地状态，后端数据不会被删除。继续？")) return;
      state = next;
      attachedFileIds = [];
      currentDraft = "";
      startupRecovered = false;
      saveState();
      render();
      recoverFromBackend();
    } catch (err) {
      alert(`导入失败：${err.message || err}`);
    }
  };
  reader.readAsText(file, "utf-8");
}

function promptImportStateJson() {
  const input = document.createElement("input");
  input.type = "file";
  input.accept = "application/json,.json";
  input.addEventListener("change", () => importStateJson(input.files?.[0]));
  input.click();
}

function cleanupLocalData() {
  const conv = activeConversation();
  if (!conv) return;
  const maxRuns = Math.max(1, Number(state.settings.maxWorkflowRuns || DEFAULT_MAX_WORKFLOW_RUNS));
  const beforeRuns = conv.workflowRuns.length;
  conv.workflowRuns = conv.workflowRuns.slice(0, maxRuns);
  conv.messages = conv.messages.filter((msg) => msg.status !== "failed" || msg.role !== "assistant" || msg.text);
  saveState();
  render();
  addMessage({
    role: "system_status",
    text: `已清理当前存档本地数据：工作流 ${beforeRuns} -> ${conv.workflowRuns.length}。后端记录未修改。`,
    status: "done",
  });
}

function openCommandPalette() {
  const cmd = prompt("命令：new / rename / dir / sync / files / clear / export / workflow / state / import / cleanup / settings / abort / 模板关键词", "");
  if (!cmd) return;
  const key = cmd.trim().toLowerCase();
  if (key === "new") createArchive();
  else if (key === "rename") renameArchive();
  else if (key === "dir") editArchiveDir();
  else if (key === "sync") syncArchive();
  else if (key === "files") toggleFilePanel();
  else if (key === "project") toggleProjectPanel();
  else if (key === "clear") clearTranscript();
  else if (key === "export") exportMarkdown();
  else if (key === "workflow") exportWorkflow("json");
  else if (key === "state") exportStateJson();
  else if (key === "import") promptImportStateJson();
  else if (key === "cleanup") cleanupLocalData();
  else if (key === "settings") toggleSettings();
  else if (key === "abort") abortRun();
  else if (key === "queue") clearQueue();
  else if (key === "trace") copyText(activeConversation()?.workflowRuns?.[0]?.traceId || "");
  else if (key === "search") {
    $("#transcriptSearch")?.focus();
  }
  else {
    const template = PROMPT_TEMPLATES.find((item) =>
      `${item.group} ${item.title} ${item.id}`.toLowerCase().includes(key)
    );
    if (template) addDraftText(template.text);
  }
}

function toggleFilePanel() {
  state.ui.filePanelOpen = !state.ui.filePanelOpen;
  saveState();
  render();
}

function toggleProjectPanel() {
  state.ui.projectPanelOpen = !state.ui.projectPanelOpen;
  saveState();
  render();
  if (state.ui.projectPanelOpen && !state.ui.projectTree.length) refreshProjectTree();
}

function toggleSettings() {
  state.ui.settingsOpen = !state.ui.settingsOpen;
  saveState();
  render();
}

function render() {
  const workflowScroll = workflowScrollSnapshot();
  const transcriptScroll = transcriptScrollSnapshot();
  document.documentElement.style.setProperty("--app-font-size", `${state.settings.fontSize || 14}px`);
  document.documentElement.style.setProperty("--code-font", state.settings.codeFont || "Consolas, Courier New, monospace");
  $("#app").innerHTML = `
    <div class="app-shell ${state.settings.sidebarOpen === false ? "sidebar-closed" : ""} ${state.settings.workflowOpen === false ? "workflow-closed" : ""}">
      <aside class="sidebar">${renderSidebar()}</aside>
      <main class="main">
        ${renderTopbar()}
        <div class="transcript" id="transcript">${renderTranscriptHtml()}</div>
        ${renderComposer()}
      </main>
      <aside class="workflow" id="workflowPane">${renderWorkflow()}</aside>
      ${renderFilePanel()}
      ${renderArtifactPanel()}
      ${renderProjectPanel()}
      ${renderSettingsPanel()}
      ${renderPreviewPanel()}
      ${renderNodeDetailsPanel()}
      <div id="notifications">${renderNotifications()}</div>
    </div>
  `;
  bindEvents();
  enhanceButtonTooltips();
  restoreWorkflowScroll(workflowScroll);
  restoreTranscriptScroll(transcriptScroll);
}

function transcriptScrollSnapshot() {
  const el = $("#transcript");
  if (!el) return transcriptLastScrollSnapshot;
  const bottomGap = el.scrollHeight - el.scrollTop - el.clientHeight;
  const viewport = el.getBoundingClientRect();
  const firstVisible = Array.from(el.querySelectorAll("[data-message-id]")).find((node) => {
    const rect = node.getBoundingClientRect();
    return rect.bottom >= viewport.top + 1;
  });
  const snapshot = {
    top: el.scrollTop,
    stickToBottom: bottomGap < 96,
    anchorId: firstVisible?.dataset?.messageId || "",
    anchorOffset: firstVisible ? firstVisible.getBoundingClientRect().top - viewport.top : 0,
  };
  if (snapshot.top > 0 || snapshot.stickToBottom || !transcriptLastScrollSnapshot) {
    transcriptLastScrollSnapshot = snapshot;
  }
  return snapshot;
}

function restoreTranscriptScroll(snapshot) {
  if (!snapshot) return;
  const apply = () => {
    const el = $("#transcript");
    if (!el) return;
    if (snapshot.stickToBottom) {
      el.scrollTop = el.scrollHeight;
      transcriptLastScrollSnapshot = transcriptScrollSnapshot();
      return;
    }
    if (snapshot.anchorId) {
      const anchor = el.querySelector(`[data-message-id="${cssEscape(snapshot.anchorId)}"]`);
      if (anchor) {
        const viewport = el.getBoundingClientRect();
        const rect = anchor.getBoundingClientRect();
        const nextTop = el.scrollTop + rect.top - viewport.top - (snapshot.anchorOffset || 0);
        const maxTop = Math.max(0, el.scrollHeight - el.clientHeight);
        el.scrollTop = Math.min(Math.max(0, nextTop), maxTop);
        transcriptLastScrollSnapshot = transcriptScrollSnapshot();
        return;
      }
    }
    const maxTop = Math.max(0, el.scrollHeight - el.clientHeight);
    el.scrollTop = Math.min(Math.max(0, snapshot.top), maxTop);
    transcriptLastScrollSnapshot = transcriptScrollSnapshot();
  };
  apply();
  requestAnimationFrame(apply);
  setTimeout(apply, 0);
  setTimeout(apply, 50);
}

function workflowScrollSnapshot() {
  const list = $("#workflowPane .workflow-list");
  if (!list) return workflowLastScrollSnapshot;
  const bottomGap = list.scrollHeight - list.scrollTop - list.clientHeight;
  const listRect = list.getBoundingClientRect();
  const visibleNodes = Array.from(list.querySelectorAll("[data-node-id]")).filter((node) => {
    const rect = node.getBoundingClientRect();
    return rect.bottom >= listRect.top + 1 && rect.top <= listRect.bottom - 1;
  });
  const topEntering = visibleNodes
    .filter((node) => node.getBoundingClientRect().top >= listRect.top - 1)
    .sort((a, b) => {
      const ar = a.getBoundingClientRect();
      const br = b.getBoundingClientRect();
      const delta = ar.top - br.top;
      if (Math.abs(delta) > 1) return delta;
      return Number(b.dataset.nodeDepth || 0) - Number(a.dataset.nodeDepth || 0);
    });
  const containingTop = visibleNodes
    .filter((node) => {
      const rect = node.getBoundingClientRect();
      return rect.top < listRect.top && rect.bottom > listRect.top;
    })
    .sort((a, b) => Number(b.dataset.nodeDepth || 0) - Number(a.dataset.nodeDepth || 0));
  const firstVisible = topEntering[0] || containingTop[0] || visibleNodes[0];
  const snapshot = {
    top: list.scrollTop,
    height: list.scrollHeight,
    stickToBottom: bottomGap < 96,
    pointerActive: workflowPointerActive,
    anchorId: firstVisible?.dataset?.nodeId || "",
    anchorOffset: firstVisible ? firstVisible.getBoundingClientRect().top - listRect.top : 0,
  };
  if (snapshot.top > 0 || snapshot.stickToBottom || !workflowLastScrollSnapshot) {
    workflowLastScrollSnapshot = snapshot;
  }
  return snapshot;
}

function restoreWorkflowScroll(snapshot) {
  if (!snapshot) return;
  const seq = ++workflowScrollRestoreSeq;
  const apply = () => {
    if (seq !== workflowScrollRestoreSeq) return;
    const list = $("#workflowPane .workflow-list");
    if (!list) return;
    if (snapshot.pointerActive || workflowPointerActive) {
      const maxTop = Math.max(0, list.scrollHeight - list.clientHeight);
      list.scrollTop = Math.min(Math.max(0, snapshot.top), maxTop);
      workflowLastScrollSnapshot = snapshot;
      return;
    }
    if (snapshot.stickToBottom) {
      list.scrollTop = list.scrollHeight;
      workflowLastScrollSnapshot = workflowScrollSnapshot();
      return;
    }
    if (snapshot.anchorId) {
      const anchor = list.querySelector(`[data-node-id="${cssEscape(snapshot.anchorId)}"]`);
      if (anchor) {
        const listRect = list.getBoundingClientRect();
        const anchorRect = anchor.getBoundingClientRect();
        const nextTop = list.scrollTop + anchorRect.top - listRect.top - (snapshot.anchorOffset || 0);
        const maxTop = Math.max(0, list.scrollHeight - list.clientHeight);
        list.scrollTop = Math.min(Math.max(0, nextTop), maxTop);
        workflowLastScrollSnapshot = workflowScrollSnapshot();
        return;
      }
    }
    const maxTop = Math.max(0, list.scrollHeight - list.clientHeight);
    list.scrollTop = Math.min(Math.max(0, snapshot.top), maxTop);
    workflowLastScrollSnapshot = workflowScrollSnapshot();
  };
  apply();
  requestAnimationFrame(apply);
  setTimeout(apply, 0);
  setTimeout(apply, 50);
}

function renderNotificationsOnly() {
  const el = $("#notifications");
  if (!el) return;
  el.innerHTML = renderNotifications();
  bindNotificationEvents();
  enhanceButtonTooltips(el);
}

function renderSidebar() {
  const account = activeAccount();
  const archiveSearch = (state.ui.archiveSearch || "").trim().toLowerCase();
  const archives = state.archives
    .filter((a) => a.userId === account?.userId)
    .filter((a) => {
      if (!archiveSearch) return !a.archived;
      return `${a.title || ""}\n${a.currentDir || ""}`.toLowerCase().includes(archiveSearch);
    })
    .sort((a, b) => Number(Boolean(b.pinned)) - Number(Boolean(a.pinned)));
  return `
    <div class="section">
      <div class="section-title">
        <span>账号</span>
        <div class="row"><button id="newAccountBtn">新建</button><button id="renameAccountBtn">改名</button><button class="danger" id="deleteAccountBtn">删除</button></div>
      </div>
      <select class="account-select" id="accountSelect">
        ${state.accounts.map((a) => `<option value="${escapeHtml(a.userId)}" ${a.userId === state.activeAccountId ? "selected" : ""}>${escapeHtml(a.displayName)}</option>`).join("")}
      </select>
    </div>
    <div class="section">
      <div class="section-title">
        <span>存档</span>
        <div class="row"><button id="newArchiveBtn">新建</button><button id="duplicateTabBtn">新窗口</button></div>
      </div>
      <input id="archiveSearchInput" class="wide-input" placeholder="搜索存档/目录；输入 archived 查看归档" value="${escapeHtml(state.ui.archiveSearch || "")}" />
      <div class="archive-list">${archives.map(renderArchiveItem).join("") || `<div class="empty">没有存档</div>`}</div>
    </div>
    <div class="section">
      <div class="section-title"><span>对话搜索</span></div>
      <input id="transcriptSearch" class="wide-input" placeholder="搜索当前 transcript" value="${escapeHtml(state.ui.transcriptSearch || "")}" />
    </div>
    <div class="section">
      <div class="section-title"><span>本地状态</span></div>
      <div class="small">队列：${activeConversation()?.queue.length || 0}</div>
      <div class="small">文件：${(state.files[activeArchive()?.id] || []).length}</div>
      <div class="small">后端：${escapeHtml(state.settings.backendUrl)}</div>
      <div class="small">持久化：${idbReady ? "IndexedDB" : "localStorage"}</div>
    </div>
  `;
}

function renderArchiveItem(archive) {
  const conv = state.conversations[archive.id] || emptyConversation(archive.id);
  return `
    <div class="archive-item ${archive.id === state.activeArchiveId ? "active" : ""}" data-archive-id="${escapeHtml(archive.id)}">
      <div class="archive-title">${escapeHtml(archive.title)}</div>
      <div class="archive-path">${escapeHtml(archive.currentDir || "空目录：普通 bot 模式")}</div>
      <div class="archive-path">状态：${escapeHtml(conv.status)} · 队列 ${conv.queue.length} ${archive.pinned ? "· pinned" : ""} ${archive.archived ? "· archived" : ""}</div>
    </div>
  `;
}

function renderNotifications() {
  const items = state.ui.notifications || [];
  if (!items.length) return "";
  return `<div class="toast-stack">${items.map((item) => `
    <div class="toast ${escapeHtml(item.kind || "info")}" data-notice-id="${escapeHtml(item.id)}">
      <div class="toast-title">${escapeHtml(item.title)}</div>
      ${item.detail ? `<div class="toast-detail">${escapeHtml(item.detail)}</div>` : ""}
      <button class="mini" data-dismiss-notice="${escapeHtml(item.id)}">关闭</button>
    </div>
  `).join("")}</div>`;
}

function renderTopbar() {
  const archive = activeArchive();
  const conv = activeConversation();
  return `
    <div class="topbar">
      <div class="top-title">
        <strong>${escapeHtml(archive?.title || "未选择存档")}</strong>
        <span class="small">${escapeHtml(archive?.currentDir || "未绑定项目目录，作为成熟对话 bot 使用")}</span>
      </div>
      <div class="top-actions">
        <button id="toggleSidebarBtn">侧栏</button>
        <button id="toggleWorkflowBtn">工作流</button>
        <span class="status-pill ${conv?.status === "running" ? "running" : conv?.status === "error" ? "error" : ""}">${escapeHtml(conv?.status || "idle")}</span>
        <button id="recoverBtn">恢复</button>
        <button id="syncArchiveBtn">同步</button>
        <button id="renameArchiveBtn">改名</button>
        <button id="editDirBtn">目录</button>
        <button id="pinArchiveBtn">固定</button>
        <button id="archiveArchiveBtn">归档</button>
        <button id="projectPanelBtn">项目</button>
        <button id="filePanelBtn">文件</button>
        <button id="artifactPanelBtn">产物</button>
        <button id="settingsBtn">设置</button>
        <button class="danger" id="abortBtn" ${conv?.status === "running" ? "" : "disabled"}>中断</button>
        <button class="danger" id="abortClearBtn" ${conv?.status === "running" || conv?.queue?.length ? "" : "disabled"}>停止并清队列</button>
      </div>
    </div>
  `;
}

function renderQuickTemplates() {
  const grouped = new Map();
  for (const item of PROMPT_TEMPLATES) {
    if (!grouped.has(item.group)) grouped.set(item.group, []);
    grouped.get(item.group).push(item);
  }
  return `
    <div class="quick-templates">
      ${[...grouped.entries()].map(([group, items]) => `
        <div class="template-group">
          <span class="small">${escapeHtml(group)}</span>
          ${items.map((item) => `
            <button class="template-chip" data-template-id="${escapeHtml(item.id)}" title="${escapeHtml(item.text)}">${escapeHtml(item.title)}</button>
          `).join("")}
        </div>
      `).join("")}
    </div>
  `;
}

function renderTranscriptHtml() {
  const conv = activeConversation();
  if (!conv?.messages.length) return `<div class="empty">开始和 bot 对话。可以绑定项目目录，也可以留空作为普通本地助手使用。</div>`;
  const search = (state.ui.transcriptSearch || "").trim().toLowerCase();
  const messages = search
    ? conv.messages.filter((msg) => `${roleLabel(msg.role)}\n${msg.text || ""}`.toLowerCase().includes(search))
    : conv.messages;
  if (!messages.length) return `<div class="empty">当前搜索没有匹配消息</div>`;
  return messages.map((msg) => renderMessage(msg, search)).join("");
}

function renderTranscript() {
  const el = $("#transcript");
  if (el) {
    const snapshot = transcriptScrollSnapshot();
    el.innerHTML = renderTranscriptHtml();
    bindTranscriptEvents();
    enhanceButtonTooltips(el);
    restoreTranscriptScroll(snapshot);
  }
}

function roleLabel(role) {
  if (role === "assistant") return "bot";
  if (role === "inserted_user") return "插入";
  if (role === "system_status") return "状态";
  if (role === "user") return "你";
  return role || "系统";
}

function highlightText(text, search) {
  const value = String(text || "");
  if (!search) return escapeHtml(value);
  const lower = value.toLowerCase();
  const idx = lower.indexOf(search);
  if (idx < 0) return escapeHtml(value);
  return `${escapeHtml(value.slice(0, idx))}<mark>${escapeHtml(value.slice(idx, idx + search.length))}</mark>${escapeHtml(value.slice(idx + search.length))}`;
}

function safeMarkdownUrl(url, { image = false } = {}) {
  const value = String(url || "").trim();
  if (!value) return "";
  if (value.startsWith("//")) return "";
  if (value.startsWith("#") || value.startsWith("/") || value.startsWith("./") || value.startsWith("../")) return value;
  if (image && value.startsWith("data:image/")) return value;
  try {
    const parsed = new URL(value, window.location.href);
    const allowed = image ? ["http:", "https:", "blob:"] : ["http:", "https:", "mailto:"];
    return allowed.includes(parsed.protocol) ? value : "";
  } catch {
    return "";
  }
}

function renderInlineMarkdown(text) {
  const tokens = [];
  const token = (html) => {
    const key = `\uE000MD${tokens.length}\uE000`;
    tokens.push([key, html]);
    return key;
  };
  let value = String(text || "");
  value = value.replace(/`([^`\n]+)`/g, (_, code) => token(`<code>${escapeHtml(code)}</code>`));
  value = value.replace(/!\[([^\]\n]*)\]\(([^)\s]+)(?:\s+"[^"]*")?\)/g, (_, alt, url) => {
    const safe = safeMarkdownUrl(url, { image: true });
    if (!safe) return alt || "";
    return token(`<img class="markdown-image" src="${escapeHtml(safe)}" alt="${escapeHtml(alt || "")}" loading="lazy" />`);
  });
  value = value.replace(/\[([^\]\n]+)\]\(([^)\s]+)(?:\s+"[^"]*")?\)/g, (_, label, url) => {
    const safe = safeMarkdownUrl(url);
    if (!safe) return label;
    return token(`<a href="${escapeHtml(safe)}" target="_blank" rel="noreferrer">${escapeHtml(label)}</a>`);
  });
  let html = escapeHtml(value);
  html = html
    .replace(/~~([^~]+)~~/g, "<del>$1</del>")
    .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
    .replace(/__([^_\n]+)__/g, "<strong>$1</strong>")
    .replace(/(^|[^\*])\*([^*\n]+)\*/g, "$1<em>$2</em>")
    .replace(/(^|[^_])_([^_\n]+)_/g, "$1<em>$2</em>")
    .replace(/(^|[\s(])(https?:\/\/[^\s<]+)/g, (match, prefix, href) => {
      const cleanHref = href.replace(/[),.;:!?]+$/, "");
      const tail = href.slice(cleanHref.length);
      const safe = safeMarkdownUrl(cleanHref);
      if (!safe) return match;
      return `${prefix}<a href="${escapeHtml(safe)}" target="_blank" rel="noreferrer">${escapeHtml(cleanHref)}</a>${escapeHtml(tail)}`;
    });
  for (const [key, replacement] of tokens) html = html.replaceAll(key, replacement);
  return html;
}

function isMarkdownTableSeparator(line) {
  const trimmed = String(line || "").trim();
  return trimmed.includes("-") && /^\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?$/.test(trimmed);
}

function splitMarkdownTableRow(line) {
  return String(line || "")
    .trim()
    .replace(/^\|/, "")
    .replace(/\|$/, "")
    .split("|")
    .map((cell) => cell.trim());
}

function renderMarkdownTable(lines) {
  const headers = splitMarkdownTableRow(lines[0] || "");
  const rows = lines.slice(2).map(splitMarkdownTableRow);
  return `<div class="markdown-table-wrap"><table><thead><tr>${headers.map((cell) => `<th>${renderInlineMarkdown(cell)}</th>`).join("")}</tr></thead><tbody>${rows.map((row) => `<tr>${headers.map((_, idx) => `<td>${renderInlineMarkdown(row[idx] || "")}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
}

function isMarkdownBlockStart(lines, index) {
  const line = lines[index] || "";
  return /^(#{1,6})\s+/.test(line)
    || /^>\s?/.test(line)
    || /^\s*([-*+])\s+/.test(line)
    || /^\s*\d+[.)]\s+/.test(line)
    || /^\s*([-*_])(?:\s*\1){2,}\s*$/.test(line)
    || (index + 1 < lines.length && line.includes("|") && isMarkdownTableSeparator(lines[index + 1]));
}

function renderMarkdownBlocks(text) {
  const lines = String(text || "").replace(/\r\n?/g, "\n").split("\n");
  const out = [];
  let i = 0;
  const paragraph = [];
  const flushParagraph = () => {
    if (!paragraph.length) return;
    out.push(`<p>${paragraph.map(renderInlineMarkdown).join("<br>")}</p>`);
    paragraph.length = 0;
  };
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) {
      flushParagraph();
      i += 1;
      continue;
    }
    const tableStart = line.includes("|") && i + 1 < lines.length && isMarkdownTableSeparator(lines[i + 1]);
    if (tableStart) {
      flushParagraph();
      const tableLines = [line, lines[i + 1]];
      i += 2;
      while (i < lines.length && lines[i].trim() && lines[i].includes("|")) {
        tableLines.push(lines[i]);
        i += 1;
      }
      out.push(renderMarkdownTable(tableLines));
      continue;
    }
    const heading = line.match(/^(#{1,6})\s+(.+)$/);
    if (heading) {
      flushParagraph();
      const level = heading[1].length;
      out.push(`<h${level}>${renderInlineMarkdown(heading[2].trim())}</h${level}>`);
      i += 1;
      continue;
    }
    if (/^\s*([-*_])(?:\s*\1){2,}\s*$/.test(line)) {
      flushParagraph();
      out.push("<hr>");
      i += 1;
      continue;
    }
    if (/^>\s?/.test(line)) {
      flushParagraph();
      const quoteLines = [];
      while (i < lines.length && /^>\s?/.test(lines[i])) {
        quoteLines.push(lines[i].replace(/^>\s?/, ""));
        i += 1;
      }
      out.push(`<blockquote>${renderMarkdownBlocks(quoteLines.join("\n"))}</blockquote>`);
      continue;
    }
    const unordered = line.match(/^\s*([-*+])\s+(.+)$/);
    const ordered = line.match(/^\s*\d+[.)]\s+(.+)$/);
    if (unordered || ordered) {
      flushParagraph();
      const orderedList = Boolean(ordered);
      const items = [];
      while (i < lines.length) {
        const match = orderedList ? lines[i].match(/^\s*\d+[.)]\s+(.+)$/) : lines[i].match(/^\s*[-*+]\s+(.+)$/);
        if (!match) break;
        items.push(match[1]);
        i += 1;
      }
      out.push(`<${orderedList ? "ol" : "ul"}>${items.map((item) => `<li>${renderInlineMarkdown(item)}</li>`).join("")}</${orderedList ? "ol" : "ul"}>`);
      continue;
    }
    paragraph.push(line);
    i += 1;
    while (i < lines.length && lines[i].trim() && !isMarkdownBlockStart(lines, i)) {
      paragraph.push(lines[i]);
      i += 1;
    }
  }
  flushParagraph();
  return out.join("");
}

function renderMarkdownContent(text) {
  const value = String(text || "");
  const lines = value.replace(/\r\n?/g, "\n").split("\n");
  const out = [];
  let normal = [];
  const flushNormal = () => {
    if (!normal.length) return;
    out.push(renderMarkdownBlocks(normal.join("\n")));
    normal = [];
  };
  for (let i = 0; i < lines.length; i += 1) {
    const fence = lines[i].match(/^```([A-Za-z0-9_+.-]*)\s*$/);
    if (!fence) {
      normal.push(lines[i]);
      continue;
    }
    flushNormal();
    const lang = fence[1] || "";
    const code = [];
    i += 1;
    while (i < lines.length && !/^```\s*$/.test(lines[i])) {
      code.push(lines[i]);
      i += 1;
    }
    out.push(`
      <div class="code-block">
        <div class="code-block-head"><span>${escapeHtml(lang || "code")}</span><button class="mini" data-copy-code>复制</button></div>
        <pre><code${lang ? ` class="language-${escapeHtml(lang)}"` : ""}>${escapeHtml(code.join("\n"))}</code></pre>
      </div>
    `);
  }
  flushNormal();
  return out.join("") || "";
}

function highlightHtmlText(html, search) {
  const query = String(search || "").trim();
  if (!query) return html;
  const template = document.createElement("template");
  template.innerHTML = html;
  const lowerQuery = query.toLowerCase();
  const walker = document.createTreeWalker(template.content, NodeFilter.SHOW_TEXT);
  const nodes = [];
  while (walker.nextNode()) nodes.push(walker.currentNode);
  for (const node of nodes) {
    const text = node.nodeValue || "";
    const lower = text.toLowerCase();
    let idx = lower.indexOf(lowerQuery);
    if (idx < 0) continue;
    const frag = document.createDocumentFragment();
    let cursor = 0;
    while (idx >= 0) {
      if (idx > cursor) frag.append(document.createTextNode(text.slice(cursor, idx)));
      const mark = document.createElement("mark");
      mark.textContent = text.slice(idx, idx + query.length);
      frag.append(mark);
      cursor = idx + query.length;
      idx = lower.indexOf(lowerQuery, cursor);
    }
    if (cursor < text.length) frag.append(document.createTextNode(text.slice(cursor)));
    node.parentNode.replaceChild(frag, node);
  }
  return template.innerHTML;
}

function renderMessageContent(text, search = "") {
  const html = renderMarkdownContent(text);
  return highlightHtmlText(html, search);
}

function renderMessageProgressPreview(msg) {
  const items = Array.isArray(msg.round2PreviewItems) ? msg.round2PreviewItems : [];
  const text = items.length
    ? items.map((item) => {
        const label = String(item.label || "").trim();
        const body = String(item.text || "").trim();
        return body ? `${label ? `**${label}**：` : ""}${body}` : "";
      }).filter(Boolean).join("\n\n")
    : "正在处理请求，等待主回复开始。";
  return `
    <div class="message-progress-preview markdown-body" aria-live="polite">
      ${renderMessageContent(text, "")}
    </div>
  `;
}

function renderMessageCurrentAction(msg) {
  if (msg.status !== "streaming" || !msg.currentActionText) return "";
  return `<div class="message-current-action" aria-live="polite"><span>当前</span><strong>${escapeHtml(msg.currentActionText)}</strong></div>`;
}

function renderMessage(msg, search = "") {
  const files = fileRefs(msg.attachments || []);
  const hasText = Boolean(msg.text);
  const text = msg.text || "";
  return `
    <div class="message ${escapeHtml(msg.role)}" data-message-id="${escapeHtml(msg.id || "")}">
      <div class="message-head">
        <span>${escapeHtml(roleLabel(msg.role))}${msg.sendMode ? ` · ${escapeHtml(msg.sendMode)}` : ""}</span>
        <span>${fmtTime(msg.createdAt)} ${msg.status ? `· ${escapeHtml(msg.status)}` : ""}</span>
      </div>
      <div class="message-text markdown-body">${hasText ? renderMessageContent(text, search) : (msg.status === "streaming" ? renderMessageProgressPreview(msg) : "")}</div>
      ${renderMessageCurrentAction(msg)}
      ${files.length ? `<div class="attachments">${files.map((f) => `<span class="chip">${escapeHtml(f.name)}</span>`).join("")}</div>` : ""}
    </div>
  `;
}

function renderComposer() {
  const conv = activeConversation();
  const files = fileRefs(attachedFileIds);
  return `
    <div class="composer">
      ${renderQuickTemplates()}
      ${files.length ? `<div class="attachments">${files.map((f) => `<span class="chip">${escapeHtml(f.name)} <button data-detach-file="${escapeHtml(f.id)}">×</button></span>`).join("")}</div>` : ""}
      <textarea id="draftInput" placeholder="向 bot 发送消息。Enter 发送，Shift+Enter 换行。">${escapeHtml(currentDraft)}</textarea>
      <div class="composer-actions">
        <div class="row">
          <input type="file" id="fileInput" multiple hidden />
          <button id="uploadBtn">上传文件</button>
          <button id="clearDraftBtn">清空输入</button>
          <button id="queueBtn">加入队列</button>
          <button id="insertBtn" ${conv?.status === "running" ? "" : "disabled"}>插入当前任务</button>
          <select id="defaultSendModeSelect">
            <option value="normal" ${state.settings.defaultSendMode === "normal" ? "selected" : ""}>发送</option>
            <option value="queue" ${state.settings.defaultSendMode === "queue" ? "selected" : ""}>队列</option>
            <option value="insert" ${state.settings.defaultSendMode === "insert" ? "selected" : ""}>插入</option>
          </select>
          <button id="paletteBtn">命令</button>
          <button id="toggleQueueAutoBtn">${conv?.autoRunQueue === false ? "队列手动" : "队列自动"}</button>
        </div>
        <button class="primary" id="sendBtn">发送</button>
      </div>
      ${conv?.queue.length ? `<div class="queue-panel"><div class="section-title"><strong>队列</strong><button data-clear-queue="1">清空</button></div>${conv.queue.map((q, idx) => `<div class="queue-item"><span>${escapeHtml(q.text.slice(0, 120))}</span><div class="row"><button data-move-queue="${escapeHtml(q.id)}" data-dir="-1" ${idx === 0 ? "disabled" : ""}>上移</button><button data-move-queue="${escapeHtml(q.id)}" data-dir="1" ${idx === conv.queue.length - 1 ? "disabled" : ""}>下移</button><button data-edit-queue="${escapeHtml(q.id)}">编辑</button><button data-remove-queue="${escapeHtml(q.id)}">删除</button></div></div>`).join("")}</div>` : ""}
    </div>
  `;
}

function renderWorkflow() {
  const conv = activeConversation();
  const run = conv?.workflowRuns?.[0];
  const nodes = workflowDisplayNodes(filterNodes(run?.nodes || []), run);
  const compact = state.ui.workflowViewMode === "compact";
  return `
      <div class="section">
        <div class="section-title">
          <span>工作流</span>
          <span class="small">${escapeHtml(run?.traceId || "无 trace")}</span>
        </div>
        <div class="row" style="margin-bottom:8px">
          <button id="exportWorkflowJsonBtn">导出 JSON</button>
          <button id="exportWorkflowMdBtn">导出 Markdown</button>
          <button id="refreshRunBtn" ${run?.traceId ? "" : "disabled"}>刷新 run</button>
        </div>
        <div class="segmented" style="margin-bottom:8px">
          <button class="${compact ? "" : "active"}" data-workflow-view="detailed">详细</button>
          <button class="${compact ? "active" : ""}" data-workflow-view="compact">简洁</button>
        </div>
        <select id="workflowFilter">
        ${["all", "run", "progress", "workflow", "command", "helper", "error"].map((v) => `<option value="${v}" ${state.ui.workflowFilter === v ? "selected" : ""}>${v}</option>`).join("")}
      </select>
      <input id="workflowSearch" style="margin-top:8px;width:100%" placeholder="搜索工作流" value="${escapeHtml(state.ui.workflowSearch || "")}" />
      <div class="small" style="margin-top:8px">${compact ? "简洁模式按时间追加最新动作，一行显示当前在做什么。" : "主进程、helper、工具调用和命令事件会在这里持续追加。"}</div>
      ${run ? `<div class="small" style="margin-top:6px">命令：${Number(run.commandCount || 0)} · helper：${Number(run.helperCount || 0)}</div>` : ""}
    </div>
    <div class="workflow-list ${compact ? "compact" : ""}">${compact ? renderWorkflowCompact(run) : (nodes.length ? nodes.map((node) => renderNode(node, 0)).join("") : `<div class="empty">暂无工作流事件</div>`)}</div>
  `;
}

function filterNodes(nodes) {
  const filter = state.ui.workflowFilter || "all";
  const search = (state.ui.workflowSearch || "").trim().toLowerCase();
  const run = activeConversation()?.workflowRuns?.[0];
  return nodes.map((node) => normalizeWorkflowNodeForRun(node, run)).filter((node) => {
    const helperId = helperNodeId(node);
    if (
      filter !== "all"
      && !(node.kind || "").includes(filter)
      && (node.status || "") !== filter
      && !(filter === "helper" && helperId)
    ) return false;
    if (!search) return true;
    return `${node.title || ""}\n${node.kind || ""}\n${node.status || ""}\n${node.detail || ""}`.toLowerCase().includes(search);
  });
}

function workflowDisplayNodes(nodes, run = activeConversation()?.workflowRuns?.[0]) {
  const activeRun = run;
  const main = {
    id: "workflow_main",
    kind: "main",
    title: "主进程",
    status: activeRun?.status === "running" ? "running" : (activeRun?.status || "done"),
    detail: "",
    children: [],
  };
  const helpers = [];
  const helperMap = new Map();
  for (const rawNode of nodes) {
    const node = normalizeWorkflowNodeForRun(rawNode, activeRun);
    const helperId = helperNodeId(node);
    const groupId = helperGroupId(node);
    const kind = String(node.kind || "");
    const isHelperRoot = groupId && isHelperWorkflowKind(kind) && kind !== "helper_progress";
    const isHelperProgress = groupId && !isHelperRoot;
    if (isHelperRoot || isHelperProgress) {
      let helper = helperMap.get(groupId);
      if (!helper) {
        helper = {
          id: `helper_group_${groupId}`,
          kind: "helper",
          title: helperRootTitle(node, helperId),
          status: workflowNodeStatus(node.status, node.kind, "", node),
          helperId,
          groupId,
          detail: helperRootDetail(node, helperId),
          children: [],
          createdAt: node.createdAt || nowIso(),
        };
        helperMap.set(groupId, helper);
        helpers.push(helper);
      }
      if (isHelperRoot) {
        helper.helperId = helperId || helper.helperId;
        helper.title = helperRootTitle(node, helperId);
        helper.status = workflowNodeStatus(node.status, node.kind, "", node) || helper.status;
        helper.detail = helperRootDetail(node, helperId) || helper.detail;
      } else {
        helper.children.push(node);
        const nodeStatus = workflowNodeStatus(node.status, node.kind, "", node);
        if ((node.kind || "") === "helper_registry_done" && helper.status !== "running") helper.status = nodeStatus || "exited";
        if ((node.kind || "") === "helper_progress" && ["running", "error", "interrupted"].includes(nodeStatus)) helper.status = nodeStatus;
        if ((node.kind || "") === "helper_start" && ["running", "error", "interrupted"].includes(nodeStatus)) helper.status = nodeStatus;
      }
      continue;
    }
    main.children.push({
      ...node,
      id: node.id || rawNode.id,
      title: node.title || rawNode.title || "事件",
    });
  }
  for (const helper of helpers) {
    const statuses = (helper.children || []).map((child) => workflowNodeStatus(child.status, child.kind, "", child));
    if (statuses.includes("error")) helper.status = "error";
    else if (statuses.includes("interrupted")) helper.status = "interrupted";
    else if (statuses.includes("running")) helper.status = "running";
    else if (statuses.includes("exited")) helper.status = "exited";
    else if (helper.status !== "running") helper.status = workflowNodeStatus(helper.status, helper.kind, "", helper);
  }
  return [main, ...helpers].filter((node) => node.children?.length || node.kind === "helper" || node.kind === "main");
}

function workflowCompactRows(run) {
  return filterNodes(run?.nodes || [])
    .map((node) => normalizeWorkflowNodeForRun(node, run))
    .sort((a, b) => {
      const at = Date.parse(a.updatedAt || a.createdAt || "") || 0;
      const bt = Date.parse(b.updatedAt || b.createdAt || "") || 0;
      return at - bt;
    });
}

function compactNodeText(node) {
  const detail = parsedNodeDetail(node) || {};
  const rawDetail = typeof node.detail === "string" ? node.detail : "";
  const textDetail = rawDetail && !parsedNodeDetail(node)
    ? detailTail(rawDetail).split("\n").filter(Boolean).slice(-3).join(" / ")
    : "";
  const streamDetail = String(node.kind || "") === "stream" && rawDetail
    ? detailTail(rawDetail).split("\n").filter(Boolean).slice(-2).join(" / ") || detailTail(rawDetail)
    : "";
  const candidates = [
    streamDetail,
    detail.what_doing,
    detail.progress_summary,
    detail.last_note,
    detail.last_thought,
    detail.message,
    textDetail,
    node.title,
  ].filter(Boolean);
  let text = String(candidates[0] || rawDetail || node.kind || "事件");
  text = text.replace(/\s+/g, " ").trim();
  if (!text && rawDetail) text = rawDetail.replace(/\s+/g, " ").trim();
  return text || "事件";
}

function renderWorkflowCompact(run) {
  const rows = workflowCompactRows(run);
  if (!rows.length) return `<div class="empty">暂无工作流事件</div>`;
  return `
    <div class="workflow-compact-list">
      ${rows.map((node) => {
        const helper = helperGroupId(node);
        const label = helper ? helperKindLabel(node.helperKind || node.kind || "helper") : (node.kind || "workflow");
        const time = fmtTime(node.updatedAt || node.createdAt);
        return `
          <div class="workflow-compact-row ${escapeHtml(node.status || "")}" data-node-id="${escapeHtml(node.id)}">
            <span class="workflow-compact-time">${escapeHtml(time ? time.split(" ").pop() : "")}</span>
            <span class="workflow-compact-kind">${escapeHtml(label)}</span>
            <span class="workflow-compact-text" title="${escapeHtml(compactNodeText(node))}">${escapeHtml(compactNodeText(node))}</span>
            <span class="workflow-compact-status">${escapeHtml(node.status || "")}</span>
          </div>
        `;
      }).join("")}
    </div>
  `;
}

function renderNode(node, depth = 0) {
  const openIds = new Set(state.ui.workflowOpenNodeIds || []);
  const detailOpen = openIds.has(node.id);
  const detail = detailTail(node.detail || "");
  const children = Array.isArray(node.children) ? node.children : [];
  const canToggle = Boolean(detail || children.length);
  return `
    <div class="node-item ${escapeHtml(node.status)}" data-node-id="${escapeHtml(node.id)}" data-node-depth="${Number(depth)}" style="--depth:${Number(depth)}">
      <div class="node-title">
        <span>${canToggle ? `<button class="mini" data-toggle-node="${escapeHtml(node.id)}">${detailOpen ? "收起" : "展开"}</button>` : ""} ${escapeHtml(node.title)}</span>
        <span class="small">${escapeHtml(node.kind)} · ${escapeHtml(node.status)}</span>
      </div>
      ${detail && (!children.length || detailOpen) ? `<div class="node-detail ${detailOpen ? "open" : ""}">${escapeHtml(detailOpen ? detail : detail.slice(0, 900))}</div>` : ""}
      ${children.length && detailOpen ? `<div class="node-children">${children.map((child) => renderNode(child, depth + 1)).join("")}</div>` : ""}
    </div>
  `;
}

function renderWorkflowOnly(options = {}) {
  const el = $("#workflowPane");
  if (el) {
    const workflowScroll = workflowScrollSnapshot();
    el.innerHTML = renderWorkflow();
    bindWorkflowEvents();
    renderNodeDetailsOnly();
    enhanceButtonTooltips(el);
    restoreWorkflowScroll(options.preserve === false ? null : (options.snapshot || workflowScroll));
  }
}

function scheduleWorkflowRender(options = {}) {
  workflowRenderOptions = { ...(workflowRenderOptions || {}), ...options };
  if (workflowRenderTimer) return;
  workflowRenderTimer = setTimeout(() => {
    workflowRenderTimer = null;
    const nextOptions = workflowRenderOptions || {};
    workflowRenderOptions = null;
    renderWorkflowOnly(nextOptions);
  }, 50);
}

function renderNodeDetailsOnly() {
  const el = $(".node-details-panel");
  if (!el) return;
  el.outerHTML = renderNodeDetailsPanel();
  $("#closeNodeDetailsBtn")?.addEventListener("click", () => {
    state.ui.selectedNodeId = "";
    saveState();
    render();
  });
  enhanceButtonTooltips($(".node-details-panel") || document);
}

function renderFilePanel() {
  const archive = activeArchive();
  const search = (state.ui.fileSearch || "").trim().toLowerCase();
  const files = (state.files[archive?.id] || []).filter((file) => {
    if (!search) return true;
    return `${file.name || ""}\n${file.status || ""}\n${file.workspace_path || ""}`.toLowerCase().includes(search);
  });
  const progressItems = Object.values(uploadProgress);
  return `
    <div class="file-panel ${state.ui.filePanelOpen ? "open" : ""}" id="filePanel">
      <div class="section">
        <div class="section-title">
          <span>bot 文件区</span>
          <div class="row"><button id="refreshFilesBtn">刷新</button><button id="closeFilePanelBtn">关闭</button></div>
        </div>
        <div class="drop-zone" id="dropZone">拖放或粘贴文件上传到 bot 文件区。这里不写入项目目录。</div>
        <input id="fileSearchInput" class="wide-input" style="margin-top:8px" placeholder="搜索文件区" value="${escapeHtml(state.ui.fileSearch || "")}" />
      </div>
      ${progressItems.length ? `<div class="section">${progressItems.map((item) => {
        const percent = Math.max(0, Math.min(100, Number(item.percent || 0)));
        return `
          <div class="upload-progress">
            <div class="small">${escapeHtml(item.name)}：${escapeHtml(item.status)} ${percent}%</div>
            <div class="upload-progress-track" aria-label="${escapeHtml(item.name)} 上传进度" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${percent}">
              <div class="upload-progress-bar" style="width:${percent}%"></div>
            </div>
          </div>
        `;
      }).join("")}</div>` : ""}
      <div class="file-list">${files.length ? files.map(renderFileItem).join("") : `<div class="empty">暂无文件</div>`}</div>
    </div>
  `;
}

function renderFilePanelOnly() {
  const el = $("#filePanel");
  if (!el) return;
  el.outerHTML = renderFilePanel();
  bindFilePanelEvents();
  enhanceButtonTooltips($("#filePanel") || document);
}

function bindFilePanelEvents() {
  $("#refreshFilesBtn")?.addEventListener("click", refreshFiles);
  $("#fileSearchInput")?.addEventListener("input", (e) => {
    state.ui.fileSearch = e.target.value;
    saveState();
    renderFilePanelOnly();
  });
  document.querySelectorAll("[data-attach-file]").forEach((el) => el.addEventListener("click", () => attachFile(el.dataset.attachFile)));
  document.querySelectorAll("[data-delete-file]").forEach((el) => el.addEventListener("click", () => deleteFile(el.dataset.deleteFile)));
  document.querySelectorAll("[data-preview-file]").forEach((el) => el.addEventListener("click", () => previewFile(el.dataset.previewFile)));
  document.querySelectorAll("[data-file-action]").forEach((el) => {
    el.addEventListener("click", () => fileQuickAction(el.dataset.fileId, el.dataset.fileAction));
  });
  document.querySelectorAll("[data-download-file]").forEach((el) => {
    el.addEventListener("click", () => {
      if (el.dataset.downloadFile) window.open(backend(el.dataset.downloadFile), "_blank");
    });
  });
}

function renderArtifactPanelOnly() {
  const el = $("#artifactPanel");
  if (el) {
    el.outerHTML = renderArtifactPanel();
    bindArtifactEvents();
    enhanceButtonTooltips($("#artifactPanel") || document);
  }
}

function renderArtifactPanel() {
  const artifacts = artifactRefs();
  return `
    <div class="artifact-panel ${state.ui.artifactPanelOpen ? "open" : ""}" id="artifactPanel">
      <div class="section">
        <div class="section-title">
          <span>bot 产物区</span>
          <div class="row"><button id="refreshArtifactsBtn">刷新</button><button id="closeArtifactPanelBtn">关闭</button></div>
        </div>
        <div class="small">这里显示 bot 本轮或历史任务生成并可下载的文件。上传给 bot 的输入文件在“文件”区。</div>
      </div>
      <div class="file-list">${artifacts.length ? artifacts.map(renderArtifactItem).join("") : `<div class="empty">暂无产物。任务生成文件后会自动出现在这里。</div>`}</div>
    </div>
  `;
}

function renderArtifactItem(file) {
  const url = file.download_url || file.url || "";
  return `
    <div class="file-item artifact-item">
      <div class="file-name">${escapeHtml(file.name || file.rel_path || "artifact")}</div>
      <div class="file-meta">${file.size ? fmtSize(file.size) + " · " : ""}${escapeHtml(file.rel_path || file.workspace_path || "")}</div>
      <div class="row" style="margin-top:8px">
        <button data-preview-artifact="${escapeHtml(file.id)}" ${!url ? "disabled" : ""}>预览</button>
        <button data-download-artifact="${escapeHtml(url)}" ${!url ? "disabled" : ""}>下载</button>
        <button data-copy-artifact-url="${escapeHtml(url)}" ${!url ? "disabled" : ""}>复制 URL</button>
        <button data-copy-artifact-path="${escapeHtml(file.rel_path || file.workspace_path || "")}">复制路径</button>
      </div>
    </div>
  `;
}

function renderFileItem(file) {
  const failed = file.status === "failed";
  return `
    <div class="file-item ${failed ? "failed" : ""}">
      <div class="file-name">${escapeHtml(file.name)}</div>
      <div class="file-meta">${fmtSize(file.size)} · ${escapeHtml(file.status || "ready")} · ${escapeHtml(file.workspace_path || "")}</div>
      ${file.error ? `<div class="file-error">${escapeHtml(file.error)}</div>` : ""}
      <div class="row" style="margin-top:8px">
        <button data-attach-file="${escapeHtml(file.id)}" ${failed ? "disabled" : ""}>附加</button>
        <button data-preview-file="${escapeHtml(file.id)}" ${failed ? "disabled" : ""}>预览</button>
        <button data-download-file="${escapeHtml(file.download_url || "")}" ${!file.download_url ? "disabled" : ""}>下载</button>
        <button data-copy-file-path="${escapeHtml(file.workspace_path || "")}">复制路径</button>
        <button data-copy-file-url="${escapeHtml(file.download_url || "")}" ${!file.download_url ? "disabled" : ""}>复制 URL</button>
        <button data-file-action="summary" data-file-id="${escapeHtml(file.id)}" ${failed ? "disabled" : ""}>总结</button>
        <button data-file-action="extract" data-file-id="${escapeHtml(file.id)}" ${failed ? "disabled" : ""}>提取</button>
        <button data-file-action="report" data-file-id="${escapeHtml(file.id)}" ${failed ? "disabled" : ""}>报告</button>
        <button class="danger" data-delete-file="${escapeHtml(file.id)}">删除</button>
      </div>
    </div>
  `;
}

function renderProjectPanel() {
  const archive = activeArchive();
  const hasDir = Boolean(archive?.currentDir);
  const tree = state.ui.projectTree || [];
  const results = state.ui.projectSearchResults || [];
  return `
    <div class="project-panel ${state.ui.projectPanelOpen ? "open" : ""}">
      <div class="section">
        <div class="section-title">
          <span>项目目录</span>
          <div class="row"><button id="refreshProjectTreeBtn" ${hasDir ? "" : "disabled"}>刷新</button><button id="closeProjectPanelBtn">关闭</button></div>
        </div>
        <div class="small">${escapeHtml(archive?.currentDir || "当前存档未绑定项目目录")}</div>
        <div class="row" style="margin-top:8px">
          <input id="projectTreePathInput" class="wide-input" placeholder="相对目录" value="${escapeHtml(state.ui.projectTreePath || ".")}" />
        </div>
      </div>
      <div class="section project-tools">
        <input id="projectSearchInput" placeholder="搜索项目文件" value="${escapeHtml(state.ui.projectSearch || "")}" />
        <button id="projectSearchBtn" ${hasDir ? "" : "disabled"}>搜索</button>
        <input id="projectCommandInput" placeholder="在当前目录运行命令" value="${escapeHtml(state.ui.projectCommand || "")}" />
        <button id="projectRunBtn" ${hasDir ? "" : "disabled"}>运行</button>
        <input id="projectDiffPathInput" placeholder="diff 源文件" value="${escapeHtml(state.ui.projectDiffPath || state.ui.projectPreviewPath || "")}" />
        <input id="projectDiffCompareInput" placeholder="diff 对比文件，可空" value="${escapeHtml(state.ui.projectDiffComparePath || "")}" />
        <button id="projectDiffBtn" ${hasDir ? "" : "disabled"}>Diff</button>
        <button id="projectDiffExplainBtn">解释 diff</button>
      </div>
      <div class="project-body">
        <div class="project-tree">
          <div class="small">状态：${escapeHtml(state.ui.projectTreeStatus || "idle")}</div>
          ${tree.length ? tree.map(renderProjectTreeItem).join("") : `<div class="empty">暂无目录数据</div>`}
          ${results.length ? `<div class="section-title" style="margin-top:12px"><span>搜索结果</span></div>${results.map(renderProjectSearchItem).join("")}` : ""}
        </div>
        <div class="project-preview">
          <div class="section-title">
            <span>${escapeHtml(state.ui.projectPreviewPath || "文件预览")}</span>
            <span class="small">${escapeHtml(state.ui.projectPreviewStatus || "idle")}</span>
          </div>
          <pre class="preview-text">${escapeHtml(state.ui.projectPreviewText || state.ui.projectCommandOutput || "")}</pre>
        </div>
      </div>
    </div>
  `;
}

function renderProjectTreeItem(item) {
  const isDir = item.type === "dir";
  return `
    <div class="project-item ${isDir ? "dir" : "file"}" data-project-${isDir ? "dir" : "file"}="${escapeHtml(item.path)}">
      <span>${isDir ? "[dir]" : "[file]"} ${escapeHtml(item.path)}</span>
      <span class="small">${isDir ? "dir" : fmtSize(item.size || 0)}</span>
    </div>
  `;
}

function renderProjectSearchItem(item) {
  return `
    <div class="project-item file" data-project-file="${escapeHtml(item.path)}">
      <span>${escapeHtml(item.path)}:${escapeHtml(item.line)}</span>
      <span class="small">${escapeHtml(item.text || "")}</span>
    </div>
  `;
}

function renderSettingsPanel() {
  return `
    <div class="settings-panel ${state.ui.settingsOpen ? "open" : ""}">
      <div class="section">
        <div class="section-title">
          <span>设置</span>
          <button id="closeSettingsBtn">关闭</button>
        </div>
        <label class="settings-row">
          <span>后端</span>
          <input id="backendUrlInput" value="${escapeHtml(state.settings.backendUrl)}" />
        </label>
        <label class="settings-row">
          <span>监控流</span>
          <select id="monitorEnabledSelect">
            <option value="true" ${state.settings.monitorEnabled ? "selected" : ""}>开启</option>
            <option value="false" ${!state.settings.monitorEnabled ? "selected" : ""}>关闭</option>
          </select>
        </label>
        <label class="settings-row">
          <span>插入策略</span>
          <select id="insertModeSelect">
            <option value="inject_only" ${state.settings.insertMode === "inject_only" ? "selected" : ""}>只插入当前任务</option>
            <option value="inject_then_followup" ${state.settings.insertMode === "inject_then_followup" ? "selected" : ""}>插入后排队下一轮</option>
          </select>
        </label>
        <label class="settings-row">
          <span>工作流保留</span>
          <input id="maxWorkflowRunsInput" type="number" min="1" max="500" value="${escapeHtml(state.settings.maxWorkflowRuns)}" />
        </label>
        <label class="settings-row">
          <span>队列自动执行</span>
          <select id="autoRunQueueSelect">
            <option value="true" ${state.settings.autoRunQueue !== false ? "selected" : ""}>开启</option>
            <option value="false" ${state.settings.autoRunQueue === false ? "selected" : ""}>关闭</option>
          </select>
        </label>
        <label class="settings-row">
          <span>自动继续</span>
          <select id="autoContinueSelect">
            <option value="false" ${!state.settings.autoContinue ? "selected" : ""}>关闭</option>
            <option value="true" ${state.settings.autoContinue ? "selected" : ""}>开启</option>
          </select>
        </label>
        <label class="settings-row">
          <span>自动继续时间上限(秒)</span>
          <input id="autoContinueMaxSecInput" type="number" min="1" max="86400" value="${escapeHtml(state.settings.autoContinueMaxSec || 900)}" />
        </label>
        <label class="settings-row">
          <span>减少确认</span>
          <select id="reduceConfirmationsSelect">
            <option value="false" ${!state.settings.reduceConfirmations ? "selected" : ""}>关闭</option>
            <option value="true" ${state.settings.reduceConfirmations ? "selected" : ""}>开启</option>
          </select>
        </label>
        <label class="settings-row">
          <span>日志 tail</span>
          <input id="logTailLinesInput" type="number" min="20" max="2000" value="${escapeHtml(state.settings.logTailLines)}" />
        </label>
        <label class="settings-row">
          <span>字号</span>
          <input id="fontSizeInput" type="number" min="12" max="20" value="${escapeHtml(state.settings.fontSize)}" />
        </label>
        <div class="row">
          <button id="saveSettingsBtn" class="primary">保存</button>
          <button id="clearTranscriptBtn">清空 transcript</button>
          <button id="exportBtn">导出 Markdown</button>
          <button id="exportStateBtn">导出状态</button>
          <button id="importStateBtn">导入状态</button>
          <button id="cleanupBtn">清理本地</button>
          <input id="stateImportInput" type="file" accept="application/json,.json" hidden />
          <button class="danger" id="deleteArchiveBtn">删本地存档</button>
        </div>
      </div>
    </div>
  `;
}

function renderPreviewPanel() {
  const archive = activeArchive();
  const file = (state.files[archive?.id] || []).find((f) => f.id === state.ui.previewFileId);
  const type = file ? previewTypeForFile(file) : "";
  const url = file?.download_url ? backend(file.download_url) : "";
  let body = `<div class="empty">未选择文件</div>`;
  if (file) {
    if (type === "image") body = `<img class="preview-media" src="${escapeHtml(url)}" alt="${escapeHtml(file.name)}" />`;
    else if (type === "pdf") body = `<iframe class="preview-frame" src="${escapeHtml(url)}"></iframe>`;
    else if (type === "audio") body = `<audio class="preview-media" controls src="${escapeHtml(url)}"></audio>`;
    else if (type === "video") body = `<video class="preview-media" controls src="${escapeHtml(url)}"></video>`;
    else if (type === "text") body = `<pre class="preview-text">${escapeHtml(state.ui.previewText || state.ui.previewStatus)}</pre>`;
    else body = `<div class="empty">该类型暂不内嵌预览，可下载查看。</div>`;
  }
  return `
    <div class="preview-panel ${state.ui.previewOpen ? "open" : ""}">
      <div class="section">
        <div class="section-title">
          <span>${escapeHtml(file?.name || "文件预览")}</span>
          <button id="closePreviewBtn">关闭</button>
        </div>
        <div class="small">状态：${escapeHtml(state.ui.previewStatus || "idle")}</div>
      </div>
      <div class="preview-body">${body}</div>
    </div>
  `;
}

function renderNodeDetailsPanel() {
  const conv = activeConversation();
  const run = conv?.workflowRuns?.[0];
  const displayNodes = workflowDisplayNodes(filterNodes(run?.nodes || []), run);
  const node = findWorkflowNodeById(displayNodes, state.ui.selectedNodeId);
  return `
    <div class="node-details-panel ${node ? "open" : ""}">
      <div class="section">
        <div class="section-title">
          <span>${escapeHtml(node?.title || "节点详情")}</span>
          <button id="closeNodeDetailsBtn">关闭</button>
        </div>
        ${node ? `<div class="small">${escapeHtml(node.kind)} · ${escapeHtml(node.status)} · ${fmtTime(node.createdAt)}</div>` : ""}
      </div>
      <pre class="preview-text">${escapeHtml(node?.detail || "")}</pre>
    </div>
  `;
}

function findWorkflowNodeById(nodes, id) {
  if (!id) return null;
  for (const node of nodes || []) {
    if (node.id === id) return node;
    const child = findWorkflowNodeById(node.children || [], id);
    if (child) return child;
  }
  return null;
}

function bindEvents() {
  $("#newAccountBtn")?.addEventListener("click", createAccount);
  $("#renameAccountBtn")?.addEventListener("click", renameAccount);
  $("#deleteAccountBtn")?.addEventListener("click", deleteLocalAccount);
  $("#newArchiveBtn")?.addEventListener("click", createArchive);
  $("#duplicateTabBtn")?.addEventListener("click", duplicateConversationTab);
  $("#accountSelect")?.addEventListener("change", (e) => {
    syncDraftToConversation();
    state.activeAccountId = e.target.value;
    const next = state.archives.find((a) => a.userId === state.activeAccountId);
    state.activeArchiveId = next?.id || "";
    loadDraftFromConversation();
    startupRecovered = false;
    saveState();
    render();
    recoverFromBackend();
  });
  document.querySelectorAll("[data-archive-id]").forEach((el) => {
    el.addEventListener("click", () => {
      syncDraftToConversation();
      state.activeArchiveId = el.dataset.archiveId;
      loadDraftFromConversation();
      saveState();
      render();
      refreshFiles();
    });
  });
  $("#recoverBtn")?.addEventListener("click", () => {
    startupRecovered = false;
    recoverFromBackend();
  });
  $("#toggleSidebarBtn")?.addEventListener("click", () => {
    state.settings.sidebarOpen = state.settings.sidebarOpen === false;
    saveState();
    render();
  });
  $("#toggleWorkflowBtn")?.addEventListener("click", () => {
    state.settings.workflowOpen = state.settings.workflowOpen === false;
    saveState();
    render();
  });
  $("#syncArchiveBtn")?.addEventListener("click", syncArchive);
  $("#renameArchiveBtn")?.addEventListener("click", renameArchive);
  $("#editDirBtn")?.addEventListener("click", editArchiveDir);
  $("#pinArchiveBtn")?.addEventListener("click", () => archiveLocalOnly("toggle_pin"));
  $("#archiveArchiveBtn")?.addEventListener("click", () => archiveLocalOnly("toggle_archive"));
  $("#filePanelBtn")?.addEventListener("click", toggleFilePanel);
  $("#artifactPanelBtn")?.addEventListener("click", () => {
    state.ui.artifactPanelOpen = !state.ui.artifactPanelOpen;
    if (state.ui.artifactPanelOpen) refreshArtifacts();
    saveState();
    render();
  });
  $("#projectPanelBtn")?.addEventListener("click", toggleProjectPanel);
  $("#settingsBtn")?.addEventListener("click", toggleSettings);
  $("#abortBtn")?.addEventListener("click", abortRun);
  $("#abortClearBtn")?.addEventListener("click", abortAndClearQueue);
  $("#sendBtn")?.addEventListener("click", () => {
    currentDraft = $("#draftInput")?.value || "";
    sendMessage(state.settings.defaultSendMode || "normal");
  });
  $("#queueBtn")?.addEventListener("click", () => {
    currentDraft = $("#draftInput")?.value || "";
    sendMessage("queue");
  });
  $("#insertBtn")?.addEventListener("click", () => {
    currentDraft = $("#draftInput")?.value || "";
    sendMessage("insert");
  });
  $("#paletteBtn")?.addEventListener("click", openCommandPalette);
  $("#uploadBtn")?.addEventListener("click", () => $("#fileInput")?.click());
  $("#fileInput")?.addEventListener("change", (e) => uploadFiles(e.target.files));
  $("#clearDraftBtn")?.addEventListener("click", () => {
    currentDraft = "";
    attachedFileIds = [];
    syncDraftToConversation();
    saveState();
    render();
  });
  $("#toggleQueueAutoBtn")?.addEventListener("click", () => {
    const conv = activeConversation();
    setQueueAutoRun(conv?.autoRunQueue === false);
  });
  $("#draftInput")?.addEventListener("input", (e) => {
    currentDraft = e.target.value;
    syncDraftToConversation();
    saveState();
  });
  $("#draftInput")?.addEventListener("keydown", (e) => {
    const conv = activeConversation();
    if (e.key === "ArrowUp" && !e.shiftKey && !e.ctrlKey && !e.metaKey && !e.target.value.includes("\n")) {
      const history = conv?.inputHistory || [];
      if (history.length) {
        e.preventDefault();
        draftHistoryIndex = draftHistoryIndex < 0 ? history.length - 1 : Math.max(0, draftHistoryIndex - 1);
        setDraft(history[draftHistoryIndex] || "");
      }
      return;
    }
    if (e.key === "ArrowDown" && draftHistoryIndex >= 0) {
      const history = conv?.inputHistory || [];
      e.preventDefault();
      draftHistoryIndex = Math.min(history.length, draftHistoryIndex + 1);
      setDraft(history[draftHistoryIndex] || "");
      if (draftHistoryIndex >= history.length) draftHistoryIndex = -1;
      return;
    }
    if ((e.key === "Enter" && !e.shiftKey) || (e.key === "Enter" && (e.ctrlKey || e.metaKey))) {
      e.preventDefault();
      currentDraft = e.target.value;
      sendMessage(state.settings.defaultSendMode || "normal");
    }
  });
  $("#closeFilePanelBtn")?.addEventListener("click", () => {
    state.ui.filePanelOpen = false;
    saveState();
    render();
  });
  $("#closeArtifactPanelBtn")?.addEventListener("click", () => {
    state.ui.artifactPanelOpen = false;
    saveState();
    render();
  });
  $("#closeSettingsBtn")?.addEventListener("click", toggleSettings);
  $("#defaultSendModeSelect")?.addEventListener("change", (e) => {
    state.settings.defaultSendMode = e.target.value || "normal";
    saveState();
    render();
  });
  $("#saveSettingsBtn")?.addEventListener("click", () => {
    state.settings.backendUrl = $("#backendUrlInput")?.value.trim() || "http://127.0.0.1:8000";
    state.settings.monitorEnabled = $("#monitorEnabledSelect")?.value === "true";
    state.settings.insertMode = $("#insertModeSelect")?.value || "inject_only";
    state.settings.maxWorkflowRuns = Math.max(1, Math.min(500, Number($("#maxWorkflowRunsInput")?.value || DEFAULT_MAX_WORKFLOW_RUNS)));
    state.settings.autoRunQueue = $("#autoRunQueueSelect")?.value !== "false";
    state.settings.autoContinue = $("#autoContinueSelect")?.value === "true";
    state.settings.autoContinueMaxSec = Math.max(1, Math.min(86400, Number($("#autoContinueMaxSecInput")?.value || 900)));
    state.settings.reduceConfirmations = $("#reduceConfirmationsSelect")?.value === "true";
    state.settings.logTailLines = Math.max(20, Math.min(2000, Number($("#logTailLinesInput")?.value || 200)));
    state.settings.fontSize = Math.max(12, Math.min(20, Number($("#fontSizeInput")?.value || 14)));
    saveState();
    render();
  });
  $("#closePreviewBtn")?.addEventListener("click", closePreview);
  $("#closeNodeDetailsBtn")?.addEventListener("click", () => {
    state.ui.selectedNodeId = "";
    saveState();
    render();
  });
  $("#clearTranscriptBtn")?.addEventListener("click", clearTranscript);
  $("#exportBtn")?.addEventListener("click", exportMarkdown);
  $("#exportStateBtn")?.addEventListener("click", exportStateJson);
  $("#importStateBtn")?.addEventListener("click", promptImportStateJson);
  $("#stateImportInput")?.addEventListener("change", (e) => importStateJson(e.target.files?.[0]));
  $("#cleanupBtn")?.addEventListener("click", cleanupLocalData);
  $("#deleteArchiveBtn")?.addEventListener("click", deleteLocalArchive);
  $("#refreshArtifactsBtn")?.addEventListener("click", refreshArtifacts);
  $("#closeProjectPanelBtn")?.addEventListener("click", () => {
    state.ui.projectPanelOpen = false;
    saveState();
    render();
  });
  $("#refreshProjectTreeBtn")?.addEventListener("click", () => {
    state.ui.projectTreePath = $("#projectTreePathInput")?.value.trim() || ".";
    refreshProjectTree(state.ui.projectTreePath);
  });
  $("#projectTreePathInput")?.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      state.ui.projectTreePath = e.target.value.trim() || ".";
      refreshProjectTree(state.ui.projectTreePath);
    }
  });
  $("#projectSearchInput")?.addEventListener("input", (e) => {
    state.ui.projectSearch = e.target.value;
    saveState();
  });
  $("#projectSearchInput")?.addEventListener("keydown", (e) => {
    if (e.key === "Enter") searchProject();
  });
  $("#projectSearchBtn")?.addEventListener("click", searchProject);
  $("#projectCommandInput")?.addEventListener("input", (e) => {
    state.ui.projectCommand = e.target.value;
    saveState();
  });
  $("#projectCommandInput")?.addEventListener("keydown", (e) => {
    if (e.key === "Enter") runProjectCommand();
  });
  $("#projectRunBtn")?.addEventListener("click", runProjectCommand);
  $("#projectDiffPathInput")?.addEventListener("input", (e) => {
    state.ui.projectDiffPath = e.target.value;
    saveState();
  });
  $("#projectDiffCompareInput")?.addEventListener("input", (e) => {
    state.ui.projectDiffComparePath = e.target.value;
    saveState();
  });
  $("#projectDiffBtn")?.addEventListener("click", loadProjectDiff);
  $("#projectDiffExplainBtn")?.addEventListener("click", explainCurrentDiff);
  document.querySelectorAll("[data-project-file]").forEach((el) => {
    el.addEventListener("click", () => {
      state.ui.projectDiffPath = el.dataset.projectFile || "";
      previewProjectFile(el.dataset.projectFile);
    });
  });
  document.querySelectorAll("[data-project-dir]").forEach((el) => {
    el.addEventListener("click", () => refreshProjectTree(el.dataset.projectDir || "."));
  });
  $("#transcriptSearch")?.addEventListener("input", (e) => {
    state.ui.transcriptSearch = e.target.value;
    saveState();
    renderTranscript();
  });
  $("#archiveSearchInput")?.addEventListener("input", (e) => {
    state.ui.archiveSearch = e.target.value;
    saveState();
    render();
  });
  document.querySelectorAll("[data-template-id]").forEach((el) => {
    el.addEventListener("click", () => applyTemplate(el.dataset.templateId, "draft"));
    el.addEventListener("contextmenu", (event) => {
      event.preventDefault();
      applyTemplate(el.dataset.templateId, "queue");
    });
  });
  document.querySelectorAll("[data-detach-file]").forEach((el) => el.addEventListener("click", () => detachFile(el.dataset.detachFile)));
  document.querySelectorAll("[data-copy-code]").forEach((el) => {
    el.addEventListener("click", () => copyText(el.closest(".code-block")?.querySelector("code")?.textContent || ""));
  });
  bindArtifactEvents();
  document.querySelectorAll("[data-copy-file-path]").forEach((el) => {
    el.addEventListener("click", () => copyText(el.dataset.copyFilePath || ""));
  });
  document.querySelectorAll("[data-copy-file-url]").forEach((el) => {
    el.addEventListener("click", () => copyText(el.dataset.copyFileUrl ? backend(el.dataset.copyFileUrl) : ""));
  });
  document.querySelectorAll("[data-remove-queue]").forEach((el) => {
    el.addEventListener("click", () => {
      const conv = activeConversation();
      if (!conv) return;
      conv.queue = conv.queue.filter((q) => q.id !== el.dataset.removeQueue);
      saveState();
      render();
    });
  });
  document.querySelectorAll("[data-edit-queue]").forEach((el) => {
    el.addEventListener("click", () => editQueueItem(el.dataset.editQueue));
  });
  document.querySelectorAll("[data-move-queue]").forEach((el) => {
    el.addEventListener("click", () => moveQueueItem(el.dataset.moveQueue, Number(el.dataset.dir || 0)));
  });
  document.querySelector("[data-clear-queue]")?.addEventListener("click", clearQueue);
  bindTranscriptEvents();
  bindWorkflowEvents();
  bindDropZone();
  bindNotificationEvents();
}

function bindArtifactEvents() {
  document.querySelectorAll("[data-download-artifact]").forEach((el) => {
    el.addEventListener("click", () => {
      if (el.dataset.downloadArtifact) window.open(backend(el.dataset.downloadArtifact), "_blank");
    });
  });
  document.querySelectorAll("[data-preview-artifact]").forEach((el) => {
    el.addEventListener("click", () => {
      const file = artifactRefs().find((item) => item.id === el.dataset.previewArtifact);
      if (!file?.download_url && !file?.url) return;
      state.ui.previewOpen = true;
      state.ui.previewFileId = file.id;
      state.files[activeArchive()?.id] = [
        { ...file, id: file.id, download_url: file.download_url || file.url },
        ...(state.files[activeArchive()?.id] || []).filter((item) => item.id !== file.id),
      ];
      saveState();
      previewFile(file.id);
    });
  });
  document.querySelectorAll("[data-copy-artifact-url]").forEach((el) => {
    el.addEventListener("click", () => copyText(el.dataset.copyArtifactUrl ? backend(el.dataset.copyArtifactUrl) : ""));
  });
  document.querySelectorAll("[data-copy-artifact-path]").forEach((el) => {
    el.addEventListener("click", () => copyText(el.dataset.copyArtifactPath || ""));
  });
}

function bindNotificationEvents() {
  document.querySelectorAll("[data-dismiss-notice]").forEach((el) => {
    el.addEventListener("click", () => dismissNotification(el.dataset.dismissNotice));
  });
}

function bindTranscriptEvents() {
  $("#transcript")?.addEventListener("scroll", () => {
    transcriptLastScrollSnapshot = transcriptScrollSnapshot();
  }, { passive: true });
}

function bindWorkflowEvents() {
  $("#workflowPane .workflow-list")?.addEventListener("scroll", () => {
    workflowLastScrollSnapshot = workflowScrollSnapshot();
  }, { passive: true });
  $("#workflowPane .workflow-list")?.addEventListener("pointerdown", () => {
    workflowPointerActive = true;
    workflowLastScrollSnapshot = workflowScrollSnapshot();
  }, { passive: true });
  $("#workflowPane .workflow-list")?.addEventListener("pointerup", () => {
    workflowPointerActive = false;
    workflowLastScrollSnapshot = workflowScrollSnapshot();
  }, { passive: true });
  $("#workflowPane .workflow-list")?.addEventListener("pointercancel", () => {
    workflowPointerActive = false;
    workflowLastScrollSnapshot = workflowScrollSnapshot();
  }, { passive: true });
  $("#exportWorkflowJsonBtn")?.addEventListener("click", () => exportWorkflow("json"));
  $("#exportWorkflowMdBtn")?.addEventListener("click", () => exportWorkflow("md"));
  $("#refreshRunBtn")?.addEventListener("click", refreshRunSnapshot);
  document.querySelectorAll("[data-workflow-view]").forEach((el) => {
    el.addEventListener("click", () => {
      state.ui.workflowViewMode = el.dataset.workflowView === "compact" ? "compact" : "detailed";
      workflowLastScrollSnapshot = null;
      saveState();
      renderWorkflowOnly({ preserve: false });
    });
  });
  $("#workflowFilter")?.addEventListener("change", (e) => {
    state.ui.workflowFilter = e.target.value;
    saveState();
    renderWorkflowOnly();
  });
  $("#workflowSearch")?.addEventListener("input", (e) => {
    state.ui.workflowSearch = e.target.value;
    saveState();
    renderWorkflowOnly();
  });
  document.querySelectorAll("[data-node-id]").forEach((el) => {
    el.addEventListener("click", (event) => {
      if (event.target.closest("[data-toggle-node]")) return;
      state.ui.selectedNodeId = state.ui.selectedNodeId === el.dataset.nodeId ? "" : el.dataset.nodeId;
      saveState();
      renderWorkflowOnly();
    });
  });
  document.querySelectorAll("[data-toggle-node]").forEach((el) => {
    el.addEventListener("click", (event) => {
      event.stopPropagation();
      const nodeId = el.dataset.toggleNode || "";
      const list = state.ui.workflowOpenNodeIds || [];
      state.ui.workflowOpenNodeIds = list.includes(nodeId)
        ? list.filter((item) => item !== nodeId)
        : [...list, nodeId];
      saveState();
      renderWorkflowOnly();
    });
  });
}

function bindDropZone() {
  const dropZone = $("#dropZone");
  if (dropZone) {
    dropZone.addEventListener("dragover", (e) => {
      e.preventDefault();
      dropZone.classList.add("dragover");
    });
    dropZone.addEventListener("dragleave", () => dropZone.classList.remove("dragover"));
    dropZone.addEventListener("drop", (e) => {
      e.preventDefault();
      dropZone.classList.remove("dragover");
      uploadFiles(e.dataTransfer.files);
    });
  }
  const draft = $("#draftInput");
  if (draft) {
    draft.addEventListener("dragover", (e) => {
      if (e.dataTransfer?.files?.length) e.preventDefault();
    });
    draft.addEventListener("drop", async (e) => {
      if (!e.dataTransfer?.files?.length) return;
      e.preventDefault();
      const before = new Set((state.files[activeArchive()?.id] || []).map((f) => f.id));
      await uploadFiles(e.dataTransfer.files);
      const added = (state.files[activeArchive()?.id] || []).filter((f) => !before.has(f.id) && f.status !== "failed");
      for (const file of added) {
        if (!attachedFileIds.includes(file.id)) attachedFileIds.push(file.id);
      }
      syncDraftToConversation();
      saveState();
      render();
    });
  }
}

window.addEventListener("paste", (e) => {
  const files = [...(e.clipboardData?.files || [])];
  if (!files.length) return;
  e.preventDefault();
  state.ui.filePanelOpen = true;
  uploadFiles(files);
});

function scrollTranscript(options = {}) {
  requestAnimationFrame(() => {
    const el = $("#transcript");
    if (!el) return;
    if (options.onlyIfNearBottom) {
      const bottomGap = el.scrollHeight - el.scrollTop - el.clientHeight;
      if (bottomGap > 160) return;
    }
    el.scrollTop = el.scrollHeight;
  });
}

window.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
    e.preventDefault();
    openCommandPalette();
  } else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "l") {
    e.preventDefault();
    $("#draftInput")?.focus();
  } else if ((e.ctrlKey || e.metaKey) && e.shiftKey && e.key.toLowerCase() === "f") {
    e.preventDefault();
    $("#transcriptSearch")?.focus();
  } else if ((e.ctrlKey || e.metaKey) && e.key === ".") {
    e.preventDefault();
    state.settings.workflowOpen = state.settings.workflowOpen === false;
    saveState();
    render();
  } else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "b") {
    e.preventDefault();
    state.settings.sidebarOpen = state.settings.sidebarOpen === false;
    saveState();
    render();
  } else if (e.key === "Escape") {
    state.ui.filePanelOpen = false;
    state.ui.projectPanelOpen = false;
    state.ui.settingsOpen = false;
    state.ui.previewOpen = false;
    state.ui.selectedNodeId = "";
    saveState();
    render();
  }
});

window.addEventListener("pointerup", () => {
  if (!workflowPointerActive) return;
  workflowPointerActive = false;
  workflowLastScrollSnapshot = workflowScrollSnapshot();
}, { passive: true });

window.addEventListener("pointercancel", () => {
  if (!workflowPointerActive) return;
  workflowPointerActive = false;
  workflowLastScrollSnapshot = workflowScrollSnapshot();
}, { passive: true });

document.addEventListener("pointerover", (event) => {
  const target = event.target.closest?.("button[data-tooltip]");
  if (target) showButtonTooltip(target);
});

document.addEventListener("pointerout", (event) => {
  const target = event.target.closest?.("button[data-tooltip]");
  if (target) hideButtonTooltip(target);
});

document.addEventListener("focusin", (event) => {
  const target = event.target.closest?.("button[data-tooltip]");
  if (target) showButtonTooltip(target);
});

document.addEventListener("focusout", (event) => {
  const target = event.target.closest?.("button[data-tooltip]");
  if (target) hideButtonTooltip(target);
});

window.addEventListener("scroll", () => {
  if (tooltipTarget) positionTooltip(tooltipTarget);
}, true);

window.addEventListener("resize", () => {
  if (tooltipTarget) positionTooltip(tooltipTarget);
});

window.addEventListener("beforeunload", stopMonitor);

async function boot() {
  if (!window.name) window.name = uid("window");
  await applyMaintenanceCleanupMarker();
  loadDraftFromConversation();
  setupStateBroadcast();
  render();
  await hydrateFromIndexedDb();
  await recoverFromBackend();
}

boot();
