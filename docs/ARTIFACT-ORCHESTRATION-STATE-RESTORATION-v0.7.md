# Artifact Orchestration State Restoration ELMAN-OS v0.7

## Statut

Dix-huitième incrément du Jalon 2 — Planification et orchestration.

Ce module ajoute une frontière de restauration vérifiée et strictement
en lecture seule pour les états produits par
`ArtifactOrchestrationStatePersistence`.

Il restaure ensemble :

- `ExecutionPlan` ;
- `ExecutionJournal` ;
- `ExecutionCheckpoint` ;
- le manifest cryptographique qui lie ces trois représentations au résultat
  d’orchestration.

## Fichiers

| Fichier | Rôle |
|---|---|
| `src/elman_os/artifact_orchestration_state_restoration.py` | Localise, lit, vérifie et reconstruit un état d’orchestration persistant sans le modifier. |
| `tests/test_artifact_orchestration_state_restoration.py` | Vérifie les contrats, la lecture seule, les hashes, les limites, les liens symboliques, les altérations et l’évaluation de reprise. |
| `docs/ARTIFACT-ORCHESTRATION-STATE-RESTORATION-v0.7.md` | Documente la procédure de restauration, les garanties et le périmètre de sécurité. |

## Contrats

### `ArtifactOrchestrationRestorationPolicy`

La politique contrôle :

- le refus des composants symboliques du chemin ;
- l’exigence d’un ensemble exact de fichiers ;
- l’exigence de payloads canoniques ;
- l’exigence d’un checkpoint compatible ;
- la taille maximale lisible par fichier ;
- le hash SHA-256 déterministe de la politique.

### `ArtifactOrchestrationRestorationRequest`

La requête est liée à :

- la politique de restauration ;
- un `persistence_id` ;
- une racine d’état absolue ;
- un demandeur ;
- un horodatage UTC ;
- un hash de manifest attendu, optionnel ;
- un hash de résultat d’orchestration attendu, optionnel.

Le `restoration_id` par défaut est déterministe pour une même politique, un
même `persistence_id`, une même racine et les mêmes attentes
cryptographiques.

Une requête peut aussi être construite directement depuis un
`ArtifactOrchestrationPersistenceResult`.

### `ArtifactOrchestrationRestoredState`

L’état restauré embarque :

- le plan exact ;
- le journal exact ;
- le checkpoint exact ;
- le statut de l’évaluation de reprise ;
- l’indicateur `can_resume` ;
- l’évaluation complète ;
- les hashes du plan, du journal et du checkpoint ;
- le hash du manifest ;
- le hash du résultat d’orchestration ;
- son propre hash SHA-256.

### `ArtifactOrchestrationRestorationResult`

Le résultat contient :

- le statut `restored` ;
- la requête et la politique ;
- la racine et le répertoire d’état ;
- le hash du manifest ;
- le hash du résultat d’orchestration ;
- l’état restauré complet ;
- la raison ;
- son propre hash SHA-256.

## Localisation déterministe

Pour un `persistence_id`, la clé de stockage est :

```text
sha256(persistence_id)
```

Le répertoire attendu est :

```text
<state-root>/<state-key>/
```

Il doit contenir :

```text
execution-plan.json
execution-journal.jsonl
execution-checkpoint.json
manifest.json
```

Par défaut, tout fichier supplémentaire est refusé.

## Procédure de restauration

La restauration suit cette séquence :

1. vérifier la politique et la requête ;
2. calculer la clé de stockage ;
3. vérifier la racine et le répertoire d’état ;
4. refuser les composants symboliques ;
5. énumérer les entrées ;
6. vérifier l’ensemble exact des quatre fichiers ;
7. lire le manifest avec une limite de taille ;
8. vérifier son JSON, son hash et sa forme canonique ;
9. vérifier sa liaison à la requête ;
10. lire les trois payloads comme fichiers réguliers ;
11. vérifier leur taille et leur SHA-256 ;
12. reconstruire le plan, le journal et le checkpoint ;
13. vérifier leur forme canonique lorsque la politique l’exige ;
14. vérifier la cohérence des identifiants ;
15. vérifier les hashes finaux du plan, du journal et du checkpoint ;
16. évaluer la reprise avec le checkpoint ;
17. produire un résultat cryptographiquement lié.

