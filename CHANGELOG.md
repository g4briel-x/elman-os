# Journal des versions — ELMAN-OS Foundation Kit

## v0.7.0-rc.1 — 2026-08-08

### Ajouté

- contrats multi-agents immuables, plans déterministes et journal hashé ;
- orchestration d’artefacts transactionnelle avec checkpoints et reprise contrôlée ;
- supervision métacognitive indépendante et findings liés aux preuves ;
- mémoire de projet append-only, révisions, provenance, recherche et rétention ;
- vérificateur final fail-closed à neuf portes avec rapport signé HMAC ;
- tableau de supervision ELMAN Studio v0.7 en lecture seule ;
- commande officielle `elman-os studio-oversight`.

### Renforcé

- refus de tout fichier distribué absent de l’inventaire SHA-256 ;
- matrice CI activée pour les PR vers `develop/v0.7.0` et `main` ;
- archive déterministe construite deux fois et comparée bit à bit ;
- approbation finale et déploiement production maintenus fermés.

### Vérification locale

- 1 923 tests unitaires hors réseau attendus ;
- roue `0.7.0rc1` installée dans un environnement isolé ;
- release-check, audit technologique et inventaire complet ;
- validation multi-plateforme requise sur GitHub avant le tag RC.

## v0.6.0 — 2026-08-03

### Publication stable

- promotion sans changement fonctionnel de `v0.6.0-rc.2` ;
- ELMAN Studio phases 1 à 3 déclaré stable ;
- inventaire SHA-256 et exclusions de tooling stabilisés ;
- contrat transactionnel non suppressif conservé ;
- documentation et migration stable ajoutées.

### Vérification

- 278 tests unitaires réussis hors réseau ;
- 10 tests de finalisation stable réussis ;
- matrice CI Windows/macOS/Linux sur Python 3.11 à 3.13 ;
- roue `0.6.0` installée hors réseau ;
- archive ZIP déterministe et reproductible ;
- distribution finale approuvée, déploiement production toujours fermé.


## v0.6.0-rc.2 — 2026-08-03

### Renforcé

- exclusion des environnements virtuels, caches, IDE et dépendances locales ;
- inventaire SHA-256 limité aux fichiers effectivement distribuables ;
- contrat `__aexit__` transactionnel explicitement non suppressif ;
- propagation garantie des erreurs de quota, d’intégrité et d’audit.

### Vérification

- 278 tests unitaires réussis hors réseau ;
- `release-check` et audit technologique réussis ;
- roue `0.6.0rc2` installée hors réseau ;
- archive ZIP déterministe et reproductible ;
- gates de production maintenues fermées.


## v0.6.0-rc.1 — 2026-08-02

### Ajouté

- ELMAN Studio pour planifier et générer un starter sous gate humaine ;
- historique SQLite strictement en lecture seule ;
- workflows déterministes locaux avec progression et persistance ;
- consultation des verdicts, preuves et décisions métacognitives ;
- protection de `.elman/` et `generated/`.

### Renforcé

- approbation d’exécution à usage unique et réinitialisation fail-closed ;
- exécution Studio hors du thread de l’interface ;
- validation des limites et identifiants avant création de la base ;
- aucune activation de fournisseur distant ou de déploiement.

### Vérification

- 278 tests unitaires réussis hors réseau ;
- validation visuelle Studio sur Windows ;
- matrice CI Windows/macOS/Linux sur Python 3.11 à 3.13 ;
- roue `0.6.0rc1` et archive ZIP déterministe validées hors réseau ;
- gates de production maintenues fermées.


## v0.5.1 — 2026-08-01

### Corrigé

- actualisation de l’empreinte SHA-256 de `.gitignore` ;
- alignement de l’inventaire de release avec le contenu réellement livré ;
- validation d’intégrité reproductible sur Windows, macOS et Linux ;
- conservation complète des API, contrats et données de `v0.5.0`.

### Vérification

- 259 tests unitaires réussis hors réseau ;
- contrôle `release-check` réussi ;
- roue Python et archive ZIP déterministe validées sans accès réseau ;
- aucun credential réel, appel réseau ou appel payant.

## v0.5.0 — 2026-07-30

### Ajouté

