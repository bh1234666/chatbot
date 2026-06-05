"""
BotCtl Helper — Python backend for botctl.bat commands.
Handles JSON parsing, API calls, and formatted display.

Usage:
  python botctl_helper.py create <name> [group_id]
  python botctl_helper.py list-groups
  python botctl_helper.py list-personas <group_id>
  python botctl_helper.py switch <group_id> [archive_id]
"""
import io
import json
import os
import sys
import urllib.request
import urllib.error
from contextlib import redirect_stdout

from app.config import Settings

API = Settings().chatbot_url.rstrip("/") + "/v1"


def _pick_persona() -> tuple[str, str] | None:
    """交互式选择人设。返回 (persona_id, persona_name)，或 None 取消。"""
    from app.memory.persona_files import list_personas
    personas = list_personas()
    if not personas:
        print("[!] No persona files found in personas/")
        return None
    if len(personas) == 1:
        p = personas[0]
        print(f"  人设（唯一）: {p.name} — {p.description}")
        return (p.id, p.name)
    print("  可用人设：")
    for i, p in enumerate(personas, 1):
        print(f"    {i}. {p.name}  — {p.description}")
    print()
    choice = _botctl_input("  选择人设 (序号/回车跳过): ").strip()
    if not choice:
        return None
    try:
        idx = int(choice) - 1
        if 0 <= idx < len(personas):
            p = personas[idx]
            return (p.id, p.name)
    except ValueError:
        pass
    # Try name/id match
    for p in personas:
        if p.name == choice or p.id == choice:
            return (p.id, p.name)
    print(f"  [!] 无效选择，跳过人设")
    return None


def _api(method: str, path: str, body: dict | None = None) -> tuple[int, dict | list]:
    """Make an API call and return (status_code, parsed_json)."""
    url = f"{API}{path}"
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    try:
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=10) as resp:
            content = resp.read().decode("utf-8")
            return resp.status, json.loads(content) if content else {}
    except urllib.error.HTTPError as e:
        return e.code, {}
    except urllib.error.URLError:
        print("[!] Cannot connect to API. Is the chatbot running?")
        sys.exit(1)


def cmd_create(args: list[str]):
    """Create a new archive, set as current, optionally join a group."""
    if not args:
        print("Usage: python botctl_helper.py create <name> [group_id]")
        sys.exit(1)

    name = args[0]
    group_id = args[1] if len(args) > 1 else None

    # Pick persona
    pick = _pick_persona()
    persona_id = pick[0] if pick else None
    persona_label = pick[1] if pick else ""

    # Create archive (with persona_id if selected)
    body: dict = {"name": name}
    if persona_id:
        body["persona_id"] = persona_id
    code, data = _api("POST", "/archives", body)
    if code not in (200, 201):
        print(f"[!] Failed to create archive (HTTP {code})")
        sys.exit(1)
    aid = data["archive_id"]
    detail = f"persona={persona_id}" if persona_id else "no persona"
    print(f"[OK] Archive: {aid}  name={name}  {detail}")

    # Set as global current archive
    _api("PUT", "/bot/current-archive", {"archive_id": aid})
    print(f"[OK] Set as current archive")

    # Optionally join group (sets participate=1 + active_archive_id)
    if group_id:
        join_body: dict = {"archive_id": aid, "persona_label": persona_label}
        code, _ = _api("POST", f"/bot/groups/{group_id}/join", join_body)
        if code == 200:
            print(f"[OK] Joined group {group_id}")
        else:
            print(f"[!] Join group failed (HTTP {code})")

    print()
    print(f"  Archive ID: {aid}")
    print(f"  Name:       {name}")
    if group_id:
        print(f"  Group:      {group_id}")
    print()


