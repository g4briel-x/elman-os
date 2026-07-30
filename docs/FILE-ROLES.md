# Rôle des fichiers ELMAN-OS v0.4.0 alpha 6

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
| `src/elman_os/provider.py` | contrat IA générique, erreurs portables et fournisseur simulé |
| `src/elman_os/configuration.py` | chargement validé des variables et masquage des secrets |
| `src/elman_os/execution.py` | timeouts, retries, vérification des réponses et budgets IA |
| `src/elman_os/registry.py` | registre, sélection, fallback et composition du runtime IA |
| `src/elman_os/openai_compatible.py` | adaptateur OpenAI/compatible et transport HTTP injectable |
| `src/elman_os/audit.py` | identité, autorisation, pseudonymisation et chaîne d'audit signée |
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
| `docs/AI-PROVIDER-CONTRACT.md` | frontière stable des futurs fournisseurs IA |
| `docs/AI-CONFIGURATION.md` | variables, sécurité et commandes PowerShell |
| `docs/AI-RUNTIME-RESILIENCE.md` | garanties d'exécution, erreurs, retries et budgets |
| `docs/AI-PROVIDER-REGISTRY.md` | sélection, capacités et fallback contrôlé |
| `docs/AI-OPENAI-COMPATIBLE.md` | protocole HTTP, configuration et sécurité des adaptateurs |
| `docs/AI-EXECUTION-AUDIT.md` | garanties d'identité, minimisation et intégrité des traces |
| `docs/INSTALL-WINDOWS.md` | installation et usage sous PowerShell |
| `docs/metacognitive-checkpoint-v0.3.json` | preuve structurée du jalon |
| `docs/metacognitive-checkpoint-foundation-kit-v0.3.0.json` | contrôle de consolidation du Foundation Kit |
| `docs/metacognitive-checkpoint-foundation-kit-v0.3.1.json` | contrôle du correctif Windows/Python 3.13 |
| `docs/metacognitive-checkpoint-v0.4.0-alpha.1.json` | preuve bornée du contrat fournisseur IA |
| `docs/metacognitive-checkpoint-v0.4.0-alpha.2.json` | preuve bornée de la configuration sécurisée |
| `docs/metacognitive-checkpoint-v0.4.0-alpha.3.json` | preuve bornée du runtime IA résilient |
| `docs/metacognitive-checkpoint-v0.4.0-alpha.5.json` | preuve bornée des adaptateurs testés hors réseau |
| `docs/metacognitive-checkpoint-v0.4.0-alpha.6.json` | preuve bornée de l'authentification et de l'audit IA |

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
| `test_provider.py` | contrat IA, validations, erreurs et double sans réseau |
| `test_configuration.py` | variables, limites, URL et absence de fuite des secrets |
| `test_execution.py` | délais, retries, annulations, contrat de réponse et budgets |
| `test_registry.py` | enregistrement, sélection, fallback et pipeline configuré |
| `test_openai_compatible.py` | traduction HTTP, erreurs et transport simulé sans réseau |
| `test_audit.py` | autorisation, confidentialité, intégrité, échecs et annulations |
