from pywikibot import Page, Category, Site, showDiff
from typing import Dict, List
import re

from jocasta.common import log, error_log, extract_nominator, word_count, validate_word_count, build_sub_page_name
from jocasta.nominations.data import NominationType
from jocasta.nominations.project_archiver import ProjectArchiver

DUMMY = "Wookieepedia:DummyCategoryPage"


def load_current_nominations(site, nom_types: Dict[str, NominationType]) -> Dict[str, List[str]]:
    """ Loads all currently-active status article nominations from the site. """

    nominations = {}
    for nom_type, nom_data in nom_types.items():
        nominations[nom_type] = []
        category = Category(site, nom_data.nomination_category)
        for page in category.articles():
            if "/" not in page.title():
                continue
            elif page.title() not in nominations[nom_type]:
                nominations[nom_type].append(page.title())

    return nominations


def load_current_reviews(site, nom_types: Dict[str, NominationType]) -> Dict[str, List[str]]:
    """ Loads all currently-active status article reviews from the site. """

    reviews = {}
    for nom_type, nom_data in nom_types.items():
        reviews[nom_type] = []
        category = Category(site, nom_data.review_category)
        for page in category.articles():
            if "/" not in page.title():
                continue
            elif page.title() not in reviews[nom_type]:
                reviews[nom_type].append(page.title())

    return reviews


def check_for_new_nominations(site, nom_types: Dict[str, NominationType], current_nominations: dict) -> Dict[str, List[Page]]:
    """ Loads all currently-active status article nominations from the site, compares them to the previously-stored
      data, and returns the new nominations. """

    new_nominations = {}
    for nom_type, nom_data in nom_types.items():
        if not nom_type.endswith("N"):
            continue
        new_nominations[nom_type] = []
        category = Category(site, nom_data.nomination_category)
        for page in category.articles():
            if "/" not in page.title():
                continue
            elif page.title() not in current_nominations[nom_type]:
                log(f"New {nom_data.name} article nomination detected: {page.title().split('/', 1)[1]}")
                new_nominations[nom_type].append(page)
                current_nominations[nom_type].append(page.title())

    return new_nominations


def check_for_new_reviews(site, nom_types: Dict[str, NominationType], current_reviews: dict) -> Dict[str, List[Page]]:
    """ Loads all currently-active status article reviews from the site, compares them to the previously-stored
      data, and returns the new reviews. """

    new_reviews = {}
    for nom_type, nom_data in nom_types.items():
        if not nom_type.endswith("N"):
            continue
        new_reviews[nom_type] = []
        category = Category(site, nom_data.review_category)
        for page in category.articles():
            if "/" not in page.title():
                continue
            elif page.title() not in current_reviews[nom_type]:
                log(f"New {nom_data.name} article review detected: {page.title().split('/', 1)[1]}")
                new_reviews[nom_type].append(page)
                current_reviews[nom_type].append(page.title())

    return new_reviews


def add_categories_to_nomination(nom_page: Page, project_archiver: ProjectArchiver) -> List[str]:
    """ Given a new status article nomination, this function adds the nomination to the parent page if it is not
     already listed there, adds the 'Nominations by User:<X>' category if it's not present, and adds any relevant
     WookieeProject categories to the nomination as well. """

    old_text = nom_page.get()
    user = extract_nominator(nom_page, old_text)

    target = nom_page.title().split("/", 1)[1]

    # add the Nominations by User:X category, and create it if it's the first time a user has nominated anything
    cat_sort = build_sub_page_name(nom_page.title())
    category_name = f"Category:Nominations by User:{user}"
    category = Page(project_archiver.site, category_name)
    if not category.exists():
        category.put("Active nominations by {{U|" + user + "}}\n\n[[Category:Nominations by user|" + user + "]]", "Creating new nomination category")

    # Add the WookieeProject categories to the nomination if any are necessary
    new_text = old_text.replace("[[{category_name}|{cat_sort}]]", "")
    categories = []
    if category_name not in new_text:
        categories.append(f"[[{category_name}|{cat_sort}]]")
    projects = project_archiver.identify_project_from_nom_page(nom_page)
    for project in projects:
        if f"[[Category:WookieeProject {project}" not in new_text:
            categories.append(f"[[Category:WookieeProject {project}|{cat_sort}]]")

    # Add the categories to the bottom of the nomination page
    if "|}}</noinclude>" in new_text:
        new_text = new_text.replace("|}}</noinclude>", "".join(categories) + "|}}</noinclude>")
    elif "}}</noinclude>" in new_text:
        new_text = new_text.replace("}}</noinclude>", "".join(categories) + "}}</noinclude>")
    elif "</noinclude>" not in new_text:
        error_log(f"Missing noinclude tags!")
        if categories:
            new_text += ("\n<noinclude>" + "".join(categories) + "</noinclude>")
    else:
        new_text = new_text.replace("</noinclude>", "".join(categories) + "</noinclude>")

    new_text = re.sub("\[\[Category:WookieeProject [^\|]+\]\]", "", new_text)

    if old_text != new_text:
        showDiff(old_text, new_text)
        nom_page.put(new_text, "Adding user-nomination and WookieeProject categories")
    return projects


def add_nom_word_count(site, nom_title, text, check_count):
    target_title = re.sub(" \((first|second|third|fourth|fifth|sixth) nomination\)", "", nom_title.split("/", 1)[1])
    status = "Featured" if "Featured article" in nom_title else ("Good" if "Good article" in nom_title else "Comprehensive")
    target = Page(site, target_title)
    if not target.exists():
        raise Exception(f"{target_title} does not exist")
    elif target.isRedirectPage():
        raise Exception(f"{target_title} is a redirect page")

    total, intro, body, bts = word_count(target.get())
    requirement_violated = validate_word_count(status, total, intro, body)

    new_text = []
    for line in text.splitlines():
        if "*'''WookieeProject (optional)''':" in line:
            new_text.append(f"*'''Word count at nomination time''': {total} words ({intro} introduction, {body} body, {bts} behind the scenes)")
        new_text.append(line)
        if check_count and requirement_violated and "===Object===" in line:
            new_text.append("=====JocastaBot=====")
            new_text.append(f"*Current word count violates {status} requirements: {requirement_violated}. ~~~~")
    return "\n".join(new_text)


def add_subpage_to_parent(target: Page, site: Site):
    # Ensure that the nomination is present in the parent nomination page
    parent_page_title, subpage = target.title().split("/", 1)
    parent_page = Page(site, parent_page_title)
    if not parent_page.exists():
        raise Exception(f"{parent_page_title} does not exist")

    text = parent_page.get()
    expected = "{{/" + subpage + "}}"
    if expected not in text:
        log(f"Nomination missing from parent page, adding: {subpage}")
        text += f"\n\n{expected}"
        parent_page.put(text, f"Adding new nomination: {subpage}")


def remove_subpage_from_parent(*, site: Site, parent_title, subpage, retry: bool, withdrawn=False):
    parent_page = Page(site, parent_title)
    if not parent_page.exists():
        raise Exception(f"{parent_title} does not exist")

    expected = "{{/" + subpage + "}}"

    text = parent_page.get()
    if expected not in text:
        if retry:
            log(f"/{subpage} not found in nomination page on retry")
            return
        raise Exception(f"Cannot find /{subpage} in nomination page")

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
        raise Exception(f"Cannot find /{subpage} in nomination page")

    if withdrawn:
        parent_page.put(new_text, f"Archiving {subpage} per nominator request")
    else:
        parent_page.put(new_text, f"Archiving {subpage}")
