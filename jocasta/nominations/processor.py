import pywikibot
from pywikibot import Page, Category
from typing import Dict, List
import re

from jocasta.common import log, error_log, extract_nominator
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


def add_categories_to_nomination(nom_page: Page, project_archiver: ProjectArchiver) -> List[str]:
    """ Given a new status article nomination, this function adds the nomination to the parent page if it is not
     already listed there, adds the 'Nominations by User:<X>' category if it's not present, and adds any relevant
     WookieeProject categories to the nomination as well. """

    old_text = nom_page.get()
    user = extract_nominator(nom_page, old_text)

    # add the Nominations by User:X category, and create it if it's the first time a user has nominated anything
    cat_sort = "{{SUBPAGENAME}}"
    if nom_page.title().count("/") > 1:
        cat_sort = nom_page.title().split("/", 1)[1]
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
        pywikibot.showDiff(old_text, new_text)
        nom_page.put(new_text, "Adding user-nomination and WookieeProject categories")
    return projects


def add_nomination_to_page(nom_page: Page, project_archiver: ProjectArchiver):
    # Ensure that the nomination is present in the parent nomination page
    parent_page_title, subpage = nom_page.title().split("/", 1)
    parent_page = Page(project_archiver.site, parent_page_title)
    if not parent_page.exists():
        raise Exception(f"{parent_page_title} does not exist")

    text = parent_page.get()
    expected = "{{/" + subpage + "}}"
    if expected not in text:
        log(f"Nomination missing from parent page, adding: {subpage}")
        text += f"\n\n{expected}"
        parent_page.put(text, f"Adding new nomination: {subpage}")
