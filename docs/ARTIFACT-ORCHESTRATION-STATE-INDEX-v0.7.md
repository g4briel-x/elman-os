# Artifact Orchestration State Index ELMAN-OS v0.7

## Statut

Dix-neuvième incrément du Jalon 2 — Planification et orchestration.

Ce module ajoute un index vérifiable et strictement en lecture seule pour les
états produits par `ArtifactOrchestrationStatePersistence` et validés par
`ArtifactOrchestrationStateRestoration`.

Il découvre les candidats présents dans une racine de persistance et les
classe séparément comme :

- `valid` ;
- `altered` ;
- `unreadable`.

## Fichiers

| Fichier | Rôle |
|---|---|
| `src/elman_os/artifact_orchestration_state_index.py` | Énumère les états persistés, vérifie leurs manifests et produit un snapshot déterministe. |
| `tests/test_artifact_orchestration_state_index.py` | Vérifie les contrats, le classement, la lecture seule, les altérations, les limites et la sérialisation. |
| `docs/ARTIFACT-ORCHESTRATION-STATE-INDEX-v0.7.md` | Documente l’algorithme d’indexation et les garanties de sécurité. |

## Contrats

### `ArtifactOrchestrationStateIndexPolicy`

La politique contrôle :

- le refus des composants symboliques ;
- l’ensemble exact des fichiers d’un état ;
- la canonicalité du manifest et des payloads ;
- la compatibilité du checkpoint ;
- le nombre maximal de candidats ;
- la taille maximale lisible par fichier ;
- le hash SHA-256 déterministe de la politique.

Elle produit également une
`ArtifactOrchestrationRestorationPolicy` liée à son propre hash.

### `ArtifactOrchestrationStateIndexEntry`

Une entrée décrit exactement un candidat de la racine.

Une entrée `valid` contient :

- la clé de stockage ;
- le `persistence_id` ;
- le hash du manifest ;
- le hash du résultat d’orchestration ;
- le plan ;
- le projet ;
- le checkpoint ;
- le statut d’évaluation de reprise ;
- l’indicateur `can_resume` ;
- l’horodatage de persistance ;
- le hash de l’état restauré ;
- son propre hash SHA-256.

Une entrée `altered` ou `unreadable` contient :

- la clé observée ;
- le chemin observé ;
- un code de raison ;
- une raison explicite ;
- son propre hash SHA-256.

Les champs de l’état restauré ne sont jamais exposés pour une entrée non
valide.

### `ArtifactOrchestrationStateIndexSnapshot`

Le snapshot contient :

- l’identifiant de l’index ;
- la politique et son hash ;
- la racine absolue ;
- le demandeur ;
- l’horodatage d’indexation ;
- les entrées triées ;
- les répertoires de contrôle ignorés ;
- les compteurs `valid`, `altered` et `unreadable` ;
- son propre hash SHA-256.

Les entrées doivent être triées par clé de stockage et ne peuvent pas être
dupliquées.

### `ArtifactOrchestrationStateIndexResult`

Le résultat contient :

- le statut `indexed` ;
- le snapshot complet ;
- la politique ;
- la racine ;
- l’horodatage ;
- la raison ;
- son propre hash SHA-256.

### `ArtifactOrchestrationStateIndex`

Le service d’indexation reçoit :

- une politique ;
- une racine absolue ;
- un demandeur ;
- un horodatage UTC ;
- un `index_id` optionnel.

Le `index_id` par défaut est déterministe pour les mêmes paramètres.

## Racine indexée

La racine peut contenir :

```text
<state-root>/
├── .locks/
├── .staging/
├── <sha256-persistence-id-1>/
├── <sha256-persistence-id-2>/
└── ...
```

Les répertoires `.locks` et `.staging` sont ignorés explicitement et consignés
dans `ignored_control_entries`.

Tout autre élément est un candidat.

## Clé de stockage

Un état valide doit être stocké dans :

```text
sha256(persistence_id)
```

La clé observée doit être composée de 64 caractères hexadécimaux minuscules.

Après lecture du manifest, la clé est recalculée depuis son `persistence_id`.

Toute différence produit une entrée `altered`.

## Procédure d’indexation

L’indexation suit cette séquence :

1. vérifier la politique ;
2. vérifier la racine absolue ;
3. refuser les composants symboliques ;
4. vérifier que la racine existe et est un répertoire ;
5. énumérer les entrées avec `os.scandir()` ;
6. ignorer `.locks` et `.staging` ;
7. appliquer `max_candidates` ;
8. trier les candidats par nom ;
9. vérifier le type de chaque candidat ;
10. lire le manifest avec une lecture bornée ;
11. vérifier le JSON, la canonicalité et le hash du manifest ;
12. vérifier la liaison entre le répertoire et le `persistence_id` ;
13. construire une requête de restauration liée au manifest ;
14. restaurer et vérifier le plan, le journal et le checkpoint ;
15. vérifier le hash de l’état restauré ;
16. produire une entrée `valid`, `altered` ou `unreadable` ;
17. produire un snapshot trié et cryptographiquement lié ;
18. produire un résultat d’indexation.

