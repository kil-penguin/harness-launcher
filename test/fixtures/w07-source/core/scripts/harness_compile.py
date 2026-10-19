#!/usr/bin/env python3
"""Compile canonical harness source into runtime surfaces."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import Any


class HarnessCompileError(Exception):
    pass


class BrokerSourceMissing(HarnessCompileError):
    pass


class SourceReader:
    """Immutable broker-mode view of the candidate files consumed by the compiler."""

    def __init__(self, root: Path):
        self.root = root
        self.files: dict[str, tuple[bytes, os.stat_result]] = {}
        self.directories: dict[str, os.stat_result] = {}
        self.nodes: dict[str, tuple[os.stat_result, int, int | None, str | None]] = {}
        self.absent: set[str] = set()
        self.absent_directories: set[str] = set()
        self._fds: list[int] = []
        self.rule_names: list[str] = []

    @staticmethod
    def _parts(relative: str) -> tuple[str, ...]:
        raw_parts = relative.split("/")
        if any(part in {"", ".", ".."} for part in raw_parts):
            raise HarnessCompileError(f"broker source path is unsafe: {relative!r}")
        path = Path(relative)
        if not relative or path.is_absolute():
            raise HarnessCompileError(f"broker source path is unsafe: {relative!r}")
        return path.parts

    def _relative(self, path: Path) -> str:
        try:
            return path.relative_to(self.root).as_posix()
        except ValueError as exc:
            raise HarnessCompileError(f"broker source path escapes input root: {path}") from exc

    @staticmethod
    def _validate_info(info: os.stat_result, relative: str, directory: bool) -> None:
        if (info.st_uid != os.getuid() or info.st_mode & 0o022
                or (directory != stat.S_ISDIR(info.st_mode))
                or (not directory and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1))):
            raise HarnessCompileError(f"broker source has unsafe metadata: {relative}")

    @staticmethod
    def _same_info(left: os.stat_result, right: os.stat_result) -> bool:
        return (left.st_dev, left.st_ino, left.st_mode, left.st_nlink, left.st_uid,
                left.st_gid, left.st_rdev, left.st_size, left.st_ctime_ns,
                left.st_mtime_ns) == (right.st_dev, right.st_ino, right.st_mode,
                                        right.st_nlink, right.st_uid, right.st_gid,
                                        right.st_rdev, right.st_size, right.st_ctime_ns,
                                        right.st_mtime_ns)

    @staticmethod
    def _same_identity(left: os.stat_result, right: os.stat_result) -> bool:
        return (left.st_dev, left.st_ino, left.st_mode, left.st_uid,
                left.st_gid, left.st_rdev) == (right.st_dev, right.st_ino,
                                                right.st_mode, right.st_uid,
                                                right.st_gid, right.st_rdev)

    def _capture_node(self, relative: str, info: os.stat_result, fd: int,
                      parent_fd: int | None = None, name: str | None = None) -> None:
        expected = self.nodes.get(relative)
        if expected is not None and not self._same_info(expected[0], info):
            raise HarnessCompileError(f"broker source changed during open: {relative}")
        self.nodes.setdefault(relative, (info, fd, parent_fd, name))

    def _verify_node(self, relative: str, phase: str) -> None:
        expected, fd, parent_fd, name = self.nodes[relative]
        try:
            held = os.fstat(fd)
            named = (os.lstat(self.root) if parent_fd is None else
                     os.stat(name, dir_fd=parent_fd, follow_symlinks=False))
        except OSError as exc:
            raise HarnessCompileError(f"broker source changed during {phase}: {relative}") from exc
        if not self._same_info(held, expected) or not self._same_info(named, expected):
            raise HarnessCompileError(f"broker source changed during {phase}: {relative}")

    def _open(self, relative: str, *, directory: bool = False) -> tuple[int, os.stat_result]:
        parts = self._parts(relative)
        root_fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        fd = root_fd
        opened = [root_fd]
        try:
            root_info = os.fstat(root_fd)
            self._validate_info(root_info, ".", True)
            captured = [(".", root_info, root_fd, None, None)]
            for index, part in enumerate(parts):
                flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                is_directory = index < len(parts) - 1 or directory
                if is_directory:
                    flags |= os.O_DIRECTORY
                parent_fd = fd
                named = os.stat(part, dir_fd=fd, follow_symlinks=False)
                self._validate_info(named, "/".join(parts[:index + 1]), is_directory)
                next_fd = os.open(part, flags, dir_fd=fd)
                fd = next_fd
                opened.append(fd)
                held = os.fstat(fd)
                if (held.st_dev, held.st_ino) != (named.st_dev, named.st_ino):
                    raise HarnessCompileError(f"broker source changed during open: {'/'.join(parts[:index + 1])}")
                self._validate_info(held, "/".join(parts[:index + 1]), is_directory)
                captured.append(("/".join(parts[:index + 1]), held, fd, parent_fd, part))
            info = os.fstat(fd)
            for node in captured:
                self._capture_node(*node)
            self._fds.extend(opened)
            return fd, info
        except FileNotFoundError as exc:
            try:
                for node in captured:
                    self._capture_node(*node)
            except HarnessCompileError:
                for opened_fd in opened:
                    os.close(opened_fd)
                raise
            self._fds.extend(opened)
            raise BrokerSourceMissing(f"broker source missing: {relative}") from exc
        except OSError as exc:
            for opened_fd in opened:
                os.close(opened_fd)
            raise HarnessCompileError(f"broker source unavailable: {relative}") from exc
        except HarnessCompileError:
            for opened_fd in opened:
                os.close(opened_fd)
            raise

    def capture_file(self, relative: str, *, required: bool = True) -> bool:
        if relative in self.files:
            return True
        try:
            fd, info = self._open(relative)
        except BrokerSourceMissing:
            if not required:
                self.absent.add(relative)
                return False
            self.close()
            raise
        except HarnessCompileError:
            self.close()
            raise
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
        self._verify_node(relative, "read")
        self.files[relative] = (b"".join(chunks), info)
        return True

    def capture_directory(self, relative: str) -> list[str]:
        try:
            fd, info = self._open(relative, directory=True)
        except BrokerSourceMissing:
            raise
        except HarnessCompileError:
            self.close()
            raise
        names = sorted(os.listdir(fd))
        self.directories[relative] = info
        return names

    def read_text(self, path: Path) -> str:
        relative = self._relative(path)
        try:
            return self.files[relative][0].decode("utf-8")
        except KeyError as exc:
            raise HarnessCompileError(f"broker source was not captured: {relative}") from exc
        except UnicodeDecodeError as exc:
            raise HarnessCompileError(f"broker source is not UTF-8: {relative}") from exc

    def exists(self, path: Path) -> bool:
        return self._relative(path) in self.files

    def close(self) -> None:
        while self._fds:
            os.close(self._fds.pop())


CODEX_KOREAN_RESPONSE_LANGUAGE_BLOCK = """<!-- BEGIN HARNESS-CODEX-RESPONSE-LANGUAGE -->
## Codex Response Language

