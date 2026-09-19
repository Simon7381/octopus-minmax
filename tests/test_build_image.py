"""Keep local and release builds on the newest stable Noble image."""

import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import build_image


class BuildImageTests(unittest.TestCase):
    def test_resolver_uses_numeric_versions_and_only_stable_noble_tags(self):
        self.assertEqual(build_image.latest_noble_version([
            "latest", "v1.9.9-noble", "v1.63.0-noble", "v1.63.1-noble",
            "v1.64.0-beta-noble", "v2.0.0-jammy", "v2.0.0-resolute",
        ]), "1.63.1")

    def test_missing_stable_noble_image_fails_instead_of_using_another_os(self):
        with self.assertRaisesRegex(ValueError, "no stable"):
            build_image.latest_noble_version(["latest", "v1.63.0-jammy"])

    def test_registry_json_is_resolved(self):
        with patch("build_image.urllib.request.urlopen", return_value=io.BytesIO(
            b'{"tags":["v1.63.0-noble","v1.62.0-noble"]}'
        )) as request:
            self.assertEqual(build_image.resolve_version(), "1.63.0")
        request.assert_called_once_with(build_image.TAGS_URL, timeout=30)

    def test_local_build_refreshes_base_and_dependencies_with_resolved_version(self):
        for engine, pull_flag in (("docker", "--pull"), ("podman", "--pull=always")):
            with self.subTest(engine=engine), \
                    patch.object(sys, "argv", ["build_image.py", "--engine", engine, "--tag", "local:test"]), \
                    patch("build_image.resolve_version", return_value="1.63.1"), \
                    patch("build_image.subprocess.run") as run:
                build_image.main()
                run.assert_called_once_with([
                    engine, "build", pull_flag, "--no-cache", "--build-arg",
                    "PLAYWRIGHT_VERSION=1.63.1", "-f", "dockerfile", "-t", "local:test", ".",
                ], cwd=build_image.ROOT, check=True)

    def test_resolution_failure_does_not_build_stale_image(self):
        with patch.object(sys, "argv", ["build_image.py"]), \
                patch("build_image.resolve_version", side_effect=OSError("Registry unavailable")), \
                patch("build_image.subprocess.run") as run:
            with self.assertRaises(OSError):
                build_image.main()
            run.assert_not_called()

    def test_github_resolution_writes_output_without_building(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            with patch.object(sys, "argv", ["build_image.py", "--resolve-only", "--github-output"]), \
                    patch.dict(os.environ, {"GITHUB_OUTPUT": str(output)}), \
                    patch("build_image.resolve_version", return_value="1.63.1"), \
                    patch("build_image.subprocess.run") as run:
                build_image.main()
                run.assert_not_called()
            self.assertEqual(output.read_text(),
                "version=1.63.1\nimage=mcr.microsoft.com/playwright/python:v1.63.1-noble\n")


if __name__ == "__main__":
    unittest.main()
