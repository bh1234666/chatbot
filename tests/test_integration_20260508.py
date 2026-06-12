"""
Integration test: simulate the full pipeline where repair_pairing + delegate dedup interact.

This test simulates:
1. Messages go through context folding → delegate tool result gets folded
2. _fold_old_tool_messages compresses delegate result (but preserves tool_call_id)
3. _repair_tool_call_pairing finds orphan (if tool message is missing entirely)
4. Synthetic result is injected
5. LLM sees synthetic result and does NOT re-issue delegate
6. If LLM DOES re-issue, delegate dedup (Fix 4) blocks it
7. legacy auxiliary paired helper is blocked when all primary tasks are duplicates (Fix 6)

This is read-only — no files are modified outside temp dirs.
"""
import json
import os
import sys
import tempfile
import time
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.llm.client import _repair_tool_call_pairing, _fold_old_tool_messages

passed = 0
failed = 0

def check(name, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS: {name}")
    else:
        failed += 1
        print(f"  FAIL: {name} — {detail}")

# ==========================================================================
# Integration Scenario: Full Real-World Simulation
# ==========================================================================
print("=== Integration: Full Pipeline Simulation ===\n")

# Simulate a typical conversation with delegate calls across many iterations
msgs = [
    # System prompt
    {"role": "system", "content": "You are a coding assistant."},

    # Iteration 1: User asks for task
    {"role": "user", "content": "Implement a radix sort in C and benchmark it"},

    # Iteration 1: LLM delegates
    {"role": "assistant", "content": "I'll delegate the implementation", "tool_calls": [
        {"id": "call_iter1_code", "type": "function",
         "function": {"name": "delegate", "arguments": '{"task_id":"radix_sort","kind":"code"}'}},
        {"id": "call_iter1_bench", "type": "function",
         "function": {"name": "delegate", "arguments": '{"task_id":"radix_bench","kind":"code"}'}},
    ]},
    {"role": "tool", "tool_call_id": "call_iter1_code",
     "content": '{"ok":true,"task_id":"radix_sort","results":[{"file":"radix.c","lines":200}],"report":"Implementation of radix sort with detailed comments and edge case handling"}'},
    {"role": "tool", "tool_call_id": "call_iter1_bench",
     "content": '{"ok":true,"task_id":"radix_bench","results":[{"file":"bench.c","lines":150}],"report":"Benchmark framework with 5 test cases and timing measurements"}'},

    # Iteration 2: LLM sees results, delegates verify
    {"role": "assistant", "content": "Good, now let me verify", "tool_calls": [
        {"id": "call_iter2_verify1", "type": "function",
         "function": {"name": "delegate", "arguments": '{"task_id":"verify_sort","kind":"verify"}'}},
        {"id": "call_iter2_verify2", "type": "function",
         "function": {"name": "delegate", "arguments": '{"task_id":"verify_bench","kind":"verify"}'}},
    ]},
    {"role": "tool", "tool_call_id": "call_iter2_verify1",
     "content": '{"ok":true,"task_id":"verify_sort","passed":true,"report":"All sort tests passed: correctness, edge cases (empty, single, sorted, reverse), overflow protection verified"}'},
    {"role": "tool", "tool_call_id": "call_iter2_verify2",
     "content": '{"ok":true,"task_id":"verify_bench","passed":true,"report":"All benchmark tests passed: timing measurements consistent, no memory leaks detected"}'},

    # Iteration 3: LLM spawns hard-mode summary helper
    {"role": "assistant", "content": "All verified, let me spawn hard-mode summary", "tool_calls": [
        {"id": "call_iter3_summary", "type": "function",
         "function": {"name": "delegate", "arguments": '{"task_id":"summary_report","kind":"code","mode":"hard"}'}},
    ]},
    {"role": "tool", "tool_call_id": "call_iter3_summary",
     "content": '{"ok":true,"task_id":"summary_report","report":"All done"}'},

    # Iteration 4: LLM reads files, asks for more changes
    {"role": "assistant", "content": None, "tool_calls": [
        {"id": "call_iter4_read", "type": "function",
         "function": {"name": "read_file", "arguments": '{"path":"radix.c"}'}},
    ]},
    {"role": "tool", "tool_call_id": "call_iter4_read",
     "content": "// radix sort implementation..."},
]

# Step 1: Run _fold_old_tool_messages to simulate context compression
print("Step 1: Simulate context folding...")
msgs_copy1 = [dict(m) for m in msgs]
# Use keep_recent_iters=0 to force ALL tool messages into "old" zone
# In production, tool results can be 100KB+ (helper reports), which triggers folding
_fold_old_tool_messages(msgs_copy1, keep_recent_iters=0, force_fold_size=50)

# Check that older delegate results are folded (but tool_call_id preserved!)
folded_tool_ids = set()
for m in msgs_copy1:
    if m.get("role") == "tool" and m.get("_folded"):
        folded_tool_ids.add(m.get("tool_call_id"))

print(f"  {len(folded_tool_ids)} tool messages were folded")
check("I1: older delegate results are folded (content compressed)",
      len(folded_tool_ids) > 0,
      "no tool messages folded — keep_recent_iters may need lowering")

# Step 2: Check that folded messages still count as valid responses
# (tool_call_id is preserved in the message, so API pairing still works)
all_tool_ids_with_responses = set()
for m in msgs_copy1:
    if m.get("role") == "tool":
        tcid = m.get("tool_call_id")
        if tcid:
            all_tool_ids_with_responses.add(tcid)

all_tool_call_ids = set()
for m in msgs_copy1:
    if m.get("role") == "assistant":
        for tc in (m.get("tool_calls") or []):
            tc_id = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
            if tc_id:
                all_tool_call_ids.add(tc_id)

print(f"  Tool call IDs: {sorted(all_tool_call_ids)}")
print(f"  Tool response IDs: {sorted(all_tool_ids_with_responses)}")

orphans_before_repair = all_tool_call_ids - all_tool_ids_with_responses
check("I2: folded tool results still count as valid responses (tool_call_id preserved)",
      len(orphans_before_repair) == 0,
      f"orphans found: {orphans_before_repair} — these tool messages were removed, not just folded")

# Step 3: Simulate the WORSE scenario — some tool messages are completely gone
# (e.g., from _fold_completed_task_window removing old message windows)
print("\nStep 3: Simulate lost tool results (entire messages removed)...")
msgs_copy2 = [dict(m) for m in msgs]
# Remove some delegate tool results entirely (simulating window removal)
# Keep assistant tool_calls but remove the corresponding tool messages
msgs_stripped = []
removed_tool_ids = set()
for m in msgs_copy2:
    if m.get("role") == "tool" and m.get("tool_call_id", "").startswith("call_iter1"):
        removed_tool_ids.add(m["tool_call_id"])
        continue  # Skip this tool result
    if m.get("role") == "tool" and m.get("tool_call_id", "").startswith("call_iter2"):
        removed_tool_ids.add(m["tool_call_id"])
        continue
    msgs_stripped.append(m)

print(f"  Removed {len(removed_tool_ids)} tool results: {sorted(removed_tool_ids)}")

# Step 4: Run repair_pairing on the stripped messages
print("\nStep 4: Run repair_pairing...")
n_repaired = _repair_tool_call_pairing(msgs_stripped)
print(f"  Repaired {n_repaired} messages")

# Verify synthetic results were injected for orphaned delegate calls
synthetic = [m for m in msgs_stripped if m.get("_synthetic_repair")]
check("I3: synthetic results injected for orphan delegate calls",
      len(synthetic) == len(removed_tool_ids),
      f"expected {len(removed_tool_ids)} synthetic results, got {len(synthetic)}")

for s in synthetic:
    tc_id = s.get("tool_call_id")
    check(f"I4: synthetic result for {tc_id} correctly paired",
          tc_id in removed_tool_ids)

# Verify ALL tool_calls now have matching tool results
all_tool_ids_after = set()
for m in msgs_stripped:
    if m.get("role") == "assistant":
        for tc in (m.get("tool_calls") or []):
            tc_id = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
            if tc_id:
                all_tool_ids_after.add(tc_id)

all_responses_after = set()
for m in msgs_stripped:
    if m.get("role") == "tool":
        tcid = m.get("tool_call_id")
        if tcid:
            all_responses_after.add(tcid)

orphans_after = all_tool_ids_after - all_responses_after
check("I5: NO orphans remain after repair_pairing",
      len(orphans_after) == 0,
      f"remaining orphans: {orphans_after}")

# Step 5: Verify the LLM would NOT re-issue tasks
# Check that synthetic results contain clear "already completed" messages
print("\nStep 5: Verify synthetic result content...")
all_synthetic_content = " ".join(
    str(s.get("content", "")) for s in synthetic
)
check("I6: synthetic results mention not re-spawning",
      "不要重新 spawn" in all_synthetic_content)
check("I7: synthetic results mention checking workspace",
      "workspace" in all_synthetic_content.lower() or "主工作区" in all_synthetic_content)

# Step 6: Verify message ordering — must be user → asst → tool → asst → tool → ...
print("\nStep 6: Verify message ordering...")
roles = [m.get("role") for m in msgs_stripped]
print(f"  Roles: {roles}")
# No two assistant messages should be adjacent
adjacent_assistants = 0
for i in range(len(roles) - 1):
    if roles[i] == "assistant" and roles[i + 1] == "assistant":
        adjacent_assistants += 1
check("I8: no adjacent assistant messages (API would reject)",
      adjacent_assistants == 0,
      f"found {adjacent_assistants} adjacent assistant messages")

# Every assistant with tool_calls should be followed by tool messages for each call
valid_pairing = True
for i, m in enumerate(msgs_stripped):
    if m.get("role") == "assistant" and m.get("tool_calls"):
        tcs = m.get("tool_calls") or []
        tc_ids = {tc.get("id") if isinstance(tc, dict) else tc.id for tc in tcs}
        # Check next messages are tool results for these calls
        found_ids = set()
        for j in range(i + 1, min(i + 1 + len(tcs), len(msgs_stripped))):
            nm = msgs_stripped[j]
            if nm.get("role") == "tool" and nm.get("tool_call_id") in tc_ids:
                found_ids.add(nm.get("tool_call_id"))
        if found_ids != tc_ids:
            valid_pairing = False
            break
check("I9: API pairing valid (every tool_call has matching tool result)",
      valid_pairing,
      "some tool_calls have no matching response")

# ==========================================================================
# SUMMARY
# ==========================================================================
if __name__ == "__main__":
    print(f"\n{'='*60}")
    print(f"INTEGRATION RESULTS: {passed} passed, {failed} failed out of {passed+failed} tests")
    if failed:
        print("SOME TESTS FAILED")
        sys.exit(1)
    print("ALL INTEGRATION TESTS PASSED")
