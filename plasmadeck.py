

import enum
import functools
from io import BytesIO
import os
import tempfile
from typing import Any, Callable, NewType
import typing

from dbus_next.message import Message
import StreamDeck
from StreamDeck.DeviceManager import DeviceManager
from PIL import Image, ImageDraw, ImageFont
from dbus_next.aio import MessageBus
from dbus_next.service import (ServiceInterface,
                               method, dbus_property, signal as dbus_signal)
from dbus_next import Variant, DBusError
from dataclasses import dataclass
import asyncio
import signal

import gi
gi.require_version("Gtk", "4.0")
try:
    gi.require_foreign("cairo")
except ImportError:
    print("No pycairo integration :(")
    exit(1)
from gi.repository import Gtk, Gdk
import cairo
import cairosvg
from StreamDeck.ImageHelpers import PILHelper
from desktop_parser import DesktopFile

# DBus Type Defs
i = NewType('i', int)
y = NewType('y', int)
h = NewType('h', int)
s = NewType('s', str)
NewType('(yb)', typing.Tuple[int, bool])

class ImageFileFormat(enum.Enum):
    PNG = 0
    JPG = 1

class StreamDeckInterface(ServiceInterface):
    def __init__(self, deck):
        super().__init__('net.shehadeh.StreamDeck')
        self.deck = deck
        self.font = ImageFont.truetype("/usr/share/fonts/noto/NotoSans-Regular.ttf", 14)
        self.deck.set_key_callback(lambda _deck, key, state: self.StateChange(key, state))

    @method()
    def SetImage(self, key: 'i', image_file_path: 's'):
        try:
            file_stream = open(image_file_path, mode="br")
        except Exception as e:
            raise DBusError('net.shehadeh.error.InvalidFileDescriptor',
                            f'failed to open file descriptor: {e}')

        icon = None
        try:
            icon = Image.open(file_stream)
        except Exception as e:
            raise DBusError('net.shehadeh.error.InvalidImage',
                            f'failed to decode image: {e}')
        try:
            key_image = PILHelper.create_scaled_key_image(self.deck, icon, margins=[0, 0, 0, 0])
            self.deck.set_key_image(key, PILHelper.to_native_key_format(self.deck, key_image))
        except Exception as e:
            raise DBusError('net.shehadeh.error.InvalidImage',
                            f'failed to set key image: {e}')

    @method()
    def SetText(self, key: 'i', text: 's'):
        try:
            icon = PILHelper.create_key_image(self.deck)
            draw = ImageDraw.Draw(icon)
            draw.text((icon.width / 2, icon.height / 2), text=text, font=self.font, anchor="ms", fill="white")

            key_image = PILHelper.create_scaled_key_image(self.deck, icon, margins=[0, 0, 0, 0])
            self.deck.set_key_image(key, PILHelper.to_native_key_format(self.deck, key_image))
        except Exception as e:
            raise DBusError('net.shehadeh.error.InvalidImage',
                            f'failed to set key image: {e}')


    @dbus_signal()
    def StateChange(self, key: int, pressed: bool) -> '(ib)':
        return [key, pressed]

def desktop_file_from_wm_class(wm_class):
    desktop_locations = [
        "/usr/share/applications/"
    ]
    
    for desktop_file_dir in desktop_locations:
        path = os.path.join(desktop_file_dir, f"{wm_class}.desktop")
        if os.path.exists(path):
            file = DesktopFile.from_file(path)
            if "Icon" in file.data["Desktop Entry"]:
                return file.data["Desktop Entry"]["Icon"]
            else:
                return None
    return None

def get_icon_path_by_wm_class(wm_class):
    theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
    icon = desktop_file_from_wm_class(wm_class)
    if icon is None:
        return None
    for resolution in [16, 20, 22, 24, 28, 32, 36, 48, 64, 72, 96, 128, 192, 256, 480, 512, 1024]:
        icon = theme.lookup_icon(icon, [], resolution, 1, 0, 0)
        if icon:
            return icon
    return None

