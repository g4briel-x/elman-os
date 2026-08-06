# Artifact Orchestration Selected-State Resume Application ELMAN-OS v0.7

## Statut

Vingt-troisième incrément du Jalon 2 — Planification et orchestration.

Ce module relie une autorisation de reprise vérifiée à la frontière
`ResumeApplication` déjà présente dans ELMAN-OS. Il applique uniquement en
mémoire la `ResumeCommand` autorisée au plan et au journal restaurés, puis
retourne les représentations mises à jour avec leurs preuves d’intégrité.

Aucune donnée n’est persistée par ce module. Aucune étape n’est exécutée et
aucun agent n’est dispatché.

## Fichiers

| Fichier | Rôle |
|---|---|
| `src/elman_os/artifact_orchestration_selected_state_resume_application.py` | Vérifie l’autorisation et applique sa commande au plan et au journal restaurés par copy-on-write. |
| `tests/test_artifact_orchestration_selected_state_resume_application.py` | Vérifie les contrats, les liaisons, l’application, l’idempotence logique et l’absence de mutation. |
| `docs/ARTIFACT-ORCHESTRATION-SELECTED-STATE-RESUME-APPLICATION-v0.7.md` | Documente la frontière d’application et ses garanties fail-closed. |

## Objectif

L’incrément précédent produit une décision de reprise déclarative et, lorsque
la décision est approuvée, une `ResumeCommand`. Le présent incrément consomme ce
résultat et prépare l’état d’orchestration à la reprise :

1. l’autorisation est vérifiée ;
2. son statut doit être `approved` ;
3. sa commande est vérifiée ;
4. le plan, le journal et le checkpoint exacts sont reconstruits depuis l’état
   sélectionné puis restauré ;
5. la commande est appliquée par `ResumeApplication` ;
6. la source est comparée avant et après l’opération ;
7. un résultat canonique et hashé est produit.

## Contrats ajoutés

### `ArtifactOrchestrationSelectedStateResumeApplicationPolicy`

La politique contient :

- un `policy_id` stable ;
- l’obligation d’utiliser une autorisation approuvée ;
- l’obligation de préserver l’immuabilité de l’état source ;
- une option permettant ou refusant un résultat `already-applied` ;
- un hash SHA-256 déterministe.

Les deux premières garanties ne peuvent pas être désactivées.

### `ArtifactOrchestrationSelectedStateResumeApplicationRequest`

La requête contient :

- un identifiant déterministe ;
- la politique canonique et son hash ;
- le résultat d’autorisation canonique et son hash ;
- l’agent ELMAN-OS demandeur ;
- un horodatage UTC ;
- une justification explicite ;
- un hash SHA-256 du document complet.

La requête ne peut pas être antérieure à l’autorisation qu’elle consomme.

Son identifiant suit la forme :

```text
resume-application-request:<identity-hash>
```

L’empreinte d’identité couvre la politique, l’autorisation, le demandeur,
l’horodatage et la justification.

### `ArtifactOrchestrationSelectedStateResumeApplicationResult`

Le résultat contient :

- le statut `applied` ou `already-applied` ;
- la requête complète ;
- le `ResumeApplicationResult` canonique ;
- l’horodatage d’application ;
- une raison explicite ;
- un hash SHA-256 du résultat complet.

Il expose également :

- `updated_plan` ;
- `updated_journal` ;
- `verify_hash()` ;
- `to_dict()` et `to_json()` ;
- `from_dict()` et `from_json()`.

### `ArtifactOrchestrationSelectedStateResumeApplication`

L’exécuteur :

1. vérifie la requête et sa politique ;
2. vérifie le résultat d’autorisation ;
3. exige le statut `approved` ;
4. exige une `ResumeCommand` vérifiée ;
5. retrouve l’état restauré exact lié à l’autorisation ;
6. reconstruit le plan, le journal et le checkpoint ;
7. capture les représentations canoniques de la source ;
8. délègue l’application à `ResumeApplication` ;
9. vérifie le résultat délégué ;
10. confirme que la source n’a pas été modifiée ;
11. lie le résultat à la commande, au checkpoint et au plan ;
12. produit un résultat canonique et hashé.

