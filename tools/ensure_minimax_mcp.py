import os
import sys
import subprocess
import tempfile
from pathlib import Path

# Fixed search order: project .runtime -> TEMP -> APPDATA -> LOCALAPPDATA
_MINIMAX_MCP_TOOL_RELATIVE_PATH = os.path.join(
    "minimax-coding-plan-mcp",
    "Scripts",
    "minimax-coding-plan-mcp.exe",
)
_LEGACY_TEMP_TOOL_DIR = "uv_tools_stock_ai_bot"


def project_root() -> Path:
    """Return repository root."""
    return Path(__file__).resolve().parent.parent


def runtime_cache_dir(root: str | Path = None) -> str:
    base = Path(root) if root is not None else project_root()
    return str(base / ".runtime" / "uv_cache")


def runtime_tool_dir(root: str | Path = None) -> str:
    base = Path(root) if root is not None else project_root()
    return str(base / ".runtime" / "uv_tools")


def project_runtime_mcp_exe(root: str | Path = None) -> str:
    """Return the project-local MiniMax MCP executable path."""
    return os.path.abspath(os.path.join(runtime_tool_dir(root), _MINIMAX_MCP_TOOL_RELATIVE_PATH))


def build_uv_env(root: str | Path = None) -> dict[str, str]:
    """Generate environment dict mapping UV dirs under project .runtime."""
    cache_dir = runtime_cache_dir(root)
    tool_dir = runtime_tool_dir(root)

    # Ensure directories exist
    os.makedirs(cache_dir, exist_ok=True)
    os.makedirs(tool_dir, exist_ok=True)

    env = dict(os.environ)
    env["UV_CACHE_DIR"] = cache_dir
    env["UV_TOOL_DIR"] = tool_dir
    return env


def find_minimax_mcp_exe(
    root: str | Path = None,
    temp_dir: str = None,
    appdata_dir: str = None,
    localappdata_dir: str = None,
) -> str | None:
    """Check if minimax-coding-plan-mcp.exe exists in any of the UV tool directories.

    Search order: project .runtime -> TEMP -> APPDATA -> LOCALAPPDATA.
    Returns the absolute path of the first existing exe, or None.
    """
    search_paths = []

    search_paths.append(os.path.join(runtime_tool_dir(root), _MINIMAX_MCP_TOOL_RELATIVE_PATH))

    if temp_dir is not None:
        search_paths.append(os.path.join(temp_dir, _LEGACY_TEMP_TOOL_DIR, _MINIMAX_MCP_TOOL_RELATIVE_PATH))
    else:
        search_paths.append(os.path.join(tempfile.gettempdir(), _LEGACY_TEMP_TOOL_DIR, _MINIMAX_MCP_TOOL_RELATIVE_PATH))

    if appdata_dir is not None:
        search_paths.append(os.path.join(appdata_dir, "uv", "tools", _MINIMAX_MCP_TOOL_RELATIVE_PATH))
    else:
        appdata = os.environ.get("APPDATA")
        if appdata:
            search_paths.append(os.path.join(appdata, "uv", "tools", _MINIMAX_MCP_TOOL_RELATIVE_PATH))

    if localappdata_dir is not None:
        search_paths.append(os.path.join(localappdata_dir, "uv", "tools", _MINIMAX_MCP_TOOL_RELATIVE_PATH))
    else:
        localappdata = os.environ.get("LOCALAPPDATA")
        if localappdata:
            search_paths.append(os.path.join(localappdata, "uv", "tools", _MINIMAX_MCP_TOOL_RELATIVE_PATH))

    for path in search_paths:
        if os.path.exists(path):
            return os.path.abspath(path)
    return None


def build_install_command(venv_dir: str = None) -> list[str]:
    """Generate command to install minimax-coding-plan-mcp, prioritizing .venv/Scripts/uv.exe."""
    if venv_dir is None:
        proj_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        venv_dir = os.path.join(proj_root, ".venv")

    venv_uv = os.path.join(venv_dir, "Scripts", "uv.exe")
    if os.path.exists(venv_uv):
        return [os.path.abspath(venv_uv), "tool", "install", "--force", "minimax-coding-plan-mcp"]
    return ["uv", "tool", "install", "--force", "minimax-coding-plan-mcp"]


def format_success_output(exe_path: str) -> list[str]:
    """Return key=value lines for successful discovery."""
    return [f"MINIMAX_MCP_READY=1", f"MINIMAX_MCP_COMMAND={exe_path}"]


def format_failure_output(error: str) -> list[str]:
    """Return key=value lines for failure."""
    return [f"MINIMAX_MCP_READY=0", f"MINIMAX_MCP_ERROR={error}"]


def main():
    # 1. The startup path must be project-local ASCII-only to avoid CMD
    # encoding issues with non-ASCII Windows usernames.
    exe = project_runtime_mcp_exe()
    if exe:
        if os.path.exists(exe):
            for line in format_success_output(exe):
                print(line)
            sys.exit(0)

    print("MiniMax MCP executable not found. Attempting auto installation...")
    # 2. Build command and environment
    cmd = build_install_command()
    env = build_uv_env()

    print(f"Running command: {' '.join(cmd)}")
    try:
        use_shell = (cmd[0] == "uv")
        result = subprocess.run(
            cmd,
            env=env,
            shell=use_shell,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode != 0:
            print("Auto installation failed.")
            print(f"Subprocess Stdout: {result.stdout}")
            print(f"Subprocess Stderr: {result.stderr}")
    except Exception as e:
        print(f"Error executing installation: {e}")

    # 3. Double check after installer run
    exe = project_runtime_mcp_exe()
    if os.path.exists(exe):
        for line in format_success_output(exe):
            print(line)
        sys.exit(0)
    else:
        for line in format_failure_output("minimax-coding-plan-mcp.exe could not be found or installed"):
            print(line)
        # Manual installation instruction
        print("Suggested manual command: uv tool install minimax-coding-plan-mcp")
        sys.exit(1)

if __name__ == "__main__":
    main()
