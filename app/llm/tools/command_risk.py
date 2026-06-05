from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CommandRiskDecision:
    allowed: bool
    reason: str = ""
    category: str = "allow"


DANGEROUS_KEYWORDS = {
    "diskpart",
    "regedit",
    "reg add",
    "reg delete",
    "reg import",
    "reg export",
    "net user",
    "net localgroup",
    "net share",
    "shutdown",
    "logoff",
    "takeown",
    "icacls",
    "cacls",
    "runas",
    "schtasks",
    "bcdedit",
    "bootcfg",
    "fsutil",
    "vssadmin",
    "wmic",
    "powershell",
    "taskkill",
    "tskill",
    "rundll32",
    "mshta",
    "cscript",
    "wscript",
    "msiexec",
}

DANGEROUS_EXACT_EXECUTABLES = {"format", "label", "convert", "compact", "cipher", "start"}

CMD_DESTRUCTIVE_OPS = {
    "del", "erase", "rmdir", "rd", "ren", "rename", "move", "mkdir", "md",
    "copy", "xcopy", "robocopy",
}

CMD_READ_OPS = {
    "dir", "type", "find", "findstr", "more", "sort", "comp", "fc", "where", "tree",
    "echo", "set", "cd", "chdir", "date", "time", "ver", "vol", "cls", "title", "color",
    "prompt", "path", "help", "assoc", "ftype", "driverquery", "systeminfo", "whoami",
    "hostname", "ipconfig", "ping", "tracert", "nslookup", "netstat", "arp", "getmac", "tasklist",
}

GCC_BLOCKED_FLAGS = {"-fplugin", "-wrapper", "-specs"}

PATH_RE = re.compile(
    r'([A-Za-z]:[\\/][^\s"\'&|><;]+)'
    r'|(?:^|\s)(\.\.[\\/][^\s"\'&|><;]+)'
    r'|"([^"]*?[\\/][^"]*?)"'
    r"|'([^']*?[\\/][^']*?)'"
)
REDIRECT_RE = re.compile(r'[>]{1,2}\s*([^\s&|<>]+)')


def _helper_has_env_workspace(ws_dir: str) -> bool:
    try:
        return bool(ws_dir) and (Path(ws_dir) / "_env").is_dir()
    except OSError:
        return False


def _helper_scope_reason(command: str, ws_dir: str, *, read_only: bool) -> str:
    normalized = command.replace("\\", "/")
    if "_env/" in normalized or "/_env/" in normalized or "\\_env\\" in command or _helper_has_env_workspace(ws_dir):
        return (
            "helpers cannot access project files through absolute or parent-directory paths. "
            "In environment/project work, commands run from the helper sandbox, not from the real project root. "
            "Use only relative paths that exist inside this sandbox, usually the staged local `_env/...` copy "
            "such as `_env/<project-relative-path>`; "
            "run commands as `cd _env/... && ...` or point tools at `_env/...` files. "
            "If the local `_env/...` copy is missing, locate the sandbox file first; then report the exact "
            "project-relative path the main process must fetch and resume with."
            "\n项目 helper 的命令只在沙箱中运行；用本地 _env 相对路径，缺副本时先定位，再向主线程请求精确项目路径。"
        )
    if read_only:
        return (
            "helpers cannot read files outside their sandbox via parent paths; "
            "use fetch_to_temp(source='main', paths=[...]) to copy main-workspace files into the helper workspace, "
            "then read the local filename without '..'."
            "\nhelper 读取主工作区文件需先 fetch_to_temp 到本地相对路径。"
        )
    return (
        "helpers cannot access .prev/ or files outside .temp/ directly; "
        "use fetch_to_temp(source='prev', paths=[...]) to request files "
        "from the previous session snapshot, or fetch_to_temp(source='main') "
        "for files from the permanent workspace."
        "\nhelper 只能访问沙箱；历史或主区文件需先获取到本地。"
    )


def _iter_path_tokens(command: str):
    for match in PATH_RE.finditer(command):
        for group in match.groups():
            if group:
                yield group.strip("\"'")


CMD_SWITCHES_WITH_VALUE = {"/a", "/u", "/t"}
CMD_SWITCHES_WITHOUT_VALUE = {"/d", "/e:on", "/e:off", "/f:on", "/f:off", "/q", "/s", "/v:on", "/v:off"}


def _cmd_payload_index(parts: list[str], start: int = 1) -> int:
    index = start
    while index < len(parts):
        token = parts[index].lower().rstrip("/")
        if token in ("/c", "/k"):
            return index + 1
        if token in CMD_SWITCHES_WITH_VALUE:
            index += 2
            continue
        if token in CMD_SWITCHES_WITHOUT_VALUE or token.startswith(("/e:", "/f:", "/v:")):
            index += 1
            continue
        return index
    return index


def _first_executable(parts: list[str]) -> str:
    if not parts:
        return ""
    exe = parts[0].lower()
    if exe in ("cmd", "cmd.exe"):
        index = _cmd_payload_index(parts)
        return parts[index].lower().rstrip("/") if index < len(parts) else ""
    return exe


