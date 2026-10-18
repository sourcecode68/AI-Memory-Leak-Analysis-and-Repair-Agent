"""The analysis agent: reads a C++ file, compiles it, runs it under Valgrind and explains any leak. It changes nothing.

Usage:  python leak_agent.py 10_config_mode.cpp
"""
import sys
import time

from google import genai
from google.genai import errors, types

from tools import compile_code, read_file, run_valgrind

client = genai.Client()                  # reads GEMINI_API_KEY from the environment
MODEL = "gemini-3.1-flash-lite"
MAX_STEPS = 10

SYSTEM_PROMPT = """You are a C++ memory-leak analysis assistant. You can read source files, compile them,
and run them under Valgrind Memcheck using the tools provided. You cannot modify files.

Process:
1. Read the source file. Note every heap allocation and every path that could skip its release:
   early returns, exceptions, loops, overwritten pointers, input-dependent branches.
2. Compile it.
3. Run it under Valgrind. If the program's behaviour depends on its command-line arguments,
   run it again with arguments that exercise the suspicious path.
4. Write the final report.

Rules:
- A Valgrind stack trace shows where a block was ALLOCATED, not where the bug is.
  Use the source to find where the release is missing.
- Call a leak "observed" only if Valgrind reported it in one of your runs.
  Otherwise call it "possible" or "suspected".
- Never claim the code is leak-free or that leaks are impossible. If Valgrind reports nothing,
  say "no leak was observed during this execution" and name the arguments used.
- Never invent tool results.

Final report format:
Observed (Valgrind): what was reported, per run, with the arguments used.
Root cause: file:line of the missing release or lost pointer, and why.
Suspected but not observed: anything you suspect from the source that no run confirmed.
Not covered: code paths that none of your runs executed."""

read_file_declaration = {
    "name": "read_file",
    "description": "Read a C/C++ source file from the workspace. Returns its content with line numbers.",
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File name relative to the workspace, e.g. leak.cpp"},
        },
        "required": ["path"],
    },
}
compile_code_declaration = {
    "name": "compile_code",
    "description": "Compile a C++ source file with g++ -g -O0 -Wall -Wextra. "
                   "Returns the executable name, or the compiler errors.",
    "parameters": {
        "type": "object",
        "properties": {
            "source_file": {"type": "string", "description": "Source file relative to the workspace"},
        },
        "required": ["source_file"],
    },
}
run_valgrind_declaration = {
    "name": "run_valgrind",
    "description": "Run a compiled executable under Valgrind Memcheck (--leak-check=full). Returns the program's "
                   "output, the leak summary, and the loss records with source locations. "
                   "Only the paths executed by this run are checked.",
    "parameters": {
        "type": "object",
        "properties": {
            "executable": {"type": "string", "description": "Executable name returned by compile_code"},
            "args": {"type": "array", "items": {"type": "string"},
                     "description": "Optional command-line arguments for the program (at most 5)"},
        },
        "required": ["executable"],
    },
}
TOOLS = {
    "read_file": read_file,
    "compile_code": compile_code,
    "run_valgrind": run_valgrind,
}
DECLARATIONS = [read_file_declaration, compile_code_declaration, run_valgrind_declaration]

# Built once and reused on every step: the model must see the same tools every time.
config = types.GenerateContentConfig(
    system_instruction=SYSTEM_PROMPT,
    tools=[types.Tool(function_declarations=DECLARATIONS)],
)


def execute(name, args):
    """Run a requested tool. Always returns a dict; failures become {"error": ...} for the model."""
    if name not in TOOLS:
        return {"error": f"unknown tool: {name}"}
    try:
        return TOOLS[name](**(args or {}))
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


def summarise(outcome):
    """One log line per tool result, instead of printing whole files and reports."""
    keys = ("error", "success", "executable", "leak_summary", "error_count")
    brief = {k: v for k, v in outcome.items() if k in keys}
    return brief or "file content"


def run_agent(question):
    """Run the agent loop for one question. Returns the final report, or None if it stopped early."""
    history = [types.Content(role="user", parts=[types.Part(text=question)])]
    for step in range(1, MAX_STEPS + 1):
        try:
            response = call_model(history)
        except errors.APIError as e:
            print(f"[step {step}] API error: {e}")
            break
        history.append(response.candidates[0].content)      # the model's turn, unchanged
        print(f"[step {step}] prompt tokens: {response.usage_metadata.prompt_token_count}")

        if not response.function_calls:
            return response.text

        # Every call in this reply gets a result, all sent back in one user turn.
        parts = []
        for fc in response.function_calls:
            print(f"[step {step}] model -> {fc.name}({dict(fc.args or {})})")
            outcome = execute(fc.name, fc.args)
            print(f"[step {step}] program -> {summarise(outcome)}")
            parts.append(types.Part.from_function_response(name=fc.name, response=outcome))
        history.append(types.Content(role="user", parts=parts))
    else:
        print("[stopped] reached MAX_STEPS without a final report")
    return None


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python leak_agent.py <file.cpp in testcases/>")
    report = run_agent(f"Find and explain any memory leaks in {sys.argv[1]}.")
    print("\n===== REPORT =====\n" + (report or "(no report: the run stopped early)"))
