import os
import random
import sqlite3
import time

import discord
from discord.ext import tasks
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_NAME = os.getenv("STOCK_CHANNEL_NAME", "주식")  # 이 이름의 채널에 패널이 자동 생성됨
START_MONEY = 100_000        # 처음 지급금
ALLOWANCE = 10_000           # 용돈 버튼 금액
ALLOWANCE_COOLDOWN = 3600    # 용돈 쿨타임(초)
UPDATE_SEC = 10              # 시세 변동 + 메시지 수정 주기(초)
DB_PATH = os.getenv("DB_PATH", "stock.db")

# 가상 종목: 심볼 -> (이름, 시작가, 변동폭)
STOCKS = {
    "COW": ("소전자", 50_000, 0.012),
    "WLD": ("야생식품", 20_000, 0.020),
    "RIDE": ("놀이공원", 8_000, 0.030),
    "STONE": ("돌광업", 3_000, 0.045),
    "CHAIN": ("체인리프트", 12_000, 0.025),
}

# ---------------------------------------------------------------- DB
db = sqlite3.connect(DB_PATH)
db.row_factory = sqlite3.Row
db.executescript(
    """
CREATE TABLE IF NOT EXISTS users(
    user_id INTEGER PRIMARY KEY, money INTEGER NOT NULL, last_allowance REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS holdings(
    user_id INTEGER, symbol TEXT, qty INTEGER NOT NULL, avg_price REAL NOT NULL,
    PRIMARY KEY(user_id, symbol));
CREATE TABLE IF NOT EXISTS prices(
    symbol TEXT PRIMARY KEY, price INTEGER NOT NULL, prev INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS panels(
    channel_id INTEGER PRIMARY KEY, message_id INTEGER NOT NULL);
"""
)
for sym, (_, start, _) in STOCKS.items():
    db.execute("INSERT OR IGNORE INTO prices VALUES(?,?,?)", (sym, start, start))
db.commit()


def get_user(uid: int) -> sqlite3.Row:
    db.execute("INSERT OR IGNORE INTO users(user_id, money) VALUES(?,?)", (uid, START_MONEY))
    db.commit()
    return db.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()


def get_prices() -> dict:
    return {r["symbol"]: (r["price"], r["prev"]) for r in db.execute("SELECT * FROM prices")}


def tick_prices():
    for sym, (_, _, vol) in STOCKS.items():
        row = db.execute("SELECT price FROM prices WHERE symbol=?", (sym,)).fetchone()
        old = row["price"]
        new = max(100, round(old * (1 + random.gauss(0, vol))))
        db.execute("UPDATE prices SET price=?, prev=? WHERE symbol=?", (new, old, sym))
    db.commit()


def resolve_symbol(text: str):
    t = text.strip().upper()
    if t in STOCKS:
        return t
    for sym, (name, _, _) in STOCKS.items():
        if name == text.strip():
            return sym
    return None


def total_assets(uid: int) -> int:
    money = get_user(uid)["money"]
    prices = get_prices()
    held = db.execute("SELECT symbol, qty FROM holdings WHERE user_id=?", (uid,)).fetchall()
    return money + sum(prices[h["symbol"]][0] * h["qty"] for h in held)


# ---------------------------------------------------------------- 거래
def buy(uid: int, sym: str, qty: int) -> str:
    price = get_prices()[sym][0]
    cost = price * qty
    user = get_user(uid)
    if cost > user["money"]:
        return f"❌ 돈이 부족해요. 필요 {cost:,}원 / 보유 {user['money']:,}원"
    h = db.execute("SELECT * FROM holdings WHERE user_id=? AND symbol=?", (uid, sym)).fetchone()
    if h:
        new_qty = h["qty"] + qty
        new_avg = (h["avg_price"] * h["qty"] + cost) / new_qty
        db.execute("UPDATE holdings SET qty=?, avg_price=? WHERE user_id=? AND symbol=?",
                   (new_qty, new_avg, uid, sym))
    else:
        db.execute("INSERT INTO holdings VALUES(?,?,?,?)", (uid, sym, qty, price))
    db.execute("UPDATE users SET money=money-? WHERE user_id=?", (cost, uid))
    db.commit()
    return f"✅ {STOCKS[sym][0]} {qty:,}주를 {price:,}원에 매수했어요. (-{cost:,}원)"


