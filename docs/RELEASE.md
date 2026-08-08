# Release candidate ELMAN-OS v0.7.0-rc.1

## Décision

ELMAN-OS Foundation Kit `v0.7.0-rc.1` prépare la stabilisation des 78 commits
de développement intégrés après `v0.6.0`. La distribution Python est
`0.7.0rc1`.

Cette décision valide le contenu candidat hors réseau. Elle n’accorde pas
l’approbation finale, n’autorise aucun déploiement automatique et ne certifie
aucun fournisseur IA distant.

## Périmètre livré

- contrats multi-agents, catalogue et planification déterministe ;
- journal d’exécution, approbations, checkpoints et reprise contrôlée ;
- cycle transactionnel des artefacts et restauration d’orchestration ;
- supervision métacognitive indépendante ;
- mémoire structurée avec provenance et rétention ;
- vérificateur final fail-closed et rapport HMAC ;
- ELMAN Studio v0.7 en lecture seule via `studio-oversight` ;
- inventaire SHA-256 exhaustif et archive reproductible.
- prise en charge vérifiée de l’alias système macOS `/var → /private/var`,
  sans assouplissement des autres contrôles de symlink.

## Preuves locales requises

- 1 938 tests réussis avec `ResourceWarning` traité comme erreur ;
- compilation de tous les fichiers Python ;
- `release-check 0.7.0-rc.1` réussi ;
- audit technologique réussi ;
- inventaire SHA-256 sans fichier absent, modifié ou non suivi ;
- roue `0.7.0rc1` installée hors réseau dans un environnement neuf ;
- deux archives ZIP produites indépendamment et identiques ;
- aucun credential réel, appel réseau ou appel payant.

## Preuves distantes obligatoires avant le tag

- Pull Request `release/v0.7.0-rc.1` vers `main` sans conflit ;
- matrice GitHub Actions réussie sur Windows, macOS et Linux ;
- Python 3.11, 3.12 et 3.13 réussis sur chaque système ;
- commit de fusion vérifié dans `main`.

## Gates

- `release_candidate_validated = true` pour la validation locale ;
- `final_release_approved = false` ;
- `not_production_ready = true` ;
- `multi_platform_ci_pending = true` avant la PR ;
- aucun déploiement automatique ;
- aucune connectivité réelle de fournisseur IA certifiée.

## Politique de gel

Après création de la branche de release, seuls les correctifs bloquants,
preuves de validation et ajustements documentaires sont autorisés. Toute
modification de fichier exige la régénération de `RELEASE-CHECKSUMS.sha256` et
la reprise de la validation complète.

## Tag

Le tag annoté `v0.7.0-rc.1` doit être créé uniquement sur le commit de `main`
obtenu après fusion et réussite de la matrice CI. Il ne doit jamais être créé
directement sur `develop/v0.7.0` ou sur la branche de release.
