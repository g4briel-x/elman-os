# Journal des versions — ELMAN-OS Foundation Kit

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
