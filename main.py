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
logging.basicConfig(filename='connections.log', level=logging.DEBUG)


def setup_logger(name, log_file, formatter, level=logging.DEBUG):
    handler = logging.FileHandler(log_file)
    handler.setFormatter(formatter)

    logger = logging.getLogger(name)
    logger.setLevel(level)

    logger.addHandler(handler)

    return logger



class InvalidOdds(Exception):
    pass


class NotArbitrageScenario(Exception):
    pass


class Site:
    def __init__(self, name, urls):
        self.name = name
        self.urls = urls

    def __repr__(self):
        return "Site name: {} urls: {}\n".format(self.name, str(self.urls))

class Bovada(Site):
    def __init__(self, name, urls):

        # Setup Logger
        self.log_name = "bovada_log"
        self.log_file = "bovada.log"
        self.formatter = logging.Formatter("%(levelname)s BOVADA: %(message)s")

        self.logger = setup_logger(self.log_name, self.log_file, self.formatter)

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

        self.logger.debug("PARSE_ALL_SPORTS_PAGE: Bovada found games this run:")
        self.logger.debug(found_games)
        return found_games


class MyBookie(Site):
    def __init__(self, name, urls):
        self.log_name = "mybookie_log"
        self.log_file = "mybookie.log"
        self.formatter = logging.Formatter("%(levelname)s MYBOOKIE: %(message)s")

        self.logger = setup_logger(self.log_name, self.log_file, self.formatter)

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
        game_html_blocks = soup.find_all("div", {"class":"row m-0 mobile sportsbook-lines mb-2 border"})


        for i in range(0, len(game_html_blocks)):
            team_a = game_html_blocks[i].find_all("div", {"class":"team-lines"})[0].find_all("a")[0].get_text()
            team_b = game_html_blocks[i].find_all("div", {"class":"team-lines"})[0].find_all("a")[1].get_text()
            self.logger.debug("PARSE_SPORTSBOOK_PAGE: Parsed game {} vs. {}".format(team_a, team_b))
            self.logger.debug("PARSE_SPORTSBOOK_PAGE: {}".format(team_a))
            self.logger.debug("PARSE_SPORTSBOOK_PAGE: {}". format(team_b))


            # Special Case: ignore any first half bets, first two characters will be "1H"
            # TODO: make this better so that 1H bets become own game
            if team_a[:2] == "1H" or team_b[:2] == "1H":
                continue

            ml1_text_data = game_html_blocks[i].find_all("div", {"class": "spread-lines"})[0].find_all("button")[0].get_text()
            ml2_text_data = game_html_blocks[i].find_all("div", {"class": "spread-lines"})[0].find_all("button")[1].get_text()



            # Skip this game if there aren't odds for both outcomes
            # TODO: make this better
            if ml1_text_data == "":
                continue
            else:
                ml1_text = re.search('\((.+?)\)', ml1_text_data)
                if ml1_text is None:
                    # There is no spread data for this game
                    continue
                else:
                    ml1 = int(ml1_text.group(1))

            if ml2_text_data == "":
                continue
            else:
                ml2_text = re.search('\((.+?)\)', ml2_text_data)
                if ml2_text is None:
                    # There is no spread data for this game
                    continue
                else:
                    ml2 = int(ml2_text.group(1))

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

    def __init__(self, config_file, email_config_file):
        self.log_name = "arb_crawler.log"
        self.log_file = "arb_crawler.log"
        self.formatter = logging.Formatter("%(levelname)s ARB_CRAWLER: %(message)s")

        self.logger = setup_logger(self.log_name, self.log_file, self.formatter)

        self.crawler_process = None
        self.arbitrage_actioner_process = None
        self.game_watcher_process = None
        self.game_queue = None

        self.shutdown_event = Event()

        self.sites = []

        with open(config_file, 'r') as f:
            c = f.read()
            self.config = toml.loads(c)

        with open(email_config_file, 'r') as f:
            c = f.read()
            self.email_config = toml.loads(c)

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
                self.logger.debug("INIT: Adding from config : {}".format(str(i)))

        # game queue is a queue for games that the crawler has found
        self.game_queue = Queue()
        # arb_gueue is a queue for games that the analyzer has determined that are arbitrage opportunities
        self.arb_queue = Queue()

        # Driver for selenium
        options = Options()
        options.headless = True
        self.driver = webdriver.Firefox(options=options)

        # How similar names have to be to match. Smaller is more lenient, larger is more stringent
        self.difference_parameter = 50

        self.mail_server = self.email_config['email_server']
        self.mail_server_port = self.email_config['email_port']
        self.gmail_user = self.email_config['email_user']
        self.gmail_pass = self.email_config['email_password']
        self.email_recipients = self.email_config['recipients']

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

        self.logger.debug("CRAWLER: Crawler starting up...")

        try:
            self.logger.debug("CRAWLER: Crawler started.")
            while True:
                games_collected = []
                for site in self.sites:
                    self.logger.debug("CRAWLER: Site to be checked this round: {}".format(str(site)))
                    for url in site.urls:
                        self.logger.debug("CRAWLER: Getting games from: {}".format(url))
                        self.driver.get(url)
                        sleep(10)
                        page_source = self.driver.page_source
                        games_from_site = site.parse_page(page_source, url)

                        # determine if games have been collected before
                        for site_game in games_from_site:
                            self.logger.debug("CRAWLER: Checking game: {} for an existing match this round".format(str(site_game)))
                            found_in_existing_games = False
                            for existing_game in games_collected:
                                # This if statement checks to see if the team names are similar enough to be considered the same
                                if fuzz.token_set_ratio(site_game.team_1_name, existing_game.team_1_name) >\
                                        self.difference_parameter and \
                                        fuzz.token_set_ratio(site_game.team_2_name, existing_game.team_2_name) >\
                                        self.difference_parameter:
                                    # code to check sequence matcher
                                    self.logger.debug("CRAWLER: We determined that {} was the same as {} with confidence {} and {} was the same as {} with confidence {}".format(
                                            site_game.team_1_name, existing_game.team_1_name,
                                            fuzz.token_set_ratio(site_game.team_1_name, existing_game.team_1_name),
                                            site_game.team_2_name, existing_game.team_2_name,
                                            fuzz.token_set_ratio(site_game.team_2_name, existing_game.team_2_name)))

                                    # update existing game
                                    self.logger.debug("Adding odds information to the existing game")
                                    existing_game.site_odds = existing_game.site_odds + site_game.site_odds
                                    found_in_existing_games = True

                            if not found_in_existing_games:
                                games_collected.append(site_game)

                for game in games_collected:
                    if len(game.site_odds) >= 2:
                        self.logger.debug("CRAWLER: Adding game {} to game queue".format(str(game)))
                        game_queue.put(game)

                self.logger.debug("CRAWLER: Completed scraping cycle")

                if shutdown_event.wait(self.interval_minutes * 60): # Wait for time in mins * 60 secs or unless shutdown event happens
                    self.logger.debug("Crawler detected shutdown event.")
                    self.driver.quit()
                    break

        except Exception as e:
            shutdown_event.set()  # Put none in queue to propagate shutdown
            game_queue.put(None)
            self.arb_queue.put(None)
            self.driver.quit()
            traceback.print_exc()
            self.send_error_notification()

        self.logger.debug("CRAWLER: Crawler shutting down")

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
                            self.logger.debug("GAME_ANALYZER: Found arbitrage opportunity")
                            arb_queue.put(game)
                            return
                        if self.determine_margin_decimal(i.odds2, j.odds1) < 0:
                            # is an opportunity
                            game.arbitrage_opportunity = True
                            game.margin = self.determine_margin_decimal(i.odds2, j.odds1)
                            game.wager_ratio_2, game.wager_ratio_1 = self.determine_wager_ratio(i.odds2, j.odds1)
                            game.arb_site_odds_1 = j
                            game.arb_site_odds_2 = i
                            self.logger.debug("GAME_ANALYZER: Found arbitrage opportunity")
                            arb_queue.put(game)
                            return

    def game_watcher(self, game_queue, arb_queue, shutdown_event):
        """
        Waits for games to enter the queue and then adds them to a threadpool for analysis.

        :param game_queue:
        :return:
        """
        self.logger.debug("GAME_WATCHER: Game watcher starting up...")
        try:

            #create a pool of worker threads to help game analyzer
            pool = ThreadPool(processes=5)

            self.logger.debug("GAME_WATCHER: Game watcher started.")
            while True:
                found_game = game_queue.get()
                self.logger.debug("GAME_WATCHER: recieved game in queue")
                if found_game is None:  # Crawler initiated shutdown
                    self.logger.debug("GAME_WATCHER: Recieved None type game")
                    break
                self.logger.debug("GAME_WATCHER: Adding game to game analyzer queue")
                pool.apply_async(func=self.game_analyzer, args=(found_game, arb_queue,))

        except Exception as e:
            self.logger.debug("GAME_WATCHER: ERROR in applying game to analysis queue")
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
            self.logger.debug("SEND_GAME_NOTIFICATION: Attemting to send an email to {} for arbitrage notification".format(self.email_recipeints))
            server = smtplib.SMTP_SSL(self.mail_server, self.mail_server_port)
            server.ehlo()
            server.login(self.gmail_user, self.gmail_pass)
            server.sendmail(self.gmail_user, self.email_recipients, message)
            server.close()
        except :
            self.logger.debug("SEND_GAME_NOTIFICATION: ERROR There was an error sending an email {}". format(sys.exc_info()[0]))

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
            self.logger.debug("SEND_GAME_NOTIFICATION: Sending an email to {} for unexpected shutdown event".format(self.email_recipients))
            server = smtplib.SMTP_SSL(self.mail_server, self.mail_server_port)
            server.ehlo()
            server.login(self.gmail_user, self.gmail_pass)
            server.sendmail(self.gmail_user, self.email_recipients, message)
            server.close()
        except:
            self.logger.debug("SEND_ERROR_NOTIFICATION: ERROR There was an error sending an email.")

    def arbitrage_actioner(self, arb_queue, shutdown_event):

        self.logger.debug("ARBITRAGE_ACTIONER: Arbitrage actioner starting up...")
        try:
            while True:
                found_arbitrage_opportunity = arb_queue.get()
                self.logger.debug("ARBITRAGE_ACTIONER: Actioning on found arbitrage opportunity for game {} vs. {}".format(found_arbitrage_opportunity.team_1_name, found_arbitrage_opportunity.team_2_name))
                if found_arbitrage_opportunity is None:
                    break
                    self.logger.debug("ARBITRAGE_ACTIONER: ARBITRAGE OPPORTUNITY")
                    self.logger.debug(found_arbitrage_opportunity)
                    self.logger.debug("    Wager {} {} at odds {} on {} and {} {} at odds {} on {} for margin {}".format(found_arbitrage_opportunity.team_1_name,
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
            self.logger.debug("ARBITRAGE_ACTIONER: ERROR Error while actioning arbitrage opportunity")
            traceback.print_exc()
            shutdown_event.set()  # Alert Crawler
            self.game_queue.put(None)  # Alert game watcher
            self.send_error_notification()

            self.logger.debug("ARBITRAGE_ACTIONER: Actioner shutting down.")

    def sigterm_handler(self, signal, frame):
        # NEEDS FIXING
        self.logger.debug('SIGTERM_HANDLER: Shutting down')
        self.game_queue.put(None)
        self.arb_queue.put(None)
        sys.exit(0)

    def main(self):
        self.logger.debug("MAIN: Starting ArbCrawler...")
        signal.signal(signal.SIGTERM, self.sigterm_handler)

        # Create and start the web crawler process
        self.logger.debug("MAIN: Creating crawler process...")
        self.crawler_process = Process(target=self.crawler, args=(self.game_queue, self.shutdown_event,))
        self.crawler_process.start()
        self.logger.debug("MAIN: Done.")

        # Create and start the game analyzer process
        self.logger.debug("MAIN: Creating game analyzer process...")
        self.game_watcher_process = Process(target=self.game_watcher, args=(self.game_queue, self.arb_queue, self.shutdown_event,))
        self.game_watcher_process.start()
        self.logger.debug("MAIN: Done.")

        # Create and start the arbitrage actioner process
        self.logger.debug("MAIN: Creating game analyzer process...")
        self.arbitrage_actioner_process = Process(target=self.arbitrage_actioner, args=(self.arb_queue, self.shutdown_event, ))
        self.arbitrage_actioner_process.start()
        self.logger.debug("MAIN: Done.")

        self.logger.debug("MAIN: ArbCrawler started, workers running...")

        # Very basic command line interface
        while True:
            command = input("$ ")
            if command == 'exit':
                self.logger.debug("MAIN: ArbCrawler shutting down worker processes...")
                self.shutdown_event.set()
                self.game_queue.put(None)
                self.arb_queue.put(None)
                break

        self.crawler_process.join()
        self.game_watcher_process.join()
        self.arbitrage_actioner_process.join()

        self.logger.debug("MAIN: Exiting ArbCrawler")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Please supply a config file and email config file")
        sys.exit()

    arb_crawler = ArbCrawler(sys.argv[1], sys.argv[2])

    arb_crawler.main()