def cmd_list_groups(args: list[str] | None = None):
    """List all groups with their active persona."""
    code, data = _api("GET", "/bot/groups")
    if code != 200:
        print(f"[!] API error (HTTP {code})")
        sys.exit(1)
    items = data.get("items", [])
    # Only show numeric QQ groups; filter out non-QQ alphanumeric group IDs.
    items = [g for g in items if g.get("group_id", "").isdigit()]
    if not items:
        print("No groups configured. Use: botctl create <name> <group_id>")
        return

    print()
    print(f"  {'GROUP ID':<18} {'ACTIVE PERSONA':<32} {'PARTICIPATE':<12} {'PERSONAS':<8}")
    print(f"  {'─'*18} {'─'*32} {'─'*12} {'─'*8}")
    for g in items:
        gid = g["group_id"]
        active = g.get("active_archive_id", "-") or "-"
        active_short = active[:30] if len(active) > 30 else active
        part = "YES" if g.get("participate") else "NO"
        num_personas = len(g.get("personas", []))
        print(f"  {gid:<18} {active_short:<32} {part:<12} {num_personas:<8}")
    print()


def cmd_list_personas(args: list[str]):
    """List all personas in a group with summaries."""
    if not args:
        print("Usage: python botctl_helper.py list-personas <group_id>")
        sys.exit(1)

    group_id = args[0]
    code, data = _api("GET", f"/bot/groups/{group_id}")
    if code == 404:
        print(f"[!] Group {group_id} not configured")
        print(f"    Create a persona with: botctl create <name> {group_id}")
        sys.exit(1)
    if code != 200:
        print(f"[!] API error (HTTP {code})")
        sys.exit(1)

    personas = data.get("personas", [])
    if not personas:
        print(f"No personas registered for group {group_id}")
        print(f"Create one with: botctl create <name> {group_id}")
        return

    active_aid = data.get("active_archive_id", "")

    print()
    print(f"  Group: {group_id}")
    if data.get("group_name"):
        print(f"  Name:  {data['group_name']}")
    print(f"  Participate: {'YES' if data.get('participate') else 'NO'}")
    print()
    print(f"  {'#':<4} {'STATUS':<8} {'ARCHIVE ID':<30} {'NAME':<24} {'LAST SUMMARY':<44}")
    print(f"  {'─'*4} {'─'*8} {'─'*30} {'─'*24} {'─'*44}")

    for i, p in enumerate(personas, 1):
        aid = p["archive_id"]
        name = p.get("archive_name", p.get("persona_label", "-")) or "-"
        is_active = p.get("is_active", 0)
        status = "★ ACTIVE" if is_active else ""
        summary = p.get("last_summary", "") or ""
        # Truncate long fields
        aid_short = aid[:28] if len(aid) > 28 else aid
        name_short = name[:22] if len(name) > 22 else name
        summary_short = summary[:42] if len(summary) > 42 else summary
        print(f"  {i:<4} {status:<8} {aid_short:<30} {name_short:<24} {summary_short:<44}")

    print()
    if len(personas) > 1:
        active = [p for p in personas if p.get("is_active")]
        if active:
            print(f"  ★ Active: {active[0]['archive_id']}")
            print(f"     Switch: botctl switch {group_id} <archive_id>")
        else:
            print("  No active persona! Activate one with:")
            print(f"     botctl switch {group_id} <archive_id>")
    print()


