# Artifact Orchestration Selected State Restoration ELMAN-OS v0.7

## Statut

Vingt-et-unième incrément du Jalon 2 — Planification et orchestration.

Ce module relie la sélection déterministe d’un état d’orchestration à la
frontière de restauration vérifiée existante.

Il accepte uniquement un
`ArtifactOrchestrationStateSelectionResult` cryptographiquement valide en
statut `selected`, restaure exactement le `persistence_id` sélectionné, puis
vérifie que l’état relu correspond aux identités et empreintes stables de
l’entrée d’index choisie.

Le module ne reprend pas le plan, ne modifie aucun état, n’exécute aucun agent
et ne déclenche aucune opération dans le workspace utilisateur.

## Fichiers

| Fichier | Rôle |
|---|---|
| `src/elman_os/artifact_orchestration_selected_state_restoration.py` | Transforme un résultat de sélection vérifié en restauration strictement liée à l’entrée choisie. |
| `tests/test_artifact_orchestration_selected_state_restoration.py` | Vérifie les contrats, les liaisons, les refus fail-closed, l’absence de mutation et la restauration des états ready, blocked et terminal. |
| `docs/ARTIFACT-ORCHESTRATION-SELECTED-STATE-RESTORATION-v0.7.md` | Documente la frontière de sélection-restauration et ses garanties. |

## Contrats

### `ArtifactOrchestrationSelectedStateRestorationPolicy`

La politique contient :

- un `policy_id` stable ;
- une `ArtifactOrchestrationRestorationPolicy` complète et immuable ;
- une version de format ;
- un hash SHA-256 déterministe calculé sur la politique imbriquée.

La frontière ne réimplémente pas les règles de lecture des fichiers persistés.
Elle délègue cette responsabilité au module de restauration déjà validé.

### `ArtifactOrchestrationSelectedStateRestorationRequest`

La requête contient :

- un identifiant déterministe ;
- l’identifiant et le hash de la politique ;
- le résultat de sélection sous forme JSON canonique ;
- le hash attendu du résultat de sélection ;
- la racine absolue de persistance ;
- le demandeur ;
- l’horodatage UTC ;
- le hash SHA-256 de la requête.

`from_selection_result()` construit l’identifiant à partir :

- du hash de politique ;
- du hash du résultat de sélection ;
- de la racine de persistance.

Les mêmes entrées produisent donc le même identifiant.

### `ArtifactOrchestrationSelectedStateRestorationResult`

Le résultat contient :

- le statut `restored` ;
- le hash de la requête source ;
- les liaisons de politique ;
- le résultat de sélection canonique ;
- le hash de la requête de restauration déléguée ;
- le résultat de restauration canonique ;
- l’horodatage final ;
- une raison explicite ;
- un hash SHA-256 du résultat complet.

Le résultat embarque les deux frontières nécessaires à une vérification
indépendante : la décision de sélection et la restauration effectivement
produite.

### `ArtifactOrchestrationSelectedStateRestoration`

L’exécuteur :

1. vérifie la requête ;
2. vérifie la politique ;
3. vérifie le résultat de sélection ;
4. exige le statut `selected` ;
5. vérifie l’entrée sélectionnée et son hash ;
6. vérifie que son répertoire correspond à `state_root/storage_key` ;
7. construit une requête de restauration liée au `persistence_id` choisi ;
8. impose le `manifest_hash` attendu ;
9. impose le `orchestration_result_hash` attendu ;
10. délègue la lecture au restaurateur existant ;
11. vérifie chaque liaison stable entre l’entrée et l’état restauré ;
12. produit un résultat canonique et hashé.

## Conditions d’acceptation de la sélection

Le résultat de sélection doit :

- être une instance de `ArtifactOrchestrationStateSelectionResult` ;
- réussir `verify_hash()` ;
- avoir le statut `selected` ;
- contenir exactement une entrée choisie cohérente avec son record ;
- exposer une entrée en statut `valid` ;
- conserver toutes les métadonnées obligatoires d’une entrée valide.

Les statuts suivants sont refusés :

```text
no-match
ambiguous
```

Aucune restauration n’est tentée après ce refus.

## Liaison du chemin persistant

Le répertoire sélectionné doit être exactement :

```text
<state_root>/<storage_key>
```

Le `storage_key` est lui-même lié au `persistence_id` par le contrat de l’index.

Le module refuse :

- une autre racine ;
- un autre nom de répertoire ;
- un chemin relatif ;
- un répertoire sélectionné hors de la racine fournie.

Les contrôles de liens symboliques et de fichiers réguliers restent appliqués
par la politique de restauration déléguée.

