# Migration ELMAN-OS v0.7.0-rc.1 vers v0.7.0-rc.2

## Objet

`v0.7.0-rc.2` corrige les métadonnées et la gouvernance de publication de la
première release candidate. La version de distribution Python devient
`0.7.0rc2` conformément à PEP 440.

## Changements

- enregistrement de la matrice CI réussie sur Windows, macOS et Linux ;
- enregistrement de Python 3.11, 3.12 et 3.13 comme validés ;
- enregistrement de la validation d’installation propre des artefacts RC1 ;
- correction de la procédure : une candidate est fusionnée et taguée dans
  `develop/v0.7.0`, tandis que `main` reste réservé à la promotion stable ;
- régénération de l’inventaire SHA-256 et des artefacts en `v0.7.0-rc.2`.

## Compatibilité

- aucun changement de schéma SQLite ;
- aucune suppression ou modification d’API publique ;
- aucun changement fonctionnel du kernel ou du Studio ;
- aucune migration automatique de `.elman/` ou `generated/` ;
- aucun credential réel, appel payant ou fournisseur distant activé.

## Validation

```powershell
$ProjectPython = (Resolve-Path ".\.venv\Scripts\python.exe").Path
$env:PYTHONPATH = (Resolve-Path ".\src").Path

& $ProjectPython -W error::ResourceWarning `
  -m unittest discover -s tests -v
& $ProjectPython -m elman_os release-check .
& $ProjectPython -m elman_os audit-stack .
& $ProjectPython scripts\verify_release_installation.py .

Remove-Item Env:PYTHONPATH
```

Résultats attendus :

- version importée `0.7.0rc2` ;
- 1 938 tests réussis hors réseau ;
- `release-check 0.7.0-rc.2` en PASS ;
- roue installée hors réseau ;
- deux archives ZIP strictement identiques.

## Retour arrière

Le tag et la GitHub Pre-release `v0.7.0-rc.1` restent immuables. Pour revenir à
la première candidate, réinstaller sa roue publiée ou replacer le dépôt sur le
tag `v0.7.0-rc.1`. Aucune transformation de données n’est nécessaire.