def cmd_switch(args: list[str]):
    """Switch active persona for a group. Interactive if no archive_id given."""
    if not args:
        print("Usage: python botctl_helper.py switch <group_id> [archive_id]")
        sys.exit(1)

    group_id = args[0]

    # Fetch personas
    code, data = _api("GET", f"/bot/groups/{group_id}")
    if code == 404:
        print(f"[!] Group {group_id} not configured")
        sys.exit(1)

    personas = data.get("personas", [])
    if not personas:
        print(f"[!] No personas in group {group_id}")
        sys.exit(1)

    if len(args) >= 2:
        # Direct switch
        archive_id = args[1]
        code, _ = _api("POST", f"/bot/groups/{group_id}/personas/{archive_id}/activate")
        if code == 200:
            print(f"[OK] Switched group {group_id} → persona {archive_id}")
            # Also set as global current archive
            _api("PUT", "/bot/current-archive", {"archive_id": archive_id})
            # Show the persona name
            for p in personas:
                if p["archive_id"] == archive_id:
                    name = p.get("archive_name", "")
                    if name:
                        print(f"     Name: {name}")
                    break
        else:
            print(f"[!] Switch failed (HTTP {code})")
        return

    # Interactive mode: show list and let user pick
    active_aid = data.get("active_archive_id", "")

    print()
    print(f"  Group: {group_id}")
    print(f"  Select persona to activate:")
    print()

    for i, p in enumerate(personas, 1):
        aid = p["archive_id"]
        name = p.get("archive_name", p.get("persona_label", "-")) or "-"
        is_active = p.get("is_active", 0)
        marker = " ★ (active)" if is_active else ""
        label = p.get("persona_label", "") or ""
        summary = p.get("last_summary", "") or ""
        print(f"  [{i}] {name}{marker}")
        print(f"      ID: {aid}")
        if label:
            print(f"      人设: {label}")
        if summary:
            print(f"      摘要: {summary}")
        print()

    print(f"  [0] Cancel")
    print()

    choice = _botctl_input("  Choice (number): ").strip()

    if not choice or choice == "0":
        print("  Cancelled.")
        return

    try:
        idx = int(choice) - 1
        if idx < 0 or idx >= len(personas):
            print(f"  [!] Invalid choice: {choice}")
            sys.exit(1)
    except ValueError:
        print(f"  [!] Invalid number: {choice}")
        sys.exit(1)

    chosen = personas[idx]
    archive_id = chosen["archive_id"]
    name = chosen.get("archive_name", archive_id)

    code, _ = _api("POST", f"/bot/groups/{group_id}/personas/{archive_id}/activate")
    if code == 200:
        print(f"  [OK] Activated: {name} ({archive_id})")
        _api("PUT", "/bot/current-archive", {"archive_id": archive_id})
    else:
        print(f"  [!] Activation failed (HTTP {code})")


def cmd_recent(args: list[str]):
    """Show recent conversations in a group."""
    if not args:
        print("Usage: python botctl_helper.py recent <group_id> [count]")
        sys.exit(1)

    group_id = args[0]
    count = int(args[1]) if len(args) >= 2 else 15

    # Get group config to find active archive
    code, cfg = _api("GET", f"/bot/groups/{group_id}")
    if code == 404:
        print(f"[!] Group {group_id} not configured")
        sys.exit(1)
    if code != 200:
        print(f"[!] API error (HTTP {code})")
        sys.exit(1)

    active_aid = cfg.get("active_archive_id", "")
    if not active_aid:
        print(f"[!] Group {group_id} has no active persona")
        sys.exit(1)

    # Fetch hot group events
    code, data = _api("GET", f"/archives/{active_aid}/groups/{group_id}/memory/hot")
    if code != 200:
        print(f"[!] Cannot fetch memory (HTTP {code})")
        sys.exit(1)

    events = data.get("items", [])
    if not events:
        print(f"  No conversations yet in group {group_id}")
        return

    print()
    print(f"  Group: {group_id}  |  Active persona: {active_aid}")
    print(f"  Recent {min(count, len(events))} of {len(events)} events:")
    print(f"  {'─'*70}")

    shown = 0
    for ev in reversed(events):
        if shown >= count:
            break
        actor = ev.get("actor_name", "?")
        narration = ev.get("narration", "")
        ts = ev.get("created_at", "") or ""
        ts_short = ts[:16].replace("T", " ") if ts else ""
        print(f"  [{ts_short}] {actor}: {narration}")
        shown += 1
    print()


def cmd_leave(args: list[str]):
    """Leave a group."""
    if not args:
        print("Usage: python botctl_helper.py leave <group_id>")
        sys.exit(1)
    group_id = args[0]
    code, _ = _api("POST", f"/bot/groups/{group_id}/leave")
    if code == 200:
        print(f"[OK] Left group {group_id}")
    else:
        print(f"[!] Failed (HTTP {code})")