## Liaisons vérifiées après restauration

Le résultat de restauration doit correspondre à l’entrée sélectionnée sur :

- `state_root` ;
- `state_directory` ;
- `persistence_id` ;
- `manifest_hash` ;
- `orchestration_result_hash`.

L’état restauré embarqué doit également correspondre sur :

- `persistence_id` ;
- `manifest_hash` ;
- `orchestration_result_hash` ;
- `plan_id` ;
- `project_id` ;
- `checkpoint_id` ;
- `assessment_status` ;
- `can_resume`.

Toute divergence produit un refus fail-closed.

## Traitement du `state_hash`

L’entrée d’index conserve le `state_hash` de la restauration effectuée pendant
l’indexation. Ce hash inclut l’horodatage `restored_at` de cette observation.

Une nouvelle restauration contrôlée possède un autre horodatage et produit donc
légitimement un nouveau `state_hash`, même lorsque tous les octets persistés et
toutes les identités sont identiques.

Le module :

- vérifie le hash de l’entrée sélectionnée, qui protège le `state_hash`
  d’indexation ;
- vérifie séparément le nouveau `state_hash` produit par la restauration ;
- compare toutes les liaisons stables listées ci-dessus ;
- ne demande pas une égalité incorrecte entre deux hashes dépendant de temps
  d’observation différents.

## États de reprise

La frontière peut restaurer un checkpoint évalué :

```text
ready
blocked
terminal
```

Elle ne transforme pas cette évaluation en commande de reprise.

Un état `ready` reste seulement une donnée vérifiée avec `can_resume = true`.
Une autorisation humaine et une commande explicite restent nécessaires avant
toute transition d’exécution.

## Idempotence et déterminisme

Pour les mêmes :

- politique ;
- résultat de sélection ;
- racine ;
- demandeur ;
- horodatage ;
- état persistant inchangé ;

le module produit :

- le même identifiant ;
- la même requête de restauration déléguée ;
- le même résultat canonique ;
- le même hash final.

Aucune écriture n’est utilisée pour obtenir cette idempotence.

## Échecs délégués

Une erreur de restauration telle que :

- état supprimé après sélection ;
- manifest absent ou altéré ;
- payload manquant ;
- hash de fichier divergent ;
- checkpoint incompatible ;
- lien symbolique interdit ;
- lecture impossible ;

est encapsulée dans
`ArtifactOrchestrationSelectedStateRestorationExecutionError` avec la cause
originale conservée.

## Sérialisation

La politique, la requête et le résultat fournissent :

- `to_dict()` ;
- `to_json()` ;
- `from_dict()` ;
- `from_json()`.

La requête et le résultat fournissent `verify_hash()`.

Toutes les représentations utilisent le JSON canonique ELMAN-OS.

## Tests spécifiques

La suite vérifie notamment :

- déterminisme des politiques et identifiants ;
- sérialisation canonique ;
- immutabilité des contrats ;
- refus des sélections `no-match` ;
- refus des chemins hors racine ;
- refus des hashes altérés ;
- restauration d’un état `ready` ;
- restauration d’un état `blocked` ;
- restauration d’un état `terminal` ;
- liaison au manifest sélectionné ;
- liaison au plan sélectionné ;
- suppression de l’état après sélection ;
- altération du manifest après sélection ;
- absence de mutation de l’arbre persistant ;
- déterminisme du résultat final ;
- détection des résultats sérialisés altérés.

## Garanties

- seule une sélection explicite et vérifiée autorise la lecture ;
- aucune recherche implicite d’un autre état ;
- aucun fallback vers un état plus ancien ;
- aucune résolution silencieuse d’ambiguïté ;
- aucune restauration d’une entrée `altered` ou `unreadable` ;
- aucune reprise automatique ;
- aucune transition du plan ;
- aucune écriture dans le journal ;
- aucune modification de checkpoint ;
- aucune modification de l’état persistant ;
- aucune modification du workspace utilisateur ;
- aucune exécution de contenu ;
- aucune importation dynamique ;
- aucun appel à un fournisseur IA ;
- aucune connexion réseau ;
- aucune modification de version, tag ou release.

## Hors périmètre

Cet incrément ne fournit pas encore :

- une commande automatique de reprise ;
- l’application automatique d’une approbation ;
- un pointeur persistant `latest` ;
- la persistance du résultat de sélection ;
- une politique de rétention ;
- la suppression ou réparation des états altérés ;
- la réplication distribuée ;
- l’intégration au runtime principal ;
- l’intégration ELMAN Studio ;
- la supervision métacognitive.
