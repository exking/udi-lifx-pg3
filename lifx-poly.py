#!/usr/bin/env python3
"""
LiFX NodeServer for UDI Polyglot v2
by Einstein.42 (James Milne) milne.james@gmail.com
"""

import udi_interface
import time
import sys
import lifxlan
from copy import deepcopy
import json
import yaml
from threading import Thread
from pathlib import Path
import math


LOGGER = udi_interface.LOGGER
Custom = udi_interface.Custom
BR_INCREMENT = 2620    # this is ~4% of 65535
BR_MIN = 1310          # minimum brightness value ~2%
BR_MAX = 65535         # maximum brightness value
FADE_INTERVAL = 5000   # 5s
BRTDIM_INTERVAL = 400  # 400ms

with open('server.json') as data:
    SERVERDATA = json.load(data)
    data.close()
try:
    VERSION = SERVERDATA['credits'][0]['version']
except (KeyError, ValueError):
    LOGGER.info('Version not found in server.json.')
    VERSION = '0.0.0'


# Changing these will not update the ISY names and labels, you will have to edit the profile.
COLORS = {
    0: ['RED', [62978, 65535, 65535, 3500]],
    1: ['ORANGE', [5525, 65535, 65535, 3500]],
    2: ['YELLOW', [7615, 65535, 65535, 3500]],
    3: ['GREEN', [16173, 65535, 65535, 3500]],
    4: ['CYAN', [29814, 65535, 65535, 3500]],
    5: ['BLUE', [43634, 65535, 65535, 3500]],
    6: ['PURPLE', [50486, 65535, 65535, 3500]],
    7: ['PINK', [58275, 65535, 47142, 3500]],
    8: ['WHITE', [58275, 0, 65535, 5500]],
    9: ['COLD_WHTE', [58275, 0, 65535, 9000]],
    10: ['WARM_WHITE', [58275, 0, 65535, 3200]],
    11: ['GOLD', [58275, 0, 65535, 2500]]
}


