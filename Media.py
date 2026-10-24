try:
    from Utils import *
except ModuleNotFoundError:
    try:
        from .Utils import *
    except ModuleNotFoundError:
        print("No Utils")
        import sys
        sys.exit()

try:
    from gi.repository import Gst
except:
    Gst = False
if not Gst:
    print("GStreamer not found")
    sys.exit()

class MediaPlayer(Gtk.Overlay):
    __gtype_name__ = "MediaPlayer"
    tracks = ["Video", "Audio", "Subtitle"]

    @GObject.Property(type=float, minimum=-4.0, default=1.0, maximum=4.0)
    def speed(self): return getattr(self, "_speed", 1.0)

    @speed.setter
    def speed(self, value):
        if value == 0.0: return
        self._speed = value
        self.pipeline.seek(self._speed, Gst.Format.TIME, Gst.SeekFlags.INSTANT_RATE_CHANGE, Gst.SeekType.NONE , -1, Gst.SeekType.NONE, -1)

    @GObject.Property(type=GObject.GType.from_name("gint64"), flags=GObject.ParamFlags.READABLE)
    def duration(self): return self._duration if getattr(self, "_duration", 0) > 1 else 0

    @GObject.Property(type=GObject.GType.from_name("gint64"), flags=GObject.ParamFlags.READWRITE)
    def position(self): return getattr(self, "_position", 0)

    @position.setter
    def position(self, timestamp):
        self._position = min(self.duration, max(0, timestamp))
        if self._position >= 0: self.pipeline.seek_simple(Gst.Format.TIME, Gst.SeekFlags.ACCURATE | Gst.SeekFlags.FLUSH, self._position)
        self.pipeline.sync_children_states()

    @GObject.Property(type=bool, default=False)
    def playing(self): return getattr(self, "_playing", False)

    @playing.setter
    def playing(self, value):
        if not self.pipeline: return
        self._playing = value
        state = Gst.State.PLAYING if value else Gst.State.PAUSED
        self.pipeline.set_state(state)
        self.pipeline.sync_children_states()

    pipeline = GObject.Property(type=Gst.Pipeline)

    volume = GObject.Property(type=float, minimum=0.0, default=0.8, maximum=1.0)
    video_props = ["gamma", "brightness", "contrast", "hue", "saturation"]
    subtitle_props = ["font-desc", "color", "silent", "draw-outline", "outline-color", "draw-shadow", "shaded-background", "shading-value", "scale-mode", "auto-resize", "deltay", "deltax", "halignment", "line-alignment", "valignment", "vertical-render", "wrap-mode", "x-absolute", "xpad", "xpos", "y-absolute", "ypad", "ypos"]

    actions = GObject.Property(type=Gio.SimpleActionGroup)
    streams = GObject.Property(type=object)
    decode_sinks = GObject.Property(type=object)

    toc = GObject.Property(type=Gst.Toc)

    video_sink = GObject.Property(type=Gst.Pad)
    audio_sink = GObject.Property(type=Gst.Pad)
    subtitle_sink = GObject.Property(type=Gst.Pad)

    block = GObject.Property(type=bool, default=False)

    seekbar = GObject.Property(type=MediaSeekbar)

    def __init__(self):
        picture = Gtk.Picture()
        super().__init__(css_name=self.__gtype_name__.lower(), focusable=True, child=Gtk.GraphicsOffload(child=picture))
        self.actions = Gio.SimpleActionGroup()
        self.insert_action_group(self.__gtype_name__.lower(), self.actions)
        r = "" if sys.platform == "linux" else "audioresample ! "
        elements = ""
        if Gst.ElementFactory.find("gamma"):
            elements += "gamma name=Gamma ! "
        if Gst.ElementFactory.find("videobalance"):
            elements += "videobalance name=Balance ! "
        self.pipeline = pipeline = Gst.parse_launch(
            "decodebin3 name=Decode async_handling=false message_forward=false "
            "urisourcebin name=VideoURI "
            "urisourcebin name=AudioURI "
            "urisourcebin name=SubtitleURI "
            f"textoverlay name=Video queue name=VideoQueue ! videoconvert ! {elements}gtk4paintablesink name=Paintable qos=false "
            "queue name=Subtitle "
            f"queue name=Audio ! {r}audioconvert ! volume name=Volume ! audioconvert ! autoaudiosink")
        self.pipeline.set_properties(async_handling=False, message_forward=False)
        for i in self.pipeline.children: setattr(self.pipeline, i.name.lower(), i)
        picture.set_paintable(self.pipeline.paintable.get_property("paintable"))
        for i in self.__class__.video_props:
            e = getattr(self.pipeline, i if i == "gamma" else "balance", None)
            if e: e.connect(f"notify::{i}", self.__class__.prop_flush, self)
        self.pipeline.video.set_property("wait-text", False)
        for i in self.__class__.subtitle_props:
            prop = self.pipeline.video.__class__.find_property(i)
            if hasattr(prop, "enum_class"):
                enum = self.__class__.get_enum(self.pipeline.video, i)
                for k, v in enum.items():
                    if v == self.pipeline.video.get_property(i):
                        v = get_variant(k)
                        a = Gio.SimpleAction.new_stateful(i, v.get_type(), v)
                        self.pipeline.video.bind_property(i, a, "state", SYNC_BIND, self.__class__.enum_bind, self.__class__.enum_bind)
                        self.add_action(a)
            elif type(prop.default_value) == bool:
                a = Gio.SimpleAction.new_stateful(i, None, get_variant(prop.default_value))
                self.pipeline.video.bind_property(i, a, "state", SYNC_BIND, variant_bind, unpack_bind)
                self.add_action(a)
            self.pipeline.video.connect(f"notify::{i}", self.__class__.prop_flush, self)
        self.pipeline.volume.bind_property("volume", self, "volume", SYNC_BIND)
        pipeline.get_bus().add_signal_watch()
        pipeline.get_bus().connect("message", self.__class__.message, self)
        self.video_sink = self.pipeline.video.get_static_pad("video_sink")
        self.audio_sink = self.pipeline.audio.get_static_pad("sink")
        for i in (self.video_sink, self.audio_sink): i.add_probe(Gst.PadProbeType.BUFFER, self.__class__.update_position, self)
        self.subtitle_sink = self.pipeline.subtitle.get_static_pad("sink")
        self.pipeline.video.get_static_pad("src").link(self.pipeline.videoqueue.get_static_pad("sink"))
        self.pipeline.subtitle.get_static_pad("src").link(self.pipeline.video.get_static_pad("text_sink"))
        variant = get_variant("0")
        a = Gio.SimpleAction.new_stateful("orientation", variant.get_type(), get_variant("Auto"))
        self.pipeline.paintable.get_property("paintable").bind_property("orientation", a, "state", SYNC_BIND, self.__class__.enum_bind, self.__class__.enum_bind)
        self.add_action(a)
        for i in self.__class__.tracks:
            u = getattr(self.pipeline, i.lower() + "uri")
            u.connect("pad-added", self.__class__.uri_pad_added, self)
            u.connect("source-setup", self.__class__.source_setup, self)
            a = Gio.SimpleAction.new_stateful(i.lower() + "-selected", variant.get_type(), variant)
            a.connect("notify::state", self.__class__.tracks_state, self)
            self.add_action(a)
        for i in ("add", "remove"):
            a = Gio.SimpleAction.new(f"{i}-uri", variant.get_type())
            a.connect("activate", self.__class__.action_change_uri, self)
            self.add_action(a)
        self.connect("notify::toc", self.__class__.toc_changed)
        self.decode_sinks = [self.pipeline.decode.request_pad_simple(f"sink_{n}") for n in range(3)]
        self.reset()

    @staticmethod
    def source_setup(b, source, self):
        if hasattr(self, "user_agent") and source and source.find_property("user-agent"): source.set_property("user-agent", self.user_agent)
        if hasattr(self, "headers") and source.find_property("extra-headers"):
            structure = Gst.Structure.new_empty("headers")
            for name, value in self.headers.items(): structure.set_value(name, get_variant(str(value)))
            source.set_property("extra-headers", structure)

    def play(self): setattr(self, "playing", True)
    def pause(self): setattr(self, "playing", False)
    def playpause(self): setattr(self, "playing", not self.playing)
    def backward(self): setattr(self, "position", self.position - (2 * 1e9))
    def forward(self): setattr(self, "position", self.position + (2 * 1e9))
    def page_backward(self): setattr(self, "position", self.position - (10 * 1e9))
    def page_forward(self): setattr(self, "position", self.position + (10 * 1e9))
    def set_volume(self, v): setattr(self, "volume", v)
    def volumeup(self): setattr(self, "volume", min(1.0, self.volume + 0.1))
    def volumedown(self): setattr(self, "volume", max(0.0, self.volume - 0.1))

    @staticmethod
    def decode_pad_added(decode, pad, self):
        if pad.direction != Gst.PadDirection.SRC: return
        stream = pad.get_stream()
        if not stream: return
        type = stream.get_stream_type()
        sink = None
        if type == Gst.StreamType.VIDEO:
            sink = "Video"
        elif type == Gst.StreamType.AUDIO:
            sink = "Audio"
        elif type == Gst.StreamType.TEXT:
            sink = "Subtitle"
        s = getattr(self, sink.lower() + "_sink") if sink else None
        if sink and not s.get_peer() == pad:
            self.force_unlink(s)
            if self.lookup_action(f"{sink.lower()}-selected").get_state().unpack().strip("'") != "0": pad.link(s)

    @staticmethod
    def action_change_uri(a, p, self):
        v = p.unpack().strip("'")
        if a.get_name().startswith("remove"): self.play_uri("", v)
        if a.get_name().startswith("add"):
            self.pause()
            group = Adw.PreferencesGroup()
            group.add(URIRow(multiple=False))
            p = Prompt(callback=self.__class__.do_add, confirm_callback=False, heading="Add URI", extra_child=group)
            p.player = self
            p.type = v
            p.present(self.get_root())

    @staticmethod
    def do_add(p, r):
        if r != "confirm":
            del p.player
            del p.type
            return
        uri = p.get_extra_child().get_row(0).get_text()
        p.player.play_uri(uri, p.type)
        p.player.play()
        p.player.position = p.player.position
        del p.player
        del p.type

    @staticmethod
    def get_enum(o, name):
        prop = o.__class__.find_property(name)
        if name == "scale-mode":
            enum = {"None": 0, "Par": 1, "Display": 2, "User": 3}
        elif name == "halignment":
            enum = {"Left": 0, "Center": 1, "Right": 2, "Absolute Position Clamped to Canvas": 4, "Absolute Position": 5}
        elif name == "line-alignment":
            enum = {"Left": 0, "Center": 1, "Right": 2}
        elif name == "valignment":
            enum = {"Baseline": 0, "Bottom": 1, "Top": 2, "Absolute Position Clamped to Canvas": 3, "Center": 4, "Absolute Position": 5}
        else:
            enum = get_enum(prop.enum_class)
        e = {}
        for k, v in enum.items():
            k = k.replace("_", " ").lower()
            for i in ("rotate", ):
                k = f" {i} ".join([it.strip() for it in k.split(i)]).strip()
            e[k.title()] = v
        return e

    @staticmethod
    def enum_bind(b, v):
        enum = MediaPlayer.get_enum(b.get_property("source"), b.get_source_property())
        if isinstance(v, int):
            for k, vv in enum.items():
                if v == vv: return get_variant(k)
        else:
            v = v.unpack()
            for k, vv in enum.items():
                if k == v: return vv

    def prop_flush(self, p=None, s=False):
        if s:
            self = s
        self.position = 0

    def check_eos(self, e, p, d):
        if e.is_set(): return
        if self.playing and self.position == p and self._duration == d: Task.invoke(self.eos)

    @staticmethod
    def update_position(pad, info, self):
        if pad == self.audio_sink and self.video_sink.is_linked(): return Gst.PadProbeReturn.OK
        try:
            p, op = info.get_buffer(), self.position
            self._position = p.pts
            if self._position > self._duration:
                self._duration = 0
                Task.invoke(self.do_update, cover=False)
            if self._position != op:
                if self.playing and pad == self.audio_sink:
                    per = (max(1, self._position) / max(1, self._duration)) * 100
                    if per >= 99.8:
                        previous = getattr(pad, "event", False)
                        if previous: previous.set()
                        pad.event = Event()
                        GLib.timeout_add(900, self.check_eos, *(pad.event, p.pts, self._duration))
                Task.invoke(self.notify, "position")
        except Exception as e: print(e)
        return Gst.PadProbeReturn.OK

    @staticmethod
    def tracks_state(a, s, self):
        if self.block: return
        self.send_streams(self.get_selected_streams())

    def play_uri(self, uri, type="Video"):
        if not type in self.__class__.tracks: return print("Player unknown type:", type)
        u = getattr(self.pipeline, type.lower() + "uri")
        u.set_state(Gst.State.NULL)
        if uri and not u.parent: self.pipeline.add(u)
        u.set_property("uri", uri)
        if not uri and u.parent:
            self.reset_sink(type)
            self.pipeline.remove(u)
        if uri: u.set_state(Gst.State.PLAYING)
        if not any(getattr(self.pipeline, i.lower() + "uri").get_property("uri") for i in self.__class__.tracks): self.reset()
        else: self.notify("streams")

    def reset_sink(self, type):
        n = self.__class__.tracks.index(type)
        self.force_unlink(self.decode_sinks[n])
        self.pipeline.decode.release_request_pad(self.decode_sinks[n])
        self.decode_sinks[n] = self.pipeline.decode.request_pad_simple(self.decode_sinks[n].name)

    @staticmethod
    def uri_pad_added(sourcebin, pad, self):
        n = self.__class__.tracks.index(sourcebin.name.strip("URI"))
        pad.link(self.decode_sinks[n])

    def reset(self):
        self._playing = False
        self.notify("playing")
        self.block = True
        self.pipeline.set_state(Gst.State.NULL)
        self.pipeline.sync_children_states()
        self._duration = 0
        self.notify("duration")
        self.streams = dict((i, []) for i in self.__class__.tracks)
        variant = get_variant("0")
        for i in self.__class__.tracks:
            self.force_unlink(getattr(self, i.lower() + "_sink"))
            self.block = True
            self.lookup_action(i.lower() + "-selected").set_state(variant)
            self.block = False
            uri = getattr(self.pipeline, i.lower() + "uri")
            uri.set_property("uri", None)
            uri.set_state(Gst.State.NULL)
            self.pipeline.remove(uri)
            self.reset_sink(i)
        self._position = 0
        self.pipeline.seek_simple(Gst.Format.TIME, Gst.SeekFlags.ACCURATE | Gst.SeekFlags.FLUSH, 0)
        self.pipeline.sync_children_states()
        self.toc = None
        self.block = False

    def add_action(self, action): self.actions.add_action(action)

    def lookup_action(self, name): return self.actions.lookup_action(name)

    @staticmethod
    def message(bus, message, self):
        t = Gst.message_type_get_name(message.type)
        if not self.block and t == "toc" and self.seekbar:
            v = next(iter(message.parse_toc()), None)
            if v != self.toc:
                self.toc = v
        if t == "stream-collection": Task.invoke(self.parse_collection, message.parse_stream_collection())
        if t in ("streams-selected", "duration-changed") and self.tracks: Task.invoke(self.do_update)
        if t == "eos": Task.invoke(self.eos)

    def eos(self): pass

    def parse_collection(self, col):
        self.toc = None
        self.streams = dict((i, []) for i in self.__class__.tracks)
        for n in range(col.get_size()):
            stream = col.get_stream(n)
            type = stream.get_stream_type()
            if type == Gst.StreamType.VIDEO and not stream in self.streams["Video"]: self.streams["Video"].append(stream)
            elif type == Gst.StreamType.AUDIO and not stream in self.streams["Audio"]: self.streams["Audio"].append(stream)
            elif type == Gst.StreamType.TEXT and not stream in self.streams["Subtitle"]: self.streams["Subtitle"].append(stream)
        self.notify("streams")
        self.parse_select_streams()

    def toc_changed(self, p):
        if self.toc and self.seekbar:
            for entry in self.toc.get_entries(): Task.invoke(self.walk_entry, entry)
        elif self.seekbar: Task.invoke(self.seekbar.clear_marks)

    def walk_entry(self, entry):
        if entry.get_entry_type() == Gst.TocEntryType.CHAPTER:
            uid = entry.get_uid()
            success, start, stop = entry.get_start_stop_times()
            t = entry.get_tags()
            title = t.get_string("title") if t else None
            Task.invoke(self.seekbar.add_mark, start, Gtk.PositionType.BOTTOM, title[1] if title and title[0] else None)
        for sub in entry.get_sub_entries(): Task.invoke(self.walk_entry, sub)

    def do_update(self):
        self._duration = self.pipeline.decode.query_duration(Gst.Format.TIME)[-1]
        self.notify("duration")

    @staticmethod
    def stream_get_title(stream):
        if isinstance(stream, Gst.Pad):
            stream = stream.get_stream()
        if not stream: return ""
        tags = stream.get_tags()
        title = tags.get_string("title") if tags else ""
        return title[1] if title and title[0] else ""

    def get_selected_streams(self):
        streams = []
        for i in self.__class__.tracks:
            n = int(self.lookup_action(f"{i.lower()}-selected").get_state().unpack())
            if not n: continue
            streams.append(self.streams[i][n - 1])
        return streams

    def parse_select_streams(self):
        self.position = 0
        streams = []
        for i in self.__class__.tracks:
            v = next(iter(self.streams[i]), False)
            if v:
                n = self.streams[i].index(v)
                self.block = True
                self.lookup_action(f"{i.lower()}-selected").set_state(get_variant(str(n + 1)))
                self.block = False
                streams.append(v)
            else:
                self.block = True
                self.lookup_action(f"{i.lower()}-selected").set_state(get_variant("0"))
                self.block = False
        self.send_streams(streams)

    def send_streams(self, streams):
        state = self.playing
        self.pause()
        self.pipeline.sync_children_states()
        for i in self.__class__.tracks:
            v = next((it for it in streams if it in self.streams[i]), False)
            if v:
                n = self.streams[i].index(v)
                self.block = True
                self.lookup_action(f"{i.lower()}-selected").set_state(get_variant(str(n + 1)))
                self.block = False
            else:
                self.force_unlink(getattr(self, i.lower() + "_sink"))
        self.pipeline.send_event(Gst.Event.new_select_streams([i.get_stream_id() for i in streams]))
        for i in self.pipeline.decode.pads: self.decode_pad_added(self.pipeline.decode, i, self)
        Task.invoke(self.set_property, "playing", state)
        Task.invoke(self.prop_flush)

    @staticmethod
    def force_unlink(sink):
        peer = sink.get_peer()
        if peer: peer.unlink(sink)

