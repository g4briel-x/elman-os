# Artifact Orchestration State Persistence ELMAN-OS v0.7

## Statut

Dix-septième incrément du Jalon 2 — Planification et orchestration.

Ce module ajoute une frontière de persistance durable pour l’état
d’orchestration produit par
`ArtifactTransactionOrchestrationAdapter`.

Il persiste ensemble :

- `ExecutionPlan` ;
- `ExecutionJournal` ;
- `ExecutionCheckpoint` ;
- un manifest cryptographique reliant ces trois représentations au résultat
  d’orchestration.

La persistance est effectuée dans un répertoire d’état immuable identifié par
le SHA-256 du `persistence_id`.

## Fichiers

| Fichier | Rôle |
|---|---|
| `src/elman_os/artifact_orchestration_state_persistence.py` | Persiste atomiquement le plan, le journal, le checkpoint et leur manifest. |
| `tests/test_artifact_orchestration_state_persistence.py` | Vérifie les contrats, verrous, écritures atomiques, rejeux, reprises et altérations. |
| `docs/ARTIFACT-ORCHESTRATION-STATE-PERSISTENCE-v0.7.md` | Documente les frontières, le format du répertoire d’état et les garanties. |

## Contrats

### `ArtifactOrchestrationPersistencePolicy`

La politique contrôle :

- l’appel à `fsync()` sur chaque fichier ;
- l’appel optionnel à `fsync()` sur les répertoires ;
- le refus des composants de chemin symboliques ;
- la taille maximale de chaque fichier persistant ;
- le hash SHA-256 déterministe de la politique.

### `ArtifactOrchestrationPersistenceRequest`

La requête est liée à :

- la politique de persistance ;
- l’identifiant d’orchestration ;
- le hash du résultat d’orchestration ;
- le plan et le projet ;
- l’étape et l’agent ;
- la transaction ;
- le hash de l’état final du plan ;
- le hash final du journal ;
- le hash final du checkpoint ;
- la racine absolue de persistance ;
- le demandeur ;
- l’horodatage UTC.

Le `persistence_id` par défaut est déterministe pour une même politique, un
même résultat d’orchestration et une même racine d’état.

### `ArtifactOrchestrationPersistenceFile`

Chaque fichier décrit dans le manifest contient :

- son nom relatif ;
- son type média ;
- sa taille en octets ;
- son SHA-256.

Les chemins imbriqués et les tailles négatives sont refusés.

### `ArtifactOrchestrationStateManifest`

Le manifest lie cryptographiquement :

- la requête de persistance ;
- la politique ;
- le résultat d’orchestration ;
- le plan ;
- le journal ;
- le checkpoint ;
- la transaction ;
- l’étape et l’agent ;
- les trois fichiers persistés ;
- l’horodatage de persistance.

Il exige exactement :

```text
execution-plan.json
execution-journal.jsonl
execution-checkpoint.json
```

Son propre hash est stocké dans `manifest_hash`.

### `ArtifactOrchestrationPersistenceResult`

Le résultat contient :

- `persisted` ou `noop` ;
- toutes les références cryptographiques ;
- la racine d’état ;
- le répertoire final ;
- le manifest complet ;
- le hash du manifest ;
- la raison ;
- le hash SHA-256 du résultat.

## Structure persistée

Pour un `persistence_id`, la clé de stockage est :

```text
sha256(persistence_id)
```

La structure finale est :

```text
<state-root>/
├── .locks/
│   └── <state-key>.lock
├── .staging/
│   └── <state-key>.<request-prefix>/
└── <state-key>/
    ├── execution-plan.json
    ├── execution-journal.jsonl
    ├── execution-checkpoint.json
    └── manifest.json
```

Le lock et le staging disparaissent après une persistance réussie.

## Procédure atomique

La persistance suit cette séquence :

1. vérifier la requête et le résultat d’orchestration ;
2. calculer les trois payloads ;
3. calculer le manifest ;
4. créer la racine d’état ;
5. acquérir un verrou exclusif avec `O_CREAT | O_EXCL` ;
6. détecter un état final déjà présent ;
7. vérifier ou créer le staging ;
8. écrire chaque fichier par fichier temporaire et `os.replace()` ;
9. relire et vérifier tous les fichiers ;
10. renommer le répertoire staging vers le répertoire final ;
11. revérifier l’état final ;
12. libérer le verrou.

