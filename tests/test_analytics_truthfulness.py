"""Fast contract tests for the recording-derived analytics view."""

from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]


class AnalyticsTruthfulnessTests(unittest.TestCase):
    def test_page_labels_recorded_signals_and_model_boundaries(self) -> None:
        page = (ROOT / "cloud" / "static" / "index.html").read_text()

        self.assertIn(
            "Real: sampled video motion, brightness, stability, and audio spectrum; "
            "battery, ergonomics, and object labels are not measured here.",
            page,
        )
        self.assertIn("Observed visual conditions", page)
        self.assertIn("semantic object detection was not run", page)
        self.assertIn("Not included:</b> battery telemetry, ergonomics, or object labels such as stop signs.", page)

    def test_removed_planning_models_cannot_return_without_a_clear_label(self) -> None:
        page = (ROOT / "cloud" / "static" / "index.html").read_text()

        for legacy_control_or_chart in (
            "startCharge",
            "mountDistance",
            "targetSession",
            "Operating-state Pareto frontier",
            "Capture scenarios",
            "Battery estimate",
        ):
            self.assertNotIn(legacy_control_or_chart, page)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
