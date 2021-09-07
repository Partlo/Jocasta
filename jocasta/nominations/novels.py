import re
from datetime import datetime
from pywikibot import Page

from jocasta.common import determine_title_format
from jocasta.data.nom_data import NOM_TYPES


def extract_book_name(page_text: str) -> str:
    appearances = page_text.split("==Appearances==")[1]

    first = {}
    for line in appearances.splitlines():
        if "{{1st" in line:
            m = re.search("\*'?'?\[\[(.*?)[\|\]].*?\].*?\{\{(1stm?p?)[\|\}]", line)
            if m:
                first[m.group(2)] = m.group(1)
    if not first:
        for line in appearances.splitlines():
            if "{{1st" in line:
                m1 = re.search("\*.*?\|book=(.*?)[\|\}].*?\{\{(1stm?p?)[\|\}]", line)
                if m1:
                    first[m1.group(2)] = m1.group(1)
                else:
                    m2 = re.search("\*(.*?\}\}).*?\{\{(1stm?p?)[\|\}]", line)
                    if m2:
                        first[m2.group(2)] = m2.group(1)
    return first.get("1st", first.get("1stp", first.get("1stm")))


def parse_tables(page_text: str):
    """ :rtype: tuple[list[tuple[str, list[tuple[str, list[str]]]]], list[tuple[str, list[str]]]] """

    series = []
    standalone = []
    current_series = []
    current_table = []
    book_title = None
    series_title = None
    is_standalone = False
    for line in page_text.splitlines():
        if line.startswith("====="):
            if current_table and is_standalone:
                standalone.append((book_title, current_table))
            elif current_table:
                current_series.append((book_title, current_table))
            book_title = line.replace("=", "")
            current_table = []

        elif line.startswith("===") and "Standalone" in line:
            is_standalone = True

        elif line.startswith("==="):
            if current_table and is_standalone:
                standalone.append((book_title, current_table))
            elif current_table:
                current_series.append((book_title, current_table))
            if current_series:
                series.append((series_title, current_series))
            if is_standalone:
                is_standalone = False
            series_title = line.replace("=", "")
            current_series = []
            current_table = []

        elif line.startswith("| "):
            current_table.append(line)

    if current_table and is_standalone:
        standalone.append((book_title, current_table))
    elif current_table:
        current_series.append((book_title, current_table))
    if current_series:
        series.append((series_title, current_series))

    return series, standalone


def build_row(article_link, user, nom_type, nom_page, date):
    u = "{{U|" + user + "}}"
    d = date.strftime("%B %d, %Y").replace(" 0", " ")
    return f"| [[{NOM_TYPES[nom_type].premium_icon}|center]] || {article_link} || {u} || [[{nom_page}|{d}]] || ".replace("[[en:", "[[")


def create_table(book: str, rows: list):
    text = []
    if "{" in book:
        text.append(f"====={book}=====")
    elif "(" in book:
        b = book.split("(", 1)[0].strip()
        text.append(f"=====[[{book}|''{b}'']]=====")
    else:
        text.append(f"=====''[[{book}]]''=====")

    if len(rows) >= 13:
        text.append('<div style="height: 500px; overflow:auto;">')
    text.append("""{| class="wikitable sortable" {{Prettytable}}""")
    text.append("""! Status || Article || Nominator(s) || Date and Entry || Notes""")
    for row in rows:
        text.append("|-")
        text.append(row)
    text.append("|}")
    if len(rows) >= 13:
        text.append("</div>")

    return "\n".join(text)


def add_article_to_rows(rows: list, article_link, user, nom_type, nom_page, date, old) -> list:
    new_line = build_row(article_link, user, nom_type, nom_page, date)
    if old:
        new_rows = []
        found = False
        for row in rows:
            if not found:
                m = re.search(r"\[\[Wookieepedia:.*?\|(.*?)\]\]", row)
                if not m:
                    assert False
                row_date = datetime.strptime(m.group(1), "%B %d, %Y")
                if row_date > date:
                    new_rows.append(new_line)
                    found = True
            new_rows.append(row)
        if not found:
            new_rows.append(new_line)
        return new_rows
    else:
        rows.append(new_line)
        return rows


def parse_novel_page_tables(page_text):
    series, standalone = parse_tables(page_text)
    tables_by_name = {}

    series_ordering = []
    for series_name, series_books in series:
        series_order = []
        for book_name, table in series_books:
            m = re.search(r"\[\[(.*?)[\|\]]", book_name)
            if m:
                tables_by_name[m.group(1)] = table
                series_order.append((book_name, m.group(1)))
            else:
                tables_by_name[book_name] = table
                series_order.append((book_name, book_name))
        series_ordering.append((series_name, series_order))

    standalone_ordering = []
    for book_name, table in standalone:
        m = re.search(r"\[\[(.*?)[\|\]]", book_name)
        if m:
            tables_by_name[m.group(1)] = table
            standalone_ordering.append((book_name, m.group(1)))
        else:
            tables_by_name[book_name] = table
            standalone_ordering.append((book_name, book_name))

    return tables_by_name, standalone_ordering, series_ordering


def rebuild_novels_page_text(tables_by_name, standalone_ordering, series_ordering, has_standalone):
    sections = []
    if has_standalone:
        sections.append("===Standalone===")
        for formatted_name, book_name in standalone_ordering:
            sections.append(create_table(book_name, tables_by_name[book_name]))
            sections.append("")

    for series_title, series_order in series_ordering:
        sections.append(f"==={series_title}===")
        for formatted_name, book_name in series_order:
            sections.append(create_table(book_name, tables_by_name[book_name]))
            sections.append("")

    return "\n".join(sections)


def add_article_to_tables(tables_by_name, standalone_ordering, nom_type, article: Page, user, date, nom_page=None, old=False):
    if not nom_page:
        nom_page = NOM_TYPES[nom_type].nomination_page + "/" + article.title()

    article_text = article.get()
    article_link = determine_title_format(article.title(), article_text)
    book = extract_book_name(article_text)

    has_standalone = bool(standalone_ordering)
    if book in tables_by_name:
        rows = add_article_to_rows(tables_by_name[book], article_link, user, nom_type, nom_page, date, old)
    else:
        rows = [build_row(article, user, nom_type, nom_page, date)]
        if "{" in book:
            standalone_ordering.append((book, book))
        elif "(" in book:
            b = book.split("(", 1)[0].strip()
            standalone_ordering.append((f"[[{book}|''{b}'']]", book))
        else:
            standalone_ordering.append((f"''[[{book}]]''", book))
        has_standalone = True
    tables_by_name[book] = rows
    return has_standalone