## Autorisation requise

Le module refuse :

- une autorisation `rejected` ;
- une autorisation altérée ;
- une autorisation sans commande ;
- une commande dont le hash est invalide ;
- une commande liée à un autre checkpoint ;
- une commande liée à un autre plan ;
- une politique différente de celle embarquée dans la requête.

Aucune autorisation implicite n’est créée.

## Application en mémoire

La délégation à `ResumeApplication` produit une copie mise à jour :

- les étapes sélectionnées passent à `approved` ;
- la référence d’approbation humaine est conservée ;
- les événements `step.approved` sont ajoutés au journal copié ;
- un plan `pending` peut recevoir l’événement `plan.approved` ;
- les hashes avant et après sont enregistrés ;
- les séquences ajoutées sont contiguës et vérifiées.

Le plan et le journal d’origine restent inchangés.

## Liaison cryptographique

Le résultat d’application doit correspondre à l’autorisation sur :

- `command_id` ;
- `command_hash` ;
- `checkpoint_id` ;
- `checkpoint_hash` ;
- `plan_id` ;
- `selected_step_ids`.

Toute divergence provoque un refus d’intégrité.

## Statuts

### `applied`

La commande a été appliquée à une copie de l’état restauré. Le résultat contient
au moins un événement ajouté et un plan mis à jour vérifiable.

### `already-applied`

Le contrat sous-jacent peut reconnaître les marqueurs d’une commande déjà
appliquée dans un journal source compatible. La politique peut accepter ou
refuser explicitement ce statut.

Dans le chemin normal de cet incrément, l’état restauré est celui qui a servi à
l’autorisation ; une répétition avec les mêmes entrées reproduit donc le même
résultat `applied` sans muter la source.

## Déterminisme

Pour les mêmes :

- autorisation ;
- politique ;
- demandeur ;
- horodatage ;
- justification ;
- état restauré inchangé ;

le module produit :

- le même identifiant de requête ;
- le même résultat d’application ;
- le même plan mis à jour ;
- le même journal mis à jour ;
- le même hash final.

## Absence de mutation

Avant et après l’application, le module compare :

- le résultat d’autorisation ;
- le plan restauré ;
- le journal restauré ;
- le checkpoint restauré.

Une différence inattendue produit une erreur d’intégrité.

## Limites de responsabilité

Cet incrément ne réalise pas :

- la persistance du plan mis à jour ;
- la persistance du journal mis à jour ;
- la création d’un nouveau checkpoint durable ;
- l’exécution d’une étape ;
- le dispatch d’un agent ;
- l’appel à un fournisseur IA ;
- l’écriture dans le workspace utilisateur ;
- la modification de l’état persistant source ;
- une connexion réseau ;
- une modification de version, de tag ou de release.

## Tests spécifiques

La suite vérifie notamment :

- le déterminisme de la politique ;
- la sérialisation canonique ;
- l’immuabilité des contrats ;
- l’impossibilité de désactiver les garanties obligatoires ;
- la liaison de la requête à l’autorisation ;
- le refus d’un horodatage antérieur ;
- le refus des hashes altérés ;
- l’application d’une autorisation prête ;
- l’application de plusieurs étapes ;
- la conservation de la référence d’approbation ;
- l’ajout des événements attendus ;
- la liaison à la commande autorisée ;
- l’absence de mutation de la source ;
- le refus d’une autorisation rejetée ;
- le déterminisme des applications répétées ;
- la vérification du journal produit ;
- la détection d’un résultat embarqué altéré.

## Garanties finales

- autorisation approuvée obligatoire ;
- commande vérifiée obligatoire ;
- état restauré exact obligatoire ;
- application par la frontière existante ;
- comportement fail-closed ;
- copy-on-write du plan et du journal ;
- sortie entièrement sérialisable et vérifiable ;
- aucune persistance automatique ;
- aucune exécution d’agent ;
- aucun accès réseau.