def sell(uid: int, sym: str, qty: int) -> str:
    h = db.execute("SELECT * FROM holdings WHERE user_id=? AND symbol=?", (uid, sym)).fetchone()
    if not h or h["qty"] < qty:
        have = h["qty"] if h else 0
        return f"❌ 보유 수량이 부족해요. (보유 {have:,}주)"
    price = get_prices()[sym][0]
    gain = price * qty
    profit = round((price - h["avg_price"]) * qty)
    if h["qty"] == qty:
        db.execute("DELETE FROM holdings WHERE user_id=? AND symbol=?", (uid, sym))
    else:
        db.execute("UPDATE holdings SET qty=qty-? WHERE user_id=? AND symbol=?", (qty, uid, sym))
    db.execute("UPDATE users SET money=money+? WHERE user_id=?", (gain, uid))
    db.commit()
    sign = "+" if profit >= 0 else ""
    return f"✅ {STOCKS[sym][0]} {qty:,}주를 {price:,}원에 매도했어요. (+{gain:,}원, 손익 {sign}{profit:,}원)"


# ---------------------------------------------------------------- 화면
def panel_embed() -> discord.Embed:
    prices = get_prices()
    lines = []
    for sym, (name, _, _) in STOCKS.items():
        p, prev = prices[sym]
        pct = (p - prev) / prev * 100
        arrow = "🔺" if p > prev else ("🔻" if p < prev else "➖")
        lines.append(f"{arrow} **{name}** (`{sym}`)  {p:,}원  ({pct:+.2f}%)")
    embed = discord.Embed(title="📈 실시간 주식 현황", description="\n".join(lines), color=0x2ECC71)
    embed.set_footer(text=f"{UPDATE_SEC}초마다 자동 갱신 · 가상 머니 게임")
    embed.timestamp = discord.utils.utcnow()
    return embed


def my_embed(user: discord.abc.User) -> discord.Embed:
    u = get_user(user.id)
    prices = get_prices()
    held = db.execute("SELECT * FROM holdings WHERE user_id=?", (user.id,)).fetchall()
    lines = []
    for h in held:
        p = prices[h["symbol"]][0]
        profit = round((p - h["avg_price"]) * h["qty"])
        lines.append(f"**{STOCKS[h['symbol']][0]}** {h['qty']:,}주 · 평단 {round(h['avg_price']):,}원 · 손익 {profit:+,}원")
    embed = discord.Embed(title=f"💼 {user.display_name}님의 자산", color=0x3498DB)
    embed.add_field(name="현금", value=f"{u['money']:,}원")
    embed.add_field(name="총 자산", value=f"{total_assets(user.id):,}원")
    embed.add_field(name="보유 종목", value="\n".join(lines) or "없음", inline=False)
    return embed


class TradeModal(discord.ui.Modal):
    def __init__(self, mode: str):
        super().__init__(title="매수" if mode == "buy" else "매도")
        self.mode = mode
        self.symbol = discord.ui.TextInput(label="종목 (심볼 또는 이름)", placeholder="예: COW 또는 소전자")
        self.qty = discord.ui.TextInput(
            label="수량" + (" (매도는 '전부' 가능)" if mode == "sell" else ""), placeholder="예: 10"
        )
        self.add_item(self.symbol)
        self.add_item(self.qty)

    async def on_submit(self, interaction: discord.Interaction):
        sym = resolve_symbol(self.symbol.value)
        if not sym:
            await interaction.response.send_message("❌ 없는 종목이에요.", ephemeral=True)
            return
        q = self.qty.value.strip().replace(",", "")
        if self.mode == "sell" and q == "전부":
            h = db.execute("SELECT qty FROM holdings WHERE user_id=? AND symbol=?",
                           (interaction.user.id, sym)).fetchone()
            qty = h["qty"] if h else 0
        elif q.isdigit():
            qty = int(q)
        else:
            await interaction.response.send_message("❌ 수량은 숫자로 입력해 주세요.", ephemeral=True)
            return
        if qty <= 0:
            await interaction.response.send_message("❌ 수량은 1 이상이어야 해요.", ephemeral=True)
            return
        get_user(interaction.user.id)
        msg = buy(interaction.user.id, sym, qty) if self.mode == "buy" else sell(interaction.user.id, sym, qty)
        await interaction.response.send_message(msg, ephemeral=True)


