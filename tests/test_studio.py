import unittest
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from elman_os.planning import PipelinePlanner, ProjectKind
from elman_os.studio import StudioForm, StudioSession, slugify, split_entries


@dataclass(frozen=True)
class FakeGenerationResult:
    project_root: Path
    files: tuple[str, ...]
    plan: object


class RecordingService:
    def __init__(self) -> None:
        self.planned = []
        self.generated = []

    def plan(self, intent):
        self.planned.append(intent)
        return PipelinePlanner().build(intent)

    def generate(self, intent, output_root):
        self.generated.append((intent, Path(output_root)))
        plan = PipelinePlanner().build(intent)
        return FakeGenerationResult(
            project_root=Path(output_root) / intent.slug,
            files=("README.md", "elman.project.json"),
            plan=plan,
        )


class StudioTests(unittest.TestCase):
    def test_split_entries_deduplicates_and_preserves_order(self) -> None:
        self.assertEqual(
            split_entries("auth\npaiement, auth; notifications"),
            ("auth", "paiement", "notifications"),
        )

    def test_slugify_produces_a_portable_slug(self) -> None:
        self.assertEqual(slugify("ÉLAN Mobile 2026"), "elan-mobile-2026")
        self.assertEqual(slugify("123 Projet"), "project-123-projet")

    def test_form_creates_a_valid_saas_intent(self) -> None:
        form = StudioForm.from_text(
            name="ELMAN Tasks",
            slug="elman-tasks",
            kind="saas",
            platforms=("web",),
            features="authentification\nprojets",
            acceptance_criteria="Un projet peut être créé",
        )
        intent = form.to_intent()
        self.assertEqual(intent.kind, ProjectKind.SAAS)
        self.assertEqual(intent.platforms, ("web",))
        self.assertEqual(intent.features, ("authentification", "projets"))

    def test_invalid_platform_combination_is_rejected(self) -> None:
        form = StudioForm.from_text(
            name="Mobile",
            slug="mobile",
            kind="mobile",
            platforms=("web",),
        )
        with self.assertRaises(ValueError):
            form.to_intent()

    def test_approval_before_preview_is_rejected(self) -> None:
        session = StudioSession(service=RecordingService())
        with self.assertRaises(RuntimeError):
            session.approve()

    def test_generation_requires_explicit_approval(self) -> None:
        service = RecordingService()
        with TemporaryDirectory() as temporary:
            session = StudioSession(
                service=service,
                generated_root=Path(temporary),
            )
            form = StudioForm.from_text(
                name="ELMAN Tasks",
                slug="elman-tasks",
                kind="saas",
                platforms=("web",),
            )
            session.preview(form)
            with self.assertRaises(PermissionError):
                session.generate()
            self.assertEqual(service.generated, [])

    def test_approved_plan_can_be_generated_and_new_preview_revokes_it(self) -> None:
        service = RecordingService()
        with TemporaryDirectory() as temporary:
            session = StudioSession(
                service=service,
                generated_root=Path(temporary),
            )
            form = StudioForm.from_text(
                name="ELMAN Tasks",
                slug="elman-tasks",
                kind="saas",
                platforms=("web",),
            )
            session.preview(form)
            session.approve()
            result = session.generate()

            self.assertEqual(result.project_root.name, "elman-tasks")
            self.assertEqual(len(service.generated), 1)

            session.preview(form)
            self.assertFalse(session.approved)


if __name__ == "__main__":
    unittest.main()
