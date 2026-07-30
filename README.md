# ELMAN-OS Foundation Kit v0.4.0

ELMAN-OS est une fabrique logicielle multi-agents destinée à transformer une
intention en application SaaS web, mobile ou full-stack traçable.

Cette version stable part de la fondation v0.3.1 compatible Windows/Python 3.13 et
ajoute le runtime IA v0.4 : contrat générique testable sans appel payant,
configuration sécurisée par variables d'environnement, exécution bornée avec
timeouts, retries et budgets, registre dynamique, adaptateurs OpenAI et
OpenAI-compatible à transport injectable, puis authentification et audit signé
des exécutions. La version v0.4.0 stabilise cet ensemble avec prévalidation,
quotas par identité et tenant, audit persistant vérifiable après redémarrage,
réservations non rejouables et validation de release hors réseau.
Elle constitue la version stable du Foundation Kit, sans être encore une
plateforme autonome de production :

- 1 orchestrateur : ELMAN Nexus ;
- 15 agents spécialisés ;
- 1 vérificateur final indépendant : ELMAN Proof ;
- 4 agents métacognitifs internes ;
- planification déterministe du pipeline ;
- boucle d’amélioration bornée ;
- mémoire de workflow et persistance SQLite ;
- approbations humaines indépendantes ;
- sandbox de génération par chemin ;
- premier starter Python SaaS/mobile ;
- 4 plugins internes à permissions ;
- contrat générique de fournisseur IA ;
- fournisseur IA déterministe sans réseau pour les tests ;
- configuration immuable et validée sans secret dans les diagnostics ;
- délais réels, retries bornés et erreurs d'exécution portables ;
- budgets partagés d'appels, de tokens et de durée ;
- registre des fournisseurs, contrôle des capacités et sélection configurée ;
- fallback déterministe désactivé par défaut et explicitement traçable ;
- adaptateurs OpenAI et OpenAI-compatible sans dépendance SDK ;
- transport HTTP injectable, erreurs normalisées et tests entièrement hors réseau ;
- exécution réservée aux principaux authentifiés possédant le rôle `ai.execute` ;
- audit minimal pseudonymisé, signé et chaîné sans journalisation de payload ;
- prévalidation de compatibilité avant création d'un adaptateur ;
- quotas atomiques de requêtes, tokens et concurrence par identité ;
- audit JSONL append-only, durable et repris après vérification de chaîne ;
- réservations de quota non rejouables, isolées par tenant et identité ;
- inventaire SHA-256 et contrôle de release hors réseau ;
- matrice CI Windows/macOS/Linux sur Python 3.11 à 3.13 ;
- politique Python-first contrôlée par couche.

Le standard « 15+ années » décrit un niveau de méthode, de jugement et de
rigueur. Il ne prétend pas attribuer aux agents une carrière humaine réelle.

## Statut fonctionnel

| Capacité | Statut v0.4.0 |
|---|---|
| Registre des 21 agents | Exécutable |
| Prompts et frontières d’autorité | Exécutables |
| Boucle métacognitive et arrêts | Exécutables |
| Pipeline SaaS/mobile | Planifiable |
| Générateur de starter | Exécutable, déterministe |
| Persistance locale | SQLite, exécutable |
| Approbations humaines | Exécutables |
| Plugins internes | 4 plugins exécutables |
| API de contrôle | Adaptateur FastAPI optionnel |
| Contrat fournisseur IA | Exécutable et testé |
| Configuration IA | Variables d'environnement validées, secrets masqués |
| Exécution IA | Timeouts, retries et budgets bornés, sans réseau |
| Registre IA | Sélection configurée et fallback déterministe contrôlé |
| Adaptateurs distants | OpenAI/compatible livrés, connectivité réelle non validée |
| Authentification d'exécution | Principal vérifié à la frontière et rôle obligatoire |
| Stabilisation IA | Prévalidation et quotas atomiques par identité |
| Audit IA | Événements minimaux HMAC, persistants et vérifiables après reprise |
| Validation de release | Versions, SHA-256, secrets, chemins et politique contrôlés hors réseau |
| ELMAN Studio | Non livré |
| Sandbox de processus/conteneurs | Non livrée |
| Déploiement production/stores | Interdit sans approbation |

