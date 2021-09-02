from datetime import datetime
from pywikibot import Category

bots = ["01miki10-bot", "C4-DE Bot", "EcksBot", "JocastaBot", "RoboCade", "PLUMEBOT", "TOM-E Macaron.ii"]


def archive_stagnant_senate_hall_threads(site):
    for page in Category(site, "Senate Hall").articles(namespaces=100):
        text = page.get()
        if "{{sticky}}" in text.lower():
            continue

        stagnant = False
        for revision in page.revisions(reverse=True, total=10):
            if revision["user"] in bots:
                continue
            duration = datetime.now() - revision["timestamp"]
            stagnant = duration.days >= 30
            break

        if stagnant:
            new_text = text.replace("{{Shtop}}", "{{Shtop-arc}}").replace("{{shtop}}", "{{shtop-arc}}")
            if text == new_text:
                print("ERROR: cannot find {{Shtop}}")
            else:
                page.put(new_text, f"Archiving stagnant Senate Hall thread")
