import datetime
import logging
import re
import sys
import threading
import time
from _tkinter import TclError
from configparser import ConfigParser
from multiprocessing.pool import ThreadPool

import PySimpleGUI as sg
import serial.tools.list_ports
import socketio
from loguru import logger
from notifiers.logging import NotificationHandler
from serial import SerialException

import gsmmodem
from gsmmodem import GsmModem
from gsmmodem.exceptions import CommandError, TimeoutException

MAX_RETRY = 4

logger.add("hire_logs/log.log", rotation="00:00")

logger.remove(0)
logger.add(sys.stdout, level=logging.INFO)

logger.info("Start")
logger.info("Read config file")

CONFIG_INI = 'hire_config.ini'

config = ConfigParser()
config.read(CONFIG_INI)
API_HOST = config['default']['api_host']
SERVER_NAME = config['default']['server_name']
print(API_HOST)
EXCLUDE_PORTS = config['default']['exclude_ports'].split()

telegram = NotificationHandler('telegram', defaults={'token': config['default']['telegram_api'],
                                                     'chat_id': config['default']['telegram_id']})
logger.add(telegram, level=logging.ERROR)

sg.ChangeLookAndFeel('Reddit')
window = sg.Window("SMS Receiver")
table = sg.Table([[' ' * 15, ' ' * 18, ' ' * 12, ' ' * 16, ' ' * 12, ' ' * 36]], size=(200, 33),
                 max_col_width=100,
                 headings=['Port', 'IMSI', 'Network', 'Phone', 'Signal', 'Status'],
                 justification='right', key='thread_table')
window.Layout([[
    sg.Column([
        [table]
    ]), sg.Column([
        [sg.Button("Refresh ports", key='refresh')],
        [sg.T("")],
        [sg.Button('Connect', key='connect', button_color=('white', 'green'))],
        [sg.Button('Connect all', key='connect_all', button_color=('white', 'green'))],
        [sg.Button("Disconnect", key='disconnect', button_color=('white', 'red'))],
        [sg.Button("Restart", key='restart')],
        [sg.Button("Run USSD", key='ussd')]
    ])
]])

window.Finalize()
sio = socketio.Client()
all_port = []


def get_table_row(port):
    thread = SMSRunner.get_by_port(port)
    if thread:
        signal = 'Off'
        try:
            if thread.status == "Connected":
                if thread.network_name == "" or thread.network_name is None:
                    thread.network_name = thread.modem.networkName
                    signal = thread.get_signal(force=True)
                network = thread.network_name
                signal = thread.get_signal()
            else:
                network = 'Not connected'
        except:
            network = 'Not connected'

        return [thread.port, thread.imsi, network, thread.phone_number, signal,
                thread.status]
    else:
        return [port, "", "", "", "Off", "Not connected"]


pool = ThreadPool(32)

prev_sims = []


def update_table():
    global all_port, prev_sims
    data = pool.map(get_table_row, all_port)

    rows = table.SelectedRows
    table.Update(data, select_rows=rows)
    sims = []
    for row in data:
        port, imsi, network, phone_number, signal, status = row
        if status == 'Connected' and len(phone_number) == len('84947431685'):
            sims.append({'imsi': imsi, 'phone': phone_number, 'port': port})
    if prev_sims != sims:
        prev_sims = sims
        if sio.connected:
            sio.emit('update_sim', sims, namespace='/sim')