class Controller(udi_interface.Node):
    def __init__(self, polyglot, primary, address, name):
        super().__init__(polyglot, primary, address, name)
        self.parameters = Custom(polyglot, 'customparams')
        self.cust_data = Custom(polyglot, 'customdata')
        self.lifxLan = None
        self.name = 'LiFX Controller'
        self.discovery_thread = None
        self.update_nodes = False
        self.change_pon = True
        self.ignore_second_on = False
        self.bulbs_found = 0
        self.poly.subscribe(polyglot.START, self.start, address)
        self.poly.subscribe(polyglot.CUSTOMPARAMS, self.parameter_handler)
        self.poly.subscribe(polyglot.CUSTOMDATA, self.data_handler)
        self.poly.subscribe(polyglot.POLL, self.poll)
        self.poly.subscribe(polyglot.STOP, self.stop)
        self.poly.ready()
        self.poly.addNode(self)

    def start(self):
        LOGGER.info('Starting LiFX Polyglot v2 NodeServer version {}, LiFX LAN: {}'.format(VERSION, lifxlan.__version__))
        polyglot.updateProfile()
        self.poly.setCustomParamsDoc()

    def parameter_handler(self, params):
        self.parameters.load(params)
        self.poly.Notices.clear()
        if self.parameters['change_no_pon']:
            LOGGER.debug('Change of color won\'t power bulbs on')
            self.change_pon = False
        if self.parameters['ignore_second_on']:
            LOGGER.debug('DON will be ignored if already on')
            self.ignore_second_on = True
        self.discover()
        LOGGER.debug('Start complete')

    def data_handler(self, data):
        self.cust_data.load(data)

    def stop(self):
        LOGGER.info('Stopping LiFX Polyglot v2 NodeServer version {}'.format(VERSION))

    def poll(self, polltype):
        if self.discovery_thread is not None:
            if self.discovery_thread.is_alive():
                LOGGER.debug('Skipping poll() while discovery in progress...')
                return
            else:
                self.discovery_thread = None
        for node in self.poly.getNodes().values():
            if polltype == 'shortPoll':
                node.update()
            else:
                node.long_update()

    def update(self):
        pass

    def long_update(self):
        pass

    def discover(self, command=None):
        self.lifxLan = lifxlan.LifxLAN()
        if self.discovery_thread is not None:
            if self.discovery_thread.is_alive():
                LOGGER.info('Discovery is still in progress')
                return
        self.discovery_thread = Thread(target=self._discovery_process)
        self.discovery_thread.start()

    def _manual_discovery(self):
        try:
            f = open(self.parameters['devlist'])
        except Exception as ex:
            LOGGER.error('Failed to open {}: {}'.format(self.paramaters['devlist'], ex))
            return False
        try:
            data = yaml.safe_load(f.read())
            f.close()
        except Exception as ex:
            LOGGER.error('Failed to parse {} content: {}'.format(self.parameters['devlist'], ex))
            return False

        if 'bulbs' not in data:
            LOGGER.error('Manual discovery file {} is missing bulbs section'.format(self.parameters['devlist']))
            return False

        for b in data['bulbs']:
            name = b['name']
            address = b['mac'].replace(':', '').lower()
            mac = b['mac']
            ip = b['ip']
            if not self.poly.getNode(address):
                self.bulbs_found += 1
                if b['type'] == 'multizone':
                    d = lifxlan.MultiZoneLight(mac, ip)
                    ''' Save object reference if we need it for group membership '''
                    b['object'] = d
                    LOGGER.info('Found MultiZone Bulb: {}({})'.format(name, address))
                    self.poly.addNode(MultiZone(self.poly, self.address, address, name, d))
                elif b['type'] == 'bulb':
                    d = lifxlan.Light(mac, ip)
                    ''' Save object reference if we need it for group membership '''
                    b['object'] = d
                    LOGGER.info('Found Bulb: {}({})'.format(name, address))
                    self.poly.addNode(Light(self.poly, self.address, address, name, d))
                elif b['type'] == 'tile':
                    d = lifxlan.TileChain(mac, ip)
                    ''' Save object reference if we need it for group membership '''
                    b['object'] = d
                    LOGGER.info('Found Tile: {}({})'.format(name, address))
                    self.poly.addNode(Tile(self.poly, self.address, address, name, d))
                else:
                    LOGGER.error('Unknown type: {}'.format(b['type']))
        self.setDriver('GV0', self.bulbs_found)

        if 'groups' not in data:
            LOGGER.info('Manual discovery file {} is missing groups section'.format(self.parameters['devlist']))
            return True

        for grp in data['groups']:
            members = []
            for member_light in grp['members']:
                light_found = False
                for b in data['bulbs']:
                    if b['name'] == member_light:
                        members.append(b['object'])
                        light_found = True
                        break
                if not light_found:
                    LOGGER.error('Group {} light {} is not found'.format(grp['name'], member_light))
            LOGGER.info('Group {}, {} members'.format(grp['name'], len(members)))
            if len(members) > 0:
                gaddress = grp['address']
                glabel = grp['name']
                if not self.poly.getNode(gaddress):
                    LOGGER.info('Found LiFX Group: {}'.format(glabel))
                    grp = lifxlan.Group(members)
                    self.poly.addNode(Group(self.poly, self.address, gaddress, glabel, grp))
        return True

    def _discovery_process(self):
        LOGGER.info('Starting LiFX Discovery thread...')
        if self.parameters['devlist']:
            LOGGER.info('Attempting manual discovery...')
            if self._manual_discovery():
                LOGGER.info('Manual discovery is complete')
                return
            else:
                LOGGER.error('Manual discovery failed')
        try:
            devices = self.lifxLan.get_lights()
            LOGGER.info('{} bulbs found. Checking status and adding to ISY if necessary.'.format(len(devices)))
            for d in devices:
                label = str(d.get_label())
                name = 'LIFX {}'.format(label)
                address = d.get_mac_addr().replace(':', '').lower()
                if not self.poly.getNode(address):
                    self.bulbs_found += 1
                    if d.supports_multizone():
                        LOGGER.info('Found MultiZone Bulb: {}({})'.format(name, address))
                        self.poly.addNode(MultiZone(self.poly, self.address, address, name, d))
                    else:
                        LOGGER.info('Found Bulb: {}({})'.format(name, address))
                        self.poly.addNode(Light(self.poly, self.address, address, name, d))
                gid, glabel, gupdatedat = d.get_group_tuple()
                gaddress = glabel.replace("'", "").replace(' ', '').lower()[:12]
                if not self.poly.getNode(gaddress):
                    LOGGER.info('Found LiFX Group: {}'.format(glabel))
                    self.poly.addNode(Group(self.poly, self.address, gaddress, glabel))
        except (lifxlan.WorkflowException, OSError, IOError, TypeError) as ex:
            LOGGER.error('discovery Error: {}'.format(ex))
        self.update_nodes = False
        try:
            old_bulbs_found = int(self.getDriver('GV0'))
        except:
            old_bulbs_found = self.bulbs_found
        else:
            if self.bulbs_found != old_bulbs_found:
                LOGGER.info('NOTICE: Bulb count {} is different, was {} previously'.format(self.bulbs_found, old_bulbs_found))
        self.setDriver('GV0', self.bulbs_found)
        LOGGER.info('LiFX Discovery thread is complete.')

    def all_on(self, command):
        try:
            self.lifxLan.set_power_all_lights("on", rapid=True)
        except (lifxlan.WorkflowException, OSError, IOError, TypeError) as ex:
            LOGGER.error('All On Error: {}'.format(str(ex)))

    def all_off(self, command):
        try:
            self.lifxLan.set_power_all_lights("off", rapid=True)
        except (lifxlan.WorkflowException, OSError, IOError, TypeError) as ex:
            LOGGER.error('All Off Error: {}'.format(str(ex)))

    def set_wf(self, command):
        WAVEFORM = ['Saw', 'Sine', 'HalfSine', 'Triangle', 'Pulse']
        query = command.get('query')
        wf_color = [int(query.get('H.uom56')), int(query.get('S.uom56')), int(query.get('B.uom56')), int(query.get('K.uom26'))]
        wf_period = int(query.get('PE.uom42'))
        wf_cycles = int(query.get('CY.uom56'))
        wf_duty_cycle = int(query.get('DC.uom56'))
        wf_form = int(query.get('WF.uom25'))
        if wf_form >= 5:
            wf_transient = 1
            wf_form -= 5
        else:
            wf_transient = 0
        LOGGER.debug('Color tuple: {}, Period: {}, Cycles: {}, Duty cycle: {}, Form: {}, Transient: {}'.format(wf_color, wf_period, wf_cycles, wf_duty_cycle, WAVEFORM[wf_form], wf_transient))
        try:
            self.lifxLan.set_waveform_all_lights(wf_transient, wf_color, wf_period, wf_cycles, wf_duty_cycle, wf_form)
        except lifxlan.WorkflowException as ex:
                LOGGER.error('Connection Error on setting Waveform for all lights: {}'.format(str(ex)))

    def setColor(self, command):
        _color = int(command.get('value'))
        try:
            self.lifxLan.set_color_all_lights(COLORS[_color][1], rapid=True)
        except lifxlan.WorkflowException as ex:
            LOGGER.error('Connection Error on setting all bulb color: {}'.format(str(ex)))

    def setHSBKD(self, command):
        query = command.get('query')
        try:
            color = [int(query.get('H.uom56')), int(query.get('S.uom56')), int(query.get('B.uom56')), int(query.get('K.uom26'))]
            duration = int(query.get('D.uom42'))
            LOGGER.info('Received manual change, updating all bulb to: {} duration: {}'.format(str(color), duration))
        except TypeError:
            duration = 0
        try:
            self.lifxLan.set_color_all_lights(color, duration=duration, rapid=True)
        except lifxlan.WorkflowException as ex:
                LOGGER.error('Connection Error on setting all bulb color: {}'.format(str(ex)))

    drivers = [{'driver': 'ST', 'value': 1, 'uom': 2},
               {'driver': 'GV0', 'value': 0, 'uom': 56}
              ]

    id = 'controller'

    commands = {'DISCOVER': discover, 'DON': all_on, 'DOF': all_off,
                'SET_COLOR': setColor, 'SET_HSBKD': setHSBKD, 'WAVEFORM': set_wf
               }


