"""Copy helper outputs back into the main workspace."""
from __future__ import annotations

import logging
import os
import re
import shutil

from app.core.filesystem import FileRegistry
from app.core.filesystem.models import FileKind, FileStatus, Visibility
from app.core.filesystem.transfers import intake_workspace_file

log = logging.getLogger(__name__)


_INTERNAL_PATH_PREFIX_RE = re.compile(
    r"(?<![A-Za-z0-9_\-./])(?:\./)?_env/(?=[A-Za-z0-9_])"
)


def _scrub_internal_paths(text: str) -> tuple[str, int]:
    if "_env/" not in text:
        return text, 0
    new_text, count = _INTERNAL_PATH_PREFIX_RE.subn("", text)
    return new_text, count


def _sync_delegate_globals() -> None:
    from app.llm.tools import delegate as _delegate
    globals().update({
        name: value
        for name, value in vars(_delegate).items()
        if not name.startswith("__") and name not in {"_copy_results_to_main"}
    })


_SOURCE_EXTENSIONS = {".py", ".c", ".cpp", ".h", ".js", ".ts", ".sh", ".bat", ".cmd", ".ps1"}

# Large test-artifact threshold: helper outputs above this size are not copied back into the permanent main workspace.
# Large files are usually test data or transient products; they should not be delivered to users by default,
# and should be cleaned when the helper workspace is cleaned.
# 50MB is a practical ceiling: normal documents/images are smaller; truly huge outputs are usually test artifacts.
_RESULT_COPY_BACK_MAX_SIZE = 50 * 1024 * 1024  # 50MB

