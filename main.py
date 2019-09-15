from multiprocessing import Process, Queue
from multiprocessing.pool import ThreadPool
from multiprocessing import Event
from time import sleep
import sys
import toml
from selenium import webdriver
from selenium.webdriver.firefox.options import Options
import re
from bs4 import BeautifulSoup
import signal
from fuzzywuzzy import fuzz
import smtplib
import traceback
import logging
logging.basicConfig(filename='arb_crawler.log', level=logging.DEBUG)

class InvalidOdds(Exception):
    pass


class NotArbitrageScenario(Exception):
    pass


class Site:
    def __init__(self, name, urls):
        self.name = name
        self.urls = urls


class Bovada(Site):
    def __init__(self, name, urls):
        Site.__init__(self, name, urls)
        self.games_xpath = "/html/body/bx-site/ng-component/div/sp-main/div/main/div/section/main/sp-home/div/sp-next-events/div/div/div/sp-coupon"

    def parse_page(self, page_source, url):
        """
        Determine which parser to use for particular page source based on the url

        :param page_source: String HTML page source
        :param url: String URL of source location
        :return: List of Game objects
        """

        return self.parse_all_sports_page(page_source)

    def parse_all_sports_page(self, page_source):
        """
        Takes the page source for the main sports page on Bovada and parses the page to create a list of games

        :param page_source: String HTML page source
        :return: List of Game objects
        """
        found_games = []

        soup = BeautifulSoup(page_source, 'lxml')
        games = soup.find_all("sp-coupon")

        for game in games:
            competitors = game.find_all("h4", {"class": "competitor-name"})
            team_a = competitors[0].find('span').text
            team_b = competitors[1].find('span').text
            if len(game.find_all("sp-two-way-vertical", {"class": "market-type"})) == 3:
                # normal game with no draw
                bet_types = game.find_all("sp-two-way-vertical", {
                    "class": "market-type"})  # find all market types, this will be spread, moneyline win and O/U
                odds = bet_types[1].find_all('span', {
                    "class": "bet-price"})  # this will take just the moneyline win odds
                if len(odds) < 2:
                    continue

            elif len(game.find_all("sp-three-way-vertical", {"class": "market-type"})) == 3:
                # game with draw
                bet_types = game.find_all("sp-three-way-vertical", {
                    "class": "market-type"})  # find all market types, this will be spread, moneyline win and O/U
                odds = bet_types[1].find_all('span', {
                    "class": "bet-price"})  # this will take just the moneyline win odds
                if len(odds) < 2:
                    continue
            else:
                # something went wrong
                continue

            ml1 = re.search('[+-]\d+|EVEN', odds[0].text).group(0)

            ml2 = re.search('[+-]\d+|EVEN', odds[1].text).group(0)

            # check if the moneyline is EVEN, and change to string of numerical counterpart
            if ml1 == "EVEN":
                ml1 = "+100"
            if ml2 == "EVEN":
                ml2 = "+100"

            new_site_odds = SiteOdds(self, ml1=int(ml1), ml2=int(ml2))

            found_games.append(Game(team_1_name=team_a, team_2_name=team_b, site_odds=new_site_odds))

        logging.debug("BOVADA: PARSE_ALL_SPORTS_PAGE: Bovada found games this run:")
        logging.debug(found_games)
        return found_games


class MyBookie(Site):
    def __init__(self, name, urls):
        Site.__init__(self, name, urls)
        self.games_xpath = ""
        self.game_element_mapping = {}

    def parse_page(self, page_source, url):
        """
        Determine which parser to use for particular page source based on the url

        :param page_source: String HTML page source
        :param url: String URL of source location
        :return: List of Game objects
        """

        return self.parse_sportsbook_page(page_source)

    def parse_sportsbook_page(self, page_source):
        """
        Takes the page source for the main sports page on Bovada and parses the page to create a list of games

        :param page_source: String HTML page source
        :return: List of Game objects
        """

        found_games = []

        soup = BeautifulSoup(page_source, 'lxml')
        tags = soup.find_all("button", {"class": "lines-odds", "data-wager-type": "ml"})

        for i in range(0, len(tags), 2):
            team_a = tags[i]['data-team']
            team_b = tags[i+1]['data-team']

            # Special Case: ignore any first half bets, first two characters will be "1H"
            # TODO: make this better so that 1H bets become own game
            if team_a[:2] == "1H" or team_b[:2] == "1H":
                continue

            ml1 = tags[i]['data-odds']
            ml2 = tags[i+1]['data-odds']

            # Skip this game if there aren't odds for both outcomes
            # TODO: make this better
            if ml1 == "":
                continue
            else:
                ml1 = int(ml1)
            if ml2 == "":
                continue
            else:
                ml2 = int(ml2)

            new_site_odds = SiteOdds(self, ml1=ml1, ml2=ml2)

            found_games.append(Game(team_1_name=team_a, team_2_name=team_b, site_odds=new_site_odds))

        return found_games