class StockView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="매수", style=discord.ButtonStyle.success, emoji="🟢", custom_id="stock:buy")
    async def buy_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(TradeModal("buy"))

    @discord.ui.button(label="매도", style=discord.ButtonStyle.danger, emoji="🔴", custom_id="stock:sell")
    async def sell_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(TradeModal("sell"))

    @discord.ui.button(label="내 자산", style=discord.ButtonStyle.primary, emoji="💼", custom_id="stock:me")
    async def me_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        get_user(interaction.user.id)
        await interaction.response.send_message(embed=my_embed(interaction.user), ephemeral=True)

    @discord.ui.button(label="용돈 받기", style=discord.ButtonStyle.secondary, emoji="💰", custom_id="stock:allowance")
    async def allowance_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        u = get_user(interaction.user.id)
        left = ALLOWANCE_COOLDOWN - (time.time() - u["last_allowance"])
        if left > 0:
            await interaction.response.send_message(
                f"⏳ {int(left // 60) + 1}분 뒤에 다시 받을 수 있어요.", ephemeral=True)
            return
        db.execute("UPDATE users SET money=money+?, last_allowance=? WHERE user_id=?",
                   (ALLOWANCE, time.time(), interaction.user.id))
        db.commit()
        await interaction.response.send_message(f"💰 용돈 {ALLOWANCE:,}원을 받았어요!", ephemeral=True)

    @discord.ui.button(label="랭킹", style=discord.ButtonStyle.secondary, emoji="🏆", custom_id="stock:rank")
    async def rank_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        ids = [r["user_id"] for r in db.execute("SELECT user_id FROM users")]
        board = sorted(((total_assets(i), i) for i in ids), reverse=True)[:10]
        lines = [f"{n}. <@{i}> — {a:,}원" for n, (a, i) in enumerate(board, 1)]
        await interaction.response.send_message(
            embed=discord.Embed(title="🏆 총 자산 랭킹", description="\n".join(lines) or "아직 없어요"),
            ephemeral=True)


# ---------------------------------------------------------------- 봇
class StockBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.view = StockView()

    async def setup_hook(self):
        self.add_view(self.view)  # 재시작해도 버튼이 계속 동작
        self.update_loop.start()

    async def on_ready(self):
        print(f"로그인: {self.user}")
        for guild in self.guilds:
            await self.ensure_panel(guild)

    async def on_guild_join(self, guild: discord.Guild):
        await self.ensure_panel(guild)

    async def ensure_panel(self, guild: discord.Guild):
        channel = discord.utils.get(guild.text_channels, name=CHANNEL_NAME)
        if not channel:
            return
        if db.execute("SELECT 1 FROM panels WHERE channel_id=?", (channel.id,)).fetchone():
            return
        await self.create_panel(channel)

    async def create_panel(self, channel: discord.TextChannel):
        msg = await channel.send(embed=panel_embed(), view=self.view)
        db.execute("INSERT OR REPLACE INTO panels VALUES(?,?)", (channel.id, msg.id))
        db.commit()

    @tasks.loop(seconds=UPDATE_SEC)
    async def update_loop(self):
        tick_prices()
        embed = panel_embed()
        for row in db.execute("SELECT * FROM panels").fetchall():
            channel = self.get_channel(row["channel_id"])
            if channel is None:
                continue
            try:
                await channel.get_partial_message(row["message_id"]).edit(embed=embed, view=self.view)
            except discord.NotFound:  # 패널 메시지가 지워졌으면 다시 만든다
                try:
                    await self.create_panel(channel)
                except discord.HTTPException:
                    pass
            except discord.HTTPException as e:
                print("패널 수정 실패:", e)

    @update_loop.before_loop
    async def before_update(self):
        await self.wait_until_ready()


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN이 없어요. .env 파일을 확인하세요.")
    StockBot().run(TOKEN)
