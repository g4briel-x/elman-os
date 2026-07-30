"""Command-line entry point for the ELMAN-OS Foundation Kit."""

from __future__ import annotations

import argparse
import importlib.util
import json
import platform
from dataclasses import asdict
from pathlib import Path

from .catalog import AGENT_CATALOG
from .configuration import ConfigurationError, load_provider_settings
from .domain import CycleResult, Verdict
from .metacognition import SupervisorPolicy
from .persistence import SQLiteKernelStore
from .planning import PipelinePlanner, ProjectIntent, ProjectKind
from .plugins import built_in_registry
from .service import ElmanKernelService
from .technology_policy import TECHNOLOGY_STACK, audit_technology_policy
from .workflow import ElmanWorkflow


def _agents_command(as_json: bool) -> int:
    if as_json:
        print(json.dumps([asdict(agent) for agent in AGENT_CATALOG], ensure_ascii=False, indent=2))
        return 0
    for agent in AGENT_CATALOG:
        print(f"{agent.agent_id:<20} {agent.layer.value:<14} {agent.role}")
    return 0


def _demo_command(
    pass_on: int,
    max_iterations: int,
    database: str | None,
) -> int:
    if pass_on < 1:
        raise ValueError("--pass-on doit être supérieur ou égal à 1")

    store = SQLiteKernelStore(database) if database else None
    workflow = ElmanWorkflow(
        policy=SupervisorPolicy(
            max_iterations=max_iterations,
            max_same_failure=max_iterations + 1,
            max_no_progress=max_iterations + 1,
        ),
        report_sink=store.save_workflow if store else None,
    )

    def demo_cycle(iteration: int, context: dict[str, object]) -> CycleResult:
        if iteration >= pass_on:
            return CycleResult(
                proof_verdict=Verdict.PASS,
                criteria_validated=True,
                progress_score=1.0,
                cost_units=1.0,
                evidence=["DEMO-001 validé par le provider déterministe"],
            )
        return CycleResult(
            proof_verdict=Verdict.REWORK_REQUIRED,
            criteria_validated=False,
            progress_score=min(0.9, iteration / max(pass_on, 1)),
            cost_units=1.0,
            evidence=[f"Cycle de démonstration {iteration} exécuté"],
            failure_fingerprint=f"demo-failure-{iteration}",
        )

    report = workflow.run(demo_cycle, workflow_id="elman-demo")
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2, default=str))
    return 0


def _project_intent(args: argparse.Namespace) -> ProjectIntent:
    return ProjectIntent(
        name=args.name,
        slug=args.slug,
        kind=ProjectKind(args.kind),
        platforms=tuple(args.platform),
        features=tuple(args.feature),
        acceptance_criteria=tuple(args.acceptance),
    )


def _plan_command(args: argparse.Namespace) -> int:
    plan = PipelinePlanner().build(_project_intent(args))
    print(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2, default=str))
    return 0


