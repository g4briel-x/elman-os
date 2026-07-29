# ELMAN-OS — Politique technologique v1.2

## Décision

> ELMAN-OS est Python-first, mais pas Python-only.

Python est obligatoire pour :

- kernel ;
- orchestrateur, spécialistes et vérificateur ;
- couche métacognitive ;
- control API et services du kernel ;
- plugins internes ;
- mémoire, persistance, audits et tests du kernel ;
- installation et packaging du kernel.

Les autres langages sont autorisés uniquement dans une couche où ils apportent
une adéquation technique claire et restent isolés du kernel.

## Matrice des couches

| Couche | Choix par défaut | Alternatives |
|---|---|---|
| Kernel | Python 3.11+ | aucune |
| Web | Python/Flet | JavaScript, TypeScript, React, Next.js, Vue, Svelte |
| Mobile | Python/Flet | TypeScript, Dart, Kotlin, Swift, Java |
| Extension native | Python | Rust, C, C++ |
| Données | Python/SQLite | SQL, PostgreSQL |
| Plateforme | Python | PowerShell ou shell dédié |

## Frontières exécutables

Zones Python-only :

- `src/elman_os`
- `tests`
- `apps/control_api`
- `plugins`

Zones web approuvées :

- `apps/studio/frontend`
- `apps/web`
- `apps/mobile` pour React Native/Expo
- `templates/web`
- `templates/mobile`
- `generated`

Zones mobiles natives :

- `apps/mobile`
- `templates/mobile`
- `generated`

Zones natives :

- `extensions/native`
- `generated`

Zones plateforme :

- `infrastructure`
- `scripts/platform`
- `generated`

Zones SQL :

- `data/sql`
- `migrations`
- `generated`

La commande suivante applique ces frontières :

```powershell
.\.venv\Scripts\python.exe -m elman_os audit-stack .
```

## Règles de décision

Un langage alternatif doit avoir :

1. une exigence produit ou plateforme ;
2. une décision Atlas traçable ;
3. un propriétaire d’agent ;
4. une zone autorisée ;
5. des tests et outils de build identifiés ;
6. un contrôle Shield sur dépendances et permissions ;
7. un contrôle Proof avant livraison ;
8. une procédure de rollback.

L’usage d’un langage ne doit jamais être justifié uniquement par une préférence
ou une mode technique.

## Installation

L’installation du kernel n’exige que Python et `pip`.

Node.js, Flutter, Android SDK, Xcode, Rust ou un compilateur natif sont installés
uniquement dans le workspace du projet qui les exige. Ils ne deviennent pas des
dépendances du noyau.

