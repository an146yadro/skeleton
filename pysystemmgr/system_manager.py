#!/usr/bin/python -u

import sys
import subprocess
import gobject
import dbus
import dbus.service
import dbus.mainloop.glib
import os
import obmc.dbuslib.propertycacher as PropertyCacher
from obmc.dbuslib.bindings import DbusProperties, DbusObjectManager, get_dbus
import obmc.enums
import obmc_system_config as System
import obmc.dbuslib.introspection
import obmc.utils.misc

DBUS_NAME = 'org.openbmc.managers.System'
OBJ_NAME = '/org/openbmc/managers/System'
INTF_SENSOR = 'org.openbmc.SensorValue'
INTF_ITEM = 'org.openbmc.InventoryItem'
INTF_CONTROL = 'org.openbmc.Control'


class SystemManager(DbusProperties, DbusObjectManager):
    def __init__(self, bus, obj_name):
        DbusProperties.__init__(self)
        DbusObjectManager.__init__(self)
        dbus.service.Object.__init__(self, bus, obj_name)
        self.bus = bus

        bus.add_signal_receiver(
            self.bus_handler,
            dbus_interface=dbus.BUS_DAEMON_IFACE,
            signal_name='NameOwnerChanged')
        bus.add_signal_receiver(
            self.NewObjectHandler,
            signal_name="InterfacesAdded", sender_keyword='bus_name')
        bus.add_signal_receiver(
            self.SystemStateHandler, signal_name="GotoSystemState")

        self.Set(DBUS_NAME, "current_state", "")
        self.system_states = {}
        self.bus_name_lookup = {}
        self.bin_path = os.path.dirname(os.path.realpath(sys.argv[0]))

        for name in System.APPS.keys():
            sys_state = System.APPS[name]['system_state']
            if sys_state not in self.system_states:
                self.system_states[sys_state] = []
            self.system_states[sys_state].append(name)

        ## replace symbolic path in ID_LOOKUP
        for category in System.ID_LOOKUP:
            for key in System.ID_LOOKUP[category]:
                val = System.ID_LOOKUP[category][key]
                new_val = val.replace(
                    "<inventory_root>", System.INVENTORY_ROOT)
                System.ID_LOOKUP[category][key] = new_val

        self.SystemStateHandler(System.SYSTEM_STATES[0])

        if not os.path.exists(PropertyCacher.CACHE_PATH):
            print "Creating cache directory: "+PropertyCacher.CACHE_PATH
            os.makedirs(PropertyCacher.CACHE_PATH)

        for s in self.bus.list_names():
            if obmc.utils.misc.org_dot_openbmc_match(s):
                self.bus_handler(s, '', s)

        print "SystemManager Init Done"

    def try_next_state(self):
        current_state = self.Get(DBUS_NAME, "current_state")
        if current_state not in System.EXIT_STATE_DEPEND:
            return

        if all(System.EXIT_STATE_DEPEND[current_state].values()):
            print "All required objects started for "+current_state
            self.gotoNextState()

    def SystemStateHandler(self, state_name):
        ## clearing object started flags
        current_state = self.Get(DBUS_NAME, "current_state")
        try:
            for obj_path in System.EXIT_STATE_DEPEND[current_state]:
                System.EXIT_STATE_DEPEND[current_state][obj_path] = 0
        except:
            pass

        print "Running System State: "+state_name
        if state_name in self.system_states:
            for name in self.system_states[state_name]:
                self.start_process(name)

        try:
            cb = System.ENTER_STATE_CALLBACK[state_name]
            for methd in cb.keys():
                obj = bus.get_object(
                    cb[methd]['bus_name'],
                    cb[methd]['obj_name'],
                    introspect=False)
                method = obj.get_dbus_method(
                    methd, cb[methd]['interface_name'])
                method()
        except:
            pass

        self.Set(DBUS_NAME, "current_state", state_name)

    def gotoNextState(self):
        s = 0
        current_state = self.Get(DBUS_NAME, "current_state")
        for i in range(len(System.SYSTEM_STATES)):
            if (System.SYSTEM_STATES[i] == current_state):
                s = i+1

        if (s == len(System.SYSTEM_STATES)):
            print "ERROR SystemManager: No more system states"
        else:
            new_state_name = System.SYSTEM_STATES[s]
            print "SystemManager Goto System State: "+new_state_name
            self.SystemStateHandler(new_state_name)

    @dbus.service.method(DBUS_NAME, in_signature='', out_signature='s')
    def getSystemState(self):
        return self.Get(DBUS_NAME, "current_state")

    def doObjectLookup(self, category, key):
        bus_name = ""
        obj_path = ""
        intf_name = INTF_ITEM
        try:
            obj_path = System.ID_LOOKUP[category][key]
            bus_name = self.bus_name_lookup[obj_path]
            parts = obj_path.split('/')
            if (parts[3] == 'sensors'):
                intf_name = INTF_SENSOR
        except Exception as e:
            print "ERROR SystemManager: "+str(e)+" not found in lookup"

        return [bus_name, obj_path, intf_name]

    @dbus.service.method(DBUS_NAME, in_signature='ss', out_signature='(sss)')
    def getObjectFromId(self, category, key):
        return self.doObjectLookup(category, key)

    @dbus.service.method(DBUS_NAME, in_signature='sy', out_signature='(sss)')
    def getObjectFromByteId(self, category, key):
        byte = int(key)
        return self.doObjectLookup(category, byte)

    # Get the FRU area names defined in ID_LOOKUP table given a fru_id.
    # If serval areas are defined for a fru_id, the areas are returned
    # together as a string with each area name seperated with ','.
    # If no fru area defined in ID_LOOKUP, an empty string will be returned.
    @dbus.service.method(DBUS_NAME, in_signature='y', out_signature='s')
    def getFRUArea(self, fru_id):
        ret_str = ''
        fru_id = '_' + str(fru_id)
        area_list = [
            area for area in System.ID_LOOKUP['FRU_STR'].keys()
            if area.endswith(fru_id)]
        for area in area_list:
            ret_str = area + ',' + ret_str
        # remove the last ','
        return ret_str[:-1]

    def start_process(self, name):
        if System.APPS[name]['start_process']:
            app = System.APPS[name]
            process_name = self.bin_path+"/"+app['process_name']
            cmdline = []
            cmdline.append(process_name)
            if 'args' in app:
                for a in app['args']:
                    cmdline.append(a)
            try:
                print "Starting process: "+" ".join(cmdline)+": "+name
                if app['monitor_process']:
                    app['popen'] = subprocess.Popen(cmdline)
                else:
                    subprocess.Popen(cmdline)

            except Exception as e:
                ## TODO: error
                print "ERROR: starting process: "+" ".join(cmdline)

    def bus_handler(self, owned_name, old, new):
        if obmc.dbuslib.bindings.is_unique(owned_name) or not new:
            return

        if owned_name == DBUS_NAME:
            return

        objs = obmc.dbuslib.introspection.find_dbus_interfaces(
            self.bus, owned_name, '/', bool)
        current_state = self.Get(DBUS_NAME, "current_state")
        for o in objs.keys():
            if o in self.bus_name_lookup:
                continue
            self.bus_name_lookup[o] = owned_name

            if current_state not in System.EXIT_STATE_DEPEND:
                continue
            if o in System.EXIT_STATE_DEPEND[current_state]:
                print "New object: "+o+" ("+owned_name+")"
                System.EXIT_STATE_DEPEND[current_state][o] = 1

        self.try_next_state()

    def NewObjectHandler(self, obj_path, iprops, bus_name=None):
        current_state = self.Get(DBUS_NAME, "current_state")
        if obj_path in self.bus_name_lookup:
            if (self.bus_name_lookup[obj_path] == bus_name):
                return
        self.bus_name_lookup[obj_path] = bus_name
        if current_state not in System.EXIT_STATE_DEPEND:
            return

        if obj_path in System.EXIT_STATE_DEPEND[current_state]:
            print "New object: "+obj_path+" ("+bus_name+")"
            System.EXIT_STATE_DEPEND[current_state][obj_path] = 1
            ## check if all required objects are
            # started to move to next state
            self.try_next_state()

    @dbus.service.method(DBUS_NAME, in_signature='s', out_signature='sis')
    def gpioInit(self, name):
        gpio_path = ''
        gpio_num = -1
        r = ['', gpio_num, '']
        if name not in System.GPIO_CONFIG:
            # TODO: Error handling
            print "ERROR: "+name+" not found in GPIO config table"
        else:

            gpio_num = -1
            gpio = System.GPIO_CONFIG[name]
            if 'gpio_num' in System.GPIO_CONFIG[name]:
                gpio_num = gpio['gpio_num']
            else:
                if 'gpio_pin' in System.GPIO_CONFIG[name]:
                    gpio_num = System.convertGpio(gpio['gpio_pin'])
                else:
                    print "ERROR: SystemManager - GPIO lookup failed for "+name

            if (gpio_num != -1):
                r = [obmc.enums.GPIO_DEV, gpio_num, gpio['direction']]
        return r


if __name__ == '__main__':
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = get_dbus()
    obj = SystemManager(bus, OBJ_NAME)
    mainloop = gobject.MainLoop()
    obj.unmask_signals()
    name = dbus.service.BusName(DBUS_NAME, bus)

    print "Running SystemManager"
    mainloop.run()
