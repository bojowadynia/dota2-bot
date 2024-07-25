import atexit
from collections import deque
import random
import typing
import uuid
import yaml
import logging

# with open("logging.yaml", "rt") as f:
#     logging_config = yaml.safe_load(f.read())
# logging.config.dictConfig(logging_config)
logging.basicConfig(level=logging.DEBUG)

from enum import IntEnum
from dataclasses import dataclass
import os

import gevent
from django.core.management.base import BaseCommand, no_translations
from steam.client import SteamClient, SteamID
from steam.client.builtins.friends import SteamFriendlist

import dota2
import dota2.client
import dota2.features
from dota2.client import Dota2Client
from dota2.enums import DOTA_GC_TEAM
from dota2.proto_enums import EDOTAGCMsg, EGCBaseMsg
from dota2.features.chat import ChatChannel
from dota2.proto_enums import DOTAChatChannelType_t
from dota2.protobufs import dota_gcmessages_client_chat_pb2

from bot.models import LadderQueue


class LobbyState(IntEnum):
    UI = 0
    READYUP = 4
    SERVERSETUP = 1
    RUN = 2
    POSTGAME = 3
    NOTREADY = 5
    SERVERASSIGN = 6


lobby_strings = {
    0: "UI",
    4: "READYUP",
    1: "SERVERSETUP",
    2: "RUN",
    3: "POSTGAME",
    5: "NOTREADY",
    6: "SERVERASSIGN",
}


game_servers = {
    "EU": dota2.enums.EServerRegion.Europe,
    "USE": dota2.enums.EServerRegion.USEast,
    "USW": dota2.enums.EServerRegion.USWest,
    "AU": dota2.enums.EServerRegion.Australia,
    "SEA": dota2.enums.EServerRegion.Singapore,
}


@dataclass
class Credentials:
    login: str
    password: str


class Command(BaseCommand):
    help = "Dota bot"

    def __init__(self):
        super().__init__()
        self.free_bots = {}
        self.busy_bots = {}

    def add_arguments(self, parser):
        pass

    @no_translations
    def handle(self, *args, **options):
        logging.info("we alive")

        creds = [Credentials(login=os.getenv("BOT_LOGIN"), password=os.getenv("BOT_PASSWORD"))]

        coros = [gevent.spawn(self.sync_queue_worker)]
        for cred in creds:
            bot = Bot(free_bots=self.free_bots, busy_bots=self.busy_bots)
            coros.append(gevent.spawn(bot.work, cred))

        try:
            gevent.joinall(coros)
        finally:
            logging.info("all greenlets exitted")
            for bot in self.free_bots.values():
                bot.handle_atexit()
            for bot in self.busy_bots.values():
                bot.handle_atexit()

    # Poll lobby for readiness
    def sync_queue_worker(self):
        while True:
            gevent.sleep(5)
            self.sync_queue()

    def sync_queue(self):
        queues = LadderQueue.objects.filter(active=True)
        for queue in queues:
            count = queue.players.count()
            if count < 1:
                logging.debug(f"queue {queue.id} has not enough players yet: {count}")
                continue

            available_bots = [bot_id for bot_id, bot in self.free_bots.items() if bot.has_lobby_ready()]

            if len(available_bots) < 1:
                logging.debug("no free bots available")
                continue

            bot = self.free_bots.pop(available_bots[0])
            logging.info(f"attaching to bot: {bot.id}")
            bot.attach_queue(queue)
            self.busy_bots[bot.id] = bot


