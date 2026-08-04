# Artifact Workspace Preflight ELMAN-OS v0.7

## Statut

Onzième incrément du Jalon 2 — Planification et orchestration.

Ce module inspecte le workspace en lecture seule avant toute écriture. Il
valide les préconditions de chaque opération du `ArtifactApplicationPlan`,
recalcule l’état des fichiers existants et produit un snapshot déterministe
lié au résultat de vérification des payloads.

Aucun fichier n’est créé, modifié, supprimé, déplacé ou exécuté.

## Fichiers

| Fichier | Rôle |
|---|---|
| `src/elman_os/artifact_workspace_preflight.py` | Définit la politique, la requête, le snapshot, les enregistrements et l’inspection en lecture seule. |
| `tests/test_artifact_workspace_preflight.py` | Vérifie les créations, mises à jour, hashes, liens symboliques, conflits de casse, rollback et sérialisations. |
| `docs/ARTIFACT-WORKSPACE-PREFLIGHT-v0.7.md` | Documente les préconditions, décisions, garanties et limites. |

## Contrats

### `ArtifactWorkspacePreflightPolicy`

La politique définit :

- le nombre maximal d’opérations ;
- la taille maximale d’un fichier existant ;
- le seuil de taille exigeant une revue humaine ;
- l’obligation d’un parent inscriptible ;
- l’obligation d’un parent existant pour `create` ;
- le refus des liens symboliques ;
- le refus d’une racine de workspace symbolique ;
- l’obligation de disponibilité du stockage de rollback ;
- les classifications exigeant une revue ;
- un hash SHA-256 déterministe.

### `ArtifactWorkspacePreflightRequest`

La requête est liée cryptographiquement à :

- la politique ;
- l’identifiant et le hash du résultat de vérification des payloads ;
- l’identifiant et le hash du plan d’application ;
- le plan, l’étape et l’agent ;
- la racine absolue et résolue du workspace ;
- l’orchestrateur demandeur ;
- l’horodatage UTC.

`from_sources()` vérifie les objets sources, résout la racine et construit une
frontière immuable.

### `ArtifactWorkspaceSnapshotEntry`

Chaque opération produit une entrée de snapshot comprenant :

- la séquence et l’identifiant d’opération ;
- le chemin de destination ;
- l’opération `create` ou `update` ;
- la classification ;
- le type d’entrée observé ;
- l’existence de la destination ;
- la taille et le SHA-256 du fichier existant ;
- l’état du répertoire parent ;
- le chemin de rollback ;
- la disponibilité du rollback ;
- les liens symboliques détectés ;
- les conflits de casse ;
- un hash SHA-256 de l’entrée.

### `ArtifactWorkspacePreflightRecord`

Chaque entrée reçoit une décision :

```text
ready
requires-review
rejected
```

Le record est lié au hash exact de l’entrée de snapshot.

### `ArtifactWorkspacePreflightResult`

Le résultat contient :

- le statut global ;
- toutes les références cryptographiques ;
- la racine du workspace ;
- le snapshot complet ;
- les enregistrements ;
- les raisons globales ;
- les compteurs ;
- le hash du snapshot ;
- l’horodatage ;
- un hash SHA-256 du résultat complet.

## Préconditions de `create`

Une opération `create` est prête lorsque :

- la destination est absente ;
- le répertoire parent existe lorsque la politique l’exige ;
- le répertoire parent est inscriptible lorsque la politique l’exige ;
- aucun lien symbolique n’est rencontré ;
- aucun fichier ne diffère uniquement par la casse ;
- aucune règle de revue ne s’applique.

Une destination déjà existante est refusée, même lorsque son contenu possède
le hash attendu du nouveau payload.

## Préconditions de `update`

Une opération `update` est prête lorsque :

- la destination existe ;
- la destination est un fichier ordinaire ;
- le fichier est lisible ;
- sa taille reste dans les limites ;
- son SHA-256 actuel peut être recalculé ;
- le parent est inscriptible ;
- le stockage de rollback est disponible ;
- aucun lien symbolique ou conflit de casse n’est présent.

Le SHA-256 observé représente l’état antérieur à préserver. Il n’est pas
comparé au SHA-256 du payload, car ce dernier représente le nouveau contenu.

## Inspection des fichiers existants

Pour une mise à jour, le module lit le fichier en blocs de 1 Mio et calcule :

```text
existing_size_bytes
existing_sha256
```

