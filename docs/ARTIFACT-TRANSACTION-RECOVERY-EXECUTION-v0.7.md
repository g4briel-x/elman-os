# Artifact Transaction Recovery Execution ELMAN-OS v0.7

## Statut

Quatorzième incrément du Jalon 2 — Planification et orchestration.

Ce module exécute un plan de récupération produit par
`ArtifactTransactionReconciliation`. Il accepte uniquement une réconciliation
au statut `recoverable` et refuse les états `clean`, `committed` et
`conflicted`.

Les stratégies exécutables sont :

```text
cleanup-only
finalize-commit
rollback
```

## Contrats

### `ArtifactTransactionRecoveryPolicy`

La politique définit le répertoire de contrôle, le nom du verrou de
récupération, le répertoire des reçus, le répertoire d’undo, le nombre maximal
d’actions, l’utilisation de `fsync`, le nettoyage des fichiers d’undo et
l’exécution des actions de nettoyage proposées par la réconciliation.

### `ArtifactTransactionRecoveryRequest`

La requête est liée par SHA-256 à la politique, au résultat de réconciliation,
à la requête transactionnelle, au plan d’application, aux payloads vérifiés,
au preflight, au snapshot et à la racine absolue du workspace.

### `ArtifactTransactionRecoveryActionResult`

Chaque action produit un résultat contenant son index, son type, son chemin,
son statut, les hashes avant et après, le nombre d’octets concernés, une raison
explicite et un hash SHA-256.

Types d’action :

```text
delete-created-destination
restore-backup
finalize-committed-receipt
remove-residual-lock
remove-temporary
remove-valid-backup
```

Statuts d’action :

```text
applied
skipped
rolled-back
failed
```

### `ArtifactTransactionRecoveryResult`

Le résultat contient le statut global, la stratégie, toutes les références
cryptographiques, les chemins de verrou et de reçu, les actions, les compteurs,
les horodatages, la raison globale et un hash SHA-256.

Statuts globaux :

```text
completed
noop
rolled-back
failed
```

## Verrou exclusif

Le verrou est créé dans :

```text
.elman-os/recovery.lock
```

Il utilise `O_CREAT | O_EXCL`. Une deuxième récupération ne peut donc pas
modifier le même workspace simultanément. Le verrou contient uniquement le
`recovery_id` et le `request_hash`.

## Revalidation

Après acquisition du verrou et avant toute mutation, le module revérifie :

- l’alignement entre opérations et records ;
- l’état `before` ou `after` de chaque destination ;
- la taille et le SHA-256 ;
- le type de chaque fichier de contrôle ;
- le hash des locks, temporaires, sauvegardes et reçus observés ;
- l’absence de liens symboliques dans les cibles de mutation.

Toute divergence produit un échec sans appliquer le plan.

## Stratégie `cleanup-only`

Les actions explicitement produites par la réconciliation peuvent supprimer :

- un lock transactionnel résiduel ;
- un fichier temporaire abandonné ;
- une sauvegarde valide devenue inutile.

Chaque fichier est revérifié, copié dans le répertoire d’undo, supprimé, puis
contrôlé comme absent.

## Stratégie `finalize-commit`

Cette stratégie est utilisée lorsque toutes les destinations correspondent
aux payloads vérifiés mais que le reçu transactionnel est absent.

Le module :

1. revérifie chaque destination ;
2. construit un `ArtifactTransactionResult` au statut `committed` ;
3. écrit le reçu avec fichier temporaire, `fsync` et publication atomique sans
   écrasement ;
4. ne réécrit aucun artefact.

## Stratégie `rollback`

### Création partielle

Un fichier créé est supprimé uniquement lorsqu’il est encore ordinaire et que
sa taille et son SHA-256 correspondent au payload vérifié.

### Mise à jour partielle

Une mise à jour est restaurée uniquement lorsque :

- la destination correspond encore au payload `after` ;
- la sauvegarde correspond exactement au snapshot `before` ;
- la restauration temporaire possède la taille et le SHA-256 attendus.

La publication utilise `os.replace` sur le même système de fichiers.

## Undo et rollback de la récupération

Avant chaque mutation, l’état supprimé ou remplacé est copié dans :

```text
.elman-os/recovery-undo/<sha256(recovery_id)>
```

Les copies d’undo sont vérifiées par taille et SHA-256. Si une action ultérieure
échoue, les actions déjà appliquées sont inversées dans l’ordre décroissant.

Le rollback refuse d’écraser un fichier apparu extérieurement ou dont le hash a
changé après l’action de récupération.

## Reçu durable et idempotence

Une récupération réussie écrit :

```text
.elman-os/recoveries/<sha256(recovery_id)>.json
```

Un rejeu valide le reçu et l’état final, puis retourne exactement le résultat
précédent sans réécriture.

Un reçu corrompu, symbolique, associé à une autre requête ou contredit par le
workspace est refusé.

## Intégrité

Le module expose :

- `ArtifactTransactionRecoveryPolicy.policy_hash` ;
- `ArtifactTransactionRecoveryRequest.request_hash` ;
- `ArtifactTransactionRecoveryActionResult.action_hash` ;
- `ArtifactTransactionRecoveryResult.result_hash`.

Toute altération d’un chemin, compteur, hash, statut ou résultat d’action est
détectée.

## Garanties

- acceptation uniquement d’une réconciliation `recoverable` ;
- refus des conflits ;
- verrou exclusif ;
- revalidation avant chaque mutation ;
- chemins bornés au workspace ;
- sauvegardes et fichiers d’undo vérifiés ;
- écritures temporaires et remplacements atomiques ;
- rollback de la récupération ;
- reçu durable ;
- rejeu idempotent ;
- aucune exécution d’artefact ;
- aucune importation dynamique du contenu ;
- aucun appel à un fournisseur IA ;
- aucune connexion réseau ;
- aucun changement de version, tag ou release.

## Hors périmètre

- reprise après arrêt brutal au milieu d’une action noyau ;
- verrou global imposé aux processus externes ;
- préservation complète des ACL et métadonnées temporelles ;
- journal append-only séparé du reçu ;
- nettoyage planifié des anciens reçus ;
- analyse antivirus et détection de secrets ;
- intégration ELMAN Studio.
