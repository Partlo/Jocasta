import datetime
import re
from typing import Tuple
import time
import json
from discord import Message, Game
from discord.abc import GuildChannel
from discord.channel import TextChannel, DMChannel
from discord.ext import commands, tasks

import pywikibot
from auth import build_auth
from archiver import Archiver, ArchiveCommand, ArchiveResult
from common import ArchiveException, build_analysis_response, clean_text, log, error_log
from data.filenames import *
from data.nom_data import NOM_TYPES
from nomination_processor import add_categories_to_nomination, load_current_nominations, check_for_new_nominations
from objection import check_active_nominations, check_active_nominations_on_page
from rankings import update_rankings_table
from version_reader import report_version_info
from twitter import TwitterBot


CADE = 346767878005194772
MONITOR = 268478587651358721
MAIN = "wookieepedia"
COMMANDS = "bot-commands"
NOM_CHANNEL = "article-nominations"
SOCIAL_MEDIA = "social-media-team"

THUMBS_UP = "👍"
TIMER = "⏲️"
EXCLAMATION = "❗"


class JocastaBot(commands.Bot):
    """
    :type channels: dict[str, GuildChannel]
    :type emoji_storage: dict[str, int]
    :type analysis_cache: dict[str, dict[str, tuple[int, float]]]
    :type current_nominations: dict[str, list[str]]
    """

    def __init__(self, *, loop=None, **options):
        super().__init__("", loop=loop, **options)
        log("JocastaBot online!")

        self.refresh = 0
        self.objection_schedule_count = 0
        self.successful_count = 0
        self.initial_run_nom = True
        self.initial_run_twitter = True

        self.version = None
        with open(VERSION_FILE, "r") as f:
            self.version = f.readline()

        self.twitter_bot = TwitterBot(auth=build_auth())
        self.channels = {}
        self.emoji_storage = {}

        self.archiver = Archiver(test_mode=False, auto=True)
        self.current_nominations = {}
        self.project_data = {}
        self.signatures = {}
        self.user_message_data = {}

        self.analysis_cache = {"CA": {}, "GA": {}, "FA": {}}

        self.scheduled_check_for_new_nominations.start()
        self.scheduled_check_for_objections.start()
        self.post_to_twitter.start()

    async def on_ready(self):
        log(f'Jocasta on as {self.user}!')

        if self.version:
            await self.change_presence(activity=Game(name=f"ArchivalSystem v. {self.version}"))
            log(f"Running version {self.version}")
        else:
            error_log("No version found")

        site = pywikibot.Site(user="JocastaBot")
        self.reload_project_data(site)
        self.reload_user_message_data(site)
        self.reload_signatures(site)
        log("Loading current nomination list")
        self.current_nominations = load_current_nominations(site)

        for c in self.get_all_channels():
            self.channels[c.name] = c

        for e in self.emojis:
            self.emoji_storage[e.name.lower()] = e.id

        try:
            info = report_version_info(self.archiver.site, self.version)
            if info:
                await self.text_channel(COMMANDS).send(info)
        except Exception as e:
            error_log(type(e), e)

        await self.run_analysis()

        # for message in await self.text_channel("wookieepedia").history(limit=10).flatten():
        #     if message.id == 876670646313185310:
        #         await message.edit(content=":waves hands: you saw.... nothing")
        #     if message.author.id == MONITOR:
        #         print(message.content)
        #         await self.handle_new_nomination(message)

        # for message in await self.text_channel("novels").history(limit=50).flatten():
        #     if message.id not in [878396823713251338, 878396831829205052, 878396840226222134, 878396849659207700, 878610163811123290]:
        #         continue
        #     content = message.content.replace("<<", "<").replace(">>", ">")
        #     await message.edit(content=content)

    # noinspection PyTypeChecker
    def text_channel(self, name) -> TextChannel:
        return self.channels[name]

    def emoji_by_name(self, name):
        if self.emoji_storage.get(name.lower()):
            return self.get_emoji(self.emoji_storage[name.lower()])
        return name

    def is_mention(self, message: Message):
        for mention in message.mentions:
            if mention == self.user:
                return True
        return False

    async def join_channels(self):
        channel = self.text_channel("wookieeprojects")
        for message in await channel.history().flatten():
            if message.id == 833046509494075422:
                for reaction in message.reactions:
                    if reaction.me:
                        await message.remove_reaction(reaction.emoji, self.user)
                        # time.sleep(1)
                        # await message.add_reaction(reaction.emoji)
                    else:
                        await message.add_reaction(reaction.emoji)

    async def find_nomination(self, nomination):
        for message in await self.text_channel(NOM_CHANNEL).history(limit=25).flatten():
            if message.author.id == MONITOR:
                if re.search("New .*?(Featured|Good|Comprehensive) article nomination", message.content):
                    log("Found: ", message.content)
                if nomination in message.content.replace("_", " "):
                    await self.handle_new_nomination_report(message)
                    return True
        return False

    commands = {
        "is_reload_command": "handle_reload_command",
        "is_update_rankings_command": "handle_update_rankings_command",
        "is_analyze_command": "handle_analyze_command",
        "is_project_status_command":  "handle_project_status_command",
        "is_talk_page_command": "handle_talk_page_command",
        "is_new_nomination_command": "handle_new_nomination_command",
        "is_check_nominations_command": "check_for_new_nominations",
        "is_check_objections_command": "handle_check_objections_command"
    }

    async def on_message(self, message: Message):
        if message.author == self.user:
            return
        elif isinstance(message.channel, DMChannel):
            await self.handle_direct_message(message)
            return
        elif not (self.is_mention(message) or "@JocastaBot" in message.content):
            return

        log(f'Message from {message.author} in {message.channel}: [{message.content}]')

        if "Hello!" in message.content:
            await message.channel.send("Hello there!")
            return

        if "list all commands" in message.content:
            await self.update_command_messages()
            return

        for identifier, handler in self.commands.items():
            command_dict = getattr(self, identifier)(message)
            if command_dict:
                await getattr(self, handler)(message, command_dict)
                return

        command = self.is_archive_command(message)
        if command and command.test_mode:
            await self.handle_test_command(message, command)
        elif command:
            await self.handle_archive_command(message, command)
            return

    async def handle_direct_message(self, message: Message):
        if message.content == "join channels":
            await self.join_channels()
            return

        if message.author.id != CADE:
            return

        if message.content == "list all commands":
            await self.update_command_messages()
            return

        m = re.search("check for nomination: (.*?)$", message.content)
        if m:
            result = await self.find_nomination(m.group(1))
            if result:
                await message.add_reaction(THUMBS_UP)
            else:
                await message.add_reaction(EXCLAMATION)
            return

        project_command = self.is_project_status_command(message)
        if project_command:
            log(f"Project Command: {message.content}")
            await self.handle_project_status_command(message, project_command)
            return

        match = re.search("message #(?P<channel>.*?): (?P<text>.*?)$", message.content)
        if match:
            channel = match.groupdict()['channel']
            text = match.groupdict()['text'].replace(":star:", "🌠")

            try:
                await self.text_channel(channel).send(text)
            except Exception as e:
                error_log(type(e), e)

    def list_commands(self):
        text = [
            f"Current JocastaBot Commands (v. {self.version}):",
            "- **@JocastaBot successful (FAN|GAN|CAN): <article> (second nomination)** - archives the target"
            " FAN/GAN/CAN as successful, and leaves a talk page message notifying the nominator. Also updates any"
            " WookieeProjects listed in the nomination, and adds the article to the  WookShowcase Twitter queue."
            " Reserved for members of the Inquisitorius, AgriCorps, and  EduCorps.",
            "- **@JocastaBot successful (FAN|GAN|CAN): <article> (second nomination) (custom message: <message>)** -"
            " same as the above command, but uses the custom message value as the header for the talk page message.",
            "- **@JocastaBot successful (FAN|GAN|CAN): <article> (second nomination) (no message)** - same as the above"
            " command, but skips the talk page message, allowing the review board member to do it themselves.",
            "- **@JocastaBot unsuccessful (FAN|GAN|CAN)): <article> (second nomination)** - archives the target"
            " FAN/GAN/CAN as unsuccessful. Reserved for members of the Inquisitorius, AgriCorps, and EduCorps.",
            "- **@JocastaBot withdrawn (FAN|GAN|CAN)): <article> (second nomination)** - archives the target"
            " FAN/GAN/CAN as withdrawn. Reserved for the nominator, or members of the Inquisitorius, AgriCorps, and"
            " EduCorps.",
            "- **@JocastaBot add (FA|GA|CA) to <WP:TOR>: <article> (second nomination)** - updates the target"
            " WookieeProject's  portfolio with the given status article. Used for when Jocasta misses it on first"
            " pass, or to add past articles.",
            "- **@JocastaBot add (FA|GA|CA) to <WP:TOR>: <article1> | <article2> | <article3> ** - updates the target"
            " WookieeProject's  portfolio with the given set of status articles. Does *not* work for nominations with"
            " (second nomination); those must be done separately.",
            "- **@JocastaBot post (FA|GA|CA): <article> (second nomination)** - adds the target article to the"
            " WookShowcase Twitter queue to be posted. Reserved for members of the social media team."
        ]

        related = [
            "- **@JocastaBot (analyze|compare|run analysis on) WP:(FA|GA|CA)** - compares the contents of the given"
            " status article type's main page (i.e. Wookieepedia:Comprehensive articles) and the category, finding any"
            " articles that are missing from either location.",
            "- **@JocastaBot new (FAN|GAN|CAN): <article> (second nomination)** - processes a new nomination, adding it"
            " to the parent page and adding the appropriate WookieeProject categories and channels. Serves as a manual"
            " backup to the scheduled new-nomination process, which runs every 5 minutes."
            "",
            "**Additional Info (contact Cade if you have questions):**",
            "- WookieeProject portfolio configuration JSON (editable by anyone): https://starwars.fandom.com/wiki/User:JocastaBot/Project Data",
            "- Status Article Rankings & History: https://starwars.fandom.com/wiki/User:JocastaBot/Rankings",
            "- Review Board Member Signature Configuration: https://starwars.fandom.com/wiki/User:JocastaBot/Signatures"
        ]

        return [(875035361070424107, "\n".join(text)), (875035362395815946, "\n".join(related))]

    async def update_command_messages(self):
        posts = self.list_commands()
        history = await self.text_channel(COMMANDS).history(oldest_first=True, limit=10).flatten()
        target = None
        for message_id, content in posts:
            for post in history:
                if post.id == message_id:
                    await post.edit(content=content)
                if post.id == 875035361070424107:
                    target = post

        if target:
            await target.reply("**Commands have been updated! Please view this channel's pinned messages for more info.**")

    @staticmethod
    def is_reload_command(message: Message):
        return "reload data" in message.content

    async def handle_reload_command(self, message: Message, _):
        self.reload_project_data(self.archiver.site)
        self.reload_user_message_data(self.archiver.site)
        await message.add_reaction(THUMBS_UP)

    def reload_project_data(self, site):
        log("Loading project data")
        page = pywikibot.Page(site, "User:JocastaBot/Project Data")
        data = {}
        for rev in page.revisions(content=True, total=5):
            try:
                data = json.loads(rev.text)
            except Exception as e:
                error_log(type(e), e)
            if data:
                log(f"Loaded valid data from revision {rev.revid}")
                break
        if not data:
            raise ArchiveException("Cannot load project data")
        self.project_data = data
        self.archiver.project_archiver.project_data = self.project_data

    def reload_user_message_data(self, site):
        log("Loading user message data")
        page = pywikibot.Page(site, "User:JocastaBot/Messages")
        data = {}
        for rev in page.revisions(content=True, total=5):
            try:
                data = json.loads(rev.text)
            except Exception as e:
                error_log(type(e), e)
            if data:
                log(f"Loaded valid data from revision {rev.revid}")
                break
        if not data:
            raise ArchiveException("Cannot load user message data")
        self.user_message_data = data
        self.archiver.user_message_data = self.user_message_data

    def reload_signatures(self, site):
        log("Loading signatures")
        page = pywikibot.Page(site, "User:JocastaBot/Signatures")
        data = {}
        for rev in page.revisions(content=True, total=5):
            try:
                data = json.loads(rev.text)
            except Exception as e:
                error_log(type(e), e)
            if data:
                log(f"Loaded valid data from revision {rev.revid}")
                break
        if not data:
            raise ArchiveException("Cannot load project data")
        self.signatures = data
        self.archiver.signatures = self.signatures
        
    @staticmethod
    def is_update_rankings_command(message: Message):
        return "update rankings table" in message.content.lower()
    
    async def handle_update_rankings_command(self, message: Message, _: dict):
        await message.add_reaction(TIMER)
        try:
            update_rankings_table(self.archiver.site)
            await message.remove_reaction(TIMER, self.user)
            await message.add_reaction(THUMBS_UP)
        except Exception as e:
            error_log(type(e), e)
            await message.remove_reaction(TIMER, self.user)
            await message.add_reaction(EXCLAMATION)

    @staticmethod
    def is_analyze_command(message: Message):
        match = re.search("(run analysis on|analyze|compare) WP:(?P<nom_type>(FA|GA|CA))", message.content)
        return None if not match else match.groupdict()

    async def handle_analyze_command(self, message: Message, command: dict):
        try:
            await message.add_reaction(TIMER)
            lines = build_analysis_response(self.archiver.site, command["nom_type"])
            await message.remove_reaction(TIMER, self.user)
            if lines:
                await message.channel.send("\n".join(lines))
            else:
                await message.add_reaction(THUMBS_UP)
        except Exception as e:
            await message.add_reaction(EXCLAMATION)
            await message.channel.send(f"{type(e)}: {e}")

    async def run_analysis(self):
        channel = self.text_channel(COMMANDS)

        for nom_type in self.analysis_cache.keys():
            pop = []
            user_ids = set()
            now = datetime.datetime.now()
            for article, (user_id, timestamp) in self.analysis_cache[nom_type].items():
                if (now.timestamp() - timestamp) > (30 * 60):
                    user_ids.add(user_id)
                    pop.append(article)

            if pop:
                for a in pop:
                    self.analysis_cache[nom_type].pop(a)
                lines = build_analysis_response(self.archiver.site, nom_type)
                if lines:
                    mentions = " ".join(f"<@{user_id}>" for user_id in list(user_ids))
                    await channel.send(f"{mentions} Please check {NOM_TYPES[nom_type].page}; articles are missing.")
                    await channel.send("\n".join(lines))

    @staticmethod
    def is_project_status_command(message: Message):
        match = re.search("add (?P<nt>[CFG]A) to (?P<prj>WP:[A-z]+): (?P<article>.*?)( - Nom: (?P<nom>.*?))?$", message.content)
        if match:
            return match.groupdict()
        return None

    async def handle_project_status_command(self, message: Message, project_command: dict):
        await message.add_reaction(TIMER)
        archive_result, response = self.process_project_status_command(project_command)
        await message.remove_reaction(TIMER, self.user)

        if archive_result and response != THUMBS_UP:
            await message.add_reaction(THUMBS_UP)
            await message.channel.send(response)
        elif archive_result:
            await message.add_reaction(self.emoji_by_name(response))
        else:
            await message.add_reaction(EXCLAMATION)
            await message.channel.send(response)

    @staticmethod
    def is_talk_page_command(message: Message):
        match = re.search("leave (?P<nom_type>[CGF]AN) message for (?P<user>.*?) about (?P<article>.*?)(?P<x>with custom message: (?P<custom>.*?))?$", message.content)
        if match:
            return match.groupdict()
        return

    async def handle_talk_page_command(self, message: Message, command: dict):
        await message.add_reaction(TIMER)
        result = self.process_talk_page_command(command, message.author.display_name)
        await message.remove_reaction(TIMER, self.user)

        if result:
            await message.add_reaction(THUMBS_UP)
        else:
            await message.add_reaction(EXCLAMATION)

    @staticmethod
    def is_archive_command(message: Message):
        command = None
        try:
            command = ArchiveCommand.parse_command(message.content)
            command.requested_by = message.author.display_name
        except ArchiveException as e:
            error_log(e.message)
        except Exception as e:
            error_log(str(e.args))
        return command

    async def handle_test_command(self, message: Message, command: ArchiveCommand):
        await message.add_reaction(TIMER)
        completed = command.article_name != "Fail-Page"
        response = "Test Error" if command.article_name == "Fail-Page" else ""
        archive_result = ArchiveResult(True, command, "", pywikibot.Page(self.archiver.site, command.article_name),
                                       None, ["The Old Republic", "Pride", "New Sith Wars"], None)
        time.sleep(2)
        await message.remove_reaction(TIMER, self.user)

        if not completed or not archive_result:  # Failed to complete or error state
            await message.add_reaction(EXCLAMATION)
            await message.channel.send(response)
        elif not archive_result.successful:  # Completed archival of unsuccessful nomination
            await message.add_reaction(THUMBS_UP)
        else:  # Completed archival of successful nomination
            status_message = self.build_message(archive_result)
            await message.channel.send(status_message)

            err_msg = "Test Error-2" if command.article_name == "Fail-Page-2" else ""
            emojis = [self.archiver.project_archiver.emoji_for_project(p) for p in archive_result.projects]
            if err_msg:
                await message.add_reaction(EXCLAMATION)
                await message.channel.send(err_msg)
            elif emojis:
                for emoji in emojis:
                    await message.add_reaction(self.emoji_by_name(emoji))

    async def handle_archive_command(self, message: Message, command: ArchiveCommand):
        accept_command = False
        if message.author.id == CADE:
            command.bypass = True
            accept_command = True
        elif command.post_mode and message.channel.name == SOCIAL_MEDIA:
            accept_command = True
        elif message.channel.name == NOM_CHANNEL or message.channel.name == COMMANDS:
            if any(r.name in ["AgriCorps", "EduCorps", "Inquisitorius"] for r in message.author.roles):
                command.bypass = True
                accept_command = True
            elif not command.success:
                accept_command = True
            else:
                await message.channel.send("Sorry, this command is restricted to members of the review panels.")

        if accept_command:
            await message.add_reaction(TIMER)
            completed, archive_result, response = self.process_archive_command(command)
            await message.remove_reaction(TIMER, self.user)

            if not completed or not archive_result:  # Failed to complete or error state
                await message.add_reaction(EXCLAMATION)
                await message.channel.send(response)
            elif not archive_result.successful:  # Completed archival of unsuccessful nomination
                await message.add_reaction(THUMBS_UP)
            elif command.post_mode:
                await message.add_reaction(THUMBS_UP)
            else:  # Completed archival of successful nomination
                self.successful_count += 1
                self.analysis_cache[command.nom_type][command.article_name] = (message.author.id,
                                                                               datetime.datetime.now().timestamp())
                status_message = self.build_message(archive_result)
                print(status_message)
                await self.text_channel("article-nominations").send(status_message)

                emojis, channels, err_msg = self.handle_archive_followup(archive_result)
                if err_msg:
                    await message.add_reaction(EXCLAMATION)
                    await message.channel.send(err_msg)
                else:
                    for emoji in (emojis or [THUMBS_UP]):
                        await message.add_reaction(self.emoji_by_name(emoji))
                    for channel in (channels or []):
                        await self.text_channel(channel).send(status_message)

                if self.successful_count >= 10:
                    try:
                        update_rankings_table(self.archiver.site)
                        self.successful_count = 0
                    except Exception as e:
                        error_log(type(e), e)

    def build_message(self, result: ArchiveResult):
        icon = self.emoji_by_name(result.nom_type[:2])
        return f"{icon} New {NOM_TYPES[result.nom_type].name} Article! <{result.page.full_url()}>"

    def process_project_status_command(self, command: dict):
        result, err_msg = False, None
        response = None
        try:
            project = self.archiver.project_archiver.find_project_from_shortcut(command["prj"])
            if not project:
                return False, f"{command['prj']} is not a valid project shortcut"
            elif command["nt"] not in ["FA", "GA", "CA"]:
                return False, f"{command['nt']} is not a valid article type"

            if "|" in command["article"]:
                articles = [x.strip() for x in command["article"].split("|")]
                response = self.archiver.project_archiver.add_multiple_articles_to_page(
                    project=project, nom_type=command["nt"], articles=articles)
            else:
                title = command['article']
                nomination = None
                if command.get('nom'):
                    nomination = command['nom']
                elif "nomination)" in title:
                    m = re.search("((.*?) \([A-z]+ nomination\))$", title)
                    if not m:
                        raise ValueError(f"Cannot extract nomination title from {title}")
                    title = m.group(2)
                    nomination = m.group(1)

                self.archiver.project_archiver.add_single_article_to_page(
                    project=project, article_title=title, nom_page_title=nomination, nom_type=command["nt"])
            result = True
        except ArchiveException as e:
            err_msg = e.message
            error_log(e.message)
        except Exception as e:
            try:
                err_msg = str(e.args[0] if str(e.args).startswith('(') else e.args)
            except Exception as _:
                err_msg = str(e.args)
            error_log(type(e), e.args)

        if response:
            return True, response
        elif result:
            return True, THUMBS_UP
        else:
            return False, err_msg

    def process_archive_command(self, command: ArchiveCommand) -> Tuple[bool, ArchiveResult, str]:
        result, err_msg = None, ""
        try:
            if command.post_mode:
                result = self.archiver.post_process(command)
            else:
                result = self.archiver.archive_process(command)

            if result and result.completed and result.successful:
                info = result.to_info()
                log(f"Twitter Post scheduled for new {command.nom_type}: {info.article_title}")
                self.twitter_bot.add_post_to_queue(info)
        except ArchiveException as e:
            err_msg = e.message
            error_log(e.message)
        except Exception as e:
            try:
                err_msg = str(e.args[0] if str(e.args).startswith('(') else e.args)
            except Exception as _:
                err_msg = str(e.args)
            error_log(type(e), e, e.args)

        if not result:
            return False, result, err_msg
        elif result and not result.completed:
            return False, result, result.message
        elif result and result.message:
            return False, result, f"UNKNOWN STATE: {result.message}"
        else:
            return True, result, ""

    def process_talk_page_command(self, command, requested_by):
        try:
            header = (command.get("custom_message") or "").strip() or command["article"]
            self.archiver.leave_talk_page_message(
                header=header, article_name=command["article"], nom_type=command["nom_type"], nominator=command["user"],
                archiver=requested_by, test=True)
            return True
        except Exception as e:
            error_log(type(e), e)
            return False

    def handle_archive_followup(self, archive_result: ArchiveResult) -> Tuple[list, list, str]:
        results, channels, err_msg = None, [], ""
        try:
            results, channels = self.archiver.handle_successful_nomination(archive_result)
        except ArchiveException as e:
            err_msg = e.message
            error_log(e.message)
        except Exception as e:
            try:
                err_msg = str(e.args[0] if str(e.args).startswith('(') else e.args)
            except Exception as _:
                err_msg = str(e.args)
            error_log(type(e), e, e.args)
        return results, channels, err_msg

    @staticmethod
    def is_check_objections_command(message: Message):
        match = re.search("check (for )?objections (on|for) (?P<nt>(FAN|GAN|CAN))(: (?P<page>.*?))?$", message.content)
        if match:
            return match.groupdict()
        return None

    async def handle_check_objections_command(self, message: Message, command: dict):
        await message.add_reaction(TIMER)
        nom_type = command["nt"]
        page_name = clean_text(command.get("page"))
        overdue, normal, err_msg = self.process_check_objections(nom_type, page_name)
        await message.remove_reaction(TIMER, self.user)

        if err_msg:
            await message.add_reaction(EXCLAMATION)
            await message.channel.send(err_msg)
        elif not normal and not overdue:
            await message.add_reaction(THUMBS_UP)
        else:
            for url, lines in normal.items():
                if lines:
                    text = f"{nom_type}: <{url}>"
                    for n, u in lines:
                        text += f"\n- {u}: {n}"
                    await message.channel.send(text)
            for url, lines in overdue.items():
                if lines:
                    text = f"{nom_type}: <{url}>\n" + "\n".join(f"- {n}" for n in lines)
                    await message.channel.send(text)

    async def handle_check_objections(self, nom_type):
        overdue, normal, err_msg = self.process_check_objections(nom_type, None)

        channel = self.text_channel(COMMANDS)
        if err_msg:
            msg = await channel.send(err_msg)
            await msg.add_reaction(EXCLAMATION)
        elif normal or overdue:
            for url, lines in normal.items():
                if lines:
                    text = f"{nom_type}: <{url}>"
                    for n, u in lines:
                        text += f"\n- {u}: {n}"
                    await channel.send(text)
            for url, lines in overdue.items():
                if lines:
                    text = f"{nom_type}: <{url}>\n" + "\n".join(f"- {n}" for n in lines)
                    await channel.send(text)

    def process_check_objections(self, nom_type, page_name):
        o, n, err_msg = [], [], ""
        try:
            if page_name:
                o, n = check_active_nominations_on_page(self.archiver.site, nom_type, page_name)
            else:
                o, n = check_active_nominations(self.archiver.site, nom_type)
        except Exception as e:
            try:
                err_msg = str(e.args[0] if str(e.args).startswith('(') else e.args)
            except Exception as _:
                err_msg = str(e.args)
            error_log(type(e), e, e.args)
        return o, n, err_msg

    @staticmethod
    def is_new_nomination_command(message: Message):
        match = re.search("new (?P<nt>[CFG]AN): (?P<article>.*?)(?P<suffix> \([A-z]+ nomination\))?", message.content)
        if match:
            return match.groupdict()
        return None

    @staticmethod
    def is_check_nominations_command(message: Message):
        return "check for nominations" in message.content or "check for new nominations" in message.content

    async def handle_new_nomination_command(self, message: Message, command: dict):
        await message.add_reaction(TIMER)
        nom_type = command["nt"]
        article = clean_text(command["article"])
        suffix = clean_text(command.get("suffix", ""))
        page_name = NOM_TYPES[nom_type].nomination_page + f"/{article}"
        if suffix:
            page_name += f" {suffix}"
        page = pywikibot.Page(self.archiver.site, page_name)
        await self._handle_new_nomination(message, page)

    async def handle_new_nomination_report(self, message: Message):
        match = re.search("wiki/(Wookieepedia:[A-z]+_article_nominations/.*)$", message.content)
        if not match:
            error_log(f"No match: {message.content}")
            return
        page_name = match.group(1)
        page = pywikibot.Page(self.archiver.site, page_name)
        await self._handle_new_nomination(message, page)

    async def check_for_new_nominations(self, _, __):
        new_nominations = check_for_new_nominations(self.archiver.site, self.current_nominations)
        if not new_nominations:
            return

        channel = self.text_channel(NOM_CHANNEL)
        for nom_type, nominations in new_nominations.items():
            for nomination in nominations:
                log(f"Processing new {nom_type}: {nomination.title().split('/', 1)[1]}")
                report = self.build_nomination_report_message(nom_type, nomination)
                if report:
                    msg = await channel.send(report)
                    await self._handle_new_nomination(msg, nomination)

    def build_nomination_report_message(self, nom_type, nomination: pywikibot.Page):
        nominator = None
        for revision in nomination.revisions(total=1):
            nominator = revision["user"]
        if not nominator:
            error_log(f"Cannot identify nominator for page {nomination.title()}")
            return
        emoji = self.emoji_by_name(nom_type[:2])
        report = NOM_TYPES[nom_type].build_report_message(nomination, nominator)
        return "{0} {1}".format(emoji, report)

    async def _handle_new_nomination(self, message: Message, page: pywikibot.Page):
        projects = add_categories_to_nomination(page, self.archiver.project_archiver)
        if projects:
            for project in projects:
                channel_name = self.project_data[project].get("channel")
                if channel_name:
                    await self.text_channel(channel_name).send(message.content)
                emoji = self.archiver.project_archiver.emoji_for_project(project)
                if emoji:
                    await message.add_reaction(self.emoji_by_name(emoji))
        else:
            await message.add_reaction(THUMBS_UP)

    @tasks.loop(minutes=5)
    async def scheduled_check_for_new_nominations(self):
        log("Scheduled Operation: Checking for New Nominations")
        if self.initial_run_nom:
            self.initial_run_nom = False
        elif self.archiver and self.archiver.project_archiver:
            await self.check_for_new_nominations(None, None)

    @tasks.loop(minutes=20)
    async def scheduled_check_for_objections(self):
        if self.objection_schedule_count == 0:
            if datetime.datetime.now().hour == 12:
                self.objection_schedule_count += 1
                await self.handle_check_objections("FAN")
        elif self.objection_schedule_count == 1:
            self.objection_schedule_count += 1
            await self.handle_check_objections("GAN")
        elif self.objection_schedule_count == 2:
            self.objection_schedule_count = 0
            await self.handle_check_objections("CAN")

    @tasks.loop(minutes=30)
    async def post_to_twitter(self):
        if self.initial_run_twitter:
            self.initial_run_twitter = False
            return
        elif self.archiver:
            if self.refresh == 2:
                self.archiver.reload_site()
                self.refresh = 0
            else:
                self.refresh += 1

        log("Scheduled Operation: Checking Twitter Post Queue")
        self.twitter_bot.scheduled_post()

        await self.run_analysis()