Le contenu n’est pas conservé dans le résultat.

Un fichier trop volumineux est refusé. Un fichier dépassant le seuil de revue
mais restant sous la limite maximale produit `requires-review`.

## Types d’entrées

Les types observés sont :

```text
absent
regular-file
directory
symlink
other
```

Une mise à jour accepte uniquement `regular-file`.

Une création exige `absent`.

## Sécurité des chemins

Les chemins d’opération sont déjà portables et sans traversée grâce aux
contrats précédents. Le preflight ajoute :

- la résolution sécurisée de la racine ;
- la vérification que les destinations restent sous la racine ;
- l’inspection des préfixes existants ;
- la détection des liens symboliques ;
- la détection des variantes de casse dans le répertoire parent ;
- la validation du chemin de rollback.

La racine est refusée lorsqu’elle est un lien symbolique, sauf politique
explicite contraire.

## Conflits de casse

Le module inspecte les entrées du répertoire parent.

Par exemple, une opération visant :

```text
src/file.py
```

est refusée lorsque le parent contient déjà :

```text
src/File.py
```

Cette règle préserve un comportement identique entre Windows, macOS et Linux.

## Disponibilité du rollback

Pour une opération `update`, le module inspecte le chemin de sauvegarde
déclaré dans le plan.

Il vérifie :

- que le chemin reste dans le workspace ;
- qu’aucun préfixe existant n’est symbolique ;
- qu’aucun préfixe existant n’est un fichier ;
- que le plus proche ancêtre existant est un répertoire ;
- que cet ancêtre est inscriptible.

Le répertoire de rollback n’est pas créé pendant le preflight.

## Décision globale

Le statut est calculé de façon déterministe :

1. au moins un rejet produit `rejected` ;
2. sinon, au moins une revue produit `requires-review` ;
3. sinon le résultat est `ready`.

Un plan non `ready`, un résultat de payload non `verified`, un plan vide ou
un dépassement du nombre d’opérations provoque un rejet global.

## Snapshot déterministe

Les opérations sont inspectées dans l’ordre déjà déterministe du plan.

Le hash du snapshot couvre toutes les entrées, y compris :

- les types ;
- les tailles ;
- les hashes ;
- les permissions observées ;
- les chemins de rollback ;
- les conflits ;
- les liens symboliques.

Un changement du workspace modifie le hash du snapshot.

## Intégrité

Le module expose :

- `ArtifactWorkspacePreflightPolicy.policy_hash` ;
- `ArtifactWorkspacePreflightRequest.request_hash` ;
- `ArtifactWorkspaceSnapshotEntry.entry_hash` ;
- `ArtifactWorkspacePreflightResult.snapshot_hash` ;
- `ArtifactWorkspacePreflightResult.result_hash`.

Toute altération d’un chemin, d’un hash, d’une taille, d’une permission,
d’un record, d’un compteur ou du snapshot est détectée.

## Sérialisation

La politique, la requête et le résultat fournissent :

- `to_dict()` ;
- `to_json()` ;
- `from_dict()` ;
- `from_json()`.

Les entrées de snapshot et records fournissent `to_dict()` et `from_dict()`.

La requête, les entrées et le résultat fournissent `verify_hash()`.

Le JSON est compact, trié et déterministe pour un même workspace inchangé.

## Garanties

- inspection strictement en lecture seule ;
- acceptation uniquement d’un plan `ready` ;
- acceptation uniquement de payloads `verified` ;
- résolution sécurisée de la racine ;
- validation des préconditions `create` et `update` ;
- calcul du SHA-256 des fichiers existants ;
- détection des liens symboliques ;
- détection des conflits de casse ;
- vérification du stockage de rollback ;
- snapshot déterministe ;
- absence de mutation des objets sources ;
- aucune création de fichier ;
- aucune modification de fichier ;
- aucune suppression de fichier ;
- aucune création de sauvegarde ;
- aucune exécution de contenu ;
- aucune connexion réseau ;
- aucun changement de version, tag ou release.

## Hors périmètre

Cet incrément ne fournit pas encore :

- le verrouillage exclusif du workspace ;
- la création effective du répertoire de rollback ;
- la sauvegarde des fichiers ;
- l’écriture atomique des payloads ;
- la vérification post-écriture ;
- l’exécution du rollback ;
- la reprise après panne ;
- l’analyse antivirus ou statique ;
- la détection de secrets ;
- l’intégration ELMAN Studio.
