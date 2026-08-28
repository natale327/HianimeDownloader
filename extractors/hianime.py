import base64
import json
import os
import socket
import sys
import time
from argparse import Namespace
from dataclasses import asdict, dataclass
from glob import glob
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup, Tag
from colorama import Fore
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from selenium_stealth import stealth
from seleniumwire import webdriver
from yt_dlp import YoutubeDL

from tools.functions import get_conformation, get_int_in_range, safe_remove
from tools.YTDLogger import YTDLogger


@dataclass
class Anime:
    name: str
    url: str
    sub_episodes: int
    dub_episodes: int
    anime_id: str = ""
    download_type: str = ""
    season_number: int = -1


class HianimeExtractor:
    def __init__(self, args: Namespace, name: str | None = None) -> None:
        self.args: Namespace = args

        self.link = self.args.link
        self.name = name

        self.HEADERS: dict[str, str] = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.8",
            "Referer": "https://hianime.dk/",
        }
        self.URL: str = "https://hianime.dk"
        self.DOWNLOAD_ATTEMPT_CAP: int = 45
        self.DOWNLOAD_REFRESH: tuple[int, int] = (20,)
        self.BAD_TITLE_CHARS: list[str] = [
            "-",
            ".",
            "/",
            "\\",
            "?",
            "%",
            "*",
            "<",
            ">",
            "|",
            '"',
            "[",
            "]",
            ":",
        ]
        self.TITLE_TRANS: dict[int, Any] = str.maketrans(
            "", "", "".join(self.BAD_TITLE_CHARS)
        )
        self.session: requests.Session = requests.Session()
        self.session.headers.update(self.HEADERS)

    def run(self):
        anime: Anime | None = (  # type: ignore
            self.get_anime_from_link(self.link)
            if self.link
            else (self.get_anime(self.name) if self.name else self.get_anime())
        )

        if not anime:
            return

        # Display chosen anime details
        print(
            Fore.LIGHTGREEN_EX
            + "\nYou have chosen "
            + Fore.LIGHTBLUE_EX
            + anime.name
            + Fore.LIGHTGREEN_EX
            + f"\nURL: {Fore.LIGHTBLUE_EX}{anime.url}{Fore.LIGHTGREEN_EX}"
            + "\nSub Episodes: "
            + Fore.LIGHTYELLOW_EX
            + str(anime.sub_episodes)
            + Fore.LIGHTGREEN_EX
            + "\nDub Episodes: "
            + Fore.LIGHTYELLOW_EX
            + str(anime.dub_episodes)
            + Fore.LIGHTCYAN_EX
        )

        if anime.sub_episodes != 0 and anime.dub_episodes != 0:
            anime.download_type = self.get_download_type()
        elif anime.dub_episodes == 0:
            print("Dub episodes are not available. Defaulting to sub.")
            anime.download_type = "sub"
        else:
            print("Sub episodes are not available. Defaulting to dub.")
            anime.download_type = "dub"

        episodes = self.get_episode_list(anime)

        if not episodes:
            print(f"{Fore.LIGHTRED_EX}No episodes found for this anime.")
            return

        number_of_episodes = len(episodes)
        if number_of_episodes != 1:
            start_ep = get_int_in_range(
                f"{Fore.LIGHTCYAN_EX}Enter the starting episode number (inclusive):{Fore.LIGHTYELLOW_EX} ",
                1,
                number_of_episodes,
            )
            end_ep = get_int_in_range(
                f"{Fore.LIGHTCYAN_EX}Enter the ending episode number (inclusive):{Fore.LIGHTYELLOW_EX} ",
                1,
                number_of_episodes,
            )
        else:
            start_ep = 1
            end_ep = 1

        anime.season_number = get_int_in_range(
            f"{Fore.LIGHTCYAN_EX}Enter the season number for this anime:{Fore.LIGHTYELLOW_EX} "
        )

        # Pick the streaming server once, based on the first selected episode
        server_name = self.select_server(anime, episodes[start_ep - 1])

        self.configure_driver()
        self.prime_player_settings()

        self.captured_video_urls: list[str] = []
        self.captured_subtitle_urls: list[str] = []
        for episode in episodes[start_ep - 1 : end_ep]:
            number = episode["number"]
            title = episode["title"]

            try:
                embed_available = (
                    self.get_embed_url(episode, server_name, anime) is not None
                )
            except Exception as e:
                print(
                    f"{Fore.LIGHTRED_EX}Error checking servers for episode "
                    f"{number}: {Fore.LIGHTWHITE_EX}{e}"
                )
                continue

            if not embed_available:
                print(
                    f"{Fore.LIGHTRED_EX}Server '{server_name}' not available for episode "
                    f"{number}, skipping."
                )
                continue

            print(
                Fore.LIGHTGREEN_EX
                + "Getting"
                + Fore.LIGHTWHITE_EX
                + f" Episode {number} - {title} via {server_name}"
                + Fore.LIGHTWHITE_EX
            )

            try:
                media_requests = self.capture_media_requests(
                    episode, server_name, anime
                )
                if not media_requests:
                    print("No m3u8 file was found skipping download")
                    continue

                episode.update(media_requests)
                self.captured_video_urls.append(str(media_requests["m3u8"]).lower())
                if media_requests.get("vtt"):
                    self.captured_subtitle_urls.append(
                        str(media_requests["vtt"]).lower()
                    )
            except KeyboardInterrupt:
                print("\n\nCanceling media capture...")
                if not get_conformation(
                    "Would you like to download link capture up to now? (y/n): "
                ):
                    self.driver.quit()
                    return

        self.driver.quit()
        print()
        self.download_streams(anime, episodes[start_ep - 1 : end_ep])

    def download_streams(self, anime: Anime, episodes: list[dict[str, Any]]):
        folder = (
            os.path.abspath(self.args.output_dir)
            + os.sep
            + anime.name
            + f" ({anime.download_type[0].upper()}{anime.download_type[1:]}){os.sep}"
        )
        os.makedirs(folder, exist_ok=True)

        # Write to JSON file
        with open(
            f"{folder}{anime.name} (Season {anime.season_number}).json", "w"
        ) as json_file:
            json.dump({**asdict(anime), "episodes": episodes}, json_file, indent=4)

        for episode in episodes:
            name = f"{anime.name} - s{anime.season_number:02}e{episode['number']:02} - {episode['title']}"
            if not episode.get("m3u8"):
                print(f"Skipping {name} (No M3U8 Stream Found)")
                continue

            result = self.yt_dlp_download(
                episode["m3u8"],
                episode["headers"],
                f"{folder}{name}.mp4",
            )
            if not result:
                break

            if episode.get("vtt"):
                self.yt_dlp_download(
                    episode["vtt"], episode["headers"], f"{folder}{name}.vtt"
                )
            elif not self.args.no_subtitles:
                print(f"Skipping {name}.vtt (No VTT Stream Found)")

    @staticmethod
    def get_download_type():
        ans = (
            input(
                f"\n{Fore.LIGHTCYAN_EX}Both sub and dub episodes are available. Do you want to download sub or dub? (Enter 'sub' or 'dub'):{Fore.LIGHTYELLOW_EX} "
            )
            .strip()
            .lower()
        )
        if ans == "sub" or ans == "s":
            return "sub"
        elif ans == "dub" or ans == "d":
            return "dub"
        print(
            f"{Fore.LIGHTRED_EX}Invalid response, please respond with either 'sub' or 'dub'."
        )
        return HianimeExtractor.get_download_type()

    def configure_driver(self) -> None:
        options: webdriver.ChromeOptions = webdriver.ChromeOptions()

        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument("window-size=760,900")
        options.add_argument("--disable-gpu")
        options.add_argument("--log-level=3")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-notifications")
        options.add_experimental_option("excludeSwitches", ["enable-logging"])
        options.add_experimental_option(
            "prefs",
            {
                "profile.default_content_setting_values.notifications": 2,
                "profile.default_content_setting_values.popups": 2,
            },
        )

        # selenium-wire's automatic proxy injection no longer works with
        # recent chromedriver builds, so the proxy is wired up by hand on a
        # free local port
        proxy_port = self._free_port()
        options.add_argument(f"--proxy-server=http://127.0.0.1:{proxy_port}")
        options.add_argument("--proxy-bypass-list=<-loopback>")

        seleniumwire_options: dict[str, Any] = {
            "port": proxy_port,
            "verify_ssl": False,
            "disable_encoding": True,
        }

        self.driver: webdriver.Chrome = webdriver.Chrome(
            options=options,
            seleniumwire_options=seleniumwire_options,
        )

        # capture everything (including megaplay/vidtube m3u8); ad blocking
        # is handled via Chrome prefs + JS popup block above, not via scopes
        # (scopes is a whitelist — setting it to ad hosts would hide m3u8)
        try:
            self.driver.scopes = [r".*"]
        except Exception:
            pass

        self.driver.implicitly_wait(10)

        self.driver.execute_script(
            """
                window.alert = function() {};
                window.confirm = function() { return true; };
                window.prompt = function() { return null; };
                window.open = function() {
                    console.log("Blocked a popup attempt.");
                    return null;
                };
            """
        )

        try:
            stealth(
                self.driver,
                languages=["en-US", "en"],
                vendor="Google Inc.",
                platform="Win32",
                webgl_vendor="Intel Inc.",
                renderer="Intel Iris OpenGL Engine",
                fix_hairline=True,
            )
        except Exception:
            pass

    @staticmethod
    def _free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    def prime_player_settings(self) -> None:
        """Turn on auto play so the embedded player starts streaming itself."""
        try:
            self.driver.get(self.URL + "/")
            self.driver.execute_script(
                "window.localStorage.setItem('settings', JSON.stringify("
                "{auto_play:'1', auto_skip_intro:'0', auto_next:'0'}));"
            )
        except Exception:
            pass

    def click_server(self, server_name: str, download_type: str) -> bool:
        """Click the matching server button on the watch page, if present."""
        try:
            WebDriverWait(self.driver, 20).until(
                EC.presence_of_element_located((By.ID, "servers-content"))
            )
        except Exception:
            return False

        script = """
            var name = arguments[0].toLowerCase(), type = arguments[1].toLowerCase();
            var items = document.querySelectorAll('#servers-content .server-item');
            for (var i = 0; i < items.length; i++) {
                var it = items[i];
                var t = (it.getAttribute('data-type') || '').toLowerCase();
                if (!t && it.closest) {
                    var p = it.closest('[data-type]');
                    t = p ? (p.getAttribute('data-type') || '').toLowerCase() : '';
                }
                var n = (it.getAttribute('data-server-name') || it.textContent || '').trim().toLowerCase();
                if (n === name && (t === type || t === '')) {
                    var btn = it.querySelector('.btn') || it;
                    btn.click();
                    return true;
                }
            }
            return false;
        """
        try:
            clicked = bool(
                self.driver.execute_script(script, server_name, download_type)
            )
        except Exception:
            return False
        if clicked:
            # Drop any traffic from the pre-click default embed so only the
            # chosen server's stream is captured
            del self.driver.requests
        return clicked

    def select_server(
        self, anime: Anime, episode: dict[str, Any]
    ) -> str:
        """Pick a server name (HD-1, HD-2, ...) for the chosen download type."""
        servers = self.get_servers(episode["id"], episode["url"])
        names: list[str] = []
        for server in servers:
            if server["type"] == anime.download_type and server["name"] not in names:
                names.append(server["name"])

        if not names:
            print(
                f"{Fore.LIGHTRED_EX}No servers found for '{anime.download_type}' episodes."
            )
            sys.exit(1)

        if self.args.server:
            for option in names:
                if option.lower().strip() == self.args.server.lower().strip():
                    print(
                        f"\n{Fore.LIGHTGREEN_EX}You chose: {Fore.LIGHTCYAN_EX}{option}"
                    )
                    return option
            print(
                f"{Fore.LIGHTGREEN_EX}The server name you provided does not exist, available servers: {', '.join(names)}\n"
            )

        print(f"\n{Fore.LIGHTGREEN_EX}Select the server you want to download from: \n")
        for i, option in enumerate(names, 1):
            print(f"{Fore.LIGHTRED_EX} {i}: {Fore.LIGHTCYAN_EX}{option}")

        selection = names[
            get_int_in_range(
                f"\n{Fore.LIGHTCYAN_EX}Server:{Fore.LIGHTYELLOW_EX} ", 1, len(names)
            )
            - 1
        ]
        print(f"\n{Fore.LIGHTGREEN_EX}You chose: {Fore.LIGHTCYAN_EX}{selection}")
        return selection

    def _api_get(
        self, path: str, params: dict[str, str] | None, referer: str
    ) -> dict[str, Any]:
        """GET one of the site's JSON API endpoints.

        The API rejects requests whose Referer is not a real watch page URL,
        so the page the user is "on" must always be passed along.
        """
        response = self.session.get(
            f"{self.URL}{path}",
            params=params,
            headers={
                "Referer": referer,
                "Origin": self.URL,
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "application/json, text/javascript, */*; q=0.01",
            },
            timeout=30,
        )
        response.raise_for_status()
        return response.json()

    def get_servers(self, episode_id: str, referer: str) -> list[dict[str, str]]:
        """Fetch the server list (with base64-encoded embed URLs) for an episode."""
        data = self._api_get(
            "/api/theme/episode/servers", {"episodeId": episode_id}, referer
        )
        if not data.get("status"):
            print(f"{Fore.LIGHTRED_EX}Failed to load servers for episode.")
            return []

        soup = BeautifulSoup(data.get("html", ""), "html.parser")
        servers: list[dict[str, str]] = []
        for item in soup.select(".server-item"):
            server_hash = item.get("data-hash")
            if not server_hash:
                continue
            parent = item.find_parent(attrs={"data-type": True})
            server_type = item.get("data-type") or (
                parent.get("data-type") if parent else ""
            )
            try:
                embed_url = str(base64.b64decode(str(server_hash)), "utf-8")
            except Exception:
                continue
            if "http" not in embed_url:
                continue
            servers.append(
                {
                    "name": str(item.get("data-server-name") or item.get_text(strip=True)),
                    "type": str(server_type),
                    "url": embed_url,
                }
            )
        return servers

    def get_embed_url(
        self, episode: dict[str, Any], server_name: str, anime: Anime
    ) -> str | None:
        servers = self.get_servers(episode["id"], episode["url"])
        for server in servers:
            if server["type"] == anime.download_type and server["name"] == server_name:
                return server["url"]
        print(
            f"{Fore.LIGHTRED_EX}Server '{server_name}' not found for episode {episode['number']}, skipping."
        )
        return None

    def get_episode_list(self, anime: Anime) -> list[dict[str, Any]]:
        """Fetch the full episode list through the site's JSON API."""
        if not anime.anime_id:
            return []

        data = self._api_get(
            f"/api/theme/episode/list/{anime.anime_id}", None, anime.url
        )
        if not data.get("status"):
            print(f"{Fore.LIGHTRED_EX}Failed to load episode list.")
            return []

        soup = BeautifulSoup(data.get("html", ""), "html.parser")
        episodes: list[dict[str, Any]] = []
        for item in soup.select("a.ep-item"):
            ep_id = str(item.get("data-id") or "")
            if not ep_id:
                continue
            episodes.append(
                {
                    "id": ep_id,
                    "number": int(str(item.get("data-number") or 0)),
                    "title": str(item.get("title") or ""),
                    "url": urljoin(self.URL, str(item.get("href") or "")),
                }
            )
        episodes.sort(key=lambda e: e["number"])
        return episodes

    def _response_body(self, request: Any) -> str | None:
        try:
            body = request.response.body
            if isinstance(body, bytes):
                return body.decode("utf-8", errors="replace")
            return str(body) if body else None
        except Exception:
            return None

    def capture_media_requests(
        self, episode: dict[str, Any], server_name: str, anime: Anime
    ) -> dict[str, Any] | None:
        attempt: int = 0
        settle: int = 3  # keep collecting a few extra seconds after a hit
        vtt_deadline: int = self.DOWNLOAD_ATTEMPT_CAP

        m3u8_requests: list[dict[str, Any]] = []  # {url, headers}
        seen_m3u8: set[str] = set()
        vtt_urls: list[str] = []
        seen_vtt: set[str] = set()
        seen_sources: set[str] = set()
        sources_bodies: list[str] = []

        del self.driver.requests
        self.driver.get(episode["url"])
        self.click_server(server_name, anime.download_type)

        while attempt <= self.DOWNLOAD_ATTEMPT_CAP:
            sys.stdout.write(
                f"\r{Fore.CYAN}Attempt #{attempt} - {self.DOWNLOAD_ATTEMPT_CAP - attempt} Attempts Remaining"
            )
            sys.stdout.flush()

            for request in list(self.driver.requests):
                if not request.response:
                    continue
                url = str(request.url)
                low = url.lower()

                if (
                    ".m3u8" in low
                    and "thumbnail" not in low
                    and low not in seen_m3u8
                    and low not in self.captured_video_urls
                ):
                    seen_m3u8.add(low)
                    m3u8_requests.append(
                        {"url": url, "headers": dict(request.headers)}
                    )
                    vtt_deadline = attempt + 8
                elif (
                    ".vtt" in low
                    and "thumbnail" not in low
                    and low not in seen_vtt
                    and low not in self.captured_subtitle_urls
                ):
                    seen_vtt.add(low)
                    vtt_urls.append(url)
                elif "getsources" in low and low not in seen_sources:
                    seen_sources.add(low)
                    body = self._response_body(request)
                    if body:
                        sources_bodies.append(body)

            found_m3u8: bool = bool(m3u8_requests)
            found_vtt: bool = bool(vtt_urls) or bool(sources_bodies)

            if found_m3u8 and (found_vtt or self.args.no_subtitles):
                if settle <= 0 or attempt >= vtt_deadline:
                    break
                settle -= 1
            elif found_m3u8 and attempt >= vtt_deadline:
                break

            attempt += 1
            if attempt in self.DOWNLOAD_REFRESH:
                self.driver.refresh()
                self.click_server(server_name, anime.download_type)
            time.sleep(1)

        print()
        if not m3u8_requests:
            print(f"{Fore.LIGHTRED_EX}No .m3u8 streams found.")
            return None

        # Prefer a master playlist if one was seen; otherwise use the media
        # playlist the player actually requested (last one wins)
        masters = [r for r in m3u8_requests if "master" in r["url"]]
        chosen = (masters or m3u8_requests)[-1]
        raw_headers = dict(chosen["headers"])
        # yt-dlp's http downloader is strict — extra proxy/browser headers
        # (Host, Content-Length, Sec-Fetch-*, etc.) cause 403 / 0 blocks on
        # the CDN. Keep only the ones the CDN actually checks.
        allowed = {"user-agent", "referer", "origin", "cookie", "accept", "accept-language"}
        headers = {k: v for k, v in raw_headers.items() if k.lower() in allowed}
        # ensure a Referer exists — fall back to the watch page / embed origin
        if "Referer" not in headers and "referer" not in headers:
            headers["Referer"] = episode["url"]
        if "User-Agent" not in headers and "user-agent" not in headers:
            headers["User-Agent"] = self.HEADERS["User-Agent"]
        # debug hint for the user
        print(f"{Fore.LIGHTBLACK_EX}  m3u8: {chosen['url'][:120]}")

        urls: dict[str, Any] = {
            "m3u8": str(chosen["url"]),
            "headers": headers,
        }

        # Subtitles: prefer explicit .vtt requests, then tracks inside the
        # getSources JSON response
        vtt: str | None = None
        for url in reversed(vtt_urls):
            if "english" in url or "/en-" in url:
                vtt = url
                break
        if not vtt and vtt_urls:
            vtt = vtt_urls[-1]
        if not vtt:
            for body in sources_bodies:
                try:
                    data = json.loads(body)
                except json.JSONDecodeError:
                    continue
                candidates: list[str] = []
                for track in data.get("tracks") or []:
                    file_url = str(track.get("file") or "")
                    label = str(track.get("label") or "").lower()
                    if file_url.endswith(".vtt"):
                        candidates.append(file_url)
                        if "english" in label or label == "en":
                            vtt = file_url
                if not vtt and candidates:
                    vtt = candidates[0]
                if vtt:
                    break

        if vtt:
            urls["vtt"] = vtt
        elif not self.args.no_subtitles:
            print(
                f"\n{Fore.LIGHTRED_EX}No .vtt streams found. Check that the subtitles are not apart of the video file, option '--no-subtitles' can be used to skip downloading subtitles."
            )
            self.args.no_subtitles = get_conformation(
                f"\n{Fore.LIGHTCYAN_EX}Would you like to skip the collection of subtiles on the following episodes (y/n): "
            )
            print()

        return urls

    def yt_dlp_download(self, url: str, headers: dict[str, str], location: str) -> bool:
        # yt-dlp's generic extractor needs a valid Referer/UA for the HLS CDN;
        # noisy headers from selenium-wire (sec-ch-*, Host) break it.
        yt_dlp_options: dict[str, Any] = {
            "no_warnings": False,
            "quiet": False,
            "outtmpl": location,
            "format": "best",
            "http_headers": headers,
            "logger": YTDLogger(),
            "fragment_retries": 10,
            "retries": 10,
            "socket_timeout": 60,
            "force_keyframes_at_cuts": True,
            "allow_unplayable_formats": True,
            "concurrent_fragment_downloads": 1,
            "hls_use_mpegts": True,
        }

        _return = True
        with YoutubeDL(yt_dlp_options) as ydl:
            try:
                ydl.download([url])
            except KeyboardInterrupt:
                print(
                    f"\n\n{Fore.LIGHTCYAN_EX}Canceling Downloads...\nRemoving Temp Files for {location[location.rfind(os.sep) + 1:-4]}"
                )
                _return = False
                ydl.close()

        if not _return:
            for file in [
                f
                for f in glob(location[:-4] + ".*")
                if not f.endswith((".mp4", ".vtt"))
            ]:
                safe_remove(file)

        return _return

    def get_anime(self, name: str | None = None) -> Anime | None:
        os.system("cls" if os.name == "nt" else "clear")
        print(Fore.LIGHTGREEN_EX + "\nHiAnime " + Fore.LIGHTWHITE_EX + "GDown\n")

        search_name: str = name if name else input("Enter Name of Anime: ")

        # GET ANIME ELEMENTS FROM PAGE
        url: str = urljoin(self.URL, "/search?keyword=" + search_name)
        search_page_response: requests.Response = self.session.get(url, timeout=30)
        search_page_soup: BeautifulSoup = BeautifulSoup(
            search_page_response.content, "html.parser"
        )

        main_content: Tag = search_page_soup.find("div", id="main-content")  # type: ignore
        anime_elements: list[Tag] = main_content.find_all("div", class_="flw-item")  # type: ignore

        if not anime_elements:
            print("No anime found")
            return  # Exit if no anime is found

        # MAKE DICT WITH ANIME TITLES
        anime_list: list[Anime] = []
        for element in anime_elements:
            raw_name: str = element.find("h3", class_="film-name").text  # type: ignore
            name_of_anime: str = raw_name.translate(self.TITLE_TRANS)
            link_element = element.find("a", class_="film-poster-ahref")
            href: str = str(link_element.get("href") if link_element else "")
            url_of_anime: str = urljoin(self.URL, href)

            try:
                # Some anime has no subs
                sub_episodes_available: int = element.find(
                    "div", class_="tick-item tick-sub"
                ).text  # type: ignore
            except AttributeError:
                sub_episodes_available: int = 0
            try:
                dub_episodes_available: int = element.find(
                    "div", class_="tick-item tick-dub"
                ).text  # type: ignore
            except AttributeError:
                dub_episodes_available: int = 0

            anime_list.append(
                Anime(
                    name_of_anime,
                    url_of_anime,
                    int(sub_episodes_available),
                    int(dub_episodes_available),
                )
            )

        # PRINT ANIME TITLES TO THE CONSOLE
        for i, anime in enumerate(anime_list, start=1):
            print(
                " "
                + Fore.LIGHTRED_EX
                + str(i)
                + ": "
                + Fore.LIGHTCYAN_EX
                + anime.name
                + Fore.WHITE
                + " | "
                + "Episodes: "
                + Fore.LIGHTYELLOW_EX
                + str(anime.sub_episodes)
                + Fore.LIGHTWHITE_EX
                + " sub"
                + Fore.LIGHTGREEN_EX
                + " / "
                + Fore.LIGHTYELLOW_EX
                + str(anime.dub_episodes)
                + Fore.LIGHTWHITE_EX
                + " dub"
            )

        # USER SELECTS ANIME
        selected = anime_list[
            get_int_in_range(
                f"\n{Fore.LIGHTCYAN_EX}Select an anime you want to download:{Fore.LIGHTYELLOW_EX} ",
                1,
                len(anime_list) + 1,
            )
            - 1
        ]
        return self.enrich_anime(selected)

    def get_anime_from_link(self, link: str) -> Anime:
        link_page: requests.Response = self.session.get(link, timeout=30)
        link_page_soup = BeautifulSoup(link_page.content, "html.parser")
        detail = link_page_soup.find(id="ani_detail")
        anime_id: str = str(detail.get("data-anime-id") or "") if detail else ""

        main_div: Tag = link_page_soup.find("div", "anisc-detail")  # type: ignore
        anime_name = ""
        if main_div:
            name_tag = main_div.find("h2", "film-name")
            if name_tag is not None:
                a_tag = name_tag.find("a")
                anime_name = str((a_tag or name_tag).text).translate(self.TITLE_TRANS)

        anime = Anime(
            name=anime_name or link.rstrip("/").rsplit("/", 1)[-1],
            url=link,
            sub_episodes=0,
            dub_episodes=0,
            anime_id=anime_id,
        )

        if main_div:
            anime_stats: Tag = main_div.find("div", "film-stats")  # type: ignore
            try:
                anime.sub_episodes = int(
                    anime_stats.find("div", class_="tick-item tick-sub").text  # type: ignore
                )
            except AttributeError:
                pass
            try:
                anime.dub_episodes = int(
                    anime_stats.find("div", class_="tick-item tick-dub").text  # type: ignore
                )
            except AttributeError:
                pass

        return self.enrich_anime(anime)

    def enrich_anime(self, anime: Anime) -> Anime:
        """Resolve the anime id (and correct detail URL) for an anime."""
        if not anime.anime_id:
            # detail page (/frieren-...-8018) has no ani_detail; watch page does.
            # Try extracting from URL first (slug ends with -<id>), then fetch page.
            import re

            m = re.search(r"-(\d+)(?:/)?(?:\?.*)?$", anime.url.strip())
            if m:
                anime.anime_id = m.group(1)
            if not anime.anime_id:
                try:
                    response = self.session.get(anime.url, timeout=30)
                    soup = BeautifulSoup(response.content, "html.parser")
                    detail = soup.find(id="ani_detail")
                    if detail:
                        anime.anime_id = str(detail.get("data-anime-id") or "")
                    if not anime.anime_id:
                        mm = re.search(r'data-anime-id="(\d+)"', response.text)
                        if mm:
                            anime.anime_id = mm.group(1)
                except Exception:
                    pass
            # need a watch-style URL as Referer for the JSON API
            if anime.anime_id and "/watch/" not in anime.url:
                anime.url = f"{self.URL}/watch/{anime.url.rstrip('/').rsplit('/', 1)[-1]}"
        return anime
