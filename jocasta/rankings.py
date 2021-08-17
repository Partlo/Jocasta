import pywikibot
import datetime
import re

blacklisted = ["AV-6R7", "Toprawa and Ralltiir", "Darth_Culator", "Goodwood"]


hex_codes = [
    "FF3300",
    "FF6600",
    "FF9900",
    "FFCC00",
    "FFFF00",
    "CCFF00",
    "99FF00",
    "66FF00",
    "33FF00"
]


def compile_data(site):
    data = {}
    for year in range(2008, datetime.datetime.now().year + 1):
        page = pywikibot.Page(site, f"User:JocastaBot/Rankings/{year}")
        for line in page.get().splitlines():
            if line.startswith("|{{U|"):
                user, fa, ga, ca, score = line.split("||")
                user = user.replace("|{{U|", "").replace("}}", "")
                if user == "Spookycat27":
                    user = "Spookywilloww"
                if user not in data:
                    data[user] = {}
                data[user][year] = {"FA": int(fa), "GA": int(ga), "CA": int(ca), "score": int(score)}
    return data


def build_table(data, n_type):
    last_year = datetime.datetime.now().year + 1
    totals = {}
    header = ["! User"]
    start = 2010 if n_type == "CA" else 2008
    for year in range(2008, last_year):
        header.append(str(year))
        totals[year] = 0
    header.append("Total")

    lines = ['{|class="sortable" {{prettytable}}', " !! ".join(header)]
    if n_type == "merge":
        for user in sorted(data.keys()):
            lines.append('|- style="text-align:center"')
            line = '|style="text-align:left"|{{U|' + user + "}}"
            f_total = 0
            g_total = 0
            c_total = 0
            for year in range(2008, last_year):
                f = data[user].get(year, {}).get("FA", 0)
                f_total += f
                g = data[user].get(year, {}).get("GA", 0)
                g_total += g
                c = data[user].get(year, {}).get("CA", 0)
                c_total += c
                if f + g + c == 0:
                    line += '||style="color: #606060 !important; background-color: #b7b7b7;"|' + str(0)
                else:
                    line += f"||{f}-{g}-{c}"
            if f_total + g_total + c_total == 0:
                line += '||style="color: #606060 !important; background-color: #b7b7b7;"|' + str(0)
            else:
                line += f"||{f_total}-{g_total}-{c_total}"
            lines.append(line)
    else:
        for user in sorted(data.keys()):
            lines.append('|- style="text-align:center"')
            line = '|style="text-align:left"|{{U|' + user + "}}"
            user_total = 0
            for year in range(2008, last_year):
                x = data[user].get(year, {}).get(n_type, 0)
                user_total += x
                totals[year] += x
                if x == 0:
                    line += '||style="color: #606060 !important; background-color: #b7b7b7;"|' + str(x)
                else:
                    line += '||' + str(x)
            if user_total == 0:
                line += '||style="color: #606060 !important; background-color: #b7b7b7;"|' + str(user_total)
            else:
                line += '||' + str(user_total)
            lines.append(line)

    lines.append("|-")
    if n_type != "merge":
        total_row = ["|'''Total'''"]
        for year in range(2008, last_year):
            total_row.append(str(totals[year]))
        total_row.append(str(sum(totals.values())))
        lines.append('||style="text-align:center;"|'.join(total_row))
    lines.append("|}")

    return "\n".join(lines)


def combine_rankings(site, data):
    f = build_table(data, "FA")
    g = build_table(data, "GA")
    c = build_table(data, "CA")
    s = build_table(data, "score")
    m = build_table(data, "merge")
    lines = ["{{User:JocastaBot/Rankings/Header}}", "<tabber>", "|-|", "Featured=", f, "|-|", "Good=", g, "|-|",
             "Comprehensive=", c, "|-|", "Score=", s, "|-|", "Combined=", m, "</tabber>"]
    page = pywikibot.Page(site, "User:JocastaBot/Rankings")
    page.put("\n".join(lines), "Updating unified table")


def update_rankings_table(site):
    data = compile_data(site)
    combine_rankings(site, data)


def update_current_year_rankings(*, site, nominator, nom_type):
    """ Updates the rankings table, located at User:JocastaBot/Rankings/{CURRENT_YEAR} """

    page = pywikibot.Page(site, f"User:JocastaBot/Rankings/{datetime.datetime.now().year}")
    text = page.get()
    user_data = {}
    totals = {"CA": 0, "GA": 0, "FA": 0, "score": 0}
    for line in text.splitlines():
        if "{{U|" in line:
            match = re.search("\|.*?{{U\|(.*?)\}}.*?\|\|([0-9]+?)\|\|([0-9]+?)\|\|([0-9]+?)\|\|", line)
            if match:
                user = match.group(1)
                user_data[user] = {
                    "FA": int(match.group(2)),
                    "GA": int(match.group(3)),
                    "CA": int(match.group(4))
                }
                if user == nominator:
                    user_data[user][nom_type] += 1
                user_data[user]["score"] = (5 * user_data[user]["FA"]) + (3 * user_data[user]["GA"]) + user_data[user]["CA"]

                for k, v in user_data[user].items():
                    totals[k] += v

    rows = [
        "{{User:JocastaBot/Rankings/Header}}",
        """*FA earns 5 points, GA earns 3 points, and CA earns 1 point""",
        """{|class="sortable" {{prettytable}}""",
        """! User !! FAs !! GAs !! CAs !! Score"""
    ]
    for user, data in sorted(user_data.items(), key=lambda i: i[0].lower()):
        if user in blacklisted:
            s = "|<s>{{U|" + user + "}}</s>"
        else:
            s = "|{{U|" + user + "}}"
        s += f"||{data['FA']}||{data['GA']}||{data['CA']}||{data['score']}"
        rows.append("|-")
        rows.append(s)

    rows.append("|-")
    rows.append(f"|'''Total'''||{totals['FA']}||{totals['GA']}||{totals['CA']}||{totals['score']}")
    rows.append("|}")

    new_text = "\n".join(rows)
    page.put(new_text, f"Updating Rankings: +1 {nom_type} for [[User:{nominator}]]")
