---
name: backtest-before-deploy
description: "Use when a strategy is declared ready for live trading. Triggers on 'ready to deploy', 'let's go live', 'strategy is done', or any indication that a strategy should move from development to production. Also triggers after completing strategy implementation work. Mandatory gate: no strategy goes live without passing a backtest."
---

# Backtest Before Deploy

Aucune strategie ne passe en live sans backtest. Cette skill enforce ce gate et definit ce que "passer" un backtest signifie.

## When to Use

- L'utilisateur indique qu'une strategie est prete pour le deploiement
- Une tache d'implementation de strategie est marquee complete
- L'utilisateur demande d'activer une strategie en production

## Process

### Step 1: Verifier que les donnees enregistrees existent

Checker `data/raw/` pour des donnees de marche enregistrees. Si pas de donnees:
- **STOP.** Informer l'utilisateur que le backtesting requiert des donnees enregistrees.
- Suggerer de lancer `python scripts/record_data.py` pendant au moins 24h.
- Ne pas continuer sans donnees.

### Step 2: Lancer le backtest

```bash
python scripts/backtest.py --strategy <strategy_name> --data data/raw/ --output data/processed/backtest_<strategy>_<timestamp>/
```

Capturer toute la sortie. Lire le fichier de resultats.

### Step 3: Evaluer les metriques

| Metrique | Seuil minimum | Description |
|----------|--------------|-------------|
| Net P&L (apres fees) | > 0 | Doit etre profitable apres les ~3.15% de fees |
| Win Rate | > 52% | Doit surmonter le drag des fees |
| Max Drawdown | < 15% du capital | Un losing streak ne doit pas etre catastrophique |
| Sharpe Ratio | > 1.0 | Rendements ajustes au risque justifient le risque |
| Nombre de trades | > 100 | Significativite statistique requiert un echantillon |
| Duree moy. trade | < 5 min | Doit correspondre a la structure du marche 5-min |

### Step 4: Rapport

```
## Backtest Report -- [Strategy] -- [Date]

### READY / NOT READY

**Periode:** [start] to [end] ([N] intervalles)
**Total Trades:** [N]

**Metriques:**
| Metrique | Valeur | Seuil | Statut |
|----------|--------|-------|--------|
| Net P&L | ... | > 0 | PASS/FAIL |
| Win Rate | ... | > 52% | PASS/FAIL |
| Max Drawdown | ... | < 15% | PASS/FAIL |
| Sharpe Ratio | ... | > 1.0 | PASS/FAIL |

**Pire periode:** [description du pire losing streak]
**Recommendation:** [DEPLOY / DO NOT DEPLOY / DEPLOY WITH REDUCED SIZE]
```

### Step 5: Decision

- **Tout PASS:** Deployer. Recommander de commencer avec 25% de la taille max pendant 24h.
- **Un FAIL:** NOT READY. Identifier les metriques en echec et ce que la strategie doit changer.
- **Borderline:** Recommander le paper trading (pipeline complet, pas d'ordres reels).

## Anti-patterns prevenus

- "Ca marche en tests unitaires, on shippe" -- Les unit tests testent la logique, pas la profitabilite
- "Je backtesterai apres le live" -- Trop tard, l'argent est deja perdu
- "Le backtest n'a que 20 trades mais tous gagnants" -- Echantillon insuffisant
- "C'est profitable avant les fees" -- Non pertinent; les fees sont reelles et obligatoires