- validation JWT/OIDC hors réseau avec politique stricte et vérification HMAC ;
- persistance transactionnelle SQLite isolée par tenant et contrôle optimiste ;
- quotas atomiques et audit HMAC persistants partagés entre instances ;
- runtime de production composant authentification, autorisation, quotas et audit ;
- route FastAPI authentifiée `POST /v1/ai/generate` ;
- migration documentée depuis `v0.4.0`.

### Vérification

- 259 tests unitaires réussis hors réseau ;
- roue Python construite puis installée dans un environnement neuf sans index ;
- version importée, manifeste, archive déterministe et inventaire SHA-256 validés ;
- aucun credential réel, appel réseau ou appel payant ;
- gates de déploiement autonome maintenues fermées.

## v0.4.0 — 2026-07-30

### Publication stable

- promotion de la release candidate validée vers la version stable ;
- installation réelle de la roue Python validée en environnement neuf ;
- matrice CI réussie sur neuf combinaisons OS/Python ;
- métadonnées, manifeste et documentation promus vers v0.4.0 ;
- frontières de sécurité et approbation de production maintenues.

## v0.4.0-rc.1 — 2026-07-30

### Ajouté

- commande `elman-os release-check` entièrement hors réseau ;
- cohérence bloquante entre version Python, runtime et manifeste ;
- inventaire SHA-256 de tous les fichiers livrés ;
- validation des chemins pour Windows, macOS et Linux ;
- détection des fichiers de secrets, credentials et clés privées ;
- constructeur d’archive ZIP déterministe fondé sur la bibliothèque standard ;
- matrice GitHub Actions pour trois systèmes et Python 3.11, 3.12 et 3.13 ;
- documentation de validation et vingt tests de release.

### Renforcé

- réservations de quota identifiées et non rejouables ;
- empreintes de quota isolées par tenant et par sujet ;
- gates de production explicitement fermées dans le manifeste ;
- tests de noms de chemins interdits indépendants des normalisations de l’hôte ;
- validation pure des noms bruts avant leur création sur le système de fichiers ;
- échec fermé sur inventaire absent, modifié, malformé ou non portable.

### Vérification

- 180 tests du Kernel réussis hors réseau ;
- compilation Python et audit technologique réussis ;
- installation editable et archive extraite validées ;
- aucun credential réel, appel IA réseau ou appel payant ;
- validation Windows/macOS déléguée à la matrice CI et à la validation manuelle.

## v0.4.0 alpha 7 — 2026-07-30

### Ajouté

- prévalidation de compatibilité entre configuration et registre ;
- quotas atomiques de requêtes, tokens et concurrence par identité HMAC ;
- refus des dépassements avant tout appel fournisseur ;
- journal d'audit JSONL append-only avec synchronisation durable ;
- reprise d'une chaîne d'audit persistante après redémarrage ;
- runtime `StabilizedAIRuntime` composant toutes les frontières v0.4 ;
- commande de diagnostic `elman-os ai-readiness` ;
- guide de migration depuis v0.3.1 et documentation de stabilisation ;
- vingt-quatre nouveaux tests.

### Sécurité

- aucun identifiant brut dans les compteurs de quota ;
- annulations et échecs libèrent toujours la concurrence réservée ;
- dépassements de quota audités avec un code portable minimal ;
- journal borné, liens symboliques refusés et altérations détectées à la reprise ;
- prévalidation sans factory, réseau, clé réelle ni payload utilisateur.

### Vérification

- 160 tests du Kernel réussis ;
- pipeline configuration → registre → quota → audit → exécution validé ;
- concurrence, annulation, reprise et altération testées hors réseau ;
- compilation Python, CLI, manifestes JSON et audit technologique réussis ;
- aucune clé réelle, aucun appel réseau et aucun appel payant.

## v0.4.0 alpha 6 — 2026-07-30

### Ajouté

- principal d'exécution typé et méthodes d'authentification explicites ;
- autorisation obligatoire par rôle `ai.execute` et motif contrôlé ;
- enveloppe `AuditedAIExecutor` autour du runtime résilient ;
- événements `started`, `succeeded`, `failed`, `denied` et `cancelled` ;
- empreintes HMAC séparées pour principal, tenant et requête ;
- signatures HMAC-SHA-256 chaînées et vérification d'intégrité ;
- commande de diagnostic `elman-os ai-audit` ;
- dix-huit nouveaux tests d'identité, confidentialité et intégrité.

### Sécurité

