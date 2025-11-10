import datetime

from pywikibot import Page, Site, showDiff, input_choice, Category
from typing import Tuple, List, Dict
import json
import re
import time

from jocasta.common import ArchiveException, calculate_nominated_revision, calculate_revisions, determine_nominator, \
    determine_title_format, log, error_log, extract_err_msg, word_count, build_sub_page_name, extract_support_section
from jocasta.data.filenames import *
from jocasta.nominations.data import ArchiveCommand, ArchiveResult, NominationType, build_nom_types
from jocasta.nominations.project_archiver import ProjectArchiver
from jocasta.nominations.processor import remove_subpage_from_parent
from jocasta.nominations.rankings import update_current_year_rankings, update_current_year_rankings_for_multiple
from jocasta.nominations.talk_page import build_history_text, build_talk_page


# noinspection RegExpRedundantEscape
class Archiver:
    """ A class encapsulating the core archival logic for Jocasta.

    :type project_archiver: ProjectArchiver
    :type project_data: dict[str, dict]
    :type nom_types: dict[str, NominationType]
    :type signatures: dict[str, str]
    :type user_message_data: dict[str, list[str]]
    """
    def __init__(self, *, test_mode=False, auto=False, project_data: dict = None, nom_types: dict = None,
                 signatures: dict = None, timezone_offset=0):
        self.site = Site(user="JocastaBot")
        self.site.login(user="JocastaBot")
        self.timezone_offset = timezone_offset

        if not project_data:
            try:
                with open(PROJECT_DATA_FILE, "r") as f:
                    project_data = json.load(f)
            except Exception:
                pass
        self.project_data = project_data or {}

        if not nom_types:
            try:
                with open(NOM_DATA_FILE, "r") as f:
                    nom_types = build_nom_types(json.load(f))
            except Exception:
                pass
        self.nom_types = nom_types or {}

        if not signatures:
            try:
                with open(SIGNATURES_FILE, "r") as f:
                    signatures = json.load(f)
            except Exception:
                pass
        self.signatures = signatures
        self.user_message_data = {}

        self.project_archiver = ProjectArchiver(self.site, self.project_data)
        self.test_mode = test_mode
        self.auto = auto
        self.talk_ns = "User talk" if self.test_mode else "Talk"

    def reload_site(self):
        self.site = Site(user="JocastaBot")
        self.site.login()

    def input_prompts(self, old_text, new_text):
        if self.auto:
            return

        showDiff(old_text, new_text, context=3)

        choice = input_choice('Do you want to accept these changes?', [('Yes', 'y'), ('No', 'n')], default='N')
        if choice == 'n':
            assert False

    def calculate_nomination_page_name(self, command: ArchiveCommand):
        return self.nom_types[command.nom_type].nomination_page + f"/{command.article_name}{command.suffix}"

    def post_process(self, command: ArchiveCommand) -> ArchiveResult:
        """ An abridged version of the archival process, used to extract the info necessary for Twitter posts. Can be
          used for older nominations. """

        page = Page(self.site, command.article_name)
        if not page.exists():
            return ArchiveResult(False, command, f"Target: {command.article_name} does not exist")

        try:
            # Extract the nominated revision
            nom_page_name = self.calculate_nomination_page_name(command)
            nom_page = Page(self.site, nom_page_name)
            nominator = determine_nominator(page=page, nom_type=command.nom_type, nom_page=nom_page)
            projects = self.project_archiver.identify_project_from_nom_page(nom_page)
            return ArchiveResult(True, command, "", page, nom_page, projects, nominator, success=True)

        except ArchiveException as e:
            error_log(e.message)
            return ArchiveResult(False, command, e.message)
        except Exception as e:
            error_log(type(e), e)
            return ArchiveResult(False, command, extract_err_msg(e))

    @staticmethod
    def are_users_different(u1, u2):
        return u1.replace("_", " ").lower() != u2.replace("_", " ").replace(".", "").lower()

    def get_page_and_nom(self, command: ArchiveCommand):
        page = Page(self.site, command.article_name)
        if not page.exists():
            raise ArchiveException(f"Target: {command.article_name} does not exist")

        nom_page_name = self.calculate_nomination_page_name(command)
        nom_page = Page(self.site, nom_page_name)
        if not nom_page.exists():
            raise ArchiveException(f"{nom_page_name} does not exist")

        if command.success:
            self.check_approval_and_fields(nom_page_name, nom_page, self.nom_types[command.nom_type], command.retry)

        return page, nom_page

    def archive_process(self, command: ArchiveCommand) -> ArchiveResult:
        try:
            page, nom_page = self.get_page_and_nom(command)
        except ArchiveException as e:
            return ArchiveResult(False, command, e.message)
        return self.archive_process_after_check(command, page, nom_page)

    def archive_process_after_check(self, command: ArchiveCommand, page: Page, nom_page: Page) -> ArchiveResult:
        """ The core archival process of Jocasta. Given an ArchiveCommand, which specifies the nomination type, article
          name, result, and optional nomination-page suffix, runs through the archival process. The main steps are:
        - Removes the nomination from the parent page
        - Edits the target article to remove the nomination template (and add status if successful)
        - Archives the nomination page
        - Updates the article's talk page with the revision data for the {{Ahh}} template
        - Updates the overall nomination history table with the nomination.
        """
        nom_page_name = nom_page.title()
        talk_page = Page(self.site, f"{self.talk_ns}:{command.article_name}")

        try:
            # Checks for the appropriate Approved template on successful nominations, and rejects users from withdrawing
            # nominations other than their own
            if not command.bypass:
                nom_revision = calculate_nominated_revision(page=page, nom_type=command.nom_type)
                if self.are_users_different(nom_revision['user'], command.requested_by):
                    raise ArchiveException(f"Archive requested by {command.requested_by}, but {page.title()} "
                                           f"was nominated by {nom_revision['user']}")
            projects = self.project_archiver.identify_project_from_nom_page(nom_page)

            self.check_for_redirect_pages(page=page, nom_page=nom_page, talk_page=talk_page)

            total, intro, body, bts = word_count(page.get())
            word_count_text = f"{total} words ({intro} introduction, {body} body, {bts} behind the scenes)"

            # Remove nomination subpage from nomination page
            if not command.multiple:
                log(f"Removing nomination from parent page")
                remove_subpage_from_parent(
                    site=self.site, parent_title=self.nom_types[command.nom_type].nomination_page, retry=command.retry,
                    subpage=f"{command.article_name}{command.suffix}", withdrawn=command.withdrawn)

            # Remove nomination template from the article itself (and add status if necessary)
            if command.success:
                comment = f"Successful {command.nom_type}N"
            elif command.withdrawn:
                comment = f"Closing {command.nom_type}N by nominator request"
            else:
                comment = f"Failed {command.nom_type}N"
            log(f"Marking {command.article_name} as {comment}")
            former_status = self.edit_target_article(
                retry=command.retry, page=page, successful=command.success, nom_type=command.nom_type, comment=comment)
            time.sleep(1)

            # Calculate the revision IDs for the nomination
            completed, nominated = calculate_revisions(page=page, template=f"{command.nom_type}nom", comment=comment)

            # Apply archive template to nomination subpage
            log(f"Archiving {nom_page_name}")
            self.archive_nomination_page(
                retry=command.retry, nom_page=nom_page, nom_type=command.nom_type, successful=command.success,
                withdrawn=command.withdrawn, nominator=nominated["user"], word_count_text=word_count_text)
            time.sleep(1)

            # Create or update the talk page with the {Ahm} status templates
            log("Updating talk page with status history")
            self.update_talk_page(
                talk_page=talk_page, nom_type=command.nom_type, nom_page_name=nom_page_name, successful=command.success,
                nominated=nominated, completed=completed, projects=projects, withdrawn=command.withdrawn)
            time.sleep(1)

            # Update nomination history
            if not command.multiple:
                log("Updating nomination history table")
                new_row = self.build_history_row(
                    successful=command.success, page=page, nom_page_name=nom_page_name, nominated_revision=nominated,
                    completed_revision=completed, withdrawn=command.withdrawn)
                self.update_nomination_history(
                    nom_type=command.nom_type, rows=[new_row], summary=f"Archiving {nom_page_name}", retry=command.retry)

            # For successful nominations, leave a talk page message, and removes upgraded articles from their old page
            if command.success:
                if former_status:
                    self.remove_article_from_previous_status_page(former_status, command.article_name)

                if command.custom_message:
                    self.leave_talk_page_message(
                        header=command.custom_message, nom_type=command.nom_type, article_name=command.article_name,
                        nominator=nominated["user"], archiver=command.requested_by, retry=command.retry)
                elif command.send_message:
                    self.leave_talk_page_message(
                        header=command.article_name, nom_type=command.nom_type, article_name=command.article_name,
                        nominator=nominated["user"], archiver=command.requested_by, retry=command.retry)
                else:
                    log("Talk page message disabled for this command")

            log("Done!")
            return ArchiveResult(True, command, "", page, nom_page, projects, nominated["user"],
                                 nominated=nominated, completion=completed)

        except ArchiveException as e:
            error_log(e.message)
            return ArchiveResult(False, command, e.message)
        except Exception as e:
            error_log(type(e), e)
            return ArchiveResult(False, command, extract_err_msg(e))

    def check_for_redirect_pages(self, *, page: Page, nom_page: Page, talk_page: Page):
        if page.isRedirectPage():
            raise ArchiveException(f"{page.title()} is a redirect page")
        if nom_page.isRedirectPage():
            raise ArchiveException(f"{nom_page.title()} is a redirect page")
        if talk_page.isRedirectPage():
            raise ArchiveException(f"{talk_page.title()} is a redirect page")

    def handle_successful_nomination(self, result: ArchiveResult) -> Tuple[List[str], List[str], dict]:
        """ Followup method for handling successful nominations - updates the rankings table and relevant projects. """
        
        counts = update_current_year_rankings(site=self.site, nominator=result.nominator, nom_type=result.nom_type)

        emojis = set()
        channels = set()
        for project in result.projects:
            e, c = self.update_project(project, result.page, result.nom_page, result.nom_type)
            emojis.add(e)
            channels.add(c)

        return list(emojis - {None}), list(channels - {None}), counts

    def handle_successful_nominations(self, results: List[ArchiveResult]) -> Tuple[List[str], Dict[ArchiveResult, set], dict]:
        """ Followup method for handling successful nominations - updates the rankings table and relevant projects. """

        data = {}
        for r in results:
            if r.nominator not in data:
                data[r.nominator] = 1
            else:
                data[r.nominator] += 1
        counts = update_current_year_rankings_for_multiple(site=self.site, data=data, nom_type=results[0].nom_type)

        emojis = set()
        channels = {}
        projects = {}
        for result in results:
            for project in result.projects:
                if project not in projects:
                    projects[project] = []
                projects[project].append(result)
            channels[result] = set()

        for project, noms in projects.items():
            for result in noms:
                e, c = self.update_project(project, result.page, result.nom_page, result.nom_type)
                if e:
                    emojis.add(e)
                if c:
                    channels[result].add(c)

        return list(emojis), {k: v for k, v in channels.items() if v}, counts

    def update_project(self, project: str, article: Page, nom_page: Page, nom_type: str) -> Tuple[str, str]:
        try:
            return self.project_archiver.add_article_with_pages(
                project=project, article=article, nom_page=nom_page, nom_type=nom_type)
        except Exception as e:
            error_log(type(e), e)
        # noinspection PyTypeChecker
        return None, None

    def check_approval_and_fields(self, name, nom_page, nom_data: NominationType, retry):
        text = nom_page.get()
        u = re.search("Nominated by.*?(\[\[User:|\{\{U\|)(.*?)[\|\]\}]", text)
        if not u:
            raise ArchiveException("Nominated by field lacks a link to nominator's userpage")
        elif not re.search("Nominated by.*?[0-9]+:[0-9]+, [0-9]+ [A-z]+ 20[0-9]{2}", text):
            raise ArchiveException("Nominated by field lacks nomination date")
        elif not re.search("{{(AC|Inq|EC)approved\|", text):
            raise ArchiveException("Nomination page lacks the approved template")
        # elif "Category:Nominations by User:" not in text:
        #     nominator = u.group(2)
        #     if not nominator or nominator == "JocastaBot":
        #         raise ArchiveException("Cannot determine nominator from nomination page and revisions; "
        #                                "please update the nomination accordingly")
        #     ni = "}}</noinclude>" if "}}</noinclude>" in text else "</noinclude>"
        #     sort_text = build_sub_page_name(nom_page.title())
        #     text = text.replace(ni, f"[[Category:Nominations by User:{nominator}|{sort_text}]]{ni}")

        if re.search("{{(AC|Inq|EC)approved\|.*?(\[\[User:|\{\{U\|)", text):
            text = re.sub("({{(AC|Inq|EC)approved\|).*?(\[\[User:|\{\{U\|).*? ([0-9]+:[0-9]+, [0-9]+ [A-z]+ 20[0-9]{2}.*?}})",
                          r"\1\4", text)
        if re.search("{{(AC|Inq|EC)approved\|.*?(\[\[User:|\{\{U\|)", text):
            raise ArchiveException("Approval template contains username, and was unable to remove it")

        first_revision = list(nom_page.revisions(total=1, reverse=True))[0]
        diff = datetime.datetime.now() + datetime.timedelta(hours=self.timezone_offset + 2) - first_revision['timestamp']
        if diff.days < 2:
            raise ArchiveException(f"Nomination for {name} is only {diff.days} days old, cannot pass yet.")

        category = Category(self.site, nom_data.votes_category)
        if not any(nom_page.title() == p.title() for p in category.articles()):
            if retry:
                return True
            raise ArchiveException("Nomination page lacks the number of sufficient votes")

        if diff.days >= 7:
            return True

        text_to_search = extract_support_section(text.lower())

        found = text_to_search.count(nom_data.template)
        votes = [vl.strip() for vl in text_to_search.splitlines() if vl.strip().startswith("#")]
        inq_votes = 0 if nom_data.nom_type == "FAN" else text_to_search.count("{{inq}}")
        user_votes = len(votes) - found - inq_votes

        self.check_vote_counts_for_approval(nom_data, found, len(votes), inq_votes, user_votes)

        missing_user = 0
        missing_date = 0
        for vote in votes:
            if not re.search("(\[\[[Uu]ser:|\{\{[Uu]\|)", vote):
                missing_user += 1
            elif not re.search("[0-9]+:[0-9]+, [0-9]+ [A-z]+ 20[0-9]{2}", vote):
                missing_date += 1

        if missing_date and missing_user:
            raise ArchiveException(f"{missing_date} support votes are missing dates, and {missing_user} votes are "
                                   f"missing usernames")
        elif missing_date:
            raise ArchiveException(f"{missing_date} support votes are missing dates")
        elif missing_user:
            raise ArchiveException(f"{missing_user} support votes are missing usernames")

    @staticmethod
    def check_vote_counts_for_approval(nom_data, found, total_votes, inq_votes, user_votes):
        if found >= nom_data.fast_review_votes:
            return True
        elif found >= nom_data.min_review_votes and total_votes >= nom_data.min_total_votes:
            return True
        elif nom_data.nom_type == "FAN" and found >= (nom_data.min_review_votes + 1) \
                and total_votes >= (nom_data.min_total_votes - 1):
            return True
        elif nom_data.nom_type == "GAN" and inq_votes >= 1:
            if found == nom_data.fast_review_votes - 1:
                return True
            elif found == nom_data.min_review_votes - 1 and total_votes >= nom_data.min_total_votes:
                return True
            raise ArchiveException(f"Nomination only has {found} AgriCorps votes, {inq_votes} Inquisitorius votes,"
                                   f" and {user_votes} user votes; cannot pass yet")
        else:
            raise ArchiveException(f"Nomination only has {found} review board votes and {user_votes} user votes,"
                                   f" cannot pass yet")

    def archive_nomination_page(self, *, nom_page: Page, nom_type: str, successful: bool, retry: bool,
                                withdrawn: bool, nominator: str, word_count_text: str):
        """ Applies the {nom_type}_archive template to the nomination page. """

        if nom_page.isRedirectPage():
            raise ArchiveException(f"{nom_page.title()} is a redirect page")
        text = nom_page.get()
        if successful:
            result = "successful"
        elif withdrawn:
            result = "withdrawn"
        else:
            result = "unsuccessful"

        if retry and "The following discussion is preserved as an" in text:
            log("Nomination already archived, bypassing due to retry")
            return

        lines = text.splitlines()
        new_lines = [f"{{{{subst:{nom_type} archive|{result}}}}}"]
        found = False
        dnw_found = False
        for line in lines:
            if "ANvotes|" in line:
                new_lines.append(re.sub("(\{\{[FGC][AT]Nvotes\|.*?)\}\}", "\\1|1}}", line))
            elif "Nomination comments" in line:
                new_lines.append(line)
                new_lines.append("*'''Date Archived''': ~~~~~")
                new_lines.append(f"*'''Final word count''': {word_count_text}")
            elif line == "<!-- DO NOT WRITE BELOW THIS LINE! -->":
                dnw_found = True
            elif dnw_found:
                if not found and self.nom_types[nom_type].nomination_category in line:
                    found = True
            elif not found and self.nom_types[nom_type].nomination_category in line:
                found = True
            elif "[[Category:Status article nominations that violate the word count requirement]]" in line and "nowiki" not in line:
                new_lines.append(f"<nowiki>{line}</nowiki>")
            else:
                new_lines.append(line)

        category_name = f"Category:Archived nominations by User:{nominator}"
        category = Page(self.site, category_name)
        if not category.exists():
            category.put("Archived nominations by {{U|" + nominator + "}}\n\n__EXPECTUNUSEDCATEGORY__\n[[Category:Archived nominations by user|"
                         + nominator + "]]", "Creating new nomination category")

        sort_text = build_sub_page_name(nom_page.title())
        if not successful:
            sort_text = f" {sort_text}"
        new_lines.append(f"[[{category_name}|{sort_text}]]")
        new_lines.append("</div>")

        new_text = "\n".join(new_lines)
        if not found:
            if retry:
                log("Nomination already archived, bypassing due to retry")
                return
            raise ArchiveException(f"Cannot find category in nomination page")

        self.input_prompts(text, new_text)

        nom_page.put(new_text, f"Archiving {result} nomination")

    def build_history_row(self, successful: bool, page: Page, nom_page_name, nominated_revision: dict,
                          completed_revision: dict, withdrawn=False):
        if successful:
            result = "Success"
        elif withdrawn:
            result = "Withdrawn"
        else:
            result = "Failure"
        text = page.get()
        formatted_link = determine_title_format(page.title(), text)
        nom_date = nominated_revision['timestamp'].strftime('%Y/%m/%d')
        end_date = completed_revision['timestamp'].strftime('%Y/%m/%d')
        user = "{{U|" + nominated_revision['user'] + "}}"

        return f"|-\n| {formatted_link} || {nom_date} || {end_date} || {user} || [[{nom_page_name} | {result}]]"

    def update_nomination_history(self, nom_type, rows, summary, retry=False):
        """ Updates the nomination /History page with the nomination's information. """

        history_page = Page(self.site, self.nom_types[nom_type].nomination_page + "/History")
        text = history_page.get()
        new_lines = []
        for row in rows:
            if retry and row in text:
                continue
            new_lines.append(row)
        if not new_lines:
            return

        new_text = text.replace("|}", "\n".join(new_lines) + "\n|}")

        self.input_prompts(text, new_text)

        history_page.put(new_text, summary)

    def edit_target_article(self, *, page: Page, successful: bool, nom_type: str, comment: str, retry: bool):
        """ Edits the article in question, removing the nomination template and, if the nomination was successful,
         adding the appropriate flag to the {{Top}} template. """

        if page.isRedirectPage():
            raise ArchiveException(f"{page.title()} is a redirect page")
        text = page.get()
        
        former_status = None
        if successful:
            match = re.search("{{[Tt]op.*?\|([cgf]a)[|}]", text)
            if match:
                former_status = match.group(1)

            text1 = re.sub("({{[Tt]op.*?)\|f?[cgf]a([|}])", "\\1\\2", text)
            text2 = re.sub("{{[Tt]op([|\}])", f"{{{{Top|{nom_type.lower()}\\1", text1)
            if text1 == text2:
                raise ArchiveException("Could not add status to {{Top}} template")
        else:
            text2 = text
        text3 = re.sub("{{" + nom_type + "nom[|}].*?\n", "", text2)
        if text2 == text3:
            if retry:
                log("Nomination already archived, bypassing due to retry")
                return
            raise ArchiveException("Could not remove nomination template from page")

        self.input_prompts(text, text3)

        page.put(text3, comment)

        return former_status

    def update_talk_page(self, *, talk_page: Page, nom_type: str, successful: bool, withdrawn: bool,
                         nom_page_name: str, nominated: dict, completed: dict, projects: list):
        """ Updates the talk page of the target article with the appropriate {{Ahm}} templates, and updates the {{Ahf}}
          status. Also adds a {{Talkheader}} template if necessary. """

        nom_type = "CA" if nom_type == "JA" else nom_type
        result = "Success" if successful else ("Withdrawn" if withdrawn else "Failure")
        history_text = build_history_text(nom_type=nom_type, result=result, link=nom_page_name,
                                          start=nominated, completed=completed)

        text, new_text, comment = build_talk_page(talk_page=talk_page, nom_type=nom_type, history_text=history_text,
                                                  successful=successful, project_data=self.project_data, projects=projects)
        if text and f"|oldid={completed['revid']}" in text:
            return

        self.input_prompts(text, new_text)
        talk_page.put(new_text, comment)

    def remove_article_from_previous_status_page(self, former_status, article):
        """ For status articles that are upgraded to a higher status, removes them from their previous status's list page.
          Kind of untested. """

        if former_status == "fa":
            page_name = self.nom_types["FAN"].page
        elif former_status == "ga":
            page_name = self.nom_types["GAN"].page
        elif former_status == "ca":
            page_name = self.nom_types["CAN"].page
        elif former_status == "ft":
            page_name = self.nom_types["FTN"].page
        else:
            return

        page = Page(self.site, page_name)
        if not page.exists():
            log(f"Unexpected state: {page_name}")
            return

        text = page.get()
        lines = []
        for line in text.splitlines():
            if line.count("[[") > 1 and (f"[[{article}]]" in line or f"[[{article}|" in line):
                error_log(f"Unable to remove article, page is in an unexpected state: {line}")
                return
            elif f"[[{article}]]" in line:
                pass
            elif f"[[{article}|" in line:
                pass
            else:
                lines.append(line)

        new_text = "\n".join(lines)
        page.put(new_text, f"Removing newly-promoted {article}")

    def determine_signature(self, user):
        if user in self.signatures:
            return self.signatures[user]
        log(f"No signature found for user {user}! Signature may be invalid")
        return "{{U|" + user + "}}"

    def leave_talk_page_message(self, header: str, nom_type: str, article_name: str, nominator: str, archiver: str, retry=False):
        """ Leaves a talk page message about a successful article nomination on the nominator's talk page. """

        log(nominator, nom_type, self.user_message_data)
        if nominator in self.user_message_data:
            if nom_type in self.user_message_data[nominator]:
                log(f"Bypassing {nom_type} talk page message notification for user {nominator}")
                return

        talk_page = Page(self.site, f"User talk:{nominator}")
        if not talk_page.exists():
            return

        if retry and f"[[{article_name}]]''' has been approved" in talk_page.get():
            return

        signature = self.determine_signature(archiver)
        log(f"{archiver} signature: {signature}")

        new_text = f"=={header}=="
        new_text += "\n{{subst:" + nom_type[:2] + " notify|1=" + article_name + "|2=" + signature + " ~~~~~}}"

        talk_page.put(talk_page.get() + "\n\n" + new_text, f"Notifying user about new {nom_type}: {article_name}")
