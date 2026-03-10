"""
Analyze diffs between initial and best programs for each framework.

Extracts Go routing code from initial and best programs, generates unified diffs,
and optionally calls an LLM to explain the changes.

Usage: python benchmarks/blis_router/scripts/analyze_diffs.py outputs/blis_router/<experiment>/
"""

import difflib
import os
import re
import sys
from pathlib import Path

import yaml


def extract_go_code(program_path: Path) -> str | None:
    """Extract Go code from a program file.

    Supports both native .go files and legacy .py files with GO_ROUTING_CODE wrapper.
    """
    text = program_path.read_text()

    # Native .go file — return the full content
    if program_path.suffix == ".go":
        return text

    # Legacy .py file — extract GO_ROUTING_CODE
    match = re.search(r'GO_ROUTING_CODE\s*=\s*"""(.*?)"""', text, re.DOTALL)
    if match:
        return match.group(1)
    return None


def generate_diff(initial_code: str, best_code: str, framework: str) -> str:
    """Generate unified diff between initial and best Go code."""
    initial_lines = initial_code.splitlines(keepends=True)
    best_lines = best_code.splitlines(keepends=True)
    diff = difflib.unified_diff(
        initial_lines,
        best_lines,
        fromfile="initial_program.go",
        tofile=f"{framework}/best/best_program.go",
    )
    return "".join(diff)


def load_api_config() -> tuple[str, str]:
    """Load api_base and model from config.yaml."""
    config_path = Path(__file__).parent.parent / "config.yaml"
    with open(config_path) as f:
        config = yaml.safe_load(f)
    api_base = config.get("llm", {}).get("api_base", "")
    model = "aws/claude-sonnet-4-5"
    return api_base, model


def explain_diffs_with_llm(diffs: dict[str, str]) -> str:
    """Call LLM to explain all diffs. Returns markdown explanation."""
    api_base, model = load_api_config()

    prompt_parts = [
        "Below are unified diffs showing how each search framework modified a Go "
        "routing algorithm (WeightedScoring.Route) for an LLM inference cluster. "
        "The EVOLVE-BLOCK markers delimit the mutable region.\n\n"
        "For each framework, explain the routing changes in 2-3 sentences. "
        "Focus on *what* strategy the algorithm discovered and *why* it might "
        "improve latency.\n"
    ]
    for fw, diff in sorted(diffs.items()):
        prompt_parts.append(f"\n## {fw}\n```diff\n{diff}\n```\n")

    prompt = "".join(prompt_parts)

    try:
        from openai import OpenAI

        client = OpenAI(
            base_url=api_base,
            api_key=os.environ.get("OPENAI_API_KEY", "dummy"),
        )
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=4000,
            temperature=0.3,
        )
        return response.choices[0].message.content
    except Exception as e:
        return f"*LLM explanation unavailable: {e}*\n\nReview the diffs manually."


def find_initial_program() -> Path:
    """Find the initial program file (.go or .py)."""
    base = Path(__file__).parent.parent
    go_path = base / "initial_program.go"
    py_path = base / "initial_program.py"
    if go_path.exists():
        return go_path
    if py_path.exists():
        return py_path
    raise FileNotFoundError(f"No initial_program found in {base}")


def find_best_program(fw_dir: Path) -> Path | None:
    """Find the best program file (.go or .py) in a framework's best/ dir."""
    best_dir = fw_dir / "best"
    if not best_dir.is_dir():
        return None
    for suffix in (".go", ".py"):
        path = best_dir / f"best_program{suffix}"
        if path.exists():
            return path
    return None


def main():
    if len(sys.argv) < 2:
        print("Usage: python analyze_diffs.py <results_dir>")
        sys.exit(1)

    results_dir = Path(sys.argv[1])
    if not results_dir.is_dir():
        print(f"Directory not found: {results_dir}")
        sys.exit(1)

    initial_program = find_initial_program()
    initial_code = extract_go_code(initial_program)
    if not initial_code:
        print(f"Could not extract Go code from {initial_program}")
        sys.exit(1)

    frameworks = sorted(
        p.name
        for p in results_dir.iterdir()
        if p.is_dir() and find_best_program(p) is not None
    )

    if not frameworks:
        print("No framework results with best programs found.")
        sys.exit(1)

    diffs: dict[str, str] = {}
    for fw in frameworks:
        best_program = find_best_program(results_dir / fw)
        best_code = extract_go_code(best_program)
        if not best_code:
            print(f"  {fw}: Could not extract Go code, skipping")
            continue

        diff = generate_diff(initial_code, best_code, fw)
        if not diff:
            print(f"  {fw}: No changes from initial program")
            diff = "(no changes)"

        # Save diff file
        diff_path = results_dir / fw / "best" / "best_vs_initial.diff"
        diff_path.write_text(diff)
        print(f"  {fw}: saved {diff_path}")
        diffs[fw] = diff

    if not diffs:
        print("No diffs generated.")
        sys.exit(1)

    # Generate LLM explanations
    print("\nGenerating LLM explanations...")
    explanations = explain_diffs_with_llm(diffs)

    # Write explanations markdown
    output_path = results_dir / "diff_explanations.md"
    with open(output_path, "w") as f:
        f.write("# Diff Analysis: Initial vs Best Programs\n\n")
        for fw, diff in sorted(diffs.items()):
            f.write(f"## {fw}\n\n")
            f.write(f"```diff\n{diff}\n```\n\n")
        f.write("---\n\n## LLM Explanation\n\n")
        f.write(explanations)
        f.write("\n")

    print(f"\nExplanations saved to {output_path}")


if __name__ == "__main__":
    main()