def cmd_quick(args: list[str]):
    """One-shot: pick persona → create archive → join group."""
    if not args:
        print("Usage: python botctl_helper.py quick <group_id>")
        sys.exit(1)
    group_id = args[0]
    name = f"bot-{group_id}"

    # Pick persona
    pick = _pick_persona()
    persona_id = pick[0] if pick else None
    persona_label = pick[1] if pick else ""

    # Create archive (with persona_id if selected)
    body: dict = {"name": name}
    if persona_id:
        body["persona_id"] = persona_id
    code, data = _api("POST", "/archives", body)
    if code not in (200, 201):
        print(f"[!] Failed to create archive (HTTP {code})")
        sys.exit(1)
    aid = data["archive_id"]
    detail = f"persona={persona_id}" if persona_id else "no persona"
    print(f"[OK] Archive: {aid}  {detail}")

    # Set as global current archive
    _api("PUT", "/bot/current-archive", {"archive_id": aid})

    # Join group
    join_body: dict = {"archive_id": aid, "persona_label": persona_label}
    code, _ = _api("POST", f"/bot/groups/{group_id}/join", join_body)
    if code == 200:
        print(f"[OK] Joined group {group_id}")
    else:
        print(f"[!] Join failed (HTTP {code})")
        sys.exit(1)


def cmd_join(args: list[str]):
    """Join a group. Default: current active archive, else group's latest, else global latest."""
    if not args:
        print("Usage: python botctl_helper.py join <group_id> [archive_id]")
        sys.exit(1)

    group_id = args[0]
    aid = args[1] if len(args) >= 2 else ""

    if not aid:
        # 1. Use global current archive
        code, cur = _api("GET", "/bot/current-archive")
        if code == 200 and cur.get("archive_id"):
            aid = cur["archive_id"]
            print(f"  Using current archive: {aid}")

        # 2. Fallback: latest persona already registered to this group
        if not aid:
            code, cfg = _api("GET", f"/bot/groups/{group_id}")
            if code == 200:
                personas = cfg.get("personas", [])
                if personas:
                    aid = personas[0]["archive_id"]
                    name = personas[0].get("archive_name", aid)
                    print(f"  Using group's latest persona: {name}")
                    print(f"  Archive: {aid}")

        # 3. Fallback: globally latest archive
        if not aid:
            code, data = _api("GET", "/archives")
            if code != 200:
                print("[!] Cannot list archives. Is the API running?")
                sys.exit(1)
            if not data:
                print("[!] No archives found. Use: botctl create <name>")
                sys.exit(1)
            aid = data[0]["archive_id"]
            print(f"  Using latest global archive: {aid}")

    code, _ = _api("POST", f"/bot/groups/{group_id}/join", {"archive_id": aid})
    if code == 200:
        print(f"[OK] Joined group {group_id}")
    else:
        print(f"[!] Join failed (HTTP {code})")
        sys.exit(1)


# ── "all" 确认状态(跨 bridge turn 保持) ──
# bridge 的会话机制每次从头执行 cmd_del,需要模块级变量跨越 turns
_pending_delete_group: dict[str, list[dict]] = {}  # group_id → personas


