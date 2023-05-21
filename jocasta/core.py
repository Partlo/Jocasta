import datetime
import re
import sys
import traceback
from typing import Tuple
import time
import json
from discord import Message, Game, Intents, HTTPException
from discord.abc import GuildChannel
from discord.channel import TextChannel, DMChannel
from discord.ext import commands, tasks

import pywikibot
from pywikibot.exceptions import EditConflictError
from jocasta.auth import build_auth
from jocasta.common import ArchiveException, UnknownCommand, build_analysis_response, clean_text, log, error_log, \
    word_count, divide_chunks, validate_word_count
from jocasta.version_reader import report_version_info
from jocasta.twitter import TwitterBot

from jocasta.data.filenames import *

from jocasta.nominations.archiver import Archiver, ArchiveCommand, ArchiveResult
from jocasta.nominations.data import NominationType, build_nom_types
from jocasta.nominations.processor import add_categories_to_nomination, load_current_nominations, \
    check_for_new_nominations, check_for_new_reviews, load_current_reviews, add_subpage_to_parent, add_nom_word_count
from jocasta.nominations.objection import check_active_nominations, check_for_objections_on_page, check_active_reviews, \
    check_for_objections_on_review_page
from jocasta.nominations.rankings import update_rankings_table
from jocasta.nominations.review import Reviewer


CADE = 346767878005194772
MONITOR = 268478587651358721
MAIN = "wookieepedia"
COMMANDS = "bot-commands"
NOM_CHANNEL = "article-nominations"
REVIEWS = "status-article-reviews"
SOCIAL_MEDIA = "social-media-team"

THUMBS_UP = "👍"
TIMER = "⏲️"
EXCLAMATION = "❗"
QUESTION = "❓"
CLOCKS = {0: "🕛", 1: "🕐", 2: "🕑", 3: "🕒", 4: "🕓", 5: "🕔", 6: "🕕", 7: "🕖", 8: "🕗", 9: "🕘", 10: "🕙", 11: "🕚"}