## Architecture agentique

| Couche | Agents | Responsabilité |
|---|---:|---|
| Orchestration | 1 | Cadrage, routage, budgets et transitions |
| Production | 15 | Produit, UX/UI, architecture, code, sécurité et livraison |
| Vérification | 1 | Verdict indépendant avant livraison |
| Métacognition | 4 | Supervision, réflexion, mémoire et apprentissage contrôlé |
| **Total** | **21** | Rôles séparés et auditables |

Les agents métacognitifs observent le pipeline sans produire le code du
produit :

- **ELMAN Supervisor** décide de continuer, corriger, arrêter ou escalader ;
- **ELMAN Reflective** analyse l’écart entre intention, action et résultat ;
- **ELMAN Memory** conserve les éléments autorisés avec provenance ;
- **ELMAN Learning** propose des améliorations sans les activer seul.

## Conditions d’arrêt

La boucle s’arrête sur :

- critères validés par Proof ;
- itérations maximales ;
- budget ou durée maximale ;
- même échec répété ;
- absence de progrès mesurable ;
- finding critique ;
- permission ou décision humaine manquante ;
- annulation.

Un succès technique mène à `ready_for_human_approval`, jamais à un déploiement
automatique.

## Politique technologique

ELMAN-OS est **Python-first, pas Python-only** :

- kernel, agents, orchestration, métacognition, API de contrôle, plugins et
  tests du kernel : Python obligatoire ;
- web : JavaScript/TypeScript autorisés dans les couches web approuvées ;
- mobile : TypeScript, Dart, Kotlin, Swift ou Java autorisés dans les couches
  mobiles approuvées ;
- extension native : Rust/C/C++ autorisés dans une couche native isolée ;
- données et plateforme : SQL, PowerShell ou shell autorisés dans les couches
  dédiées ;
- aucun langage spécialisé ne peut remplacer le kernel Python.

L’installation du kernel nécessite uniquement Python et `pip`. Les outils
Node, Flutter, Android, Xcode ou Rust restent optionnels et propres au produit
qui les exige.

## Installation rapide sous Windows

```powershell
Expand-Archive `
  "$env:USERPROFILE\Downloads\ELMAN-OS-Foundation-Kit-v0.4.0.zip" `
  -DestinationPath "$env:USERPROFILE\Desktop"

Set-Location "$env:USERPROFILE\Desktop\elman-os-foundation-kit-v0.4.0"

py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip setuptools wheel
.\.venv\Scripts\python.exe -m pip install -e .

.\.venv\Scripts\python.exe -W error::ResourceWarning `
  -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m elman_os doctor
.\.venv\Scripts\python.exe -m elman_os ai-config
.\.venv\Scripts\python.exe -m elman_os ai-providers
.\.venv\Scripts\python.exe -m elman_os ai-audit
.\.venv\Scripts\python.exe -m elman_os ai-readiness
.\.venv\Scripts\python.exe -m elman_os release-check .
.\.venv\Scripts\python.exe -m elman_os agents
.\.venv\Scripts\python.exe -m elman_os plugins
.\.venv\Scripts\python.exe -m elman_os audit-stack .
```

## Planifier un SaaS

```powershell
.\.venv\Scripts\python.exe -m elman_os plan `
  --name "ELMAN Tasks" `
  --slug "elman-tasks" `
  --kind saas `
  --platform web `
  --feature "authentification" `
  --acceptance "Une tâche peut être créée et listée"
```

## Générer le premier starter

```powershell
.\.venv\Scripts\python.exe -m elman_os generate `
  --name "ELMAN Tasks" `
  --slug "elman-tasks" `
  --kind fullstack `
  --platform web `
  --platform android `
  --feature "authentification" `
  --acceptance "Le domaine est couvert par des tests" `
  --output generated
