#!/usr/bin/env python3
r"""
Log Viewer — 独立日志分析网页。

用法:  python log_viewer_server.py
然后浏览器打开 http://localhost:8765

═══════════════════════════════════════════════════════════════════════════
2026-05-11 重大修复 (与旧版完全兼容,但解析正确率显著提升):

  Bug A — section 标题行被丢弃
    旧版 LINE_RE 只匹配带前缀的行,而 debug.py 的 section() 写文件时把
    `===\n>>> ROUND 1 ...\n===` 当成单条 msg 写入,只有第一行带前缀,后两行裸
    在那里。LINE_RE 不匹配,续行检测又只认 3 空格开头(裸行以 `>>>` 或 `=` 开
    头) → 直接丢弃。后果:section 事件的 msg 永远是 `===...===`,从来没有
    "ROUND 1" 之类的字眼,所以 trace["rounds"] 永远是空的,前端的 ROUND 分组
    完全失效(实测:一个 364 个事件的 trace,r1=0/r2=0/r3=0)。
    
    修复:解析时检测到裸 `>>> XXX` 行紧跟在 [section] 事件后,把 XXX 写回到
    那个 section 事件的 msg 上(替换掉 === 横线)。同时建议给 debug.py 打补丁
    让 section() 直接写单行,避免破坏分行解析(见末尾的 debug_py_patch.md)。

  Bug B — 用户消息提取拿到的是带缩进的原始 payload 行
    旧版逻辑里 `if inner is ev: continue` 跳过了自己,但 "message" 字段恰恰
    在自己这条事件里(因为每个 payload 行是独立 event)。fallback 路径把
    `msg[:200]` 直接当成 message,结果存进了 `    "message": "...你好"` 这种
    带前导空格和 JSON 包装的丑陋字符串。
    
    修复:先把同 (ts,tid,cat) 的连续 payload 行重新拼回完整 JSON,再
    json.loads 拿到结构化的 message 字段。

  Bug C — 每个 payload 行变成独立 event
    "续行 — 附加到上一个事件的 msg" 这段代码只认裸 3 空格开头的行,但
    debug.py 实际写的是 `[ts] [tid] [cat]   line`(带完整前缀),LINE_RE 直接
    匹配进新 event。结果一段 4 行的 JSON 变成 4 个 event,污染计数。
    
    修复:LINE_RE 匹配后,检查 (ts,tid,cat) 与上一条相同且 captured msg 以
    2 空格开头(payload 缩进)→ 合并到上一条,不新建 event。

  Bug D — error_count 不算 helper 的错误
    旧版 stats.total_errors = sum(主 trace 的 error_count) → 完全丢掉 helper
    里的错误(实测 quicksort_verify helper 有 12 个错误,gen_charts 有 13 个,
    但 total_errors 显示 0)。
    
    修复:total_errors 累加所有主+helper 的 error_count,前端单独显示
    "主进程 N / 子进程 M" 两个数字。

  Bug E — 错误识别太窄,漏掉 tool 输出里嵌入的 error
    `"error" in msg.lower()[:60]` 只看前 60 字符,而很多 tool.bash.output
    的开头是 `"command": "..."`,真正的 error: 在后面被埋掉。timeout、重读
    拒绝、API stall 这些用户高度关心的失败模式根本没被分类。
    
    修复:新增 anomaly tagger,识别 timeout / reread_rejected / stall_killed
    / compile_error 等具体类型,顶部新增"异常摘要"面板。

  Bug F — `--------` 系统启动伪 trace 当作对话显示
    旧版没过滤,UI 里会冒出一个空卡片(没有 user/message/archive)。
    
    修复:把 tid == "--------" 的事件归入 system_events,不进 main_traces。

  Bug G — LINE_RE 写死 `.{20}` 宽度
    debug.py 的 _TRACE_DISPLAY_MAX 在 2026-05-04 刚从 16 改成 20,以后还可能
    再改。
    
    修复:改成 `\[([^\]]+)\]` 再 strip(),宽度无关。

═══════════════════════════════════════════════════════════════════════════
"""

import re
import json
import os
import sys
import time
import traceback
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote
from datetime import datetime

# ── 配置 ───────────────────────────────────────────────
PORT = 8765
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
CACHE: dict = {}
CACHE_MAX = 3
MAX_EVENTS_PER_TRACE_IN_RESPONSE = 5000  # 单 trace 超过这个数量做截断,避免 JSON 爆炸

# ── 正则 ─────────────────────────────────────────────────
# 关键改动:trace_id 宽度无关 → 即便 debug.py 把 _TRACE_DISPLAY_MAX 改了也能匹配
LINE_RE = re.compile(r'^\[([\d:.]+)\] \[([^\]]+)\] \[([^\]]+)\] (.*)$')

# 元数据提取
ACQUIRED_RE = re.compile(r'archive=(\S+)\s+group=(\S+)\s+user=(\S+)')
USER_NAME_RE = re.compile(r"user_name='([^']*)'")
ELAPSED_RE = re.compile(r'elapsed=([\d.]+)s')
TIMEOUT_RE = re.compile(r'timed out after (\d+)s')

# Round/section 标题(支持中英文 ASCII 等号)
ROUND_TITLE_RE = re.compile(r'ROUND\s+([123])\b')

# ── 异常分类 ───────────────────────────────────────────────

def detect_anomaly(cat: str, msg: str) -> list[str]:
    """返回事件的异常 tag 列表。

    用全 msg(不只前 60 字符)+ 多种已知失败模式匹配。
    去重在调用方做 — 同一 (ts, tid, anomaly_type) 算一次。
    """
    tags = []
    cl = cat.lower()
    # 全文检查(限长以避免极大 payload 上的全量扫描)
    body = msg if len(msg) < 4000 else msg[:4000]

    # 类别级
    if cl.endswith(".error") or cl in ("error",):
        tags.append("error")
    if cl in ("warn",):
        tags.append("warn")

    # 模式级 — 用户在 analysis_helper_failures.md 里点名关心的几种
    if "timed out after" in body or "command timed out" in body:
        tags.append("timeout")
    if "重读拒绝" in body or "read_file rejected" in body:
        tags.append("reread_rejected")
    if "aborted by user/system request" in body or "subprocess killed" in body:
        tags.append("stall_killed")
    if "API stall" in body or "API_STALL" in body:
        tags.append("stall_killed")

    # 编译/链接错误(嵌在 tool.bash.output 的 stdout 里)
    body_lower = body.lower()
    if "fatal error:" in body_lower or "compilation terminated" in body_lower:
        tags.append("compile_error")
    if "ld returned" in body_lower and "exit status" in body_lower:
        tags.append("link_error")

    # 通用 error: 前缀 (排除已被上面更具体规则覆盖的)
    # 注意:tool.xxx.output 的 msg 可能是 `ERROR: file not found: ...`
    if not tags:
        # 看 msg 头(跳过 payload 缩进)
        head = body.lstrip()[:120].lower()
        if head.startswith("error:") or head.startswith("error,") or head.startswith("[error"):
            tags.append("error")

    return tags


