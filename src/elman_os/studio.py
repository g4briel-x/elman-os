"""Local ELMAN Studio MVP with an explicit human approval gate."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .planning import ExecutionPlan, ProjectIntent, ProjectKind
from .service import ElmanKernelService
from .studio_history import (
    HistoryReadError,
    WorkflowDetails,
    WorkflowHistoryReader,
)
from .studio_runtime import (
    LocalWorkflowEvent,
    LocalWorkflowRequest,
    LocalWorkflowRunner,
)


_ENTRY_SEPARATOR = re.compile(r"[\n,;]+")
_SLUG_SEPARATOR = re.compile(r"[^a-z0-9]+")


def split_entries(value: str) -> tuple[str, ...]:
    """Split comma/newline-separated entries, trim them and preserve order."""

    seen: set[str] = set()
    entries: list[str] = []
    for raw in _ENTRY_SEPARATOR.split(value):
        item = raw.strip()
        if item and item not in seen:
            seen.add(item)
            entries.append(item)
    return tuple(entries)


def slugify(value: str) -> str:
    """Create a portable lowercase slug suitable for ProjectIntent."""

    normalized = unicodedata.normalize("NFKD", value)
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii").lower()
    slug = _SLUG_SEPARATOR.sub("-", ascii_value).strip("-")
    if not slug:
        return "elman-project"
    if not slug[0].isalpha():
        slug = f"project-{slug}"
    return slug


@dataclass(frozen=True, slots=True)
class StudioForm:
    """Validated Studio form data before it reaches the kernel service."""

    name: str
    slug: str
    kind: str
    platforms: tuple[str, ...]
    features: tuple[str, ...] = ()
    acceptance_criteria: tuple[str, ...] = ()

    @classmethod
    def from_text(
        cls,
        *,
        name: str,
        slug: str,
        kind: str,
        platforms: tuple[str, ...],
        features: str = "",
        acceptance_criteria: str = "",
    ) -> "StudioForm":
        return cls(
            name=name.strip(),
            slug=slug.strip(),
            kind=kind.strip(),
            platforms=tuple(platforms),
            features=split_entries(features),
            acceptance_criteria=split_entries(acceptance_criteria),
        )

    def to_intent(self) -> ProjectIntent:
        return ProjectIntent(
            name=self.name,
            slug=self.slug,
            kind=ProjectKind(self.kind),
            platforms=self.platforms,
            features=self.features,
            acceptance_criteria=self.acceptance_criteria,
        )


@dataclass(slots=True)
class StudioSession:
    """Stateful Studio application service with a mandatory human gate."""

    service: Any = field(repr=False)
    generated_root: Path = field(default_factory=lambda: Path("generated"))
    intent: ProjectIntent | None = field(init=False, default=None)
    plan: ExecutionPlan | None = field(init=False, default=None)
    approved: bool = field(init=False, default=False)

    @classmethod
    def default(cls, generated_root: str | Path = "generated") -> "StudioSession":
        return cls(
            service=ElmanKernelService.default(),
            generated_root=Path(generated_root),
        )

    def preview(self, form: StudioForm) -> ExecutionPlan:
        """Build a plan without writing files and revoke prior approval."""

        intent = form.to_intent()
        plan = self.service.plan(intent)
        self.intent = intent
        self.plan = plan
        self.approved = False
        return plan

    def approve(self) -> None:
        if self.plan is None or self.intent is None:
            raise RuntimeError("Prévisualiser un plan avant de l'approuver")
        self.approved = True

    def revoke(self) -> None:
        self.approved = False

    def generate(self) -> Any:
        """Generate only after the currently previewed plan is approved."""

        if self.intent is None or self.plan is None:
            raise RuntimeError("Aucun plan n'a été prévisualisé")
        if not self.approved:
            raise PermissionError(
                "Une approbation humaine explicite est requise avant génération"
            )
        return self.service.generate(self.intent, self.generated_root)


def launch_studio(
    generated_root: str | Path = "generated",
    database_path: str | Path = ".elman/elman.db",
) -> None:
    """Launch the optional Flet desktop/web UI."""

    try:
        import flet as ft
    except ImportError as exc:  # pragma: no cover - optional integration
        raise RuntimeError(
            'Installer Studio avec: python -m pip install -e ".[studio]"'
        ) from exc

    session = StudioSession.default(generated_root)
    history_reader = WorkflowHistoryReader(database_path)
    workflow_runner = LocalWorkflowRunner(
        database_path,
        iteration_delay_seconds=0.25,
    )

    def main(page: Any) -> None:
        page.title = "ELMAN Studio"
        page.padding = 24
        page.scroll = ft.ScrollMode.AUTO
        page.window_width = 1180
        page.window_height = 820

        title = ft.Text("ELMAN Studio", size=30, weight=ft.FontWeight.BOLD)
        subtitle = ft.Text(
            "Planifier puis générer un projet sous approbation humaine explicite."
        )

        name_field = ft.TextField(
            label="Nom du projet",
            hint_text="Ex. ELMAN Tasks",
            autofocus=True,
        )
        slug_field = ft.TextField(
            label="Slug",
            hint_text="elman-tasks",
        )
        kind_field = ft.Dropdown(
            label="Type de projet",
            value=ProjectKind.SAAS.value,
            options=[
                ft.dropdown.Option(ProjectKind.SAAS.value, "SaaS web"),
                ft.dropdown.Option(ProjectKind.MOBILE.value, "Application mobile"),
                ft.dropdown.Option(ProjectKind.FULLSTACK.value, "Full-stack"),
            ],
        )

        platform_boxes = {
            platform: ft.Checkbox(label=platform, value=(platform == "web"))
            for platform in ("web", "android", "ios", "windows", "macos", "linux")
        }

        features_field = ft.TextField(
            label="Fonctionnalités",
            hint_text="Une fonctionnalité par ligne",
            multiline=True,
            min_lines=4,
            max_lines=7,
        )
        acceptance_field = ft.TextField(
            label="Critères d'acceptation",
            hint_text="Un critère par ligne",
            multiline=True,
            min_lines=4,
            max_lines=7,
        )

        status_text = ft.Text("Statut : en attente d'un plan")
        approval_box = ft.Checkbox(
            label="J'approuve explicitement ce plan pour la génération locale",
            value=False,
            disabled=True,
        )
        generate_button = ft.ElevatedButton(
            "Générer le starter",
            disabled=True,
        )
        plan_view = ft.Column(spacing=10)
        history_status = ft.Text("Historique : non chargé")
        history_view = ft.Column(spacing=8)
        history_details = ft.Column(spacing=6)

        workflow_id_field = ft.TextField(
            label="Identifiant du workflow",
            value="studio-local-demo",
        )
        workflow_pass_on_field = ft.TextField(
            label="Réussite à l'itération",
            value="2",
            keyboard_type=ft.KeyboardType.NUMBER,
        )
        workflow_limit_field = ft.TextField(
            label="Nombre maximal d'itérations",
            value="5",
            keyboard_type=ft.KeyboardType.NUMBER,
        )
        workflow_approval_box = ft.Checkbox(
            label=(
                "J'approuve explicitement cette exécution locale "
                "déterministe"
            ),
            value=False,
        )
        workflow_run_button = ft.ElevatedButton(
            "Lancer le workflow local",
            disabled=True,
        )
        workflow_status = ft.Text("Exécution : prête")
        workflow_progress = ft.ProgressBar(value=0.0)
        workflow_log = ft.Column(spacing=4)

        def notify(message: str, *, error: bool = False) -> None:
            prefix = "Erreur : " if error else ""
            page.snack_bar = ft.SnackBar(ft.Text(f"{prefix}{message}"))
            page.snack_bar.open = True
            page.update()

        def current_form() -> StudioForm:
            selected = tuple(
                platform
                for platform, checkbox in platform_boxes.items()
                if checkbox.value
            )
            return StudioForm.from_text(
                name=name_field.value or "",
                slug=slug_field.value or "",
                kind=kind_field.value or ProjectKind.SAAS.value,
                platforms=selected,
                features=features_field.value or "",
                acceptance_criteria=acceptance_field.value or "",
            )

        def render_plan(plan: ExecutionPlan) -> None:
            plan_view.controls.clear()
            for index, stage in enumerate(plan.stages, start=1):
                gate = " • approbation humaine" if stage.human_gate_after else ""
                plan_view.controls.append(
                    ft.Card(
                        content=ft.Container(
                            content=ft.Column(
                                [
                                    ft.Text(
                                        f"{index}. {stage.name}{gate}",
                                        weight=ft.FontWeight.BOLD,
                                    ),
                                    ft.Text(
                                        "Agents : " + ", ".join(stage.agent_ids)
                                    ),
                                    ft.Text(
                                        "Sorties : "
                                        + ", ".join(stage.required_outputs)
                                    ),
                                ],
                                spacing=6,
                            ),
                            padding=14,
                        )
                    )
                )

        def generate_slug(_: Any) -> None:
            slug_field.value = slugify(name_field.value or "")
            page.update()

        def preview_plan(_: Any) -> None:
            try:
                plan = session.preview(current_form())
            except (ValueError, RuntimeError) as exc:
                status_text.value = "Statut : plan invalide"
                approval_box.value = False
                approval_box.disabled = True
                generate_button.disabled = True
                notify(str(exc), error=True)
                return

            render_plan(plan)
            approval_box.value = False
            approval_box.disabled = False
            generate_button.disabled = True
            status_text.value = (
                f"Statut : plan prêt — {len(plan.stages)} étapes, "
                f"vérificateur final {plan.final_verifier}"
            )
            page.update()

        def approval_changed(_: Any) -> None:
            try:
                if approval_box.value:
                    session.approve()
                    generate_button.disabled = False
                    status_text.value = "Statut : plan approuvé pour génération"
                else:
                    session.revoke()
                    generate_button.disabled = True
                    status_text.value = "Statut : approbation retirée"
            except RuntimeError as exc:
                approval_box.value = False
                generate_button.disabled = True
                notify(str(exc), error=True)
            page.update()

        def generate_project(_: Any) -> None:
            try:
                result = session.generate()
            except (PermissionError, RuntimeError, ValueError, OSError) as exc:
                notify(str(exc), error=True)
                return

            status_text.value = (
                f"Statut : projet généré dans {result.project_root} "
                f"({len(result.files)} fichiers)"
            )
            generate_button.disabled = True
            approval_box.value = False
            session.revoke()
            notify(f"Projet généré dans {result.project_root}")
            page.update()

        def render_detail_group(
            heading: str,
            values: tuple[str, ...],
        ) -> list[Any]:
            controls: list[Any] = [
                ft.Text(heading, weight=ft.FontWeight.BOLD)
            ]
            if values:
                controls.extend(
                    ft.Text(f"• {value}", selectable=True)
                    for value in values
                )
            else:
                controls.append(ft.Text("Aucune donnée enregistrée."))
            return controls

        def render_history_details(details: WorkflowDetails) -> None:
            summary = details.summary
            history_details.controls.clear()
            history_details.controls.extend(
                [
                    ft.Text(
                        summary.workflow_id,
                        size=18,
                        weight=ft.FontWeight.BOLD,
                    ),
                    ft.Text(
                        "État : "
                        f"{summary.status} • arrêt : {summary.stop_reason} • "
                        f"itérations : {summary.iteration_count} • "
                        f"verdict : {summary.final_verdict or '-'}"
                    ),
                    *render_detail_group("Preuves", details.evidence),
                    *render_detail_group(
                        "Décisions métacognitives",
                        details.decisions,
                    ),
                    *render_detail_group(
                        "Propositions d'apprentissage",
                        details.learning_proposals,
                    ),
                    *render_detail_group(
                        "Clés de mémoire",
                        details.memory_keys,
                    ),
                ]
            )

        def select_history_run(event: Any) -> None:
            workflow_id = str(event.control.data or "")
            try:
                details = history_reader.get_run(workflow_id)
            except (HistoryReadError, ValueError) as exc:
                notify(str(exc), error=True)
                return
            if details is None:
                notify("Le workflow sélectionné n'existe plus.", error=True)
                return
            render_history_details(details)
            page.update()

        def refresh_history(_: Any) -> None:
            history_view.controls.clear()
            history_details.controls.clear()

            if not history_reader.available:
                history_status.value = (
                    "Historique : base absente — aucune base n'a été créée"
                )
                history_view.controls.append(
                    ft.Text(str(history_reader.database_path), selectable=True)
                )
                page.update()
                return

            try:
                snapshots = history_reader.list_runs(50)
            except HistoryReadError as exc:
                history_status.value = "Historique : lecture impossible"
                notify(str(exc), error=True)
                return

            if not snapshots:
                history_status.value = "Historique : base vide"
                history_view.controls.append(
                    ft.Text("Aucun workflow persistant n'est disponible.")
                )
                page.update()
                return

            history_status.value = (
                f"Historique : {len(snapshots)} workflow(s) en lecture seule"
            )
            for snapshot in snapshots:
                verdict = snapshot.final_verdict or "-"
                history_view.controls.append(
                    ft.Card(
                        content=ft.Container(
                            content=ft.Row(
                                [
                                    ft.Container(
                                        content=ft.Column(
                                            [
                                                ft.Text(
                                                    snapshot.workflow_id,
                                                    weight=ft.FontWeight.BOLD,
                                                ),
                                                ft.Text(
                                                    f"{snapshot.status} • "
                                                    f"{snapshot.stop_reason}"
                                                ),
                                                ft.Text(
                                                    f"{snapshot.iteration_count} "
                                                    f"itération(s) • "
                                                    f"verdict {verdict} • "
                                                    f"{snapshot.updated_at}"
                                                ),
                                            ],
                                            spacing=4,
                                        ),
                                        expand=True,
                                    ),
                                    ft.OutlinedButton(
                                        "Consulter",
                                        data=snapshot.workflow_id,
                                        on_click=select_history_run,
                                    ),
                                ]
                            ),
                            padding=12,
                        )
                    )
                )
            page.update()

        def workflow_approval_changed(_: Any) -> None:
            workflow_run_button.disabled = not bool(
                workflow_approval_box.value
            )
            page.update()

        def apply_workflow_event(event: LocalWorkflowEvent) -> None:
            workflow_progress.value = max(0.0, min(1.0, event.progress))
            workflow_status.value = f"Exécution : {event.message}"
            workflow_log.controls.append(
                ft.Text(
                    (
                        f"{event.kind} • {event.message}"
                        + (
                            f" • verdict {event.verdict}"
                            if event.verdict
                            else ""
                        )
                    ),
                    selectable=True,
                )
            )
            page.update()

        def execute_local_workflow(request: LocalWorkflowRequest) -> None:
            try:
                report = workflow_runner.run(
                    request,
                    approved=True,
                    on_event=apply_workflow_event,
                )
            except Exception as exc:
                workflow_status.value = f"Exécution : échec — {exc}"
                notify(str(exc), error=True)
            else:
                workflow_status.value = (
                    f"Exécution : terminée — {report.status.value} / "
                    f"{report.stop_reason.value}"
                )
                refresh_history(None)
            finally:
                workflow_run_button.disabled = False
                workflow_approval_box.value = False
                workflow_progress.value = 1.0
                page.update()

        def start_local_workflow(_: Any) -> None:
            if not workflow_approval_box.value:
                notify(
                    "L'approbation humaine est obligatoire avant exécution.",
                    error=True,
                )
                return
            try:
                request = LocalWorkflowRequest(
                    workflow_id=workflow_id_field.value or "",
                    pass_on=int(workflow_pass_on_field.value or ""),
                    max_iterations=int(workflow_limit_field.value or ""),
                )
            except (TypeError, ValueError) as exc:
                notify(str(exc), error=True)
                return

            workflow_run_button.disabled = True
            workflow_approval_box.value = False
            workflow_progress.value = 0.0
            workflow_log.controls.clear()
            workflow_status.value = "Exécution : démarrage..."
            page.update()
            page.run_thread(execute_local_workflow, request)

        workflow_approval_box.on_change = workflow_approval_changed
        workflow_run_button.on_click = start_local_workflow
        approval_box.on_change = approval_changed
        generate_button.on_click = generate_project

        page.add(
            title,
            subtitle,
            ft.Divider(),
            ft.ResponsiveRow(
                [
                    ft.Container(name_field, col={"sm": 12, "md": 6}),
                    ft.Container(
                        ft.Row(
                            [
                                ft.Container(slug_field, expand=True),
                                ft.OutlinedButton(
                                    "Créer le slug",
                                    on_click=generate_slug,
                                ),
                            ]
                        ),
                        col={"sm": 12, "md": 6},
                    ),
                    ft.Container(kind_field, col={"sm": 12, "md": 6}),
                ]
            ),
            ft.Text("Plateformes", weight=ft.FontWeight.BOLD),
            ft.Row(list(platform_boxes.values()), wrap=True),
            ft.ResponsiveRow(
                [
                    ft.Container(features_field, col={"sm": 12, "md": 6}),
                    ft.Container(acceptance_field, col={"sm": 12, "md": 6}),
                ]
            ),
            ft.Row(
                [
                    ft.ElevatedButton(
                        "Prévisualiser le plan",
                        on_click=preview_plan,
                    ),
                    generate_button,
                ]
            ),
            status_text,
            approval_box,
            ft.Divider(),
            ft.Text("Pipeline proposé", size=22, weight=ft.FontWeight.BOLD),
            plan_view,
            ft.Divider(),
            ft.Text(
                "Exécution locale d'un workflow",
                size=22,
                weight=ft.FontWeight.BOLD,
            ),
            ft.Text(
                "Exécution déterministe, bornée, sans fournisseur distant."
            ),
            ft.ResponsiveRow(
                [
                    ft.Container(
                        workflow_id_field,
                        col={"sm": 12, "md": 6},
                    ),
                    ft.Container(
                        workflow_pass_on_field,
                        col={"sm": 6, "md": 3},
                    ),
                    ft.Container(
                        workflow_limit_field,
                        col={"sm": 6, "md": 3},
                    ),
                ]
            ),
            workflow_approval_box,
            ft.Row([workflow_run_button]),
            workflow_progress,
            workflow_status,
            workflow_log,
            ft.Divider(),
            ft.Row(
                [
                    ft.Text(
                        "Historique des workflows",
                        size=22,
                        weight=ft.FontWeight.BOLD,
                    ),
                    ft.OutlinedButton(
                        "Actualiser l'historique",
                        on_click=refresh_history,
                    ),
                ]
            ),
            ft.Text(
                f"Base SQLite : {history_reader.database_path}",
                selectable=True,
            ),
            history_status,
            history_view,
            ft.Text(
                "Détails du workflow",
                size=18,
                weight=ft.FontWeight.BOLD,
            ),
            history_details,
        )
        refresh_history(None)

    ft.app(target=main)
