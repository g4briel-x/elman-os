# Validation de la release candidate v0.6.0-rc.1

## Décision

ELMAN-OS Foundation Kit `v0.6.0-rc.1` est une release candidate destinée à
valider ELMAN Studio et son intégration au kernel avant promotion vers
`v0.6.0`.

La version de distribution Python est `0.6.0rc1`. Cette release candidate
n’est ni une approbation finale ni une autorisation de déploiement en
production.

## Périmètre livré

- ELMAN Studio phase 1 : intention, plan, approbation et génération locale ;
- phase 2 : historique SQLite strictement en lecture seule ;
- phase 3 : workflows déterministes locaux avec suivi de progression ;
- exécution hors du thread de l’interface ;
- verdict, raison d’arrêt, preuves et décisions consultables ;
- gate d’exécution réinitialisée après chaque workflow ;
- protection des états locaux `.elman/` et `generated/` ;
- kernel, authentification, persistance et gouvernance de `v0.5.1` conservés.

## Preuves de validation attendues

- 278 tests unitaires réussis hors réseau ;
- compilation Python réussie ;
- `release-check` réussi ;
- audit technologique réussi ;
- roue `0.6.0rc1` construite et installée sans index ;
- version importée vérifiée dans un environnement neuf ;
- deux archives ZIP produites avec le même SHA-256 ;
- matrice CI Windows, macOS et Linux sur Python 3.11 à 3.13 ;
- aucun credential réel, appel payant ou fournisseur distant utilisé.

## Gates de production

Les gates restent fermées :

- `release_candidate_validated = true` ;
- `final_release_approved = false` ;
- `not_production_ready = true` ;
- aucun déploiement automatique ;
- aucune connectivité réelle de fournisseur IA certifiée.

## Commandes de contrôle

```powershell
.\.venv\Scripts\python.exe -W error::ResourceWarning `
  -m unittest discover -s tests -v

.\.venv\Scripts\python.exe -m elman_os release-check .

.\.venv\Scripts\python.exe scripts\verify_release_installation.py .
```

## Tag

Le tag annoté `v0.6.0-rc.1` doit être créé uniquement sur le commit de `main`
obtenu après fusion de la Pull Request et réussite de la matrice CI.

Il ne doit pas être créé directement sur la branche
`release/v0.6.0-rc.1`.
