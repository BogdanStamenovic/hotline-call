"""Read-only lookups for the Haiku call voice: read a file, grep, glob, git log.

**Why these and nothing else.** The first three are what `CallAgent` gets from
`claude -p --tools=Read,Grep,Glob`; git_log is there because "what changed"
is the commonest question on a call and `.git` is skipped by the others
(2026-10-30: asked for cvoice's last commit, it searched for five rounds and
gave up). The same rule holds: the voice on the
call SAYS things, the session that owns the work DOES them (his instruction,
2026-09-10). Nothing here writes, runs, or reaches the network.

**Why confined to ~/data, and why secrets are refused.** Whatever a tool
returns is sent to the API as the next turn's input. `claude -p` with Read
could open any file; this one cannot leave `ROOT`, and files that look like
credentials (.env, *key*, *token*, *secret*, ssh keys) are refused by name,
because a secret read here has left the machine. The name filter is a screen,
not a guarantee - a key pasted into a file called notes.md gets through.

**Why the output is small.** Every byte returned is read by the model before
it can say the next word, on a live call. Capped at ~12k characters per call.
"""

from __future__ import annotations

import fnmatch
import os
import pathlib
import re
import shutil
import subprocess

ROOT = pathlib.Path(os.environ.get("HOTLINE_CWD", "/home/bodas/data")).resolve()
MAX_CHARS = 12_000
MAX_LINES = 200
MAX_HITS = 60
TIMEOUT = 5.0

SECRET_NAMES = (
    ".env", ".env.*", "*.env", "*key*", "*token*", "*secret*", "*credential*",
    "*password*", "id_rsa*", "id_ed25519*", "*.pem", "*.p12", "*.kdbx", ".netrc",
)
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".cache"}

SCHEMAS = [
    {
        "name": "read_file",
        "description": "Read a text file under ~/data. Returns numbered lines.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute, or relative to ~/data."},
                "offset": {"type": "integer", "description": "First line, 1-based. Default 1."},
                "limit": {"type": "integer", "description": f"Lines to read. Default and max {MAX_LINES}."},
            },
            "required": ["path"],
        },
    },
    {
        "name": "grep",
        "description": "Search file contents under ~/data with a regular expression. "
                       "Returns path:line:text matches.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "path": {"type": "string", "description": "Directory or file. Default ~/data."},
                "glob": {"type": "string", "description": "Only files matching this, e.g. '*.md'."},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "git_log",
        "description": "Recent commits of a git repository under ~/data: hash, age, subject.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Repo directory, e.g. 'cvoice' or '/home/bodas/data/hotline'."},
                "count": {"type": "integer", "description": "How many commits. Default 10, max 30."},
            },
            "required": ["repo"],
        },
    },
    {
        "name": "glob",
        "description": "List files under ~/data matching a glob pattern, newest first.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "e.g. 'hotline*/README.md' or '**/*.log'."},
                "path": {"type": "string", "description": "Where to start. Default ~/data."},
            },
            "required": ["pattern"],
        },
    },
]


class Refused(ValueError):
    pass


def _secret(path: pathlib.Path) -> bool:
    name = path.name.lower()
    return any(fnmatch.fnmatch(name, pat) for pat in SECRET_NAMES)


def _resolve(raw: str | None) -> pathlib.Path:
    p = pathlib.Path(os.path.expanduser(raw or str(ROOT)))
    if not p.is_absolute():
        p = ROOT / p
    p = p.resolve()
    if p != ROOT and ROOT not in p.parents:
        raise Refused(f"{raw} is outside {ROOT}")
    if not p.is_dir() and _secret(p):
        raise Refused(f"{p.name} looks like a credential file; not read on a call")
    return p


def _cap(text: str) -> str:
    return text if len(text) <= MAX_CHARS else text[:MAX_CHARS] + "\n[...cut]"


def read_file(path: str, offset: int = 1, limit: int = MAX_LINES) -> str:
    p = _resolve(path)
    if not p.is_file():
        return f"no such file: {p}"
    offset = max(1, int(offset or 1))
    limit = max(1, min(int(limit or MAX_LINES), MAX_LINES))
    out = []
    with p.open("r", encoding="utf-8", errors="replace") as handle:
        for number, line in enumerate(handle, 1):
            if number < offset:
                continue
            if number >= offset + limit:
                out.append(f"[...more after line {number - 1}]")
                break
            out.append(f"{number}\t{line.rstrip()}")
    return _cap("\n".join(out) or "(empty)")


def grep(pattern: str, path: str | None = None, glob: str | None = None) -> str:
    p = _resolve(path)
    re.compile(pattern)  # a bad pattern is the model's mistake; say so plainly
    rg = shutil.which("rg")
    if rg:
        cmd = [rg, "-n", "--no-heading", "--max-count", "5", "--max-columns", "200"]
        for pat in SECRET_NAMES:
            cmd += ["-g", f"!{pat}"]
        if glob:
            cmd += ["-g", glob]
        cmd += ["-e", pattern, str(p)]
    else:
        cmd = ["grep", "-rnIE", "-m", "5", *[f"--exclude={x}" for x in SECRET_NAMES],
               *[f"--exclude-dir={d}" for d in SKIP_DIRS]]
        if glob:
            cmd.append(f"--include={glob}")
        cmd += ["-e", pattern, str(p)]
    try:
        run = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT)
        lines = run.stdout.splitlines()
    except subprocess.TimeoutExpired as exc:
        lines = (exc.stdout or b"").decode("utf-8", "replace").splitlines() \
            if isinstance(exc.stdout, bytes) else (exc.stdout or "").splitlines()
        lines.append(f"[search stopped after {TIMEOUT:.0f} s; narrow the path]")
    if not lines:
        return "no matches"
    more = f"\n[...{len(lines) - MAX_HITS} more]" if len(lines) > MAX_HITS else ""
    return _cap("\n".join(lines[:MAX_HITS]) + more)


