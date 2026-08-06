# Artifact Orchestration Selected-State Resume Persistence ELMAN-OS v0.7

## Statut

Vingt-quatrième incrément du Jalon 2 — Planification et orchestration.

Cet incrément transforme le résultat en mémoire de
`ArtifactOrchestrationSelectedStateResumeApplication` en un nouvel état
d’orchestration durable, atomique et restaurable. L’état persisté source reste
immuable et ne peut jamais être utilisé comme destination.

## Fichiers

| Fichier | Rôle |
|---|---|
| `src/elman_os/artifact_orchestration_selected_state_resume_persistence.py` | Capture un checkpoint neuf et persiste atomiquement le plan et le journal repris. |
| `tests/test_artifact_orchestration_selected_state_resume_persistence.py` | Vérifie les contrats, la persistance, l’idempotence, la restauration et l’absence de mutation. |
| `docs/ARTIFACT-ORCHESTRATION-SELECTED-STATE-RESUME-PERSISTENCE-v0.7.md` | Documente les contrats, les liaisons cryptographiques et les garanties fail-closed. |

## Objectif

La chaîne de reprise contrôlée fournit désormais :

1. la sélection d’un état persisté ;
2. sa restauration vérifiée en lecture seule ;
3. une approbation humaine explicite ;
4. une décision de reprise déclarative ;
5. l’application copy-on-write de la commande au plan et au journal ;
6. la persistance atomique du nouvel état repris.

L’incrément 24 réalise uniquement la sixième étape.

## Contrats ajoutés

### `ArtifactOrchestrationSelectedStateResumePersistencePolicy`

La politique contient :

- un identifiant stable ;
- une `ArtifactOrchestrationPersistencePolicy` canonique embarquée ;
- le hash de cette politique de stockage ;
- l’obligation d’accepter uniquement une application réussie ;
- l’obligation d’utiliser un nouvel identifiant de persistance ;
- l’obligation de conserver l’état source immuable ;
- un hash SHA-256 déterministe.

Les trois garanties obligatoires ne peuvent pas être désactivées.

### `ArtifactOrchestrationSelectedStateResumePersistenceRequest`

La requête contient :

- un identifiant déterministe ;
- la politique complète et son hash ;
- le résultat d’application de reprise complet et son hash ;
- l’identifiant de persistance source ;
- un nouvel identifiant de persistance destination ;
- l’identifiant du nouveau checkpoint ;
- une racine absolue de stockage ;
- l’agent ELMAN-OS demandeur ;
- un horodatage UTC ;
- une justification explicite ;
- un hash SHA-256 du document complet.

`from_application_result()` dérive par défaut des identifiants déterministes :

```text
resume-state:<sha256>
resume-checkpoint:<sha256>
resume-persistence-request:<sha256>
```

La destination doit être différente de l’état source. Une demande antérieure à
l’application de reprise est refusée.

### `ArtifactOrchestrationSelectedStateResumePersistenceResult`

Le résultat contient :

- le statut `persisted` ou `noop` ;
- la requête canonique ;
- le nouveau checkpoint et son hash ;
- le résultat de persistance standard ELMAN-OS et son hash ;
- l’identifiant de l’état source ;
- l’horodatage final ;
- une raison explicite ;
- un hash SHA-256 du résultat complet.

Le résultat vérifie que le manifest persistant référence le checkpoint capturé
et le résultat d’application exact.

### `ArtifactOrchestrationSelectedStateResumePersistence`

La frontière :

1. vérifie la requête, la politique et le résultat d’application ;
2. vérifie l’état restauré source ;
3. capture les représentations canoniques de la source ;
4. récupère le plan et le journal copy-on-write ;
5. valide le journal ;
6. capture un nouveau `ExecutionCheckpoint` ;
7. vérifie que la préparation n’a pas modifié la source ;
8. construit les trois payloads canoniques ;
9. construit un `ArtifactOrchestrationStateManifest` standard ;
10. acquiert un verrou exclusif ;
11. écrit et vérifie les fichiers dans un staging privé ;
12. renomme atomiquement le staging vers la destination finale ;
13. relit et vérifie l’état final ;
14. confirme de nouveau l’immuabilité de la source ;
15. retourne un résultat canonique et hashé.

## Nouveau checkpoint

Le checkpoint destination est capturé depuis :

- le plan mis à jour par `ResumeApplication` ;
- le journal mis à jour par `ResumeApplication` ;
- l’identifiant de checkpoint déterministe de la requête ;
- l’horodatage UTC de persistance.

`ExecutionCheckpoint.capture()` vérifie la compatibilité du plan et du journal
avant de produire le checkpoint.

Le checkpoint source n’est ni réutilisé ni modifié.

## Format de stockage

