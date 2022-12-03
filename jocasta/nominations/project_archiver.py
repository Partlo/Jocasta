from datetime import datetime
from pywikibot import Page, Site
import re
import json
from typing import List, Optional, Tuple

from jocasta.common import determine_title_format, determine_nominator, log, error_log
from jocasta.data.filenames import *
from jocasta.nominations.data import NominationType, build_nom_types
from jocasta.nominations.novels import add_article_to_tables, rebuild_novels_page_text, parse_novel_page_tables


# noinspection RegExpRedundantEscape
class ProjectArchiver:
    """ Centralized class for logic dealing with WookieeProjects.

    :type project_data: dict[str, dict]
    :type nom_types: dict[str, NominationType]
    """

    BLANK = "File:Blank portrait.svg"

    def __init__(self, site=None, project_data: dict=None, nom_types: dict=None):
        self.site = site or Site(user="JocastaBot")
        self.site.login()
        if not project_data:
            with open(PROJECT_DATA_FILE, "r") as f:
                project_data = json.load(f)
        self.project_data = project_data

        if not nom_types:
            with open(NOM_DATA_FILE, "r") as f:
                nom_types = build_nom_types(json.load(f))
        self.nom_types = nom_types

    def find_project_from_shortcut(self, shortcut) -> Optional[str]:
        for project, data in self.project_data.items():
            match = [s for s in data["shortcut"] if s.upper() == shortcut.upper()]
            if match:
                return project
        return None

    def identify_project_from_nom_page_name(self, nom_page_name: str):
        return self.identify_project_from_nom_page(Page(self.site, nom_page_name))

    def identify_project_from_nom_page(self, nom_page: Page) -> List[str]:
        """ Parses the WookieeProject field from a nomination page to identify the related WookieeProjects. """

        text = nom_page.get()
        match = re.search("'+WookieeProject.*'+:(.*)", text)
        if not match:
            return []

        project_text = match.group(1).strip().upper()
        if not project_text:
            return []

        projects = []
        for project_name, data in self.project_data.items():
            if f"WookieeProject {project_name}".upper() in project_text:
                projects.append(project_name)
            elif project_name.upper() in project_text:
                projects.append(project_name)
            else:
                shortcuts = data.get("shortcut", []) or []
                for shortcut in shortcuts:
                    if shortcut.upper() in project_text:
                        projects.append(project_name)
                        break

        return projects

    def emoji_for_project(self, project) -> str:
        e = self.project_data.get(project, {}).get("emoji", "wook")
        if e == ":stars:":
            e = "🌠"
        return e

    @staticmethod
    def determine_continuity(article: Page) -> str:
        if "/Legends" in article.title():
            return "Legends"
        elif re.search("\{\{[Tt]op.*?\|leg[\|\}]", article.get()):
            return "Legends"
        elif re.search("\{\{[Tt]op.*?\|canon=.*?\}\}", article.get()):
            return "Legends"
        else:
            return "Canon"

    def add_article_with_pages(self, article: Page, nom_page: Page, project, nom_type, old=False) \
            -> Tuple[Optional[str], Optional[str]]:
        """ Adds a single article to the target project's portfolio page. Wrapper around the text-generator function """

        target_project = self.project_data.get(project)
        if not target_project:
            raise Exception(f"No project data found for {project}")
        elif not target_project.get(f"{nom_type}N"):
            raise Exception(f"{nom_type} not found in project")

        props = target_project[f"{nom_type}N"]

        continuity = self.determine_continuity(article)
        if props.get("continuitySplit") and continuity == "Legends":
            page = Page(self.site, props["page"] + "/Legends")
        else:
            page = Page(self.site, props["page"])

        nominator = determine_nominator(page=article, nom_type=nom_type, nom_page=nom_page)

        try:
            page_text = "" if not page.exists() else page.get()
            text = self.add_article_to_page_text(
                page_text=page_text, article=article, nom_type=nom_type, nom_page=nom_page, props=props,
                continuity=continuity, nominator=nominator, old=old)
            if not text:
                log("No update required")
                return None, None
            page.put(text, f"Adding new {nom_type}: {article.title()}")
        except Exception as e:
            error_log(f"{type(e)}: {e}")
            return None, None

        if project == "Novels":
            passed_date = datetime.now() if not old else self.identify_completion_date(article.title(), nom_type)
            try:
                self.add_articles_to_novel_pages([{
                    "continuity": continuity,
                    "article": article,
                    "user": nominator,
                    "date": passed_date,
                    "nom_page": nom_page.title()
                }], nom_type, old)
            except Exception as e:
                error_log(f"{type(e)}: {e}")
                return None, None

        emoji = target_project.get("emoji", "wook")
        if emoji == ":stars:":
            emoji = "🌠"
        return emoji, target_project.get("channel")

    def add_single_article_to_page(self, project: str, article_title: str, nom_page_title: str, nom_type: str):
        """  Adds the given article & nomination to the target project's portfolio page, generating Page objects for
          the article and nomination pages before calling the main function. """

        article = Page(self.site, article_title)
        if not article.exists():
            raise Exception(f"{article_title} does not exist")

        nom_page_title = self.nom_types[nom_type].nomination_page + f"/{nom_page_title or article_title}"

        nom_page = Page(self.site, nom_page_title)
        if not nom_page.exists() and self.project_data.get(project, {}).get(f"{nom_type}N", {}).get("format") != "alphabet":
            raise Exception(f"{nom_page_title} does not exist")

        self.add_article_with_pages(project=project, article=article, nom_page=nom_page, nom_type=nom_type, old=True)

    def add_multiple_articles_to_page(self, project, nom_type, articles: list) -> Optional[str]:
        """ Adds multiple articles for the given nomination type to the target project. """

        target_project = self.project_data.get(project)
        if not target_project:
            raise Exception(f"No project data found for {project}")
        elif not target_project.get(f"{nom_type}N"):
            raise Exception(f"{nom_type} not found in project")

        props = target_project[f"{nom_type}N"]

        main_page = Page(self.site, props["page"])
        main_page_text = "" if not main_page.exists() else main_page.get()

        legends_page = None
        legends_page_text = None
        if props.get("continuitySplit"):
            legends_page = Page(self.site, props["page"] + "/Legends")
            legends_page_text = "" if not legends_page.exists() else legends_page.get()

        failed = []
        data = []
        for article_title in articles:
            article = Page(self.site, article_title)
            if not article.exists():
                failed.append(article_title)
                continue

            nom_page_title = self.nom_types[nom_type].nomination_page + f"/{article_title}"
            nom_page = Page(self.site, nom_page_title)
            if not nom_page.exists() and self.project_data.get(project, {}).get(f"{nom_type}N", {}).get(
                    "format") != "alphabet":
                failed.append(article_title)
                continue

            nominator = determine_nominator(page=article, nom_type=nom_type, nom_page=nom_page)
            continuity = self.determine_continuity(article)
            if legends_page_text is not None and continuity == "Legends":
                legends_page_text = self.add_article_to_page_text(
                    page_text=legends_page_text, article=article, nom_page=nom_page, nom_type=nom_type, props=props,
                    continuity=continuity, nominator=nominator, old=True)
            else:
                main_page_text = self.add_article_to_page_text(
                    page_text=main_page_text, article=article, nom_page=nom_page, nom_type=nom_type, props=props,
                    continuity=continuity, nominator=nominator, old=True)

            if project == "Novels":
                data.append({
                    "continuity": continuity,
                    "article": article,
                    "user": nominator,
                    "date": self.identify_completion_date(article.title(), nom_type),
                    "nom_page": nom_page_title
                })

        if main_page.get() != main_page_text:
            main_page.put(main_page_text, f"Adding {len(articles)} {nom_type}s")
        if legends_page and legends_page_text and legends_page.get() != legends_page_text:
            legends_page.put(legends_page_text, f"Adding {len(articles)} {nom_type}s")

        if data:
            self.add_articles_to_novel_pages(data, nom_type, True)

        if failed:
            return "The following pages do not exist: " + ", ".join(failed)
        return None

    def add_articles_to_novel_pages(self, data, nom_type, old):
        legends, canon = [], []
        for d in data:
            if d["continuity"] == "Legends":
                legends.append(d)
            else:
                canon.append(d)

        try:
            if legends:
                sub_page = Page(self.site, self.project_data["Novels"]["legendsSubPage"])
                self.add_articles_to_novels_table(legends, sub_page, nom_type, old)
        except Exception as e:
            error_log(f"{type(e)}: {e}")

        try:
            if canon:
                sub_page = Page(self.site, self.project_data["Novels"]["canonSubPage"])
                self.add_articles_to_novels_table(canon, sub_page, nom_type, old)
        except Exception as e:
            error_log(f"{type(e)}: {e}")

    @staticmethod
    def add_articles_to_novels_table(pages, sub_page, nom_type, old):
        sub_page_text = "" if not sub_page.exists() else sub_page.get()
        tables_by_name, standalone_ordering, series_ordering = parse_novel_page_tables(sub_page_text)

        has_standalone = False
        added = False
        for page in pages:
            if f"[[{page['article']}|" in sub_page_text or f"[[{page['article']}]]" in sub_page_text:
                print(f"{page['article']} is already listed in {sub_page['article']}")
                continue
            added = True
            s = add_article_to_tables(
                tables_by_name=tables_by_name, standalone_ordering=standalone_ordering, nom_type=nom_type,
                article=page["article"], user=page["user"], date=page["date"], nom_page=page["nom_page"], old=old)
            has_standalone = has_standalone or s

        if added:
            new_text = rebuild_novels_page_text(tables_by_name, standalone_ordering, series_ordering, has_standalone)

            sub_page.put(new_text, f"Adding {len(pages)} {nom_type}s")

    def add_article_to_page_text(self, page_text, article: Page, nom_page: Page, nom_type: str, props: dict,
                                 continuity: str, nominator: str, old=False) -> str:
        """ Adds a new status article to the given page text, based on the project's properties """

        if not continuity:
            continuity = self.determine_continuity(article)

        if props["format"] == "alphabet":
            if not page_text:
                page_text = self.new_alphabet_table()
            lines = self.alphabet_table(page_text=page_text, article=article)
        elif props["format"] == "table":
            if not page_text:
                page_text = self.build_empty_table(props["columns"])
            lines = self.table(page_text=page_text, article=article, nom_page=nom_page, nom_type=nom_type,
                               nominator=nominator, properties=props, continuity=continuity, old_nom=old)
        elif props["format"] == "portfolio":
            lines = self.portfolio(page_text=page_text, article=article, nom_page=nom_page, nom_type=nom_type,
                                   nominator=nominator, old_nom=old)
        else:
            raise Exception(f"{props['format']} is not valid")

        return "\n".join(lines)

    @staticmethod
    def alphabet_table(*, page_text: str, article: Page) -> List[str]:
        restored = False
        if f"[[{article.title()}|" in page_text or f"[[{article.title()}]]" in page_text:
            if re.search("\*<s>.*\[\[" + article.title() + "[|\]]", page_text):
                restored = True
            else:
                log(f"{article.title()} is already listed in the project status page!")
                return page_text.splitlines()

        first_letter = article.title()[0].upper()
        if not first_letter.isalpha():
            first_letter = "#"

        target = determine_title_format(article.title(), article.get())

        lines = []
        found = False
        for line in page_text.splitlines():
            if f"||'''{first_letter}'''||" in line:
                found = True
            elif found:
                if restored and f"<s>{target}</s>" in line:
                    lines.append(f"*{target}")
                    found = False
                    continue
                elif line.startswith("*") and article.title() < line.replace("''", "").replace("[", "").replace("]", "").replace("*", "").replace("<s>", ""):
                    lines.append(f"*{target}")
                    found = False
                elif line == "}}" or line == "|-" or "||'''" in line:
                    lines.append(f"*{target}")
                    found = False
            lines.append(line)

        return lines

    @staticmethod
    def build_empty_table(columns) -> str:
        lines = ["""{| class="wikitable sortable" {{Prettytable}}"""]

        header_names = {
            "image": "Image", "blankImage": "Image",
            "article": "Article",
            "date": "Date passed", "dateWithLink": "Date passed",
            "mainPageDate": "Date on Main Page",
            "user": "Nominator",
            "statusIcon": "Status",
            "nomLink": "Nomination", "nomPage": "Nomination",
            "beforeAfter": "Before/After project was founded",
            "crossover": "Crossover",
            "grid": "Grid Coordinates",
            "notes": "Notes"
        }
        lines.append("! " + "''' || '''".join(header_names.get(c) for c in columns))
        lines.append("|-")
        lines.append("|}")
        return "\n".join(lines)

    def build_table_row(self, article: Page, nom_page: Page, nom_type: str, nominator: str, properties: dict,
                        continuity: str, old_nom=False):
        """ :rtype: tuple[datetime, str] """
        columns = []
        if old_nom:
            passed_date = self.identify_completion_date(article.title(), nom_type)
        else:
            passed_date = datetime.now()

        nt = self.nom_types[nom_type]
        for i, col_name in enumerate(properties["columns"]):
            if col_name == "image" or col_name == "blankImage":
                image = self.extract_image(article)
                if image:
                    columns.append(f"[[{image}|center|{properties.get('imageSize', 50)}px]]")
                elif col_name == "blankImage":
                    columns.append(f"[[{self.BLANK}|center|50px]]")
                else:
                    columns.append("")
            elif col_name == "article":
                columns.append(determine_title_format(article.title(), article.get()))
            elif col_name == "date":
                columns.append(passed_date.strftime(properties["dateFormat"]))
            elif col_name == "dateWithLink":
                date = passed_date.strftime(properties["dateFormat"])
                columns.append(f"[[{nom_page.title()}|{date}]]")
            elif col_name == "mainPageDate":
                columns.append("")
            elif col_name == "user":
                columns.append("{{U|" + nominator + "}}")
            elif col_name == "statusIconWithLink":
                columns.append(f"[[{nt.premium_icon}|center|{properties.get('statusIconSize', 20)}px|link={nt.page}]]")
            elif col_name == "statusIcon":
                columns.append(f"[[{nt.icon}|center|{properties.get('statusIconSize', 30)}px]]")
            elif col_name == "nomLink":
                columns.append(f"[[{nom_page.title()}|Link]]")
            elif col_name == "nomPage":
                columns.append(f"[[{nom_page.title()}|{nom_type}N]]")
            elif col_name == "beforeAfter" and continuity == "Legends":
                columns.append("After")
            elif col_name == "crossover":
                columns.append("&ndash;")
            elif col_name == "grid":
                grid = self.extract_grid(article) or ""
                columns.append(grid)
            elif col_name == "notes":
                columns.append("")

        return passed_date, "| " + " || ".join(columns)

    def table(self, *, page_text: str, article: Page, nom_page: Page, nom_type: str, nominator: str, properties: dict,
              continuity: str, old_nom=False) -> List[str]:

        if f"[[{article.title()}|" in page_text or f"[[{article.title()}]]" in page_text:
            log(f"{article.title()} is already listed in the project status page!")
            return page_text.splitlines()

        passed_date, text = self.build_table_row(article=article, nom_page=nom_page, nom_type=nom_type, old_nom=old_nom,
                                                 nominator=nominator, properties=properties, continuity=continuity)
        log(text)

        lines = []
        found = False
        header = properties.get("locateHeader")
        if not header and properties.get("continuityHeader") and continuity:
            header = "[[Canon]]" if continuity == "Canon" else "[[Star Wars Legends|Legends]]"

        if old_nom and not properties.get("alphabetical"):
            try:
                before_table, rows, after_table = self.sort_table(page_text, nom_type, properties["columns"],
                                                                  properties.get("dateFormat"))
                rows.append((passed_date, text))
                rows = sorted(rows, key=lambda x: x[0])
                lines += before_table
                for r in rows:
                    lines.append("|-")
                    lines.append(r[1])
                lines.append("|-")
                lines += after_table
                return lines
            except Exception as e:
                print(f"Encountered {type(e)} while adding old nomination to non-alphabetical page: {e}")
                lines = []

        if not lines:
            target_title = article.title()
            if target_title.startswith("The "):
                target_title = target_title[4:]
            table_found = False if header else True
            for line in page_text.splitlines():
                if not found:
                    if table_found and properties.get("alphabetical") and "||" in line and "[[" in line:
                        t = next(r for r in line.split("[[")[1:] if "File:" not in r)
                        t = t.replace("[[", "").replace("]]", "").strip()
                        if t.startswith("The "):
                            t = t[4:]
                        if target_title < t:
                            lines.append(text)
                            lines.append("|-")
                            found = True
                    elif table_found and line.startswith("|}"):
                        if properties.get("alphabetical"):
                            lines.append("|-")
                            lines.append(text)
                        else:
                            lines.append(text)
                            lines.append("|-")
                        found = True
                    elif not table_found and f"={header}=" in line:
                        table_found = True
                lines.append(line)

        if not found:
            raise Exception("Not found!")

        return lines

    @staticmethod
    def clean(t):
        return t.strip().replace("[", "").replace("]", "").split("|")[0]

    def sort_table(self, page_text, nom_type, columns, date_format):
        before_table = []
        after_table = []

        name_index = columns.index("article")

        if "date" in columns:
            date_index = columns.index("date")
        elif "dateWithLink" in columns:
            date_index = columns.index("dateWithLink")
        else:
            return None

        table_rows = []
        table_found = False
        table_end = False
        for line in page_text.splitlines():
            if table_end:
                after_table.append(line)
            elif table_found and line.startswith("|}"):
                table_end = True
                after_table.append(line)
            elif table_found:
                if line.strip() == "|-":
                    continue
                elif line.startswith("!"):
                    before_table.append(line)
                    continue

                pieces = line.split("||")
                date = pieces[date_index].strip()
                if date == "&mdash;":
                    try:
                        passed_date = self.identify_completion_date(self.clean(pieces[name_index]), nom_type)
                        date = passed_date.strftime(date_format)
                    except Exception as e:
                        error_log(type(e), e)

                parsed_date = None
                try:
                    parsed_date = datetime.strptime(date, date_format)
                except Exception as e:
                    error_log(type(e), e)
                table_rows.append((parsed_date, line))
            else:
                if line.startswith("{|"):
                    table_found = True
                before_table.append(line)

        return before_table, table_rows, after_table

    def portfolio(self, *, page_text: str, article: Page, nom_page: Page, nom_type: str, nominator: str,
                  old_nom=False) -> List[str]:
        """ Adds a new {{Portfolio}} template to the target portfolio page, with the intro, quote, image, and nomination
          info for the status article. """

        if f"|article={article.title()}" in page_text:
            log(f"{article.title()} is already listed in the project status page!")
            return page_text.splitlines()

        lines = []

        if old_nom:
            passed_date = self.identify_completion_date(article.title(), nom_type)
        else:
            passed_date = datetime.now()

        intro, quote, title_format, image = self.extract_intro_and_image(article)

        nom_title = nom_page.title().split("/", 1)[1]

        lines.append("{{Portfolio")
        lines.append("|type=" + nom_type[:2])
        lines.append("|article=" + article.title())
        if title_format != f"[[{article.title()}]]":
            lines.append("|link=" + title_format)
        lines.append("|user=" + nominator)
        lines.append("|date=" + passed_date.strftime('%B %d, %Y'))
        if nom_title != article.title():
            lines.append("|nompage=" + nom_title)
        if image:
            lines.append("|image=" + image)
        if quote:
            lines.append("|quote=" + quote)
        lines.append("|intro=" + intro)
        lines.append("}}")

        all_lines = page_text.splitlines()
        all_lines += lines
        return all_lines

    @staticmethod
    def extract_grid(article: Page) -> Optional[str]:
        for line in article.get().splitlines():
            if "|coord=" in line or "|coordinates=" in line:
                match = re.search("\|coord(inates)?=(\[\[.*?\|)?(?P<c>[A-Z]-[0-9]+)", line)
                if match:
                    return match.groupdict()['c']
                break
        return None

    @staticmethod
    def extract_image(article: Page) -> Optional[str]:
        image = None
        for line in article.get().splitlines():
            if "|image=" in line:
                i = re.search("\|image=.*?\[\[([Ff]ile:.*?)[|\]]", line)
                if i:
                    image = i.group(1)
            elif line.startswith("=="):
                break
        return image

    @staticmethod
    def extract_intro_and_image(article: Page) -> Tuple[str, str, str, str]:
        image = None
        intro = []
        quote = []
        text = article.get()
        title = article.title()

        title_format = determine_title_format(page_title=title, text=text)

        found = False
        bracket_count = 0
        quote_bracket_count = 0
        for line in text.splitlines():
            if "|image=" in line:
                i = re.search("\|image=.*?\[\[([Ff]ile:.*?)[|\]]", line)
                if i:
                    image = i.group(1)

            if line.startswith("=="):
                if intro and not intro[-1].strip():
                    intro.pop(-1)
                break
            elif "{{quote" in line.lower() or "{{dialogue" in line.lower():
                quote.append(line)
                quote_bracket_count += line.count("{")
                quote_bracket_count -= line.count("}")
            elif quote_bracket_count > 0:
                quote.append(line)
                quote_bracket_count += line.count("{")
                quote_bracket_count -= line.count("}")
            elif found:
                if not (line.strip().startswith("{{") and line.strip().endswith("}}")):
                    intro.append(line)
            elif bracket_count == 0 and not line.strip().startswith("{{"):
                intro.append(line)
                found = True
            else:
                bracket_count += line.count("{")
                bracket_count -= line.count("}")

        if not found:
            raise ValueError("Cannot find intro")
        elif not intro:
            raise ValueError("Cannot find intro")

        q = None
        if quote:
            q = "\n".join(quote)

        full_intro = "\n".join(intro)
        if "<ref" in full_intro:
            full_intro = re.sub("<ref.*?(/>|</ref>)", "", full_intro)

        return full_intro, q, title_format, image

    def identify_completion_date(self, article_title, nom_type) -> datetime:
        """ Extracts the completion date from the Ahh templates on an article's talk page. """

        talk_page = Page(self.site, f"Talk:{article_title}")
        page_text = talk_page.get()
        date = None
        for line in page_text.splitlines():
            if "|date=" in line:
                date = line.split("|date=")[1]
            elif line == f"|process={nom_type[:2]}":
                break

        if not date:
            raise Exception(f"Cannot identify date")
        return datetime.strptime(date, "%B %d, %Y")

    @staticmethod
    def new_alphabet_table():
        return """{| class="wikitable sortable" {{Prettytable}}
|width="51"| ||width="15%"|'''Letter''' ||width="80%"| '''Completed articles'''
|-
| [[File:Aurek.svg|x40px|link=Aurek]]||'''A'''||
|-
| [[File:Besh.svg|x40px|link=Besh]]||'''B'''||
|-
| [[File:Cresh.svg|x40px|link=Cresh]]||'''C'''||
|-
| [[File:Dorn.svg|x40px|link=Dorn]]||'''D'''||
|-
| [[File:Esk.svg|x40px|link=Esk]]||'''E'''||
|-
| [[File:Forn.svg|x40px|link=Forn]]||'''F'''||
|-
| [[File:Grek.svg|x40px|link=Grek]]||'''G'''||
|-
| [[File:Herf.svg|x40px|link=Herf/Legends]]||'''H'''||
|-
| [[File:Isk.svg|x40px|link=Isk/Legends]]||'''I'''||
|-
| [[File:Jenth.svg|x40px|link=Jenth/Legends]]||'''J'''||
|-
| [[File:Krill.svg|x40px|link=Krill/Legends]]||'''K'''||
|-
| [[File:Leth.svg|x40px|link=Leth/Legends]]||'''L'''||
|-
| [[File:Mern.svg|x40px|link=Mern/Legends]]||'''M'''||
|-
| [[File:Nern.svg|x40px|link=Nern/Legends]]||'''N'''||
|-
| [[File:Osk.svg|x40px|link=Osk/Legends]]||'''O'''||
|-
| [[File:Peth.svg|x40px|link=Peth/Legends]]||'''P'''||
|-
| [[File:Qek.svg|x40px|link=Qek/Legends]]||'''Q'''||
|-
| [[File:Resh.svg|x40px|link=Resh/Legends]]||'''R'''||
|-
| [[File:Senth.svg|x40px|link=Senth/Legends]]||'''S'''||
|-
| [[File:Trill.svg|x40px|link=Trill/Legends]]||'''T'''||
|-
| [[File:Usk.svg|x40px|link=Usk/Legends]]||'''U'''||
|-
| [[File:Vev.svg|x40px|link=Vev/Legends]]||'''V'''||
|-
| [[File:Wesk.svg|x40px|link=Wesk/Legends]]||'''W'''||
|-
| [[File:Xesh.svg|x40px|link=Xesh/Legends]]||'''X'''||
|-
| [[File:Yirt.svg|x40px|link=Yirt/Legends]]||'''Y'''||&mdash;
|-
| [[File:Zerek.svg|x40px|link=Zerek/Legends]]||'''Z'''||
|-
| [[File:Aur1.svg|x40px|link=Aurebesh/Legends]]||'''#'''||
|-
|}"""
