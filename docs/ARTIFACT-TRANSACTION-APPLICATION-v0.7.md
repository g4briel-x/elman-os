# Artifact Transaction Application ELMAN-OS v0.7

## Statut

Douzième incrément du Jalon 2 — Planification et orchestration.

Ce module constitue la première étape de ELMAN-OS v0.7 qui modifie réellement
le workspace. L’écriture reste strictement bornée par toutes les frontières
précédentes :

1. `ArtifactApplicationPlan` en statut `ready` ;
2. `ArtifactPayloadVerificationResult` en statut `verified` ;
3. `ArtifactWorkspacePreflightResult` en statut `ready` ;
4. `ArtifactTransactionRequest` lié par SHA-256 à ces trois objets.

Le contenu des artefacts n’est jamais exécuté et aucun accès réseau n’est
effectué.

## Fichiers

| Fichier | Rôle |
|---|---|
| `src/elman_os/artifact_transaction_application.py` | Applique les artefacts avec verrou exclusif, sauvegardes, écritures atomiques, vérification, rollback et reçu durable. |
| `tests/test_artifact_transaction_application.py` | Vérifie les créations, mises à jour, locks, reçus, idempotence, rollback, altérations et sérialisations. |
| `docs/ARTIFACT-TRANSACTION-APPLICATION-v0.7.md` | Documente le protocole transactionnel, les garanties, décisions et limites. |

## Contrats

### `ArtifactTransactionPolicy`

La politique définit :

- le répertoire de contrôle interne ;
- le nom du verrou exclusif ;
- le répertoire des reçus durables ;
- le nombre maximal d’opérations ;
- la taille cumulée maximale des payloads ;
- l’activation de `fsync` sur les fichiers ;
- la conservation des sauvegardes après succès ;
- la vérification post-écriture ;
- un hash SHA-256 déterministe.

Valeurs internes par défaut :

```text
control_root       = .elman-os
lock_path          = .elman-os/transaction.lock
receipt_root       = .elman-os/transactions
```

Les destinations d’artefacts qui chevauchent le répertoire de contrôle sont
refusées.

### `ArtifactTransactionRequest`

La requête est liée à :

- la politique ;
- l’identifiant et le hash du preflight ;
- le hash du snapshot ;
- l’identifiant et le hash de la vérification des payloads ;
- le hash du manifeste des payloads ;
- l’identifiant et le hash du plan d’application ;
- le plan, l’étape et l’agent ;
- la racine résolue du workspace ;
- le demandeur ;
- l’horodatage UTC.

L’identifiant de transaction par défaut dépend des hashes des frontières et de
la racine du workspace. Il ne dépend pas de l’heure de la demande. Une même
frontière produit donc le même `transaction_id`.

### `ArtifactTransactionOperationResult`

Chaque opération produit :

- la séquence ;
- l’identifiant de l’opération ;
- le chemin ;
- le type `create` ou `update` ;
- le statut ;
- le SHA-256 du payload ;
- le SHA-256 avant écriture ;
- le SHA-256 après écriture ;
- le chemin de sauvegarde ;
- le nombre d’octets écrits ;
- une raison explicite ;
- un hash SHA-256 de l’enregistrement.

Statuts d’opération :

```text
committed
rolled-back
failed
skipped
```

### `ArtifactTransactionResult`

Le résultat contient :

- le statut global ;
- toutes les références cryptographiques ;
- la racine du workspace ;
- le chemin du lock ;
- le chemin du reçu ;
- les résultats d’opération ;
- les compteurs ;
- les horodatages ;
- la raison globale ;
- un hash SHA-256 du résultat complet.

Statuts globaux :

```text
committed
rolled-back
failed
```

## Protocole d’application

### 1. Rejeu idempotent

Avant de prendre le verrou, le module cherche le reçu déterministe de la
transaction.

Lorsque le reçu existe :

- il doit être un fichier ordinaire ;
- il ne peut pas être un lien symbolique ;
- son JSON doit être valide ;
- tous ses hashes doivent être valides ;
- il doit correspondre au même `transaction_id` et au même `request_hash` ;
- son statut doit être `committed` ;
- chaque fichier final doit encore correspondre au payload vérifié.

Lorsque ces conditions sont satisfaites, le résultat existant est retourné
sans réécriture.

### 2. Verrou exclusif

Le verrou est créé avec une ouverture exclusive :

```text
O_CREAT | O_EXCL
```

Une transaction concurrente ne peut donc pas acquérir le même verrou.

Le fichier de verrou contient uniquement :

- le `transaction_id` ;
- le `request_hash`.

Le verrou est supprimé dans le bloc de nettoyage final, y compris après une
erreur.

### 3. Revalidation du snapshot

Après acquisition du verrou et immédiatement avant les écritures, le module
revalide :

- la racine du workspace ;
- l’ordre et les identifiants d’opération ;
- l’absence de liens symboliques ;
- l’absence de conflits de casse ;
- l’existence et les permissions du parent ;
- l’absence d’une destination `create` ;
- le type, la taille et le SHA-256 d’une destination `update` ;
- l’absence de la destination de sauvegarde.

Toute divergence avec le preflight empêche l’écriture.

### 4. Sauvegarde des mises à jour

Avant une opération `update` :

