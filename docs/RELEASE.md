# Validation de la version stable v0.5.1

## Décision

ELMAN-OS Foundation Kit `v0.5.1` est approuvé comme version corrective stable
du backend IA authentifié et persistant.

Cette version reprend les capacités de `v0.5.0` et corrige l’inventaire
SHA-256 de `.gitignore`. Elle ne modifie ni les API publiques, ni les schémas
SQLite, ni les contrats des agents. Elle ne déclare pas encore ELMAN-OS
autonome ni prêt à un déploiement sans approbation.

## Preuves de validation

- 259 tests unitaires réussis hors réseau ;
- validation JWT/OIDC, refus fermés et autorisation testés ;
- transactions, rollback, tenants et concurrence multi-instance testés ;
- quotas partagés et reprise de la chaîne d’audit testés ;
- pipeline authentifié et route d’exécution testés ;
- roue Python construite et installée hors réseau dans un environnement neuf ;
- version importée `0.5.1` vérifiée ;
- archive ZIP déterministe `v0.5.1` inspectée ;
- empreinte de `.gitignore` et inventaire SHA-256 corrigés ;
- chemins et politique technologique contrôlés ;
- aucun secret réel, credential fournisseur, appel réseau ou appel payant.

## Frontières maintenues

- SQLite reste l’implémentation locale ; l’adaptateur PostgreSQL n’est pas livré ;
- la connectivité réelle des fournisseurs distants n’est pas certifiée ;
- la rotation opérationnelle des clés n’est pas automatisée ;
- aucune sandbox de processus ou de conteneur n’est livrée ;
- l’API FastAPI reste un extra optionnel ;
- le déploiement reste soumis à une approbation humaine et aux contrôles CI.

## Commandes de contrôle

```powershell
.\.venv\Scripts\python.exe -W error::ResourceWarning `
  -m unittest discover -s tests -v

.\.venv\Scripts\python.exe -m elman_os release-check .
.\.venv\Scripts\python.exe scripts\verify_release_installation.py .
```

## Tag

Le tag annoté `v0.5.1` doit pointer sur le commit de `main` obtenu après fusion
de la branche validée. Il ne doit pas être créé sur la branche de release.
