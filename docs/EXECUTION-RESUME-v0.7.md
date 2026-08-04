# Controlled Execution Resume ELMAN-OS v0.7

## Statut

Quatrième incrément du Jalon 2 — Planification et orchestration.

Ce module transforme un `ResumeAssessment` valide en une décision
d’autorisation ou de refus. Une décision approuvée contient une commande
déclarative sérialisable, mais aucune étape ni aucun agent n’est exécuté.

## Fichiers

| Fichier | Rôle |
|---|---|
| `src/elman_os/execution_resume.py` | Définit les politiques, requêtes, décisions et commandes de reprise contrôlée. |
| `tests/test_execution_resume.py` | Vérifie les approbations, politiques, refus, hashes et sélections déterministes. |
| `docs/EXECUTION-RESUME-v0.7.md` | Documente les contrats, invariants, décisions et limites. |

## Contrats

### `ResumePolicy`

La politique fixe les règles d’autorisation :

- identifiant stable ;
- stratégie `ready-only` ;
- approbation humaine obligatoire ;
- liste facultative d’étapes autorisées ;
- nombre maximal facultatif d’étapes ;
- hash déterministe de la politique.

La désactivation de l’approbation humaine est refusée.

### `ResumeRequest`

La requête contient :

- `request_id` ;
- référence explicite au checkpoint ;
- hash exact du checkpoint ;
- `plan_id` ;
- agent demandeur ;
- référence d’approbation humaine ;
- horodatage UTC ;
- justification ;
- sélection facultative d’étapes.

Une sélection vide signifie : appliquer la politique aux étapes déclarées
prêtes par le `ResumeAssessment`.

### `ResumeDecision`

La décision est soit :

- `approved` ;
- `rejected`.

Une décision approuvée exige :

- au moins une étape sélectionnée ;
- une commande de reprise ;
- une cohérence exacte entre la décision et la commande.

Une décision rejetée ne contient ni commande ni étape sélectionnée.

### `ResumeCommand`

La commande est une autorisation déclarative, non un moteur d’exécution.

Elle contient :

- identifiant de commande ;
- identifiant et hash de la requête ;
- identifiant et hash de la politique ;
- identifiant et hash du checkpoint ;
- `plan_id` ;
- référence d’approbation ;
- étapes autorisées ;
- horodatage d’émission ;
- hash SHA-256 de la commande.

## Validation du checkpoint et de l’évaluation

`decide_resume()` applique un contrôle fail-closed.

La décision est rejetée lorsque :

- le checkpoint échoue à la vérification d’intégrité ;
- l’identifiant du checkpoint diffère ;
- le hash du checkpoint diffère ;
- le `plan_id` diffère ;
- l’évaluation référence un autre checkpoint ;
- le checkpoint est bloqué ou terminal ;
- l’évaluation n’est pas `ready` ;
- `can_resume` est faux ;
- le nombre d’événements diffère ;
- le hash de tête du journal diffère ;
- l’évaluation référence une étape inconnue ;
- une étape non prête est déclarée prête ;
- une étape demandée n’est pas autorisée ou reprenable ;
- aucune étape ne reste après application de la politique.

Les checkpoints `stale`, `blocked`, `terminal` et les évaluations
`incompatible` sont donc refusés.

## Sélection déterministe

La sélection suit cet ordre :

1. prendre les `ready_step_ids` validés ;
2. les trier lexicalement ;
3. appliquer `allowed_step_ids` ;
4. appliquer les étapes explicitement demandées ;
5. appliquer `max_steps` ;
6. refuser une sélection vide.

Les étapes `running` ne sont pas considérées comme prêtes dans cet incrément.
Une relance d’étape en cours nécessitera une politique dédiée ultérieure.

## Approbation humaine

L’approbation est obligatoire à deux niveaux :

- la politique ne peut pas la désactiver ;
- la requête doit contenir une référence d’approbation valide.

La référence est recopiée dans la commande approuvée pour assurer la
traçabilité.

## Intégrité

Les artefacts suivants exposent un hash déterministe :

- `ResumePolicy.policy_hash` ;
- `ResumeRequest.request_hash` ;
- `ResumeCommand.command_hash` ;
- `ResumeDecision.decision_hash`.

Les hashes utilisent le JSON canonique et SHA-256. Toute modification d’une
commande ou d’une décision sérialisée est détectée à la reconstruction ou par
`verify_hash()`.

## Sérialisation

Les politiques, requêtes, commandes et décisions fournissent :

- `to_dict()` ;
- `to_json()` ;
- `from_dict()` ;
- `from_json()`.

Le format est compact, trié et déterministe.

## Garanties

- approbation humaine obligatoire ;
- référence explicite au checkpoint ;
- liaison cryptographique aux artefacts précédents ;
- sélection déterministe des étapes prêtes ;
- refus fail-closed ;
- commande déclarative immuable ;
- aucune exécution automatique ;
- aucune relance d’agent ;
- aucune mutation du plan ;
- aucune mutation du journal ;
- aucune écriture dans le projet ;
- aucune connexion réseau ;
- aucun changement de version ou de release.

## Hors périmètre

Cet incrément ne fournit pas encore :

- l’application de la commande ;
- l’exécution ou la relance d’un agent ;
- la transition des statuts du plan ;
- l’ajout automatique d’événements au journal ;
- la persistance atomique ;
- le verrouillage multi-processus ;
- l’authentification HMAC ;
- la signature numérique ;
- la reprise d’étapes `running` ;
- l’intégration ELMAN Studio.
