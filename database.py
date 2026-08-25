from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import secrets
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Iterable

import aiosqlite

log = logging.getLogger("froxx.db")


def now_ts() -> int:
    return int(datetime.now(timezone.utc).timestamp())


@dataclass(slots=True)
class Account:
    user_id: int
    wallet: int
    bank: int
    xp: int
    level: int
    total_earned: int
    total_spent: int
    daily_streak: int
    created_at: int
    last_active: int
    last_daily: int | None
    daily_cycle: int


class Database:
    """SQLite repository. SQL is isolated here so a PostgreSQL repository can replace it later."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._write_lock = asyncio.Lock()
        import os
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)

    async def healthcheck(self) -> bool:
        try:
            async with aiosqlite.connect(self.path) as db:
                row = await (await db.execute('PRAGMA quick_check')).fetchone()
                return bool(row and str(row[0]).lower() == 'ok')
        except (OSError, sqlite3.Error):
            log.exception('SQLite healthcheck failed for %s', self.path)
            return False

    async def init(self) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA synchronous=NORMAL")
            await db.execute("PRAGMA temp_store=MEMORY")
            await db.execute("PRAGMA cache_size=-16000")
            await db.execute("PRAGMA mmap_size=0")
            await db.execute("PRAGMA foreign_keys=ON")
            await db.execute("PRAGMA busy_timeout=5000")
            await db.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    wallet INTEGER NOT NULL DEFAULT 0 CHECK(wallet >= 0),
                    bank INTEGER NOT NULL DEFAULT 0 CHECK(bank >= 0),
                    xp INTEGER NOT NULL DEFAULT 0 CHECK(xp >= 0),
                    level INTEGER NOT NULL DEFAULT 1 CHECK(level >= 1),
                    total_earned INTEGER NOT NULL DEFAULT 0 CHECK(total_earned >= 0),
                    total_spent INTEGER NOT NULL DEFAULT 0 CHECK(total_spent >= 0),
                    daily_streak INTEGER NOT NULL DEFAULT 0 CHECK(daily_streak >= 0),
                    created_at INTEGER NOT NULL,
                    last_active INTEGER NOT NULL,
                    last_daily INTEGER,
                    daily_cycle INTEGER NOT NULL DEFAULT 0,
                    weekly_earned INTEGER NOT NULL DEFAULT 0,
                    weekly_anchor INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(user_id)
                );
                CREATE TABLE IF NOT EXISTS froxx_admins (
                    user_id INTEGER PRIMARY KEY,
                    granted_by INTEGER NOT NULL,
                    granted_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_froxx_admins_granted_by ON froxx_admins(granted_by);

                CREATE TABLE IF NOT EXISTS game_rounds (
                    round_id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    game_key TEXT NOT NULL,
                    bet INTEGER NOT NULL CHECK(bet > 0),
                    status TEXT NOT NULL DEFAULT 'pending',
                    payout INTEGER NOT NULL DEFAULT 0 CHECK(payout >= 0),
                    created_at INTEGER NOT NULL,
                    settled_at INTEGER,
                    FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_game_rounds_pending ON game_rounds(status, created_at);
                CREATE INDEX IF NOT EXISTS idx_game_rounds_user_game ON game_rounds(user_id, game_key, status);

                CREATE TABLE IF NOT EXISTS items (
                    item_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    emoji TEXT NOT NULL,
                    rarity TEXT NOT NULL,
                    description TEXT NOT NULL,
                    item_type TEXT NOT NULL,
                    buy_price INTEGER NOT NULL DEFAULT 0,
                    sell_price INTEGER NOT NULL DEFAULT 0,
                    effect_json TEXT NOT NULL DEFAULT '{}',
                    active INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS inventory (
                    user_id INTEGER NOT NULL,
                    item_id TEXT NOT NULL,
                    quantity INTEGER NOT NULL CHECK(quantity > 0),
                    PRIMARY KEY(user_id, item_id),
                    FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE,
                    FOREIGN KEY(item_id) REFERENCES items(item_id)
                );
                CREATE TABLE IF NOT EXISTS transactions (
                    tx_id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    amount INTEGER NOT NULL,
                    wallet_delta INTEGER NOT NULL DEFAULT 0,
                    bank_delta INTEGER NOT NULL DEFAULT 0,
                    meta_json TEXT NOT NULL DEFAULT '{}',
                    created_at INTEGER NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_transactions_user_time ON transactions(user_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS cooldowns (
                    user_id INTEGER NOT NULL,
                    key TEXT NOT NULL,
                    expires_at INTEGER NOT NULL,
                    PRIMARY KEY(user_id, key)
                );
                CREATE INDEX IF NOT EXISTS idx_cooldowns_expiry ON cooldowns(expires_at);
                CREATE TABLE IF NOT EXISTS achievements (
                    achievement_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    emoji TEXT NOT NULL,
                    description TEXT NOT NULL,
                    requirement INTEGER NOT NULL,
                    metric TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS user_achievements (
                    user_id INTEGER NOT NULL,
                    achievement_id TEXT NOT NULL,
                    unlocked_at INTEGER NOT NULL,
                    PRIMARY KEY(user_id, achievement_id),
                    FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE,
                    FOREIGN KEY(achievement_id) REFERENCES achievements(achievement_id)
                );
                CREATE TABLE IF NOT EXISTS shop_items (
                    item_id TEXT PRIMARY KEY,
                    slot INTEGER NOT NULL,
                    featured INTEGER NOT NULL DEFAULT 0,
                    expires_at INTEGER NOT NULL,
                    FOREIGN KEY(item_id) REFERENCES items(item_id)
                );
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS trade_sessions (
                    trade_id TEXT PRIMARY KEY,
                    user_a INTEGER NOT NULL,
                    user_b INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    offer_a_json TEXT NOT NULL,
                    offer_b_json TEXT NOT NULL,
                    confirm_a INTEGER NOT NULL DEFAULT 0,
                    confirm_b INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL,
                    FOREIGN KEY(user_a) REFERENCES users(user_id) ON DELETE CASCADE,
                    FOREIGN KEY(user_b) REFERENCES users(user_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_trade_users ON trade_sessions(user_a, user_b, state);
                """
            )
            await db.commit()
        await self.seed()

    async def seed(self) -> None:
        items = [
            ("mystery_box", "Mystery Box", "🎁", "COMMON", "A surprise bundle of coins, XP or collectibles.", "crate", 1500, 450, '{"crate":"mystery"}'),
            ("rare_crate", "Rare Crate", "💎", "RARE", "A higher-tier virtual reward crate.", "crate", 12000, 3600, '{"crate":"rare"}'),
            ("legendary_crate", "Legendary Crate", "👑", "LEGENDARY", "A premium virtual reward crate.", "crate", 75000, 22500, '{"crate":"legendary"}'),
            ("ancient_crystal", "Ancient Crystal", "💠", "EPIC", "A mysterious collectible from forgotten ruins.", "collectible", 0, 3500, '{}'),
            ("moonstone", "Moonstone", "🌙", "RARE", "A luminous collectible fragment.", "collectible", 0, 1800, '{}'),
            ("golden_key", "Golden Key", "🗝️", "LEGENDARY", "Opens special virtual reward caches.", "collectible", 9000, 2700, '{}'),
            ("xp_booster", "XP Booster", "⚡", "UNCOMMON", "Adds +25% XP to your next qualifying activity.", "booster", 5000, 1500, '{"xp_mult":1.25,"uses":1}'),
            ("coin_booster", "Coin Booster", "🪙", "RARE", "Adds +15% coins to your next qualifying activity.", "booster", 10000, 3000, '{"coin_mult":1.15,"uses":1}'),
            ("collector_badge", "Collector Badge", "🏅", "EPIC", "A prestigious virtual collectible.", "cosmetic", 25000, 7500, '{}'),
            ("iron_scrap", "Iron Scrap", "🔩", "COMMON", "A rough piece of metal recovered from a random build.", "collectible", 0, 120, '{}'),
            ("copper_wire", "Copper Wire", "🧵", "COMMON", "Useful wire with a surprising resale value.", "collectible", 0, 260, '{}'),
            ("moon_shard", "Moon Shard", "🌙", "UNCOMMON", "A glowing shard found in strange night material.", "collectible", 0, 650, '{}'),
            ("emerald_chip", "Emerald Chip", "🟢", "RARE", "A bright emerald fragment.", "collectible", 0, 1800, '{}'),
            ("ruby_core", "Ruby Core", "🔴", "RARE", "A hot red core from a mysterious machine.", "collectible", 0, 3200, '{}'),
            ("void_fragment", "Void Fragment", "🕳️", "EPIC", "A fragment that seems to absorb light.", "collectible", 0, 7000, '{}'),
            ("star_relic", "Star Relic", "🌟", "LEGENDARY", "A relic worth a fortune to collectors.", "collectible", 0, 22000, '{}'),
            ("ancient_core", "Ancient Core", "💠", "MYTHIC", "An extremely rare core from a forgotten age.", "collectible", 0, 75000, '{}'),
        ]
        achievements = [
            ("first_fortune", "First Fortune", "💰", "Earn your first 1,000 coins.", 1000, "total_earned"),
            ("banker", "Banker", "🏦", "Hold 50,000 coins in your bank.", 50000, "bank"),
            ("hard_worker", "Hard Worker", "⛏️", "Complete 25 work shifts.", 25, "work_count"),
            ("seven_day", "Seven Day Streak", "🔥", "Reach a 7-day daily streak.", 7, "daily_streak"),
            ("collector", "Collector", "💎", "Own 5 different collectible items.", 5, "unique_collectibles"),
            ("millionaire", "Millionaire", "👑", "Reach 1,000,000 total coins across wallet and bank.", 1000000, "net_worth"),
            ("shopaholic", "Shopaholic", "🛒", "Spend 100,000 coins in the economy.", 100000, "total_spent"),
            ("top10", "Top 10", "🏆", "Reach a top-10 wallet rank.", 10, "rank"),
        ]
        async with self._write_lock:
            async with aiosqlite.connect(self.path) as db:
                await db.executemany(
                    "INSERT OR IGNORE INTO items(item_id,name,emoji,rarity,description,item_type,buy_price,sell_price,effect_json) VALUES(?,?,?,?,?,?,?,?,?)",
                    items,
                )
                await db.executemany(
                    "INSERT OR IGNORE INTO achievements(achievement_id,name,emoji,description,requirement,metric) VALUES(?,?,?,?,?,?)",
                    achievements,
                )
                await db.commit()

    @asynccontextmanager
    async def tx(self) -> AsyncIterator[aiosqlite.Connection]:
        async with self._write_lock:
            db = await aiosqlite.connect(self.path)
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA foreign_keys=ON")
            await db.execute("PRAGMA busy_timeout=5000")
            try:
                await db.execute("BEGIN IMMEDIATE")
                yield db
                await db.commit()
            except Exception:
                await db.rollback()
                raise
            finally:
                await db.close()

    async def ensure_user(self, user_id: int, starter: bool = False) -> tuple[Account, bool]:
        ts = now_ts()
        async with self.tx() as db:
            row = await (await db.execute("SELECT * FROM users WHERE user_id=?", (user_id,))).fetchone()
            if row:
                await db.execute("UPDATE users SET last_active=? WHERE user_id=?", (ts, user_id))
                return Account(**{k: row[k] for k in Account.__annotations__}), False
            wallet = 10_000 if starter else 0
            xp = 100 if starter else 0
            await db.execute(
                "INSERT INTO users(user_id,wallet,xp,created_at,last_active,weekly_anchor) VALUES(?,?,?,?,?,?)",
                (user_id, wallet, xp, ts, ts, ts),
            )
            await db.execute(
                "INSERT INTO transactions(tx_id,user_id,kind,amount,wallet_delta,meta_json,created_at) VALUES(?,?,?,?,?,?,?)",
                (f"start:{user_id}", user_id, "Starter Reward", wallet, wallet, json.dumps({"starter": starter}), ts),
            )
            row = await (await db.execute("SELECT * FROM users WHERE user_id=?", (user_id,))).fetchone()
            return Account(**{k: row[k] for k in Account.__annotations__}), True

    async def get_account(self, user_id: int) -> Account | None:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            row = await (await db.execute("SELECT * FROM users WHERE user_id=?", (user_id,))).fetchone()
            return Account(**{k: row[k] for k in Account.__annotations__}) if row else None

    async def get_item(self, item_id: str) -> aiosqlite.Row | None:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            return await (await db.execute("SELECT * FROM items WHERE item_id=? AND active=1", (item_id,))).fetchone()

    async def all_items(self) -> list[aiosqlite.Row]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            return await (await db.execute("SELECT * FROM items WHERE active=1 ORDER BY CASE rarity WHEN 'MYTHIC' THEN 6 WHEN 'LEGENDARY' THEN 5 WHEN 'EPIC' THEN 4 WHEN 'RARE' THEN 3 WHEN 'UNCOMMON' THEN 2 ELSE 1 END DESC,name")).fetchall()

    async def get_inventory(self, user_id: int, item_type: str | None = None) -> list[aiosqlite.Row]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            sql = "SELECT i.*, inv.quantity FROM inventory inv JOIN items i ON i.item_id=inv.item_id WHERE inv.user_id=?"
            args: list[Any] = [user_id]
            if item_type:
                sql += " AND i.item_type=?"
                args.append(item_type)
            sql += " ORDER BY CASE i.rarity WHEN 'MYTHIC' THEN 6 WHEN 'LEGENDARY' THEN 5 WHEN 'EPIC' THEN 4 WHEN 'RARE' THEN 3 WHEN 'UNCOMMON' THEN 2 ELSE 1 END DESC,i.name"
            return await (await db.execute(sql, args)).fetchall()

    async def cooldown_remaining(self, user_id: int, key: str) -> int:
        ts = now_ts()
        async with aiosqlite.connect(self.path) as db:
            row = await (await db.execute("SELECT expires_at FROM cooldowns WHERE user_id=? AND key=?", (user_id, key))).fetchone()
            if not row:
                return 0
            remaining = max(0, row[0] - ts)
            if remaining == 0:
                await db.execute("DELETE FROM cooldowns WHERE user_id=? AND key=?", (user_id, key))
                await db.commit()
            return remaining

    async def claim_free_reward(
        self,
        user_id: int,
        key: str,
        kind: str,
        coins: int,
        xp: int,
        cooldown: int,
        tx_id: str | None = None,
        work_delta: int = 0,
    ) -> tuple[bool, Account]:
        """Atomically validate cooldown and settle a free reward.

        The cooldown check, reward credit, transaction log and cooldown write
        happen in one SQLite transaction. This closes the classic
        check-then-write race where two simultaneous commands could both pay.
        """
        if coins < 0 or xp < 0 or cooldown <= 0:
            raise ValueError("invalid reward")
        ts = now_ts()
        if not tx_id:
            tx_id = f"free:{key}:{user_id}:{ts}:{secrets.token_hex(8)}"
        async with self.tx() as db:
            row = await (await db.execute(
                "SELECT * FROM users WHERE user_id=?", (user_id,)
            )).fetchone()
            if not row:
                raise ValueError("account not found")

            cd = await (await db.execute(
                "SELECT expires_at FROM cooldowns WHERE user_id=? AND key=?",
                (user_id, key),
            )).fetchone()
            if cd and cd[0] > ts:
                return False, Account(**{k: row[k] for k in Account.__annotations__})

            if await (await db.execute(
                "SELECT 1 FROM transactions WHERE tx_id=?", (tx_id,)
            )).fetchone():
                return True, Account(**{k: row[k] for k in Account.__annotations__})

            await db.execute(
                """UPDATE users
                   SET wallet=wallet+?,
                       xp=xp+?,
                       total_earned=total_earned+?,
                       weekly_earned=weekly_earned+?,
                       last_active=?
                   WHERE user_id=?""",
                (coins, xp, coins, coins, ts, user_id),
            )
            await db.execute(
                """INSERT INTO transactions(
                       tx_id,user_id,kind,amount,wallet_delta,bank_delta,meta_json,created_at
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    tx_id, user_id, kind, coins, coins, 0,
                    json.dumps({"xp": xp}, separators=(",", ":")), ts
                ),
            )
            if work_delta:
                await db.execute(
                    """INSERT INTO settings(key,value) VALUES(?,?)
                       ON CONFLICT(key) DO UPDATE SET value=CAST(CAST(value AS INTEGER)+? AS TEXT)""",
                    (f"stat:work:{user_id}", str(work_delta), work_delta),
                )
            await db.execute(
                """INSERT INTO cooldowns(user_id,key,expires_at)
                   VALUES(?,?,?)
                   ON CONFLICT(user_id,key)
                   DO UPDATE SET expires_at=excluded.expires_at""",
                (user_id, key, ts + cooldown),
            )
            updated = await (await db.execute(
                "SELECT * FROM users WHERE user_id=?", (user_id,)
            )).fetchone()
            return True, Account(**{k: updated[k] for k in Account.__annotations__})

    async def create_random_drop(self, user_id: int, weighted_items: list[tuple[str, int]], cooldown: int, tx_id: str) -> dict[str, Any]:
        """Atomically check the create cooldown and award one weighted loot item."""
        if not weighted_items or cooldown <= 0:
            raise ValueError("invalid create configuration")
        total_weight = sum(max(0, int(w)) for _, w in weighted_items)
        if total_weight <= 0:
            raise ValueError("invalid loot weights")
        import random
        roll = random.SystemRandom().randrange(total_weight)
        chosen_id = weighted_items[-1][0]
        cursor = 0
        for item_id, weight in weighted_items:
            cursor += max(0, int(weight))
            if roll < cursor:
                chosen_id = item_id
                break
        ts = now_ts()
        async with self.tx() as db:
            user = await (await db.execute("SELECT * FROM users WHERE user_id=?", (user_id,))).fetchone()
            if not user:
                raise ValueError("account not found")
            cd = await (await db.execute("SELECT expires_at FROM cooldowns WHERE user_id=? AND key=?", (user_id, 'create'))).fetchone()
            if cd and cd[0] > ts:
                return {"claimed": False, "remaining": cd[0] - ts}
            item = await (await db.execute("SELECT * FROM items WHERE item_id=? AND active=1", (chosen_id,))).fetchone()
            if not item:
                raise ValueError("loot item missing")
            if await (await db.execute("SELECT 1 FROM transactions WHERE tx_id=?", (tx_id,))).fetchone():
                return {"claimed": True, "item": item}
            await db.execute(
                """INSERT INTO inventory(user_id,item_id,quantity) VALUES(?,?,1)
                   ON CONFLICT(user_id,item_id) DO UPDATE SET quantity=quantity+1""",
                (user_id, chosen_id),
            )
            await db.execute(
                """INSERT INTO transactions(tx_id,user_id,kind,amount,wallet_delta,meta_json,created_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (tx_id, user_id, "Create Loot", 0, 0, json.dumps({"item": chosen_id, "rarity": item["rarity"]}, separators=(",", ":")), ts),
            )
            await db.execute(
                """INSERT INTO cooldowns(user_id,key,expires_at) VALUES(?,?,?)
                   ON CONFLICT(user_id,key) DO UPDATE SET expires_at=excluded.expires_at""",
                (user_id, 'create', ts + cooldown),
            )
            return {"claimed": True, "item": item}


    async def set_cooldown(self, user_id: int, key: str, seconds: int) -> bool:
        ts = now_ts()
        async with self.tx() as db:
            row = await (await db.execute("SELECT expires_at FROM cooldowns WHERE user_id=? AND key=?", (user_id, key))).fetchone()
            if row and row[0] > ts:
                return False
            await db.execute("INSERT INTO cooldowns(user_id,key,expires_at) VALUES(?,?,?) ON CONFLICT(user_id,key) DO UPDATE SET expires_at=excluded.expires_at", (user_id, key, ts + seconds))
            return True

    async def charge_game(self, user_id: int, bet: int, key: str, cooldown: int = 1, tx_id: str | None = None) -> Account:
        """Compatibility + public API for game debits.

        Older main.py builds called ``charge_game`` while the optimized
        repository used ``game_bet_atomic``. Keeping this method here makes
        the two layers contract-safe and preserves atomic debit/cooldown.
        """
        if bet <= 0 or bet > 10**9:
            raise ValueError("Bet must be between 1 and 1,000,000,000 coins.")
        ts = now_ts()
        tx_id = tx_id or f"game:bet:{key}:{user_id}:{ts}:{secrets.token_hex(8)}"
        async with self.tx() as db:
            row = await (await db.execute("SELECT * FROM users WHERE user_id=?", (user_id,))).fetchone()
            if not row:
                raise ValueError("account not found")
            cd = await (await db.execute("SELECT expires_at FROM cooldowns WHERE user_id=? AND key=?", (user_id, key))).fetchone()
            if cd and cd[0] > ts:
                raise ValueError(f"cooldown:{cd[0] - ts}")
            if row["wallet"] < bet:
                raise ValueError("insufficient funds")
            await db.execute(
                "UPDATE users SET wallet=wallet-?, total_spent=total_spent+?, last_active=? WHERE user_id=?",
                (bet, bet, ts, user_id),
            )
            await db.execute(
                "INSERT INTO transactions(tx_id,user_id,kind,amount,wallet_delta,bank_delta,meta_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (tx_id, user_id, f"{key} Bet", -bet, -bet, 0, json.dumps({"bet": bet, "game": key}, separators=(",", ":")), ts),
            )
            await db.execute(
                "INSERT INTO cooldowns(user_id,key,expires_at) VALUES(?,?,?) ON CONFLICT(user_id,key) DO UPDATE SET expires_at=excluded.expires_at",
                (user_id, key, ts + cooldown),
            )
            round_id = f"round:{tx_id}"
            await db.execute(
                "INSERT INTO game_rounds(round_id,user_id,game_key,bet,status,payout,created_at) VALUES(?,?,?,?,?,?,?)",
                (round_id, user_id, key, bet, 'pending', 0, ts),
            )
            updated = await (await db.execute("SELECT * FROM users WHERE user_id=?", (user_id,))).fetchone()
            return Account(**{k: updated[k] for k in Account.__annotations__})

    async def settle_game(
        self,
        user_id: int,
        game_key: str,
        payout: int,
        label: str,
        tx_id: str | None = None,
    ) -> Account:
        """Atomically settle the pending game round; safe against crashes and duplicates."""
        if payout < 0 or payout > 10**12:
            raise ValueError("invalid game payout")
        ts = now_ts()
        async with self.tx() as db:
            round_row = await (await db.execute(
                """SELECT * FROM game_rounds
                   WHERE user_id=? AND game_key=? AND status='pending'
                   ORDER BY created_at DESC LIMIT 1""",
                (user_id, game_key),
            )).fetchone()
            if not round_row:
                row = await (await db.execute("SELECT * FROM users WHERE user_id=?", (user_id,))).fetchone()
                if not row:
                    raise ValueError("account not found")
                return Account(**{k: row[k] for k in Account.__annotations__})

            bet = int(round_row["bet"])
            if payout > 10**12:
                raise ValueError("invalid payout")

            if payout:
                await db.execute(
                    """UPDATE users
                       SET wallet=wallet+?,
                           total_earned=total_earned+?,
                           weekly_earned=weekly_earned+?,
                           last_active=?
                       WHERE user_id=?""",
                    (payout, payout, payout, ts, user_id),
                )
            else:
                await db.execute("UPDATE users SET last_active=? WHERE user_id=?", (ts, user_id))

            await db.execute(
                """UPDATE game_rounds
                   SET status='settled', payout=?, settled_at=?
                   WHERE round_id=? AND status='pending'""",
                (payout, ts, round_row["round_id"]),
            )

            result = "win" if payout > bet else "loss" if payout == 0 else "cashout"
            stat_keys = [
                f"stat:games_played:{user_id}",
                f"stat:game_{result}:{user_id}",
                f"stat:games_played:{game_key}:{user_id}",
                f"stat:game_{result}:{game_key}:{user_id}",
            ]
            for stat_key in stat_keys:
                await db.execute(
                    """INSERT INTO settings(key,value) VALUES(?,?)
                       ON CONFLICT(key) DO UPDATE SET value=CAST(CAST(value AS INTEGER)+1 AS TEXT)""",
                    (stat_key, "1"),
                )

            settlement_tx = tx_id or f"game:settle:{round_row['round_id']}"
            await db.execute(
                """INSERT OR IGNORE INTO transactions(
                       tx_id,user_id,kind,amount,wallet_delta,bank_delta,meta_json,created_at
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    settlement_tx,
                    user_id,
                    label,
                    payout,
                    payout,
                    0,
                    json.dumps({
                        "game": game_key,
                        "bet": bet,
                        "payout": payout,
                        "round_id": round_row["round_id"],
                        "result": result,
                    }, separators=(",", ":")),
                    ts,
                ),
            )
            row = await (await db.execute("SELECT * FROM users WHERE user_id=?", (user_id,))).fetchone()
            return Account(**{k: row[k] for k in Account.__annotations__})

    async def recover_pending_games(self) -> int:
        """Refund unfinished rounds after a process restart/crash."""
        ts = now_ts()
        async with self.tx() as db:
            rows = await (await db.execute(
                "SELECT * FROM game_rounds WHERE status='pending' ORDER BY created_at"
            )).fetchall()
            recovered = 0
            for row in rows:
                bet = int(row["bet"])
                uid = int(row["user_id"])
                await db.execute(
                    """UPDATE users
                       SET wallet=wallet+?,
                           total_spent=MAX(0,total_spent-?),
                           last_active=?
                       WHERE user_id=?""",
                    (bet, bet, ts, uid),
                )
                refund_tx = f"game:recovery:{row['round_id']}"
                await db.execute(
                    """INSERT OR IGNORE INTO transactions(
                           tx_id,user_id,kind,amount,wallet_delta,bank_delta,meta_json,created_at
                       ) VALUES(?,?,?,?,?,?,?,?)""",
                    (
                        refund_tx, uid, "Game Recovery Refund", bet, bet, 0,
                        json.dumps({
                            "game": row["game_key"],
                            "bet": bet,
                            "round_id": row["round_id"],
                            "reason": "unfinished_round_after_restart",
                        }, separators=(",", ":")),
                        ts,
                    ),
                )
                await db.execute(
                    "UPDATE game_rounds SET status='recovered',payout=0,settled_at=? WHERE round_id=? AND status='pending'",
                    (ts, row["round_id"]),
                )
                recovered += 1
            return recovered

    async def ensure_admin(self, user_id: int, granted_by: int) -> None:
        async with self.tx() as db:
            await db.execute(
                "INSERT INTO froxx_admins(user_id,granted_by,granted_at) VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET granted_by=excluded.granted_by,granted_at=excluded.granted_at",
                (user_id, granted_by, now_ts()),
            )

    async def remove_admin(self, user_id: int) -> None:
        async with self.tx() as db:
            await db.execute("DELETE FROM froxx_admins WHERE user_id=?", (user_id,))

    async def list_admins(self) -> list[int]:
        async with aiosqlite.connect(self.path) as db:
            rows = await (await db.execute("SELECT user_id FROM froxx_admins ORDER BY granted_at")).fetchall()
            return [int(r[0]) for r in rows]

    async def list_admins_detailed(self) -> list[aiosqlite.Row]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            return await (await db.execute("SELECT * FROM froxx_admins ORDER BY granted_at")).fetchall()

    async def game_stats(self, user_id: int) -> dict[str, int]:
        async with aiosqlite.connect(self.path) as db:
            async def get(key: str) -> int:
                row = await (await db.execute("SELECT value FROM settings WHERE key=?", (f"stat:{key}:{user_id}",))).fetchone()
                return int(row[0]) if row else 0
            return {
                "played": await get("games_played"),
                "wins": await get("game_win"),
                "losses": await get("game_loss"),
                "cashouts": await get("game_cashout"),
            }

    async def game_bet_atomic(self, user_id: int, key: str, bet: int, cooldown: int) -> Account:
        """Atomically validate cooldown + debit a game bet.

        This prevents a failed/insufficient bet from consuming the cooldown and
        closes the small race window between a separate cooldown and debit.
        """
        if bet <= 0 or bet > 10**9:
            raise ValueError("Bet must be between 1 and 1,000,000,000 coins.")
        ts = now_ts()
        tx_id = f"game:bet:{key}:{user_id}:{ts}:{asyncio.get_running_loop().time():.9f}"
        async with self.tx() as db:
            row = await (await db.execute("SELECT * FROM users WHERE user_id=?", (user_id,))).fetchone()
            if not row:
                raise ValueError("account not found")
            cd = await (await db.execute("SELECT expires_at FROM cooldowns WHERE user_id=? AND key=?", (user_id, key))).fetchone()
            if cd and cd[0] > ts:
                raise ValueError(f"GAME_COOLDOWN:{cd[0] - ts}")
            if row["wallet"] < bet:
                raise ValueError("Insufficient FROXX coins for that bet.")
            await db.execute(
                "UPDATE users SET wallet=wallet-?, total_spent=total_spent+?, last_active=? WHERE user_id=?",
                (bet, bet, ts, user_id),
            )
            await db.execute(
                "INSERT INTO transactions(tx_id,user_id,kind,amount,wallet_delta,bank_delta,meta_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (tx_id, user_id, f"{key} Bet", -bet, -bet, 0, json.dumps({"bet": bet}, separators=(",", ":")), ts),
            )
            await db.execute(
                "INSERT INTO cooldowns(user_id,key,expires_at) VALUES(?,?,?) ON CONFLICT(user_id,key) DO UPDATE SET expires_at=excluded.expires_at",
                (user_id, key, ts + cooldown),
            )
            updated = await (await db.execute("SELECT * FROM users WHERE user_id=?", (user_id,))).fetchone()
            return Account(**{k: updated[k] for k in Account.__annotations__})

    async def economy_tx(
        self,
        user_id: int,
        kind: str,
        wallet_delta: int = 0,
        bank_delta: int = 0,
        meta: dict[str, Any] | None = None,
        tx_id: str | None = None,
        xp_delta: int = 0,
        work_delta: int = 0,
    ) -> Account:
        tx_id = tx_id or f"{user_id}:{now_ts()}:{asyncio.get_running_loop().time()}"
        if abs(wallet_delta) > 10**12 or abs(bank_delta) > 10**12 or xp_delta < -10**9 or xp_delta > 10**9:
            raise ValueError("invalid amount")
        async with self.tx() as db:
            exists = await (await db.execute("SELECT 1 FROM transactions WHERE tx_id=?", (tx_id,))).fetchone()
            if exists:
                row = await (await db.execute("SELECT * FROM users WHERE user_id=?", (user_id,))).fetchone()
                return Account(**{k: row[k] for k in Account.__annotations__})
            row = await (await db.execute("SELECT * FROM users WHERE user_id=?", (user_id,))).fetchone()
            if not row:
                raise ValueError("account not found")
            nw, nb = row["wallet"] + wallet_delta, row["bank"] + bank_delta
            if nw < 0 or nb < 0:
                raise ValueError("insufficient funds")
            earned = max(0, wallet_delta + bank_delta)
            spent = max(0, -(wallet_delta + bank_delta))
            current_ts = now_ts()
            anchor = int(row["weekly_anchor"] or current_ts)
            weekly = row["weekly_earned"] + earned if current_ts - anchor < 604800 else earned
            new_anchor = anchor if current_ts - anchor < 604800 else current_ts
            await db.execute(
                "UPDATE users SET wallet=?,bank=?,xp=max(0,xp+?),total_earned=total_earned+?,total_spent=total_spent+?,weekly_earned=?,weekly_anchor=?,last_active=? WHERE user_id=?",
                (nw, nb, xp_delta, earned, spent, weekly, new_anchor, current_ts, user_id),
            )
            await db.execute(
                "INSERT INTO transactions(tx_id,user_id,kind,amount,wallet_delta,bank_delta,meta_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (tx_id, user_id, kind, wallet_delta + bank_delta, wallet_delta, bank_delta, json.dumps(meta or {}, separators=(",", ":")), now_ts()),
            )
            if work_delta:
                await db.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=CAST(CAST(value AS INTEGER)+? AS TEXT)", (f"stat:work:{user_id}", str(work_delta), work_delta))
            row = await (await db.execute("SELECT * FROM users WHERE user_id=?", (user_id,))).fetchone()
            return Account(**{k: row[k] for k in Account.__annotations__})

    async def consume_booster(self, user_id: int, item_id: str, quantity: int, tx_id: str) -> tuple[dict, int, int]:
        """Atomically consume boosters and grant their immediate virtual effect."""
        if quantity < 1 or quantity > 25:
            raise ValueError("Quantity must be between 1 and 25.")
        async with self.tx() as db:
            item = await (await db.execute("SELECT * FROM items WHERE item_id=? AND active=1", (item_id,))).fetchone()
            if not item or item["item_type"] != "booster":
                raise ValueError("That item is not a usable booster.")
            inv = await (await db.execute("SELECT quantity FROM inventory WHERE user_id=? AND item_id=?", (user_id, item_id))).fetchone()
            if not inv or inv[0] < quantity:
                raise ValueError("You do not own enough of that item.")
            effect = json.loads(item["effect_json"] or "{}")
            coins = int(effect.get("use_coins", 0)) * quantity
            xp = int(effect.get("use_xp", 0)) * quantity
            new_qty = inv[0] - quantity
            if new_qty:
                await db.execute("UPDATE inventory SET quantity=? WHERE user_id=? AND item_id=?", (new_qty, user_id, item_id))
            else:
                await db.execute("DELETE FROM inventory WHERE user_id=? AND item_id=?", (user_id, item_id))
            if coins or xp:
                await db.execute("UPDATE users SET wallet=wallet+?, xp=xp+?, total_earned=total_earned+?, last_active=? WHERE user_id=?", (coins, xp, coins, now_ts(), user_id))
                await db.execute("INSERT INTO transactions(tx_id,user_id,kind,amount,wallet_delta,meta_json,created_at) VALUES(?,?,?,?,?,?,?)", (tx_id,user_id,"Booster Use",coins,coins,json.dumps({"item_id":item_id,"quantity":quantity,"xp":xp},separators=(",",":")),now_ts()))
            return dict(item), coins, xp

    async def consume_item_reward(self, user_id: int, item_id: str, quantity: int, coins: int, xp: int, tx_id: str) -> None:
        """Atomically consume a usable item and apply its virtual reward."""
        if quantity < 1 or quantity > 25 or coins < 0 or xp < 0:
            raise ValueError("invalid item use")
        async with self.tx() as db:
            item = await (await db.execute("SELECT item_type FROM items WHERE item_id=? AND active=1", (item_id,))).fetchone()
            inv = await (await db.execute("SELECT quantity FROM inventory WHERE user_id=? AND item_id=?", (user_id, item_id))).fetchone()
            if not item or not inv or inv[0] < quantity:
                raise ValueError("not enough items")
            if item[0] not in {"food", "utility", "booster"}:
                raise ValueError("item is not usable")
            if await (await db.execute("SELECT 1 FROM transactions WHERE tx_id=?", (tx_id,))).fetchone():
                return
            left = inv[0] - quantity
            if left:
                await db.execute("UPDATE inventory SET quantity=? WHERE user_id=? AND item_id=?", (left, user_id, item_id))
            else:
                await db.execute("DELETE FROM inventory WHERE user_id=? AND item_id=?", (user_id, item_id))
            ts = now_ts()
            await db.execute("UPDATE users SET wallet=wallet+?, xp=xp+?, total_earned=total_earned+?, last_active=? WHERE user_id=?", (coins, xp, coins, ts, user_id))
            await db.execute("INSERT INTO transactions(tx_id,user_id,kind,amount,wallet_delta,meta_json,created_at) VALUES(?,?,?,?,?,?,?)", (tx_id,user_id,"Item Use",coins,coins,json.dumps({"item_id":item_id,"quantity":quantity,"xp":xp},separators=(",",":")),ts))

    async def transfer(self, sender: int, receiver: int, amount: int, tx_id: str) -> tuple[Account, Account]:
        if sender == receiver or amount <= 0:
            raise ValueError("invalid transfer")
        async with self.tx() as db:
            if await (await db.execute("SELECT 1 FROM transactions WHERE tx_id=?", (tx_id,))).fetchone():
                a = await (await db.execute("SELECT * FROM users WHERE user_id=?", (sender,))).fetchone()
                b = await (await db.execute("SELECT * FROM users WHERE user_id=?", (receiver,))).fetchone()
                if not a or not b:
                    raise ValueError("account missing")
                return Account(**{k: a[k] for k in Account.__annotations__}), Account(**{k: b[k] for k in Account.__annotations__})
            a = await (await db.execute("SELECT * FROM users WHERE user_id=?", (sender,))).fetchone()
            b = await (await db.execute("SELECT * FROM users WHERE user_id=?", (receiver,))).fetchone()
            if not a or not b:
                raise ValueError("account missing")
            if a["wallet"] < amount:
                raise ValueError("insufficient funds")
            ts = now_ts()
            await db.execute("UPDATE users SET wallet=wallet-?,total_spent=total_spent+?,last_active=? WHERE user_id=?", (amount, amount, ts, sender))
            await db.execute("UPDATE users SET wallet=wallet+?,total_earned=total_earned+?,last_active=?,weekly_earned=weekly_earned+? WHERE user_id=?", (amount, amount, ts, amount, receiver))
            await db.execute("INSERT INTO transactions(tx_id,user_id,kind,amount,wallet_delta,meta_json,created_at) VALUES(?,?,?,?,?,?,?)", (tx_id, sender, "Transfer Sent", -amount, -amount, json.dumps({"to": receiver}), ts))
            await db.execute("INSERT INTO transactions(tx_id,user_id,kind,amount,wallet_delta,meta_json,created_at) VALUES(?,?,?,?,?,?,?)", (f"{tx_id}:recv", receiver, "Transfer Received", amount, amount, json.dumps({"from": sender}), ts))
            ra = await (await db.execute("SELECT * FROM users WHERE user_id=?", (sender,))).fetchone()
            rb = await (await db.execute("SELECT * FROM users WHERE user_id=?", (receiver,))).fetchone()
            return Account(**{k: ra[k] for k in Account.__annotations__}), Account(**{k: rb[k] for k in Account.__annotations__})

    async def add_item(self, user_id: int, item_id: str, qty: int, kind: str = "Item Reward") -> None:
        if qty <= 0 or qty > 1_000_000:
            raise ValueError("invalid quantity")
        async with self.tx() as db:
            if not await (await db.execute("SELECT 1 FROM users WHERE user_id=?", (user_id,))).fetchone():
                raise ValueError("account missing")
            if not await (await db.execute("SELECT 1 FROM items WHERE item_id=?", (item_id,))).fetchone():
                raise ValueError("item missing")
            await db.execute("INSERT INTO inventory(user_id,item_id,quantity) VALUES(?,?,?) ON CONFLICT(user_id,item_id) DO UPDATE SET quantity=quantity+excluded.quantity", (user_id, item_id, qty))
            await db.execute("UPDATE users SET last_active=? WHERE user_id=?", (now_ts(), user_id))

    async def remove_item(self, user_id: int, item_id: str, qty: int) -> None:
        if qty <= 0:
            raise ValueError("invalid quantity")
        async with self.tx() as db:
            row = await (await db.execute("SELECT quantity FROM inventory WHERE user_id=? AND item_id=?", (user_id, item_id))).fetchone()
            if not row or row[0] < qty:
                raise ValueError("not enough items")
            if row[0] == qty:
                await db.execute("DELETE FROM inventory WHERE user_id=? AND item_id=?", (user_id, item_id))
            else:
                await db.execute("UPDATE inventory SET quantity=quantity-? WHERE user_id=? AND item_id=?", (qty, user_id, item_id))

    async def buy_item(self, user_id: int, item_id: str, qty: int, tx_id: str) -> tuple[Account, aiosqlite.Row]:
        if qty <= 0 or qty > 1000:
            raise ValueError("invalid quantity")
        async with self.tx() as db:
            item = await (await db.execute("SELECT * FROM items WHERE item_id=? AND active=1", (item_id,))).fetchone()
            user = await (await db.execute("SELECT * FROM users WHERE user_id=?", (user_id,))).fetchone()
            if not item or not user:
                raise ValueError("item/account missing")
            if item["buy_price"] <= 0:
                raise ValueError("item is not purchasable")
            cost = item["buy_price"] * qty
            if user["wallet"] < cost:
                raise ValueError("insufficient funds")
            if await (await db.execute("SELECT 1 FROM transactions WHERE tx_id=?", (tx_id,))).fetchone():
                return Account(**{k: user[k] for k in Account.__annotations__}), item
            ts = now_ts()
            await db.execute("UPDATE users SET wallet=wallet-?,total_spent=total_spent+?,last_active=? WHERE user_id=?", (cost, cost, ts, user_id))
            await db.execute("INSERT INTO inventory(user_id,item_id,quantity) VALUES(?,?,?) ON CONFLICT(user_id,item_id) DO UPDATE SET quantity=quantity+excluded.quantity", (user_id, item_id, qty))
            await db.execute("INSERT INTO transactions(tx_id,user_id,kind,amount,wallet_delta,meta_json,created_at) VALUES(?,?,?,?,?,?,?)", (tx_id, user_id, "Shop Purchase", -cost, -cost, json.dumps({"item": item_id, "qty": qty}), ts))
            row = await (await db.execute("SELECT * FROM users WHERE user_id=?", (user_id,))).fetchone()
            return Account(**{k: row[k] for k in Account.__annotations__}), item

    async def sell_item(self, user_id: int, item_id: str, qty: int, tx_id: str) -> tuple[Account, aiosqlite.Row]:
        if qty <= 0 or qty > 1000:
            raise ValueError("invalid quantity")
        async with self.tx() as db:
            item = await (await db.execute("SELECT * FROM items WHERE item_id=? AND active=1", (item_id,))).fetchone()
            inv = await (await db.execute("SELECT quantity FROM inventory WHERE user_id=? AND item_id=?", (user_id, item_id))).fetchone()
            user = await (await db.execute("SELECT * FROM users WHERE user_id=?", (user_id,))).fetchone()
            if not item or not inv or not user or item["sell_price"] <= 0 or inv[0] < qty:
                raise ValueError("cannot sell")
            if await (await db.execute("SELECT 1 FROM transactions WHERE tx_id=?", (tx_id,))).fetchone():
                return Account(**{k: user[k] for k in Account.__annotations__}), item
            revenue = item["sell_price"] * qty
            ts = now_ts()
            if inv[0] == qty:
                await db.execute("DELETE FROM inventory WHERE user_id=? AND item_id=?", (user_id, item_id))
            else:
                await db.execute("UPDATE inventory SET quantity=quantity-? WHERE user_id=? AND item_id=?", (qty, user_id, item_id))
            await db.execute("UPDATE users SET wallet=wallet+?,total_earned=total_earned+?,last_active=? WHERE user_id=?", (revenue, revenue, ts, user_id))
            await db.execute("INSERT INTO transactions(tx_id,user_id,kind,amount,wallet_delta,meta_json,created_at) VALUES(?,?,?,?,?,?,?)", (tx_id, user_id, "Item Sale", revenue, revenue, json.dumps({"item": item_id, "qty": qty}), ts))
            row = await (await db.execute("SELECT * FROM users WHERE user_id=?", (user_id,))).fetchone()
            return Account(**{k: row[k] for k in Account.__annotations__}), item

    async def daily_claim(self, user_id: int, coins: int, xp: int, new_streak: int, cycle: int, tx_id: str) -> Account:
        """Atomically claim the daily reward and its 24h cooldown."""
        if coins < 0 or xp < 0:
            raise ValueError("invalid daily reward")
        async with self.tx() as db:
            u = await (await db.execute(
                "SELECT * FROM users WHERE user_id=?", (user_id,)
            )).fetchone()
            if not u:
                raise ValueError("account missing")

            ts = now_ts()
            if u["last_daily"] is not None and ts - u["last_daily"] < 86400:
                raise ValueError("daily already claimed")

            await db.execute(
                """UPDATE users
                   SET wallet=wallet+?,
                       xp=xp+?,
                       total_earned=total_earned+?,
                       daily_streak=?,
                       last_daily=?,
                       daily_cycle=?,
                       last_active=?,
                       weekly_earned=weekly_earned+?
                   WHERE user_id=?""",
                (coins, xp, coins, new_streak, ts, cycle, ts, coins, user_id),
            )
            await db.execute(
                """INSERT INTO transactions(
                       tx_id,user_id,kind,amount,wallet_delta,meta_json,created_at
                   ) VALUES(?,?,?,?,?,?,?)""",
                (
                    tx_id, user_id, "Daily Reward", coins, coins,
                    json.dumps({"streak": new_streak, "cycle": cycle},
                               separators=(",", ":")),
                    ts,
                ),
            )
            await db.execute(
                """INSERT INTO cooldowns(user_id,key,expires_at)
                   VALUES(?,?,?)
                   ON CONFLICT(user_id,key)
                   DO UPDATE SET expires_at=excluded.expires_at""",
                (user_id, "free_daily", ts + 86400),
            )
            row = await (await db.execute(
                "SELECT * FROM users WHERE user_id=?", (user_id,)
            )).fetchone()
            return Account(**{k: row[k] for k in Account.__annotations__})

    async def set_level(self, user_id: int, xp: int, level: int) -> None:
        async with self.tx() as db:
            await db.execute("UPDATE users SET xp=?,level=? WHERE user_id=?", (xp, level, user_id))

    async def transactions(self, user_id: int, limit: int = 50) -> list[aiosqlite.Row]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            return await (await db.execute("SELECT * FROM transactions WHERE user_id=? ORDER BY created_at DESC LIMIT ?", (user_id, min(limit, 100)))).fetchall()

    async def leaderboard(self, metric: str, limit: int = 10) -> list[aiosqlite.Row]:
        cols = {"wallet": "wallet", "level": "level", "xp": "xp", "collectibles": "(SELECT COUNT(*) FROM inventory i JOIN items it ON it.item_id=i.item_id WHERE i.user_id=users.user_id AND it.item_type='collectible')", "weekly": "weekly_earned"}
        col = cols.get(metric, "wallet")
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            return await (await db.execute(f"SELECT *, ROW_NUMBER() OVER(ORDER BY {col} DESC,user_id) AS rank FROM users ORDER BY {col} DESC,user_id LIMIT ?", (limit,))).fetchall()

    async def rank_of(self, user_id: int, metric: str = "wallet") -> int:
        cols = {"wallet": "wallet", "level": "level", "xp": "xp", "weekly": "weekly_earned"}
        col = cols.get(metric, "wallet")
        async with aiosqlite.connect(self.path) as db:
            row = await (await db.execute(f"SELECT 1 + (SELECT COUNT(*) FROM users b WHERE b.{col} > a.{col}) FROM users a WHERE a.user_id=?", (user_id,))).fetchone()
            return row[0] if row else 0

    async def achievement_rows(self) -> list[aiosqlite.Row]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            return await (await db.execute("SELECT * FROM achievements ORDER BY requirement")).fetchall()

    async def unlocked(self, user_id: int) -> set[str]:
        async with aiosqlite.connect(self.path) as db:
            rows = await (await db.execute("SELECT achievement_id FROM user_achievements WHERE user_id=?", (user_id,))).fetchall()
            return {r[0] for r in rows}

    async def unlock(self, user_id: int, achievement_id: str) -> bool:
        async with self.tx() as db:
            cur = await db.execute("INSERT OR IGNORE INTO user_achievements(user_id,achievement_id,unlocked_at) VALUES(?,?,?)", (user_id, achievement_id, now_ts()))
            return cur.rowcount > 0

    async def stat(self, user_id: int, key: str) -> int:
        async with aiosqlite.connect(self.path) as db:
            row = await (await db.execute("SELECT value FROM settings WHERE key=?", (f"stat:{key}:{user_id}",))).fetchone()
            return int(row[0]) if row else 0

    async def set_setting(self, key: str, value: str) -> None:
        async with self.tx() as db:
            await db.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    async def get_setting(self, key: str, default: str | None = None) -> str | None:
        async with aiosqlite.connect(self.path) as db:
            row = await (await db.execute("SELECT value FROM settings WHERE key=?", (key,))).fetchone()
            return row[0] if row else default

    async def reset_user(self, user_id: int) -> None:
        async with self.tx() as db:
            await db.execute("DELETE FROM users WHERE user_id=?", (user_id,))
            await db.execute("DELETE FROM cooldowns WHERE user_id=?", (user_id,))
            await db.execute("DELETE FROM settings WHERE key LIKE ?", (f"stat:%:{user_id}",))

    async def stats_snapshot(self, user_id: int) -> dict[str, int]:
        a = await self.get_account(user_id)
        if not a:
            raise ValueError("account missing")
        inv = await self.get_inventory(user_id, "collectible")
        return {"total_earned": a.total_earned, "total_spent": a.total_spent, "bank": a.bank, "net_worth": a.wallet + a.bank, "daily_streak": a.daily_streak, "unique_collectibles": len(inv), "work_count": await self.stat(user_id, "work")}

    async def transfer_items_atomic(self, a: int, b: int, offers_a: dict[str, int], offers_b: dict[str, int], coin_a: int, coin_b: int, trade_id: str) -> None:
        if a == b or any(v <= 0 or v > 1000 for v in [*offers_a.values(), *offers_b.values()]):
            raise ValueError("invalid trade")
        if coin_a < 0 or coin_b < 0:
            raise ValueError("invalid coin offer")
        async with self.tx() as db:
            t = await (await db.execute("SELECT state FROM trade_sessions WHERE trade_id=?", (trade_id,))).fetchone()
            if t and t[0] == "completed":
                return
            ua = await (await db.execute("SELECT wallet FROM users WHERE user_id=?", (a,))).fetchone()
            ub = await (await db.execute("SELECT wallet FROM users WHERE user_id=?", (b,))).fetchone()
            if not ua or not ub or ua[0] < coin_a or ub[0] < coin_b:
                raise ValueError("insufficient trade coins")
            for uid, offers in ((a, offers_a), (b, offers_b)):
                for iid, qty in offers.items():
                    row = await (await db.execute("SELECT quantity FROM inventory WHERE user_id=? AND item_id=?", (uid, iid))).fetchone()
                    if not row or row[0] < qty:
                        raise ValueError("insufficient trade item")
            ts = now_ts()
            await db.execute("UPDATE users SET wallet=wallet-?+?,total_spent=total_spent+?,total_earned=total_earned+?,last_active=? WHERE user_id=?", (coin_a, coin_b, coin_a, coin_b, ts, a))
            await db.execute("UPDATE users SET wallet=wallet-?+?,total_spent=total_spent+?,total_earned=total_earned+?,last_active=? WHERE user_id=?", (coin_b, coin_a, coin_b, coin_a, ts, b))
            for owner, receiver, offers in ((a, b, offers_a), (b, a, offers_b)):
                for iid, qty in offers.items():
                    row = await (await db.execute("SELECT quantity FROM inventory WHERE user_id=? AND item_id=?", (owner, iid))).fetchone()
                    if row[0] == qty:
                        await db.execute("DELETE FROM inventory WHERE user_id=? AND item_id=?", (owner, iid))
                    else:
                        await db.execute("UPDATE inventory SET quantity=quantity-? WHERE user_id=? AND item_id=?", (qty, owner, iid))
                    await db.execute("INSERT INTO inventory(user_id,item_id,quantity) VALUES(?,?,?) ON CONFLICT(user_id,item_id) DO UPDATE SET quantity=quantity+excluded.quantity", (receiver, iid, qty))
            await db.execute("UPDATE trade_sessions SET state='completed' WHERE trade_id=?", (trade_id,))
            for uid in (a, b):
                await db.execute("INSERT INTO transactions(tx_id,user_id,kind,amount,meta_json,created_at) VALUES(?,?,?,?,?,?)", (f"trade:{trade_id}:{uid}", uid, "Trade Completed", 0, json.dumps({"trade_id": trade_id}), ts))

