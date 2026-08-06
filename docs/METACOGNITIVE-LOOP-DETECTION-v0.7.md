# Metacognitive Loop Detection — ELMAN-OS v0.7

## Statut

Deuxième incrément du Jalon 3 — Supervision métacognitive.

Cet incrément analyse en lecture seule un journal d’exécution vérifié afin de
détecter des cycles d’événements contigus et répétés. Chaque boucle détectée est
transformée en constat métacognitif immuable, canonique et lié
cryptographiquement au contexte de supervision.

Aucune décision n’est appliquée par ce module.

## Fichiers

| Fichier | Rôle |
|---|---|
| `src/elman_os/metacognitive_loop_detection.py` | Détecte les cycles répétés dans un journal vérifié et produit des constats métacognitifs liés aux preuves. |
| `tests/test_metacognitive_loop_detection.py` | Vérifie les politiques, les liaisons d’intégrité, les algorithmes de détection, les risques et l’absence de mutation. |
| `docs/METACOGNITIVE-LOOP-DETECTION-v0.7.md` | Documente les contrats, l’algorithme et les garanties fail-closed. |

## Objectif

La frontière de détection reçoit :

1. une politique de détection ;
2. un contexte métacognitif vérifié ;
3. un journal d’exécution hashé et scellé ;
4. une référence de preuve du journal ;
5. un demandeur et un horodatage UTC.

Elle retourne soit :

- `clear` lorsqu’aucun cycle conforme à la politique n’est détecté ;
- `loops-detected` avec un ou plusieurs enregistrements vérifiables.

## Définition d’une boucle

Une boucle est une séquence contiguë de signatures d’événements répétée au
moins `minimum_repetitions` fois.

Une signature contient uniquement :

```text
event_type | step_id | agent_id
```

Les payloads ne participent pas à la signature. Une variation d’agent ou
d’étape produit donc une signature différente.

Par défaut, les événements de plan agissent comme des barrières entre les
segments d’étapes. Une politique explicite peut inclure les événements de plan.

## Sélection déterministe

À chaque position du journal, l’algorithme examine les longueurs de cycle
autorisées puis sélectionne :

1. la couverture totale la plus grande ;
2. le nombre de répétitions le plus élevé ;
3. la longueur fondamentale la plus courte ;
4. la signature lexicographiquement déterministe.

Les enregistrements sélectionnés ne se chevauchent pas.

## Contrats

### `MetacognitiveLoopDetectionPolicy`

La politique fixe notamment :

- le nombre minimal de répétitions ;
- la longueur maximale d’un cycle ;
- les seuils de risque élevé et critique ;
- la confiance de base ;
- l’incrément de confiance par répétition ;
- l’inclusion éventuelle des événements de plan ;
- l’obligation `fail_closed`.

### `MetacognitiveLoopDetectionRequest`

La requête incorpore et lie :

- la politique complète et son hash ;
- le contexte complet et son hash ;
- l’identifiant du plan ;
- le nombre d’événements ;
- le hash de tête du journal ;
- le hash global du journal ;
- la référence de preuve ;
- le demandeur ;
- l’horodatage UTC ;
- la justification.

Le hash du journal doit être identique à celui du contexte métacognitif.

### `MetacognitiveLoopPattern`

Un motif détecté contient :

- les séquences de début et de fin ;
- la longueur fondamentale ;
- le nombre de répétitions ;
- la signature du cycle ;
- les étapes affectées ;
- le niveau de risque ;
- la confiance ;
- les références de preuve ;
- un identifiant et un hash déterministes.

### `MetacognitiveLoopDetectionRecord`

Chaque enregistrement associe exactement :

- un motif de boucle ;
- un `MetacognitiveSupervisionFinding` de type `loop`.

Le risque, la confiance, les étapes, les preuves et le résumé du constat sont
vérifiés contre le motif.

### `MetacognitiveLoopDetectionResult`

Le résultat contient :

- la requête complète ;
- le statut ;
- les enregistrements et leurs hashes ;
- le nombre d’événements inspectés ;
- l’horodatage de fin ;
- une raison explicite ;
- un identifiant et un hash déterministes.

## Niveaux de risque

La politique détermine le risque à partir du nombre de répétitions :

- répétitions minimales : `medium` ;
- seuil élevé atteint : `high` ;
- seuil critique atteint : `critical`.

La confiance augmente de manière entière et déterministe, puis est plafonnée à
`10000` points de base.

## Garanties fail-closed

- le contexte doit être valide et hashé ;
- le journal doit être valide, scellé et identique à la requête ;
- la preuve du journal doit déjà appartenir au contexte ;
- toute altération d’un hash est refusée ;
- un résultat `clear` ne peut contenir aucun enregistrement ;
- un résultat `loops-detected` doit contenir au moins un enregistrement ;
- chaque constat doit correspondre exactement à son motif ;
- les horodatages ne peuvent pas remonter dans le temps ;
- la politique ne peut pas désactiver `fail_closed`.

## Absence d’effets de bord

Avant et après l’analyse, la frontière compare les sérialisations canoniques du
contexte et du journal.

L’incrément garantit :

- aucune mutation du plan ;
- aucune écriture dans le journal ;
- aucune persistance ;
- aucune application de décision ;
- aucun dispatch d’agent ;
- aucun appel à un fournisseur IA ;
- aucune connexion réseau ;
- aucune modification de version, tag ou release.

## Tests spécifiques

La suite vérifie notamment :

- le déterminisme et la sérialisation des politiques ;
- les seuils incohérents ;
- l’impossibilité de désactiver `fail_closed` ;
- les liaisons entre contexte, journal et preuve ;
- la détection d’un cycle d’un événement ;
- la détection d’un cycle de plusieurs événements ;
- la sélection de la période fondamentale ;
- plusieurs boucles non chevauchantes ;
- les barrières créées par les événements de plan ;
- l’inclusion facultative des événements de plan ;
- les niveaux de risque moyen, élevé et critique ;
- le plafonnement de la confiance ;
- l’effet des identités d’agents sur la signature ;
- la longueur maximale autorisée ;
- les rejets d’altération ;
- l’absence de mutation ;
- les aller-retour JSON de tous les contrats.

## Hors périmètre

Cet incrément ne fournit pas encore :

- la détection des contradictions ;
- la détection de stagnation temporelle ;
- l’agrégation de plusieurs catégories de constats ;
- la sélection automatique d’une décision ;
- l’application d’une commande `pause`, `stop` ou `escalate` ;
- l’intégration dans ELMAN Studio.
