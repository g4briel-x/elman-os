# Validation de la release candidate v0.4.0-rc.1

## Décision

Cette release candidate gèle le périmètre fonctionnel du Kernel IA v0.4 pour
une revue finale. Elle ne constitue pas encore la version de production
`v0.4.0`.

## Contrôles bloquants

- 180 tests unitaires hors réseau ;
- compilation de tous les modules Python ;
- cohérence de version entre `pyproject.toml`, le runtime et le manifeste ;
- inventaire SHA-256 de chaque fichier livré ;
- chemins compatibles Windows, macOS et Linux ;
- absence de fichiers de secrets, clés privées, credentials et marqueurs
  sensibles à haute confiance ;
- audit de la politique technologique Python-first ;
- installation editable et installation depuis une extraction neuve ;
- matrice CI Python 3.11, 3.12 et 3.13 sur Windows, macOS et Linux.

La commande suivante exécute les contrôles déterministes disponibles sur
l’hôte courant, sans contacter de fournisseur IA :

```powershell
.\.venv\Scripts\python.exe -m elman_os release-check .
```

Une sortie JSON est disponible :

```powershell
.\.venv\Scripts\python.exe -m elman_os release-check . --json
```

## Frontières de sécurité

- aucune clé réelle n’est livrée ou requise ;
- aucun prompt ni réponse n’est inscrit dans l’audit ;
- le fallback distant reste désactivé par défaut ;
- les journaux JSONL sont locaux et ne sont pas un backend multi-instance ;
- JWT/OIDC doivent être vérifiés par une frontière d’authentification externe ;
- les adaptateurs distants sont présents, mais leur connectivité réelle n’est
  pas validée dans cette RC.

## Critères avant `v0.4.0`

1. La matrice CI doit réussir sur les neuf combinaisons OS/Python.
2. La branche doit être revue par Pull Request vers `main`.
3. Aucun finding critique ou secret ne doit rester ouvert.
4. Le journal de changements et le guide de migration doivent être approuvés.
5. Le tag final ne doit être créé qu’après fusion et validation humaine.