# Hard cap for files copied back from one helper. Producing 50+ files at once usually indicates
# a runaway loop or workspace-pollution cascade, so copy-back is blocked to protect the main workspace.
_RESULT_COPY_BACK_MAX_FILES = 50
_TEXT_REPAIR_EXTENSIONS = {".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".jsonl", ".yaml", ".yml", ".xml"}
_TEXT_REPAIR_MAX_SIZE = 2 * 1024 * 1024


def _copyback_registry(main_ws: str) -> FileRegistry | None:
    try:
        return FileRegistry.load(scope_id=f"workspace:{os.path.abspath(main_ws)}", workspace_root=main_ws)
    except Exception:
        return None


def _register_copyback_file(
    registry: FileRegistry | None,
    workspace_path: str,
    *,
    task_id: str,
    helper_kind: str,
    kind: FileKind,
    visibility: Visibility,
    status: FileStatus = FileStatus.READY,
) -> None:
    if registry is None or not workspace_path:
        return
    try:
        intake_workspace_file(
            registry,
            workspace_path,
            kind=kind,
            status=status,
            visibility=visibility,
            owner_task_id=task_id,
            helper_kind=helper_kind,
            metadata={"source": "delegate_copyback"},
        )
    except Exception:
        return


def _copy2_with_text_repair(src: str, dst: str, stats: dict, display_name: str) -> None:
    ext = os.path.splitext(str(src or ""))[1].lower()
    if ext not in _TEXT_REPAIR_EXTENSIONS:
        shutil.copy2(src, dst)
        return
    try:
        size = os.path.getsize(src)
    except OSError:
        shutil.copy2(src, dst)
        return
    if size > _TEXT_REPAIR_MAX_SIZE:
        shutil.copy2(src, dst)
        return
    try:
        raw = open(src, "r", encoding="utf-8", errors="replace").read()
    except OSError:
        shutil.copy2(src, dst)
        return
    try:
        repaired, info = repair_common_mojibake_text(raw)
    except Exception:
        repaired, info = raw, None
    scrubbed, scrub_count = _scrub_internal_paths(repaired)
    if not info and scrub_count == 0:
        shutil.copy2(src, dst)
        return
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    with open(dst, "w", encoding="utf-8", newline="") as fh:
        fh.write(scrubbed)
    if info:
        stats.setdefault("text_mojibake_repaired", []).append({
            "file": display_name,
            **info,
        })
    if scrub_count:
        stats.setdefault("internal_paths_scrubbed", []).append({
            "file": display_name,
            "occurrences": scrub_count,
        })


def _normalize_declared_project_path(value: str) -> str:
    norm = str(value or "").replace("\\", "/").strip().lstrip("./")
    if norm.startswith("_env/"):
        norm = norm[len("_env/"):]
    return norm.strip("/")


def _safe_join_project_rel(root: str, rel_path: str) -> str | None:
    if not rel_path or rel_path.startswith(("_helpers_shared/", "_shared/")):
        return None
    root_abs = os.path.abspath(root)
    candidate = os.path.abspath(os.path.join(root_abs, rel_path.replace("/", os.sep)))
    try:
        common = os.path.commonpath([root_abs, candidate])
    except ValueError:
        return None
    if common != root_abs:
        return None
    return candidate


def _copy_results_to_main(
    helper_ws: str,
    main_ws: str,
    task_id: str,
    *,
    fork_snapshot: dict[str, tuple[float, int]] | None = None,
    declared_files: set[str] | None = None,
    allow_shared_merge: bool = True,
    expected_outputs: list[str] | None = None,
    is_race_aborted_twin: bool = False,
    suppress_declared_missing: bool = False,
    copy_unexpected_env_files: bool = False,
    helper_kind: str = "",
) -> tuple[list[str], dict, list[dict]]:
    """Copy helper-produced files back to main workspace.

    Prefer the helper-reported declared_files list;
    fall back to workspace diff only when no declared list is available.

    Returns:
        (copied_names, stats, file_map). file_map is a list[dict],
        姣忔潯 {"helper_name": ..., "main_name": ..., "shared_name": ...|None}
        mapping helper filenames to main workspace filenames so the main process does not guess.

    2026-05-15 P73: outputs listed in expected_outputs default to clean names without task_id prefixes.
    Cause: for simple outputs such as sin_curve.png, prefixing the file name forced extra inspect/copy cleanup
    and made the user-facing artifact name differ from the expected name.
    Fix: when expected_outputs names a basename and the main workspace has no conflict, copy using that clean name.
    This keeps simple final artifacts easy to find and avoids unnecessary main-process repair loops.
    
    """
    _sync_delegate_globals()
    helper_kind_norm = str(helper_kind or "").strip().lower()
    registry = _copyback_registry(main_ws)

    # Helper source files are copied back too, but source files get helper_{task_id}_ prefixes.
    # Earlier source-extension skipping forced the main process to regenerate helper code from reports.
    # Prefixing lets search_files reuse helper source while distinguishing helper outputs from native main files.
    # This keeps code evidence visible without overwriting existing main-workspace source.
    stats = {
        "skipped_unchanged": 0,
        "skipped_large": [],   # list[(name, size)]
        "skipped_empty": [],   # list[name]; zero-byte files are not copied
        "skipped_source": 0,        # legacy compatibility field; source is no longer skipped
        "copied_source_count": 0,   # source files copied with helper_ prefix
        "total_visited": 0,         # files visited, including skipped/filtered files
        "capped": False,
        "shared_merge_allowed": allow_shared_merge,
        "text_mojibake_repaired": [],
    }
    if not helper_ws or not main_ws or not os.path.isdir(helper_ws):
        return [], stats, []
    os.makedirs(main_ws, exist_ok=True)
    copied: list[str] = []
    file_map: list[dict] = []  # helper_name -> main_name -> shared_name
    _declared_by_base: dict[str, str] = {}
    _declared_norms: set[str] = set()
    _declared_bases: set[str] = set()
    for _decl in declared_files or set():
        if not isinstance(_decl, str):
            continue
        _decl_norm = _decl.replace("\\", "/").strip()
        if not _decl_norm:
            continue
        _declared_norms.add(_decl_norm)
        _declared_bases.add(os.path.basename(_decl_norm))
        _declared_by_base.setdefault(os.path.basename(_decl_norm), _decl_norm)

    def _declared_dir_prefix(raw: object) -> str | None:
        if not isinstance(raw, str):
            return None
        norm = raw.replace("\\", "/").strip().lstrip("./")
        if not norm:
            return None
        if norm.endswith("/"):
            return norm.rstrip("/") + "/"
        # Treat extensionless _env/_shared/_helpers_shared paths as directory declarations.
        # This remains conservative for ordinary bare names such as "report" because those
        # can be legitimate file names without extensions.
        head = norm.split("/", 1)[0]
        if "/" in norm and head in {"_env", "_shared", "_helpers_shared"} and not os.path.splitext(os.path.basename(norm))[1]:
            return norm.rstrip("/") + "/"
        if norm in {"_env", "_shared", "_helpers_shared"}:
            return norm.rstrip("/") + "/"
        return None

    _declared_dir_prefixes = {
        prefix for prefix in (_declared_dir_prefix(x) for x in declared_files or set()) if prefix
    }

    candidates: list[tuple[str, str, int, bool]] = []  # (name, src_path, size, is_source)
    # Merge helper _helpers_shared/ outputs back into the main workspace.
    # These files keep their paths so later helpers can see shared resources during workspace copy.
    # Other helper subdirectories remain non-recursive unless explicitly handled.
    helpers_shared_src = os.path.join(helper_ws, "_helpers_shared")
    if allow_shared_merge and os.path.isdir(helpers_shared_src):
        helpers_shared_dst = os.path.join(main_ws, "_helpers_shared")
        shared_copied: list[str] = []
        shared_skipped_unchanged = 0
        try:
            os.makedirs(helpers_shared_dst, exist_ok=True)
            for root, _dirs, files in os.walk(helpers_shared_src):
                rel_root = os.path.relpath(root, helpers_shared_src)
                for sf in files:
                    src_path = os.path.join(root, sf)
                    rel_name = sf if rel_root == "." else os.path.normpath(os.path.join(rel_root, sf))
                    if sf.startswith(".") or sf.endswith((".session_tag", ".helper_summary.txt")):
                        continue
                    if sf in {".read_history.json", ".todos.json", ".edit_history.json", ".session_tag"}:
                        continue
                    if sf.endswith(("_call_count.json", "_history.json", "_count.json", "_rewrite_count.json")):
                        continue
                    rel_name_posix = rel_name.replace(os.sep, "/")
                    rel_base = os.path.basename(rel_name_posix)
                    _declared_shared_name = f"_helpers_shared/{rel_name_posix}"
                    _is_declared_shared_output = (
                        _declared_shared_name in _declared_norms
                        or rel_name_posix in _declared_norms
                        or rel_base in _declared_bases
                        or any(
                            _declared_shared_name.startswith(_prefix)
                            or rel_name_posix.startswith(_prefix)
                            for _prefix in _declared_dir_prefixes
                        )
                    )
                    if (
                        rel_root == "."
                        and os.path.splitext(sf)[1].lower() in {".py", ".pyw", ".pyc", ".pyo"}
                        and not _is_declared_shared_output
                    ):
                        continue
                    if (
                        _is_internal_helper_artifact(f"_helpers_shared/{rel_name_posix}")
                        and not _is_declared_shared_output
                    ):
                        continue
                    # fork_snapshot diff: copy only shared files that this helper actually added or changed.
                    # This prevents stale _helpers_shared/ directories from previous runs being reported as new outputs.
                    # Without this, old shared files can pollute final delivery and confuse the main process.
                    try:
                        cur_st = os.stat(src_path)
                    except OSError:
                        continue
                    if fork_snapshot is not None:
                        _snap_key = f"_helpers_shared/{rel_name_posix}"
                        prev = fork_snapshot.get(_snap_key)
                        if prev is not None:
                            prev_mtime, prev_size = prev
                            if prev_size == cur_st.st_size and abs(cur_st.st_mtime - prev_mtime) < 1.0:
                                shared_skipped_unchanged += 1
                                continue
                    dst_path = os.path.join(helpers_shared_dst, rel_name)
                    try:
                        if cur_st.st_size > 1_000_000:
                            debug.log(
                                f"delegate.{task_id}.helpers_shared_skip",
                                f"skipped oversized shared file: {rel_name} ({cur_st.st_size} bytes)",
                            )
                            continue
                    except OSError:
                        continue
                    os.makedirs(os.path.dirname(dst_path), exist_ok=True)
                    _copy2_with_text_repair(src_path, dst_path, stats, f"_helpers_shared/{rel_name_posix}")
                    shared_copied.append(f"_helpers_shared/{rel_name_posix}")
                    copied.append(f"_helpers_shared/{rel_name_posix}")
                    _needs_declared_internal_mapping = (
                        rel_root == "."
                        and os.path.splitext(sf)[1].lower() in {".py", ".pyw", ".pyc", ".pyo"}
                    )
                    if _is_declared_shared_output and _needs_declared_internal_mapping and not any(
                        isinstance(m, dict)
                        and m.get("main_name") == _declared_shared_name
                        and m.get("shared_name") == _declared_shared_name
                        for m in file_map
                    ):
                        file_map.append({
                            "helper_name": rel_base,
                            "main_name": _declared_shared_name,
                            "shared_name": _declared_shared_name,
                        })
            if shared_copied or shared_skipped_unchanged:
                _msg = (
                    f"merged _helpers_shared/ to main workspace: "
                    f"{', '.join(shared_copied[:20])}"
                    + (f" and {len(shared_copied)} files total" if len(shared_copied) > 20 else "")
                )
                if shared_skipped_unchanged:
                    _msg += f"; skipped {shared_skipped_unchanged} unchanged (fork-snapshot diff)"
                debug.log(
                    f"delegate.{task_id}.helpers_shared",
                    _msg,
                )
            stats["shared_skipped_unchanged"] = shared_skipped_unchanged
        except OSError:
            log.exception("failed to merge _helpers_shared back to main")

    env_src = os.path.join(helper_ws, "_env")
    if os.path.isdir(env_src):
        env_dst = os.path.join(main_ws, "_env")
        env_copied: list[str] = []
        env_skipped_unchanged = 0
        env_skipped_unexpected_new: list[str] = []
        env_skipped_read_evidence: list[str] = []
        _declared_env_outputs: set[str] = set()
        for _raw in list(declared_files or set()) + list(expected_outputs or []):
            if not isinstance(_raw, str):
                continue
            _norm = _raw.replace("\\", "/").strip().lstrip("./")
            if _norm.startswith("_env/"):
                _declared_env_outputs.add(_norm[len("_env/"):])
            elif _norm == "_env":
                _declared_env_outputs.add("")
            elif _norm and not _norm.startswith(("_helpers_shared/", "_shared/")):
                _declared_env_outputs.add(_norm)
        try:
            os.makedirs(env_dst, exist_ok=True)
            for root, dirs, files in os.walk(env_src):
                dirs[:] = [d for d in dirs if d not in {"__pycache__"}]
                rel_root = os.path.relpath(root, env_src)
                for fname in files:
                    if fname.startswith(".") and fname != ".manifest.json":
                        continue
                    src_path = os.path.join(root, fname)
                    rel_name = fname if rel_root == "." else os.path.normpath(os.path.join(rel_root, fname))
                    rel_posix = rel_name.replace(os.sep, "/")
                    if helper_kind_norm in {"read", "ocr"}:
                        env_skipped_read_evidence.append(f"_env/{rel_posix}")
                        continue
                    try:
                        cur_st = os.stat(src_path)
                    except OSError:
                        continue
                    if cur_st.st_size > _RESULT_COPY_BACK_MAX_SIZE:
                        stats["skipped_large"].append((f"_env/{rel_posix}", cur_st.st_size))
                        continue
                    dst_path = os.path.join(env_dst, rel_name)
                    if fork_snapshot is not None:
                        prev = fork_snapshot.get(f"_env/{rel_posix}")
                        if prev is not None:
                            prev_mtime, prev_size = prev
                            if prev_size == cur_st.st_size and abs(cur_st.st_mtime - prev_mtime) < 1.0:
                                try:
                                    if os.path.isfile(dst_path) and open(src_path, "rb").read() == open(dst_path, "rb").read():
                                        env_skipped_unchanged += 1
                                        continue
                                except OSError:
                                    pass
                        elif (
                            rel_posix not in _declared_env_outputs
                            and not copy_unexpected_env_files
                        ):
                            env_skipped_unexpected_new.append(f"_env/{rel_posix}")
                            continue
                    os.makedirs(os.path.dirname(dst_path), exist_ok=True)
                    _copy2_with_text_repair(src_path, dst_path, stats, f"_env/{rel_posix}")
                    env_main_name = f"_env/{rel_posix}"
                    env_copied.append(env_main_name)
                    copied.append(env_main_name)
                    _register_copyback_file(
                        registry,
                        env_main_name,
                        task_id=task_id,
                        helper_kind=helper_kind_norm,
                        kind=FileKind.HELPER_OUTPUT,
                        visibility=Visibility.PROJECT,
                    )
                    file_map.append({
                        "helper_name": rel_posix,
                        "main_name": env_main_name,
                        "shared_name": None,
                    })
            if env_copied or env_skipped_unchanged:
                debug.log(
                    f"delegate.{task_id}.env_copy",
                    f"merged _env/ files: {env_copied[:20]} "
                    f"(copied={len(env_copied)}, skipped_unchanged={env_skipped_unchanged})",
                )
            stats["env_copied_count"] = len(env_copied)
            stats["env_copied_files"] = env_copied
            if copy_unexpected_env_files:
                stats["env_internal_evidence_files"] = env_copied
            stats["env_skipped_unchanged"] = env_skipped_unchanged
            stats["env_skipped_unexpected_new"] = env_skipped_unexpected_new[:50]
            stats["env_skipped_read_evidence"] = env_skipped_read_evidence[:50]
            if env_skipped_read_evidence:
                debug.log(
                    f"delegate.{task_id}.env_read_evidence_guard",
                    "read-helper _env writes were not merged as project files: "
                    f"{env_skipped_read_evidence[:20]}"
                    + (f" and {len(env_skipped_read_evidence)} files total" if len(env_skipped_read_evidence) > 20 else ""),
                )
            if env_skipped_unexpected_new:
                debug.log(
                    f"delegate.{task_id}.env_copy_guard",
                    "skipped new _env/ files not declared in expected_outputs: "
                    f"{env_skipped_unexpected_new[:20]}"
                    + (f" and {len(env_skipped_unexpected_new)} files total" if len(env_skipped_unexpected_new) > 20 else ""),
                )
        except OSError:
            log.exception("failed to merge _env back to main")

    # Environment helpers sometimes construct declared project-relative files
    # directly in their sandbox, for example native/textutil.c, because helpers
    # do not own env_apply_create. Preserve those exact declared paths as staged
    # _env copies so the main process can inspect and apply them deliberately.
    declared_project_paths: set[str] = set()
    for _raw in list(declared_files or set()) + list(expected_outputs or []):
        if not isinstance(_raw, str):
            continue
        _rel = _normalize_declared_project_path(_raw)
        if _rel and not _rel.startswith(("_helpers_shared/", "_shared/")):
            declared_project_paths.add(_rel)
    if helper_kind_norm in {"read", "ocr"}:
        declared_project_paths.clear()
    if declared_project_paths:
        staged_copied: list[str] = []
        staged_skipped_unchanged = 0
        try:
            env_dst = os.path.join(main_ws, "_env")
            os.makedirs(env_dst, exist_ok=True)
            for rel_posix in sorted(declared_project_paths):
                src_path = _safe_join_project_rel(helper_ws, rel_posix)
                dst_path = _safe_join_project_rel(env_dst, rel_posix)
                if not src_path or not dst_path or not os.path.isfile(src_path):
                    continue
                # Skip staging bare-name files (no subdir) — the normal copyback delivers them
                # directly to the main workspace root, making an _env/ duplicate unnecessary.
                if os.path.basename(rel_posix) == rel_posix:
                    stats.setdefault("env_stage_skipped_already_delivered", []).append(rel_posix)
                    continue
                try:
                    cur_st = os.stat(src_path)
                except OSError:
                    continue
                if cur_st.st_size > _RESULT_COPY_BACK_MAX_SIZE:
                    stats["skipped_large"].append((f"_env/{rel_posix}", cur_st.st_size))
                    continue
                if cur_st.st_size == 0:
                    stats["skipped_empty"].append(f"_env/{rel_posix}")
                    continue
                if fork_snapshot is not None:
                    prev = fork_snapshot.get(rel_posix)
                    if prev is not None:
                        prev_mtime, prev_size = prev
                        if prev_size == cur_st.st_size and abs(cur_st.st_mtime - prev_mtime) < 1.0:
                            staged_skipped_unchanged += 1
                            continue
                os.makedirs(os.path.dirname(dst_path), exist_ok=True)
                _copy2_with_text_repair(src_path, dst_path, stats, f"_env/{rel_posix}")
                staged_main_name = f"_env/{rel_posix}"
                staged_copied.append(staged_main_name)
                copied.append(staged_main_name)
                _register_copyback_file(
                    registry,
                    staged_main_name,
                    task_id=task_id,
                    helper_kind=helper_kind_norm,
                    kind=FileKind.HELPER_OUTPUT,
                    visibility=Visibility.PROJECT,
                )
                file_map.append({
                    "helper_name": rel_posix,
                    "main_name": staged_main_name,
                    "shared_name": None,
                })
            if staged_copied or staged_skipped_unchanged:
                debug.log(
                    f"delegate.{task_id}.env_declared_copy",
                    f"staged declared project-relative files: {staged_copied[:20]} "
                    f"(copied={len(staged_copied)}, skipped_unchanged={staged_skipped_unchanged})",
                )
            if staged_copied:
                stats["env_copied_count"] = int(stats.get("env_copied_count") or 0) + len(staged_copied)
                existing_env_files = list(stats.get("env_copied_files") or [])
                existing_env_files.extend(staged_copied)
                stats["env_copied_files"] = existing_env_files
            if staged_skipped_unchanged:
                stats["env_skipped_unchanged"] = int(stats.get("env_skipped_unchanged") or 0) + staged_skipped_unchanged
        except OSError:
            log.exception("failed to stage declared project-relative helper outputs")

    for name in os.listdir(helper_ws):
        src = os.path.join(helper_ws, name)
        if not os.path.isfile(src):
            continue
        if name.startswith(".helper_"):
            continue
        # Skip helper metadata files generated for self-checking, editing history, and todo tracking.
        # They are not user-facing outputs and should not pollute the main workspace.
        # Earlier traces copied these JSON files back from every helper, creating noisy artifacts.
        # Common metadata patterns: .read_history.json / .todos.json / .edit_history.json.
        # Matched metadata patterns: `.read_history.json` / `.todos.json` / `.edit_history.json`.
        if name in (".read_history.json", ".todos.json", ".edit_history.json"):
            continue
        if name.endswith(("_call_count.json", "_history.json", "_count.json", "_rewrite_count.json")):
            continue
        stats["total_visited"] += 1
        ext = os.path.splitext(name)[1].lower()
        is_source = ext in _SOURCE_EXTENSIONS
        try:
            st = os.stat(src)
        except OSError:
            continue
        size = st.st_size
        if size > _RESULT_COPY_BACK_MAX_SIZE:
            stats["skipped_large"].append((name, size))
            continue
        if size == 0:
            stats["skipped_empty"].append(name)
            continue

        # Core fork diff: skip files that existed in the fork snapshot and were not changed.
        if fork_snapshot is not None:
            prev = fork_snapshot.get(name)
            if prev is not None:
                prev_mtime, prev_size = prev
                if prev_size == size and abs(st.st_mtime - prev_mtime) < 1.0:
                    stats["skipped_unchanged"] += 1
                    continue

        candidates.append((name, src, size, is_source))

    # If the helper explicitly declared output files, copy only those declared files.
    # Workspace diff can include compiler intermediates, fetched references, and scratch files;
    # treating all of them as outputs can trigger the file cap and discard real deliverables.
    # The helper-declared file list is the intended delivery boundary.
    if declared_files:
        _declared_bases = {
            os.path.basename(str(x).replace("\\", "/")).lower()
            for x in declared_files
            if str(x).strip()
        }
        declared_candidates = [
            (n, s, sz, iss) for n, s, sz, iss in candidates
            if n in declared_files or os.path.basename(n).lower() in _declared_bases
        ]
        missing = declared_files - {n for n, _, _, _ in declared_candidates}
        if missing:
            remapped_candidates: list[tuple[str, str, int, bool]] = []
            remaining_missing = set(missing)
            matched_candidate_names = {n for n, _, _, _ in declared_candidates}
            shared_deliverable_candidates: list[tuple[str, str, int, bool]] = []
            for _decl in list(remaining_missing):
                _decl_norm = str(_decl or "").replace("\\", "/").strip()
                if not _decl_norm:
                    continue
                _decl_base = os.path.basename(_decl_norm)
                _decl_ext = os.path.splitext(_decl_base)[1]
                if allow_shared_merge and helpers_shared_src and _decl_base:
                    _shared_rel_candidates: list[str] = []
                    _shared_rel_exact = ""
                    if _decl_norm.startswith("_helpers_shared/"):
                        _shared_rel_exact = _decl_norm[len("_helpers_shared/"):]
                    elif _decl_norm.startswith("_shared/"):
                        _shared_rel_exact = _decl_norm[len("_shared/"):]
                    elif "/" in _decl_norm:
                        _shared_rel_exact = _decl_norm
                    if _shared_rel_exact:
                        _shared_src_exact = os.path.join(helpers_shared_src, _shared_rel_exact.replace("/", os.sep))
                        if os.path.isfile(_shared_src_exact):
                            _shared_rel_candidates.append(_shared_rel_exact)
                    if not _shared_rel_candidates and os.path.isdir(helpers_shared_src):
                        for _root, _dirs, _files in os.walk(helpers_shared_src):
                            for _file in _files:
                                if _file == _decl_base:
                                    _shared_rel_candidates.append(
                                        os.path.relpath(os.path.join(_root, _file), helpers_shared_src).replace(os.sep, "/")
                                    )
                            if len(_shared_rel_candidates) > 1:
                                break
                    if len(_shared_rel_candidates) == 1:
                        _shared_name = f"_helpers_shared/{_shared_rel_candidates[0]}"
                        if _shared_name in copied:
                            remaining_missing.remove(_decl)
                            debug.log(
                                f"delegate.{task_id}.declared_shared_satisfied",
                                f"declared output {_decl!r} was already satisfied by merged shared file {_shared_name!r}",
                            )
                            continue
                if _decl_norm.startswith("_helpers_shared/"):
                    _shared_rel = _decl_norm[len("_helpers_shared/"):]
                    _shared_src = os.path.join(helpers_shared_src, _shared_rel.replace("/", os.sep))
                    if os.path.isfile(_shared_src):
                        try:
                            _shared_size = os.path.getsize(_shared_src)
                        except OSError:
                            _shared_size = 0
                        shared_deliverable_candidates.append((_decl_norm, _shared_src, _shared_size, False))
                        remaining_missing.remove(_decl)
                        debug.log(
                            f"delegate.{task_id}.declared_shared",
                            f"declared shared output {_decl!r} matched helpers_shared file {_shared_rel!r}",
                        )
                        continue
                _decl_dir = _declared_dir_prefix(_decl_norm)
                if _decl_dir:
                    _dir_matches: list[tuple[str, str, int, bool]] = []
                    for cand in candidates:
                        _cand_norm = str(cand[0] or "").replace("\\", "/").strip().lstrip("./")
                        if cand[0] in matched_candidate_names:
                            continue
                        if _cand_norm.startswith(_decl_dir) and _cand_norm != _decl_dir.rstrip("/"):
                            _dir_matches.append(cand)
                    if _decl_dir.startswith("_helpers_shared/"):
                        _shared_rel_dir = _decl_dir[len("_helpers_shared/"):]
                        _shared_dir = os.path.join(helpers_shared_src, _shared_rel_dir.replace("/", os.sep))
                        if os.path.isdir(_shared_dir):
                            for _root, _dirs, _files in os.walk(_shared_dir):
                                for _file in _files:
                                    _shared_src = os.path.join(_root, _file)
                                    try:
                                        _shared_size = os.path.getsize(_shared_src)
                                    except OSError:
                                        continue
                                    _rel = os.path.relpath(_shared_src, helpers_shared_src).replace(os.sep, "/")
                                    _shared_name = f"_helpers_shared/{_rel}"
                                    if any(_shared_name == existing[0] for existing in _dir_matches):
                                        continue
                                    _dir_matches.append((_shared_name, _shared_src, _shared_size, False))
                    if _dir_matches:
                        remapped_candidates.extend(_dir_matches)
                        matched_candidate_names.update(n for n, _, _, _ in _dir_matches)
                        remaining_missing.remove(_decl)
                        debug.log(
                            f"delegate.{task_id}.declared_dir",
                            f"declared output directory {_decl!r} matched {len(_dir_matches)} file(s)",
                        )
                        continue
                if not _decl_base or not _decl_ext:
                    continue
                _matches = [
                    cand for cand in candidates
                    if cand[0] not in matched_candidate_names
                    and cand[0].endswith(_decl_ext)
                    and cand[0].endswith(_decl_norm)
                ]
                if len(_matches) != 1:
                    _matches = [
                        cand for cand in candidates
                        if cand[0] not in matched_candidate_names
                        and cand[0].endswith(_decl_ext)
                        and cand[0].endswith("_" + _decl_base)
                    ]
                if len(_matches) != 1 and _decl_base != _decl_norm:
                    _matches = [
                        cand for cand in candidates
                        if cand[0] not in matched_candidate_names
                        and os.path.basename(cand[0]) == _decl_base
                    ]
                if len(_matches) == 1:
                    remapped_candidates.append(_matches[0])
                    matched_candidate_names.add(_matches[0][0])
                    remaining_missing.remove(_decl)
                    debug.log(
                        f"delegate.{task_id}.declared_remap",
                        f"declared output {_decl!r} matched workspace file {_matches[0][0]!r}",
                    )
            if remapped_candidates:
                declared_candidates.extend(remapped_candidates)
            if shared_deliverable_candidates:
                declared_candidates.extend(shared_deliverable_candidates)
            missing = remaining_missing
        if missing:
            if is_race_aborted_twin:
                # A race-aborted hard twin should not be treated as missing deliverables.
                # The winning twin already delivered; the aborted backup does not need independent outputs.
                # Log this as normal lifecycle information rather than a missing-output warning.
                debug.log(
                    f"delegate.{task_id}.twin_aborted_no_output",
                    f"race-aborted twin did not produce {sorted(missing)} "
                )
            elif suppress_declared_missing:
                debug.log(
                    f"delegate.{task_id}.declared_pending",
                    f"helper declared {sorted(missing)} but this run is waiting on an external resource; "
                    f"treat as pending outputs rather than missing copyback files",
                )
            else:
                debug.log(
                    f"delegate.{task_id}.declared_missing",
                    f"helper declared {sorted(missing)} but not found in workspace; "
                    f"may have been cleaned or renamed",
                )
        if declared_candidates:
            debug.log(
                f"delegate.{task_id}.declared_copy",
                f"copying only {len(declared_candidates)} declared file(s): "
                f"{sorted(n for n, _, _, _ in declared_candidates)} "
                f"(filtered from {len(candidates)} workspace candidates)",
            )
            for _name, _src, _size, _is_source in declared_candidates:
                _name_norm = str(_name).replace("\\", "/")
                if not _name_norm.startswith("_helpers_shared/"):
                    continue
                _shared_name = _name_norm
                _base_name = os.path.basename(_shared_name)
                _shared_rel = _shared_name[len("_helpers_shared/"):]
                _needs_declared_internal_mapping = (
                    "/" not in _shared_rel
                    and os.path.splitext(_base_name)[1].lower() in {".py", ".pyw", ".pyc", ".pyo"}
                )
                if not _needs_declared_internal_mapping:
                    continue
                if not any(
                    isinstance(m, dict)
                    and m.get("main_name") == _shared_name
                    and m.get("shared_name") == _shared_name
                    for m in file_map
                ):
                    file_map.append({
                        "helper_name": _base_name,
                        "main_name": _shared_name,
                        "shared_name": _shared_name,
                    })
        candidates = declared_candidates
        # Declared-file copy skips the 50-file cap because the helper made an explicit delivery boundary.
    else:
        # Without declarations, keep the hard cap: 50+ loose files usually indicate pollution or a loop.
        if len(candidates) > _RESULT_COPY_BACK_MAX_FILES:
            sample_names = [n for n, _, _, _ in candidates[:20]]
            debug.error(
                f"delegate.{task_id} produced {len(candidates)} files exceeds cap "
                f"({_RESULT_COPY_BACK_MAX_FILES}); rejecting ALL copy-back; "
                f"main workspace fully protected. "
                f"This usually indicates a fork-copy pollution snowball / runaway loop / "
                f"resume race condition (two LLM streams on same helper)."
            )
            log.error(
                "delegate.%s rejected %d-file copy-back; main workspace untouched. "
                "If you intended these files, debug the helper for runaway behavior.",
                task_id, len(candidates),
            )
            stats["capped"] = True
            stats["rejected_count"] = len(candidates)
            stats["rejected_sample"] = sample_names
            stats["error_kind"] = "copyback_requires_declared_outputs"
            stats["recovery_hint"] = (
                "The helper produced many files but did not declare which files are intended project outputs. "
                "The main workspace was protected and no loose files were copied. Treat this as an output-declaration gap "
                "or start a same-goal v2 task. Resume the same task_id with a compact instruction: inventory the "
                "existing helper workspace, list the intended project files, remove scratch/generated noise, and "
                "report or declare concrete expected outputs. For a genuine large project, keep the canonical "
                "framework and split outputs by coherent directories or modules before copy-back.\n\n"
                "helper 产物过多且未声明交付文件；继续同一 task_id，清理噪声并声明真实项目产物。"
            )
            stats["suggested_next_action"] = {
                "tool": "delegate",
                "action": "spawn",
                "task_template": {
                    "task_id": task_id,
                    "resume": True,
                    "kind": helper_kind_norm or "code",
                    "mode": "hard",
                    "framework": "<existing framework plus the copy-back cap evidence>",
                    "prompt": (
                        "Continue the same helper. Inventory the current helper workspace, identify intended project "
                        "outputs, delete or ignore scratch files, and finish by reporting concrete expected outputs "
                        "or by narrowing the output set while preserving existing work."
                    ),
                    "expected_outputs": ["<concrete project files or _env/... paths>"],
                    "acceptance_checks": [
                        "intended output file list is explicit",
                        "scratch/generated noise is excluded",
                        "outputs can be copied back without the undeclared-file cap",
                    ],
                },
            }
            return copied, stats, file_map

    # BUG FIX: define _DATA_EXTS before loops that use it in file_map construction.
    # Python treats a variable assigned anywhere in a function as local throughout that function.
    # If _DATA_EXTS were assigned later, earlier references would raise UnboundLocalError.
    # That made helper finalization fail even when files had been produced correctly,
    # causing the main process to misread success as helper failure and repeatedly respawn work.
    # Keeping the definition here prevents that local-binding failure.
    # 
    # 
    # 
    _DATA_EXTS = {".json", ".csv", ".txt", ".tsv", ".yaml", ".yml", ".xml"}
    _declared_by_base: dict[str, str] = {}
    for _decl in declared_files or set():
        if not isinstance(_decl, str):
            continue
        _decl_norm = _decl.replace("\\", "/").strip()
        if not _decl_norm:
            continue
        _declared_by_base.setdefault(os.path.basename(_decl_norm), _decl_norm)

    # expected_outputs basenames may use clean names without task_id prefixes.
    _expected_basenames: set[str] = set()
    for _eo in expected_outputs or []:
        if isinstance(_eo, str):
            _eo_norm = _eo.replace("\\", "/").strip()
            if _eo_norm:
                _expected_basenames.add(os.path.basename(_eo_norm).lower())

    for name, src, _size, is_source in candidates:
        # 鍛藉悕绛栫暐:
        # Naming policy:
        #   - source files: helper_{task_id}_{name}, so the main process can identify helper-produced source
        #   - artifacts: {task_id}_{name}, preserving older frontend filename parsing behavior
        _clean_name_allowed = (
            name.lower() in _expected_basenames
            and not os.path.exists(os.path.join(main_ws, name))
        )
        if _clean_name_allowed:
            dst_name = name  # clean name: use the helper original filename
            stats.setdefault("clean_named_count", 0)
            stats["clean_named_count"] += 1
        elif is_source:
            dst_name = f"helper_{task_id}_{name}"
            stats["copied_source_count"] += 1
        else:
            dst_name = f"{task_id}_{name}"
        dst = os.path.join(main_ws, dst_name)
        try:
            _copy2_with_text_repair(src, dst, stats, dst_name)
            copied.append(dst_name)
            _register_copyback_file(
                registry,
                dst_name,
                task_id=task_id,
                helper_kind=helper_kind_norm,
                kind=FileKind.HELPER_OUTPUT,
                visibility=Visibility.INTERNAL,
            )
            # Record helper filename -> main filename mapping.
            file_map.append({
                "helper_name": name,
                "main_name": dst_name,
                "shared_name": (
                    _declared_by_base.get(name)
                    or (
                        f"_helpers_shared/{task_id}/{name}"
                        if (ext := os.path.splitext(name)[1].lower()) in _DATA_EXTS
                        or (is_source and declared_files and name in declared_files)
                        else None
                    )
                ),
            })
        except OSError:
            pass

    # Propagate data files into _helpers_shared/<task_id>/ for downstream helpers.
    # If intermediate data files only get task_id-prefixed in the main workspace,
    # later helpers that were told to use the original filename cannot find them.
    # Keep original filenames in the shared helper folder to preserve handoff paths.
    # Source files declared as outputs are also propagated for verification helpers.
    #
    # This prevents verifier helpers from searching .prev/ or guessing renamed files.
    # Example: a helper reports files=["sbt.c"]; the shared copy remains sbt.c.
    # The main-workspace source may still be prefixed internally, but shared handoff keeps the clean name.
    # 
    # 
    _hs_dst = os.path.join(main_ws, "_helpers_shared", task_id)
    _hs_copied = 0
    for name, src, _size, is_source in candidates:
        ext = os.path.splitext(name)[1].lower()
        propagate = (not is_source and ext in _DATA_EXTS) or (is_source and declared_files and name in declared_files)
        if propagate:
            try:
                os.makedirs(_hs_dst, exist_ok=True)
                _copy2_with_text_repair(src, os.path.join(_hs_dst, name), stats, f"_helpers_shared/{task_id}/{name}")
                _hs_copied += 1
            except OSError:
                pass
    if _hs_copied:
        debug.log(
            f"delegate.{task_id}.data_propagate",
            f"propagated {_hs_copied} file(s) to _helpers_shared/{task_id}/ "
            f"(incl. declared sources) for downstream helpers",
        )

    if stats["skipped_large"]:
        total_mb = sum(s for _, s in stats["skipped_large"]) / 1024 / 1024
        debug.log(
            f"delegate.{task_id}.large_skipped",
            f"helper produced {len(stats['skipped_large'])} large files "
            f"(>{_RESULT_COPY_BACK_MAX_SIZE//1024//1024}MB, total {total_mb:.1f}MB) "
            f"NOT copying to main workspace (will be cleaned with helper dir): "
            f"{[n for n, _ in stats['skipped_large'][:3]]}",
        )
    if stats["skipped_unchanged"] > 0 or stats["copied_source_count"] > 0:
        debug.log(
            f"delegate.{task_id}.diff",
            f"copy-back diff: copied={len(copied)} unchanged_skipped={stats['skipped_unchanged']} "
            f"sources_with_helper_prefix={stats['copied_source_count']} (fork-snapshot diff)",
        )

    # Mirror copied outputs to the permanent root as a persistence layer separate from .temp.
    # Helper push writes to the current .temp workspace; rotating sessions can otherwise lose that state.
    # The permanent mirror lets the next session restore prior helper outputs instead of restarting.
    # Layering:
    #   - push layer: helper outputs -> .temp/ for the active main process
    #   - persistence layer: copied outputs -> permanent root for cross-session retention
    #   - maintenance layer: new session syncs permanent root back into fresh .temp/
    # Keeping these layers separate makes failure boundaries clear.
    if copied and main_ws:
        _perm_ws = _derive_permanent_root(main_ws)
        if _perm_ws and _perm_ws != main_ws and os.path.isdir(_perm_ws):
            _mirrored = 0
            for _name in copied:
                _src = os.path.join(main_ws, _name)
                _dst = os.path.join(_perm_ws, _name)
                try:
                    if os.path.isfile(_src):
                        shutil.copy2(_src, _dst)
                        _mirrored += 1
                except OSError:
                    pass
            # Mirror _helpers_shared/ as a whole directory.
            _src_hs = os.path.join(main_ws, "_helpers_shared")
            _dst_hs = os.path.join(_perm_ws, "_helpers_shared")
            if os.path.isdir(_src_hs):
                try:
                    shutil.copytree(_src_hs, _dst_hs, dirs_exist_ok=True)
                except OSError:
                    pass
            if _mirrored:
                debug.log(
                    f"delegate.{task_id}.p46_mirror",
                    f"P46: mirrored {_mirrored} files to permanent root {_perm_ws}",
                )

    # Store main_name -> helper_name mapping in main workspace metadata.
    # User-facing delivery can display the clean helper filename while internal main filenames stay stable.
    # Main workspace filenames keep internal prefixes for stable file operations.
    if file_map:
        try:
            from app.llm.tools.workspace import update_displayed_name_remap
            _displayed_remap: dict[str, str] = {}
            for entry in file_map:
                _main = entry.get("main_name")
                _helper = entry.get("helper_name")
                if _main and _helper and _main != _helper:
                    _displayed_remap[_main] = _helper
            if _displayed_remap:
                update_displayed_name_remap(main_ws, _displayed_remap)
                debug.log(
                    f"delegate.{task_id}.displayed_name_remap",
                    f"P58: wrote {len(_displayed_remap)} main-to-helper filename remap entries "
                    f"(delivery strips internal prefixes; main workspace names remain stable)",
                )
        except Exception:
            log.exception("P58 displayed_name_remap write failed (non-fatal)")

    return copied, stats, file_map

