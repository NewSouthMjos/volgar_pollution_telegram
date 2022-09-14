import asyncio
import json
import logging
import os
import signal
import sys
import time
import traceback
from collections import deque

import requests
import telegram
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from selenium import webdriver

FORMAT = '[%(levelname)s] [%(module)s:%(lineno)d]: %(message)s'
logging.basicConfig(format=FORMAT)
logger = logging.getLogger('__name__')
logger.setLevel(str(os.getenv('LOG_LEVEL', 'INFO')).upper())


### Screenshoots from grafana: ###

URL_PATH_GRAFANA = str(os.getenv('URL_PATH_GRAFANA'))
URL_ADDRESS_PROMETHEUS = str(os.getenv('URL_ADDRESS_PROMETHEUS'))
CRON_MINUTE = str(os.getenv('CRON_MINUTE', '*/3'))
VERSION = '1.0.1'


def get_current_screenshot(height: int = 950, width: int = 500):
    chrome_options = webdriver.ChromeOptions()
    chrome_options.add_argument('--no-sandbox')
    chrome_options.add_argument('--headless')
    chrome_options.add_argument('--disable-gpu')
    chrome_options.add_argument('--disable-dev-shm-usage')
    chrome_options.add_argument(f"--window-size={width}, {height}")
    # Увеличение разрешения скриншота
    chrome_options.add_argument('--force-device-scale-factor=2.0')
    driver = webdriver.Chrome(options=chrome_options)

    # Changing timezone
    tz_params = {'timezoneId': 'Europe/Samara'}
    driver.execute_cdp_cmd('Emulation.setTimezoneOverride', tz_params)
    logger.info('Getting URL...')
    driver.get(URL_PATH_GRAFANA)
    while not driver.execute_script("return document.readyState") == 'complete':
        print(driver.execute_script("return document.readyState"))
        time.sleep(0.2)
    time.sleep(2)  # 2 Секунды - отрисовка java-графика на Grafana
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


class Pollution:
    def __init__(self, id, name) -> None:
        self.id = id
        self.name = name
        self.pollution_pdk_percents = 0
        self.is_polluted = False
        self.last_reported_pollution_pdk_percents = None

    def __str__(self) -> str:
        return f'<{self.__class__} at {hex(id(self))}: {self.id}: {self.name}' \
            f', % от пдк: {self.pollution_pdk_percents}>'

    def __repr__(self) -> str:
        return self.__str__()


class PollutionsHandler:
    def __init__(self) -> None:
        self.pollutions: list[Pollution] = []

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
            pollution.pollution_pdk_percents = self._get_pollution_value_by_id(
                pollution.id)
            if pollution.pollution_pdk_percents >= 100:
                pollution.is_polluted = True
            else:
                pollution.is_polluted = False

    @staticmethod
    def _get_pollution_value_by_id(id: int) -> float:
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
        return int(value)


class TelegramHandler:
    def __init__(self) -> None:
        # TODO: доставать токен из .env
        self.bot = telegram.Bot(os.getenv('BOT_TOKEN'))
        self.chat_id = os.getenv("TARGET_CHAT_NAME")

    async def log_bot_info(self) -> None:
        async with self.bot:
            logger.info(await self.bot.get_me())

    async def send_text_message(self, msg, chat_id=None) -> None:
        if chat_id is None:
            chat_id = self.chat_id
        async with self.bot:
            await self.bot.send_message(chat_id, msg)

    async def send_photo(self, photo: bytes, msg: str, chat_id=None) -> None:
        if chat_id is None:
            chat_id = self.chat_id
        async with self.bot:
            await self.bot.send_photo(chat_id, photo, caption=msg, parse_mode='MarkdownV2')


