# Installation du Foundation Kit ELMAN-OS v0.4.0 alpha 7 sous Windows PowerShell

PowerShell sert de terminal. Le kernel, les agents, l’orchestration, la
métacognition, les plugins, la persistance et les tests sont exécutés par
Python.

## 1. Vérifier Python

```powershell
py -0p
py -3.13 --version
```

Python 3.11 ou ultérieur est requis. Python 3.13 est recommandé lorsqu’il est
déjà installé ; éviter Python 3.14 tant que les dépendances optionnelles du
projet n’ont pas été validées avec cette version.

## 2. Extraire

```powershell
Expand-Archive `
  "$env:USERPROFILE\Downloads\ELMAN-OS-Foundation-Kit-v0.4.0-alpha.7.zip" `
  -DestinationPath "$env:USERPROFILE\Desktop"

Set-Location "$env:USERPROFILE\Desktop\elman-os-foundation-kit-v0.4.0-alpha.7"
```

## 3. Créer l’environnement

```powershell
py -3.13 -m venv .venv
```

Il n’est pas nécessaire d’exécuter `Activate.ps1`.

## 4. Installer le Foundation Kit

```powershell
.\.venv\Scripts\python.exe -m pip install --upgrade pip setuptools wheel
.\.venv\Scripts\python.exe -m pip install -e .
```

## 5. Vérifier

```powershell
.\.venv\Scripts\python.exe -W error::ResourceWarning `
  -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m elman_os doctor
.\.venv\Scripts\python.exe -m elman_os ai-config
.\.venv\Scripts\python.exe -m elman_os ai-providers
.\.venv\Scripts\python.exe -m elman_os ai-audit
.\.venv\Scripts\python.exe -m elman_os ai-readiness
.\.venv\Scripts\python.exe -m elman_os agents
.\.venv\Scripts\python.exe -m elman_os plugins
.\.venv\Scripts\python.exe -m elman_os technology
.\.venv\Scripts\python.exe -m elman_os audit-stack .
```

## 6. Tester les arrêts

Succès à la troisième itération :

```powershell
.\.venv\Scripts\python.exe -m elman_os demo `
  --pass-on 3 `
  --max-iterations 5 `
  --database ".elman\elman.db"
```

Limite sans réussite :

```powershell
.\.venv\Scripts\python.exe -m elman_os demo `
  --pass-on 99 `
  --max-iterations 3
```

## 7. Générer un projet

```powershell
.\.venv\Scripts\python.exe -m elman_os generate `
  --name "ELMAN Tasks" `
  --slug "elman-tasks" `
  --kind fullstack `
  --platform web `
  --platform android `
  --acceptance "Le domaine est couvert par des tests" `
  --output generated
```

## 8. Installer l’API optionnelle

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[api]"
.\.venv\Scripts\python.exe -m elman_os serve
```

L’extra API installe FastAPI et Uvicorn. Il n’est pas requis par le kernel.

## 9. Commandes Git

À exécuter à la racine d’un dépôt Git initialisé :

```powershell
git status -sb
git add .
git commit -m "feat: add authenticated AI execution audit"
git push
```

## Dépannage

### `py` est introuvable

Installer Python pour Windows avec le lanceur `py`.

### `Activate.ps1` est bloqué

Ne pas modifier la politique PowerShell : utiliser directement
`.\.venv\Scripts\python.exe`.

### FastAPI ou Flet est absent

Ces dépendances sont optionnelles :

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[api]"
.\.venv\Scripts\python.exe -m pip install -e ".[mobile]"
```

### Build iOS sous Windows

La signature iOS exige macOS et Xcode. Android peut utiliser Flet, Flutter,
React Native ou une couche native approuvée selon le contrat du projet.