Le passage du staging au répertoire final utilise un renommage dans la même
racine d’état.

## Écritures de fichiers

Chaque fichier du staging est écrit selon le protocole :

```text
création exclusive du fichier .tmp
écriture complète
flush
fsync optionnel
os.replace vers le nom final du staging
```

Les fichiers temporaires sont supprimés en cas d’échec.

## Manifest

Le manifest contient pour chaque payload :

```text
path
media_type
size_bytes
sha256
```

Il contient également :

```text
persistence_id
request_hash
policy_id
policy_hash
orchestration_id
orchestration_result_hash
plan_id
project_id
step_id
agent_id
transaction_id
result_plan_state_hash
result_journal_hash
result_checkpoint_hash
persisted_at
manifest_hash
```

## Rejeu idempotent

Lorsque le répertoire final existe déjà, le composant :

- exige exactement les quatre fichiers attendus ;
- refuse tout fichier supplémentaire ;
- relit les trois payloads ;
- compare leurs octets avec le résultat d’orchestration ;
- relit et vérifie le manifest ;
- compare le manifest attendu au manifest observé.

Si tout est identique, le résultat est :

```text
noop
```

Aucun fichier n’est réécrit.

## Reprise d’un staging complet

Un staging complet peut subsister après une interruption avant renommage.

Dans ce cas :

- tous ses fichiers sont revérifiés ;
- le manifest est revérifié ;
- le staging est renommé vers l’état final ;
- le résultat est `persisted`.

Un staging incomplet ou divergent est conservé et refusé afin d’éviter toute
suppression automatique d’éléments non validés.

## Rollback

Lorsque le staging vient d’être créé pendant l’appel courant et qu’une écriture
ou le renommage échoue :

- le staging créé pendant cet appel est supprimé ;
- l’état final n’est pas créé ;
- le verrou est libéré.

Un staging préexistant n’est jamais supprimé automatiquement sur divergence.

## Verrou exclusif

Le verrou contient :

```text
record_type
persistence_id
request_hash
```

Avant suppression, son type et son contenu sont revérifiés.

Un verrou déjà présent provoque
`ArtifactOrchestrationPersistenceLockError`.

## Protection des chemins

Le composant exige une racine absolue.

Lorsque la politique l’impose, tout composant symbolique existant dans le
chemin est refusé.

Le résultat vérifie que `state_directory` demeure sous `state_root`.

Le nettoyage du staging vérifie qu’il reste sous son parent attendu.

## Limite de taille

Chaque représentation est limitée par `max_file_bytes`.

La taille est vérifiée avant l’acquisition du verrou et avant toute écriture.

## Sérialisation

La politique, la requête, le manifest et le résultat fournissent :

- `to_dict()` ;
- `to_json()` ;
- `from_dict()` ;
- `from_json()`.

La requête, le manifest et le résultat fournissent `verify_hash()`.

Le JSON est canonique, compact et trié.

## Garanties

- liaison SHA-256 au résultat d’orchestration ;
- persistance du plan exact ;
- persistance du journal exact ;
- persistance du checkpoint exact ;
- manifest déterministe ;
- verrou exclusif ;
- staging privé ;
- écritures temporaires vérifiées ;
- commit par renommage de répertoire ;
- relecture après écriture ;
- rejeu idempotent ;
- reprise d’un staging complet ;
- refus d’un état final divergent ;
- refus d’un staging divergent ;
- rollback du staging créé pendant l’appel ;
- aucune exécution du contenu persisté ;
- aucune importation dynamique du contenu ;
- aucun appel à un fournisseur IA ;
- aucune connexion réseau ;
- aucune modification de version, tag ou release.

## Hors périmètre

Cet incrément ne fournit pas encore :

- un pointeur `latest` par projet ;
- l’indexation de plusieurs états persistés ;
- la restauration automatique au démarrage ;
- la rétention ou le nettoyage programmé ;
- la réplication distribuée ;
- le chiffrement au repos ;
- la signature asymétrique du manifest ;
- l’intégration ELMAN Studio.
