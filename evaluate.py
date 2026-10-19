"""Evaluate both agents on every test program, several runs each, and score them against expected.json.

Usage:  python evaluate.py [--runs 3] [--only 07_partners.cpp,10_config_mode.cpp] [--skip-analysis] [--skip-repair]
        python evaluate.py --rescore results/<time>.json      (recompute the scores only; no API calls)

Writes results/<time>.json (every run, with reports and diffs for manual review) and prints a summary.
The agents are not modified: their execute() and call_model() are wrapped to record what happened.
"""
import argparse
import contextlib
import io
import json
import re
import time
from pathlib import Path

from google.genai import errors

import leak_agent
import repair_agent
import tools

ROOT = Path(__file__).resolve().parent
TESTCASES = ROOT / "testcases"
EXPECTED = json.loads((ROOT / "expected.json").read_text())   # ground truth: outside testcases/, never shown to the agents
RESULTS_DIR = ROOT / "results"
FREE_TIER_PER_DAY = 500     # gemini-3.1-flash-lite, from the 429 message on 18 Oct 2026

# Phrases the system prompts forbid. Matching is approximate: every hit is saved for a person to check.
OVERCLAIMS = re.compile(
    r"leak[- ]free|free (?:of|from) (?:any )?(?:memory )?leaks|correctly manages (?:its |all )?memory"
    r"|(?:there are|has|contains|have) no (?:memory )?leaks|no (?:memory )?leaks (?:in|exist)"
    r"|all (?:memory )?leaks (?:were|have been|are) (?:eliminated|fixed|removed|resolved)"
    r"|(?:ensures?|guarantees?) (?:that )?(?:there are )?no (?:memory )?leaks|memory[- ]safe", re.I)
# "64 bytes ... definitely lost" (no other number in between); "definitively" is a real model misspelling.
BYTES_KIND = re.compile(r"(\d[\d,]*)\s*bytes?(?:\s+in\s+\d+\s+blocks?)?[^\d.\n]{0,30}?(defin\w*|indirect\w*|possibl\w*)\s+lost", re.I)
KIND_BYTES = re.compile(r"(defin\w*|indirect\w*|possibl\w*)\s+lost\s*[:=]?\s*(\d[\d,]*)\s*bytes", re.I)
# "48 bytes ... definitely/indirectly lost" is a total of two kinds; checked against their sum, then removed
COMBINED = re.compile(r"(\d[\d,]*)\s*bytes?(?:\s+in\s+\d+\s+blocks?)?[^\d.\n]{0,30}?(?:defin\w*|indirect\w*)\s*(?:/|and|or|\+)\s*(?:defin\w*|indirect\w*)\s+lost", re.I)
KIND_KEYS = {"defin": "definitely_lost", "indir": "indirectly_lost", "possi": "possibly_lost"}
LINE_REF = re.compile(r"(?:\blines?\s+|\.cpp:)(\d+(?:\s*(?:-|–|to|,|and|&)\s*\d+)*)", re.I)
INVENTED = re.compile(r"==\s*(?:\d+|X+|PID)\s*==", re.I)   # tools strip ==PID==, so the model never saw one
SECTION = re.compile(r"\*{0,2}(Root cause)\*{0,2}:?(.*?)(?=\n\s*#*\s*\*{0,2}(?:Suspected|Fix|Verification|Not covered)\b|\Z)", re.I | re.S)


class Recorder:
    """Wraps an agent module's execute() and call_model(); collects tool calls, tokens, the report and API errors."""

    def __init__(self, module, execute_takes_map):
        self.module, self.execute_takes_map = module, execute_takes_map
        self.real_execute, self.real_call = module.execute, module.call_model

    def __enter__(self):
        self.tools, self.prompt_tokens, self.calls, self.report, self.api_error = [], 0, 0, None, None

        def execute(*a):
            name, args = (a[1], a[2]) if self.execute_takes_map else (a[0], a[1])
            outcome = self.real_execute(*a)
            self.tools.append({"tool": name, "args": dict(args or {}), "outcome": outcome})
            return outcome

        def call_model(history, retries=3):
            try:
                response = self.real_call(history, retries)
            except errors.APIError as e:
                self.api_error = f"{e.code}: {e}"
                raise
            self.calls += 1
            self.prompt_tokens += response.usage_metadata.prompt_token_count or 0
            if not response.function_calls:
                self.report = response.text
            return response

        self.module.execute, self.module.call_model = execute, call_model
        return self

    def __exit__(self, *exc):
        self.module.execute, self.module.call_model = self.real_execute, self.real_call


