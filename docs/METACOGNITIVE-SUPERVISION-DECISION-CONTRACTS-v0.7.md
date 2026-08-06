# Metacognitive Supervision Decision Contracts — ELMAN-OS v0.7

## Statut

Premier incrément du Jalon 3 — Supervision métacognitive.

Cet incrément définit uniquement les contrats déterministes permettant au
superviseur métacognitif de produire une décision vérifiable sur l’état d’une
orchestration. Il n’applique aucune décision au plan, au journal ou au stockage.

## Fichiers

| Fichier | Rôle |
|---|---|
| `src/elman_os/metacognitive_supervision_decision_contracts.py` | Définit les politiques, contextes, constats et décisions métacognitives immuables et hashées. |
| `tests/test_metacognitive_supervision_decision_contracts.py` | Vérifie le déterminisme, les règles fail-closed, les cinq décisions et l’absence de mutation. |
| `docs/METACOGNITIVE-SUPERVISION-DECISION-CONTRACTS-v0.7.md` | Documente les contrats et leurs garanties. |

## Décisions supportées

- `continue` : poursuite déclarative lorsque le risque maximal est faible et la confiance suffisante ;
- `correct` : correction déclarative associée à des étapes précises ;
- `pause` : suspension recommandée à partir d’un risque au moins moyen ;
- `stop` : arrêt recommandé pour un risque élevé ou critique ;
- `escalate` : transfert vers une autorité humaine pour un risque au moins moyen.

Aucune décision n’est exécutée par ce module.

## Contrats

### `MetacognitiveSupervisionDecisionPolicy`

La politique fixe :

- le seuil minimal de confiance pour `continue` ;
- le seuil minimal de confiance pour `correct` ;
- les exigences d’approbation pour `pause`, `stop` et `escalate` ;
- l’obligation `fail_closed`.

Les niveaux de confiance utilisent des points de base entiers de `0` à `10000`
afin d’éviter les ambiguïtés des nombres flottants.

### `MetacognitiveSupervisionContext`

Le contexte lie cryptographiquement :

- le plan et le projet ;
- le hash d’état du plan ;
- le hash du journal ;
- le hash du checkpoint ;
- les références de preuve ;
- l’agent observateur ;
- l’horodatage UTC ;
- l’objectif de supervision.

L’identifiant du contexte est dérivé de son contenu canonique.

### `MetacognitiveSupervisionFinding`

Un constat contient :

- un type : boucle, contradiction, incertitude, violation de politique,
  manque de preuve, blocage, risque de ressource ou autre ;
- un niveau de risque : information, faible, moyen, élevé ou critique ;
- un résumé ;
- des références de preuve déjà présentes dans le contexte ;
- les étapes affectées ;
- un niveau de confiance ;
- un identifiant et un hash déterministes.

### `MetacognitiveSupervisionDecision`

La décision incorpore :

- la politique complète et son hash ;
- le contexte complet et son hash ;
- les constats complets et leurs hashes ;
- l’action choisie ;
- la confiance ;
- l’exigence d’approbation ;
- une référence d’approbation facultative ;
- les étapes correctives éventuelles ;
- l’agent décideur ;
- l’horodatage ;
- la justification ;
- un identifiant et un hash déterministes.

## Règles fail-closed

- un risque critique impose `stop` ou `escalate` ;
- `continue` refuse tout risque moyen, élevé ou critique ;
- `continue` exige le seuil de confiance configuré ;
- `correct` exige au moins un constat et une étape corrective ;
- `correct` refuse les risques critiques ;
- `pause` exige au moins un risque moyen ;
- `stop` exige un risque élevé ou critique ;
- `escalate` exige au moins un risque moyen ;
- les exigences d’approbation doivent correspondre exactement à la politique ;
- les constats doivent être liés au contexte fourni ;
- toute altération de hash est refusée.

## Garanties

- sérialisation JSON canonique ;
- contrats immuables ;
- identifiants déterministes ;
- hashes SHA-256 vérifiables ;
- horodatages UTC stricts ;
- décisions déclaratives uniquement ;
- aucune mutation du plan ;
- aucune écriture dans le journal ;
- aucune persistance ;
- aucun dispatch d’agent ;
- aucun appel IA ;
- aucune connexion réseau.

## Hors périmètre

Cet incrément ne détecte pas encore automatiquement :

- les boucles ;
- les contradictions ;
- les stagnations ;
- les insuffisances de preuve ;
- les violations de politique.

Il ne traduit pas non plus une décision en commande d’orchestration. Ces
fonctions seront ajoutées dans les incréments suivants du Jalon 3.
