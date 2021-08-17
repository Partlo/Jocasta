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
from common import ArchiveException, build_analysis_response
from filenames import *
from nom_data import NOM_TYPES
from nomination_processor import add_categories_to_nomination
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


class JocastaBot(commands.Bot):
    """
    :type channels: dict[str, GuildChannel]
    :type emoji_storage: dict[str, int]
    :type analysis_cache: dict[str, dict[str, tuple[int, float]]]
    """

    def __init__(self, *, loop=None, **options):
        super().__init__("", loop=loop, **options)
        print("JocastaBot online!")

        self.refresh = 0
        self.successful_count = 0
        self.initial_run = True

        self.version = None
        with open(VERSION_FILE, "r") as f:
            self.version = f.readline()

        self.twitter_bot = TwitterBot(auth=build_auth())
        self.channels = {}
        self.emoji_storage = {}

        self.archiver = Archiver(test_mode=False, auto=True)
        self.project_data = {}
        self.signatures = {}

        self.analysis_cache = {"CA": {}, "GA": {}, "FA": {}}

        self.post_to_twitter.start()

    async def on_ready(self):
        print('Jocasta on as {0}!'.format(self.user))

        if self.version:
            await self.change_presence(activity=Game(name=f"ArchivalSystem v. {self.version}"))
            print(f"Running version {self.version}")
        else:
            print("No version found")

        self.reload_project_data()
        self.reload_signatures()

        for c in self.get_all_channels():
            self.channels[c.name] = c

        for e in self.emojis:
            self.emoji_storage[e.name.lower()] = e.id

        try:
            info = report_version_info(self.archiver.site, self.version)
            if info:
                await self.text_channel(COMMANDS).send(info)
        except Exception as e:
            print(type(e), e)

        await self.run_analysis()

        # for message in await self.text_channel("wookieepedia").history(limit=10).flatten():
        #     if message.id == 876670646313185310:
        #         await message.edit(content=":waves hands: you saw.... nothing")
        #     if message.author.id == MONITOR:
        #         print(message.content)
        #         await self.handle_new_nomination(message)

    # noinspection PyTypeChecker
    def text_channel(self, name) -> TextChannel:
        return self.channels[name]

    def emoji_by_name(self, name):
        if self.emoji_storage.get(name.lower()):
            return self.get_emoji(self.emoji_storage[name.lower()])
        return name

    def is_mention(self, message: Message):
        for mention in message.mentions:
            # print(mention)
            if mention == self.user:
                return True
        return False

    async def join_channels(self):
        channel = self.text_channel("wookieeprojects")
        for message in await channel.history().flatten():
            if message.id == 833046509494075422:
                for reaction in message.reactions:
                    print(reaction.me, reaction.emoji)
                    if reaction.me:
                        await message.remove_reaction(reaction.emoji, self.user)
                        # time.sleep(1)
                        # await message.add_reaction(reaction.emoji)
                    else:
                        await message.add_reaction(reaction.emoji)

    async def find_nomination(self, nomination):
        for message in await self.text_channel(NOM_CHANNEL).history(limit=25).flatten():
            if message.author.id == MONITOR:
                print(message.content)
                if re.search("New .*?(Featured|Good|Comprehensive) article nomination", message.content):
                    print("Found: ", message.content)
                if nomination in message.content.replace("_", " "):
                    print(message.content)
                    await self.handle_new_nomination(message)
                    return True
        return False

    commands = {
        "is_reload_command": "handle_reload_command",
        "is_update_rankings_command": "handle_update_rankings_command",
        "is_analyze_command": "handle_analyze_command",
        "is_project_status_command":  "handle_project_status_command",
        "is_talk_page_command": "handle_talk_page_command"
    }

    async def on_message(self, message: Message):
        if message.author == self.user:
            return
        elif isinstance(message.channel, DMChannel):
            await self.handle_direct_message(message)
            return
        elif message.channel.name == "article-nominations" and message.author.id == MONITOR:
            if re.search("New .*?(Featured|Good|Comprehensive) article nomination", message.content):
                await self.handle_new_nomination(message)
            return
        elif not (self.is_mention(message) or "@JocastaBot" in message.content):
            return

        print(f'Message from {message.author} in {message.channel}: [{message.content}]')

        if "Hello!" in message.content:
            await message.channel.send("Hello there!")
            return
        if "Login" in message.content:
            try:
                archiver = Archiver(test_mode=False, auto=True)
            except ArchiveException as e:
                print(e.message)
            except Exception as e:
                print(e, e.args)
            return

        if "list all commands" in message.content:
            await self.update_command_messages()
            return

        for identifier, handler in self.commands.items():
            command_dict = getattr(self, identifier)(message)
            if command_dict:
                await getattr(self, handler)(message, command_dict)
                return

        # reload_command = self.is_reload_command(message)
        # if reload_command:
        #     await self.handle_reload_command(message)
        #     return
        #
        # update_rankings_command = self.is_update_rankings_command(message)
        # if update_rankings_command:
        #     await self.handle_update_rankings_command(message)
        #     return
        #
        # analyze_command = self.is_analyze_command(message)
        # if analyze_command:
        #     await self.handle_analyze_command(message, analyze_command)
        #     return
        #
        # project_command = self.is_project_status_command(message)
        # if project_command:
        #     print(f"Project Command: {message.content}")
        #     await self.handle_project_status_command(message, project_command)
        #     return
        #
        # talk_page_command = self.is_talk_page_command(message)
        # if talk_page_command:
        #     await self.handle_talk_page_command(message, talk_page_command)
        #     return

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
                await message.add_reaction("❗")
            return

        project_command = self.is_project_status_command(message)
        if project_command:
            print(f"Project Command: {message.content}")
            await self.handle_project_status_command(message, project_command)
            return

        match = re.search("message #(?P<channel>.*?): (?P<text>.*?)$", message.content)
        if match:
            channel = match.groupdict()['channel']
            text = match.groupdict()['text'].replace(":star:", "🌠")

            try:
                await self.text_channel(channel).send(text)
            except Exception as e:
                print(type(e), e)

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
        return "reload project data" in message.content

    def reload_project_data(self):
        print("Loading project data")
        site = pywikibot.Site()
        page = pywikibot.Page(site, "User:JocastaBot/Project Data")
        data = {}
        for rev in page.revisions(content=True, total=5):
            try:
                data = json.loads(rev.text)
            except Exception as e:
                print(type(e), e)
            if data:
                print(f"Loaded valid data from revision {rev.revid}")
                break
        if not data:
            raise ArchiveException("Cannot load project data")
        self.project_data = data
        self.archiver.project_archiver.project_data = self.project_data

    async def handle_reload_command(self, message: Message):
        self.reload_project_data()
        await message.add_reaction(THUMBS_UP)

    def reload_signatures(self):
        print("Loading signatures")
        site = pywikibot.Site()
        page = pywikibot.Page(site, "User:JocastaBot/Signatures")
        data = {}
        for rev in page.revisions(content=True, total=5):
            try:
                data = json.loads(rev.text)
            except Exception as e:
                print(type(e), e)
            if data:
                print(f"Loaded valid data from revision {rev.revid}")
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
            print(type(e), e)
            await message.remove_reaction(TIMER, self.user)
            await message.add_reaction("!")

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
            await message.add_reaction("!")
            await message.channel.send(f"{type(e)}: {e}")

    async def run_analysis(self):
        channel = self.text_channel(COMMANDS)

        for nom_type in self.analysis_cache.keys():
            print(f"Checking {nom_type} page and category...")
            pop = []
            user_ids = set()
            now = datetime.datetime.now()
            for article, (user_id, timestamp) in self.analysis_cache[nom_type].items():
                print(article, user_id, timestamp)
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

        print(archive_result, response)
        if archive_result and response != THUMBS_UP:
            await message.add_reaction(THUMBS_UP)
            await message.channel.send(response)
        elif archive_result:
            await message.add_reaction(self.emoji_by_name(response))
        else:
            await message.add_reaction("❗")
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
            await message.add_reaction("❗")

    @staticmethod
    def is_archive_command(message: Message):
        command = None
        try:
            command = ArchiveCommand.parse_command(message.content)
            command.requested_by = message.author.display_name
        except ArchiveException as e:
            print(e.message)
        except Exception as e:
            print(str(e.args))
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
            await message.add_reaction("❗")
            await message.channel.send(response)
        elif not archive_result.successful:  # Completed archival of unsuccessful nomination
            await message.add_reaction(THUMBS_UP)
        else:  # Completed archival of successful nomination
            status_message = self.build_message(archive_result)
            await message.channel.send(status_message)

            err_msg = "Test Error-2" if command.article_name == "Fail-Page-2" else ""
            emojis = [self.archiver.project_archiver.emoji_for_project(p) for p in archive_result.projects]
            if err_msg:
                await message.add_reaction("❗")
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
                await message.add_reaction("❗")
                await message.channel.send(response)
            elif not archive_result.successful:  # Completed archival of unsuccessful nomination
                await message.add_reaction(THUMBS_UP)
            else:  # Completed archival of successful nomination
                self.successful_count += 1
                self.analysis_cache[command.nom_type][command.article_name] = (message.author.id,
                                                                               datetime.datetime.now().timestamp())
                status_message = self.build_message(archive_result)
                await self.text_channel("article-nominations").send(status_message)

                emojis, channels, err_msg = self.handle_archive_followup(archive_result)
                if err_msg:
                    await message.add_reaction("❗")
                    await message.channel.send(err_msg)
                else:
                    for emoji in (emojis or []):
                        await message.add_reaction(self.emoji_by_name(emoji))
                    for channel in (channels or []):
                        await self.text_channel(channel).send(status_message)

                if self.successful_count >= 10:
                    try:
                        update_rankings_table(self.archiver.site)
                        self.successful_count = 0
                    except Exception as e:
                        print(type(e), e)

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
                print(articles)
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
            print(e.message)
        except Exception as e:
            try:
                err_msg = str(e.args[0] if str(e.args).startswith('(') else e.args)
            except Exception as _:
                err_msg = str(e.args)
            print(type(e), e.args)

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
                print(f"Twitter Post scheduled for new {command.nom_type}: {info.article_title}")
                self.twitter_bot.add_post_to_queue(info)
        except ArchiveException as e:
            err_msg = e.message
            print(e.message)
        except Exception as e:
            try:
                err_msg = str(e.args[0] if str(e.args).startswith('(') else e.args)
            except Exception as _:
                err_msg = str(e.args)
            print(e, e.args)

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
                archiver=requested_by)
            return True
        except Exception as e:
            print(type(e), e)
            return False

    def handle_archive_followup(self, archive_result: ArchiveResult) -> Tuple[list, list, str]:
        results, channels, err_msg = None, [], ""
        try:
            results, channels = self.archiver.handle_successful_nomination(archive_result)
        except ArchiveException as e:
            err_msg = e.message
            print(e.message)
        except Exception as e:
            try:
                err_msg = str(e.args[0] if str(e.args).startswith('(') else e.args)
            except Exception as _:
                err_msg = str(e.args)
            print(e, e.args)
        return results, channels, err_msg

    async def handle_new_nomination(self, message: Message):
        match = re.search("wiki/(Wookieepedia:[A-z]+_article_nominations/.*)$", message.content)
        if not match:
            print(f"No match: {message.content}")
            return
        page_name = match.group(1)
        projects = add_categories_to_nomination(page_name, self.archiver.project_archiver)
        if projects:
            print(projects)
            for project in projects:
                channel_name = self.project_data[project].get("channel")
                if channel_name:
                    await self.text_channel(channel_name).send(message.content.replace("http", "<http").rstrip() + ">")
                emoji = self.archiver.project_archiver.emoji_for_project(project)
                print(project, emoji)
                if emoji:
                    await message.add_reaction(self.emoji_by_name(emoji))
        else:
            await message.add_reaction(THUMBS_UP)

    @tasks.loop(minutes=30)
    async def post_to_twitter(self):
        if self.initial_run:
            self.initial_run = False
            return
        elif self.archiver:
            if self.refresh == 2:
                self.archiver.reload_site()
                self.refresh = 0
            else:
                self.refresh += 1

        print("Scheduled Operation: Checking Twitter Post Queue")
        self.twitter_bot.scheduled_post()

        await self.run_analysis()

