import re
from datetime import datetime
from pywikibot import Page, Category, Site
from typing import List, Tuple, Dict
from urllib.parse import unquote

from jocasta.common import error_log, determine_target_of_nomination
from jocasta.nominations.data import NominationType


class ObjectionTree:
    """
    :type lines: dict[int, ObjectionLine]
    """
    def __init__(self, *, user=None, nested=False, struck=False, lines=None):
        self.user = user
        self.nested = nested
        self.struck = struck
        self.lines = lines or {}


class ObjectionLine:
    def __init__(self, counter, user, date, content):
        self.counter = counter
        self.user = user
        self.date = date
        self.content = content

    def is_struck(self):
        bullets = "*".__mul__(self.counter)
        return (self.content or "").lower().startswith(f"{bullets}<s>") or (self.content or "").lower().startswith(f":{bullets}<s>")


class ObjectionResult:
    def __init__(self, nominator: str, objector: str, addressed: bool, overdue: bool, first_notification: bool,
                 last_date: datetime, lines):
        self.nominator = nominator
        self.objector = objector
        self.overdue = overdue
        self.first_notification = first_notification
        self.addressed = addressed
        self.last_date = last_date
        self.lines = lines


def find_nominator(text) -> str:
    return re.findall("Category:Nominations by User:(.*?)[\|\]]", text)[0]


def is_review_note(s):
    return "review note" in s.lower().replace("reviewing", "review") or "(comment)" in s.lower()


def parse_date(d):
    if d:
        try:
            return datetime.strptime(d, "%H:%M, %d %B %Y")
        except Exception:
            try:
                return datetime.strptime(d, "%H:%M, %B %d, %Y")
            except Exception as e:
                error_log(type(e), e, d)
    return None


def extract_support_votes(page: Page, template):
    text = page.get()
    votes = {}

    support_section = text.split("====Support====", 1)[-1].split("====Object====", 1)[0]
    all_votes = [vl.strip() for vl in support_section.splitlines() if vl.strip().startswith("#")]
    for vote in all_votes:
        if template in vote:
            u = re.findall("[\[\{]{2}[Uu]s?e?r?[\|:](.*?)[\|\]\}]", vote)

            m = re.search("([0-9]{2}:[0-9]{2}, [0-9]+ [A-z]+ 20[0-9]+) \(UTC\)", vote)
            if not m:
                m = re.search("([0-9]{2}:[0-9]{2}, [A-z]+ [0-9]+, 20[0-9]+) \(UTC\)", vote)

            if u and m:
                votes[u[-1]] = parse_date(m.group(1))

    return votes


def build_objection_trees(page_name, lines) -> List[List[Tuple[bool, dict]]]:
    objections_found = False
    initial_section_found = False
    sections = []
    current_section = []
    current_tree = {}

    nested = False
    for line in lines:
        try:
            if objections_found:
                if "===Comments===" in line:
                    current_section.append((nested, {**current_tree}))
                    sections.append(current_section)
                    break
                elif line.startswith("==="):
                    if initial_section_found:
                        current_section.append((nested, {**current_tree}))
                        sections.append(current_section)
                    else:
                        initial_section_found = True
                    current_section = []
                    current_tree = {}
                    continue
                elif not line.startswith("*") and not line.startswith(":*"):
                    continue

                [bullets] = re.findall("^:?(\*+)", line)
                if len(bullets) == 1 and line.startswith(":*") and 1 in current_tree:
                    current_tree = {}
                elif len(bullets) == 1:
                    current_section.append((nested, {**current_tree}))
                    nested = False
                    current_tree = {}
                elif nested and len(bullets) in current_tree:
                    current_section.append((nested, {**current_tree}))
                    current_tree = {k: v for k, v in current_tree.items() if k < len(bullets)}
                elif len(bullets) == 2 and 2 in current_tree:
                    nested = True
                    current_section.append((nested, {**current_tree}))
                    current_tree = {k: v for k, v in current_tree.items() if k < len(bullets)}
                elif len(bullets) in current_tree:
                    print(f"{page_name}: Unexpected state: {len(bullets)}, {line}")
                current_tree[len(bullets)] = line

            elif "===Object===" in line:
                objections_found = True

        except Exception as e:
            print(f"X: {page_name}:", type(e), e, line)
    return sections


