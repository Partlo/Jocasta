import re
import traceback
from pywikibot import Page, Category
from datetime import datetime


class ArchiveException(Exception):
    def __init__(self, message):
        self.message = message


class UnknownCommand(Exception):
    def __init__(self, message):
        self.message = message


def log(text, *args):
    print(f"[{datetime.now().isoformat()}] {text}", *args)


def error_log(text, *args):
    log(f"ERROR: {text}", *args)
    traceback.print_exc()


def clean_text(text):
    return (text or '').replace('\t', '').replace('\n', '').replace('\u200e', '').strip()


def extract_err_msg(e: Exception):
    try:
        if hasattr(e, "message"):
            return e.message
        return " " + str(e.args[0] if str(e.args).startswith('(') else e.args)
    except Exception as _:
        return str(e.args)


def determine_target_of_nomination(title):
    return re.sub(" \((first|second|third|fourth|fifth|sixth) nomination\)", "", title.split("/", 1)[1])


def determine_title_format(page_title, text) -> str:
    """ Examines the target article's usage of {{Top}} and extracts the title= and title2= parameters, in order to
      generate a properly-formatted pipelink to the target. """

    print(page_title)
    if page_title.startswith("en:"):
        page_title = page_title[3:]

    pagename = re.match("{{[Tt]op\|[^\n]+\|title=''{{PAGENAME}}''", text)
    if pagename:
        return f"''[[{page_title}]]''"

    title1 = None
    title_match = re.match("{{[Tt]op\|[^\n]+\|title=(?P<title>.*?)[|}]", text)
    if title_match:
        title1 = title_match.groupdict()['title']
        if title1 == f"''{page_title}''":
            return f"''[[{page_title}]]''"

    match = re.match("^(?P<title>.+?) \((?P<paren>.*?)\)$", page_title)
    if match:
        title2 = None
        title2_match = re.match("{{[Tt]op\|[^\n]+\|title2=(?P<title>.*?)[|}]", text)
        if title2_match:
            title2 = title2_match.groupdict()['title']

        if title1 or title2:
            title1 = title1 or match.groupdict()['title']
            title2 = title2 or match.groupdict()['paren']
            return f"[[{page_title}|{title1} ({title2})]]"
        else:
            return f"[[{page_title}]]"
    elif title1 and title1 != page_title:
        return f"[[{page_title}|{title1}]]"
    else:
        return f"[[{page_title}]]"


def determine_nominator(page: Page, nom_type: str, nom_page: Page) -> str:
    revision = calculate_nominated_revision(page=page, nom_type=nom_type, raise_error=False)
    if revision and revision.get("user"):
        return revision["user"]
    return extract_nominator(nom_page=nom_page)


def extract_nominator(nom_page: Page, page_text: str = None):
    match = re.search("Nominated by.*?(User:|U\|)(.*?)[\]\|\}/]", page_text or nom_page.get())
    if match:
        return match.group(2).replace("_", " ").strip()
    else:
        return list(nom_page.revisions(reverse=True, total=1))[0]["user"]


def calculate_nominated_revision(*, page: Page, nom_type, raise_error=True, content=False):
    nominated_revision = None
    for revision in page.revisions(content=content):
        if f"Added {nom_type}nom" in revision['tags'] or revision['comment'] == f"Added {nom_type}nom":
            nominated_revision = revision
            break

    if nominated_revision is None and raise_error:
        raise ArchiveException("Could not find nomination revision")
    return nominated_revision


def calculate_revisions(*, page, template, comment, comment2=None):
    """ Examines the target article's revision history to identify the revisions where the nomination template was
     added and removed. """

    nominated_revision = None
    completed_revision = None
    for revision in page.revisions():
        if revision['comment'] == comment:
            completed_revision = revision
        if comment2 and revision['comment'] == comment2:
            nominated_revision = revision
            break
        if f"Added {template}" in revision['tags'] or revision['comment'] == f"Added {template}" or template in revision['comment']:
            nominated_revision = revision
            break

    if completed_revision is None:
        raise ArchiveException("Could not find completed revision")
    elif nominated_revision is None:
        raise ArchiveException("Could not find nomination revision")
    return completed_revision, nominated_revision


