# Artifact Orchestration State Selection ELMAN-OS v0.7

## Statut

Vingtième incrément du Jalon 2 — Planification et orchestration.

Ce module ajoute une frontière déterministe, vérifiable et strictement en
lecture seule pour sélectionner un état d’orchestration à partir d’un
`ArtifactOrchestrationStateIndexSnapshot`.

Il ne parcourt pas le système de fichiers et ne restaure aucun état. Il reçoit
un snapshot déjà vérifié, applique des critères explicites, classe uniquement
les entrées `valid`, puis produit l’un des résultats suivants :

- `selected` ;
- `no-match` ;
- `ambiguous`.

## Fichiers

| Fichier | Rôle |
|---|---|
| `src/elman_os/artifact_orchestration_state_selection.py` | Filtre et classe les états indexés, puis sélectionne un candidat sans restaurer ni exécuter son contenu. |
| `tests/test_artifact_orchestration_state_selection.py` | Vérifie les contrats, filtres, classements, ambiguïtés, limites, hashes et sérialisations. |
| `docs/ARTIFACT-ORCHESTRATION-STATE-SELECTION-v0.7.md` | Documente l’algorithme de sélection et les garanties fail-closed. |

## Contrats

### `ArtifactOrchestrationStateSelectionPolicy`

La politique contient :

- `policy_id` ;
- `strategy` ;
- `reject_ambiguous` ;
- `max_snapshot_entries` ;
- `max_eligible_entries` ;
- `policy_hash`.

Stratégies prises en charge :

- `latest-persisted` ;
- `oldest-persisted`.

`max_eligible_entries` ne peut pas dépasser `max_snapshot_entries`.

### `ArtifactOrchestrationStateSelectionRequest`

La requête contient :

- un identifiant ;
- le snapshot d’index canonique ;
- le hash attendu du snapshot ;
- le demandeur ;
- l’horodatage UTC ;
- un filtre optionnel par `persistence_id` ;
- un filtre optionnel par projet ;
- un filtre optionnel par plan ;
- un filtre optionnel par checkpoint ;
- les statuts d’évaluation de reprise autorisés ;
- un filtre optionnel `can_resume` ;
- une borne temporelle inférieure ;
- une borne temporelle supérieure ;
- un hash SHA-256 de la requête.

Le hash de la requête est lié au hash du snapshot et non à une représentation
non vérifiée.

### `ArtifactOrchestrationStateSelectionRecord`

Un record décrit la décision pour une entrée du snapshot.

Décisions :

- `eligible` ;
- `excluded`.

Un record éligible doit :

- provenir d’une entrée `valid` ;
- ne contenir aucun motif d’exclusion ;
- posséder un rang temporel ;
- posséder une position de classement.

Un record exclu doit :

- contenir au moins un motif ;
- ne contenir aucun rang ;
- ne contenir aucune position.

Chaque record possède son propre hash SHA-256.

### `ArtifactOrchestrationStateSelectionResult`

Le résultat contient :

- le statut final ;
- les liaisons à la requête, à la politique et au snapshot ;
- les records classés ;
- le nombre d’entrées éligibles ;
- le nombre d’entrées exclues ;
- l’entrée sélectionnée lorsque le statut est `selected` ;
- le hash du record sélectionné ;
- l’horodatage ;
- la raison ;
- le hash SHA-256 du résultat.

L’entrée sélectionnée est embarquée sous sa forme JSON canonique afin de
préserver exactement la frontière cryptographique de l’index.

### `ArtifactOrchestrationStateSelector`

Le sélecteur :

1. vérifie la requête ;
2. vérifie le snapshot ;
3. applique les limites de politique ;
4. vérifie chaque hash d’entrée ;
5. exclut toute entrée non `valid` ;
6. applique les filtres explicites ;
7. classe les entrées éligibles ;
8. détecte les égalités de rang principal ;
9. refuse l’ambiguïté lorsque la politique l’exige ;
10. produit un résultat cryptographiquement lié.

## Critères de filtrage

### `persistence_id`

Lorsqu’il est fourni, seul l’état portant exactement cet identifiant peut être
éligible.

Motif d’exclusion :

```text
persistence-id-mismatch
```

### Projet

Motif d’exclusion :

```text
project-id-mismatch
```

### Plan

Motif d’exclusion :

```text
plan-id-mismatch
```

### Checkpoint

Motif d’exclusion :

```text
checkpoint-id-mismatch
```

### Statut d’évaluation de reprise

La requête peut autoriser explicitement une ou plusieurs valeurs :

- `ready` ;
- `stale` ;
- `incompatible` ;
- `blocked` ;
- `terminal`.

