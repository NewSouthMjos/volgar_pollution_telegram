import asyncio
import json
import logging
import os
import signal
import sys
import time
import traceback
from enum import IntEnum

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
INFORM_CHAT_ID = os.getenv('INFORM_CHAT_ID')
BOT_TOKEN = str(os.getenv('BOT_TOKEN'))
VERSION = '1.2.0'


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
        self.last_reported_pollution_pdk_percents = 0
        self.max_report_period_pdk_percents = 0

    def __str__(self) -> str:
        return f'<{self.__class__} at {hex(id(self))}: {self.id}: {self.name}' \
            f', % от пдк: {self.pollution_pdk_percents}>'

    def __repr__(self) -> str:
        return self.__str__()

    def update_max(self) -> None:
        if self.max_report_period_pdk_percents < self.pollution_pdk_percents:
            self.max_report_period_pdk_percents = self.pollution_pdk_percents

    def reset_max(self) -> None:
        self.max_report_period_pdk_percents = 0


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

    def reset_all_max(self) -> None:
        for p in self.pollutions:
            p.reset_max()

    @staticmethod
    def _get_pollution_value_by_id(id: int) -> int:
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
        return round(float(value))


class TelegramHandler:
    def __init__(self) -> None:
        # TODO: доставать токен из .env
        self.bot = telegram.Bot(BOT_TOKEN)
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
            await self.bot.send_photo(chat_id, photo, caption=msg, parse_mode='HTML')


class MessageType(IntEnum):
    NONE = 0
    # UNPOLLUTED = 1
    # POLLUTED = 2
    ALL_CLEAR_NOW = 3
    POLLUTION_APPEAR = 4
    POLLUTION_CONTINUES = 5