- aucune trace de prompt, réponse, secret, metadata libre ou request ID distant ;
- appel fournisseur bloqué avant authentification ou si l'audit initial échoue ;
- annulations propagées après production d'un événement minimal ;
- clé de signature masquée dans `str` et `repr` ;
- identifiants fournisseur ou modèle non sûrs remplacés par une empreinte.

### Vérification

- 136 tests du Kernel réussis ;
- altération et suppression d'événements détectées ;
- compilation Python, CLI et audit technologique réussis ;
- aucune clé réelle, aucun appel réseau et aucun appel payant.

## v0.4.0 alpha 5 — 2026-07-30

### Ajouté

- adaptateur `OpenAICompatibleProvider` sans dépendance SDK ;
- profils intégrés `openai` et `openai-compatible` ;
- transport HTTP asynchrone injectable et implémentation standard `urllib` ;
- traduction du contrat ELMAN vers l'endpoint `chat/completions` ;
- conversion typée des réponses, usages et raisons de terminaison ;
- injection de transports simulés dans le registre ;
- dix-huit nouveaux tests d'adaptateur et de pipeline hors réseau.

### Sécurité

- clé API masquée dans les représentations et transmise seulement à la frontière HTTP ;
- corps d'erreur fournisseur ignoré dans les messages portables ;
- erreurs 401, 403, 404, 408, 429, 5xx et réseau classifiées explicitement ;
- `Retry-After` numérique accepté uniquement entre 0 et 300 secondes ;
- URL compatible personnalisée soumise à la validation HTTPS existante ;
- JSON, usages et structures de réponse invalides rejetés sans fuite de payload.

### Vérification

- 118 tests du Kernel réussis ;
- pipeline registre → adaptateur → exécuteur testé avec transport injecté ;
- compilation Python, CLI et audit technologique réussis ;
- archive validée depuis une extraction neuve ;
- aucune clé réelle, aucun appel réseau et aucun appel payant.

## v0.4.0 alpha 4 — 2026-07-30

### Ajouté

- registre `ProviderRegistry` indépendant des SDK fournisseurs ;
- enregistrements immuables associant descriptor et factory ;
- sélection du fournisseur et du modèle depuis `ProviderSettings` ;
- contrôle préalable des modèles et capacités obligatoires ;
- fallback déterministe explicitement activable et entièrement traçable ;
- runtime configuré reliant registre, sélection et exécuteur résilient ;
- commande `elman-os ai-providers` sans contact réseau ;
- documentation dédiée et dix-huit tests de registre et de pipeline.

### Sécurité

- un fournisseur inconnu ou indisponible échoue par défaut ;
- le fallback ne masque jamais une incompatibilité de modèle ou de capacité ;
- l'instance créée doit respecter le protocole et son descriptor enregistré ;
- les résumés de sélection n'exposent aucune clé ;
- le fallback supprime la clé, l'URL et l'authentification distantes.

### Vérification

- 100 tests du Kernel réussis ;
- pipeline configuration → sélection → exécution testé de bout en bout ;
- compilation Python et audit technologique réussis ;
- installation et CLI testées depuis une extraction neuve ;
- aucun adaptateur distant, appel réseau ou appel payant.

## v0.4.0 alpha 3 — 2026-07-30

### Ajouté

- exécuteur `ResilientAIExecutor` indépendant des fournisseurs ;
- délais globaux réels avec normalisation des timeouts ;
- retries exponentiels appliqués uniquement aux erreurs temporaires ;
- prise en charge bornée de `retry_after_seconds` ;
- budgets partagés d'appels fournisseur, de tokens et de durée ;
- validation de l'identité des réponses et de la limite de sortie ;
- paramètres de résilience chargés depuis l'environnement ;
- dix-huit nouveaux tests de runtime et de configuration.

### Sécurité

- une annulation n'est jamais convertie en retry ;
- une erreur non temporaire échoue immédiatement ;
- un budget de tokens insuffisant bloque l'appel avant le fournisseur ;
- toutes les tentatives, attentes et durées sont plafonnées ;
- aucune dépendance fournisseur, clé réelle ou journalisation de payload.

### Vérification

- 82 tests du Kernel réussis ;
- compilation Python réussie ;
- installation et CLI testées depuis une extraction neuve ;
- aucun appel réseau ou payant.

## v0.4.0 alpha 2 — 2026-07-30

### Ajouté

- chargement du fournisseur, du modèle, de l'authentification, de l'URL et des
  limites depuis les variables d'environnement ;
