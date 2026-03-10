#!/usr/bin/env python3
"""Analyze algorithm diffs between initial and best programs.

Produces per-framework diffs and LLM-generated explanations.

Usage:
    python scripts/analyze_diffs.py <results_dir>
"""
import difflib
import json
import os
import re
import sys
from pathlib import Path

BENCHMARK_DIR = Path(__file__).resolve().parent.parent
INITIAL_PROGRAM = BENCHMARK_DIR / "initial_program.py"


def extract_evolve_block(code: str) -> str:
    m = re.search(r"# EVOLVE-BLOCK-START\n(.*?)# EVOLVE-BLOCK-END", code, re.DOTALL)
    return m.group(1) if m else code


def generate_diff(initial_code: str, best_code: str, framework: str) -> str:
    initial_block = extract_evolve_block(initial_code)
    best_block = extract_evolve_block(best_code)
    diff = difflib.unified_diff(
        initial_block.splitlines(keepends=True),
        best_block.splitlines(keepends=True),
        fromfile=f"initial_program.py",
        tofile=f"{framework}/best_program.py",
    )
    return "".join(diff)


def find_best_program(fw_dir: Path) -> Path:
    candidates = [
        fw_dir / "best" / "best_program.py",
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


def load_api_config() -> dict:
    config_path = BENCHMARK_DIR / "config.yaml"
    if not config_path.exists():
        return {}
    import yaml
    with open(config_path) as f:
        config = yaml.safe_load(f)
    return config.get("llm", {})


def explain_diffs_with_llm(diffs: dict) -> str:
    """Call LLM to explain algorithm diffs."""
    llm_config = load_api_config()
    api_base = llm_config.get("api_base")
    if not api_base:
        return "(LLM explanation skipped — no api_base in config.yaml)"

    try:
        import litellm
    except ImportError:
        return "(LLM explanation skipped — litellm not installed)"

    prompt_parts = ["Analyze these routing algorithm diffs. For each framework, explain in 2-3 sentences:\n"
                    "1. What strategy does the evolved algorithm use?\n"
                    "2. Why might it improve latency over the baseline (LOR)?\n\n"]

    for fw, diff_text in diffs.items():
        prompt_parts.append(f"## {fw}\n```diff\n{diff_text}\n```\n\n")

    try:
        response = litellm.completion(
            model="aws/claude-sonnet-4-5",
            messages=[{"role": "user", "content": "".join(prompt_parts)}],
            api_base=api_base,
            max_tokens=4000,
            temperature=0.3,
        )
        return response.choices[0].message.content
    except Exception as e:
        return f"(LLM explanation failed: {e})"


def main():
    if len(sys.argv) < 2:
        print("Usage: analyze_diffs.py <results_dir>")
        sys.exit(1)

    results_dir = Path(sys.argv[1])
    frameworks = sorted([
        d.name for d in results_dir.iterdir()
        if d.is_dir() and (d / "best").is_dir()
    ])

    if not INITIAL_PROGRAM.exists():
        print(f"Error: {INITIAL_PROGRAM} not found")
        sys.exit(1)

    initial_code = INITIAL_PROGRAM.read_text()
    diffs = {}

    print("Generating diffs...")
    for fw in frameworks:
        best_path = find_best_program(results_dir / fw)
        if best_path is None:
            print(f"  {fw}: no best program found, skipping")
            continue

        best_code = best_path.read_text()
        diff = generate_diff(initial_code, best_code, fw)

        if not diff.strip():
            print(f"  {fw}: no diff (identical to initial)")
            continue

        diffs[fw] = diff

        # Write per-framework diff
        diff_path = results_dir / fw / "best" / "best_vs_initial.diff"
        diff_path.write_text(diff)
        print(f"  {fw}: {diff_path}")

    if not diffs:
        print("No diffs to analyze")
        return

    # LLM explanation
    print("\nGenerating LLM explanations...")
    explanation = explain_diffs_with_llm(diffs)

    # Write markdown
    md_parts = ["# Algorithm Diff Analysis\n\n"]
    for fw, diff_text in diffs.items():
        md_parts.append(f"## {fw}\n\n```diff\n{diff_text}\n```\n\n")
    md_parts.append("## LLM Explanation\n\n")
    md_parts.append(explanation)
    md_parts.append("\n")

    md_path = results_dir / "diff_explanations.md"
    md_path.write_text("".join(md_parts))
    print(f"\nWrote {md_path}")
    print("Done!")


if __name__ == "__main__":
    main()