def fix_missing_strikethroughs(page_name, section: List[Tuple[bool, Dict[int, str]]]):
    s_diff = 0
    for i in range(len(section)):
        n, t = section[i]
        if not t:
            continue

        if s_diff > 0 and not n and "<s>" not in t[1]:
            # print(f"{page_name}: Adding <s> to line 1 in tree: {t}")
            t[1] = re.sub("^(:?\*+)[ ]*", "\\1<s>", t[1])
        elif s_diff > 0 and n and 2 in t and "<s>" not in t[2]:
            # print(f"{page_name}: Adding <s> to line 2 in tree: {t}")
            t[2] = re.sub("^(:?\*+)[ ]*", "\\1<s>", t[2])

        s_diff += t[1].count("<s>")
        s_diff -= t[1].count("</s>")
        if n and 2 in t:
            s_diff += t[2].count("<s>")
            s_diff -= t[2].count("</s>")


def extract_actual_objections(page_name, section: List[Tuple[bool, Dict[int, str]]]) -> Tuple[str, List[ObjectionTree]]:
    current_user = None
    tree_dates = {}
    results = []
    user_ordering = {}
    all_done = None
    i = 0
    trees = list(reversed(section))
    for nested, tree in trees:
        if not tree:
            continue

        if all_done and i > 0:
            if all_done in tree:
                all_done = 0
            else:
                _, previous_tree = trees[i - 1]
                if all_done in previous_tree:
                    tree[all_done] = previous_tree[all_done]
                    # print(f"{page_name}: Copying previous response {tree[all_done]} from previous tree")

        struck = False
        tree_data = {}
        for count, line in tree.items():
            if count == "nested":
                continue
            m = re.search("([0-9]{2}:[0-9]{2}, [0-9]+ [A-z]+ 20[0-9]+) \(UTC\)", line)
            if not m:
                m = re.search("([0-9]{2}:[0-9]{2}, [A-z]+ [0-9]+, 20[0-9]+) \(UTC\)", line)
            if m:
                tree_dates[count] = m.group(1)

            if "all done" in line.lower() or "all handled" in line.lower():
                all_done = count

            u = None
            if "[[user:" in line.lower() or "{{u|" in line.lower():
                u = re.findall("[\[\{]{2}[Uu]s?e?r?[\|:](.*?)[\|\]\}]", line)
                if u and count not in user_ordering:
                    user_ordering[count] = u[-1]
                elif u and user_ordering[count] != u:
                    user_ordering[count] = u[-1]

            if count == 1:
                if not current_user and u:
                    current_user = u[-1]
                if re.search("^:?\*[ ]*<s>", line):
                    struck = True
                elif is_review_note(line):
                    struck = True

            elif count == 2 and nested and not struck:
                if not current_user and u:
                    current_user = u[-1]
                if re.search("^:?\*\*[ ]*<s>", line):
                    struck = True
                elif is_review_note(line):
                    struck = True

            tree_data[count] = ObjectionLine(counter=count, user=user_ordering.get(count),
                                             date=tree_dates.get(count), content=line)
        i += 1

        results.append(ObjectionTree(user=current_user, nested=nested, struck=struck, lines=tree_data))
    return current_user, results


def identify_overdue_objections(page_name, nom_data: NominationType, nominator: str, user: str,
                                trees: List[ObjectionTree]) -> List[ObjectionResult]:
    overdue = []
    now = datetime.now()
    for tree_data in trees:
        if tree_data.struck:
            continue

        counts = list(tree_data.lines.keys())
        target = tree_data.lines[max(counts)]
        if target.date is None:
            if max(counts) - 1 in tree_data.lines and tree_data.lines[max(counts) - 1].date:
                print(f"{page_name}: Unable to determine date for line {max(counts)}, "
                      f"defaulting to earlier line's date: {tree_data.lines[max(counts) - 1].date}")
                target.date = tree_data.lines[max(counts) - 1].date
            else:
                print(f"{page_name}: Cannot check date for objections from {user}: {target.content}")
                continue
        date = parse_date(target.date)
        if not date:
            print(f"{page_name}: Cannot check date for objections from {user}: {target.date} | {target.content}")
            continue

        duration = now - date
        if duration.days >= nom_data.notification_days and len(counts) % 2 == 0:
            overdue.append(ObjectionResult(
                nominator=nominator, objector=tree_data.user, overdue=duration.days >= nom_data.overdue_days,
                first_notification=duration.days == nom_data.notification_days, addressed=True, last_date=target.date,
                lines=tree_data.lines))
        elif duration.days >= nom_data.notification_days:
            overdue.append(ObjectionResult(
                nominator=nominator, objector=tree_data.user, overdue=duration.days >= nom_data.overdue_days,
                first_notification=duration.days == nom_data.notification_days, addressed=False, last_date=target.date,
                lines=tree_data.lines))
        # elif max(counts) % 2 == (1 if tree_data.nested else 0):
        #     print(f"{page_name}: {date} is within time window, skipping response from nominator - {len(counts) % 2 == 0}")
        # else:
        #     print(f"{page_name}: {date} is within time window, skipping objection from {user} - {len(counts) % 2 == 1}")
    return overdue


