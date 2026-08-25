from __future__ import annotations

import asyncio
import logging
import os
import secrets
from typing import Optional
from datetime import datetime, timezone
from random import SystemRandom

import discord
from discord.ext import commands

from database import Database

def _load_local_env() -> None:
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
    if not os.path.isfile(env_path):
        return
    try:
        with open(env_path, 'r', encoding='utf-8') as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                key, value = line.split('=', 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
    except OSError:
        pass

_load_local_env()
TOKEN = os.getenv('DISCORD_TOKEN', '').strip()
def _safe_int_env(name: str, default: int = 0) -> int:
    try:
        return int(os.getenv(name, str(default)) or default)
    except (TypeError, ValueError):
        return default
OWNER_ID = _safe_int_env('OWNER_ID', 0)
DATABASE_PATH = os.getenv('DATABASE_PATH', 'froxx.sqlite3').strip() or 'froxx.sqlite3'
if not os.path.isabs(DATABASE_PATH):
    DATABASE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), DATABASE_PATH)
os.makedirs(os.path.dirname(os.path.abspath(DATABASE_PATH)), exist_ok=True)
LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO').upper()
PREFIX = 'fx '
COIN = '🪙'
XP = '⭐'
rng = SystemRandom()

logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO), format='%(asctime)s | %(levelname)s | %(name)s | %(message)s')
log = logging.getLogger('froxx')

FREE_COOLDOWN = 7200
DAILY_COOLDOWN = 86400
GAME_COOLDOWN = 1
ANIM_STEP = 0.50
MAX_BET = 1_000_000_000
STARTER = 10_000
CREATE_COOLDOWN = 25
CREATE_LOOT = [
    ('iron_scrap', 34), ('copper_wire', 24), ('moon_shard', 15),
    ('emerald_chip', 9), ('ruby_core', 6), ('void_fragment', 4),
    ('star_relic', 2), ('ancient_core', 1),
]


def fmt(n: int) -> str:
    return f'{n:,}'


def xp_needed(level: int) -> int:
    return int(100 + 65 * (level - 1) + 18 * (level - 1) ** 2)


def emb(title: str, desc: str = '', color: int = 0x5865F2) -> discord.Embed:
    e = discord.Embed(title=title, description=desc, color=color, timestamp=discord.utils.utcnow())
    e.set_footer(text='FROXX • Virtual economy only • No real-money value')
    return e


def is_admin(ctx: commands.Context) -> bool:
    # FROXX admin access is independent from Discord's Administrator permission.
    # The configured owner is always an admin; additional admins are persistent.
    return bool(ctx.author.id == OWNER_ID or ctx.author.id in getattr(bot, 'admin_ids', set()))


# ---------------------------------------------------------------------------
# Discord outbound protection
# ---------------------------------------------------------------------------
# Discord.py already understands Discord's rate-limit buckets, but FROXX also
# generates a lot of message edits during games.  We deliberately keep a small
# per-channel spacing between our own outbound message operations so bursts from
# multiple players do not hammer the same channel/API bucket.
OUTBOUND_MIN_INTERVAL = 0.65
OUTBOUND_RETRIES = 1


async def _channel_slot(channel_id: int | None) -> None:
    if channel_id is None:
        return

    loop = asyncio.get_running_loop()
    async with bot._outbound_lock:
        now = loop.time()
        target = max(now, bot._next_outbound.get(channel_id, now))
        bot._next_outbound[channel_id] = target + OUTBOUND_MIN_INTERVAL

    delay = target - now
    if delay > 0:
        await asyncio.sleep(delay)


async def safe_send(ctx: commands.Context, *args, **kwargs):
    """Send a message without turning a 429 into an error-handler loop."""
    channel_id = getattr(getattr(ctx, 'channel', None), 'id', None)

    for attempt in range(OUTBOUND_RETRIES + 1):
        await _channel_slot(channel_id)
        try:
            return await ctx.send(*args, **kwargs)
        except discord.HTTPException as exc:
            status = getattr(exc, 'status', None)
            retry_after = getattr(exc, 'retry_after', None)

            if status == 429 and attempt < OUTBOUND_RETRIES:
                delay = float(retry_after or 1.0)
                delay = min(max(delay, 0.5), 10.0)
                log.warning(
                    "Discord 429 while sending | channel=%s retry_after=%.2fs attempt=%d",
                    channel_id, delay, attempt + 1
                )
                await asyncio.sleep(delay)
                continue

            log.warning(
                "Discord send failed | status=%s channel=%s attempt=%d | %s",
                status, channel_id, attempt + 1, exc
            )
            return None


async def safe_edit(message: discord.Message, *args, **kwargs):
    """Edit an animation message safely; a failed edit must never crash gameplay."""
    channel_id = getattr(getattr(message, 'channel', None), 'id', None)

    for attempt in range(OUTBOUND_RETRIES + 1):
        await _channel_slot(channel_id)
        try:
            return await message.edit(*args, **kwargs)
        except discord.HTTPException as exc:
            status = getattr(exc, 'status', None)
            retry_after = getattr(exc, 'retry_after', None)

            if status == 429 and attempt < OUTBOUND_RETRIES:
                delay = float(retry_after or 1.0)
                delay = min(max(delay, 0.5), 10.0)
                log.warning(
                    "Discord 429 while editing | channel=%s retry_after=%.2fs attempt=%d",
                    channel_id, delay, attempt + 1
                )
                await asyncio.sleep(delay)
                continue

            log.warning(
                "Discord edit failed | status=%s channel=%s attempt=%d | %s",
                status, channel_id, attempt + 1, exc
            )
            return None


class VisualReel(discord.ui.View):
    """Public disabled game-reel buttons. Highlighting is visual only; outcomes are fixed before the reveal."""
    def __init__(self, icons: list[str], highlight: int | set[int] | None = None, highlight_style: discord.ButtonStyle = discord.ButtonStyle.success):
        super().__init__(timeout=None)
        highlights = {highlight} if isinstance(highlight, int) else (highlight or set())
        for i, icon in enumerate(icons[:20]):
            style = highlight_style if i in highlights else discord.ButtonStyle.secondary
            self.add_item(discord.ui.Button(
                label=icon[:80],
                style=style,
                disabled=True,
                row=i // 5,
            ))


async def animated_result(
    ctx: commands.Context,
    title: str,
    frames: list[str],
    final: str,
    color: int,
    icon_frames: list[list[str]] | None = None,
    footer: str = "FROXX • Virtual economy only • No real-money value",
    final_highlight: int | set[int] | None = None,
):
    """Public staged reveal.

    The outcome is settled before the reveal.  Under normal load the reveal
    keeps the same ~2-second feel, but uses only three message operations
    (send -> mid reveal -> final) instead of a burst of five.
    If another animation is already running in the same channel, the result is
    posted immediately so a busy channel cannot build an API request backlog.
    """
    icon_frames = icon_frames or [
        ["🔴", "🔵", "🟢", "🟡", "🟣"],
        ["🟣", "🟡", "🔴", "🟢", "🔵"],
        ["🔵", "🔴", "🟡", "🟣", "🟢"],
        ["🟢", "🟣", "🔵", "🟡", "🔴"],
    ]

    channel_id = getattr(getattr(ctx, 'channel', None), 'id', None)
    if channel_id is None:
        return await safe_send(ctx, embed=emb(title, final, color))

    lock = bot._animation_locks.setdefault(channel_id, asyncio.Lock())

    # Busy-channel fallback: preserve the result and economy outcome without
    # starting another 2-second edit sequence.
    if lock.locked():
        e = emb(title, final, color)
        e.set_footer(text=footer)
        return await safe_send(
            ctx,
            embed=e,
            view=VisualReel(icon_frames[-1], final_highlight, _result_button_style(color)),
        )

    async with lock:
        msg = await safe_send(
            ctx,
            embed=emb(title, frames[0] if frames else final, 0x5865F2),
            view=VisualReel(icon_frames[0], 0),
        )

        if msg is None:
            return None

        # One middle reveal + one final reveal keeps the visual experience while
        # materially reducing REST pressure.
        if len(frames) > 1:
            await asyncio.sleep(ANIM_STEP * 2)
            mid_index = min(1, len(icon_frames) - 1)
            await safe_edit(
                msg,
                embed=emb(title, frames[mid_index], 0x5865F2),
                view=VisualReel(
                    icon_frames[mid_index],
                    mid_index % max(1, len(icon_frames[mid_index])),
                ),
            )

        await asyncio.sleep(ANIM_STEP * 2)
        e = emb(title, final, color)
        e.set_footer(text=footer)
        await safe_edit(
            msg,
            embed=e,
            view=VisualReel(icon_frames[-1], final_highlight, _result_button_style(color)),
        )
        return msg