class MediaPlayerOptions(Gtk.MenuButton):
    __gtype_name__ = "MediaPlayerOptions"
    video_options = GObject.Property(type=object)
    audio_options = GObject.Property(type=object)
    subtitle_options = GObject.Property(type=object)
    options = GObject.Property(type=object)
    widgets = GObject.Property(type=object)
    player_action = GObject.Property(type=str)
    def __init__(self, player):
        popover = Gtk.PopoverMenu(autohide=False)
        super().__init__(icon_name="open-menu-symbolic", tooltip_text="Options", css_classes=["flat"], popover=popover)
        self.player_action = player.__gtype_name__.lower()
        self.video_options = {
            "Orientation": dict((i, f"{self.player_action}.orientation::{i}") for i in player.__class__.get_enum(player.pipeline.paintable.get_property("paintable"), "orientation"))
        }
        self.audio_options = {
            "Volume": "custom::volume"
        }
        self.options = {
            "Speed": "custom::speed"
        }
        self.widgets = []
        for i in player.__class__.video_props:
            title = i.title()
            if i == "gamma":
                if not getattr(player.pipeline, "gamma", False): continue
                self.widgets.append(MenuSpinRow(player.pipeline.gamma, title, i, step_increment=0.1))
            else:
                if not getattr(player.pipeline, "balance", False): continue
                self.widgets.append(MenuSpinRow(player.pipeline.balance, title, i, step_increment=0.05))
            self.video_options[title] = f"custom::{i}"
        for i in ["Volume", "Speed"]: self.widgets.append(MenuSpinRow(player, i, i.lower(), step_increment=0.1 if i == "Gamma" else 0.05))
        self.subtitle_options = {}
        for i in player.__class__.subtitle_props:
            title = i.replace("-", " ").title()
            name = i.replace("-", "_")
            if i.startswith("delta"):
                title = "Delta " + i.strip("delta").upper()
            if len(i) == 4 and i.startswith(("x", "y")):
                title = i[0].upper() + " " + i.strip("xy").title()
            if i.startswith(("h", "v")) and i.endswith("alignment"):
                title = i.strip("alignment").upper() + " Alignment"
            if i in ("scale-mode", "halignment"):
                prop = self
                self.enum_class = None
            else:
                prop = player.pipeline.video.__class__.find_property(i)
            if name in ("color", "outline_color"):
                box = MenuColorRow(title, name)
                player.pipeline.video.bind_property(i, box.get_last_child(), "rgba", SYNC_BIND, self.__class__.color_bind, self.__class__.color_bind)
                self.widgets.append(box)
                self.subtitle_options[title] = f"custom::{name}"
            elif name == "font_desc":
                box = MenuFontRow("Font", name)
                player.pipeline.video.bind_property("font-desc", box.get_last_child(), "font-desc", SYNC_BIND, self.__class__.font_bind, self.__class__.font_bind)
                self.widgets.append(box)
                self.subtitle_options[title] = f"custom::{name}"
            elif hasattr(prop, "enum_class"):
                self.subtitle_options[title] = dict((it, f"{self.player_action}.{i}::{it}") for it in player.__class__.get_enum(player.pipeline.video, i))
            elif type(prop.default_value) == int:
                self.widgets.append(MenuSpinRow(player.pipeline.video, title, i, step_increment=5))
                self.subtitle_options[title] = f"custom::{name}"
            elif type(prop.default_value) == float:
                self.widgets.append(MenuSpinRow(player.pipeline.video, title, i, step_increment=0.5))
                self.subtitle_options[title] = f"custom::{name}"
            elif type(prop.default_value) == bool:
                self.subtitle_options[title] = f"{self.player_action}.{i}"
        self.setup_options()
        popover.connect("notify::menu-model", self.__class__.model_changed)
        player.bind_property("streams", popover, "menu-model", DEFAULT_BIND, self.__class__.do_menu)

    def setup_options(self): pass

    @staticmethod
    def color_bind(b, v):
        if isinstance(v, Gdk.RGBA):
            r = ((int(v.alpha * 255) & 0xFF) << 24) | ((int(v.red * 255) & 0xFF) << 16) | ((int(v.green * 255) & 0xFF) << 8) | (int(v.blue * 255) & 0xFF)
        else:
            argb = v & 0xFFFFFFFF
            argb = str(hex(argb)).split("x", 1)[-1]
            r = RGBA("#" + argb[2:] + argb[:2])
        return r

    @staticmethod
    def font_bind(b, v):
        if isinstance(v, str) and v: return Pango.FontDescription.from_string(v)
        return v.to_string() if v else None

    @staticmethod
    def model_changed(p, s):
        self = p.get_ancestor(MediaPlayerOptions)
        if not self: return
        for i in self.widgets:
            if i.get_parent(): p.remove_child(i)
            p.add_child(i, i.get_name())

    @staticmethod
    def do_menu(b, v):
        self = b.get_property("target").get_ancestor(MediaPlayerOptions)
        player = b.get_property("source")
        m = {}
        c = MediaPlayer
        for i in c.tracks:
            tracks = {"None": f"{self.player_action}.{i.lower()}-selected::0" }
            for n, s in enumerate(v[i]):
                tracks[c.stream_get_title(s) or f"Track {n + 1}".strip()] = f"{self.player_action}.{i.lower()}-selected::{n + 1}"
            o = getattr(self, i.lower() + "_options", {})
            m[i] = { "!t": Menu(tracks) }
            r = "Remove" if getattr(player.pipeline, i.lower() + "uri").get_property("uri") else "Add"
            m[i]["!a"] = Menu({f"{r} URI": f"{self.player_action}.{r.lower()}-uri::{i}"})
            if o:
                m[i]["!o"] = Menu(o)
        if self.options:
            m["!a"] = Menu(self.options)
        return Menu(m)

class ShuffleButton(Gtk.ToggleButton):
    __gtype_name__ = "Shuffle"
    def __init__(self, playlist):
        super().__init__(icon_name="media-playlist-shuffle", tooltip_text="Shuffle")
        playlist.bind_property("shuffle", self, "active", SYNC_BIND)
        self.add_css_class("shufflebutton")

class LoopButton(Gtk.Button):
    __gtype_name__ = "Loop"

    loops = { "None": "consecutive", "Track": "repeat-song", "Playlist": "repeat" }

    def __init__(self, playlist):
        super().__init__()
        self.connect("clicked", self.__class__.clicked)
        playlist.bind_property("loop", self, "tooltip-text", DEFAULT_BIND)
        self.bind_property("tooltip-text", self, "icon-name", DEFAULT_BIND, self.__class__.icon_bind)
        self.add_css_class("loopbutton")

    @staticmethod
    def icon_bind(b, v): return "media-playlist-" + LoopButton.loops.get(v, "consecutive")

    @staticmethod
    def clicked(b):
        v = b.get_root().playlist.loop
        b.get_root().playlist.loop = "Track" if v == "None" else "Playlist" if v == "Track" else "None"

class FullscreenButton(Gtk.Button):
    __gtype_name__ = "Fullscreen"
    def __init__(self, player):
        super().__init__()
        self.connect("clicked", self.__class__.clicked)
        player.bind_property("fullscreen", self, "tooltip-text", DEFAULT_BIND, self.__class__.tooltip_bind)
        self.bind_property("tooltip-text", self, "icon-name", DEFAULT_BIND, self.__class__.icon_bind)
        self.add_css_class("fullscreenbutton")

    @staticmethod
    def icon_bind(b, v): return "view-restore-symbolic" if v == "Restore" else "view-fullscreen-symbolic"
    @staticmethod
    def tooltip_bind(b, v): return "Restore" if v else "Fullscreen"

    def clicked(b):
        player = b if isinstance(b, Player) else b.get_ancestor(Player)
        player.fullscreen = not player.fullscreen

class PreviousButton(Gtk.Button):
    __gtype_name__ = "Previous"
    def __init__(self, playlist):
        super().__init__(icon_name="media-skip-backward", tooltip_text=self.__gtype_name__)
        self.add_css_class("previousbutton")
        self.connect("clicked", playlist.__class__.previous)
        playlist.bind_property(f"can-go-{self.__gtype_name__.lower()}", self, "sensitive", DEFAULT_BIND)

class NextButton(Gtk.Button):
    __gtype_name__ = "Next"
    def __init__(self, playlist):
        super().__init__(icon_name="media-skip-forward", tooltip_text=self.__gtype_name__)
        self.add_css_class("nextbutton")
        self.connect("clicked", playlist.__class__.next)
        playlist.bind_property(f"can-go-{self.__gtype_name__.lower()}", self, "sensitive", DEFAULT_BIND)

class OptionsButton(MediaPlayerOptions):
    __gtype_name__ = "Options"
    def __init__(self, player):
        super().__init__(player)
        self.add_css_class("optionsbutton")

    def setup_options(self):
        del self.audio_options["Volume"]
        self.options = {}

