#!/bin/bash
# v55 — 部署后监控脚本 (55 stage + 3 hotfix 全覆盖)
# Usage: bash monitor.sh [debug_log_path]

set -e

DELEG="/opt/chatbot/app/llm/tools/delegate.py"
WSP="/opt/chatbot/app/llm/tools/workspace.py"
OFFICE="/opt/chatbot/app/llm/tools/office.py"
CTX="/opt/chatbot/app/core/context.py"
ORCH="/opt/chatbot/app/core/orchestrator.py"
LLM="/opt/chatbot/app/llm/client.py"
LOCKS="/opt/chatbot/app/core/locks.py"
LOG="${1:-/opt/chatbot/app/debug.log}"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

ok() { echo -e "  ${GREEN}✓${NC} $1"; }
warn() { echo -e "  ${YELLOW}⚠${NC} $1"; }
fail() { echo -e "  ${RED}✗${NC} $1"; }

echo "════════════════════════════════════════════════════════════════"
echo "  v55 健康检查 — 55 stage + 3 hotfix"
echo "════════════════════════════════════════════════════════════════"

echo
echo "Stage 1: 源码完整性"

check_in() {
    local file=$1
    local pattern=$2
    local label=$3
    local expected=$4
    if [ ! -f "$file" ]; then
        fail "$label: 文件不存在 $file"
        return 1
    fi
    local count=$(grep -cE "$pattern" "$file" 2>/dev/null || echo 0)
    if [ "$count" -ge "$expected" ]; then
        ok "$label ($count 处)"
    else
        fail "$label: 期望 ≥$expected, 实际 $count"
    fi
}

# Hotfix
check_in "$DELEG" "_os_p\.path\.basename" "Hotfix #1 (os.basename)" 1
check_in "$DELEG" "isinstance\(result_str" "Hotfix #2 (result_str)" 1
check_in "$DELEG" "_re_p31" "Hotfix #3 (re 别名)" 2

# P33-P47 (前轮)
check_in "$DELEG" "## 缺失或警告" "P33 helper 诚实声明" 1
check_in "$DELEG" "p34_inject" "P34 依赖路径注入" 1
check_in "$DELEG" "p35_warn" "P35 依赖警告" 1
check_in "$CTX"   "P36" "P36 workspace 注入" 1
check_in "$LOCKS" "续作意图" "P37 续作清 stop_mode" 1
check_in "$ORCH"  "_stop_mode_triggered" "P38 注入诚实" 1
check_in "$LLM"   "json_repaired" "P39 JSON 修复" 1
check_in "$LLM"   "_is_unterminated" "P39 加强" 1
check_in "$DELEG" "_STDLIB_HEADERS" "P40 STDLIB" 1
check_in "$DELEG" "不越界|P41" "P41 helper 越界守则" 1
check_in "$DELEG" "P41\.B fuzzy" "P41.B fuzzy match" 1
check_in "$WSP"   "_auto_redirect_path" "P42 read_file fuzzy" 1
check_in "$LLM"   "P43: 主进程 iter" "P43 ctx 监控" 1
check_in "$LLM"   "_P44_TOOL_RESULT_BUDGET" "P44 工具结果预算" 1
check_in "$DELEG" "HELPER_CONFIGS" "P45 helper 配置化" 1
check_in "$DELEG" "P46-A" "P46-A 镜像永久根" 1
check_in "$WSP"   "P46-B" "P46-B sync 永久根" 1
check_in "$OFFICE" "P47" "P47 office 图 fuzzy" 3

# P48-P55 (cache 优化 本会话核心)
check_in "$CTX"   "P48" "P48 时间精度: 秒→分" 1
check_in "$LLM"   "P49" "P49 cache stats 收集" 2
check_in "$LLM"   "include_usage" "P50 streaming usage" 2
check_in "$CTX"   "P51" "P51 round2 system 顺序" 1
check_in "$CTX"   "P52" "P52 三级稳定性" 1
check_in "$CTX"   "P53 修" "P53 修 KB 分级" 2
check_in "$LLM"   "P54" "P54 cache log 分类" 1
check_in "$CTX"   "Quick Reference" "P55 ROUND2 头部强化" 1

echo
echo "Stage 2: 运行时回归 (log 应 0 次)"

[ ! -f "$LOG" ] && { warn "log 不存在 $LOG, 跳过运行时检查"; exit 0; }

