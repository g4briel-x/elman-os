# Journal des versions — ELMAN-OS Foundation Kit

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