Aucun fichier n’est créé, modifié, remplacé ou supprimé.

## Lecture contrôlée

Chaque fichier est lu selon le protocole :

```text
lstat avant ouverture
refus d’un lien symbolique
refus d’un type non régulier
contrôle de taille avant ouverture
ouverture O_RDONLY
O_NOFOLLOW lorsque disponible
fstat du descripteur
vérification device/inode
lecture bornée
lstat après lecture
revérification device/inode/taille/mtime
```

Cette séquence réduit les risques de remplacement concurrent du fichier entre
l’inspection et la lecture.

## Manifest

Le manifest est vérifié avec
`ArtifactOrchestrationStateManifest.from_json()` puis `verify_hash()`.

La restauration vérifie notamment :

```text
persistence_id
manifest_hash attendu
orchestration_result_hash attendu
plan_id
project_id
result_plan_state_hash
result_journal_hash
result_checkpoint_hash
taille de chaque fichier
SHA-256 de chaque fichier
```

## Reconstruction

Les représentations sont reconstruites avec :

```text
ExecutionPlan.from_json()
ExecutionJournal.from_jsonl()
ExecutionCheckpoint.from_json()
```

Le journal est scellé de nouveau afin de recalculer son hash.

Le checkpoint est vérifié avant l’évaluation de reprise.

## Évaluation de reprise

Le checkpoint restauré est évalué contre le plan et le journal.

Les principaux statuts sont :

```text
ready
blocked
terminal
stale
incompatible
```

Par défaut, `stale` et `incompatible` provoquent un refus de restauration.

Un plan en cours peut produire `ready`.

Un plan bloqué peut produire `blocked`.

Un plan terminé produit `terminal`.

## Canonicalité

Par défaut :

- le manifest doit être du JSON canonique ;
- le plan doit correspondre exactement à `ExecutionPlan.to_json()` ;
- le journal doit correspondre exactement à `ExecutionJournal.to_jsonl()` ;
- le checkpoint doit correspondre exactement à
  `ExecutionCheckpoint.to_json()`.

La politique peut autoriser des payloads non canoniques mais valides. Dans ce
cas, les octets textuels exacts sont conservés dans l’état restauré et liés à
leur hash.

## Protection des chemins

La racine doit être absolue.

Les composants symboliques existants sont refusés lorsque la politique
l’exige.

Le résultat vérifie que `state_directory` reste sous `state_root`.

Les fichiers symboliques sont toujours refusés.

## Limite de taille

`max_file_bytes` limite :

- le manifest ;
- le plan ;
- le journal ;
- le checkpoint.

La limite est vérifiée avant et pendant la lecture.

## Sérialisation

La politique, la requête, l’état restauré et le résultat fournissent :

- `to_dict()` ;
- `to_json()` ;
- `from_dict()` ;
- `from_json()`.

La requête, l’état restauré et le résultat fournissent `verify_hash()`.

## Garanties

- localisation déterministe ;
- restauration strictement en lecture seule ;
- refus des fichiers manquants ;
- refus des fichiers supplémentaires par défaut ;
- refus des liens symboliques ;
- lecture bornée ;
- protection contre un remplacement concurrent simple ;
- vérification SHA-256 du manifest ;
- vérification SHA-256 des trois payloads ;
- reconstruction du plan ;
- reconstruction du journal ;
- reconstruction du checkpoint ;
- vérification croisée des identifiants ;
- vérification croisée des hashes ;
- évaluation de reprise ;
- conservation optionnelle des payloads non canoniques valides ;
- aucune exécution du contenu ;
- aucune importation dynamique du contenu ;
- aucun appel à un fournisseur IA ;
- aucune connexion réseau ;
- aucune écriture dans l’état persistant ;
- aucune modification de version, tag ou release.

## Hors périmètre

Cet incrément ne fournit pas encore :

- la sélection d’un état `latest` ;
- un index de recherche d’états ;
- la restauration automatique au démarrage ;
- l’application automatique d’une reprise ;
- la réplication distribuée ;
- le déchiffrement d’un stockage chiffré ;
- la vérification d’une signature asymétrique ;
- l’intégration ELMAN Studio.
