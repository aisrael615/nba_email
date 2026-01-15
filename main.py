import calendar
import datetime
import os
import re
import smtplib
import urllib
import urllib.parse
from email.mime.text import MIMEText

from dotenv import load_dotenv
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from nba_api.stats.endpoints import leaguegamefinder, boxscoretraditionalv3
from nba_api.stats.static import teams

from consts import channels, players_to_watch


class NbaEmail:
    def __init__(self):
        load_dotenv()
        self.api_key = os.getenv("API_KEY")
        self.email_address = os.getenv("EMAIL_ADDRESS")
        self.email_password = os.getenv("EMAIL_PASSWORD")
        self.yesterday = datetime.datetime.now() - datetime.timedelta(days=1)
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
        youtube = build("youtube", "v3", developerKey=self.api_key)

        try:
            response = (
                youtube.channels()
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
        self, channel_username, search_terms, exclude_highlights=False
    ):
        """Search for a video in a specific channel, ensuring all search terms appear in the title, and it was posted in the last 5 days.
        Optionally exclude videos with 'FULL GAME HIGHLIGHTS' in the title if exclude_highlights is True.
        """
        channel_id = self.get_channel_id(channel_username)
        if not channel_id:
            return None  # If the channel ID isn't found, return None

        youtube = build("youtube", "v3", developerKey=self.api_key)

        try:
            search_response = (
                youtube.search()
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
                five_days_ago = datetime.datetime.now(
                    datetime.UTC
                ) - datetime.timedelta(days=5)

                for item in search_response["items"]:
                    title = item["snippet"]["title"]

                    published_at = datetime.datetime.strptime(
                        item["snippet"]["publishedAt"], "%Y-%m-%dT%H:%M:%SZ"
                    ).replace(tzinfo=datetime.UTC)

                    if exclude_highlights and "HIGHLIGHTS".lower() in title.lower():
                        continue  # Skip videos with "FULL GAME HIGHLIGHTS" in the title if flag is enabled

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

    def search_video(self, search_terms):
        """Search in multiple channels sequentially, then return a YouTube search URL if no results."""
        highlight_video = (
            self.search_video_in_channel(
                "@NBA", self.filter_key_terms(search_terms), exclude_highlights=True
            )
            or ""
        )

        # Try searching in each channel in order
        for channel in channels:
            result = self.search_video_in_channel(channel, search_terms)
            if result:
                highlight_video += "\n" + result
                return highlight_video

        encoded_query = urllib.parse.quote(search_terms)
        highlight_video += (
            "\n" + f"https://www.youtube.com/results?search_query={encoded_query}"
        )
        return highlight_video

    @staticmethod
    def youtube_search_url(query):
        base_url = "https://www.youtube.com/results"
        params = {"search_query": query}
        return f"{base_url}?{urllib.parse.urlencode(params)}"

    @staticmethod
    def get_full_team_matchup(abbreviated_matchup):
        abbrevs = abbreviated_matchup.split(" @ ")
        fulls = []
        for team in teams.get_teams():
            if team["abbreviation"] in abbrevs:
                fulls.append(team["full_name"])
        return " ".join(fulls)

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

        print(email_string)

    def run(self):
        close_games = ""
        blowouts = ""
        knicks_game = ""
        game_ids = {}
        top_scorers = "\n\nTop Scorers:\n"

        for game in self.games:
            # For example:
            matchup_name = game[self.matchup_header_idx["MATCHUP"]]
            expanded_matchup_name = self.get_full_team_matchup(matchup_name)
            game_id = game[self.matchup_header_idx["GAME_ID"]]
            # {"NYK @ TOR": 1627673}
            game_ids[matchup_name] = game_id
            if "NYK" in matchup_name:
                knicks_game += f"\n\nKnicks Played Last Night! {matchup_name}\n"
                knicks_game += self.search_video(
                    f"{expanded_matchup_name} {self.yesterday.strftime('%b %d %Y').replace(' 0', ' ')}"
                )

            elif abs(game[self.matchup_header_idx["PLUS_MINUS"]]) < 11.0:
                close_games += f"\n\nClose game! {matchup_name}\n"
                close_games += self.search_video(
                    f"{expanded_matchup_name} {self.yesterday.strftime('%b %d %Y').replace(' 0', ' ')}"
                )

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
    nba = NbaEmail()
    nba.run()
