# Migration ELMAN-OS v0.6.0 vers v0.7.0-rc.1

## Portée

Cette migration fait passer le Foundation Kit stable `v0.6.0` à la première
release candidate `v0.7.0-rc.1`. La version de distribution Python est
`0.7.0rc1`, conformément à PEP 440.

La migration est additive pour les données : elle ne supprime ni `.elman/` ni
`generated/`. Ces deux dossiers restent exclus de l’archive et de l’inventaire
de release.

## Changements majeurs

- contrats immuables des agents et plans d’exécution déterministes ;
- journal d’exécution hashé, approbations et reprise contrôlée ;
- transactions d’artefacts et restauration d’état d’orchestration ;
- supervision métacognitive et détections fail-closed ;
- mémoire de projet SQLite append-only avec provenance et rétention ;
- vérificateur final à neuf portes et rapport signé HMAC-SHA-256 ;
- projection ELMAN Studio v0.7 en lecture seule ;
- inventaire SHA-256 exhaustif qui refuse désormais les fichiers non suivis ;
- archive `v0.7.0-rc.1` construite deux fois et comparée bit à bit.

## Préparation sous Windows

Sauvegarder les données locales avant tout essai :

```powershell
Set-Location "$env:USERPROFILE\Desktop\elman-os"

Copy-Item .elman "$env:USERPROFILE\Desktop\elman-state-v060-backup" `
  -Recurse -Force -ErrorAction SilentlyContinue
Copy-Item generated "$env:USERPROFILE\Desktop\elman-generated-v060-backup" `
  -Recurse -Force -ErrorAction SilentlyContinue
```

Préparer une branche de release à partir du développement fusionné :

```powershell
git fetch origin
git switch develop/v0.7.0
git pull --ff-only origin develop/v0.7.0
git status -sb
git switch -c release/v0.7.0-rc.1
```

## Installation et validation

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --no-deps -e .

.\.venv\Scripts\python.exe -W error::ResourceWarning `
  -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m elman_os release-check .
.\.venv\Scripts\python.exe -m elman_os audit-stack .
.\.venv\Scripts\python.exe scripts\verify_release_installation.py .
```

Résultats attendus :

- version importée `0.7.0rc1` ;
- 1 923 tests réussis hors réseau ;
- release-check `0.7.0-rc.1` en PASS ;
- inventaire SHA-256 exhaustif ;
- roue installée hors réseau ;
- deux archives ZIP strictement identiques.

## ELMAN Studio v0.7

Le Studio historique reste disponible avec `elman-os studio`. La projection de
supervision v0.7 est exposée par l’entrée officielle suivante :

```powershell
.\.venv\Scripts\python.exe -m elman_os studio-oversight `
  --request .elman\final-verification-request.json `
  --report .elman\final-verification-report.json `
  --key-file .elman\final-report.key `
  --key-id key:release-001
```

La clé n’est jamais passée en texte brut sur la ligne de commande. Sans
rapport signé et vérifié, Studio refuse d’afficher la clôture comme autorisée.

## Compatibilité et limites

- Python 3.11, 3.12 et 3.13 sont ciblés ;
- Windows, macOS et Linux sont couverts par la matrice GitHub Actions ;
- la validation locale de ce bundle ne remplace pas la matrice distante ;
- les fournisseurs IA réels et le déploiement automatique restent non validés ;
- aucune donnée locale n’est migrée automatiquement.

## Retour arrière

Avant la fusion ou le tag, revenir à la branche stable :

```powershell
git switch main
git pull --ff-only origin main
Remove-Item .venv -Recurse -Force -ErrorAction SilentlyContinue
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
```

Si des essais ont modifié les données locales, restaurer explicitement les
sauvegardes après avoir fermé ELMAN Studio et tout processus ELMAN-OS. Ne pas
écraser `.elman/` ou `generated/` sans avoir vérifié la sauvegarde.

Le tag `v0.7.0-rc.1` ne doit être créé qu’après fusion dans `main` et réussite
de toute la matrice GitHub Actions.
