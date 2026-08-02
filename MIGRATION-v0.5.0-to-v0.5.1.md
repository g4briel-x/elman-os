# Migration ELMAN-OS v0.5.0 → v0.5.1

## Portée

La version `v0.5.1` est une version corrective de `v0.5.0`.

Elle ne modifie ni les API publiques, ni les schémas SQLite, ni les contrats
des agents. Elle actualise l’inventaire SHA-256 afin que le contenu réel de
`.gitignore` soit correctement vérifié sur Windows, macOS et Linux.

## Compatibilité

- compatibilité fonctionnelle complète avec `v0.5.0` ;
- aucune migration de données ;
- aucune modification des variables d’environnement ;
- aucune modification des permissions ou des contrats fournisseurs ;
- les gates de production restent fermées.

## Validation avant migration

```powershell
git status -sb
.\.venv\Scripts\python.exe -m elman_os release-check .
.\.venv\Scripts\python.exe -W error::ResourceWarning `
  -m unittest discover -s tests -v
```

## Mise à niveau

Installer le paquet ou l’archive `v0.5.1` à la place de `v0.5.0`. Les bases
SQLite et les journaux d’audit existants doivent être conservés.

## Retour arrière

1. arrêter les instances utilisant `v0.5.1` ;
2. conserver les bases SQLite et les journaux d’audit ;
3. réinstaller le tag `v0.5.0` ;
4. relancer les validations locales avant toute reprise.

Le retour à `v0.5.0` réintroduit l’ancien inventaire SHA-256 et n’est donc pas
recommandé pour la production d’une nouvelle archive.
