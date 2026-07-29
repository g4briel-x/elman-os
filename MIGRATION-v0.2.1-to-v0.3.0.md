# Migration du Foundation Kit v0.2.1 vers v0.3.0

La migration recommandée est **côte à côte**. Elle évite d’écraser le kit
v0.2.1 et permet un retour immédiat à l’ancienne version.

## 1. Extraire la nouvelle version

```powershell
Expand-Archive `
  "$env:USERPROFILE\Downloads\ELMAN-OS-Foundation-Kit-v0.3.0.zip" `
  -DestinationPath "$env:USERPROFILE\Desktop"

Set-Location "$env:USERPROFILE\Desktop\elman-os-foundation-kit-v0.3.0"
```

## 2. Installer dans un nouvel environnement Python

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip setuptools
.\.venv\Scripts\python.exe -m pip install -e .
```

## 3. Vérifier avant de reprendre une configuration

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m elman_os doctor
.\.venv\Scripts\python.exe -m elman_os audit-stack .
```

## 4. Reprendre uniquement les paramètres voulus

Comparer l’ancien `config\metacognitive-policy.json` avec le nouveau. Reporter
manuellement les valeurs propres au projet ; ne pas remplacer le nouveau
fichier en bloc, car de nouvelles limites peuvent avoir été ajoutées.

Les imports Python `elman_os` et la commande `elman-os` restent compatibles.
La v0.3.0 ajoute des modules ; elle ne demande pas de migration de base v0.2.1,
car cette version ne possédait pas encore de stockage SQLite du kernel.

## 5. Retour arrière

Le retour arrière consiste à quitter le nouveau dossier et à réutiliser
l’environnement de la v0.2.1. Ne pas partager le même `.venv` entre les deux
versions.

## Évolutions à prendre en compte

- les actions protégées exigent une approbation humaine indépendante ;
- un projet existant n’est plus écrasé silencieusement ;
- les écritures hors workspace sont bloquées ;
- JavaScript, TypeScript et les autres langages spécialisés sont autorisés
  seulement dans les couches approuvées ;
- SQLite crée ses fichiers à l’emplacement fourni à la commande.
