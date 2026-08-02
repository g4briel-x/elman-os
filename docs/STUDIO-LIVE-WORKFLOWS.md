# ELMAN Studio MVP — phase 3 : workflows locaux en direct

## Objectif

La phase 3 permet de lancer depuis Studio un workflow déterministe local,
borné et persisté dans `.elman/elman.db`.

L’exécution est séparée du thread de l’interface afin que la fenêtre reste
réactive. Chaque cycle publie un événement minimal contenant uniquement
l’itération, la progression, le verdict et le statut.

## Gate humaine

Un workflow ne peut pas démarrer sans approbation explicite. La gate est
également appliquée dans `LocalWorkflowRunner`, indépendamment de l’interface.

Les paramètres sont validés avant toute création de base :

- identifiant portable de 3 à 64 caractères ;
- nombre maximal d’itérations compris entre 1 et 50 ;
- itération de réussite comprise entre 1 et 50 ; elle peut dépasser la limite pour tester un arrêt borné.

## Frontière d’autorité

Cette phase :

- n’appelle aucun fournisseur IA ;
- ne réalise aucun appel réseau ;
- ne déploie aucun projet ;
- n’exécute aucune commande externe ;
- persiste uniquement le rapport final du workflow local ;
- conserve l’historique en lecture seule dans la section dédiée.

## Lancement

```powershell
.\.venv\Scripts\python.exe -m elman_os studio `
  --generated-root generated `
  --database .elman\elman.db
```

Dans la section **Exécution locale d’un workflow** :

1. définir l’identifiant ;
2. définir l’itération de réussite et la limite ;
3. approuver explicitement ;
4. cliquer sur **Lancer le workflow local** ;
5. suivre la progression ;
6. consulter automatiquement le rapport dans l’historique.

## Validation

```powershell
.\.venv\Scripts\python.exe -W error::ResourceWarning `
  -m unittest discover -s tests -p "test_studio_runtime.py" -v

.\.venv\Scripts\python.exe -W error::ResourceWarning `
  -m unittest discover -s tests -v
```