```

La commande écrit uniquement dans `generated\elman-tasks` et crée :

- contrat ELMAN du projet ;
- plan du pipeline ;
- domaine Python et repository SQLite ;
- API FastAPI optionnelle ;
- interface mobile Flet optionnelle ;
- tests `unittest` ;
- dossier Proof avec critères en attente.

## Démontrer et persister la boucle

```powershell
.\.venv\Scripts\python.exe -m elman_os demo `
  --pass-on 3 `
  --max-iterations 5 `
  --database ".elman\elman.db"

.\.venv\Scripts\python.exe -m elman_os runs `
  --database ".elman\elman.db"
```

## API de contrôle optionnelle

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[api]"
.\.venv\Scripts\python.exe -m elman_os serve
```

Endpoints initiaux :

- `GET /health`
- `GET /v1/agents`
- `POST /v1/plans`
- `POST /v1/projects`

## Structure

```text
elman-os-foundation-kit-v0.4.0/
├── CHANGELOG.md
├── MIGRATION-v0.2.1-to-v0.3.0.md
├── MIGRATION-v0.3.1-to-v0.4.0.md
├── RELEASE-CHECKSUMS.sha256
├── RELEASE-MANIFEST.json
├── config/
├── docs/
├── examples/
├── src/elman_os/
│   ├── api.py
│   ├── approvals.py
│   ├── audit.py
│   ├── catalog.py
│   ├── cli.py
│   ├── configuration.py
│   ├── execution.py
│   ├── generator.py
│   ├── governance.py
│   ├── metacognition.py
│   ├── openai_compatible.py
│   ├── persistence.py
│   ├── planning.py
│   ├── plugins.py
│   ├── provider.py
│   ├── registry.py
│   ├── release.py
│   ├── service.py
│   ├── technology_policy.py
│   └── workflow.py
├── tests/
└── pyproject.toml
```

Le contrat du fournisseur IA est détaillé dans
`docs/AI-PROVIDER-CONTRACT.md`. La configuration et les commandes PowerShell
sont détaillées dans `docs/AI-CONFIGURATION.md`.
Les garanties de timeout, retry et budget sont décrites dans
`docs/AI-RUNTIME-RESILIENCE.md`.
Le registre, la sélection et le fallback sont décrits dans
`docs/AI-PROVIDER-REGISTRY.md`.
L'adaptateur, son transport et sa configuration sont décrits dans
`docs/AI-OPENAI-COMPATIBLE.md`.
L'identité, l'autorisation et la trace signée sont décrites dans
`docs/AI-EXECUTION-AUDIT.md`.
La stabilisation, les quotas et la reprise persistante sont décrits dans
`docs/AI-KERNEL-STABILIZATION.md`.
Les critères de gel, d’intégrité et de revue finale sont décrits dans
`docs/RELEASE.md`.

## Limites connues

- les adaptateurs distants sont livrés, mais aucun endpoint réel, modèle réel,
  débit ou coût n'est validé par cette version stable ;
- le sink fichier est local et mono-machine ; la validation JWT/OIDC, la
  rotation des clés et un backend multi-instance restent à intégrer ;
- aucun catalogue monétaire de prix ni routage par coût ou qualité n'est livré ;
- le générateur produit un starter, pas une application métier finalisée ;
- SQLite couvre le MVP local ; PostgreSQL reste une cible d’adaptateur ;
- la sandbox actuelle protège les chemins, pas encore l’exécution de code non
  fiable dans un conteneur isolé ;
- FastAPI et Flet sont des extras optionnels non requis par le kernel ;
- un build iOS signé exige macOS et Xcode.

La publication stable requiert la fusion de cette promotion dans `main`, la
validation de la matrice CI multi-plateforme et la création contrôlée du tag `v0.4.0`.
