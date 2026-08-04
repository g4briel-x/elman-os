# Step Dispatch ELMAN-OS v0.7

## Statut

Sixième incrément du Jalon 2 — Planification et orchestration.

Ce module prépare de manière déterministe le dispatch d’une étape approuvée
vers un agent enregistré. Il construit un `AgentRequest` strict, met à jour le
plan et le journal en mémoire, mais n’appelle aucun fournisseur IA et
n’exécute aucun code généré.

## Fichiers

| Fichier | Rôle |
|---|---|
| `src/elman_os/step_dispatch.py` | Définit la requête, la préparation, le résultat, l’idempotence et les contrôles du dispatch. |
| `tests/test_step_dispatch.py` | Vérifie les transitions, agents, permissions, dépendances, marqueurs, rejeux, hashes et sérialisations. |
| `docs/STEP-DISPATCH-v0.7.md` | Documente les contrats, frontières d’état, événements, garanties et limites. |

## Contrats

### `StepDispatchRequest`

La requête de dispatch contient :

- un `dispatch_id` ;
- le `plan_id` ;
- l’étape sélectionnée ;
- l’agent destinataire ;
- l’agent orchestrateur demandeur ;
- un horodatage UTC ;
- l’identifiant et le hash du `ResumeApplicationResult` ;
- l’identifiant et le hash de la commande de reprise ;
- le hash du plan à la frontière de dispatch ;
- le nombre d’événements du journal ;
- le hash de tête du journal ;
- le hash global du journal ;
- un hash SHA-256 de la requête.

`from_resume_application()` construit cette frontière directement depuis un
`ResumeApplicationResult` vérifié.

Lorsque aucun identifiant n’est fourni, `dispatch_id` est dérivé
déterministement du contenu de la frontière.

### `StepDispatch`

Le dispatch associe exactement :

- une `StepDispatchRequest` ;
- le `ResumeApplicationResult` référencé ;
- un `AgentRegistry`.

La construction refuse :

- une requête liée à un autre plan ;
- un autre résultat de reprise ;
- un autre identifiant ou hash de commande ;
- une étape absente de la sélection autorisée par la reprise ;
- un hash de requête ou de résultat invalide.

### `StepDispatchResult`

Le résultat contient :

- le statut `prepared` ou `already-prepared` ;
- toutes les références de dispatch et de reprise ;
- le `AgentRequest` strict destiné à l’agent ;
- le plan et les événements mis à jour ;
- les séquences ajoutées ;
- les hashes du plan avant et après ;
- les compteurs et hashes du journal avant et après ;
- un hash SHA-256 du résultat.

## Validation de la frontière

Avant la première préparation, ELMAN-OS vérifie que l’état courant correspond
exactement à la frontière capturée dans la requête :

- même `plan_id` ;
- même JSON canonique du plan ;
- même nombre d’événements ;
- même hash de tête ;
- même hash global du journal ;
- même plan et même journal que le `ResumeApplicationResult`.

Toute progression, divergence ou modification est refusée.

## Sélection de l’étape

L’étape doit :

- appartenir aux étapes autorisées par le résultat de reprise ;
- exister dans le plan ;
- être en statut `APPROVED` ;
- avoir toutes ses dépendances en statut `COMPLETED` ;
- ne pas être affectée à un autre agent.

Le plan doit être en statut `APPROVED` ou `RUNNING`.

## Validation de l’agent

L’agent destinataire doit :

- exister dans `AgentRegistry` ;
- être configuré en mode fail-closed ;
- exposer la capacité demandée par l’étape ;
- posséder toutes les permissions requises ;
- satisfaire l’exigence d’approbation humaine de l’étape ou de la capacité.

Le dispatch ne sélectionne pas silencieusement un autre agent. L’identifiant
demandé est vérifié de manière stricte.

## Construction de `AgentRequest`

Le `AgentRequest` contient notamment :

- un identifiant dérivé du hash de la requête de dispatch ;
- le projet ;
- la capacité ;
- l’objectif de l’étape ;
- l’orchestrateur demandeur ;
- le plan, l’étape, les dépendances et les métadonnées ;
- les références de reprise et de commande ;
- les permissions requises ;
- l’agent destinataire ;
- la référence d’approbation humaine.