class Light(udi_interface.Node):
    """
    LiFX Light Parent Class
    """
    def __init__(self, polyglot, primary, address, name, dev):
        super().__init__(polyglot, primary, address, name)
        self.controller = self.poly.getNode(self.primary)
        self.device = dev
        self.name = name
        self.power = False
        self.connected = 1
        self.uptime = 0
        self.color= []
        self.lastupdate = time.time()
        self.duration = 0
        self.ir_support = False

    def start(self):
        try:
            self.duration = int(self.getDriver('RR'))
        except:
            self.duration = 0
        self.update()
        self.long_update()

    def query(self, command = None):
        self.update()
        self.long_update()
        self.reportDrivers()

    def update(self):
        self.connected = 0
        try:
            self.color = list(self.device.get_color())
        except Exception as ex:
            LOGGER.error('Connection Error on getting {} bulb color. This happens from time to time, normally safe to ignore. {}'.format(self.name, str(ex)))
            ''' stop here as proceeding without self.color may cause exceptions '''
            self.setDriver('GV5', self.connected)
            return
        else:
            self.connected = 1
            for ind, driver in enumerate(('GV1', 'GV2', 'GV3', 'CLITEMP')):
                self.setDriver(driver, self.color[ind])
        try:
            power_now = True if self.device.get_power() == 65535 else False
            if self.power != power_now:
                if power_now:
                    self.reportCmd('DON')
                else:
                    self.reportCmd('DOF')
            self.power = power_now
        except Exception as ex:
            LOGGER.error('Connection Error on getting {} bulb power. This happens from time to time, normally safe to ignore. {}'.format(self.name, str(ex)))
        else:
            self.connected = 1
            if self.power:
                self.setDriver('ST', self._bri_to_percent(self.color[2]))
            else:
                self.setDriver('ST', 0)
        self.setDriver('GV5', self.connected)
        self.setDriver('RR', self.duration)
        self.lastupdate = time.time()

    def long_update(self):
        self.connected = 0
        try:
            self.uptime = self._nanosec_to_hours(self.device.get_uptime())
            self.ir_support = self.device.supports_infrared()
        except Exception as ex:
            LOGGER.error('Connection Error on getting {} bulb uptime. This happens from time to time, normally safe to ignore. {}'.format(self.name, str(ex)))
        else:
            self.connected = 1
            self.setDriver('GV6', self.uptime)
        if self.ir_support:
            try:
                ir_brightness = self.device.get_infrared()
            except Exception as ex:
                LOGGER.error('Connection Error on getting {} bulb Infrared. This happens from time to time, normally safe to ignore. {}'.format(self.name, str(ex)))
            else:
                self.connected = 1
                self.setDriver('GV7', ir_brightness)
        else:
            self.setDriver('GV7', 0)
        try:
            wifi_signal = math.floor(10 * math.log10(self.device.get_wifi_signal_mw()) + 0.5)
        except (lifxlan.WorkflowException, OSError, ValueError) as ex:
            LOGGER.error('Connection Error on getting {} bulb WiFi signal strength. This happens from time to time, normally safe to ignore. {}'.format(self.name, str(ex)))
        else:
            self.connected = 1
            self.setDriver('GV0', wifi_signal)
        self.setDriver('GV5', self.connected)
        self.lastupdate = time.time()

    def _nanosec_to_hours(self, ns):
        return int(round(ns/(1000000000.0*60*60)))

    def _bri_to_percent(self, bri):
        return float(round(bri*100/65535, 4))

    def _power_on_change(self):
        if not self.controller.change_pon or self.power:
            return
        try:
            self.device.set_power(True)
        except lifxlan.WorkflowException as ex:
            LOGGER.error('Connection Error on setting {} bulb power. This happens from time to time, normally safe to ignore. {}'.format(self.name, str(ex)))
        else:
            self.power = True
            self.setDriver('ST', self._bri_to_percent(self.color[2]))

    def setOn(self, command):
        cmd = command.get('cmd')
        val = command.get('value')
        new_bri = None
        if cmd == 'DFON' and self.color[2] != BR_MAX:
            new_bri = BR_MAX
            trans = 0
        elif cmd == 'DON' and val is not None:
            new_bri = int(round(int(val)*65535/255))
            if new_bri > BR_MAX:
                new_bri = BR_MAX
            elif new_bri < BR_MIN:
                new_bri = BR_MIN
            trans = self.duration
        elif self.power and self.controller.ignore_second_on:
            LOGGER.info('{} is already On, ignoring DON'.format(self.name))
            return
        elif self.power and self.color[2] != BR_MAX:
            new_bri = BR_MAX
            trans = self.duration
        if new_bri is not None:
            self.color[2] = new_bri
            try:
                self.device.set_color(self.color, trans, rapid=False)
            except lifxlan.WorkflowException as ex:
                LOGGER.error('Connection Error DON {} bulb. This happens from time to time, normally safe to ignore. {}'.format(self.name, str(ex)))
            else:
                self.setDriver('GV3', self.color[2])
        try:
            self.device.set_power(True)
        except lifxlan.WorkflowException as ex:
            LOGGER.error('Connection Error on setting {} bulb power. This happens from time to time, normally safe to ignore. {}'.format(self.name, str(ex)))
        else:
            self.power = True
            self.setDriver('ST', self._bri_to_percent(self.color[2]))

    def setOff(self, command):
        try:
            self.device.set_power(False)
        except lifxlan.WorkflowException as ex:
            LOGGER.error('Connection Error on setting {} bulb power. This happens from time to time, normally safe to ignore. {}'.format(self.name, str(ex)))
        else:
            self.power = False
            self.setDriver('ST', 0)

    def dim(self, command):
        if self.power is False:
            LOGGER.info('{} is off, ignoring DIM'.format(self.name))
        new_bri = self.color[2] - BR_INCREMENT
        if new_bri < BR_MIN:
            new_bri = BR_MIN
        self.color[2] = new_bri
        try:
            self.device.set_color(self.color, BRTDIM_INTERVAL, rapid=False)
        except lifxlan.WorkflowException as ex:
            LOGGER.error('Connection Error on dimming {} bulb. This happens from time to time, normally safe to ignore. {}'.format(self.name, str(ex)))
        else:
            self.setDriver('ST', self._bri_to_percent(self.color[2]))
            self.setDriver('GV3', self.color[2])

    def brighten(self, command):
        if self.power is False:
            # Bulb is currently off, let's turn it on ~2%
            self.color[2] = BR_MIN
            try:
                self.device.set_color(self.color, 0, rapid=False)
                self.device.set_power(True)
            except lifxlan.WorkflowException as ex:
                LOGGER.error('Connection Error on brightnening {} bulb. This happens from time to time, normally safe to ignore. {}'.format(self.name, str(ex)))
            else:
                self.power = True
                self.setDriver('ST', self._bri_to_percent(self.color[2]))
            return
        new_bri = self.color[2] + BR_INCREMENT
        if new_bri > BR_MAX:
            new_bri = BR_MAX
        self.color[2] = new_bri
        try:
            self.device.set_color(self.color, BRTDIM_INTERVAL, rapid=False)
        except lifxlan.WorkflowException as ex:
            LOGGER.error('Connection Error on dimming {} bulb. This happens from time to time, normally safe to ignore. {}'.format(self.name, str(ex)))
        else:
            self.setDriver('ST', self._bri_to_percent(self.color[2]))
            self.setDriver('GV3', self.color[2])

    def fade_up(self, command):
        if self.power is False:
            # Bulb is currently off, let's turn it on ~2%
            self.color[2] = BR_MIN
            try:
                self.device.set_color(self.color, 0, rapid=False)
                self.device.set_power(True)
            except lifxlan.WorkflowException as ex:
                LOGGER.error('Connection Error on brightnening {} bulb. This happens from time to time, normally safe to ignore. {}'.format(self.name, str(ex)))
            else:
                self.power = True
                self.setDriver('ST', self._bri_to_percent(self.color[2]))
        if self.color[2] == BR_MAX:
            LOGGER.info('{} Can not FadeUp, already at maximum'.format(self.name))
            return
        self.color[2] = BR_MAX
        try:
            self.device.set_color(self.color, FADE_INTERVAL, rapid=False)
        except lifxlan.WorkflowException as ex:
            LOGGER.error('Connection Error {} bulb Fade Up. This happens from time to time, normally safe to ignore. {}'.format(self.name, str(ex)))

    def fade_down(self, command):
        if self.power is False:
            LOGGER.error('{} can not FadeDown as it is currently off'.format(self.name))
            return
        if self.color[2] <= BR_MIN:
            LOGGER.error('{} can not FadeDown as it is currently at minimum'.format(self.name))
            return
        self.color[2] = BR_MIN
        try:
            self.device.set_color(self.color, FADE_INTERVAL, rapid=False)
        except lifxlan.WorkflowException as ex:
            LOGGER.error('Connection Error {} bulb Fade Down. This happens from time to time, normally safe to ignore. {}'.format(self.name, str(ex)))

    def fade_stop(self, command):
        if self.power is False:
            LOGGER.error('{} can not FadeStop as it is currently off'.format(self.name))
            return
        # check current brightness level
        try:
            self.color = list(self.device.get_color())
        except Exception as ex:
            LOGGER.error('Connection Error on getting {} bulb color. This happens from time to time, normally safe to ignore. {}'.format(self.name, str(ex)))
        else:
            for ind, driver in enumerate(('GV1', 'GV2', 'GV3', 'CLITEMP')):
                self.setDriver(driver, self.color[ind])
        if self.color[2] == BR_MIN or self.color[2] == BR_MAX:
            LOGGER.error('{} can not FadeStop as it is currently at limit'.format(self.name))
            return
        try:
            self.device.set_color(self.color, 0, rapid=False)
        except lifxlan.WorkflowException as ex:
            LOGGER.error('Connection Error {} bulb Fade Stop. This happens from time to time, normally safe to ignore. {}'.format(self.name, str(ex)))

    def setColor(self, command):
        if self.connected:
            _color = int(command.get('value'))
            try:
                self.device.set_color(COLORS[_color][1], duration=self.duration, rapid=False)
            except lifxlan.WorkflowException as ex:
                LOGGER.error('Connection Error on setting {} bulb color. This happens from time to time, normally safe to ignore. {}'.format(self.name, str(ex)))
            LOGGER.info('Received SetColor command from ISY. Changing color to: {}'.format(COLORS[_color][0]))
            for ind, driver in enumerate(('GV1', 'GV2', 'GV3', 'CLITEMP')):
                self.setDriver(driver, COLORS[_color][1][ind])
            self._power_on_change()
        else:
            LOGGER.error('Received SetColor, however the bulb is in a disconnected state... ignoring')

    def setManual(self, command):
        if self.connected:
            _cmd = command.get('cmd')
            _val = int(command.get('value'))
            if _cmd == 'SETH':
                self.color[0] = _val
                driver = ['GV1', self.color[0]]
            elif _cmd == 'SETS':
                self.color[1] = _val
                driver = ['GV2', self.color[1]]
            elif _cmd == 'SETB':
                self.color[2] = _val
                driver = ['GV3', self.color[2]]
            elif _cmd == 'CLITEMP':
                self.color[3] = _val
                driver = ['CLITEMP', self.color[3]]
            elif _cmd == 'RR':
                self.duration = _val
                driver = ['RR', self.duration]
            try:
                self.device.set_color(self.color, self.duration, rapid=False)
            except lifxlan.WorkflowException as ex:
                LOGGER.error('Connection Error on setting {} bulb {}. This happens from time to time, normally safe to ignore. {}'.format(self.name, _cmd, str(ex)))
            LOGGER.info('Received manual change, updating the bulb to: {} duration: {}'.format(str(self.color), self.duration))
            if driver:
                self.setDriver(driver[0], driver[1])
            self._power_on_change()
        else: LOGGER.info('Received manual change, however the bulb is in a disconnected state... ignoring')

    def setHSBKD(self, command):
        query = command.get('query')
        try:
            self.color = [int(query.get('H.uom56')), int(query.get('S.uom56')), int(query.get('B.uom56')), int(query.get('K.uom26'))]
            self.duration = int(query.get('D.uom42'))
            LOGGER.info('Received manual change, updating the bulb to: {} duration: {}'.format(str(self.color), self.duration))
        except TypeError:
            self.duration = 0
        try:
            self.device.set_color(self.color, duration=self.duration, rapid=False)
        except lifxlan.WorkflowException as ex:
                LOGGER.error('Connection Error on setting {} bulb color. This happens from time to time, normally safe to ignore. {}'.format(self.name, str(ex)))
        for ind, driver in enumerate(('GV1', 'GV2', 'GV3', 'CLITEMP')):
            self.setDriver(driver, self.color[ind])
        self._power_on_change()
        self.setDriver('RR', self.duration)

    def set_ir_brightness(self, command):
        _val = int(command.get('value'))
        if not self.ir_support:
            LOGGER.error('{} is not IR capable'.format(self.name))
            return
        try:
            self.device.set_infrared(_val)
        except lifxlan.WorkflowException as ex:
            LOGGER.error('Connection Error on setting {} bulb IR Brightness. This happens from time to time, normally safe to ignore. {}'.format(self.name, str(ex)))
        else:
            self.setDriver('GV7', _val)

    def set_wf(self, command):
        WAVEFORM = ['Saw', 'Sine', 'HalfSine', 'Triangle', 'Pulse']
        if self.power is False:
            LOGGER.error('{} can not run Waveform as it is currently off'.format(self.name))
            return
        query = command.get('query')
        wf_color = [int(query.get('H.uom56')), int(query.get('S.uom56')), int(query.get('B.uom56')), int(query.get('K.uom26'))]
        wf_period = int(query.get('PE.uom42'))
        wf_cycles = int(query.get('CY.uom56'))
        wf_duty_cycle = int(query.get('DC.uom56'))
        wf_form = int(query.get('WF.uom25'))
        if wf_form >= 5:
            wf_transient = 1
            wf_form -= 5
        else:
            wf_transient = 0
        LOGGER.debug('Color tuple: {}, Period: {}, Cycles: {}, Duty cycle: {}, Form: {}, Transient: {}'.format(wf_color, wf_period, wf_cycles, wf_duty_cycle, WAVEFORM[wf_form], wf_transient))
        try:
            self.device.set_waveform(wf_transient, wf_color, wf_period, wf_cycles, wf_duty_cycle, wf_form)
        except lifxlan.WorkflowException as ex:
                LOGGER.error('Connection Error on setting {} bulb Waveform. This happens from time to time, normally safe to ignore. {}'.format(self.name, str(ex)))

    drivers = [{'driver': 'ST', 'value': 0, 'uom': 51},
                {'driver': 'GV0', 'value': 0, 'uom': 56},
                {'driver': 'GV1', 'value': 0, 'uom': 56},
                {'driver': 'GV2', 'value': 0, 'uom': 56},
                {'driver': 'GV3', 'value': 0, 'uom': 56},
                {'driver': 'CLITEMP', 'value': 0, 'uom': 26},
                {'driver': 'GV5', 'value': 0, 'uom': 2},
                {'driver': 'GV6', 'value': 0, 'uom': 20},
                {'driver': 'GV7', 'value': 0, 'uom': 56},
                {'driver': 'RR', 'value': 0, 'uom': 42}]

    id = 'lifxcolor'

    commands = {
                    'DON': setOn, 'DOF': setOff, 'QUERY': query,
                    'SET_COLOR': setColor, 'SETH': setManual,
                    'SETS': setManual, 'SETB': setManual,
                    'CLITEMP': setManual,
                    'RR': setManual, 'SET_HSBKD': setHSBKD,
                    'BRT': brighten, 'DIM': dim, 'FDUP': fade_up,
                    'FDDOWN': fade_down, 'FDSTOP': fade_stop,
                    'DFON': setOn, 'DFOF': setOff,
                    'SETIR': set_ir_brightness, 'WAVEFORM': set_wf
                }