def _main_thread_helper_sandbox_copy_reason() -> str:
    return (
        "Main workflow cannot promote files by copying from `.temp/_delegate_*` helper sandboxes. "
        "Helper sandboxes are internal execution areas; completed helper outputs are exposed through "
        "`file_map`, `main_available_files`, and `copy_stats.env_copied_files`. Use those main-workspace "
        "paths directly, or resume/replace the helper if the expected output is not listed. "
        "You may inspect helper status, but do not repair delivery by copying sandbox paths."
        "\n主流程不要从 _delegate_* 沙箱复制产物；以 helper 结果里的主区路径和 copy_stats 为准。"
    )


def _main_thread_copies_from_helper_sandbox(command: str) -> bool:
    """Detect main-thread attempts to use helper sandboxes as a delivery source.

    Reading/log inspection can still be useful, so this only targets promotion-like
    copy/move operations, including common Python one-liners.
    """
    normalized = command.replace("\\", "/").lower()
    if "_delegate_" not in normalized:
        return False

    parts = command.split()
    first_exe = _first_executable(parts)
    if first_exe in {"copy", "xcopy", "robocopy", "move", "ren", "rename"}:
        return True

    copy_patterns = (
        "shutil.copy(",
        "shutil.copy2(",
        "shutil.copyfile(",
        "shutil.copytree(",
        "os.replace(",
        "os.rename(",
        "pathlib.path(",
        ".replace(",
        ".rename(",
    )
    if first_exe.startswith("python") or first_exe in {"py", "python3"}:
        return any(pattern in normalized for pattern in copy_patterns)

    if first_exe in {"cmd", "cmd.exe"}:
        return any(
            re.search(r"\b" + re.escape(op) + r"\b", normalized)
            for op in ("copy", "xcopy", "robocopy", "move", "ren", "rename")
        )

    return False


def helper_sandbox_copy_error(command: str, *, is_main_thread: bool = True) -> str | None:
    if is_main_thread and _main_thread_copies_from_helper_sandbox(command):
        return _main_thread_helper_sandbox_copy_reason()
    return None


def analyze_command(command: str, ws_dir: str, *, is_main_thread: bool = True) -> CommandRiskDecision:
    cmd_lower = command.lower()
    for keyword in DANGEROUS_KEYWORDS:
        if re.search(r"\b" + re.escape(keyword) + r"\b", cmd_lower):
            return CommandRiskDecision(
                False,
                f"security blocked: command uses restricted system operation '{keyword}'.\n安全策略拦截该系统操作。",
                "blocked_keyword",
            )

    if helper_sandbox_copy_error(command, is_main_thread=is_main_thread):
        return CommandRiskDecision(
            False,
            _main_thread_helper_sandbox_copy_reason(),
            "helper_sandbox_copy",
        )

    parts = command.split()
    first_exe = _first_executable(parts)
    if first_exe in DANGEROUS_EXACT_EXECUTABLES:
        return CommandRiskDecision(
            False,
            f"security blocked: command uses restricted executable '{first_exe}'.\n安全策略拦截该可执行程序。",
            "blocked_keyword",
        )

    exe = parts[0].lower() if parts else ""
    if exe in ("cmd", "cmd.exe"):
        return _analyze_cmd(command, ws_dir, is_main_thread=is_main_thread)
    if exe in ("gcc", "g++", "gcc.exe", "g++.exe"):
        return _analyze_gcc(command, ws_dir)
    if exe in CMD_READ_OPS or exe in CMD_DESTRUCTIVE_OPS:
        return _analyze_bare_cmd_builtin(command, ws_dir, is_main_thread=is_main_thread)
    if has_redirect_to_outside(command, ws_dir):
        return CommandRiskDecision(False, "security blocked: refusing redirect outside workspace", "outside_redirect")
    return CommandRiskDecision(True)


def _analyze_bare_cmd_builtin(command: str, ws_dir: str, *, is_main_thread: bool) -> CommandRiskDecision:
    parts = command.split()
    first_token = parts[0].lower().rstrip("/") if parts else ""

    if not is_main_thread and touches_prev_or_outside(command, ws_dir):
        if first_token in CMD_READ_OPS:
            return CommandRiskDecision(
                False,
                _helper_scope_reason(command, ws_dir, read_only=True),
                "helper_scope",
            )
        return CommandRiskDecision(
            False,
            _helper_scope_reason(command, ws_dir, read_only=False),
            "helper_scope",
        )

    if first_token in CMD_READ_OPS:
        if has_redirect_to_outside(command, ws_dir):
            return CommandRiskDecision(False, "security blocked: refusing redirect outside workspace", "outside_redirect")
        return CommandRiskDecision(True)

    if first_token in CMD_DESTRUCTIVE_OPS:
        for path in extract_paths(command):
            if is_abs_outside(path, ws_dir):
                return CommandRiskDecision(
                    False,
                    f"security blocked: refusing {first_token} outside workspace ({path})",
                    "outside_destructive",
                )
        if has_redirect_to_outside(command, ws_dir):
            return CommandRiskDecision(False, "security blocked: refusing redirect outside workspace", "outside_redirect")

    return CommandRiskDecision(True)


