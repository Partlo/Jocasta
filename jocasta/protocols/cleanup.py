import re
from datetime import datetime, timedelta

import pywikibot

from pywikibot import Category, Page

bots = ["01miki10-bot", "C4-DE Bot", "EcksBot", "JocastaBot", "RoboCade", "PLUMEBOT", "TOM-E Macaron.ii"]


def archive_stagnant_senate_hall_threads(site):
    for page in Category(site, "Senate Hall").articles(namespaces=100):
        text = page.get()
        if "{{sticky}}" in text.lower():
            continue

        stagnant = False
        for revision in page.revisions(total=10):
            if revision["user"] in bots:
                continue
            elif "Undo revision" in revision["comment"] and any(b in revision["comment"] for b in bots):
                continue
            duration = datetime.now() - revision["timestamp"]
            stagnant = duration.days >= 31
            break

        if stagnant:
            new_text = text.replace("{{Shtop}}", "{{Shtop-arc}}").replace("{{shtop}}", "{{shtop-arc}}")
            if text == new_text:
                print("ERROR: cannot find {{Shtop}}")
            else:
                page.put(new_text, f"Archiving stagnant Senate Hall thread")


def remove_spoiler_tags_from_page(site, page, limit=30):
    text = page.get()

    line = re.findall("\n\{\{[Ss]poiler\|(.*?)\}\}.*?\n", text)
    if not line:
        print(f"Cannot find spoiler tag on {page.title()}")
        return "no-tag"

    target = line[0].split("|")
    fields = []
    named = {}
    for field in target:
        if field.startswith("time="):
            named["time"] = field.split("=", 1)[1]
        elif field.startswith("quote"):
            f, v = field.split("=", 1)
            named[f] = v
        else:
            fields.append(field)

    if named.get("time") == "skip":
        return "skip"
    elif not named.get("time"):
        print(f"{page.title()}: No time defined in the Spoiler template")
        return "none"
    elif len(fields) <= 2:
        t = page.title() if len(fields) == 0 else fields[0]
        time = datetime.strptime(named["time"], "%Y-%m-%d")
        if time < (datetime.now() + timedelta(hours=5)):
            print(f"{page.title()}: Spoilers for {t} do not expire until {time}")
            return time
        new_text = re.sub("\{\{Spoiler.*?\}\}.*?\n", "", text)
    else:
        time, new_text = remove_expired_fields(site, text, fields, named, limit=limit)

    page.put(new_text, "Removing expired spoiler notices")
    return time


def remove_expired_fields(site, text, fields: list, named: dict, limit=30):
    i, j = 0, 0
    fields_to_keep = []
    quotes_to_keep = []
    release_dates = []
    now = datetime.now() + timedelta(hours=5)
    while i < len(fields):
        f1 = fields[i]
        release_date = extract_release_date(site, f1, limit)
        if release_date:
            release_dates.append(release_date)

        if not release_date or release_date > now:
            fields_to_keep.append(f1)
            if i < len(fields):
                fields_to_keep.append(fields[i + 1])
            j += 1
            if f"quote{j}" in named:
                quotes_to_keep.append(j)
        i += 2

    if not fields_to_keep:
        return "no-fields", re.sub("\{\{Spoiler.*?\}\}.*?\n", "", text)

    new_text = "|".join(fields_to_keep)
    for q in quotes_to_keep:
        new_text += f"|quote{q}=1"

    new_time = None
    if release_dates:
        new_time = (min(release_dates) + timedelta(days=limit)).strftime("%Y-%m-%d")
        new_text += f"|time={new_time}"

    new_text = "{{Spoiler|" + new_text + "}}"
    return new_time, re.sub("\{\{Spoiler\|.*?\}\}.*?\n", new_text, text)


def extract_release_date(site, name, limit):
    page = Page(site, name)
    if not page.exists():
        print(f"Cannot check release date for invalid page {page.title()}")
        return None
    elif page.isRedirectPage():
        page = page.getRedirectTarget()

    p_text = page.get()
    date_match = re.search(r"\n\|(publication date|release date|publish date|airdate)=(?P<t>.*?)[\n<]", p_text)
    if not date_match:
        print(f"No date field found on {name}")
        return None

    date_str = date_match.groupdict()["t"].replace("[", "").replace("]", "")
    date = None
    try:
        date = datetime.strptime(date_str, "%B %d, %Y")
    except Exception as e:
        print(type(e), e)

    if date:
        return date + timedelta(days=limit)
    else:
        print(f"No date found on {name}")
        return None