# Bot holds a single dota client and can serve a single queue at a time
class Bot(object):
    # TODO: add typing
    def __init__(self, free_bots: typing.Deque, busy_bots: typing.Dict, server="EU") -> None:
        self.id = uuid.uuid4()
        self.queue = None
        self.free_bots = free_bots
        self.busy_bots = busy_bots
        self.dota_client = None
        self.server = server
        # atexit.register(self.handle_atexit)

    def attach_queue(self, queue: LadderQueue):
        if not self.dota_client:
            logging.error(f"{self.id}: Queue handling attempt on uninitialized bot")
        queue.active = False
        queue.save()
        self.busy_bots[self.id] = self
        self.queue = queue
        logging.info(f"{self.id}: inviting players: {queue.id}")
        self.invite_players(self.dota_client)
        logging.info(f"{self.id}: queue attached: {queue.id}")

    def has_lobby_ready(self) -> bool:
        if not self.dota_client:
            return False
        if not self.dota_client.lobby:
            return False
        return True

    def work(self, credentials: Credentials) -> None:
        steam_client = SteamClient()
        dota_client = Dota2Client(steam_client)
        dota_client.verbose_debug = True

        @steam_client.on("logged_on")
        def logged_on():
            self.handle_steam_logged_on(dota_client)

        @steam_client.on("channel_secured")
        def channel_secured():
            self.handle_steam_channel_secured(steam_client)

        @dota_client.on("notready")
        def dota_connection_lost():
            self.handle_dota_notready(dota_client)

        @dota_client.on("ready")
        def dota_started():
            self.handle_dota_ready(dota_client)

        @dota_client.on(dota2.features.Lobby.EVENT_LOBBY_NEW)
        def dota_lobby_new(lobby: dota2.features.Lobby):
            self.handle_dota_lobby_new(dota_client, lobby)

        @dota_client.on(dota2.features.Lobby.EVENT_LOBBY_CHANGED)
        def dota_lobby_changed(lobby: dota2.features.Lobby):
            self.handle_dota_lobby_changed(dota_client, lobby)

        @dota_client.on(dota2.features.Lobby.EVENT_LOBBY_REMOVED)
        def dota_lobby_removed(lobby: dota2.features.Lobby):
            self.handle_dota_lobby_removed(dota_client)

        @dota_client.channels.on(dota2.features.chat.ChannelManager.EVENT_JOINED_CHANNEL)
        def chat_joined(channel):
            logging.info(f"{dota_client.steam.username} joined chat channel {channel.name}")

        @dota_client.channels.on(dota2.features.chat.ChannelManager.EVENT_MESSAGE)
        def chat_message(channel: ChatChannel, msg: dota_gcmessages_client_chat_pb2.CMsgDOTAChatMessage):
            self.handle_dota_chat_message()

        self.free_bots[self.id] = self

        logging.info(f"{self.id}: attempting login")
        steam_client.login(credentials.login, credentials.password)
        logging.info(f"{self.id}: we running")
        self.dota_client = dota_client
        steam_client.run_forever()

    def handle_steam_logged_on(self, dota_client: Dota2Client):
        dota_client.launch()

    def handle_steam_channel_secured(self, steam_client: SteamClient):
        if steam_client.relogin_available:
            steam_client.relogin()

    def handle_dota_notready(self, dota_client: Dota2Client):
        logging.error(f"Dota connection lost: {dota_client.steam.username} {dota_client.account_id}")
        delay = 30
        gevent.sleep(delay)

        logging.info("Trying to launch dota again.")
        dota_client.launch()

    def handle_dota_ready(self, dota_client: Dota2Client):
        logging.info(f"Logged in: {dota_client.steam.username} {dota_client.account_id}")
        self.create_new_lobby(dota_client)

    def handle_dota_lobby_new(self, dota_client: Dota2Client, lobby: dota2.features.Lobby):
        logging.info(f"found new lobby: {dota_client.steam.username} {lobby.lobby_id}")

        if not dota_client.lobby:
            logging.error(f"{self.id}: No lobby")
            return

        # TODO: might not need join_practice_lobby_team since we jump on coach slot anyways
        dota_client.join_practice_lobby_team()  # jump to spectator slot
        dota_client.send(EDOTAGCMsg.EMsgGCPracticeLobbySetCoach, {})
        dota_client.channels.join_lobby_channel()
        logging.info(f"{self.id}: lobby joined: {lobby.lobby_id}")
        # self.invite_players(dota)

    def handle_dota_lobby_removed(self, dota_client: Dota2Client):
        self.create_new_lobby(dota_client)

    def handle_dota_lobby_changed(self, dota_client: Dota2Client, lobby: dota2.features.Lobby):
        logging.info(f"lobby state change: {lobby_strings.get(lobby.state, int(lobby.state))}")

        if int(lobby.state) == LobbyState.UI:
            self.handle_dota_lobby_ui(dota_client)

        if int(lobby.state) == LobbyState.RUN:
            self.handle_dota_lobby_run(dota_client)

        if int(lobby.state) == LobbyState.POSTGAME:
            self.handle_dota_lobby_postgame(dota_client)

    def handle_dota_lobby_ui(self, dota_client: Dota2Client):
        logging.info(f"{self.id}: lobby created")

    def handle_dota_lobby_run(self, dota_client: Dota2Client):
        logging.info(f"{self.id}: lobby started: {self.queue.id}")

    def handle_dota_lobby_postgame(self, dota_client: Dota2Client):
        self.busy_bots.pop(self.id)

    def handle_dota_chat_message(
        self, dota_client: Dota2Client, channel: ChatChannel, msg: dota_gcmessages_client_chat_pb2.CMsgDOTAChatMessage
    ):
        if channel.type != DOTAChatChannelType_t.DOTAChannelType_Lobby:
            return  # ignore postgame and other chats

        if not msg.text.startswith("!"):
            return

        actions = {
            "!start": Command.start_game,
            "!forcestart": Command.force_start_game,
        }

        actions.get(msg.text, self.handle_dota_cmd_unsupported)(dota_client)

    def handle_dota_cmd_unsupported(self, dota_client: Dota2Client):
        pass

    def handle_dota_cmd_start(self, dota_client: Dota2Client):
        dota_client.launch_practice_lobby()

    def handle_dota_cmd_forcestart(self, dota_client: Dota2Client):
        dota_client.launch_practice_lobby()

    def create_new_lobby(self, dota_client: Dota2Client):
        self.lobby_options = {
            "game_name": "dynia testing",
            "game_mode": dota2.enums.DOTA_GameMode.DOTA_GAMEMODE_CM,
            "server_region": game_servers[self.server],
            "leagueid": int(os.getenv("LEAGUE_ID", 0)),
            "fill_with_bots": True,
            "allow_spectating": True,
            "allow_cheats": True,
            "allchat": True,
            # 'dota_tv_delay': LobbyDotaTVDelay.LobbyDotaTV_10,  # TODO: this is LobbyDotaTV_10
            "pause_setting": 0,  # TODO: LobbyDotaPauseSetting_Unlimited
            "do_player_draft": True,
        }

        # TODO: send to players via steam message?
        self.lobby_password = generate_lobby_password()

        dota_client.create_practice_lobby(password=self.lobby_password, options=self.lobby_options)

    def invite_players(self, dota_client: Dota2Client):
        # TODO: this could use some state, make in memory lobby abstraction
        lobby_members = [p.id for p in dota_client.lobby.all_members]
        logging.info(f"lobby_members: {lobby_members}")

        # TODO: either use dota_id or steam_id everywhere
        players = [SteamID(p.dota_id) for p in self.queue.players.all()]
        logging.info(f"Inviting players: {players}")

        for player_steam_id in players:
            if player_steam_id not in lobby_members:
                logging.info(f"inviting: {player_steam_id}")
                dota_client.invite_to_lobby(player_steam_id)

    def cleanup(self):
        self.queue = None
        if not self.dota_client:
            logging.error("cleanup called on bot which is not ready")
            return

        # Callback on removed lobby will create a new one
        # when queue gets attached lobby name needs adjustment
        # could also trigger lobby creation routine when 10 players are available
        self.dota_client.destroy_lobby()

    def handle_atexit(self):
        if not self.dota_client:
            return
        self.dota_client.destroy_lobby()
        self.dota_client.exit()
        self.dota_client.steam.logout()

    def validate_lobby(self, dota_client: Dota2Client):
        if not self.queue:
            return

        radiant = {}
        dire = {}
        lobby = {}
        for player in dota_client.lobby.all_members:
            player_id = SteamID(player.id).as_32
            if player.team == DOTA_GC_TEAM.GOOD_GUYS:
                radiant[player_id] = player
            if player.team == DOTA_GC_TEAM.BAD_GUYS:
                dire[player_id] = player
            if player.team == DOTA_GC_TEAM.PLAYER_POOL:
                lobby[player_id] = player

        players_queue = self.queue.players.all().values_list("dota_id", "ladder_mmr", flat=True)

        if len(players_queue) < 2:
            return

        players_queue.sort(key=lambda x: x[1])
        captains = players_queue[-2:]
        queue_players = {SteamID(player.id).as_32: player for player in players_queue[:8]}

        random.shuffle(captains)
        radiant_cap, dire_cap = captains

        for player_id, player in radiant:
            if player_id != SteamID(radiant_cap.id).as_32:
                dota_client.practice_lobby_kick_from_team(player_id)

        for player_id, player in dire:
            if player_id != SteamID(dire_cap.id).as_32:
                dota_client.practice_lobby_kick_from_team(player_id)

        for player_id, player in lobby:
            if player_id == SteamID(radiant_cap.id).as_32:
                dota_client.channels.lobby.send(
                    f"{radiant_cap.name}, you are a radiant captain. Move to a slot in radiant team"
                )
                continue
            if player_id == SteamID(dire_cap.id).as_32:
                dota_client.channels.lobby.send(f"{dire_cap.name}, you are a dire captain. Move to a slot in dire team")
                continue
            if player_id not in queue_players:
                dota_client.channels.lobby.send(f"{player.name}, you are not in this queue.")
                gevent.sleep(2)
                dota_client.practice_lobby_kick(player_id)

    # def handle_cmd_start(lobby: Lobby, msg):
    #     lobby.start_votes[msg.account_id] = True

    #     votes_needed = 2
    #     if len(lobby.start_votes) < 2:
    #         lobby.dota_client.channels.lobby.send(
    #             "I need {} more votes to start.".format(votes_needed - len(bot.start_votes))
    #         )
    #         return

    #     lobby.dota_client.channels.lobby.send("Ok, let's go! GL HF")
    #     gevent.sleep(2)

    # def start_game(lobby: Lobby):
    #     lobby.game_start_time = timezone.now()

    #     # with db.Session(lobby.db_engine) as session:
    #     # session.

    #     # if bot.queue:
    #     #     bot.queue.active = False
    #     #     bot.queue.game_start_time = bot.game_start_time
    #     #     bot.queue.save()

    #     lobby.dota_client.launch_practice_lobby()


def generate_lobby_password() -> str:
    # guaranteed by a fair dice roll
    return "topkek"
