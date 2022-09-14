from enum import Enum
import os
from time import sleep
import json
import logging
import requests

FORMAT = '[%(levelname)s] [%(module)s:%(lineno)d]: %(message)s'
logging.basicConfig(format=FORMAT)
logger = logging.getLogger('__name__')
logger.setLevel(logging.DEBUG)

from selenium import webdriver


### Screenshoots from grafana: ###

URL_PATH_GRAFANA = str(os.getenv('URL_PATH_GRAFANA'))
URL_ADDRESS_PROMETHEUS = str(os.getenv('URL_ADDRESS_PROMETHEUS'))


def get_current_screenshot(height: int = 950, width: int = 450):
    chrome_options = webdriver.ChromeOptions()
    chrome_options.add_argument('--no-sandbox')
    chrome_options.add_argument('--headless')
    chrome_options.add_argument('--disable-gpu')
    chrome_options.add_argument('--disable-dev-shm-usage')
    chrome_options.add_argument(f"--window-size={width}, {height}")
    chrome_options.add_argument('--force-device-scale-factor=2.0') # Увеличение разрешения скриншота 
    driver = webdriver.Chrome(options=chrome_options)

    # Changing timezone
    tz_params = {'timezoneId': 'Europe/Samara'}
    driver.execute_cdp_cmd('Emulation.setTimezoneOverride', tz_params)
    logger.info('Getting URL...')
    driver.get(URL_PATH_GRAFANA)
    while not driver.execute_script("return document.readyState") == 'complete':
        print(driver.execute_script("return document.readyState"))
        sleep(0.2)
    sleep(2) # 2 Секунды - отрисовка java-графика на Grafana
    # driver.get_screenshot_as_file("/app/screenshot_raw.png")
    screenshot = driver.get_screenshot_as_png()
    driver.quit()
    return screenshot

def write_picture_to_disk(screenshot: bytes):
    with open("/app/screenshot.png", "wb") as f:
        f.write(screenshot)


### Last data from prometheus: ###

class PrometheusScrappingException(Exception):
    """
    Исключение для обработки пустой строки
    'results' при запросе к Prometheus
    """

class LastPollutionState(Enum):
    UNPOLLUTED = 0
    POLLUTED = 1

class Pollution:
    def __init__(self, id, name) -> None:
        self.id = id
        self.name = name
        self.pollution_pdk_percents = 0
        self.last_pollution_state = LastPollutionState.UNPOLLUTED
        self.last_reported_pollution_pdk_percents = 0

    def __str__(self) -> str:
        return f'<{self.__class__} at {hex(id(self))}: {self.id}: {self.name}' \
            f', % от пдк: {self.pollution_pdk_percents}>'
    
    def __repr__(self) -> str:
        return self.__str__()

class PollutionsHandler:
    def __init__(self) -> None:
        self.pollutions = []

    def read_pollutions_names_from_file(self, filename: str) -> None:
        with open(filename) as json_config:
            pollutions = json.load(json_config)
        for id, pol_name in pollutions.items():
            self.pollutions.append(Pollution(int(id), pol_name))
        logger.debug(self.pollutions)

    def update_pollutions_values(self) -> None:
        if len(self.pollutions) == 0:
            raise Exception('First execute read_pollutions_names_from_file!')
        for pollution in self.pollutions:
            pollution.pollution_pdk_percents = self.update_pollution_value_by_id(pollution.id)

    @staticmethod
    def update_pollution_value_by_id(id: int) -> float:
        response = requests.get(f'http://{URL_ADDRESS_PROMETHEUS}/api/v1/query?'
            'query=pollutions{id="%d", data_source="pogoda_sv_rounded"}' % id)
        logger.debug(response.text)
        response = response.json()
        try:
            if len(response['data']['result']) == 0:
                return 0
            value = response['data']['result'][0]['value'][1]
        except IndexError:
            logger.warning('No data from prometheus!')
            raise PrometheusScrappingException
        logger.debug(f'{id}:{value}')
        return value



if __name__ == "__main__":
    picture = get_current_screenshot()
    write_picture_to_disk(picture)

    pol_handler = PollutionsHandler()
    pol_handler.read_pollutions_names_from_file('pollutions_names.json')
    pol_handler.update_pollutions_values()
    logger.info('END')

    