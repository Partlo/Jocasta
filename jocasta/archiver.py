import datetime

from pywikibot import Page, Site, showDiff, input_choice, Category
from typing import Tuple, List
import json
import re
import time

from common import ArchiveException, calculate_nominated_revision, calculate_revisions, determine_nominator,\
    determine_title_format, clean_text, log, error_log, extract_err_msg
from data.filenames import *
from data.nom_data import NOM_TYPES, NominationType
from project_archiver import ProjectArchiver
from rankings import blacklisted, update_current_year_rankings


class ArticleInfo:
    def __init__(self, title: str, page_url: str, nom_type: str, nominator: str, projects: List[str] = None):
        self.article_title = title
        self.page_url = page_url
        self.nom_type = nom_type
        self.nominator = nominator
        self.projects = projects or []


class ArchiveCommand:
    def __init__(self, successful: bool, nom_type: str, article_name: str, suffix: str, post_mode: bool, retry: bool,
                 test_mode: bool, withdrawn=False, bypass=False, send_message=True, custom_message=None):
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
    def parse_command(command):
        """ Parses the nomination type, result, article name and optional suffix from the given command. """

        match = re.search("(?P<result>([Ss]uccessful|[Uu]nsuccessful|[Ff]ailed|[Ww]ithdrawn|[Tt]est|[Pp]ost)) (?P<ntype>[CGFJ]A)N: (?P<article>.*?)(?P<suffix> \([A-z]+ nomination\))?(?P<no_msg> \(no message\))?(, | \()?(?P<custom>custom message: .*?\)?)?$",
                          command.strip().replace('\\n', ''))
        if not match:
            raise ArchiveException("Invalid command")

        result_str = clean_text(match.groupdict().get('result')).lower()
        test_mode, post_mode, withdrawn = False, False, False
        if result_str == "successful":
            successful = True
        elif result_str == "unsuccessful" or result_str == "failed":
            successful = False
        elif result_str == "withdrawn":
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
                              send_message=send_message, custom_message=custom_message)


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


