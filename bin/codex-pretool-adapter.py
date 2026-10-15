#!/usr/bin/env python3
"""Run command-sensitive Claude hooks against static Codex exec calls."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any


ALLOWED_HOOKS = {
    "pre-bash-harness-main-only-guard.sh",
    "pre-bash-pr-gate.sh",
    "pre-tool-peer-checkout-guard.sh",
}
IDENTIFIER = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")
NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")


class InputError(ValueError):
    pass


class StaticExecParser:
    def __init__(self, source: str) -> None:
        self.source = source
        self.length = len(source)

    def skip_space(self, index: int) -> int:
        while index < self.length and self.source[index].isspace():
            index += 1
        return index

    def skip_trivia(self, index: int) -> int:
        while True:
            index = self.skip_space(index)
            if self.source.startswith("//", index):
                newline = self.source.find("\n", index + 2)
                index = self.length if newline < 0 else newline + 1
                continue
            if self.source.startswith("/*", index):
                end = self.source.find("*/", index + 2)
                if end < 0:
                    raise InputError("unterminated block comment")
                index = end + 2
                continue
            return index

    def parse_string(self, index: int) -> tuple[str, int]:
        quote = self.source[index]
        if quote not in {'"', "'"}:
            raise InputError("expected a literal string")
        result: list[str] = []
        index += 1
        escapes = {
            "b": "\b",
            "f": "\f",
            "n": "\n",
            "r": "\r",
            "t": "\t",
            "v": "\v",
            "0": "\0",
        }
        while index < self.length:
            char = self.source[index]
            if char == quote:
                return "".join(result), index + 1
            if char in "\r\n":
                raise InputError("literal strings cannot contain raw newlines")
            if char != "\\":
                result.append(char)
                index += 1
                continue
            index += 1
            if index >= self.length:
                raise InputError("unterminated string escape")
            escaped = self.source[index]
            if escaped in "\r\n":
                if escaped == "\r" and index + 1 < self.length and self.source[index + 1] == "\n":
                    index += 1
                index += 1
                continue
            if escaped in {"x", "u"}:
                width = 2 if escaped == "x" else 4
                digits = self.source[index + 1 : index + 1 + width]
                if len(digits) != width or not all(c in "0123456789abcdefABCDEF" for c in digits):
                    raise InputError("invalid hexadecimal string escape")
                result.append(chr(int(digits, 16)))
                index += width + 1
                continue
            result.append(escapes.get(escaped, escaped))
            index += 1
        raise InputError("unterminated literal string")

    def skip_quoted_source(self, index: int) -> int:
        quote = self.source[index]
        start = index
        index += 1
        while index < self.length:
            char = self.source[index]
            if char == "\\":
                index += 2
                continue
            if char == quote:
                template = self.source[start : index + 1]
                if quote == "`" and "${" in template:
                    raise InputError("template interpolation is not supported")
                return index + 1
            index += 1
        raise InputError("unterminated source string or template")

    def regex_can_start(self, index: int) -> bool:
        before = self.source[:index].rstrip()
        return not before or before[-1] in "=(:,[!&|?;{}"

    def previous_code_char(self, index: int) -> str | None:
        prefix = self.source[:index]
        while True:
            prefix = prefix.rstrip()
            if not prefix:
                return None
            if prefix.endswith("*/"):
                start = prefix.rfind("/*")
                if start < 0:
                    return prefix[-1]
                prefix = prefix[:start]
                continue
            line_start = prefix.rfind("\n") + 1
            comment = prefix.find("//", line_start)
            if comment >= 0:
                prefix = prefix[:comment]
                continue
            return prefix[-1]

    def skip_regex(self, index: int) -> int:
        index += 1
        in_class = False
        while index < self.length:
            char = self.source[index]
            if char == "\\":
                index += 2
                continue
            if char == "[":
                in_class = True
            elif char == "]":
                in_class = False
            elif char == "/" and not in_class:
                index += 1
                while index < self.length and self.source[index].isalpha():
                    index += 1
                return index
            elif char in "\r\n":
                raise InputError("unterminated regex literal")
            index += 1
        raise InputError("unterminated regex literal")

    def parse_scalar(self, index: int) -> tuple[Any, int]:
        index = self.skip_space(index)
        if index < self.length and self.source[index] in {'"', "'"}:
            return self.parse_string(index)
        for word, value in (("true", True), ("false", False), ("null", None)):
            end = index + len(word)
            if self.source.startswith(word, index) and (
                end == self.length
                or not (self.source[end].isalnum() or self.source[end] in "_$")
            ):
                return value, end
        match = NUMBER.match(self.source, index)
        if match:
            raw = match.group(0)
            return (float(raw) if any(c in raw for c in ".eE") else int(raw)), match.end()
        raise InputError("values must be string, number, boolean, or null literals")

    def parse_object(self, index: int) -> tuple[dict[str, Any], int]:
        if index >= self.length or self.source[index] != "{":
            raise InputError("exec_command arguments must be an inline object literal")
        result: dict[str, Any] = {}
        index = self.skip_space(index + 1)
        if index < self.length and self.source[index] == "}":
            return result, index + 1
        while True:
            index = self.skip_space(index)
            if index < self.length and self.source[index] in {'"', "'"}:
                key, index = self.parse_string(index)
            else:
                match = IDENTIFIER.match(self.source, index)
                if not match:
                    raise InputError("computed keys and spreads are not supported")
                key = match.group(0)
                index = match.end()
            if key in result:
                raise InputError(f"duplicate key is not supported: {key}")
            index = self.skip_space(index)
            if index >= self.length or self.source[index] != ":":
                raise InputError("shorthand properties are not supported")
            value, index = self.parse_scalar(index + 1)
            result[key] = value
            index = self.skip_space(index)
            if index < self.length and self.source[index] == "}":
                return result, index + 1
            if index >= self.length or self.source[index] != ",":
                raise InputError("expected ',' between object properties")
            index = self.skip_space(index + 1)
            if index < self.length and self.source[index] == "}":
                return result, index + 1

    def parse_tools_reference(self, index: int) -> tuple[dict[str, Any] | None, int]:
        member = self.skip_trivia(index + len("tools"))
        if self.source.startswith("?.", member):
            raise InputError("optional tool access is not supported")
        if member < self.length and self.source[member] == ".":
            property_start = self.skip_trivia(member + 1)
            if self.source.startswith("\\u", property_start):
                raise InputError("Unicode-escaped tool properties are not supported")
            match = IDENTIFIER.match(self.source, property_start)
            if not match:
                raise InputError("tool access must use a literal property")
            property_name = match.group(0)
            property_end = match.end()
        elif member < self.length and self.source[member] == "[":
            property_start = self.skip_trivia(member + 1)
            if property_start >= self.length or self.source[property_start] not in {'"', "'"}:
                raise InputError("dynamic computed tool access is not supported")
            property_name, property_end = self.parse_string(property_start)
            property_end = self.skip_trivia(property_end)
            if property_end >= self.length or self.source[property_end] != "]":
                raise InputError("computed tool access must use one literal property")
            property_end += 1
            if property_name == "exec_command":
                raise InputError("computed exec_command access is not supported")
            return None, property_end
        else:
            raise InputError("standalone or aliased tools access is not supported")

        if property_name != "exec_command":
            return None, property_end
        open_paren = self.skip_trivia(property_end)
        if open_paren >= self.length or self.source[open_paren] != "(":
            raise InputError("exec_command must be called directly")
        argument = self.skip_trivia(open_paren + 1)
        call, close_object = self.parse_object(argument)
        if not isinstance(call.get("cmd"), str):
            raise InputError("exec_command.cmd must be an inline literal string")
        if "workdir" in call and not isinstance(call["workdir"], str):
            raise InputError("exec_command.workdir must be an inline literal string")
        close_paren = self.skip_trivia(close_object)
        if close_paren >= self.length or self.source[close_paren] != ")":
            raise InputError("exec_command accepts one inline object argument")
        return call, close_paren + 1

    def parse(self) -> list[dict[str, Any]]:
        calls: list[dict[str, Any]] = []
        index = 0
        while index < self.length:
            if self.source.startswith("//", index):
                newline = self.source.find("\n", index + 2)
                index = self.length if newline < 0 else newline + 1
                continue
            if self.source.startswith("/*", index):
                end = self.source.find("*/", index + 2)
                if end < 0:
                    raise InputError("unterminated block comment")
                index = end + 2
                continue
            if self.source[index] in {'"', "'", "`"}:
                index = self.skip_quoted_source(index)
                continue
            if self.source[index] == "/" and self.regex_can_start(index):
                index = self.skip_regex(index)
                continue
            if self.source.startswith("?.", index):
                raise InputError("optional member access is not supported")
            if self.source[index] == "[":
                previous = self.previous_code_char(index)
                if previous is not None and (
                    previous.isalnum() or previous in "_$])'\"`"
                ):
                    raise InputError("computed member access is not supported")
            match = IDENTIFIER.match(self.source, index)
            if match:
                identifier = match.group(0)
                if identifier == "tools":
                    call, index = self.parse_tools_reference(index)
                    if call is not None:
                        calls.append(call)
                    continue
                if identifier == "exec_command":
                    raise InputError("aliased exec_command calls are not supported")
                if identifier in {
                    "eval",
                    "Function",
                    "AsyncFunction",
                    "constructor",
                    "Object",
                    "Reflect",
                    "globalThis",
                    "global",
                    "window",
                    "self",
                    "this",
                }:
                    raise InputError(f"dynamic evaluator is not supported: {identifier}")
            if self.source.startswith("\\u", index):
                raise InputError("Unicode escapes outside literals are not supported")
            index = match.end() if match else index + 1
        return calls


def fail(message: str) -> int:
    print(
        "Codex PreToolUse gate could not prove the composite exec input safe: "
        f"{message}. Retry with every tools.exec_command argument written as an inline "
        "literal object and inline literal cmd/workdir strings.",
        file=sys.stderr,
    )
    return 2


def validated_hook(raw_path: str) -> Path:
    hook = Path(raw_path).resolve(strict=True)
    if (
        hook.name not in ALLOWED_HOOKS
        or hook.parent.name != "hooks"
        or hook.parent.parent.name != "core"
        or not hook.is_file()
    ):
        raise InputError("adapter target is not an allowlisted canonical hook path")
    return hook


def payload_cwd(payload: dict[str, Any]) -> Path:
    raw_cwd = payload.get("cwd")
    if not isinstance(raw_cwd, str):
        raise InputError("payload.cwd is missing")
    cwd = Path(raw_cwd)
    if not cwd.is_absolute():
        raise InputError(f"payload.cwd must be absolute: {raw_cwd}")
    if not cwd.is_dir():
        raise InputError(f"payload.cwd does not exist: {cwd}")
    return cwd


def normalize_calls(payload: dict[str, Any], default_cwd: Path) -> list[tuple[dict[str, Any], Path]]:
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict) or not isinstance(tool_input.get("command"), str):
        raise InputError("tool_input.command is missing")
    parser = StaticExecParser(tool_input["command"])
    calls = parser.parse()
    normalized: list[tuple[dict[str, Any], Path]] = []
    for call in calls:
        raw_workdir = call.get("workdir")
        workdir = default_cwd if raw_workdir is None else Path(raw_workdir)
        if raw_workdir is not None and not workdir.is_absolute():
            raise InputError(f"workdir must be absolute: {raw_workdir}")
        if not workdir.is_dir():
            raise InputError(f"workdir does not exist: {workdir}")
        child = dict(payload)
        child["tool_name"] = "Bash"
        child["cwd"] = str(workdir)
        child_input = dict(tool_input)
        child_input["command"] = call["cmd"]
        child_input["workdir"] = str(workdir)
        child["tool_input"] = child_input
        normalized.append((child, workdir))
    return normalized


def normalize_raw_call(payload: dict[str, Any], cwd: Path) -> list[tuple[dict[str, Any], Path]]:
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict) or not isinstance(tool_input.get("command"), str):
        raise InputError("tool_input.command is missing")
    child = dict(payload)
    child["cwd"] = str(cwd)
    child["tool_name"] = "Bash"
    child["tool_input"] = dict(tool_input)
    return [(child, cwd)]


def normalize_payload(payload: dict[str, Any]) -> list[tuple[dict[str, Any], Path]]:
    cwd = payload_cwd(payload)
    tool_name = payload.get("tool_name")
    if tool_name == "Bash":
        return normalize_raw_call(payload, cwd)
    if tool_name in {"code_mode_exec", "exec"}:
        return normalize_calls(payload, cwd)
    raise InputError("tool_name must be exactly Bash, code_mode_exec, or exec")


def parse_hook_output(stdout: str) -> tuple[str | None, str | None]:
    if not stdout.strip():
        return None, None
    try:
        output = json.loads(stdout)
    except json.JSONDecodeError as error:
        raise InputError("canonical hook returned malformed JSON") from error
    if not isinstance(output, dict):
        raise InputError("canonical hook output must be a JSON object")
    allowed_top_level = {"decision", "reason", "additionalContext", "hookSpecificOutput"}
    unknown_top_level = set(output) - allowed_top_level
    if unknown_top_level:
        raise InputError(
            "canonical hook returned unsupported fields: "
            + ", ".join(sorted(unknown_top_level))
        )
    decision = output.get("decision")
    if "decision" in output and decision != "block":
        raise InputError("canonical hook returned an unsupported decision")
    if "reason" in output and decision != "block":
        raise InputError("canonical hook returned a reason without a block decision")
    if decision == "block":
        reason = output.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise InputError("canonical hook returned a block without a reason")
        return None, reason.strip()
    context = output.get("additionalContext")
    if "additionalContext" in output and not isinstance(context, str):
        raise InputError("canonical hook additionalContext must be a string")
    specific = output.get("hookSpecificOutput")
    if "hookSpecificOutput" in output:
        if not isinstance(specific, dict):
            raise InputError("canonical hook hookSpecificOutput must be a JSON object")
        unknown_specific = set(specific) - {"hookEventName", "additionalContext"}
        if unknown_specific:
            raise InputError(
                "canonical hook returned unsupported hookSpecificOutput fields: "
                + ", ".join(sorted(unknown_specific))
            )
        event_name = specific.get("hookEventName")
        if event_name is not None and event_name != "PreToolUse":
            raise InputError("canonical hook returned the wrong hook event name")
        specific_context = specific.get("additionalContext")
        if not isinstance(specific_context, str):
            raise InputError("canonical hook hookSpecificOutput requires additionalContext")
        if context is not None:
            raise InputError("canonical hook returned duplicate additionalContext fields")
        context = specific_context
    return context, None


def main() -> int:
    if len(sys.argv) != 2:
        return fail("expected exactly one canonical hook path")
    try:
        hook = validated_hook(sys.argv[1])
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise InputError("hook input must be a JSON object")
        calls = normalize_payload(payload)
    except (InputError, OSError, json.JSONDecodeError) as error:
        return fail(str(error))

    if not calls:
        return 0

    contexts: list[str] = []
    buffered_stderr: list[str] = []
    for child_payload, workdir in calls:
        environment = dict(os.environ)
        environment["HARNESS_HOOK_RUNTIME"] = "codex"
        # The hook's session owner is the Codex process that spawned this
        # adapter (parity with codex-hook-adapter.sh), never the adapter or a
        # value inherited from an unrelated environment.
        environment["HARNESS_HOOK_OWNER_PID"] = str(os.getppid())
        result = subprocess.run(
            ["bash", str(hook)],
            input=json.dumps(child_payload, ensure_ascii=False),
            text=True,
            capture_output=True,
            cwd=workdir,
            env=environment,
            check=False,
        )
        if result.returncode != 0:
            reason = result.stderr.strip() or f"canonical hook exited with code {result.returncode}"
            return fail(reason)
        try:
            context, block_reason = parse_hook_output(result.stdout)
        except InputError as error:
            return fail(str(error))
        if block_reason is not None:
            return fail(block_reason)
        if context:
            contexts.append(context)
        if result.stderr:
            buffered_stderr.append(result.stderr)

    if buffered_stderr:
        sys.stderr.write("".join(buffered_stderr))
    if contexts:
        json.dump(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "additionalContext": "\n".join(contexts),
                }
            },
            sys.stdout,
            ensure_ascii=False,
        )
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
