import logging
import random
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
from gsmmodem.exceptions import CommandError, TimeoutException, CmsError
from gsmmodem.modem import StatusReport

MAX_RETRY = 10

logger.add("logs/log.log", rotation="00:00")

logger.remove(0)
logger.add(sys.stdout, level=logging.INFO)

logger.info("Start")
logger.info("Read config file")

CONFIG_INI = 'config.ini'

config = ConfigParser()
config.read(CONFIG_INI)
API_HOST = config['default']['api_host']
EXCLUDE_PORTS = config['default']['exclude_ports'].split()
SERVER_NAME = config['default']['server_name']
telegram = NotificationHandler('telegram', defaults={'token': config['default']['telegram_api'],
                                                     'chat_id': config['default']['telegram_id']})
logger.add(telegram, level=logging.ERROR)

sg.ChangeLookAndFeel('Reddit')
window = sg.Window("SMS Deliver")
table = sg.Table([[' ' * 15, ' ' * 18, ' ' * 12, ' ' * 8, ' ' * 8, ' ' * 12, ' ' * 36]], size=(200, 33),
                 max_col_width=100,
                 headings=['Port', 'IMSI', 'Network', 'SMS count', 'Fails', 'Signal', 'Status'],
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
        [sg.Button("Run USSD", key='ussd'), ],
        [sg.Button("Switch SMSC", key='smsc'), ],
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
                    if thread.network_name.lower().startswith('vn'):
                        thread.disconnect()
                network = thread.network_name
                signal = thread.get_signal()
            else:
                network = 'Not connected'
        except:
            network = 'Not connected'
        row_index = thread.get_table_row()
        if thread.sms_count > 45 or thread.sms_fail * 2 > thread.sms_count:
            color = (row_index, 'white', 'red',)
        else:
            color = (row_index, 'black', 'white',)

        return [thread.port, thread.imsi, network, thread.sms_count, thread.sms_fail, signal,
                thread.status], color
    else:
        return [port, "", "", "", "", "Off", "Not connected"], (-1, 'black', 'white',)


pool = ThreadPool(32)


def update_table():
    global all_port
    data = pool.map(get_table_row, all_port)
    rows_data = [x[0] for x in data]
    rows_color = [x[1] for x in data]
    rows = table.SelectedRows
    table.Update(rows_data, select_rows=rows, row_colors=rows_color)


class SMSRunner(threading.Thread):
    def __init__(self, port):
        super().__init__()
        logger.debug(f"Start new thread {port}")
        self.port = port
        self.alive = True
        self.modem: GsmModem = gsmmodem.GsmModem(self.port, smsReceivedCallbackFunc=self.receive_sms,
                                                 # smsStatusReportCallback=self.on_sms_status,
                                                 cpinCallbackFunc=self.on_cpin)
        self.clear_data()
        self.last_check_signal = 0
        self.signal = 'Off'
        self.sms_count = 0
        self.sms_fail = 0

        self.sms_lock = threading.Lock()
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
        self.sms_ref_to_uid = {}

    def reset(self):
        self.set_status('Reseting')
        com = serial.Serial(self.port, 115200, timeout=3)
        com.write(b'AT\r\n')
        com.write(b'AT+CFUN=1,1\r\n')
        com.close()
        time.sleep(0.5)

    def connect(self):
        self.reset()

        while self.alive:
            self.set_status('Connecting')
            try:
                self.modem.connect(waitingForModemToStartInSeconds=20)
            except CommandError as e:
                if e.code == 10:
                    self.set_status(f'No SIM detected')
                elif e.code == 3:
                    self.set_status(f'Command error')
                self.close_modem()

            except SerialException:
                self.set_status('Cannot open this port')
                self.close_modem()
            except TimeoutException:
                self.set_status(f'Timeout open this port')
                self.close_modem()
            except:
                self.close_modem()
            else:
                break

        else:
            if hasattr(self.modem, 'serial') and self.modem.serial.isOpen():
                self.disconnect()
            self.set_status('Die')
            return

        imsi = self.modem.imsi
        while len(imsi) != 15:
            imsi = self.modem.imsi
            time.sleep(0.5)
        self.imsi = imsi
        # self.modem.write('AT+CNMI=3,1,0,2,0')
        # self.modem.write('AT+CPMS="SM","SM","SM"')
        self.set_status(f'Connected')
        self.modem.smsTextMode = True

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
            f'== SMS message received ==\nFrom: {sms.number}\nTime: {sms.time}\nMessage:\n{sms.text}\n')

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

    def on_sms_status(self, report: StatusReport):
        logger.debug(f'''
        ==On status==
        Status: {report.status}, Ref:  {report.reference}, Delivery: {report.deliveryStatus}''')
        if report.reference in self.sms_ref_to_uid:
            uid = self.sms_ref_to_uid[report.reference]
            # if report.deliveryStatus == 0:
            #     pass
            # deliver_status = 'delivered'
            if report.deliveryStatus != 0:
                deliver_status = 'failed'
                logger.error(
                    {'uid': uid, 'status': f'delivery status: {report.deliveryStatus}',
                     'signal': self.modem.signalStrength
                        , 'imsi': self.imsi})
                sio.emit('update_otp', {'uid': uid, 'status': deliver_status}, namespace='/otp')

    @logger.catch
    def run_ussd(self, ussd: str):
        res = self.modem.sendUssd(ussd).message
        logger.info(
            f''''Network: {self.modem.networkName}
        IMSI: {self.imsi}  USSD: {ussd}  Signal: {self.modem.signalStrength}
        Result:
         "{res}"''')

        return res

    def set_smsc(self, smsc):
        logger.info(f"IMSI: {self.modem.imsi} => SMSC: {self.modem.smsc}")
        self.modem.smsc = smsc
        logger.info(f"IMSI: {self.modem.imsi} => SMSC: {self.modem.smsc}")

    def send_sms(self, number, content, uid):
        with self.sms_lock:
            self.modem.smsTextMode = False
            sms = self.modem.sendSms(number, content)
            self.modem.smsTextMode = True
            self.sms_ref_to_uid[sms.reference] = uid
            logger.debug(f"Sent sms ref: {sms.reference}, UID: {uid}")

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