def valgrind_runs(recorded):
    return [t for t in recorded if t["tool"] == "run_valgrind" and "leak_summary" in t["outcome"]]


def observed(run):
    """Did this Valgrind run show a problem? Lost bytes (definitely/indirectly/possibly) or a memory-error record."""
    s = run["outcome"]["leak_summary"]
    lost = sum(s[k]["bytes"] for k in ("definitely_lost", "indirectly_lost", "possibly_lost"))
    errs = [r for r in run["outcome"]["records"] if " lost in loss record" not in r and "still reachable" not in r]
    return lost > 0 or bool(errs)


def lines_named(text):
    found = set()
    for group in LINE_REF.findall(text or ""):
        for a, b in re.findall(r"(\d+)(?:\s*(?:-|–|to)\s*(\d+))?", group):
            a = int(a)
            found.update(range(a, int(b) + 1) if b and 0 < int(b) - a < 40 else [a])
    return found


def score_report(raw_report, recorded, truth):
    """Checks on the model's written report, against the ground truth and the tool results it actually saw."""
    raw_report = raw_report or ""
    report = re.sub(r"[*`]", "", raw_report)          # Markdown: "lines **14** and **15**" -> "lines 14 and 15"
    section = SECTION.search(report)
    named = lines_named(section.group(2) if section else report)
    bug, alloc = set(truth["root_cause_lines"]), set(truth["alloc_lines"])
    seen = {k: {0} for k in KIND_KEYS.values()}
    totals = {0}
    for run in valgrind_runs(recorded):
        summary = run["outcome"]["leak_summary"]
        for k in seen:
            seen[k].add(summary[k]["bytes"])
        totals.add(summary["definitely_lost"]["bytes"] + summary["indirectly_lost"]["bytes"])
    wrong_numbers = []
    for m in COMBINED.finditer(report):
        if int(m.group(1).replace(",", "")) not in totals:
            wrong_numbers.append(f"{m.group(1)} bytes definitely+indirectly lost (tool said {sorted(totals - {0}) or [0]})")
    report = COMBINED.sub(" ", report)
    claims = BYTES_KIND.findall(report) + [(n, k) for k, n in KIND_BYTES.findall(report)]
    for number, kind in claims:
        key = KIND_KEYS[kind[:5].lower()]
        if int(number.replace(",", "")) not in seen[key]:
            wrong_numbers.append(f"{number} bytes {kind} lost (tool said {sorted(seen[key] - {0}) or [0]})")
    return {
        "root_cause_found": bool(named & bug) if bug else None,
        "named_alloc_line_only": bool(named & alloc) and not (named & bug) if bug - alloc else None,
        "lines_named": sorted(named),
        "overclaims": sorted({m.group(0) for m in OVERCLAIMS.finditer(report)}),
        "wrong_numbers": wrong_numbers,
        "invented_valgrind_lines": bool(INVENTED.search(raw_report)),
    }


def evaluate_analysis(name, truth):
    tools.WORKSPACE = TESTCASES                 # a repair session may have switched it to a work copy
    log = io.StringIO()
    with Recorder(leak_agent, execute_takes_map=False) as rec, contextlib.redirect_stdout(log):
        start = time.time()
        leak_agent.run_agent(f"Find and explain any memory leaks in {name}.")
    return {
        "agent": "analysis", "program": name, "seconds": round(time.time() - start),
        "finished": rec.report is not None, "api_error": rec.api_error,
        "model_calls": rec.calls, "prompt_tokens": rec.prompt_tokens,
        "report": rec.report, "trace": stored(rec.tools), "log": log.getvalue(),
    }