def glob(pattern: str, path: str | None = None) -> str:
    base = _resolve(path)
    hits = []
    for p in base.glob(pattern):
        if any(part in SKIP_DIRS for part in p.relative_to(base).parts) or \
                (not p.is_dir() and _secret(p)):
            continue
        try:
            hits.append((p.stat().st_mtime, p))
        except OSError:
            continue
        if len(hits) >= 2000:
            break
    if not hits:
        return "no matches"
    hits.sort(reverse=True)
    lines = [str(p) for _, p in hits[:MAX_HITS]]
    if len(hits) > MAX_HITS:
        lines.append(f"[...{len(hits) - MAX_HITS} more]")
    return "\n".join(lines)


def git_log(repo: str, count: int = 10) -> str:
    p = _resolve(repo)
    count = max(1, min(int(count or 10), 30))
    try:
        run = subprocess.run(["git", "-C", str(p), "log", f"-n{count}",
                              "--format=%h  %ar  %s"],
                             capture_output=True, text=True, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        return "git log timed out"
    return _cap(run.stdout.strip() or run.stderr.strip() or "no commits")


BASH_TIMEOUT = 10.0
HOME = pathlib.Path.home()
# Masked out of the sandbox entirely: what a command prints is sent to the API.
SECRET_DIRS = (".ssh", ".gnupg", ".config/anthropic", ".config/gh", ".config/cvoice",
               ".config/hotline-call", ".config/hotline", ".local/share/keyrings",
               ".password-store", ".aws", ".docker")
SECRET_FILES = (".claude/.credentials.json", ".netrc", ".git-credentials")
_REDACT = re.compile(
    r"(sk-ant-[\w-]{8,}|gh[pousr]_[A-Za-z0-9]{20,}|xox[abp]-[\w-]{10,}"
    r"|(?i:(?:token|secret|password|passwd|api[_-]?key)\s*[=:]\s*)\S+)")


def _env_files() -> list[pathlib.Path]:
    out = []
    for base in (ROOT, HOME / ".config"):
        try:
            out += [p for p in base.glob("*/.env*") if p.is_file()]
            out += [p for p in base.glob("*/*/.env*") if p.is_file()]
            out += [p for p in base.glob("*/*.env") if p.is_file()]
        except OSError:
            continue
    return out


def _sandbox() -> list[str]:
    """bwrap: the whole filesystem read-only, a private /tmp and /run (so no
    D-Bus or docker socket: `systemctl stop` cannot reach anything), no
    network, secrets masked. Read-only is enforced by the mounts, not asked
    for in the prompt - plenty of ordinary commands write."""
    cmd = ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
           "--tmpfs", "/tmp", "--tmpfs", "/run", "--unshare-net", "--unshare-ipc",
           "--die-with-parent", "--new-session", "--chdir", str(ROOT)]
    for dev in ("nvidiactl", "nvidia0", "nvidia-uvm"):
        if pathlib.Path("/dev", dev).exists():     # nvidia-smi needs these
            cmd += ["--dev-bind", f"/dev/{dev}", f"/dev/{dev}"]
    for rel in SECRET_DIRS:
        if (HOME / rel).is_dir():
            cmd += ["--tmpfs", str(HOME / rel)]
    for path in [HOME / rel for rel in SECRET_FILES] + _env_files():
        if path.is_file():
            cmd += ["--ro-bind", "/dev/null", str(path)]
    return cmd


def bash(command: str) -> str:
    """A shell command, read-only and offline. See `_sandbox` for what that means."""
    if not command.strip():
        raise ValueError("empty command")
    try:
        run_ = subprocess.run(_sandbox() + ["bash", "-c", command], capture_output=True,
                              text=True, timeout=BASH_TIMEOUT)
        out = (run_.stdout + (("\n" + run_.stderr) if run_.stderr.strip() else "")).strip()
        status = "" if run_.returncode == 0 else f"\n[exit {run_.returncode}]"
    except subprocess.TimeoutExpired:
        return f"[stopped after {BASH_TIMEOUT:.0f} s]"
    return _cap(_REDACT.sub("[redacted]", out) + status) or "(no output)"


SCHEMAS.append({
    "name": "bash",
    "description": (
        "Run a shell command on archserver to LOOK something up: nvidia-smi, df, free, "
        "ps, uptime, journalctl --user -u X, ls, cat, grep, git log... The whole "
        "filesystem is READ-ONLY and there is no network: anything that writes, "
        "installs, restarts or sends fails. systemctl cannot reach services (use "
        "journalctl to see their state). Output is capped; keep commands short."),
    "input_schema": {"type": "object", "properties": {"command": {"type": "string"}},
                     "required": ["command"]},
})

TOOLS = {"read_file": read_file, "grep": grep, "glob": glob, "git_log": git_log, "bash": bash}


def run(name: str, args: dict) -> tuple[str, bool]:
    """(result text, is_error). Never raises: a failed lookup is something to
    say out loud, not a dropped call."""
    fn = TOOLS.get(name)
    if fn is None:
        return f"unknown tool {name}", True
    try:
        return fn(**args), False
    except Refused as exc:
        return f"refused: {exc}", True
    except (TypeError, ValueError, re.error, OSError) as exc:
        return f"{type(exc).__name__}: {exc}", True
