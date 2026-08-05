# Artifact Transaction Lifecycle ELMAN-OS v0.7

## Statut

Quinzième incrément du Jalon 2 — Planification et orchestration.

Ce module coordonne le cycle de vie complet d’une transaction d’artefacts en
réutilisant exclusivement les composants bas niveau déjà validés :

1. application transactionnelle ;
2. réconciliation en lecture seule ;
3. exécution contrôlée de la récupération.

Le coordinateur ne réimplémente aucune écriture de fichier, sauvegarde,
restauration ou suppression. Il sélectionne une route à partir d’une nouvelle
réconciliation, appelle le composant spécialisé correspondant et lie chaque
résultat dans un journal déterministe.

## Fichiers

| Fichier | Rôle |
|---|---|
| `src/elman_os/artifact_transaction_lifecycle.py` | Définit les politiques, requêtes, états, records, résultats et le coordinateur du cycle de vie. |
| `tests/test_artifact_transaction_lifecycle.py` | Vérifie les routes d’application, vérification, récupération, refus, idempotence et intégrité. |
| `docs/ARTIFACT-TRANSACTION-LIFECYCLE-v0.7.md` | Documente les états, routes, transitions et garanties. |

## Contrats

### `ArtifactTransactionLifecyclePolicy`

La politique contrôle :

- l’application automatique lorsque la transaction est propre ;
- la récupération automatique lorsqu’elle est récupérable ;
- la réapplication éventuelle après un rollback réussi ;
- la vérification idempotente d’un état déjà commité ;
- le nombre maximal de transitions ;
- un hash SHA-256 déterministe.

### `ArtifactTransactionLifecycleRequest`

La requête est liée cryptographiquement à :

- la politique du cycle de vie ;
- la requête et la politique transactionnelles ;
- la politique de réconciliation ;
- la politique de récupération ;
- le plan d’application ;
- la vérification des payloads ;
- le preflight et son snapshot ;
- le plan, l’étape et l’agent ;
- la racine du workspace ;
- le demandeur et l’horodatage UTC.

L’identifiant par défaut est déterministe pour un même ensemble de frontières.

### `ArtifactTransactionLifecycleState`

Les états de haut niveau sont :

```text
clean
apply-required
committed
recovery-required
recovered
conflicted
failed
```

### `ArtifactTransactionLifecycleRoute`

Les routes possibles sont :

```text
inspect-only
apply
verify-committed
recover
recover-then-apply
refuse
```

### `ArtifactTransactionLifecycleRecord`

Chaque transition contient :

- un index contigu ;
- la phase ;
- le statut ;
- l’état avant et après ;
- l’identifiant du composant bas niveau ;
- le hash de son résultat ;
- une raison explicite ;
- un hash SHA-256 du record.

### `ArtifactTransactionLifecycleResult`

Le résultat final contient :

- l’état final ;
- la route choisie ;
- toutes les frontières cryptographiques ;
- le journal de transitions ;
- le hash de la réconciliation initiale ;
- le hash de la réconciliation finale, lorsqu’elle existe ;
- le hash du résultat transactionnel ;
- le hash du résultat de récupération ;
- le nombre de transitions ;
- l’horodatage ;
- un hash SHA-256 du résultat complet.

## Algorithme de coordination

### 1. Réconciliation initiale

Chaque exécution commence par une nouvelle réconciliation en lecture seule.
Aucune décision n’est prise à partir d’un état mémorisé ou supposé.

### 2. État `clean`

Lorsque toutes les destinations correspondent au snapshot initial et qu’aucun
contrôle résiduel n’existe :

- `auto_apply_when_clean=true` route vers l’application transactionnelle ;
- sinon le résultat est `apply-required` avec la route `inspect-only`.

### 3. État `committed`

Un reçu durable valide et des destinations finales conformes produisent
`committed`.

Le coordinateur appelle le rejeu idempotent de l’application transactionnelle
pour vérifier :

- le reçu ;
- le `request_hash` ;
- les tailles finales ;
- les SHA-256 finaux.

Aucun fichier n’est réécrit.

### 4. État `recoverable`

Lorsque la réconciliation produit `recoverable` :

- la récupération peut être différée par politique ;
- sinon un `ArtifactTransactionRecoveryRequest` est construit ;
- le plan de récupération est exécuté par le composant spécialisé ;
- une nouvelle réconciliation vérifie l’état obtenu.

Une récupération peut produire :

- `committed`, après finalisation d’un reçu ;
- `recovered`, après retour à l’état propre ;
- `recover-then-apply`, lorsque la politique autorise une réapplication après
  rollback.

### 5. État `conflicted`

Un état conflictuel est refusé sans appel au moteur de récupération.

La route est :

```text
refuse
```

Le coordinateur ne tente aucune réparation heuristique.

## Phases du journal

```text
reconcile
application
recovery
post-recovery-reconcile
committed-verification
```

Chaque phase référence le résultat cryptographique exact du composant appelé.

## Idempotence

L’idempotence du cycle complet repose sur les reçus durables des composants
bas niveau :

- une transaction commitée est vérifiée sans réécriture ;
- une récupération déjà terminée retourne son reçu existant ;
- une nouvelle réconciliation confirme l’état réel ;
- des exécutions répétées sur un état commitée produisent le même résultat de
  cycle de vie.

## Limite de transitions

`max_transitions` empêche une boucle de coordination non bornée.

Le coordinateur utilise au maximum :

- une réconciliation initiale ;
- une application ou une récupération ;
- une réconciliation post-récupération ;
- une vérification du reçu commité ;
- éventuellement une réapplication.

Tout dépassement est refusé.

## Garanties

- nouvelle réconciliation avant chaque routage ;
- refus des états conflictuels ;
- appels uniquement aux composants bas niveau validés ;
- aucune duplication de logique d’écriture ;
- journal de transitions déterministe ;
- liaison SHA-256 de chaque résultat ;
- reprise contrôlée après interruption ;
- rejeu idempotent ;
- aucune exécution du contenu des artefacts ;
- aucune importation dynamique du contenu ;
- aucune connexion réseau ;
- aucun appel à un fournisseur IA ;
- aucune modification hors du workspace fourni ;
- aucun changement de version, tag ou release.

## Hors périmètre

Cet incrément ne fournit pas encore :

- un service de démarrage automatique ;
- une file persistante de transactions ;
- une API HTTP du cycle de vie ;
- un tableau de bord ELMAN Studio ;
- une politique de rétention globale ;
- un verrou distribué multi-machine ;
- l’analyse antivirus ou la détection de secrets.