class SiteOdds:

    def moneyline_to_decimal(self, ml):
        """ Converts the positive or negative moneyline value into a
        decimal odds, essentially the amount of payout including
        initial $1 wager if event happens.

        :param ml: integer moneyline value for event
        :return: float decimal odds
        """

        if ml > 0:
            return (ml + 100)/100
        return 100 / -ml + 1

    def __init__(self, site, ml1=None, ml2=None, odds1=None, odds2=None, url=None):
        if ml1 is None and ml2 is None and odds1 is None and odds2 is None:
            raise InvalidOdds
        if odds1 is None and odds2 is None:
            self.odds1 = self.moneyline_to_decimal(ml1)
            self.odds2 = self.moneyline_to_decimal(ml2)
        else:
            self.odds1 = odds1
            self.odds2 = odds2

        self.site = site
        self.ml1 = ml1
        self.ml2 = ml2
        self.url = url


class Game:

    def __init__(self, team_1_name, team_2_name, site_odds=None):
        self.team_1_name = team_1_name

        self.team_2_name = team_2_name

        if site_odds is not None:
            self.site_odds = [site_odds]
        else:
            self.site_odds = []

        self.arbitrage_opportunity = False
        self.margin = None
        self.wager_ratio_1 = None
        self.wager_ratio_2 = None
        self.arb_site_odds_1 = None
        self.arb_site_odds_2 = None

        self.league = None
        self.time = None
        self.date = None

    def add_site_odds(self, site_odds):
        self.site_odds.append(site_odds)

    def __repr__(self):
        repr_str = "{} vs. {}\n".format(self.team_1_name, self.team_2_name)
        for i in self.site_odds:
            repr_str = repr_str + "    Site: {} {} {} vs. {} {}".format(i.site.name, i.ml1, i.odds1, i.ml2, i.odds2)
        return repr_str