def cmd_del(args: list[str]):
    """Delete a persona/archive from a group. Interactive selection like switch."""
    global _pending_delete_group

    if not args:
        print("Usage: python botctl_helper.py del <group_id>")
        sys.exit(1)

    group_id = args[0]

    code, data = _api("GET", f"/bot/groups/{group_id}")
    if code == 404:
        print(f"[!] Group {group_id} not configured")
        sys.exit(1)
    if code != 200:
        print(f"[!] API error (HTTP {code})")
        sys.exit(1)

    personas = data.get("personas", [])
    if not personas:
        print(f"[!] No personas in group {group_id}")
        sys.exit(1)

    active_aid = data.get("active_archive_id", "")

    # Direct mode: botctl del <group_id> <archive_id>
    if len(args) >= 2:
        archive_id = args[1]
        if archive_id.lower() == "all":
            _do_delete_group(group_id, personas, confirmed=True)
            return
        _do_delete(group_id, archive_id, active_aid)
        return

    # ── 跨 turn 确认:上次选了 all,现在等 yes/no ──
    if group_id in _pending_delete_group:
        saved_personas = _pending_delete_group.pop(group_id)
        confirm = _botctl_input("  输入 'yes' 确认删除: ").strip()
        if confirm.lower() == "yes":
            _do_delete_group(group_id, saved_personas, confirmed=True)
        else:
            print("  Cancelled.")
        return

    # Interactive mode
    print()
    print(f"  Group: {group_id}")
    print(f"  Select persona to delete:")
    print()

    for i, p in enumerate(personas, 1):
        aid = p["archive_id"]
        name = p.get("archive_name", p.get("persona_label", "-")) or "-"
        is_active = p.get("is_active", 0)
        marker = " ★ (active)" if is_active else ""
        label = p.get("persona_label", "") or ""
        summary = p.get("last_summary", "") or ""
        print(f"  [{i}] {name}{marker}")
        print(f"      ID: {aid}")
        if label:
            print(f"      人设: {label}")
        if summary:
            print(f"      摘要: {summary}")
        print()

    print(f"  [all] 删除该群所有存档及群配置")
    print(f"  [0] Cancel")
    print()

    choice = _botctl_input("  Choice (number or 'all'): ").strip()

    if not choice or choice == "0":
        print("  Cancelled.")
        return

    if choice.lower() == "all":
        _show_delete_group_warning(group_id, personas)
        _pending_delete_group[group_id] = personas
        confirm = _botctl_input("  输入 'yes' 确认删除: ").strip()
        # bridge 模式: _botctl_input 会 raise SystemExit → 下面不会执行
        # CLI 模式: _botctl_input 返回用户输入 → 直接处理确认
        if confirm.lower() == "yes":
            _pending_delete_group.pop(group_id, None)
            _do_delete_group(group_id, personas, confirmed=True)
        else:
            _pending_delete_group.pop(group_id, None)
            print("  Cancelled.")
        return

    try:
        idx = int(choice) - 1
        if idx < 0 or idx >= len(personas):
            print(f"  [!] Invalid choice: {choice}")
            sys.exit(1)
    except ValueError:
        print(f"  [!] 无效输入: '{choice}'")
        print(f"  请输入数字编号、'all'(删除全部) 或 '0'(取消)")
        sys.exit(1)

    chosen = personas[idx]
    _do_delete(group_id, chosen["archive_id"], active_aid)


def _do_delete(group_id: str, archive_id: str, active_aid: str) -> None:
    """Remove persona from group, then soft-delete the archive."""
    if archive_id == active_aid:
        print(f"  [!] Cannot delete active persona ({archive_id})")
        print(f"  Switch to another persona first: botctl sw {group_id}")
        sys.exit(1)

    # 1. Remove persona from group
    code, _ = _api("DELETE", f"/bot/groups/{group_id}/personas/{archive_id}")
    if code == 200:
        print(f"  [OK] Removed persona from group: {archive_id}")
    else:
        print(f"  [!] Remove from group failed (HTTP {code})")

    # 2. Soft-delete the archive
    code, _ = _api("DELETE", f"/archives/{archive_id}")
    if code == 204:
        print(f"  [OK] Archive deleted: {archive_id}")
    else:
        print(f"  [!] Archive delete failed (HTTP {code})")


def _show_delete_group_warning(group_id: str, personas: list[dict]) -> None:
    """Print warning about deleting a group and all its archives."""
    n = len(personas)
    print()
    print(f"  ⚠ 即将删除群 {group_id} 下的全部 {n} 个存档:")
    for p in personas:
        aid = p["archive_id"]
        name = p.get("archive_name", p.get("persona_label", "-")) or "-"
        marker = " ★ (active)" if p.get("is_active") else ""
        print(f"    - {name} ({aid}){marker}")
    print()
    print(f"  此操作不可逆! 所有存档的工作区文件也将被清理。")
    print()


def _do_delete_group(group_id: str, personas: list[dict], confirmed: bool = False) -> None:
    """Delete all archives in a group and the group itself.
    When confirmed=True, skip the interactive yes/no prompt (used in bridge cross-turn flow)."""
    n = len(personas)
    if n == 0:
        print(f"  [!] No personas in group {group_id}")
        sys.exit(1)

    if not confirmed:
        _show_delete_group_warning(group_id, personas)
        confirm = _botctl_input("  输入 'yes' 确认删除: ").strip()
        if confirm.lower() != "yes":
            print("  Cancelled.")
            return

    print(f"  正在删除群 {group_id} ({n} 个存档)...")

    code, data = _api("DELETE", f"/bot/groups/{group_id}")
    if code == 200:
        deleted = data.get("deleted_archives", [])
        print(f"  [OK] 群 {group_id} 已删除")
        print(f"  [OK] {len(deleted)} 个存档已软删除, 工作区已清理")
    else:
        print(f"  [!] 删除失败 (HTTP {code}): {data}")
        sys.exit(1)


