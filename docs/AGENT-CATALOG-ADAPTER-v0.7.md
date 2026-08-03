# Adaptateur du catalogue d’agents ELMAN-OS v0.7

## Statut

Deuxième incrément technique du cycle v0.7.0.

L’adaptateur relie le catalogue historique des 21 `AgentProfile` aux contrats
stricts introduits par `agent_contracts.py`. Il ne remplace ni le catalogue,
ni les profils historiques, ni le futur orchestrateur.

## Correction v2

Le catalogue historique contient des noms d’artefacts, notamment `README.md`.
Le contrat strict des `output_kinds` exige des jetons commençant par une lettre
minuscule.

La version v2 applique donc une adaptation de casse déterministe :

- `README.md` devient `readme.md` dans `AgentCapability.output_kinds` ;
- la valeur historique `README.md` reste intacte dans les métadonnées ;
- aucun espace ni caractère incompatible n’est corrigé silencieusement ;
- toute valeur non adaptable reste refusée.

## Fichiers

| Fichier | Rôle |
|---|---|
| `src/elman_os/agent_catalog_adapter.py` | Convertit les profils historiques en définitions strictes et construit un registre local. |
| `tests/test_agent_catalog_adapter.py` | Vérifie les 21 agents, la conversion, le déterminisme et les refus fail-closed. |
| `docs/AGENT-CATALOG-ADAPTER-v0.7.md` | Documente le mapping, les garanties et les limites. |

## Mapping déterministe

### Identité

Les champs suivants sont conservés :

- `agent_id` ;
- `name` ;
- `role` ;
- `forbidden_actions` ;
- `experience_standard` ;
- `mission` ;
- `layer` ;
- `required_outputs`.

### Scopes et capacités

Chaque valeur de `allowed_scopes` devient :

- un `AgentCapability.capability_id` ;
- une permission exigée par cette capacité ;
- une permission déclarée par l’agent.

Les scopes ne sont ni renommés ni corrigés silencieusement.

### Sorties

Chaque capacité adaptée déclare les `required_outputs` du profil historique
comme `output_kinds`.

Pour satisfaire le contrat strict, seule la casse ASCII est normalisée vers
les minuscules. La valeur historique originale reste disponible dans les
métadonnées. Les espaces et caractères non autorisés provoquent un refus.

### Métadonnées

Les éléments historiques sans champ direct dans `AgentDefinition` sont
conservés dans des métadonnées JSON immuables :

- type de l’adaptateur ;
- couche de l’agent ;
- mission ;
- standard d’expérience ;
- sorties historiques originales.

## API

### `profile_to_definition`

Convertit un `AgentProfile` en `AgentDefinition`.

### `catalog_to_definitions`

Convertit un ensemble de profils et trie les définitions par `agent_id`.

### `catalog_to_registry`

Construit un `AgentRegistry` frais. Les identifiants dupliqués sont refusés.

### `built_in_agent_registry`

Construit un registre frais contenant les 21 agents canoniques.

## Garanties

- conservation des 21 identifiants ;
- distribution des couches conservée ;
- conversion déterministe ;
- registre frais à chaque appel ;
- permissions explicites ;
- actions interdites conservées ;
- sorties historiques conservées dans les métadonnées ;
- validation stricte des valeurs non adaptables ;
- fonctionnement entièrement hors réseau ;
- aucune écriture dans le projet utilisateur ;
- aucun fournisseur IA ;
- aucun changement de version ou de release.

## Limites

Cet incrément ne fournit pas encore :

- l’orchestrateur ;
- la sélection par score ou coût ;
- les dépendances entre agents ;
- la persistance du registre ;
- les approbations dynamiques ;
- la négociation de versions ;
- la signature cryptographique du catalogue.