1. le chemin de sauvegarde du plan est résolu sous le workspace ;
2. les répertoires de contrôle sont créés segment par segment ;
3. chaque segment existant est vérifié comme répertoire non symbolique ;
4. le fichier existant est copié vers un fichier temporaire ;
5. le fichier temporaire est synchronisé par `fsync` ;
6. sa taille et son SHA-256 sont comparés au snapshot ;
7. la sauvegarde est publiée sans écrasement avec un hard link atomique.

Une sauvegarde existante provoque un refus.

### 5. Écriture temporaire

Le payload est écrit dans un fichier temporaire situé dans le même répertoire
que sa destination.

Avant publication :

- la taille temporaire est comparée au plan ;
- le SHA-256 temporaire est comparé au plan ;
- le contenu n’est jamais interprété ni importé.

### 6. Commit `create`

Une création utilise :

```text
os.link(temp, destination)
```

Le hard link permet une publication atomique sans écrasement. Lorsque la
destination apparaît entre le preflight et le commit, l’opération échoue.

Le fichier temporaire est ensuite supprimé.

### 7. Commit `update`

Une mise à jour utilise :

```text
os.replace(temp, destination)
```

Le fichier temporaire et la destination se trouvent sur le même système de
fichiers. Le remplacement est donc atomique selon les garanties du système
d’exploitation.

### 8. Vérification post-écriture

Après chaque commit, le module recalcule :

```text
destination.stat().st_size
sha256(destination)
```

Les valeurs doivent correspondre exactement au payload validé.

### 9. Reçu durable

Après le succès de toutes les opérations, le résultat `committed` est écrit
dans :

```text
.elman-os/transactions/<sha256(transaction_id)>.json
```

Le reçu est lui-même créé via fichier temporaire, `fsync` et publication
atomique sans écrasement.

Le reçu est la frontière d’idempotence durable.

## Rollback automatique

Lorsqu’une erreur survient après une ou plusieurs opérations, les opérations
déjà appliquées sont inversées dans l’ordre décroissant.

### Rollback d’une création

Le fichier est supprimé uniquement lorsque :

- il est encore un fichier ordinaire ;
- son SHA-256 correspond encore au payload écrit par la transaction.

Une modification externe après le commit empêche la suppression et fait
passer la transaction en statut `failed`.

### Rollback d’une mise à jour

La sauvegarde est restaurée uniquement lorsque :

- la destination contient encore le payload de la transaction ;
- la sauvegarde est un fichier ordinaire ;
- le SHA-256 de la sauvegarde correspond au snapshot initial.

La restauration utilise `os.replace`, puis le hash restauré est revérifié.

### Résultat du rollback

- rollback complet : `rolled-back` ;
- rollback incomplet ou état externe divergent : `failed` ;
- opérations non commencées : `skipped`.

Aucun reçu `committed` n’est écrit après un échec.

## Idempotence

Une transaction validée et déjà commitée peut être rejouée.

Le rejeu :

- lit le reçu ;
- vérifie son intégrité ;
- vérifie l’état final ;
- retourne exactement le résultat d’origine ;
- ne prend pas le lock ;
- ne réécrit pas les artefacts ;
- ne modifie pas leur date de modification.

Lorsque le fichier final a été altéré après le commit, le rejeu est refusé.

## Intégrité

Le module expose :

- `ArtifactTransactionPolicy.policy_hash` ;
- `ArtifactTransactionRequest.request_hash` ;
- `ArtifactTransactionOperationResult.operation_result_hash` ;
- `ArtifactTransactionResult.result_hash`.

Toute altération d’un compteur, chemin, hash, statut, résultat d’opération ou
reçu est détectée.

## Sérialisation

La politique, la requête et le résultat fournissent :

- `to_dict()` ;
- `to_json()` ;
- `from_dict()` ;
- `from_json()`.

La requête, les résultats d’opération et le résultat global fournissent
`verify_hash()`.

Le JSON est compact, trié et déterministe.

## Garanties

- frontières précédentes obligatoires ;
- verrou exclusif dans le workspace ;
- revalidation juste avant écriture ;
- refus d’écraser une création ;
- sauvegarde vérifiée avant mise à jour ;
- écriture temporaire sur le même système de fichiers ;
- remplacement atomique ;
- vérification post-écriture ;
- rollback automatique inversé ;
- refus de rollback sur état externe modifié ;
- reçu durable et idempotence ;
- nettoyage des fichiers temporaires ;
- aucune exécution de contenu ;
- aucune importation dynamique ;
- aucune connexion réseau ;
- aucun appel à un fournisseur IA ;
- aucun changement de version, tag ou release.

## Limites

- le lock est coopératif au niveau ELMAN-OS et non un verrou noyau sur tous les
  fichiers ;
- un processus externe ignorant le lock peut modifier le workspace ;
- `create` nécessite un système de fichiers prenant en charge les hard links ;
- les permissions et ACL détaillées ne sont pas préservées dans la sauvegarde ;
- les métadonnées temporelles ne sont pas restaurées ;
- la conservation des sauvegardes peut nécessiter une politique de rétention ;
- aucun journal append-only séparé du reçu n’est encore fourni ;
- aucune reprise automatique d’un processus interrompu au milieu d’un commit ;
- aucun nettoyage automatique des anciens reçus ;
- aucune intégration ELMAN Studio.