select_lock = threading.Lock()


def send_sms(sms_otp):
    number = sms_otp['number']
    content = sms_otp['content']
    uid = sms_otp['uid']
    network = sms_otp['network']
    selected_runners = [runner for runner in SMSRunner.get_online_runners() if
                        network.lower() == runner.network_name.lower()]
    if len(selected_runners) == 0:
        selected_runners = SMSRunner.get_online_runners()
    if len(selected_runners) == 0:
        data = {'uid': uid, 'status': 'no sim available'}
        logger.error(f"Server:{SERVER_NAME} No sim available ")
        sio.emit('update_otp', data, namespace='/otp')
    else:
        with select_lock:
            random.shuffle(selected_runners)
            best_runner = None
            for runner in selected_runners:
                if best_runner is None or (runner.sms_count + runner.sms_fail) < (
                        best_runner.sms_count + best_runner.sms_fail):
                    best_runner = runner
            best_runner.sms_count += 1
            logger.info(f'Select SIM {best_runner.network_name}, IMSI: {best_runner.imsi}')
        try:
            best_runner.send_sms(number, content, uid)
        except CmsError as e:
            best_runner.sms_fail += 1
            logger.error(f"Send message error code {e.code} Server:{SERVER_NAME} IMSI: {best_runner.imsi}")
            if e.code == 38:
                data = {'uid': uid, 'status': f"SMS error code {e.code}. Server:{SERVER_NAME} IMSI: {best_runner.imsi}",
                        'action': 'stop'}
            else:
                data = {'uid': uid, 'status': f"SMS error code {e.code}. Server:{SERVER_NAME} IMSI: {best_runner.imsi}"}

        except Exception as e1:
            best_runner.sms_fail += 1
            txt = f"Other error code {str(e1)} Server:{SERVER_NAME} IMSI: {best_runner.imsi}"
            logger.error(txt)
            data = {'uid': uid, 'status': txt}
        else:
            data = {'uid': uid, 'status': 'sent'}
        if data:
            logger.info(data)
            sio.emit('update_otp', data, namespace='/otp')


@sio.on('send_sms', namespace='/otp')
def _send_sms(sms_otp):
    if sms_otp['client_id'] == sio.sid:
        logger.info(f"New SMS {str(sms_otp).encode('utf8')}")
        send_sms(sms_otp)


@sio.on('connect', namespace='/otp')
def _connect():
    logger.info(f'Connected to {API_HOST} with ID {sio.sid}')
    sio.emit('update_id', {'name': SERVER_NAME}, namespace='/otp')


@sio.on('disconnect', namespace='/otp')
def _disconnect():
    logger.info(f'Disconnected to {API_HOST} with ID {sio.sid}')


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
    elif btn is 'smsc':
        smsc = sg.PopupGetText('SMSC')
        for index in values['thread_table']:
            selected_port = all_port[index]
            runner = SMSRunner.get_by_port(selected_port)
            if runner:
                threading.Thread(target=runner.set_smsc, args=(smsc,)).start()

    btn, values = window.Read(timeout=2000)
    if btn is None:
        break
    try:
        update_table()
    except TclError as e:
        pass
    except:
        logger.opt(exception=True).error("Table error")
