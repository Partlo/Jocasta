from typing import Dict
from pywikibot import Page
from typing import List
import re

from jocasta.common import ArchiveException, UnknownCommand, clean_text
from jocasta.nominations.rankings import blacklisted


class ArticleInfo:
    def __init__(self, title: str, page_url: str, nom_type: str, nominator: str, projects: List[str] = None):
        self.article_title = title
        self.page_url = page_url
        self.nom_type = nom_type
        self.nominator = nominator
        self.projects = projects or []


class ArchiveCommand:
    def __init__(self, successful: bool, nom_type: str, article_name: str, suffix: str, post_mode: bool, retry: bool,
                 test_mode: bool, author: str, withdrawn=False, bypass=False, send_message=True, custom_message=None):
        self.author = author
        self.success = successful
        self.nom_type = nom_type
        self.article_name = article_name
        self.suffix = suffix
        self.retry = retry
        self.post_mode = post_mode
        self.test_mode = test_mode
        self.bypass = bypass
        self.withdrawn = withdrawn
        self.requested_by = None
        self.send_message = send_message
        self.custom_message = custom_message

    @staticmethod
    def parse_command(command: str, author: str):
        """ Parses the nomination type, result, article name and optional suffix from the given command. """

        match = re.search("(?P<result>([Ss]uc(c)?es(s)?ful|[Uu]nsuc(c)?es(s)?ful|[Ff]ailed|[Ww]ithdrawn?|[Tt]est|[Pp]ost)) (?P<ntype>[CGFJ]A)N: ?(?P<article>.*?)(?P<suffix> \([A-z]+ nomination\))?(?P<no_msg> \(no message\))?(, | \()?(?P<custom>custom message: .*?\)?)?$",
                          command.strip().replace('\\n', ''))
        if not match:
            raise UnknownCommand("Invalid command")

        result_str = clean_text(match.groupdict().get('result')).lower()
        test_mode, post_mode, withdrawn = False, False, False
        if result_str in ["successful", "succesful", "sucessful", "sucesful"]:
            successful = True
        elif result_str in ["unsuccessful", "unsuccesful", "unsucessful", "unsucesful", "failed"]:
            successful = False
        elif result_str == "withdrawn" or result_str == "withdraw":
            successful = False
            withdrawn = True
        elif result_str == "test":
            test_mode = True
            successful = True
        elif result_str == "post":
            post_mode = True
            successful = False
        else:
            raise ArchiveException(f"Invalid result {result_str}")

        nom_type = clean_text(match.groupdict().get('ntype'))
        if nom_type not in ["CA", "GA", "FA"]:
            raise ArchiveException(f"Unrecognized nomination type {nom_type}")

        article_name = clean_text(match.groupdict().get('article'))
        suffix = clean_text(match.groupdict().get('suffix'))
        if suffix:
            suffix = f" {suffix}"
        retry = "retry " in command.split(":")[0]
        send_message = not bool(clean_text(match.groupdict()['no_msg']))
        custom_message = clean_text(match.groupdict()['custom'])
        if custom_message:
            custom_message = custom_message.split("custom message: ")[1].strip()
            if custom_message.endswith(")"):
                custom_message = custom_message[:-1]

        return ArchiveCommand(successful=successful, nom_type=nom_type, article_name=article_name, suffix=suffix,
                              post_mode=post_mode, retry=retry, test_mode=test_mode, withdrawn=withdrawn,
                              send_message=send_message, custom_message=custom_message, author=author)


class ArchiveResult:
    def __init__(self, completed: bool, command: ArchiveCommand, msg: str, page: Page = None,
                 nom_page: Page = None, projects: list = None, nominator: str = None, success=False):
        self.completed = completed
        self.nom_type = command.nom_type
        self.successful = success or command.success
        self.message = msg

        self.page = page
        self.nom_page = nom_page
        self.projects = projects
        self.nominator = nominator

    def to_info(self):
        if self.completed and self.successful:
            user = None if self.nominator in blacklisted else self.nominator
            return ArticleInfo(self.page.title(), self.page.full_url(), self.nom_type, user, self.projects)
        return None


class NominationType:
    def __init__(self, abbr: str, data: Dict[str, str]):
        self.abbreviation = abbr
        self.nom_type = f"{abbr}N"
        self.adjective = data["type"]
        self.mode = data["mode"]
        self.full_name = f"{self.adjective} {self.mode}"

        self.page = f"Wookieepedia:{self.adjective} {self.mode}s"
        self.category = f"Category:Wookieepedia {self.adjective} {self.mode}s"
        self.nomination_page = f"Wookieepedia:{self.adjective} {self.mode} nominations"
        self.nomination_category = f"Category:Wookieepedia {self.adjective} {self.mode} nomination pages"
        self.votes_category = f"Category:{self.adjective} {self.mode} nominations with sufficient votes"
        self.review_category = f"Category:Wookieepedia {self.adjective} {self.mode} review pages"

        self.fast_review_votes = data["reviewBoardOnlyVoteCount"]
        self.min_total_votes = data["totalVotesForFastPass"]
        self.min_review_votes = data["minReviewBoardVotes"]
        self.template = data["voteTemplate"].lower()
        self.icon = data["icon"]
        self.premium_icon = data["premiumIcon"]
        self.channel = data["channel"]
        self.overdue_days = data["overdueDays"]
        self.notification_days = data["notificationDays"]

    def build_report_message(self, page: Page, nominator: str):
        url = (page.site.base_url(page.site.article_path) + page.title()).replace(' ', '_')
        title = page.title().split("/", 1)[1]
        target = re.sub(" \([A-z]+ nomination\)", "", title)
        target_url = (page.site.base_url(page.site.article_path) + target).replace(' ', '_')
        return f"New **[{self.full_name} nomination](<{url}>)** by **{nominator}**: [{title}](<{target_url}>)"

    def build_review_message(self, page: Page, user_text):
        url = (page.site.base_url(page.site.article_path) + page.title()).replace(' ', '_')
        title = page.title().split("/", 1)[1]
        target = re.sub(" \([A-z]+ review\)", "", title)
        target_url = (page.site.base_url(page.site.article_path) + target).replace(' ', '_')
        u = 'written by ' + user_text if user_text else ''
        return f"[New review](<{url}>) requested for **{self.full_name}: [{title}](<{target_url}>)** {u}"


def build_nom_types(data):
    result = {}
    for k, v in data.items():
        x = NominationType(k, v)
        if x.mode == "topic":
            continue
        result[k] = x
        result[f"{k}N"] = x
    return result