def run_in_executor(f):
    @functools.wraps(f)
    def inner(*args, **kwargs):
        loop = asyncio.get_running_loop()
        return loop.run_in_executor(None, functools.partial(f, *args, **kwargs))

    return inner

@dataclass
class WindowData:
    uuid: str
    caption: str
    resourceClass: str

def to_pil(surface: cairo.ImageSurface) -> Image.Image:
    format = surface.get_format()
    size = (surface.get_width(), surface.get_height())
    stride = surface.get_stride()

    with surface.get_data() as memory:
        if format == cairo.Format.RGB24:
            return Image.frombuffer(
                "RGB", size, memory.tobytes(),
                'raw', "BGRX", stride)
        elif format == cairo.Format.ARGB32:
            return Image.frombuffer(
                "RGBA", size, memory.tobytes(),
                'raw', "BGRa", stride)
        else:
            raise NotImplementedError(repr(format))


class LoadedKWinScript:
    _script_id: int
    _filename: str
    _proxy_obj: Any
    _interface: Any
    def __init__(self, id: int, filename: str, introsepct, bus) -> None:
        self._script_id = id
        self._filename = filename
        self._proxy_obj = bus.get_proxy_object("org.kde.KWin", "/Scripting/Script" + str(id), introsepct)
        self._interface = self._proxy_obj.get_interface('org.kde.kwin.Script')
    

    async def run(self) -> None:
        await self._interface.call_run()

    async def stop(self) -> None:
        await self._interface.call_stop()

class KWinScriptRunner:
    def __init__(self, bus, introspect) -> None:
        self.bus = bus
        self.kwin_proxy = self.bus.get_proxy_object("org.kde.KWin", "/Scripting", introspect)
        self.scripting = self.kwin_proxy.get_interface('org.kde.kwin.Scripting')
    
    @classmethod
    async def create(cls, bus = None):
        bus = (await MessageBus().connect()) if bus is None else bus
        introspection = await bus.introspect("org.kde.KWin", "/Scripting")
        return KWinScriptRunner(bus, introspection)
    
    async def load(self, script: str) -> LoadedKWinScript:
        (fd, filename) = tempfile.mkstemp(text=True)
        script_bytes = script.encode("utf-8")
        written_len = os.write(fd, script_bytes)
        assert len(script_bytes) == written_len

        script_id = await self.scripting.call_load_script(filename)
        os.close(fd)
        print(f"Created Script #{script_id}: '{filename}'")
        introspection = await self.bus.introspect("org.kde.KWin", "/Scripting/Script" + str(script_id))
        return LoadedKWinScript(script_id, filename, introspection, self.bus)
    
    async def unload(self, script: LoadedKWinScript) -> None:
        await self.scripting.call_unload_script(script._filename)
        os.remove(script._filename)

DBUS_ADDR="net.shehadeh.PlasmaDeckWindowListener"
DBUS_OBJ="/net/shehadeh/PlasmaDeckWindowListener"
DBUS_IFACE="net.shehadeh.PlasmaDeckWindowListener"

def js_call_dbus(*args):
    return f"callDBus(\"{DBUS_ADDR}\", \"{DBUS_OBJ}\", \"{DBUS_IFACE}\", {",".join(args)})"