# ── 解析 ─────────────────────────────────────────────────

def _abs_time(rel_ts: str, base_h: int, base_m: int, base_s: int) -> str:
    """事件相对时间 → 加上文件名时间戳基准,得到真实 HH:MM:SS。"""
    try:
        parts = rel_ts.split(":")
        h, m = int(parts[0]), int(parts[1])
        s = float(parts[2])
    except (ValueError, IndexError):
        return rel_ts
    total = base_h * 3600 + base_m * 60 + base_s + h * 3600 + m * 60 + s
    ah = int(total // 3600) % 24
    am = int((total % 3600) // 60)
    as_ = int(total % 60)
    return f"{ah:02d}:{am:02d}:{as_:02d}"


def _try_extract_message_from_event(msg: str) -> str | None:
    """如果 msg 是 reassembled 的 JSON payload(包含 "message": "..."),
    json.loads 取出 message 字段。否则用宽容的正则兜底。失败返回 None。"""
    text = msg.strip()
    # 优先 JSON parse
    if text.startswith("{") and text.endswith("}"):
        try:
            obj = json.loads(text)
            if isinstance(obj, dict) and "message" in obj:
                v = obj["message"]
                return v if isinstance(v, str) else str(v)
        except (json.JSONDecodeError, ValueError):
            pass
    # 兜底正则:允许 "message" 后面跟非贪婪带转义的字符串
    # 注意 JSON 字符串里的 \" 会先被解析,这里我们 fallback 用更宽容的匹配
    m = re.search(r'"message"\s*:\s*"((?:[^"\\]|\\.)*)"', msg)
    if m:
        # 还原一下基础的 JSON 转义
        return m.group(1).replace('\\"', '"').replace('\\n', '\n').replace('\\\\', '\\')
    return None


def parse_log(filepath: str) -> dict:
    """解析 debug 日志文件,返回结构化数据。"""

    if not os.path.exists(filepath):
        return {"error": f"File not found: {filepath}"}

    fname = os.path.basename(filepath)
    fsize = os.path.getsize(filepath)

    # 文件名时间基准
    base_h = base_m = base_s = 0
    base_date = ""
    m_ts = re.search(r'debug_(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})', fname)
    if m_ts:
        base_date = f"{m_ts.group(1)}-{m_ts.group(2)}-{m_ts.group(3)}"
        base_h, base_m, base_s = int(m_ts.group(4)), int(m_ts.group(5)), int(m_ts.group(6))

    # ── 第一遍:行 → events,处理 section 裸标题 + payload 续行合并 ──
    events: list[dict] = []
    # newline=None 自动归一化 CRLF/LF
    with open(filepath, "r", encoding="utf-8", errors="replace", newline=None) as f:
        for raw_line in f:
            line = raw_line.rstrip("\n\r")
            if not line:
                continue
            mo = LINE_RE.match(line)
            if mo:
                ts, tid_raw, cat, msg = mo.groups()
                tid = tid_raw.strip()

                # ── Bug C 修:payload 续行合并 ──
                # 同 (ts, tid, cat) 且 captured msg 以 2 个以上空格开头 → 续行
                if (events
                    and msg.startswith("  ")
                    and events[-1]["ts"] == ts
                    and events[-1]["tid"] == tid
                    and events[-1]["cat"] == cat):
                    # 把 payload 行(去掉缩进)拼到上一条
                    events[-1]["msg"] += "\n" + msg
                    # payload 续行不影响 anomaly tagging,但我们重新扫描一遍
                    # (因为可能新行才暴露了 error 字眼)
                    continue

                ev = {
                    "ts": ts,
                    "tid": tid,
                    "cat": cat,
                    "msg": msg,
                }
                events.append(ev)
            else:
                # ── Bug A 修:裸 `>>> XXX` 标题行回填到上一条 section 事件 ──
                if (events
                    and events[-1]["cat"] == "section"
                    and line.lstrip().startswith(">>>")):
                    title = line.lstrip().lstrip(">").strip()
                    # 替换掉横线 msg(===...===)
                    events[-1]["msg"] = title
                # 其他裸行(横线、可能的旧格式遗留)直接丢

    # ── 第二遍:打 anomaly tag(在 events 完整后做,因为续行已经合并) ──
    anomaly_summary: dict[str, int] = {}  # type → count
    for ev in events:
        tags = detect_anomaly(ev["cat"], ev["msg"])
        if tags:
            ev["tags"] = tags
            for t in tags:
                anomaly_summary[t] = anomaly_summary.get(t, 0) + 1

    # ── 第三遍:按 trace 分桶 ──
    # 主 trace:tid 不为空、不为 "--------"、不含 "."
    # helper:tid 含 "."
    # 系统启动:tid == "--------"
    main_traces: dict[str, dict] = {}
    helper_event_lists: dict[str, list] = {}  # parent_prefix → [events]
    system_events: list[dict] = []

    last_main_tid: str | None = None
    for ev in events:
        tid = ev["tid"]
        if not tid:
            # 没有 tid 的事件(很少见)挂到上一个主 trace
            if last_main_tid and last_main_tid in main_traces:
                main_traces[last_main_tid]["events"].append(ev)
            else:
                system_events.append(ev)
            continue

        if tid == "--------":
            system_events.append(ev)
            continue

        if "." in tid:
            # helper sub-trace
            parent_prefix = tid.split(".", 1)[0]
            helper_event_lists.setdefault(parent_prefix, []).append(ev)
            continue

        # 主 trace
        if tid not in main_traces:
            main_traces[tid] = {
                "trace_id": tid,
                "archive_id": "",
                "group_id": "",
                "user_id": "",
                "user_name": "",
                "message": "",
                "events": [],
                "helpers": {},
                "time_start": None,
                "time_end": None,
                "error_count": 0,           # 仅主 trace
                "anomaly_count": 0,         # 仅主 trace
                "helper_error_count": 0,    # 所有 helper 之和
                "helper_anomaly_count": 0,
                "helper_count": 0,
                "anomaly_types": {},        # type → count (主+helper 合并)
            }
        main_traces[tid]["events"].append(ev)
        last_main_tid = tid

    # ── 第四遍:从主 trace 事件里提取元数据 ──
    for tid, trace in main_traces.items():
        # 把 events 按时间排好(其实已经是顺序的,但保险)
        evs = trace["events"]
        trace["time_start"] = evs[0]["ts"] if evs else None
        trace["time_end"] = evs[-1]["ts"] if evs else None

        for ev in evs:
            cat, msg = ev["cat"], ev["msg"]

            # archive / group / user
            if cat == "user.acquired":
                am = ACQUIRED_RE.search(msg)
                if am:
                    trace["archive_id"] = am.group(1)
                    trace["group_id"] = am.group(2)
                    trace["user_id"] = am.group(3)

            # user_name(可能在 orchestrate.start 主行或后续 payload 行里)
            if cat == "orchestrate.start" and not trace["user_name"]:
                un = USER_NAME_RE.search(msg)
                if un:
                    trace["user_name"] = un.group(1)

            # ── Bug B 修:用户消息提取 ──
            # 现在每个 orchestrate.start 事件已经把所有 payload 行合并好了,
            # msg 形如 `user_name='包涵'\n  {\n    "message": "..."\n  }`
            # 直接用 helper 抽出 message
            if cat == "orchestrate.start" and not trace["message"]:
                extracted = _try_extract_message_from_event(msg)
                if extracted:
                    trace["message"] = extracted[:200]

            # 错误计数
            tags = ev.get("tags", [])
            if "error" in tags:
                trace["error_count"] += 1
            if tags:
                trace["anomaly_count"] += 1
                for t in tags:
                    trace["anomaly_types"][t] = trace["anomaly_types"].get(t, 0) + 1

    # ── 第五遍:helper 事件归属到主 trace ──
    # 用 6 字符前缀匹配主 trace。注意潜在的前缀冲突极少见(2^-24)。
    for parent_prefix, hevs in helper_event_lists.items():
        # 找匹配的主 trace
        matched_tid = None
        for mt in main_traces:
            if mt.startswith(parent_prefix):
                matched_tid = mt
                break
        if not matched_tid:
            # 父 trace 不在本文件中(可能跨文件) → 挂到 system
            system_events.extend(hevs)
            continue

        # 按 helper trace_id 分组
        hgroups: dict[str, list] = {}
        for ev in hevs:
            hgroups.setdefault(ev["tid"], []).append(ev)

        for htid, hev_list in hgroups.items():
            task_part = htid.split(".", 1)[1].strip() if "." in htid else htid

            # helper 内的错误/异常统计
            h_error = 0
            h_anomaly = 0
            h_types: dict[str, int] = {}
            for ev in hev_list:
                tags = ev.get("tags", [])
                if "error" in tags:
                    h_error += 1
                if tags:
                    h_anomaly += 1
                    for t in tags:
                        h_types[t] = h_types.get(t, 0) + 1

            main_traces[matched_tid]["helpers"][task_part] = {
                "trace_id": htid,
                "task_id": task_part,
                "events": hev_list,
                "time_start": hev_list[0]["ts"] if hev_list else "",
                "time_end": hev_list[-1]["ts"] if hev_list else "",
                "event_count": len(hev_list),
                "tool_count": sum(1 for e in hev_list if e["cat"].startswith("tool.")),
                "error_count": h_error,
                "anomaly_count": h_anomaly,
                "anomaly_types": h_types,
            }
            # 上卷到主 trace
            main_traces[matched_tid]["helper_error_count"] += h_error
            main_traces[matched_tid]["helper_anomaly_count"] += h_anomaly
            for t, c in h_types.items():
                main_traces[matched_tid]["anomaly_types"][t] = \
                    main_traces[matched_tid]["anomaly_types"].get(t, 0) + c

        main_traces[matched_tid]["helper_count"] = len(main_traces[matched_tid]["helpers"])

    # ── 索引:archives → groups → users → traces ──
    archives: dict[str, dict] = {}
    for tid, trace in main_traces.items():
        aid = trace["archive_id"] or "__unknown__"
        gid = trace["group_id"] or "__unknown__"
        uid = trace["user_id"] or "__unknown__"
        uname = trace["user_name"] or uid

        if aid not in archives:
            archives[aid] = {"groups": {}}
        if gid not in archives[aid]["groups"]:
            archives[aid]["groups"][gid] = {"users": {}, "traces": []}
        if uid not in archives[aid]["groups"][gid]["users"]:
            archives[aid]["groups"][gid]["users"][uid] = uname
        archives[aid]["groups"][gid]["traces"].append(tid)

    # 绝对时间
    for tid, trace in main_traces.items():
        if trace["time_start"]:
            trace["time_start_abs"] = _abs_time(trace["time_start"], base_h, base_m, base_s)
        if trace["time_end"]:
            trace["time_end_abs"] = _abs_time(trace["time_end"], base_h, base_m, base_s)
        for h in trace["helpers"].values():
            if h["time_start"]:
                h["time_start_abs"] = _abs_time(h["time_start"], base_h, base_m, base_s)
            if h["time_end"]:
                h["time_end_abs"] = _abs_time(h["time_end"], base_h, base_m, base_s)

    # 截断超大 trace 的 events 以控制 JSON 体积
    total_main_events = sum(len(t["events"]) for t in main_traces.values())
    total_helper_events = sum(
        sum(len(h["events"]) for h in t["helpers"].values())
        for t in main_traces.values()
    )
    truncated_traces: list[str] = []
    for tid, trace in main_traces.items():
        if len(trace["events"]) > MAX_EVENTS_PER_TRACE_IN_RESPONSE:
            keep = MAX_EVENTS_PER_TRACE_IN_RESPONSE
            trace["events"] = trace["events"][:keep // 2] + trace["events"][-keep // 2:]
            trace["events_truncated"] = True
            truncated_traces.append(tid)
        for h in trace["helpers"].values():
            if len(h["events"]) > MAX_EVENTS_PER_TRACE_IN_RESPONSE:
                keep = MAX_EVENTS_PER_TRACE_IN_RESPONSE
                h["events"] = h["events"][:keep // 2] + h["events"][-keep // 2:]
                h["events_truncated"] = True

    # 顶层统计(包含 helper 错误)
    total_errors = sum(t["error_count"] for t in main_traces.values())
    total_helper_errors = sum(t["helper_error_count"] for t in main_traces.values())
    total_helpers = sum(t["helper_count"] for t in main_traces.values())

    return {
        "traces": main_traces,
        "archives": archives,
        "anomaly_summary": anomaly_summary,
        "system_events_count": len(system_events),
        "truncated_traces": truncated_traces,
        "stats": {
            "file_name": fname,
            "file_size": fsize,
            "file_size_mb": round(fsize / 1024 / 1024, 2),
            "total_traces": len(main_traces),
            "total_helpers": total_helpers,
            "total_main_errors": total_errors,
            "total_helper_errors": total_helper_errors,
            "total_errors": total_errors + total_helper_errors,
            "total_main_events": total_main_events,
            "total_helper_events": total_helper_events,
            "base_time": f"{base_h:02d}:{base_m:02d}:{base_s:02d}",
            "base_date": base_date,
        },
    }


# ── HTTP Handler ────────────────────────────────────────

class LogViewerHandler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {args[0]}")

    def _send_json(self, data, status=200):
        # indent=None: 节省 50%+ 体积; ensure_ascii=False: 保留中文可读
        body = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html, status=200):
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        qs = parse_qs(parsed.query)

        try:
            if path == "/":
                self._send_html(HTML_PAGE)
            elif path == "/api/logs":
                self._handle_list_logs()
            elif path == "/api/parse":
                self._handle_parse(qs)
            else:
                self._send_json({"error": "Not found"}, 404)
        except Exception as e:
            traceback.print_exc()
            self._send_json({"error": str(e), "trace": traceback.format_exc()}, 500)

    def _handle_list_logs(self):
        files = []
        if os.path.isdir(LOG_DIR):
            for name in os.listdir(LOG_DIR):
                if name.endswith(".log") and name.startswith("debug_"):
                    fpath = os.path.join(LOG_DIR, name)
                    fsize = os.path.getsize(fpath)
                    m_ts = re.search(r'debug_(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})', name)
                    dt_str = ""
                    if m_ts:
                        dt_str = (f"{m_ts.group(1)}-{m_ts.group(2)}-{m_ts.group(3)} "
                                  f"{m_ts.group(4)}:{m_ts.group(5)}:{m_ts.group(6)}")
                    files.append({
                        "name": name,
                        "size": fsize,
                        "size_kb": round(fsize / 1024, 1),
                        "size_mb": round(fsize / 1024 / 1024, 2),
                        "datetime": dt_str,
                    })
        files.sort(key=lambda x: x["name"], reverse=True)
        self._send_json({"files": files})

    def _handle_parse(self, qs):
        filename = qs.get("file", [None])[0]
        if not filename:
            self._send_json({"error": "Missing ?file= parameter"}, 400)
            return

        if ".." in filename or "/" in filename or "\\" in filename:
            self._send_json({"error": "Invalid filename"}, 400)
            return
        if not filename.endswith(".log"):
            self._send_json({"error": "Only .log files allowed"}, 400)
            return

        filepath = os.path.join(LOG_DIR, filename)
        if not os.path.exists(filepath):
            self._send_json({"error": f"File not found: {filename}"}, 404)
            return

        cache_key = (filename, os.path.getmtime(filepath))  # mtime 进 key,文件改了自动失效
        if cache_key in CACHE:
            print(f"[cache hit] {filename}")
            self._send_json(CACHE[cache_key])
            return

        print(f"[parsing] {filename} ...")
        t0 = time.time()
        data = parse_log(filepath)
        elapsed = time.time() - t0
        print(f"[parsed]  {filename}  ({elapsed*1000:.0f}ms, "
              f"{data['stats']['total_traces']} traces, "
              f"{data['stats']['total_errors']} errors total, "
              f"{data['stats']['total_main_events']+data['stats']['total_helper_events']} events)")

        if len(CACHE) >= CACHE_MAX:
            CACHE.pop(next(iter(CACHE)))
        CACHE[cache_key] = data
        self._send_json(data)


# ── HTML 页面 ────────────────────────────────────────────

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Log Viewer</title>
<style>
:root {
  --bg: #0f1119;
  --panel: #161b26;
  --card: #1c2333;
  --card-hover: #232b3e;
  --border: #2a3346;
  --text: #c8d0e0;
  --dim: #6b7394;
  --accent: #5b8def;
  --green: #3fb98e;
  --orange: #e8913a;
  --red: #e5534b;
  --redbg: rgba(229,83,75,0.10);
  --purple: #a371f7;
  --cyan: #39c5cf;
  --yellow: #e5c07b;
}
* { box-sizing:border-box; margin:0; padding:0; }
body { background:var(--bg); color:var(--text); font:14px/1.5 system-ui,-apple-system,sans-serif; }

/* 顶栏 */
#topbar { background:var(--panel); border-bottom:1px solid var(--border);
  padding:12px 20px; display:flex; align-items:center; gap:12px; flex-wrap:wrap; }
#topbar h1 { font-size:18px; font-weight:600; color:var(--accent); white-space:nowrap; }
#topbar select, #topbar button, #topbar input {
  background:var(--card); color:var(--text); border:1px solid var(--border);
  padding:6px 12px; border-radius:6px; font-size:13px; cursor:pointer; }
#topbar input { cursor:text; min-width:220px; }
#topbar button { background:var(--accent); color:#fff; border:none; font-weight:500; }
#topbar button:disabled { opacity:0.4; cursor:default; }
#topbar button.secondary { background:var(--card); border:1px solid var(--border); color:var(--text); }

/* 异常摘要面板 */
#anomalyPanel { background:var(--panel); border-bottom:1px solid var(--border);
  padding:10px 20px; display:none; flex-wrap:wrap; gap:8px; align-items:center; font-size:12px; }
