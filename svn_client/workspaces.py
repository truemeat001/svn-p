"""Local workspace bookmarks and atomic, credential-free application settings.

These records are not SVN working-copy metadata or server-side P4 workspaces.
Deleting a record never touches the working directory or its contents.
"""
from __future__ import annotations

import json
import ntpath
import os
from pathlib import Path
import re
import tempfile
import threading
import uuid


_WRITE_LOCK = threading.RLock()
_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_WINDOWS = re.compile(r"^[A-Za-z]:[\\/]")


def _text(value):
    return isinstance(value, str) and bool(value.strip()) and not any(ord(c) < 32 for c in value)


def valid_local_path(value):
    return _text(value) and "://" not in value


def valid_workspace_path(value):
    return valid_local_path(value) and (os.path.isabs(value) or ntpath.isabs(value))


def path_key(value):
    if _WINDOWS.match(value) or value.startswith("\\\\"):
        return ntpath.normcase(ntpath.normpath(value))
    return os.path.normcase(os.path.abspath(os.path.expanduser(value)))


def default_name(path):
    value = ((ntpath.basename(path.rstrip("\\/")) if _WINDOWS.match(path) or path.startswith("\\\\")
              else Path(path).name) or path).strip()
    return (value or "工作区")[:120]


def validate_name(name):
    if not _text(name) or len(name.strip()) > 120 or "://" in name:
        raise ValueError("工作区名称需为 1–120 个非控制字符，不能填写 URL 或认证信息。")
    return name.strip()


def normalize_settings(data):
    """Allowlist only names, local paths and non-secret app preferences."""
    data = data if isinstance(data, dict) else {}
    result = {"format": 1, "workspaces": [], "restore_last_workspace": data.get("restore_last_workspace", True) is not False}
    ids, paths = set(), set()
    records = data.get("workspaces", [])
    for item in records if isinstance(records, list) else []:
        if not isinstance(item, dict) or not valid_workspace_path(item.get("path")):
            continue
        path = item["path"]
        key = path_key(path)
        if key in paths:
            continue
        identifier = item.get("id")
        if not isinstance(identifier, str) or not _ID.fullmatch(identifier) or identifier in ids:
            identifier = uuid.uuid4().hex
        name = item.get("name")
        try:
            name = validate_name(name)
        except ValueError:
            name = default_name(path)
        result["workspaces"].append({"id": identifier, "name": name, "path": path})
        ids.add(identifier)
        paths.add(key)
    legacy = data.get("last_workspace")
    if valid_workspace_path(legacy) and path_key(legacy) not in paths:
        result["workspaces"].append({"id": uuid.uuid4().hex, "name": default_name(legacy), "path": legacy})
    last_id = data.get("last_workspace_id")
    last = next((r for r in result["workspaces"] if r["id"] == last_id), None)
    if last is None and valid_workspace_path(legacy):
        last = next((r for r in result["workspaces"] if path_key(r["path"]) == path_key(legacy)), None)
    if last:
        result["last_workspace_id"] = last["id"]
        result["last_workspace"] = last["path"]
    executable = data.get("svn_executable")
    if valid_local_path(executable):
        result["svn_executable"] = executable
    return result


def load_settings(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        data = {}
    if not isinstance(data, dict):
        raise ValueError("本地设置格式不是对象。原文件未修改。")
    return normalize_settings(data)


def save_settings(path, settings):
    """Atomic replacement. No credentials, URLs or unknown fields are written."""
    path = Path(path)
    safe = normalize_settings(settings)
    temporary = None
    with _WRITE_LOCK:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", prefix=".settings-", suffix=".tmp", dir=path.parent, delete=False) as handle:
                temporary = Path(handle.name)
                json.dump(safe, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def remember_workspace(settings, path, *, name=None, record_id=None, set_last=True):
    """Register only after the caller has successfully validated the SVN root."""
    if not valid_workspace_path(path):
        raise ValueError("工作区必须是本地目录路径，不能是仓库 URL。")
    records = settings.setdefault("workspaces", [])
    by_path = next((r for r in records if path_key(r["path"]) == path_key(path)), None)
    record = next((r for r in records if r["id"] == record_id), None) if record_id else by_path
    if record_id and record is None:
        raise ValueError("此工作区记录已不存在。请重新添加。")
    if record_id and by_path is not None and by_path is not record:
        raise ValueError(f"此目录已登记为“{by_path['name']}”，请选择已有记录。")
    if name is not None:
        name = validate_name(name)
    if record is None:
        record = {"id": uuid.uuid4().hex, "name": name or default_name(path), "path": path}
        records.append(record)
    else:
        record["path"] = path
        if name is not None:
            record["name"] = name
    if set_last:
        settings["last_workspace_id"] = record["id"]
        settings["last_workspace"] = path
    return record


def rename_workspace(settings, identifier, name):
    name = validate_name(name)
    record = next((r for r in settings.get("workspaces", []) if r["id"] == identifier), None)
    if record is None:
        raise ValueError("此工作区记录已不存在。")
    record["name"] = name
    return record


def remove_workspace(settings, identifier):
    """Remove a bookmark only. This function performs no filesystem actions."""
    records = settings.get("workspaces", [])
    settings["workspaces"] = [r for r in records if r["id"] != identifier]
    if settings.get("last_workspace_id") == identifier:
        settings.pop("last_workspace_id", None)
        settings.pop("last_workspace", None)


def startup_workspace(settings):
    if not settings.get("restore_last_workspace", True):
        return None
    return next((dict(r) for r in settings.get("workspaces", []) if r["id"] == settings.get("last_workspace_id")), None)
