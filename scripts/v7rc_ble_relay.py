#!/usr/bin/env python3
"""V7RC Bluetooth (BLE) -> UDP relay.

Advertises the Nordic UART Service that the V7RC app looks for in BLE mode.
Every command the app writes (e.g. "SRV1500150015001500#") is forwarded to the
v7rc_bridge ROS2 node on UDP 127.0.0.1:6188, so Wi-Fi and BLE share the same
mapping and failsafe stop. Runs on the host (BlueZ over D-Bus), needs root.
"""
import os
import socket
import sys

import dbus
import dbus.exceptions
import dbus.mainloop.glib
import dbus.service
from gi.repository import GLib

DEVICE_NAME = os.environ.get("V7RC_BLE_NAME", "UGV-Rover")  # name shown in the V7RC app
UDP_TARGET = ("127.0.0.1", 6188)

NUS_SERVICE = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
NUS_RX = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"  # phone -> robot
NUS_TX = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"  # robot -> phone (notify)

BLUEZ = "org.bluez"
ADAPTER_IFACE = "org.bluez.Adapter1"
GATT_MANAGER_IFACE = "org.bluez.GattManager1"
AD_MANAGER_IFACE = "org.bluez.LEAdvertisingManager1"
DBUS_PROPS = "org.freedesktop.DBus.Properties"
DBUS_OM = "org.freedesktop.DBus.ObjectManager"
GATT_SERVICE_IFACE = "org.bluez.GattService1"
GATT_CHRC_IFACE = "org.bluez.GattCharacteristic1"
AD_IFACE = "org.bluez.LEAdvertisement1"


class InvalidArgs(dbus.exceptions.DBusException):
    _dbus_error_name = "org.freedesktop.DBus.Error.InvalidArgs"


class Application(dbus.service.Object):
    def __init__(self, bus):
        self.path = "/v7rc"
        self.services = []
        super().__init__(bus, self.path)

    @dbus.service.method(DBUS_OM, out_signature="a{oa{sa{sv}}}")
    def GetManagedObjects(self):
        objs = {}
        for s in self.services:
            objs[s.get_path()] = s.get_properties()
            for c in s.characteristics:
                objs[c.get_path()] = c.get_properties()
        return objs

    def get_path(self):
        return dbus.ObjectPath(self.path)


class Service(dbus.service.Object):
    def __init__(self, bus, index, uuid):
        self.path = f"/v7rc/service{index}"
        self.uuid = uuid
        self.characteristics = []
        super().__init__(bus, self.path)

    def get_properties(self):
        return {GATT_SERVICE_IFACE: {
            "UUID": self.uuid,
            "Primary": True,
            "Characteristics": dbus.Array([c.get_path() for c in self.characteristics], signature="o"),
        }}

    def get_path(self):
        return dbus.ObjectPath(self.path)


class Characteristic(dbus.service.Object):
    def __init__(self, bus, index, uuid, flags, service):
        self.path = f"{service.path}/char{index}"
        self.uuid = uuid
        self.flags = flags
        self.service = service
        super().__init__(bus, self.path)

    def get_properties(self):
        return {GATT_CHRC_IFACE: {
            "Service": self.service.get_path(),
            "UUID": self.uuid,
            "Flags": self.flags,
        }}

    def get_path(self):
        return dbus.ObjectPath(self.path)

    @dbus.service.method(DBUS_PROPS, in_signature="s", out_signature="a{sv}")
    def GetAll(self, interface):
        if interface != GATT_CHRC_IFACE:
            raise InvalidArgs()
        return self.get_properties()[GATT_CHRC_IFACE]

    @dbus.service.method(GATT_CHRC_IFACE, in_signature="a{sv}", out_signature="ay")
    def ReadValue(self, options):
        return dbus.Array([], signature="y")

    @dbus.service.method(GATT_CHRC_IFACE, in_signature="aya{sv}")
    def WriteValue(self, value, options):
        pass

    @dbus.service.method(GATT_CHRC_IFACE)
    def StartNotify(self):
        pass

    @dbus.service.method(GATT_CHRC_IFACE)
    def StopNotify(self):
        pass