def cmd_archives(args: list[str] | None = None):
    """List all archives."""
    code, data = _api("GET", "/archives")
    if code != 200:
        print(f"[!] API error (HTTP {code})")
        sys.exit(1)
    if not data:
        print("No archives found.")
        return

    # Get current archive
    code_cur, cur = _api("GET", "/bot/current-archive")
    current_aid = cur.get("archive_id", "") if code_cur == 200 else ""

    # Get all groups to show usage
    code_grp, groups_data = _api("GET", "/bot/groups")
    group_map: dict[str, list[str]] = {}  # archive_id → [group_ids]
    if code_grp == 200:
        for g in groups_data.get("items", []):
            for p in g.get("personas", []):
                aid = p["archive_id"]
                if aid not in group_map:
                    group_map[aid] = []
                group_map[aid].append(g["group_id"])

    print()
    print(f"  {'ARCHIVE ID':<30} {'NAME':<24} {'STATUS':<12} {'GROUPS':<20}")
    print(f"  {'─'*30} {'─'*24} {'─'*12} {'─'*20}")
    for a in data:
        aid = a["archive_id"]
        name = a.get("name", "-") or "-"
        is_current = "★ CURRENT" if aid == current_aid else ""
        groups = ", ".join(group_map.get(aid, [])) or "-"
        aid_short = aid[:28] if len(aid) > 28 else aid
        name_short = name[:22] if len(name) > 22 else name
        groups_short = groups[:18] if len(groups) > 18 else groups
        print(f"  {aid_short:<30} {name_short:<24} {is_current:<12} {groups_short:<20}")
    print()


def cmd_delete_archive(args: list[str]):
    """Delete an archive (soft-delete). Interactive if no ID given."""
    if not args:
        # Interactive mode: list archives and let user pick
        code, data = _api("GET", "/archives")
        if code != 200 or not data:
            print("No archives found.")
            return

        code_cur, cur = _api("GET", "/bot/current-archive")
        current_aid = cur.get("archive_id", "") if code_cur == 200 else ""

        print()
        print("  Select archive to delete:")
        print()
        for i, a in enumerate(data, 1):
            aid = a["archive_id"]
            name = a.get("name", "-") or "-"
            marker = " ★ (current)" if aid == current_aid else ""
            print(f"  [{i}] {name}{marker}")
            print(f"      ID: {aid}")
            print()
        print("  [0] Cancel")
        print()

        choice = _botctl_input("  Choice (number): ").strip()

        if not choice or choice == "0":
            print("  Cancelled.")
            return

        try:
            idx = int(choice) - 1
            if idx < 0 or idx >= len(data):
                print(f"  [!] Invalid choice: {choice}")
                sys.exit(1)
        except ValueError:
            print(f"  [!] Invalid number: {choice}")
            sys.exit(1)

        archive_id = data[idx]["archive_id"]
        name = data[idx].get("name", archive_id)
    else:
        archive_id = args[0]
        name = archive_id

    code, _ = _api("DELETE", f"/archives/{archive_id}")
    if code == 204:
        print(f"[OK] Deleted: {name} ({archive_id})")
    elif code == 404:
        print(f"[!] Archive not found: {archive_id}")
    else:
        print(f"[!] Delete failed (HTTP {code})")