# noinspection RegExpRedundantEscape
class Archiver:
    """ A class encapsulating the core archival logic for Jocasta.

    :type project_data: dict[str, dict]
    :type signatures: dict[str, str]
    :type user_message_data: dict[str, list[str]]
    """
    def __init__(self, *, test_mode=False, auto=False, project_data: dict = None, signatures: dict = None,
                 timezone_offset=0):
        self.site = Site(user="JocastaBot")
        self.site.login(user="JocastaBot")
        self.timezone_offset = timezone_offset

        if not project_data:
            with open(PROJECT_DATA_FILE, "r") as f:
                project_data = json.load(f)
        self.project_data = project_data

        if not signatures:
            with open(SIGNATURES_FILE, "r") as f:
                signatures = json.load(f)
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

    @staticmethod
    def calculate_nomination_page_name(command: ArchiveCommand):
        return NOM_TYPES[command.nom_type].nomination_page + f"/{command.article_name}{command.suffix}"

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
        return u1.replace("_", " ") != u2.replace("_", " ")

    def archive_process(self, command: ArchiveCommand) -> ArchiveResult:
        """ The core archival process of Jocasta. Given an ArchiveCommand, which specifies the nomination type, article
          name, result, and optional nomination-page suffix, runs through the archival process. The main steps are:
        - Removes the nomination from the parent page
        - Edits the target article to remove the nomination template (and add status if successful)
        - Archives the nomination page
        - Updates the article's talk page with the revision data for the {{Ahh}} template
        - Updates the overall nomination history table with the nomination.
        """
        page = Page(self.site, command.article_name)
        if not page.exists():
            return ArchiveResult(False, command, f"Target: {command.article_name} does not exist")

        nom_page_name = self.calculate_nomination_page_name(command)
        nom_page = Page(self.site, nom_page_name)
        if not nom_page.exists():
            return ArchiveResult(False, command, f"{nom_page_name} does not exist")

        talk_page = Page(self.site, f"{self.talk_ns}:{command.article_name}")

        try:
            # Checks for the appropriate Approved template on successful nominations, and rejects users from withdrawing
            # nominations other than their own
            if command.success:
                self.check_for_approved_template(nom_page, NOM_TYPES[command.nom_type])
            elif not command.bypass:
                nom_revision = calculate_nominated_revision(page=page, nom_type=command.nom_type)
                if self.are_users_different(nom_revision['user'], command.requested_by):
                    raise ArchiveException(f"Archive requested by {command.requested_by}, but {page.title()} "
                                           f"was nominated by {nom_revision['user']}")
            projects = self.project_archiver.identify_project_from_nom_page(nom_page)

            # Remove nomination subpage from nomination page
            log(f"Removing nomination from parent page")
            self.remove_nomination_from_parent_page(
                retry=command.retry, nom_type=command.nom_type, subpage=f"{command.article_name}{command.suffix}",
                withdrawn=command.withdrawn)

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
            completed, nominated = calculate_revisions(page=page, nom_type=command.nom_type, comment=comment)

            # Apply archive template to nomination subpage
            log(f"Archiving {nom_page_name}")
            self.archive_nomination_page(
                retry=command.retry, nom_page=nom_page, nom_type=command.nom_type, successful=command.success,
                withdrawn=command.withdrawn, nominator=nominated["user"])
            time.sleep(1)

            # Create or update the talk page with the {Ahm} status templates
            log("Updating talk page with status history")
            self.update_talk_page(
                talk_page=talk_page, nom_type=command.nom_type, nom_page_name=nom_page_name, successful=command.success,
                nominated=nominated, completed=completed, projects=projects, withdrawn=command.withdrawn)
            time.sleep(1)

            # Update nomination history
            log("Updating nomination history table")
            self.update_nomination_history(
                nom_type=command.nom_type, page=page, nom_page_name=nom_page_name, successful=command.success,
                nominated_revision=nominated, completed_revision=completed, withdrawn=command.withdrawn)

            # For successful nominations, leave a talk page message, and removes upgraded articles from their old page
            if command.success:
                if former_status:
                    self.remove_article_from_previous_status_page(former_status, command.article_name)

                if command.custom_message:
                    self.leave_talk_page_message(
                        header=command.custom_message, nom_type=command.nom_type, article_name=command.article_name,
                        nominator=nominated["user"], archiver=command.requested_by)
                elif not self.are_users_different(nominated['user'], command.requested_by):
                    log(f"User {command.requested_by} archived their own nomination, so no message necessary")
                elif command.send_message:
                    self.leave_talk_page_message(
                        header=command.article_name, nom_type=command.nom_type, article_name=command.article_name,
                        nominator=nominated["user"], archiver=command.requested_by
                    )
                else:
                    log("Talk page message disabled for this command")

            log("Done!")
            return ArchiveResult(True, command, "", page, nom_page, projects, nominated["user"])

        except ArchiveException as e:
            error_log(e.message)
            return ArchiveResult(False, command, e.message)
        except Exception as e:
            error_log(type(e), e)
            return ArchiveResult(False, command, extract_err_msg(e))

    def handle_successful_nomination(self, result: ArchiveResult) -> Tuple[List[str], List[str]]:
        """ Followup method for handling successful nominations - updates the rankings table and relevant projects. """
        
        update_current_year_rankings(site=self.site, nominator=result.nominator, nom_type=result.nom_type)

        emojis = set()
        channels = set()
        for project in result.projects:
            e, c = self.update_project(project, result.page, result.nom_page, result.nom_type)
            emojis.add(e)
            channels.add(c)

        return list(emojis - {None}), list(channels - {None})

    def update_project(self, project: str, article: Page, nom_page: Page, nom_type: str) -> Tuple[str, str]:
        try:
            return self.project_archiver.add_article_with_pages(
                project=project, article=article, nom_page=nom_page, nom_type=nom_type)
        except Exception as e:
            error_log(type(e), e)
        # noinspection PyTypeChecker
        return None, None

    def check_for_approved_template(self, nom_page, nom_data: NominationType):
        text = nom_page.get()
        if not re.search("{{(AC|Inq|EC)approved\|", text):
            raise ArchiveException("Nomination page lacks the approved template")

        first_revision = list(nom_page.revisions(total=1, reverse=True))[0]
        diff = datetime.datetime.now() - datetime.timedelta(hours=self.timezone_offset) - first_revision['timestamp']
        print(nom_page.title(), diff.days)
        if diff.days < 2:
            raise ArchiveException(f"Nomination is only {diff.days} days old, cannot pass yet.")

        category = Category(self.site, nom_data.votes_category)
        if not any(nom_page.title() == p.title() for p in category.articles()):
            raise ArchiveException("Nomination page lacks the number of sufficient votes")

        if diff.days >= 7:
            return True

        num_votes, min_votes, template = nom_data.review_votes
        text_to_search = text.lower()

        found = text_to_search.count(template)
        if found >= num_votes:
            return True
        elif found >= min_votes:
            inq_votes = text_to_search.count("{{inq}}")
            if (found + inq_votes) >= num_votes:
                return True
            raise ArchiveException(f"Nomination only has {found + inq_votes} review board votes, cannot pass yet")
        else:
            raise ArchiveException(f"Nomination only has {found} review board votes, cannot pass yet")

    def remove_nomination_from_parent_page(self, *, nom_type, subpage, retry: bool, withdrawn: bool):
        """ Removes the {{/<nom title>}} transclusion from the parent nomination page. """

        parent_page = Page(self.site, NOM_TYPES[nom_type].nomination_page)
        if not parent_page.exists():
            raise ArchiveException(f"{NOM_TYPES[nom_type].nomination_page} does not exist")

        expected = "{{/" + subpage + "}}"

        text = parent_page.get()
        if expected not in text:
            if retry:
                log(f"/{subpage} not found in nomination page on retry")
                return
            raise ArchiveException(f"Cannot find /{subpage} in nomination page")

        lines = text.splitlines()
        new_lines = []
        found = False
        white = False
        for line in lines:
            if not found:
                if line.strip() == expected:
                    found = True
                    white = True
                else:
                    new_lines.append(line)
            elif white:
                if line.strip() != "":
                    new_lines.append(line)
                    white = False
            else:
                new_lines.append(line)
        new_text = "\n".join(new_lines)
        if not found:
            if retry:
                log(f"/{subpage} not found in nomination page on retry")
                return
            raise ArchiveException(f"Cannot find /{subpage} in nomination page")

        self.input_prompts(text, new_text)

        if withdrawn:
            parent_page.put(new_text, f"Archiving {subpage} per nominator request")
        else:
            parent_page.put(new_text, f"Archiving {subpage}")

    def archive_nomination_page(self, *, nom_page: Page, nom_type: str, successful: bool, retry: bool,
                                withdrawn: bool, nominator: str):
        """ Applies the {nom_type}_archive template to the nomination page. """

        text = nom_page.get()
        if successful:
            result = "successful"
        elif withdrawn:
            result = "withdrawn"
        else:
            result = "unsuccessful"

        lines = text.splitlines()
        new_lines = [f"{{{{subst:{nom_type} archive|{result}}}}}"]
        found = False
        dnw_found = False
        for line in lines:
            if "ANvotes|" in line:
                new_lines.append(re.sub("(\{\{[FGC]ANvotes\|.*?)\}\}", "\\1|1}}", line))
            elif line == "<!-- DO NOT WRITE BELOW THIS LINE! -->":
                dnw_found = True
            elif dnw_found:
                if not found and NOM_TYPES[nom_type].nomination_category in line:
                    found = True
            elif not found and NOM_TYPES[nom_type].nomination_category in line:
                found = True
            else:
                new_lines.append(line)

        category_name = f"Category:Archived nominations by User:{nominator}"
        category = Page(self.site, category_name)
        if not category.exists():
            category.put("Archived nominations by {{U|" + nominator + "}}\n\n[[Category:Archived nominations by user|"
                         + nominator + "]]", "Creating new nomination category")

        sort_text = "{{SUBPAGENAME}}"
        if nom_page.title().count("/") > 1:
            sort_text = nom_page.title().split("/", 1)[1]
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

    def update_nomination_history(self, nom_type, successful: bool, page: Page, nom_page_name,
                                  nominated_revision: dict, completed_revision: dict, withdrawn: bool):
        """ Updates the nomination /History page with the nomination's information. """

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

        new_row = f"|-\n| {formatted_link} || {nom_date} || {end_date} || {user} || [[{nom_page_name} | {result}]]"

        history_page = Page(self.site, NOM_TYPES[nom_type].nomination_page + "/History")
        text = history_page.get()
        new_text = text.replace("|}", new_row + "\n|}")

        self.input_prompts(text, new_text)

        history_page.put(new_text, f"Archiving {nom_page_name}")

    def edit_target_article(self, *, page: Page, successful: bool, nom_type: str, comment: str, retry: bool):
        """ Edits the article in question, removing the nomination template and, if the nomination was successful,
         adding the appropriate flag to the {{Top}} template. """

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
        if successful:
            result = "Success"
            status = nom_type
        elif withdrawn:
            result = "Withdrawn"
            status = f"F{nom_type}N"
        else:
            result = "Failure"
            status = f"F{nom_type}N"

        history_text = f"""{{{{Ahm
|date={nominated['timestamp'].strftime('%B %d, %Y')}
|oldid={nominated['revid']}
|process={nom_type}N
|result={result}
}}}}
{{{{Ahm
|date={completed['timestamp'].strftime('%B %d, %Y')}
|link={nom_page_name}
|process={status}
|oldid={completed['revid']}
}}}}
{{{{Ahf|status={status}}}}}"""

        # No talk page exists, so we can just create the talk page with the {{Talkheader}} and history templates.
        if not talk_page.exists():
            new_lines = ["{{Talkheader}}"]
            if successful:
                new_lines.append(f"{{{{{nom_type}}}}}")
            new_lines.append("{{Ahh}}")
            new_lines.append(history_text)
            for project in projects:
                project_talk = self.project_data.get(project, {}).get("template")
                if project_talk:
                    new_lines.append("{{" + project_talk + "}}")
            text = "\n".join(new_lines)

            self.input_prompts("", text)
            talk_page.put(text, "Creating talk page with article nomination history")
            return

        text = talk_page.get()
        lines = text.splitlines()
        new_lines = []

        for project in projects:
            project_talk = self.project_data.get(project, {}).get("template")
            if project_talk and project_talk not in text:
                history_text += ("\n{{" + project_talk + "}}")

        # {{Ahh}} template is present in page - add new entries
        if "{{ahh" in text.lower():
            found = False
            for line in lines:
                if "{{CA}}" in line or "{{FA}}" in line or "{{GA}}" in line:
                    log(f"Removing old status template: {line}")
                    continue
                elif "{{FormerCA}}" in line or "{{FormerGA}}" in line or "{{FormerFA}}" in line:
                    log(f"Removing old status template: {line}")
                    continue
                elif "{{ahh" in line.lower():
                    if successful:
                        new_lines.append(f"{{{{{nom_type}}}}}")
                    new_lines.append(line)
                    found = True
                    continue
                elif "{{ahf" in line.lower():
                    if not found:
                        new_lines.append("{{Ahh}}")
                        if successful:
                            new_lines.append(f"{{{{{nom_type}}}}}")
                    new_lines.append(history_text)
                else:
                    new_lines.append(line)
            if not found:
                raise ArchiveException("Could not find {ahf} template")

        # {{Ahh}} template is not present, and no {{Talkheader}} either - add all templates
        elif "{{talkheader" not in text.lower():
            if successful:
                new_lines = ["{{Talkheader}}", f"{{{{{nom_type}}}}}", "{{Ahh}}", history_text, *lines]
            else:
                new_lines = ["{{Talkheader}}", "{{Ahh}}", history_text, *lines]

        # {{Ahh}} template is not present, but {{Talkheader}} is - add the {{Ahh}} templates below the {{Talkheader}}
        else:
            found = False
            for line in lines:
                if "{{talkheader" in line.lower():
                    new_lines.append(line)
                    found = True
                    if successful:
                        new_lines.append(f"{{{{{nom_type}}}}}")
                    new_lines.append("{{Ahh}}")
                    new_lines.append(history_text)
                else:
                    new_lines.append(line)
            if not found:
                new_lines.insert(0, history_text)
                new_lines.insert(0, "{{Ahh}}")
                if successful:
                    new_lines.insert(0, f"{{{{{nom_type}}}}}")

        new_text = "\n".join(new_lines)

        self.input_prompts(text, new_text)

        talk_page.put(new_text, "Updating talk page with article nomination history")

    def remove_article_from_previous_status_page(self, former_status, article):
        """ For status articles that are upgraded to a higher status, removes them from their previous status's list page.
          Kind of untested. """

        if former_status == "fa":
            page_name = NOM_TYPES["FAN"].page
        elif former_status == "ga":
            page_name = NOM_TYPES["GAN"].page
        elif former_status == "ca":
            page_name = NOM_TYPES["CAN"].page
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

    def leave_talk_page_message(self, header: str, nom_type: str, article_name: str, nominator: str, archiver: str, test=False):
        """ Leaves a talk page message about a successful article nomination on the nominator's talk page. """

        log(nominator, nom_type, self.user_message_data)
        if nominator in self.user_message_data:
            if nom_type in self.user_message_data[nominator]:
                log(f"Bypassing {nom_type} talk page message notification for user {nominator}")
                return

        talk_page = Page(self.site, f"User talk:{nominator}")
        if not talk_page.exists():
            return

        signature = self.determine_signature(archiver)
        log(f"{archiver} signature: {signature}")

        new_text = f"=={header}=="
        new_text += "\n{{subst:" + nom_type[:2] + " notify|1=" + article_name + "|2=" + signature + " ~~~~~}}"
        print(new_text)

        if test:
            print(f"Notifying user about new {nom_type}: {article_name}")
        else:
            talk_page.put(talk_page.get() + "\n\n" + new_text, f"Notifying user about new {nom_type}: {article_name}")
