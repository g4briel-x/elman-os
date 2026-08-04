# Resume Application ELMAN-OS v0.7

## Statut

Cinquième incrément du Jalon 2 — Planification et orchestration.

Ce module applique en mémoire une `ResumeCommand` approuvée à un
`ExecutionPlan` et à son `ExecutionJournal`. Il prépare les étapes pour une
reprise contrôlée, sans appeler de fournisseur IA et sans exécuter d’agent.

## Fichiers

| Fichier | Rôle |
|---|---|
| `src/elman_os/resume_application.py` | Valide et applique une commande de reprise de façon atomique, déterministe et idempotente. |
| `tests/test_resume_application.py` | Vérifie les transitions, marqueurs du journal, conflits, rejeux, hashes et sérialisations. |
| `docs/RESUME-APPLICATION-v0.7.md` | Documente les contrats, les transitions, l’idempotence et les limites. |

## Contrats

### `ResumeApplication`

Une application associe exactement :

- une `ResumeCommand` ;
- le `ExecutionCheckpoint` référencé par la commande.

La construction refuse :

- un identifiant de checkpoint différent ;
- un hash de checkpoint différent ;
- un `plan_id` différent ;
- une commande ou un checkpoint dont le hash est invalide.

La méthode `apply(plan, journal)` ne modifie jamais les objets reçus. Elle
construit un nouveau plan et un nouveau journal, puis retourne un
`ResumeApplicationResult`.

### `ResumeApplicationStatus`

Deux résultats sont possibles :

- `applied` : la commande vient d’être appliquée ;
- `already-applied` : les marqueurs exacts de cette commande existent déjà
  dans le journal et aucun nouvel événement n’est ajouté.

### `ResumeApplicationResult`

Le résultat contient notamment :

- l’identifiant déterministe de l’application ;
- le statut ;
- les références de commande et de checkpoint ;
- les étapes sélectionnées ;
- les séquences d’événements ajoutées ;
- les hashes du plan avant et après ;
- les compteurs et hashes du journal avant et après ;
- le plan mis à jour ;
- les événements du journal mis à jour ;
- un hash SHA-256 du reçu.

`to_journal()` reconstruit un journal indépendant à partir des événements
validés.

## Validation préalable

Avant toute transition, l’application vérifie :

- le hash de la commande ;
- le hash du checkpoint ;
- la validité cryptographique du journal ;
- la correspondance des `plan_id` ;
- la correspondance de la commande avec le checkpoint ;
- un `ResumeAssessment` courant, `ready` et autorisant la reprise ;
- un statut de plan parmi `pending`, `approved` ou `running` ;
- que toutes les étapes sélectionnées appartiennent aux étapes actuellement
  prêtes.

Un checkpoint périmé sans marqueurs d’application correspondants est refusé.

## Transition du plan

ELMAN-OS v0.7 ne possède pas de statut d’étape nommé `READY`. La disponibilité
est calculée par `ExecutionPlan.ready_steps()`.

L’application matérialise l’autorisation de reprise par :

- `StepStatus.APPROVED` pour chaque étape sélectionnée ;
- la référence d’approbation humaine de la commande ;
- `PlanStatus.APPROVED` lorsque le plan était encore `pending` ;
- la conservation de `PlanStatus.RUNNING` lorsque le plan était déjà en cours.

Les dépendances et les étapes non sélectionnées restent inchangées.

## Événements du journal

Lorsque le plan est `pending`, l’application ajoute d’abord :

```text
plan.approved
```

Elle ajoute ensuite, dans l’ordre lexical des identifiants :

```text
step.approved
```

pour chaque étape sélectionnée.

Chaque événement porte un payload de traçabilité contenant :

- `resume_application_id` ;
- `resume_command_id` ;
- `resume_command_hash` ;
- `resume_checkpoint_id` ;
- `resume_checkpoint_hash` ;
- `resume_approval_reference`.

Le marqueur `plan.approved` conserve aussi la liste complète des étapes
sélectionnées.

Tous les événements utilisent l’horodatage UTC de la commande. Les règles
monotones du journal restent applicables.

## Atomicité en mémoire

L’application travaille sur une copie reconstruite du journal et sur un
nouveau plan immuable.

La compatibilité du plan et du journal produits est validée avant le retour.
En cas d’erreur :

- le plan d’entrée reste inchangé ;
- le journal d’entrée reste inchangé ;
- aucun événement partiel n’est exposé.

Cet incrément ne fournit pas encore une transaction persistante sur disque.

## Idempotence

L’identifiant d’application est dérivé du hash de la commande :

```text
application:<command_hash>
```

Avant une première application, le journal est inspecté.

Une commande est reconnue comme déjà appliquée uniquement lorsque :

- le même hash de commande est présent ;
- les marqueurs commencent immédiatement après la frontière du checkpoint ;
- les séquences sont contiguës ;
- le nombre de marqueurs est exact ;
- les types d’événements sont exacts ;
- tous les champs du payload correspondent ;
- les étapes marquées correspondent exactement à la commande ;
- le plan et le journal courants restent compatibles avec l’historique du
  checkpoint.

Dans ce cas, le résultat est `already-applied` et aucun événement n’est ajouté.

## Détection des conflits

L’application échoue de manière fermée lorsque :

- un même `command_id` apparaît avec un autre hash ;
- les marqueurs sont incomplets ou dupliqués ;
- les marqueurs ne sont pas contigus ;
- un champ de traçabilité diffère ;
- un type d’événement inattendu porte le hash de la commande ;
- une étape possède déjà une référence d’approbation incompatible ;
- la commande sélectionne une étape non prête ;
- l’horodatage de la commande régresse par rapport au journal.

## Intégrité du résultat

Le reçu conserve :

- `plan_before_hash` et `plan_after_hash` ;
- les hashes de tête du journal ;
- les hashes globaux du journal ;
- les compteurs d’événements ;
- les séquences ajoutées ;
- `result_hash`.

Lors d’une reconstruction JSON, le plan et tous les événements sont
revalidés. Une modification du reçu, du plan ou du journal est détectée.

## Sérialisation

`ResumeApplicationResult` fournit :

- `to_dict()` ;
- `to_json()` ;
- `from_dict()` ;
- `from_json()` ;
- `verify_hash()` ;
- `to_journal()`.

Le format JSON est canonique, compact et déterministe.

## Garanties

- application déterministe ;
- comportement idempotent ;
- validation fail-closed ;
- transition contrôlée vers `APPROVED` ;
- préservation de la chaîne SHA-256 ;
- traçabilité de l’approbation humaine ;
- absence de mutation des entrées ;
- aucune exécution d’agent ;
- aucun appel à un fournisseur IA ;
- aucune connexion réseau ;
- aucune écriture dans un projet utilisateur ;
- aucun changement de version, de tag ou de release.

## Hors périmètre

Cet incrément ne fournit pas encore :

- le démarrage réel d’une étape ;
- l’exécution ou la relance d’un agent ;
- la consommation d’une réponse d’agent ;
- la persistance atomique du plan et du journal ;
- le verrouillage multi-processus ;
- l’authentification HMAC ;
- la signature numérique ;
- le chiffrement ;
- l’intégration ELMAN Studio.