def identify_review_objections(nominator: str, trees: List[ObjectionTree]) -> List[ObjectionResult]:
    objections = []
    for tree_data in trees:
        if tree_data.struck:
            continue

        counts = list(tree_data.lines.keys())
        target = tree_data.lines[max(counts)]
        objections.append(ObjectionResult(
            nominator=nominator, objector=tree_data.user, overdue=False, first_notification=False,
            addressed=len(counts) % 2 == 0, last_date=target.date, lines=tree_data.lines))
    return objections


def examine_objections_on_nomination(page: Page, nom_data: NominationType):
    """ :rtype: tuple[str, dict[bool, dict[str, dict[datetime, list[ObjectionResult]]]]] """
    try:
        text = page.get()
        title = page.title()
        nominator = find_nominator(text)
        sections = build_objection_trees(title, text.splitlines())

        result_map = {True: {}, False: {}}
        for section in sections:
            fix_missing_strikethroughs(title, section)
            user, results = extract_actual_objections(title, section)
            objections = identify_overdue_objections(title, nom_data, nominator, user, results)

            for o in objections:
                if o.objector is None:
                    print(o.last_date, o.lines)
                if o.objector not in result_map[o.addressed]:
                    result_map[o.addressed][o.objector] = {}
                if o.last_date not in result_map[o.addressed][o.objector]:
                    result_map[o.addressed][o.objector][o.last_date] = []
                result_map[o.addressed][o.objector][o.last_date].append(o)

        return nominator, result_map
    except Exception as e:
        print("Y", page.title(), type(e), e)
        return None, {True: {}, False: {}}


def examine_objections_on_review(page: Page):
    """ :rtype: tuple[str, bool, dict[bool, dict[str, dict[datetime, list[ObjectionResult]]]]] """
    try:
        text = page.get()
        title = page.title()
        target = determine_target_of_nomination(title)
        on_probation = check_if_on_probation(page.site, target)
        sections = build_objection_trees(title, text.splitlines())

        result_map = {True: {}, False: {}}
        for section in sections:
            fix_missing_strikethroughs(title, section)
            user, results = extract_actual_objections(title, section)
            objections = identify_review_objections(user, results)

            for o in objections:
                if o.objector not in result_map[o.addressed]:
                    result_map[o.addressed][o.objector] = {}
                if o.last_date not in result_map[o.addressed][o.objector]:
                    result_map[o.addressed][o.objector][o.last_date] = []
                result_map[o.addressed][o.objector][o.last_date].append(o)

        return target, on_probation, result_map
    except Exception as e:
        print("Y", page.title(), type(e), e)
        return None, False, {True: {}, False: {}}


def check_if_on_probation(site: Site, title: str):
    page = Page(site, title)
    if page.isRedirectPage():
        page = page.getRedirectTarget()
    elif not page.exists():
        error_log(f"{title} does not exist; cannot determine probation status")
    return re.search("\{\{[Tt]op.*?\|p[fgc]a[|}]", page.get()) is not None


def examine_nomination_and_prepare_results(page: Page, nom_data: NominationType, include: bool):
    """ :rtype: tuple[list[str], list[tuple[str, str]]] """
    nominator, objection_data = examine_objections_on_nomination(page, nom_data)
    if not nominator:
        return [], []

    overdue, normal = [], []
    i = " or more" if include else ""
    for is_addressed, a_data in objection_data.items():
        a_str = "addressed" if is_addressed else "unaddressed"
        for user, user_data in a_data.items():
            oc, nc = 0, 0
            for date, objections in user_data.items():
                for o in objections:
                    if o.overdue:
                        oc += 1
                    elif o.first_notification:
                        nc += 1
                    elif include:
                        nc += 1

            if oc:
                overdue.append(f"{oc} objections from {user} have been {a_str} for {nom_data.overdue_days}{i} days")

            if nc and is_addressed:
                normal.append((user, f"{nc} of your objections have been {a_str} for {nom_data.notification_days}{i} days"))
            elif nc:
                normal.append((nominator, f"{nc} objections from {user} have been {a_str} for {nom_data.notification_days}{i} days"))

    return overdue, normal


