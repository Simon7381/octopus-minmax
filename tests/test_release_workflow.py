"""Exercise the add-on workflow script without GitHub or Docker access."""

import json
import os
import re
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = (ROOT / ".github/workflows/release-build.yaml").read_text(encoding="utf-8")
ADDON_STEP = WORKFLOW.split("- name: Update add-on configuration and release notes", 1)[1]
SCRIPT = compile(textwrap.dedent(re.search(
    r"python - <<'PY'\n(.*?)          PY", ADDON_STEP, re.DOTALL,
).group(1)), "<update-addon>", "exec")
OLDER_ENTRY = "## v2.0.0 - v2.0.0\n## What's Changed\n* Hand-written notes.\n"
GENERATED_NOTES = "## What's Changed\n* Generated notes for the merged changes."


class ReleaseWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        original_directory = Path.cwd()
        os.chdir(self.directory.name)
        self.addCleanup(os.chdir, original_directory)
        self.environment = patch.dict(os.environ, {
            "VERSION": "v2.0.1", "DOCKER_IMAGE": "ExampleOwner/example-image",
            "GITHUB_REPOSITORY": "ExampleOwner/example-repo",
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.addon = Path("octopus_minmax_bot_addon")
        self.addon.mkdir()
        (self.addon / "config.yaml").write_text(
            "version: v2.0.0\nimage: old/image\nslug: unchanged\n", encoding="utf-8",
        )
        (self.addon / "CHANGELOG.md").write_text(OLDER_ENTRY, encoding="utf-8")
        (self.addon / "README.md").write_text("Old README\n", encoding="utf-8")
        Path("README.md").write_text("Current README\n", encoding="utf-8")

    def run_step(self, notes="", title="v2.0.1", generated_notes=GENERATED_NOTES):
        with patch("subprocess.check_output", side_effect=[
            json.dumps({"name": title, "body": notes}),
            json.dumps({"name": "Generated title", "body": generated_notes}),
        ]) as release:
            exec(SCRIPT, {})
        return release

    def changelog(self):
        return (self.addon / "CHANGELOG.md").read_text(encoding="utf-8")

    def test_image_comes_from_variable_and_readme_is_copied_once(self):
        self.run_step()
        self.assertEqual((self.addon / "config.yaml").read_text(),
                         "version: v2.0.1\nimage: exampleowner/example-image\nslug: unchanged\n")
        self.assertEqual((self.addon / "README.md").read_bytes(), Path("README.md").read_bytes())

    def test_blank_image_fails_without_changing_files(self):
        before = {p: p.read_bytes() for p in self.addon.iterdir()}
        for value in ("", "  "):
            with self.subTest(value=value), patch.dict(os.environ, DOCKER_IMAGE=value):
                with self.assertRaisesRegex(SystemExit, "DOCKER_IMAGE"):
                    self.run_step()
                self.assertEqual(before, {p: p.read_bytes() for p in self.addon.iterdir()})

    def test_empty_release_body_generates_notes_between_exact_tags(self):
        release = self.run_step(notes=" \n")
        self.assertIn(GENERATED_NOTES, self.changelog())
        self.assertNotIn("No release notes were provided", self.changelog())
        self.assertEqual(release.call_count, 2)
        self.assertEqual(release.call_args.args[0], [
            "gh", "api", "--method", "POST",
            "repos/ExampleOwner/example-repo/releases/generate-notes",
            "-f", "tag_name=v2.0.1", "-f", "previous_tag_name=v2.0.0",
        ])
        self.assertTrue(self.changelog().endswith(OLDER_ENTRY))

    def test_existing_empty_entry_is_repaired_without_duplication(self):
        (self.addon / "CHANGELOG.md").write_text(
            "## v2.0.1 - v2.0.1\n\n\n" + OLDER_ENTRY, encoding="utf-8",
        )
        self.run_step(notes="* Fixed release automation.")
        self.assertEqual(self.changelog().count("## v2.0.1 - v2.0.1"), 1)
        self.assertIn("* Fixed release automation.", self.changelog())
        self.assertTrue(self.changelog().endswith(OLDER_ENTRY))

    def test_handwritten_entry_with_subheadings_survives_rerun(self):
        with patch.dict(os.environ, VERSION="v2.0.0"):
            release = self.run_step(notes="Replacement notes")
        release.assert_not_called()
        self.assertEqual(self.changelog(), OLDER_ENTRY)

    def test_release_notes_preserve_markdown_and_unicode(self):
        notes = "## What's Changed\n* Keep `code`, $variables, \\paths and \u00a3."
        self.run_step(notes=notes).assert_called_once()
        self.assertIn(notes, self.changelog())
        before = self.changelog()
        self.run_step().assert_not_called()
        self.assertEqual(self.changelog(), before)

    def test_first_release_without_notes_uses_automatic_previous_tag(self):
        (self.addon / "CHANGELOG.md").write_text("", encoding="utf-8")
        release = self.run_step(notes=None, title=None)
        self.assertIn("## v2.0.1 - v2.0.1\n" + GENERATED_NOTES, self.changelog())
        self.assertFalse(any("previous_tag_name" in arg for arg in release.call_args.args[0]))

    def test_old_placeholders_are_replaced_with_published_notes(self):
        for placeholder in (
            "No release notes were provided.\n\n**Full Changelog**: https://github.com/ExampleOwner/example-repo/compare/v2.0.0...v2.0.1",
            "**Release details**: https://github.com/ExampleOwner/example-repo/releases/tag/v2.0.1",
            "**Full Changelog**: https://github.com/ExampleOwner/example-repo/compare/v2.0.0...v2.0.1",
        ):
            with self.subTest(placeholder=placeholder):
                (self.addon / "CHANGELOG.md").write_text(
                    f"## v2.0.1 - v2.0.1\n{placeholder}\n\n{OLDER_ENTRY}", encoding="utf-8",
                )
                self.run_step(notes="* Notes added after publication.").assert_called_once()
                self.assertIn("* Notes added after publication.", self.changelog())
                self.assertEqual(self.changelog().count("## v2.0.1 - v2.0.1"), 1)
                self.assertTrue(self.changelog().endswith(OLDER_ENTRY))

    def test_placeholder_is_repaired_with_generated_notes_and_rerun_is_idempotent(self):
        (self.addon / "CHANGELOG.md").write_text(
            "## v2.0.1 - v2.0.1\nNo release notes were provided.\n\n"
            "**Full Changelog**: https://github.com/ExampleOwner/example-repo/compare/v2.0.0...v2.0.1\n\n"
            + OLDER_ENTRY, encoding="utf-8",
        )
        self.run_step()
        self.assertIn(GENERATED_NOTES, self.changelog())
        self.assertNotIn("No release notes were provided", self.changelog())
        before = self.changelog()
        self.run_step().assert_not_called()
        self.assertEqual(self.changelog(), before)

    def test_repairing_older_version_keeps_newer_notes_and_uses_older_base(self):
        newer = "## v2.0.2 - Newer\n* Keep these notes.\n\n"
        (self.addon / "CHANGELOG.md").write_text(
            newer + "## v2.0.1 - v2.0.1\n\n" + OLDER_ENTRY, encoding="utf-8",
        )
        release = self.run_step()
        self.assertIn("previous_tag_name=v2.0.0", release.call_args.args[0])
        self.assertTrue(self.changelog().startswith(newer))
        self.assertTrue(self.changelog().endswith(OLDER_ENTRY))

    def test_empty_generated_notes_fail_without_changing_files(self):
        before = {p: p.read_bytes() for p in self.addon.iterdir()}
        with self.assertRaisesRegex(SystemExit, "empty generated release notes"):
            self.run_step(generated_notes=" \n")
        self.assertEqual(before, {p: p.read_bytes() for p in self.addon.iterdir()})

    def test_generation_failure_leaves_files_unchanged(self):
        before = {p: p.read_bytes() for p in self.addon.iterdir()}
        with patch("subprocess.check_output", side_effect=[
            json.dumps({"name": "v2.0.1", "body": ""}), RuntimeError("Generation unavailable"),
        ]):
            with self.assertRaisesRegex(RuntimeError, "Generation unavailable"):
                exec(SCRIPT, {})
        self.assertEqual(before, {p: p.read_bytes() for p in self.addon.iterdir()})

    def test_release_lookup_failure_leaves_files_unchanged(self):
        before = {p: p.read_bytes() for p in self.addon.iterdir()}
        with patch("subprocess.check_output", side_effect=RuntimeError("API unavailable")):
            with self.assertRaisesRegex(RuntimeError, "API unavailable"):
                exec(SCRIPT, {})
        self.assertEqual(before, {p: p.read_bytes() for p in self.addon.iterdir()})


if __name__ == "__main__":
    unittest.main()