def cmd_cleanup(args: list[str]):
    """Clean up temp workspaces for an archive (helper sandboxes, .temp dirs).
    Keeps main workspace permanent files. Interactive if no ID given."""
    if not args:
        code, data = _api("GET", "/archives")
        if code != 200 or not data:
            print("No archives found.")
            return

        code_cur, cur = _api("GET", "/bot/current-archive")
        current_aid = cur.get("archive_id", "") if code_cur == 200 else ""

        print()
        print("  Select archive to clean up temp workspaces:")
        print()
        for i, a in enumerate(data, 1):
            aid = a["archive_id"]
            name = a.get("name", "-") or "-"
            marker = " ★ (current)" if aid == current_aid else ""
            print(f"  [{i}] {name}{marker}")
            print(f"      ID: {aid}")
            print()
        print("  [0] Cancel")
        print()

        choice = _botctl_input("  Choice (number): ").strip()

        if not choice or choice == "0":
            print("  Cancelled.")
            return

        try:
            idx = int(choice) - 1
            if idx < 0 or idx >= len(data):
                print(f"  [!] Invalid choice: {choice}")
                sys.exit(1)
        except ValueError:
            print(f"  [!] Invalid number: {choice}")
            sys.exit(1)

        archive_id = data[idx]["archive_id"]
        name = data[idx].get("name", archive_id)
    else:
        archive_id = args[0]
        name = archive_id

    code, data = _api("POST", f"/archives/{archive_id}/cleanup")
    if code == 200:
        groups = data.get("groups_scanned", 0)
        dirs = data.get("dirs_removed", 0)
        mb = data.get("bytes_freed", 0) / 1024 / 1024
        print(f"[OK] Cleaned {name} ({archive_id})")
        print(f"     Groups scanned: {groups}")
        print(f"     Temp dirs removed: {dirs}")
        print(f"     Freed: {mb:.1f} MB")
    elif code == 404:
        print(f"[!] Archive not found: {archive_id}")
    else:
        print(f"[!] Cleanup failed (HTTP {code})")


def cmd_help(args: list[str] | None = None):
    """Show all available commands."""
    lines = [
        "botctl commands:",
        "  create NAME [GROUP_ID]   Create new persona archive",
        "  list                     List all groups",
        "  list GROUP_ID            List personas in group (with summaries)",
        "  switch GROUP_ID [AID]    Switch active persona (interactive w/o AID)",
        "  recent GROUP_ID [N]      Show recent conversations (default 15)",
        "  leave GROUP_ID           Leave a group",
        "  del GROUP_ID [AID]       Delete persona/archive from group (interactive)",
        "  info GROUP_ID            Show group config (raw JSON)",
        "  admin GROUP_ID           Set admin group (for botctl via QQ)",
        "  admin off                Remove admin group",
        "  archives                 List all archives",
        "  delete-archive [AID]     Delete an archive (interactive w/o ID)",
        "  cleanup [AID]            Clean temp workspaces (helper sandboxes, .temp)",
        "  cleanup-kb-placeholders AID  Remove stale file placeholder KB nodes",
        "  quick GROUP_ID           One-step: create + join",
        "  join GROUP_ID [AID]      Join a group with existing archive",
        "  help                     Show this help",
    ]
    for line in lines:
        print(line)


def cmd_info(args: list[str]):
    """Show group config as raw JSON."""
    if not args:
        print("Usage: python botctl_helper.py info <group_id>")
        sys.exit(1)
    group_id = args[0]
    code, data = _api("GET", f"/bot/groups/{group_id}")
    if code != 200:
        print(f"[!] API error (HTTP {code})")
        sys.exit(1)
    print(json.dumps(data, ensure_ascii=False, indent=2))


def cmd_admin(args: list[str]):
    """Set or clear the admin group for in-QQ botctl commands."""
    if not args:
        print("Usage: botctl admin <group_id>")
        print("       botctl admin off    (remove admin group)")
        sys.exit(1)
    group_id = args[0]
    if group_id.lower() in ("off", "none", "remove", "clear"):
        code, _ = _api("DELETE", "/bot/admin-group")
        if code != 200:
            print(f"[!] Failed to remove admin group (HTTP {code})")
            sys.exit(1)
        print("[OK] Admin group removed")
        return
    code, _ = _api("PUT", "/bot/admin-group", {"group_id": group_id})
    if code != 200:
        print(f"[!] Failed to set admin group (HTTP {code})")
        sys.exit(1)
    print(f"[OK] Admin group set to {group_id}")
    print(f"     In that group, type: botctl <command>")


