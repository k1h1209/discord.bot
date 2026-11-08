import asyncio
import ipaddress
import json
import logging
import os
import re
import socket
import tempfile
from dataclasses import dataclass, field
from typing import Optional

import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv
from mcstatus import JavaServer

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
# 개발 중에 슬래시 명령어를 바로 반영하고 싶을 때만 .env에 서버(길드) ID를 적어요 (선택)
DEV_GUILD_ID = os.getenv("DEV_GUILD_ID")

# 확인 주기(초)
CHECK_INTERVAL = 60
# True  = 서버 상태(열림/닫힘/인원 등)가 바뀔 때만 메시지 수정
# False = 확인할 때마다 항상 메시지 수정
EDIT_ONLY_ON_CHANGE = True

CONFIG_FILE = "config.json"
CHANNEL_NAME = "마크서버-상태"

log = logging.getLogger("athenos")

# ---------------------------------------------------------------------------
# 기본값 / 상수
# ---------------------------------------------------------------------------

DEFAULT_CUSTOM = {
    "title": "",  # 비우면 서버 이름을 제목으로 써요
    "description": "",
    "open_text": "🟢 열려있음",
    "closed_text": "🔴 닫혀있음",
    "starting_text": "🟡 켜지는 중",
    "open_color": "57F287",
    "closed_color": "ED4245",
    "starting_color": "FEE75C",
    "show": ["players", "address", "updated"],
}

SHOW_OPTIONS = {
    "players": "접속자 수",
    "player_list": "접속 중인 플레이어 목록",
    "address": "서버 주소",
    "version": "버전",
    "ping": "응답 속도",
    "updated": "마지막 업데이트 시간",
}

STATE_LABELS = {"open": "열림", "starting": "켜지는 중", "closed": "닫힘"}

REQUIRED_PERMS = {
    "view_channel": "채널 보기",
    "send_messages": "메시지 보내기",
    "embed_links": "링크 첨부",
    "read_message_history": "메시지 기록 보기",
}

ADDR_RE = re.compile(r"^(?P<host>[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?)(?::(?P<port>\d{1,5}))?$")
COLOR_RE = re.compile(r"^#?([0-9a-fA-F]{6})$")


def new_custom() -> dict:
    return {**DEFAULT_CUSTOM, "show": list(DEFAULT_CUSTOM["show"])}


# ---------------------------------------------------------------------------
# 순수 함수 (디스코드와 상관없는 부분)
# ---------------------------------------------------------------------------


def parse_address(raw: str):
    """'host' 또는 'host:port' 를 (host, port) 로 바꿔요. 형식이 틀리면 None."""
    text = re.sub(r"^[a-zA-Z]+://", "", raw.strip()).rstrip("/")
    match = ADDR_RE.match(text)
    if not match:
        return None
    port = int(match["port"]) if match["port"] else 25565
    if not 1 <= port <= 65535:
        return None
    return match["host"].lower(), port


def parse_color(value: str) -> Optional[int]:
    match = COLOR_RE.match(value.strip())
    return int(match.group(1), 16) if match else None


async def resolves_to_private(host: str):
    """주소가 내부망/자기 자신을 가리키면 True, 공개 주소면 False, 확인 불가면 None."""
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not ip.is_global:
            return True
    return False


@dataclass
class Status:
    state: str  # "open" | "closed" | "starting"
    online: int = 0
    max: int = 0
    version: str = ""
    latency: float = 0.0
    players: tuple = ()


async def check_server(host: str, port: int) -> Status:
    try:
        server = JavaServer(host, port, timeout=5)
        status = await server.async_status()
    except Exception:
        return Status("closed")

    version = status.version.name or ""
    motd = status.motd.to_plain()
    text = f"{version} {motd}".lower()

    # Aternos는 꺼져있을 때 "Offline" 이라고 응답하는 경우가 있어요
    if "offline" in text:
        return Status("closed")
    if any(word in text for word in ("starting", "loading", "preparing")):
        return Status("starting")

    sample = tuple(p.name for p in (status.players.sample or []))
    return Status("open", status.players.online, status.players.max, version, status.latency, sample)


