from pywikibot import Page, Site, showDiff, input_choice
import json
import re
import time

from jocasta.common import ArchiveException, calculate_revisions, log, error_log, determine_title_format
from jocasta.data.filenames import *
from jocasta.nominations.data import build_nom_types
from jocasta.nominations.processor import add_subpage_to_parent, remove_subpage_from_parent
from jocasta.nominations.talk_page import build_history_text, build_history_text_for_removal, build_talk_page


class Reviewer:
    suffixes = ["", " (second)", " (third)", " (fourth)", " (fifth)", " (sixth)", " (seventh)", " (eighth)", " (ninth)", " (tenth)"]

    def __init__(self, *, nom_types: dict=None, auto: bool):
        self.site = Site(user="JocastaBot")
        self.site.login(user="JocastaBot")

        if not nom_types:
            with open(NOM_DATA_FILE, "r") as f:
                nom_types = build_nom_types(json.load(f))
        self.nom_types = nom_types

        self.auto = auto

    def input_prompts(self, old_text, new_text):
        if self.auto:
            return

        showDiff(old_text, new_text, context=3)

        choice = input_choice('Do you want to accept these changes?', [('Yes', 'y'), ('No', 'n')], default='N')
        if choice == 'n':
            assert False

    @staticmethod
    def determine_status_type(text):
        top = re.search("\{\{[Tt]op(.*?)}}", text)
        if not top:
            return None
        params = top.group(1)
        if "|ca" in params or "|pca" in params:
            return "Comprehensive"
        elif "|ga" in params or "|pga" in params:
            return "Good"
        elif "|fa" in params or "|pfa" in params:
            return "Featured"
        return None

    def determine_pages(self, article_name):
        page = Page(self.site, article_name)
        if not page.exists():
            raise Exception(f"Target: {article_name} does not exist")

        status = self.determine_status_type(page.get())
        if not status:
            raise Exception(f"Cannot determine status for article {article_name}")

        review_page, parent, subpage = self.determine_current_review_page(status, article_name)
        if not review_page.exists():
            raise Exception(f"Target: {review_page.title()} does not exist")

        if article_name.startswith("User:"):
            talk_page = Page(self.site, article_name.replace("User:", "User talk:"))
        else:
            talk_page = Page(self.site, f"Talk:{article_name}")
        return page, status, review_page, parent, subpage, talk_page

    @staticmethod
    def determine_requested(review_page):
        revision = review_page.oldest_revision
        if revision['user'] != "JocastaBot":
            return revision['user']
        r = re.search("Requested By.*?: (.*?)$", review_page.text)
        return "Unknown" if not r else r.group(1)

    def create_new_review_page(self, article_name, requested_by):
        page = Page(self.site, article_name)
        if not page.exists():
            raise Exception(f"Target: {article_name} does not exist")

        status = self.determine_status_type(page.get())
        if not status:
            raise Exception(f"Cannot determine status for article {article_name}")

        review_page, suffix = self.build_review_page(status, article_name, requested_by)

        add_subpage_to_parent(review_page, self.site)

        self.mark_page_as_under_review(page, status, suffix)
        return review_page.title()

    def mark_review_as_complete(self, article_name, retry):
        """ Marks a review as passed, archiving the review page and updating the target article and its history. """

        page, status, review_page, parent, subpage, talk_page = self.determine_pages(article_name)

        try:
            comment = f"{status} article successfully passed review"
            log(f"Marking {article_name} as {comment}")
            self.remove_review_template(page=page, comment=comment, successful=True, retry=retry)
            time.sleep(1)

            # Calculate the revision IDs for the review
            completed, started = calculate_revisions(page=page, template=f"{status[0]}Areview", comment=comment,
                                                     comment2=f"Marking {status} article as under review")

            log("Archiving review section")
            requested = self.determine_requested(review_page)
            self.archive_review_page(review_page=review_page, status=status, successful=True, retry=retry)
            time.sleep(1)

            log("Removing review from parent page")
            remove_subpage_from_parent(site=self.site, parent_title=parent, subpage=subpage, retry=retry)
            time.sleep(1)

            log("Updating review history")
            self.update_review_history(
                page=page, status=status, successful=True, review_page_name=review_page.title(), retry=retry,
                completed_revision=completed, nominated_revision=started, requested_by=requested)
            time.sleep(1)

            log("Updating talk page with review history")
            self.update_talk_page(
                talk_page=talk_page, status=status, successful=True, review_page_name=review_page.title(),
                started=started, completed=completed)

        except ArchiveException as e:
            error_log(e.message)
        except Exception as e:
            error_log(type(e), e)
        return status

    def mark_article_as_on_probation(self, article_name, retry):
        """ Marks a review as passed, archiving the review page and updating the target article and its history. """

        page, status, review_page, parent, subpage, talk_page = self.determine_pages(article_name)

        try:
            comment = f"{status} article under review and put on probation"
            log(f"Marking {article_name} as {comment}")
            self.change_to_probation(page=page, comment=comment, retry=retry)
            time.sleep(1)

            # Calculate the revision IDs for the review
            completed, started = calculate_revisions(page=page, template=f"{status[0]}Areview", comment=comment,
                                                     comment2=f"Marking {status} article as under review")

            requested = self.determine_requested(review_page)

            log("Updating review history")
            self.update_review_history(
                page=page, status=status, successful=False, review_page_name=review_page.title(), retry=retry,
                completed_revision=completed, nominated_revision=started, requested_by=requested)
            time.sleep(1)

            log("Updating talk page with review history")
            self.update_talk_page(
                talk_page=talk_page, status=status, successful=False, review_page_name=review_page.title(),
                started=started, completed=completed)

        except ArchiveException as e:
            error_log(e.message)
        except Exception as e:
            error_log(type(e), e)
        return status

    def mark_article_as_former(self, article_name, retry):
        page, status, review_page, parent, subpage, talk_page = self.determine_pages(article_name)

        try:
            comment = f"Article failed review and {status} status has been revoked"
            log(f"Marking {article_name} as {comment}")
            self.remove_review_template(page=page, comment=comment, successful=False, retry=retry)
            time.sleep(1)

            # Calculate the revision IDs for the review
            completed, started = calculate_revisions(page=page, template=f"{status[0]}Areview", comment=comment,
                                                     comment2=f"Marking {status} article as under review")

            log("Archiving review section")
            requested = self.determine_requested(review_page)
            self.archive_review_page(review_page=review_page, status=status, successful=True, retry=retry)
            time.sleep(1)

            log("Removing review from parent page")
            remove_subpage_from_parent(site=self.site, parent_title=parent, subpage=subpage, retry=retry)
            time.sleep(1)

            log("Updating review history")
            self.update_review_history_with_removal(page=page, status=status, review_page_name=review_page.title(),
                                                    started=started, completed=completed, requested_by=requested)
            time.sleep(1)

            log("Updating talk page with status removal")
            self.update_talk_page_with_removal(
                talk_page=talk_page, status=status, review_page_name=review_page.title(), revision=completed)

        except ArchiveException as e:
            error_log(e.message)
        except Exception as e:
            error_log(type(e), e)
        return status

    def mark_page_as_under_review(self, page, status, suffix):
        if page.isRedirectPage():
            raise Exception(f"{page.title()} is a redirect page")
        text = page.get()

        if not re.search("{{[Tt]op.*?}}", text):
            raise Exception(f"Cannot find Top template on {page.title()}")

        st = f"|{suffix}" if suffix else ""
        text1 = re.sub("({{[Tt]op.*?}})", "\\1\n{{" + status[0] + "Areview" + st + "}}", text)
        if text1 == text:
            raise Exception("Could not add review template to page")

        self.input_prompts(text, text1)

        page.put(text1, f"Marking {status} article as under review")
        return

    def change_to_probation(self, *, page, comment: str, retry):
        if page.isRedirectPage():
            raise Exception(f"{page.title()} is a redirect page")
        text = page.get()

        text1 = re.sub("({{[Tt]op.*?\|)([cgf]a)([|}])", "\\1p\\2\\3", text)
        if text1 == text:
            if retry:
                log(f"Cannot update status on article")
                return
            raise Exception("Could not add status to {{Top}} template")

        self.input_prompts(text, text1)

        page.put(text1, comment)

    def remove_review_template(self, *, page, comment: str, successful, retry):
        if page.isRedirectPage():
            raise Exception(f"{page.title()} is a redirect page")
        text = page.get()

        text1 = re.sub("\n{{[FGC]Areview.*?}}", "", text)
        if not successful:
            text1 = re.sub("({{[Tt]op.*?\|)p([cgf]a)([|}])", "\\1f\\2\\3", text1)
        if text1 == text:
            if retry:
                log("Review template already removed, bypassing due to retry")
                return
            raise Exception("Could not add status to {{Top}} template")

        self.input_prompts(text, text1)

        page.put(text1, comment)

    def determine_current_review_page(self, status, article_name):
        parent = f"Wookieepedia:{status} article reviews"
        base_review_page_name = f"{parent}/{article_name}"
        target = None
        for i, s in enumerate(self.suffixes):
            page = Page(self.site, base_review_page_name + s)
            if not page.exists():
                break
            target = page

        if target is None or not target.exists():
            raise Exception(f"Review page exists or is null: {target}")
        subpage = target.title().replace(f"{parent}/", "")
        return target, parent, subpage

    def build_review_page(self, status, article_name, requested_by):
        base_review_page_name = f"Wookieepedia:{status} article reviews/{article_name}"
        review_page, suffix = None, None
        for i, s in enumerate(self.suffixes):
            review_page = Page(self.site, base_review_page_name + s)
            if not review_page.exists():
                suffix = s
                break

        if review_page is None or review_page.exists():
            raise Exception(f"Review page exists or is null: {review_page}")

        text = f"""===[[{article_name}]]===
*'''Requested By''': {requested_by}
*'''Date Requested''': ~~~~~

{{{{{status[0]}ARvotes|{review_page.title()}}}}}
====Support====

====Object====

====Comments====

<!-- DO NOT WRITE BELOW THIS LINE! -->
<noinclude>{{{{SpecialCategorizer|[[Category:Wookieepedia {status} article review pages]]}}}}</noinclude>"""

        review_page.put(text, f"Creating new review page for {status} article: {article_name}")
        return review_page, suffix

    def archive_review_page(self, review_page, status, successful: bool, retry: bool):
        text = review_page.get()
        if "Date Archived" in text:
            if retry:
                log("Review template already archived, bypassing due to retry")
                return
            raise Exception(f"{review_page} has already been archived")

        template = "{{subst:" + status[0] + "AR archive"
        if not successful:
            template += "|failed"
        template += "}}"
        new_lines = [template]

        lines = text.splitlines()
        for line in lines:
            if "Date Requested" in line:
                new_lines.append(line)
                new_lines.append("*'''Date Archived''': ~~~~~")
            else:
                new_lines.append(line)

        new_lines.append("</div>")
        text = "\n".join(new_lines)
        review_page.put(text, "Archiving review page")

    def update_talk_page(self, *, talk_page: Page, status: str, successful: bool, review_page_name: str, started: dict,
                         completed: dict):
        nom_type = status[0] + "A"
        result = "Kept" if successful else "Probation"
        history_text = build_history_text(nom_type=nom_type, result=result, link=review_page_name,
                                          start=started, completed=completed)

        text, new_text, comment = build_talk_page(talk_page=talk_page, nom_type=nom_type, history_text=history_text,
                                                  successful=successful, project_data={}, projects=[])

        self.input_prompts(text, new_text)
        talk_page.put(new_text, comment)

    def update_talk_page_with_removal(self, *, talk_page: Page, status: str, review_page_name: str, revision: dict):
        nom_type = status[0] + "A"
        history_text = build_history_text_for_removal(nom_type=nom_type, link=review_page_name, revision=revision)

        text, new_text, comment = build_talk_page(talk_page=talk_page, nom_type=nom_type, history_text=history_text,
                                                  successful=False, project_data={}, projects=[])

        self.input_prompts(text, new_text)
        talk_page.put(new_text, comment)

    def update_review_history(self, *, page: Page, status, successful: bool, review_page_name, retry: bool,
                              nominated_revision: dict, completed_revision: dict, requested_by):
        """ Updates the nomination /History page with the nomination's information. """

        if successful:
            result = "Kept"
        else:
            result = "Probation"
        formatted_link = determine_title_format(page.title(), page.get())
        nom_date = nominated_revision['timestamp'].strftime('%Y/%m/%d')
        end_date = completed_revision['timestamp'].strftime('%Y/%m/%d')
        user = requested_by or "Unknown"

        new_row = f"|-\n| {formatted_link} || <!--1-->{nom_date} || <!--2-->{end_date} || {user} || [[{review_page_name} |<!--3-->{result}]]"

        history_page = Page(self.site, f"Wookieepedia:{status} article reviews/History")
        text = history_page.get()
        if retry and f"[[{review_page_name}" in text:
            return
        new_text = text.replace("|}", new_row + "\n|}")

        self.input_prompts(text, new_text)

        history_page.put(new_text, f"Archiving {review_page_name}")

    def update_review_history_with_removal(self, *, page: Page, status, review_page_name, started: dict, completed: dict, requested_by):
        start_date = started['timestamp'].strftime('%Y/%m/%d')
        new_date = completed['timestamp'].strftime('%Y/%m/%d')

        history_page = Page(self.site, f"Wookieepedia:{status} article reviews/History")
        text = history_page.get()
        lines = text.splitlines()
        new_lines = []
        found = False
        for line in lines:
            if f"[[{review_page_name} |" in line or f"[[{review_page_name}|" in line:
                found = True
                new_line = re.sub("<!--2-->(.*?)\|\|", f"<!--2-->{new_date} ||", line)
                new_line = re.sub("<!--3-->(.*?)]]", f"<!--3-->Revoked]]", new_line)
                new_lines.append(new_line)

        if found:
            new_text = "\n".join(new_lines)
        else:
            text = page.get()
            formatted_link = determine_title_format(page.title(), text)
            new_row = f"|-\n| {formatted_link} || {start_date} || <!--2-->{new_date} || {requested_by} || [[{review_page_name} | Revoked]]"
            new_text = text.replace("|}", new_row + "\n|}")

        self.input_prompts(text, new_text)

        history_page.put(new_text, f"Archiving {review_page_name}")