def compare_category_and_page(site, nom_page, category):
    page = Page(site, nom_page)

    page_articles = []
    dupes = []
    start_found = False
    for line in page.get().splitlines():
        if start_found and "<!--End-->" in line:
            break
        elif start_found:
            if line.count("[[") > 1:
                for r in re.findall("\[\[(.*?)[\|\]]", line):
                    if r.replace("\u200e", "") in page_articles:
                        dupes.append(r.replace("\u200e", ""))
                    else:
                        page_articles.append(r.replace("\u200e", ""))
            elif "[[" in line:
                target = line.split("[[")[1].split("]]")[0].split("|")[0].replace("\u200e", "")
                if target in page_articles:
                    dupes.append(target)
                else:
                    page_articles.append(target)
        elif "<!--Start-->" in line:
            start_found = True

    articles = list(Category(site, category).articles(content=False))
    articles += list(Category(site, f"{category} on probation").articles(content=False))
    missing_from_page = []
    for p in articles:
        if p.namespace().id != 0:
            continue
        elif p.title() in page_articles:
            page_articles.remove(p.title())
        elif p.title()[0].lower() + p.title()[1:] in page_articles:
            page_articles.remove(p.title()[0].lower() + p.title()[1:])
        else:
            missing_from_page.append(p.title())

    return dupes, page_articles, missing_from_page


def build_analysis_response(site, nom_page, category):
    dupes, missing_from_category, missing_from_page = compare_category_and_page(site, nom_page, category)
    lines = []
    if dupes:
        lines.append(f"Duplicates on {nom_page}:")
        for p in dupes:
            lines.append(f"- {p}")
    if missing_from_page:
        lines.append(f"Missing from {nom_page}:")
        for p in missing_from_page:
            lines.append(f"- {p}")
    if missing_from_category:
        lines.append(f"Listed on {nom_page}, but not in {category}:")
        for p in missing_from_category:
            lines.append(f"- {p}")
    return lines


FINAL_HEADERS = ["appearances", "sources", "notes and references", "external links", "bibliography", "filmography",
                 "discography"]
SKIP = ["[[file:", "{{", "==", "*", "|", "<!--"]


def word_count(text: str):
    intro_count = 0
    body_count = 0
    bts_count = 0
    text = re.sub(r"\[\[(?![Ff]ile:)[^\|\[\]]*?\|(.*?)\]\]", r"\1", text)
    text = re.sub(r"\[\[([^|\[\]]*?)\]\]", r"\1", text)
    text = re.sub(r"<ref[ \n]+name[^</>]*?=[^</>]*?>.*?</ref>", "", text)
    text = re.sub(r"<ref[ \n]+name[^</>]*?=[^<>\[\]]*?/>", "", text)
    text = re.sub(r"'''?", "", text)
    text = text.replace(" - ", " ").replace("&mdash;", " ").replace("&ndash;", " ").replace("&nbsp;", " ").replace("{{'s}}", "'s")

    intro = True
    behind_the_scenes = False
    excerpt = False
    for line in text.splitlines():
        if not behind_the_scenes and "==behind the scenes==" in line.lower():
            behind_the_scenes = True
            continue
        elif any(f"=={h}" in line.strip().lower() for h in FINAL_HEADERS):
            break
        elif intro and line.strip().startswith("=="):
            intro = False
            continue
        elif "{{excerpt" in line.lower():
            excerpt = True
        elif excerpt and re.search("\|source=.*?\}\}", line):
            excerpt = False
            continue
        elif any(line.strip().lower().startswith(h) for h in SKIP):
            continue
        elif len(line.strip()) == 0:
            continue

        if excerpt:
            continue

        if behind_the_scenes:
            bts_count += len([x for x in line.split(" ") if x])
        elif intro:
            intro_count += len([x for x in line.split(" ") if x])
        else:
            body_count += len([x for x in line.split(" ") if x])

    if body_count == 0:
        return intro_count + body_count + bts_count, body_count, intro_count, bts_count
    else:
        return intro_count + body_count + bts_count, intro_count, body_count, bts_count


def validate_word_count(status, total, intro, body):
    if status == "Featured":
        if total < 1000:
            return "total word count is under 1000 words"
    elif status == "Good":
        if total < 250:
            return "total word count is under 250 words"
        elif total > 1000:
            return "total word count exceeds 1000 words"
    elif status == "Comprehensive":
        if total > 250 and intro > 0:
            return "total word count exceeds 250 words"
        elif body >= 165 and intro == 0:
            return "word count of body exceeds 165 words, but article lacks an introduction"
        elif body < 165 and intro > 0:
            return "article has an introduction, but word count of body is under 165 words"
    return None


def build_sub_page_name(title):
    sort_text = "{{SUBPAGENAME}}"
    if title.count("/") > 1:
        sort_text = title.split("/", 1)[1]
    return sort_text


def divide_chunks(l, n):
    # looping till length l
    for i in range(0, len(l), n):
        yield l[i:i + n]
