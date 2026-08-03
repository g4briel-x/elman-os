# Migration de ELMAN-OS v0.6.0-rc.1 vers v0.6.0-rc.2

## Objet

`v0.6.0-rc.2` consolide la première release candidate sans modifier le schéma
SQLite ni les contrats publics du kernel.

## Changements

- exclusion des environnements virtuels, caches, IDE et dépendances locales ;
- inventaire SHA-256 limité aux fichiers distribuables ;
- contrat de sortie transactionnelle explicitement non suppressif ;
- maintien des exceptions de quota, d’intégrité et d’audit ;
- version Python de distribution : `0.6.0rc2`.

## Compatibilité

- aucune migration de base de données ;
- aucune suppression d’API publique ;
- aucun credential réel requis ;
- aucun appel payant ou fournisseur distant activé.

## Validation

```powershell
$Python = (Resolve-Path ".\.venv\Scripts\python.exe").Path
& $Python -m pip install --no-build-isolation -e .
& $Python -W error::ResourceWarning -m unittest discover -s tests -v
& $Python -m elman_os release-check .
& $Python scripts\verify_release_installation.py .
```

## Retour arrière

Réinstaller la roue `0.6.0rc1` ou replacer le dépôt sur le tag immuable
`v0.6.0-rc.1`. Aucune transformation de données n’est nécessaire.