class RxCharacteristic(Characteristic):
    """Receives V7RC commands; a command may arrive split across writes, '#' ends it."""

    def __init__(self, bus, index, service):
        super().__init__(bus, index, NUS_RX, ["write", "write-without-response"], service)
        self.buf = b""
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.packets = 0

    @dbus.service.method(GATT_CHRC_IFACE, in_signature="aya{sv}")
    def WriteValue(self, value, options):
        self.buf += bytes(value)
        while b"#" in self.buf:
            cmd, self.buf = self.buf.split(b"#", 1)
            cmd = cmd.strip() + b"#"
            self.sock.sendto(cmd, UDP_TARGET)
            self.packets += 1
            if self.packets == 1:
                print(f"First V7RC command over BLE: {cmd!r}", flush=True)
        if len(self.buf) > 64:  # Garbage without a terminator
            self.buf = b""


class Advertisement(dbus.service.Object):
    def __init__(self, bus):
        self.path = "/v7rc/advertisement0"
        super().__init__(bus, self.path)

    def get_path(self):
        return dbus.ObjectPath(self.path)

    @dbus.service.method(DBUS_PROPS, in_signature="s", out_signature="a{sv}")
    def GetAll(self, interface):
        if interface != AD_IFACE:
            raise InvalidArgs()
        return {
            "Type": "peripheral",
            "ServiceUUIDs": dbus.Array([NUS_SERVICE], signature="s"),
            "LocalName": DEVICE_NAME,
            "Includes": dbus.Array(["tx-power"], signature="s"),
        }

    @dbus.service.method(AD_IFACE)
    def Release(self):
        pass


def find_adapter(bus):
    objs = dbus.Interface(bus.get_object(BLUEZ, "/"), DBUS_OM).GetManagedObjects()
    for path, ifaces in objs.items():
        if GATT_MANAGER_IFACE in ifaces and AD_MANAGER_IFACE in ifaces:
            return path
    return None


def main():
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()
    adapter_path = find_adapter(bus)
    if not adapter_path:
        sys.exit("No BLE adapter with GATT + advertising support found")

    adapter_props = dbus.Interface(bus.get_object(BLUEZ, adapter_path), DBUS_PROPS)
    adapter_props.Set(ADAPTER_IFACE, "Powered", dbus.Boolean(True))
    adapter_props.Set(ADAPTER_IFACE, "Alias", dbus.String(DEVICE_NAME))

    app = Application(bus)
    service = Service(bus, 0, NUS_SERVICE)
    service.characteristics.append(RxCharacteristic(bus, 0, service))
    service.characteristics.append(Characteristic(bus, 1, NUS_TX, ["notify"], service))
    app.services.append(service)
    adv = Advertisement(bus)

    loop = GLib.MainLoop()
    obj = bus.get_object(BLUEZ, adapter_path)

    def fail(what):
        return lambda e: (print(f"{what} failed: {e}", flush=True), loop.quit())

    dbus.Interface(obj, GATT_MANAGER_IFACE).RegisterApplication(
        app.get_path(), {},
        reply_handler=lambda: print("GATT Nordic UART service registered", flush=True),
        error_handler=fail("RegisterApplication"))
    dbus.Interface(obj, AD_MANAGER_IFACE).RegisterAdvertisement(
        adv.get_path(), {},
        reply_handler=lambda: print(f"Advertising as '{DEVICE_NAME}', relaying to UDP {UDP_TARGET}", flush=True),
        error_handler=fail("RegisterAdvertisement"))
    loop.run()
    sys.exit(1)  # Only reached on a registration failure; systemd restarts us


if __name__ == "__main__":
    main()
