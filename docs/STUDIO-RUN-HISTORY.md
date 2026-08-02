# ELMAN Studio MVP — phase 2 : historique des workflows

## Objectif

La phase 2 ajoute une consultation locale et strictement en lecture seule de
la base SQLite `.elman/elman.db`.

Studio peut désormais :

- lister les workflows persistés ;
- afficher leur état, raison d’arrêt, date et nombre d’itérations ;
- afficher le dernier verdict de Proof ;
- consulter les preuves et décisions métacognitives ;
- afficher les propositions d’apprentissage et les clés de mémoire ;
- actualiser manuellement l’historique.

## Frontière d’autorité

`WorkflowHistoryReader` utilise SQLite en mode URI `mode=ro` avec
`PRAGMA query_only = ON`.

Il ne crée pas la base lorsqu’elle est absente, ne crée aucune table et
n’exécute aucune instruction d’écriture. Une base absente ou sans table
`workflow_reports` est présentée comme un historique vide.

## Lancement

```powershell
.\.venv\Scripts\python.exe -m elman_os studio `
  --generated-root generated `
  --database .elman\elman.db
```

## Création d’un workflow de démonstration

```powershell
.\.venv\Scripts\python.exe -m elman_os demo `
  --pass-on 2 `
  --max-iterations 3 `
  --database .elman\elman.db
```

Relancer Studio ou cliquer sur **Actualiser l’historique** permet ensuite de
consulter ce workflow.

## Validation

```powershell
.\.venv\Scripts\python.exe -W error::ResourceWarning `
  -m unittest discover -s tests -p "test_studio_history.py" -v

.\.venv\Scripts\python.exe -W error::ResourceWarning `
  -m unittest discover -s tests -v
```

## Limites

- aucune suppression ou modification d’un workflow ;
- aucune actualisation automatique en arrière-plan ;
- aucune reprise d’exécution depuis l’interface ;
- aucune authentification de session Studio ;
- aucune donnée distante.
