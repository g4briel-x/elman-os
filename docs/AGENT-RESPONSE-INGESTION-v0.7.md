# Agent Response Ingestion ELMAN-OS v0.7

## Statut

Septième incrément du Jalon 2 — Planification et orchestration.

Ce module consomme de manière contrôlée un `AgentResponse` lié à un
`StepDispatchResult`. Il transforme le résultat déclaré par l’agent en
transition vérifiable du plan et du journal, sans exécuter le contenu produit
et sans intégrer automatiquement des fichiers dans le projet utilisateur.

## Fichiers

| Fichier | Rôle |
|---|---|
| `src/elman_os/agent_response_ingestion.py` | Définit la requête, l’ingestion, le résultat, les transitions et les contrôles d’idempotence. |
| `tests/test_agent_response_ingestion.py` | Vérifie les statuts, frontières, conflits, rejeux, hashes, événements et sérialisations. |
| `docs/AGENT-RESPONSE-INGESTION-v0.7.md` | Documente les contrats, transitions, garanties et limites. |

## Contrats

### `AgentResponseIngestionRequest`

La requête contient :

- un `ingestion_id` ;
- l’identifiant du dispatch ;
- le hash du `StepDispatchResult` ;
- le `plan_id` ;
- l’étape concernée ;
- l’identifiant exact du `AgentRequest` ;
- l’agent attendu ;
- le statut déclaré par l’agent ;
- le hash SHA-256 du `AgentResponse` ;
- l’horodatage UTC de réception ;
- le hash de l’état du plan ;
- le nombre d’événements du journal ;
- le hash de tête du journal ;
- le hash global du journal ;
- un hash SHA-256 de la requête d’ingestion.

`from_dispatch_result()` construit cette frontière à partir d’un
`StepDispatchResult` vérifié et du `AgentResponse` reçu.

La construction refuse immédiatement :

- un `request_id` différent de celui du `AgentRequest` dispatché ;
- un `agent_id` différent de l’agent destinataire ;
- un résultat de dispatch ou une réponse mal typée ;
- un horodatage non UTC.

Lorsque aucun identifiant n’est fourni, `ingestion_id` est dérivé
déterministement du contenu de la frontière et du hash de la réponse.

### `AgentResponseIngestion`

L’ingestion associe exactement :

- une `AgentResponseIngestionRequest` ;
- le `StepDispatchResult` référencé ;
- le `AgentResponse` reçu.

La construction vérifie :

- tous les hashes ;
- le dispatch ;
- le plan ;
- l’étape ;
- le `AgentRequest.request_id` ;
- l’agent ;
- le statut ;
- le contenu complet de la réponse.

### `AgentResponseIngestionResult`

Le résultat contient :

- le statut `ingested` ou `already-ingested` ;
- toutes les références du dispatch et de la réponse ;
- le `AgentResponse` immuable ;
- les séquences d’événements ajoutées ;
- les hashes du plan avant et après ;
- les compteurs et hashes du journal avant et après ;
- le plan mis à jour ;
- les événements mis à jour ;
- un hash SHA-256 du résultat.

## Validation de la frontière

Avant une première ingestion, l’état courant doit correspondre exactement à
la frontière capturée :

- même `plan_id` ;
- même JSON canonique du plan ;
- même nombre d’événements ;
- même hash de tête ;
- même hash global du journal ;
- même plan et même journal que le `StepDispatchResult`.

Toute progression, divergence ou modification est refusée.

## Validation de l’étape et de l’agent

L’ingestion exige :

- un plan en statut `RUNNING` ;
- une étape en statut `RUNNING` ;
- la même étape que le dispatch ;
- le même agent que le dispatch ;
- un `request_id` égal au `AgentRequest.request_id` ;
- un hash de réponse exact.

Une réponse d’un autre agent ou liée à une autre demande est refusée.

## Mapping des statuts

### Réponse `succeeded`

L’étape passe à :

```text
COMPLETED
```

Le journal reçoit :

```text
step.completed
```

Lorsque toutes les étapes sont terminées, le plan passe à `COMPLETED` et le
journal reçoit aussi :

```text
plan.completed
```

Lorsque d’autres étapes restent à traiter, le plan demeure `RUNNING`.

### Réponse `blocked`

L’étape passe à `BLOCKED`, le plan passe à `BLOCKED` et le journal reçoit :

```text
step.blocked
plan.blocked
```

### Réponse `failed`

L’étape passe à `FAILED`, le plan passe à `FAILED` et le journal reçoit :

```text
step.failed
plan.failed
```

## Contenu de la réponse

