# ELMAN Studio MVP — phase 1

## Objectif

La phase 1 fournit une interface locale Python/Flet au-dessus du service du
kernel. Elle permet de saisir une intention produit, prévisualiser le pipeline
des agents, approuver explicitement le plan, puis générer un starter dans le
sandbox local `generated/`.

## Frontière d’autorité

Studio ne déploie rien, n’appelle aucun fournisseur IA distant et ne contourne
aucune gate de production.

La génération est bloquée tant que les trois conditions suivantes ne sont pas
réunies :

1. le formulaire produit est valide ;
2. un plan a été construit par `ElmanKernelService` ;
3. l’utilisateur a approuvé explicitement le plan actuellement affiché.

Toute nouvelle prévisualisation révoque automatiquement l’approbation
précédente.

## Architecture

```text
CLI `elman-os studio`
        |
        v
launch_studio()
        |
        v
StudioSession ----> ElmanKernelService
        |                  |
        |                  +---- plan()
        |                  +---- generate()
        |
        +---- StudioForm -> ProjectIntent
```

Le module `src/elman_os/studio.py` garde l’import Flet à l’intérieur de
`launch_studio()`. Le kernel, les tests et la CLI restent donc utilisables sans
installer la dépendance graphique.

## Installation

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[studio]"
```

## Lancement

```powershell
.\.venv\Scripts\python.exe -m elman_os studio `
  --generated-root generated
```

## Validation hors interface

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_studio.py" -v
.\.venv\Scripts\python.exe -m elman_os studio --help
```

## Limites de la phase 1

- pas encore de consultation des workflows SQLite ;
- pas encore de streaming de progression ;
- pas encore d’intégration HTTP avec le control plane FastAPI ;
- pas encore d’authentification de session Studio ;
- pas de déploiement automatique.
