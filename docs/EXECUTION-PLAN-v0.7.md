# Execution Plan ELMAN-OS v0.7

## Statut

Premier incrément du Jalon 2 — Planification et orchestration.

Ce module définit un graphe d’exécution strict, immuable et déterministe.
Il prépare l’orchestration sans exécuter d’agent, sans écrire dans un projet
utilisateur et sans établir de connexion réseau.

## Fichiers

| Fichier | Rôle |
|---|---|
| `src/elman_os/execution_plan.py` | Définit les étapes, plans, états, dépendances, approbations et l’affectation déterministe des agents. |
| `tests/test_execution_plan.py` | Vérifie les contrats, cycles, transitions, sérialisation et affectations. |
| `docs/EXECUTION-PLAN-v0.7.md` | Documente l’API, les invariants et le périmètre. |

## Contrats

### `ExecutionStep`

Une étape contient :

- un identifiant stable ;
- un titre et un objectif ;
- une capacité requise ;
- des dépendances explicites ;
- des permissions minimales ;
- un agent éventuellement affecté ;
- une exigence éventuelle d’approbation humaine ;
- un état ;
- des métadonnées JSON immuables.

Les états d’étape sont :

- `pending` ;
- `approved` ;
- `running` ;
- `blocked` ;
- `failed` ;
- `completed`.

Une étape active ou terminée exige un agent affecté. Une étape marquée comme
soumise à approbation ne peut devenir approuvée, active ou terminée sans
référence d’approbation.

### `ExecutionPlan`

Un plan contient :

- un identifiant de plan ;
- un identifiant de projet ;
- un objectif ;
- l’agent créateur ;
- un ensemble non vide d’étapes ;
- un état global ;
- une politique d’approbation humaine ;
- une référence d’approbation ;
- des métadonnées JSON immuables.

Les états du plan sont :

- `pending` ;
- `approved` ;
- `running` ;
- `blocked` ;
- `failed` ;
- `completed`.

## Graphe de dépendances

La construction d’un plan refuse :

- les identifiants d’étape dupliqués ;
- les dépendances inconnues ;
- les dépendances vers soi-même ;
- les cycles directs ou indirects.

L’ordre topologique utilise un départage lexical stable. Deux plans contenant
les mêmes étapes dans un ordre d’entrée différent produisent le même ordre et
le même JSON canonique.

## Cohérence des états

Le plan applique notamment les invariants suivants :

- un plan `pending` ne contient pas d’étape active ou terminée ;
- un plan `completed` exige toutes les étapes `completed` ;
- un plan `failed` exige au moins une étape `failed` ;
- un plan `blocked` exige au moins une étape `blocked` ;
- une étape `running` ou `completed` exige que ses dépendances soient
  `completed`.

## Approbation humaine

`ExecutionPlan.approve(reference)` retourne un nouveau plan immuable marqué
`approved`. Le plan original reste inchangé.

Une approbation du plan peut être utilisée pour satisfaire une capacité
d’agent déclarée comme nécessitant une approbation humaine. La référence est
alors propagée dans l’étape affectée afin de préserver la traçabilité.

## Affectation des agents

`ExecutionPlan.bind_agents(registry)` utilise le `AgentRegistry` strict de
v0.7.

Pour chaque étape :

- la capacité doit être disponible ;
- les permissions requises doivent être couvertes ;
- une capacité sensible exige une approbation ;
- une affectation explicite doit être valide ;
- sans affectation explicite, le registre sélectionne déterministement le
  premier agent admissible selon l’ordre stable des identifiants.

La méthode retourne un nouveau plan. Elle n’exécute aucun agent.

## Étapes prêtes

`ready_steps()` retourne le front d’exécution structurel :

- étapes `pending` ou `approved` ;
- toutes les dépendances déjà `completed` ;
- ordre topologique déterministe.

## Sérialisation

`ExecutionStep` et `ExecutionPlan` fournissent :

- `to_dict()` ;
- `to_json()` ;
- `from_dict()` ;
- `from_json()`.

Le JSON est canonique, trié, sans nombres non finis et indépendant de l’ordre
d’entrée des étapes.

## Garanties

- contrats immuables ;
- validation fail-closed ;
- détection déterministe des cycles ;
- sélection déterministe des agents ;
- approbations explicites ;
- métadonnées JSON immuables ;
- aucune connexion réseau ;
- aucune exécution de code ;
- aucune mutation automatique du projet ;
- aucun changement de version ou de release.

## Hors périmètre

Cet incrément ne fournit pas encore :

- le moteur d’exécution ;
- le journal d’exécution persistant ;
- la reprise après interruption ;
- les délais et budgets ;
- les tentatives et politiques de retry ;
- l’annulation ;
- la supervision métacognitive ;
- la mémoire de projet ;
- l’intégration ELMAN Studio.