def calculate_date_for_review(date_created, unaddressed: dict):
    dates = [date_created]
    if unaddressed:
        for u, tree in unaddressed.items():
            for ds, objections in tree.items():
                d = parse_date(ds)
                if d:
                    dates.append(d)
    return max(dates)


def count_votes(page_text, verb):
    section = page_text.split(f"==Vote to {verb}", 1)[-1].replace("status====", "").split("====", 1)[0]
    return len([s for s in section.splitlines() if s.strip().startswith("#")])


def examine_review_and_prepare_results(review: Page, now: datetime, bypass_check: bool):
    target, on_probation, objection_data = examine_objections_on_review(review)
    date_to_compare = calculate_date_for_review(review.oldest_revision['timestamp'], objection_data[False])
    duration = now - date_to_compare
    last_change = now - review.latest_revision['timestamp']

    support = count_votes(review.get(), "retain")
    strip = count_votes(review.get(), "strip")

    x = ""
    if last_change and last_change.days > 7:
        x = f" ({last_change.days} days since last objection/response)"
    message = f"- **{target}** ({support} / {strip}){x}: <{unquote(review.full_url())}>"
    if not objection_data[False]:
        return "ready", message
    elif not on_probation and duration.days >= 30:
        return "probe", message
    elif on_probation and (bypass_check or (duration.days > 1 and duration.days % 7 == 0)):
        return "probation", message
    elif bypass_check or (duration.days > 1 and duration.days % 3 == 0):
        return "normal", message
    return None, None


def check_for_objections_on_page(site, nom_data: NominationType, page_name):
    page = Page(site, nom_data.nomination_page + "/" + page_name)
    if not page.exists():
        raise Exception(f"{nom_data.nom_type} {page_name} does not exist")
    o, n = examine_nomination_and_prepare_results(page, nom_data, True)
    return {page.full_url(): o}, {page.full_url(): n}


def check_active_nominations(site, nom_data: NominationType, include: bool):
    """:rtype: tuple[dict[str, list[str]], dict[str, list[tuple[str, str]]]]"""
    category = Category(site, nom_data.nomination_category)
    total_overdue, total_normal = {}, {}
    for nom in category.articles():
        if "/" not in nom.title() or "/Header" in nom.title():
            continue
        overdue, normal = examine_nomination_and_prepare_results(nom, nom_data, include)
        if overdue:
            total_overdue[unquote(nom.full_url())] = overdue
        if normal:
            total_normal[unquote(nom.full_url())] = normal

    return total_overdue, total_normal


def check_for_objections_on_review_page(site, nom_data: NominationType, page_name):
    page = Page(site, nom_data.nomination_page.replace("nominations", "reviews") + "/" + page_name)
    if not page.exists():
        raise Exception(f"{nom_data.nom_type} {page_name} does not exist")
    status, msg = examine_review_and_prepare_results(page, datetime.now(), True)
    return {status: [msg]}


def check_active_reviews(site, nom_data: NominationType):
    category = Category(site, nom_data.review_category)

    results = {"ready": [], "probe": [], "normal": [], "probation": []}
    now = datetime.now()
    for review in category.articles():
        if "/" not in review.title() or "/Header" in review.title():
            continue
        status, msg = examine_review_and_prepare_results(review, now, False)
        if status and msg:
            results[status].append(msg)

    return results


def calculate_reviews_by_board_members():
    pass


def leave_talk_page_message(site, user: str, nom_page, texts: Dict[str, str]):
    page = Page(site, f"User talk:{user}")
    if not page.exists():
        raise Exception(f"User_talk:{user} does not exist")

    text_to_add = "\n\n==Overdue objections==\n"
    for page_name, text in texts.items():
        text_to_add += f"Regarding [[{nom_page}/{page_name}]]:\n"

    text_to_add += "\n\nPlease check these at your earliest convenience. The Inquisitorius, AgriCorps, and EduCorps " \
                   "appreciates your participation in our processes. {{U|JocastaBot}} ~~~~~"

    page.put(page.get() + text_to_add, "Notifying user about nearly-overdue objections on nomination pages")