def stored(recorded):
    """The raw tool calls and results, saved so a scoring bug can be fixed by rescoring, without rerunning the model."""
    out = []
    for t in recorded:
        outcome = dict(t["outcome"])
        if "content" in outcome:
            outcome["content"] = f"<{len(outcome['content'])} characters>"
        out.append({"tool": t["tool"], "args": t["args"], "outcome": outcome})
    return out


def score(result, truth):
    """Add every metric that can be computed from the saved report, trace and diff."""
    trace = result["trace"]
    result.update(score_report(result["report"], trace, truth))
    if result["agent"] == "analysis":
        runs = valgrind_runs(trace)
        result["args_tried"] = [r["args"].get("args", []) for r in runs]
        result["observed"] = any(observed(r) for r in runs)
    else:
        result["quality_flags"], result["diff_lines"] = quality_flags(result["diff"])
        result["apply_fix_on_clean"] = truth["problem"] == "none" and result["attempts"] > 0
        result["fix_attempts"] = [{"explanation": t["args"].get("explanation"), "edits": t["args"].get("edits"),
                                   "verdict": t["outcome"].get("status") or t["outcome"].get("reason") or t["outcome"].get("error")}
                                  for t in trace if t["tool"] == "apply_fix"]
    return result


def quality_flags(diff):
    added = [l[1:] for l in diff.splitlines() if l.startswith("+") and not l.startswith("+++")]
    removed = [l[1:] for l in diff.splitlines() if l.startswith("-") and not l.startswith("---")]
    header = re.compile(r"^[\w:<>*&\s]+\b(\w+)\s*\([^;=]*\)\s*\{?\s*$")
    flags = []
    if any(re.search(r"\btry\b|\bcatch\b", l) for l in added):
        flags.append("try/catch added")
    changed = {header.match(l).group(1) for l in removed if header.match(l) and not re.match(r"\s*(if|for|while|switch)\b", l)}
    if changed:
        flags.append("signature changed: " + ", ".join(sorted(changed)))
    if any(re.search(r"\bdelete\b|\bfree\s*\(", l) for l in added):
        flags.append("manual release added")
    return flags, len(added) + len(removed)


def evaluate_repair(name, truth):
    log = io.StringIO()
    with Recorder(repair_agent, execute_takes_map=True) as rec, contextlib.redirect_stdout(log):
        start = time.time()
        session = repair_agent.run_repair(name, review=False)   # unattended, and its output is captured
    return {
        "agent": "repair", "program": name, "seconds": round(time.time() - start),
        "finished": rec.report is not None, "api_error": rec.api_error,
        "model_calls": rec.calls, "prompt_tokens": rec.prompt_tokens,
        "status": session.final_status().split(":")[0], "attempts": session.attempts,
        "verdicts": [v.get("status") or v.get("reason") for v in session.log],
        "report": rec.report, "diff": session.diff(), "trace": stored(rec.tools), "log": log.getvalue(),
    }


def count(rows, test):
    return f"{sum(1 for r in rows if test(r))}/{len(rows)}"