def _result_button_style(color: int) -> discord.ButtonStyle:
    if color == 0x57F287:
        return discord.ButtonStyle.success
    if color == 0xED4245:
        return discord.ButtonStyle.danger
    return discord.ButtonStyle.primary



class FROXX(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        super().__init__(command_prefix=commands.when_mentioned_or('fx ', 'Fx ', 'FX ', 'fX '), intents=intents, help_command=None, case_insensitive=True)
        self.db = Database(DATABASE_PATH)
        self.admin_ids: set[int] = {OWNER_ID} if OWNER_ID else set()
        self.mines: dict[tuple[int, int], dict] = {}
        self._outbound_lock = asyncio.Lock()
        self._next_outbound: dict[int, float] = {}
        self._animation_locks: dict[int, asyncio.Lock] = {}

    async def setup_hook(self):
        await self.db.init()
        await self.db.seed()
        self.admin_ids = set(await self.db.list_admins())
        if OWNER_ID:
            self.admin_ids.add(OWNER_ID)
            await self.db.ensure_admin(OWNER_ID, OWNER_ID)
        recovered = await self.db.recover_pending_games()
        if recovered:
            log.warning('Recovered and refunded %d unfinished game rounds after startup.', recovered)
        log.info('FROXX database initialized | admins=%d | prefix=%r', len(self.admin_ids), PREFIX)

    async def on_ready(self):
        log.info('FROXX ECONOMY online as %s | guilds=%d', self.user, len(self.guilds))

    async def on_command_error(self, ctx, error):
        if isinstance(error, commands.CommandNotFound):
            return

        if isinstance(error, commands.MissingRequiredArgument):
            await safe_send(
                ctx,
                embed=emb(
                    '❌ Missing input',
                    f'Use `{PREFIX}help` to see the correct syntax.',
                    0xED4245,
                ),
            )
            return

        if isinstance(error, commands.BadArgument):
            await safe_send(
                ctx,
                embed=emb(
                    '❌ Invalid input',
                    f'Check the argument format with `{PREFIX}help`.',
                    0xED4245,
                ),
            )
            return

        if isinstance(error, commands.CheckFailure):
            await safe_send(
                ctx,
                embed=emb(
                    '🔒 Access denied',
                    'You do not have permission to use this command.',
                    0xED4245,
                ),
            )
            return

        if isinstance(error, commands.CommandInvokeError):
            original = error.original

            # Game/economy validation errors are expected user-facing errors,
            # not crashes.  Most importantly, do not send a second traceback
            # message through Discord when the original failure was a 429.
            if isinstance(original, ValueError):
                await safe_send(
                    ctx,
                    embed=emb('❌ Action not completed', str(original), 0xED4245),
                )
                return

            if isinstance(original, discord.HTTPException):
                if getattr(original, 'status', None) == 429:
                    log.warning(
                        'Command hit Discord rate limit | command=%s channel=%s',
                        getattr(getattr(ctx, 'command', None), 'qualified_name', None),
                        getattr(getattr(ctx, 'channel', None), 'id', None),
                    )
                else:
                    log.error(
                        'Discord HTTP error in command | status=%s command=%s',
                        getattr(original, 'status', None),
                        getattr(getattr(ctx, 'command', None), 'qualified_name', None),
                    )
                return

            error = original

        log.exception(
            'Command error | command=%s author=%s',
            getattr(getattr(ctx, 'command', None), 'qualified_name', None),
            getattr(getattr(ctx, 'author', None), 'id', None),
            exc_info=error,
        )

        await safe_send(
            ctx,
            embed=emb(
                '⚠️ Something went wrong',
                'The action was not completed and no coins were charged.',
                0xED4245,
            ),
        )



bot = FROXX()


async def account(uid: int):
    return (await bot.db.ensure_user(uid, starter=True))[0]


async def level_up(uid: int):
    a = await account(uid)
    level = a.level
    while a.xp >= xp_needed(level):
        level += 1
    if level != a.level:
        await bot.db.set_level(uid, a.xp, level)
        return level - a.level
    return 0


async def game_charge(uid: int, bet: int, key: str):
    if bet < 1 or bet > MAX_BET:
        raise ValueError(f'Bet must be between 1 and {fmt(MAX_BET)}.')
    try:
        await bot.db.charge_game(uid, bet, key, GAME_COOLDOWN, secrets.token_hex(16))
    except ValueError as exc:
        msg = str(exc)
        if msg.startswith('cooldown:'):
            raise ValueError(f'⏱️ Cooldown: {msg.split(":", 1)[1]}s remaining.') from None
        if msg == 'insufficient funds':
            a = await account(uid)
            raise ValueError(f'Not enough {COIN} FROXX. Wallet: **{fmt(a.wallet)}**.') from None
        raise


GAME_LABEL_TO_KEY = {
    'coinflip_win': 'game_coinflip',
    'dice_win': 'game_dice',
    'slots_win': 'game_slots',
    'roulette_win': 'game_roulette',
    'rps_result': 'game_rps',
    'higherlower_result': 'game_higherlower',
    'blackjack_result': 'game_blackjack',
    'color_win': 'game_color',
    'fight_result': 'game_fight',
    'mines_cashout': 'game_mines',
    'mines_timeout_cashout': 'game_mines',
}


async def game_payout(uid: int, amount: int, label: str):
    if amount < 0:
        raise ValueError('invalid game payout')
    key = GAME_LABEL_TO_KEY.get(label)
    if not key:
        raise ValueError(f'unknown game settlement: {label}')
    await bot.db.settle_game(uid, key, amount, label, secrets.token_hex(16))


class HelpPanel(discord.ui.View):
    """Four-button help hub. Button results are ephemeral, so only the clicker sees them."""
    def __init__(self):
        super().__init__(timeout=180)

    async def _show(self, interaction: discord.Interaction, title: str, lines: list[str], color: int):
        e = emb(title, '\n'.join(lines), color)
        e.set_footer(text='FROXX • Only you can see this command list • fx is case-insensitive')
        await interaction.response.send_message(embed=e, ephemeral=True)

    @discord.ui.button(label='🪙 Coin + Free', style=discord.ButtonStyle.success, row=0)
    async def coin_free(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._show(interaction, '🪙 COIN + FREE COMMANDS', [
            '`fx start` — Create your FROXX account.',
            '`fx balance` — See your wallet and bank.',
            '`fx profile` — See your level and progress.',
            '`fx work` — Work and earn coins + XP.',
            '`fx daily` — Claim your daily reward.',
            '`fx fish` — Fish for a random reward.',
            '`fx deposit <amount>` — Move coins into your bank.',
            '`fx withdraw <amount>` — Move bank coins to your wallet.',
            '`fx interest` — Claim bank interest.',
            '`fx pay @user <amount>` — Send coins to another player.',
            '`fx leaderboard [type]` — See the top players.',
            '`fx achievements` — See your unlocked goals.',
            '`fx transactions` — See your latest money history.',
        ], 0x57F287)

    @discord.ui.button(label='🎰 Games + Stats', style=discord.ButtonStyle.danger, row=0)
    async def games_stats(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._show(interaction, '🎰 GAMES + STATS', [
            '`fx games` — Open the game command list.',
            '`fx coinflip <bet> <heads|tails>` — Flip a coin.',
            '`fx dice <bet> <1-6>` — Guess the dice number.',
            '`fx slots <bet>` — Spin three reels.',
            '`fx roulette <bet> <pick>` — Pick a wheel result.',
            '`fx rps <bet> <move>` — Play rock paper scissors.',
            '`fx higherlower <bet> <higher|lower>` — Predict the next number.',
            '`fx blackjack <bet>` — Compare your hand with the dealer.',
            '`fx fight <bet> <move>` — Pick strike, guard or magic.',
            '`fx color <bet> <color>` — Pick one of six colors.',
            '`fx mines <bet> [2-12]` — Reveal safe tiles and cash out.',
            '`fx stats` — See wins, losses and win/loss percentages.',
        ], 0xED4245)

    @discord.ui.button(label='🛒 Shop + Create', style=discord.ButtonStyle.primary, row=1)
    async def shop_create(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._show(interaction, '🛒 SHOP + CREATE', [
            '`fx shop` — Browse the market and rarity tiers.',
            '`fx buy <item> [qty]` — Buy an item.',
            '`fx sell <item> [qty]` — Sell an item for coins.',
            '`fx inventory` — See everything you own.',
            '`fx create` — Find a random collectible to sell.',
            '`fx open <crate>` — Open a reward crate.',
            '`fx use <item> [qty]` — Use a booster item.',
            '`fx trade @user` — Start a player trade.',
        ], 0x5865F2)

    @discord.ui.button(label='🛡️ Admin + Utility', style=discord.ButtonStyle.secondary, row=1)
    async def admin_utility(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id == OWNER_ID or interaction.user.id in getattr(bot, 'admin_ids', set()):
            lines = [
                '`fx admin` — Open the admin command list.',
                '`fx admin addadmin @user` — Make a user a FROXX admin.',
                '`fx admin removeadmin @user` — Remove FROXX admin access.',
                '`fx admin admins` — List FROXX admins.',
                '`fx admin add @user <amount>` — Give wallet coins.',
                '`fx admin remove @user <amount>` — Remove wallet coins.',
                '`fx admin set @user <amount>` — Set wallet coins.',
                '`fx admin reset @user` — Reset an account.',
                '`fx admin giveitem @user <item> [qty]` — Give items.',
                '`fx admin removeitem @user <item> [qty]` — Remove items.',
                '`fx admin inspect @user` — Inspect an account.',
                '`fx admin reloadshop` — Re-seed the catalog.',
            ]
            await self._show(interaction, '🛡️ ADMIN COMMANDS', lines, 0x9B59B6)
        else:
            await interaction.response.send_message('🔒 This section is only for FROXX admins.', ephemeral=True)


@bot.command(name='help', aliases=['h'])
async def fx_help(ctx):
    e = emb('💀 FROXX HELP CORE',
        '**Welcome to FROXX.**\n\n'
        'Pick one button below. The command list you open is **private to you**.\n\n'
        '🪙 **Coin + Free** — simple money commands.\n'
        '🎰 **Games + Stats** — games and your W/L record.\n'
        '🛒 **Shop + Create** — collect, buy and sell loot.\n'
        '🛡️ **Admin + Utility** — admin tools if you have access.\n\n'
        '**Every command starts with `fx`.**\n'
        '**Commands ignore capital letters**, so `fx CF 100 HEAD` works too.',
        0xF1C40F)
    e.set_footer(text='FROXX • Click a button • Help panel stays open for 3 minutes')
    await safe_send(ctx, embed=e, view=HelpPanel())


@bot.command(name='start')
async def start(ctx):
    a, created = await bot.db.ensure_user(ctx.author.id, starter=True)
    if created:
        await safe_send(ctx, embed=emb('🚀 FROXX ONLINE', f'Account created! You received **{fmt(STARTER)} {COIN}** starter coins.', 0x57F287))
    else:
        await safe_send(ctx, embed=emb('✅ Already registered', f'Wallet **{fmt(a.wallet)} {COIN}** • Bank **{fmt(a.bank)} {COIN}**'))


@bot.command(name='balance', aliases=['b', 'wallet', 'bank', 'bal'])
async def balance(ctx):
    a = await account(ctx.author.id)
    await safe_send(ctx, embed=emb(f'💰 {ctx.author.display_name} • FROXX', f'Wallet: **{fmt(a.wallet)} {COIN}**\nBank: **{fmt(a.bank)} {COIN}**\nNet worth: **{fmt(a.wallet+a.bank)} {COIN}**'))


@bot.command(name='profile')
async def profile(ctx):
    a = await account(ctx.author.id)
    rank = await bot.db.rank_of(ctx.author.id, 'wallet')
    await safe_send(ctx, embed=emb(f'👤 {ctx.author.display_name}', f'{XP} Level **{a.level}** • XP **{fmt(a.xp)}**\n{COIN} Wallet **{fmt(a.wallet)}**\n🏦 Bank **{fmt(a.bank)}**\n🔥 Daily streak **{a.daily_streak}**\n📈 Earned **{fmt(a.total_earned)}**\n📉 Spent **{fmt(a.total_spent)}**\n🏆 Rank **#{rank}**'))


def _cooldown_text(seconds: int) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f'**{h}h {m}m {s}s**'
    if m:
        return f'**{m}m {s}s**'
    return f'**{s}s**'


async def free_reward(ctx, key: str, label: str, low: int, high: int, xp: int):
    coins = rng.randint(low, high)
    claimed, _ = await bot.db.claim_free_reward(
        ctx.author.id,
        key,
        label,
        coins,
        xp,
        FREE_COOLDOWN,
        secrets.token_hex(16),
        work_delta=1 if key == 'free_work' else 0,
    )
    if not claimed:
        remain = await bot.db.cooldown_remaining(ctx.author.id, key)
        await safe_send(
            ctx,
            embed=emb(
                '⏱️ Reward cooling down',
                f'You already claimed this reward.\nCome back in {_cooldown_text(remain)}.',
                0xFEE75C,
            ),
        )
        return

    await level_up(ctx.author.id)
    await safe_send(
        ctx,
        embed=emb(
            f'✨ {label.upper()}',
            f'You earned **+{fmt(coins)} {COIN}** and **+{xp} {XP}**.\n'
            f'⏳ Next claim in **2h**.',
            0x57F287,
        ),
    )


@bot.command()
async def work(ctx):
    await free_reward(ctx, 'free_work', 'work', 180, 450, 12)


@bot.command()
async def daily(ctx):
    # One atomic DB transaction enforces the 24h rule even if the same user
    # sends multiple daily commands at the same moment.
    coins = rng.randint(1200, 2800)
    xp_gain = 60
    current = await account(ctx.author.id)
    if current.last_daily is not None:
        elapsed = max(0, int(datetime.now(timezone.utc).timestamp()) - current.last_daily)
        if elapsed < DAILY_COOLDOWN:
            remain = DAILY_COOLDOWN - elapsed
            h, rem = divmod(remain, 3600)
            m, sec = divmod(rem, 60)
            await safe_send(ctx, embed=emb('📅 DAILY COOLDOWN', f'Come back in **{h}h {m}m {sec}s**.\nThis reward can be claimed **once every 24 hours**.', 0xFEE75C))
            return
    previous_streak = current.daily_streak
    if current.last_daily is None:
        streak = 1
    else:
        elapsed = int(datetime.now(timezone.utc).timestamp()) - current.last_daily
        streak = previous_streak + 1 if elapsed < 172800 else 1
    cycle = current.daily_cycle + 1
    try:
        await bot.db.daily_claim(ctx.author.id, coins, xp_gain, streak, cycle, secrets.token_hex(16))
    except ValueError as exc:
        if str(exc) == 'daily already claimed':
            remain = await bot.db.cooldown_remaining(ctx.author.id, 'free_daily')
            if remain <= 0:
                current_after = await account(ctx.author.id)
                if current_after.last_daily is not None:
                    remain = max(
                        0,
                        DAILY_COOLDOWN
                        - (int(datetime.now(timezone.utc).timestamp()) - current_after.last_daily),
                    )
            await safe_send(ctx, 
                embed=emb(
                    '📅 DAILY COOLDOWN',
                    f'You already claimed today.\nNext daily in {_cooldown_text(remain)}.\nThis reward can be claimed once every 24 hours.',
                    0xFEE75C,
                )
            )
            return
        raise
    await level_up(ctx.author.id)
    bonus = '🔥 **Streak bonus!**' if streak >= 7 and streak % 7 == 0 else f'🔥 Streak **{streak}**'
    await animated_result(
        ctx, '📅 DAILY DROP',
        ['🎁 Opening your daily vault…', '🗝️ Unlocking…', '✨ Reward secured!'],
        f'🎉 **+{fmt(coins)} {COIN}** • **+{xp_gain} {XP}**\n{bonus}\n⏳ Next daily: **24 hours**',
        0x57F287,
        [['🟣','🔵','🟢','🟡','🔴'], ['🟡','🔴','🟣','🟢','🔵'], ['🎁','✨','💎','🪙','👑'], ['🎁','🎁','🎁','✨','🎉']]
    )


@bot.command()
async def fish(ctx):
    coins = rng.randint(120, 650)
    xp_gain = rng.randint(10, 30)
    catch = rng.choice([
        '🐟 Golden Carp', '🦑 Neon Squid', '🐠 Crystal Fish',
        '🦈 Tiny Shark', '🐡 Moon Puffer', '🐋 Mini Whale'
    ])
    claimed, _ = await bot.db.claim_free_reward(
        ctx.author.id,
        'free_fish',
        'fish',
        coins,
        xp_gain,
        FREE_COOLDOWN,
    )
    if not claimed:
        remain = await bot.db.cooldown_remaining(ctx.author.id, 'free_fish')
        await safe_send(
            ctx,
            embed=emb(
                '🎣 FISHING COOLDOWN',
                f'You already fished recently.\nNext catch in {_cooldown_text(remain)}.',
                0xFEE75C,
            ),
        )
        return

    await level_up(ctx.author.id)
    await animated_result(
        ctx,
        '🎣 FISHING',
        ['🌊 Casting the line…', '〰️ Something is pulling…', '🐟 REELING IT IN…'],
        f'✨ **{catch}** surfaced!\n\n'
        f'🪙 **+{fmt(coins)}** FROXX • ⭐ **+{xp_gain} XP**\n'
        f'⏳ Next fishing: **2h**',
        0x57F287,
        [["🎣","🌊","🐟"],["🎣","🌊","❔"],["🐟","✨","🎉"]],
    )


@bot.command()
async def deposit(ctx, amount: int):
    if amount <= 0: raise ValueError('Amount must be positive.')
    a = await account(ctx.author.id)
    if a.wallet < amount: raise ValueError('Insufficient wallet balance.')
    await bot.db.economy_tx(ctx.author.id, 'deposit', wallet_delta=-amount, bank_delta=amount, tx_id=secrets.token_hex(16))
    await safe_send(ctx, embed=emb('🏦 DEPOSIT COMPLETE', f'**{fmt(amount)} {COIN}** moved to your bank.', 0x57F287))


@bot.command()
async def withdraw(ctx, amount: int):
    if amount <= 0: raise ValueError('Amount must be positive.')
    a = await account(ctx.author.id)
    if a.bank < amount: raise ValueError('Insufficient bank balance.')
    await bot.db.economy_tx(ctx.author.id, 'withdraw', wallet_delta=amount, bank_delta=-amount, tx_id=secrets.token_hex(16))
    await safe_send(ctx, embed=emb('💳 WITHDRAW COMPLETE', f'**{fmt(amount)} {COIN}** moved to your wallet.', 0x57F287))


@bot.command()
async def interest(ctx):
    await free_reward(ctx, 'bank_interest', 'bank interest', 50, 250, 5)


@bot.command(aliases=['cr', 'craft'])
async def create(ctx):
    """Random loot hunt. The database settles cooldown + inventory atomically."""
    result = await bot.db.create_random_drop(ctx.author.id, CREATE_LOOT, CREATE_COOLDOWN, secrets.token_hex(16))
    if not result['claimed']:
        raise ValueError(f"⏱️ Create is recharging. Come back in **{_cooldown_text(result['remaining'])}**.")
    item = result['item']
    await level_up(ctx.author.id)
    rarity = item['rarity']
    bonus = '🔥 JACKPOT DROP!' if rarity in {'LEGENDARY', 'MYTHIC'} else '✨ Nice find!'
    await animated_result(
        ctx,
        '🧪 FROXX CREATE LAB',
        ['🧪 Scanning materials…', '⚙️ Building a random drop…', '✨ Something is forming…'],
        f"{bonus}\n\n{item['emoji']} **{item['name']}**\nRarity: **{rarity}**\nSell value: **{fmt(item['sell_price'])} {COIN}**\n\n🎒 Added to your inventory. Try `fx sell {item['item_id']}`.",
        0xF1C40F,
        [["🧪","⚙️","❔"],["⚙️","✨","💎"],[item['emoji'],"✨","🎉"]],
        final_highlight=0,
    )


@bot.command()
async def shop(ctx):
    rows = await bot.db.all_items()
    e = emb('🛒 FROXX MARKET',
        '**Pick a treasure. Buy it. Use it. Sell it. Repeat.**\n\n'
        '🧪 `fx create` finds random loot.\n'
        '🛍️ `fx buy <item> [qty]` buys stock.\n'
        '💰 `fx sell <item> [qty]` cashes out loot.', 0x2ECC71)
    for x in rows[:24]:
        buy = f"{fmt(x['buy_price'])} {COIN}" if x['buy_price'] else 'Not for sale'
        sell = f"{fmt(x['sell_price'])} {COIN}" if x['sell_price'] else 'No sell value'
        e.add_field(
            name=f"{x['emoji']} {x['name']} • {x['rarity']}",
            value=f"ID: `{x['item_id']}`\nBuy: **{buy}**\nSell: **{sell}**\n{x['description'][:70]}",
            inline=True,
        )
    await safe_send(ctx, embed=e)


@bot.command()
async def buy(ctx, item_id: str, quantity: int = 1):
    if not 1 <= quantity <= 100: raise ValueError('Quantity must be 1–100.')
    await bot.db.buy_item(ctx.author.id, item_id.lower(), quantity, secrets.token_hex(16))
    await safe_send(ctx, embed=emb('🛍️ PURCHASED', f'Bought **{quantity}× {item_id.lower()}**. Check `fx inventory`.', 0x57F287))


@bot.command()
async def sell(ctx, item_id: str, quantity: int = 1):
    if not 1 <= quantity <= 100: raise ValueError('Quantity must be 1–100.')
    a, item = await bot.db.sell_item(ctx.author.id, item_id.lower(), quantity, secrets.token_hex(16))
    await safe_send(ctx, embed=emb('💱 SOLD', f'Sold **{quantity}× {item_id.lower()}** for **{fmt(item["sell_price"]*quantity)} {COIN}**.'))


@bot.command()
async def inventory(ctx):
    rows = await bot.db.get_inventory(ctx.author.id)
    if not rows:
        await safe_send(ctx, embed=emb('🎒 EMPTY INVENTORY', 'Visit `fx shop` to start collecting.')); return
    e = emb('🎒 FROXX INVENTORY')
    for x in rows[:20]: e.add_field(name=f"{x['emoji']} {x['name']} ×{x['quantity']}", value=f'ID: `{x["item_id"]}` • Sell {fmt(x["sell_price"])}', inline=True)
    await safe_send(ctx, embed=e)


@bot.command()
async def open(ctx, item: str):
    item = item.lower()
    if item not in {'mystery_box','rare_crate','legendary_crate'}: raise ValueError('Unknown crate.')
    inv = await bot.db.get_inventory(ctx.author.id)
    owned = next((x for x in inv if x['item_id'] == item and x['quantity'] > 0), None)
    if not owned: raise ValueError('You do not own that crate.')
    await bot.db.remove_item(ctx.author.id, item, 1)
    tier = {'mystery_box':(800,5000),'rare_crate':(5000,25000),'legendary_crate':(25000,150000)}[item]
    coins = rng.randint(*tier)
    await bot.db.economy_tx(ctx.author.id, 'crate_reward', wallet_delta=coins, xp_delta=rng.randint(20,150), tx_id=secrets.token_hex(16))
    await safe_send(ctx, embed=emb('🎁 CRATE CRACKED!', f'**{item}** → **+{fmt(coins)} {COIN}**\nThe reward was settled atomically.', 0xF1C40F))


@bot.command()
async def use(ctx, item_id: str, quantity: int = 1):
    if quantity < 1: raise ValueError('Quantity must be positive.')
    item_id = item_id.lower()
    if item_id not in {'xp_booster','coin_booster'}: raise ValueError('That item is not a consumable in this build.')
    await bot.db.remove_item(ctx.author.id, item_id, quantity)
    if item_id == 'xp_booster':
        gain = 50 * quantity; await bot.db.economy_tx(ctx.author.id, 'xp_booster', xp_delta=gain, tx_id=secrets.token_hex(16)); msg=f'+{gain} {XP}'
    else:
        gain = 500 * quantity; await bot.db.economy_tx(ctx.author.id, 'coin_booster', wallet_delta=gain, tx_id=secrets.token_hex(16)); msg=f'+{fmt(gain)} {COIN}'
    await safe_send(ctx, embed=emb('⚡ BOOSTER USED', msg, 0x57F287))


@bot.command()
async def pay(ctx, member: discord.Member, amount: int):
    if member.bot or member.id == ctx.author.id: raise ValueError('Choose another human member.')
    if amount <= 0: raise ValueError('Amount must be positive.')
    await bot.db.transfer(ctx.author.id, member.id, amount, secrets.token_hex(16))
    await safe_send(ctx, embed=emb('💸 PAYMENT SENT', f'{ctx.author.mention} → {member.mention}\n**{fmt(amount)} {COIN}**', 0x57F287))


@bot.command(name='stats', aliases=['stat', 'wl', 'winloss'])
async def stats(ctx):
    g = await bot.db.game_stats(ctx.author.id)
    played = g['played']
    wins = g['wins']
    losses = g['losses']
    cashouts = g['cashouts']
    win_pct = (wins / played * 100.0) if played else 0.0
    loss_pct = (losses / played * 100.0) if played else 0.0
    a = await account(ctx.author.id)
    e = emb(f'📊 {ctx.author.display_name} • GAME STATS',
        f'🎮 Games played: **{played:,}**\n'
        f'🏆 Wins: **{wins:,}**\n'
        f'💥 Losses: **{losses:,}**\n'
        f'💰 Cashouts: **{cashouts:,}**\n\n'
        f'📈 Win rate: **{win_pct:.2f}%**\n'
        f'📉 Loss rate: **{loss_pct:.2f}%**\n\n'
        f'🔥 Daily streak: **{a.daily_streak}**\n'
        f'⭐ Level: **{a.level}**', 0xF1C40F)
    e.set_footer(text='FROXX • Fixed odds • Your history does not change future odds')
    await safe_send(ctx, embed=e)


@bot.command()
async def leaderboard(ctx, metric: str = 'wallet'):
    metric = metric.lower(); aliases={'richest':'wallet','level':'level','xp':'xp','collectors':'collectibles','weekly':'weekly'}; metric=aliases.get(metric,metric)
    if metric not in {'wallet','level','xp','collectibles','weekly'}: raise ValueError('Metric: wallet, level, xp, collectibles, weekly.')
    rows = await bot.db.leaderboard(metric, 10)
    e = emb(f'🏆 FROXX LEADERBOARD • {metric.upper()}')
    for i,r in enumerate(rows,1):
        val = r['wallet'] if metric=='wallet' else r['level'] if metric=='level' else r['xp'] if metric=='xp' else r['weekly_earned'] if metric=='weekly' else 'collector'
        e.add_field(name=f'#{i} • <@{r["user_id"]}>', value=f'**{fmt(val) if isinstance(val,int) else val}**', inline=False)
    await safe_send(ctx, embed=e)


@bot.command()
async def achievements(ctx):
    rows = await bot.db.achievement_rows(); unlocked = await bot.db.unlocked(ctx.author.id)
    e = emb('🏅 ACHIEVEMENTS')
    for r in rows[:15]: e.add_field(name=('✅ ' if r['achievement_id'] in unlocked else '🔒 ')+r['name'], value=r['description'], inline=False)
    await safe_send(ctx, embed=e)


@bot.command()
async def transactions(ctx):
    rows = await bot.db.transactions(ctx.author.id, 10)
    if not rows: await safe_send(ctx, embed=emb('📜 LEDGER','No transactions yet.')); return
    e=emb('📜 RECENT LEDGER')
    for r in rows: e.add_field(name=r['kind'], value=f"{r['amount']:+,} {COIN} • <t:{r['created_at']}:R>", inline=False)
    await safe_send(ctx, embed=e)


@bot.command(aliases=['g'])
async def games(ctx):
    await safe_send(ctx, embed=emb('🎮 FROXX GAME ARCADE',
        '`fx coinflip <bet> <heads|tails>`\n'
        '`fx dice <bet> <1-6>`\n'
        '`fx slots <bet>`\n'
        '`fx roulette <bet> <red|black|green|0-36>`\n'
        '`fx rps <bet> <rock|paper|scissors>`\n'
        '`fx higherlower <bet> <higher|lower>`\n'
        '`fx blackjack <bet>` • `fx bj <bet>`\n'
        '`fx fight <bet> <strike|guard|magic>` • `fx duel <bet> <move>`\n'
        '`fx color <bet> <red|blue|green|yellow|purple|orange>`\n'
        '`fx mines <bet> [2-12]`\n\n'
        '⏱️ Instant-game cooldown: **1 second per game**.\n'
        '🎯 Outcome is settled first with secure randomness; the 2-second reel is only a public reveal.\n'
        '🪙 Payouts are fixed and economy-safe.'
    ))

DICE_ICONS = ['⚀', '⚁', '⚂', '⚃', '⚄', '⚅']
RPS_ICONS = {'rock': '🪨', 'paper': '📄', 'scissors': '✂️'}
COIN_ICONS = {'heads': '🪙', 'tails': '🔄'}
HL_ICONS = {'higher': '⬆️', 'lower': '⬇️', 'tie': '🟰'}
CARD_ICONS = ['🂡', '🂱', '🃁', '🃑', '🂮', '🃞']


def _rotate(items: list[str], shift: int) -> list[str]:
    if not items:
        return []
    shift %= len(items)
    return items[shift:] + items[:shift]


def _dice_frames(result: int):
    return [_rotate(DICE_ICONS, 0), _rotate(DICE_ICONS, 3), _rotate(DICE_ICONS, 1), _rotate(DICE_ICONS, 5)], result - 1


def _coin_frames(result: str):
    base = [COIN_ICONS['heads'], COIN_ICONS['tails']]
    return [base, base[::-1], base, base[::-1]], (0 if result == 'heads' else 1)


def _rps_frames(result: str):
    base = [RPS_ICONS['rock'], RPS_ICONS['paper'], RPS_ICONS['scissors']]
    return [base, [base[1], base[2], base[0]], [base[2], base[0], base[1]], base], ['rock', 'paper', 'scissors'].index(result)


def _hl_frames(result: str):
    base = [HL_ICONS['higher'], HL_ICONS['lower'], HL_ICONS['tie']]
    return [base, [base[1], base[2], base[0]], [base[2], base[0], base[1]], base], {'higher': 0, 'lower': 1, 'tie': 2}[result]


def _blackjack_frames(player: int, dealer: int):
    final = CARD_ICONS[(player + dealer) % len(CARD_ICONS)]
    return [
        ['🂠', '🂠', '🂠', '🂠', '🂠', '🂠'],
        ['🃏', '🂠', '🃏', '🂠', '🃏', '🂠'],
        ['🂡', '🂱', '🃁', '🃑', '🂮', '🃞'],
        [final, '🂠', '🃏', '🂠', '🃏', '🂠'],
    ], 0


def _roulette_frames(number: int, color: str):
    red = {1,3,5,7,9,12,14,16,18,19,21,23,25,27,30,32,34,36}
    pockets = [f"{'🟢' if n == 0 else ('🔴' if n in red else '⚫')}{n}" for n in range(37)]
    window_start = max(0, min(27, number - 4))
    final_window = pockets[window_start:window_start + 10]
    if len(final_window) < 10:
        final_window += pockets[:10-len(final_window)]
    final_index = final_window.index(pockets[number])
    frames = [
        [pockets[i] for i in [0,7,14,21,28,35,3,10,17,24]],
        [pockets[i] for i in [5,12,19,26,33,2,9,16,23,30]],
        [pockets[i] for i in [11,18,25,32,36,4,13,20,27,34]],
        final_window,
    ]
    return frames, final_index


def _slot_frames(reels: list[str]):
    syms = ['🍒','🍋','🔔','💎','7️⃣']
    return [
        ['❔'] * 9,
        ['🍒','🔔','💎','🍋','7️⃣','🍒','🔔','🍋','💎'],
        [syms[2],syms[4],syms[0],syms[3],syms[1],syms[2],syms[4],syms[0],syms[3]],
        [reels[0],reels[1],reels[2],'🎯','🎯','🎯','💰','💰','💰'],
    ], {0, 1, 2}


@bot.command(aliases=['cf','flip','coin'])
async def coinflip(ctx, bet: int, choice: str):
    choice = choice.lower().strip()
    choice = {'head':'heads','h':'heads','tail':'tails','t':'tails'}.get(choice, choice)
    if choice not in {'heads','tails'}:
        raise ValueError('Choice must be heads/head or tails/tail.')
    await game_charge(ctx.author.id, bet, 'game_coinflip')
    result = rng.choice(['heads','tails'])
    win = result == choice
    payout = int(bet * 1.98) if win else 0
    await game_payout(ctx.author.id, payout, 'coinflip_win')
    frames, hi = _coin_frames(result)
    final = f'🪙 **LANDED: {result.upper()}** • Pick: **{choice.upper()}**\n' + (f'🏆 **WIN • +{fmt(payout-bet)} profit** {COIN}' if win else f'💥 **LOSS • -{fmt(bet)}** {COIN}')
    await animated_result(ctx, '🪙 FROXX COIN FLIP', ['🪙 Coin ready…','🔄 Heads or tails…','⚡ Final spin…','🎯 Locking the side…'], final, 0x57F287 if win else 0xED4245, frames, final_highlight=hi)


@bot.command(aliases=['d'])
async def dice(ctx, bet: int, guess: int):
    if not 1 <= guess <= 6:
        raise ValueError('Guess must be 1–6.')
    await game_charge(ctx.author.id, bet, 'game_dice')
    result = rng.randint(1,6)
    win = result == guess
    payout = int(bet * 5.70) if win else 0
    await game_payout(ctx.author.id, payout, 'dice_win')
    frames, hi = _dice_frames(result)
    final = f'🎲 **LANDED: {result}** • Your pick: **{guess}**\n' + (f'🏆 **WIN • +{fmt(payout-bet)} profit** {COIN}' if win else f'💥 **LOSS • -{fmt(bet)}** {COIN}')
    await animated_result(ctx, '🎲 FROXX DICE — SIX OUTCOMES', ['🎲 Six faces loaded…','🔄 Faces rotating…','⚡ Rolling faster…','🎯 One face is locking…'], final, 0x57F287 if win else 0xED4245, frames, final_highlight=hi)


@bot.command(aliases=['s','slot'])
async def slots(ctx, bet: int):
    await game_charge(ctx.author.id, bet, 'game_slots')
    syms = ['🍒','🍋','🔔','💎','7️⃣']
    reels = [rng.choice(syms) for _ in range(3)]
    mult = {'7️⃣':12,'💎':8,'🔔':5,'🍒':4,'🍋':3}
    payout = bet * mult[reels[0]] if reels[0] == reels[1] == reels[2] else (int(bet * 1.4) if len(set(reels)) == 2 else 0)
    await game_payout(ctx.author.id, payout, 'slots_win')
    win = payout > 0
    frames, hi = _slot_frames(reels)
    final = f"🎰 **REELS: {' '.join(reels)}**\n" + (f'🏆 **WIN • {fmt(payout)} {COIN} total return**' if win else f'💥 **LOSS • -{fmt(bet)}** {COIN}')
    await animated_result(ctx, '🎰 FROXX SLOTS — THREE REELS', ['🎰 Reels loaded…','🔄 Reel 1 spinning…','🔄 Reel 2 spinning…','🎯 Reel 3 locking…'], final, 0x57F287 if win else 0xED4245, frames, final_highlight=hi)


@bot.command(aliases=['rl','r','wheel'])
async def roulette(ctx, bet: int, pick: str):
    pick = pick.lower()
    valid = {'red','black','green'} | {str(i) for i in range(37)}
    if pick not in valid:
        raise ValueError('Pick red, black, green or 0–36.')
    await game_charge(ctx.author.id, bet, 'game_roulette')
    n = rng.randint(0,36)
    red_numbers = {1,3,5,7,9,12,14,16,18,19,21,23,25,27,30,32,34,36}
    color = 'green' if n == 0 else ('red' if n in red_numbers else 'black')
    win = pick == color or pick == str(n)
    mult = 35 if pick.isdigit() else 14 if pick == 'green' else 1.95
    payout = int(bet * mult) if win else 0
    await game_payout(ctx.author.id, payout, 'roulette_win')
    frames, hi = _roulette_frames(n, color)
    final = f'🎡 **LANDED: {n} {color.upper()}** • Pick: **{pick.upper()}**\n' + (f'🏆 **WIN • +{fmt(payout-bet)} profit** {COIN}' if win else f'💥 **LOSS • -{fmt(bet)}** {COIN}')
    await animated_result(ctx, '🎡 FROXX ROULETTE — 37 POCKETS', ['🎡 Wheel loaded…','🔄 Pockets flying…','⚡ Wheel slowing…','🎯 Final pocket locking…'], final, 0x57F287 if win else 0xED4245, frames, final_highlight=hi)


@bot.command(aliases=['rp','rpss'])
async def rps(ctx, bet: int, choice: str):
    choice = choice.lower()
    if choice not in {'rock','paper','scissors'}:
        raise ValueError('Pick rock, paper or scissors.')
    await game_charge(ctx.author.id, bet, 'game_rps')
    botpick = rng.choice(['rock','paper','scissors'])
    win = (choice,botpick) in {('rock','scissors'),('paper','rock'),('scissors','paper')}
    tie = choice == botpick
    payout = bet if tie else int(bet * 1.90) if win else 0
    await game_payout(ctx.author.id, payout, 'rps_result')
    frames, hi = _rps_frames(botpick)
    final = f'✊ **YOU: {choice.upper()}** • **FROXX: {botpick.upper()}**\n' + ('🤝 **PUSH • bet returned**' if tie else f'🏆 **WIN • +{fmt(payout-bet)} profit** {COIN}' if win else f'💥 **LOSS • -{fmt(bet)}** {COIN}')
    await animated_result(ctx, '✊ FROXX RPS — THREE CHOICES', ['✊ Choices loaded…','🔄 Opponent choosing…','⚡ Reveal approaching…','🎯 Locking the hand…'], final, 0x57F287 if win else 0xED4245 if not tie else 0xFEE75C, frames, final_highlight=hi)


@bot.command(aliases=['hl','hilo','highlow'])
async def higherlower(ctx, bet: int, guess: str):
    guess = guess.lower()
    if guess not in {'higher','lower'}:
        raise ValueError('Guess higher or lower.')
    await game_charge(ctx.author.id, bet, 'game_higherlower')
    a = rng.randint(1,100); b = rng.randint(1,100)
    outcome = 'tie' if a == b else ('higher' if b > a else 'lower')
    win = outcome == guess; tie = outcome == 'tie'
    payout = bet if tie else int(bet * 1.85) if win else 0
    await game_payout(ctx.author.id, payout, 'higherlower_result')
    frames, hi = _hl_frames(outcome)
    final = f'📊 **{a} → {b}** • Result: **{outcome.upper()}**\n' + ('🤝 **TIE • bet returned**' if tie else f'🏆 **WIN • +{fmt(payout-bet)} profit** {COIN}' if win else f'💥 **LOSS • -{fmt(bet)}** {COIN}')
    await animated_result(ctx, '📊 HIGHER / LOWER — RESULT REVEAL', [f'🔮 First number: **{a}**','🔄 Reading the next roll…','⚡ Comparing…','🎯 Locking the direction…'], final, 0x57F287 if win else 0xED4245 if not tie else 0xFEE75C, frames, final_highlight=hi)


@bot.command(aliases=['bj','blackjackgame'])
async def blackjack(ctx, bet: int):
    await game_charge(ctx.author.id, bet, 'game_blackjack')
    player = rng.randint(15,21); dealer = rng.randint(15,21)
    win = player > dealer; tie = player == dealer
    payout = bet if tie else int(bet * 1.95) if win else 0
    await game_payout(ctx.author.id, payout, 'blackjack_result')
    frames, hi = _blackjack_frames(player, dealer)
    final = f'🃏 **PLAYER: {player}** • **DEALER: {dealer}**\n' + (f'🏆 **WIN • +{fmt(payout-bet)} profit** {COIN}' if win else '🤝 **PUSH • bet returned**' if tie else f'💥 **LOSS • -{fmt(bet)}** {COIN}')
    await animated_result(ctx, '🃏 FROXX BLACKJACK — HAND REVEAL', ['🃏 Dealing the hand…','🂠 Dealer card hidden…','⚡ Comparing totals…','🎯 Locking the result…'], final, 0x57F287 if win else 0xED4245 if not tie else 0xFEE75C, frames, final_highlight=hi)


@bot.command(aliases=['c','colour','clr'])
async def color(ctx, bet: int, choice: str):
    colors = ['red','blue','green','yellow','purple','orange']
    choice = choice.lower()
    if choice not in colors:
        raise ValueError('Pick red, blue, green, yellow, purple or orange.')
    await game_charge(ctx.author.id, bet, 'game_color')
    result = rng.choice(colors)
    win = result == choice
    payout = int(bet * 5.70) if win else 0
    await game_payout(ctx.author.id, payout, 'color_win')
    frames = [colors, colors[2:]+colors[:2], colors[4:]+colors[:4], colors]
    hi = colors.index(result)
    final = f'🎨 **LANDED: {result.upper()}** • Pick: **{choice.upper()}**\n' + (f'🏆 **WIN • +{fmt(payout-bet)} profit** {COIN}' if win else f'💥 **LOSS • -{fmt(bet)}** {COIN}')
    await animated_result(ctx, '🎨 FROXX COLOR LOCK', ['🎨 Colors loaded…','🔄 Color wheel moving…','⚡ Wheel accelerating…','🎯 Final color locking…'], final, 0x57F287 if win else 0xED4245, [frames, frames[1:]+[frames[0]], frames[2:]+frames[:2], frames], final_highlight=hi)


@bot.command(aliases=['f','duel','arena'])
async def fight(ctx, bet: int, choice: str):
    moves = ['strike','guard','magic']
    choice = choice.lower()
    if choice not in moves:
        raise ValueError('Pick strike, guard or magic.')
    await game_charge(ctx.author.id, bet, 'game_fight')
    enemy = rng.choice(moves)
    beats = {'strike':'magic','magic':'guard','guard':'strike'}
    tie = enemy == choice
    win = beats[choice] == enemy
    payout = bet if tie else int(bet * 1.90) if win else 0
    await game_payout(ctx.author.id, payout, 'fight_result')
    icons = {'strike':'⚔️','guard':'🛡️','magic':'🔮'}
    frames = [[icons[x] for x in moves], [icons[x] for x in moves[1:]+moves[:1]], [icons[x] for x in moves[2:]+moves[:2]], [icons[x] for x in moves]]
    hi = moves.index(enemy)
    final = f'⚔️ **YOU: {choice.upper()}** • **FROXX: {enemy.upper()}**\n' + ('🤝 **PUSH • bet returned**' if tie else f'🏆 **WIN • +{fmt(payout-bet)} profit** {COIN}' if win else f'💥 **LOSS • -{fmt(bet)}** {COIN}')
    await animated_result(ctx, '⚔️ FROXX ARENA — MOVE REVEAL', ['⚔️ Opponent enters…','🔄 Moves cycling…','⚡ Final move loading…','🎯 Clash locked…'], final, 0x57F287 if win else 0xED4245 if not tie else 0xFEE75C, frames, final_highlight=hi)


class MinesView(discord.ui.View):
    def __init__(self, ctx: commands.Context, bet: int, mine_count: int, mines: set[int]):
        super().__init__(timeout=90)
        self.ctx = ctx
        self.bet = bet
        self.mine_count = mine_count
        self.mines = mines
        self.safe_reveals = 0
        self.active = True
        self.revealed: set[int] = set()
        self.multiplier = 1.0
        self._round_lock = asyncio.Lock()
        for i in range(20):
            self.add_item(MinesButton(i))
        self.cashout = CashoutButton()
        self.add_item(self.cashout)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message('🔒 This Mines board belongs to the player who started it.', ephemeral=True)
            return False
        if not self.active:
            await interaction.response.send_message('This round is already finished.', ephemeral=True)
            return False
        return True

    def current_multiplier(self) -> float:
        # Fair probability model with a 3% house margin.
        safe_total = 20 - self.mine_count
        if self.safe_reveals >= safe_total:
            return 1.0
        mult = 1.0
        for k in range(self.safe_reveals):
            mult *= (20 - k) / (safe_total - k)
        return round(min(250.0, max(1.01, mult * 0.97)), 2)

    def board_text(self) -> str:
        tiles = []
        for i in range(20):
            if i in self.revealed:
                tiles.append('💎')
            else:
                tiles.append('⬜')
        rows = [' '.join(tiles[i:i+5]) for i in range(0,20,5)]
        return '\n'.join(rows)

    async def explode(self, interaction: discord.Interaction, mine: int) -> None:
        self.active = False
        for child in self.children:
            child.disabled = True
            if isinstance(child, MinesButton) and child.index == mine:
                child.label = '💣'
        self.cashout.disabled = True
        await interaction.response.edit_message(embed=emb('💥 MINES — BOOM', f'{self.board_text()}\n\n💣 You hit a mine. **-{fmt(self.bet)} {COIN}**\nMine count: **{self.mine_count}**', 0xED4245), view=self)
        self.stop()

    async def reveal(self, interaction: discord.Interaction, index: int) -> None:
        async with self._round_lock:
            if not self.active:
                await interaction.response.send_message('This round is already finished.', ephemeral=True)
                return
            if index in self.revealed:
                await interaction.response.send_message('That tile is already revealed.', ephemeral=True)
                return
            if index in self.mines:
                return await self.explode(interaction, index)
            self.revealed.add(index)
            self.safe_reveals += 1
            self.multiplier = self.current_multiplier()
            button = next(x for x in self.children if isinstance(x, MinesButton) and x.index == index)
            button.disabled = True
            button.label = '💎'
            if self.safe_reveals >= 20 - self.mine_count:
                self.active = False
                payout = int(self.bet * self.multiplier)
                await game_payout(self.ctx.author.id, payout, 'mines_cashout')
                for child in self.children:
                    child.disabled = True
                await interaction.response.edit_message(
                    embed=emb(
                        '🏆 MINES — PERFECT CLEAR',
                        f'{self.board_text()}\n\n💎 All safe tiles found!\n💰 Return: **{fmt(payout)} {COIN}** • Multiplier: **{self.multiplier:.2f}x**',
                        0x57F287,
                    ),
                    view=self,
                )
                self.stop()
                return
            await interaction.response.edit_message(
                embed=emb(
                    '💣 MINES — SAFE!',
                    f'{self.board_text()}\n\n💎 Safe picks: **{self.safe_reveals}**\n📈 Multiplier: **{self.multiplier:.2f}x**\n💰 Cashout now: **{fmt(int(self.bet*self.multiplier))} {COIN}**\n\nPick another tile or hit **CASH OUT**.',
                    0x57F287,
                ),
                view=self,
            )

    async def do_cashout(self, interaction: discord.Interaction) -> None:
        async with self._round_lock:
            if not self.active:
                await interaction.response.send_message('This round is already finished.', ephemeral=True)
                return
            self.active = False
            payout = int(self.bet * self.multiplier)
            await game_payout(self.ctx.author.id, payout, 'mines_cashout')
            for child in self.children:
                child.disabled = True
            await interaction.response.edit_message(
                embed=emb(
                    '💰 MINES — CASHED OUT',
                    f'{self.board_text()}\n\n🧠 Safe picks: **{self.safe_reveals}**\n📈 Final multiplier: **{self.multiplier:.2f}x**\n💰 Return: **{fmt(payout)} {COIN}**',
                    0x57F287,
                ),
                view=self,
            )
            self.stop()

    async def on_timeout(self) -> None:
        async with self._round_lock:
            if not self.active:
                return
            self.active = False
            for child in self.children:
                child.disabled = True
            payout = int(self.bet * self.multiplier)
            await game_payout(self.ctx.author.id, payout, 'mines_timeout_cashout')
            try:
                await self.message.edit(
                    embed=emb(
                        '⏱️ MINES — AUTO CASHOUT',
                        f'{self.board_text()}\n\nInactivity timeout. Your current value was automatically cashed out for **{fmt(payout)} {COIN}**.',
                        0xFEE75C,
                    ),
                    view=self,
                )
            except Exception:
                pass

class MinesButton(discord.ui.Button):
    def __init__(self, index: int):
        super().__init__(label='⬜', style=discord.ButtonStyle.secondary, row=index//5)
        self.index = index
    async def callback(self, interaction: discord.Interaction):
        view: MinesView = self.view
        await view.reveal(interaction, self.index)

class CashoutButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label='💰 CASH OUT', style=discord.ButtonStyle.success, row=4)
    async def callback(self, interaction: discord.Interaction):
        view: MinesView = self.view
        await view.do_cashout(interaction)

@bot.command(aliases=['m'])
async def mines(ctx, bet: int, mine_count: int = 5):
    if not 2 <= mine_count <= 12: raise ValueError('Mine count must be 2–12.')
    key = f'game_mines'
    await game_charge(ctx.author.id, bet, key)
    mine_positions = set(rng.sample(range(20), mine_count))
    view = MinesView(ctx, bet, mine_count, mine_positions)
    msg = await safe_send(ctx, embed=emb('💣 MINES — BOARD READY', f'{view.board_text()}\n\n💰 Bet: **{fmt(bet)} {COIN}**\n💣 Mines: **{mine_count}**\n📈 Multiplier: **1.00x**\n\nReveal safe tiles and cash out before you hit a mine.', 0x5865F2), view=view)
    view.message = msg


@bot.group(name='admin', invoke_without_command=True)
@commands.check(is_admin)
async def admin(ctx):
    await safe_send(ctx, embed=emb('🛡️ ADMIN', '`fx admin add @user amount`\n`fx admin remove @user amount`\n`fx admin addadmin @user` / `removeadmin @user`\n`fx admin set @user amount`\n`fx admin reset @user`\n`fx admin giveitem @user item qty`\n`fx admin removeitem @user item qty`\n`fx admin inspect @user`\n`fx admin reloadshop`'))


@admin.command(name='addadmin', aliases=['grantadmin','promote'])
async def admin_addadmin(ctx, member: discord.Member):
    if ctx.author.id != OWNER_ID:
        raise commands.CheckFailure('Only the configured owner can grant FROXX admin access.')
    if member.bot:
        raise ValueError('Bots cannot be FROXX economy admins.')
    await bot.db.ensure_admin(member.id, ctx.author.id)
    bot.admin_ids.add(member.id)
    await safe_send(ctx, f'🛡️ **FROXX ADMIN GRANTED** — {member.mention} can now use `fx admin` commands.')


@admin.command(name='removeadmin', aliases=['revokeadmin','demote'])
async def admin_removeadmin(ctx, member: discord.Member):
    if ctx.author.id != OWNER_ID:
        raise commands.CheckFailure('Only the configured owner can revoke FROXX admin access.')
    if member.id == OWNER_ID:
        raise ValueError('The configured owner cannot be removed.')
    await bot.db.remove_admin(member.id)
    bot.admin_ids.discard(member.id)
    await safe_send(ctx, f'🔒 **FROXX ADMIN REVOKED** — {member.mention} can no longer use `fx admin` commands.')


@admin.command(name='admins', aliases=['adminlist','listadmins'])
async def admin_admins(ctx):
    rows = await bot.db.list_admins_detailed()
    if not rows:
        await safe_send(ctx, '🛡️ No additional FROXX admins are configured.')
        return
    lines = [f'• <@{r["user_id"]}> — granted by <@{r["granted_by"]}>' for r in rows]
    await safe_send(ctx, embed=emb('🛡️ FROXX ADMIN LIST', '\n'.join(lines)))


@admin.command(name='add')
async def admin_add(ctx, member: discord.Member, amount: int):
    if amount<=0: raise ValueError('Amount must be positive.')
    await bot.db.economy_tx(member.id,'admin_add',wallet_delta=amount,tx_id=secrets.token_hex(16)); await safe_send(ctx, f'✅ Added **{fmt(amount)} {COIN}** to {member.mention}.')

@admin.command(name='remove')
async def admin_remove(ctx, member: discord.Member, amount: int):
    if amount<=0: raise ValueError('Amount must be positive.')
    a=await account(member.id)
    if a.wallet<amount: raise ValueError('User does not have enough wallet coins.')
    await bot.db.economy_tx(member.id,'admin_remove',wallet_delta=-amount,tx_id=secrets.token_hex(16)); await safe_send(ctx, f'✅ Removed **{fmt(amount)} {COIN}** from {member.mention}.')

@admin.command(name='set')
async def admin_set(ctx, member: discord.Member, amount: int):
    if amount<0: raise ValueError('Amount cannot be negative.')
    a=await account(member.id); delta=amount-a.wallet
    if delta: await bot.db.economy_tx(member.id,'admin_set',wallet_delta=delta,tx_id=secrets.token_hex(16))
    await safe_send(ctx, f'✅ {member.mention} wallet set to **{fmt(amount)} {COIN}**.')

@admin.command(name='reset')
async def admin_reset(ctx, member: discord.Member):
    await bot.db.reset_user(member.id); await safe_send(ctx, f'♻️ Reset {member.mention}.')

@admin.command(name='giveitem')
async def admin_giveitem(ctx, member: discord.Member, item_id: str, quantity: int=1):
    if quantity<1: raise ValueError('Quantity must be positive.')
    await bot.db.add_item(member.id,item_id.lower(),quantity,'Admin Gift'); await safe_send(ctx, f'🎁 Gave **{quantity}× {item_id.lower()}** to {member.mention}.')

@admin.command(name='removeitem')
async def admin_removeitem(ctx, member: discord.Member, item_id: str, quantity: int=1):
    if quantity<1: raise ValueError('Quantity must be positive.')
    await bot.db.remove_item(member.id,item_id.lower(),quantity); await safe_send(ctx, f'🗑️ Removed **{quantity}× {item_id.lower()}** from {member.mention}.')

@admin.command(name='inspect')
async def admin_inspect(ctx, member: discord.Member):
    a=await account(member.id); await safe_send(ctx, embed=emb(f'🔎 {member.display_name}',f'Wallet **{fmt(a.wallet)}**\nBank **{fmt(a.bank)}**\nXP **{fmt(a.xp)}**\nLevel **{a.level}**\nEarned **{fmt(a.total_earned)}**\nSpent **{fmt(a.total_spent)}**'))

@admin.command(name='reloadshop')
async def admin_reloadshop(ctx):
    await bot.db.seed(); await safe_send(ctx, '🛒 Shop catalog verified/reseeded.')


async def main():
    if not TOKEN:
        raise RuntimeError('DISCORD_TOKEN is missing from .env')
    async with bot:
        await bot.start(TOKEN)

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception:
        log.exception('FROXX fatal startup/runtime failure')
        raise