class MultiZone(Light):
    def __init__(self, polyglot, primary, address, name, dev):
        super().__init__(polyglot, primary, address, name, dev)
        self.num_zones = 0
        self.current_zone = 0
        self.new_color = None
        self.pending = False
        self.effect = 0

    def update(self):
        self.connected = 0
        zone = deepcopy(self.current_zone)
        if self.current_zone != 0: zone -= 1
        if not self.pending:
            try:
                self.color = self.device.get_color_zones()
            except Exception as ex:
                LOGGER.error('Connection Error on getting {} multizone color. This happens from time to time, normally safe to ignore. {}'.format(self.name, str(ex)))
            else:
                self.connected = 1
                self.num_zones = len(self.color)
                for ind, driver in enumerate(('GV1', 'GV2', 'GV3', 'CLITEMP')):
                    try:
                        self.setDriver(driver, self.color[zone][ind])
                    except (TypeError) as e:
                        LOGGER.debug('setDriver for color caught an error. color was : {}'.format(self.color or None))
                self.setDriver('GV4', self.current_zone)
        try:
            power_now = True if self.device.get_power() == 65535 else False
            if self.power != power_now:
                if power_now:
                    self.reportCmd('DON')
                else:
                    self.reportCmd('DOF')
            self.power = power_now
        except Exception as ex:
            LOGGER.error('Connection Error on getting {} multizone power. This happens from time to time, normally safe to ignore. {}'.format(self.name, str(ex)))
        else:
            self.connected = 1
            self._set_st()
        self.setDriver('GV5', self.connected)
        self.setDriver('RR', self.duration)
        self.lastupdate = time.time()

    def _set_st(self):
        if self.num_zones == 0: return
        if self.power:
            avg_brightness = 0
            for z in self.color:
                avg_brightness += z[2]
            avg_brightness /= self.num_zones
            self.setDriver('ST', self._bri_to_percent(avg_brightness))
        else:
            self.setDriver('ST', 0)

    def start(self):
        try:
            self.duration = int(self.getDriver('RR'))
        except:
            self.duration = 0
        try:
            self.current_zone = int(self.getDriver('GV4'))
        except:
            self.current_zone = 0
        self.update()
        self.long_update()

    def setOn(self, command):
        zone = deepcopy(self.current_zone)
        if self.current_zone != 0: zone -= 1
        cmd = command.get('cmd')
        val = command.get('value')
        new_bri = None
        if cmd == 'DFON' and self.color[zone][2] != BR_MAX:
            new_bri = BR_MAX
            trans = 0
        elif cmd == 'DON' and val is not None:
            new_bri = int(round(int(val)*65535/255))
            if new_bri > BR_MAX:
                new_bri = BR_MAX
            elif new_bri < BR_MIN:
                new_bri = BR_MIN
            trans = self.duration
        elif self.power and self.color[zone][2] != BR_MAX:
            new_bri = BR_MAX
            trans = self.duration
        if new_bri is not None:
            new_color = list(self.color[zone])
            new_color[2] = new_bri
            try:
                if self.current_zone == 0:
                    self.device.set_color(new_color, trans, rapid=False)
                else:
                    self.device.set_zone_color(zone, zone, new_color, trans, rapid=False)
            except lifxlan.WorkflowException as ex:
                LOGGER.error('Connection Error DON {} bulb. This happens from time to time, normally safe to ignore. {}'.format(self.name, str(ex)))
            else:
                self.setDriver('GV3', new_color[2])
        try:
            self.device.set_power(True)
        except lifxlan.WorkflowException as ex:
            LOGGER.error('Connection Error on setting {} bulb power. This happens from time to time, normally safe to ignore. {}'.format(self.name, str(ex)))
        else:
            self.power = True
            self._set_st()

    def dim(self, command):
        zone = deepcopy(self.current_zone)
        if self.current_zone != 0: zone -= 1
        if self.power is False:
            LOGGER.info('{} is off, ignoring DIM'.format(self.name))
        new_bri = self.color[zone][2] - BR_INCREMENT
        if new_bri < BR_MIN:
            new_bri = BR_MIN
        new_color = list(self.color[zone])
        new_color[2] = new_bri
        try:
            if self.current_zone == 0:
                self.device.set_color(new_color, BRTDIM_INTERVAL, rapid=False)
            else:
                self.device.set_zone_color(zone, zone, new_color, BRTDIM_INTERVAL, rapid=False)
        except lifxlan.WorkflowException as ex:
            LOGGER.error('Connection Error on dimming {} bulb. This happens from time to time, normally safe to ignore. {}'.format(self.name, str(ex)))
        else:
            self._set_st()
            self.setDriver('GV3', new_color[2])

    def brighten(self, command):
        zone = deepcopy(self.current_zone)
        if self.current_zone != 0: zone -= 1
        new_color = list(self.color[zone])
        if self.power is False:
            # Bulb is currently off, let's turn it on ~2%
            new_color[2] = BR_MIN
            try:
                if self.current_zone == 0:
                    self.device.set_color(new_color, 0, rapid=False)
                else:
                    self.device.set_zone_color(zone, zone, new_color, 0, rapid=False)
                self.device.set_power(True)
            except lifxlan.WorkflowException as ex:
                LOGGER.error('Connection Error on brightnening {} bulb. This happens from time to time, normally safe to ignore. {}'.format(self.name, str(ex)))
            else:
                self.power = True
                self._set_st()
            return
        new_bri = self.color[zone][2] + BR_INCREMENT
        if new_bri > BR_MAX:
            new_bri = BR_MAX
        new_color[2] = new_bri
        try:
            if self.current_zone == 0:
                self.device.set_color(new_color, BRTDIM_INTERVAL, rapid=False)
            else:
                self.device.set_zone_color(zone, zone, new_color, BRTDIM_INTERVAL, rapid=False)
        except lifxlan.WorkflowException as ex:
            LOGGER.error('Connection Error on dimming {} bulb. This happens from time to time, normally safe to ignore. {}'.format(self.name, str(ex)))
        else:
            self._set_st()
            self.setDriver('GV3', new_color[2])

    def fade_up(self, command):
        zone = deepcopy(self.current_zone)
        if self.current_zone != 0: zone -= 1
        new_color = list(self.color[zone])
        if self.power is False:
            # Bulb is currently off, let's turn it on ~2%
            new_color[2] = BR_MIN
            try:
                if self.current_zone == 0:
                    self.device.set_color(new_color, 0, rapid=False)
                else:
                    self.device.set_zone_color(zone, zone, new_color, 0, rapid=False)
                self.device.set_power(True)
            except lifxlan.WorkflowException as ex:
                LOGGER.error('Connection Error on brightnening {} bulb. This happens from time to time, normally safe to ignore. {}'.format(self.name, str(ex)))
            else:
                self.power = True
                self._set_st()
        if self.color[zone][2] == BR_MAX:
            LOGGER.info('{} Can not FadeUp, already at maximum'.format(self.name))
            return
        new_color[2] = BR_MAX
        try:
            if self.current_zone == 0:
                self.device.set_color(new_color, FADE_INTERVAL, rapid=False)
            else:
                self.device.set_zone_color(zone, zone, new_color, FADE_INTERVAL, rapid=False)
        except lifxlan.WorkflowException as ex:
            LOGGER.error('Connection Error {} bulb Fade Up. This happens from time to time, normally safe to ignore. {}'.format(self.name, str(ex)))

    def fade_down(self, command):
        zone = deepcopy(self.current_zone)
        if self.current_zone != 0: zone -= 1
        new_color = list(self.color[zone])
        if self.power is False:
            LOGGER.error('{} can not FadeDown as it is currently off'.format(self.name))
            return
        if self.color[zone][2] <= BR_MIN:
            LOGGER.error('{} can not FadeDown as it is currently at minimum'.format(self.name))
            return
        new_color[2] = BR_MIN
        try:
            if self.current_zone == 0:
                self.device.set_color(new_color, FADE_INTERVAL, rapid=False)
            else:
                self.device.set_zone_color(zone, zone, new_color, FADE_INTERVAL, rapid=False)
        except lifxlan.WorkflowException as ex:
            LOGGER.error('Connection Error {} bulb Fade Down. This happens from time to time, normally safe to ignore. {}'.format(self.name, str(ex)))

    def fade_stop(self, command):
        zone = deepcopy(self.current_zone)
        if self.current_zone != 0: zone -= 1
        if self.power is False:
            LOGGER.error('{} can not FadeStop as it is currently off'.format(self.name))
            return
        # check current brightness level
        try:
            self.color = self.device.get_color_zones()
        except Exception as ex:
            LOGGER.error('Connection Error on getting {} multizone color. This happens from time to time, normally safe to ignore. {}'.format(self.name, str(ex)))
        else:
            for ind, driver in enumerate(('GV1', 'GV2', 'GV3', 'CLITEMP')):
                self.setDriver(driver, self.color[zone][ind])
        if self.color[zone][2] == BR_MIN or self.color[zone][2] == BR_MAX:
            LOGGER.error('{} can not FadeStop as it is currently at limit'.format(self.name))
            return
        try:
            if self.current_zone == 0:
                self.device.set_color(self.color[zone], 0, rapid=False)
            else:
                self.device.set_zone_color(zone, zone, self.color[zone], 0, rapid=False)
        except lifxlan.WorkflowException as ex:
            LOGGER.error('Connection Error {} bulb Fade Stop. This happens from time to time, normally safe to ignore. {}'.format(self.name, str(ex)))

    def apply(self, command):
        try:
            if self.new_color:
                self.color = deepcopy(self.new_color)
                self.new_color = None
            self.device.set_zone_colors(self.color, self.duration, rapid=True)
        except (lifxlan.WorkflowException, IOError) as ex:
            LOGGER.error('Connection Error on setting {} bulb color. This happens from time to time, normally safe to ignore. {}'.format(self.name, str(ex)))
        LOGGER.info('Received apply command for {}'.format(self.address))
        self.pending = False

    def setColor(self, command):
        if self.connected:
            try:
                _color = int(command.get('value'))
                zone = deepcopy(self.current_zone)
                if self.current_zone != 0: zone -= 1
                if self.current_zone == 0:
                    self.device.set_color(COLORS[_color][1], self.duration, True)
                else:
                    self.device.set_zone_color(zone, zone, COLORS[_color][1], self.duration, True)
                LOGGER.info('Received SetColor command from ISY. Changing {} color to: {}'.format(self.address, COLORS[_color][0]))
            except (lifxlan.WorkflowException, IOError) as ex:
                LOGGER.error('mz setcolor error {}'.format(str(ex)))
            for ind, driver in enumerate(('GV1', 'GV2', 'GV3', 'CLITEMP')):
                self.setDriver(driver, COLORS[_color][1][ind])
        else: LOGGER.info('Received SetColor, however the bulb is in a disconnected state... ignoring')

    def setManual(self, command):
        if self.connected:
            _cmd = command.get('cmd')
            _val = int(command.get('value'))
            try:
                if _cmd == 'SETZ':
                    self.current_zone = int(_val)
                    if self.current_zone > self.num_zones: self.current_zone = 0
                    driver = ['GV4', self.current_zone]
                zone = deepcopy(self.current_zone)
                if self.current_zone != 0: zone -= 1
                new_color = list(self.color[zone])
                if _cmd == 'SETH':
                    new_color[0] = int(_val)
                    driver = ['GV1', new_color[0]]
                elif _cmd == 'SETS':
                    new_color[1] = int(_val)
                    driver = ['GV2', new_color[1]]
                elif _cmd == 'SETB':
                    new_color[2] = int(_val)
                    driver = ['GV3', new_color[2]]
                elif _cmd == 'CLITEMP':
                    new_color[3] = int(_val)
                    driver = ['CLITEMP', new_color[3]]
                elif _cmd == 'RR':
                    self.duration = _val
                    driver = ['RR', self.duration]
                self.color[zone] = new_color
                if self.current_zone == 0:
                    self.device.set_color(new_color, self.duration, rapid=False)
                else:
                    self.device.set_zone_color(zone, zone, new_color, self.duration, rapid=False)
            except (lifxlan.WorkflowException, TypeError) as ex:
                LOGGER.error('setmanual mz error {}'.format(ex))
            LOGGER.info('Received manual change, updating the mz bulb zone {} to: {} duration: {}'.format(zone, new_color, self.duration))
            if driver:
                self.setDriver(driver[0], driver[1])
        else: LOGGER.info('Received manual change, however the mz bulb is in a disconnected state... ignoring')

    def setHSBKDZ(self, command):
        query = command.get('query')
        if not self.pending:
            self.new_color = deepcopy(self.color)
            self.pending = True
        current_zone = int(query.get('Z.uom56'))
        zone = deepcopy(current_zone)
        if current_zone != 0: zone -= 1
        self.new_color[zone] = [int(query.get('H.uom56')), int(query.get('S.uom56')), int(query.get('B.uom56')), int(query.get('K.uom26'))]
        try:
            self.duration = int(query.get('D.uom42'))
        except TypeError:
            self.duration = 0
        try:
            if current_zone == 0:
                self.device.set_color(self.new_color, self.duration, rapid=False)
            else:
                self.device.set_zone_color(zone, zone, self.new_color, self.duration, rapid=False, apply = 0)
        except (lifxlan.WorkflowException, IOError) as ex:
            LOGGER.error('set mz hsbkdz error %s', str(ex))

    def set_effect(self, command):
        query = command.get('query')
        effect_type = int(query.get('EF.uom25'))
        if effect_type < 0 or effect_type > 1:
            LOGGER.error('Invalid effect type requested')
            return
        ''' 0 - No effect, 1 - Move '''
        effect_speed = int(query.get('ES.uom42'))
        ''' needs effect duration in nanoseconds so multiply by 2*10^6 '''
        effect_duration = int(query.get('ED.uom42'))*1000000
        parameters = [ 0, int(query.get('ER.uom2')) ]
        try:
            self.device.set_multizone_effect(effect_type=effect_type, speed=effect_speed, duration=effect_duration, parameters=parameters)
        except (lifxlan.WorkflowException, TypeError) as ex:
            LOGGER.error('set_effect error {}'.format(ex))


    commands = {
                    'DON': setOn, 'DOF': Light.setOff,
                    'APPLY': apply, 'QUERY': Light.query,
                    'SET_COLOR': setColor, 'SETH': setManual,
                    'SETS': setManual, 'SETB': setManual,
                    'CLITEMP': setManual, 'RR': setManual,
                    'SETZ': setManual, 'SET_HSBKDZ': setHSBKDZ,
                    'BRT': brighten, 'DIM': dim,
                    'FDUP': fade_up, 'FDDOWN': fade_down,
                    'FDSTOP': fade_stop, 'DFON': setOn,
                    'DFOF': Light.setOff, 'SETIR': Light.set_ir_brightness,
                    'WAVEFORM': Light.set_wf, 'EFFECT': set_effect
                }

    drivers = [{'driver': 'ST', 'value': 0, 'uom': 51},
                {'driver': 'GV0', 'value': 0, 'uom': 56},
                {'driver': 'GV1', 'value': 0, 'uom': 56},
                {'driver': 'GV2', 'value': 0, 'uom': 56},
                {'driver': 'GV3', 'value': 0, 'uom': 56},
                {'driver': 'CLITEMP', 'value': 0, 'uom': 26},
                {'driver': 'GV4', 'value': 0, 'uom': 56},
                {'driver': 'GV5', 'value': 0, 'uom': 2},
                {'driver': 'GV6', 'value': 0, 'uom': 20},
                {'driver': 'GV7', 'value': 0, 'uom': 56},
                {'driver': 'RR', 'value': 0, 'uom': 42}]

    id = 'lifxmultizone'