Les contraintes déclarent explicitement :

```text
execution_mode = dispatch-preparation-only
provider_call_allowed = false
generated_code_execution_allowed = false
project_write_allowed = false
```

Le résultat prépare donc une enveloppe de travail sans l’exécuter.

## Transition du plan

Lors d’une première préparation :

- l’étape sélectionnée reçoit `assigned_agent_id` ;
- l’étape passe de `APPROVED` à `RUNNING` ;
- le plan passe à `RUNNING` ;
- les autres étapes restent inchangées.

Les objets d’entrée ne sont jamais mutés.

## Événements du journal

Lorsque le plan était `APPROVED`, l’ordre est :

```text
plan.started
step.assigned
step.started
```

Lorsque le plan était déjà `RUNNING`, l’ordre est :

```text
step.assigned
step.started
```

Tous les événements utilisent le même horodatage UTC et portent un payload
de traçabilité contenant :

- `step_dispatch_id` ;
- `step_dispatch_request_hash` ;
- `resume_application_id` ;
- `resume_application_result_hash` ;
- `resume_command_id` ;
- `resume_command_hash` ;
- `step_dispatch_step_id` ;
- `step_dispatch_agent_id`.

La chaîne SHA-256 du journal est reconstruite et validée.

## Atomicité en mémoire

Le module travaille sur :

- un nouveau `ExecutionPlan` immuable ;
- une copie reconstruite du `ExecutionJournal`.

Avant de retourner le résultat, un nouveau `ExecutionCheckpoint` est capturé
pour vérifier la compatibilité du plan et du journal produits.

En cas d’échec, aucun état partiel n’est exposé.

## Idempotence

Le journal est inspecté avant toute nouvelle transition.

Une préparation est reconnue comme déjà réalisée uniquement lorsque :

- le même hash de requête existe ;
- les marqueurs commencent exactement après la frontière capturée ;
- les séquences sont contiguës ;
- le nombre et l’ordre des événements sont exacts ;
- tous les champs du payload correspondent ;
- le plan et l’étape sont déjà `RUNNING` ;
- l’agent affecté correspond ;
- le plan et le journal restent compatibles.

Le résultat devient alors `already-prepared` et aucun événement n’est ajouté.

## Détection des conflits

Le module refuse notamment :

- un même `dispatch_id` associé à un autre hash ;
- des marqueurs incomplets ou dupliqués ;
- des marqueurs non contigus ;
- un type d’événement inattendu ;
- une autre étape ou un autre agent ;
- un agent inconnu ;
- une capacité absente ;
- des permissions insuffisantes ;
- un agent non fail-closed ;
- des dépendances incomplètes ;
- une affectation préexistante vers un autre agent ;
- une régression d’horodatage ;
- une modification du plan ou du journal depuis la frontière.

## Sérialisation

`StepDispatchRequest` et `StepDispatchResult` fournissent :

- `to_dict()` ;
- `to_json()` ;
- `from_dict()` ;
- `from_json()` ;
- `verify_hash()`.

`StepDispatchResult.to_journal()` reconstruit un journal indépendant.

Le JSON est compact, trié et déterministe.

## Garanties

- sélection explicite d’une étape approuvée ;
- validation stricte de l’agent ;
- validation des dépendances et permissions ;
- construction d’un `AgentRequest` strict ;
- transition déterministe vers `RUNNING` ;
- chaîne SHA-256 préservée ;
- comportement idempotent et fail-closed ;
- absence de mutation des entrées ;
- aucun appel à un fournisseur IA ;
- aucune exécution de code généré ;
- aucune écriture dans un projet utilisateur ;
- aucune connexion réseau ;
- aucun changement de version, tag ou release.

## Hors périmètre

Cet incrément ne fournit pas encore :

- l’envoi réel du `AgentRequest` ;
- l’appel d’un modèle IA ;
- l’exécution d’un agent ;
- la consommation d’un `AgentResponse` ;
- la validation ou l’intégration des sorties ;
- la persistance atomique sur disque ;
- le verrouillage multi-processus ;
- l’authentification HMAC ;
- la signature numérique ;
- l’intégration ELMAN Studio.
