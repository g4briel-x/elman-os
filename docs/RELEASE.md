# Publication stable ELMAN-OS v0.6.0

## Décision

ELMAN-OS Foundation Kit `v0.6.0` est la promotion stable de
`v0.6.0-rc.2`. La distribution Python est `0.6.0`.

La publication stable approuve le contenu de la distribution. Elle n’autorise
pas un déploiement automatique en production et ne certifie pas la
connectivité réelle d’un fournisseur IA distant.

## Périmètre livré

- ELMAN Studio phases 1 à 3 ;
- génération locale sous approbation humaine ;
- historique SQLite en lecture seule ;
- workflows déterministes locaux et progression visible ;
- approbation d’exécution à usage unique ;
- persistance transactionnelle isolée par tenant ;
- authentification JWT/OIDC et autorisation par rôle ;
- quotas persistants et audit HMAC chaîné ;
- inventaire SHA-256 durci ;
- exclusions des environnements, caches, IDE et dépendances locales ;
- transactions asynchrones ne supprimant jamais les exceptions ;
- archive ZIP reproductible et roue installable hors réseau.

## Preuves requises

- 278 tests unitaires réussis ;
- 10 tests spécifiques à `v0.6.0` réussis ;
- compilation Python réussie ;
- `release-check 0.6.0` réussi avec 107 fichiers vérifiés ;
- audit technologique réussi ;
- roue `0.6.0` installée dans un environnement neuf sans index ;
- archive `v0.6.0` déterministe et reproductible ;
- matrice CI Windows, macOS et Linux sur Python 3.11 à 3.13 ;
- tags RC1 et RC2 conservés immuables ;
- aucun credential réel ni appel payant.

## Gates

- `release_candidate_validated = true` ;
- `final_release_approved = true` ;
- `not_production_ready = true` ;
- aucun déploiement automatique ;
- aucune connectivité réelle de fournisseur IA certifiée.

## Tag

Le tag annoté `v0.6.0` doit être créé uniquement sur le commit de `main`
obtenu après fusion de la Pull Request stable et réussite de la matrice CI.
Il ne doit jamais être créé directement sur `release/v0.6.0`.
