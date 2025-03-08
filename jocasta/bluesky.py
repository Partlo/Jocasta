import os
import json
import re
import requests
from atproto import Client, models, client_utils
from typing import Optional, Tuple
from datetime import datetime
from bs4 import BeautifulSoup
from bs4.element import Tag

from jocasta.nominations.data import ArticleInfo
from jocasta.common import ArchiveException, error_log, log
from jocasta.data.filenames import *


MAIN_DID = "did:plc:jmn2aepnms3cnzjuntg7i42w"
MAX_LENGTH = 300


class BlueSkyBot:
    """ Centralized class for handling BlueSky posts.

    :type post_queue: list[ArticleInfo]
    :type backlog: list[ArticleInfo]
    """
    def __init__(self, *, client: Client):
        self.client = client

        self.post_queue = []
        self.backlog = []
        self.last_post_time = None
        lpt1, lpt2 = None, None
        try:
            with open(QUEUE_FILE, "r") as f:
                for line in f.readlines():
                    entry = line.strip()
                    if entry and entry.startswith("Last Post Time:"):
                        lpt1 = self.parse_last_post_time(entry)
                    elif entry:
                        info = self.parse_queue_info(entry)
                        if info:
                            self.post_queue.append(info)
        except Exception:
            pass
        try:
            with open(BACKLOG_FILE, "r") as f:
                for line in f.readlines():
                    entry = line.strip()
                    if entry and entry.startswith("Last Post Time:") and not self.last_post_time:
                        lpt2 = self.parse_last_post_time(entry)
                    elif entry:
                        info = self.parse_queue_info(entry)
                        if info:
                            self.backlog.append(info)
        except Exception:
            pass
        if lpt2 and not lpt1:
            self.last_post_time = lpt2
        elif lpt1 and not lpt2:
            self.last_post_time = lpt1
        elif lpt1 and lpt2:
            self.last_post_time = max(lpt1, lpt2)
        else:
            self.last_post_time = None

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
            return ArticleInfo(data["title"], data["pageUrl"], data["nomType"], data["projects"])
        except Exception as e:
            error_log(type(e), e, entry)
        return None

    def update_stored_queue(self):
        """ Writes the current post queue to a text file, with each entry in JSON form, as a backup. """

        with open(QUEUE_FILE, "w+") as f:
            lines = []
            if self.last_post_time:
                lines.append(f"Last Post Time: {self.last_post_time.timestamp()}")
            for entry in self.post_queue:
                lines.append(json.dumps({
                    "title": entry.article_title,
                    "pageUrl": entry.page_url,
                    "nomType": entry.nom_type,
                    "projects": entry.projects
                }))
            f.writelines("\n".join(lines))

    def update_backlog(self):
        """ Writes the current post backlog to a text file, with each entry in JSON form, as a backup. """

        with open(BACKLOG_FILE, "w+") as f:
            lines = []
            if self.last_post_time:
                lines.append(f"Last Post Time: {self.last_post_time.timestamp()}")
            for entry in self.backlog:
                lines.append(json.dumps({
                    "title": entry.article_title,
                    "pageUrl": entry.page_url,
                    "nomType": entry.nom_type,
                    "projects": entry.projects
                }))
            f.writelines("\n".join(lines))

    def add_post_to_queue(self, info):
        self.post_queue.append(info)
        self.update_stored_queue()

    def scheduled_post(self):
        """ Method used by the scheduler to post the next entry in the post queue to BlueSky. """

        log(f"Queue Length: {len(self.post_queue)}; backlog length: {len(self.backlog)}")

        window = 20 if len(self.post_queue) else 60
        if self.last_post_time:
            diff = datetime.now().timestamp() - self.last_post_time.timestamp()
            if diff < (window * 60):
                self.last_post_time = None
                return

        if len(self.post_queue) > 0:
            post = self.post_queue.pop(0)
            self.post_article_to_bluesky(info=post)
            self.update_stored_queue()
        elif len(self.backlog) > 0:
            post = self.backlog.pop(-1)
            self.post_article_to_bluesky(info=post)
            self.update_backlog()

    def post_article_to_bluesky(self, *, info: ArticleInfo):
        """ Posts an article to BlueSky, in a series of threaded posts. """

        article_type = self.article_types[info.nom_type]
        original_title = info.article_title.replace("/Legends", "")

        try:
            full_intro, image_url, title = self.extract_intro(url=info.page_url, page_title=original_title)
            short_intro = self.prepare_intro(full_intro)
            intro_post = self.post_article(text=short_intro, image_url=image_url, title=title)

            link_post = self.post_link(post_ref=intro_post, title=title, article_type=article_type,
                                       projects=info.projects, url=info.page_url)
            log(f"Posting complete: {link_post.cid}")
            self.last_post_time = datetime.now()
        except Exception as e:
            error_log(f"Encountered error while posting to BlueSky: {e}")

    # def post_tweet(self, info: ArticleInfo):
    #     """ Posts the initial tweet, containing the article intro and image (if there is one) """
    #
    #     title = info.article_title.replace("/Legends", "")
    #     a_type = self.article_types[info.nom_type]
    #     tweet = f"Our newest #{a_type}Article, {title}, by user {info.nominator}! #StarWars"
    #     tweet += f"\nRead more here! {info.page_url}"
    #
    #     self.client.create_tweet(text=tweet)
    #     log("Posting to Twitter:")
    #     log(tweet)

    @staticmethod
    def extract_intro(url, page_title) -> Tuple[str, str, str]:
        """ Extracts the introduction paragraph from the target article, ignoring the infobox and templates, and
          stripping out references. Also extracts the infobox image's URL. """

        full_text = requests.get(url).text
        soup = BeautifulSoup(full_text, 'html.parser')
        target = soup.find("div", attrs={"class": "mw-parser-output"})
        if not target:
            raise ArchiveException("Cannot find article in page")

        paragraphs = []
        image_url = None
        first_header = None
        for child in target.children:
            if isinstance(child, Tag):
                if child.name == "h2" or child.name == "h3":
                    if not first_header:
                        first_header = child.text.replace("[", "").replace("]", "")
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
            t = target.text.split("[Source]", 1)[-1] if "[Source]" in target.text else target.text
            if f"\n{first_header}[]\n" in t:
                t = t.split(f"\n{first_header}[]\n", 1)[0]
                paragraphs.append(re.sub("\\[[0-9]+]", "", t.replace('\n', '')))

        if not paragraphs:
            raise ArchiveException(f"Cannot extract intro for {url}")

        text = "\n".join(paragraphs)
        page_title = f"{page_title}".split(" (")[0]
        if page_title in text:
            title = page_title
        elif (page_title[0].lower() + page_title[1:]).split(" (")[0] in text:
            title = page_title[0].lower() + page_title[1:]
        else:
            title = page_title

        if f"the {title}" in text or f"The {title}" in text:
            title = f"the {title}"
        elif f" a {title}" in text or f" A {title}" in text or text.startswith(f"A {title}"):
            title = f"a {title}"
        elif f" an {title}" in text or f" An {title}" in text or text.startswith(f"An {title}"):
            title = f"an {title}"

        return "\n".join(paragraphs), image_url, title

    @staticmethod
    def prepare_intro(intro: str) -> str:
        """ Limits the introduction to 280 characters, ending it with an ellipsis if it runs over. """

        length = 0
        text = ""
        for word in intro.split(" "):
            if length == 0:
                text = word
                length = len(word)
            elif (length + 1 + len(word)) >= MAX_LENGTH:
                text = f"{text} {word}"[:(MAX_LENGTH - 3)] + "..."
                break
            else:
                text += f" {word}"
                length += (1 + len(word))
                if length == MAX_LENGTH:
                    break
        return text

    @staticmethod
    def download_image(image_url) -> Optional[str]:
        """ Downloads the target image and writes it to a temporary file so that it can be uploaded to BlueSky. """
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

    def post_article(self, *, text, image_url, title):
        """ Posts the initial introductory post to BlueSky, along with the infobox image if there is one. """

        log(f"Posting to BlueSky: {title}")
        filename = self.download_image(image_url)

        if filename:
            with open(filename, 'rb') as f:
                img_data = f.read()

            aspect_ratio = models.AppBskyEmbedDefs.AspectRatio(height=100, width=100)
            post = self.client.send_image(
                text=text,
                image=img_data,
                image_alt=f'An image of {title}',
                image_aspect_ratio=aspect_ratio,
            )
            os.remove(filename)
            return models.create_strong_ref(post)

        return models.create_strong_ref(self.client.send_post(text=text))

    @staticmethod
    def build_start(title, article_type):
        builder = client_utils.TextBuilder()
        builder.text(f"Read more about {title}, a ")
        builder.link(f"{article_type} Article", f"https://starwars.fandom.com/wiki/Wookieepedia:{article_type}_articles")
        builder.text(", on ")
        builder.tag("#Wookieepedia", "#Wookieepedia")
        builder.text(", the Star Wars Wiki!")
        return builder

    @staticmethod
    def build_end(b: client_utils.TextBuilder):
        b.text("\n\nFollow this account and ")
        b.mention("@WookOfficial", MAIN_DID)
        b.text(" for more updates on all things ")
        b.tag("#StarWars", "#StarWars")
        b.text("!")

    @staticmethod
    def project_link(p):
        return f"https://starwars.fandom.com/wiki/Wookieepedia:WookieeProject_{p}".replace(" ", "_")

    def prepare_projects(self, builder, nxt, projects, post_length, end_length):
        if projects is not None and post_length <= MAX_LENGTH:
            if len(projects) == 1 or (post_length + len(nxt) + len(f"WookieeProject: {projects[0]}.")) > MAX_LENGTH:
                builder.text(nxt)
                builder.link(f" WookieeProject: {projects[0]}", self.project_link(projects[0]))
            elif projects:
                builder.text(f"{nxt}WookieeProjects: ")
                for p in projects:
                    if len(builder.build_text()) + end_length > (MAX_LENGTH - 1):
                        break
                    builder.link(p, self.project_link(p))
            builder.text(".")

    def post_link(self, *, post_ref, title, article_type, projects, url):
        """ Posts the credit post to BlueSky, including the article name, nominator, and any WookieeProjects. """

        builder = self.build_start(title, article_type)
        end = client_utils.TextBuilder()
        self.build_end(end)
        end_length = len(end.build_text())
        post_length = len(builder.build_text()) + end_length

        self.prepare_projects(builder, " This article was nominated as part of ", projects, post_length, end_length)
        if len(builder.build_text()) + end_length > MAX_LENGTH:
            self.prepare_projects(builder, " Nominated by ", projects, post_length, end_length)

        if len(builder.build_text()) + end_length > MAX_LENGTH:
            builder = self.build_start(title, article_type)

        self.build_end(builder)

        embed = models.AppBskyEmbedExternal.Main(
            external=models.AppBskyEmbedExternal.External(
                title=title,
                description='Read more on Wookieepedia, the Star Wars Wiki!',
                uri=url,
            )
        )
        return models.create_strong_ref(self.client.send_post(
            text=builder, embed=embed,
            reply_to=models.AppBskyFeedPost.ReplyRef(parent=post_ref, root=post_ref)
        ))
