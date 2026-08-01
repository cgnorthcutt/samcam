"""Shareable workspace-tab links must remain a tiny static contract."""

from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]


class WorkspaceDeepLinkTests(unittest.TestCase):
    def test_server_serves_archive_and_analytics_paths(self) -> None:
        relay = (ROOT / "cloud" / "main.py").read_text()
        self.assertIn('@app.get("/archive")', relay)
        self.assertIn('@app.get("/analytics")', relay)

    def test_client_uses_path_or_hash_to_choose_a_workspace_tab(self) -> None:
        page = (ROOT / "cloud" / "static" / "index.html").read_text()
        self.assertIn("function tabFromLocation()", page)
        self.assertIn("location.hash", page)
        self.assertIn("`/${tab}`", page)
        self.assertIn("setTab(tabFromLocation(),{updateUrl:false})", page)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
