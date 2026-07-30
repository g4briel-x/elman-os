# Validation de la version stable v0.4.0

## Décision

ELMAN-OS Foundation Kit v0.4.0 est approuvé comme version stable du Kernel IA.

Cette décision valide le paquet, son runtime, son intégrité et sa compatibilité.
Elle ne transforme pas encore ELMAN-OS en plateforme autonome de production :
les déploiements réels, credentials et appels fournisseurs restent soumis à une
configuration sécurisée et à une approbation humaine.

## Preuves de validation

- 180 tests unitaires réussis hors réseau ;
- matrice CI réussie sur Windows, Ubuntu et macOS ;
- Python 3.11, 3.12 et 3.13 validés ;
- roue Python construite et installée dans un environnement neuf ;
- version importée vérifiée ;
- inventaire SHA-256 vérifié ;
- chemins compatibles Windows, macOS et Linux ;
- aucun secret réel et aucun appel IA payant.

## Frontières maintenues

- aucun credential réel n’est livré ;
- aucun prompt ni réponse n’est inscrit dans l’audit ;
- le fallback distant reste désactivé par défaut ;
- JWT/OIDC restent vérifiés par une frontière externe ;
- la connectivité réelle des adaptateurs distants n’est pas certifiée ;
- le déploiement en production reste soumis à approbation.

## Commande de contrôle

```powershell
.\.venv\Scripts\python.exe -m elman_os release-check .
```
