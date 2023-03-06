from pywikibot import Page

from jocasta.common import ArchiveException, log


def build_history_text(*, nom_type: str, result: str, link: str, start: dict, completed: dict):
    if result == "Success":
        process = f"{nom_type}N"
        status = nom_type
    elif result == "Withdrawn":
        process = f"{nom_type}N"
        status = f"F{nom_type}N"
    elif result == "Kept":
        process = f"{nom_type}R"
        status = nom_type
    elif result == "Probation":
        process = f"{nom_type}R"
        status = f"P{nom_type}"
    else:
        result = "Failure"
        process = f"{nom_type}N"
        status = f"F{nom_type}N"

    return f"""{{{{Ahm
|date={start['timestamp'].strftime('%B %d, %Y')}
|oldid={start['revid']}
|process={process}
|result={result}
}}}}
{{{{Ahm
|date={completed['timestamp'].strftime('%B %d, %Y')}
|oldid={completed['revid']}
|process={status}
|user={start['user']}
|link={link}
}}}}
{{{{Ahf|status={status}}}}}"""


def build_history_text_for_removal(*, nom_type: str, link: str, revision: dict):
    return f"""{{{{Ahm
|date={revision['timestamp'].strftime('%B %d, %Y')}
|oldid={revision['revid']}
|process=F{nom_type}
|link={link}
}}}}
{{{{Ahf|status=F{nom_type}}}}}"""


def build_talk_page(*, talk_page: Page, nom_type: str, history_text: str, successful: bool, project_data: dict,
                    projects: list):
    # No talk page exists, so we can just create the talk page with the {{Talkheader}} and history templates.
    if not talk_page.exists():
        new_lines = ["{{Talkheader}}"]
        if successful:
            new_lines.append(f"{{{{{nom_type}}}}}")
        new_lines.append("{{Ahh}}")
        new_lines.append(history_text)
        for project in projects:
            project_talk = project_data.get(project, {}).get("template")
            print(f"{project}: {project_talk}")
            if project_talk:
                new_lines.append("{{" + project_talk + "}}")
        text = "\n".join(new_lines)

        return "", text, "Creating talk page with article nomination history"

    text = talk_page.get()
    lines = text.splitlines()
    new_lines = []

    for project in projects:
        project_talk = project_data.get(project, {}).get("template")
        if project_talk and project_talk not in text:
            history_text += ("\n{{" + project_talk + "}}")

    # {{Ahh}} template is present in page - add new entries
    if "{{ahh" in text.lower():
        found = False
        for line in lines:
            if "{{CA}}" in line or "{{FA}}" in line or "{{GA}}" in line:
                log(f"Removing old status template: {line}")
                continue
            elif "{{FormerCA}}" in line or "{{FormerGA}}" in line or "{{FormerFA}}" in line:
                log(f"Removing old status template: {line}")
                continue
            elif "{{ahh" in line.lower():
                if successful:
                    new_lines.append(f"{{{{{nom_type}}}}}")
                new_lines.append(line)
                found = True
                continue
            elif "{{ahf" in line.lower():
                if not found:
                    new_lines.append("{{Ahh}}")
                    if successful:
                        new_lines.append(f"{{{{{nom_type}}}}}")
                new_lines.append(history_text)
            else:
                new_lines.append(line)
        if not found:
            raise ArchiveException("Could not find {ahf} template")

    # {{Ahh}} template is not present, and no {{Talkheader}} either - add all templates
    elif "{{talkheader" not in text.lower():
        if successful:
            new_lines = ["{{Talkheader}}", f"{{{{{nom_type}}}}}", "{{Ahh}}", history_text, *lines]
        else:
            new_lines = ["{{Talkheader}}", "{{Ahh}}", history_text, *lines]

    # {{Ahh}} template is not present, but {{Talkheader}} is - add the {{Ahh}} templates below the {{Talkheader}}
    else:
        found = False
        for line in lines:
            if "{{talkheader" in line.lower():
                new_lines.append(line)
                found = True
                if successful:
                    new_lines.append(f"{{{{{nom_type}}}}}")
                new_lines.append("{{Ahh}}")
                new_lines.append(history_text)
            else:
                new_lines.append(line)
        if not found:
            new_lines.insert(0, history_text)
            new_lines.insert(0, "{{Ahh}}")
            if successful:
                new_lines.insert(0, f"{{{{{nom_type}}}}}")

    new_text = "\n".join(new_lines)

    return text, new_text, "Updating talk page with article nomination history"
