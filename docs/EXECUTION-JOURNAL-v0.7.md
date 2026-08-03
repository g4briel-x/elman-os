# Execution Journal ELMAN-OS v0.7

## Statut

Deuxième incrément du Jalon 2 — Planification et orchestration.

Ce module ajoute un journal d’exécution append-only, hors réseau et
déterministe. Il enregistre les événements d’un plan sans exécuter d’agent,
sans reprendre automatiquement une exécution et sans écrire dans un projet
utilisateur.

## Fichiers

| Fichier | Rôle |
|---|---|
| `src/elman_os/execution_journal.py` | Définit les événements, le journal append-only, la chaîne SHA-256, le sceau et la relecture JSONL. |
| `tests/test_execution_journal.py` | Vérifie les séquences, horodatages, liens, altérations, suppressions et relectures. |
| `docs/EXECUTION-JOURNAL-v0.7.md` | Documente le format, les invariants, les garanties et les limites. |

## Contrats

### `ExecutionEventType`

Les types d’événements sont séparés entre événements de plan et événements
d’étape :

- `plan.created` ;
- `plan.approved` ;
- `plan.started` ;
- `plan.blocked` ;
- `plan.failed` ;
- `plan.completed` ;
- `step.ready` ;
- `step.assigned` ;
- `step.approved` ;
- `step.started` ;
- `step.blocked` ;
- `step.failed` ;
- `step.completed`.

### `ExecutionEvent`

Chaque événement contient :

- un numéro de séquence positif ;
- un type d’événement ;
- un horodatage UTC explicite ;
- un `plan_id` ;
- un `step_id` pour les événements d’étape ;
- un `agent_id` pour les événements d’exécution concernés ;
- une charge utile JSON immuable ;
- le hash de l’événement précédent ;
- son propre hash SHA-256.

Les horodatages sont normalisés vers :

```text
YYYY-MM-DDTHH:MM:SS.ffffffZ
```

Les dates sans fuseau, non UTC ou dépourvues du suffixe `Z` sont refusées.

## Chaîne d’intégrité

Le premier événement référence le hash de genèse :

```text
0000000000000000000000000000000000000000000000000000000000000000
```

Chaque hash d’événement est calculé sur le JSON canonique de :

- son numéro de séquence ;
- son type ;
- son horodatage ;
- ses identifiants ;
- sa charge utile ;
- le hash précédent.

Toute modification d’un champ invalide le hash de l’événement ou la chaîne
des événements suivants.

## Journal append-only

`ExecutionJournal` autorise uniquement l’ajout en fin de journal.

Les invariants principaux sont :

- le premier événement est obligatoirement `plan.created` ;
- les séquences commencent à 1 et progressent de 1 ;
- tous les événements appartiennent au même plan ;
- les horodatages ne reculent jamais ;
- un second `plan.created` est refusé ;
- aucun événement ne peut suivre `plan.failed` ou `plan.completed` ;
- le hash précédent doit correspondre exactement à la tête du journal.

L’API publique expose les événements sous forme de tuple. Aucun mécanisme de
suppression ou de remplacement n’est fourni.

## Sceau du journal

Le format JSONL se termine par un `JournalSeal` contenant :

- la version du format ;
- l’algorithme `sha256` ;
- le `plan_id` ;
- le nombre d’événements ;
- le hash de tête ;
- le hash global du journal.

Ce sceau permet de détecter :

- la modification d’un événement ;
- la suppression d’un événement intermédiaire ;
- la suppression du dernier événement ;
- la suppression du sceau ;
- une discordance avec un nombre, un plan ou un hash attendu.

## Relecture et validation

`ExecutionJournal.from_jsonl()` :

1. analyse chaque ligne JSON ;
2. exige un sceau final ;
3. reconstruit le journal dans l’ordre ;
4. vérifie chaque hash ;
5. vérifie les séquences ;
6. vérifie la chaîne ;
7. vérifie les horodatages ;
8. compare le journal reconstruit au sceau.

Des attentes externes peuvent aussi être fournies :

- `expected_plan_id` ;
- `expected_event_count` ;
- `expected_head_hash` ;
- `expected_journal_hash`.

`replay()` valide le journal puis retourne les événements dans leur ordre
canonique.

## Format JSONL

Chaque ligne avant la dernière est un événement canonique. La dernière ligne
est le sceau.

Exemple conceptuel :

```json
{"record_type":"event","sequence":1,"event_type":"plan.created"}
{"record_type":"event","sequence":2,"event_type":"step.ready"}
{"record_type":"seal","event_count":2,"algorithm":"sha256"}
```

Le format réel contient tous les champs contractuels et les hashes complets.

## Garanties

- append-only par API ;
- séquences strictement monotones ;
- horodatages UTC explicites ;
- liens plan, étape et agent validés ;
- charges utiles JSON immuables ;
- JSON canonique ;
- chaîne SHA-256 ;
- sceau final ;
- relecture entièrement hors réseau ;
- aucune exécution d’agent ;
- aucune mutation automatique de projet ;
- aucun changement de version ou de release.

## Limite cryptographique

SHA-256 fournit une preuve d’intégrité, pas une preuve d’authenticité contre
un attaquant capable de réécrire l’ensemble du journal et de recalculer tous
les hashes.

Une authentification HMAC ou une signature numérique pourra être ajoutée dans
un incrément ultérieur. Le présent incrément détecte les altérations et
suppressions lorsque le sceau ou les valeurs attendues proviennent d’une
source de confiance.

## Hors périmètre

Cet incrément ne fournit pas encore :

- la persistance atomique sur disque ;
- le verrouillage multi-processus ;
- la reprise après interruption ;
- les checkpoints d’état ;
- l’authentification HMAC ;
- la signature numérique ;
- l’exécution des agents ;
- la supervision métacognitive ;
- l’intégration ELMAN Studio.
