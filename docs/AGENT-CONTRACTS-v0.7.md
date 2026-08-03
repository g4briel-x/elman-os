# Contrats multi-agents ELMAN-OS v0.7

## Statut

Premier incrément technique du cycle v0.7.0.

Ce module est additif. Il ne remplace ni `domain.py`, qui contient les contrats
historiques du workflow, ni `catalog.py`, qui contient le roster canonique des
21 agents. Il introduit une frontière stricte pour les prochains incréments
d’orchestration.

## Fichiers

| Fichier | Rôle |
|---|---|
| `src/elman_os/agent_contracts.py` | Contrats immuables, validation stricte, JSON canonique et registre local. |
| `tests/test_agent_contracts.py` | Tests hors réseau de validation, immutabilité, résolution et sérialisation. |
| `docs/AGENT-CONTRACTS-v0.7.md` | Architecture, garanties, limites et exemple d’usage. |

## Contrats

### `AgentCapability`

Déclare un identifiant de capacité, ses entrées, ses sorties, ses permissions
et son éventuelle obligation d’approbation humaine.

### `AgentDefinition`

Déclare un agent immuable avec une version sémantique, des capacités, des
permissions, des actions interdites et une politique `fail_closed`.

Une permission exigée par une capacité doit être déclarée par l’agent. Une
action ne peut pas être simultanément autorisée et interdite.

### `AgentRequest`

Décrit une demande adressée à un agent. Les entrées et contraintes JSON sont
copiées et figées lors de la création.

### `AgentResponse`

Décrit une réponse vérifiable. Une réussite ne peut pas contenir d’erreur, un
échec doit contenir une erreur et un blocage doit expliquer sa cause.

### `AgentRegistry`

Registre local déterministe avec refus des doublons, recherche par capacité,
contrôle des permissions et contrôle de l’approbation humaine.

## JSON canonique

La sérialisation trie les clés, conserve UTF-8, refuse les nombres non finis,
les clés non textuelles et les objets Python non JSON.

## Frontières de sécurité

Cet incrément :

- n’appelle aucun fournisseur distant ;
- n’exécute aucun code généré ;
- n’écrit pas dans le projet utilisateur ;
- n’active aucune permission ;
- ne contourne aucune approbation ;
- ne modifie pas les métadonnées ou tags de la release v0.6.0.

## Exemple

```python
from elman_os.agent_contracts import (
    AgentCapability,
    AgentDefinition,
    AgentRegistry,
)

definition = AgentDefinition(
    agent_id="ELMAN_DISCOVERY",
    name="ELMAN Discovery",
    role="Product requirements",
    version="1.0.0",
    capabilities=(
        AgentCapability(
            capability_id="requirements.analyze",
            description="Analyze validated requirements",
            permissions=("project.read",),
        ),
    ),
    permissions=("project.read",),
    forbidden_actions=("production.deploy",),
)

registry = AgentRegistry((definition,))
selected = registry.resolve("requirements.analyze")
assert selected.agent_id == "ELMAN_DISCOVERY"
```

## Limites

- pas encore d’adaptateur automatique vers `AgentProfile` ;
- pas encore de persistance SQLite ;
- pas encore d’orchestrateur ;
- pas encore de protocole réseau ;
- pas encore de signature cryptographique ;
- pas encore de négociation de version entre agents.
