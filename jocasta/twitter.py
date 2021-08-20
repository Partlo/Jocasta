import os
import json
import re
import requests
import tweepy
from datetime import datetime
from bs4 import BeautifulSoup
from bs4.element import Tag

from auth import build_auth
from archiver import ArchiveException, ArticleInfo
from data.filenames import *


class TwitterBot:

    def __init__(self, *, auth):
        self.api = tweepy.API(auth)

        self.post_queue = []
        self.last_post_time = None
        with open(QUEUE_FILE, "r") as f:
            for line in f.readlines():
                entry = line.strip()
                if entry and entry.startswith("Last Post Time:"):
                    self.last_post_time = self.parse_last_post_time(entry)
                elif entry:
                    info = self.parse_queue_info(entry)
                    if info:
                        self.post_queue.append(info)

    article_types = {"FA": "Featured", "GA": "Good", "CA": "Comprehensive"}

    @staticmethod
    def parse_last_post_time(entry):
        try:
            return datetime.fromtimestamp(int(entry.replace("Last Post Time:", "").strip()))
        except Exception as e:
            print(type(e), e, entry)
        return None

    @staticmethod
    def parse_queue_info(entry):
        try:
            data = json.loads(entry)
            return ArticleInfo(data["title"], data["pageUrl"], data["nomType"], data["nominator"], data["projects"])
        except Exception as e:
            print(type(e), e, entry)
        return None

    def update_stored_queue(self):
        with open(QUEUE_FILE, "w") as f:
            lines = []
            for entry in self.post_queue:
                lines.append(json.dumps({
                    "title": entry.article_title,
                    "pageUrl": entry.page_url,
                    "nomType": entry.nom_type,
                    "nominator": entry.nominator,
                    "projects": entry.projects
                }))
            f.writelines("\n".join(lines))

    def add_post_to_queue(self, info):
        self.post_queue.append(info)
        self.update_stored_queue()

    def scheduled_post(self):
        print(f"Queue Length: {len(self.post_queue)}")

        if len(self.post_queue) > 0:
            if self.last_post_time:
                diff = datetime.now().timestamp() - self.last_post_time.timestamp()
                if diff < (20 * 60):
                    self.last_post_time = None
                    return

            post = self.post_queue.pop(0)
            self.post_article_to_twitter(info=post)
            self.update_stored_queue()

    def post_article_to_twitter(self, *, info: ArticleInfo):
        article_type = self.article_types[info.nom_type]
        title = info.article_title.replace("/Legends", "")

        full_intro, image_url = self.extract_intro(url=info.page_url)
        short_intro = self.prepare_intro(full_intro)
        filename = self.download_image(image_url)

        try:
            intro_post = self.post_article(tweet=short_intro, filename=filename)
            credit_post = self.post_credit(post_id=intro_post.id, title=title, article_type=article_type,
                                           nominator=info.nominator, projects=info.projects)
            url_post = self.post_url(post_id=credit_post.id, url=info.page_url, article_type=article_type)
            print(f"Posting complete: {url_post.id}")
        except Exception as e:
            print(f"Encountered error while posting to Twitter: {e}")

    def post_tweet(self, info: ArticleInfo):
        title = info.article_title.replace("/Legends", "")
        a_type = self.article_types[info.nom_type]
        tweet = f"Our newest #{a_type}Article, {title}, by user {info.nominator}! #StarWars"
        tweet += f"\nRead more here! {info.page_url}"

        self.api.update_status(status=tweet)
        print("Posting to Twitter:")
        print(tweet)

    def extract_intro(self, url):
        soup = BeautifulSoup(requests.get(url).text, 'html.parser')
        target = soup.find("div", attrs={"class": "mw-parser-output"})
        if not target:
            raise ArchiveException("Cannot find article in page")

        paragraphs = []
        image_url = None
        for child in target.children:
            if isinstance(child, Tag):
                if child.name == "h2" or child.name == "h3":
                    break
                elif child.name == "div" and "toc" in child.get("id", ""):
                    break

                infobox = child.find("aside", attrs={"class": "portable-infobox"})
                if infobox:
                    img = infobox.find("img", attrs={"class": "pi-image-thumbnail"})
                    if img and img.get("src"):
                        image_url = img.get("src", "")
                elif child.name == "div" and "quote" in child.get("class", ""):
                    continue
                elif child.name == "p":
                    if child.text.strip():
                        paragraphs.append(re.sub("\\[[0-9]+]", "", child.text.replace('\n', '')))
        if not paragraphs:
            raise ArchiveException("Cannot extract intro")

        return "\n".join(paragraphs), image_url

    def prepare_intro(self, intro: str):
        length = 0
        tweet = ""
        for word in intro.split(" "):
            if length == 0:
                tweet = word
                length = len(word)
            elif (length + 1 + len(word)) >= 280:
                tweet = f"{tweet} {word}"[:277] + "..."
                break
            else:
                tweet += f" {word}"
                length += (1 + len(word))
                if length == 280:
                    break
        return tweet

    def download_image(self, image_url):
        if not image_url:
            return None
        try:
            filename = "temp.jpg"
            request = requests.get(image_url, stream=True)
            if request.status_code == 200:
                with open(filename, 'wb') as image:
                    for chunk in request:
                        image.write(chunk)
                return filename
            else:
                print(f"Unable to download image: {request.status_code} response")
                return None
        except Exception as e:
            print(type(e), e)
            return None

    def post_article(self, *, tweet, filename):
        print("Posting to Twitter:")
        print(tweet)
        if filename:
            result = self.api.update_with_media(filename, tweet)
            os.remove(filename)
            return result
        else:
            return self.api.update_status(status=tweet)

    def post_credit(self, *, post_id, title, article_type, nominator, projects):
        if nominator:
            reply = f"Read more about {title}, a {article_type} Article nominated by user {nominator}, on Wookieepedia, the Star Wars Wiki!"
        else:
            reply = f"Read more about {title}, a {article_type} Article, on Wookieepedia, the Star Wars Wiki!"
        end = "\n\nFollow this account and @WookOfficial for more updates on all things #StarWars!"
        post_length = len(reply) + len(end)

        middle = ""
        if projects is not None and len(projects) == 1:
            middle = f" This article was nominated as part of WookieeProject: {projects[0]}"
        elif projects:
            middle = f" This article was nominated as part of WookieeProjects: {' & '.join(projects)}"
            if post_length + len(middle) > 280:
                middle = f" This article was nominated as part of WookieeProject: {projects[0]}."

        if post_length + len(middle) > 280:
            middle = middle.replace("This article was nominated", "Nominated")
        if post_length + len(middle) > 280:
            middle = ""

        reply += (middle + end)
        print(reply)
        return self.api.update_status(reply, post_id)

    def post_url(self, *, post_id, url, article_type):
        reply = f"#StarWars #Wookieepedia #{article_type}Articles\n{url}"
        print(reply)
        return self.api.update_status(reply, post_id)


def main(*args):
    # authentication of consumer key and secret
    auth = build_auth()

    bot = TwitterBot(auth=auth)

    bot.post_tweet(
        ArticleInfo(title="Rath Cartel", page_url="https://starwars.fandom.com/wiki/Rath_Cartel", nom_type="CA",
                    nominator="Cade Calrayn", projects=["WookieeProject: The Old Republic"]))


if __name__ == "__main__":
    try:
        main()
    finally:
        pass