class Store:
    """서버(길드)별 설정을 config.json 에 저장해요."""

    def __init__(self, path: str):
        self.path = path
        self.data: dict = {}
        self.load()

    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                self.data = json.load(f)
        except FileNotFoundError:
            self.data = {}
        except (json.JSONDecodeError, OSError):
            log.exception("%s 를 읽지 못했어요. 백업(.bak)으로 옮기고 새로 시작해요.", self.path)
            try:
                os.replace(self.path, self.path + ".bak")
            except OSError:
                pass
            self.data = {}

    def save(self):
        directory = os.path.dirname(os.path.abspath(self.path))
        fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except OSError:
            log.exception("설정 저장 실패")
            try:
                os.remove(tmp)
            except OSError:
                pass

    def set(self, guild_id: str, cfg: dict):
        self.data[guild_id] = cfg
        self.save()

    def pop(self, guild_id: str) -> Optional[dict]:
        cfg = self.data.pop(guild_id, None)
        if cfg is not None:
            self.save()
        return cfg


# ---------------------------------------------------------------------------
# 디스코드 쪽
# ---------------------------------------------------------------------------


class StatusBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.last_snapshot: dict = {}

    async def setup_hook(self):
        try:
            if DEV_GUILD_ID:
                guild = discord.Object(id=int(DEV_GUILD_ID))
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
            else:
                await self.tree.sync()
        except discord.HTTPException:
            log.exception("슬래시 명령어 등록에 실패했어요")
        poll_status.start()

    async def on_ready(self):
        log.info("로그인 완료: %s", self.user)

    async def on_guild_remove(self, guild: discord.Guild):
        store.pop(str(guild.id))
        self.last_snapshot.pop(str(guild.id), None)


client = StatusBot()
store = Store(CONFIG_FILE)


def render_embed(cfg: dict, status: Status) -> discord.Embed:
    custom = {**DEFAULT_CUSTOM, **cfg.get("custom", {})}
    show = custom["show"]
    state = status.state

    color = parse_color(custom[f"{state}_color"])
    title = (custom["title"] or cfg["name"])[:256]
    description = f"**{custom[f'{state}_text']}**"
    if custom["description"]:
        description += f"\n\n{custom['description']}"

    embed = discord.Embed(title=title, description=description[:4096], color=color or 0x99AAB5)

    if state == "open":
        if "players" in show:
            embed.add_field(name="접속자", value=f"{status.online} / {status.max}명")
        if "version" in show and status.version:
            embed.add_field(name="버전", value=status.version[:1024])
        if "ping" in show:
            embed.add_field(name="응답 속도", value=f"{status.latency:.0f}ms")
        if "player_list" in show:
            if status.players:
                names = ", ".join(discord.utils.escape_markdown(n) for n in status.players)
            elif status.online:
                names = "목록을 알 수 없어요"
            else:
                names = "없음"
            embed.add_field(name="접속 중인 플레이어", value=names[:1024], inline=False)
    if "address" in show:
        embed.add_field(name="주소", value=f"`{cfg['host']}:{cfg['port']}`", inline=False)
    if "updated" in show:
        embed.timestamp = discord.utils.utcnow()
        embed.set_footer(text="마지막 업데이트")
    return embed


def sample_status(state: str) -> Status:
    if state == "open":
        return Status("open", 2, 20, "Paper 1.21", 42.0, ("Steve", "Alex"))
    return Status(state)


def missing_perms(channel, me) -> list:
    perms = channel.permissions_for(me)
    return [label for attr, label in REQUIRED_PERMS.items() if not getattr(perms, attr)]


