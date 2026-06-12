"""Quick import verification — ensures bot will start with all fixes."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Core modules
from app.core.core_processes import ProcessRegistry
print("  OK: processes")

from app.llm.client import _repair_tool_call_pairing, _fold_old_tool_messages, _fold_completed_task_window
from app.llm.tool_pairing import repair_tool_call_pairing
print("  OK: client (repair + fold)")

from app.llm.tools.workspace import (
    rotate_temp_to_prev, ensure_temp_workspace, fetch_to_temp,
    get_temp_workspace, get_prev_workspace, _write_session_manifest
)
print("  OK: workspace (all new functions)")

import app.llm.tools.registry as reg
print(f"  OK: registry")

from app.llm.tools.delegate import handle_delegate
print("  OK: delegate")

# Verify ProcessRegistry has new methods
pr = ProcessRegistry()
assert hasattr(pr, "was_recently_completed"), "missing was_recently_completed"
assert hasattr(pr, "mark_recently_completed"), "missing mark_recently_completed"
assert hasattr(pr, "_recently_completed"), "missing _recently_completed dict"
print("  OK: ProcessRegistry has all new methods")

# Verify repair_pairing has the TASK_MGMT_TOOLS set
import inspect
src = inspect.getsource(repair_tool_call_pairing)
module_src = inspect.getsource(sys.modules[repair_tool_call_pairing.__module__])
assert "delegate" in module_src and "spawn_helper" in module_src and "wait_helper" in module_src
assert "_synthetic_repair" in src
assert "protocol_repair_required" in src
assert "inspect the current workspace" in src
print("  OK: repair_pairing has synthetic injection for task_mgmt tools")

# Verify delegate.py has Fix 4 (resume dedup) and Fix 6 (legacy auxiliary-pair block)
dl_src = inspect.getsource(handle_delegate)
assert "dup_resume_completed_tids" in dl_src, "Fix 4 missing: resume dedup"
assert "_all_orig_dup" in dl_src or "legacy_aux_pair_blocked_after_dedup" in dl_src, "Fix 6 missing: legacy auxiliary-pair block"
print("  OK: delegate has Fix 4 (resume dedup) and Fix 6 (legacy auxiliary-pair block)")

# Verify workspace has Fix 3 and Fix 5
ws_src = inspect.getsource(ensure_temp_workspace)
assert "rotate_temp_to_prev" in ws_src, "Fix 3 infrastructure missing"
ms_src = inspect.getsource(_write_session_manifest)
assert "main_ws" in ms_src, "Fix 5 missing: manifest main_ws scan"
print("  OK: workspace has Fix 3 (preserve to .prev/) and Fix 5 (manifest scan all)")

print()
print("All verifications passed. Bot should start cleanly.")