log = """
function log(msg) {
    console.log('PlasmaDeckWindowListener', msg);
    """ + js_call_dbus("'Log'", "msg.toString()") + """
}
"""
class PlasmaDeckWindowListener(ServiceInterface):
    windows: dict[str, WindowData]
    slots: list[None | str]
    runner: KWinScriptRunner

    def __init__(self, deck, runner):
        super().__init__('net.shehadeh.PlasmaDeckWindowListener')
        self.windows = {}
        self.deck = deck
        self.deck.set_key_callback_async(self.handle_press())
        self.runner = runner
        self.slots = [None, None, None, None, None, None, None, None]

    def handle_press(self):
        async def _cb(deck, key, state):
            script = log + """
                for(const win of workspace.windowList()) {
                    log(win.internalId.toString() + " == " + '""" + self.slots[key] + """');
                    if (win.internalId.toString() == '""" + self.slots[key] + """')
                        workspace.activeWindow = win
                }
            """
            if state:
                script_obj = await self.runner.load(script)
                await script_obj.run()
        return _cb

    @method()
    def Log(self, msg: 's'):
        print("SCRIPT: ", msg)

    @method()
    def WindowAdded(self, uuid: 's', caption: 's', resourceClass: 's'):
        print("Add", uuid)
        icon = get_icon_path_by_wm_class(resourceClass)
        if icon is not None:
            icon_path = icon.get_file().get_path()
            img = None
            if icon_path.endswith(".svg"):
                png = cairosvg.svg2png(url=icon_path)
                img = Image.open(BytesIO(png))
            else:
                img = Image.open(icon_path)
            scale_img = PILHelper.create_scaled_key_image(self.deck, img, margins=[0, 0, 0, 0])
            native_img = PILHelper.to_native_key_format(self.deck, scale_img)
            my_key = None
            for key, win in enumerate(self.slots):
                if win is None:
                    my_key = key
                    self.slots[my_key] = uuid
                    break
            if my_key is not None:
                self.deck.set_key_image(my_key, native_img)
        self.windows[uuid] = WindowData(uuid, caption, resourceClass)

    @method()
    def WindowRemoved(self, uuid: 's'):
        del self.windows[uuid]
        for key, win in enumerate(self.slots):
            if win == uuid:
                self.slots[key] = None
                self.deck.set_key_image(key, None)


SCRIPT = log +"""
function add(window) {
  try {
    log("ADD [enter] caption='" + window.caption + "', resourceClass=" + window.resourceClass);
    """ + js_call_dbus("'WindowAdded'", "window.internalId.toString()", "window.caption", "window.resourceClass") +"""
    log("ADD [exit] caption='" + window.caption + "', resourceClass=" + window.resourceClass);
  } catch(e) {
    log("ADD [error] caption='" + window.caption + "', resourceClass=" + window.resourceClass + ", error=" + e.toString());
  }
}

function remove(window) {
  try {
    log("REMOVE [enter] caption='" + window.caption + "', resourceClass=" + window.resourceClass);
    """ + js_call_dbus("'WindowRemoved'", "window.internalId.toString()", "window.caption", "window.resourceClass") +"""
    log("REMOVE [exit] caption='" + window.caption + "', resourceClass=" + window.resourceClass);
  } catch(e) {
    log("REMOVE [error] caption='" + window.caption + "', resourceClass=" + window.resourceClass + ", error=" + e.toString());
  }
}

log("INIT")

for (const window of workspace.windowList()) {
  add(window)
}

workspace.windowAdded.connect(add)
workspace.windowRemoved.connect(remove)
"""
    
async def main():
    bus = await MessageBus().connect()
    script_runner = await KWinScriptRunner.create(bus)
    streamdecks = DeviceManager().enumerate()
    print(f"Found {len(streamdecks)} Stream Deck devices")

    for index, deck in enumerate(streamdecks):
        # This example only works with devices that have screens.
        if not deck.is_visual():
            continue

        deck.open()
        deck.reset()

        print("Opened '{}' device (serial number: '{}', fw: '{}')".format(
            deck.deck_type(), deck.get_serial_number(), deck.get_firmware_version()
        ))

        # Set initial screen brightness to 30%.
        deck.set_brightness(30)
        
        # deck_interface_name = deck.deck_type().replace(" ", "")

        # interface = StreamDeckInterface(deck)
    
        bus.export(f'/net/shehadeh/PlasmaDeckWindowListener', PlasmaDeckWindowListener(deck, script_runner))
    await bus.request_name('net.shehadeh.PlasmaDeckWindowListener')

    listener_script = await script_runner.load(SCRIPT)

    try:
        await listener_script.run()
        await bus.wait_for_disconnect()
    except asyncio.CancelledError:
        print("cleaning up")
        await listener_script.stop()

loop = asyncio.get_event_loop()

main_task = asyncio.ensure_future(main())
for signal in [signal.SIGINT, signal.SIGTERM]:
    loop.add_signal_handler(signal, main_task.cancel)
try:
    loop.run_until_complete(main_task)
finally:
    loop.close()