def _analyze_cmd(command: str, ws_dir: str, *, is_main_thread: bool) -> CommandRiskDecision:
    parts = command.split()
    actual_start = _cmd_payload_index(parts)
    if actual_start >= len(parts):
        return CommandRiskDecision(True)

    actual_parts = parts[actual_start:]
    first_token = actual_parts[0].lower().rstrip("/")

    if not is_main_thread and touches_prev_or_outside(command, ws_dir):
        if not is_main_thread and first_token in CMD_READ_OPS:
            return CommandRiskDecision(
                False,
                _helper_scope_reason(command, ws_dir, read_only=True),
                "helper_scope",
            )
        return CommandRiskDecision(
            False,
            _helper_scope_reason(command, ws_dir, read_only=False),
            "helper_scope",
        )

    if first_token in CMD_READ_OPS:
        if first_token == "echo" and has_redirect_to_outside(command, ws_dir):
            return CommandRiskDecision(
                False,
                "security blocked: refusing redirect outside workspace.\n安全策略拦截沙箱外重定向写入。",
                "outside_redirect",
            )
        return CommandRiskDecision(True)

    if first_token in CMD_DESTRUCTIVE_OPS:
        for path in extract_paths(command):
            if is_abs_outside(path, ws_dir):
                return CommandRiskDecision(
                    False,
                    f"security blocked: refusing {first_token} outside workspace ({path}).\n安全策略拦截沙箱外文件操作。",
                    "outside_destructive",
                )

    if has_redirect_to_outside(command, ws_dir):
        return CommandRiskDecision(
            False,
            "security blocked: refusing redirect outside workspace.\n安全策略拦截沙箱外重定向写入。",
            "outside_redirect",
        )

    return CommandRiskDecision(True)


def _analyze_gcc(command: str, ws_dir: str) -> CommandRiskDecision:
    parts = command.split()
    for index, part in enumerate(parts):
        part_lower = part.lower()
        base = part_lower.split("=")[0].rstrip("/")
        if base in GCC_BLOCKED_FLAGS:
            return CommandRiskDecision(
                False,
                f"security blocked: GCC flag '{part}' is restricted.\n安全策略拦截该 GCC 参数。",
                "gcc_blocked_flag",
            )
        if part.startswith("@") and len(part) > 1:
            return CommandRiskDecision(
                False,
                "security blocked: GCC @file argument files are restricted.\n安全策略拦截 GCC @file 参数。",
                "gcc_at_file",
            )
        if part == "-o" and index + 1 < len(parts):
            output_path = parts[index + 1]
            if is_abs_outside(output_path, ws_dir):
                return CommandRiskDecision(
                    False,
                    f"security blocked: GCC -o output must stay inside the workspace ({output_path}).\nGCC 输出路径必须在工作区内。",
                    "outside_output",
                )
        elif part.startswith("-o") and len(part) > 2:
            output_path = part[2:]
            if is_abs_outside(output_path, ws_dir):
                return CommandRiskDecision(
                    False,
                    f"security blocked: GCC -o output must stay inside the workspace ({output_path}).\nGCC 输出路径必须在工作区内。",
                    "outside_output",
                )

    if has_redirect_to_outside(command, ws_dir):
        return CommandRiskDecision(
            False,
            "security blocked: refusing redirect outside workspace.\n安全策略拦截沙箱外重定向写入。",
            "outside_redirect",
        )
    return CommandRiskDecision(True)


def extract_paths(command: str) -> list[str]:
    return list(_iter_path_tokens(command))


def is_abs_outside(path_str: str, ws_dir: str) -> bool:
    path = Path(path_str)
    if path.is_absolute():
        try:
            resolved = path.resolve()
            workspace = Path(ws_dir).resolve()
            resolved.relative_to(workspace)
            return False
        except (ValueError, OSError):
            return True
    try:
        resolved = (Path(ws_dir) / path).resolve()
        resolved.relative_to(Path(ws_dir).resolve())
        return False
    except (ValueError, OSError):
        return True


def has_redirect_to_outside(command: str, ws_dir: str) -> bool:
    match = REDIRECT_RE.search(command)
    if not match:
        return False
    dest = match.group(1).strip("\"'")
    if dest.replace("\\", "/").lower() in {"/dev/null", "nul", "null"}:
        return False
    return is_abs_outside(dest, ws_dir)


def touches_prev_or_outside(command: str, ws_dir: str) -> bool:
    workspace = Path(ws_dir).resolve()
    for path_str in _iter_path_tokens(command):
        if not path_str:
            continue
        path = Path(path_str)
        try:
            resolved = path.resolve() if path.is_absolute() else (workspace / path).resolve()
        except (OSError, ValueError):
            if ".prev" in path_str.replace("\\", "/").split("/"):
                return True
            continue
        try:
            resolved.relative_to(workspace)
        except ValueError:
            return True
        parts = str(resolved.relative_to(workspace)).replace("\\", "/").split("/")
        if ".prev" in parts:
            return True
    return re.search(r"\b\.prev[/\\]", command) is not None
