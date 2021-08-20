import re
import pywikibot

from data.filenames import *


def read_version_info(target_version):
    changes = []
    text = []
    found = False
    with open(VERSION_HISTORY, "r") as f:
        for line in f.readlines():
            if found and line.startswith("*'''"):
                break
            elif found:
                changes.append(line.replace("**", "- "))
            elif f"*'''{target_version}'''" in line:
                found = True
            elif line:
                text.append(line.strip())

    if not found:
        raise Exception("Version info not found!")

    z = "\n".join(changes)
    return re.sub("(\r?\n)+", "\n", z), "\n".join(text)


def report_version_info(site, version):
    with open(OLD_VERSION_FILE, "r") as f:
        old_version = f.readline()

        if old_version is None:
            print("Not found!")
        elif old_version == version:
            return

    updates, total = read_version_info(version)
    if updates:
        with open(OLD_VERSION_FILE, "w") as f:
            f.write(version)
        page = pywikibot.Page(site, "User:JocastaBot/History")
        page.put(total, "Updating JocastaBot changelog")
        return f"**JocastaBot: Version {version}**\n{updates}"
    else:
        return None
