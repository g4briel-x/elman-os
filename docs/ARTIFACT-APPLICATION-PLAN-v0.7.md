# Artifact Application Plan ELMAN-OS v0.7

## Statut

Neuvième incrément du Jalon 2 — Planification et orchestration.

Ce module transforme un `AgentOutputValidationResult` en plan transactionnel
déterministe d’application des artefacts. Il décrit l’ordre des opérations,
les préconditions, les sauvegardes et le rollback, mais ne lit ni ne modifie
le workspace.

## Fichiers

| Fichier | Rôle |
|---|---|
| `src/elman_os/artifact_application_plan.py` | Définit la politique, la requête, les opérations, le manifeste de rollback et le plan. |
| `tests/test_artifact_application_plan.py` | Vérifie l’ordre, les approbations, les sauvegardes, les refus, les hashes et la sérialisation. |
| `docs/ARTIFACT-APPLICATION-PLAN-v0.7.md` | Documente le contrat transactionnel et ses limites. |

## Contrats

### `ArtifactApplicationPolicy`

La politique définit :

- le nombre maximal d’opérations ;
- l’obligation d’approbation humaine pour les mises à jour ;
- l’obligation d’un rollback ;
- la racine portable du manifeste de sauvegarde ;
- les classifications d’artefacts autorisées ;
- un hash SHA-256 déterministe de la politique.

La racine de rollback par défaut est :

```text
.elman-os/rollback
```

### `ArtifactApplicationRequest`

La requête est liée cryptographiquement à :

- la politique d’application ;
- l’identifiant et le hash du résultat de validation ;
- le plan et l’étape ;
- l’agent producteur ;
- l’orchestrateur demandeur ;
- l’horodatage UTC ;
- la référence d’approbation humaine éventuelle ;
- le hash de l’état du plan ;
- le nombre d’événements du journal ;
- le hash de tête du journal ;
- le hash global du journal.

`from_validation_result()` construit cette frontière depuis un
`AgentOutputValidationResult` vérifié.

### `ArtifactApplicationOperation`

Chaque opération contient :

- une séquence contiguë ;
- un identifiant déterministe ;
- l’index de l’artefact validé ;
- le chemin de destination ;
- l’opération `create` ou `update` ;
- la classification ;
- le hash SHA-256 attendu ;
- la taille déclarée ;
- le type de média ;
- une précondition explicite ;
- l’obligation de sauvegarde ;
- le chemin de sauvegarde éventuel ;
- l’action de rollback ;
- la référence d’approbation éventuelle ;
- un hash SHA-256 de l’opération.

### `ArtifactRollbackEntry`

Chaque opération possède exactement une entrée de rollback :

- `create` → `delete-created` ;
- `update` → `restore-backup`.

Le manifeste couvre toutes les opérations, dans le même ordre et avec les
mêmes chemins et hashes.

### `ArtifactApplicationPlan`

Le plan contient :

- la décision globale ;
- la requête source ;
- la politique ;
- le résultat de validation ;
- les opérations ordonnées ;
- le manifeste de rollback ;
- les raisons de blocage éventuelles ;
- la frontière plan/journal ;
- le hash des opérations ;
- le hash du manifeste ;
- le hash SHA-256 du plan complet.

## Décisions

### `ready`

Le plan est prêt uniquement lorsque :

- le résultat de validation est `accepted` ;
- chaque artefact est individuellement `accepted` ;
- chaque déclaration est complète ;
- chaque classification est autorisée ;
- le nombre maximal n’est pas dépassé ;
- aucun doublon ou conflit de casse n’existe ;
- chaque `update` possède l’approbation exigée.

La propriété `executable` vaut alors `true`.

Cette propriété indique uniquement que le plan contractuel est prêt. Le
module n’exécute aucune opération.

### `requires-approval`

Cette décision est produite lorsque le plan est structurellement valide mais
qu’une ou plusieurs opérations `update` n’ont pas de référence d’approbation
humaine.

Les opérations et le manifeste restent visibles pour revue, mais
`executable` vaut `false`.