Motif d’exclusion :

```text
assessment-status-not-allowed
```

La restauration vérifiée actuelle produit normalement des entrées valides
`ready`, `blocked` ou `terminal`. Les autres valeurs restent représentables
dans le contrat pour préserver la compatibilité du domaine.

### Capacité de reprise

La requête peut exiger :

- `can_resume = true` ;
- `can_resume = false` ;
- aucune contrainte.

Motif d’exclusion :

```text
can-resume-mismatch
```

### Fenêtre temporelle

Les deux bornes sont inclusives.

Motifs d’exclusion :

```text
persisted-before-lower-bound
persisted-after-upper-bound
```

## Exclusion des entrées non valides

Les entrées `altered` et `unreadable` sont toujours exclues.

Motif :

```text
entry-not-valid
```

Leur contenu n’est jamais considéré pour une sélection.

## Classement déterministe

### `latest-persisted`

Le classement utilise :

1. `persisted_at` décroissant ;
2. `storage_key` croissant comme ordre secondaire stable.

### `oldest-persisted`

Le classement utilise :

1. `persisted_at` croissant ;
2. `storage_key` croissant comme ordre secondaire stable.

La position commence à `1`.

## Traitement de l’ambiguïté

Le `storage_key` fournit toujours un ordre reproductible, mais il ne masque pas
une égalité du rang principal.

Lorsque plusieurs entrées éligibles possèdent le même meilleur
`persisted_at` :

- `reject_ambiguous = true` produit `ambiguous` sans entrée sélectionnée ;
- `reject_ambiguous = false` sélectionne la première clé de stockage selon
  l’ordre déterministe.

Cette séparation évite qu’un simple ordre lexical soit interprété comme une
préférence métier implicite.

## Résultats

### `selected`

Conditions :

- au moins une entrée éligible ;
- aucune ambiguïté refusée ;
- le record choisi possède la position `1` ;
- le hash du record correspond ;
- le hash de l’entrée correspond.

### `no-match`

Condition :

- aucune entrée ne satisfait tous les critères.

Aucune exception n’est nécessaire pour un résultat métier sans candidat.

### `ambiguous`

Conditions :

- au moins deux entrées éligibles ;
- plusieurs entrées partagent le meilleur rang principal ;
- la politique refuse l’ambiguïté.

Aucune entrée n’est sélectionnée.

## Ordre des records

Le résultat place :

1. les records éligibles par `rank_position` ;
2. les records exclus par `storage_key`.

Le résultat refuse :

- des clés de stockage dupliquées ;
- des compteurs incohérents ;
- un ordre non déterministe ;
- un record sélectionné autre que le rang `1` ;
- une entrée sélectionnée non valide ;
- une liaison incohérente entre l’entrée et son record.

## Limites

`max_snapshot_entries` borne le nombre total d’entrées du snapshot.

`max_eligible_entries` borne le nombre d’entrées ayant franchi les filtres.

Le dépassement déclenche
`ArtifactOrchestrationStateSelectionLimitError`.

## Sérialisation

La politique, la requête, le record et le résultat fournissent :

- `to_dict()` ;
- `to_json()` ;
- `from_dict()` ;
- `from_json()`.

La requête, le record et le résultat fournissent `verify_hash()`.

Toutes les représentations utilisent le JSON canonique ELMAN-OS.

## Garanties

- sélection uniquement depuis un snapshot vérifié ;
- sélection uniquement d’entrées `valid` ;
- critères explicites ;
- classement déterministe ;
- détection distincte de l’ambiguïté ;
- absence de choix implicite lorsque l’ambiguïté est refusée ;
- liaisons SHA-256 entre politique, requête, snapshot, records et résultat ;
- objets immuables ;
- aucune lecture du système de fichiers ;
- aucune écriture du système de fichiers ;
- aucune restauration d’état ;
- aucune mutation de l’index ;
- aucune reprise automatique ;
- aucune exécution de contenu ;
- aucune importation dynamique de contenu ;
- aucun appel à un fournisseur IA ;
- aucune connexion réseau ;
- aucune modification de version, tag ou release.

## Hors périmètre

Cet incrément ne fournit pas encore :

- un pointeur persistant `latest` ;
- la persistance du résultat de sélection ;
- la restauration automatique de l’entrée sélectionnée ;
- l’application automatique d’une commande de reprise ;
- une politique de rétention ;
- la suppression d’états ;
- la réparation d’un état altéré ;
- une API de pagination ;
- une sélection distribuée multi-machine ;
- l’intégration au runtime principal ;
- l’intégration ELMAN Studio.