#anomalyPanel.show { display:flex; }
#anomalyPanel .label { color:var(--dim); margin-right:4px; }
.atag { padding:3px 9px; border-radius:12px; font-size:11px; font-weight:500;
  cursor:pointer; user-select:none; border:1px solid transparent; }
.atag:hover { filter:brightness(1.2); }
.atag.active { box-shadow:0 0 0 2px var(--accent); }
.atag-error { background:rgba(229,83,75,0.18); color:#ff8378; }
.atag-warn { background:rgba(229,192,123,0.18); color:#f0c878; }
.atag-timeout { background:rgba(232,145,58,0.18); color:#ffb070; }
.atag-reread_rejected { background:rgba(163,113,247,0.18); color:#c599ff; }
.atag-stall_killed { background:rgba(229,83,75,0.18); color:#ff8378; }
.atag-compile_error { background:rgba(57,197,207,0.18); color:#5cdce5; }
.atag-link_error { background:rgba(57,197,207,0.18); color:#5cdce5; }

/* 筛选栏 */
#filterbar { background:var(--panel); border-bottom:1px solid var(--border);
  padding:8px 20px; display:flex; align-items:center; gap:12px; flex-wrap:wrap; font-size:13px; }
#filterbar select { background:var(--card); color:var(--text); border:1px solid var(--border);
  padding:5px 10px; border-radius:6px; font-size:13px; cursor:pointer; }
#filterbar .stats { color:var(--dim); font-size:12px; margin-left:auto; }

/* 主内容 */
#main { padding:16px 20px; }
#main .empty { text-align:center; color:var(--dim); padding:60px 0; font-size:15px; }

/* Trace 卡片 */
.trace-card { background:var(--card); border:1px solid var(--border); border-radius:8px;
  margin-bottom:10px; overflow:hidden; }
.trace-card.has-error { border-left:3px solid var(--red); }
.trace-card .header { display:flex; align-items:center; gap:12px; padding:12px 16px;
  cursor:pointer; user-select:none; }
.trace-card .header:hover { background:var(--card-hover); }
.trace-card .header .arrow { width:14px; color:var(--dim); transition:transform .15s; flex-shrink:0; }
.trace-card.open .header .arrow { transform:rotate(90deg); }
.trace-card .header .tid { font-family:monospace; font-size:13px; color:var(--accent); min-width:150px; flex-shrink:0; }
.trace-card .header .user { color:var(--orange); font-weight:500; flex-shrink:0; max-width:120px;
  white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.trace-card .header .msg { flex:1; color:var(--dim); font-size:12px; overflow:hidden;
  text-overflow:ellipsis; white-space:nowrap; min-width:100px; }
.trace-card .header .meta { font-size:11px; color:var(--dim); white-space:nowrap; flex-shrink:0; }
.badge { font-size:10px; padding:2px 7px; border-radius:10px; white-space:nowrap; flex-shrink:0; }
.badge-err { background:rgba(229,83,75,0.20); color:#ff8378; }
.badge-warn { background:rgba(229,192,123,0.20); color:#f0c878; }
.badge-ok { background:rgba(63,185,142,0.20); color:#5fd1a8; }
.badge-helper { background:rgba(91,141,239,0.20); color:#8ab0f5; }
.badge-evcount { background:rgba(107,115,148,0.18); color:var(--dim); }

/* 展开内容 */
.trace-card .body { display:none; border-top:1px solid var(--border); }
.trace-card.open .body { display:block; }

.trace-toolbar { padding:8px 16px; display:flex; gap:8px; align-items:center;
  background:rgba(0,0,0,0.15); border-bottom:1px solid var(--border); font-size:12px; }
.trace-toolbar input { background:var(--card); color:var(--text); border:1px solid var(--border);
  padding:4px 8px; border-radius:4px; font-size:12px; flex:1; max-width:300px; }
.trace-toolbar .stat { color:var(--dim); margin-left:auto; }

/* 轮次分组 */
.round-group { margin:8px 16px; }
.round-group .round-label { font-size:11px; font-weight:600; color:var(--accent);
  text-transform:uppercase; padding:6px 0 4px; border-bottom:1px solid var(--border);
  margin-bottom:4px; letter-spacing:0.5px; }
.event-row { display:flex; align-items:flex-start; gap:8px; padding:2px 4px;
  font-size:12px; font-family:'SF Mono','Consolas',monospace; border-left:2px solid transparent; }
.event-row:hover { background:rgba(255,255,255,0.02); }
.event-row.has-error { background:var(--redbg); border-left-color:var(--red); }
.event-row.has-anomaly { border-left-color:var(--yellow); }
.event-row.hidden { display:none; }
.event-row .time { color:var(--dim); min-width:80px; flex-shrink:0; }
.event-row .cat { min-width:140px; flex-shrink:0; }
.event-row .txt { color:var(--text); word-break:break-all; white-space:pre-wrap; opacity:0.85; }
.event-row.has-error .txt { color:var(--text); opacity:1; }
.event-row .tags { margin-left:6px; }
.event-row .minitag { display:inline-block; padding:0 5px; margin-right:3px;
  font-size:9px; border-radius:6px; vertical-align:middle; }

/* 类别颜色 */
.cat-blue { color:#5b8def; }
.cat-purple { color:#a371f7; }
.cat-green { color:#3fb98e; }
.cat-orange { color:#e8913a; }
.cat-red { color:#e5534b; font-weight:bold; }
.cat-cyan { color:#39c5cf; }
.cat-gray { color:#6b7394; }
.cat-yellow { color:#e5c07b; }

/* Helper 子进程 */
.helper-group { margin:6px 16px 6px 32px; border:1px solid var(--border);
  border-radius:6px; overflow:hidden; background:rgba(255,255,255,0.01); }
.helper-group.has-error { border-left:3px solid var(--red); }
.helper-group .helper-header { display:flex; align-items:center; gap:10px; padding:8px 12px;
  background:rgba(255,255,255,0.02); cursor:pointer; user-select:none; font-size:12px; }
.helper-group .helper-header:hover { background:rgba(255,255,255,0.05); }
.helper-group .helper-header .arrow { width:12px; color:var(--dim); font-size:10px; transition:transform .15s; }
.helper-group.open .helper-header .arrow { transform:rotate(90deg); }
.helper-group .helper-body { display:none; padding:4px 12px 8px; }
.helper-group.open .helper-body { display:block; }
.helper-group .task-id { font-family:monospace; color:var(--orange); font-weight:500; }
.helper-group .htrace { font-family:monospace; color:var(--dim); font-size:11px; }

/* Truncation warning */
.truncated-warn { padding:6px 12px; background:rgba(229,192,123,0.10);
  color:var(--yellow); font-size:11px; border-left:3px solid var(--yellow); margin:4px 0; }

/* Scrollbar */
::-webkit-scrollbar { width:8px; height:8px; }
::-webkit-scrollbar-track { background:var(--bg); }
::-webkit-scrollbar-thumb { background:var(--border); border-radius:3px; }
::-webkit-scrollbar-thumb:hover { background:var(--dim); }
</style>
</head>
<body>

<div id="topbar">
  <h1>Log Viewer</h1>
  <select id="fileSelect"><option value="">-- 选择日志文件 --</option></select>
  <button id="btnLoad" onclick="loadFile()">加载</button>
  <button class="secondary" onclick="refreshFiles()">刷新列表</button>
  <input id="globalSearch" type="text" placeholder="搜索事件内容…(本次加载文件内)" oninput="onGlobalSearch()">
  <span id="loadStatus" style="font-size:12px;color:var(--dim);"></span>
</div>

<div id="anomalyPanel">
  <span class="label">异常摘要:</span>
  <span id="anomalyTags"></span>
  <span style="margin-left:auto;color:var(--dim);font-size:11px;" id="anomalyTip"></span>
</div>

<div id="filterbar" style="display:none;">
  Archive: <select id="archiveFilter" onchange="onFilterChange()"><option value="">全部</option></select>
  Group: <select id="groupFilter" onchange="onFilterChange()"><option value="">全部</option></select>
  User: <select id="userFilter" onchange="onFilterChange()"><option value="">全部</option></select>
  <label style="cursor:pointer;"><input type="checkbox" id="onlyErrors" onchange="renderTraces()"> 只看有错的</label>
  <span class="stats" id="statsText"></span>
</div>

<div id="main"><div class="empty">选择日志文件开始</div></div>

<script>
let parsedData = null;
let activeAnomalyFilter = null;  // 当前激活的异常类型筛选

// ── 文件列表 ──
async function refreshFiles() {
  const sel = document.getElementById('fileSelect');
  sel.innerHTML = '<option value="">-- 加载中 --</option>';
  try {
    const r = await fetch('/api/logs');
    const d = await r.json();
    sel.innerHTML = '<option value="">-- 选择日志文件 --</option>';
    for (const f of d.files) {
      const opt = document.createElement('option');
      opt.value = f.name;
      const sizeLabel = f.size_mb >= 0.1 ? `${f.size_mb} MB` : `${f.size_kb} KB`;
      opt.textContent = `${f.datetime}  |  ${sizeLabel}  |  ${f.name}`;
      sel.appendChild(opt);
    }
  } catch(e) {
    sel.innerHTML = '<option value="">加载失败</option>';
  }
}

// ── 加载文件 ──
async function loadFile() {
  const sel = document.getElementById('fileSelect');
  const fname = sel.value;
  if (!fname) return;

  const btn = document.getElementById('btnLoad');
  const st = document.getElementById('loadStatus');
  btn.disabled = true;
  st.textContent = '解析中…';

  try {
    const t0 = performance.now();
    const r = await fetch('/api/parse?file=' + encodeURIComponent(fname));
    parsedData = await r.json();
    const dt = ((performance.now() - t0)/1000).toFixed(1);

    if (parsedData.error) {
      st.textContent = '错误: ' + parsedData.error;
      btn.disabled = false;
      return;
    }
    const s = parsedData.stats;
    st.textContent = `解析完成 ${dt}s · ${s.total_traces} 对话 · ${s.total_helpers} 子进程 · 主${s.total_main_errors}+子${s.total_helper_errors} 错误`;
    buildAnomalyPanel();
    buildFilters();
    renderTraces();
  } catch(e) {
    st.textContent = '加载失败: ' + e.message;
  }
  btn.disabled = false;
}

// ── 异常摘要面板 ──
function buildAnomalyPanel() {
  const panel = document.getElementById('anomalyPanel');
  const tagsEl = document.getElementById('anomalyTags');
  const summary = parsedData.anomaly_summary || {};
  const entries = Object.entries(summary).sort((a,b) => b[1]-a[1]);

  if (entries.length === 0) {
    panel.classList.remove('show');
    return;
  }
  panel.classList.add('show');
  activeAnomalyFilter = null;
  tagsEl.innerHTML = entries.map(([type, count]) =>
    `<span class="atag atag-${type}" data-type="${type}" onclick="toggleAnomalyFilter('${type}')">${type} <strong>${count}</strong></span>`
  ).join('');
  document.getElementById('anomalyTip').textContent =
    `点击 tag 仅显示包含该异常的对话`;
}

function toggleAnomalyFilter(type) {
  activeAnomalyFilter = (activeAnomalyFilter === type) ? null : type;
  // 更新 UI
  document.querySelectorAll('.atag').forEach(el => {
    el.classList.toggle('active', el.dataset.type === activeAnomalyFilter);
  });
  renderTraces();
}

// ── 筛选器 ──
function buildFilters() {
  document.getElementById('filterbar').style.display = 'flex';
  const aSel = document.getElementById('archiveFilter');
  aSel.innerHTML = '<option value="">全部 Archive</option>';
  for (const aid of Object.keys(parsedData.archives).sort()) {
    const gcount = Object.keys(parsedData.archives[aid].groups).length;
    aSel.innerHTML += `<option value="${aid}">${aid} (${gcount}群)</option>`;
  }
  onFilterChange();
}

function onFilterChange() {
  const aid = document.getElementById('archiveFilter').value;
  const gSel = document.getElementById('groupFilter');
  const prevG = gSel.value;
  gSel.innerHTML = '<option value="">全部群</option>';
  if (aid && parsedData.archives[aid]) {
    for (const g of Object.keys(parsedData.archives[aid].groups).sort()) {
      const unames = Object.values(parsedData.archives[aid].groups[g].users).join(', ');
      gSel.innerHTML += `<option value="${g}">${g} [${unames}]</option>`;
    }
  }
  gSel.value = (aid && parsedData.archives[aid] && parsedData.archives[aid].groups[prevG]) ? prevG : '';

  const gid = gSel.value;
  const uSel = document.getElementById('userFilter');
  const prevU = uSel.value;
  uSel.innerHTML = '<option value="">全部用户</option>';
  if (aid && parsedData.archives[aid]) {
    const groups = gid && parsedData.archives[aid].groups[gid]
      ? {[gid]: parsedData.archives[aid].groups[gid]}
      : parsedData.archives[aid].groups;
    const seen = new Set();
    for (const gd of Object.values(groups)) {
      for (const [uid, uname] of Object.entries(gd.users)) {
        if (!seen.has(uid)) {
          seen.add(uid);
          uSel.innerHTML += `<option value="${uid}">${uname} (${uid})</option>`;
        }
      }
    }
  }
  uSel.value = seen2(uSel) ? prevU : '';

  renderTraces();
}
function seen2(sel) {
  // 简单检查 prevU 是否还在选项里
  for (const o of sel.options) if (o.value === sel.value) return true;
  return false;
}

// ── 筛选 traces ──
function getFilteredTraces() {
  if (!parsedData) return {};
  const aid = document.getElementById('archiveFilter').value;
  const gid = document.getElementById('groupFilter').value;
  const uid = document.getElementById('userFilter').value;
  const onlyErr = document.getElementById('onlyErrors').checked;

  let result = {};
  for (const [tid, trace] of Object.entries(parsedData.traces)) {
    if (aid && trace.archive_id !== aid) continue;
    if (gid && trace.group_id !== gid) continue;
    if (uid && trace.user_id !== uid) continue;
    if (onlyErr && (trace.error_count + trace.helper_error_count) === 0) continue;
    if (activeAnomalyFilter && !(trace.anomaly_types && trace.anomaly_types[activeAnomalyFilter])) continue;
    result[tid] = trace;
  }
  return result;
}

// ── 全局搜索 ──
let searchDebounce = null;
function onGlobalSearch() {
  clearTimeout(searchDebounce);
  searchDebounce = setTimeout(renderTraces, 150);
}

// ── 渲染对话列表 ──
function renderTraces() {
  const traces = getFilteredTraces();
  const main = document.getElementById('main');
  const entries = Object.entries(traces);
  const q = (document.getElementById('globalSearch').value || '').trim().toLowerCase();

  if (!parsedData) { main.innerHTML = '<div class="empty">选择日志文件开始</div>'; return; }
  if (entries.length === 0) { main.innerHTML = '<div class="empty">没有匹配的对话</div>'; updateStatsText(); return; }

  // 按时间排序(最新在前)
  entries.sort((a, b) => (b[1].time_start || '').localeCompare(a[1].time_start || ''));

  main.innerHTML = entries.map(([tid, t]) => {
    const userName = t.user_name || t.user_id || '?';
    const msgPreview = (t.message || '(无消息)').slice(0, 140);
    const totalErrs = t.error_count + t.helper_error_count;
    const errBadge = totalErrs > 0
      ? `<span class="badge badge-err">${totalErrs} 错</span>` : '';
    const helperBadge = t.helper_count > 0
      ? `<span class="badge badge-helper">${t.helper_count} helpers</span>` : '';
    const anomalyTagsBadge = t.anomaly_types && Object.keys(t.anomaly_types).length > 0
      ? Object.entries(t.anomaly_types).map(([k,v]) => `<span class="badge atag-${k}" style="cursor:default;">${k}:${v}</span>`).join(' ')
      : '';
    const evCount = t.events.length + Object.values(t.helpers || {}).reduce((s,h) => s + h.event_count, 0);
    const evBadge = `<span class="badge badge-evcount">${evCount} ev</span>`;

    return `
<div class="trace-card${totalErrs>0?' has-error':''}" id="card-${tid}" data-tid="${tid}">
  <div class="header" onclick="toggleTrace('${tid}')">
    <span class="arrow">▶</span>
    <span class="tid">${tid}</span>
    <span class="user">${escHtml(userName)}</span>
    <span class="msg">${escHtml(msgPreview)}</span>
    <span class="meta">${t.time_start_abs||t.time_start||''} → ${t.time_end_abs||t.time_end||''}</span>
    ${helperBadge} ${evBadge} ${errBadge} ${anomalyTagsBadge}
  </div>
  <div class="body" id="body-${tid}"></div>
</div>`;
  }).join('');

  // 如果用户在全局搜索,自动展开所有匹配
  if (q) {
    for (const [tid, t] of entries) {
      if (hasMatch(t, q)) {
        const card = document.getElementById('card-' + tid);
        if (card && !card.classList.contains('open')) toggleTrace(tid);
      }
    }
  }
  updateStatsText();
}

function hasMatch(trace, q) {
  for (const e of trace.events) {
    if ((e.msg || '').toLowerCase().includes(q)) return true;
    if ((e.cat || '').toLowerCase().includes(q)) return true;
  }
  for (const h of Object.values(trace.helpers || {})) {
    for (const e of h.events) {
      if ((e.msg || '').toLowerCase().includes(q)) return true;
      if ((e.cat || '').toLowerCase().includes(q)) return true;
    }
  }
  return false;
}

function updateStatsText() {
  if (!parsedData) return;
  const traces = getFilteredTraces();
  let helpers = 0, errors = 0;
  for (const t of Object.values(traces)) {
    helpers += t.helper_count;
    errors += (t.error_count + t.helper_error_count);
  }
  document.getElementById('statsText').textContent =
    `显示 ${Object.keys(traces).length} 对话 · ${helpers} 子进程 · ${errors} 错误`;
}

// ── 展开 trace ──
function toggleTrace(tid) {
  const card = document.getElementById('card-' + tid);
  const body = document.getElementById('body-' + tid);
  if (card.classList.contains('open')) { card.classList.remove('open'); return; }
  card.classList.add('open');
  if (body.children.length > 0) return;  // 已渲染

  const trace = parsedData.traces[tid];
  if (!trace) return;
  body.innerHTML = renderTraceBody(tid, trace);
  // 应用全局搜索高亮
  applyEventFilter(tid);
}

function renderTraceBody(tid, trace) {
  // 工具栏
  let html = `<div class="trace-toolbar">
    <input type="text" placeholder="筛选本对话事件…" oninput="onTraceLocalFilter('${tid}', this.value)">
    <label style="font-size:12px;cursor:pointer;"><input type="checkbox" onchange="onTraceErrorOnly('${tid}', this.checked)"> 仅错</label>
    <span class="stat">主 ${trace.events.length} ev / helper ${trace.helper_count} 个</span>
  </div>`;

  if (trace.events_truncated) {
    html += `<div class="truncated-warn">⚠ 主 trace 事件过多已截断显示(只保留首尾各 ${Math.floor(parsedData.config?.max||2500)})</div>`;
  }

  // 按 ROUND 分组(现在 Bug A 修了,可以正常拿到 round 标题)
  let currentRound = 'pre';
  let roundEvents = {pre:[], round1:[], round2:[], round3:[], post:[]};
  let sawRound1 = false, sawRound2 = false, sawRound3 = false;

  for (const ev of trace.events) {
    if (ev.cat === 'section') {
      const m = (ev.msg || '').match(/ROUND\s+([123])/);
      if (m) {
        currentRound = 'round' + m[1];
        if (m[1] === '1') sawRound1 = true;
        if (m[1] === '2') sawRound2 = true;
        if (m[1] === '3') sawRound3 = true;
      } else if ((ev.msg||'').includes('MAINTENANCE')) {
        currentRound = 'post';
      } else if ((ev.msg||'').includes('NEW CHAT')) {
        currentRound = 'pre';
      }
    }
    roundEvents[currentRound].push(ev);
  }

  const groupLabels = {
    pre: '初始化 / 加载',
    round1: 'ROUND 1 — 意图分析',
    round2: 'ROUND 2 — 工具执行',
    round3: 'ROUND 3 — 生成回复',
    post: '维护 / 收尾',
  };
  for (const rk of ['pre','round1','round2','round3','post']) {
    const evs = roundEvents[rk];
    if (evs.length === 0) continue;
    html += `<div class="round-group" data-group="${rk}">
      <div class="round-label">${groupLabels[rk]} · ${evs.length} 事件</div>
      ${evs.map(e => renderEventRow(e)).join('')}
    </div>`;
  }

  // Helper 子进程
  const helperEntries = Object.entries(trace.helpers || {});
  if (helperEntries.length > 0) {
    // 按错误数倒序,有错的优先
    helperEntries.sort((a,b) => (b[1].error_count + b[1].anomaly_count) - (a[1].error_count + a[1].anomaly_count));
    html += `<div class="round-group"><div class="round-label">子进程 helpers · ${helperEntries.length} 个</div>`;
    for (const [taskId, h] of helperEntries) {
      const hErrBadge = h.error_count > 0 ? `<span class="badge badge-err">${h.error_count} 错</span>` : '';
      const hAnomalyBadges = h.anomaly_types && Object.keys(h.anomaly_types).length > 0
        ? Object.entries(h.anomaly_types).map(([k,v]) =>
            `<span class="badge atag-${k}" style="cursor:default;">${k}:${v}</span>`).join(' ')
        : '';
      const safeTask = escHtml(taskId).replace(/'/g, '_');
      html += `
<div class="helper-group${h.error_count>0?' has-error':''}" id="helper-${tid}-${safeTask}">
  <div class="helper-header" onclick="toggleHelper('${tid}', ${JSON.stringify(taskId).replace(/'/g,"\\'")})">
    <span class="arrow">▶</span>
    <span class="task-id">${escHtml(taskId)}</span>
    <span class="htrace">${escHtml(h.trace_id)}</span>
    <span style="color:var(--dim);font-size:11px;">${h.time_start_abs||h.time_start||''} → ${h.time_end_abs||h.time_end||''}</span>
    <span style="color:var(--dim);font-size:11px;">${h.event_count} ev / ${h.tool_count} 工具</span>
    ${hErrBadge} ${hAnomalyBadges}
  </div>
  <div class="helper-body" id="hbody-${tid}-${safeTask}"></div>
</div>`;
    }
    html += '</div>';
  }
  return html;
}

function renderEventRow(ev) {
  const catClass = getCatClass(ev.cat);
  const hasError = (ev.tags || []).includes('error');
  const hasAnomaly = (ev.tags || []).length > 0;
  const msgPreview = ev.msg.length > 300 ? ev.msg.slice(0, 300) + '…' : ev.msg;
  const tagsHtml = (ev.tags || []).map(t =>
    `<span class="minitag atag-${t}">${t}</span>`).join('');
  return `<div class="event-row${hasError?' has-error':(hasAnomaly?' has-anomaly':'')}" data-msg="${escAttr(ev.msg)}">
    <span class="time">${ev.ts}</span>
    <span class="cat ${catClass}">[${escHtml(ev.cat)}]</span>
    <span class="txt">${escHtml(msgPreview)}${tagsHtml ? '<span class="tags">' + tagsHtml + '</span>' : ''}</span>
  </div>`;
}

function getCatClass(cat) {
  if (cat.startsWith('orchestrate') || cat.startsWith('load')) return 'cat-blue';
  if (cat.startsWith('llm')) return 'cat-purple';
  if (cat.startsWith('tool.')) return 'cat-green';
  if (cat.startsWith('delegate')) return 'cat-orange';
  if (cat.startsWith('memory')) return 'cat-cyan';
  if (cat.startsWith('workspace')) return 'cat-yellow';
  if (cat.startsWith('round')) return 'cat-blue';
  if (cat === 'section') return 'cat-yellow';
  if (cat.toLowerCase().includes('error') || cat === 'ERROR') return 'cat-red';
  if (cat === 'WARN' || cat === 'warn') return 'cat-yellow';
  return 'cat-gray';
}

// ── 展开 helper ──
function toggleHelper(tid, taskId) {
  const safeTask = escHtml(taskId).replace(/'/g, '_');
  const group = document.getElementById('helper-' + tid + '-' + safeTask);
  const body = document.getElementById('hbody-' + tid + '-' + safeTask);
  if (group.classList.contains('open')) { group.classList.remove('open'); return; }
  group.classList.add('open');
  if (body.children.length > 0) return;

  const trace = parsedData.traces[tid];
  const hdata = trace.helpers[taskId];
  if (!hdata) return;

  let html = '';
  if (hdata.events_truncated) {
    html += `<div class="truncated-warn">⚠ helper 事件过多已截断,只保留首尾各 ${Math.floor(hdata.events.length/2)}</div>`;
  }
  html += hdata.events.map(e => renderEventRow(e)).join('');
  body.innerHTML = html;
  applyEventFilter(tid);
}

// ── 本对话事件筛选 ──
function onTraceLocalFilter(tid, val) {
  const card = document.getElementById('card-' + tid);
  if (!card) return;
  card.dataset.localFilter = (val || '').toLowerCase();
  applyEventFilter(tid);
}
function onTraceErrorOnly(tid, checked) {
  const card = document.getElementById('card-' + tid);
  if (!card) return;
  card.dataset.errorOnly = checked ? '1' : '';
  applyEventFilter(tid);
}
function applyEventFilter(tid) {
  const card = document.getElementById('card-' + tid);
  if (!card) return;
  const localQ = card.dataset.localFilter || '';
  const errorOnly = card.dataset.errorOnly === '1';
  const globalQ = (document.getElementById('globalSearch').value || '').trim().toLowerCase();
  const q = localQ || globalQ;

  card.querySelectorAll('.event-row').forEach(row => {
    let show = true;
    if (q) {
      const msg = (row.dataset.msg || '').toLowerCase();
      const cat = (row.querySelector('.cat')?.textContent || '').toLowerCase();
      show = msg.includes(q) || cat.includes(q);
    }
    if (show && errorOnly) {
      show = row.classList.contains('has-error') || row.classList.contains('has-anomaly');
    }
    row.classList.toggle('hidden', !show);
  });
}

// ── HTML 转义 ──
function escHtml(s) {
  if (s == null) return '';
  const d = document.createElement('div');
  d.textContent = String(s);
  return d.innerHTML;
}
function escAttr(s) {
  return escHtml(s).replace(/"/g, '&quot;');
}

refreshFiles();
</script>
</body>
</html>
"""

# ── 入口 ─────────────────────────────────────────────────

def main():
    print(f"Log Viewer — http://localhost:{PORT}")
    print(f"日志目录: {LOG_DIR}")
    print(f"按 Ctrl+C 停止\n")

    server = HTTPServer(("0.0.0.0", PORT), LogViewerHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
        server.shutdown()


if __name__ == "__main__":
    main()
