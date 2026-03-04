"""Tests for BLIS router evaluator — pure-Python logic only (no Go/BLIS required)."""

import json
import textwrap

import pytest


def test_extract_go_code_from_python_wrapper():
    """extract_go_code finds GO_ROUTING_CODE triple-quoted string."""
    from benchmarks.blis_router.evaluator import extract_go_code

    program_text = textwrap.dedent('''\
        GO_ROUTING_CODE = """package sim

        import "fmt"

        func main() {}
        """
    ''')
    result = extract_go_code(program_text)
    assert result.startswith("package sim")
    assert 'import "fmt"' in result


def test_extract_go_code_raw_go():
    """extract_go_code accepts raw Go code (no Python wrapper)."""
    from benchmarks.blis_router.evaluator import extract_go_code

    raw_go = "package sim\n\nfunc Route() {}\n"
    result = extract_go_code(raw_go)
    assert result == raw_go


def test_extract_go_code_empty():
    """extract_go_code returns empty string on garbage input."""
    from benchmarks.blis_router.evaluator import extract_go_code

    assert extract_go_code("no go code here") == ""
    assert extract_go_code("") == ""


def test_parse_cluster_metrics_valid():
    """_parse_cluster_metrics finds the cluster JSON block."""
    from benchmarks.blis_router.evaluator import _parse_cluster_metrics

    output = textwrap.dedent("""\
        === Simulation Metrics ===
        {"instance_id": "inst-0", "e2e_mean_ms": 100.0}
        {"instance_id": "cluster", "e2e_mean_ms": 4523.17, "e2e_p95_ms": 8234.56, "completed_requests": 1189}
    """)
    result = _parse_cluster_metrics(output)
    assert result is not None
    assert result["instance_id"] == "cluster"
    assert result["e2e_mean_ms"] == 4523.17
    assert result["e2e_p95_ms"] == 8234.56


def test_parse_cluster_metrics_missing():
    """_parse_cluster_metrics returns None when no cluster block exists."""
    from benchmarks.blis_router.evaluator import _parse_cluster_metrics

    assert _parse_cluster_metrics("no json here") is None
    assert _parse_cluster_metrics('{"instance_id": "inst-0"}') is None


def test_extract_evolve_block():
    """extract_evolve_block pulls content between markers."""
    from benchmarks.blis_router.evaluator import extract_evolve_block

    code = "before\n// EVOLVE-BLOCK-START\nchanged code\n// EVOLVE-BLOCK-END\nafter"
    assert extract_evolve_block(code) == "changed code"


def test_extract_evolve_block_missing():
    """extract_evolve_block returns empty string if no markers."""
    from benchmarks.blis_router.evaluator import extract_evolve_block

    assert extract_evolve_block("no markers here") == ""
