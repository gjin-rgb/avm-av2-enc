"""The command line, exercised for real against a temporary workspace."""

import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from av2ra.cli import main

CONFIG_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config"
)


def run(*argv, workspace):
  buffer = io.StringIO()
  with redirect_stdout(buffer), redirect_stderr(buffer):
    code = main(["--site", "sim", "--workspace", workspace, *argv])
  return (code, buffer.getvalue())


class CliTest(unittest.TestCase):

  def setUp(self):
    self.workspace = os.path.join(tempfile.mkdtemp(), "ws")
    os.environ["AV2RA_SITE"] = "sim"

  def test_init_creates_a_usable_workspace(self):
    code, output = run("init", workspace=self.workspace)
    self.assertEqual(code, 0)
    self.assertIn("workspace ready", output)
    for name in ("experiments", "worktrees", "buildcache", "reports"):
      self.assertTrue(os.path.isdir(os.path.join(self.workspace, name)), name)

  def test_doctor_reports_attention_rather_than_pretending(self):
    run("init", workspace=self.workspace)
    code, output = run("doctor", "--offline", workspace=self.workspace)
    self.assertIn("AV2 RESEARCH AGENT HEALTH", output)
    self.assertIn(code, (0, 2))

  def test_rules_are_listed_with_their_enforcement(self):
    code, output = run("rules", "--stage", "measurement", workspace=self.workspace)
    self.assertEqual(code, 0)
    self.assertIn("enforced by av2ra.measure.acceptance", output)

  def test_leaderboard_and_frontier_run_on_an_empty_registry(self):
    run("init", workspace=self.workspace)
    for command in ("leaderboard", "frontier", "status"):
      code, output = run(command, workspace=self.workspace)
      self.assertEqual(code, 0, command)
      self.assertTrue(output.strip(), command)

  def test_dashboard_writes_a_self_contained_page(self):
    run("init", workspace=self.workspace)
    out = os.path.join(self.workspace, "dash.html")
    code, _ = run("dashboard", "--out", out, workspace=self.workspace)
    self.assertEqual(code, 0)
    with open(out, encoding="utf-8") as handle:
      html = handle.read()
    self.assertIn("<style>", html)
    self.assertNotIn("http://", html)
    self.assertNotIn("https://", html)

  def test_registry_export_and_conflict_audit(self):
    run("init", workspace=self.workspace)
    code, output = run("registry", "export", workspace=self.workspace)
    self.assertEqual(code, 0)
    self.assertTrue(os.path.exists(output.strip()))
    code, output = run("registry", "conflicts", workspace=self.workspace)
    self.assertEqual(code, 0)
    self.assertIn("no measurement conflicts", output)


if __name__ == "__main__":
  unittest.main()
