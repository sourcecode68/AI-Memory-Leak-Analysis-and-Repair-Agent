"""The repair session. The model proposes a fix; this code applies it to a copy, verifies it, and keeps or reverts it.

No model is involved here. Checked by tests/test_repair.py.
"""
import difflib
import json
import re
import shutil
import subprocess
from pathlib import Path

import tools   # RepairSession switches tools.WORKSPACE, so every tool sees only the work copy

TESTCASES = Path(__file__).resolve().parent / "testcases"
WORK_ROOT = Path("/tmp/leak-agent/work")
MAX_ATTEMPTS = 3
MAX_CHANGED_LINES = 20   # lines removed + added, against the original; the largest accepted fix so far changed 14
HEADER_DIRS = (Path("/usr/include"), Path("/usr/lib/gcc"))   # where g++ finds the standard headers
_LINE_PREFIX = re.compile(r"^\s*\d+\| ", re.M)   # the "  9| " that read_file adds


def load_inputs(name):
    """The predefined argument lists for one program, from testcases/inputs.json."""
    table = json.loads((TESTCASES / "inputs.json").read_text())
    return table.get(name, table["default"])


def measure(name, inputs):
    """Compile the work copy and run it on every input. Returns (runs, None) or (None, problem)."""
    built = tools.compile_code(name)
    if not built.get("success"):
        return None, built.get("errors") or built.get("error")
    runs = []
    for args in inputs:
        r = tools.run_valgrind(built["executable"], args)
        if "error" in r:
            return None, r["error"]
        s = r["leak_summary"]
        runs.append({
            "args": args,
            "output": r["program_output"],
            "exit_code": r["program_exit_code"],
            "lost_bytes": sum(s[k]["bytes"] for k in ("definitely_lost", "indirectly_lost", "possibly_lost")),
            # first line of every record that isn't a leak: "Invalid read of size 4", "Mismatched free() ..."
            "memory_errors": [rec.splitlines()[0] for rec in r["records"]
                              if " lost in loss record" not in rec and "still reachable" not in rec],
        })
    return runs, None


def totals(runs):
    """(lost bytes, memory errors) summed over all inputs."""
    return sum(r["lost_bytes"] for r in runs), sum(len(r["memory_errors"]) for r in runs)


def apply_edits(text, edits):
    """Apply exact, unique text replacements. Returns (new_text, None) or (None, problem)."""
    if not isinstance(edits, list) or not edits:
        return None, "edits must be a non-empty list of {old_text, new_text}"
    for i, edit in enumerate(edits, 1):
        if not isinstance(edit, dict) or not isinstance(edit.get("old_text"), str) or not isinstance(edit.get("new_text"), str):
            return None, f"edit {i}: needs string fields old_text and new_text"
        old = edit["old_text"]
        if not old:
            return None, f"edit {i}: old_text is empty"
        if _LINE_PREFIX.search(old):
            return None, f"edit {i}: old_text contains line-number prefixes like '  9| '; copy the code without them"
        count = text.count(old)
        if count != 1:
            return None, f"edit {i}: old_text must appear exactly once in the current file, found {count} times"
        text = text.replace(old, edit["new_text"])
    return text, None


def changed_lines(before, after):
    """Lines removed plus lines added, as a diff counts them."""
    matcher = difflib.SequenceMatcher(None, before.splitlines(), after.splitlines())
    return sum((i2 - i1) + (j2 - j1) for tag, i1, i2, j1, j2 in matcher.get_opcodes() if tag != "equal")


def outside_includes(text, work_dir):
    """Files outside HEADER_DIRS that `text` would #include. Returns (paths, None) or (None, problem).

    Asks the preprocessor (g++ -M lists every file it opens) instead of reading #include lines,
    so a macro, a comment, a line splice or a digraph can't hide an include.
    """
    cmd = ["g++", "-std=c++17", "-M", "-x", "c++", "-"]   # "-": read the candidate from stdin; nothing is written
    try:
        r = subprocess.run(cmd, input=text, capture_output=True, text=True,
                           timeout=tools.COMPILE_TIMEOUT, cwd=work_dir)
    except subprocess.TimeoutExpired:
        return None, "checking the #include lines timed out"
    if r.returncode != 0:
        # The preprocessor's messages are never passed on: they can quote lines of an included file.
        missing = [m for m in re.findall(r"fatal error: (\S+): No such file", r.stderr) if m in text]
        return None, f"no such header: {', '.join(missing)}" if missing else "the preprocessor rejected this fix"
    deps = r.stdout.split(":", 1)[-1].replace("\\\n", " ").split()   # "-: /usr/include/... \<newline> ..."
    return [d for d in deps if not any(Path(d).resolve().is_relative_to(root) for root in HEADER_DIRS)], None


