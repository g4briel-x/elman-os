import compileall
import json
import tempfile
import unittest
from pathlib import Path

from elman_os.generator import StarterProjectGenerator, WorkspaceSandbox
from elman_os.planning import PipelinePlanner, ProjectIntent, ProjectKind
from elman_os.technology_policy import audit_technology_policy


class GeneratorTests(unittest.TestCase):
    def test_fullstack_starter_is_generated_inside_workspace(self) -> None:
        intent = ProjectIntent(
            name="Task Flow",
            slug="task-flow",
            kind=ProjectKind.FULLSTACK,
            platforms=("web", "android"),
            acceptance_criteria=("Le domaine est testé",),
        )
        plan = PipelinePlanner().build(intent)

        with tempfile.TemporaryDirectory() as directory:
            result = StarterProjectGenerator().generate(intent, plan, directory)
            self.assertEqual(
                result.project_root,
                (Path(directory) / "task-flow").resolve(),
            )
            self.assertIn("src/task_flow/mobile.py", result.files)
            self.assertTrue((result.project_root / "proof/acceptance.json").is_file())
            manifest = json.loads(
                (result.project_root / "elman.project.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(manifest["intent"]["kind"], "fullstack")
            self.assertEqual(
                manifest["pipeline"]["final_verifier"],
                "ELMAN_PROOF",
            )
            self.assertTrue(
                compileall.compile_dir(result.project_root, quiet=1)
            )
            self.assertEqual(audit_technology_policy(result.project_root), ())

    def test_workspace_rejects_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sandbox = WorkspaceSandbox(Path(directory) / "safe")
            with self.assertRaises(PermissionError):
                sandbox.write_text("../outside.txt", "no")
            self.assertFalse((Path(directory) / "outside.txt").exists())

    def test_existing_project_is_not_overwritten(self) -> None:
        intent = ProjectIntent(
            name="Existing",
            slug="existing",
            kind=ProjectKind.SAAS,
            platforms=("web",),
        )
        plan = PipelinePlanner().build(intent)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "existing"
            root.mkdir()
            protected = root / "README.md"
            protected.write_text("user content", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                StarterProjectGenerator().generate(intent, plan, directory)
            self.assertEqual(protected.read_text(encoding="utf-8"), "user content")


if __name__ == "__main__":
    unittest.main()
