import re
import threading
import time
import traceback
from configparser import ConfigParser
from tkinter import TclError

import PySimpleGUI as sg
import serial.tools.list_ports
import socketio
from loguru import logger
from serial import SerialException

import gsmmodem
from gsmmodem import GsmModem
from gsmmodem.exceptions import CommandError, TimeoutException

CONFIG_INI = 'config.ini'

config = ConfigParser()
config.read(CONFIG_INI)
API_HOST = config['default']['api_host']

sg.ChangeLookAndFeel('Reddit')
window = sg.Window("SMS Deliver")
table = sg.Table([[' ' * 15, ' ' * 18, ' ' * 12, ' ' * 8, ' ' * 48]], size=(200, 24),
                 max_col_width=100,
                 headings=['Port', 'IMSI', 'Network', 'SMS count', 'Status'],
                 justification='right', key='thread_table')
window.Layout([[
    sg.Column([
        [table]
    ]), sg.Column([
        [sg.Button("Refresh ports", key='refresh')],
        [sg.T("")],
        [sg.Button('Connect', key='connect', button_color=('white', 'green'))],
        [sg.Button("Disconnect", key='disconnect', button_color=('white', 'red'))],
        [sg.Button("Restart", key='restart')],
        [sg.Button("Run USSD", key='ussd')]
    ])
]])

window.Finalize()
sio = socketio.Client()
all_port = []


def update_table():
    global all_port
    data = []
    colors = []
    for port in all_port:
        thread = SMSRunner.get_by_port(port)
        if thread:
            try:
                network = thread.modem.networkName
            except:
                network = 'Not connected'
            data.append(
                [thread.port, thread.imsi, network, thread.sms_count,
                 thread.status])
        else:
            data.append(
                [port, "", "", "", "", "Not connected"])
    rows = table.SelectedRows
    table.Update(data, select_rows=rows, row_colors=colors)


class SMSRunner(threading.Thread):
    def __init__(self, port):
        global count
        super().__init__()
        self.port = port
        self.alive = True
        self.modem: GsmModem = gsmmodem.GsmModem(self.port, smsReceivedCallbackFunc=self.receive_sms,
                                                 cpinCallbackFunc=self.on_cpin)
        self.clear_data()
        self.sms_count = 0
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
            if isinstance(thread, SMSRunner) and thread.modem.networkName:
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
        self.number = "Unknown"
        self.imsi = "Unknown"
        self.first_time = True

    def reset(self):
        self.set_status('Reseting...')
        com = serial.Serial(self.port, 115200, timeout=3)
        com.write(b'AT\r\n')
        com.write(b'AT+CFUN=1,1\r\n')
        com.close()
        time.sleep(0.5)
        self.set_status(f'Done reset.')

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

        imsi = self.modem.imsi
        while len(imsi) != 15:
            imsi = self.modem.imsi
            time.sleep(0.5)
        self.imsi = imsi
        self.modem.write('AT+CNMI=3,1,0,2,0')
        self.set_status(f'Connected')

    def close_modem(self):
        try:
            self.modem.close()
        except:
            pass

    def disconnect(self):
        self.number = None
        self.alive = False
        try:
            self.modem.write('AT+CFUN=0')
            self.modem.close()
        except:
            print('cannot close')
        logger.info(f'Kill {self.name} {self.port}')

    def receive_sms(self, sms):
        print(
            f'== SMS message received ==\nFrom: {sms.number}\nTo: {self.number}\nTime: {sms.time}\nMessage:\n{sms.text}\n')
        # data = {
        #     'imsi': self.imsi,
        #     'time': str(datetime.datetime.now()),
        #     'text': sms.text,
        #     'network': self.modem.networkName,
        #     'number': sms.number
        # }
        # r = requests.post(API_HOST, data=json.dumps(data))
        # print(r.text)

    def on_cpin(self, line):
        print(line)
        if '+CPIN: NOT READY' in line:
            self.imsi = "Unknown"
            self.number = "Unknown"
            self.set_status("No SIM detected")
        elif '+CPIN: READY' in line:
            self.imsi = "SIM inserted"
            self.set_status("Read SIM")
            if not self.first_time:
                self.restart()

    def run_ussd(self, ussd: str):
        res = self.modem.sendUssd(ussd).message
        logger.info(
            f'Network: {self.modem.networkName}, IMSI: {self.imsi}, Phone: {self.number},  USSD: {ussd},\n Result: "{res}"')
        return res

    def send_sms(self, number, content):
        with self.sms_lock:
            self.sms_count += 1
            sms = self.modem.sendSms(number, content, True)
        return str(sms.status)


