# Migration ELMAN-OS v0.5.1 vers v0.6.0-rc.1

## Objet

Cette migration introduit ELMAN Studio sans retirer les contrats publics du
kernel `v0.5.1`.

La version de distribution Python est `0.6.0rc1` conformément à PEP 440.
Le nom public de la release candidate est `v0.6.0-rc.1`.

## Capacités ajoutées

- Studio local optionnel fondé sur Flet ;
- planification et génération de starter sous approbation humaine ;
- historique SQLite strictement en lecture seule ;
- workflows déterministes locaux exécutés hors du thread d’interface ;
- progression, verdict et raison d’arrêt visibles ;
- approbations d’exécution à usage unique ;
- protection de `generated/` et `.elman/` contre Git et l’inventaire de release.

## Compatibilité

Les contrats suivants sont conservés :

- catalogue et identifiants des agents ;
- planificateur et service du kernel ;
- schéma `workflow_reports` de SQLite ;
- contrats de fournisseurs IA ;
- authentification, quotas, audit et persistance transactionnelle ;
- commande `elman-os` et commandes existantes.

Studio reste un extra optionnel :

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[studio]"
```

Le kernel sans interface continue de s’installer avec :

```powershell
.\.venv\Scripts\python.exe -m pip install -e .
```

## Données locales

Les dossiers suivants ne sont pas livrés dans l’archive :

- `.elman/` : état runtime et base SQLite locale ;
- `generated/` : projets produits par Studio ;
- `.venv/`, `build/`, `dist/` et caches Python.

Aucune migration destructive de la base SQLite n’est réalisée.

## Vérification après migration

```powershell
.\.venv\Scripts\python.exe -W error::ResourceWarning `
  -m unittest discover -s tests -v

.\.venv\Scripts\python.exe -m elman_os release-check .

.\.venv\Scripts\python.exe scripts\verify_release_installation.py .
```

Pour tester Studio :

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[studio]"

.\.venv\Scripts\python.exe -m elman_os studio `
  --generated-root generated `
  --database .elman\elman.db
```

## Frontières maintenues

- aucun fournisseur IA réel n’est invoqué par Studio ;
- aucun déploiement automatique n’est autorisé ;
- la release candidate n’est pas une approbation de production ;
- les appels réseau et credentials réels restent hors du périmètre validé ;
- les décisions sensibles restent soumises à une gate humaine.

## Retour arrière

1. fermer ELMAN Studio ;
2. sauvegarder `.elman/elman.db` et les projets de `generated/` si nécessaire ;
3. revenir au tag stable `v0.5.1` ;
4. recréer l’environnement virtuel ;
5. réinstaller la version stable hors cache.

```powershell
git switch main
git checkout v0.5.1

Remove-Item .venv -Recurse -Force -ErrorAction SilentlyContinue
py -3.13 -m venv .venv

.\.venv\Scripts\python.exe -m pip install --upgrade pip setuptools wheel
.\.venv\Scripts\python.exe -m pip install -e .
```

Les dossiers `.elman/` et `generated/` ne sont pas supprimés par ce retour
arrière.