class RepairSession:
    """All state for repairing one file: the original, the last accepted version, measurements, attempts."""

    def __init__(self, source_name, approve=None):
        self.name = source_name
        self.approve = approve   # approve(diff, explanation) -> (approved, reason), asked before a fix runs; None: don't ask
        self.work_dir = WORK_ROOT / Path(source_name).stem
        shutil.rmtree(self.work_dir, ignore_errors=True)
        self.work_dir.mkdir(parents=True)
        self.path = self.work_dir / source_name
        shutil.copy(TESTCASES / source_name, self.path)    # the test set itself is never edited
        tools.WORKSPACE = self.work_dir                     # every tool now sees only the work copy
        self.original = self.accepted = self.path.read_text()
        self.inputs = load_inputs(source_name)
        self.baseline, problem = measure(source_name, self.inputs)
        if self.baseline is None:
            raise RuntimeError(f"the original does not build or run: {problem}")
        self.current = self.baseline                        # measurements of the last accepted version
        self.attempts = 0
        self.log = []

    def apply_fix(self, source_file, edits, explanation=""):
        """The apply_fix tool. Returns the program's verdict, or {"error": ...} for a malformed request."""
        if source_file != self.name:
            return {"error": f"only {self.name} can be changed in this session"}
        if self.attempts >= MAX_ATTEMPTS:
            return {"error": f"no attempts left: all {MAX_ATTEMPTS} have been used"}
        candidate, problem = apply_edits(self.accepted, edits)
        if problem is None:
            problem = self._outside_limits(candidate)
        if problem:
            return {"error": problem, "note": "nothing was changed; this did not use an attempt"}
        if self.approve is not None:                       # a person sees the fix before it is written or run
            proposed = "".join(difflib.unified_diff(self.accepted.splitlines(True), candidate.splitlines(True),
                                                    f"current/{self.name}", f"proposed/{self.name}"))
            approved, reason = self.approve(proposed, explanation)
            if not approved:
                return {"error": "the user declined to run this fix" + (f": {reason}" if reason else ""),
                        "note": "nothing was changed or run; this did not use an attempt"}

        self.attempts += 1
        self.path.write_text(candidate)
        runs, build_problem = measure(self.name, self.inputs)
        verdict = self._judge(runs, build_problem)
        if verdict["accepted"]:
            self.accepted, self.current = candidate, runs
        else:
            self.path.write_text(self.accepted)            # revert
            tools.compile_code(self.name)                  # rebuild, so no rejected binary is left behind
        verdict.update(attempt=self.attempts, attempts_left=MAX_ATTEMPTS - self.attempts)
        self.log.append({"explanation": explanation, **verdict})
        return verdict

    def _outside_limits(self, candidate):
        """Limits on the fix itself, checked before anything is written, compiled or run."""
        changed = changed_lines(self.original, candidate)
        if changed > MAX_CHANGED_LINES:
            return (f"this fix would change {changed} lines of the original (limit {MAX_CHANGED_LINES}); "
                    "make the smallest change that removes the leak")
        outside, problem = outside_includes(candidate, self.work_dir)
        if problem:
            return problem
        if outside:
            return f"only standard headers may be included, not: {', '.join(outside)}"
        return None

    def _judge(self, runs, build_problem):
        """The acceptance checks, in order. The first failure is the reason."""
        if runs is None:
            return {"accepted": False, "reason": "does not compile or run", "details": build_problem}
        for base, now in zip(self.baseline, runs):         # behaviour: against the ORIGINAL
            if (now["output"], now["exit_code"]) != (base["output"], base["exit_code"]):
                return {"accepted": False, "reason": f"behaviour changed for args={now['args']}",
                        "expected_output": base["output"], "actual_output": now["output"],
                        "expected_exit_code": base["exit_code"], "actual_exit_code": now["exit_code"]}
        for prev, now in zip(self.current, runs):          # memory: against the LAST ACCEPTED version
            if len(now["memory_errors"]) > len(prev["memory_errors"]):
                return {"accepted": False, "reason": f"new memory error for args={now['args']}",
                        "details": now["memory_errors"]}
            if now["lost_bytes"] > prev["lost_bytes"]:
                return {"accepted": False, "reason": f"more memory lost for args={now['args']}"}
        before, after = totals(self.current), totals(runs)
        if after == before:
            return {"accepted": False, "reason": "no measurable improvement: lost bytes and memory errors unchanged"}
        return {"accepted": True, "status": "fixed" if after == (0, 0) else "improved",
                "lost_bytes": f"{before[0]} -> {after[0]}", "memory_errors": f"{before[1]} -> {after[1]}"}

    def final_status(self):
        if totals(self.baseline) == (0, 0):
            return "nothing to fix: no leak or memory error observed on the predefined inputs"
        if self.accepted == self.original:
            return "not fixed: no fix was accepted"
        if totals(self.current) == (0, 0):
            return "fixed: no leak or memory error observed on the predefined inputs"
        return "improved: some leaks or memory errors remain"

    def diff(self):
        return "".join(difflib.unified_diff(self.original.splitlines(True), self.accepted.splitlines(True),
                                            f"original/{self.name}", f"repaired/{self.name}"))
