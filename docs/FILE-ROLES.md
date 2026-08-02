# Rôle des fichiers ELMAN-OS v0.5.1

## Racine

| Fichier | Rôle |
|---|---|
| `README.md` | démarrage, capacités, commandes et limites |
| `pyproject.toml` | packaging Python, CLI et extras optionnels |
| `CHANGELOG.md` | historique des capacités ajoutées à la fondation |
| `MIGRATION-v0.2.1-to-v0.3.0.md` | procédure de migration sûre depuis v0.2.1 |
| `MIGRATION-v0.3.1-to-v0.4.0.md` | migration, validation et retour arrière depuis v0.3.1 |
| `MIGRATION-v0.4.0-to-v0.5.0.md` | migration sûre, compatibilité et retour arrière depuis v0.4.0 |
| `MIGRATION-v0.5.0-to-v0.5.1.md` | correctif d’intégrité et retour arrière depuis v0.5.0 |
| `RELEASE-MANIFEST.json` | identité, contenu et limites vérifiées du bundle |
| `RELEASE-CHECKSUMS.sha256` | empreintes de chaque fichier livré |

## Kernel

| Fichier | Rôle |
|---|---|
| `src/elman_os/catalog.py` | registre et prompts des 21 agents |
| `src/elman_os/domain.py` | contrats typés partagés |
| `src/elman_os/planning.py` | validation d’intention et plan du pipeline |
| `src/elman_os/workflow.py` | exécution de la boucle bornée |
| `src/elman_os/metacognition.py` | Supervisor, Reflective, Memory et Learning |
| `src/elman_os/approvals.py` | approbations humaines indépendantes |
| `src/elman_os/persistence.py` | rapports et approbations SQLite historiques |
| `src/elman_os/authentication.py` | validation JWT/OIDC et politique de jetons |
| `src/elman_os/transactional_persistence.py` | transactions multi-tenant et concurrence optimiste |
| `src/elman_os/persistent_governance.py` | quotas et audit persistants multi-instance |
| `src/elman_os/production_runtime.py` | composition authentifiée du runtime de production |
| `src/elman_os/generator.py` | génération sécurisée du starter |
| `src/elman_os/service.py` | composition planification/génération |
| `src/elman_os/provider.py` | contrat IA générique, erreurs portables et fournisseur simulé |
| `src/elman_os/configuration.py` | chargement validé des variables et masquage des secrets |
| `src/elman_os/execution.py` | timeouts, retries, vérification des réponses et budgets IA |
| `src/elman_os/registry.py` | registre, sélection, fallback et composition du runtime IA |
| `src/elman_os/openai_compatible.py` | adaptateur OpenAI/compatible et transport HTTP injectable |
| `src/elman_os/audit.py` | identité, autorisation, chaîne signée et persistance JSONL durable |
| `src/elman_os/governance.py` | compatibilité, quotas par identité et runtime IA stabilisé |
| `src/elman_os/release.py` | validation hors réseau, portabilité et intégrité SHA-256 |
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
| `docs/AI-KERNEL-STABILIZATION.md` | prévalidation, quotas et reprise d'audit |
| `docs/RELEASE.md` | décision v0.5.1, preuves de validation et limites opérationnelles |
| `docs/JWT-OIDC-AUTHENTICATION.md` | frontière et politique d’authentification |
| `docs/TRANSACTIONAL-PERSISTENCE.md` | stockage transactionnel isolé par tenant |
| `docs/PERSISTENT-QUOTAS-AUDIT.md` | gouvernance persistante partagée entre instances |
| `docs/PRODUCTION-EXECUTION-RUNTIME.md` | pipeline authentifié et route d’exécution |
| `docs/INSTALL-WINDOWS.md` | installation et usage sous PowerShell |
| `docs/metacognitive-checkpoint-v0.3.json` | preuve structurée du jalon |
| `docs/metacognitive-checkpoint-foundation-kit-v0.3.0.json` | contrôle de consolidation du Foundation Kit |
| `docs/metacognitive-checkpoint-foundation-kit-v0.3.1.json` | contrôle du correctif Windows/Python 3.13 |
| `docs/metacognitive-checkpoint-v0.4.0-alpha.1.json` | preuve bornée du contrat fournisseur IA |
| `docs/metacognitive-checkpoint-v0.4.0-alpha.2.json` | preuve bornée de la configuration sécurisée |
| `docs/metacognitive-checkpoint-v0.4.0-alpha.3.json` | preuve bornée du runtime IA résilient |
| `docs/metacognitive-checkpoint-v0.4.0-alpha.5.json` | preuve bornée des adaptateurs testés hors réseau |
| `docs/metacognitive-checkpoint-v0.4.0-alpha.6.json` | preuve bornée de l'authentification et de l'audit IA |
| `docs/metacognitive-checkpoint-v0.4.0-alpha.7.json` | preuve de stabilisation du Kernel IA |
| `docs/metacognitive-checkpoint-v0.4.0.json` | preuve de validation de la version stable |

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
| `test_stabilization.py` | compatibilité, quotas, concurrence, persistance et pipeline complet |
| `test_release.py` | versions, checksums, portabilité et fermeture des gates |
| `test_authentication.py` | jetons, signatures, claims et refus fermés |
| `test_transactional_persistence.py` | transactions, tenants et concurrence multi-instance |
| `test_persistent_governance.py` | quotas et chaînes d’audit persistants |
| `test_production_runtime.py` | composition complète et API authentifiée |
| `test_release_v051.py` | cohérence de la finalisation v0.5.1 |

## Scripts de release

| Fichier | Rôle |
|---|---|
| `scripts/build_release.py` | construction ZIP déterministe v0.5.1 |
| `scripts/verify_release_installation.py` | roue, installation isolée et archive hors réseau |

## ELMAN Studio MVP — phase 1

| Fichier | Rôle |
|---|---|
| `src/elman_os/studio.py` | Modèle de formulaire, session avec gate humaine et interface Flet optionnelle. |
| `tests/test_studio.py` | Tests hors réseau du formulaire, du slug, du plan et de l'approbation. |
| `docs/STUDIO-MVP.md` | Architecture, règles d'autorité, lancement et limites de Studio. |

## ELMAN Studio MVP — phase 2

| Fichier | Rôle |
|---|---|
| `src/elman_os/studio_history.py` | Lecture SQLite strictement read-only, résumés et détails des workflows. |
| `tests/test_studio_history.py` | Non-régression : base absente, base vide, preuves, décisions et absence d'écriture. |
| `docs/STUDIO-RUN-HISTORY.md` | Architecture, lancement, frontières d'autorité et limites de la phase 2. |

## ELMAN Studio — phase 3

| Fichier | Rôle |
|---|---|
| `src/elman_os/studio_runtime.py` | Exécution locale déterministe, gate humaine, événements minimaux et persistance SQLite. |
| `tests/test_studio_runtime.py` | Validation de la gate, des limites, des événements et de la persistance. |
| `docs/STUDIO-LIVE-WORKFLOWS.md` | Architecture, autorité, lancement et limites de la phase 3. |

## Release candidate v0.6.0-rc.1

| Fichier | Rôle |
|---|---|
| `MIGRATION-v0.5.1-to-v0.6.0-rc.1.md` | Compatibilité, installation Studio, données locales et retour arrière. |
| `tests/test_release_v060rc1.py` | Cohérence PEP 440, manifeste, gates, documentation et archive RC. |
| `docs/RELEASE.md` | Décision, preuves, frontières et procédure de tag de la release candidate. |
| `scripts/verify_release_installation.py` | Construction et installation hors réseau de la roue `0.6.0rc1`. |
