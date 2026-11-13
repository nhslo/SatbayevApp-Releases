import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path

import requests
from markdown_it import MarkdownIt
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ASSET_NAME = "app-arm64-v8a-release.apk"
MAX_FILE_SIZE = 50_000_000
FOOTER = [
    ("Оригинальное приложение", "https://play.google.com/store/apps/details?id=kz.almaty.satbayevuniversity&pcampaignid=web_share"),
    ("GitHub", "https://github.com/nhslo/SatbayevApp-Releases"),
]


class Text:
    def __init__(self):
        self.value = ""
        self.entities = []

    def add(self, value):
        self.value += value

    def span(self, start, kind, **kwargs):
        if start < len(self.value):
            self.entities.append((start, len(self.value), {"type": kind, **kwargs}))

    def newline(self):
        if self.value and not self.value.endswith("\n"):
            self.add("\n")


def render_notes(markdown):
    text = Text()
    tokens = MarkdownIt("commonmark", {"html": False}).parse(markdown or "")
    language_heading = re.compile(r"^(русский|russian|ru|english|английский|en)\s*:?[\s]*$", re.I)
    section_label = re.compile(r"^(новое|изменения|исправления|обновления)\s*:?\s*$", re.I)
    english = re.compile(r"^(english|английский|en)\s*:?\s*$", re.I)
    russian = re.compile(r"^(русский|russian|ru)\s*:?\s*$", re.I)
    markers = [(i, re.sub(r"[*_`]+", "", token.content).strip())
               for i, token in enumerate(tokens) if token.type == "inline"]
    ru_markers = [(i, name) for i, name in markers if russian.fullmatch(name)]
    if ru_markers:
        start = ru_markers[0][0] + 1
        end = next((i for i, name in markers if i >= start and language_heading.fullmatch(name)), len(tokens))
        tokens = tokens[start:end]
    else:
        end = next((i for i, name in markers if english.fullmatch(name)), len(tokens))
        tokens = tokens[:end]

    for token in tokens:
        if token.type != "inline":
            continue
        fragment = Text()
        links = []
        for child in token.children or []:
            if child.type in ("text", "code_inline", "image"):
                fragment.add(child.content)
            elif child.type in ("softbreak", "hardbreak"):
                fragment.add("\n")
            elif child.type == "link_open":
                links.append((len(fragment.value), child.attrGet("href")))
            elif child.type == "link_close" and links:
                link_start, url = links.pop()
                if url and re.match(r"^https?://", url):
                    fragment.span(link_start, "text_link", url=url)
        # Category labels and links that some historical releases repeat are not changes.
        visible = fragment.value.strip()
        if not visible or section_label.fullmatch(visible) or language_heading.fullmatch(visible):
            continue
        if re.search(r"(?:оригинальное приложение|github приложения)", visible, re.I) and fragment.entities:
            continue
        # A paragraph may contain several explicit line breaks instead of a Markdown list.
        offset = 0
        for raw_line in fragment.value.split("\n"):
            prefix = re.match(r"^\s*(?:[-*•]\s+|\d+[.)]\s+)?\s*", raw_line).end()
            line = raw_line[prefix:].rstrip()
            if not line or section_label.fullmatch(line):
                offset += len(raw_line) + 1
                continue
            if text.value:
                text.add("\n")
            begin = len(text.value)
            text.add("- " + line)
            text.span(begin, "italic")
            for left, right, details in fragment.entities:
                left, right = max(left, offset + prefix), min(right, offset + prefix + len(line))
                if left < right:
                    text.entities.append((begin + 2 + left - offset - prefix,
                                          begin + 2 + right - offset - prefix, details))
            offset += len(raw_line) + 1
    if not re.search(r"[А-Яа-яЁё]", text.value):
        raise ValueError("Release notes must contain Russian text before the English section")
    text.add("\n\n")
    for index, (label, url) in enumerate(FOOTER):
        if index:
            text.add(" | ")
        start = len(text.value)
        text.add(label)
        text.span(start, "bold")
        text.span(start, "text_link", url=url)
    return text


def utf16_len(value):
    return len(value.encode("utf-16-le")) // 2