class MainHandler:
    def __init__(
            self,
            p_handler: PollutionsHandler,
            tg_handler: TelegramHandler,
    ) -> None:
        self.p_handler = p_handler
        self.tg_handler = tg_handler
        self.exceptions_counter = 0
        self.last_reported_msg_type = MessageType.NONE

    def post_init(self):
        self.p_handler.read_pollutions_names_from_file('pollutions_names.json')

    def is_anything_polluted(self) -> bool:
        return any((p.is_polluted for p in self.p_handler.pollutions))

    def update_pollutions(self) -> None:
        self.p_handler.update_pollutions_values()

    def get_important_pollution_changes(self) -> list[Pollution]:
        """
        Возвращаются объекты Pollution,
        чьи загрязнения превышают ПДК и изменились с
        последней отправки более чем загрязнений более
        чем на 100% ПДК
        """
        p_result = []
        for p in self.p_handler.pollutions:
            if p.pollution_pdk_percents >= 100 \
                    and any((
                        (p.pollution_pdk_percents\
                            - p.last_reported_pollution_pdk_percents > 0),
                        (p.pollution_pdk_percents\
                            - p.last_reported_pollution_pdk_percents < -50)
                        )):
                p_result.append(p)
        return p_result

    def get_all_pollution(self) -> list[Pollution]:
        """
        Возвращаются объекты Pollution,
        превышения ПДК по которым > 100%
        """
        p_result = []
        for p in self.p_handler.pollutions:
            if p.pollution_pdk_percents >= 100:
                p_result.append(p)
        return p_result

    def get_type_message_to_send(self) -> tuple[MessageType, list[Pollution]]:
        if self.last_reported_msg_type in (
                MessageType.ALL_CLEAR_NOW,
                MessageType.NONE):
            p = self.get_all_pollution()
            if len(p) > 0:
                return MessageType.POLLUTION_APPEAR, p
            else:
                return MessageType.NONE, []
        elif self.last_reported_msg_type in (
                MessageType.POLLUTION_APPEAR,
                MessageType.POLLUTION_CONTINUES):
            p = self.get_important_pollution_changes()
            if len(p) > 0:
                return MessageType.POLLUTION_CONTINUES, p
            elif not(self.is_anything_polluted()):
                return MessageType.ALL_CLEAR_NOW, []
            else:
                return MessageType.NONE, []
        
        # Should not be executed if all works fine:
        else:
            return MessageType.NONE, [] 

    def _construct_polluted_part_msg(self, p_list: list[Pollution]) -> str:
        return "".join([
            f'<b>• {p.name}: {p.pollution_pdk_percents} %ПДК</b>\n'
            for p in p_list
        ])

    def _construct_after_polluted_part_msg(self) -> str:
        return "".join([
            f'<i>• {p.name}: {p.max_report_period_pdk_percents} %ПДК</i>\n'
            for p in self.p_handler.pollutions if p.max_report_period_pdk_percents >= 100
        ])

    async def send_all_clear_now_message(self) -> None:
        await self.tg_handler.send_photo(
            get_current_screenshot(),
            ('Значения всех измеряемых веществ вернулось в пределы ПДК.\n'
            'Максимальные значения в последний период загрязнения:\n\n'
            f'{self._construct_after_polluted_part_msg()}'
            )
        )

    async def send_pollution_appear_message(self, p_list: list[Pollution]) -> None:
        for p in p_list:
            p.last_reported_pollution_pdk_percents = p.pollution_pdk_percents
        main_msg = 'Внимание! Зарегистрировано превышение предельной допустимой концентрации по следующим веществам:\n\n'
        pdk_msg_l = self._construct_polluted_part_msg(p_list)
        end_msg = '\nРекомендуется закрыть окна'
        full_msg = f'{main_msg}{pdk_msg_l}{end_msg}'
        await self.tg_handler.send_photo(
            get_current_screenshot(),
            full_msg
        )

    async def send_pollution_continues_message(self, p_list: list[Pollution]) -> None:
        for p in p_list:
            p.last_reported_pollution_pdk_percents = p.pollution_pdk_percents
        main_msg = 'Продолжается превышение предельной допустимой концентрации по следующим веществам:\n\n'
        pdk_msg_l = self._construct_polluted_part_msg(p_list)
        full_msg = f'{main_msg}{pdk_msg_l}'
        await self.tg_handler.send_photo(
            get_current_screenshot(),
            full_msg
        )

    async def send_message_if_necessary(self) -> None:
        logger.info('Updating pollutions...')
        self.update_pollutions()
        logger.info('Updating done')
        msg_t, p_l = self.get_type_message_to_send()
        if msg_t == MessageType.NONE:
            logger.info('No new message is necessary')
            return
        elif msg_t == MessageType.ALL_CLEAR_NOW:
            logger.info('send_all_clear_now_message...')
            await self.send_all_clear_now_message()
            self.last_reported_msg_type = MessageType.ALL_CLEAR_NOW
            logger.info(f'send_all_clear_now_message done')
            self.p_handler.reset_all_max()
        elif msg_t == MessageType.POLLUTION_APPEAR:
            logger.info(f'send_pollution_appear_message for pollutions: \
                {" ".join([p.name for p in p_l])} ...')
            await self.send_pollution_appear_message(p_l)
            self.last_reported_msg_type = MessageType.POLLUTION_APPEAR
            logger.info(f'send_pollution_appear_message done')
            [p.update_max() for p in p_l]
        elif msg_t == MessageType.POLLUTION_CONTINUES:
            logger.info(f'send_pollution_continues_message for pollutions: \
                {" ".join([p.name for p in p_l])} ...')
            await self.send_pollution_continues_message(p_l)
            self.last_reported_msg_type = MessageType.POLLUTION_CONTINUES
            logger.info(f'send_pollution_continues_message done')
            [p.update_max() for p in p_l]

    async def main_job_wrapper(self) -> None:

        # Первый запуск приложения:
        if len(self.p_handler.pollutions) == 0:
            await self.tg_handler.log_bot_info()
            self.post_init()

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
        requests.post(
            f'https://api.telegram.org/bot{BOT_TOKEN}/sendMessage',
            json={
                    "text": f"Started volgar_pollution_telegram version: {VERSION}",
                    "chat_id": INFORM_CHAT_ID
                }
        )
        asyncio.get_event_loop().run_forever()
    except (KeyboardInterrupt, SystemExit):
        logging.warning('Interrupted')
    finally:
        requests.post(
            f'https://api.telegram.org/bot{BOT_TOKEN}/sendMessage',
            json={
                    "text": f"Exited volgar_pollution_telegram version: {VERSION}",
                    "chat_id": INFORM_CHAT_ID
                }
        )
        sys.exit(0)


if __name__ == "__main__":
    main()
    # scr = get_current_screenshot(950, 400)
    # write_picture_to_disk(scr)