### `rejected`

Le plan est refusé lorsque :

- le résultat de validation n’est pas `accepted` ;
- un artefact n’est pas `accepted` ;
- une déclaration est incomplète ;
- une classification n’est pas autorisée ;
- une limite d’opérations est dépassée ;
- une destination est dupliquée ;
- deux destinations ne diffèrent que par la casse ;
- la requête ne correspond pas à la politique ou au résultat source.

Un plan refusé ne contient aucune opération.

## Ordre déterministe

Les opérations sont triées par :

1. chemin insensible à la casse ;
2. chemin original ;
3. index de l’artefact.

Les séquences commencent à `1` et restent contiguës.

Le même ensemble d’entrées, la même politique et la même requête produisent
exactement le même JSON et les mêmes hashes.

## Opération `create`

Une création utilise :

```text
precondition = destination-must-not-exist
requires_backup = false
rollback_action = delete-created
```

La phase d’exécution future devra refuser l’opération lorsque la destination
existe déjà.

## Opération `update`

Une mise à jour utilise :

```text
precondition = destination-must-exist-and-be-backed-up
requires_backup = true
rollback_action = restore-backup
```

Le chemin de sauvegarde est dérivé de manière déterministe :

```text
.elman-os/rollback/<préfixe-du-hash-de-requête>/<destination>
```

Lorsque la politique l’exige, une référence d’approbation humaine doit être
présente avant que le plan devienne `ready`.

## Manifeste de rollback

Le manifeste est construit en même temps que les opérations.

Pour une création, le rollback futur supprimera uniquement le fichier créé
par la transaction.

Pour une mise à jour, le rollback futur restaurera la sauvegarde réalisée
avant l’écriture.

Le module vérifie que :

- chaque opération possède une entrée ;
- les séquences correspondent ;
- les chemins correspondent ;
- les actions correspondent ;
- les sauvegardes correspondent ;
- les hashes d’artefacts correspondent.

## Intégrité

Le module expose :

- `ArtifactApplicationPolicy.policy_hash` ;
- `ArtifactApplicationRequest.request_hash` ;
- `ArtifactApplicationOperation.operation_hash` ;
- `ArtifactRollbackEntry.entry_hash` ;
- `ArtifactApplicationPlan.operations_hash` ;
- `ArtifactApplicationPlan.rollback_manifest_hash` ;
- `ArtifactApplicationPlan.plan_hash`.

Toute modification d’un chemin, d’une opération, d’un hash, d’une
sauvegarde, d’un manifeste ou de la frontière source est détectée lors de la
désérialisation.

## Sérialisation

La politique, la requête et le plan fournissent :

- `to_dict()` ;
- `to_json()` ;
- `from_dict()` ;
- `from_json()`.

Les opérations et entrées de rollback fournissent `to_dict()` et
`from_dict()`.

La requête, les opérations, les entrées et le plan fournissent
`verify_hash()`.

Le JSON est compact, trié et déterministe.

## Garanties

- acceptation fail-closed du résultat de validation ;
- ordre déterministe ;
- distinction explicite entre `create` et `update` ;
- approbation humaine contrôlée ;
- manifeste complet de rollback ;
- chemins relatifs et portables ;
- hashes SHA-256 liés à toutes les couches ;
- absence de mutation du résultat source ;
- aucune lecture du workspace ;
- aucune création de fichier ;
- aucune modification de fichier ;
- aucune suppression de fichier ;
- aucune création effective de sauvegarde ;
- aucune application de patch ;
- aucune exécution de code ;
- aucune connexion réseau ;
- aucun changement de version, tag ou release.

## Hors périmètre

Cet incrément ne fournit pas encore :

- la vérification de l’existence des destinations ;
- la lecture du contenu réel ;
- la comparaison des hashes avec le disque ;
- la création des sauvegardes ;
- l’écriture atomique des artefacts ;
- l’exécution du rollback ;
- le verrouillage du workspace ;
- la reprise après panne pendant l’écriture ;
- l’analyse antivirus ou statique ;
- l’intégration ELMAN Studio.