def _generate_command(args: argparse.Namespace) -> int:
    service = ElmanKernelService.default()
    result = service.generate(_project_intent(args), args.output)
    print(
        json.dumps(
            {
                "project_root": str(result.project_root),
                "files": list(result.files),
                "final_verifier": result.plan.final_verifier,
                "metacognitive_agents": list(result.plan.metacognitive_agents),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def _plugins_command(as_json: bool) -> int:
    manifests = built_in_registry().manifests
    if as_json:
        print(
            json.dumps(
                [asdict(manifest) for manifest in manifests],
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )
        return 0
    for manifest in manifests:
        permissions = ",".join(sorted(item.value for item in manifest.permissions))
        print(f"{manifest.plugin_id:<30} {manifest.version:<8} {permissions or '-'}")
    return 0


def _runs_command(database: str, limit: int) -> int:
    store = SQLiteKernelStore(database)
    print(json.dumps(store.list_workflows(limit), ensure_ascii=False, indent=2))
    return 0


def _doctor_command(as_json: bool) -> int:
    optional_modules = ("fastapi", "uvicorn", "flet")
    report = {
        "python": platform.python_version(),
        "python_supported": tuple(map(int, platform.python_version_tuple()[:2])) >= (3, 11),
        "optional_modules": {
            module: importlib.util.find_spec(module) is not None
            for module in optional_modules
        },
        "core_ready": True,
    }
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"Python: {report['python']} (supported={report['python_supported']})")
        for module, installed in report["optional_modules"].items():
            print(f"{module:<10} {'installed' if installed else 'optional/missing'}")
        print("ELMAN-OS core: ready")
    return 0 if report["python_supported"] else 1


def _ai_config_command() -> int:
    try:
        settings = load_provider_settings()
    except ConfigurationError as exc:
        print(f"AI configuration: INVALID - {exc}")
        return 2
    print(json.dumps(settings.safe_summary(), ensure_ascii=False, indent=2))
    return 0


def _serve_command(host: str, port: int, generated_root: str) -> int:
    try:
        import uvicorn
    except ImportError as exc:
        raise RuntimeError(
            'Installer le control plane avec: python -m pip install -e ".[api]"'
        ) from exc
    from .api import create_app

    uvicorn.run(
        create_app(generated_root=generated_root),
        host=host,
        port=port,
    )
    return 0


def _technology_command(as_json: bool) -> int:
    policy = {
        "policy": "python-core-with-layer-bounded-specialized-languages",
        "stack": TECHNOLOGY_STACK,
        "core_source": "Python obligatoire",
        "specialized_sources": (
            "Web, mobile, natif, données et plateforme dans les zones approuvées"
        ),
    }
    if as_json:
        print(json.dumps(policy, ensure_ascii=False, indent=2))
        return 0

    print("ELMAN-OS technology policy: Python core + layer-bounded languages")
    for label, value in TECHNOLOGY_STACK.items():
        print(f"{label:<18} {value}")
    return 0


def _audit_stack_command(path: str) -> int:
    violations = audit_technology_policy(path)
    if not violations:
        print("Technology policy audit: PASS")
        return 0

    print("Technology policy audit: FAIL")
    for violation in violations:
        print(f"- {violation.path}: {violation.reason}")
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="elman-os",
        description="Foundation Kit multi-agents et métacognitif ELMAN-OS",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    agents = subparsers.add_parser("agents", help="Lister le registre des agents")
    agents.add_argument("--json", action="store_true", help="Sortie JSON")

    subparsers.add_parser(
        "ai-config",
        help="Valider et afficher la configuration IA sans révéler les secrets",
    )

    demo = subparsers.add_parser("demo", help="Exécuter une boucle métacognitive déterministe")
    demo.add_argument("--pass-on", type=int, default=3, help="Itération de réussite")
    demo.add_argument("--max-iterations", type=int, default=5)
    demo.add_argument("--database", help="SQLite: persister le rapport")

    def add_intent_arguments(command: argparse.ArgumentParser) -> None:
        command.add_argument("--name", required=True)
        command.add_argument("--slug", required=True)
        command.add_argument(
            "--kind",
            choices=[kind.value for kind in ProjectKind],
            required=True,
        )
        command.add_argument(
            "--platform",
            action="append",
            required=True,
            choices=["web", "android", "ios", "windows", "macos", "linux"],
        )
        command.add_argument("--feature", action="append", default=[])
        command.add_argument("--acceptance", action="append", default=[])

    plan = subparsers.add_parser(
        "plan",
        help="Construire le pipeline d'un produit sans écrire de fichiers",
    )
    add_intent_arguments(plan)

    generate = subparsers.add_parser(
        "generate",
        help="Générer un starter local borné et son dossier Proof",
    )
    add_intent_arguments(generate)
    generate.add_argument(
        "--output",
        default="generated",
        help="Dossier parent du projet généré",
    )

    plugins = subparsers.add_parser(
        "plugins",
        help="Lister les plugins internes prêts à l'emploi",
    )
    plugins.add_argument("--json", action="store_true")

    runs = subparsers.add_parser(
        "runs",
        help="Lister les workflows persistés dans SQLite",
    )
    runs.add_argument("--database", default=".elman/elman.db")
    runs.add_argument("--limit", type=int, default=50)

    doctor = subparsers.add_parser(
        "doctor",
        help="Vérifier Python et les dépendances optionnelles",
    )
    doctor.add_argument("--json", action="store_true")

    serve = subparsers.add_parser(
        "serve",
        help="Démarrer le control plane FastAPI optionnel",
    )
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--generated-root", default="generated")

    technology = subparsers.add_parser(
        "technology",
        help="Afficher la stack Python et les exceptions frontend bornées",
    )
    technology.add_argument("--json", action="store_true", help="Sortie JSON")

    audit_stack = subparsers.add_parser(
        "audit-stack",
        help="Contrôler le noyau Python et les zones frontend autorisées",
    )
    audit_stack.add_argument("path", nargs="?", default=".")

    audit_python = subparsers.add_parser(
        "audit-python",
        help="Alias de compatibilité pour audit-stack",
    )
    audit_python.add_argument("path", nargs="?", default=".")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "agents":
        return _agents_command(args.json)
    if args.command == "ai-config":
        return _ai_config_command()
    if args.command == "demo":
        return _demo_command(args.pass_on, args.max_iterations, args.database)
    if args.command == "plan":
        return _plan_command(args)
    if args.command == "generate":
        return _generate_command(args)
    if args.command == "plugins":
        return _plugins_command(args.json)
    if args.command == "runs":
        return _runs_command(args.database, args.limit)
    if args.command == "doctor":
        return _doctor_command(args.json)
    if args.command == "serve":
        return _serve_command(args.host, args.port, args.generated_root)
    if args.command == "technology":
        return _technology_command(args.json)
    if args.command in {"audit-stack", "audit-python"}:
        return _audit_stack_command(args.path)
    raise AssertionError("Commande non routée")