class SMSRunner(threading.Thread):
    def __init__(self, port):
        super().__init__()
        logger.debug(f"Start new thread {port}")
        self.port = port
        self.alive = True
        self.modem: GsmModem = gsmmodem.GsmModem(self.port, smsReceivedCallbackFunc=self.receive_sms,
                                                 cpinCallbackFunc=self.on_cpin)
        self.clear_data()
        self.last_check_signal = 0
        self.signal = 'Off'
        self.phone_number = "--"
        self.sms_lock = threading.Lock()
        self.status = ""
        self.set_status('Initializing...')

    @staticmethod
    def get_all_runners():
        result = []
        for thread in threading.enumerate():
            if isinstance(thread, SMSRunner):
                result.append(thread)
        return result

    @staticmethod
    def get_online_runners():
        result = []
        for thread in threading.enumerate():
            if isinstance(thread, SMSRunner) and thread.network_name:
                result.append(thread)
        return result

    @staticmethod
    def get_by_port(port):
        for thread in threading.enumerate():
            if isinstance(thread, SMSRunner) and thread.port.lower() == port.lower():
                return thread
        return None

    @staticmethod
    def get_by_imsi(imsi):
        for thread in threading.enumerate():
            if isinstance(thread, SMSRunner) and thread.imsi == imsi:
                return thread
        return None

    def set_status(self, status):
        self.status = status
        # logger.debug(f"{self.port} change status to {status}")

    def run(self):
        self.connect()
        self.first_time = False
        while self.alive:
            time.sleep(0.1)

    def get_table_row(self):
        for index, row in enumerate(table.Values):
            if row[0] == self.port:
                return index
        return -1

    def restart(self):
        try:
            self.modem.close()
        except:
            print('cannot close')
        self.clear_data()
        self.connect()

    def clear_data(self):
        self.network_name = ""
        self.imsi = "Unknown"
        self.first_time = True

    def reset(self):
        self.set_status('Reseting')
        com = serial.Serial(self.port, 115200, timeout=3)
        com.write(b'AT\r\n')
        com.write(b'AT+CFUN=1,1\r\n')
        com.close()
        time.sleep(0.5)

    def connect(self):
        self.reset()

        self.set_status('Connecting')
        while self.alive:
            try:
                self.modem.connect(waitingForModemToStartInSeconds=20)
            except CommandError as e:
                self.close_modem()
                if e.code == 10:
                    self.set_status(f'No SIM detected')
                elif e.code == 3:
                    self.set_status(f'Command error')
            except SerialException:
                self.set_status('Cannot open this port')
                self.close_modem()
            except TimeoutException:
                self.close_modem()
                self.set_status(f'Timeout open this port')
            except:
                self.close_modem()
            else:
                break

        else:
            if hasattr(self.modem, 'serial') and self.modem.serial.isOpen():
                self.disconnect()
            self.set_status('Die')
            return

        while self.alive:
            self.set_status('Try to get number')
            if self.modem.networkName:
                try:
                    self.phone_number = self.get_own_number()
                    if len(self.phone_number) > 8:
                        print('number', self.phone_number)
                        break
                except:
                    self.modem.write('AT+CUSD=2')
            else:
                time.sleep(1)

        imsi = self.modem.imsi
        while len(imsi) != 15:
            imsi = self.modem.imsi
            time.sleep(0.5)
        self.imsi = imsi
        self.modem.write('AT+CNMI=3,1,0,2,0')
        self.modem.write('AT+CPMS="SM","SM","SM"')
        self.set_status(f'Connected')

    def close_modem(self):
        try:
            self.modem.close()
        except:
            pass

    def disconnect(self):
        self.alive = False
        try:
            self.modem.write('AT+CFUN=0')
            self.modem.close()
        except:
            logger.warning(f'Cannot close {self.port}')
        logger.info(f'Kill {self.name} {self.port}')

    def receive_sms(self, sms):
        logger.debug(
            f'== SMS message received ==\nFrom: {sms.number}\nTime: {sms.time}\nMessage:\n{sms.text.encode()}\n')
        data = {
            'imsi': self.imsi, 'phone': self.phone_number, 'time': str(datetime.datetime.now()),
            'text': sms.text,
            'number': sms.number
        }
        if sio.connected:
            sio.emit('save_sms', data, namespace='/sim')

    def on_cpin(self, line):
        logger.info(line)
        if '+CPIN: NOT READY' in line:
            self.imsi = "Unknown"
            self.set_status("No SIM detected")
        elif '+CPIN: READY' in line:
            self.imsi = "SIM inserted"
            self.set_status("Read SIM")
            if not self.first_time:
                self.restart()

    @logger.catch
    def run_ussd(self, ussd: str):
        res = self.modem.sendUssd(ussd).message
        logger.info(
            f''''Network: {self.modem.networkName}
        IMSI: {self.imsi}  USSD: {ussd}  Signal: {self.modem.signalStrength}
        Result:
         "{res}"''')

        return res

    def get_signal(self, force=False):
        if force or time.time() - self.last_check_signal > 10000:
            self.last_check_signal = time.time()
            self.signal = self.modem.signalStrength
            self.network_name = self.modem.networkName
        tail = "Off"
        if 2 <= self.signal < 10:
            tail = "Marginal"
        elif 10 <= self.signal < 15:
            tail = "OK"
        elif 15 <= self.signal < 20:
            tail = "Good"
        elif 20 <= self.signal:
            tail = "Excellent"
        return f"{tail}:{self.signal}"

    def get_own_number(self):
        network_name = self.modem.networkName.lower()
        if 'mobifone' in network_name:
            res = self.run_ussd('*0#')
            phone = '84' + res[2:]
            return phone
        elif 'vinaphone' in network_name:
            res = self.run_ussd('*110#')
            phone = '84' + re.search('(\d{7,})', res)[0]
            return phone
        elif 'viettel' in network_name:
            res = self.run_ussd('*101#')
            phone = '84' + re.search('(\d{7,})', res)[0][2:]
            return phone
        elif 'vietnamobile' in network_name:
            res = self.run_ussd('*101#')
            phone = re.search('(\d{7,})', res)[0]
            return phone
        else:
            print(self.modem.networkName)
            return self.modem.ownNumber


