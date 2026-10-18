"""The leak agent's tools: ordinary Python with no AI in it.

The model can only *request* these functions. Every path, flag and limit is decided here,
and every function returns a dict (an {"error": ...} dict for bad input) instead of raising.
"""
import re
import subprocess
from pathlib import Path

# Read at call time (not copied into defaults) so tests can point them at temporary folders.
WORKSPACE = Path(__file__).resolve().parent / "testcases"   # the only folder the agent may see
BUILD_DIR = Path("/tmp/leak-agent/build")                   # binaries never land in the repo

MAX_FILE_BYTES = 100_000
MAX_OUTPUT_CHARS = 2_000
MAX_RECORDS = 10
MAX_ARGS = 5
MAX_ARG_CHARS = 100
COMPILE_TIMEOUT = 30
VALGRIND_TIMEOUT = 60
SOURCE_SUFFIXES = {".cpp", ".cc", ".h", ".hpp"}
COMPILE_SUFFIXES = {".cpp", ".cc"}


def _in_workspace(path):
    """Resolve a model-supplied path. None if it escapes the workspace (../, absolute path, symlink)."""
    candidate = (WORKSPACE / str(path)).resolve()
    return candidate if candidate.is_relative_to(WORKSPACE.resolve()) else None


def _trim(text, limit=MAX_OUTPUT_CHARS):
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [{len(text) - limit} more characters cut]"


def read_file(path):
    """Return a source file with line numbers, so the model can match Valgrind's file:line."""
    file = _in_workspace(path)
    if file is None:
        return {"error": f"path is outside the workspace: {path}"}
    if file.suffix not in SOURCE_SUFFIXES:
        return {"error": f"not a C/C++ source file: {path}"}
    if not file.is_file():
        return {"error": f"no such file: {path}"}
    if file.stat().st_size > MAX_FILE_BYTES:
        return {"error": f"file too large: {path}"}
    lines = file.read_text(errors="replace").splitlines()
    numbered = "\n".join(f"{n:4}| {line}" for n, line in enumerate(lines, 1))
    return {"path": path, "content": numbered}


def compile_code(source_file):
    """Compile with fixed flags. The model chooses only which file, never how."""
    src = _in_workspace(source_file)
    if src is None:
        return {"error": f"path is outside the workspace: {source_file}"}
    if src.suffix not in COMPILE_SUFFIXES or not src.is_file():
        return {"error": f"no such C++ source file: {source_file}"}

    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    exe = BUILD_DIR / src.stem
    exe.unlink(missing_ok=True)   # a failed build must not leave an old binary for run_valgrind to test

    # An argument list, never a shell string: a file name like "x.cpp; rm -rf ~" stays one argument.
    cmd = ["g++", "-std=c++17", "-g", "-O0", "-Wall", "-Wextra", str(src), "-o", str(exe)]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=COMPILE_TIMEOUT)
    except subprocess.TimeoutExpired:
        return {"error": "compilation timed out"}

    diagnostics = result.stderr.replace(str(src.parent) + "/", "")   # shorter paths, fewer tokens
    if result.returncode != 0:
        return {"success": False, "errors": _trim(diagnostics)}
    return {"success": True, "executable": exe.name, "warnings": _trim(diagnostics) or "none"}


_SUMMARY = re.compile(r"(definitely lost|indirectly lost|possibly lost|still reachable): ([\d,]+) bytes in ([\d,]+) blocks")
_ERRORS = re.compile(r"ERROR SUMMARY: ([\d,]+) errors")
_RECORD_START = re.compile(r"^(Invalid |Mismatched |Conditional jump|Use of uninitialised"
                           r"|.* are (definitely|indirectly|possibly) lost|.* are still reachable)")
_FRAME = re.compile(r"\((\S+?\.(?:cpp|cc|h|hpp)):\d+\)")


def _num(text):
    return int(text.replace(",", ""))   # Valgrind prints 78,074


def parse_valgrind(report):
    """Turn a Memcheck log into the few facts the model needs."""
    lines = [re.sub(r"^==\d+== ?", "", line) for line in report.splitlines()]

    summary = {kind: {"bytes": 0, "blocks": 0}
               for kind in ("definitely_lost", "indirectly_lost", "possibly_lost", "still_reachable")}
    for kind, nbytes, blocks in _SUMMARY.findall(report):
        summary[kind.replace(" ", "_")] = {"bytes": _num(nbytes), "blocks": _num(blocks)}
    errors = _ERRORS.search(report)

    # A record starts at an error or loss-record header and ends at a blank line.
    records, current = [], None
    for line in lines:
        if _RECORD_START.match(line):
            current = [line]
            records.append(current)
        elif current is not None:
            if not line.strip():
                current = None
            elif line.startswith(" ") and not line.startswith("   "):
                current.append(line.strip())           # sub-header: "Address ... free'd", "Block was alloc'd at"
            else:
                frame = _FRAME.search(line)
                if frame and (WORKSPACE / frame.group(1)).is_file():
                    current.append("  " + line.strip())  # keep frames from the user's own files only

    return {
        "leak_summary": summary,
        "error_count": _num(errors.group(1)) if errors else None,
        "records": ["\n".join(record) for record in records[:MAX_RECORDS]],
    }


def run_valgrind(executable, args=None):
    """Run a binary built by compile_code under Memcheck. The model may choose the program's arguments."""
    exe = (BUILD_DIR / str(executable)).resolve()
    if exe.parent != BUILD_DIR.resolve() or not exe.is_file():
        return {"error": f"unknown executable: {executable}. Compile the source first."}

    args = [] if args is None else args
    if (not isinstance(args, list) or len(args) > MAX_ARGS
            or not all(isinstance(a, str) and len(a) <= MAX_ARG_CHARS for a in args)):
        return {"error": f"args must be a list of at most {MAX_ARGS} strings, each at most {MAX_ARG_CHARS} characters"}

    log = BUILD_DIR / f"{exe.name}.valgrind.log"   # keeps Valgrind's report apart from the program's output
    cmd = ["valgrind", "--leak-check=full", "--show-leak-kinds=all", f"--log-file={log}", str(exe), *args]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=VALGRIND_TIMEOUT, cwd=BUILD_DIR)
    except subprocess.TimeoutExpired:
        return {"error": "program timed out under Valgrind"}

    return {
        "args": args,
        "program_exit_code": result.returncode,
        "program_output": _trim(result.stdout),
        **parse_valgrind(log.read_text(errors="replace")),
        "coverage_note": f"Only the code paths executed with args={args} were checked.",
    }
