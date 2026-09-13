from __future__ import annotations

import argparse
import base64
import datetime as dt
import io
import os
import re
import subprocess
from pathlib import Path
from typing import Literal

import keyboard
import pyautogui
from mcp.server import MCPServer
from mcp.types import ImageContent, TextContent, ToolAnnotations


BASE_DIR = Path(__file__).resolve().parent
WORKSPACE_DIR = BASE_DIR / "workspace"
MEMORY_DIR = WORKSPACE_DIR / "memory"

CORE_MEMORY_FILES = [
    "AGENTS.md",
    "IDENTITY.md",
    "SOUL.md",
    "USER.md",
    "MEMORY.md",
    "TOOLS.md",
    "HEARTBEAT.md",
]

SKILL_MEMORY_FILES = ["LEARNINGS.md", "ERRORS.md", "FEATURE_REQUESTS.md"]
PROTECTED_FILES = set(CORE_MEMORY_FILES + SKILL_MEMORY_FILES + ["SKILL-TEMPLATE.md"])

WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
MEMORY_DIR.mkdir(parents=True, exist_ok=True)

SERVER_INSTRUCTIONS = """
This MCP app exposes a trusted local development workspace to ChatGPT.
Use load_agent_context when a task depends on the workspace's persistent identity,
project memory, skills, learnings, or recent daily logs. Use read/list tools before
writing when the target state is uncertain. Treat run_shell, write_file, memory writes,
mouse/keyboard actions, and other state-changing tools as consequential actions.
The MCP server itself performs no LLM inference and calls no model API; ChatGPT is the
reasoning/orchestration layer.
""".strip()

mcp = MCPServer(
    "Local Development Agent",
    version="2.0.0",
    instructions=SERVER_INSTRUCTIONS,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_workspace_path(path: str, *, must_exist: bool = False) -> Path:
    """Resolve a user path inside WORKSPACE_DIR and reject path traversal."""
    raw = Path(path)
    candidate = raw.resolve() if raw.is_absolute() else (WORKSPACE_DIR / raw).resolve()
    root = WORKSPACE_DIR.resolve()

    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"Path must stay inside workspace: {path}") from exc

    if must_exist and not candidate.exists():
        raise FileNotFoundError(f"Path does not exist: {path}")

    return candidate


def _safe_decode(data: bytes | None) -> str:
    if not data:
        return ""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("gbk", errors="replace")


def _truncate(text: str, limit: int = 20_000) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n...[truncated {len(text) - limit} characters]"


def _sanitize_name(value: str, field: str) -> str:
    value = value.strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]+", value):
        raise ValueError(f"{field} may contain only letters, digits, dot, underscore and hyphen")
    if value in {".", ".."}:
        raise ValueError(f"Invalid {field}")
    return value


# ---------------------------------------------------------------------------
# Read-only workspace/context tools
# ---------------------------------------------------------------------------

@mcp.tool(
    title="Load persistent agent context",
    annotations=ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    ),
)
def load_agent_context(
    include_skills: bool = True,
    include_recent_daily_logs: bool = True,
    max_chars_per_file: int = 50_000,
) -> str:
    """Load persistent identity/memory files, skills, and the latest three daily logs.

    Call this when the current task should use the agent's saved project conventions,
    memory, skills, learnings, or recent work history.
    """
    sections: list[str] = []

    for filename in CORE_MEMORY_FILES:
        path = WORKSPACE_DIR / filename
        if path.exists() and path.is_file():
            content = path.read_text(encoding="utf-8", errors="replace")
            sections.append(f"=== FILE: {filename} ===\n{_truncate(content, max_chars_per_file)}")

    if include_skills:
        skills_dir = WORKSPACE_DIR / "skills"
        if skills_dir.exists():
            for path in sorted(skills_dir.rglob("*.md")):
                if path.is_file():
                    rel = path.relative_to(WORKSPACE_DIR).as_posix()
                    content = path.read_text(encoding="utf-8", errors="replace")
                    sections.append(f"=== FILE: {rel} ===\n{_truncate(content, max_chars_per_file)}")

    if include_recent_daily_logs:
        for days_ago in range(2, -1, -1):
            day = dt.date.today() - dt.timedelta(days=days_ago)
            path = MEMORY_DIR / f"{day:%Y-%m-%d}.md"
            if path.exists() and path.is_file():
                content = path.read_text(encoding="utf-8", errors="replace")
                if days_ago > 0 and len(content) > 5_000:
                    content = "...(older content omitted)...\n" + content[-5_000:]
                label = "today" if days_ago == 0 else "recent"
                sections.append(f"=== DAILY LOG ({label}, {day:%Y-%m-%d}) ===\n{content}")

    sections.append(
        "=== SYSTEM INFO ===\n"
        f"os.name: {os.name}\n"
        f"workspace: {WORKSPACE_DIR}\n"
        f"cwd: {Path.cwd()}"
    )
    return "\n\n".join(sections) if sections else "No persistent context files were found."