class Tile(Light):
    """
    LiFX Light is a Parent Class
    """
    def __init__(self, polyglot, primary, address, name, dev):
        super().__init__(polyglot, primary, address, name, dev)
        self.controller = self.poly.getNode(self.primary)
        self.tile_count = 0
        self.effect = 0

    def start(self):
        try:
            self.tile_count = self.device.get_tile_count()
        except Exception as ex:
            LOGGER.error(f'Failed to get tile count for {self.name}: {ex}')
        self.setDriver('GV8', self.tile_count)
        super().start()

    def update(self):
        effect = None
        try:
            effect = self.device.get_tile_effect()
        except Exception as ex:
            LOGGER.error(f'Failed to get {self.name} effect {ex}')
        if effect is not None:
            if int(effect['type']) > 0:
                self.effect = int(effect['type']) - 1
            else:
                self.effect = 0
        self.setDriver('GV9', self.effect)
        super().update()

    def save_state(self, command):
        mem_index = str(command.get('value'))
        try:
            color_array = self.device.get_tilechain_colors()
        except Exception as ex:
            LOGGER.error(f'Failed to retrieve colors for {self.name}: {ex}')
            return
        ''' Create structure for color storage'''
        if 'saved_tile_colors' not in self.controller.cust_data:
            self.controller.cust_data['saved_tile_colors'] = {}
        if self.address not in self.controller.cust_data['saved_tile_colors']:
            self.controller.cust_data['saved_tile_colors'][self.address] = {}
        self.controller.cust_data['saved_tile_colors'][self.address].update({ mem_index: color_array })
        LOGGER.info(self.controller.cust_data['saved_tile_colors'])

    def recall_state(self, command):
        if self.effect > 0:
            LOGGER.info(f'{self.name} is running effect, stopping effect before recall_state()')
            try:
                self.device.set_tile_effect(effect_type=0, speed=3000, duration=0, palette=[])
            except Exception as ex:
                LOGGER.error(f'Failed to stop {self.name} effect')
            self.effect = 0
            self.setDriver('GV9', self.effect)
        mem_index = str(command.get('value'))
        try:
            color_array = self.controller.cust_data['saved_tile_colors'][self.address][mem_index]
        except Exception as ex:
            LOGGER.error(f'Failed to retrieve saved tile colors {mem_index} for {self.name}: {ex}')
            return
        try:
            self.device.set_tilechain_colors(color_array, self.duration)
        except Exception as ex:
            LOGGER.error(f'Failed to set tile colors for {self.name}: {ex}')

    def set_tile_effect(self, command):
        query = command.get('query')
        effect_type = int(query.get('EF.uom25'))
        if effect_type < 0 or effect_type > 2:
            LOGGER.error('Invalid effect type requested')
            return
        self.setDriver('GV9', effect_type)
        ''' 0 - No effect, 1 - Reserved, 2 - Morph, 3 - Flame '''
        ''' However we skip 1 in the NodeDef '''
        if effect_type > 0:
            effect_type += 1
        if effect_type == 2:
            brightness = int(query.get('B.uom56'))
            palette = [(0, 65535, brightness, 3500), (7281, 65535, brightness, 3500), (10922, 65535, brightness, 3500), (22209, 65535, brightness, 3500),
                       (43507, 65535, brightness, 3500), (49333, 65535, brightness, 3500), (53520, 65535, brightness, 3500)]
        else:
            palette = []
        effect_speed = int(query.get('ES.uom42'))
        ''' Tile needs effect duration in nanoseconds so multiply by 2*10^6 '''
        effect_duration = int(query.get('ED.uom42'))*1000000
        try:
            self.device.set_tile_effect(effect_type=effect_type, speed=effect_speed, duration=effect_duration, palette=palette)
        except (lifxlan.WorkflowException, TypeError) as ex:
            LOGGER.error('set_tile_effect error {}'.format(ex))

    drivers = [{'driver': 'ST', 'value': 0, 'uom': 51},
                {'driver': 'GV0', 'value': 0, 'uom': 56},
                {'driver': 'GV1', 'value': 0, 'uom': 56},
                {'driver': 'GV2', 'value': 0, 'uom': 56},
                {'driver': 'GV3', 'value': 0, 'uom': 56},
                {'driver': 'CLITEMP', 'value': 0, 'uom': 26},
                {'driver': 'GV5', 'value': 0, 'uom': 2},
                {'driver': 'GV6', 'value': 0, 'uom': 20},
                {'driver': 'GV7', 'value': 0, 'uom': 56},
                {'driver': 'GV8', 'value': 0, 'uom': 56},
                {'driver': 'GV9', 'value': 0, 'uom': 25},
                {'driver': 'RR', 'value': 0, 'uom': 42}]

    id = 'lifxtile'

    commands = {
                    'DON': Light.setOn, 'DOF': Light.setOff, 'QUERY': Light.query,
                    'SET_COLOR': Light.setColor, 'SETH': Light.setManual,
                    'SETS': Light.setManual, 'SETB': Light.setManual,
                    'CLITEMP': Light.setManual,
                    'RR': Light.setManual, 'SET_HSBKD': Light.setHSBKD,
                    'BRT': Light.brighten, 'DIM': Light.dim, 'FDUP': Light.fade_up,
                    'FDDOWN': Light.fade_down, 'FDSTOP': Light.fade_stop,
                    'DFON': Light.setOn, 'DFOF': Light.setOff,
                    'SETIR': Light.set_ir_brightness, 'WAVEFORM': Light.set_wf,
                    'EFFECT': set_tile_effect, 'TILESV': save_state, 'TILERT': recall_state
                }


