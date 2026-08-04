# Execution Checkpoint ELMAN-OS v0.7

## Statut

Troisième incrément du Jalon 2 — Planification et orchestration.

Ce module crée des points de reprise vérifiables à partir d’un
`ExecutionPlan` et de son `ExecutionJournal`. Il évalue ensuite la possibilité
d’une reprise sans exécuter automatiquement un agent et sans modifier le
projet utilisateur.

## Fichiers

| Fichier | Rôle |
|---|---|
| `src/elman_os/execution_checkpoint.py` | Capture l’état du plan et du journal, scelle le checkpoint et produit une évaluation de reprise. |
| `tests/test_execution_checkpoint.py` | Vérifie la compatibilité plan/journal, l’intégrité, la péremption et les décisions de reprise. |
| `docs/EXECUTION-CHECKPOINT-v0.7.md` | Documente les contrats, les hashes, les statuts et les limites. |

## Contrats

### `StepCheckpointState`

Capture pour chaque étape :

- `step_id` ;
- `status` ;
- `assigned_agent_id` ;
- `approval_reference`.

Les états sont triés par identifiant afin de produire un document
déterministe.

### `CheckpointStatus`

Le statut intrinsèque du checkpoint est dérivé du plan :

- `ready` pour un plan pouvant encore progresser ;
- `blocked` pour un plan bloqué ;
- `terminal` pour un plan échoué ou terminé.

### `ResumeAssessmentStatus`

L’évaluation de reprise peut produire :

- `ready` : checkpoint courant et compatible ;
- `stale` : le journal a avancé depuis le checkpoint ;
- `incompatible` : divergence d’identifiant, de définition, d’état ou de
  chaîne d’intégrité ;
- `blocked` : le checkpoint représente un plan bloqué ;
- `terminal` : le checkpoint représente un plan terminé ou échoué.

### `ResumeAssessment`

L’évaluation contient :

- le statut ;
- `can_resume` ;
- les raisons ;
- les étapes prêtes ;
- les étapes en cours ;
- le nombre courant d’événements ;
- le hash courant de tête.

Cette structure n’effectue aucune reprise. Elle fournit uniquement une
décision vérifiable à un futur orchestrateur.

## Compatibilité plan/journal

La capture refuse un plan et un journal qui ne représentent pas le même état.

Les contrôles incluent :

- même `plan_id` ;
- journal cryptographiquement valide ;
- statut du plan identique au dernier événement de plan ;
- statut de chaque étape identique au dernier événement d’étape ;
- agent affecté identique au dernier agent enregistré pour l’étape ;
- absence d’étape inconnue dans le journal.

L’état est reconstruit en relisant les événements dans leur ordre validé.

## Hashes du checkpoint

Le checkpoint contient trois catégories de hashes.

### `plan_definition_hash`

Couvre la définition stable du plan :

- identité du plan et du projet ;
- objectif et créateur ;
- politique d’approbation ;
- métadonnées ;
- définition, dépendances, permissions et métadonnées des étapes.

Les états, agents affectés et références d’approbation sont exclus afin de
distinguer une modification structurelle d’une évolution d’état.

### `plan_state_hash`

Couvre le JSON canonique complet du plan au moment de la capture.

### Hashes du journal

Le checkpoint conserve :

- le nombre d’événements ;
- le hash de tête ;
- le hash global du sceau du journal.

### `checkpoint_hash`

Le hash SHA-256 du checkpoint couvre tous les champs précédents ainsi que
l’horodatage, les états des étapes et les statuts.

Toute modification du checkpoint sérialisé est détectée lors de la
reconstruction ou de `verify_hash()`.

## Détection de péremption

Un checkpoint est `stale` lorsque :

1. le journal courant contient plus d’événements ;
2. tous les événements jusqu’à la frontière du checkpoint sont identiques ;
3. le hash et le sceau du préfixe correspondent au checkpoint ;
4. la définition du plan reste identique.

Un checkpoint périmé n’est jamais repris directement. Un nouveau checkpoint
doit être créé à partir de l’état courant.

## Détection de divergence

L’évaluation retourne `incompatible` notamment lorsque :

- le plan ou le journal porte un autre identifiant ;
- le projet diffère ;
- la définition du plan a changé ;
- l’état du plan a changé sans progression du journal ;
- le journal contient moins d’événements que le checkpoint ;
- le préfixe du journal diverge ;
- le sceau courant ne correspond pas ;
- le plan et le journal ne décrivent pas les mêmes statuts ou agents.

## Horodatage

`created_at` doit être explicitement UTC :

```text
YYYY-MM-DDTHH:MM:SS.ffffffZ
```

Les dates naïves ou non UTC sont refusées.

## Sérialisation

`ExecutionCheckpoint` fournit :

- `capture()` ;
- `to_dict()` ;
- `to_json()` ;
- `from_dict()` ;
- `from_json()` ;
- `verify_hash()` ;
- `assess_resume()`.

Le JSON est canonique, compact et déterministe.

## Garanties

- checkpoint immuable ;
- capture strictement compatible avec le plan et le journal ;
- hash de définition du plan ;
- hash d’état du plan ;
- liaison au sceau du journal ;
- détection de péremption ;
- détection de divergence ;
- évaluation fail-closed ;
- aucune reprise automatique ;
- aucune exécution d’agent ;
- aucune connexion réseau ;
- aucune écriture persistante ;
- aucun changement de version ou de release.

## Limites

Cet incrément ne fournit pas encore :

- la persistance atomique du checkpoint ;
- le verrouillage multi-processus ;
- le chiffrement ;
- l’authentification HMAC ;
- la signature numérique ;
- la sélection automatique du checkpoint le plus récent ;
- l’application automatique d’un checkpoint ;
- l’exécution ou la relance d’un agent ;
- l’intégration ELMAN Studio.
