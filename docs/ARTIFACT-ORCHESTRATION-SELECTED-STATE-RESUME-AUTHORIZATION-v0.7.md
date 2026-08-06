# Artifact Orchestration Selected-State Resume Authorization ELMAN-OS v0.7

## Statut

Vingt-deuxième incrément du Jalon 2 — Planification et orchestration.

Ce module relie un état d’orchestration sélectionné puis restauré à la frontière
d’autorisation déclarative de reprise déjà présente dans ELMAN-OS.

Il exige une approbation humaine explicite, limitée à des étapes nommées, puis
recalcule l’évaluation de reprise depuis le plan, le journal et le checkpoint
restaurés. Il produit ensuite une décision `approved` ou `rejected` ainsi qu’une
`ResumeCommand` uniquement lorsque toutes les contraintes sont satisfaites.

La commande produite reste déclarative. Elle n’est pas appliquée par ce module.

## Fichiers

| Fichier | Rôle |
|---|---|
| `src/elman_os/artifact_orchestration_selected_state_resume_authorization.py` | Lie la restauration sélectionnée, l’approbation humaine et la décision déclarative de reprise. |
| `tests/test_artifact_orchestration_selected_state_resume_authorization.py` | Vérifie les contrats, l’intégrité, les périmètres d’approbation, les décisions et l’absence de mutation. |
| `docs/ARTIFACT-ORCHESTRATION-SELECTED-STATE-RESUME-AUTHORIZATION-v0.7.md` | Documente la frontière d’autorisation de reprise et ses garanties fail-closed. |

## Contrats ajoutés

### `ArtifactOrchestrationHumanResumeApproval`

Ce contrat représente une preuve explicite fournie à la frontière
ELMAN-OS. Il contient :

- un identifiant d’approbation ;
- une référence d’approbation ;
- le principal humain déclarant l’approbation ;
- l’horodatage UTC ;
- une déclaration lisible ;
- la liste exacte des étapes approuvées ;
- le hash du résultat de restauration sélectionnée ;
- un hash SHA-256 du contrat complet.

L’approbation est liée à un seul
`ArtifactOrchestrationSelectedStateRestorationResult`.

Une liste d’étapes vide est refusée. L’autorisation ne peut jamais élargir ce
périmètre.

Le contrat fournit :

- `for_restoration()` ;
- `to_dict()` ;
- `to_json()` ;
- `from_dict()` ;
- `from_json()` ;
- `verify_hash()`.

Cette preuve enregistre une déclaration explicite et son intégrité. Elle ne
prétend pas vérifier à elle seule l’identité réelle du principal ni fournir une
signature asymétrique.

### `ArtifactOrchestrationSelectedStateResumeAuthorizationPolicy`

La politique contient :

- un `policy_id` stable ;
- une `ResumePolicy` complète ;
- l’obligation de recalculer et comparer l’évaluation de reprise ;
- l’obligation de conserver un périmètre d’étapes explicite ;
- un hash SHA-256 déterministe.

Les deux contrôles de sécurité doivent rester activés.

La `ResumePolicy` imbriquée continue d’imposer :

- une approbation humaine obligatoire ;
- la stratégie `ready-only` ;
- une liste optionnelle d’étapes autorisées ;
- une limite optionnelle du nombre d’étapes.

### `ArtifactOrchestrationSelectedStateResumeAuthorizationRequest`

La requête contient :

- un identifiant déterministe ;
- la politique canonique et son hash ;
- le résultat canonique de restauration sélectionnée et son hash ;
- l’approbation humaine canonique et son hash ;
- le demandeur ELMAN-OS ;
- l’horodatage UTC ;
- la justification ;
- les étapes demandées ;
- un hash SHA-256 de la requête complète.

`from_restoration_result()` calcule l’identité depuis :

- le hash de politique ;
- le hash de restauration ;
- le hash d’approbation ;
- le demandeur ;
- l’horodatage ;
- la justification ;
- les étapes demandées.

Les étapes demandées doivent être un sous-ensemble exact des étapes approuvées.

L’horodatage de la demande ne peut pas précéder l’approbation.

### `ArtifactOrchestrationSelectedStateResumeAuthorizationResult`

Le résultat contient :

- le statut `approved` ou `rejected` ;
- la requête d’autorisation canonique ;
- la `ResumeRequest` dérivée ;
- la `ResumeAssessment` recalculée ;
- la `ResumeDecision` canonique ;
- l’horodatage final ;
- une raison explicite ;
- un hash SHA-256 du résultat complet.

Le résultat est autonome pour les vérifications de liaison. Il permet de
retrouver :

- la restauration sélectionnée ;
- la preuve d’approbation ;
- la politique ;
- la requête déclarative ;
- l’évaluation ;
- la décision ;
- la commande éventuelle.

### `ArtifactOrchestrationSelectedStateResumeAuthorization`

L’exécuteur :

1. vérifie la requête et la politique ;
2. vérifie le résultat de restauration sélectionnée ;
3. vérifie l’approbation et son périmètre ;
4. reconstruit le plan, le journal et le checkpoint ;
5. vérifie le hash du checkpoint ;
6. recalcule `ResumeAssessment` ;
7. compare cette évaluation à celle de l’état restauré ;
8. crée une `ResumeRequest` liée à la requête d’autorisation ;
9. délègue la décision à `decide_resume()` ;
10. vérifie la décision et sa commande éventuelle ;
11. confirme qu’aucun objet source n’a été modifié ;
12. produit un résultat canonique et hashé.

## Approbation humaine explicite

Aucune approbation n’est inventée ou déduite.

La frontière exige :