## Classification

### `valid`

Un candidat est valide lorsque :

- son nom est une clé SHA-256 valide ;
- il s’agit d’un répertoire régulier ;
- son manifest est lisible et valide ;
- son `persistence_id` correspond à son nom de répertoire ;
- sa restauration complète réussit ;
- son plan, journal et checkpoint sont cohérents ;
- son état restauré est cryptographiquement valide.

### `altered`

Un candidat est altéré notamment lorsque :

- son nom n’est pas une clé valide ;
- il s’agit d’un fichier ou d’un lien symbolique ;
- un fichier requis manque ;
- le manifest est invalide ;
- le manifest n’est pas canonique lorsque la politique l’exige ;
- un hash ne correspond pas ;
- le plan, le journal ou le checkpoint est altéré ;
- un fichier supplémentaire est présent lorsque l’ensemble exact est exigé ;
- le checkpoint est incompatible ;
- le nom du répertoire ne correspond pas au `persistence_id`.

### `unreadable`

Un candidat est illisible notamment lorsque :

- une permission empêche l’inspection ou la lecture ;
- une erreur d’entrée-sortie survient ;
- un fichier dépasse la limite configurée ;
- la restauration signale une erreur de lecture.

Une entrée illisible ne provoque pas l’échec de l’ensemble du snapshot.

## Lecture contrôlée du manifest

Le manifest est lu selon le protocole :

```text
lstat avant ouverture
refus d’un lien symbolique
refus d’un type non régulier
contrôle de taille
ouverture O_RDONLY
O_NOFOLLOW lorsque disponible
fstat du descripteur
vérification device/inode
lecture bornée
lstat après lecture
revérification device/inode/taille/mtime
```

## Vérification complète des états

L’index ne se limite pas au manifest.

Pour chaque candidat dont le manifest est valide, il appelle la frontière de
restauration avec :

- le `persistence_id` du manifest ;
- le hash du manifest attendu ;
- le hash du résultat d’orchestration attendu ;
- la même racine de persistance ;
- une politique dérivée de la politique d’index.

La restauration vérifie ensuite :

- `execution-plan.json` ;
- `execution-journal.jsonl` ;
- `execution-checkpoint.json` ;
- leurs tailles ;
- leurs hashes ;
- leur canonicalité ;
- leur cohérence ;
- l’évaluation de reprise.

## Ordre déterministe

Les candidats sont triés par nom avant inspection.

Les entrées du snapshot sont triées par `storage_key`.

Le snapshot refuse :

- un ordre non déterministe ;
- les clés dupliquées ;
- les compteurs incohérents ;
- un chemin d’état situé hors de la racine.

## Limites

`max_candidates` limite le nombre d’éléments non contrôlés présents directement
sous la racine.

`max_file_bytes` limite chaque fichier lu par l’index et par la restauration.

Le dépassement du nombre de candidats provoque une erreur de racine.

Le dépassement de taille d’un état produit une entrée `unreadable`.

## Sérialisation

La politique, l’entrée, le snapshot et le résultat fournissent :

- `to_dict()` ;
- `to_json()` ;
- `from_dict()` ;
- `from_json()`.

L’entrée, le snapshot et le résultat fournissent `verify_hash()`.

## Garanties

- indexation strictement en lecture seule ;
- localisation déterministe ;
- ordre déterministe ;
- séparation `valid`, `altered`, `unreadable` ;
- compteurs vérifiés ;
- exclusion explicite des répertoires de contrôle ;
- refus des liens symboliques ;
- lecture bornée ;
- vérification SHA-256 du manifest ;
- restauration complète des états valides ;
- vérification des hashes du plan, du journal et du checkpoint ;
- conservation d’un diagnostic pour chaque candidat ;
- aucune création de fichier ;
- aucune modification de fichier ;
- aucune suppression ;
- aucune exécution du contenu ;
- aucune importation dynamique du contenu ;
- aucun appel à un fournisseur IA ;
- aucune connexion réseau ;
- aucune modification de version, tag ou release.

## Hors périmètre

Cet incrément ne fournit pas encore :

- un fichier d’index persistant ;
- un pointeur `latest` ;
- une sélection automatique d’un état ;
- une API de pagination ;
- une politique de rétention ;
- une réparation automatique ;
- une suppression d’état ;
- une réplication distribuée ;
- l’intégration ELMAN Studio.