def cmd_cleanup_kb_placeholders(args: list[str]):
    """Clean stale KB file placeholder nodes for an archive."""
    if not args:
        print("Usage: botctl cleanup-kb-placeholders <archive_id>")
        sys.exit(1)
    archive_id = args[0]
    code, data = _api("POST", f"/archives/{archive_id}/cleanup/kb-placeholders")
    if code == 200:
        print(f"[OK] Cleaned KB placeholders for {archive_id}")
        print(f"     Groups scanned: {data.get('groups_scanned', 0)}")
        print(f"     Nodes removed: {data.get('nodes_removed', 0)}")
    elif code == 404:
        print(f"[!] Archive not found: {archive_id}")
    else:
        print(f"[!] KB placeholder cleanup failed (HTTP {code})")


# ── Headless input (for napcat_bridge multi-turn sessions) ────

_pending_input: str | None = None
_inside_run_command: bool = False


def set_pending_input(value: str | None) -> None:
    """Set the input value to inject on the next _botctl_input() call.
    Called by napcat_bridge to continue a multi-turn session."""
    global _pending_input
    _pending_input = value


def _botctl_input(prompt: str) -> str:
    """Get user input. When called from run_command() (bridge context):
    - If _pending_input is set: return it (session continuation)
    - Otherwise: print marker + raise SystemExit so bridge saves session
    When called from CLI: uses real stdin input()."""
    global _pending_input
    if _pending_input is not None:
        result = _pending_input
        _pending_input = None
        print(f"  {result}")
        return result
    if _inside_run_command:
        print(prompt)
        print("__BOTCTL_AWAIT__")
        raise SystemExit(0)
    try:
        return input(prompt)
    except (EOFError, KeyboardInterrupt):
        return ""


# ── Programmatic entry point (for napcat_bridge) ─────────────

def run_command(cmd_line: str) -> str:
    """Execute a botctl command string and return captured output.

    Called by napcat_bridge when someone types 'botctl <cmd>' in the admin group.
    Does NOT call sys.exit() — all SystemExit is caught.
    """
    if not cmd_line or not cmd_line.strip():
        return "Usage: botctl <command>\nType 'botctl help' for command list."

    parts = cmd_line.strip().split()
    cmd = parts[0].lower()
    args = parts[1:]

    # Map common aliases
    if cmd in ("ls", "list") and args:
        cmd = "list-personas"
    elif cmd == "ls":
        cmd = "list"

    handler = COMMANDS.get(cmd)
    if handler is None:
        return (
            f"Unknown command: {cmd}\n"
            f"Type 'botctl help' for available commands.\n"
            f"Available: {', '.join(sorted(COMMANDS))}"
        )

    global _inside_run_command
    _inside_run_command = True
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            handler(args)
    except SystemExit:
        pass  # handlers call sys.exit(1) on error; we capture output and continue
    except Exception as e:
        _inside_run_command = False
        return f"[!] Error: {type(e).__name__}: {e}"
    finally:
        _inside_run_command = False

    return buf.getvalue().rstrip() or "(no output)"


# ── Main (CLI) ────────────────────────────────────────────────
COMMANDS = {
    "create": cmd_create,
    "list-groups": cmd_list_groups,
    "list-personas": cmd_list_personas,
    "switch": cmd_switch,
    "leave": cmd_leave,
    "recent": cmd_recent,
    "quick": cmd_quick,
    "join": cmd_join,
    "help": cmd_help,
    "info": cmd_info,
    "admin": cmd_admin,
    "archives": cmd_archives,
    "delete-archive": cmd_delete_archive,
    "cleanup": cmd_cleanup,
    "cleanup-kb-placeholders": cmd_cleanup_kb_placeholders,
    "del": cmd_del,
    "list": cmd_list_groups,           # botctl list (no args) → list all groups
    "list-groups": cmd_list_groups,
    "list-personas": cmd_list_personas,
}

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("BotCtl Helper — backend for botctl.bat")
        print(f"Commands: {', '.join(COMMANDS)}")
        print("Use botctl.bat instead of calling this directly.")
        sys.exit(0)

    cmd = sys.argv[1]
    handler = COMMANDS.get(cmd)
    if handler is None:
        print(f"Unknown command: {cmd}")
        print(f"Available: {', '.join(COMMANDS)}")
        sys.exit(1)

    handler(sys.argv[2:])