key_pat = re.compile(r"^(\D+)(\d+)$")


def key(item):
    m = key_pat.match(item)
    return m.group(1), int(m.group(2))


def update_all_port():
    global all_port
    all_port = [port.device for port in serial.tools.list_ports.comports() if port.device not in EXCLUDE_PORTS]
    all_port.sort(key=key)
    runners = SMSRunner.get_all_runners()
    for runner in runners:
        if runner.port not in all_port:
            runner.disconnect()
    update_table()


update_all_port()

prev_data = []

time_out = time.time()


@sio.on('connect', namespace='/sim')
def _connect():
    global prev_sims
    logger.info(f'Connected to {API_HOST} with ID {sio.sid}')
    sio.emit('update_server', SERVER_NAME, namespace='/sim')
    prev_sims = []


threading.Thread(target=sio.connect, args=(API_HOST,)).start()
btn = 1
values = {}
while btn is not None:
    if btn == 'refresh':
        update_all_port()
    elif btn == 'connect':
        for index in values['thread_table']:
            selected_port = all_port[index]
            if SMSRunner.get_by_port(selected_port) is None:
                SMSRunner(selected_port).start()
    elif btn == 'connect_all':
        for port in all_port:
            if SMSRunner.get_by_port(port) is None:
                SMSRunner(port).start()
    elif btn == 'disconnect':
        for index in values['thread_table']:
            selected_port = all_port[index]
            runner = SMSRunner.get_by_port(selected_port)
            if runner:
                threading.Thread(target=runner.disconnect).start()
    elif btn == 'restart':
        for index in values['thread_table']:
            selected_port = all_port[index]
            runner = SMSRunner.get_by_port(selected_port)
            if runner:
                threading.Thread(target=runner.restart).start()
    elif btn is 'ussd':
        cmd = sg.PopupGetText('USSD Command')
        for index in values['thread_table']:
            selected_port = all_port[index]
            runner = SMSRunner.get_by_port(selected_port)
            if runner:
                threading.Thread(target=runner.run_ussd, args=(cmd,)).start()
    btn, values = window.Read(timeout=2000)
    if btn is None:
        break
    try:
        update_table()
    except TclError as e:
        pass
    except:
        logger.opt(exception=True).error("Table error")