async def update_message(guild_id: str, cfg: dict, status: Status, force: bool = False):
    snapshot = (status.state, status.online, status.max, status.players, status.version)
    if EDIT_ONLY_ON_CHANGE and not force and client.last_snapshot.get(guild_id) == snapshot:
        return

    channel = client.get_channel(cfg["channel_id"])
    if channel is None:
        try:
            channel = await client.fetch_channel(cfg["channel_id"])
        except discord.NotFound:
            log.warning("서버 %s: 상태 채널이 삭제되어 설정을 지워요", guild_id)
            store.pop(guild_id)
            return
        except discord.HTTPException:
            return

    embed = render_embed(cfg, status)
    try:
        await channel.get_partial_message(cfg["message_id"]).edit(embed=embed)
    except discord.NotFound:
        # 누가 메시지를 지웠다면 새로 보내요
        message = await channel.send(embed=embed)
        cfg["message_id"] = message.id
        store.save()
    except discord.Forbidden:
        log.warning("서버 %s: 상태 채널에 쓸 권한이 없어요", guild_id)
        return
    client.last_snapshot[guild_id] = snapshot


@tasks.loop(seconds=CHECK_INTERVAL)
async def poll_status():
    entries = list(store.data.items())
    if not entries:
        return
    targets = list({(cfg["host"], cfg["port"]) for _, cfg in entries})
    results = await asyncio.gather(*(check_server(h, p) for h, p in targets))
    by_target = dict(zip(targets, results))
    for guild_id, cfg in entries:
        try:
            await update_message(guild_id, cfg, by_target[(cfg["host"], cfg["port"])])
        except Exception:
            log.exception("서버 %s 상태 갱신 실패", guild_id)


@poll_status.before_loop
async def before_poll():
    await client.wait_until_ready()


# ---------------------------------------------------------------------------
# 설정 세션과 UI
# ---------------------------------------------------------------------------


@dataclass
class Session:
    owner_id: int
    guild: discord.Guild
    host: str
    port: int
    name: str
    want_custom: bool
    interaction: discord.Interaction
    channel: Optional[discord.TextChannel] = None  # None 이면 '생성' 모드
    custom: dict = field(default_factory=new_custom)
    preview_state: str = "open"


class OwnedView(discord.ui.View):
    def __init__(self, session: Session):
        super().__init__(timeout=600)
        self.session = session

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.session.owner_id:
            await interaction.response.send_message("명령어를 입력한 사람만 쓸 수 있어요.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        try:
            await self.session.interaction.edit_original_response(
                content="⏱️ 시간이 지나서 설정이 취소됐어요. 명령어를 다시 입력해 주세요.", embed=None, view=None
            )
        except discord.HTTPException:
            pass


def preview_embed(session: Session) -> discord.Embed:
    cfg = {"name": session.name, "host": session.host, "port": session.port, "custom": session.custom}
    return render_embed(cfg, sample_status(session.preview_state))


CUSTOM_HELP = (
    "🎨 **상태 표시 꾸미기**\n"
    "아래 미리보기를 보면서 문구, 색상, 표시 항목을 바꿔 보세요. 숫자와 플레이어 이름은 예시예요.\n"
    "다 됐으면 **완료**를 눌러 주세요."
)


async def advance(interaction: discord.Interaction, session: Session):
    """채널이 정해진 뒤: 커스텀 창으로 가거나 바로 마무리해요. (interaction 은 defer 된 상태)"""
    if session.want_custom:
        view = CustomizeView(session)
        await interaction.edit_original_response(content=CUSTOM_HELP, embed=preview_embed(session), view=view)
    else:
        await finalize(interaction, session)


async def finalize(interaction: discord.Interaction, session: Session):
    guild = session.guild
    guild_id = str(guild.id)
    channel = session.channel
    created = False

    async def fail(text: str):
        await interaction.edit_original_response(content=text, embed=None, view=None)

    if channel is None:
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(send_messages=False, add_reactions=False),
            guild.me: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, embed_links=True, read_message_history=True
            ),
        }
        try:
            channel = await guild.create_text_channel(
                CHANNEL_NAME,
                overwrites=overwrites,
                topic=f"{session.name} 서버 상태",
                reason=f"{interaction.user} 님이 마인크래프트 서버 상태 채널을 만들었어요",
            )
            created = True
        except discord.Forbidden:
            await fail("❌ 채널을 만들 권한이 없어요. 봇에 **채널 관리** 권한을 주거나, 채널을 '설정'으로 골라 주세요.")
            return
        except discord.HTTPException:
            log.exception("채널 생성 실패")
            await fail("❌ 채널을 만들지 못했어요. 잠시 후 다시 시도해 주세요.")
            return

    missing = missing_perms(channel, guild.me)
    if missing:
        await fail(f"❌ 봇이 {channel.mention} 에서 필요한 권한이 없어요: {', '.join(missing)}")
        return

    cfg = {
        "channel_id": channel.id,
        "message_id": 0,
        "host": session.host,
        "port": session.port,
        "name": session.name,
        "custom": session.custom,
        "created_channel": created,
    }
    status = await check_server(session.host, session.port)
    try:
        message = await channel.send(embed=render_embed(cfg, status))
    except discord.HTTPException:
        log.exception("상태 메시지 전송 실패")
        await fail(f"❌ {channel.mention} 에 메시지를 보내지 못했어요. 봇 권한을 확인해 주세요.")
        return
    cfg["message_id"] = message.id

    old = store.data.get(guild_id)
    store.set(guild_id, cfg)
    client.last_snapshot[guild_id] = (status.state, status.online, status.max, status.players, status.version)

    # 이전에 설정해 둔 상태 메시지가 있으면 지워요
    if old and old.get("message_id") and old["message_id"] != message.id:
        old_channel = client.get_channel(old["channel_id"])
        if old_channel is not None:
            try:
                await old_channel.get_partial_message(old["message_id"]).delete()
            except discord.HTTPException:
                pass

    label = STATE_LABELS[status.state]
    note = "" if status.state != "closed" else "\n(지금은 서버가 닫혀있거나 응답이 없어서 '닫힘'으로 보여요. 주소를 확인해 주세요.)"
    await interaction.edit_original_response(
        content=(
            f"✅ 설정 완료! {channel.mention} 에 `{session.host}:{session.port}` 상태를 표시해요. "
            f"(지금 상태: {label}, {CHECK_INTERVAL}초마다 확인){note}"
        ),
        embed=None,
        view=None,
    )


