"""Tests for site/build.py (TASK-004-015): inlining dashboard.css,
dashboard.js, and metrics.json into a single self-contained dist/index.html.

Covers test-plan.md's E2E-003, E2E-004, E2E-009, E2E-012. Per GAP-2's
resolution (see handoff-dev-performance-dashboard.md): dist/index.html is a
fully self-contained single file -- only the pre-existing Chart.js CDN
<script> tag remains external; dashboard.css, dashboard.js, and the
self-hosted Inter/JetBrains Mono fonts (base64-inlined into the CSS) do
not.
"""
from __future__ import annotations

import json
import re

import pytest


def test_e2e_012_dist_index_html_created_nonzero_and_well_formed(
    build_module, site_dir, metrics_fixture, tmp_path
):
    out = tmp_path / "dist" / "index.html"
    html = build_module.build(site_dir, metrics_fixture.path, out)

    assert out.is_file()
    assert out.stat().st_size > 0
    assert html.startswith("<!DOCTYPE html>")
    assert "<head>" in html
    assert "<body" in html
    assert html.rstrip().endswith("</html>")


def test_e2e_004_dist_is_fully_self_contained(build_module, site_dir, metrics_fixture, tmp_path):
    out = tmp_path / "dist" / "index.html"
    html = build_module.build(site_dir, metrics_fixture.path, out)

    assert "href=\"dashboard.css\"" not in html
    assert "src=\"dashboard.js\"" not in html
    assert "<style>" in html
    external_scripts = re.findall(r'<script[^>]*\ssrc="([^"]+)"', html)
    assert external_scripts == [
        "https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js"
    ]


def test_e2e_009_build_twice_produces_byte_identical_output(
    build_module, site_dir, metrics_fixture, tmp_path
):
    out1 = tmp_path / "run1" / "index.html"
    out2 = tmp_path / "run2" / "index.html"

    html1 = build_module.build(site_dir, metrics_fixture.path, out1)
    html2 = build_module.build(site_dir, metrics_fixture.path, out2)

    assert html1 == html2
    assert out1.read_bytes() == out2.read_bytes()


def test_e2e_003_metrics_data_script_matches_input_metrics_json(
    build_module, site_dir, metrics_fixture, tmp_path
):
    out = tmp_path / "dist" / "index.html"
    html = build_module.build(site_dir, metrics_fixture.path, out)

    match = re.search(
        r'<script id="metrics-data" type="application/json">\s*(.*?)\s*</script>',
        html, re.DOTALL,
    )
    assert match is not None
    assert json.loads(match.group(1)) == metrics_fixture.data


def test_build_missing_metrics_json_raises_clear_error(build_module, site_dir, tmp_path):
    missing = tmp_path / "does-not-exist.json"
    out = tmp_path / "dist" / "index.html"

    with pytest.raises(build_module.BuildError, match="run the collector first"):
        build_module.build(site_dir, missing, out)
    assert not out.exists()


def test_escapes_closing_script_sequence_in_embedded_json(build_module, site_dir, tmp_path):
    """A free-text field (e.g. a developer display name) containing a
    literal "</script>" must not be able to prematurely close the
    #metrics-data <script> tag and break the page -- build.py escapes it.
    """
    data = {
        "generated_at": "2026-07-19T00:00:00Z",
        "roster": [{"name": "</script><script>alert(1)</script>", "handle": "x"}],
    }
    metrics_path = tmp_path / "metrics.json"
    metrics_path.write_text(json.dumps(data), encoding="utf-8")
    out = tmp_path / "dist" / "index.html"

    html = build_module.build(site_dir, metrics_path, out)

    match = re.search(
        r'<script id="metrics-data" type="application/json">(.*?)</script>',
        html, re.DOTALL,
    )
    assert match is not None
    restored = json.loads(match.group(1).replace("<\\/script", "</script"))
    assert restored["roster"][0]["name"] == "</script><script>alert(1)</script>"


def test_real_metrics_json_inlines_exactly_when_present(
    build_module, site_dir, real_metrics_path, tmp_path
):
    """When data/metrics.json exists (the collector has been run for
    real), build.py must inline it byte-for-byte-equivalent (same parsed
    value) into dist/index.html -- not a fixture, not a stale copy.
    """
    out = tmp_path / "dist" / "index.html"
    html = build_module.build(site_dir, real_metrics_path, out)

    match = re.search(
        r'<script id="metrics-data" type="application/json">\s*(.*?)\s*</script>',
        html, re.DOTALL,
    )
    assert match is not None
    embedded = json.loads(match.group(1))
    real_data = json.loads(real_metrics_path.read_text(encoding="utf-8"))
    assert embedded == real_data
    assert len(embedded["roster"]) == 8
    assert len(embedded["repos"]) == 8
    assert embedded["team"]["dora"]["mttr"] is None