def summarise(results):
    analysis = [r for r in results if r["agent"] == "analysis" and "crashed" not in r]
    repairs = [r for r in results if r["agent"] == "repair" and "crashed" not in r]
    names = sorted({r["program"] for r in results})
    if analysis:
        print(f"\nANALYSIS AGENT ({len(analysis)} runs)")
        print(f"{'program':26} {'observed':>9} {'root cause':>11} {'alloc only':>11} {'overclaim':>10} {'wrong nums':>11} {'invented':>9} {'tokens':>8}")
        for n in names:
            rows = [r for r in analysis if r["program"] == n]
            if not rows:
                continue
            clean = EXPECTED[n]["problem"] == "none"
            print(f"{n:26} {count(rows, lambda r: r['observed']):>9} "
                  f"{'-' if clean else count(rows, lambda r: r['root_cause_found']):>11} "
                  f"{count([r for r in rows if r['named_alloc_line_only'] is not None], lambda r: r['named_alloc_line_only']) if not clean else '-':>11} "
                  f"{count(rows, lambda r: r['overclaims']):>10} {count(rows, lambda r: r['wrong_numbers']):>11} "
                  f"{count(rows, lambda r: r['invented_valgrind_lines']):>9} {sum(r['prompt_tokens'] for r in rows) // len(rows):>8}")
    if repairs:
        print(f"\nREPAIR AGENT ({len(repairs)} runs)")
        print(f"{'program':26} {'fixed':>6} {'improved':>9} {'not fixed':>10} {'nothing':>8} {'attempts':>9} {'overclaim':>10} {'wrong nums':>11} {'invented':>9}  flags")
        for n in names:
            rows = [r for r in repairs if r["program"] == n]
            if not rows:
                continue
            flags = sorted({f for r in rows for f in r["quality_flags"]})
            print(f"{n:26} {count(rows, lambda r: r['status'] == 'fixed'):>6} {count(rows, lambda r: r['status'] == 'improved'):>9} "
                  f"{count(rows, lambda r: r['status'] == 'not fixed'):>10} {count(rows, lambda r: r['status'] == 'nothing to fix'):>8} "
                  f"{sum(r['attempts'] for r in rows) / len(rows):>9.1f} {count(rows, lambda r: r['overclaims']):>10} "
                  f"{count(rows, lambda r: r['wrong_numbers']):>11} {count(rows, lambda r: r['invented_valgrind_lines']):>9}  {'; '.join(flags)}")
    crashed = [r for r in results if "crashed" in r]
    if crashed:
        print(f"\nHARNESS CRASHES: {len(crashed)} (see 'crashed' in the JSON)")
    infra = [r for r in results if r["api_error"]]
    print(f"\nAPI failures (not counted as model failures): {len(infra)}; unfinished runs: {count(results, lambda r: not r['finished'])}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--only", default="")
    parser.add_argument("--skip-analysis", action="store_true")
    parser.add_argument("--skip-repair", action="store_true")
    parser.add_argument("--rescore", metavar="RESULTS_JSON", help="recompute every metric from a saved results file; no model calls")
    opts = parser.parse_args()
    if opts.rescore:
        saved = json.loads(Path(opts.rescore).read_text())["results"]
        results = [r for r in saved if r["program"] in EXPECTED]   # skips programs removed from the set since
        summarise([r if "crashed" in r else score(r, EXPECTED[r["program"]]) for r in results])
        return
    names = opts.only.split(",") if opts.only else sorted(EXPECTED)
    RESULTS_DIR.mkdir(exist_ok=True)
    out = RESULTS_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}.json"
    jobs = [(name, i, agent, run) for name in names for i in range(1, opts.runs + 1)
            for agent, run in (("analysis", evaluate_analysis), ("repair", evaluate_repair))
            if not (agent == "analysis" and opts.skip_analysis) and not (agent == "repair" and opts.skip_repair)]
    print(f"{len(jobs)} agent runs, roughly {len(jobs) * 5} model calls "
          f"(the free tier allows {FREE_TIER_PER_DAY} requests per day per model)")
    results, failures_in_a_row = [], 0
    for name, i, agent, run in jobs:
        try:
            result = score(run(name, EXPECTED[name]), EXPECTED[name])
        except Exception as e:          # one broken run must not lose the others
            result = {"agent": agent, "program": name, "crashed": f"{type(e).__name__}: {e}",
                      "finished": False, "api_error": None, "model_calls": 0, "seconds": 0,
                      "observed": False, "status": "crashed", "prompt_tokens": 0}
        result["run"] = i
        results.append(result)
        brief = result.get("status") or ("observed" if result["observed"] else "not observed")
        print(f"[{name} #{i} {agent}] {brief}, {result['model_calls']} calls, {result['seconds']}s"
              + (f", API error {result['api_error'][:60]}" if result["api_error"] else ""), flush=True)
        out.write_text(json.dumps({"model": leak_agent.MODEL, "results": results}, indent=1))
        failures_in_a_row = failures_in_a_row + 1 if result["api_error"] else 0
        if failures_in_a_row >= 3 or "PerDay" in (result["api_error"] or ""):
            print("[stopped] the API keeps failing (a daily quota or an outage); every later run would fail too")
            break
    summarise(results)
    print(f"\nall runs, reports and diffs: {out}")


if __name__ == "__main__":
    main()
