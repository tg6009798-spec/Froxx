# FROXX Economy — Fixed Build

## Implemented
- Mines mine-hit now **settles the pending round with a 0 payout**, so `game_rounds.status` becomes `settled` and the same user can immediately start another game.
- Mines loss is recorded in the existing game stats/transaction ledger.
- Added `fx admin charge @user <amount> [reason]` (aliases: `deduct`, `fine`). The negative wallet delta is permanently visible in `fx transactions`.
- Crate opening is atomic: crate consumption + coins + optional item + ledger entry happen in one DB transaction.
- Mystery Box coin reward is capped at **1,700**, can be below 1,500, and its bonus item is rare.
- Crate + bonus-item total value is capped at **purchase price + 150 coins**, so crate loops cannot generate large guaranteed profit. Losses are possible.
- Existing game odds remain fixed/random rather than being dynamically manipulated per player.

## QA
`simulate_froxx.py` ran an offline **2,000,000-round** Monte Carlo test covering Mines and all three crate tiers.

Results from the run included:
- Mines test: 69.53% loss / 30.47% cashout under the simulated random strategy; net -60,585,380 coins over 2,000,000 rounds.
- Mystery Box: average cash 972.32; item rate 2.51%; maximum total reward value 1,650; profit range -1,250 to +150.
- Rare Crate: average cash 9,320.06; item rate 1.79%; maximum total reward value 12,150; profit range -5,500 to +150.
- Legendary Crate: average cash 45,062.12; item rate 1.20%; maximum total reward value 75,150; profit range -60,000 to +150.

## Run
Set `DISCORD_TOKEN` and `OWNER_ID` in `.env`, then run `main.py`.