@mcp.resource(
    "agent://context",
    name="Persistent agent context",
    description="Identity, memory, skills, learnings, and recent daily logs for this local agent.",
    mime_type="text/markdown",
)
def agent_context_resource() -> str:
    """Resource form of load_agent_context for MCP clients that browse resources."""
    return load_agent_context()


@mcp.tool(
    title="List workspace files",
    annotations=ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    ),
)
def list_workspace(max_depth: int = 4, max_entries: int = 500) -> str:
    """List files and folders in the local workspace without reading file contents."""
    max_depth = max(0, min(max_depth, 12))
    max_entries = max(1, min(max_entries, 5000))

    lines: list[str] = []
    root_depth = len(WORKSPACE_DIR.parts)
    for path in sorted(WORKSPACE_DIR.rglob("*")):
        depth = len(path.parts) - root_depth
        if depth > max_depth:
            continue
        rel = path.relative_to(WORKSPACE_DIR).as_posix()
        suffix = "/" if path.is_dir() else ""
        lines.append(rel + suffix)
        if len(lines) >= max_entries:
            lines.append(f"...[stopped after {max_entries} entries]")
            break

    return "\n".join(lines) if lines else "Workspace is empty."


@mcp.tool(
    title="Read workspace file",
    annotations=ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    ),
)
def read_file(path: str, max_chars: int = 50_000) -> str:
    """Read a UTF-8 text file from the local workspace."""
    file_path = _resolve_workspace_path(path, must_exist=True)
    if not file_path.is_file():
        raise IsADirectoryError(f"Not a file: {path}")
    max_chars = max(1_000, min(max_chars, 500_000))
    content = file_path.read_text(encoding="utf-8", errors="replace")
    return _truncate(content, max_chars)


# ---------------------------------------------------------------------------
# Filesystem / shell tools
# ---------------------------------------------------------------------------

@mcp.tool(
    title="Run local shell command",
    annotations=ToolAnnotations(
        read_only_hint=False,
        destructive_hint=True,
        idempotent_hint=False,
        open_world_hint=True,
    ),
)
def run_shell(command: str, cwd: str = ".", timeout_seconds: int = 120) -> str:
    """Run a shell command on the local machine with the working directory constrained to workspace.

    The command itself is intentionally powerful and may modify files, launch local tools,
    or access the network. ChatGPT should use it only when necessary.
    """
    workdir = _resolve_workspace_path(cwd, must_exist=True)
    if not workdir.is_dir():
        raise NotADirectoryError(f"cwd is not a directory: {cwd}")

    timeout_seconds = max(1, min(timeout_seconds, 1800))
    try:
        result = subprocess.run(
            command,
            cwd=workdir,
            shell=True,
            capture_output=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return f"Command timed out after {timeout_seconds} seconds."
    except Exception as exc:
        return f"Command failed to start: {exc}"

    output = (_safe_decode(result.stdout) + _safe_decode(result.stderr)).strip()
    if not output:
        output = "Command completed with no output."
    return _truncate(f"exit_code={result.returncode}\n{output}", 30_000)


@mcp.tool(
    title="Write workspace file",
    annotations=ToolAnnotations(
        read_only_hint=False,
        destructive_hint=True,
        idempotent_hint=True,
        open_world_hint=False,
    ),
)
def write_file(path: str, content: str) -> str:
    """Create or overwrite a text file inside workspace.

    Core memory files are write-protected here; use update_memory_file for those.
    """
    file_path = _resolve_workspace_path(path)
    if file_path.name in PROTECTED_FILES:
        return (
            f"Refused: {file_path.name} is a protected memory/control file. "
            "Use update_memory_file instead."
        )

    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content, encoding="utf-8")
    rel = file_path.relative_to(WORKSPACE_DIR).as_posix()
    return f"Wrote {len(content)} characters to {rel}."


# ---------------------------------------------------------------------------
# Screen / input tools
# ---------------------------------------------------------------------------

@mcp.tool(
    title="Capture current screen",
    annotations=ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=False,
        open_world_hint=False,
    ),
    structured_output=False,
)
def take_screenshot(filename: str = "screenshot.png") -> list[TextContent | ImageContent]:
    """Capture the current screen, save it under workspace, and return the image to ChatGPT."""
    safe_name = Path(filename).name or "screenshot.png"
    if not safe_name.lower().endswith(".png"):
        safe_name += ".png"

    target = WORKSPACE_DIR / safe_name
    image = pyautogui.screenshot()
    image.save(target, format="PNG")

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")

    return [
        TextContent(type="text", text=f"Screenshot saved as {safe_name}."),
        ImageContent(type="image", data=encoded, mime_type="image/png"),
    ]