Les champs du `AgentResponse` sont conservés dans le résultat :

- `summary` ;
- `outputs` ;
- `evidence` ;
- `warnings` ;
- `errors` ;
- `confidence` ;
- `next_handoff`.

Les `outputs` sont uniquement enregistrés comme données déclaratives. Ils ne
sont pas :

- exécutés ;
- copiés ;
- écrits sur disque ;
- fusionnés dans le projet ;
- interprétés comme commandes ;
- transmis automatiquement à un autre agent.

## Événements du journal

Chaque événement d’ingestion porte un payload de traçabilité contenant :

- `agent_response_ingestion_id` ;
- `agent_response_ingestion_request_hash` ;
- `agent_response_hash` ;
- `agent_response_request_id` ;
- `agent_response_status` ;
- `step_dispatch_id` ;
- `step_dispatch_result_hash` ;
- `agent_response_step_id` ;
- `agent_response_agent_id`.

Tous les événements utilisent l’horodatage UTC de réception et préservent la
chaîne SHA-256 du journal.

## Atomicité en mémoire

Le module travaille sur :

- un nouveau `ExecutionPlan` immuable ;
- une copie reconstruite du `ExecutionJournal`.

Avant le retour, un `ExecutionCheckpoint` est capturé pour vérifier la
compatibilité du plan et du journal produits.

En cas d’échec :

- le plan d’entrée reste inchangé ;
- le journal d’entrée reste inchangé ;
- aucun état partiel n’est exposé.

## Idempotence

Le journal est inspecté avant toute nouvelle transition.

Une réponse est reconnue comme déjà ingérée uniquement lorsque :

- le même hash de requête est présent ;
- les marqueurs commencent exactement après la frontière capturée ;
- le nombre d’événements est exact ;
- les séquences sont contiguës ;
- les types d’événements sont exacts ;
- tous les champs du payload correspondent ;
- le plan correspond exactement au résultat attendu ;
- l’étape et l’agent correspondent ;
- aucun événement ultérieur n’a dépassé la frontière de rejeu.

Le résultat devient alors `already-ingested` et aucun événement n’est ajouté.

## Détection des conflits

Le module refuse notamment :

- un même `ingestion_id` associé à un autre hash de requête ;
- plusieurs hashes de réponse pour le même `AgentRequest` ;
- des marqueurs incomplets ou dupliqués ;
- des marqueurs non contigus ;
- un journal ayant progressé au-delà du rejeu exact ;
- un autre type d’événement ;
- une autre étape ;
- un autre agent ;
- une modification du plan ou du journal ;
- une régression d’horodatage ;
- une réponse dont le contenu ne correspond pas au hash déclaré.

## Intégrité

Les artefacts exposent :

- `AgentResponseIngestionRequest.request_hash` ;
- `AgentResponseIngestionResult.result_hash` ;
- un hash SHA-256 du `AgentResponse` ;
- les hashes du plan avant et après ;
- les hashes de tête et globaux du journal avant et après.

La reconstruction JSON revalide :

- la réponse ;
- le plan ;
- tous les événements ;
- le hash du résultat.

## Sérialisation

`AgentResponseIngestionRequest` et `AgentResponseIngestionResult` fournissent :

- `to_dict()` ;
- `to_json()` ;
- `from_dict()` ;
- `from_json()` ;
- `verify_hash()`.

`AgentResponseIngestionResult.to_journal()` reconstruit un journal indépendant.

Le JSON est compact, trié et déterministe.

## Garanties

- liaison stricte au dispatch ;
- validation du `request_id` et de l’agent ;
- validation cryptographique de la frontière ;
- mapping déterministe des statuts ;
- plan et journal compatibles ;
- chaîne SHA-256 préservée ;
- idempotence fail-closed ;
- absence de mutation des entrées ;
- aucune exécution de sortie ;
- aucune intégration automatique de fichier ;
- aucune écriture dans le projet utilisateur ;
- aucun appel à un fournisseur IA ;
- aucune connexion réseau ;
- aucun changement de version, tag ou release.

## Hors périmètre

Cet incrément ne fournit pas encore :

- la validation métier approfondie des artefacts produits ;
- l’analyse de sécurité des fichiers générés ;
- l’application d’un patch ;
- l’écriture dans le workspace utilisateur ;
- la création automatique du prochain dispatch ;
- l’exécution d’un `next_handoff` ;
- la persistance atomique sur disque ;
- le verrouillage multi-processus ;
- l’authentification HMAC ;
- la signature numérique ;
- l’intégration ELMAN Studio.
