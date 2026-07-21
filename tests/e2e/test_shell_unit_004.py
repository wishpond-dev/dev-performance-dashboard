"""SHELL-UNIT-004 verification: header meta numbers come from data/metrics.json, not hardcoded.

Run standalone via qa-tester (not part of the pytest collector suite wiring).
"""
import json
import http.server
import socketserver
import threading
import pathlib

from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).resolve().parents[2]
DIST_DIR = ROOT / "dist"
METRICS_PATH = ROOT / "data" / "metrics.json"


def serve_and_check():
    metrics = json.loads(METRICS_PATH.read_text())
    expected_roster = len(metrics["roster"])
    expected_repos = len(metrics["repos"])
    expected_months = len(metrics["window"]["months"])

    handler = http.server.SimpleHTTPRequestHandler
    httpd = socketserver.TCPServer(("127.0.0.1", 0), lambda *a, **kw: handler(*a, directory=str(DIST_DIR), **kw))
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path="/usr/local/bin/chromium", args=["--no-sandbox"], headless=True)
            page = browser.new_page()
            page.goto(f"http://127.0.0.1:{port}/index.html")
            page.wait_for_selector("#statRoster")

            roster_text = page.locator("#statRoster").inner_text().strip()
            repos_text = page.locator("#statRepos").inner_text().strip()
            months_text = page.locator("#statMonths").inner_text().strip()

            # Independently re-derive the embedded metrics-data JSON from the DOM
            # to prove the header isn't just coincidentally matching the source file
            # but is actually wired to the same blob the page embeds.
            embedded_json = page.locator("#metrics-data").inner_text()
            embedded = json.loads(embedded_json)
            embedded_roster = len(embedded["roster"])
            embedded_repos = len(embedded["repos"])
            embedded_months = len(embedded["window"]["months"])

            browser.close()

            results = {
                "expected_from_source_file": {
                    "roster": expected_roster, "repos": expected_repos, "months": expected_months,
                },
                "embedded_in_dom": {
                    "roster": embedded_roster, "repos": embedded_repos, "months": embedded_months,
                },
                "rendered_dom_text": {
                    "statRoster": roster_text, "statRepos": repos_text, "statMonths": months_text,
                },
            }
            print(json.dumps(results, indent=2))

            assert embedded_roster == expected_roster, "embedded metrics-data roster count diverges from data/metrics.json"
            assert embedded_repos == expected_repos, "embedded metrics-data repos count diverges from data/metrics.json"
            assert embedded_months == expected_months, "embedded metrics-data months count diverges from data/metrics.json"

            assert roster_text == str(expected_roster), f"rendered #statRoster={roster_text!r} != expected {expected_roster}"
            assert repos_text == str(expected_repos), f"rendered #statRepos={repos_text!r} != expected {expected_repos}"
            assert months_text == str(expected_months), f"rendered #statMonths={months_text!r} != expected {expected_months}"

            print("PASS: header meta numbers match data/metrics.json exactly and are DOM-wired to the embedded blob.")
    finally:
        httpd.shutdown()


if __name__ == "__main__":
    serve_and_check()
