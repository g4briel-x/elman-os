# Artifact Transaction Reconciliation ELMAN-OS v0.7

## Statut

Treizième incrément du Jalon 2 — Planification et orchestration.

Ce module inspecte une transaction d’artefacts après une interruption,
sans modifier le workspace. Il relie l’état observé aux frontières déjà
validées :

1. `ArtifactApplicationPlan` en statut `ready` ;
2. `ArtifactPayloadVerificationResult` en statut `verified` ;
3. `ArtifactWorkspacePreflightResult` en statut `ready` ;
4. `ArtifactTransactionRequest` ;
5. `ArtifactTransactionPolicy`.

Il classe ensuite la transaction comme :

```text
clean
committed
recoverable
conflicted
```

Aucune action de récupération n’est exécutée par ce module.

## Fichiers

| Fichier | Rôle |
|---|---|
| `src/elman_os/artifact_transaction_reconciliation.py` | Inspecte locks, reçus, sauvegardes, temporaires et destinations, puis produit un plan de récupération déterministe. |
| `tests/test_artifact_transaction_reconciliation.py` | Vérifie les états propres, commités, récupérables, conflictuels, les hashes et la non-mutation. |
| `docs/ARTIFACT-TRANSACTION-RECONCILIATION-v0.7.md` | Documente la classification, les stratégies et les garanties. |

## Contrats

### `ArtifactTransactionReconciliationPolicy`

La politique définit :

- la taille maximale des fichiers de contrôle inspectés ;
- le nombre maximal de fichiers temporaires inspectés ;
- l’autorisation de finaliser un commit sans reçu ;
- la planification de suppression d’un lock résiduel ;
- la planification de suppression des temporaires ;
- la planification de suppression des sauvegardes valides après récupération ;
- un hash SHA-256 déterministe.

### `ArtifactTransactionReconciliationRequest`

La requête est liée cryptographiquement à :

- la politique de réconciliation ;
- l’identifiant et le hash de la requête transactionnelle ;
- la politique transactionnelle ;
- le preflight et son snapshot ;
- la vérification des payloads et son manifeste ;
- le plan d’application ;
- le plan, l’étape et l’agent ;
- la racine absolue du workspace ;
- le demandeur ;
- l’horodatage UTC.

L’identifiant de réconciliation par défaut est déterministe pour une même
transaction, une même politique et un même workspace.

### `ArtifactTransactionControlEntry`

Chaque entrée de contrôle contient :

- un index déterministe ;
- un chemin relatif portable ;
- un type `lock`, `receipt`, `backup` ou `temporary` ;
- le type d’entrée observé ;
- l’état `absent`, `matching`, `residual` ou `invalid` ;
- la taille et le SHA-256 lorsque l’entrée est un fichier ordinaire ;
- une raison explicite ;
- un hash SHA-256 de l’entrée.

### `ArtifactTransactionReconciliationRecord`

Chaque opération contient :

- la séquence ;
- l’identifiant et le chemin ;
- l’opération `create` ou `update` ;
- l’état de destination `before`, `after` ou `conflicted` ;
- la taille et le SHA-256 actuels ;
- l’état de sauvegarde ;
- l’action de récupération recommandée ;
- les raisons ;
- un hash SHA-256 du record.

### `ArtifactTransactionReconciliationResult`

Le résultat contient :

- le statut global ;
- la stratégie ;
- toutes les références cryptographiques ;
- les entrées de contrôle ;
- les records d’opération ;
- les actions de contrôle ;
- les raisons globales ;
- les compteurs ;
- l’horodatage ;
- un hash SHA-256 du résultat complet.

## Inspection du lock

Le lock attendu est :

```text
.elman-os/transaction.lock
```

Un lock régulier est lu dans une limite stricte et doit contenir exactement
une liaison cohérente vers :

- `transaction_id` ;
- `request_hash`.

Un lock correspondant est classé `residual`.

Un lock malformé, appartenant à une autre transaction, symbolique ou non
ordinaire est classé `invalid`.

Le module ne supprime jamais le lock.

## Inspection du reçu

Le reçu attendu est calculé par la politique transactionnelle :

```text
.elman-os/transactions/<sha256(transaction_id)>.json
```

Le reçu doit :

- être un fichier ordinaire ;
- respecter la limite de taille ;
- être un JSON valide ;
- reconstruire un `ArtifactTransactionResult` valide ;
- posséder un `result_hash` valide ;
- avoir le statut `committed` ;
- correspondre au `transaction_id` et au `request_hash` ;
- correspondre au plan, aux payloads et au snapshot ;
- déclarer le bon chemin de reçu.

Un reçu valide ne suffit pas à classer la transaction comme commitée :
chaque destination finale doit encore correspondre au payload vérifié.

## Inspection des destinations

### Opération `create`

La destination est :

- `before` lorsqu’elle est absente ;
- `after` lorsqu’elle est un fichier ordinaire correspondant exactement au
  payload vérifié ;
- `conflicted` dans tout autre cas.

### Opération `update`

La destination est :

- `before` lorsqu’elle correspond exactement à la taille et au SHA-256 du
  snapshot ;