check_no() {
    local pattern=$1
    local label=$2
    local count=$(grep -cE "$pattern" "$LOG" 2>/dev/null || echo 0)
    if [ "$count" -eq 0 ]; then
        ok "$label: 0 次"
    else
        fail "$label: $count 次"
    fi
}

check_no "no attribute 'basename'"           "代码崩溃 #1"
check_no "name 'result_str' is not defined"  "代码崩溃 #2"
check_no "name 're' is not defined"          "代码崩溃 #3"

echo
echo "Stage 3: 修复触发统计"

check_some() {
    local pattern=$1
    local label=$2
    local count=$(grep -cE "$pattern" "$LOG" 2>/dev/null || echo 0)
    if [ "$count" -gt 0 ]; then
        ok "$label: 触发 $count 次"
    else
        warn "$label: 0 次 (未触发场景)"
    fi
}

check_some "P42: 系统自动重定向"        "P42 read_file 修复"
check_some "P43: 主进程 iter"           "P43 ctx 监控"
check_some "p44_truncated"              "P44 工具截断"
check_some "p46_mirror"                 "P46-A 镜像"
check_some "p46_sync"                   "P46-B sync"
check_some "P47|工作区内相似文件"        "P47 office fuzzy"

# P49 cache 命中率统计
check_some "llm\.cache_stats"           "P49 cache stats 触发"

echo
echo "Stage 4: cache 命中率统计 (本轮核心)"

if [ -f "$LOG" ]; then
    # 总命中率
    total_stats=$(grep -c "P49 \[" "$LOG" 2>/dev/null || echo 0)
    if [ "$total_stats" -gt 0 ]; then
        echo "  cache stats 调用: $total_stats 次"
        
        # main 线程命中率
        main_rate=$(grep "P49 \[main\]" "$LOG" 2>/dev/null | \
            awk -F'cache_hit=|cache_miss=|hit_rate=' \
            'NF>=4 {gsub(/[^0-9]/,"",$2); gsub(/[^0-9]/,"",$3); hit+=$2; miss+=$3} 
             END {if (hit+miss>0) print int(hit*100/(hit+miss)); else print "n/a"}')
        echo "  主线程 cache 命中率: ${main_rate}%"
        if [ "$main_rate" != "n/a" ] && [ "$main_rate" -ge 90 ]; then
            ok "主线程命中率达标 (≥90%)"
        elif [ "$main_rate" != "n/a" ] && [ "$main_rate" -ge 85 ]; then
            warn "主线程命中率 ${main_rate}% (期望 ≥90%)"
        elif [ "$main_rate" != "n/a" ]; then
            fail "主线程命中率偏低 ${main_rate}%"
        fi
        
        # helper 命中率
        helper_rate=$(grep "P49 \[helper\." "$LOG" 2>/dev/null | \
            awk -F'cache_hit=|cache_miss=|hit_rate=' \
            'NF>=4 {gsub(/[^0-9]/,"",$2); gsub(/[^0-9]/,"",$3); hit+=$2; miss+=$3} 
             END {if (hit+miss>0) print int(hit*100/(hit+miss)); else print "n/a"}')
        echo "  helper cache 命中率: ${helper_rate}%"
        
        # 整体命中率
        total_rate=$(grep "P49 \[" "$LOG" 2>/dev/null | \
            awk -F'cache_hit=|cache_miss=|hit_rate=' \
            'NF>=4 {gsub(/[^0-9]/,"",$2); gsub(/[^0-9]/,"",$3); hit+=$2; miss+=$3} 
             END {if (hit+miss>0) print int(hit*100/(hit+miss)); else print "n/a"}')
        echo "  整体 cache 命中率: ${total_rate}%"
    else
        warn "无 cache stats 数据 (可能是 streaming 未返回 usage)"
    fi
fi

echo
echo "Stage 5: 任务执行统计"

if [ -f "$LOG" ]; then
    total_done=$(grep -cE "delegate\.\w+\.done" "$LOG" 2>/dev/null || echo 0)
    interrupted=$(grep -cE "interrupted=True" "$LOG" 2>/dev/null || echo 0)
    if [ "$total_done" -gt 0 ]; then
        success_rate=$(( (total_done - interrupted) * 100 / total_done ))
        echo "  helper 完成总数: $total_done"
        echo "  interrupted: $interrupted"
        echo "  成功率: ${success_rate}%"
    fi
fi

echo
echo "════════════════════════════════════════════════════════════════"
echo "  v55 检查完成"
echo "════════════════════════════════════════════════════════════════"
echo "  ✓ = 修复就位 | ⚠ = 未触发 | ✗ = 仍有问题"