Le nouvel état utilise le format standard déjà pris en charge par les frontières
d’indexation et de restauration :

```text
<state-root>/
├── .locks/
├── .staging/
└── <sha256(persistence_id)>/
    ├── execution-plan.json
    ├── execution-journal.jsonl
    ├── execution-checkpoint.json
    └── manifest.json
```

Le manifest est un `ArtifactOrchestrationStateManifest` et contient notamment :

- le nouvel identifiant de persistance ;
- le hash de la requête ;
- le hash de la politique ;
- le hash du résultat d’application ;
- les identifiants du plan et du projet ;
- le hash d’état du plan ;
- le hash du journal ;
- le hash du checkpoint ;
- la taille et le SHA-256 de chaque payload.

## Atomicité

Les fichiers sont écrits dans un staging privé. Chaque payload est écrit dans un
fichier temporaire puis déplacé avec `os.replace`. Le répertoire complet est
ensuite déplacé avec `os.rename` vers sa destination finale.

Le répertoire final n’est jamais écrasé.

Un échec pendant un staging créé par l’appel déclenche son nettoyage contrôlé.
Un staging préexistant complet et strictement identique peut être repris.

## Idempotence

Une répétition avec les mêmes :

- résultat d’application ;
- politique ;
- racine de stockage ;
- identifiants ;
- demandeur ;
- horodatage ;
- justification ;

produit la même destination.

Si l’état final est strictement identique, le statut est `noop`.
Si un fichier ou le manifest diverge, la persistance échoue fermée.

## Compatibilité avec la restauration existante

Le nouvel état peut être lu par `ArtifactOrchestrationStateRestoration` sans
adaptateur supplémentaire, car il conserve :

- les quatre noms de fichiers standards ;
- le manifest standard ;
- les sérialisations canoniques ;
- les liaisons plan, journal et checkpoint ;
- les hashes attendus par la frontière de restauration.

## Verrouillage

Le verrou est créé avec `O_CREAT | O_EXCL` dans `.locks`.

Il contient uniquement :

- l’identifiant de la requête ;
- l’identifiant de persistance ;
- le hash de la requête.

Sa disparition, sa transformation en lien symbolique ou la modification de son
contenu avant libération provoque une erreur dédiée.

## Protection des chemins

La frontière :

- exige une racine absolue ;
- refuse les composants symboliques lorsque la politique l’impose ;
- limite toutes les écritures à la racine fournie ;
- dérive le nom du répertoire final depuis le SHA-256 de l’identifiant ;
- refuse les entrées supplémentaires dans un état final ;
- refuse les fichiers non réguliers ;
- applique la limite `max_file_bytes`.

## Absence de mutation

Avant et après la persistance, la frontière compare les représentations
canoniques :

- du résultat d’application ;
- de l’état restauré source ;
- du plan source ;
- du journal source ;
- du checkpoint source.

L’état source n’est jamais remplacé, supprimé ou modifié.

## Tests spécifiques

La suite vérifie notamment :

- le déterminisme et la sérialisation de la politique ;
- l’impossibilité de désactiver les garanties obligatoires ;
- la détection d’un hash de politique altéré ;
- le déterminisme de la requête ;
- la dérivation de nouveaux identifiants ;
- le refus de réutiliser l’identifiant source ;
- le refus d’un horodatage antérieur ;
- le refus d’une racine relative ;
- la détection des requêtes et résultats altérés ;
- la création exacte des quatre fichiers ;
- la capture d’un nouveau checkpoint ;
- les liaisons du manifest ;
- l’absence de mutation de la source ;
- le rejeu `noop` ;
- le refus d’un état final divergent ;
- le refus d’un verrou existant ;
- l’application de la limite de taille ;
- la reprise d’un staging complet ;
- la restauration par la frontière standard ;
- l’immuabilité des contrats.

## Garanties finales

- application de reprise vérifiée obligatoire ;
- destination distincte de la source ;
- checkpoint neuf et vérifié ;
- persistance atomique ;
- manifest standard et restaurable ;
- verrou exclusif ;
- comportement fail-closed ;
- idempotence contrôlée ;
- état source immuable ;
- aucune exécution d’étape ;
- aucun dispatch d’agent ;
- aucun appel à un fournisseur IA ;
- aucune connexion réseau ;
- aucune modification de version, tag ou release.

## Hors périmètre

Cet incrément ne fournit pas encore :

- le démarrage automatique d’une étape après persistance ;
- le dispatch effectif d’un agent ;
- un ordonnanceur permanent ;
- une politique de rétention ;
- la suppression automatique des anciens états ;
- le chiffrement au repos ;
- la signature asymétrique du manifest ;
- la réplication multi-machine ;
- l’intégration dans ELMAN Studio.