class ChannelPickView(OwnedView):
    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        channel_types=[discord.ChannelType.text],
        placeholder="상태를 표시할 채널을 골라 주세요",
        min_values=1,
        max_values=1,
    )
    async def pick(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        await interaction.response.defer()
        self.session.interaction = interaction
        channel = interaction.guild.get_channel(select.values[0].id)
        if channel is None:
            await interaction.followup.send("❌ 그 채널을 찾을 수 없어요. 다른 채널을 골라 주세요.", ephemeral=True)
            return
        missing = missing_perms(channel, interaction.guild.me)
        if missing:
            await interaction.followup.send(
                f"❌ 봇이 {channel.mention} 에서 필요한 권한이 없어요: {', '.join(missing)}\n"
                "채널 권한을 고치거나 다른 채널을 골라 주세요.",
                ephemeral=True,
            )
            return
        self.session.channel = channel
        self.stop()
        await advance(interaction, self.session)

    @discord.ui.button(label="취소", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        self.stop()
        await interaction.edit_original_response(content="설정을 취소했어요.", embed=None, view=None)


class ShowSelect(discord.ui.Select):
    def __init__(self, session: Session):
        options = [
            discord.SelectOption(label=label, value=key, default=key in session.custom["show"])
            for key, label in SHOW_OPTIONS.items()
        ]
        super().__init__(placeholder="표시할 항목 고르기", min_values=0, max_values=len(options), options=options, row=0)
        self.session = session

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        self.session.interaction = interaction
        self.session.custom["show"] = [key for key in SHOW_OPTIONS if key in self.values]
        for option in self.options:
            option.default = option.value in self.values
        await interaction.edit_original_response(embed=preview_embed(self.session), view=self.view)


class TextsModal(discord.ui.Modal):
    def __init__(self, session: Session, panel: "CustomizeView"):
        super().__init__(title="상태 문구 편집", timeout=600)
        self.session = session
        self.panel = panel
        c = session.custom
        self.f_title = discord.ui.TextInput(
            label="제목 (비우면 서버 이름)", default=c["title"], required=False, max_length=100
        )
        self.f_open = discord.ui.TextInput(label="열려 있을 때 문구", default=c["open_text"], max_length=50)
        self.f_closed = discord.ui.TextInput(label="닫혀 있을 때 문구", default=c["closed_text"], max_length=50)
        self.f_starting = discord.ui.TextInput(label="켜지는 중일 때 문구", default=c["starting_text"], max_length=50)
        self.f_desc = discord.ui.TextInput(
            label="추가 설명 (선택)",
            default=c["description"],
            required=False,
            style=discord.TextStyle.paragraph,
            max_length=500,
        )
        for item in (self.f_title, self.f_open, self.f_closed, self.f_starting, self.f_desc):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        self.session.interaction = interaction
        c = self.session.custom
        c["title"] = self.f_title.value.strip()
        c["open_text"] = self.f_open.value.strip() or DEFAULT_CUSTOM["open_text"]
        c["closed_text"] = self.f_closed.value.strip() or DEFAULT_CUSTOM["closed_text"]
        c["starting_text"] = self.f_starting.value.strip() or DEFAULT_CUSTOM["starting_text"]
        c["description"] = self.f_desc.value.strip()
        await interaction.edit_original_response(embed=preview_embed(self.session), view=self.panel)


class ColorsModal(discord.ui.Modal):
    def __init__(self, session: Session, panel: "CustomizeView"):
        super().__init__(title="상태 색상 편집", timeout=600)
        self.session = session
        self.panel = panel
        c = session.custom
        self.f_open = discord.ui.TextInput(label="열림 색 (예: 57F287)", default=c["open_color"], min_length=6, max_length=7)
        self.f_closed = discord.ui.TextInput(label="닫힘 색 (예: ED4245)", default=c["closed_color"], min_length=6, max_length=7)
        self.f_starting = discord.ui.TextInput(label="켜지는 중 색 (예: FEE75C)", default=c["starting_color"], min_length=6, max_length=7)
        for item in (self.f_open, self.f_closed, self.f_starting):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        values = [self.f_open.value, self.f_closed.value, self.f_starting.value]
        parsed = [COLOR_RE.match(v.strip()) for v in values]
        if not all(parsed):
            await interaction.response.send_message(
                "❌ 색상은 `57F287` 처럼 16진수 6자리로 입력해 주세요. (앞에 `#`는 있어도 돼요) 색상은 바뀌지 않았어요.",
                ephemeral=True,
            )
            return
        await interaction.response.defer()
        self.session.interaction = interaction
        c = self.session.custom
        c["open_color"], c["closed_color"], c["starting_color"] = (m.group(1).upper() for m in parsed)
        await interaction.edit_original_response(embed=preview_embed(self.session), view=self.panel)


class CustomizeView(OwnedView):
    def __init__(self, session: Session):
        super().__init__(session)
        self.add_item(ShowSelect(session))

    @discord.ui.button(label="문구 편집", emoji="✏️", style=discord.ButtonStyle.primary, row=1)
    async def edit_texts(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(TextsModal(self.session, self))

    @discord.ui.button(label="색상 편집", emoji="🎨", style=discord.ButtonStyle.primary, row=1)
    async def edit_colors(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ColorsModal(self.session, self))

    @discord.ui.button(label="미리보기 상태 바꾸기", emoji="🔁", style=discord.ButtonStyle.secondary, row=1)
    async def cycle_preview(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        self.session.interaction = interaction
        order = ["open", "starting", "closed"]
        self.session.preview_state = order[(order.index(self.session.preview_state) + 1) % len(order)]
        await interaction.edit_original_response(embed=preview_embed(self.session), view=self)

    @discord.ui.button(label="완료", emoji="✅", style=discord.ButtonStyle.success, row=2)
    async def done(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        self.session.interaction = interaction
        self.stop()
        await finalize(interaction, self.session)

    @discord.ui.button(label="취소", style=discord.ButtonStyle.danger, row=2)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        self.stop()
        await interaction.edit_original_response(content="설정을 취소했어요.", embed=None, view=None)


# ---------------------------------------------------------------------------
# 슬래시 명령어:  /마인크래프트 서버 상태설정
# ---------------------------------------------------------------------------

mc_group = app_commands.Group(
    name="마인크래프트",
    description="마인크래프트 서버 관련 명령어",
    guild_only=True,
    default_permissions=discord.Permissions(manage_guild=True),
)
server_group = app_commands.Group(name="서버", description="마인크래프트 서버 상태 표시", parent=mc_group)
client.tree.add_command(mc_group)


def can_manage(interaction: discord.Interaction) -> bool:
    perms = interaction.permissions
    return perms.manage_guild or perms.administrator


@server_group.command(name="상태설정", description="마인크래프트 서버 상태를 보여줄 채널과 서버 주소를 설정해요")
@app_commands.rename(channel_mode="채널", address="주소", want_custom="챗커스텀", server_name="이름")
@app_commands.describe(
    channel_mode="상태를 보여줄 채널을 새로 만들지, 이미 있는 채널을 고를지",
    address="마인크래프트 서버 주소 (예: abc.aternos.me 또는 abc.aternos.me:12345)",
    want_custom="True면 상태 표시를 꾸미는 창을 띄워요 (기본값: False)",
    server_name="상태 메시지 제목에 쓸 서버 이름 (비우면 주소를 써요)",
)
@app_commands.choices(
    channel_mode=[
        app_commands.Choice(name="생성", value="create"),
        app_commands.Choice(name="설정", value="select"),
    ]
)
async def setup_status(
    interaction: discord.Interaction,
    channel_mode: app_commands.Choice[str],
    address: str,
    want_custom: bool = False,
    server_name: Optional[str] = None,
):
    if not can_manage(interaction):
        await interaction.response.send_message("이 명령어는 **서버 관리** 권한이 있는 사람만 쓸 수 있어요.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)

    parsed = parse_address(address)
    if parsed is None:
        await interaction.edit_original_response(
            content="❌ 주소 형식이 올바르지 않아요. `abc.aternos.me` 또는 `abc.aternos.me:12345` 처럼 입력해 주세요."
        )
        return
    host, port = parsed

    if await resolves_to_private(host):
        await interaction.edit_original_response(content="❌ 내부망이나 자기 자신을 가리키는 주소는 쓸 수 없어요.")
        return

    session = Session(
        owner_id=interaction.user.id,
        guild=interaction.guild,
        host=host,
        port=port,
        name=(server_name or host).strip()[:100],
        want_custom=want_custom,
        interaction=interaction,
    )

    if channel_mode.value == "select":
        await interaction.edit_original_response(
            content="📺 상태를 표시할 채널을 골라 주세요.", view=ChannelPickView(session)
        )
    else:
        await advance(interaction, session)


@server_group.command(name="상태삭제", description="설정해 둔 마인크래프트 서버 상태 표시를 지워요")
async def remove_status(interaction: discord.Interaction):
    if not can_manage(interaction):
        await interaction.response.send_message("이 명령어는 **서버 관리** 권한이 있는 사람만 쓸 수 있어요.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    guild_id = str(interaction.guild.id)
    cfg = store.pop(guild_id)
    client.last_snapshot.pop(guild_id, None)
    if cfg is None:
        await interaction.edit_original_response(content="설정된 상태 표시가 없어요.")
        return
    channel = client.get_channel(cfg["channel_id"])
    if channel is not None:
        try:
            await channel.get_partial_message(cfg["message_id"]).delete()
        except discord.HTTPException:
            pass
    await interaction.edit_original_response(content="🗑️ 상태 표시를 지웠어요. (채널은 그대로 두었어요)")


@client.tree.error
async def on_tree_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    log.error("명령어 오류", exc_info=error)
    text = "❌ 오류가 났어요. 잠시 후 다시 시도해 주세요."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)
    except discord.HTTPException:
        pass


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit(".env 파일에 DISCORD_TOKEN 이 없어요.")
    client.run(TOKEN)