- une référence non vide ;
- un principal humain déclaré ;
- un horodatage UTC ;
- une déclaration non vide ;
- au moins une étape approuvée ;
- une liaison au hash exact du résultat restauré.

L’autorisation refuse :

- une approbation destinée à une autre restauration ;
- une demande antérieure à l’approbation ;
- une étape demandée hors du périmètre approuvé ;
- un hash d’approbation altéré ;
- une représentation non canonique incohérente.

## Recalcul de l’évaluation de reprise

La frontière ne se contente pas du statut embarqué dans l’état restauré.

Elle reconstruit :

- `ExecutionPlan` ;
- `ExecutionJournal` ;
- `ExecutionCheckpoint`.

Puis elle exécute :

```text
checkpoint.assess_resume(plan, journal)
```

Le JSON canonique recalculé doit être identique à l’évaluation conservée dans
l’état restauré.

Une divergence provoque un refus d’intégrité avant toute décision.

## Décisions

### `approved`

Une décision est approuvée uniquement lorsque :

- le checkpoint est `ready` ;
- `can_resume = true` ;
- les compteurs et le head hash du journal correspondent ;
- les étapes demandées sont réellement prêtes ;
- les étapes respectent la `ResumePolicy` ;
- le périmètre reste inclus dans l’approbation humaine ;
- au moins une étape subsiste après application des limites.

Le résultat contient alors une `ResumeCommand` vérifiée.

### `rejected`

Une décision rejetée ne contient aucune commande.

Les cas incluent notamment :

- checkpoint `blocked` ;
- checkpoint `terminal` ;
- évaluation non prête ;
- étape demandée non disponible ;
- filtre de politique excluant toutes les étapes ;
- aucune étape reprenable après application des limites.

Le rejet est un résultat métier déterministe, pas nécessairement une exception.

## Liaison de la `ResumeRequest`

La requête déclarative produite est liée à :

- l’identifiant de la requête d’autorisation ;
- l’identifiant et le hash du checkpoint ;
- l’identifiant du plan ;
- la référence d’approbation ;
- le demandeur ;
- l’horodatage ;
- la justification ;
- les étapes demandées.

Son identifiant suit la forme :

```text
resume-request:<authorization_request_hash>
```

## Liaison de la décision et de la commande

La décision doit correspondre à :

- la `ResumeRequest` ;
- la `ResumePolicy` ;
- le checkpoint restauré ;
- l’évaluation recalculée.

Une commande approuvée doit conserver :

- la référence d’approbation humaine ;
- le checkpoint et son hash ;
- le plan ;
- la politique ;
- le périmètre d’étapes sélectionné.

Les étapes de la décision doivent rester des sous-ensembles :

- des étapes demandées ;
- des étapes explicitement approuvées.

## Limites de politique

`allowed_step_ids` peut restreindre les étapes considérées.

`max_steps` peut réduire de façon déterministe le nombre d’étapes autorisées.

Ces contrôles ne peuvent jamais élargir le périmètre humain.

## Idempotence

Pour les mêmes :

- restauration sélectionnée ;
- approbation ;
- politique ;
- demandeur ;
- horodatage ;
- justification ;
- étapes demandées ;

la frontière produit :

- le même identifiant d’autorisation ;
- la même `ResumeRequest` ;
- la même évaluation ;
- la même décision ;
- le même hash final.

## Absence de mutation

Avant et après la décision, le module compare les représentations canoniques :

- du résultat de restauration ;
- du plan ;
- du journal ;
- du checkpoint.

Toute mutation inattendue déclenche un refus d’intégrité.

## Sérialisation

Les politiques, approbations, requêtes et résultats utilisent le JSON canonique
ELMAN-OS.

Les objets hashés fournissent `verify_hash()`.

## Tests spécifiques

La suite vérifie notamment :

- déterminisme et sérialisation des politiques ;
- immutabilité des contrats ;
- déterminisme de l’approbation ;
- refus d’un périmètre vide ;
- détection d’un hash d’approbation altéré ;
- refus d’une approbation liée à une autre restauration ;
- refus d’une demande antérieure à l’approbation ;
- refus d’étapes hors périmètre ;
- approbation d’un état `ready` ;
- rejet d’un état `blocked` ;
- rejet d’un état `terminal` ;
- filtrage par étapes autorisées ;
- limitation par `max_steps` ;
- rejet d’une étape indisponible ;
- conservation de la référence d’approbation ;
- déterminisme du résultat ;
- absence de mutation ;
- détection des résultats sérialisés altérés.

## Garanties

- aucune approbation implicite ;
- aucune extension silencieuse du périmètre humain ;
- évaluation de reprise recalculée ;
- comportement fail-closed sur divergence ;
- décisions et commandes liées par SHA-256 ;
- aucune application de `ResumeCommand` ;
- aucune transition du plan ;
- aucun ajout d’événement au journal ;
- aucune écriture de checkpoint ;
- aucune écriture dans l’état persistant ;
- aucune modification du workspace ;
- aucune exécution d’agent ;
- aucune importation dynamique ;
- aucun appel à un fournisseur IA ;
- aucune connexion réseau ;
- aucune modification de version, tag ou release.

## Hors périmètre

Cet incrément ne fournit pas encore :

- l’application de la `ResumeCommand` à l’état restauré ;
- la persistance du plan et du journal repris ;
- le démarrage réel d’une étape ;
- l’exécution ou la relance d’un agent ;
- une signature asymétrique de l’approbation ;
- la vérification d’identité via un fournisseur externe ;
- la révocation persistante d’une approbation ;
- un pointeur persistant `latest` ;
- une politique de rétention ;
- l’intégration au runtime principal ;
- l’intégration ELMAN Studio ;
- la supervision métacognitive.
