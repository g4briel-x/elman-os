# Artifact Transaction Orchestration Adapter ELMAN-OS v0.7

## Statut

Seizième incrément du Jalon 2 — Planification et orchestration.

Ce module relie le cycle transactionnel des artefacts au moteur
d’orchestration ELMAN-OS. Il transforme un
`ArtifactTransactionLifecycleResult` vérifié en nouvelles représentations
cohérentes de :

- `ExecutionPlan` ;
- `ExecutionJournal` ;
- `ExecutionCheckpoint`.

Le module ne réimplémente aucune écriture transactionnelle sur le workspace.
Toutes les mutations d’artefacts restent exclusivement gérées par les
composants transactionnels précédents.

## Fichiers

| Fichier | Rôle |
|---|---|
| `src/elman_os/artifact_transaction_orchestration_adapter.py` | Propage le résultat du cycle transactionnel dans le plan, le journal et le checkpoint. |
| `tests/test_artifact_transaction_orchestration_adapter.py` | Vérifie les transitions, l’intégrité, l’idempotence, les checkpoints et la non-mutation du workspace. |
| `docs/ARTIFACT-TRANSACTION-ORCHESTRATION-ADAPTER-v0.7.md` | Documente les contrats, décisions, événements et garanties. |

## Frontières obligatoires

L’adaptateur exige :

1. un `ArtifactTransactionLifecycleRequest` valide ;
2. un `ArtifactTransactionLifecycleResult` valide et lié à cette requête ;
3. un `ExecutionPlan` valide ;
4. un `ExecutionJournal` valide ;
5. un `ExecutionCheckpoint` correspondant exactement au plan et au journal ;
6. une `ArtifactTransactionOrchestrationPolicy` valide ;
7. une `ArtifactTransactionOrchestrationRequest` liée par SHA-256 à toutes ces
   frontières.

La requête de cycle de vie fournit notamment :

- `plan_id` ;
- `step_id` ;
- `agent_id` ;
- `transaction_id`.

L’adaptateur vérifie que l’étape existe et que son agent assigné correspond à
l’agent du cycle transactionnel.

## Contrats

### `ArtifactTransactionOrchestrationPolicy`

La politique définit :

- l’obligation que l’étape soit `running` avant intégration ;
- la complétion automatique du plan lorsque toutes les étapes sont terminées ;
- le blocage ou l’échec après un état `recovered` ;
- le blocage ou l’échec après un état `conflicted` ;
- le blocage ou l’échec pour les états différés ;
- la longueur maximale de la raison copiée dans le journal ;
- un hash SHA-256 déterministe.

### `ArtifactTransactionOrchestrationRequest`

La requête est liée à :

- la politique ;
- l’identifiant et le hash de la requête de cycle de vie ;
- l’identifiant et le hash du résultat de cycle de vie ;
- l’état final et la route du cycle de vie ;
- la transaction ;
- le plan, le projet, l’étape et l’agent ;
- le hash de l’état initial du plan ;
- le statut initial du plan et de l’étape ;
- le seal initial du journal ;
- l’identifiant et le hash du checkpoint initial ;
- le demandeur ;
- l’horodatage UTC.

L’identifiant d’orchestration par défaut est déterministe pour une même
politique, un même résultat de cycle de vie et une même frontière
plan/journal/checkpoint.

### `ArtifactTransactionOrchestrationRecord`

Chaque nouvel événement journalisé produit un record contenant :

- un index déterministe ;
- la séquence réelle dans le journal ;
- le type d’événement ;
- l’étape et l’agent, lorsqu’ils sont applicables ;
- le hash de l’événement ;
- le hash du payload ;
- une raison explicite ;
- un hash SHA-256 du record.

### `ArtifactTransactionOrchestrationResult`

Le résultat contient :

- le statut global ;
- la décision appliquée ;
- toutes les références cryptographiques ;
- les statuts avant et après du plan et de l’étape ;
- les seals avant et après du journal ;
- les checkpoints avant et après ;
- les records des nouveaux événements ;
- le plan mis à jour en JSON ;
- le journal mis à jour en JSONL ;
- le checkpoint mis à jour en JSON ;
- un hash SHA-256 du résultat complet.

Le résultat permet de reconstruire :

```python
result.execution_plan
result.execution_journal
result.execution_checkpoint
```

Chaque reconstruction est revérifiée contre les hashes déclarés.

## Statuts et décisions

Statuts globaux :

```text
completed
blocked
failed
noop
```

Décisions :

```text
complete-step
block-step
fail-step
noop
```

## Propagation des états du cycle de vie

### `committed`

Décision :

```text
complete-step
```

Effets :

