# Rôle des fichiers ELMAN-OS v0.3.1

## Racine

| Fichier | Rôle |
|---|---|
| `README.md` | démarrage, capacités, commandes et limites |
| `pyproject.toml` | packaging Python, CLI et extras optionnels |
| `CHANGELOG.md` | historique des capacités ajoutées à la fondation |
| `MIGRATION-v0.2.1-to-v0.3.0.md` | procédure de migration sûre depuis v0.2.1 |
| `RELEASE-MANIFEST.json` | identité, contenu et limites vérifiées du bundle |

## Kernel

| Fichier | Rôle |
|---|---|
| `src/elman_os/catalog.py` | registre et prompts des 21 agents |
| `src/elman_os/domain.py` | contrats typés partagés |
| `src/elman_os/planning.py` | validation d’intention et plan du pipeline |
| `src/elman_os/workflow.py` | exécution de la boucle bornée |
| `src/elman_os/metacognition.py` | Supervisor, Reflective, Memory et Learning |
| `src/elman_os/approvals.py` | approbations humaines indépendantes |
| `src/elman_os/persistence.py` | rapports et approbations SQLite |
| `src/elman_os/generator.py` | génération sécurisée du starter |
| `src/elman_os/service.py` | composition planification/génération |
| `src/elman_os/provider.py` | contrat d’adaptateur pour les modèles |
| `src/elman_os/plugins.py` | permissions et plugins internes |
| `src/elman_os/technology_policy.py` | frontières Python et langages spécialisés |
| `src/elman_os/api.py` | control plane FastAPI optionnel |
| `src/elman_os/cli.py` | commandes utilisateur |

## Documentation

| Fichier | Rôle |
|---|---|
| `docs/ARCHITECTURE.md` | architecture agentique, métacognitive et technique |
| `docs/AGENT-PROMPTS.md` | contrat commun et instructions spécialisées |
| `docs/TECHNOLOGY-POLICY.md` | règles Python-first par couche |
| `docs/PLUGIN-CONTRACT.md` | permissions et extension des plugins |
| `docs/INSTALL-WINDOWS.md` | installation et usage sous PowerShell |
| `docs/metacognitive-checkpoint-v0.3.json` | preuve structurée du jalon |
| `docs/metacognitive-checkpoint-foundation-kit-v0.3.0.json` | contrôle de consolidation du Foundation Kit |
| `docs/metacognitive-checkpoint-foundation-kit-v0.3.1.json` | contrôle du correctif Windows/Python 3.13 |

## Tests

| Groupe | Rôle |
|---|---|
| `test_catalog.py` | nombres, unicité et prompts |
| `test_planning.py` | routage SaaS/mobile/fullstack |
| `test_workflow.py` | correction puis réussite et limite |
| `test_metacognition.py` | conditions d’arrêt, mémoire et apprentissage |
| `test_approvals.py` | séparation demandeur/décideur et portée |
| `test_persistence.py` | SQLite et rapports |
| `test_generator.py` | sandbox, non-écrasement et starter |
| `test_plugins.py` | permissions, approbation humaine et plugins |
| `test_technology_policy.py` | frontières des langages |
