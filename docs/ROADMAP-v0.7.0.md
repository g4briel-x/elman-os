# ELMAN-OS v0.7.0 Roadmap

## Vision

ELMAN-OS v0.7.0 introduit une architecture d’orchestration multi-agent
capable de planifier, superviser, vérifier et mémoriser l’exécution
d’un projet logiciel piloté par intelligence artificielle.

Le système reste local-first, auditable, portable et soumis à une
approbation humaine explicite avant toute opération sensible.

## Objectifs

1. Définir un contrat commun pour tous les agents.
2. Construire un orchestrateur multi-agent.
3. Ajouter une supervision métacognitive indépendante.
4. Introduire une mémoire structurée du projet.
5. Ajouter un vérificateur final fail-closed.
6. Exposer plans, décisions et preuves dans ELMAN Studio.
7. Maintenir les tests et validations hors réseau.

## Architecture cible

### Orchestrateur

L’orchestrateur doit :

- analyser l’intention utilisateur ;
- produire un plan explicite ;
- sélectionner les agents appropriés ;
- gérer les dépendances entre étapes ;
- demander une approbation humaine ;
- suspendre une exécution incertaine ;
- produire un journal auditable.

### Agents spécialisés

Chaque agent possède :

- un identifiant stable ;
- un domaine de compétence ;
- un contrat d’entrée ;
- un contrat de sortie ;
- des capacités déclarées ;
- des permissions minimales ;
- des limites d’exécution ;
- un niveau de confiance ;
- des preuves associées au résultat.

### Vérificateur final

Le vérificateur contrôle :

- la conformité du résultat au plan ;
- l’intégrité des fichiers produits ;
- la présence des preuves requises ;
- les violations de politique ;
- les erreurs non résolues ;
- la cohérence entre décisions et résultats.

Un résultat non vérifié ne peut pas être marqué comme terminé.

## Couche métacognitive

### Superviseur métacognitif

Le superviseur détecte :

- les plans incomplets ;
- les contradictions ;
- les dépendances manquantes ;
- les boucles d’exécution ;
- les décisions insuffisamment justifiées ;
- les dérives par rapport à l’intention initiale.

### Agent réflexif

L’agent réflexif produit après chaque exécution :

- les éléments réussis ;
- les erreurs rencontrées ;
- les causes probables ;
- les hypothèses à vérifier ;
- les améliorations proposées.

Il ne modifie jamais automatiquement les politiques du système.

### Gestionnaire de mémoire

La mémoire du projet conserve :

- les décisions validées ;
- les contraintes ;
- les conventions ;
- les résultats de tests ;
- les migrations ;
- les incidents ;
- les sources de vérité ;
- les éléments devenus obsolètes.

Chaque entrée doit être traçable et associée à une origine.

### Agent d’apprentissage

L’agent d’apprentissage transforme les observations validées en
propositions d’amélioration.

Toute modification durable des règles ou comportements nécessite une
validation humaine explicite.

## Jalons

### Jalon 1 — Contrats multi-agents

- AgentDefinition ;
- AgentCapability ;
- AgentRequest ;
- AgentResponse ;
- registre local d’agents ;
- permissions et limites ;
- sérialisation déterministe ;
- validation stricte ;
- tests hors réseau.

### Jalon 2 — Planification et orchestration

- ExecutionPlan ;
- étapes et dépendances ;
- sélection déterministe des agents ;
- journal d’exécution ;
- approbations humaines ;
- reprise contrôlée après interruption.

États minimaux :

- pending ;
- approved ;
- running ;
- blocked ;
- failed ;
- completed.

### Jalon 3 — Supervision métacognitive

- superviseur métacognitif ;
- agent réflexif ;
- détection de boucle ;
- détection de contradiction ;
- rapport de confiance ;
- propositions non automatiques.

### Jalon 4 — Mémoire de projet

- stockage local structuré ;
- décisions immuables ;
- historique des révisions ;
- recherche par projet et exécution ;
- règles de rétention ;
- exclusion des secrets.

État : implémenté par le contrat `project_memory` et sa persistance SQLite
append-only. L’intégration à l’orchestrateur et à Studio reste couverte par les
jalons 5 et 6.

### Jalon 5 — Vérification finale

- validation des sorties ;
- validation des preuves ;
- contrôle d’intégrité ;
- comportement fail-closed ;
- refus des résultats incomplets ;
- rapport final signé.

État : implémenté par le contrat `final_verification`. Neuf portes obligatoires
contrôlent le plan, le journal, les sorties, les artefacts, les preuves, les
politiques, les erreurs, les décisions et la supervision métacognitive. Chaque
acceptation ou rejet produit un rapport HMAC-SHA-256 vérifiable. L’exposition
du rapport et de ses preuves dans Studio reste couverte par le jalon 6.

### Jalon 6 — Intégration ELMAN Studio

- visualisation du plan ;
- agents sélectionnés ;
- progression des étapes ;
- décisions et preuves ;
- erreurs et blocages ;
- rapports métacognitifs ;
- approbations explicites.

État : implémenté par la projection en lecture seule `studio_v07`. Studio expose
le plan, les agents, la progression, les approbations, la mémoire, les preuves,
les erreurs, la supervision et les neuf portes du rapport final. La clôture
reste fail-closed tant que la signature HMAC du rapport n'est pas vérifiée et
que toutes les approbations affichées ne sont pas accordées. Le basculement de
l'entrée Studio stable et la régénération des métadonnées de distribution sont
réservés au jalon 7.

### Jalon 7 — Stabilisation v0.7.0

- migration depuis v0.6.0 ;
- compatibilité Windows, Linux et macOS ;
- Python 3.11, 3.12 et 3.13 ;
- installation hors réseau ;
- archive déterministe ;
- audit technologique ;
- validation reproductible.

## Premier incrément technique

Le premier développement de v0.7.0 sera le contrat multi-agent du kernel.

Périmètre :

- contrats Python typés ;
- registre local en mémoire ;
- sérialisation déterministe ;
- validation stricte des entrées et sorties ;
- permissions explicites ;
- aucune connexion distante ;
- aucune exécution de code généré ;
- aucune modification automatique du projet ;
- tests entièrement hors réseau.

## Contraintes de sécurité

- approbation humaine avant toute opération sensible ;
- permissions minimales ;
- aucune suppression destructive implicite ;
- aucune exposition de secrets ;
- aucune modification silencieuse de la mémoire ;
- journalisation des décisions ;
- comportement fail-closed en cas d’incertitude ;
- séparation entre observation, proposition et exécution.

## Critères de réussite

La version v0.7.0 pourra être considérée comme candidate lorsque :

- les contrats multi-agents sont stables ;
- l’orchestrateur fonctionne hors réseau ;
- la supervision métacognitive est testée ;
- la mémoire est traçable ;
- le vérificateur bloque les sorties invalides ;
- ELMAN Studio expose les plans et preuves ;
- les migrations sont documentées ;
- tous les tests passent sur les plateformes supportées ;
- la distribution est déterministe et reproductible.

## Hors périmètre

- autonomie complète sans validation humaine ;
- apprentissage auto-appliqué ;
- exécution distante en production ;
- facturation SaaS ;
- marketplace publique d’agents ;
- déploiement Kubernetes automatique ;
- utilisation de véritables identifiants IA dans les tests.
