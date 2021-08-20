import pywikibot
from pywikibot import Page, Category
import re

from project_archiver import ProjectArchiver
from data.nom_data import *

DUMMY = "Wookieepedia:DummyCategoryPage"


def load_current_nominations(site):
    nominations = {}
    for nom_type, nom_data in NOM_TYPES.items():
        nominations[nom_type] = []
        category = Category(site, nom_data.nomination_category)
        for page in category.articles():
            if "/" not in page.title():
                continue
            elif page.title() not in nominations[nom_type]:
                nominations[nom_type].append(page.title())

    return nominations


def check_for_new_nominations(site, current_nominations: dict):
    """ :rtype: dict[str, list[Page]] """
    new_nominations = {}
    for nom_type, nom_data in NOM_TYPES.items():
        new_nominations[nom_type] = set()
        category = Category(site, nom_data.nomination_category)
        for page in category.articles():
            if "/" not in page.title():
                continue
            elif page.title() not in current_nominations[nom_type]:
                print(f"New {nom_data.name} article nomination detected: {page.title().split('/', 1)[1]}")
                new_nominations[nom_type].add(page)
                current_nominations[nom_type].append(page.title())

    return new_nominations


def add_categories_to_nomination(nom_page: Page, project_archiver: ProjectArchiver):
    old_text = nom_page.get()

    match = re.search("Nominated by.*?(User:|U\|)(.*?)[\]\|/]", old_text)
    if match:
        user = match.group(2).strip()
    else:
        user = nom_page.revisions(reverse=True, total=1)[0]["user"]

    cat_sort = "{{SUBPAGENAME}}"
    if nom_page.title().count("/") > 1:
        cat_sort = nom_page.title().split("/", 1)[1]
    category_name = f"Category:Nominations by User:{user}"
    category = Page(project_archiver.site, category_name)
    if not category.exists():
        category.put("Active nominations by {{U|" + user + "}}\n\n[[Category:Nominations by user|" + user + "]]", "Creating new nomination category")
        # dummy_page = Page(project_archiver.site, DUMMY)
        # dummy_page.put(dummy_page.get() + f"\n[[{category_name}]]", "Adding new category to maintenance page")

    new_text = old_text.replace("[[{category_name}|{cat_sort}]]", "")
    categories = []
    if category_name not in new_text:
        categories.append(f"[[{category_name}|{cat_sort}]]")
    projects = project_archiver.identify_project_from_nom_page(nom_page)
    for project in projects:
        if f"[[Category:WookieeProject {project}" not in new_text:
            categories.append(f"[[Category:WookieeProject {project}|{cat_sort}]]")

    if "|}}</noinclude>" in new_text:
        new_text = new_text.replace("|}}</noinclude>", "".join(categories) + "|}}</noinclude>")
    elif "}}</noinclude>" in new_text:
        new_text = new_text.replace("}}</noinclude>", "".join(categories) + "}}</noinclude>")
    elif "</noinclude>" not in new_text:
        print(f"Missing noinclude tags!")
        if categories:
            new_text += ("\n<noinclude>" + "".join(categories) + "</noinclude>")
    else:
        new_text = new_text.replace("</noinclude>", "".join(categories) + "</noinclude>")

    new_text = re.sub("\[\[Category:WookieeProject [^\|]+\]\]", "", new_text)

    if old_text != new_text:
        pywikibot.showDiff(old_text, new_text)
        nom_page.put(new_text, "Adding user-nomination and WookieeProject categories")

    parent_page_title, subpage = nom_page.title().split("/", 1)
    parent_page = Page(project_archiver.site, parent_page_title)
    if not parent_page.exists():
        raise Exception(f"{parent_page_title} does not exist")

    text = parent_page.get()
    expected = "{{/" + subpage + "}}"
    if expected not in text:
        print(f"Nomination missing from parent page, adding: {subpage}")
        text += f"\n\n{expected}"
        parent_page.put(text, f"Adding new nomination: {subpage}")

    return projects

