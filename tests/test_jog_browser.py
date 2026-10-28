"""Opt-in browser checks: TIME_VOLUME_TEST_BROWSER=1 python3 -m unittest discover -s tests."""

import html
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
# Bypass Debian's shell wrapper, which cleans the real user's crash reports.
BROWSER = (
    "/usr/lib/chromium/chromium"
    if Path("/usr/lib/chromium/chromium").is_file()
    else shutil.which("chromium") or shutil.which("chromium-browser")
)


@unittest.skipUnless(
    os.environ.get("TIME_VOLUME_TEST_BROWSER") == "1" and BROWSER,
    "set TIME_VOLUME_TEST_BROWSER=1 with Chromium installed to run isolated browser checks",
)
class JogBrowserTests(unittest.TestCase):
    def test_pointer_holds_and_network_races(self):
        page = (ROOT / "web" / "controller.html").read_text(encoding="utf-8")
        script = page.split("<script>", 1)[1].split("</script>", 1)[0]
        start = script.index("        const JOG_HEARTBEAT_MS")
        end = script.index('        targetRange.addEventListener("input"', start)
        apply_start = script.index("        async function applyState(state)")
        apply_end = script.index("        async function refreshStateLoop()", apply_start)
        harness = (ROOT / "tests" / "jog_browser_harness.js").read_text(encoding="utf-8")
        harness = harness.replace("// JOG_SOURCE", script[start:end])
        harness = harness.replace("// APPLY_STATE_SOURCE", script[apply_start:apply_end])
        # Check syntax of the entire controller script without executing the app.
        harness = "new Function(" + json.dumps(script) + ");\n" + harness
        harness = harness.replace("</script", "<\\/script")

        with tempfile.TemporaryDirectory(prefix="time-volume-jog-tests-") as temp:
            temp_root = Path(temp)
            test_page = temp_root / "test.html"
            test_page.write_text(
                '<!doctype html><html><head><meta charset="utf-8"></head>'
                "<body><script>" + harness + "</script></body></html>",
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    BROWSER,
                    "--headless",
                    "--no-sandbox",
                    "--disable-gpu",
                    "--disable-dev-shm-usage",
                    "--disable-background-networking",
                    "--disable-sync",
                    "--no-first-run",
                    f"--user-data-dir={temp_root / 'profile'}",
                    "--dump-dom",
                    "--virtual-time-budget=3000",
                    test_page.as_uri(),
                ],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        body = re.search(r"<body>(.*?)</body>", result.stdout, re.DOTALL)
        self.assertIsNotNone(body, result.stdout[-2000:])
        outcomes = json.loads(html.unescape(body.group(1)))
        self.assertEqual(len(outcomes), 16, outcomes)
        for outcome in outcomes:
            with self.subTest(scenario=outcome["name"]):
                self.assertTrue(outcome["passed"], outcome.get("error"))


if __name__ == "__main__":
    unittest.main()
