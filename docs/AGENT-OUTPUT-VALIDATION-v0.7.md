# Agent Output Validation ELMAN-OS v0.7

## Statut

Huitième incrément du Jalon 2 — Planification et orchestration.

Ce module valide de manière strictement déclarative les artefacts annoncés
dans un `AgentResponseIngestionResult`. Il vérifie la structure, les chemins,
les empreintes, les tailles, les classifications et les conflits sans lire,
créer, modifier ou exécuter un fichier.

## Fichiers

| Fichier | Rôle |
|---|---|
| `src/elman_os/agent_output_validation.py` | Définit la politique, la requête, les enregistrements et le résultat de validation. |
| `tests/test_agent_output_validation.py` | Vérifie les chemins, hashes, limites, classifications, conflits, statuts et sérialisations. |
| `docs/AGENT-OUTPUT-VALIDATION-v0.7.md` | Documente le format des artefacts, les décisions, les contrôles et les limites. |

## Contrats

### `AgentOutputValidationPolicy`

La politique définit les limites et classifications de validation :

- nombre maximal d’artefacts ;
- taille maximale par artefact ;
- taille maximale cumulée ;
- longueur maximale des chemins et segments ;
- classifications exigeant une revue ;
- extensions exigeant une revue ;
- extensions interdites ;
- noms et suffixes sensibles interdits ;
- préfixes de chemins exigeant une revue ;
- hash SHA-256 déterministe de la politique.

Les valeurs par défaut sont conservatrices et portables entre Windows,
macOS et Linux.

### `AgentOutputValidationRequest`

La requête est liée cryptographiquement à :

- la politique ;
- l’ingestion de la réponse ;
- le plan ;
- l’étape ;
- le `AgentRequest` ;
- l’agent ;
- le statut et le hash du `AgentResponse` ;
- l’état canonique du plan ;
- le nombre d’événements du journal ;
- le hash de tête du journal ;
- le hash global du journal.

`from_ingestion_result()` construit cette frontière à partir d’un
`AgentResponseIngestionResult` vérifié.

### `ArtifactValidationRecord`

Chaque déclaration produit un enregistrement comprenant :

- son index ;
- le chemin normalisé ;
- la décision ;
- la classification ;
- l’opération ;
- l’empreinte SHA-256 ;
- la taille déclarée ;
- le type de média ;
- les raisons exactes de la décision.

### `AgentOutputValidationResult`

Le résultat contient :

- le statut global ;
- les références de politique et d’ingestion ;
- tous les enregistrements d’artefacts ;
- les raisons de niveau global ;
- les compteurs `accepted`, `requires-review` et `rejected` ;
- la taille totale déclarée ;
- la frontière plan/journal ;
- un hash SHA-256 du résultat.

## Format des outputs

Le `AgentResponse.outputs` peut déclarer un artefact unique :

```json
{
  "artifact": {
    "path": "src/generated.py",
    "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "size_bytes": 120,
    "media_type": "text/x-python",
    "kind": "source",
    "operation": "create",
    "executable": false,
    "metadata": {
      "generator": "ELMAN_CORE"
    }
  }
}
```

Ou une collection :

```json
{
  "artifacts": [
    {
      "path": "src/generated.py",
      "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "size_bytes": 120,
      "media_type": "text/x-python",
      "kind": "source"
    },
    {
      "path": "tests/test_generated.py",
      "sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
      "size_bytes": 220,
      "media_type": "text/x-python",
      "kind": "test"
    }
  ]
}
```

La présence simultanée de `artifact` et `artifacts` est refusée.

## Champs requis

Chaque artefact exige :

- `path` ;
- `sha256` ;
- `size_bytes` ;
- `media_type` ;
- `kind`.

Champs facultatifs :

- `operation`, valeur par défaut `create` ;
- `executable`, valeur par défaut `false` ;
- `metadata`, objet JSON.

Les champs inconnus sont refusés afin d’éviter toute interprétation
silencieuse.

## Classifications

Les classifications prises en charge sont :

```text
source
test
documentation
configuration
data
report
patch
archive
binary
other
```

Par défaut, `patch`, `archive`, `binary` et `other` exigent une revue humaine.

## Opérations

Deux opérations déclaratives sont admises :

```text
create
update
```

`update` exige une revue, car ce module ne lit pas le workspace et ne peut
pas vérifier l’état antérieur du fichier.

La suppression d’un fichier n’est pas autorisée dans cet incrément.

## Validation des chemins

Les chemins doivent être :

- relatifs ;
- écrits avec `/` ;
- canoniques ;
- sans segment vide ;
- sans `.` ou `..` ;
- sans traversée de répertoires ;
- sans lettre de lecteur Windows ;
- sans caractère de contrôle ;
- sans caractère non portable `< > : " | ? *` ;
- sans nom réservé Windows tel que `CON`, `NUL`, `COM1` ou `LPT1` ;
- conformes aux limites de longueur de la politique.

