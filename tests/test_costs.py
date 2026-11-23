import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agw import cli as run

CATALOG = {
    "metadata": {"generatedAt": "2026-11-20T08:24:57Z"},
    "providers": {
        "zeta": {"models": {"m": {"rates": {"input": "1", "output": "2"}}}},
        "anthropic": {
            "models": {
                "claude-b": {
                    "name": "Claude B",
                    "limits": {"context": 1000000},
                    "rates": {
                        "input": "5",
                        "output": "25",
                        "cacheWrite": "6.25",
                        "cacheWriteDetails": {"1h": "10"},
                    },
                    "tiers": [
                        {"serviceTier": "fast", "rates": {"input": "10"}},
                        {
                            "contextOver": 200000,
                            "rates": {"input": "10", "cacheWriteDetails": {"1h": "20"}},
                        },
                    ],
                },
                "claude-a": {
                    "rates": {"input": "1", "output": "5", "perAudioMinute": "0.1"},
                    "tiers": [{"serviceTier": "flex", "rates": {"input": "0.5"}}],
                },
                "no-rates": {"rates": {}},
            }
        },
    },
}


class ProjectionTests(unittest.TestCase):
    def test_keeps_rates_and_context_tiers_only(self):
        self.assertEqual(
            run.catalog_to_base_costs(CATALOG),
            {
                "providers": {
                    "anthropic": {
                        "models": {
                            "claude-a": {"rates": {"input": "1", "output": "5"}},
                            "claude-b": {
                                "rates": {"input": "5", "output": "25", "cacheWrite": "6.25"},
                                "tiers": [{"contextOver": 200000, "rates": {"input": "10"}}],
                            },
                        }
                    },
                    "zeta": {"models": {"m": {"rates": {"input": "1", "output": "2"}}}},
                }
            },
        )

    def test_output_is_sorted(self):
        out = run.catalog_to_base_costs(CATALOG)["providers"]
        self.assertEqual(list(out), ["anthropic", "zeta"])
        self.assertEqual(list(out["anthropic"]["models"]), ["claude-a", "claude-b"])


class CmdCostsTests(unittest.TestCase):
    def test_download_writes_catalog_and_base_costs(self):
        raw = json.dumps(CATALOG).encode()
        with tempfile.TemporaryDirectory() as d, mock.patch.object(
            run, "download_catalog", return_value=(raw, CATALOG)
        ), mock.patch("builtins.print"):
            run.cmd_costs(config_dir=d)
            self.assertEqual((Path(d) / run.CATALOG_FILE).read_bytes(), raw)
            self.assertEqual(
                json.loads((Path(d) / run.BASE_COSTS_FILE).read_text()),
                run.catalog_to_base_costs(CATALOG),
            )
            self.assertEqual(sorted(p.name for p in Path(d).iterdir()),
                             sorted([run.CATALOG_FILE, run.BASE_COSTS_FILE]))

    def test_empty_catalog_leaves_existing_file_alone(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "catalog.json"
            src.write_text('{"providers": {}}')
            out = Path(d) / run.BASE_COSTS_FILE
            out.write_text("keep")
            with self.assertRaises(SystemExit), mock.patch("sys.stderr"):
                run.cmd_costs(config_dir=d, source=src)
            self.assertEqual(out.read_text(), "keep")


if __name__ == "__main__":
    unittest.main()