class JocastaBot(commands.Bot):
    """
    :type channels: dict[str, GuildChannel]
    :type emoji_storage: dict[str, int]
    :type analysis_cache: dict[str, dict[str, tuple[int, float]]]
    :type current_nominations: dict[str, list[str]]
    :type current_reviews: dict[str, list[str]]
    :type nom_types: dict[str, NominationType]

    :type report_dm: discord.DMChannel
    """

    def __init__(self, *, loop=None, **options):
        intents = Intents.default()
        intents.members = True
        super().__init__("", loop=loop, intents=intents, **options)
        log("JocastaBot online!")
        self.timezone_offset = 5

        self.refresh = 0
        with open(OBJECTION_SCHEDULE, "r") as f:
            self.objection_schedule_count = f.readline()

        self.successful_count = 0
        self.initial_run_twitter = True
        self.ready = False

        self.version = None
        with open(VERSION_FILE, "r") as f:
            self.version = f.readline()

        self.twitter_bot = TwitterBot(auth=build_auth())
        self.channels = {}
        self.emoji_storage = {}

        self.archiver = Archiver(test_mode=False, auto=True, timezone_offset=self.timezone_offset)
        self.reviewer = Reviewer(auto=True)
        self.current_nominations = {}
        self.current_reviews = {}
        self.admin_users = {
            "Imperators II": "Imperators",
            "Master Fredcerique": "MasterFred",
            "Zed42": "Zed"
        }
        self.project_data = {}
        self.nom_types = {}
        self.signatures = {}
        self.user_message_data = {}

        self.analysis_cache = {"CA": {}, "GA": {}, "FA": {}}

        self.report_dm = None

    @property
    def site(self):
        return self.archiver.site

    async def on_ready(self):
        log(f'Jocasta on as {self.user}!')

        self.report_dm = await self.get_user(CADE).create_dm()

        if self.version:
            await self.change_presence(activity=Game(name=f"ArchivalSystem v. {self.version}"))
            log(f"Running version {self.version}")
        else:
            error_log("No version found")

        site = pywikibot.Site(user="JocastaBot")
        await self.reload_project_data(site)
        await self.reload_nomination_data(site)
        await self.reload_user_message_data(site)
        await self.reload_signatures(site)
        log("Loading current nomination list")
        self.current_nominations = load_current_nominations(site, self.nom_types)
        self.current_reviews = load_current_reviews(site, self.nom_types)

        for c in self.get_all_channels():
            self.channels[c.name] = c

        for e in self.emojis:
            self.emoji_storage[e.name.lower()] = e.id

        try:
            info = report_version_info(self.archiver.site, self.version)
            if info:
                await self.text_channel("announcements").send(info)
        except Exception as e:
            error_log(type(e), e)

        await self.run_analysis()

        if not self.ready:
            self.scheduled_check_for_new_nominations.start()
            self.scheduled_check_for_objections.start()
            self.post_to_twitter.start()
            self.ready = True

    # noinspection PyTypeChecker
    def text_channel(self, name) -> TextChannel:
        try:
            return self.channels[name]
        except KeyError:
            return next(c for c in self.get_all_channels() if c.name == name)

    def emoji_by_name(self, name):
        if self.emoji_storage.get(name.lower()):
            return self.get_emoji(self.emoji_storage[name.lower()])
        return name

    def is_mention(self, message: Message):
        for mention in message.mentions:
            if mention == self.user:
                return True
        return "@JocastaBot" in message.content or "<@&863310484517027861>" in message.content

    async def find_nomination(self, nomination):
        for message in await self.text_channel(NOM_CHANNEL).history(limit=25).flatten():
            if message.author.id == MONITOR:
                if re.search("New .*?(Featured|Good|Comprehensive) article nomination", message.content):
                    log("Found: ", message.content)
                if nomination in message.content.replace("_", " "):
                    await self.handle_new_nomination_report(message)
                    return True
        return False

    async def report_error(self, command, text, *args):
        await self.report_error(command, None, text, *args)

    async def report_error(self, command, author, text, *args):
        if author:
            command = f"{command} from {author}"
        try:
            if text == "Invalid":
                await self.report_dm.send(f"Invalid Command: {command}")
            else:
                await self.report_dm.send(f"Command: {command}")
                await self.report_dm.send(f"ERROR: {text}\t{args or ''}")
                traceback.print_exc()
        except Exception:
            error_log(text, *args)

    commands = {
        "is_reload_command": "handle_reload_command",
        "is_update_rankings_command": "handle_update_rankings_command",
        "is_word_count_category_command": "handle_word_count_category_command",
        "is_word_count_command": "handle_word_count_command",
        "is_analyze_command": "handle_analyze_command",
        "is_project_status_command":  "handle_project_status_command",
        "is_talk_page_command": "handle_talk_page_command",
        "is_new_nomination_command": "handle_new_nomination_command",
        "is_check_nominations_command": "check_for_new_nominations",
        "is_check_nomination_objections_command": "handle_check_nomination_objections_command",
        "is_check_review_objections_command": "handle_check_review_objections_command",
        "is_create_review_command": "handle_create_review_command",
        "is_pass_review_command": "handle_pass_review_command",
        "is_probation_command": "handle_probation_command",
        "is_remove_status_command": "handle_remove_status_command"
    }

    async def on_message(self, message: Message):
        # print(message.channel, message.content)
        if message.author == self.user:
            return
        elif isinstance(message.channel, DMChannel):
            await self.handle_direct_message(message)
            return
        elif not self.is_mention(message):
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

        if message.reference is not None and not message.is_system():
            return

        command = await self.is_archive_command(message)
        if command and command.test_mode:
            await self.handle_test_command(message, command)
        elif command:
            await self.handle_archive_command(message, command)
            return
        elif message.channel.name == "word-count":
            await self.handle_word_count_command(message, None)
            return

    async def handle_direct_message(self, message: Message):
        cmd = self.is_word_count_command(message)
        if cmd:
            await self.handle_word_count_command(message, cmd)
            return
        log(f"Message from {message.author}: {message.content}")

        if message.author.id != CADE:
            return

        if message.content.lower() in ["kill", "die", "exit"]:
            sys.exit()

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

        match = re.search("add word count for (?P<status>(Featured|Good|Comprehensive))", message.content)
        if match:
            category = pywikibot.Category(self.site, f"Category:Wookieepedia {match['status']} article nomination pages")
            for page in category.articles():
                if "/" not in page.title():
                    continue
                text = page.get()
                new_text = add_nom_word_count(self.site, page.title(), text, False)
                if text != new_text:
                    page.put(new_text, "Updating with word count")

        match = re.search("check word count for (?P<status>.*)", message.content)
        if match:
            await self.handle_word_count_category_command(message, match.groupdict())

        match = re.search("message #(?P<channel>.*?): (?P<text>.*?)$", message.content)
        if match:
            channel = match.groupdict()['channel']
            text = match.groupdict()['text'].replace(":star:", "🌠")

            try:
                await self.text_channel(channel).send(text)
            except Exception as e:
                await self.report_error(message.content, message.author, type(e), e)

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

        review_commands = [
            "**Status Article Review Commands**:",
            "- **@JocastaBot create review for <article1> ** - creates a new review page for the given article.",
            "- **@JocastaBot mark review for <article1> as passed ** - archives the target article's review as passed.",
            "- **@JocastaBot mark review for <article1> as on probation ** - marks the target article as On Probation"
            " due to long-outstanding objections and issues, changing its status",
            "- **@JocastaBot (remove|revoke) status for <article1> ** - strips the target article of its status and"
            " archives the review.",
        ]

        related = [
            "- **@JocastaBot (analyze|compare|run analysis on) WP:(FA|GA|CA)** - compares the contents of the given"
            " status article type's main page (i.e. Wookieepedia:Comprehensive articles) and the category, finding any"
            " articles that are missing from either location.",
            "- **@JocastaBot new (FAN|GAN|CAN): <article> (second nomination)** - processes a new nomination, adding it"
            " to the parent page and adding the appropriate WookieeProject categories and channels. Serves as a manual"
            " backup to the scheduled new-nomination process, which runs every 5 minutes."
            "- **@JocastaBot reload data** - reloads data from Project Data, Nomination Data, and Signatures subpages",
            "",
            "- **@JocastaBot word count for <article>** - calculates word count for an article, including intro vs body",
            "**Additional Info (contact Cade if you have questions):**",
            "- WookieeProject portfolio configuration JSON (editable by anyone): https://starwars.fandom.com/wiki/User:JocastaBot/Project_Data",
            "- Nomination data configuration JSON (editable by anyone): https://starwars.fandom.com/wiki/User:JocastaBot/Nomination_Data",
            "- Status Article Rankings & History: https://starwars.fandom.com/wiki/User:JocastaBot/Rankings",
            "- Review Board Member Signature Configuration: https://starwars.fandom.com/wiki/User:JocastaBot/Signatures"
        ]

        return {875035361070424107: "\n".join(text), 1070735568423632967: "\n".join(review_commands), 875035362395815946: "\n".join(related)}

    async def update_command_messages(self):
        posts = self.list_commands()
        pins = await self.text_channel(COMMANDS).pins()
        target = None
        for post in pins:
            if post.id in posts:
                await post.edit(content=posts[post.id])
                if post.id == 875035361070424107:
                    target = post

        if target:
            await target.reply("**Commands have been updated! Please view this channel's pinned messages for more info.**")

    @staticmethod
    def is_reload_command(message: Message):
        return "reload data" in message.content

    async def handle_reload_command(self, message: Message, _):
        success1 = await self.reload_project_data(self.archiver.site)
        success2 = await self.reload_user_message_data(self.archiver.site)
        if success1 or success2:
            await message.add_reaction(EXCLAMATION)
            await message.channel.send(success1)
        elif success2:
            await message.add_reaction(EXCLAMATION)
            await message.channel.send(success2)
        else:
            await message.add_reaction(THUMBS_UP)

    async def reload_data(self, site, data_type, page_name):
        log(f"Loading {data_type} data")
        page = pywikibot.Page(site, f"User:JocastaBot/{page_name}")
        data = {}
        error, first = False, True
        editor = None
        for rev in page.revisions(content=True, total=5):
            try:
                data = json.loads(rev.text)
            except Exception as e:
                await self.report_error(f"{data_type} data reload", None, type(e), e)
                if first:
                    error = True
                    editor = rev['user']
                    first = False
            if data:
                log(f"Loaded valid data from revision {rev.revid}")
                break
        if not data:
            if editor:
                user_str = self.get_user_id(editor)
                text = f"{user_str}: Your last edit to [[User:JocastaBot/{page_name}]] resulted in a malformed JSON." \
                       f" Please review your edit and fix any JSON errors."
                await self.text_channel(COMMANDS).send(text)
            raise ArchiveException(f"Cannot load {data_type} data")
        return data, error

    async def reload_project_data(self, site):
        data, error = await self.reload_data(site, "project", "Project Data")
        self.project_data = data
        self.archiver.project_archiver.project_data = self.project_data
        return error

    async def reload_nomination_data(self, site):
        data, error = await self.reload_data(site, "nomination", "Nomination Data")
        self.nom_types = build_nom_types(data)
        self.archiver.nom_types = self.nom_types
        self.reviewer.nom_types = self.nom_types

    async def reload_user_message_data(self, site):
        data, error = await self.reload_data(site, "user message", "Messages")
        self.user_message_data = data
        self.archiver.user_message_data = self.user_message_data
        return error

    async def reload_signatures(self, site):
        data, error = await self.reload_data(site, "signatures", "Signatures")
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
            await self.report_error(message.content, message.author, type(e), e)
            await message.remove_reaction(TIMER, self.user)
            await message.add_reaction(EXCLAMATION)

    @staticmethod
    def is_word_count_command(message: Message):
        match = re.search("(check )?(word count|count words)( for)?:? (?P<article>.*)", message.content)
        return None if not match else match.groupdict()

    async def handle_word_count_command(self, message: Message, command: dict):
        if not command:
            print(message.content, type(message.content))
            match = re.search("<@[0-9]+> (?P<article>.*)", message.content)
            if not match:
                command = match.groupdict()
            else:
                await message.add_reaction(EXCLAMATION)
                return
        await message.add_reaction(TIMER)
        try:
            page = pywikibot.Page(self.site, command["article"])
            if not page.exists():
                await message.remove_reaction(TIMER, self.user)
                await message.add_reaction(EXCLAMATION)
                await message.channel.send(f"{command['article']} does not exist")
                return
            else:
                total, intro, body, bts = word_count(page.get())
                await message.remove_reaction(TIMER, self.user)
                await message.channel.send(f"{total:,} words = {intro:,} (introduction) + {body:,} (body) + {bts:,} (behind the scenes)")
        except Exception as e:
            await self.report_error(message.content, message.author, type(e), e)
            await message.remove_reaction(TIMER, self.user)
            await message.add_reaction(EXCLAMATION)

    @staticmethod
    def is_word_count_category_command(message: Message):
        match = re.search("check word count for (?P<status>([Ff]eatured|[Gg]ood|[Cc]omprehensive))( article)?(?P<nom> nominations)?", message.content)
        return None if not match else match.groupdict()

    async def handle_word_count_category_command(self, message: Message, command: dict):
        await message.add_reaction(CLOCKS[0])
        s = 0
        try:
            status = command['status'].capitalize()
            if command.get('nom'):
                category = pywikibot.Category(self.site, f"Category:Wookieepedia {status} article nominations")
            else:
                category = pywikibot.Category(self.site, f"Category:Wookieepedia {status} articles")
            articles = list(category.articles(namespaces=0))
            total_articles = len(articles)
            results = {}
            i = 0
            for page in articles:
                i += 1
                if i % 50 == 0:
                    print(i, page.title())
                if (i / total_articles) > ((s + 1) / 12):
                    try:
                        await message.add_reaction(CLOCKS[s + 1])
                        await message.remove_reaction(CLOCKS[s], self.user)
                    except Exception as e:
                        await self.report_error(message.content, message.author, type(e), e)
                    s += 1
                total, intro, body, bts = word_count(page.get())
                if validate_word_count(status, total, intro, body):
                    values = []
                    if intro:
                        values.append(f"{intro} (intro)")
                    values.append(f"{body} (body)" if body else "no body")
                    if bts:
                        values.append(f"{bts:,} (behind the scenes)")
                    results[page.title()] = f"{total:,} = {' + '.join(values)}"
                    # results[page.title()] = f"{total:,} = {intro} (intro) + {body} (body) + {bts:,} (behind the scenes)"
                    print(page.title(), results[page.title()])

            for chunk in divide_chunks(list(results.items()), 10):
                msg = "\n".join(f"- {title}: {m}" for title, m in chunk)
                await message.channel.send(msg)
                await message.remove_reaction(CLOCKS[s], self.user)
                if s < len(CLOCKS) - 1:
                    await message.remove_reaction(CLOCKS[s + 1], self.user)
        except Exception as e:
            await self.report_error(message.content, message.author, type(e), e)
            await message.remove_reaction(CLOCKS[s], self.user)
            if s < len(CLOCKS) - 1:
                await message.remove_reaction(CLOCKS[s + 1], self.user)
            await message.add_reaction(EXCLAMATION)

    @staticmethod
    def is_analyze_command(message: Message):
        match = re.search("(run analysis on|analyze|compare) WP:(?P<nom_type>(FA|GA|CA))", message.content)
        return None if not match else match.groupdict()

    async def handle_analyze_command(self, message: Message, command: dict):
        nom_data = self.nom_types[command["nom_type"]]
        try:
            await message.add_reaction(TIMER)
            lines = build_analysis_response(self.archiver.site, nom_data.page, nom_data.category)
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
            nom_data = self.nom_types[nom_type]
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
                lines = build_analysis_response(self.archiver.site, nom_data.page, nom_data.category)
                if lines:
                    mentions = " ".join(f"<@{user_id}>" for user_id in list(user_ids))
                    await channel.send(f"{mentions} Please check {self.nom_types[nom_type].page}; articles are missing.")
                    await channel.send("\n".join(lines))

    @staticmethod
    def is_project_status_command(message: Message):
        match = re.search("add (?P<nt>[CFG]A) to (?P<prj>WP:[A-z]+): (?P<article>.*?)( - Nom: (?P<nom>.*?))?$", message.content)
        if match:
            return match.groupdict()
        return None

    async def handle_project_status_command(self, message: Message, project_command: dict):
        await message.add_reaction(TIMER)
        archive_result, response = await self.process_project_status_command(project_command, message.author)
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
        result = await self.process_talk_page_command(message.content, command, message.author.display_name)
        await message.remove_reaction(TIMER, self.user)

        if result:
            await message.add_reaction(THUMBS_UP)
        else:
            await message.add_reaction(EXCLAMATION)

    async def is_archive_command(self, message: Message):
        command = None
        try:
            command = ArchiveCommand.parse_command(message.content, message.author)
            command.requested_by = message.author.display_name
        except ArchiveException as e:
            await self.report_error(message.content, message.author, e.message)
        except UnknownCommand:
            await message.add_reaction(QUESTION)
            await self.report_error(message.content, message.author, "Invalid", "")
        except Exception as e:
            await self.report_error(message.content, message.author, str(e.args))
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
            completed, archive_result, response = await self.process_archive_command(message.content, command)
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

                emojis, channels, err_msg = await self.handle_archive_followup(message.content, archive_result)
                if err_msg:
                    await message.add_reaction(EXCLAMATION)
                    await message.channel.send(err_msg)
                else:
                    for emoji in (emojis or [THUMBS_UP]):
                        try:
                            await message.add_reaction(self.emoji_by_name(emoji))
                        except HTTPException as e:
                            await self.report_error(message.content, message.author, f"Emoji: {emoji}", e)
                    for channel in (channels or []):
                        await self.text_channel(channel).send(status_message)

                if self.successful_count >= 10:
                    try:
                        update_rankings_table(self.archiver.site)
                        self.successful_count = 0
                    except Exception as e:
                        await self.report_error(message.content, message.author, type(e), e)

    def build_message(self, result: ArchiveResult):
        icon = self.emoji_by_name(result.nom_type[:2])
        return f"{icon} New {self.nom_types[result.nom_type].name} Article! <{result.page.full_url()}>"

    async def process_project_status_command(self, command: dict, author: str):
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
            await self.report_error(command, author, e.message)
        except Exception as e:
            try:
                err_msg = str(e.args[0] if str(e.args).startswith('(') else e.args)
            except Exception as _:
                err_msg = str(e.args)
            await self.report_error(command, author, type(e), e.args)

        if response:
            return True, response
        elif result:
            return True, THUMBS_UP
        else:
            return False, err_msg

    async def process_archive_command(self, text, command: ArchiveCommand) -> Tuple[bool, ArchiveResult, str]:
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
            await self.report_error(text, command.author, e.message)
        except Exception as e:
            try:
                err_msg = str(e.args[0] if str(e.args).startswith('(') else e.args)
            except Exception as _:
                err_msg = str(e.args)
            await self.report_error(text, command.author, type(e), e, e.args)

        if not result:
            return False, result, err_msg
        elif result and not result.completed:
            return False, result, result.message
        elif result and result.message:
            return False, result, f"UNKNOWN STATE: {result.message}"
        else:
            return True, result, ""

    async def process_talk_page_command(self, text, command: dict, requested_by):
        try:
            header = (command.get("custom_message") or "").strip() or command["article"]
            self.archiver.leave_talk_page_message(
                header=header, article_name=command["article"], nom_type=command["nom_type"], nominator=command["user"],
                archiver=requested_by)
            return True
        except Exception as e:
            await self.report_error(text, requested_by, type(e), e)
            return False

    async def handle_archive_followup(self, text, archive_result: ArchiveResult) -> Tuple[list, list, str]:
        results, channels, err_msg = None, [], ""
        try:
            results, channels = self.archiver.handle_successful_nomination(archive_result)
        except ArchiveException as e:
            err_msg = e.message
            await self.report_error(text, None, e.message)
        except Exception as e:
            try:
                err_msg = str(e.args[0] if str(e.args).startswith('(') else e.args)
            except Exception as _:
                err_msg = str(e.args)
            await self.report_error(text, None, type(e), e, e.args)
        return results, channels, err_msg

    @staticmethod
    def build_url(article):
        return f"https://starwars.fandom.com/wiki/{article.replace(' ', '_')}"

    # Review Creation/Modification Commands

    @staticmethod
    def is_create_review_command(message: Message):
        match = re.search("[Cc]reate review (of|for) (?P<article>.*)", message.content)
        if match:
            return match.groupdict()
        return None

    async def handle_create_review_command(self, message: Message, command: dict):
        nom_type, result, err_msg, user = None, None, "", None

        await message.add_reaction(TIMER)
        try:
            nom_type, result, user = self.reviewer.create_new_review_page(command['article'].strip(), message.author.display_name)
        except Exception as e:
            try:
                err_msg = str(e.args[0] if str(e.args).startswith('(') else e.args)
            except Exception as _:
                err_msg = str(e.args)
            await self.report_error(message.content, message.author.display_name, type(e), e, e.args)
        await message.remove_reaction(TIMER, self.user)

        if err_msg or not result:  # Failed to complete or error state
            await message.add_reaction(EXCLAMATION)
            await message.channel.send(err_msg or "UNKNOWN STATE: no result or error message")
        else:
            self.current_reviews[nom_type].append(result.title())
            response = await self.build_review_report_message(nom_type, result, user)
            await self.text_channel(REVIEWS).send(response)
            await message.add_reaction(THUMBS_UP)

    @staticmethod
    def is_pass_review_command(message: Message):
        match = re.search("[Mm]ark review (of|for) (?P<article>.*?) as passed", message.content)
        if match:
            return match.groupdict()
        return None

    async def handle_pass_review_command(self, message: Message, command: dict):
        status, err_msg = None, ""

        await message.add_reaction(TIMER)
        try:
            status = self.reviewer.mark_review_as_complete(command['article'], "retry " in message.content)
        except Exception as e:
            try:
                err_msg = str(e.args[0] if str(e.args).startswith('(') else e.args)
            except Exception as _:
                err_msg = str(e.args)
            await self.report_error(message.content, message.author.display_name, type(e), e, e.args)
        await message.remove_reaction(TIMER, self.user)

        if err_msg:  # Failed to complete or error state
            await message.add_reaction(EXCLAMATION)
            await message.channel.send(err_msg or "UNKNOWN STATE: no result or error message")
        else:
            icon = self.emoji_by_name("GroguCheer")
            response = f"{icon} **{status} Article: {command['article']}** is no longer under review!"
            print(response)
            await self.text_channel(REVIEWS).send(response)
            await message.add_reaction(THUMBS_UP)

    @staticmethod
    def is_probation_command(message: Message):
        match = re.search("[Mm]ark review (of|for) (?P<article>.*?) as ((on )?probation|probed)", message.content)
        if match:
            return match.groupdict()
        return None

    async def handle_probation_command(self, message: Message, command: dict):
        status, err_msg = None, ""

        await message.add_reaction(TIMER)
        try:
            status = self.reviewer.mark_article_as_on_probation(command['article'], "retry " in message.content)
        except Exception as e:
            try:
                err_msg = str(e.args[0] if str(e.args).startswith('(') else e.args)
            except Exception as _:
                err_msg = str(e.args)
            await self.report_error(message.content, message.author.display_name, type(e), e, e.args)
        await message.remove_reaction(TIMER, self.user)

        if err_msg:  # Failed to complete or error state
            await message.add_reaction(EXCLAMATION)
            await message.channel.send(err_msg or "UNKNOWN STATE: no result or error message")
        else:
            icon = self.emoji_by_name("Mtsorrow")
            response = f"{icon} {status} Article **{command['article']}** is now on probation: <{self.build_url(command['article'])}>"
            print(response)
            await self.text_channel(REVIEWS).send(response)
            await message.add_reaction(THUMBS_UP)

    @staticmethod
    def is_remove_status_command(message: Message):
        match = re.search("([Rr]emove|[Rr]evoke) status (of|for) (?P<article>.*)", message.content)
        if match:
            return match.groupdict()
        return None

    async def handle_remove_status_command(self, message: Message, command: dict):
        status, err_msg = None, ""

        await message.add_reaction(TIMER)
        try:
            status = self.reviewer.mark_article_as_former(command['article'], "retry " in message.content)
        except Exception as e:
            try:
                err_msg = str(e.args[0] if str(e.args).startswith('(') else e.args)
            except Exception as _:
                err_msg = str(e.args)
            await self.report_error(message.content, message.author.display_name, type(e), e, e.args)
        await message.remove_reaction(TIMER, self.user)

        if err_msg:  # Failed to complete or error state
            await message.add_reaction(EXCLAMATION)
            await message.channel.send(err_msg or "UNKNOWN STATE: no result or error message")
        else:
            icon = self.emoji_by_name("yodafacepalm")
            response = f"{icon} **{command['article']}** has failed review and has been stripped of its {status} status."
            await self.text_channel(REVIEWS).send(response)
            await message.add_reaction(THUMBS_UP)

    # Check Nomination Objections

    @staticmethod
    def is_check_nomination_objections_command(message: Message):
        match = re.search("check (for )?objections (on|for) (?P<nt>(FAN|GAN|CAN))(: (?P<page>.*?))?$", message.content)
        if match:
            return match.groupdict()
        return None

    async def handle_check_nomination_objections_command(self, message: Message, command: dict):
        await message.add_reaction(TIMER)
        nom_type = command["nt"]
        page_name = clean_text(command.get("page"))
        overdue, normal, err_msg = await self.process_check_objections(nom_type, page_name, True)
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

    async def handle_check_nomination_objections(self, nom_type):
        overdue, normal, err_msg = await self.process_check_objections(nom_type, None, False)

        channel = self.text_channel(NOM_CHANNEL)
        if err_msg:
            msg = await channel.send(err_msg)
            await msg.add_reaction(EXCLAMATION)
            return

        if normal:
            user_ids = self.get_user_ids()
            for url, lines in normal.items():
                if lines:
                    text = f"{nom_type}: <{url}>"
                    for u, n in lines:
                        user_str = self.get_user_id(u, user_ids)
                        text += f"\n- {user_str}: {n}"
                    await channel.send(text)

        if overdue:
            review_channel = self.text_channel(self.nom_types[nom_type].channel)

            for url, lines in overdue.items():
                if lines:
                    text = f"{nom_type}: <{url}>\n" + "\n".join(f"- {n}" for n in lines)
                    log(f"Sending message to #{review_channel}:\n{text}")
                    await review_channel.send(text)

    # Check Review Objections

    @staticmethod
    def is_check_review_objections_command(message: Message):
        match = re.search("check (for )?objections (on|for) (?P<nt>(FA|GA|CA)) reviews?(: (?P<page>.*?))?$", message.content)
        if match:
            return match.groupdict()
        return None

    REVIEW_MESSAGES = {
        "ready": "The following articles have no outstanding objections:",
        "probe": "The following articles have been under review for 30 or more days and have outstanding objections:",
        "normal": "The following articles are under review but have outstanding objections:",
        "probation": "The following articles on probation continue to have outstanding objections:"
    }

    async def handle_check_review_objections_command(self, message: Message, command: dict):
        await message.add_reaction(TIMER)
        nom_type = command["nt"]
        page_name = clean_text(command.get("page"))
        results, err_msg = await self.process_check_reviews(nom_type, page_name)
        await message.remove_reaction(TIMER, self.user)

        if err_msg:
            await message.add_reaction(EXCLAMATION)
            await message.channel.send(err_msg)
        elif results.get("ready") and page_name:
            await message.add_reaction(THUMBS_UP)
        else:
            for rt, header in self.REVIEW_MESSAGES.items():
                if results.get(rt):
                    await message.channel.send(header)
                    for msg in results[rt]:
                        await message.channel.send(msg)

    async def handle_check_review_objections(self, nom_type):
        results, err_msg = await self.process_check_reviews(nom_type, None)

        channel = self.text_channel(REVIEWS)
        if err_msg:
            msg = await channel.send(err_msg)
            await msg.add_reaction(EXCLAMATION)
            return

        if results.get("ready"):
            await channel.send(self.REVIEW_MESSAGES["ready"])
            for msg in results["ready"]:
                await channel.send(msg)

        if results.get("probe"):
            await channel.send(self.REVIEW_MESSAGES["probe"])
            for msg in results["probe"]:
                await channel.send(msg)

    # Objection Checking

    def get_user_ids(self):
        results = {}
        for user in self.text_channel(MAIN).guild.members:
            results[user.name] = user.id
            results[user.display_name] = user.id
        return results

    def get_user_id(self, editor, user_ids=None):
        if not user_ids:
            user_ids = self.get_user_ids()
        user_id = user_ids.get(self.admin_users.get(editor, editor), user_ids.get(editor))
        return f"<@{user_id}>" if user_id else editor

    async def process_check_objections(self, nom_type, page_name, include) -> Tuple[dict, dict, str]:
        o, n, err_msg = {}, {}, ""
        try:
            if page_name:
                o, n = check_for_objections_on_page(self.archiver.site, self.nom_types[nom_type], page_name)
            else:
                o, n = check_active_nominations(self.archiver.site, self.nom_types[nom_type], include)
        except Exception as e:
            try:
                err_msg = str(e.args[0] if str(e.args).startswith('(') else e.args)
            except Exception as _:
                err_msg = str(e.args)
            await self.report_error(f"Objection check: {page_name}", None, type(e), e, e.args)
        return o, n, err_msg

    async def process_check_reviews(self, nom_type, page_name) -> Tuple[dict, str]:
        results, err_msg = {}, ""
        try:
            if page_name:
                results = check_for_objections_on_review_page(self.archiver.site, self.nom_types[nom_type], page_name)
            else:
                results = check_active_reviews(self.archiver.site, self.nom_types[nom_type])
        except Exception as e:
            try:
                err_msg = str(e.args[0] if str(e.args).startswith('(') else e.args)
            except Exception as _:
                err_msg = str(e.args)
            await self.report_error(f"Review objection check", None, type(e), e, e.args)
        return results, err_msg

    # New Nominations

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
        page_name = self.nom_types[nom_type].nomination_page + f"/{article}"
        if suffix:
            page_name += f" {suffix}"
        page = pywikibot.Page(self.archiver.site, page_name)
        await self._handle_new_nomination(message, page)

    async def handle_new_nomination_report(self, message: Message):
        match = re.search("wiki/(Wookieepedia:[A-z]+_article_nominations/.*)$", message.content)
        if not match:
            await self.report_error(message.content, message.author, f"No match: {message.content}")
            return
        page_name = match.group(1)
        page = pywikibot.Page(self.archiver.site, page_name)
        await self._handle_new_nomination(message, page)

    async def check_for_new_nominations(self, _, __):
        new_nominations = check_for_new_nominations(self.archiver.site, self.nom_types, self.current_nominations)
        if not new_nominations:
            return

        channel = self.text_channel(NOM_CHANNEL)
        for nom_type, nominations in new_nominations.items():
            for nomination in nominations:
                log(f"Processing new {nom_type}: {nomination.title().split('/', 1)[1]}")
                report = await self.build_nomination_report_message(nom_type, nomination)
                if report:
                    msg = await channel.send(report)
                    await self._handle_new_nomination(msg, nomination)

    async def build_nomination_report_message(self, nom_type, nomination: pywikibot.Page):
        nominator = None
        for revision in nomination.revisions(total=1, reverse=True):
            nominator = revision["user"]
        if not nominator:
            await self.report_error(f"Nomination check for {nomination.title()}", None, f"Cannot identify nominator for page {nomination.title()}")
            return
        emoji = self.emoji_by_name(nom_type[:2])
        report = self.nom_types[nom_type].build_report_message(nomination, nominator)
        return "{0} {1}".format(emoji, report)

    async def _handle_new_nomination(self, message: Message, page: pywikibot.Page):
        try:
            projects = add_categories_to_nomination(page, self.archiver.project_archiver)
        except EditConflictError:
            projects = add_categories_to_nomination(page, self.archiver.project_archiver)
        try:
            add_subpage_to_parent(page, self.archiver.site, "nomination")
        except EditConflictError:
            add_subpage_to_parent(page, self.archiver.site, "nomination")

        if projects:
            for project in projects:
                channel_name = self.project_data[project].get("channel")
                if channel_name:
                    await self.text_channel(channel_name).send(message.content)
                emoji = self.archiver.project_archiver.emoji_for_project(project)
                if emoji:
                    try:
                        await message.add_reaction(self.emoji_by_name(emoji))
                    except HTTPException as e:
                        await self.report_error(message.content, message.author, f"Emoji: {emoji}", e)
        else:
            await message.add_reaction(THUMBS_UP)

    # New Reviews

    async def check_for_new_reviews(self, _, __):
        new_reviews = check_for_new_reviews(self.archiver.site, self.nom_types, self.current_reviews)
        if not new_reviews:
            return

        channel = self.text_channel(REVIEWS)
        for nom_type, reviews in new_reviews.items():
            for review in reviews:
                log(f"Processing new {nom_type} review: {review.title().split('/', 1)[1]}")
                report = await self.build_review_report_message(nom_type, review)
                if report:
                    await channel.send(report)
                    await self._handle_new_review(review)

    async def build_review_report_message(self, nom_type, review: pywikibot.Page, user=None):
        emoji = self.emoji_by_name("Sadme")
        user = self.get_user_id(user) if user else None
        report = self.nom_types[nom_type].build_review_message(review, user)
        return "{0} {1}".format(emoji, report)

    async def _handle_new_review(self, page: pywikibot.Page):
        try:
            add_subpage_to_parent(page, self.archiver.site, "review")
        except EditConflictError:
            add_subpage_to_parent(page, self.archiver.site, "review")

    # Scheduled Tasks

    @tasks.loop(minutes=5)
    async def scheduled_check_for_new_nominations(self):
        try:
            log("Scheduled Operation: Checking for New Nominations")
            if not self.channels:
                return
            elif self.archiver and self.archiver.project_archiver:
                await self.check_for_new_nominations(None, None)
                await self.check_for_new_reviews(None, None)
        except Exception as e:
            await self.report_error("Nomination check", None, type(e), e)

    def update_objection_schedule(self, val):
        self.objection_schedule_count = val
        with open(OBJECTION_SCHEDULE, "w") as f:
            f.writelines(val)

    @tasks.loop(minutes=20)
    async def scheduled_check_for_objections(self):
        if not self.channels:
            return
        if self.objection_schedule_count == "FAN":
            if datetime.datetime.now().hour == 12:
                self.update_objection_schedule("GAN")
                await self.handle_check_nomination_objections("FAN")
                await self.handle_check_review_objections("FA")
        elif self.objection_schedule_count == "GAN":
            self.update_objection_schedule("CAN")
            await self.handle_check_nomination_objections("GAN")
            await self.handle_check_review_objections("GA")
        elif self.objection_schedule_count == "CAN":
            self.update_objection_schedule("FAN")
            await self.handle_check_nomination_objections("CAN")
            await self.handle_check_review_objections("CA")

    @tasks.loop(minutes=5)
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