class ArbCrawler:
    """
    Web crawler that finds arbitrage betting situations
    Maths: http://www.aussportsbetting.com/guide/sports-betting-arbitrage/
    """

    def __init__(self, config_file):
        self.crawler_process = None
        self.arbitrage_actioner_process = None
        self.game_watcher_process = None
        self.game_queue = None

        self.shutdown_event = Event()

        self.sites = []

        with open(config_file, 'r') as f:
            c = f.read()
            self.config = toml.loads(c)

        # Create the site object that arbcrawler will use from the config
        # Only supports Bovada and MyBookie

        for page in self.config['pages']:
            found_existing_site = False
            for site in self.sites:
                if site.name == page['site_name']:
                    site.urls.append(page['url'])
                    found_existing_site = True
                    break
            if not found_existing_site:
                if page['site_name'] == 'Bovada':
                    new_site = Bovada(name=page['site_name'], urls=[page['url']])
                elif page['site_name'] == 'MyBookie':
                    new_site = MyBookie(name=page['site_name'], urls=[page['url']])

                self.sites.append(new_site)

        for i in self.sites:
            for url in i.urls:
                print(i.name)
                print(url)

        # game queue is a queue for games that the crawler has found
        self.game_queue = Queue()
        # arb_gueue is a queue for games that the analyzer has determined that are arbitrage opportunities
        self.arb_queue = Queue()

        # Driver for selenium
        options = Options()
        options.headless = False
        self.driver = webdriver.Firefox(options=options)

        # How similar names have to be to match. Smaller is more lenient, larger is more stringent
        self.difference_parameter = 50

        self.mail_server = 'smtp.gmail.com'
        self.mail_server_port = 465
        self.gmail_user = 'sportsarbbot@gmail.com'
        self.gmail_pass = 'S7#lf5wU6m'
        self.email_recipients = ['nateohlson@gmail.com', '5743231919@vtext.com']

        # interval in minutes for site recheck
        self.interval_minutes = 5

    def moneyline_to_decimal(self, ml):
        """ Converts the positive or negative moneyline value into
        decimal odds, essentially the amount of payout including
        initial $1 wager if event happens.

        :param ml: integer moneyline value for event
        :return: float decimal odds
        """

        if ml > 0:
            return (ml + 100)/100
        return 100 / -ml + 1

    def determine_margin_moneyline(self, mla, mlb):
        """ Determine the margin from moneyline odds

        :param mla: int moneyline odds for event a
        :param mlb: int moneyline odds for event b
        :return: float margin value
        """

        margin = (1 / self.moneyline_to_decimal(mla) + 1 / self.moneyline_to_decimal(mlb)) - 1

        return margin

    def determine_margin_decimal(self, mla, mlb):
        """ Determine the margin from decimal odds

        :param mla: float decimal odds for event a
        :param mlb: float decimal odds for event b
        :return: float margin value
        """

        margin = (1/mla + 1/mlb) - 1

        return margin

    def determine_wager_ratio(self, odds1, odds2):
        """
        Determines the ratio of money should be spent on event1 vs event 2,
        if not an arbitrage opportunity will raise NotArbitrageScenario. Returns a tuple with the proportion of
        money that should be wagered

        :param odds1: float decimal odds of event 1 happening
        :param odds2: float decimal odds of event 2 happening
        :return: (float, float) tuple of money amounts to wager on event1 and event2 respectively
        :raises NotArbitrageScenario:
        """

        if self.determine_margin_decimal(odds1, odds2) >= 0:
            raise NotArbitrageScenario

        w1 = 1 / ((odds1 / odds2) + 1)
        w2 = 1 / ((odds2 / odds1) + 1)

        return w1, w2

    def determine_arb_profit(self, wager1, odds1, wager2, odds2):
        """
        Checks for arbitrage scenario, then returns tuple of payoffs for event1 and event2 respectively

        :param wager1:
        :param odds1:
        :param wager2:
        :param odds2:
        :return:
        """

        if self.determine_margin_decimal(odds1, odds2) >= 0:
            raise NotArbitrageScenario

        return wager1 * odds1 - (wager1 + wager2), wager2 * odds2 - (wager1 + wager2)

    def crawler(self, game_queue, shutdown_event):
        """
        Crawler that runs on a separate process to find potential games.

        :param game_queue: Queue object for crawler to enqueue potential games for analysis
        :return:
        """

        print("Crawler starting up...")

        try:
            print("Crawler started.")
            while True:
                games_collected = []
                for site in self.sites:
                    logging.debug("ARB_CRAWLER: Site to be checked this round:")
                    logging.debug(site)
                    for url in site.urls:
                        print("Getting games from: {}".format(url))
                        logging.debug("ARB_CRAWLER: Getting games from: {}".format(url))
                        self.driver.get(url)
                        sleep(10)
                        page_source = self.driver.page_source
                        games_from_site = site.parse_page(page_source, url)

                        # determine if games have been collected before
                        for site_game in games_from_site:
                            logging.debug("ARB_CRAWLER: Checking game:")
                            logging.debug(site_game)
                            logging.debug("for a existing match this round")
                            found_in_existing_games = False
                            for existing_game in games_collected:
                                # This if statement checks to see if the team names are similar enough to be considered the same
                                if fuzz.token_set_ratio(site_game.team_1_name, existing_game.team_1_name) >\
                                        self.difference_parameter and \
                                        fuzz.token_set_ratio(site_game.team_2_name, existing_game.team_2_name) >\
                                        self.difference_parameter:
                                    # code to check sequence matcher
                                    print(
                                        " We determined that {} was the same as {} with confidence {} and {} was the same as {} with confidence {}".format(
                                            site_game.team_1_name, existing_game.team_1_name,
                                            fuzz.token_set_ratio(site_game.team_1_name, existing_game.team_1_name),
                                            site_game.team_2_name, existing_game.team_2_name,
                                            fuzz.token_set_ratio(site_game.team_2_name, existing_game.team_2_name)))
                                    logging.debug("ARB_CRAWLER: We determined that {} was the same as {} with confidence {} and {} was the same as {} with confidence {}".format(
                                            site_game.team_1_name, existing_game.team_1_name,
                                            fuzz.token_set_ratio(site_game.team_1_name, existing_game.team_1_name),
                                            site_game.team_2_name, existing_game.team_2_name,
                                            fuzz.token_set_ratio(site_game.team_2_name, existing_game.team_2_name)))

                                    # update existing game
                                    logging.debug("ARB_CRAWLER: Adding odds information to the existing game")
                                    existing_game.site_odds = existing_game.site_odds + site_game.site_odds
                                    found_in_existing_games = True

                            if not found_in_existing_games:
                                games_collected.append(site_game)

                for game in games_collected:
                    if len(game.site_odds) >= 2:
                        print(game)
                        logging.debug("ARB_CRAWLER: Adding game {} vs. {} to game queue".format(game.team_1_name, game.team_2_name))
                        game_queue.put(game)

                print("Completed scraping cycle")

                if shutdown_event.wait(self.interval_minutes * 60): # Wait for time in mins * 60 secs or unless shutdown event happens
                    print("Crawler detected shutdown event.")
                    logging.debug("ARB_CRAWLER: Crawler detected shutdown event.")
                    self.driver.quit()
                    break

        except Exception as e:
            shutdown_event.set()  # Put none in queue to propagate shutdown
            game_queue.put(None)
            self.arb_queue.put(None)
            self.driver.quit()
            traceback.print_exc()
            self.send_error_notification()

        print("Crawler shutting down")

    def game_analyzer(self, game, arb_queue, shutdown_event):
        """
        Worker thread routine for game_watcher to for analysis

        As written it will return true at first arbitrage opportunity found between a pair of SiteOdds with necessary information
        populated in the Game object

        :param game: Game object, the game that is being analyzed
        :param arb_queue: Queue to place Game objects that have been deemed arbitrage opportunities
        :return: None
        """

        # compare odds between all combinations of sites
        for i in game.site_odds:
            for j in game.site_odds:
                # Current version only supports moneyline
                if i is not j:
                    # Determine if one site has best odds for one event and the other site
                    # has best odds for the other event. this is necessary for arbitrage
                    if i.odds1 > j.odds1 and j.odds2 > i.odds2:
                        # Test i.odds1 and j.odds2 for arbitrage opportunity
                        if self.determine_margin_decimal(i.odds1, j.odds2) < 0:
                            # is an opportunity
                            game.arbitrage_opportunity = True
                            game.margin = self.determine_margin_decimal(i.odds1, j.odds2)
                            game.wager_ratio_1, game.wager_ratio_2 = self.determine_wager_ratio(i.odds1, j.odds2)
                            game.arb_site_odds_1 = i
                            game.arb_site_odds_2 = j
                            print("Found arbitrage opportunity")
                            arb_queue.put(game)
                            return
                        if self.determine_margin_decimal(i.odds2, j.odds1) < 0:
                            # is an opportunity
                            game.arbitrage_opportunity = True
                            game.margin = self.determine_margin_decimal(i.odds2, j.odds1)
                            game.wager_ratio_2, game.wager_ratio_1 = self.determine_wager_ratio(i.odds2, j.odds1)
                            game.arb_site_odds_1 = j
                            game.arb_site_odds_2 = i
                            print("Found arbitrage opportunity")
                            arb_queue.put(game)
                            return

    def game_watcher(self, game_queue, arb_queue, shutdown_event):
        """
        Waits for games to enter the queue and then adds them to a threadpool for analysis.

        :param game_queue:
        :return:
        """
        print("Game watcher starting up...")
        try:

            #create a pool of worker threads to help game analyzer
            pool = ThreadPool(processes=5)

            print("Game watcher started.")
            while True:
                found_game = game_queue.get()
                logging.debug("GAME_WATCHER: recieved game in queue")
                if found_game is None:  # Crawler initiated shutdown
                    logging.debug("GAME_WATCHER: Recieved None type game")
                    break
                logging.debug("GAME_WATCHER: Adding game to game analyzer queue")
                pool.apply_async(func=self.game_analyzer, args=(found_game, arb_queue,))

        except Exception as e:
            print("ERROR")
            traceback.print_exc()
            shutdown_event.set()  # Alert Crawler
            arb_queue.put(None)  # Alert game analyzer
            self.send_error_notification()

        pool.close()
        pool.join()

    def send_game_notification(self, game):
        """
        Send an email notification with information pertaining to the list of arb opportunity games
        :param games: List of Game objects
        :return: None
        """
        subject = 'Arbitrage Opportunity'

        mail_body = "The bot has found the following arbitrage opportunity:\n"

        mail_body += "{}\n".format(str(game))

        mail_body += "\nUse this strategy:\n"

        mail_body += "    Wager {} {} at odds {} on {} and {} {} at odds {} on {} for margin {}\n".format(
            game.team_1_name,
            game.wager_ratio_1,
            game.arb_site_odds_1.odds1,
            game.arb_site_odds_1.site.name,
            game.team_2_name,
            game.wager_ratio_2,
            game.arb_site_odds_2.odds2,
            game.arb_site_odds_2.site.name,
            game.margin)

        mail_body += "\nSent from ArbCrawler"

        message = "From: {}\nTo: {}\nSubject: {} {}".format(self.gmail_user, ", ".join(self.email_recipients),
                                                            subject, mail_body)

        try:
            logging.debug("SEND_GAME_NOTIFICATION: Attemting to send an email to {} for arbitrage notification".format(self.email_recipeints))
            server = smtplib.SMTP_SSL(self.mail_server, self.mail_server_port)
            server.ehlo()
            server.login(self.gmail_user, self.gmail_pass)
            server.sendmail(self.gmail_user, self.email_recipients, message)
            server.close()
        except:
            print("There was an error sending an email.")

    def send_error_notification(self):
        """
        Upon unexpected shutdown of ArbCrawler, send notification to email list
        :return: None
        """
        subject = 'ArbCrawler Error: UNEXPECTED SHUTDOWN'

        mail_body = "ArbCrawler bot has unexpectedly shutdown.\n"

        message = "From: {}\nTo: {}\nSubject: {} {}".format(self.gmail_user, ", ".join(self.email_recipients),
                                                            subject, mail_body)

        try:
            logging.debug("SEND_GAME_NOTIFICATION: Sending an email to {} for unexpected shutdown event".format(self.email_recipients))
            server = smtplib.SMTP_SSL(self.mail_server, self.mail_server_port)
            server.ehlo()
            server.login(self.gmail_user, self.gmail_pass)
            server.sendmail(self.gmail_user, self.email_recipients, message)
            server.close()
        except:
            print("There was an error sending an email.")

    def arbitrage_actioner(self, arb_queue, shutdown_event):

        print("Arbitrage actioner starting up...")
        try:
            while True:
                found_arbitrage_opportunity = arb_queue.get()
                logging.debug("ARBITRAGE_ACTIONER: Actioning on found arbitrage opportunity for game {} vs. {}".format(found_arbitrage_opportunity.team_1_name, found_arbitrage_opportunity.team_2_name))
                if found_arbitrage_opportunity is None:
                    break
                print("ARBITRAGE OPPORTUNITY")
                print(found_arbitrage_opportunity)
                print("    Wager {} {} at odds {} on {} and {} {} at odds {} on {} for margin {}".format(found_arbitrage_opportunity.team_1_name,
                                                                                   found_arbitrage_opportunity.wager_ratio_1,
                                                                                   found_arbitrage_opportunity.arb_site_odds_1.odds1,
                                                                                   found_arbitrage_opportunity.arb_site_odds_1.site.name,
                                                                                   found_arbitrage_opportunity.team_2_name,
                                                                                   found_arbitrage_opportunity.wager_ratio_2,
                                                                                   found_arbitrage_opportunity.arb_site_odds_2.odds2,
                                                                                   found_arbitrage_opportunity.arb_site_odds_2.site.name,
                                                                                   found_arbitrage_opportunity.margin))

                # Notify by email
                self.send_game_notification(found_arbitrage_opportunity)
        except Exception as e:
            print("ERROR")
            traceback.print_exc()
            shutdown_event.set()  # Alert Crawler
            self.game_queue.put(None)  # Alert game watcher
            self.send_error_notification()

        print("Actioner shutting down.")

    def sigterm_handler(self, signal, frame):
        # NEEDS FIXING
        print('Shutting down')
        self.game_queue.put(None)
        self.arb_queue.put(None)
        sys.exit(0)

    def main(self):
        print("Starting ArbCrawler...")
        signal.signal(signal.SIGTERM, self.sigterm_handler)

        # Create and start the web crawler process
        print("Creating crawler process...")
        self.crawler_process = Process(target=self.crawler, args=(self.game_queue, self.shutdown_event,))
        self.crawler_process.start()
        print("Done.")

        # Create and start the game analyzer process
        print("Creating game analyzer process...")
        self.game_watcher_process = Process(target=self.game_watcher, args=(self.game_queue, self.arb_queue, self.shutdown_event,))
        self.game_watcher_process.start()
        print("Done.")

        # Create and start the arbitrage actioner process
        print("Creating game analyzer process...")
        self.arbitrage_actioner_process = Process(target=self.arbitrage_actioner, args=(self.arb_queue, self.shutdown_event, ))
        self.arbitrage_actioner_process.start()
        print("Done.")

        print("ArbCrawler started, workers running...")

        # Very basic command line interface
        while True:
            command = input("$ ")
            if command == 'exit':
                print("ArbCrawler shutting down worker processes...")
                self.shutdown_event.set()
                self.game_queue.put(None)
                self.arb_queue.put(None)
                break

        self.crawler_process.join()
        self.game_watcher_process.join()
        self.arbitrage_actioner_process.join()

        print("Exiting ArbCrawler")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Please supply a config file")
        sys.exit()

    arb_crawler = ArbCrawler(sys.argv[1])

    arb_crawler.main()