Les variantes de casse sont comparées de manière insensible à la casse pour
détecter les conflits portables entre systèmes de fichiers.

## Empreintes et tailles

`sha256` doit contenir exactement 64 caractères hexadécimaux minuscules.

`size_bytes` doit être un entier positif ou nul.

La validation applique :

- la limite par artefact ;
- la limite cumulée ;
- la limite du nombre d’artefacts.

Le module valide uniquement les valeurs déclarées. Il ne lit pas le contenu
réel et ne recalcule donc pas encore l’empreinte d’un fichier sur disque.

## Types de média

`media_type` doit respecter une forme minuscule `type/subtype`, par exemple :

```text
text/x-python
text/markdown
application/json
application/zip
```

Aucune détection de contenu n’est effectuée dans cet incrément.

## Décisions

### `accepted`

Une déclaration est acceptée lorsque :

- tous les champs sont valides ;
- le chemin est portable ;
- l’empreinte et la taille sont valides ;
- aucune limite n’est dépassée ;
- aucune règle de revue ou d’interdiction ne s’applique ;
- aucun doublon ou conflit n’est détecté.

### `requires-review`

Une revue est requise notamment pour :

- une opération `update` ;
- un artefact déclaré exécutable ;
- une archive, un binaire, un patch ou une classification `other` ;
- une extension de script ou d’archive sensible ;
- un chemin sous `.github/workflows/`, `infrastructure/`, `security/` ou
  `scripts/release/` ;
- le même hash déclaré sous plusieurs chemins ;
- des outputs supplémentaires non interprétés ;
- une réponse réussie ne déclarant aucun artefact.

### `rejected`

Une déclaration est refusée notamment pour :

- une réponse dont le statut n’est pas `succeeded` ;
- un schéma incomplet ou inconnu ;
- un chemin absolu ou non portable ;
- une traversée `..` ;
- une empreinte invalide ;
- une taille invalide ou excessive ;
- une classification ou opération inconnue ;
- un exécutable natif interdit ;
- un fichier secret ou certificat sensible ;
- un doublon de chemin ;
- un conflit de casse ou de contenu sur le même chemin ;
- le dépassement du nombre ou de la taille cumulée autorisés.

## Fichiers sensibles

La politique par défaut refuse notamment :

```text
.env
.npmrc
.pypirc
id_rsa
id_ed25519
*.key
*.pem
*.p12
*.pfx
```

Elle refuse aussi les extensions exécutables natives suivantes :

```text
.exe
.dll
.so
.dylib
.msi
.com
.scr
.pyd
```

## Doublons et conflits

Le module détecte :

- la même déclaration répétée ;
- plusieurs contenus visant le même chemin ;
- deux variantes de casse du même chemin portable ;
- le même contenu déclaré sous plusieurs chemins.

Les conflits de chemin sont refusés.

Le même hash sous plusieurs chemins exige une revue, car il peut s’agir d’une
duplication intentionnelle ou d’une erreur.

## Statut global

Le statut global est calculé de façon déterministe :

1. au moins un refus entraîne `rejected` ;
2. sinon, au moins une revue entraîne `requires-review` ;
3. sinon le résultat est `accepted`.

Les compteurs et le statut sont revalidés lors de la désérialisation.

## Intégrité

Les artefacts exposent :

- `AgentOutputValidationPolicy.policy_hash` ;
- `AgentOutputValidationRequest.request_hash` ;
- `AgentOutputValidationResult.result_hash`.

Le résultat conserve également :

- le hash du résultat d’ingestion ;
- le hash de la réponse ;
- le hash de l’état du plan ;
- les hashes du journal ;
- le nombre d’événements.

Toute altération d’un enregistrement, d’un compteur, d’une taille ou du statut
est détectée.

## Sérialisation

La politique, la requête et le résultat fournissent :

- `to_dict()` ;
- `to_json()` ;
- `from_dict()` ;
- `from_json()`.

La requête et le résultat fournissent aussi `verify_hash()`.

Le JSON est compact, trié et déterministe.

## Garanties

- validation purement déclarative ;
- aucune lecture du workspace ;
- aucune création de fichier ;
- aucune modification de fichier ;
- aucune suppression de fichier ;
- aucune application de patch ;
- aucune exécution de contenu ;
- aucune importation dynamique ;
- aucune connexion réseau ;
- aucune mutation du plan ou du journal ;
- aucune transition d’état ;
- aucun changement de version, tag ou release.

## Hors périmètre

Cet incrément ne fournit pas encore :

- la matérialisation des artefacts ;
- la vérification du contenu réel ;
- le recalcul des hashes depuis le disque ;
- l’analyse antivirus ;
- l’analyse statique du code ;
- la détection de secrets dans le contenu ;
- la comparaison avec le workspace ;
- l’application transactionnelle d’un patch ;
- la signature numérique ;
- l’intégration ELMAN Studio.