class MainHandler:
    def __init__(
            self,
            p_handler: PollutionsHandler,
            tg_handler: TelegramHandler,
    ) -> None:
        self.p_handler = p_handler
        self.tg_handler = tg_handler
        self.is_anything_polluted_deque = deque(maxlen=2)
        self.exceptions_counter = 0

    def post_init(self):
        self.p_handler.read_pollutions_names_from_file('pollutions_names.json')

    def is_anything_polluted(self) -> bool:
        return any((p.is_polluted for p in self.p_handler.pollutions))

    def update_pollutions(self) -> None:
        self.p_handler.update_pollutions_values()
        self.is_anything_polluted_deque.append(
            self.is_anything_polluted()
        )

    def get_important_pollution_changes(self) -> list[Pollution]:
        """
        Возвращаются объекты Pollution,
        чьи загрязнения превышают ПДК и изменились с
        последней отправки более чем загрязнений более
        чем на 100% ПДК
        """
        p_result = []
        for p in self.p_handler.pollutions:
            if p.last_reported_pollution_pdk_percents is not None:
                if p.pollution_pdk_percents > 100 \
                        and abs(p.pollution_pdk_percents - p.last_reported_pollution_pdk_percents) > 100:
                    p_result.append(p)
            else:
                if p.pollution_pdk_percents > 100:
                    p_result.append(p)
        return p_result

    def is_new_message_necessary(self) -> bool:
        if len(self.is_anything_polluted_deque) == 1:
            if len(self.get_important_pollution_changes()) > 0:
                return True
            else:
                return False
        if self.is_anything_polluted_deque[-2] != self.is_anything_polluted_deque[-1]:
            return True
        if len(self.get_important_pollution_changes()) > 0:
            return True
        return False

    async def send_unpolluted_message(self) -> None:
        logger.info('Sending unpolluted message...')
        await self.tg_handler.send_photo(
            get_current_screenshot(),
            'Значения всех измеряемых веществ находятся в пределах ПДК'
        )
        logger.info(f'Sending unpolluted message done')

    async def send_polluted_message(self, p_list: list[Pollution]) -> None:
        logger.info(f'Sending polluted message for pollutions: {" ".join([p.name for p in p_list])} ...')
        for p in p_list:
            p.last_reported_pollution_pdk_percents = p.pollution_pdk_percents
        main_msg = 'Превышение предельной допустимой концентрации по следующим веществам:\n'
        pdk_msg_l = [f'**\- {p.name}: {p.pollution_pdk_percents} %ПДК**\n' for p in p_list] 
        end_msg = 'Рекомендуется закрыть окна.'
        full_msg = f'{main_msg}{"".join(pdk_msg_l)}{end_msg}'
        await self.tg_handler.send_photo(
            get_current_screenshot(),
            full_msg
        )
        logger.info(f'Sending polluted message done')

    async def send_message_if_necessary(self) -> None:
        logger.info('Updating pollutions...')
        self.update_pollutions()
        logger.info('Updating done')
        if not(self.is_new_message_necessary()):
            logger.info('No new message is necessary')
            return
        p_to_report = self.get_important_pollution_changes()
        if len(p_to_report) == 0:
            await self.send_unpolluted_message()
        else:
            await self.send_polluted_message(p_to_report)

    async def main_job_wrapper(self) -> None:

        # Первый запуск приложения:
        if len(self.p_handler.pollutions) == 0:
            await self.tg_handler.log_bot_info()
            self.post_init()
            await self.tg_handler.send_text_message(
                f'Started volgar_pollution_telegram version: {VERSION}',
                chat_id=os.getenv('INFORM_CHAT_ID')
                )

        try:
            await self.send_message_if_necessary()
            self.exceptions_counter = 0
        except Exception as err:
            self.exceptions_counter += 1
            logger.exception(err)
            exc_str = traceback.format_exc()
            await self.tg_handler.send_text_message(
                f'Exception occurs! version: {VERSION} Info: \n{exc_str}',
                chat_id=os.getenv('INFORM_CHAT_ID')
                )
            if self.exceptions_counter >= 5:
                await self.tg_handler.send_text_message(
                f'Too many exceptions. Terminating program',
                chat_id=os.getenv('INFORM_CHAT_ID')
                )
                os._exit(1)


def handle_sigterm(*args):
    raise KeyboardInterrupt()


def main():
    signal.signal(signal.SIGTERM, handle_sigterm)  # For catch sig in docker

    logger.info(f'Starting volgar_pollution_telegram version: {VERSION}')
    tg_handler = TelegramHandler()
    pol_handler = PollutionsHandler()
    main_handler = MainHandler(pol_handler, tg_handler)
    
    scheduler = AsyncIOScheduler()
    
    scheduler.add_job(main_handler.main_job_wrapper, 'cron', minute=CRON_MINUTE)
    scheduler.start()
    logger.info('Started successfully')
    try:
        asyncio.get_event_loop().run_forever()
    except (KeyboardInterrupt, SystemExit):
        logging.warning('Interrupted')
    finally:
        sys.exit(0)


if __name__ == "__main__":
    main()
    # scr = get_current_screenshot(950, 500)
    # write_picture_to_disk(scr)