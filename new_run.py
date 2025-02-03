from jocasta.core import JocastaBot
import os

password_file = os.path.abspath(os.path.join(os.path.curdir, "user-password-new.py"))
if not os.path.isfile(password_file):
    pf = password_file.replace('\\', '/')
    print(f"Creating password file at {pf}")
    with open(pf, "w+") as f:
        f.writelines(f"('JocastaBot', '{os.getenv('PASSWORD')}')")

client = JocastaBot()
client.run(os.getenv("DISCORD_TOKEN"))
