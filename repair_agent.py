"""The repair agent: the analysis tools plus apply_fix. The program, not the model, decides the result.

Usage:  python repair_agent.py 07_partners.cpp
"""
import sys
import time

from google import genai
from google.genai import errors, types

import tools
from leak_agent import DECLARATIONS as ANALYSIS_DECLARATIONS
from repair import MAX_ATTEMPTS, RepairSession

client = genai.Client()                  # reads GEMINI_API_KEY from the environment
MODEL = "gemini-3.1-flash-lite"
MAX_STEPS = 14

SYSTEM_PROMPT = f"""You are a C++ memory-leak repair assistant. You can read the source file, compile it,
run it under Valgrind Memcheck, and propose fixes with apply_fix.

Process:
1. Analyse: read the file, compile it, run it under Valgrind. If its behaviour depends on
   command-line arguments, also run it with arguments that reach the suspicious path.
2. If Valgrind observed a leak or another memory error, propose ONE fix with apply_fix.
   A fix may contain several edits.
3. The program, not you, verifies every fix: it recompiles, runs the program under Valgrind on
   predefined inputs, compares the output with the original, and keeps or reverts the fix.
   Read its verdict.
4. If the fix was rejected, read the reason and try a different fix. If it was accepted with status
   "improved", fix what remains. Stop when the status is "fixed", when no attempts are left
   (you have {MAX_ATTEMPTS}), or when Valgrind observed nothing to fix.
5. Write the final report.

Repair rules:
- Make the smallest change that removes the leak. Inside a function, prefer RAII: std::vector,
  std::string, std::unique_ptr, or a plain value on the stack. Break shared_ptr cycles with std::weak_ptr.
- Never change what the program prints or returns. Never remove functionality, function calls or
  code paths to avoid an allocation.
- Keep function signatures unless the leak cannot be fixed otherwise.
- old_text must be copied exactly from the current file, without the line-number prefixes,
  and must appear exactly once. Add any #include the fix needs.
- If Valgrind observed no leak and no memory error, do not call apply_fix.

Report rules:
- Call a problem "observed" only if Valgrind reported it. Never claim the code is leak-free.
- Do not declare the program fixed yourself; quote the program's verdicts.

Final report format:
Observed before the fix: what Valgrind reported, per run, with arguments.
Root cause: file:line and why.
Fix: what you changed and why (or why no fix was attempted).
Verification: the program's verdict for each attempt.
Not covered: code paths that the predefined inputs and your runs did not execute."""

apply_fix_declaration = {
    "name": "apply_fix",
    "description": "Propose a fix as one or more exact text replacements in the source file. The program applies "
                   "it, recompiles, runs Valgrind on predefined inputs and compares the output with the original. "
                   "It keeps the fix only if the behaviour is unchanged, no new memory error appears, and lost bytes "
                   "or memory errors decreased; otherwise it reverts the file. Returns the verdict.",
    "parameters": {
        "type": "object",
        "properties": {
            "source_file": {"type": "string", "description": "The file being repaired"},
            "edits": {
                "type": "array",
                "description": "Replacements applied in order",
                "items": {
                    "type": "object",
                    "properties": {
                        "old_text": {"type": "string", "description": "Exact text from the current file, "
                                                                      "without line numbers; must appear exactly once"},
                        "new_text": {"type": "string", "description": "Replacement text"},
                    },
                    "required": ["old_text", "new_text"],
                },
            },
            "explanation": {"type": "string", "description": "One sentence: what the fix changes and why"},
        },
        "required": ["source_file", "edits", "explanation"],
    },
}

# Built once and reused on every step: the model must see the same tools every time.
config = types.GenerateContentConfig(
    system_instruction=SYSTEM_PROMPT,
    tools=[types.Tool(function_declarations=ANALYSIS_DECLARATIONS + [apply_fix_declaration])],
)


def execute(tool_map, name, args):
    """Run a requested tool. Always returns a dict; failures become {"error": ...} for the model."""
    if name not in tool_map:
        return {"error": f"unknown tool: {name}"}
    try:
        return tool_map[name](**(args or {}))
    except TypeError as e:               # missing or unexpected argument names
        return {"error": f"bad arguments for {name}: {e}"}
    except Exception as e:
        print(f"[program] {name} raised {type(e).__name__}: {e}")
        return {"error": f"{type(e).__name__}: {e}"}


def call_model(history, retries=3):
    """Retry overload (503) and rate-limit (429) errors with growing delays; anything else stops the run."""
    for attempt in range(retries + 1):
        try:
            return client.models.generate_content(model=MODEL, contents=history, config=config)
        except errors.APIError as e:
            if e.code not in (429, 503) or attempt == retries:
                raise
            delay = 5 * 2 ** attempt
            print(f"[retry] {e.code} from the API, waiting {delay}s")
            time.sleep(delay)


def summarise(name, outcome):
    """One log line per tool result, instead of printing whole files, reports and verdicts."""
    keys = ("error", "success", "accepted", "status", "reason", "lost_bytes", "memory_errors", "attempts_left")
    brief = {k: v for k, v in outcome.items() if k in keys}
    if name == "run_valgrind" and "leak_summary" in outcome:
        brief["lost"] = {k: v["bytes"] for k, v in outcome["leak_summary"].items() if v["bytes"]}
    return brief or "file content"


def run_repair(source_name):
    """Run the repair loop for one file. Returns the session; its final_status() is the result."""
    session = RepairSession(source_name)          # copies the file and measures the baseline
    tool_map = {
        "read_file": tools.read_file,
        "compile_code": tools.compile_code,
        "run_valgrind": tools.run_valgrind,
        "apply_fix": session.apply_fix,           # the only tool that can change a file
    }
    history = [types.Content(role="user", parts=[types.Part(text=f"Find and fix the memory leak in {source_name}.")])]
    report = None
    for step in range(1, MAX_STEPS + 1):
        try:
            response = call_model(history)
        except errors.APIError as e:
            print(f"[step {step}] API error: {e}")
            break
        history.append(response.candidates[0].content)      # the model's turn, unchanged

        if not response.function_calls:
            report = response.text
            break

        parts = []
        for fc in response.function_calls:
            args = dict(fc.args or {})
            shown = {k: v for k, v in args.items() if k != "edits"}
            if "edits" in args:
                shown["edits"] = f"{len(args['edits'])} edit(s)"
            print(f"[step {step}] model -> {fc.name}({shown})")
            outcome = execute(tool_map, fc.name, args)
            print(f"[step {step}] program -> {summarise(fc.name, outcome)}")
            parts.append(types.Part.from_function_response(name=fc.name, response=outcome))
        history.append(types.Content(role="user", parts=parts))
    else:
        print("[stopped] reached MAX_STEPS without a final report")

    print("\n===== MODEL REPORT =====\n" + (report or "(no report: the run stopped early)"))
    print("\n===== DECIDED BY THE PROGRAM =====")
    print(f"status:   {session.final_status()}")
    print(f"attempts: {session.attempts} of {MAX_ATTEMPTS}")
    print(f"result:   {session.path}")
    print(session.diff() or "(no change)")
    return session


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python repair_agent.py <file.cpp in testcases/>")
    run_repair(sys.argv[1])
