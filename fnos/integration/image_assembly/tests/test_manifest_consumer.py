"""Focused compatibility regression for the real r3 v2 manifest.

This test intentionally consumes the actual producer manifest with the real
lab loader.  It is not an acceptance test: accepted must remain false.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from fnos.lab.candidate import load_candidate


class ActualR3ManifestConsumerTest(unittest.TestCase):
    def test_actual_r3_v2_manifest_loads_and_stays_unaccepted(self) -> None:
        root = Path(__file__).resolve().parents[4]
        manifest_path = root / "fnos/logs/fnos-x5-candidate-r3-20261106/00-candidate.manifest.json"
        manifest = load_candidate(manifest_path)
        self.assertEqual(manifest.schema, "fnos-x5-assembly/v2")
        self.assertEqual(manifest.kernel_release, "6.18.0")
        self.assertEqual(manifest.root_uuid, "c2d3e4f5-a6b7-4c8d-9e0f-1a2b3c4d5e6f")
        self.assertEqual(manifest.fnos_version, "1.2.0701")
        self.assertEqual(
            manifest.image_sha256,
            "653ffe7a5e20b019f0a5192799c5ddbda3752ffe5d8f6d67ba87e1afc498f599",
        )
        self.assertEqual(manifest.image_size_bytes, 13157531648)
        self.assertFalse(manifest.accepted)


if __name__ == "__main__":
    unittest.main()
