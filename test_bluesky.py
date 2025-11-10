import json
from jocasta.bluesky import BlueskyBot
from jocasta.auth import build_auth_client


def send_post():
    queue = []
    with open("jocasta/data/queue.txt", "r") as f:
        for x in f.readlines():
            if x and x.startswith("{"):
                queue.append(json.loads(x))
    print(len(queue))

    client = build_auth_client()
    bot = BlueskyBot(client=client)

    bot.scheduled_post()


if __name__ == "__main__":
    send_post()
