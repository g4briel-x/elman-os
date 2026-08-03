# Migration de ELMAN-OS v0.6.0-rc.2 vers v0.6.0

## Objet

`v0.6.0` est la promotion stable de la release candidate validée
`v0.6.0-rc.2`. La promotion aligne les métadonnées, la documentation, le
manifeste, les tests de release et les artefacts distribuables.

## Compatibilité

- aucun changement fonctionnel du kernel ;
- aucun changement du schéma SQLite ;
- aucune suppression d’API publique ;
- aucune migration destructive ;
- conservation des données `.elman/` existantes ;
- conservation des workspaces `generated/` existants ;
- aucun credential réel requis ;
- aucun fournisseur distant ou appel payant activé.

## Installation

```powershell
py -3.13 -m venv .venv
$Python = (Resolve-Path ".\.venv\Scripts\python.exe").Path

& $Python -m pip install --upgrade pip setuptools wheel
& $Python -m pip install --no-build-isolation -e .

& $Python -W error::ResourceWarning `
  -m unittest discover -s tests -v

& $Python -m elman_os release-check .
& $Python scripts\verify_release_installation.py .
```

## Retour arrière

Le retour arrière consiste à réinstaller la roue `0.6.0rc2` ou à replacer le
dépôt sur le tag immuable `v0.6.0-rc.2`.

Aucune transformation de données n’est requise.
