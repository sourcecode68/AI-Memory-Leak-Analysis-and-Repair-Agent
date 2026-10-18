"""Checks for tools.py.  Run from the project root:  python tests/test_tools.py

Uses a temporary copy of a few test programs, so testcases/ is never touched.
"""
import re
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))   # the modules live in the project root
import tools  # noqa: E402

TESTCASES = ROOT / "testcases"
results = {"passed": 0, "failed": 0}


def check(name, condition, got=None):
    if condition:
        results["passed"] += 1
        print(f"  PASS  {name}")
    else:
        results["failed"] += 1
        print(f"  FAIL  {name}\n        got: {got!r}"[:600])


def setup():
    tmp = Path(tempfile.mkdtemp(prefix="leak-agent-test-"))
    workspace = tmp / "workspace"
    workspace.mkdir()
    for name in ["01_point.cpp", "05_buffer_process.cpp", "08_linked_sum.cpp", "10_config_mode.cpp"]:
        shutil.copy(TESTCASES / name, workspace / name)
    (workspace / "broken.cpp").write_text("int main() { return undefined_name; }\n")
    (workspace / "notes.txt").write_text("not a source file\n")
    tools.WORKSPACE = workspace
    tools.BUILD_DIR = tmp / "build"
    return tmp


def test_read_file():
    print("read_file")
    r = tools.read_file("01_point.cpp")
    content = r.get("content", "")
    check("reads a file in the workspace", "new Point{3, 4}" in content, r)
    check("numbers every line (line 9 holds the new)", re.search(r"^\s*9\s*[|:].*new Point", content, re.M), content[:300])
    for bad in ["../README.md", "/etc/passwd", "notes.txt", "missing.cpp"]:
        check(f"refuses {bad!r}", "error" in tools.read_file(bad), tools.read_file(bad))


def test_compile_code():
    print("compile_code")
    r = tools.compile_code("01_point.cpp")
    check("compiles a valid file", r.get("success") is True and r.get("executable") == "01_point", r)
    check("binary lands in BUILD_DIR", (tools.BUILD_DIR / "01_point").is_file())
    r = tools.compile_code("broken.cpp")
    check("reports a compile failure", r.get("success") is False and "undefined_name" in r.get("errors", ""), r)
    for bad in ["../../../etc/passwd", "notes.txt", "missing.cpp"]:
        check(f"refuses {bad!r}", "error" in tools.compile_code(bad), tools.compile_code(bad))

    stale = tools.WORKSPACE / "stale.cpp"
    shutil.copy(tools.WORKSPACE / "01_point.cpp", stale)
    tools.compile_code("stale.cpp")
    stale.write_text("int main() { return undefined_name; }\n")
    tools.compile_code("stale.cpp")
    check("a failed build removes the old binary", not (tools.BUILD_DIR / "stale").exists())
    check("so the old binary can't be run", "error" in tools.run_valgrind("stale"), tools.run_valgrind("stale"))


def test_run_valgrind():
    print("run_valgrind")
    for name in ["01_point.cpp", "05_buffer_process.cpp", "08_linked_sum.cpp", "10_config_mode.cpp"]:
        tools.compile_code(name)

    r = tools.run_valgrind("01_point")
    s = r.get("leak_summary", {})
    check("01: 8 bytes definitely lost in 1 block", s.get("definitely_lost") == {"bytes": 8, "blocks": 1}, r)
    check("01: error_count is 1", r.get("error_count") == 1, r.get("error_count"))
    check("01: a record points at 01_point.cpp:9", any("01_point.cpp:9" in rec for rec in r.get("records", [])), r.get("records"))
    check("01: records keep only frames from your own files",
          all("vgpreload" not in rec and "operator new" not in rec for rec in r.get("records", [])), r.get("records"))
    check("01: program output captured", "point: 3,4" in r.get("program_output", ""), r.get("program_output"))
    check("01: coverage note present", "coverage_note" in r, r)

    s = tools.run_valgrind("05_buffer_process").get("leak_summary", {})
    check("05: 16 definitely + 16 indirectly lost", s.get("definitely_lost", {}).get("bytes") == 16
          and s.get("indirectly_lost", {}).get("bytes") == 16, s)

    r = tools.run_valgrind("08_linked_sum")
    zero = all(v.get("bytes") == 0 for v in r.get("leak_summary", {}).values()) and r.get("leak_summary")
    check("08: no leaks, 0 errors", zero and r.get("error_count") == 0, r)

    r = tools.run_valgrind("10_config_mode")
    check("10 without args: nothing lost", r.get("leak_summary", {}).get("definitely_lost", {}).get("bytes") == 0, r)
    r = tools.run_valgrind("10_config_mode", ["debug"])
    check("10 with ['debug']: 12 bytes definitely lost", r.get("leak_summary", {}).get("definitely_lost", {}).get("bytes") == 12, r)
    check("10 with ['debug']: args echoed back", r.get("args") == ["debug"], r.get("args"))

    for bad in ["/bin/ls", "../../../bin/ls", "never_built"]:
        check(f"refuses executable {bad!r}", "error" in tools.run_valgrind(bad), tools.run_valgrind(bad))
    check("refuses args that aren't a list", "error" in tools.run_valgrind("01_point", "debug"))
    check("refuses more than 5 args", "error" in tools.run_valgrind("01_point", ["x"] * 6))


if __name__ == "__main__":
    tmp = setup()
    try:
        test_read_file()
        test_compile_code()
        test_run_valgrind()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{results['passed']} passed, {results['failed']} failed")
    sys.exit(1 if results["failed"] else 0)
