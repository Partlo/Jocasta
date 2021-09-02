import os
import json
import re
import requests
import tweepy
from typing import Optional, Tuple
from datetime import datetime
from bs4 import BeautifulSoup
from bs4.element import Tag

from jocasta.nominations.archiver import ArticleInfo
from jocasta.common import ArchiveException, error_log, log
from jocasta.data.filenames import *


class TwitterBot:
    """ Centralized class for handling Twitter posts.

    :type post_queue: list[ArticleInfo]
    """
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
    def parse_last_post_time(entry) -> Optional[datetime]:
        """ Parses last post time from the queue file. """

        try:
            return datetime.fromtimestamp(int(entry.replace("Last Post Time:", "").strip()))
        except Exception as e:
            error_log(type(e), e, entry)
        return None

    @staticmethod
    def parse_queue_info(entry) -> Optional[ArticleInfo]:
        """ Parses an ArticleInfo object from the queue file. """

        try:
            data = json.loads(entry)
            return ArticleInfo(data["title"], data["pageUrl"], data["nomType"], data["nominator"], data["projects"])
        except Exception as e:
            error_log(type(e), e, entry)
        return None

    def update_stored_queue(self):
        """ Writes the current post queue to a text file, with each entry in JSON form, as a backup. """

        with open(QUEUE_FILE, "w") as f:
            lines = []
            if self.last_post_time:
                lines.append(f"Last Post Time: {self.last_post_time.timestamp()}")
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
        """ Method used by the scheduler to post the next entry in the post queue to Twitter. """

        log(f"Queue Length: {len(self.post_queue)}")

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
        """ Posts an article to Twitter, in a series of threaded tweets. """

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
            log(f"Posting complete: {url_post.id}")
        except Exception as e:
            error_log(f"Encountered error while posting to Twitter: {e}")

    def post_tweet(self, info: ArticleInfo):
        """ Posts the initial tweet, containing the article intro and image (if there is one) """

        title = info.article_title.replace("/Legends", "")
        a_type = self.article_types[info.nom_type]
        tweet = f"Our newest #{a_type}Article, {title}, by user {info.nominator}! #StarWars"
        tweet += f"\nRead more here! {info.page_url}"

        self.api.update_status(status=tweet)
        log("Posting to Twitter:")
        log(tweet)

    @staticmethod
    def extract_intro(url) -> Tuple[str, str]:
        """ Extracts the introduction paragraph from the target article, ignoring the infobox and templates, and
          stripping out references. Also extracts the infobox image's URL. """

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

    @staticmethod
    def prepare_intro(intro: str) -> str:
        """" Limits the introduction to 280 characters, ending it with an ellipsis if it runs over. """

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

    @staticmethod
    def download_image(image_url) -> Optional[str]:
        """ Downloads the target image and writes it to a temporary file so that it can be uploaded to Twitter. """
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
                error_log(f"Unable to download image: {request.status_code} response")
                return None
        except Exception as e:
            error_log(type(e), e)
            return None

    def post_article(self, *, tweet, filename):
        """ Posts the initial introductory tweet to Twitter, along with the infobox image if there is one. """

        log("Posting to Twitter:")
        log(tweet)
        if filename:
            result = self.api.update_with_media(filename, tweet)
            os.remove(filename)
            return result
        else:
            return self.api.update_status(status=tweet)

    def post_credit(self, *, post_id, title, article_type, nominator, projects):
        """ Posts the credit tweet to Twitter, including the article name, nominator, and any WookieeProjects. """

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
        log(reply)
        return self.api.update_status(reply, post_id)

    def post_url(self, *, post_id, url, article_type):
        reply = f"#StarWars #Wookieepedia #{article_type}Articles\n{url}"
        log(reply)
        return self.api.update_status(reply, post_id)
