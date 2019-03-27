from multiprocessing import Process, Queue
from multiprocessing.pool import ThreadPool
import time
import sys
import toml
import random
from selenium import webdriver
from selenium.webdriver.firefox.options import Options
import re
from bs4 import BeautifulSoup
import traceback
import signal
from difflib import SequenceMatcher

class InvalidOdds(Exception):
    pass

class NotArbitrageScenario(Exception):
    pass

class Site:
    def __init__(self, name, url):
        self.name = name
        self.url = url

class Bovada(Site):
    def __init__(self, name, url):
        Site.__init__(self, name, url)
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

        return found_games


class MyBookie(Site):
    def __init__(self, name, url):
        Site.__init__(self, name, url)
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

            ml1 = tags[i]['data-odds']
            ml2 = tags[i+1]['data-odds']

            new_site_odds = SiteOdds(self, ml1=int(ml1), ml2=int(ml2))

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

        if ml > 0: return (ml + 100)/100
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
        self.game_queue = None
        self.sites = []

        with open(config_file, 'r') as f:
            c = f.read()
            self.config = toml.loads(c)

        #create the site object that arbcrawler will use from the config

        for i in self.config['sites']:
            if i['name'] == 'Bovada':
                new_site = Bovada(name=i['name'], url=i['url'])
            elif i['name'] == 'MyBookie':
                new_site = MyBookie(name=i['name'], url=i['url'])
            else:
                new_site = Site(name=i['name'], url=i['url'])
            self.sites.append(new_site)

        for i in self.sites:
            print(i.name)

        # game queue is a queue for games that the crawler has found
        self.game_queue = Queue()
        # arb_gueue is a queue for games that the analyzer has determined that are arbitrage opportunities
        self.arb_queue = Queue()

        #driver for selenium
        options = Options()
        options.headless = True
        self.driver = webdriver.Firefox(options=options)

        # How similar names have to be to match. Smaller is more lenient, larger is more stringent
        self.difference_parameter = 0.6

    def moneyline_to_decimal(self, ml):
        """ Converts the positive or negative moneyline value into
        decimal odds, essentially the amount of payout including
        initial $1 wager if event happens.

        :param ml: integer moneyline value for event
        :return: float decimal odds
        """

        if ml > 0: return (ml + 100)/100
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

        if self.determine_margin_decimal(odds1, odds2) >= 0: raise NotArbitrageScenario

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

        if self.determine_margin_decimal(odds1, odds2) >= 0: raise NotArbitrageScenario

        return wager1 * odds1 - (wager1 + wager2), wager2 * odds2 - (wager1 + wager2)

    def crawler(self, game_queue):
        """
        Crawler that runs on a separate process to find potential games.

        :param game_queue: Queue object for crawler to enqueue potential games for analysis
        :return:
        """

        print("Crawler starting up...")
        print("Crawler started.")
        while True:

            games_collected =[]
            try:
                for site in self.sites:
                    self.driver.get(site.url)
                    page_source = self.driver.page_source
                    games_from_site = site.parse_page(page_source, site.url)


                    # determine if games have been collected before
                    for site_game in games_from_site:
                        found_in_existing_games = False
                        for existing_game in games_collected:
                            # This if statement checks to see if the team names are similar enough to be considered the same
                            if SequenceMatcher(None, site_game.team_1_name.lower(), existing_game.team_1_name.lower()).ratio() >\
                                    self.difference_parameter and \
                                    SequenceMatcher(None, site_game.team_2_name.lower(), existing_game.team_2_name.lower()).ratio() >\
                                    self.difference_parameter:
                                # code to check sequence matcher
                                print(" We determined that {} was the same as {} with confidence {} and {} was the same as {} with confidence {}".format(site_game.team_1_name, existing_game.team_1_name, SequenceMatcher(None, site_game.team_1_name.lower(), existing_game.team_1_name.lower()).ratio(), site_game.team_2_name, existing_game.team_2_name,  SequenceMatcher(None, site_game.team_2_name.lower(), existing_game.team_2_name.lower()).ratio()))

                                # update existing game
                                existing_game.site_odds = existing_game.site_odds + site_game.site_odds
                                found_in_existing_games = True

                        if not found_in_existing_games:
                            games_collected.append(site_game)


                for game in games_collected:

                    if len(game.site_odds) >= 2:
                        print(game)
                        game_queue.put(game)




                print("Here")
                time.sleep(10)
            except Exception as e:
                traceback.print_exc()
                self.driver.quit()
                game_queue.put(None)
                sys.exit()




        print("Crawler shutting down")


    def game_analyzer(self, game, arb_queue):
        """
        Worker thread routine for game_watcher to for analysis

        As written it will return true at first arbitrage opportunity with necessary information
        populated in the Game object

        :param game: Game object, the game that is being analyzed
        :return:
        """
        # Check to see among SiteOdds if there is an arbitrage play
        time.sleep(1)

        # compare odds between all combinations of sites
        for i in game.site_odds:
            for j in game.site_odds:
                #current version only supports moneyline
                if i is not j:
                    # Determine if one site has best odds for one event and the other site
                    # has best odds for the other event. this is necessary for arbitrage
                    if i.odds1 > j.odds1 and j.odds2 > i.odds2:
                        #test i.odds1 and j.odds2 for arbitrage opportunity
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





    def game_watcher(self, game_queue, arb_queue):
        """
        Waits for games to enter the queue and then adds them to a threadpool for analysis.

        :param game_queue:
        :return:
        """
        print("Game watcher starting up...")

        #create a pool of worker threads to help game analyzer
        pool = ThreadPool(processes=5)


        print("Game watcher started.")
        while True:
            found_game = game_queue.get()
            if found_game is None:
                arb_queue.put(None)
                pool.join()
                sys.exit()

            pool.apply_async(func=self.game_analyzer, args=(found_game, arb_queue,))

        pool.join()

    def arbitrage_actioner(self, arb_queue):


        print("Arbitrage actioner starting up...")

        while True:
            found_arbitrage_opportunity = arb_queue.get()
            if found_arbitrage_opportunity is None:
                sys.exit()
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
            # Do something useful here

    def sigterm_handler(self, signal, frame):
        #NEEDS FIXING
        print('Shutting down')
        self.game_queue.put(None)
        self.arb_queue.put(None)
        sys.exit(0)


    def main(self):
        print("Starting ArbCrawler...")
        signal.signal(signal.SIGTERM, self.sigterm_handler)

        # Create and start the web crawler process
        print("Creating crawler process...")
        self.crawler_process = Process(target=self.crawler, args=(self.game_queue,))
        self.crawler_process.start()

        # Create and start the game analyzer process
        print("Creating game analyzer process...")
        self.game_watcher_process = Process(target=self.game_watcher, args=(self.game_queue, self.arb_queue,))
        self.game_watcher_process.start()

        # Create and start the arbitrage actioner process
        print("Creating game analyzer process...")
        self.arbitrage_actioner_thread = Process(target=self.arbitrage_actioner, args=(self.arb_queue,))
        self.arbitrage_actioner_thread.start()



        print("ArbCrawler started, workers running...")
        self.crawler_process.join()
        self.game_watcher_process.join()
        self.arbitrage_actioner_thread.join()

        print("Exiting ArbCrawler")






if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Please supply a config file")
        sys.exit()

    arb_crawler = ArbCrawler(sys.argv[1])


    arb_crawler.main()