- type `SecretValue` masquant les clés dans `str`, `repr` et les diagnostics ;
- validation stricte des identifiants, limites, modes d'authentification et URL ;
- commande `elman-os ai-config` sans divulgation de secret ;
- exemple de configuration sans clé réelle et documentation PowerShell ;
- onze tests de configuration sécurisée.

### Sécurité

- un fournisseur distant exige une clé par défaut ;
- une clé est refusée lorsque l'authentification est désactivée ;
- HTTPS est obligatoire, sauf pour une adresse locale explicite ;
- aucune valeur invalide ou secrète n'est répétée dans un message d'erreur.

### Vérification

- 64 tests du Kernel réussis ;
- compilation Python réussie ;
- commande de diagnostic testée avec et sans clé fictive ;
- aucun appel réseau ou payant.

## v0.4.0 alpha 1 — 2026-07-29

### Ajouté

- contrat générique `AIProvider` sans dépendance fournisseur ;
- requêtes, réponses, messages, capacités et consommations typés ;
- erreurs portables et indications de retry explicites ;
- fournisseur déterministe sans réseau pour les tests ;
- huit tests de contrat et documentation dédiée.

### Limites

- aucun adaptateur OpenAI, Anthropic, Google ou local n'est encore inclus ;
- aucune clé API n'est lue ;
- configuration, retries bornés, budgets et routage appartiennent aux
  prochains lots v0.4.

### Vérification

- 53 tests du Kernel réussis ;
- compilation Python réussie ;
- audit technologique inchangé ;
- aucun appel réseau ou payant.

## v0.3.1 — 2026-07-29

Correctif de compatibilité Windows et Python 3.13 fondé sur l’exécution réelle
des 44 tests du Foundation Kit v0.3.0.

### Corrigé

- fermeture explicite de chaque connexion SQLite après transaction afin de
  libérer immédiatement `elman.db` sous Windows ;
- normalisation POSIX des chemins exposés par l’audit technologique et les
  plugins, indépendamment du système d’exploitation ;
- comparaison canonique des chemins de génération pour accepter les alias
  Windows 8.3 tels que `SEPICT~1` sans affaiblir la protection anti-traversal ;
- fermeture déterministe des repositories SQLite dans les starters générés.

### Vérification

- ajout d’un test de non-régression contrôlant la fermeture réelle des
  connexions ;
- suite portée à 45 tests du kernel ;
- 2 tests du starter généré conservés ;
- politique technologique et limites de sécurité inchangées.

## v0.3.0 — 2026-07-29

Cette version consolide le Foundation Kit v0.2.1 et le Kernel MVP v0.3.0 dans
un seul socle de référence.

### Ajouté

- planification déterministe des pipelines SaaS, mobile et full-stack ;
- générateur sécurisé de starters avec dossier Proof ;
- persistance locale SQLite des exécutions et approbations ;
- approbations humaines indépendantes avec refus de l’auto-approbation ;
- API FastAPI optionnelle de contrôle ;
- services de composition pour planifier et générer ;
- plugins `blueprint_validator` et `technology_policy_auditor` ;
- audit technologique par couche ;
- documentation des rôles de fichiers et procédure de migration.

### Renforcé

- arrêt métacognitif sur succès, limite, budget, durée, stagnation, échec
  répété, finding critique ou blocage humain ;
- masquage des secrets dans la mémoire et les demandes d’approbation ;
- blocage du path traversal et de l’écrasement silencieux d’un projet ;
- politique Python-first autorisant les langages spécialisés uniquement dans
  leurs couches approuvées ;
- séparation entre permission technique et approbation humaine.

### Conservé

- 1 orchestrateur, 15 spécialistes et 1 vérificateur final ;
- 4 agents métacognitifs internes ;
- installation du noyau avec Python et `pip` ;
- compatibilité des imports `elman_os` et de la commande `elman-os`.

### Limites

- aucun fournisseur IA réel n’est encore connecté ;
- le générateur livre un starter, pas une application métier finalisée ;
- la sandbox protège les chemins mais n’isole pas encore l’exécution ;
- PostgreSQL, ELMAN Studio et les builds mobiles signés restent à construire.

## v0.2.1

- fondation agentique à 21 rôles ;
- boucle métacognitive bornée ;
- premiers plugins internes ;
- politique Python-first initiale.
