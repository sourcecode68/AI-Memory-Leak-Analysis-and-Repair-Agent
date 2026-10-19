"""Checks for repair.py, using hand-written fixes (no model, no tokens).

Run from the project root:  python tests/test_repair.py
Work copies and binaries go to a temporary folder; testcases/ is checked to be unchanged at the end.
"""
import hashlib
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))   # the modules live in the project root
import tools  # noqa: E402
import repair  # noqa: E402

results = {"passed": 0, "failed": 0}


def check(name, condition, got=None):
    if condition:
        results["passed"] += 1
        print(f"  PASS  {name}")
    else:
        results["failed"] += 1
        print(f"  FAIL  {name}\n        got: {got!r}"[:600])


def edit(old, new):
    return {"old_text": old, "new_text": new}


def testcases_digest():
    return hashlib.md5(b"".join(p.read_bytes() for p in sorted((ROOT / "testcases").glob("*")))).hexdigest()


def test_edit_format():
    print("edit format (03)")
    s = repair.RepairSession("03_sum_positive.cpp")
    check("baseline: 12 bytes lost", [r["lost_bytes"] for r in s.baseline] == [12], s.baseline)
    v = s.apply_fix("03_sum_positive.cpp", [edit("   4|     int* copy = new int[n];", "x")])
    check("line-number prefixes are refused", "error" in v, v)
    v = s.apply_fix("03_sum_positive.cpp", [edit("not in the file", "x")])
    check("text that isn't in the file is refused", "error" in v, v)
    v = s.apply_fix("03_sum_positive.cpp", [edit("i", "j")])
    check("text that appears more than once is refused", "error" in v, v)
    check("refused edits don't use an attempt", s.attempts == 0, s.attempts)
    v = s.apply_fix("../testcases/03_sum_positive.cpp", [edit("int total = 0;", "int total = 0; ")])
    check("only the session's file can be changed", "error" in v, v)


def test_verdicts_03():
    print("verdicts (03)")
    s = repair.RepairSession("03_sum_positive.cpp")
    v = s.apply_fix("03_sum_positive.cpp", [edit('    std::cout << "bad: " << sum_positive(bad, 3) << "\\n";\n', "")])
    check("removing functionality is rejected (output changed)", v.get("accepted") is False, v)
    check("...and the file is reverted", s.path.read_text() == s.original)
    v = s.apply_fix("03_sum_positive.cpp", [edit("int* copy = new int[n];", "std::vector<int> copy(n);")])
    check("a fix that doesn't compile is rejected", v.get("accepted") is False, v)
    v = s.apply_fix("03_sum_positive.cpp", [
        edit("#include <iostream>", "#include <iostream>\n#include <vector>"),
        edit("int* copy = new int[n];", "std::vector<int> copy(n);"),
        edit("    delete[] copy;\n", ""),
    ])
    check("an RAII fix is accepted as fixed", v.get("accepted") is True and v.get("status") == "fixed", v)
    check("final status says fixed", s.final_status().startswith("fixed"), s.final_status())
    check("diff shows the change", "+    std::vector<int> copy(n);" in s.diff(), s.diff())
    v = s.apply_fix("03_sum_positive.cpp", [edit("int total = 0;", "int total = 0; ")])
    check(f"no attempt after MAX_ATTEMPTS ({repair.MAX_ATTEMPTS})", "error" in v, v)


def test_new_memory_error_01():
    print("new memory errors (01)")
    s = repair.RepairSession("01_point.cpp")
    v = s.apply_fix("01_point.cpp", [edit("    Point* p = new Point{3, 4};\n",
                                          "    Point* p = new Point{3, 4};\n    delete p;\n")])
    check("a use-after-free 'fix' is rejected even though the leak is gone", v.get("accepted") is False, v)
    v = s.apply_fix("01_point.cpp", [edit("Point* p = new Point{3, 4};", "Point pt{3, 4};\n    Point* p = &pt;")])
    check("a stack-value fix is accepted", v.get("status") == "fixed", v)


def test_partial_then_complete_09():
    print("partial fixes (09)")
    s = repair.RepairSession("09_list_and_counts.cpp")
    v = s.apply_fix("09_list_and_counts.cpp", [edit("    delete list;\n", "    delete list->next;\n    delete list;\n")])
    check("a partial fix is accepted as improved", v.get("accepted") is True and v.get("status") == "improved", v)
    check("final status says improved", s.final_status().startswith("improved"), s.final_status())
    v = s.apply_fix("09_list_and_counts.cpp", [edit(
        "    delete list->next;\n    delete list;\n",
        "    while (list != nullptr) {\n        Node* next = list->next;\n        delete list;\n        list = next;\n    }\n")])
    check("the complete fix is accepted as fixed", v.get("status") == "fixed", v)