class Group(udi_interface.Node):
    """
    LiFX Group Node Class
    """
    def __init__(self, polyglot, primary, address, label, grp=None):
        self.label = label.replace("'", "")
        super().__init__(polyglot, primary, address, 'LIFX Group ' + str(label))
        self.controller = self.poly.getNode(self.primary)
        self.lifxLabel = label
        if grp:
            self.lifxGroup = grp
        else:
            self.lifxGroup = self.controller.lifxLan.get_devices_by_group(label)
        self.numMembers = len(self.lifxGroup.devices)

    def start(self):
        self.update()
        #self.reportDrivers()

    def update(self):
        self.numMembers = len(self.lifxGroup.devices)
        self.setDriver('ST', self.numMembers)

    def long_update(self):
        pass

    def query(self, command = None):
        self.update()
        self.reportDrivers()

    def _power_on_change(self):
        if not self.controller.change_pon:
            return
        try:
            self.lifxGroup.set_power(True,rapid=True)
        except lifxlan.WorkflowException as ex:
            LOGGER.error('Error on setting {} power. This happens from time to time, normally safe to ignore. {}'.format(self.name, str(ex)))

    def setOn(self, command):
        try:
            self.lifxGroup.set_power(True, rapid = True)
        except (lifxlan.WorkflowException, IOError) as ex:
            LOGGER.error('group seton error caught %s', str(ex))
        else:
            LOGGER.info('Received SetOn command for group {} from ISY. Setting all {} members to ON.'.format(self.label, self.numMembers))

    def setOff(self, command):
        try:
            self.lifxGroup.set_power(False, rapid = True)
        except (lifxlan.WorkflowException, IOError) as e:
            LOGGER.error('group setoff error caught {}'.format(str(e)))
        else:
            LOGGER.info('Received SetOff command for group {} from ISY. Setting all {} members to OFF.'.format(self.label, self.numMembers))

    def setColor(self, command):
        _color = int(command.get('value'))
        try:
            self.lifxGroup.set_color(COLORS[_color][1], 0, rapid = True)
        except (lifxlan.WorkflowException, IOError) as ex:
            LOGGER.error('group setcolor error caught %s', str(ex))
        else:
            LOGGER.info('Received SetColor command for group {} from ISY. Changing color to: {} for all {} members.'.format(self.name, COLORS[_color][0], self.numMembers))
            self._power_on_change()

    def setHue(self, command):
        _hue = int(command.get('value'))
        try:
            self.lifxGroup.set_hue(_hue, 0, rapid = True)
        except (lifxlan.WorkflowException, IOError) as ex:
            LOGGER.error('group sethue error caught %s', str(ex))
        else:
            LOGGER.info('Received SetHue command for group {} from ISY. Changing hue to: {} for all {} members.'.format(self.name, _hue, self.numMembers))
            self._power_on_change()

    def setSat(self, command):
        _sat = int(command.get('value'))
        try:
            self.lifxGroup.set_saturation(_sat, 0, rapid = True)
        except (lifxlan.WorkflowException, IOError) as ex:
            LOGGER.error('group setsaturation error caught %s', str(ex))
        else:
            LOGGER.info('Received SetSat command for group {} from ISY. Changing saturation to: {} for all {} members.'.format(self.name, _sat, self.numMembers))
            self._power_on_change()

    def setBri(self, command):
        _bri = int(command.get('value'))
        try:
            self.lifxGroup.set_brightness(_bri, 0, rapid = True)
        except (lifxlan.WorkflowException, IOError) as ex:
            LOGGER.error('group setbrightness error caught %s', str(ex))
        else:
            LOGGER.info('Received SetBri command for group {} from ISY. Changing brightness to: {} for all {} members.'.format(self.name, _bri, self.numMembers))
            self._power_on_change()

    def setCTemp(self, command):
        _ctemp = int(command.get('value'))
        try:
            self.lifxGroup.set_colortemp(_ctemp, 0, rapid = True)
        except (lifxlan.WorkflowException, IOError) as ex:
            LOGGER.error('group setcolortemp error caught %s', str(ex))
        else:
            LOGGER.info('Received SetCTemp command for group {} from ISY. Changing color temperature to: {} for all {} members.'.format(self.name, _ctemp, self.numMembers))
            self._power_on_change()

    def set_ir_brightness(self, command):
        _val = int(command.get('value'))
        try:
            self.lifxGroup.set_infrared(_val)
        except (lifxlan.WorkflowException, IOError) as ex:
            LOGGER.error('group set_infrared_brightness error caught %s', str(ex))
        else:
            LOGGER.info('Received SetIR command for group {} from ISY. Changing infrared brightness to: {} for all {} members.'.format(self.name, _val, self.numMembers))
            self._power_on_change()

    def setHSBKD(self, command):
        query = command.get('query')
        try:
            color = [int(query.get('H.uom56')), int(query.get('S.uom56')), int(query.get('B.uom56')), int(query.get('K.uom26'))]
            duration = int(query.get('D.uom42'))
        except TypeError:
            duration = 0

        try:
            self.lifxGroup.set_color(color, duration = duration, rapid = True)
        except (lifxlan.WorkflowException, IOError) as ex:
            LOGGER.error('group sethsbkd error caught {}'.format(str(ex)))
        else:
            LOGGER.info('Recieved SetHSBKD command for group {} from ISY, Setting all members to Color {}, duration {}'.format(self.label, color, duration))
            self._power_on_change()

    drivers = [{'driver': 'ST', 'value': 0, 'uom': 56}]

    commands = {
                    'DON': setOn, 'DOF': setOff, 'QUERY': query,
                    'SET_COLOR': setColor, 'SET_HSBKD': setHSBKD,
                    'SETH': setHue, 'SETS': setSat, 'SETB': setBri,
                    'CLITEMP': setCTemp, 'DFON': setOn, 'DFOF': setOff,
                    'SETIR': set_ir_brightness
                }

    id = 'lifxgroup'

if __name__ == "__main__":
    try:
        polyglot = udi_interface.Interface('LiFX')
        polyglot.start()
        Controller(polyglot, 'lifxctl', 'lifxctl', 'LiFX')
        polyglot.runForever()
    except (KeyboardInterrupt, SystemExit):
        sys.exit(0)
