# Contrat des plugins ELMAN-OS v0.3.1

Les plugins étendent un agent sans lui donner un accès général à la machine.

## Principes

- refus par défaut ;
- permissions explicites ;
- workspace résolu ;
- action inconnue refusée ;
- secrets bruts interdits ;
- approbation humaine pour réseau sensible, secret ou déploiement.

## Permissions

| Permission | Usage |
|---|---|
| `read_workspace` | lecture bornée |
| `write_workspace` | écriture bornée |
| `run_tests` | tests allowlistés |
| `network` | réseau allowlisté |
| `use_secrets` | référence opaque vers secret |
| `deploy` | publication après approbation |

## Plugins intégrés

### `elman.project_inspector`

- `list`
- `read_text`
- protection traversal ;
- taille de lecture limitée.

### `elman.acceptance_checklist`

- `evaluate`
- aucune permission ;
- retourne les critères validés et manquants.

### `elman.technology_auditor`

- `audit`
- permission `read_workspace` ;
- détecte un langage hors couche approuvée.

### `elman.blueprint_validator`

- `validate`
- aucune permission ;
- valide nom, slug, type et plateformes.

## Ajout d’un plugin

1. Implémenter `ElmanPlugin`.
2. Déclarer un `PluginManifest`.
3. Valider toutes les entrées.
4. Refuser l’action inconnue.
5. Ajouter tests positifs, négatifs et de permission.
6. Enregistrer dans `built_in_registry()` ou un registre isolé.
7. Passer Shield pour accès fichier, réseau, secret ou commande.
8. Passer Proof avant diffusion.