@mcp.tool(
    title="Press keyboard shortcut",
    annotations=ToolAnnotations(
        read_only_hint=False,
        destructive_hint=True,
        idempotent_hint=False,
        open_world_hint=False,
    ),
)
def press_hotkey(keys: list[str]) -> str:
    """Press a keyboard shortcut on the local desktop, e.g. [\"ctrl\", \"s\"]."""
    if not keys:
        raise ValueError("keys must not be empty")
    pyautogui.hotkey(*keys)
    return f"Pressed shortcut: {' + '.join(keys)}"


@mcp.tool(
    title="Click mouse",
    annotations=ToolAnnotations(
        read_only_hint=False,
        destructive_hint=True,
        idempotent_hint=False,
        open_world_hint=False,
    ),
)
def mouse_click(x: int, y: int, button: Literal["left", "right", "middle"] = "left") -> str:
    """Click the local desktop at the given screen coordinates."""
    pyautogui.click(x=x, y=y, button=button)
    return f"Clicked {button} at ({x}, {y})."


@mcp.tool(
    title="Check emergency stop key",
    annotations=ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=False,
        open_world_hint=False,
    ),
)
def is_stop_requested(hotkey: str = "f12") -> bool:
    """Return whether the local emergency-stop key is currently pressed."""
    return bool(keyboard.is_pressed(hotkey))


# ---------------------------------------------------------------------------
# Persistent memory / skill tools
# ---------------------------------------------------------------------------

@mcp.tool(
    title="Update persistent memory file",
    annotations=ToolAnnotations(
        read_only_hint=False,
        destructive_hint=True,
        idempotent_hint=False,
        open_world_hint=False,
    ),
)
def update_memory_file(
    target_file: str,
    content: str,
    mode: Literal["append", "overwrite"] = "append",
) -> str:
    """Update a core memory file, skill log, or today's daily log.

    target_file may be one of the standard root memory files, LEARNINGS.md,
    ERRORS.md, FEATURE_REQUESTS.md, or 'daily'.
    """
    if target_file.lower() == "daily":
        today = dt.date.today().strftime("%Y-%m-%d")
        file_path = MEMORY_DIR / f"{today}.md"
        display = f"memory/{today}.md"
    elif target_file in CORE_MEMORY_FILES:
        file_path = WORKSPACE_DIR / target_file
        display = target_file
    elif target_file in SKILL_MEMORY_FILES:
        file_path = WORKSPACE_DIR / "skills" / target_file
        display = f"skills/{target_file}"
    else:
        return f"Refused: unsupported memory target '{target_file}'."

    file_path.parent.mkdir(parents=True, exist_ok=True)
    if mode == "append":
        prefix = "\n\n" if file_path.exists() and file_path.stat().st_size > 0 else ""
        with file_path.open("a", encoding="utf-8") as f:
            f.write(prefix + content)
    else:
        file_path.write_text(content, encoding="utf-8")

    return f"Updated {display} using mode={mode}."


@mcp.tool(
    title="Prepare skill promotion path",
    annotations=ToolAnnotations(
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    ),
)
def promote_learning_to_skill(learning_id: str, skill_name: str, category: str) -> str:
    """Create the target skill directory and return where a resolved learning should be promoted.

    The caller should then write the final skill Markdown with write_file.
    """
    skill_name = _sanitize_name(skill_name, "skill_name")
    category = _sanitize_name(category, "category")
    target_dir = WORKSPACE_DIR / "skills" / category
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{skill_name}.md"

    if target.exists():
        return f"Refused: skill already exists at skills/{category}/{skill_name}.md"

    return (
        f"Learning {learning_id} can be promoted to skills/{category}/{skill_name}.md. "
        "The directory is ready; write the final Markdown with write_file."
    )


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Local MCP server for ChatGPT Developer Mode / custom MCP app."
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default=os.getenv("MCP_TRANSPORT", "streamable-http"),
        help="Use streamable-http for ChatGPT/Secure MCP Tunnel; stdio for local MCP testing.",
    )
    parser.add_argument("--host", default=os.getenv("MCP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("MCP_PORT", "8001")))
    args = parser.parse_args()

    if args.transport == "stdio":
        mcp.run("stdio")
    else:
        print(f"MCP server starting at http://{args.host}:{args.port}/mcp")
        mcp.run(
            "streamable-http",
            host=args.host,
            port=args.port,
            streamable_http_path="/mcp",
        )


if __name__ == "__main__":
    main()