class Controls(Adw.BreakpointBin):
    __gtype_name__ = "Controls"
    seekbar = GObject.Property(type=MediaSeekbar)
    volume = GObject.Property(type=VolumeButton)
    speed = GObject.Property(type=SpeedButton)
    shuffle = GObject.Property(type=ShuffleButton)
    loop = GObject.Property(type=LoopButton)
    previous = GObject.Property(type=PreviousButton)
    next = GObject.Property(type=NextButton)
    fullscreen = GObject.Property(type=FullscreenButton)
    playback = GObject.Property(type=PlaybackButton)
    options = GObject.Property(type=OptionsButton)
    def __init__(self, player, window):
        controls_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        super().__init__(css_name="controls", child=controls_box, height_request=1, width_request=1, valign=Gtk.Align.END)
        self.seekbar = MediaSeekbar(player)
        player.seekbar = self.seekbar
        self.volume, self.speed  = VolumeButton(player), SpeedButton(player)
        self.previous, self.playback, self.next = PreviousButton(window.playlist), PlaybackButton(player), NextButton(window.playlist)
        self.shuffle, self.loop, self.fullscreen = ShuffleButton(window.playlist), LoopButton(window.playlist), FullscreenButton(player)
        player.add_overlay(self)
        start_box, center_box, end_box = [Gtk.Box(valign=Gtk.Align.END) for _ in range(3)]
        breakpoints = [Adw.Breakpoint.new(Adw.BreakpointCondition.new_length(Adw.BreakpointConditionLengthType.MAX_WIDTH, 700, Adw.LengthUnit.PX))]
        for i in (start_box, end_box): breakpoints[0].add_setter(i, "orientation", Gtk.Orientation.VERTICAL)
        buttons_box = Gtk.CenterBox(css_name="centerbox", vexpand=True, hexpand=True, start_widget=start_box, center_widget=center_box, end_widget=end_box)
        for i in breakpoints: self.add_breakpoint(i)
        self.options = OptionsButton(player)
        for i in (self.volume, self.speed, self.options):
            i.set_properties(halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
            i.add_css_class("flat")
            start_box.append(i)
        for i in (self.previous, self.playback, self.next):
            i.set_properties(halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
            i.add_css_class("flat")
            center_box.append(i)
        for i in (self.shuffle, self.loop, self.fullscreen):
            i.set_properties(halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
            i.add_css_class("flat")
            end_box.append(i)
        for i in (buttons_box, self.seekbar): controls_box.append(i)

    @staticmethod
    def font_bind(b, v): return v.to_string() if v else None

    @staticmethod
    def offset_bind(b, v): return v * 1e9

class PlayerOverlay(Adw.Bin):
    __gtype_name__ = "PlayerOverlay"

    x = GObject.Property(type=int)
    y = GObject.Property(type=int)

    width = GObject.Property(type=int, default=200)
    height = GObject.Property(type=int, default=200)

    def __init__(self, window):
        super().__init__(css_name=self.__class__.__gtype_name__.lower(), overflow=Gtk.Overflow.HIDDEN)
        window.get_content().get_first_child().get_content().connect("get-child-position", self.__class__.get_child_position)
        window.get_content().get_first_child().get_content().add_overlay(self)
        Window.controller_init(self)

    @staticmethod
    def get_child_position(o, widget, r):
        if not isinstance(widget, PlayerOverlay): return False
        p_w, p_h = o.get_width(), o.get_height()
        r.width, r.height = min(o.get_root().get_width(), max(min(widget.width, p_w), 290)), min(o.get_root().get_height() - 100, max(min(widget.height, p_h), 150))
        r.x = min(max(widget.x, 0), p_w - r.width)
        r.y = min(max(widget.y, 0), p_h - r.height)
        return True

    @staticmethod
    def on_gesture_drag_drag_begin(e, x, y):
        if e.get_current_button() != 1: return
        self = e.get_widget()
        self.drag_start = (self.x, self.y, self.width, self.height)

    @staticmethod
    def on_gesture_drag_drag_update(e, x, y):
        if e.get_current_button() != 1: return e.reset()
        self = e.get_widget()
        self.dragging = True
        po = self
        o = po.get_parent()
        p = self.compute_point(o, Graphene.Point.init(Graphene.Point(), x, y))[-1]
        if self.corner == None:
            po.x, po.y = p.x, p.y
        else:
            if "w" in self.corner or "e" in self.corner:
                ox, oy, ow, oh = self.drag_start if "e" in self.corner else (self.x, self.y, self.width, self.height)
                if p.x > 0 and not ("e" in self.corner and 0 > o.get_width() - p.x - ow):
                    po.width = ow + (x if "e" in self.corner else -x)
                    if "w" in self.corner:
                        po.x -= po.width - ow
            if "n" in self.corner or "s" in self.corner:
                ox, oy, ow, oh = self.drag_start if "s" in self.corner else (self.x, self.y, self.width, self.height)
                if p.y > 0 and not ("s" in self.corner and 0 > o.get_height() - p.y - oh):
                    po.height = oh + (y if "s" in self.corner else -y)
                    if "n" in self.corner:
                        po.y -= po.height - oh
        o.queue_resize()

    @staticmethod
    def on_gesture_drag_drag_end(e, x, y):
        e.get_widget().dragging = False

    @staticmethod
    def on_event_motion(e, x, y):
        self = e.get_widget()
        if getattr(self, "dragging", False): return
        nn = 10
        vertical = "n" if y <= nn else "s" if y >= self.get_height() - nn else ""
        horizontal = "w" if x <= nn else "e" if x >= self.get_width() - nn else ""
        self.corner = vertical + horizontal or None
        self.set_cursor(Gdk.Cursor.new_from_name(f"{self.corner}-resize", None) if self.corner else None)

class Player(MediaPlayer):
    __gtype_name__ = "Player"

    controls = GObject.Property(type=Controls)
    cover = GObject.Property(type=Gdk.Paintable)
    spinner = GObject.Property(type=Gdk.Paintable)
    artists = GObject.Property(type=Adw.WrapBox)
    overlay = GObject.Property(type=PlayerOverlay)

    paintable = GObject.Property(type=Gdk.Paintable)

    @GObject.Property(type=str)
    def uri(self): return getattr(self, "_uri", "")

    @uri.setter
    def uri(self, arg):
        arg = arg or ""
        if arg == self.uri:
            arg = ""
        self._uri = arg
        if arg: self.update_cover(spinner=True)
        self.reset()
        Task(self.do_uri).start()

    @GObject.Property(type=bool, default=False)
    def fullscreen(self): return getattr(self, "_fullscreen", False)

    @fullscreen.setter
    def fullscreen(self, value):
        self._fullscreen = value if self.uri else False
        Task.invoke(self.do_set_fullscreen)

    def do_set_fullscreen(self):
        root = self.overlay.get_root()
        if not root: return
        self.small = bool(self.overlay.get_root().get_current_breakpoint())
        if self.fullscreen:
            self.was_maximized = root.is_maximized()
            if not self.was_maximized: root.maximize()
        elif not self.was_maximized: root.unmaximize()

    @GObject.Property(type=bool, default=False)
    def small(self): return getattr(self, "_small", False)

    @small.setter
    def small(self, value):
        if self.fullscreen:
            value = True
        self._small = value
        if self.small:
            if self.overlay.get_child(): self.overlay.set_child(None)
            if not self.window_handle.get_child() == self: self.window_handle.set_child(self)
            if not self.window_handle.get_parent(): self.overlay.get_root().get_content().append(self.window_handle)
        else:
            if self.window_handle.get_parent(): self.window_handle.unparent()
            if self.window_handle.get_child(): self.window_handle.set_child(None)
            if not self.overlay.get_child() == self: self.overlay.set_child(self)
        self.sync_visible()

    window_handle = GObject.Property(type=Gtk.WindowHandle)

    ignore_video = GObject.Property(type=bool, default=False)
    ignore_cover = GObject.Property(type=bool, default=False)
    ignore_audio = GObject.Property(type=bool, default=False)
    ignore_subtitle = GObject.Property(type=bool, default=False)

    task_ongoing = GObject.Property(type=bool, default=False)

    def __init__(self, window):
        self.was_maximized = window.is_maximized()
        MediaPlayer.__init__(self)
        self.window_handle = Gtk.WindowHandle()
        self.bind_property("fullscreen", self.window_handle, "vexpand", DEFAULT_BIND)
        self.bind_property("vexpand", self.window_handle, "valign", DEFAULT_BIND, self.__class__.window_handle_bind)
        self.spinner = Adw.SpinnerPaintable.new(self)
        self.paintable = self.pipeline.paintable.get_property("paintable")
        self.paintable.colors = []
        self.paintable.connect("invalidate-contents", self.__class__.invalidate_contents, self)
        self.artists = Adw.WrapBox(align=0.50, css_name="artists", valign=Gtk.Align.START, hexpand=True)
        self.add_overlay(self.artists)
        self.bind_property("fullscreen", window.get_content().get_first_child(), "visible", INVERT_BIND)
        self.overlay = PlayerOverlay(window)
        self.controls = Controls(self, window)
        self.bind_property("cover", self.get_child().get_child(), "paintable", DEFAULT_BIND)
        window.breakpoint.add_setter(self, "small", True)
        Window.controller_init(self)

    @staticmethod
    def window_handle_bind(b, v): Gtk.Align.CENTER if v else Gtk.Align.END

    @staticmethod
    def get_all():
        l = []
        for w in Gtk.Window.list_toplevels():
            if isinstance(w, Window): l.append(w.player)
        return l

    def do_uri(self):
        if hasattr(self, "user_agent"):
            del self.user_agent
        if hasattr(self, "headers"):
            del self.headers
        if not self.get_root(): return Task.invoke(self.do_update)
        if self.uri and not App.default.data.exists(table="Medias", conditions="URI = ?", conds=(self.uri,)): return Task.invoke(setattr, self, "uri", "")
        video = self.uri
        audio = ""
        Task.invoke(self.get_root().playlist.set_selected, video)
        if video:
            if yt_dlp and is_online(video):
                new = False
                if video in App.default.infos:
                    info = App.default.infos[video]
                else:
                    try:
                        command = f"{yt_dlp} {video}"
                        print(command)
                        result, output, error, wait = GLib.spawn_command_line_sync(command)
                        info = json(output.decode())
                        if info:
                            App.default.infos[video] = info
                            new = True
                        else:
                            Toast(f"yt-dlp failed: {error.decode()}", timeout=0)
                            print(error.decode())
                            video = "none"
                    except Exception as e:
                        try:
                            Toast(f"yt-dlp failed: {error.decode()} {e}", timeout=0)
                            print(error.decode(), e)
                        except:
                            Toast(f"yt-dlp failed: {e}", timeout=0)
                            print(e)
                        video = "none"
                if video != self.uri: return
                audio = next(reversed([i for i in (info.get("formats", []) if info else []) if i and "audio only" in i.get("format", "None") and i.get("url", "")]), {})
                video = next(reversed([i for i in (info.get("formats", []) if info else []) if i and i.get("vcodec", "none") != "none" and i.get("url", "")]), {})
                if (video or audio).get("http_headers", {}):
                    self.headers = (video or audio).get("http_headers", {})
                if hasattr(self, "headers") and self.headers.get("User-Agent", ""):
                    self.user_agent = self.headers.get("User-Agent", "")
                if video.get("acodec", "none") != "none":
                    audio = ""
                else:
                    audio = audio.get("url", "")
                video = video.get("url", "")
                if is_youtube(self.uri):
                    for i in (video, audio):
                        if i and "expire=" in i and GLib.DateTime.new_now_local().to_unix() >= int(i.split("expire=", 1)[-1].split("&", 1)[0]):
                            del App.default.infos[self.uri]
                            return Toast("YouTube Link Expired. Try Again")
                if self.ignore_video:
                    video = ""
            else:
                video = uri_relative_encode(video)
            Task.invoke(self.add_count)
        if video and audio:
            Task.invoke(self.play_uri, video, type="Video")
            Task.invoke(self.play_uri, audio, type="Audio")
        elif video: Task.invoke(self.play_uri, video, type="Video")
        elif audio: Task.invoke(self.play_uri, audio, type="Audio")
        else: Task.invoke(self.do_update)
        Task.invoke(self.play)

    def send_streams(self, streams):
        self.paintable.colors = []
        MediaPlayer.send_streams(self, streams)
        if not self.tracks: return

    def do_update(self, cover=True):
        if self.uri and self._duration == 0:
            self._duration = self.pipeline.decode.query_duration(Gst.Format.TIME)[-1]
        else:
            self._duration = 0
        Task.invoke(self.notify, "duration")
        if cover:
            Task.invoke(self.update)
            Task.invoke(self.update_cover)

    @staticmethod
    def on_scroll(e, x, y):
        self = e.get_widget()
        self.volume = float(max(0.0, min(self.volume + -y * 0.02, 1.0)))
        return True

    def parse_select_streams(self):
        self.position = 0
        streams = []
        for i in self.__class__.tracks:
            v = next(iter(self.streams[i]), False)
            if getattr(self, f"ignore_{i.lower()}", False):
                v = False
            if v:
                n = self.streams[i].index(v)
                self.block = True
                self.lookup_action(f"{i.lower()}-selected").set_state(get_variant(str(n + 1)))
                self.block = False
                streams.append(v)
            else:
                self.block = True
                self.lookup_action(f"{i.lower()}-selected").set_state(get_variant("0"))
                self.block = False
        self.send_streams(streams)

    def send_streams(self, streams):
        MediaPlayer.send_streams(self, streams)
        Task.invoke(self.update_cover)

    def eos(self):
        if self.get_root().playlist.loop == "Track":
            self.position = 1
            self.play()
            self.position = 0
            self.add_count()
        else: self.get_root().playlist.next()

    def add_count(self):
        item = App.default.medias[self.uri]
        if item:
            item.use_count += 1

    def update(self, *_):
        if not self.get_root() or not self.get_root().player: return False
        self.get_root().playlist.update()
        for k, v in App.default.MPRIS.interfaces.items():
            if v.get("properties", False): App.default.get_dbus_connection().emit_signal(None, "/org/mpris/MediaPlayer2", "org.freedesktop.DBus.Properties", "PropertiesChanged", GLib.Variant.new_tuple(GLib.Variant("s", k), call(App.default.MPRIS, None, None, "", k, "GetAll", (k,), None) , GLib.Variant("as", tuple())))
        return False

    def start_colors(self, cover=False):
        self.task_ongoing = True
        cover = cover if cover != False else self.cover
        if cover and self.get_root() and self.get_root().get_visible() and not isinstance(cover, Adw.SpinnerPaintable) and not isinstance(cover, Gtk.SymbolicPaintable) and not (hasattr(cover, "colors") and cover != self.paintable):
            cover.colors = []
            cover.colors = Palette.from_paintable(cover)
        Task.invoke(Palette.set_colors, cover)
        GLib.timeout_add(1000, self.set_property, *("task-ongoing", False))

    @staticmethod
    def invalidate_contents(p, self):
        if not self.task_ongoing and self.get_mapped() and self.get_root() and self.get_root().get_visible() and not "Empty" in p.__repr__() and self.cover == self.paintable and not "colored" in self.get_root().get_css_classes():
            if not self.paintable.colors: Task.invoke(setattr, self.paintable, "colors", True)
            if self.paintable.colors == True: Task(self.start_colors, self.paintable).start()

    def update_cover(self, spinner=False):
        cover = self.spinner if spinner else None
        if not cover and self.get_root() and self.uri:
            if self.lookup_action("video-selected").get_state().unpack() != "0":
                cover = self.paintable
            elif not self.ignore_cover:
                t = App.default.medias[self.uri]
                if t:
                    cover = self.get_root().get_application().get_cover(t.get_cover())
                    if cover == True:
                        cover = self.spinner
        if cover == None and self.uri:
            cover = DEFAULT_PAINTABLE
        if self.cover != cover: Task.invoke(self.set_property, "cover", cover)
        if cover == DEFAULT_PAINTABLE:
            Task.invoke(self.add_css_class, "no-cover")
            Task.invoke(Palette.set_colors, cover)
        else:
            Task.invoke(self.remove_css_class, "no-cover")
            if cover != self.paintable: Task(self.start_colors, cover).start()
        if self.overlay.get_mapped():
            if isinstance(cover, Gtk.SymbolicPaintable):
                self.overlay.width = self.overlay.height = min(self.overlay.get_root().get_width(), 300)
                Task.invoke(self.overlay.get_parent().queue_resize)
            if isinstance(cover, MediaStream):
                self.overlay.width, self.overlay.height = clamp_size(max(self.overlay.get_width(), self.overlay.get_height()), cover.get_intrinsic_width(), cover.get_intrinsic_height())
                Task.invoke(self.overlay.get_parent().queue_resize)
        Task.invoke(self.sync_visible)

    def sync_visible(self):
        self.set_visible(self.uri and self.cover)
        self.overlay.set_visible(self.get_visible() and not self.small)

class View(Adw.Bin):
    __gtype_name__ = "View"
    default = ["Artists", "Playlists", "Playlist"]
    search = GObject.Property(type=str)
    status = GObject.Property(type=Adw.StatusPage)
    header = GObject.Property(type=Adw.HeaderBar)
    has_search = GObject.Property(type=bool, default=True)

    def __init__(self, window, child=None, has_search=True, **kwargs):
        super().__init__(**kwargs)
        self.has_search = has_search
        self.status = Adw.StatusPage()
        self.set_status()
        self.overlay = Gtk.Overlay(child=child)
        self.set_child(self.overlay)
        self.overlay.add_overlay(self.status)

    def set_status(self, b=False, t="", i=""): self.status.set_properties(title=t, icon_name=i, visible=b)

    def do_search(w):
        if isinstance(w, View):
            self = w
        elif isinstance(w, Gtk.SearchEntry):
            self = w.get_root().stack.get_visible_child()
        else:
            self = w.get_ancestor(View)
        if not isinstance(self, View) or not self.has_search or not hasattr(self, "catalog") or not self.catalog: return
        self.current = []
        self.catalog.remove_all()
        window = self.get_root()
        t = self.search.lower()
        l = getattr(window.get_application(), self.get_name().lower(), [])
        for i in sorted(l, key=App.id_sort):
            if i.hidden and not window.show_hidden: continue
            if t and not t in getattr(i, "name", t).lower(): continue
            if t and not t in getattr(i, "artists", t).lower(): continue
            self.current.append(i)
        self.__class__.load_more(self, 3)

    @staticmethod
    def load_more(w, p):
        if p != 3: return
        self = w.get_ancestor(View)
        added = 0
        for i in [i for i in self.current]:
            self.current.remove(i)
            self.catalog.append(self.item(w.get_root(), i))
            added += 1
            if added == self.added_max: break
        self.get_root().status()

class SinglesItem(Gtk.Button):
    __gtype_name__ = "SinglesItem"
    filter = GObject.Property(type=Gtk.CustomFilter)
    def __init__(self, l, p=None, **kwargs):
        super().__init__(tooltip_text="Singles", child=Gtk.Picture(focusable=True, paintable=p or DEFAULT_PAINTABLE), overflow=Gtk.Overflow.HIDDEN, **kwargs)
        self.filter = Gtk.CustomFilter.new(self.__class__.do_filter, l)
        self.get_child().bind_property("paintable", self, "css-classes", DEFAULT_BIND, self.__class__.css_bind)

    @staticmethod
    def css_bind(b, v): return ["flat", "artist"] + item_css_bind(b, v)

    @staticmethod
    def do_clicked(self):
        self.get_root().playlist.set_playlist(Playlist.default[0])
        self.get_root().playlist.filter.append(self.filter)

    @staticmethod
    def do_filter(media, l): return media in l

class ArtistViewRow(Gtk.Box):
    __gtype_name__ = "ArtistViewRow"
    def __init__(self, title="", children=None):
        super().__init__(css_name=self.__gtype_name__.lower(), orientation=Gtk.Orientation.VERTICAL)
        self.append(Gtk.Label(label=title, css_classes=["title-3"]))
        p = Adw.Carousel(spacing=10)
        if children:
            for i in children: p.append(i)
        self.append(Adw.Clamp(child=p, tightening_threshold=400, maximum_size=400, orientation=Gtk.Orientation.VERTICAL))

class ArtistView(View):
    __gtype_name__ = "ArtistView"
    prefix = "artist-"
    def __init__(self, window, artist):
        playlists = dict((i.name, i) for i in window.get_application().playlists if i.has_artist(artist.name))
        media = [i for i in window.get_application().medias if i.has_artist(artist.name)]
        other = []
        featured = []
        for i in media:
            p = [it.name for it in i.get_playlists()]
            in_playlists = any(it in playlists for it in p)
            for pi in p:
                if not pi in playlists and not pi in featured: featured.append(pi)
            if not in_playlists and not i in other: other.append(i)
        if playlists or other or featured:
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            child = Gtk.ScrolledWindow(child=box)
            if playlists or other:
                l = [PlaylistItem(window, i) for i in playlists.values()]
                if other: l.append(SinglesItem(other, artist.cover_paintable))
                box.append(ArtistViewRow("Playlists", l))
            if featured: box.append(ArtistViewRow("Featured", [PlaylistItem(window, window.get_application().playlists[i]) for i in featured]))
        else:
            child = Adw.StatusPage(icon_name="avatar-default-symbolic", title="Empty")
        View.__init__(self, window, child=child, css_name=self.__gtype_name__.lower(), has_search=False)
        self.set_name(artist.name)
        window.stack.add_titled_with_icon(self, self.__class__.prefix + self.get_name(), self.get_name(), "avatar-default-symbolic")
        window.stack.set_visible_child(self)

    @staticmethod
    def get_all():
        l = []
        for w in Gtk.Window.list_toplevels():
            if isinstance(w, Window):
                for i in w.stack:
                    if isinstance(i, ArtistView): l.append(i)
        return l

class Artist(ListObject):
    __gtype_name__ = "Artist"

    id = GObject.Property(type=int, nick="ID", default=0)

    @GObject.Property(type=str)
    def name(self): return getattr(self, "_name", "")

    @name.setter
    def name(self, value):
        old_name = self.name
        self._name = value
        if old_name:
            App.default.data.update(table=self.__class__.__gtype_name__ + "s", column="Name", value=self.name, conditions="Name = ?", conds=(old_name,))
            for l in (App.default.medias, App.default.playlists):
                for i in l:
                    if i.has_artist(old_name):
                        i.artists = list_to_str([self.name if i == old_name else i for i in str_to_list(i.artists)])
            Task.invoke(App.default.artists.do_dict)

    cover_paintable = GObject.Property(type=Gdk.Paintable)
    cover = GObject.Property(type=str)
    hidden = GObject.Property(type=bool, default=False)

    @staticmethod
    def arg_to_name(a):
        if isinstance(a, ArtistItem):
            a = a.item
        if isinstance(a, Artist):
            a = a.name
        return a

    @staticmethod
    def changed(self, p):
        if p.get_name() == "cover-paintable": return
        v = getattr(self, p.get_name(), None)
        p = p.get_nick() or p.get_name().replace("-", " ").title()
        t = self.__class__.__gtype_name__ + "s"
        c = "Name = ?"
        if p == "Cover":
            self.cover_paintable = None
        if p == "Hidden":
            for w in Gtk.Window.list_toplevels():
                if isinstance(w, Window) and not w.show_hidden:
                    view = w.stack.get_child_by_name("Artists")
                    if view.get_mapped() or view.catalog.get_first_child(): view.do_search()
        App.default.data.update(table=t, column=p, value=v, conditions=c, conds=(self.name,))

    def remove(self, files):
        if not isinstance(self, Artist):
            self = Artist.arg_to_name(self)
            self = App.default.artists[self]
        files = Media.arg_to_files(files)
        for i in files:
            t = App.default.medias.get(i, None)
            if t.has_artist(self.name):
                t.artists = list_to_str([it for it in str_to_list(t.artists) if not it == self.name])

    def add(self, files):
        if not isinstance(self, Artist):
            self = Artist.arg_to_name(self)
            self = App.default.artists[self]
        files = Media.arg_to_files(files)
        for i in files:
            t = App.default.medias.get(i, None)
            if not t.has_artist(self.name):
                t.artists = list_to_str(str_to_list(t.artists) + [self.name])

    def delete(self):
        if not isinstance(self, Artist):
            self = Artist.arg_to_name(self)
            self = App.default.artists[self]
        if self:
            App.default.artists.remove(self)
            App.default.data.delete(table="Artists", conditions="Name = ?", conds=(self.name,))

    @staticmethod
    def items_changed(l, *_):
        a = [i.name for i in l]
        for w in Gtk.Window.list_toplevels():
            if isinstance(w, Window):
                view = w.stack.get_child_by_name("Artists")
                if view.get_mapped() or view.catalog.get_first_child(): view.do_search()
        for i in ArtistView.get_all():
            if not i in a:
                stack = i.get_ancestor(Adw.ViewStack)
                stack.remove(i)
                stack.set_visible_child_name(View.default[-1])
        for l in (App.default.medias, App.default.playlists):
            for i in l:
                old = str_to_list(i.artists)
                new = list(old)
                for it in old:
                    if not it in a: new.remove(it)
                if old != new:
                    i.artists = list_to_str(new)

class ArtistItem(Gtk.Button):
    __gtype_name__ = "ArtistItem"
    item = GObject.Property(type=Artist)
    def __init__(self, window, i, small=False, **kwargs):
        super().__init__(overflow=Gtk.Overflow.HIDDEN, child=Adw.Avatar(show_initials=True, size=40 if small else 262), **kwargs)
        self.item = i
        self.item.bind_property("cover-paintable", self.get_child(), "custom-image", DEFAULT_BIND)
        self.get_child().bind_property("custom-image", self, "css-classes", DEFAULT_BIND, self.__class__.css_bind)
        self.item.bind_property("name", self, "name", DEFAULT_BIND)
        self.bind_property("name", self, "tooltip-text", DEFAULT_BIND)
        self.bind_property("name", self.get_child(), "text", DEFAULT_BIND)
        if not small: window.breakpoint.add_setter(self.get_child(), "size", 130)
        if not window.player.ignore_cover and not self.item.cover_paintable:
            cover = self.item.cover
            if cover:
                c = App.default.get_cover(cover)
                self.item.cover_paintable = Adw.SpinnerPaintable.new(self) if c == True else c
                if c == True:
                    self.item.cover_paintable.uri = cover

    @staticmethod
    def css_bind(b, v): return ["flat", "circular", "artist"] + item_css_bind(b, v)

    @staticmethod
    def do_clicked(self):
        name = ArtistView.prefix + self.item.name
        if self.get_root().stack.get_child_by_name(name): self.get_root().stack.set_visible_child_name(name)
        else: ArtistView(self.get_root(), self.item)

    @staticmethod
    def get_all():
        l = []
        for w in Gtk.Window.list_toplevels():
            if isinstance(w, Window):
                view = w.stack.get_child_by_name("Artists")
                for i in [i for i in view.catalog]: l.append(i)
        return l

class PlaylistView(View):
    __gtype_name__ = "PlaylistView"

    icon = GObject.Property(type=str)

    selection = GObject.Property(type=Gtk.MultiSelection)
    model = GObject.Property(type=Gio.ListModel)

    order = GObject.Property(type=object)
    playlist_filter = GObject.Property(type=Gtk.Filter)
    search_filter = GObject.Property(type=Gtk.AnyFilter)
    hidden_filter = GObject.Property(type=Gtk.BoolFilter)
    filter = GObject.Property(type=Gtk.EveryFilter)

    random_sorter = GObject.Property(type=Gtk.Sorter)
    playlist_sorter = GObject.Property(type=Gtk.Sorter)
    played_sorter = GObject.Property(type=Gtk.Sorter)
    id_sorter = GObject.Property(type=Gtk.Sorter)
    sorter = GObject.Property(type=Gtk.Sorter)

    can_go_previous = GObject.Property(type=bool, default=False)
    can_go_next = GObject.Property(type=bool, default=False)
    listview = GObject.Property(type=Gtk.ListView)
    spinner = GObject.Property(type=Adw.Spinner)
    playlist = GObject.Property(type=str, default="Playlist")

    loop = GObject.Property(type=str, default="None")

    @GObject.Property(type=bool, default=False)
    def shuffle(self): return getattr(self, "_shuffle", False)

    @shuffle.setter
    def shuffle(self, value):
        self._shuffle = value
        self.set_playlist(self.playlist)

    def __init__(self, window):
        listview = Gtk.ListView(css_classes=("navigation-sidebar",), model=Gtk.MultiSelection())
        View.__init__(self, window, child=Gtk.ScrolledWindow(child=listview))
        self.order = {}
        self.selection = listview.get_model()
        self.random_sorter = Gtk.CustomSorter()
        self.random_sorter.set_sort_func(return_random_negative_500)
        self.playlist_filter = Gtk.BoolFilter(expression=Gtk.ClosureExpression.new(bool, self.__class__.playlist_filter_expression, [Gtk.PropertyExpression.new(self.__class__, Gtk.ObjectExpression.new(self), "playlist")]))
        self.playlist_sorter = Gtk.NumericSorter(expression=Gtk.ClosureExpression.new(int, self.__class__.playlist_sort_expression, [Gtk.PropertyExpression.new(self.__class__, Gtk.ObjectExpression.new(self), "order")]))
        self.played_sorter = Gtk.NumericSorter(expression=Gtk.PropertyExpression.new(Media, None, "use-count"), sort_order=Gtk.SortType.DESCENDING)
        self.id_sorter = Gtk.NumericSorter(expression=Gtk.PropertyExpression.new(Media, None, "id"), sort_order=Gtk.SortType.ASCENDING)
        self.filter = Gtk.EveryFilter()
        self.search_filter = Gtk.AnyFilter()
        for i in ("Artists", "Name", "URI"):
            s = Gtk.StringFilter.new(Gtk.PropertyExpression.new(Media, None, i.lower()))
            self.bind_property("search", s, "search", DEFAULT_BIND)
            self.search_filter.append(s)
        self.hidden_filter = Gtk.BoolFilter(invert=True, expression=Gtk.PropertyExpression.new(Media, None, "hidden"))
        window.connect("notify::show-hidden", self.__class__.hidden_changed)
        self.filter.append(self.search_filter)
        filter_list = Gtk.FilterListModel(filter=self.filter)
        sort_list = Gtk.SortListModel(model=filter_list)
        self.bind_property("sorter", sort_list, "sorter", DEFAULT_BIND)
        self.selection.set_model(sort_list)
        self.bind_property("model", filter_list, "model", DEFAULT_BIND)
        self.listview = listview
        filter_list.connect("items-changed", self.__class__.update, self)
        self.listview.connect("activate", self.__class__.activate)
        self.listview.set_factory(MediaWidget.factory)
        self.spinner = Adw.Spinner()
        window.get_application().bind_property("medias", self, "model", DEFAULT_BIND)
        self.overlay.add_overlay(self.spinner)
        playlist_page = window.stack.add_titled_with_icon(self, "Playlist", "Playlist", "library-music-symbolic")
        window.stack.set_visible_child_name("Playlist")
        self.bind_property("playlist", playlist_page, "title", DEFAULT_BIND)
        self.bind_property("icon", playlist_page, "icon-name", DEFAULT_BIND)

    @staticmethod
    def get_all():
        l = []
        for w in Gtk.Window.list_toplevels():
            if isinstance(w, Window): l.append(w.playlist)
        return l

    @staticmethod
    def playlist_sort_expression(t, g): return g.get(t.uri, 1)

    @staticmethod
    def playlist_filter_expression(t, g): return t.has_playlist(g)

    @staticmethod
    def hidden_changed(window, p=None):
        if window.show_hidden:
            for n in range(window.playlist.filter.get_property("n-items")):
                f = window.playlist.filter.get_item(n)
                if f == window.playlist.hidden_filter: window.playlist.filter.remove(n)
        else: window.playlist.filter.append(window.playlist.hidden_filter)

    def find(self, uri):
        n = self.selection.get_property("n-items")
        for i in range(n):
            s = self.selection.get_item(i).uri
            if s == uri: return i

    def update(self, p=None, r=None, a=None, s=None):
        self = s if isinstance(s, PlaylistView) else self
        self = self if isinstance(self, PlaylistView) else self.get_ancestor(PlaylistView) if isinstance(self, Gtk.Widget) else None
        if not self or not self.get_root() or not self.get_root().player: return
        current = self.find(self.get_root().player.uri)
        if current == None:
            self.can_go_previous = self.can_go_next = False
        elif self.loop == "Playlist":
            self.can_go_previous = self.can_go_next = True
        else:
            self.can_go_previous = True
            self.can_go_next = self.selection.get_n_items() > current + 1
        Task.invoke(self.status_update)

    def status_update(self):
        if self.selection.get_n_items() > 0: self.set_status()
        else:
            if self.search: self.set_status(True, "No Results", "edit-find-symbolic")
            else: self.set_status(True, "Empty", self.icon)
        self.spinner.set_visible(False)

    def previous(b):
        window = b.get_root()
        if not window.playlist.can_go_previous: return
        if window.player.position > 3e9:
            window.player.position = 0
            return
        current = window.playlist.find(window.player.uri)
        n = 0
        if current == 0 and window.playlist.loop == "Playlist":
            n = window.playlist.selection.get_n_items() - 1
        elif current:
            n = current - 1
        ne = window.playlist.selection.get_item(n)
        if ne and ne.uri != window.player.uri: window.playlist.activate(window.playlist.listview, n)

    def next(b):
        window = b.get_root()
        if not window.playlist.can_go_next: return
        current = window.playlist.find(window.player.uri)
        n = 0
        if current != None and window.playlist.selection.get_n_items() > current + 1:
            n = current + 1
        ne = window.playlist.selection.get_item(n)
        if ne:
            if ne.uri == window.player.uri:
                if window.playlist.loop == "Playlist":
                    window.player.position = 0
            else: window.playlist.activate(window.playlist.listview, n)

    def get_selected(self): return [self.selection.get_item(i) for i in range(self.selection.get_property("n-items")) if self.selection.is_selected(i)]

    @staticmethod
    def activate(listview, p):
        self = listview.get_ancestor(PlaylistView)
        p = self.selection.get_item(p)
        if not p: return
        self.get_root().player.uri = p.uri

    def set_selected(self, uri=""):
        n = self.find(uri)
        if n != None: self.selection.select_item(n, True)
        else: self.selection.unselect_all()

    @staticmethod
    def most_played_sort(i): return i.get("Use Count", 0)

    def set_playlist(self, playlist=""):
        if isinstance(playlist, Playlist):
            playlist = playlist.name
        for n in range(self.filter.get_property("n-items")):
            f = self.filter.get_item(n)
            if not f in (self.hidden_filter, self.search_filter): self.filter.remove(n)
        self.playlist = playlist or "Playlist"
        self.order = {}
        icon = "music-playlist-symbolic"
        if playlist: self.filter.append(self.playlist_filter)
        if playlist == Playlist.default[0]:
            icon = "folder-music-symbolic"
            self.sorter = self.random_sorter if self.shuffle else self.id_sorter
        elif playlist == "Recently Added":
            icon = "clock-alt-symbolic"
            self.sorter = self.random_sorter if self.shuffle else self.id_sorter
        elif playlist == "Most Played":
            self.sorter = self.random_sorter if self.shuffle else self.played_sorter
            icon = "fire-symbolic"
        elif playlist:
            p = self.get_root().get_application().playlists[playlist]
            if p:
                g = str_to_list(self.get_root().get_application().playlists[playlist].list)
                for n, i in enumerate(g):
                    self.order[i] = n
                self.sorter = self.random_sorter if self.shuffle else self.playlist_sorter
        if self.sorter == self.random_sorter: self.random_sorter.changed(Gtk.SorterChange.DIFFERENT)
        self.icon = icon
        Task.invoke(self.update)
        if self.get_parent(): Task.invoke(self.get_parent().set_visible_child_name, "Playlist")
        Task.invoke(self.set_selected, self.get_root().player.uri)

class Playlist(ListObject):
    __gtype_name__ = "Playlist"

    default = ["All Media", "Most Played", "Recently Added"]

    id = GObject.Property(type=int, nick="ID", default=0)

    @GObject.Property(type=str)
    def name(self): return getattr(self, "_name", "")

    @name.setter
    def name(self, value):
        old_name = self.name
        self._name = value
        if old_name:
            App.default.data.update(table=self.__class__.__gtype_name__ + "s", column="Name", value=self.name, conditions="Name = ?", conds=(old_name,))
            Task.invoke(App.default.playlists.do_dict)

    cover_paintable = GObject.Property(type=Gdk.Paintable)
    list = GObject.Property(type=str)
    artists = GObject.Property(type=str)
    cover = GObject.Property(type=str)
    hidden = GObject.Property(type=bool, default=False)

    def has_artist(self, artist=""):
        if isinstance(artist, Artist):
            artist = artist.name
        if not artist or self.name in Playlist.default: return False
        return artist in str_to_list(self.artists)

    @staticmethod
    def arg_to_name(a):
        if isinstance(a, PlaylistItem):
            a = a.item
        if isinstance(a, Playlist):
            a = a.name
        return a

    @staticmethod
    def changed(self, p):
        if p.get_name() == "cover-paintable": return
        v = getattr(self, p.get_name(), None)
        p = p.get_nick() or p.get_name().replace("-", " ").title()
        t = self.__class__.__gtype_name__ + "s"
        c = "Name = ?"
        if p == "Cover":
            self.cover_paintable = None
        if p == "Hidden":
            for w in Gtk.Window.list_toplevels():
                if isinstance(w, Window) and not w.show_hidden:
                    view = w.stack.get_child_by_name("Playlists")
                    if view.get_mapped() or view.catalog.get_first_child(): view.do_search()
        App.default.data.update(table=t, column=p, value=v, conditions=c, conds=(self.name,))

    def remove(self, files):
        if not isinstance(self, Playlist):
            self = Playlist.arg_to_name(self)
            self = App.default.playlists[self]
        files = Media.arg_to_files(files)
        self.list = list_to_str([i for i in str_to_list(self.list) if not i in files])

    def add(self, files):
        if not isinstance(self, Playlist):
            self = Playlist.arg_to_name(self)
            self = App.default.playlists[self]
        files = Media.arg_to_files(files)
        l = str_to_list(self.list)
        for i in files:
            if not i in l: l.append(i)
        self.list = list_to_str(l)

    def delete(self):
        if not isinstance(self, Playlist):
            self = Playlist.arg_to_name(self)
            self = App.default.playlists[self]
        if self:
            App.default.playlists.remove(self)
            App.default.data.delete(table="Playlists", conditions="Name = ?", conds=(self.name,))

    @staticmethod
    def items_changed(l, *_):
        l = [i.name for i in l]
        for i in PlaylistView.get_all():
            if not i.playlist in l: i.set_playlist(Playlist.default[0])
        for w in Gtk.Window.list_toplevels():
            if isinstance(w, Window):
                view = w.stack.get_child_by_name("Playlists")
                if view.get_mapped() or view.catalog.get_first_child(): view.do_search()

class PlaylistItem(Gtk.Button):
    __gtype_name__ = "PlaylistItem"
    item = GObject.Property(type=Playlist)
    def __init__(self, window, i, **kwargs):
        super().__init__(overflow=Gtk.Overflow.HIDDEN, child=Gtk.Picture(), **kwargs)
        self.item = i
        self.item.bind_property("cover-paintable", self.get_child(), "paintable", DEFAULT_BIND, self.__class__.cover_bind)
        self.get_child().bind_property("paintable", self, "css-classes", DEFAULT_BIND, self.__class__.css_bind)
        self.item.bind_property("name", self, "name", DEFAULT_BIND)
        self.bind_property("name", self, "tooltip-text", DEFAULT_BIND)
        if not window.player.ignore_cover and not self.item.cover_paintable:
            cover = self.item.cover
            if not cover:
                if self.item.name == "Most Played":
                    cover = "fire-symbolic.svg"
                elif self.item.name == "Recently Added":
                    cover = "clock-alt-symbolic.svg"
            if cover.endswith("-symbolic.svg"):
                i = Gtk.IconTheme.get_for_display(Gdk.Display.get_default()).lookup_icon(cover.rsplit(".svg", 1)[0], None, -1, 1, Gtk.TextDirection.NONE, Gtk.IconLookupFlags.FORCE_SYMBOLIC)
                if i and i.get_file():
                    try:
                        self.item.cover_paintable = Gtk.Svg.new_from_bytes(i.get_file().load_bytes()[0])
                    except:
                        self.item.cover_paintable = Gtk.IconPaintable.new_for_file(i.get_file(), 1, 1)
            elif cover:
                c = App.default.get_cover(cover)
                self.item.cover_paintable = Adw.SpinnerPaintable.new(self) if c == True else c
                if c == True:
                    self.item.cover_paintable.uri = cover

    @staticmethod
    def css_bind(b, v): return ["flat", "playlist"] + item_css_bind(b, v)

    @staticmethod
    def do_clicked(self): self.get_root().playlist.set_playlist(self.item.name)

    @staticmethod
    def cover_bind(b, v): return v if v else DEFAULT_PAINTABLE

    @staticmethod
    def get_all():
        l = []
        for w in Gtk.Window.list_toplevels():
            if isinstance(w, Window):
                view = w.stack.get_child_by_name("Playlists")
                for i in [i for i in view.catalog]: l.append(i)
        return l

class Media(ListObject):
    __gtype_name__ = "MediaObject"
    id = GObject.Property(type=int, nick="ID", default=0)

    def __repr__(self): return self.uri

    @GObject.Property(type=str)
    def uri(self): return getattr(self, "_uri", "")

    @uri.setter
    def uri(self, value):
        old_uri = self.uri
        self._uri = value
        if old_uri:
            App.default.data.update(table="Medias", column="URI", value=value, conditions="URI = ?", conds=(old_uri,))
            for p in self.__class__.get_playlists(old_uri):
                p.list = list_to_str([self.uri if i == old_uri else i for i in str_to_list(p.list)])
            Task.invoke(App.default.medias.do_dict)

    cover = GObject.Property(type=str)
    artists = GObject.Property(type=str)
    name = GObject.Property(type=str)
    use_count = GObject.Property(type=int)
    hidden = GObject.Property(type=bool, default=False)

    @staticmethod
    def arg_to_files(files):
        if isinstance(files, str):
            if "\n" in files:
                files = str_to_list(files)
            else:
                files = [files]
        files = [i.uri if isinstance(i, Media) else i.get("URI", "None") if isinstance(i, dict) else i for i in files]
        return files

    def get_cover(self):
        l = []
        a = str_to_list(self.artists)
        for i in App.default.artists:
            if i.name in a: l.append(i.cover)
        possible = [self.cover] + [i.cover for i in self.get_playlists()] + l
        possible = [i for i in possible if i.startswith(("file://", "http://", "https://"))]
        if is_online(self.uri) and (("youtu.be/" in self.uri) or ("youtube.com" in self.uri and "watch?v=" in self.uri)): possible.append(self.uri.replace("www.", "").replace("be.com/watch?v=", ".be/").split("?")[0].replace("youtu.be", "img.youtube.com/vi").split("&", 1)[0] + "/maxresdefault.jpg")
        return next(iter(possible), None)

    def delete(uri):
        if isinstance(uri, Media):
            uri = uri.uri
        t = App.default.medias[uri]
        if t: App.default.medias.remove(t)
        App.default.data.delete(table="Medias", conditions="URI = ?", conds=(uri,))

    @staticmethod
    def changed(self, p): App.default.data.update(table="Medias", column=p.get_nick() or p.get_name().replace("-", " ").replace("_", " ").title(), value=getattr(self, p.get_name().replace("-", "_"), None), conditions="URI = ?", conds=(self.uri,))

    def has_artist(self, artist=""):
        if isinstance(artist, Artist):
            artist = artist.name
        if not artist: return False
        return artist in str_to_list(self.artists)

    def has_playlist(self, playlist=""):
        if not playlist: return False
        if playlist == Playlist.default[0]: return True
        if playlist == "Most Played": return self.use_count > 1
        if playlist == "Recently Added": return self.use_count == 0
        p = App.default.playlists[playlist]
        if p: return self.uri in str_to_list(p.list)
        return False

    def get_playlists(media=""):
        if not media: return []
        if isinstance(media, Media):
            media = media.uri
        if isinstance(media, dict):
            media = media.get("URI", "None")
        l = []
        for i in App.default.playlists:
            if i.name in Playlist.default: continue
            if media in str_to_list(i.list): l.append(i)
        return l

    @staticmethod
    def get_name(v):
        if not v: return ""
        n = v.name
        v = v.uri
        if n: return n
        if v.startswith("file://"):
            v = basename(v)
        elif is_online(v):
            v = uri_decode(v.split("://", 1)[-1])
        v = v.replace("\n", "")
        return v

class MediaWidget(Gtk.Label):
    __gtype_name__ = "MediaWidget"

    factory = Gtk.SignalListItemFactory.new()

    @GObject.Property(type=Media)
    def media(self): return getattr(self, "_media", None)

    @media.setter
    def media(self, value):
        self._media = value
        for i in getattr(self, "bindings", []): i.unbind()
        if self.media:
            self.bindings = [self.media.bind_property("name", self, "label", DEFAULT_BIND, self.__class__.bind_label), self.media.bind_property("uri", self, "name", DEFAULT_BIND)]

    def __init__(self): super().__init__(xalign=0, ellipsize=Pango.EllipsizeMode.END, lines=0, single_line_mode=True)

    @staticmethod
    def bind_label(b, v):
        t = b.get_property("source")
        return Media.get_name(t)

    @staticmethod
    def setup(f, l):
        l.set_child(MediaWidget())
        l.bind_property("item", l.get_child(), "media", DEFAULT_BIND)

    @staticmethod
    def teardown(f, l):
        if l.get_child():
            l.get_child().media = None
        l.set_child(None)

MediaWidget.factory.connect("setup", MediaWidget.setup)
MediaWidget.factory.connect("teardown", MediaWidget.teardown)

class Context(Gtk.PopoverMenu):
    __gtype_name__ = "Context"

    widget = GObject.Property(type=Gtk.Widget)

    def __init__(self): super().__init__(has_arrow=False, width_request=100)

    @staticmethod
    def activate(a, s, window): Context.action(widget=window.context.widget if window.context else window, action=s.unpack().strip("'"))

    @staticmethod
    def action(widget, action=""):
        window = widget.get_root()
        if action == "Add Artist":
            editor = Editor()
            editor.ready = False
            editor.set_title("New Artist")
            editor.show_artist()
            editor.present()
        if action == "Add Playlist":
            editor = Editor()
            editor.ready = False
            editor.set_title("New Playlist")
            editor.show_playlist()
            editor.present()
        if action == "Add Medias":
            group = Adw.PreferencesGroup()
            group.add(URIRow())
            p = Prompt(callback=Window.add_uris, confirm_callback=False, heading="Add Medias", extra_child=group)
            p.window = window
            p.present()
        if action == "Edit":
            if widget.get_ancestor(Player) or widget.get_ancestor(ArtistItem) or widget.get_ancestor(PlaylistItem) or isinstance(widget, MediaWidget):
                editor = Editor()
                editor.ready = False
                name, item = False, False
                if widget.get_ancestor(PlaylistItem):
                    item = widget.get_ancestor(PlaylistItem).item
                    name = item.name
                    editor.show_playlist(item)
                elif widget.get_ancestor(ArtistItem):
                    item = widget.get_ancestor(ArtistItem).item
                    name = item.name
                    editor.show_artist(item)
                elif widget.get_ancestor(PlaylistView):
                    item = App.default.medias[widget.get_name()]
                    name = item.uri if item else None
                    if item: editor.show_media(item)
                elif widget.get_ancestor(Player):
                    item = App.default.medias[widget.get_ancestor(Player).uri]
                    name = item.uri if item else None
                    if item: editor.show_media(item)
                if name: editor.set_title(name)
                if item:
                    editor.present()
                    editor.name_row.grab_focus()
                    editor.ready = True
        if action == "Launch URI":
            if widget.get_ancestor(Player):
                name = widget.get_ancestor(Player).uri
            else:
                name = widget.get_name()
            launch(name)
        if action == "Launch YouTube Playlist":
            selected = [i.uri for i in widget.get_root().playlist.get_selected()] if isinstance(widget, MediaWidget) else [widget.get_root().player.uri]
            launch(f"https://www.youtube.com/watch_videos?video_ids={','.join([youtube_id(i) for i in selected])}")

class Editor(PopupDialog):
    __gtype_name__ = "Editor"
    type = GObject.Property(type=str)
    ready = GObject.Property(type=bool, default=False)

    uri = GObject.Property(type=URIRow)
    name_row = GObject.Property(type=Adw.EntryRow)
    cover = GObject.Property(type=URIRow)
    hidden = GObject.Property(type=Adw.SwitchRow)
    artists = GObject.Property(type=TagRow)
    playlists = GObject.Property(type=TagRow)
    use_count = GObject.Property(type=Adw.SpinRow)

    delete = GObject.Property(type=Gtk.Button)
    confirm = GObject.Property(type=Gtk.Button)

    def __init__(self):
        group = Adw.PreferencesGroup()
        page = GroupsPage()
        page.add(group)
        toolbar = Adw.ToolbarView(content=page)
        header = Adw.HeaderBar()
        toolbar.add_top_bar(header)
        super().__init__(content=toolbar, dialog_kwargs={"content_width": 490, "content_height": 482}, window_kwargs={"default_width": 490, "default_height": 482})
        self.uri = URIRow(title="URI", multiple=False)
        self.name_row = Adw.EntryRow(title="Name")
        self.cover = URIRow(title="Cover", multiple=False)
        for i in (self.name_row, ): i.connect("changed", self.__class__.changed)
        for i in (self.uri, self.cover,): i.connect("notify::text", self.__class__.changed)
        self.hidden = Adw.SwitchRow(title="Hidden")
        self.artists = TagRow(title="Artists")
        e = Gtk.PropertyExpression.new(Gtk.StringObject, None, "string")
        self.artists.prompt = Prompt(heading="Add Artist", extra_child=Gtk.ScrolledWindow(child=Gtk.DropDown(enable_search=True, search_match_mode=Gtk.StringFilterMatchMode.SUBSTRING, expression=e, model=Gtk.MapListModel.new(App.default.artists, self.__class__.name_map)), vscrollbar_policy=Gtk.PolicyType.NEVER), callback=self.__class__.prompt_activate)
        self.artists.prompt.editor = self
        self.playlists = TagRow(title="Playlists")
        l = Gtk.MapListModel.new(App.default.playlists, self.__class__.name_map)
        l = Gtk.FilterListModel(model=l, filter=Gtk.BoolFilter(expression=Gtk.ClosureExpression.new(bool, self.__class__.playlist_filter_expression, [])))
        self.playlists.prompt = Prompt(heading="Add Playlist", extra_child=Gtk.ScrolledWindow(child=Gtk.DropDown(enable_search=True, search_match_mode=Gtk.StringFilterMatchMode.SUBSTRING, expression=e, model=l), vscrollbar_policy=Gtk.PolicyType.NEVER), callback=self.__class__.prompt_activate)
        self.playlists.prompt.editor = self
        self.use_count = Adw.SpinRow.new_with_range(0, 1000000, 1)
        self.use_count.set_properties(title="Use Count")
        for a, b in [(self.hidden, "active"), (self.artists, "tags"), (self.playlists, "tags"), (self.use_count, "value")]: a.connect(f"notify::{b}", self.__class__.changed)
        for i in (self.uri, self.name_row, self.cover, self.artists, self.playlists, self.use_count, self.hidden): group.add(i)
        self.rows = {}
        for i in ["uri", "name_row", "cover", "hidden", "artists", "use_count", "playlists"]:
            self.rows[i] = getattr(self, i, None)
        self.delete = Button(callback=self.__class__.do_delete, icon_name="user-trash", tooltip_text="Delete", css_classes=("flat", "destructive-action",))
        self.confirm = Button(callback=self.__class__.do_confirm, icon_name="list-add", tooltip_text="Add", css_classes=("flat", "suggested-action",))
        for i in (self.delete, self.confirm): header.pack_start(i)
        header.pack_end(PopupDialogButton())
        self.add_css_class("editor")

    @staticmethod
    def playlist_filter_expression(a): return not a.get_string() in Playlist.default

    @staticmethod
    def name_map(a): return Gtk.StringObject.new(a.name)

    @staticmethod
    def prompt_activate(d):
        type = d.get_heading().rsplit(" ", 1)[-1]
        t = d.get_extra_child().get_child().get_child().get_selected_item()
        if t:
            t = t.get_string()
            if type == "Artist":
                r = d.editor.artists
            if type == "Playlist":
                r = d.editor.playlists
            l = r.tags
            l.append(t)
            r.tags = l

    @staticmethod
    def changed(w, p=None):
        self = Editor.get_self(w)
        if not self or not self.ready or not w.get_visible(): return
        t = getattr(App.default, (self.type + "s").lower(), False)
        n = t[self.get_title()]
        if self.type == "Media" and not n: return
        if w == self.cover:
            n.cover = w.get_text()
        if w == self.hidden:
            n.hidden = w.get_active()
        if w == self.name_row:
            if isinstance(n, Media):
                n.name = w.get_text()
            else:
                if not w.get_text(): return Toast("Name can't be empty")
                if w.get_text() == self.get_title(): return
                if t and t[w.get_text()]: return Toast(f"{w.get_text()} already exists!")
                n.name = w.get_text()
                self.set_title(w.get_text())
        if w == self.uri:
            if not w.get_text(): return Toast("URI can't be empty")
            if App.default.medias[w.get_text()]: return Toast(f"{w.get_text()} already exists!")
            n.uri = w.get_text()
            self.set_title(w.get_text())
        if w == self.artists:
            n.artists = list_to_str(w.tags)
        if w == self.playlists:
            for i in App.default.playlists:
                l = str_to_list(i.list)
                if i.name in w.tags and not self.get_title() in l:
                    i.list = list_to_str(l + [self.get_title()])
                elif not i.name in w.tags and self.get_title() in l:
                    i.list = list_to_str([it for it in l if not it == self.get_title()])
            n.playlists = list_to_str(w.tags)
        if w == self.use_count:
            n.use_count = w.get_value()

    @staticmethod
    def do_actually_delete(d):
        t = d.editor.type.lower() + "s"
        t = getattr(App.default, t, False)
        t = t[d.editor.get_title()] if t else False
        if t: t.delete()
        d.editor.close()
        del d.editor

    @staticmethod
    def do_delete(b):
        self = Editor.get_self(b)
        if not self: return
        p = Prompt(heading=f"Delete {self.get_title()}?", callback=self.__class__.do_actually_delete)
        p.editor = self
        p.present(self.get_root())

    @staticmethod
    def do_confirm(b):
        self = Editor.get_self(b)
        if not self: return
        n = self.name_row.get_text()
        if not n: return Toast("Empty Name")
        t = self.type + "s"
        c = "Name == ?"
        if App.default.data.exists(table=t, conditions=c, conds=(n,)): return Toast(f"{n} already exists!")
        if t == "Playlists":
            v = {"ID": max((i.id for i in App.default.playlists), default=0) + 1, "Name": n, "Hidden": self.hidden.get_active(), "Cover": self.cover.get_text(), "Artists": list_to_str(self.artists.tags), "List": ""}
            App.default.data.new(table=t, names=tuple(v.keys()), values=tuple(v.values()))
            App.default.playlists.add(v)
        if t == "Artists":
            v = {"ID": max((i.id for i in App.default.artists), default=0) + 1, "Name": n, "Hidden": self.hidden.get_active(), "Cover": self.cover.get_text()}
            App.default.data.new(table=t, names=tuple(v.keys()), values=tuple(v.values()))
            App.default.artists.add(v)
        self.close()

    def show_artist(self, artist=None):
        self.type = "Artist"
        self.delete.set_visible(bool(artist))
        self.confirm.set_visible(not artist)
        self.name_row.set_text(artist.name if artist else "")
        self.cover.set_text(artist.cover if artist else "")
        self.hidden.set_active(artist.hidden if artist else False)
        v = (False, True, True, True, False, False, False)
        for n, i in enumerate(self.rows.values()): i.set_visible(v[n])

    def show_playlist(self, playlist=None):
        self.type = "Playlist"
        name = playlist.name if playlist else ""
        self.delete.set_visible(bool(playlist) and not name in Playlist.default)
        self.confirm.set_visible(not playlist)
        self.name_row.set_text(name)
        self.cover.set_text(playlist.cover if playlist else "")
        self.artists.tags = str_to_list(playlist.artists) if playlist else []
        self.hidden.set_active(playlist.hidden if playlist else False)
        v = (False, not name in Playlist.default, True, True, not name in Playlist.default, False, False)
        for n, i in enumerate(self.rows.values()): i.set_visible(v[n])

    def show_media(self, media):
        self.type = "Media"
        self.delete.set_visible(True)
        self.confirm.set_visible(False)
        self.uri.set_text(media.uri)
        self.name_row.set_text(media.name)
        self.cover.set_text(media.cover)
        self.artists.tags = str_to_list(media.artists)
        self.playlists.tags = [i.name for i in media.get_playlists()]
        self.use_count.set_value(media.use_count)
        self.hidden.set_active(media.hidden)
        for n, i in enumerate(self.rows.values()): i.set_visible(True)

class Window(Window):
    __gtype_name__ = "Window"
    back_button = GObject.Property(type=bool, default=False)

    show_hidden = GObject.Property(type=bool, default=False)
    search = GObject.Property(type=Gtk.SearchEntry)
    search_mode_enabled = GObject.Property(type=bool, default=False)
    breakpoint = GObject.Property(type=Adw.Breakpoint)
    stack = GObject.Property(type=Adw.ViewStack)
    player = GObject.Property(type=Player)
    playlist = GObject.Property(type=PlaylistView)
    context = GObject.Property(type=Context)

    delete = GObject.Property(type=str)

    drop_target_gtypes = (ArtistItem, PlaylistItem, Gdk.FileList, Gio.File, str)

    def __init__(self, _id=None):
        super().__init__()
        if not _id: App.default.data.set_default(table="Windows", conditions="ID = ?", conds=(self.get_id(),), names=("ID", ), values=(self.get_id(),))
        elif self.get_id() != _id: App.default.data.update(table="Windows", column="ID", value=self.get_id(), conditions="ID = ?", conds=(_id,))
        self.breakpoint = Adw.Breakpoint.new(Adw.BreakpointCondition.new_length(Adw.BreakpointConditionLengthType.MAX_WIDTH, 400, Adw.LengthUnit.PX))
        self.add_breakpoint(self.breakpoint)
        self.stack = Adw.ViewStack(vhomogeneous=False, vexpand=True)
        for i, ii in ((View.default[0], "system-users"), (View.default[1], "library-music")):
            if i == View.default[0]:
                catalog = Adw.WrapBox(child_spacing=10, line_spacing=10)
                child = Gtk.ScrolledWindow(child=catalog)
            else:
                child = catalog = Masonry()
            child.connect("edge-reached", View.load_more)
            v = View(self, child=child)
            v.added_max = 28 if i == View.default[0] else 20
            v.set_name(i)
            v.item = ArtistItem if i == View.default[0] else PlaylistItem
            v.connect("map", self.__class__.view_mapped)
            v.catalog = catalog
            self.stack.add_titled_with_icon(v, i, i, ii + "-symbolic")
        self.connect("notify::show-hidden", self.__class__.show_hidden_changed)
        self.playlist = PlaylistView(self)
        toolbar = Adw.ToolbarView(content=Gtk.Overlay(child=self.stack))
        self.set_content(Gtk.Box(orientation=Gtk.Orientation.VERTICAL))
        self.get_content().append(toolbar)
        self.search = Gtk.SearchEntry(placeholder_text="Search")
        self.search.connect("search-changed", self.__class__.search_changed)
        self.search.connect("activate", View.do_search)
        self.stack.connect("notify::visible-child", self.__class__.update_search)
        find = Gtk.Revealer(css_name="find", transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN, transition_duration=300, halign=Gtk.Align.CENTER, child=self.search, valign=Gtk.Align.START)
        self.bind_property("search-mode-enabled", find, "reveal-child", SYNC_BIND)
        toolbar.get_content().add_overlay(find)
        for i in ("Maximized", "Hide On Close", "Show Hidden"): self.bind_setting(self, i, False)
        for i in ("Default Width", "Default Height"): self.bind_setting(self, i, 100)
        self.player = Player(self)
        switcher = Adw.InlineViewSwitcher(halign=Gtk.Align.CENTER, hexpand=True, display_mode=Adw.InlineViewSwitcherDisplayMode.BOTH, stack=self.stack)
        self.breakpoint.add_setter(switcher, "display-mode", Adw.InlineViewSwitcherDisplayMode.ICONS)
        switcher = Gtk.ScrolledWindow(vscrollbar_policy=Gtk.PolicyType.NEVER, child=switcher)
        header = Adw.HeaderBar(title_widget=switcher, show_back_button=False, show_start_title_buttons=False)
        a = Menu({"Ignore Video": "win.ignore-video", "Ignore Audio": "win.ignore-audio", "Ignore Subtitle": "win.ignore-subtitle", "Ignore Cover": "win.ignore-cover"})
        b = Menu({"Show Hidden": "win.show-hidden", "Hide on Close": "win.hide-on-close"})
        c = Menu({"Quit": "quit"})
        header.pack_end(Button(t=Gtk.MenuButton, menu_model=Menu({"!aa": Menu({"Add Medias": "win.context::Add Medias"}), "!a": a, "!b": b, "!c": c, "!x": App.default.default_menu}), tooltip_text="Menu", icon_name="open-menu"))
        header.pack_end(Button(t=Gtk.ToggleButton, icon_name="edit-find", tooltip_text="Search", bindings=[(None, "active", self, "search-mode-enabled", SYNC_BIND), (None, "visible", self.search.get_parent(), "visible", None), (self.stack, "visible-child", None, "visible", None, self.__class__.search_bind)]))
        delete = Button(icon_name="user-trash", css_classes=("raised", "destructive-action"))
        self.bind_property("delete", delete, "tooltip-text", DEFAULT_BIND)
        revealer = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.SLIDE_RIGHT, child=delete)
        self.bind_property("delete", revealer, "reveal-child", DEFAULT_BIND, bool_bind)
        header.pack_start(Button(callback=self.__class__.do_back, icon_name="go-previous", tooltip_text="Back", visible=False, bindings=((self.stack, "visible-child-name", None, "visible", None, self.__class__.back_bind),)))
        header.pack_start(revealer)
        toolbar.add_top_bar(header)
        self.add_action(Gio.SimpleAction.new("context", get_variant("str").get_type()))
        self.lookup_action("context").connect("activate", Context.activate, self)
        self.status()
        self.bind_setting(self.player, "Volume", 0.8)
        self.bind_setting(self.player, "Speed", 1.0)
        for i in ("width", "height"): self.bind_setting(self.player.overlay, f"Player {i.title()}", 200, i)
        for i in ("x", "y"): self.bind_setting(self.player.overlay, f"Player {i.upper()}", 0, i)
        self.bind_setting(self.playlist, "Playlist", Playlist.default[0])
        self.bind_setting(self.playlist, "Shuffle", False)
        self.bind_setting(self.playlist, "Loop", "None")
        for i in ("Video", "Audio", "Subtitle", "Cover"): self.bind_setting(self.player, f"Ignore {i}", False)
        self.playlist.set_playlist(self.playlist.playlist)
        self.present()
        self.bind_setting(self.player, "Fullscreen", False)
        Task.invoke(self.bind_setting, self.player, "URI", "")

    @staticmethod
    def update_search(s, p=None):
        self = s.get_root()
        if not self: return
        self.search.changing = True
        self.search.set_text(self.stack.get_visible_child().search)
        self.search.changing = False

    @staticmethod
    def search_changed(s):
        self = s.get_root()
        if not self or getattr(s, "changing", False): return
        self.stack.get_visible_child().search = s.get_text()

    @staticmethod
    def search_bind(b, v): return v and v.has_search

    @staticmethod
    def show_hidden_changed(self, p):
        for i in [self.stack.get_child_by_name("Artists"), self.stack.get_child_by_name("Playlists")]:
            if i.catalog.get_first_child(): i.do_search()

    @staticmethod
    def do_back(b):
        self = b.get_root()
        self.stack.remove(self.stack.get_visible_child())
        self.stack.set_visible_child_name(View.default[-1])

    @staticmethod
    def back_bind(b, v): return not v in View.default

    @staticmethod
    def view_mapped(w):
        if not w.catalog.get_first_child(): w.do_search()

    def status(self):
        for t, icon in (("Artists", "avatar-default"), ("Playlists", "media-tape")):
            v = self.stack.get_child_by_name(t)
            c = v.catalog
            if c.get_first_child(): self.stack.get_child_by_name(t).set_status()
            elif v.search: self.stack.get_child_by_name(t).set_status(True, "No Results", "edit-find-symbolic")
            else: self.stack.get_child_by_name(t).set_status(True, f"No {t}", f"{icon}-symbolic")
        self.playlist.status_update()

    @staticmethod
    def on_click(*args):
        self, e, n, x, y, signal, b, state = Window.click_to_args(*args)
        if self.context and self.context.get_mapped(): return self.context.popdown()
        w = self.pick(x, y, Gtk.PickFlags.DEFAULT)
        if not w: return
        player = w.get_ancestor(Player)
        if b == 1 and player and n == 2 and signal == "pressed":
            if player.overlay.get_mapped(): self.set_property("maximized", not self.get_property("maximized"))
            return
        playlist = w.get_ancestor(PlaylistItem)
        artist = w.get_ancestor(ArtistItem)
        if signal != "released": return
        if b == 8: return call(m="Previous")
        elif b == 9: return call(m="Next")
        if b == 3:
            self.context = self.context or Context()
            self.context.widget = w
            menu = {}
            current = self.stack.get_visible_child_name()
            m = {}
            is_song = w.get_parent() and isinstance(w, MediaWidget)
            if artist or playlist or is_song or player:
                e = {"Edit": "win.context::Edit"}
                if is_song or player:
                    e["Launch URI"] = "win.context::Launch URI"
                    selected = [i.uri for i in self.playlist.get_selected()] if is_song else [self.player.uri]
                    if 50 > len(selected) and selected and all([is_youtube(i) for i in selected]):
                        e["Launch YouTube Playlist"] = "win.context::Launch YouTube Playlist"
                menu["!x"] = Menu(e)
            if not player and current == "Artists":
                m["Add Artist"] = "win.context::Add Artist"
            if not player and current == "Playlists":
                m["Add Playlist"] = "win.context::Add Playlist"
            if player or current == "Playlist":
                m["Add Medias"] = "win.context::Add Medias"
            if m:
                menu["!z"] = Menu(m)
            self.context.set_menu_model(Menu(menu))
            self.show_popover_at_widget(self.context, self.get_content(), x, y)

    @staticmethod
    def add_uris(p, r):
        if r != "confirm":
            del p.window
            return
        uris = str_to_list(p.get_extra_child().get_row(0).get_text())
        if uris: p.window.open_files(uris)
        del p.window

    def add_files(self, files):
        for i in files:
            if i and not App.default.medias[i]:
                i = {"ID": max((it.id for it in App.default.medias), default=0) + 1, "URI": i}
                self.get_application().data.new(table="Medias", names=tuple(i.keys()), values=tuple(i.values()))
                self.get_application().medias.add(i)

    def open_files(self, files, s=True):
        self.add_files(files)
        g = self.playlist.playlist
        if g in Playlist.default:
            g = Playlist.default[0]
        else:
            g = self.get_application().playlists[g]
            if g: g.add(files)
            else:
                g = Playlist.default[0]
        if s: self.playlist.set_playlist(g)

    def bind_setting(self, widget, i, v, p=None):
        p = p or i.lower().replace(" ", "-")
        self.get_application().data.persist_action(self, i, v, "Windows", i, "ID = ?", (self.get_id(),)).bind_property("state", widget, p, SYNC_BIND, unpack_bind, variant_bind)

    def clear_drop(self, b=True):
        if getattr(self, "highlight", False): self.highlight.remove_css_class("highlight")
        self.highlight = None
        if b:
            self.delete = ""

    @staticmethod
    def on_drag_source_prepare(e, x, y):
        self = e.get_widget().get_root()
        if isinstance(self.stack.get_visible_child(), ArtistView): return None
        self.clear_drop()
        w = self.pick(x, y, Gtk.PickFlags.DEFAULT)
        if w and w.get_ancestor(Player) or getattr(self.player.overlay, "dragging", False): return None
        e.f = []
        c = None
        if isinstance(w, MediaWidget):
            l = [i.uri for i in self.playlist.get_selected()]
            if not w.get_name() in l:
                self.playlist.set_selected(w.get_name())
                e.f = [Gio.File.new_for_uri(w.get_name())]
            else:
                e.f = [Gio.File.new_for_uri(i) for i in l]
            c = Gdk.ContentProvider.new_for_value(Gdk.FileList.new_from_list(e.f)) if e.f else None
            w = None
        if w and w.get_ancestor(ArtistItem):
            w = w.get_ancestor(ArtistItem)
            e.f = [Gio.File.new_for_uri(i.uri) for i in self.get_application().medias if i.has_artist(w.get_name())]
        if w and w.get_ancestor(PlaylistItem):
            w = w.get_ancestor(PlaylistItem)
            e.f = [Gio.File.new_for_uri(i.uri) for i in self.get_application().medias if i.has_playlist(w.get_name())]
        if isinstance(w, ArtistItem) or isinstance(w, PlaylistItem):
            l = [w]
            if e.f: l.append(Gdk.FileList.new_from_list(e.f))
            c = Gdk.ContentProvider.new_union([i if isinstance(i, Gdk.ContentProvider) else Gdk.ContentProvider.new_for_value(i) for i in l])
            e.set_actions((Gdk.DragAction.COPY | Gdk.DragAction.MOVE) if e.f else Gdk.DragAction.MOVE)
        else:
            e.set_actions((Gdk.DragAction.COPY | Gdk.DragAction.MOVE) if e.f else Gdk.DragAction.NONE)
        e.w = w
        return c

    @staticmethod
    def on_drag_source_drag_begin(e, d):
        self = e.get_widget().get_root()
        if e.w:
            if e.w.item.cover_paintable: Gtk.DragIcon.get_for_drag(d).set_child(Gtk.Picture(paintable=Gtk.WidgetPaintable.new(e.w)))
            else: Gtk.DragIcon.get_for_drag(d).set_child(Gtk.Label(margin_top=4, margin_start=4, label=e.w.get_name(), css_classes=("title-4",), ellipsize=Pango.EllipsizeMode.END))
        elif e.f:
            l = [i.get_uri() for i in e.f]
            for n, i in enumerate(l):
                t = self.get_application().medias[i]
                if t:
                    l[n] = Media.get_name(t)
            Gtk.DragIcon.get_for_drag(d).set_child(Gtk.Label(margin_top=4, margin_start=4, label=list_to_str(l), css_classes=("title-4",), ellipsize=Pango.EllipsizeMode.END))

    @staticmethod
    def on_drag_source_drag_end(e, d, de):
        e.w = None
        e.f = []
        e.set_content(None)
        e.set_actions(Gdk.DragAction.NONE)
        e.get_widget().get_root().clear_drop()

    @staticmethod
    def on_drop_target_drop(e, v, x, y):
        self = e.get_widget().get_root()
        w = self.pick(x, y, Gtk.PickFlags.DEFAULT)
        b = None
        if w:
            w = w.get_ancestor(ArtistItem) or w
            w = w.get_ancestor(PlaylistItem) or w
            b = w.get_ancestor(Gtk.Button)
        is_delete = b and b.get_tooltip_text() == self.delete
        if isinstance(v, ArtistItem) or isinstance(v, PlaylistItem):
            if not is_delete and not v.__class__ == w.__class__:
                if isinstance(v, ArtistItem):
                    v = [i.uri for i in self.get_application().medias if i.has_artist(v.get_name())]
                if isinstance(v, PlaylistItem):
                    v = [i.uri for i in self.get_application().medias if i.has_playlist(v.get_name())]
        if not isinstance(v, ArtistItem) and not isinstance(v, PlaylistItem):
            v = str_to_list(v) if isinstance(v, str) else [i.get_uri() for i in v] if isinstance(v, Gdk.FileList) else v.get_uri() if isinstance(v, Gio.File) else v
        a = e.get_actions()
        if is_delete:
            if self.delete == "Delete Artist": v.item.delete()
            if self.delete == "Delete Playlist": v.item.delete()
            if self.delete == "Delete Media":
                for i in v: Media.delete(i)
            if self.delete == "Remove From Playlist":
                Playlist.remove(self.playlist.playlist, v)
                self.playlist.set_playlist(self.playlist.playlist)
        elif isinstance(v, ArtistItem) or isinstance(v, PlaylistItem):
            t = "Playlists" if isinstance(v, PlaylistItem) else "Artists"
            l = getattr(self.get_application(), t.lower(), None)
            if not l: return
            ll = [i for i in l]
            start = ll.index(w.item)
            ll.remove(v.item)
            ll.insert(start, v.item)
            for n, i in enumerate(ll):
                if i.id != n + 1:
                    i.id = n + 1
            l.replace(ll)
        elif isinstance(w, MediaWidget):
            l = [self.playlist.selection.get_item(n) for n in range(self.playlist.selection.get_n_items())]
            v = [self.get_application().medias[i] for i in v]
            v = [i for i in v if i and i in l]
            start = l.index(w.media)
            for i in v:
                l.remove(i)
                l.insert(start, i)
            if self.playlist.shuffle:
                self.playlist.order = dict((i.uri, n) for n, i in enumerate(l))
                self.playlist.sorter = None
                self.playlist.sorter = self.playlist.playlist_sorter
            else:
                if self.playlist.playlist in Playlist.default:
                    for n, i in enumerate(l):
                        if i.id != n + 1:
                            i.id = n + 1
                    self.playlist.sorter = None
                    self.playlist.sorter = self.playlist.id_sorter
                else:
                    self.playlist.order = dict((i.uri, n) for n, i in enumerate(l))
                    self.playlist.sorter = None
                    self.playlist.sorter = self.playlist.playlist_sorter
                    p = self.get_application().playlists[self.playlist.playlist]
                    if p:
                        p.list = list_to_str(l)
        else:
            if isinstance(w, ArtistItem) or isinstance(w, PlaylistItem) and not (isinstance(w, PlaylistItem) and w.get_name() in Playlist.default):
                self.add_files(v)
                w.item.add(v)
            else: self.open_files(v)
        self.clear_drop()
        return True

    @staticmethod
    def on_drop_target_motion(e, x, y):
        self = e.get_widget().get_root()
        self.clear_drop(False)
        a = Gdk.DragAction.COPY
        w = self.pick(x, y, Gtk.PickFlags.DEFAULT)
        v = e.get_value()
        b = None
        if w:
            w = w.get_ancestor(ArtistItem) or w
            w = w.get_ancestor(PlaylistItem) or w
            b = w.get_ancestor(Gtk.Button)
        is_delete = b and type(b) == Gtk.Button and b.get_tooltip_text() == self.delete
        if isinstance(v, PlaylistItem) or isinstance(v, ArtistItem):
            if not v == w and (isinstance(w, PlaylistItem) or isinstance(w, ArtistItem)):
                a = Gdk.DragAction.MOVE
                self.highlight = w
            elif is_delete:
                a = Gdk.DragAction.MOVE
                self.highlight = b
            if v.get_name() in Playlist.default:
                self.delete = ""
            else:
                self.delete = f"Delete {v.__class__.__gtype_name__}"
            if not self.highlight:
                if w and w.get_ancestor(Gtk.ListView):
                    if isinstance(v, PlaylistItem) and self.playlist.playlist == v.get_name(): return Gdk.DragAction.NONE
                else: return Gdk.DragAction.NONE
        if isinstance(v, Gio.File) or isinstance(v, Gdk.FileList) or isinstance(v, str):
            self.delete = "Delete Media" if self.playlist.playlist in Playlist.default else "Remove From Playlist"
            if isinstance(v, Gio.File):
                v = [v.get_uri()]
            elif isinstance(v, str):
                v = str_to_list(v)
            else:
                v = [i.get_uri() for i in v]
            if isinstance(w, PlaylistItem) or isinstance(w, ArtistItem):
                self.highlight = w
            elif isinstance(w, MediaWidget) and not any(i == w.get_name() for i in v):
                a = Gdk.DragAction.MOVE
                self.highlight = w
            elif is_delete:
                a = Gdk.DragAction.MOVE
                self.highlight = b
            if not self.highlight:
                if all(i in self.playlist.order for i in v):
                    a = Gdk.DragAction.NONE
                elif self.playlist.playlist in Playlist.default:
                    l = [i.uri for i in self.get_application().medias]
                    if all(i in l for i in v):
                        a = Gdk.DragAction.NONE
        if a == Gdk.DragAction.NONE:
            self.highlight = None
        if self.highlight: self.highlight.add_css_class("highlight")
        return a

    @staticmethod
    def on_key_pressed(e, v, kc, s):
        self = e.get_widget()
        if self.get_visible_dialog(): return
        if s == Gdk.ModifierType.CONTROL_MASK and v == Gdk.KEY_f and getattr(self.stack.get_visible_child(), "has_search", False):
            self.search_mode_enabled = not self.search_mode_enabled
            if self.search_mode_enabled:
                self.set_focus(self.search)
                self.search.grab_focus()
            return True
        if v == Gdk.KEY_Escape and self.search_mode_enabled:
            self.search_mode_enabled = False
            return True
        f = self.get_focus()
        if isinstance(f, Gtk.Editable) or isinstance(f, Gtk.TextView): return
        is_media = f and isinstance(f.get_first_child(), MediaWidget)
        if is_media and Gdk.ModifierType.CONTROL_MASK in s and v in (Gdk.KEY_e, Gdk.KEY_E):
            Context.action(f.get_first_child() if is_media else f, "Edit")
            return True
        if self.player.uri and s == Gdk.ModifierType.SHIFT_MASK:
            if v in (Gdk.KEY_D, Gdk.KEY_d):
                call(m="Next")
                return True
            if v in (Gdk.KEY_A, Gdk.KEY_a):
                call(m="Previous")
                return True
            if v in (Gdk.KEY_F, Gdk.KEY_f):
                self.player.fullscreen = not self.player.fullscreen
                return True
        va = handle_scroll(self.get_focus(), v, s)
        if va: return
        if self.player.uri and s == 0:
            if v == Gdk.KEY_space and not isinstance(self.get_focus(), Gtk.Button): self.player.playpause()
            if v == Gdk.KEY_w: self.player.volumeup()
            if v == Gdk.KEY_s: self.player.volumedown()
            if v == Gdk.KEY_a: self.player.backward()
            if v == Gdk.KEY_d: self.player.forward()
            if v == Gdk.KEY_comma:
                call(m="Seek", p=(-1e5,))
                return True
            if v == Gdk.KEY_period:
                call(m="Seek", p=(1e5,))
                return True
        if v == Gdk.KEY_f: self.lookup_action("maximized").activate()

class App(App):
    __gtype_name__ = "App"
    artists = GObject.Property(type=List)
    playlists = GObject.Property(type=List)
    medias = GObject.Property(type=List)
    covers = GObject.Property(type=object)

    @staticmethod
    def id_sort(i): return i.id if hasattr(i, "id") else i.get("ID", 1)

    def activate(a):
        if a.get_active_window(): return a.get_active_window().present()
        a.covers = {}
        a.artists = List(Artist, unique="name", list=a.data.read(table="Artists", return_dict=True, force_tuple=True), static=True)
        a.playlists = List(Playlist, unique="name", list=a.data.read(table="Playlists", return_dict=True, force_tuple=True), static=True)
        a.medias = List(Media, unique="uri", list=a.data.read(table="Medias", return_dict=True, force_tuple=True), static=True)
        d = App.default.data.read(table="Windows", column="ID")
        if isinstance(d, int):
            d = [d]
        if not d:
            App.default.data.set_default(table="Windows", conditions="ID = '1'", values=(1,), names=("ID",))
            d = [1]
        for i in d: Window(i[0] if isinstance(i, tuple) else i)

    def get_cover(self, uri):
        if not uri: return None
        if uri in self.covers: return self.covers[uri]
        self.covers[uri] = True
        PaintableTask(func=self.__class__.finish_func, uri=uri, data=self)
        return self.covers[uri]

    def finish_func(p, app):
        app.covers[p.uri] = p
        for i in Player.get_all():
            Task.invoke(i.do_update)
            if i.get_root(): Task.invoke(i.get_root().stack.get_child_by_name("Playlists").catalog.check_layout)
        for l in (App.default.artists, App.default.playlists):
            for i in l:
                if i.cover == p.uri: Task.invoke(i.set_property, "cover-paintable", p)

    def file_open(a, files, *_):
        if not Window.current: activate(App.default)
        Window.current.open_files([i if isinstance(i, str) else i.get_uri() for i in files])

DATA_INIT = f"""
CREATE TABLE IF NOT EXISTS Artists('ID' INTEGER, Name TEXT NOT NULL PRIMARY KEY, Hidden INTEGER NOT NULL DEFAULT 0, Cover TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS Playlists('ID' INTEGER, Name TEXT NOT NULL PRIMARY KEY, Artists TEXT DEFAULT '', List TEXT DEFAULT '', Hidden INTEGER NOT NULL DEFAULT 0, Cover TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS Medias('ID' INTEGER, URI TEXT NOT NULL PRIMARY KEY, Artists TEXT DEFAULT '', Name TEXT DEFAULT '', 'Use Count' INTEGER NOT NULL DEFAULT 0, Hidden INTEGER NOT NULL DEFAULT 0, Cover TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS Windows ('ID' INTEGER PRIMARY KEY AUTOINCREMENT, 'Default Width' INTEGER NOT NULL DEFAULT 360, 'Default Height' INTEGER NOT NULL DEFAULT 760, 'Hide On Close' INTEGER NOT NULL DEFAULT 0, 'Maximized' INTEGER NOT NULL DEFAULT 0, 'Fullscreen' INTEGER NOT NULL DEFAULT 0, 'Show Hidden' INTEGER NOT NULL DEFAULT 0, 'Player Width' INTEGER NOT NULL DEFAULT 200, 'Player Height' INTEGER NOT NULL DEFAULT 200, 'Player X' INTEGER NOT NULL DEFAULT 0, 'Player Y' INTEGER NOT NULL DEFAULT 0, 'URI' TEXT, 'Playlist' TEXT DEFAULT '{Playlist.default[0]}', 'Shuffle' INTEGER NOT NULL DEFAULT 0, 'Loop' TEXT NOT NULL DEFAULT 'None', 'Volume' INTEGER NOT NULL DEFAULT 0.8, 'Speed' INTEGER NOT NULL DEFAULT 1.0, 'Ignore Video' INTEGER NOT NULL DEFAULT 0, 'Ignore Audio' INTEGER NOT NULL DEFAULT 0, 'Ignore Subtitle' INTEGER NOT NULL DEFAULT 0, 'Ignore Cover' INTEGER NOT NULL DEFAULT 0);
"""

INFO = {
    "ID": "io.github.kirukomaru11.Media",
    "Developer": "きるこ丸11号",
    "Comment": "Media player",
    "Issue URL": "https://github.com/kirukomaru11/Media/issues",
    "Website": "https://github.com/kirukomaru11/Media",
    "Keywords": ["Media", "Player", "Playlist", "Library", "Video"],
    "Categories": ["Audio", "AudioVideo", "Video"],
    "Branding": ["#c42d4a", "#d64e68"],
    "MimeTypes": ["x-scheme-handler/http", "x-scheme-handler/https"] + [i for i in Gio.content_types_get_registered() if i.startswith(("audio/", "video/"))],
}

APP_CSS = """
popover .font,
popover .delay { border-spacing: 14px; padding-left: 14px; }
popover .font > :last-child,
popover .delay > :last-child { margin: 2px 0px; }

.svg picture { -gtk-icon-transform: scale(0.7); background: color-mix(in srgb, var(--window-fg-color) 8%, transparent); }

player controls { color: white; background: linear-gradient(to top, color-mix(in srgb, black 50%, transparent), transparent); padding: 6px 7px; opacity: 0; transition: opacity 0.5s; min-width: 230px; min-height: 158px; }
player controls:hover { opacity: 1; }
player controls > box { border-spacing: 14px; }
player controls centerbox > box,
player controls centerbox > box > box { border-spacing: 6px; }
player controls .playbackbutton { margin: 0px 14px 0px 14px; transform: scale(1.6); }
player controls .nextbutton,
player controls .previousbutton { transform: scale(1.2); }

player.no-cover { background: black; color: white; }
player.no-cover picture { -gtk-icon-transform: scale(0.65); }

playeroverlay { border-radius: 10px; backdrop-filter: blur(10px); box-shadow: 0 0 14px 5px rgba(0,0,0,0), 0 0 10px 5px rgba(0,0,0,0.08), 0 0 0 1px rgba(0,0,0,0.05); }
playeroverlay > player,
playeroverlay > player.no-cover { background: none; }
@media not (prefers-color-scheme: dark) {
    playeroverlay > player.no-cover { color: black; }
}
playeroverlay > player.no-cover picture { -gtk-icon-transform: scale(0.7); }

.mediaseekbar { padding: 0px; }
.mediaseekbar mark label { font-size: 0px; transition: all 0.3s; }
windowhandle > player .mediaseekbar marks:hover mark label { font-size: unset; }
windowhandle > player .mediaseekbar.marks-after { padding: 12px 14px 4px 14px; }

window #Artists viewport > wrap-box { margin: 10px; }

window.media-window { min-width: 290px; min-height: 300px; }

listview row { padding: 0px; margin-bottom: 6px; }
listview label { padding: 10px 12px; border-radius: 8px; }
listview row:selected label { font-weight: bold; }
.colored listview row:selected { background-color: color-mix(in srgb, var(--color-2) 45%, transparent); }

:drop(active) .highlight { box-shadow: 0 0 0 2px var(--accent-bg-color); }

artistview viewport > box { padding: 10px 0px; }
artistview carousel { padding: 10px 0px; }

button.artist,
button.single,
button.playlist { transition: all 0.3s ease-in-out; padding: 0px; }

button.single,
button.playlist { border-radius: 13px; }

find > entry { margin-top: 10px; border-radius: 9px; backdrop-filter: blur(20px) brightness(0.9); background: color-mix(in srgb, var(--window-bg-color) 35%, transparent); }

sheet,
window,
controls.toolbar.card,
toolbarview.overview { transition: background 0.4s ease; }
.colored {
  --scrollbar-outline-color: rgb(0 0 0 / 25%);
  --accent-color: var(--color-1);
  --accent-bg-color: var(--color-1);
  --popover-bg-color: color-mix(in srgb, var(--color-2), var(--window-bg-color) 60%);
  --card-bg-color: rgb(255 255 255 / 4%);
}
.colored,
.colored sheet {
  background: linear-gradient(to bottom right, color-mix(in srgb, var(--color-1) 45%, transparent), transparent),
    linear-gradient(to bottom left, color-mix(in srgb, var(--color-2) 45%, transparent), transparent),
    linear-gradient(to top, color-mix(in srgb, var(--color-3) 45%, transparent), transparent),
    var(--window-bg-color);
}
.colored toggle:checked { background: color-mix(in srgb, var(--color-1), var(--window-bg-color) 30%); }
.colored controls.toolbar.card { background: color-mix(in srgb, var(--color-3), transparent 60%); }
.colored player { background: none; }
"""

def is_youtube(link): return is_online(link) and ("youtube." in link or "youtu.be" in link) and ("/watch" in link or "/shorts/" in link or ".be/" in link)

def youtube_id(link): return link.replace("youtu.be/", "youtube.com/watch?v=").replace("/shorts/", "/watch?v=").replace("www.", "").replace("be.com/watch?v=", ".be/").split("?")[0].rsplit("/", 1)[-1]

def item_css_bind(b, v):
    l = []
    if isinstance(v, Gtk.SymbolicPaintable): l.append("svg")
    if v == DEFAULT_PAINTABLE: l.append("no-cover")
    elif v: l.append("custom-cover")
    return l

def call(mpris=None, _c=None, _s=None, _o=None, i="", m="", p=None, inv=None):
    p = p or []
    w =  Window.current
    item = None
    if w and w.player.uri:
        item = App.default.medias[w.player.uri]
    if m == "Get":
        if p[1] in ("CanQuit", "CanRaise", "CanControl"):
            v = True
        if p[1] == "HasTrackList":
            v = False
        if p[1] == "Fullscreen":
            v = w.player.fullscreen
        if p[1] == "CanSetFullscreen":
            v = w.get_visible()
        if p[1] == "Identity":
            v = w.get_title()
        if p[1] == "DesktopEntry":
            v = App.default.get_application_id()
        if p[1] == "SupportedUriSchemes":
            v = ["file", "http", "https"]
        if p[1] == "SupportedMimeTypes":
            v = ["application/ogg", "audio/x-vorbis+ogg", "audio/x-flac", "audio/mpeg"]
        if p[1] == "PlaybackStatus":
            v = "Stopped" if not w.player.uri else "Playing" if w.player.playing else "Paused"
        if p[1] == "CanGoNext":
            v = w.playlist.can_go_next
        if p[1] == "CanGoPrevious":
            v = w.playlist.can_go_previous
        if p[1] in ("CanPlay", "CanPause"):
            v = bool(w.player.uri)
        if p[1] in ("Rate", "MinimumRate", "MaximumRate"):
            v = 1.0
        if p[1] == "CanSeek":
            v = True
        if p[1] == "LoopStatus":
            v = w.playlist.loop
        if p[1] == "Shuffle":
            v = w.playlist.shuffle
        if p[1] == "Volume":
            v = w.player.volume
        if p[1] == "Position":
            v = w.player.position / 1e3
        if p[1] == "Metadata":
            v = {
                "xesam:title": GLib.Variant("s", Media.get_name(item)),
                "xesam:artist": GLib.Variant("as", str_to_list(item.artists)),
                "xesam:album": GLib.Variant("s", w.playlist.playlist),
                "xesam:useCount": GLib.Variant("i", item.use_count),
                "mpris:artUrl": GLib.Variant("s", ""),
                "mpris:length": GLib.Variant("x", w.player.duration / 1e3)
            } if item else {}
        v = GLib.Variant(mpris.interfaces[p[0]]["properties"][p[1]]["type"], v)
        if inv: return inv.return_value(GLib.Variant("(v)", (v,)))
        else: return v
    if m == "GetAll":
        if p[0] in mpris.interfaces and mpris.interfaces[p[0]]["properties"]:
            s = dict((it, call(mpris, None, None, None, p[0], "Get", (p[0], it), None)) for it in mpris.interfaces[p[0]]["properties"])
        else:
            s = {}
        if inv: return inv.return_value(GLib.Variant("(a{sv})", (s, )))
        else: return GLib.Variant("a{sv}", s)
    if m == "Introspect" and inv: return inv.return_value(GLib.Variant("(s)", (GLib.Variant("s", mpris.xml),)))
    if m == "Set":
        if p[1] == "Fullscreen": setattr(w.player, "fullscreen", not w.player.fullscreen)
        if p[1] == "LoopStatus": setattr(w.playlist, "loop", p[2])
        if p[1] == "Volume": setattr(w.player, "volume", max(0, p[2]))
        if p[1] == "Shuffle": setattr(w.playlist, "shuffle", p[2])
    if m in ("Next", "Previous"): (w.playlist.next if m == "Next" else w.playlist.previous)()
    if m == "Pause": w.player.pause()
    if m == "Play": w.player.play()
    if m == "PlayPause": w.player.playpause()
    if m == "Stop": setattr(w.player, "uri", "")
    if m == "Raise": w.present()
    if m == "Quit": App.quit_default()
    if m == "SetPosition": setattr(w.player, "position", p[1] * 1000)
    if m == "Seek": setattr(w.player, "position", w.player.position + (p[0] * 1000))
    if inv: inv.return_value(None)

def main():
    App(info=INFO,
        shortcuts={
            "Player": {
                **MediaControls.shortcuts,
                "Previous": "<shift>a",
                "Next": "<shift>d",
                "Maximize": "f",
                "Fullscreen": "<shift>f",
                "Previous Frame": "comma",
                "Next Frame": "period"
            },
            "Window": {
                "Edit Focused Media/Artist/Playlist": "<primary>e",
                "Show Hidden": "<primary>h",
                "Search": "<primary>f",
                "Stop Search": "Escape",
                "Quit": "<primary>q",
            }
        },
        activate=App.activate,
        data=DATA_INIT,
        file_open=App.file_open,
        actions=(
            ("quit", App.quit_default, "<primary>q"),
        ))
    Gst.init()
    global DEFAULT_PAINTABLE
    try:
        DEFAULT_PAINTABLE = Gtk.Svg.new_from_resource("/org/gtk/libgtk/icons/folder-music-symbolic.svg")
    except:
        DEFAULT_PAINTABLE = Gtk.IconPaintable.new_for_file(Gio.File.new_for_path(Path(sys._MEIPASS if hasattr(sys, "_MEIPASS") else "usr", "share", "icons", "Adwaita", "symbolic", "places", "folder-music-symbolic.svg")), 1, 1)
    global yt_dlp
    yt_dlp = ""
    l = [App.default.data_folder.get_path(), "/usr/bin", "/bin"]
    if FLATPAK_ID: l.extend(["/run/host/bin", "/run/host/usr/bin", "/run/host/root/bin", "/run/host/root/usr/bin"])
    for i in l:
        files = [Path(i, "yt-dlp")]
        if sys.platform.startswith("win"): files.append(Path(i, "yt-dlp.exe"))
        if sys.platform == "darwin": files.append(Path(i, "yt-dlp_macos"))
        if sys.platform == "linux": files.append(Path(i, "yt-dlp_linux"))
        file = next((it for it in files if exists(it)), False)
        if file:
            if FLATPAK_ID and file.startswith("/run/host"):
                file = f"/usr/bin/flatpak-spawn --host {file.split('/run/host/root', 1)[-1].split('/run/host', 1)[-1]}"
            if file.startswith(App.default.data_folder.get_path()):
                result = GLib.spawn_command_line_sync(file + f" --update-to {GLib.getenv('YT_DLP_CHANNEL') or 'master'}")[1].decode()
                print(result)
            yt_dlp = (file + ' -J --skip-download' + " " + (GLib.getenv("YT_DLP_COMMAND") or " ")).strip()
            global json
            from json import loads as json
            break
    if not yt_dlp: print("No yt-dlp")
    App.default.infos = {}
    App.default.set_accels_for_action("win.show-hidden", ["<primary>h"])
    if sys.platform == "linux":
        App.default.MPRIS = DBUS(
            { "org.freedesktop.DBus.Introspectable": {
                "methods": { "Introspect": { "data": { "direction": "out", "type": "s" } } },
                "signals": {},
                "properties": {}
             },
            "org.freedesktop.DBus.Properties": {
                "methods": {
                  "Set": { "interface_name": { "direction": "in", "type": "s" }, "property_name": { "direction": "in", "type": "s" }, "value": { "direction": "in", "type": "v" } },
                  "Get": { "interface_name": { "direction": "in", "type": "s" }, "property": { "direction": "in", "type": "s" }, "value": { "direction": "out", "type": "v" } },
                  "GetAll": { "interface": { "direction": "in", "type": "s" }, "properties": { "direction": "out", "type": "a{sv}" } }
                },
                "signals": { "PropertiesChanged": { "interface_name": "s", "changed_properties": "a{sv}", "invalidated_properties": "as" } },
                "properties": {}
             },
             "org.mpris.MediaPlayer2": {
                "methods": { "Raise": {}, "Quit": {}, },
                "signals": {},
                "properties": {
                    "CanQuit": { "type": "b", "access": "read" },
                    "Fullscreen": { "type": "b", "access": "readwrite" },
                    "CanSetFullscreen": { "type": "b", "access": "read" },
                    "CanRaise": { "type": "b", "access": "read" },
                    "HasTrackList": { "type": "b", "access": "read" },
                    "Identity": { "type": "s", "access": "read" },
                    "DesktopEntry": { "type": "s", "access": "read" },
                    "SupportedUriSchemes": { "type": "as", "access": "read" },
                    "SupportedMimeTypes": { "type": "as", "access": "read" } },
                },
             "org.mpris.MediaPlayer2.Player": {
                "methods": {
                  "Next": {}, "Previous": {}, "Pause": {}, "Play": {}, "PlayPause": {}, "Stop": {},
                  "SetPosition": { "TrackId": { "direction": "in", "type": "o" }, "Position": { "direction": "in", "type": "x" }, },
                  "Seek": { "Offset": { "direction": "in", "type": "x" }, },
                },
                "signals": {},
                "properties": {
                    "PlaybackStatus": { "type": "s", "access": "read" },
                    "CanGoNext": { "type": "b", "access": "read" },
                    "CanGoPrevious": { "type": "b", "access": "read" },
                    "CanPlay": { "type": "b", "access": "read" },
                    "CanPause": { "type": "b", "access": "read" },
                    "CanControl": { "type": "b", "access": "read" },
                    "Rate": { "type": "d", "access": "readwrite" },
                    "MinimumRate": { "type": "d", "access": "read" },
                    "MaximumRate": { "type": "d", "access": "read" },
                    "CanSeek": { "type": "b", "access": "read" },
                    "LoopStatus": { "type": "s", "access": "readwrite" },
                    "Shuffle": { "type": "b", "access": "readwrite" },
                    "Volume": { "type": "d", "access": "readwrite" },
                    "Position": { "type": "x", "access": "read" },
                    "Metadata": { "type": "a{sv}", "access": "read" }
                }
         }}, f"org.mpris.MediaPlayer2.{App.default.get_application_id()}", "/org/mpris/MediaPlayer2", call)
    App.default.main(css=APP_CSS)

if __name__ == "__main__": main()