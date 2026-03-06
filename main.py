import calendar
import datetime
import os
import re
import smtplib
import urllib
import urllib.parse
from email.mime.text import MIMEText
from functools import cached_property
from typing import List, Optional

from dotenv import load_dotenv
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from nba_api.stats.endpoints import leaguegamefinder, boxscoretraditionalv3
from nba_api.stats.static import teams

from consts import channels, players_to_watch, NBA_channel, crunch_time_playlist


class NbaEmail:
    def __init__(self, today = None):
        load_dotenv()
        self.api_key = os.getenv("API_KEY")
        self.email_address = os.getenv("EMAIL_ADDRESS")
        self.email_password = os.getenv("EMAIL_PASSWORD")
        self.youtube = build("youtube", "v3", developerKey=self.api_key)
        self.today = today or datetime.datetime.now(datetime.UTC)
        self.yesterday = self.today - datetime.timedelta(days=1)
        self.team_nicknames = {team["nickname"] for team in teams.get_teams()}

        self.game_finder = leaguegamefinder.LeagueGameFinder(
            date_from_nullable=self.yesterday.strftime("%m/%d/%Y"),
            date_to_nullable=self.yesterday.strftime("%m/%d/%Y"),
            league_id_nullable="00",
        )

        self.matchup_header_idx = {
            h: i
            for i, h in enumerate(
                self.game_finder.league_game_finder_results.data["headers"]
            )
        }
        self.games = [
            game
            for game in self.game_finder.league_game_finder_results.data["data"]
            if "vs." not in game[self.matchup_header_idx["MATCHUP"]]
        ]

    def get_channel_id(self, username):
        """Fetch the channel ID from the username."""

        try:
            response = (
                self.youtube.channels()
                .list(
                    part="id",
                    forHandle=username,  # Use "forHandle" for @usernames
                )
                .execute()
            )

            if "items" in response and response["items"]:
                return response["items"][0]["id"]
            else:
                return None
        except HttpError as e:
            print(f"Error fetching channel ID: {e}")
            return None

    def search_video_in_channel(
        self, channel_username, search_terms):
        """Search for a video in a specific channel, ensuring all search terms appear in the title, and it was posted in the last 5 days.
        """
        channel_id = self.get_channel_id(channel_username)
        if not channel_id:
            return None  # If the channel ID isn't found, return None

        try:
            search_response = (
                self.youtube.search()
                .list(
                    part="snippet",
                    channelId=channel_id,
                    q=search_terms,
                    type="video",
                    maxResults=2,  # Fetch multiple results to check for term matches
                )
                .execute()
            )

            if "items" in search_response and search_response["items"]:
                terms = set(
                    search_terms.lower().split()
                )  # Convert search terms into a set of words
                five_days_ago = self.today - datetime.timedelta(days=5)

                for item in search_response["items"]:
                    title = item["snippet"]["title"]

                    published_at = datetime.datetime.strptime(
                        item["snippet"]["publishedAt"], "%Y-%m-%dT%H:%M:%SZ"
                    ).replace(tzinfo=datetime.UTC)

                    if (
                        all(term in title.lower() for term in terms)
                        and published_at >= five_days_ago
                    ):
                        video_id = item["id"]["videoId"]
                        channel_name = item["snippet"]["channelTitle"]
                        return f"{channel_name} - {title} - https://www.youtube.com/watch?v={video_id}"

            return None  # No matching video found in this channel

        except HttpError as e:
            print(f"API error while searching in {channel_username}: {e}")
            return None


    @cached_property
    def crunch_time_playlist_items(self) -> List[dict]:
        """
        Cached (per-instance) list of recent Crunch Time playlist items.

        Fetched from YouTube the first time it is accessed, then reused for the
        rest of the run so multiple close games don't trigger extra API calls.
        """
        channel_id = self.get_channel_id(NBA_channel)
        if not channel_id:
            return []

        playlist_id = self.get_playlist_id_by_name(channel_id, crunch_time_playlist)
        if not playlist_id:
            return []

        five_days_ago = self.today - datetime.timedelta(days=5)
        items: List[dict] = []

        try:
            request = self.youtube.playlistItems().list(
                part="snippet",
                playlistId=playlist_id,
                maxResults=50,
            )

            while request:
                response = request.execute()

                for item in response.get("items", []):
                    snippet = item.get("snippet") or {}

                    # Skip removed / private / foreign-channel videos
                    if snippet.get("videoOwnerChannelId") != channel_id:
                        continue

                    published_at_str = snippet.get("publishedAt")
                    if not published_at_str:
                        continue

                    try:
                        published_at = datetime.datetime.strptime(
                            published_at_str, "%Y-%m-%dT%H:%M:%SZ"
                        ).replace(tzinfo=datetime.UTC)
                    except ValueError:
                        continue

                    if published_at < five_days_ago:
                        continue

                    resource = snippet.get("resourceId") or {}
                    video_id = resource.get("videoId")
                    title = snippet.get("title")
                    channel_title = snippet.get("channelTitle")
                    if not (video_id and title and channel_title):
                        continue

                    items.append(
                        {
                            "title": title,
                            "publishedAt": published_at_str,
                            "videoId": video_id,
                            "channelTitle": channel_title,
                        }
                    )

                request = self.youtube.playlistItems().list_next(request, response)

        except HttpError as e:
            print(f"API error while fetching playlist '{crunch_time_playlist}': {e}")
            items = []

        return items

    def crunch_time_highlights(
            self,
            matchup,
            channel_username = NBA_channel,
            playlist_name = crunch_time_playlist
    ):
        """
        Search for a recent video (last 5 days) inside a specific playlist
        belonging to a channel. Uses a cached playlist fetch to avoid repeated API calls.
        """
        # We currently cache only the default NBA Crunch Time playlist; if a different
        # channel/playlist is requested, fall back to a one-off fetch in the future.
        cities_matchup = set(self.get_full_team_matchup(matchup, nicknames=False).lower().split())
        nicknames_matchup = set(self.get_full_team_matchup(matchup, cities=False).lower().split())

        for item in self.crunch_time_playlist_items:
            title = item["title"]
            if all(term in title.lower() for term in cities_matchup) or all(
                term in title.lower() for term in nicknames_matchup
            ):
                video_id = item["videoId"]
                channel_name = item["channelTitle"]
                return (
                    f"{channel_name} - {title} - "
                    f"https://www.youtube.com/watch?v={video_id}"
                )

        return None

    def get_highlights(self, matchup_name):
        """Search in multiple channels sequentially, then return a YouTube search URL if no results."""


        game_highlights = self.crunch_time_highlights(matchup_name) or ""

        expanded_search_terms = f"{self.get_full_team_matchup(matchup_name)} {self.yesterday.strftime('%b %d %Y').replace(' 0', ' ')}"

        # Try searching in each channel in order
        for channel in channels:
            result = self.search_video_in_channel(channel, expanded_search_terms)
            if result:
                game_highlights += "\n" + result
                return game_highlights


        # If no video found, return search link
        encoded_query = urllib.parse.quote(expanded_search_terms)
        game_highlights += (
            "\n" + f"https://www.youtube.com/results?search_query={encoded_query}"
        )
        return game_highlights

    @staticmethod
    def youtube_search_url(query):
        base_url = "https://www.youtube.com/results"
        params = {"search_query": query}
        return f"{base_url}?{urllib.parse.urlencode(params)}"

    @staticmethod
    def get_full_team_matchup(abbreviated_matchup, cities = True, nicknames = True):
        abbrevs = abbreviated_matchup.split(" @ ")
        fulls = []
        for team in teams.get_teams():
            if team["abbreviation"] in abbrevs:
                if cities:
                    fulls.append(team["city"])
                if nicknames:
                    fulls.append(team["nickname"])
        return " ".join(fulls)


    def get_playlist_id_by_name(self, channel_id, playlist_name):
        request = self.youtube.playlists().list(
            part="snippet",
            channelId=channel_id,
            maxResults=50,
        )

        while request:
            response = request.execute()

            for item in response.get("items", []):
                if item["snippet"]["title"].lower() == playlist_name.lower():
                    return item["id"]

            request = self.youtube.playlists().list_next(request, response)

        return None

    def filter_key_terms(self, input_string):
        words = input_string.split()
        filtered_terms = [
            word
            for word in words
            if word in set(calendar.month_abbr[1:])
            or word in self.team_nicknames
            or re.fullmatch(r"[1-9]|[12][0-9]|3[01]", word)
        ]
        return " ".join(filtered_terms)

    @staticmethod
    def find_top_scorers(game_id, top_scorers):
        box_score = boxscoretraditionalv3.BoxScoreTraditionalV3(
            game_id=game_id,
            start_period=1,
            end_period=4,
            start_range=0,
            end_range=0,
            range_type=0,
        )
        players = box_score.player_stats.data["data"]
        header_idx = {
            h: i for i, h in enumerate(box_score.player_stats.data["headers"])
        }

        for player in players:
            if player[header_idx["personId"]] in players_to_watch:
                to_watch = " ".join(
                    [
                        player[header_idx["firstName"]],
                        player[header_idx["familyName"]],
                        "Points:",
                        str(player[header_idx["points"]]),
                        ", FG%: ",
                        str(player[header_idx["fieldGoalsPercentage"]]),
                        ", Assists:",
                        str(player[header_idx["assists"]]),
                        ", Rebounds",
                        str(player[header_idx["reboundsTotal"]]),
                        ", Turnovers:",
                        str(player[header_idx["turnovers"]]),
                        ", +/-: ",
                        str(player[header_idx["plusMinusPoints"]]),
                        "\n",
                    ]
                )
                top_scorers += to_watch
            elif player[header_idx["points"]] > 25:
                top_scorer = " ".join(
                    [
                        player[header_idx["firstName"]],
                        player[header_idx["familyName"]],
                        str(player[header_idx["points"]]),
                        "\n",
                    ]
                )
                top_scorers += top_scorer

        return top_scorers

    def send_email(self, email_string):
        msg = MIMEText(email_string)
        msg["Subject"] = f"NBA CLOSE GAMES {self.yesterday.strftime('%B %d, %Y')}"
        msg["From"] = self.email_address
        msg["To"] = self.email_address
        # Send the email using Gmail's SMTP server

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(self.email_address, self.email_password)
            server.sendmail(self.email_address, self.email_address, msg.as_string())

        # Printing can fail on Windows consoles when the body contains emojis or
        # other characters not supported by the current code page. Fall back to
        # a lossy ASCII-safe representation instead of crashing.
        try:
            print(email_string)
        except UnicodeEncodeError:
            safe_preview = email_string.encode("ascii", errors="ignore").decode(
                "ascii", errors="ignore"
            )
            print(safe_preview)

    def run(self):
        close_games = ""
        blowouts = ""
        knicks_game = ""
        game_ids = {}
        top_scorers = "\n\nTop Scorers:\n"

        for game in self.games:
            # For example:
            matchup_name = game[self.matchup_header_idx["MATCHUP"]]
            game_id = game[self.matchup_header_idx["GAME_ID"]]
            # {"NYK @ TOR": 1627673}
            game_ids[matchup_name] = game_id
            if "NYK" in matchup_name:
                knicks_game += f"\n\nKnicks Played Last Night! {matchup_name}\n"
                knicks_game += self.get_highlights(matchup_name)

            elif abs(game[self.matchup_header_idx["PLUS_MINUS"]]) < 11.0:
                close_games += f"\n\nClose game! {matchup_name}\n"
                close_games += self.get_highlights(matchup_name)

            else:
                blowouts += "\n\n" + matchup_name + "\n"
                # Calculate score by getting the points of one the away teams and then using the Plus Minus to find the other's score
                blowouts += (
                    str(game[self.matchup_header_idx["PTS"]])
                    + "   "
                    + str(
                        int(
                            game[self.matchup_header_idx["PTS"]]
                            - game[self.matchup_header_idx["PLUS_MINUS"]]
                        )
                    )
                )

            top_scorers = self.find_top_scorers(game_id, top_scorers)

        top_plays_link = "\n\nTop Plays:\n"
        top_plays_link += self.search_video_in_channel(
            "@NBA",
            f"NBA Top Plays of the Night | {self.yesterday.strftime('%b %d %Y').replace(' 0', ' ')}",
        ) or self.youtube_search_url(
            f"NBA's Top Plays of the Night | {self.yesterday.strftime('%b %d %Y').replace(' 0', ' ')}"
        )

        nightly_recap_link = "\n\nNightly Recap:\n"
        nightly_recap_link += self.search_video_in_channel(
            "@NBA",
            f"NBA Nightly Recap | "
            f"{self.yesterday.strftime('%B')} {self.yesterday.day},"
            f" {self.yesterday.year}",
        ) or self.youtube_search_url(
            f"NBA Nightly Recap | "
            f"{self.yesterday.strftime('%B')} {self.yesterday.day},"
            f" {self.yesterday.year}"
        )

        email_string = (
            knicks_game
            + close_games
            + blowouts
            + top_plays_link
            + nightly_recap_link
            + top_scorers
        )
        self.send_email(email_string)


if __name__ == "__main__":
    x = datetime.datetime.now(datetime.UTC) # - datetime.timedelta(days=4)
    nba = NbaEmail(x)
    nba.run()
