import logging
from enum import IntEnum
from dataclasses import dataclass
import os

import gevent
from django.core.management.base import BaseCommand, no_translations
from steam.client import SteamClient, SteamID

import dota2
from dota2.client import Dota2Client
from dota2.enums import DOTA_GC_TEAM
from dota2.features.chat import ChatChannel
from dota2.proto_enums import DOTAChatChannelType_t
from dota2.protobufs import dota_gcmessages_client_chat_pb2

from bot.models import LadderQueue, LadderSettings


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

    bot = None
    server = "EU"

    def add_arguments(self, parser):
        pass

    @no_translations
    def handle(self, *args, **options):
        logging.info("we alive")

        credentials = Credentials(login=os.getenv("BOT_LOGIN"), password=os.getenv("BOT_PASSWORD"))

        if not LadderSettings.get_solo().use_queue:
            logging.error("this bot is only usable with ladder queue")
            return

        try:
            gevent.joinall([gevent.spawn(self.bot_loop, credentials)])
        finally:
            logging.info("all greenlets exitted")
            if self.bot:
                self.bot.destroy_lobby()
                self.bot.exit()
                self.bot.steam.logout()
                logging.info("cleanup done")

    def kick_not_in_queue(self, dota: Dota2Client):
        # kick people from teams if they are not in queue
        # players_steam = {
        #     SteamID(player.id).as_32: player
        #     for player in dota.lobby.all_members
        #     if player.id and player.team in (DOTA_GC_TEAM.GOOD_GUYS, DOTA_GC_TEAM.BAD_GUYS)
        # }

        # players_queue = []
        # if self.queue:
        #     players_queue = self.queue.players.all().values_list("dota_id", flat=True)

        # unnecesarry_people = (player for player in players_steam if str(player) not in players_queue)

        # for player in unnecesarry_people:
        #     dota.channels.lobby.send(f"{players_steam[player].name}, you are not in this queue.")
        #     dota.practice_lobby_kick_from_team(player)
        pass

    def invite_players(self, dota: Dota2Client):
        lobby_members = [p.id for p in dota.lobby.all_members]

        players = [p for p in self.queue.players.all()]
        logging.info(f"Inviting players: {players}")

        for player_steam_id in players:
            if player_steam_id not in lobby_members:
                dota.invite_to_lobby(player_steam_id)

    def create_new_lobby(self, dota: Dota2Client):
        dota.lobby_options = {
            "game_name": "dynia testing",
            "game_mode": dota2.enums.DOTA_GameMode.DOTA_GAMEMODE_CM,
            "server_region": game_servers[self.server],
            "leagueid": int(os.getenv("LEAGUE_ID", 0)),
            "fill_with_bots": True,
            "allow_spectating": True,
            "allow_cheats": True,
            "allchat": False,
            # 'dota_tv_delay': LobbyDotaTVDelay.LobbyDotaTV_10,  # TODO: this is LobbyDotaTV_10
            "pause_setting": 0,  # TODO: LobbyDotaPauseSetting_Unlimited
            "do_player_draft": True,
        }

        dota.create_practice_lobby(password=os.getenv("LOBBY_PASSWORD", ""), options=dota.lobby_options)

    def bot_loop(self, credentials):
        logging.info("loop starting")
        client = SteamClient()
        dota = Dota2Client(client)

        logging.info("clients created")

        self.bot = dota
        self.queue = LadderQueue.objects.filter(active=True).first()

        @client.on("logged_on")
        def logged_on():
            dota.launch()

        @dota.on("ready")
        def dota_started():
            logging.info(f"Logged in: {dota.steam.username} {dota.account_id}")

            self.create_new_lobby(dota)

        @dota.on("notready")
        def dota_connection_lost():
            logging.error(f"Dota connection lost: {dota.steam.username} {dota.account_id}")
            delay = 30
            gevent.sleep(delay)

            logging.info("Trying to launch dota again.")
            dota.launch()

        @dota.on(dota2.features.Lobby.EVENT_LOBBY_CHANGED)
        def dota_lobby_changed(lobby: dota2.features.Lobby):
            logging.info(f"lobby state change: {lobby_strings.get(lobby.state, int(lobby.state))}")

            if not lobby:
                logging.warning("Got lobby_changed with None lobby object")
                return

            # TODO: add separate per state handlers
            if int(lobby.state) == LobbyState.UI:
                self.kick_not_in_queue(dota)

        @dota.on(dota2.features.Lobby.EVENT_LOBBY_NEW)
        def dota_lobby_new(lobby: dota2.features.Lobby):
            logging.info(f"found new lobby: {dota.steam.username} {lobby.lobby_id}")

            dota.join_practice_lobby_team()  # jump to unassigned players
            dota.channels.join_lobby_channel()
            self.invite_players(dota)

        @dota.channels.on(dota2.features.chat.ChannelManager.EVENT_JOINED_CHANNEL)
        def chat_joined(channel):
            logging.info(f"{dota.steam.username} joined chat channel {channel.name}")

        @dota.channels.on(dota2.features.chat.ChannelManager.EVENT_MESSAGE)
        def chat_message(channel: ChatChannel, msg: dota_gcmessages_client_chat_pb2.CMsgDOTAChatMessage):
            if channel.type != DOTAChatChannelType_t.DOTAChannelType_Lobby:
                return  # ignore postgame and other chats

            if not msg.text.startswith("!"):
                return

            actions = {
                "!start": lambda: dota.launch_practice_lobby(),
                "!forcestart": lambda: dota.launch_practice_lobby(),
            }

            actions.get(msg.text, lambda: logging.info(f"unsupported command: {msg.text}"))()

        logging.info("attempting login")
        client.login(credentials.login, credentials.password)
        logging.info("we running")
        client.run_forever()

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