- Default to Korean for conversational responses.
- Keep code, identifiers, commands, paths, logs, errors, and quoted source text in their original language unless the user asks to translate them.
- If the user explicitly asks for another response language, follow that request for the current task.
<!-- END HARNESS-CODEX-RESPONSE-LANGUAGE -->"""


def parse_scalar(value: str) -> Any:
    value = value.strip()
    if not value:
        return ""
    if (value.startswith('"') and value.endswith('"')) or (
        value.startswith("'") and value.endswith("'")
    ):
        return value[1:-1]
    if value.isdigit():
        return int(value)
    if value.lower() == "true":
        return True
    if value.lower() == "false":
        return False
    return value


def parse_simple_yaml(path: Path, source_reader: SourceReader | None = None) -> dict[str, Any]:
    """Parse the small YAML subset used by .claude/source files.

    Supported forms:
    - top-level mapping sections
    - two-space nested scalar mappings
    - two-space lists of scalars
    - two-space lists of maps with four-space continuation keys
    """
    data: dict[str, Any] = {}
    section: str | None = None
    current_list_key: str | None = None
    current_list_item: dict[str, Any] | None = None

    if source_reader is None and not path.exists():
        return data
    text = source_reader.read_text(path) if source_reader else path.read_text(encoding="utf-8")
    for line_number, raw in enumerate(text.splitlines(), 1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))

        if indent == 0:
            if ":" not in stripped:
                raise HarnessCompileError(f"{path}:{line_number} malformed top-level line")
            key, _, value = stripped.partition(":")
            key = key.strip()
            if value.strip():
                data[key] = parse_scalar(value)
                section = None
            else:
                data[key] = {}
                section = key
            current_list_key = None
            current_list_item = None
            continue

        if section is None:
            raise HarnessCompileError(f"{path}:{line_number} nested value without section")

        if indent == 2:
            if ":" not in stripped:
                raise HarnessCompileError(f"{path}:{line_number} malformed nested line")
            key, _, value = stripped.partition(":")
            key = key.strip()
            value = value.strip()
            target = data[section]
            if not isinstance(target, dict):
                raise HarnessCompileError(f"{path}:{line_number} mixed mapping/list section")
            if value:
                target[key] = parse_scalar(value)
                current_list_key = None
            else:
                target[key] = []
                current_list_key = key
            current_list_item = None
            continue

        if indent == 4 and current_list_key is not None:
            target = data[section]
            if not isinstance(target, dict) or not isinstance(target[current_list_key], list):
                raise HarnessCompileError(f"{path}:{line_number} invalid list target")
            if not stripped.startswith("- "):
                raise HarnessCompileError(f"{path}:{line_number} expected list item")
            item = stripped[2:].strip()
            if ":" in item and not item.startswith(("'", '"')):
                key, _, value = item.partition(":")
                current_list_item = {key.strip(): parse_scalar(value)}
                target[current_list_key].append(current_list_item)
            else:
                target[current_list_key].append(parse_scalar(item))
                current_list_item = None
            continue

        if indent == 6 and current_list_item is not None:
            if ":" not in stripped:
                raise HarnessCompileError(f"{path}:{line_number} malformed list mapping")
            key, _, value = stripped.partition(":")
            current_list_item[key.strip()] = parse_scalar(value)
            continue

        raise HarnessCompileError(f"{path}:{line_number} unsupported indentation")

    return data


def load_source(root: Path, source_reader: SourceReader | None = None) -> dict[str, Any]:
    source_dir = root / ".claude" / "source"
    runtime = parse_simple_yaml(source_dir / "runtime-contract.yaml", source_reader)
    budgets = parse_simple_yaml(source_dir / "budgets.yaml", source_reader)
    settings = parse_settings_yaml(source_dir / "settings.yaml", source_reader)
    agents = parse_simple_yaml(source_dir / "agents.yaml", source_reader)
    commands = parse_simple_yaml(source_dir / "commands.yaml", source_reader)
    skills = parse_simple_yaml(source_dir / "skills.yaml", source_reader)
    claude_md = parse_simple_yaml(source_dir / "claude-md.yaml", source_reader)
    merged: dict[str, Any] = {}
    merged.update(runtime)
    merged.update(budgets)
    merged.update(settings)
    merged.update(agents)
    merged.update(commands)
    merged.update(skills)
    merged.update(claude_md)
    return merged


def parse_settings_yaml(path: Path, source_reader: SourceReader | None = None) -> dict[str, Any]:
    """Parse compiler-owned settings source.

    This intentionally supports only the small structure used by
    .claude/source/settings.yaml: settings model/env/attribution/permissions plus a flat
    ordered list of hook entries.
    """
    if source_reader is None and not path.exists():
        return {}

    data: dict[str, Any] = {
        "settings": {"env": {}, "attribution": {}},
        "hooks": {"codex_exclusions": [], "entries": [], "omp_extensions": []},
    }
    section: str | None = None
    subsection: str | None = None
    permission_list: str | None = None
    current_entry: dict[str, Any] | None = None

    text = source_reader.read_text(path) if source_reader else path.read_text(encoding="utf-8")
    for line_number, raw in enumerate(text.splitlines(), 1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))

        if indent == 0:
            if stripped not in {"settings:", "hooks:"}:
                raise HarnessCompileError(f"{path}:{line_number} unsupported top-level key")
            section = stripped[:-1]
            subsection = None
            permission_list = None
            current_entry = None
            continue

        if section is None:
            raise HarnessCompileError(f"{path}:{line_number} nested value without section")

        if section == "settings":
            if indent == 2:
                if ":" not in stripped:
                    raise HarnessCompileError(f"{path}:{line_number} malformed settings line")
                key, _, value = stripped.partition(":")
                key = key.strip()
                value = value.strip()
                permission_list = None
                if key in {"env", "attribution", "permissions"} and not value:
                    subsection = key
                    if key == "permissions":
                        if key in data["settings"]:
                            raise HarnessCompileError(f"{path}:{line_number} duplicate permissions")
                        data["settings"][key] = {}
                    continue
                if key not in {"model", "includeGitInstructions", "autoCompactWindow"}:
                    raise HarnessCompileError(f"{path}:{line_number} unsupported settings key: {key}")
                data["settings"][key] = parse_scalar(value)
                subsection = None
                continue
            if indent == 4 and subsection in {"env", "attribution"}:
                if ":" not in stripped:
                    raise HarnessCompileError(f"{path}:{line_number} malformed {subsection} line")
                key, _, value = stripped.partition(":")
                data["settings"][subsection][key.strip()] = parse_scalar(value)
                continue
            if indent == 4 and subsection == "permissions":
                if stripped not in {"allow:", "deny:", "ask:"}:
                    raise HarnessCompileError(f"{path}:{line_number} expected permissions allow/deny/ask list")
                permission_list = stripped[:-1]
                if permission_list in data["settings"]["permissions"]:
                    raise HarnessCompileError(f"{path}:{line_number} duplicate permission list")
                data["settings"]["permissions"][permission_list] = []
                continue
            if indent == 6 and subsection == "permissions" and permission_list is not None:
                if not stripped.startswith("- "):
                    raise HarnessCompileError(f"{path}:{line_number} expected permission string")
                value = stripped[2:].strip()
                try:
                    if value.startswith('"'):
                        parsed = json.loads(value)
                    elif value.startswith("'") and value.endswith("'"):
                        parsed = value[1:-1].replace("''", "'")
                    else:
                        parsed = parse_scalar(value)
                except ValueError:
                    raise HarnessCompileError(f"{path}:{line_number} invalid permission string") from None
                if not isinstance(parsed, str):
                    raise HarnessCompileError(f"{path}:{line_number} expected permission string")
                data["settings"]["permissions"][permission_list].append(parsed)
                continue

        if section == "hooks":
            if indent == 2:
                if stripped.startswith(("omp_default_mode:", "omp_default_compatibility:", "omp_required_contract:", "omp_upstream_base_version:", "omp_upstream_base_revision:")):
                    key, _, value = stripped.partition(":")
                    data["hooks"][key] = parse_scalar(value)
                    continue
                if stripped in {"codex_exclusions:", "entries:", "omp_extensions:"}:
                    subsection = stripped[:-1]
                    current_entry = None
                    continue
                raise HarnessCompileError(f"{path}:{line_number} unsupported hooks key")
            if indent == 4 and subsection == "codex_exclusions":
                if not stripped.startswith("- "):
                    raise HarnessCompileError(f"{path}:{line_number} expected codex exclusion")
                data["hooks"]["codex_exclusions"].append(parse_scalar(stripped[2:]))
                continue
            if indent == 4 and subsection in {"entries", "omp_extensions"}:
                if not stripped.startswith("- ") or ":" not in stripped:
                    raise HarnessCompileError(f"{path}:{line_number} expected hook entry")
                key, _, value = stripped[2:].partition(":")
                current_entry = {key.strip(): parse_scalar(value)}
                data["hooks"][subsection].append(current_entry)
                continue
            if indent == 6 and subsection in {"entries", "omp_extensions"} and current_entry is not None:
                if ":" not in stripped:
                    raise HarnessCompileError(f"{path}:{line_number} malformed hook entry")
                key, _, value = stripped.partition(":")
                current_entry[key.strip()] = parse_scalar(value)
                continue

        raise HarnessCompileError(f"{path}:{line_number} unsupported settings indentation")

    return data


def require_path(data: dict[str, Any], dotted_path: str) -> Any:
    cur: Any = data
    for part in dotted_path.split("."):
        if not isinstance(cur, dict) or part not in cur or cur[part] in ("", [], {}):
            raise HarnessCompileError(f"missing required source key: {dotted_path}")
        cur = cur[part]
    return cur


def check_source(root: Path, source_reader: SourceReader | None = None) -> None:
    data = load_source(root, source_reader)
    release = data.get("runtime_contract", {}).get("shared_contract_release")
    registry_path = root / "core/config/shared-contracts.json"
    if release is not None or (source_reader.exists(registry_path) if source_reader else registry_path.exists()):
        if not isinstance(release, str) or not release:
            raise HarnessCompileError("shared_contract_release is required with a shared registry")
        from cross_harness_surface_diff import check_contract_release
        errors = check_contract_release(
            root, str(release),
            read_bytes=(lambda path: source_reader.files[source_reader._relative(path)][0]) if source_reader else None,
            exists=source_reader.exists if source_reader else None,
        )
        if errors:
            raise HarnessCompileError("; ".join(errors))
    for key in (
        "runtime_contract.id",
        "runtime_contract.source_of_truth",
        "runtime_contract.before_work",
        "runtime_contract.completion_gate",
    ):
        require_path(data, key)

    for key in (
        "budgets.claude_md_max_lines",
        "budgets.always_on_total_max_lines",
        "budgets.codex_agents_max_bytes",
        "budgets.skill_md_max_bytes",
    ):
        require_path(data, key)

    budgets = data["budgets"]
    budget_limits = {
        "claude_md_max_lines": 80,
        "codex_agents_max_bytes": 9216,
        "skill_md_max_bytes": 10000,
    }
    for key, max_value in budget_limits.items():
        value = budgets.get(key)
        if not isinstance(value, int) or value <= 0 or value > max_value:
            raise HarnessCompileError(f"invalid budgets.{key}: {value}")
    total = budgets.get("always_on_total_max_lines")
    if not isinstance(total, int) or total <= 0:
        raise HarnessCompileError(f"invalid budgets.always_on_total_max_lines: {total}")

    if "settings" in data:
        settings = data["settings"]
        if not isinstance(settings.get("model"), str) or not settings["model"].strip():
            raise HarnessCompileError("missing required source key: settings.model")
        if not isinstance(settings.get("includeGitInstructions"), bool):
            raise HarnessCompileError("settings.includeGitInstructions must be boolean")
        if "autoCompactWindow" in settings:
            acw = settings["autoCompactWindow"]
            if isinstance(acw, bool) or not isinstance(acw, int) or not (100000 <= acw <= 1000000):
                raise HarnessCompileError("settings.autoCompactWindow must be int in [100000, 1000000]")
        if not isinstance(settings.get("env"), dict):
            raise HarnessCompileError("settings.env must be mapping")
        if not isinstance(settings.get("attribution"), dict):
            raise HarnessCompileError("settings.attribution must be mapping")
        # Broker rendering consumes a sealed authored-only snapshot, not existing outputs.
        if source_reader is None:
            check_settings_permissions_migrated(
                read_existing_settings(root / ".claude/settings.json"), settings)

    if "hooks" in data:
        hooks = data["hooks"]
        for key, value in {"omp_required_contract": "w07.required-resolver-consumer.v1",
                           "omp_upstream_base_version": "18.11.3",
                           "omp_upstream_base_revision": "96894a6147d8cc64b7e00ad3a7d7178fd93b9531"}.items():
            if key in hooks and hooks[key] != value:
                raise HarnessCompileError("invalid OMP required contract metadata")
        if hooks.get("omp_default_mode", "required") != "required":
            raise HarnessCompileError("hooks.omp_default_mode must retain required")
        if hooks.get("omp_default_compatibility", "unsupported") not in {"equivalent", "unsupported"}:
            raise HarnessCompileError("invalid OMP default compatibility")
        entries = hooks.get("entries")
        if not isinstance(entries, list):
            raise HarnessCompileError("hooks.entries must be list")
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                raise HarnessCompileError(f"hooks.entries[{index}] must be mapping")
            for key in ("event", "script"):
                if not isinstance(entry.get(key), str) or not entry[key].strip():
                    raise HarnessCompileError(f"hooks.entries[{index}].{key} is required")
            if not entry["script"].endswith(".sh"):
                raise HarnessCompileError(f"hooks.entries[{index}].script must end with .sh")
            if "timeout" in entry and not isinstance(entry["timeout"], int):
                raise HarnessCompileError(f"hooks.entries[{index}].timeout must be integer")
            if "hook_runtime" in entry and entry["hook_runtime"] != "claude":
                raise HarnessCompileError(f"hooks.entries[{index}].hook_runtime must be claude")
            if entry.get("omp_mode", "required") not in {"required", "auto", "manual", "off", "unsupported"}:
                raise HarnessCompileError("invalid OMP hook mode")
            if entry.get("omp_required", True) and entry.get("omp_mode", "required") != "required":
                raise HarnessCompileError("OMP required hook cannot be disabled")
            if entry.get("omp_compatibility", "equivalent") not in {"equivalent", "unsupported"}:
                raise HarnessCompileError("invalid OMP hook compatibility")
            for flag in ("omp_required", "omp_decision_only"):
                if flag in entry and type(entry[flag]) is not bool:
                    raise HarnessCompileError(f"invalid OMP {flag}")

    for catalog_key in ("agents", "commands", "skills"):
        if catalog_key in data:
            check_catalog_source(root, catalog_key, data[catalog_key], source_reader)

    # The Codex AGENTS.md byte budget is a render-time property: .claude/rules
    # content can blow it while every source value stays valid. Render here so the
    # commit fails instead of the launcher at startup.
    render_codex_agents_within_budget(root, data, source_reader)


def check_catalog_source(root: Path, catalog_key: str, catalog: Any, source_reader: SourceReader | None = None) -> None:
    if not isinstance(catalog, dict):
        raise HarnessCompileError(f"{catalog_key} must be mapping")
    if catalog_key == "commands" and "index_description" in catalog:
        index_description = catalog["index_description"]
        if not isinstance(index_description, str) or not index_description.strip():
            raise HarnessCompileError("commands.index_description must be a non-empty string")
    if catalog.get("mode") != "compiler-owned-index":
        return
    source = catalog.get("source")
    if not isinstance(source, str) or not source.strip():
        raise HarnessCompileError(f"{catalog_key}.source is required")
    entries = catalog.get("entries")
    if not isinstance(entries, list):
        raise HarnessCompileError(f"{catalog_key}.entries must be list")
    source_dir = root / source
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise HarnessCompileError(f"{catalog_key}.entries[{index}] must be mapping")
        for field in ("name", "path", "description"):
            if not isinstance(entry.get(field), str) or not entry[field].strip():
                raise HarnessCompileError(f"{catalog_key}.entries[{index}].{field} is required")
        target = source_dir / entry["path"]
        if catalog_key == "skills" and entry["path"].endswith("/"):
            target = source_dir / entry["path"] / "SKILL.md"
        if not (source_reader.exists(target) if source_reader else target.exists()):
            rel = target.relative_to(root) if target.is_absolute() else target
            raise HarnessCompileError(f"{catalog_key}.entries[{index}].path missing: {rel}")


def render_runtime_contract(data: dict[str, Any]) -> str:
    contract = require_path(data, "runtime_contract")
    contract_id = html.escape(str(require_path(data, "runtime_contract.id")))
    source = html.escape(str(require_path(data, "runtime_contract.source_of_truth")))
    forbidden = contract.get("forbidden_edits", [])
    before_work = require_path(data, "runtime_contract.before_work")
    completion_gate = require_path(data, "runtime_contract.completion_gate")

    lines = [
        f'<runtime_contract id="{contract_id}">',
        f"  <source_of_truth>{source}</source_of_truth>",
    ]
    if forbidden:
        lines.append("  <forbidden_edits>")
        for item in forbidden:
            if isinstance(item, dict):
                path = html.escape(str(item.get("path", "")))
                reason = html.escape(str(item.get("reason", "")))
                lines.append(f'    <path reason="{reason}">{path}</path>')
        lines.append("  </forbidden_edits>")
    lines.append("  <before_work>")
    for step in before_work:
        lines.append(f"    <step>{html.escape(str(step))}</step>")
    lines.append("  </before_work>")
    lines.append("  <completion_gate>")
    for requirement in completion_gate:
        lines.append(f"    <requirement>{html.escape(str(requirement))}</requirement>")
    lines.append("  </completion_gate>")
    lines.append("</runtime_contract>")
    return "\n".join(lines) + "\n"


def write_if_changed(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        path.unlink()
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return
    path.write_text(content, encoding="utf-8")


def render_claude(root: Path, data: dict[str, Any]) -> str:
    xml_contract = render_runtime_contract(data)
    config = data.get("claude_md", {})
    if not isinstance(config, dict):
        raise HarnessCompileError("claude_md must be mapping")

    configured_title = config.get("title")
    if configured_title is not None and (
        not isinstance(configured_title, str) or not configured_title.strip()
    ):
        raise HarnessCompileError("claude_md.title must be a non-empty string")
    title = configured_title.strip() if configured_title is not None else root.name.replace("-", " ")

    contract_heading = str(config.get("contract_heading") or "Runtime Contract")
    principles_heading = str(config.get("principles_heading") or "Principles")
    principles = config.get("principles") or [
        "Fundamental First — review root cause and best practice before acting.",
        "Evidence-based — verify with code, tests, logs, or docs before asserting.",
        "Test first — write or update tests before behavior changes.",
        "Keep always-on context small; detailed guidance belongs in scoped docs.",
        "When presenting choices, mark exactly one fundamental recommendation and give its reason. If the user's next reply is an unqualified approval/proceed signal for that single choice list, execute the marked recommendation; if the reply could cover multiple questions or recommendations, ask instead of guessing, and never silently substitute another option.",
    ]
    if not isinstance(principles, list) or not principles:
        raise HarnessCompileError("claude_md.principles must be a non-empty list")
    after_principles = config.get("after_principles") or []
    if not isinstance(after_principles, list):
        raise HarnessCompileError("claude_md.after_principles must be list")
    sections = config.get("sections") or []
    if not isinstance(sections, list):
        raise HarnessCompileError("claude_md.sections must be list")
    pointers = config.get("pointers") or [
        "Core details: `domains/knowledge/harness/`",
        "Cascading contract: `.claude/rules/cascading-updates.md`; matrix: `domains/knowledge/harness/cascading-update-matrix.md`",
        "RAG verification contract: `.claude/rules/rag-integration.md`; operations/source routes: `domains/knowledge/harness/{rag-architecture,rag-source-verification}.md`",
        "Performance observation contract: `.claude/rules/performance-observation.md`; test-specific details: `domains/knowledge/harness/testing-strategy.md`",
        "Runtime source: `.claude/source/`",
        "Generated Codex layer: `.harness/codex/`",
    ]
    if not isinstance(pointers, list) or not pointers:
        raise HarnessCompileError("claude_md.pointers must be a non-empty list")

    lines = [
        f"# {title}",
        "",
        "> Generated from `.claude/source/*`. Do not edit generated sections directly.",
        "",
        f"## {contract_heading}",
        "",
        xml_contract,
        f"## {principles_heading}",
        "",
    ]
    for index, principle in enumerate(principles, 1):
        lines.append(f"{index}. {principle}")
    lines.append("")
    for item in after_principles:
        lines.append(str(item))
        lines.append("")
    for section in sections:
        if not isinstance(section, dict):
            raise HarnessCompileError("claude_md.sections entries must be mapping")
        section_title = section.get("title")
        if not isinstance(section_title, str) or not section_title.strip():
            raise HarnessCompileError("claude_md.sections[].title is required")
        lines.extend([f"## {section_title}", ""])
        body = section.get("body")
        if body:
            lines.extend([str(body), ""])
    lines.extend(["## Pointers", ""])
    for pointer in pointers:
        lines.append(f"- {pointer}")
    return "\n".join(lines) + "\n"



# 컴파일러가 소유하는 settings.json 최상위 키. 나머지는 런타임이 쓴 것이라 보존한다 —
# 플러그인 설치가 직접 써 넣는 `enabledPlugins` 가 그 예다(2026-09-07: 전체 덮어쓰기라
# 다음 컴파일이 조용히 지웠다). 관리 키는 소스에서 사라지면 파일에서도 지운다.
MANAGED_SETTINGS_KEYS = (
    "model",
    "attribution",
    "env",
    "hooks",
    "autoCompactWindow",
    "includeGitInstructions",
    "permissions",
)


def check_settings_permissions_migrated(existing: dict[str, Any], managed: dict[str, Any]) -> None:
    if "permissions" in existing and "permissions" not in managed:
        raise HarnessCompileError("settings_permissions_unmigrated")


def merge_settings(existing: dict[str, Any], managed: dict[str, Any]) -> dict[str, Any]:
    """기존 키 순서를 지키며 관리 키만 갱신하고, 관리 밖 키는 그대로 둔다."""
    check_settings_permissions_migrated(existing, managed)
    merged: dict[str, Any] = {}
    for key, value in existing.items():
        if key in MANAGED_SETTINGS_KEYS:
            if key in managed:
                merged[key] = managed[key]
            continue
        merged[key] = value
    for key, value in managed.items():
        if key not in merged:
            merged[key] = value
    return merged


def read_existing_settings(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    raw = path.read_text(encoding="utf-8")
    try:
        existing = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HarnessCompileError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(existing, dict):
        raise HarnessCompileError(f"{path} must contain a JSON object")
    return existing


def render_settings(data: dict[str, Any], existing: dict[str, Any] | None = None) -> str:
    settings = require_path(data, "settings")
    hooks = data.get("hooks", {})
    output: dict[str, Any] = {
        "model": settings["model"],
    }
    if settings.get("attribution"):
        output["attribution"] = settings["attribution"]
    if settings.get("env"):
        def env_value(value: Any) -> str:
            if isinstance(value, bool):
                return "true" if value else "false"
            return str(value)

        output["env"] = {k: env_value(v) for k, v in settings["env"].items()}

    hook_output: dict[str, list[dict[str, Any]]] = {}
    for entry in hooks.get("entries", []):
        event = entry["event"]
        # hook_runtime prefixes only this hook process, so a Codex child of the
        # Claude Bash tool does not inherit the marker the way settings.env would.
        marker = "HARNESS_HOOK_RUNTIME=claude " if entry.get("hook_runtime") == "claude" else ""
        hook: dict[str, Any] = {
            "type": "command",
            "command": f'{marker}bash "$CLAUDE_PROJECT_DIR/core/hooks/{entry["script"]}"',
        }
        if "timeout" in entry:
            hook["timeout"] = entry["timeout"]
        rendered_entry: dict[str, Any] = {}
        if entry.get("matcher"):
            rendered_entry["matcher"] = entry["matcher"]
        rendered_entry["hooks"] = [hook]
        hook_output.setdefault(event, []).append(rendered_entry)
    if hook_output:
        output["hooks"] = hook_output

    if settings.get("autoCompactWindow") is not None:
        output["autoCompactWindow"] = settings["autoCompactWindow"]
    output["includeGitInstructions"] = settings["includeGitInstructions"]
    if "permissions" in settings:
        output["permissions"] = settings["permissions"]
    merged = merge_settings(existing or {}, output)
    return json.dumps(merged, ensure_ascii=False, indent=2) + "\n"


def render_catalog_index(catalog_key: str, catalog: dict[str, Any]) -> str:
    entries = catalog.get("entries", [])
    if catalog_key == "agents":
        lines = [
            "# Agents",
            "",
            "Specialized subagents dispatched for focused work. Each has its own context and tool set.",
            "",
        ]
    elif catalog_key == "commands":
        index_description = catalog.get("index_description", "Slash commands for multi-skill workflows.")
        if not isinstance(index_description, str) or not index_description.strip():
            raise HarnessCompileError("commands.index_description must be a non-empty string")
        lines = [
            "# Commands",
            "",
            index_description.strip(),
            "",
        ]
    elif catalog_key == "skills":
        lines = [
            "# Skills",
            "",
            "Personal workflow skills. Lazy additions — create only when a pattern repeats.",
            "",
        ]
    else:
        raise HarnessCompileError(f"unsupported catalog index: {catalog_key}")

    notes: list[str] = []
    for entry in entries:
        name = entry["name"]
        path = entry["path"]
        description = entry["description"]
        if catalog_key == "skills" and entry.get("surfaced") is False:
            notes.append(f"* {name} — {description}")
        else:
            lines.append(f"- [{name}]({path}) — {description}")

    if notes:
        lines.append("")
        lines.extend(notes)
    return "\n".join(lines).rstrip() + "\n"


def iter_catalog_indexes(data: dict[str, Any]) -> list[tuple[str, str]]:
    indexes: list[tuple[str, str]] = []
    for catalog_key in ("agents", "commands", "skills"):
        catalog = data.get(catalog_key)
        if isinstance(catalog, dict) and catalog.get("mode") == "compiler-owned-index":
            source = catalog["source"]
            indexes.append((f"{source.rstrip('/')}/_index.md", render_catalog_index(catalog_key, catalog)))
    return indexes


def iter_rule_files(root: Path, source_reader: SourceReader | None = None) -> list[Path]:
    rules_dir = root / ".claude" / "rules"
    if source_reader is not None:
        return [rules_dir / name for name in source_reader.rule_names]
    if not rules_dir.is_dir():
        return []
    skipped = {"_index.md", "runtime-contract.md"}
    return [
        path
        for path in sorted(rules_dir.glob("*.md"))
        if path.name not in skipped
    ]


def render_codex_agents(root: Path, data: dict[str, Any], include_contract: bool = True,
                        source_reader: SourceReader | None = None) -> str:
    xml_contract = render_runtime_contract(data)
    contract = require_path(data, "runtime_contract")
    lines = [
        "# Generated by harness_compile.py — do not edit manually.",
        "# Source-of-truth: .claude/source/* plus .claude/rules/*.md",
        "",
    ]
    if include_contract:
        lines.extend(["## Runtime Contract", "", xml_contract.rstrip(), ""])
    for rule_file in iter_rule_files(root, source_reader):
        title = rule_file.stem
        body = (source_reader.read_text(rule_file) if source_reader else rule_file.read_text(encoding="utf-8")).rstrip()
        lines.extend([f"## {title}", "", body, ""])
    response_language = contract.get("codex_response_language")
    if response_language == "korean":
        lines.extend([CODEX_KOREAN_RESPONSE_LANGUAGE_BLOCK, ""])
    elif response_language is not None:
        raise HarnessCompileError(
            "runtime_contract.codex_response_language must be 'korean' when set"
        )
    return "\n".join(lines).rstrip() + "\n"


def render_codex_agents_within_budget(root: Path, data: dict[str, Any],
                                      source_reader: SourceReader | None = None) -> str:
    content = render_codex_agents(root, data, source_reader=source_reader)
    max_bytes = data.get("budgets", {}).get("codex_agents_max_bytes")
    size = len(content.encode("utf-8"))
    if isinstance(max_bytes, int) and size > max_bytes:
        raise HarnessCompileError(
            f"generated Codex AGENTS.md exceeds budget: {size}/{max_bytes} bytes"
        )
    return content


def write_codex_agents(root: Path) -> None:
    check_source(root)
    data = load_source(root)
    content = render_codex_agents_within_budget(root, data)
    write_if_changed(root / ".harness" / "codex" / "AGENTS.md", content)


def write_codex_project_agents(root: Path) -> None:
    check_source(root)
    data = load_source(root)
    content = render_codex_agents(root, data, include_contract=False)
    write_if_changed(root / ".harness" / "codex" / "AGENTS.md", content)


def capture_broker_source(root: Path) -> SourceReader:
    reader = SourceReader(root)
    try:
        for name in ("runtime-contract.yaml", "budgets.yaml", "settings.yaml", "agents.yaml",
                     "commands.yaml", "skills.yaml", "claude-md.yaml"):
            reader.capture_file(f".claude/source/{name}")
    except Exception:
        reader.close()
        raise
    reader.capture_file("core/config/shared-contracts.json", required=False)
    try:
        names = reader.capture_directory(".claude/rules")
    except BrokerSourceMissing:
        reader.absent_directories.add(".claude/rules")
        names = []
    reader.rule_names = [name for name in names if name.endswith(".md") and name not in {"_index.md", "runtime-contract.md"}]
    for name in reader.rule_names:
        reader.capture_file(f".claude/rules/{name}")
    if reader.exists(root / "core/config/shared-contracts.json"):
        try:
            registry = json.loads(reader.read_text(root / "core/config/shared-contracts.json"))
            contracts = registry.get("contracts", []) if isinstance(registry, dict) else []
            for row in contracts:
                if isinstance(row, dict) and isinstance(row.get("path"), str):
                    reader.capture_file(row["path"])
        except (ValueError, HarnessCompileError):
            pass
    try:
        data = load_source(root, reader)
        for catalog_key in ("agents", "commands", "skills"):
            catalog = data.get(catalog_key)
            if not isinstance(catalog, dict) or catalog.get("mode") != "compiler-owned-index":
                continue
            source = catalog.get("source")
            entries = catalog.get("entries")
            if not isinstance(source, str) or not isinstance(entries, list):
                continue
            reader._parts(source)
            for entry in entries:
                if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
                    continue
                relative = f"{source.rstrip('/')}/{entry['path']}"
                if catalog_key == "skills" and entry["path"].endswith("/"):
                    relative += "SKILL.md"
                reader.capture_file(relative)
    except Exception:
        reader.close()
        raise
    return reader


def verify_broker_source(reader: SourceReader) -> None:
    for relative, (expected, fd, parent_fd, name) in reader.nodes.items():
        try:
            held = os.fstat(fd)
            named = (os.lstat(reader.root) if parent_fd is None else
                     os.stat(name, dir_fd=parent_fd, follow_symlinks=False))
        except OSError as exc:
            raise HarnessCompileError(f"broker source changed during compilation: {relative}") from exc
        if not reader._same_info(held, expected) or not reader._same_info(named, expected):
            raise HarnessCompileError(f"broker source changed during compilation: {relative}")
    for relative, (_, expected) in reader.files.items():
        fd, actual = reader._open(relative)
        current = b"".join(iter(lambda: os.read(fd, 65536), b""))
        reader._verify_node(relative, "compilation")
        if not reader._same_info(actual, expected) or current != reader.files[relative][0]:
            raise HarnessCompileError(f"broker source changed during compilation: {relative}")
    for relative, expected in reader.directories.items():
        reader.capture_directory(relative)
        actual = reader.directories[relative]
        if not reader._same_info(actual, expected):
            raise HarnessCompileError(f"broker source directory changed during compilation: {relative}")
    for relative in reader.absent:
        try:
            reader._open(relative)
        except BrokerSourceMissing:
            continue
        raise HarnessCompileError(f"broker source changed during compilation: {relative}")
    for relative in reader.absent_directories:
        try:
            reader._open(relative, directory=True)
        except BrokerSourceMissing:
            continue
        raise HarnessCompileError(f"broker source changed during compilation: {relative}")


def write_broker_project_agents(input_root: Path, output_root: Path) -> None:
    if input_root.is_symlink() or not input_root.is_dir():
        raise HarnessCompileError("broker input root must be a real directory")
    input_root = input_root.resolve()
    output_info = os.lstat(output_root)
    if (stat.S_ISLNK(output_info.st_mode) or not stat.S_ISDIR(output_info.st_mode)
            or output_info.st_uid != os.getuid() or stat.S_IMODE(output_info.st_mode) != 0o700):
        raise HarnessCompileError("broker output root must be an empty real directory")
    output_fd = os.open(output_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    opened_output = os.fstat(output_fd)
    if not SourceReader._same_identity(opened_output, output_info) or os.listdir(output_fd):
        os.close(output_fd)
        raise HarnessCompileError("broker output root must be an empty real directory")
    reader: SourceReader | None = None
    try:
        reader = capture_broker_source(input_root)
        check_source(input_root, reader)
        data = load_source(input_root, reader)
        content = render_codex_agents(input_root, data, include_contract=False, source_reader=reader)
        verify_broker_source(reader)
    except Exception:
        if reader is not None:
            reader.close()
        os.close(output_fd)
        raise
    fd = -1
    created: os.stat_result | None = None
    try:
        named_output = os.lstat(output_root)
        if not SourceReader._same_identity(named_output, opened_output):
            raise HarnessCompileError("broker output root changed during compilation")
        fd = os.open("AGENTS.md", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=output_fd)
        created = os.fstat(fd)
        payload = content.encode("utf-8")
        offset = 0
        while offset < len(payload):
            offset += os.write(fd, payload[offset:])
        os.fsync(fd)
        named_output = os.lstat(output_root)
        current = os.stat("AGENTS.md", dir_fd=output_fd, follow_symlinks=False)
        check_fd = os.open("AGENTS.md", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=output_fd)
        try:
            rendered = os.read(check_fd, len(payload) + 1)
        finally:
            os.close(check_fd)
        if (not SourceReader._same_identity(named_output, opened_output)
                or created is None or not SourceReader._same_info(current, os.fstat(fd))
                or current.st_mode & 0o777 != 0o600
                or hashlib.sha256(rendered).digest() != hashlib.sha256(payload).digest()):
            raise HarnessCompileError("broker output changed during compilation")
        os.fsync(output_fd)
        if not SourceReader._same_identity(os.lstat(output_root), opened_output):
            raise HarnessCompileError("broker output root changed during compilation")
        verify_broker_source(reader)
        current = os.stat("AGENTS.md", dir_fd=output_fd, follow_symlinks=False)
        check_fd = os.open("AGENTS.md", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=output_fd)
        try:
            rendered = os.read(check_fd, len(payload) + 1)
        finally:
            os.close(check_fd)
        if (created is None or not SourceReader._same_info(current, os.fstat(fd))
                or current.st_mode & 0o777 != 0o600
                or hashlib.sha256(rendered).digest() != hashlib.sha256(payload).digest()):
            raise HarnessCompileError("broker output changed during compilation")
    except (OSError, HarnessCompileError) as exc:
        if created is not None:
            try:
                current = os.stat("AGENTS.md", dir_fd=output_fd, follow_symlinks=False)
                if (current.st_dev, current.st_ino) == (created.st_dev, created.st_ino):
                    os.unlink("AGENTS.md", dir_fd=output_fd)
            except OSError:
                pass
        if isinstance(exc, HarnessCompileError):
            raise
        raise HarnessCompileError("broker output creation failed") from exc
    finally:
        if fd >= 0:
            os.close(fd)
        os.close(output_fd)
        reader.close()


def write_surfaces(root: Path) -> None:
    check_source(root)
    data = load_source(root)
    write_if_changed(root / "CLAUDE.md", render_claude(root, data))
    if "settings" in data:
        settings_path = root / ".claude" / "settings.json"
        write_if_changed(
            settings_path,
            render_settings(data, read_existing_settings(settings_path)),
        )
    for relative_path, content in iter_catalog_indexes(data):
        write_if_changed(root / relative_path, content)
    # The project root already contributes CLAUDE.md/AGENTS.md with the runtime
    # contract. The per-harness Codex home carries only rules and language so a
    # normal compile matches launcher preparation without duplicating context.
    write_codex_project_agents(root)


OMP_ADAPTER_VERSION = "w07-required-v2"
OMP_REQUIRED_CONTRACT = "w07.required-resolver-consumer.v1"
OMP_NATIVE_VERSION = "18.11.3-kh.w07.1"
OMP_BASE_REVISION = "96894a6147d8cc64b7e00ad3a7d7178fd93b9531"


def validate_omp_native_release(release: dict[str, Any]) -> None:
    fields = {"contract_id", "upstream_base_version", "upstream_base_revision", "version",
              "source_revision", "source_patch_sha256", "build_provenance_sha256", "binary_sha256"}
    if (not isinstance(release, dict) or set(release) != fields or
            release["contract_id"] != OMP_REQUIRED_CONTRACT or
            release["upstream_base_version"] != "18.11.3" or
            release["upstream_base_revision"] != OMP_BASE_REVISION or
            release["version"] != OMP_NATIVE_VERSION or
            not re.fullmatch(r"[0-9a-f]{40}", str(release["source_revision"])) or
            any(not re.fullmatch(r"[0-9a-f]{64}", str(release[k])) for k in
                ("source_patch_sha256", "build_provenance_sha256", "binary_sha256"))):
        raise HarnessCompileError("OMP unsupported native release binding")

OMP_EVENTS = {"SessionStart": "session_start", "UserPromptSubmit": "input",
              "PreToolUse": "tool_call", "PostToolUse": "tool_result",
              "PostToolUseFailure": "tool_result", "Stop": "session_stop",
              "SessionEnd": "session_shutdown"}


def omp_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def omp_digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def omp_read(root: Path, relative: str) -> bytes:
    """Read only owned regular inputs; a symlink is never a canonical input."""
    if (not isinstance(relative, str) or not relative or Path(relative).is_absolute()
            or any(p in {"", ".", ".."} for p in relative.split("/"))):
        raise HarnessCompileError("OMP unsafe input path")
    path = root
    for part in Path(relative).parts:
        path = path / part
        try:
            info = path.lstat()
        except OSError as exc:
            raise HarnessCompileError(f"OMP input unavailable: {relative}") from exc
        if stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise HarnessCompileError(f"OMP unsafe input metadata: {relative}")
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise HarnessCompileError(f"OMP input is not regular: {relative}")
    raw = path.read_bytes()
    if not SourceReader._same_info(info, path.lstat()):
        raise HarnessCompileError(f"OMP input changed: {relative}")
    return raw


def render_omp_bundle(root: Path, data: dict[str, Any], selection: dict[str, Any]) -> tuple[str, dict[str, str]]:
    """Render an already admitted selection. Hashes bind bytes, never grant authority.

    Normal prepare must compare CURRENT existing projection/lease before exec;
    native consumption and required policy recheck that same current binding.
    This compiler has no authority store, native import or provider operation.
    """
    selection = json.loads(omp_json(selection))
    allowed = {"schema", "profile", "runtime", "runtime_version", "source_or_release_binding",
               "repo_workspace_identity", "bundle_ids", "approved_overrides_sha256", "hooks", "skills",
               "extensions", "bridge_rewrites", "workspace", "current_projection", "hook_dependencies", "contract_id", "native_release"}
    if set(selection) - allowed:
        raise HarnessCompileError("OMP unknown selection field")
    if selection.get("schema") not in {1, 2} or selection.get("runtime") != "omp":
        raise HarnessCompileError("OMP unknown schema/runtime")
    for key in ("profile", "runtime_version", "source_or_release_binding", "repo_workspace_identity"):
        if not isinstance(selection.get(key), str) or not selection[key].strip():
            raise HarnessCompileError(f"OMP missing selection {key}")
    if not re.fullmatch(r"[0-9a-f]{64}", str(selection.get("approved_overrides_sha256", ""))):
        raise HarnessCompileError("OMP invalid overrides hash")
    if selection["schema"] == 2:
        release = selection.get("native_release")
        validate_omp_native_release(release)
        if (selection.get("contract_id") != OMP_REQUIRED_CONTRACT or
                selection["runtime_version"] != release["version"] or
                data.get("hooks", {}).get("omp_required_contract") != OMP_REQUIRED_CONTRACT or
                data.get("hooks", {}).get("omp_upstream_base_version") != "18.11.3" or
                data.get("hooks", {}).get("omp_upstream_base_revision") != OMP_BASE_REVISION):
            raise HarnessCompileError("OMP required contract mismatch")
        projection = selection.get("current_projection")
        if not isinstance(projection, dict) or not isinstance(projection.get("expected"), dict):
            raise HarnessCompileError("OMP incomplete normal binding")
        current = projection["expected"]
        def path_value(value: Any) -> bool:
            return isinstance(value, str) and value.startswith("/") and "\0" not in value
        dependencies = selection.get("hook_dependencies")
        if (not path_value(selection.get("workspace")) or
                any(not re.fullmatch(r"[0-9a-f]{64}", str(selection.get(k, "")))
                    for k in ("source_or_release_binding", "repo_workspace_identity")) or
                any(not path_value(current.get(k)) for k in ("binary_path", "namespace_path")) or
                any(not re.fullmatch(r"[0-9a-f]{64}", str(current.get(k, ""))) for k in
                    ("profile_binding_sha256", "provenance_sha256", "consumer_sha256", "projection_helper_sha256")) or
                any(not path_value(projection.get(k)) for k in ("helper", "python", "home", "registry", "source", "consumer")) or
                projection.get("source") != str(root) or
                projection.get("sha256") != current.get("projection_helper_sha256") or
                not isinstance(dependencies, list) or len(dependencies) != 3 or
                any(not isinstance(d, dict) or set(d) != {"name", "path", "sha256"} or
                    not path_value(d["path"]) or not re.fullmatch(r"[0-9a-f]{64}", str(d["sha256"])) for d in dependencies) or
                {d["name"] for d in dependencies} != {"bash", "jq", "python3"} or
                next(d["path"] for d in dependencies if d["name"] == "bash") != "/bin/bash" or
                projection.get("python") != next(d["path"] for d in dependencies if d["name"] == "python3")):
            raise HarnessCompileError("OMP incomplete normal binding")
        if (current.get("binary_sha256") != release["binary_sha256"] or
                current.get("binary_release") != {k:v for k,v in release.items() if k != "binary_sha256"}):
            raise HarnessCompileError("OMP native/current binding mismatch")
    elif selection["runtime_version"] != "18.11.3" or "native_release" in selection or "contract_id" in selection:
        raise HarnessCompileError("OMP unsupported native version")
    for key in ("hooks", "skills", "extensions", "bridge_rewrites", "bundle_ids"):
        if not isinstance(selection.get(key), list):
            raise HarnessCompileError(f"OMP {key} must be a list")
        if any(not isinstance(i, str) for i in selection[key]) or len(set(selection[key])) != len(selection[key]):
            raise HarnessCompileError(f"OMP duplicate or malformed {key}")
    canonical: dict[str, str] = {}

    def read(relative: str) -> str:
        raw = omp_read(root, relative)
        canonical[relative] = omp_digest(raw)
        return raw.decode("utf-8")

    # Seal the same authored inputs, not generated Claude/Codex output.
    for path in sorted((root / ".claude/source").glob("*.yaml")):
        read(path.relative_to(root).as_posix())
    read("core/scripts/harness_compile.py")
    template = read("core/scripts/omp_required_extension.ts")
    rule_paths = [p.relative_to(root).as_posix() for p in iter_rule_files(root)]
    policy = render_claude(root, data) + "\n" + "\n".join(read(p) for p in rule_paths)
    entries = data.get("hooks", {}).get("entries", [])
    known, hooks = set(), []
    for entry in entries:
        ident = f'{entry["event"]}:{entry.get("matcher", "*")}:{entry["script"]}'
        if ident in known:
            raise HarnessCompileError("OMP duplicate canonical hook")
        known.add(ident)
        mode = entry.get("omp_mode", data.get("hooks", {}).get("omp_default_mode", "required"))
        compatibility = entry.get("omp_compatibility", data.get("hooks", {}).get("omp_default_compatibility", "unsupported"))
        if mode not in {"required", "auto", "manual", "off", "unsupported"}:
            raise HarnessCompileError("OMP invalid canonical hook mode")
        if entry.get("omp_required", True) and mode != "required":
            raise HarnessCompileError("OMP required hook cannot be disabled")
        if mode in {"off", "unsupported"} or (mode == "manual" and ident not in selection["hooks"]):
            continue
        if compatibility != "equivalent" or entry["event"] not in OMP_EVENTS:
            raise HarnessCompileError(f"OMP unsupported selected hook: {ident}")
        script = "core/hooks/" + entry["script"]
        read(script)
        hooks.append(dict(canonical_id=ident, native_event=OMP_EVENTS[entry["event"]],
                          event=entry["event"], matcher=entry.get("matcher", ".*"),
                          script=script, script_sha256=canonical[script], required=mode == "required",
                          timeout_ms=entry.get("timeout", 10) * 1000,
                          decision_only=entry.get("omp_decision_only", False)))
    if set(selection["hooks"]) - known:
        raise HarnessCompileError("OMP unknown hook selection")
    if any(h["script"] in {"core/hooks/pre-bash-curl-block.sh", "core/hooks/pre-edit-config-protection.sh"} for h in hooks):
        read("core/bin/harness-python")
    skills, files = [], {"policy.md": policy, "required-extension.ts": template}
    catalog = {e["name"]: e for e in data.get("skills", {}).get("entries", [])}
    selected_skills = set(selection["skills"])
    selected_skills.update(k for k, v in catalog.items() if v.get("omp_mode") == "required" or v.get("omp_required"))
    if selected_skills - set(catalog):
        raise HarnessCompileError("OMP unknown skill selection")
    for name in sorted(selected_skills):
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
            raise HarnessCompileError("OMP unsafe skill name")
        entry = catalog[name]
        if entry.get("omp_required") and entry.get("omp_mode") != "required":
            raise HarnessCompileError("OMP required skill cannot be disabled")
        if entry.get("omp_mode") in {"off", "unsupported"}:
            raise HarnessCompileError("OMP disabled selected skill")
        base = data["skills"].get("source", ".claude/skills").rstrip("/")
        source_path = f'{base}/{entry["path"].rstrip("/")}'
        path = source_path if source_path.endswith("/SKILL.md") else source_path + "/SKILL.md"
        body = read(path)
        files[f"skills/{name}/SKILL.md"] = body
        # Referenced text assets retain their ordinary paths and count in the
        # manifest/caps. No packing, hidden resource inventory or downloaded assets.
        skill_dir = (root / path).parent
        for resource in sorted(skill_dir.rglob("*")):
            if resource.is_dir() and not resource.is_symlink():
                continue
            relative_resource = resource.relative_to(skill_dir).as_posix()
            if relative_resource == "SKILL.md":
                continue
            try:
                files[f"skills/{name}/{relative_resource}"] = read(resource.relative_to(root).as_posix())
            except UnicodeDecodeError as exc:
                raise HarnessCompileError(f"OMP unsupported binary skill asset: {relative_resource}") from exc
        skills.append(dict(name=name, path=f"skills/{name}/SKILL.md", required=entry.get("omp_mode") == "required"))
    # Compatibility is canonical source evidence; caller module assertions cannot grant it.
    admitted_extensions = data.get("hooks", {}).get("omp_extensions", [])
    extension_map = {e["id"]: e for e in admitted_extensions}
    if len(extension_map) != len(admitted_extensions):
        raise HarnessCompileError("OMP duplicate canonical extension")
    extensions = []
    for ident in selection["extensions"]:
        ext = extension_map.get(ident)
        if ext is None or ext.get("native_input_behavior") != "none" or not ext.get("canonical_source_evidence"):
            raise HarnessCompileError("OMP unsupported independent protected-input writer/closure")
        read(ext["path"])
        if ext.get("closure_sha256") != canonical[ext["path"]]:
            raise HarnessCompileError("OMP extension closure mismatch")
        extensions.append(ext)
    rewrite_map = {h["canonical_id"]: h for h in hooks}
    for ident in selection["bridge_rewrites"]:
        hook = rewrite_map.get(ident)
        authored = next((e for e in entries if f'{e["event"]}:{e.get("matcher", "*")}:{e["script"]}' == ident), {})
        script = authored.get("omp_rewrite_script")
        if hook is None or not script or any(not h["decision_only"] for h in hooks if h["event"] == "PreToolUse"):
            raise HarnessCompileError("OMP unsupported unaudited canonical rewrite pair")
        read(script)
        hook["rewrite"] = dict(canonical_entry_id=ident, tool="bash", script=script, sha256=canonical[script],
                               timeout_ms=hook["timeout_ms"])
    selection["bridge_rewrites"] = [rewrite_map[i]["rewrite"] for i in selection["bridge_rewrites"]]
    manifest = dict(selection, source_root=str(root), adapter_version=OMP_ADAPTER_VERSION,
                    canonical_inputs=[dict(path=p, sha256=h) for p, h in sorted(canonical.items())],
                    hook_map=hooks, selected_skills=skills, selected_extensions=extensions,
                    bridge_total_timeout_ms=28000)
    effective = omp_digest(omp_json(manifest).encode())
    manifest["effective_selection_sha256"] = effective
    manifest["artifacts"] = [dict(relative_path=p, sha256=omp_digest(v.encode()), mode="0600")
                             for p, v in sorted(files.items())]
    files["selection.json"] = omp_json(manifest) + "\n"
    return f".harness/omp/{effective}", files


def _validate_omp_skill_tuples(rows: list[dict[str, str]], *, expectation: bool = False) -> None:
    if not isinstance(rows, list) or any(not isinstance(r, dict) or set(r) != {"name", "path", "sha256"}
        or any(not isinstance(r[k], str) or not r[k] for k in r)
        or not Path(r["path"]).is_absolute() or "\0" in r["path"]
        or not re.fullmatch(r"[0-9a-f]{64}", r["sha256"]) for r in rows):
        raise HarnessCompileError("OMP malformed skill inventory")
    if expectation and len({omp_json(r) for r in rows}) != len(rows):
        raise HarnessCompileError("OMP duplicate expected skill")


def assert_omp_skill_inventory(expected: list[dict[str, str]], actual: list[dict[str, str]]) -> None:
    """Exact full UTF-8 tuple multiset; no normalization, filtering or dedup."""
    _validate_omp_skill_tuples(expected, expectation=True)
    _validate_omp_skill_tuples(actual)
    if sorted(actual, key=omp_json) != sorted(expected, key=omp_json):
        raise HarnessCompileError("OMP ambient/colliding/missing skill")


def validate_omp_caller_argv(manifest: dict[str, Any], caller_argv: list[str]) -> None:
    # Parse option/value boundaries. A prompt value resembling a switch is data.
    values = {"--system-prompt", "--system-prompt-template", "--model", "--provider", "--thinking", "--tools",
              "--smol", "--slow", "--plan", "--models", "--api-key", "--service-tier",
              "--max-time", "--approval-mode", "--resume", "-r", "--restore"}
    protected = {"--no-skills", "--skill", "--skills", "--extension", "-e",
                 "--hook", "--plugin-dir", "--config", "--profile", "--cwd", "--add-dir",
                 "--session-dir", "--from-claude", "--from-codex", "--goal",
                 "--no-extensions", "--append-system-prompt", "--no-rules", "--rule", "--rules"}
    temporal = any(h["event"] == "UserPromptSubmit" and h["required"] for h in manifest["hook_map"])
    index = 0
    while index < len(caller_argv):
        arg = caller_argv[index]
        if arg == "--":
            if any(a.split("=", 1)[0] == "--harness-required-bundle" for a in caller_argv[index+1:]):
                raise HarnessCompileError("OMP reserved launcher locator")
            if temporal and index + 1 < len(caller_argv):
                raise HarnessCompileError("OMP unsupported pre-provider timing for requested startup action/mode")
            break
        flag = arg.split("=", 1)[0]
        if flag == "--harness-required-bundle":
            raise HarnessCompileError("OMP reserved launcher locator")
        if temporal and (flag in {"--print", "-p", "--mode"} or not arg.startswith("-")):
            raise HarnessCompileError("OMP unsupported pre-provider timing for requested startup action/mode")
        if flag in protected or (arg.startswith("-e") and not arg.startswith("--")):
            raise HarnessCompileError(f"OMP unsupported caller resource override: {flag}")
        if flag in values and "=" not in arg:
            index += 1
            if index == len(caller_argv):
                raise HarnessCompileError("OMP missing caller option value")
        index += 1


def prepare_omp_bundle(bundle: Path, expected: dict[str, Any], caller_argv: list[str]) -> tuple[dict[str, Any], list[str]]:
    """Structural preparation only. Native must admit and consume its real inventory.

    The caller holds existing current/namespace/workspace gates and supplies the
    source-rendered expectation. A locator and hashes never grant authority.
    """
    if not bundle.is_absolute() or bundle.resolve() != bundle or bundle.name != expected.get("effective_selection_sha256"):
        raise HarnessCompileError("OMP bundle path binding mismatch")
    actual = json.loads(omp_read(bundle, "selection.json"))
    if actual != expected:
        raise HarnessCompileError("OMP selection mismatch")
    sealed = {k: v for k, v in actual.items() if k not in {"effective_selection_sha256", "artifacts"}}
    if omp_digest(omp_json(sealed).encode()) != bundle.name:
        raise HarnessCompileError("OMP effective selection mismatch")
    artifacts = {}
    for entry in actual["artifacts"]:
        name = entry["relative_path"]
        if name in artifacts:
            raise HarnessCompileError("OMP duplicate artifact")
        artifacts[name] = entry
        raw = omp_read(bundle, name)
        if omp_digest(raw) != entry["sha256"] or stat.S_IMODE((bundle / name).stat().st_mode) != int(entry["mode"], 8):
            raise HarnessCompileError("OMP artifact hash/mode mismatch")
    if not {"policy.md", "required-extension.ts"} <= artifacts.keys():
        raise HarnessCompileError("OMP required artifact missing")
    wanted = [dict(name=s["name"], path=str(bundle / s["path"]), sha256=artifacts[s["path"]]["sha256"])
              for s in actual["selected_skills"]]
    _validate_omp_skill_tuples(wanted, expectation=True)  # Expected shape only; no actual inventory.
    validate_omp_caller_argv(actual, caller_argv)
    required = [f"--append-system-prompt={bundle / 'policy.md'}", "--no-extensions",
                f"--extension={bundle / 'required-extension.ts'}"]
    required.extend(f'--extension={Path(actual["source_root"]) / e["path"]}' for e in actual["selected_extensions"])
    if actual["schema"] == 2:
        validate_omp_native_release(actual["native_release"])
        required.append(f"--harness-required-bundle={bundle / 'selection.json'}")
    split = caller_argv.index("--") if "--" in caller_argv else len(caller_argv)
    p = actual.get("current_projection", {})
    stable = p.get("expected", {})
    prepared = dict(schema=actual["schema"], contract_id=actual.get("contract_id"), bundle_dir=str(bundle),
        selection_sha256=actual["effective_selection_sha256"], manifest_sha256=omp_digest(omp_read(bundle, "selection.json")),
        expected_inventory=wanted, native_release=actual.get("native_release"),
        binding=dict(profile=actual["profile"], source_root=actual["source_root"], workspace=actual.get("workspace"),
            binary=stable.get("binary_path"), namespace=stable.get("namespace_path"),
            source_or_release_binding=actual["source_or_release_binding"],
            repo_workspace_identity=actual["repo_workspace_identity"], stable_current=stable,
            current_descriptor=p, dependency_binding=actual.get("hook_dependencies", [])),
        canonical_closure_sha256=omp_digest(omp_json(actual["canonical_inputs"]).encode()),
        artifact_closure_sha256=omp_digest(omp_json(actual["artifacts"]).encode()), ready=False, inventory_admitted=False)
    return prepared, caller_argv[:split] + required + caller_argv[split:]


def verify_omp_bundle(bundle: Path, expected: dict[str, Any], caller_argv: list[str],
                      resolved_skills: list[dict[str, str]]) -> list[str]:
    """Strict legacy wrapper. The actual inventory argument is mandatory."""
    prepared, argv = prepare_omp_bundle(bundle, expected, caller_argv)
    assert_omp_skill_inventory(prepared["expected_inventory"], resolved_skills)
    return argv


def write_omp_bundle(root: Path, data: dict[str, Any], selection: dict[str, Any], *, check: bool = False) -> str:
    relative, files = render_omp_bundle(root, data, selection)
    for name, content in files.items():
        path = root / relative / name
        if check:
            if omp_read(root, f"{relative}/{name}") != content.encode() or stat.S_IMODE(path.stat().st_mode) != 0o600:
                raise HarnessCompileError("OMP generated artifact mismatch")
        else:
            path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            # Refuse existing symlinks/foreign paths, never unlink to bypass.
            for node in (path.parent, *path.parent.parents):
                if node == root.parent:
                    break
                if node.is_symlink():
                    raise HarnessCompileError("OMP generated path substitution")
            if path.exists() or path.is_symlink():
                if (omp_read(root, f"{relative}/{name}") != content.encode()
                        or stat.S_IMODE(path.stat().st_mode) != 0o600):
                    raise HarnessCompileError("OMP immutable generated artifact changed")
                continue
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w") as stream:
                stream.write(content)
            path.chmod(0o600)
    return relative


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", metavar="HARNESS_ROOT")
    parser.add_argument("--write", metavar="HARNESS_ROOT")
    parser.add_argument("--write-codex", metavar="HARNESS_ROOT")
    parser.add_argument("--write-codex-project", metavar="HARNESS_ROOT")
    parser.add_argument("--broker-render-codex-project", action="store_true")
    parser.add_argument("--input-root", type=Path)
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--omp-selection", type=Path,
                        help="existing owned prepare selection; only with --check/--write")
    args = parser.parse_args()

    try:
        modes = sum(bool(value) for value in (args.check, args.write, args.write_codex, args.write_codex_project, args.broker_render_codex_project))
        if modes != 1:
            parser.error("exactly one compiler mode is required")
        if args.omp_selection and not (args.check or args.write):
            parser.error("--omp-selection requires --check or --write")
        if args.broker_render_codex_project:
            if args.input_root is None or args.output_root is None:
                parser.error("--broker-render-codex-project requires --input-root and --output-root")
            write_broker_project_agents(args.input_root, args.output_root)
            print("harness_compile: wrote broker project AGENTS.md")
            return 0
        if args.input_root is not None or args.output_root is not None:
            parser.error("--input-root and --output-root require --broker-render-codex-project")
        if args.check:
            root = Path(args.check).resolve()
            check_source(root)
            if args.omp_selection:
                write_omp_bundle(root, load_source(root), json.loads(args.omp_selection.read_text()), check=True)
            print("harness_compile: OK")
            return 0
        if args.write:
            root = Path(args.write).resolve()
            write_surfaces(root)
            if args.omp_selection:
                write_omp_bundle(root, load_source(root), json.loads(args.omp_selection.read_text()))
            print("harness_compile: wrote runtime surfaces")
            return 0
        if args.write_codex:
            write_codex_agents(Path(args.write_codex).resolve())
            print("harness_compile: wrote Codex AGENTS.md")
            return 0
        if args.write_codex_project:
            write_codex_project_agents(Path(args.write_codex_project).resolve())
            print("harness_compile: wrote project Codex AGENTS.md")
            return 0
        parser.error("one compiler mode is required")
    except HarnessCompileError as exc:
        print(f"harness_compile: ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
