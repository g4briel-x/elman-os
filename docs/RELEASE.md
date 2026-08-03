# Validation de la release candidate v0.6.0-rc.2

## Décision

ELMAN-OS Foundation Kit `v0.6.0-rc.2` consolide `v0.6.0-rc.1` avec un
inventaire de release renforcé et un contrat transactionnel explicitement non
suppressif.

La version Python est `0.6.0rc2`. Cette release candidate n’est ni une
approbation finale ni une autorisation de production.

## Périmètre

- ELMAN Studio phases 1 à 3 ;
- workflows déterministes locaux et approbation à usage unique ;
- historique SQLite en lecture seule ;
- `.elman/`, `generated/`, environnements, caches, IDE et dépendances locales
  exclus de l’inventaire ;
- transactions asynchrones qui ne suppriment jamais les exceptions.

## Preuves attendues

- 278 tests unitaires hors réseau ;
- `release-check` réussi avec 106 fichiers vérifiés ;
- audit technologique réussi ;
- roue `0.6.0rc2` installée sans index ;
- archive ZIP déterministe et reproductible ;
- matrice CI Windows/macOS/Linux sur Python 3.11 à 3.13.

## Gates

- `release_candidate_validated = true` ;
- `final_release_approved = false` ;
- `not_production_ready = true`.

## Tags

Le tag `v0.6.0-rc.1` reste immuable. Le tag `v0.6.0-rc.2` sera créé uniquement
sur `main`, après fusion de la PR RC2 et réussite de la CI. Il ne doit pas être
créé sur `release/v0.6.0-rc.2`.