- `after` lorsqu’elle correspond exactement au payload vérifié ;
- `conflicted` lorsqu’elle est absente, non ordinaire ou différente des deux
  états connus.

Les liens symboliques et conflits de casse provoquent un conflit.

## Inspection des sauvegardes

Chaque opération `update` déclare un chemin de sauvegarde dans le plan.

La sauvegarde est :

```text
absent
valid
invalid
```

Elle est `valid` uniquement lorsqu’elle est un fichier ordinaire dont la
taille et le SHA-256 correspondent à l’état `before` du snapshot.

Une opération `create` utilise l’état `not-applicable`.

## Inspection des temporaires

Le module inspecte les répertoires strictement liés à la transaction :

- parents des destinations ;
- parents des sauvegardes ;
- répertoire du lock ;
- répertoire des reçus.

Il recherche uniquement les noms :

```text
.elman-write-*.tmp
.elman-backup-*.tmp
.elman-receipt-*.tmp
```

Un fichier temporaire ordinaire est classé `residual`.

Un chemin temporaire symbolique ou non ordinaire est classé `invalid`.

Aucune recherche réseau, globale ou hors workspace n’est effectuée.

## Classification globale

### `clean`

Conditions :

- aucun reçu ;
- aucun lock résiduel ;
- aucun temporaire ;
- aucune sauvegarde résiduelle ;
- toutes les destinations sont `before`.

Stratégie :

```text
none
```

### `committed`

Conditions :

- reçu durable valide ;
- toutes les destinations sont `after` ;
- aucun contrôle invalide.

Stratégie :

```text
none
cleanup-only
```

`cleanup-only` est utilisé lorsque des contrôles résiduels non conflictuels
subsistent.

### `recoverable`

Trois stratégies sont possibles.

#### `cleanup-only`

Toutes les destinations sont `before`, mais un lock, un temporaire ou une
sauvegarde valide subsiste.

#### `finalize-commit`

Toutes les destinations sont `after`, mais le reçu durable est absent.

Le plan recommande l’écriture ultérieure d’un reçu commité après une nouvelle
revalidation.

#### `rollback`

La transaction est partiellement appliquée et chaque opération peut être
retournée à l’état `before` :

- un fichier créé peut être supprimé après revalidation du hash ;
- un fichier mis à jour peut être restauré depuis une sauvegarde valide.

### `conflicted`

Un conflit est produit lorsque :

- une destination ne correspond ni à `before` ni à `after` ;
- un reçu est invalide ;
- un reçu commité contredit l’état final ;
- un lock est invalide ;
- une sauvegarde est invalide ;
- un temporaire est symbolique ou non ordinaire ;
- aucune stratégie déterministe sûre n’existe.

Stratégie :

```text
manual-review
```

## Actions de récupération

Les actions d’opération possibles sont :

```text
none
delete-created-destination
restore-backup
finalize-commit
investigate
```

Les actions de contrôle sont des instructions déterministes telles que :

```text
WRITE_COMMITTED_RECEIPT:<path>
REMOVE_RESIDUAL_LOCK:<path>
REMOVE_TEMPORARY:<path>
REMOVE_VALID_BACKUP_AFTER_RECOVERY:<path>
MANUAL_REVIEW:<path>
```

Ces chaînes sont uniquement un plan. Elles ne sont pas exécutées.

## Déterminisme

Pour un workspace inchangé, une requête identique produit :

- le même ordre de contrôles ;
- les mêmes records ;
- les mêmes actions ;
- les mêmes raisons ;
- le même JSON ;
- le même `result_hash`.

L’horodatage du résultat est celui de la requête, et non l’heure système au
moment de l’inspection.

## Intégrité

Le module expose :

- `ArtifactTransactionReconciliationPolicy.policy_hash` ;
- `ArtifactTransactionReconciliationRequest.request_hash` ;
- `ArtifactTransactionControlEntry.entry_hash` ;
- `ArtifactTransactionReconciliationRecord.record_hash` ;
- `ArtifactTransactionReconciliationResult.result_hash`.

Toute altération d’un chemin, état, compteur, hash, action ou raison est
détectée.

## Garanties

- inspection strictement en lecture seule ;
- aucune suppression de lock ;
- aucune suppression de temporaire ;
- aucune suppression de sauvegarde ;
- aucune restauration ;
- aucune écriture de reçu ;
- aucune modification des destinations ;
- aucune création de répertoire ;
- aucune exécution d’artefact ;
- aucune importation dynamique du contenu ;
- aucune connexion réseau ;
- aucune utilisation d’un fournisseur IA ;
- aucun changement de version, tag ou release.

## Hors périmètre

Cet incrément ne fournit pas encore :

- l’exécuteur du plan de récupération ;
- un verrou noyau de réconciliation ;
- la restauration effective des sauvegardes ;
- la suppression effective des créations partielles ;
- la finalisation effective d’un reçu ;
- la reprise automatique au démarrage ;
- le nettoyage programmé des anciennes transactions ;
- l’interface ELMAN Studio ;
- l’analyse antivirus ou la détection de secrets.