- l’étape devient `completed` ;
- un événement `step.completed` est ajouté ;
- lorsque toutes les étapes sont terminées et que la politique l’autorise,
  le plan devient `completed` ;
- un événement `plan.completed` est alors ajouté ;
- un nouveau checkpoint est capturé.

### `recovered`

Par défaut :

```text
block-step
```

Effets :

- l’étape devient `blocked` ;
- le plan devient `blocked` ;
- `step.blocked` et `plan.blocked` sont ajoutés ;
- le checkpoint enregistre l’état récupéré nécessitant une suite contrôlée.

La politique peut convertir cet état en échec.

### `conflicted`

Par défaut :

```text
block-step
```

Le conflit est propagé comme blocage contrôlé afin de préserver la possibilité
d’une revue humaine. La politique peut imposer un échec terminal.

### États différés

Les états suivants sont différés :

```text
clean
apply-required
recovery-required
```

Par défaut, ils bloquent l’étape et le plan. Ils peuvent être convertis en
échec par politique.

### `failed`

Décision :

```text
fail-step
```

Effets :

- l’étape devient `failed` ;
- le plan devient `failed` ;
- `step.failed` et `plan.failed` sont ajoutés ;
- un checkpoint terminal est capturé.

## Journal d’exécution

Le payload d’un événement contient :

```text
artifact_orchestration_id
artifact_orchestration_request_hash
artifact_lifecycle_id
artifact_lifecycle_result_hash
artifact_transaction_id
artifact_lifecycle_final_state
artifact_lifecycle_route
artifact_orchestration_decision
result_step_status
lifecycle_reason
```

La raison est tronquée selon la politique afin de maintenir une limite
déterministe.

Le journal source est cloné par `ExecutionJournal.from_events`. L’objet source
n’est donc pas modifié.

## Checkpoint

Après chaque transition réelle, l’adaptateur capture un nouveau checkpoint.

Son identifiant dépend de :

- l’identifiant d’orchestration ;
- le hash du plan résultant ;
- le hash du journal résultant.

Le checkpoint résultant doit être compatible avec le plan et le journal
résultants.

## Idempotence

Avant toute nouvelle transition, l’adaptateur recherche le
`artifact_lifecycle_result_hash` dans les payloads du journal.

Lorsque le même résultat est déjà journalisé pour la même étape et le même
agent :

- aucune transition supplémentaire n’est créée ;
- aucun événement n’est ajouté ;
- le plan reste inchangé ;
- le journal reste inchangé ;
- le checkpoint reste inchangé ;
- le résultat utilise le statut `noop`.

Une intégration dupliquée ou liée à une autre étape est refusée.

## Copy-on-write

L’adaptateur ne modifie jamais les objets sources.

Il produit :

- un nouveau `ExecutionPlan` ;
- un nouveau journal reconstruit puis étendu ;
- un nouveau `ExecutionCheckpoint`.

Le résultat embarque leurs représentations sérialisées et vérifiables.

## Intégrité

Le module expose :

- `ArtifactTransactionOrchestrationPolicy.policy_hash` ;
- `ArtifactTransactionOrchestrationRequest.request_hash` ;
- `ArtifactTransactionOrchestrationRecord.record_hash` ;
- `ArtifactTransactionOrchestrationResult.result_hash`.

Toute altération du plan embarqué, du journal, du checkpoint, d’un compteur,
d’un événement, d’un hash ou d’une raison est détectée.

## Sérialisation

La politique, la requête et le résultat fournissent :

- `to_dict()` ;
- `to_json()` ;
- `from_dict()` ;
- `from_json()`.

La requête, les records et le résultat fournissent `verify_hash()`.

Le JSON est compact, trié et déterministe.

## Garanties

- liaison au cycle transactionnel ;
- liaison au plan d’exécution ;
- liaison au journal append-only ;
- liaison au checkpoint ;
- validation du seal du journal ;
- mise à jour contrôlée des statuts ;
- capture d’un checkpoint après transition ;
- idempotence par hash de cycle de vie ;
- copy-on-write ;
- aucune écriture transactionnelle d’artefact ;
- aucune lecture ou modification du workspace ;
- aucune exécution de contenu ;
- aucune importation dynamique du contenu ;
- aucune connexion réseau ;
- aucun appel à un fournisseur IA ;
- aucun changement de version, tag ou release.

## Hors périmètre

Cet incrément ne fournit pas encore :

- la persistance durable du plan, du journal et du checkpoint résultants ;
- un registre central de résultats d’artefacts ;
- l’intégration au runtime principal ;
- l’intégration à ELMAN Studio ;
- une orchestration distribuée multi-machine ;
- l’analyse antivirus ou la détection de secrets ;
- une file d’événements externe.