def chunks(text):
    start = 0
    limit = 1024
    while start < len(text.value):
        end = start
        units = 0
        while end < len(text.value):
            size = utf16_len(text.value[end])
            if units + size > limit:
                break
            units += size
            end += 1
        if end < len(text.value):
            boundary = text.value.rfind("\n", start + 1, end)
            if boundary > start + (end - start) // 2:
                end = boundary + 1
        part = text.value[start:end]
        entities = []
        for left, right, details in text.entities:
            left, right = max(left, start), min(right, end)
            if left < right:
                entities.append({**details, "offset": utf16_len(text.value[start:left]),
                                 "length": utf16_len(text.value[left:right])})
        yield part, entities
        start = end
        limit = 4096


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN secret")
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
    repo = os.environ["GITHUB_REPOSITORY"]
    api = f"https://api.github.com/repos/{repo}/releases"
    session = requests.Session()
    session.headers.update({"Accept": "application/vnd.github+json", "Authorization": f"Bearer {os.environ['GH_TOKEN']}", "X-GitHub-Api-Version": "2022-11-28"})
    retry = Retry(total=4, backoff_factor=2, status_forcelist=[429, 500, 502, 503, 504], allowed_methods=["GET"])
    session.mount("https://", HTTPAdapter(max_retries=retry))
    release_id = event.get("release", {}).get("id")
    endpoint = f"{api}/{release_id}" if release_id else f"{api}/latest"
    asset = None
    for attempt in range(31):
        response = session.get(endpoint, timeout=30)
        response.raise_for_status()
        release = response.json()
        asset = next((item for item in release["assets"] if item["name"] == ASSET_NAME and item["state"] == "uploaded"), None)
        if asset:
            break
        if attempt < 30:
            print(f"Waiting for {ASSET_NAME} ({attempt + 1}/30)", flush=True)
            time.sleep(20)
    if not asset:
        raise RuntimeError(f"{ASSET_NAME} was not uploaded within 10 minutes")
    if asset["size"] > MAX_FILE_SIZE:
        raise RuntimeError(f"APK size {asset['size']} exceeds the Telegram bot upload limit of {MAX_FILE_SIZE} bytes")
    match = re.search(r"(?<![A-Za-z0-9])v?(\d+(?:\.\d+)+(?:[-+][A-Za-z0-9.-]+)?)", release.get("name") or "", re.I)
    if not match:
        match = re.search(r"(?<![A-Za-z0-9])v?(\d+(?:\.\d+)+(?:[-+][A-Za-z0-9.-]+)?)", release["tag_name"], re.I)
    if not match:
        raise RuntimeError("Release title or tag must contain a version such as v2.5.1")
    filename = f"AppSatbayev_v{match.group(1)}.apk"
    parts = list(chunks(render_notes(release.get("body"))))
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / filename
        download = session.get(asset["browser_download_url"], timeout=(30, 180), stream=True)
        download.raise_for_status()
        digest = hashlib.sha256()
        size = 0
        with path.open("wb") as output:
            for block in download.iter_content(1024 * 1024):
                size += len(block)
                if size > MAX_FILE_SIZE:
                    raise RuntimeError("Downloaded APK exceeds Telegram's upload limit")
                digest.update(block)
                output.write(block)
        if size != asset["size"]:
            raise RuntimeError("Downloaded APK size does not match GitHub release asset")
        expected_digest = asset.get("digest")
        if expected_digest and expected_digest != f"sha256:{digest.hexdigest()}":
            raise RuntimeError("Downloaded APK checksum does not match GitHub release asset")
        base = f"https://api.telegram.org/bot{token}"
        chat = os.environ["TELEGRAM_CHAT_ID"]
        caption, entities = parts[0]
        # Never retry sends automatically: an interrupted response may have reached Telegram.
        with path.open("rb") as document:
            response = requests.post(f"{base}/sendDocument", data={"chat_id": chat, "caption": caption, "caption_entities": json.dumps(entities, ensure_ascii=False)}, files={"document": (filename, document, "application/vnd.android.package-archive")}, timeout=(30, 300))
        result = response.json()
        if not response.ok or not result.get("ok"):
            raise RuntimeError(f"Telegram rejected document: {result.get('description', response.status_code)}")
        print(f"Published {filename}; Telegram message ID: {result['result']['message_id']}", flush=True)
        for content, entities in parts[1:]:
            response = requests.post(f"{base}/sendMessage", json={"chat_id": chat, "text": content, "entities": entities, "disable_web_page_preview": True}, timeout=(30, 60))
            result = response.json()
            if not response.ok or not result.get("ok"):
                raise RuntimeError(f"Document was published, but a continuation failed: {result.get('description', response.status_code)}")
    print("Release publication completed.")


if __name__ == "__main__":
    try:
        main()
    except requests.RequestException:
        # Request exceptions may contain URLs with the bot token.
        raise SystemExit("Network request failed. Check connectivity and the Telegram channel before retrying.")