def test_other_cases():
    print("memory errors without leaks (06), nothing to fix (08), inputs (10)")
    s = repair.RepairSession("06_scores.cpp")
    v = s.apply_fix("06_scores.cpp", [edit("    delete scores;", "    delete[] scores;")])
    check("06: fixing the mismatched delete counts as fixed", v.get("status") == "fixed", v)

    s = repair.RepairSession("08_linked_sum.cpp")
    check("08: nothing to fix", s.final_status().startswith("nothing to fix"), s.final_status())
    v = s.apply_fix("08_linked_sum.cpp", [edit("int sum = 0;", "int sum = 0;  // total")])
    check("08: an edit with no improvement is rejected", v.get("accepted") is False, v)

    s = repair.RepairSession("10_config_mode.cpp")
    check("10: baseline uses both inputs from inputs.json",
          [(r["args"], r["lost_bytes"]) for r in s.baseline] == [([], 0), (["debug"], 12)], s.baseline)
    v = s.apply_fix("10_config_mode.cpp", [edit("        config = verbose;", "        delete[] config;\n        config = verbose;")])
    check("10: delete[] before reassigning is accepted as fixed", v.get("status") == "fixed", v)


def test_limits():
    print("limits on a fix: size (05) and includes (01)")
    s = repair.RepairSession("05_buffer_process.cpp")
    v = s.apply_fix("05_buffer_process.cpp", [edit(s.original, '#include <iostream>\nint main() { std::cout << "x\\n"; }\n')])
    check(f"rewriting the whole file is refused (over {repair.MAX_CHANGED_LINES} changed lines)", "error" in v, v)

    s = repair.RepairSession("01_point.cpp")
    secret = repair.WORK_ROOT.parent / "secret.txt"
    secret.write_text("FAKE_KEY=not-a-real-key\n")
    tricks = {
        "a quoted absolute path": f'#include "{secret}"',
        "a path that climbs out of the header folders": f"#include <{'../' * 8}{str(secret).lstrip('/')}>",
        "an include built by a macro": f'#define F "{secret}"\n#include F',
        "an include after a comment": f'/**/ #include "{secret}"',
        "an include split across lines": f'#inc\\\nlude "{secret}"',
        "a digraph include": f'%:include "{secret}"',
    }
    for name, text in tricks.items():
        v = s.apply_fix("01_point.cpp", [edit("#include <iostream>", "#include <iostream>\n" + text)])
        check(f"refuses {name}, without quoting the file", "error" in v and "FAKE_KEY" not in str(v), v)
    v = s.apply_fix("01_point.cpp", [edit("#include <iostream>", "#include <iostream>\n#include <vectr>")])
    check("a missing header is named", "vectr" in v.get("error", ""), v)
    check("refused fixes don't use an attempt", s.attempts == 0, s.attempts)


def test_review():
    print("asking a person before a fix runs (03)")
    shown = []

    def decline(proposed, explanation):
        shown.append(proposed)
        return False, "keep the raw pointer"

    s = repair.RepairSession("03_sum_positive.cpp", approve=decline)
    binary = tools.BUILD_DIR / "03_sum_positive"
    built = binary.stat().st_mtime_ns
    fix = [edit("#include <iostream>", "#include <iostream>\n#include <vector>"),
           edit("int* copy = new int[n];", "std::vector<int> copy(n);"),
           edit("    delete[] copy;\n", "")]
    v = s.apply_fix("03_sum_positive.cpp", fix)
    check("the person sees the diff", shown and "+    std::vector<int> copy(n);" in shown[0], shown)
    check("a declined fix sends the person's reason to the model", "keep the raw pointer" in v.get("error", ""), v)
    check("a declined fix is not written or compiled",
          s.path.read_text() == s.original and binary.stat().st_mtime_ns == built)
    check("a declined fix doesn't use an attempt", s.attempts == 0, s.attempts)
    s.approve = lambda proposed, explanation: (True, "")
    v = s.apply_fix("03_sum_positive.cpp", fix)
    check("an approved fix runs and is judged as usual", v.get("status") == "fixed", v)


if __name__ == "__main__":
    before = testcases_digest()
    tmp = Path(tempfile.mkdtemp(prefix="leak-agent-repair-test-"))
    repair.WORK_ROOT = tmp / "work"
    tools.BUILD_DIR = tmp / "build"
    try:
        test_edit_format()
        test_verdicts_03()
        test_new_memory_error_01()
        test_partial_then_complete_09()
        test_other_cases()
        test_limits()
        test_review()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    check("testcases/ was never modified", testcases_digest() == before)
    print(f"\n{results['passed']} passed, {results['failed']} failed")
    sys.exit(1 if results["failed"] else 0)
