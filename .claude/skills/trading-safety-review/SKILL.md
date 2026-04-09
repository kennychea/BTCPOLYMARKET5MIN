---
name: trading-safety-review
description: "Use when reviewing or merging code that touches trading logic: strategies/, execution/, risk/, or core/safety.py. Also use when the user says 'review this for safety', 'is this safe to deploy', or any variant of safety/risk review for trading code. Mandatory gate before deploying trading logic changes."
---

# Trading Safety Review

Revue de securite obligatoire pour tout changement de code pouvant affecter le trading live. Cette revue capture les bugs que les tests unitaires manquent: erreurs de logique syntaxiquement correctes mais financierement destructrices.

## When to Use

- Tout diff touchant: `src/polybot/strategies/`, `src/polybot/execution/`, `src/polybot/risk/`, `src/polybot/core/safety.py`
- L'utilisateur demande une revue de securite
- Avant tout merge d'une branche liee aux strategies

## Checklist obligatoire

Executer chaque item. Ne pas sauter d'items. Ne pas marquer "N/A" sans justification.

### 1. Position and Exposure Guards
- [ ] Le code respecte les limites de position de `risk/limits.py` ?
- [ ] Un chemin de code peut-il creer une position non bornee ?
- [ ] Operations arithmetiques sur les tailles de position : overflow/underflow possible ?
- [ ] Le kill switch (`core/safety.py`) est verifie avant chaque soumission d'ordre ?

### 2. Fee and Slippage Accounting
- [ ] Chaque calcul de valeur attendue inclut les ~3.15% de fees dynamiques ?
- [ ] Le slippage est comptabilise pour les market orders ?
- [ ] Valeurs de fees hardcodees ? (doivent utiliser `fee_calculator.py`)

### 3. Timing and Boundaries
- [ ] Le code utilise `core/clock.py` pour les frontieres d'intervalle ?
- [ ] Un trade peut-il etre place apres la cloture d'un intervalle ?
- [ ] Race conditions entre MAJ du feed prix et soumission d'ordre ?

### 4. Error Handling
- [ ] Les exceptions propagent au kill switch ?
- [ ] `except: pass` ou exception swallowing large ?
- [ ] Que se passe-t-il si un feed se deconnecte mid-evaluation ?

### 5. State Corruption
- [ ] Un fill partiel peut-il laisser le position tracker inconsistant ?
- [ ] Structures de donnees mutables partagees protegees ?
- [ ] En cas de restart, recovery vers un etat safe ?

### 6. Backtest Validation
- [ ] La strategie a ete backtestee avec les nouveaux changements ?
- [ ] Les resultats ne montrent pas de degradation (Sharpe, max drawdown, win rate) ?
- [ ] Edge cases testes (volatilite, faible liquidite, gaps de feed) ?

## Output

```
## Trading Safety Review -- [Date] -- [Branch/Feature]

### PASS / FAIL / CONDITIONAL

**Findings:**
1. [Finding - severity: CRITICAL / WARNING / INFO]

**Recommendations:**
1. ...

**Backtest Summary:**
- Before: [metrics]
- After: [metrics]
```

Si un finding CRITICAL existe, le resultat est FAIL. Ne pas approuver.

## Red Flags

| Pattern | Danger |
|---------|--------|
| `except Exception: pass` dans le chemin d'execution | Avale silencieusement des erreurs qui devraient trigger le kill switch |
| Fee hardcodee (ex: `0.0315`) | La fee est dynamique; les valeurs hardcodees deviennent fausses |
| `time.time()` au lieu de `core/clock` | Le drift d'horloge peut causer des trades hors intervalle |
| Pas de check position avant ordre | Peut depasser les limites si deux strategies tournent en parallele |
| `await asyncio.sleep(0)` comme "fix" de race condition | Ne fixe pas la race; la masque |
| Strategie modifie le state partage directement | Doit passer par position_tracker pour la consistance |