key_pat = re.compile(r"^(\D+)(\d+)$")


def key(item):
    m = key_pat.match(item)
    return m.group(1), int(m.group(2))


def update_all_port():
    global all_port
    all_port = [port.device for port in serial.tools.list_ports.comports()]
    all_port.sort(key=key)
    runners = SMSRunner.get_all_runners()
    for runner in runners:
        if runner.port not in all_port:
            runner.disconnect()
    update_table()


update_all_port()

prev_data = []

time_out = time.time()


@sio.on('connect', namespace='/otp')
def _connect():
    print('on connect')
    print(sio.sid)


def get_network(number: str):
    PHONES = dict(
        vinaphone=['088', '088', '091', '091', '094', '094', '0123', '083', '0124', '084', '0125', '085', '0127', '081',
                   '0129', '082'],
        viettel=['086', '086', '096', '096', '097', '097', '098', '098', '0162', '032', '0163', '033', '0164', '034',
                 '0165', '035', '0166', '036', '0167', '037', '0168', '038', '0169', '039'],
        mobifone=['089', '089', '090', '090', '093', '093', '0120', '070', '0121', '079', '0122', '077', '0126', '076',
                  '0128', '078'],
        vietnamobile=['092', '092', '056', '056', '058', '058']
    )
    if number.startswith('84'):
        number = '0' + number[2:]
    for k in PHONES.keys():
        starts = PHONES[k]
        for start in starts:
            if number.startswith(start):
                return k
    return ''


def send_sms(sms_otp):
    number = sms_otp['number']
    content = sms_otp['content']
    uid = sms_otp['uid']
    network = sms_otp['network']
    selected_runners = [runner for runner in SMSRunner.get_online_runners() if
                        runner.modem.networkName.lower() == network]
    if len(selected_runners) == 0:
        selected_runners = SMSRunner.get_online_runners()
    if len(selected_runners) == 0:
        sio.emit('update_otp', {'uid': uid, 'status': 'No sim available'}, namespace='/otp')
    else:
        best_runner: SMSRunner = None
        for runner in selected_runners:
            if best_runner is None or runner.sms_count < best_runner.sms_count:
                best_runner = runner
        print(f'Select SIM {best_runner.modem.networkName}')
        try:
            result = best_runner.send_sms(number, content)
            sio.emit('update_otp', {'uid': uid, 'status': result}, namespace='/otp')
        except TimeoutException:
            sio.emit('update_otp', {'uid': uid, 'status': 'timeout'}, namespace='/otp')


@sio.on('send_sms', namespace='/otp')
def _send_sms(sms_otp):
    print('new sms')
    print(sms_otp)
    send_sms(sms_otp)


threading.Thread(target=sio.connect, args=(API_HOST,)).start()

btn = 1
while btn is not None:
    btn, values = window.Read(timeout=2000)
    if btn == 'refresh':
        update_all_port()
    elif btn == 'connect':
        for index in values['thread_table']:
            selected_port = all_port[index]
            if SMSRunner.get_by_port(selected_port) is None:
                SMSRunner(selected_port).start()
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
                try:
                    threading.Thread(target=runner.run_ussd, args=(cmd,)).start()
                except:
                    traceback.print_exc()
    try:
        update_table()
    except TclError:
        pass
