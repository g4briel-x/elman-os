# Release candidate ELMAN-OS v0.7.0-rc.2

## Décision

ELMAN-OS Foundation Kit `v0.7.0-rc.2` consolide `v0.7.0-rc.1` sans changement
fonctionnel. La version de distribution Python est `0.7.0rc2`.

Cette candidate corrige les métadonnées de validation et la procédure de tag.
Elle n’accorde pas l’approbation finale, n’autorise aucun déploiement
automatique et ne certifie aucun fournisseur IA distant.

## Périmètre

- contenu fonctionnel de `v0.7.0-rc.1` conservé ;
- preuves CI Windows, macOS et Linux enregistrées ;
- Python 3.11, 3.12 et 3.13 enregistrés comme validés ;
- validation d’installation propre depuis les artefacts GitHub enregistrée ;
- politique de tag alignée sur la branche `develop/v0.7.0` ;
- gates de production maintenues fermées.

## Preuves acquises sur v0.7.0-rc.1

- 1 938 tests réussis avec `ResourceWarning` traité comme erreur ;
- `release-check 0.7.0-rc.1` réussi avec 223 fichiers vérifiés ;
- audit technologique réussi ;
- matrice GitHub Actions réussie sur Windows, macOS et Linux ;
- Python 3.11, 3.12 et 3.13 réussis sur chaque système ;
- roue `0.7.0rc1` installée hors réseau depuis la GitHub Pre-release ;
- archive publiée vérifiée par SHA-256 ;
- starter full-stack web et Android généré, puis ses deux tests réussis.

## Preuves requises pour v0.7.0-rc.2

- 1 938 tests unitaires hors réseau ;
- compilation de tous les fichiers Python ;
- `release-check 0.7.0-rc.2` réussi ;
- audit technologique réussi ;
- inventaire SHA-256 exhaustif ;
- roue `0.7.0rc2` installée hors réseau dans un environnement neuf ;
- deux archives ZIP produites indépendamment et identiques ;
- matrice CI réussie sur la Pull Request RC2 et sur son commit fusionné.

## Gates

- `release_candidate_validated = true` ;
- `final_release_approved = false` ;
- `not_production_ready = true` ;
- `multi_platform_ci_pending = false` pour les preuves enregistrées de RC1 ;
- aucun déploiement automatique ;
- aucune connectivité réelle de fournisseur IA certifiée.

## Politique de gel

Seuls les correctifs bloquants, preuves de validation et ajustements
documentaires sont autorisés. Toute modification exige la régénération de
`RELEASE-CHECKSUMS.sha256` et la reprise de la validation complète.

## Tags et branches

Le tag publié `v0.7.0-rc.1` reste immuable.

La branche `release/v0.7.0-rc.2` est fusionnée dans `develop/v0.7.0` après
réussite de la matrice CI. Le tag annoté `v0.7.0-rc.2` est créé uniquement sur
le commit de fusion vérifié dans `develop/v0.7.0`, jamais directement sur la
branche de release.

La fusion vers `main` et le tag `v0.7.0` appartiennent à la promotion stable et
restent interdits pendant la phase RC2.
