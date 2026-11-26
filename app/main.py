#!/usr/bin/env python3
"""
Wallpaper Studio
Hover a thumbnail to live-preview a wallpaper (hyprpaper) and its matugen theme.
Click to apply. Escape / closing without clicking reverts to the original.
"""

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, Gdk, GLib

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path


# --------------------------------------------------
# CONFIGURATION
# --------------------------------------------------

HOME = Path.home()

WALLPAPER_DIR = HOME / "Pictures/Wallpapers"

APP_DIR = HOME / ".local/share/wallpaper-studio"
CACHE_DIR = APP_DIR / "cache"
THUMB_DIR = CACHE_DIR / "thumbnails"
PALETTE_DIR = CACHE_DIR / "palettes"

MONITORS = [
    "HDMI-A-3",
    "DP-1",
]

RUNTIME_DIR = Path(
    os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
)

SOCKET_PATH = RUNTIME_DIR / "wallpaper-studio.sock"

STATE_FILE = APP_DIR / "state.json"

SUPPORTED = {".jpg", ".jpeg", ".png", ".webp"}

MAGICK = shutil.which("magick") or "/usr/sbin/magick"

# GTK theme restored after an apply. Toggling it makes already-running GTK
# apps and Chromium-based browsers (Brave with the GTK theme) re-read colours.
GTK_THEME_NAME = "adw-gtk3-dark"
RELOAD_GTK_ON_APPLY = True
GTK_RELOAD_DELAY = 4.0   # seconds on the temporary theme; raise if Brave misses it

# "alias": switch between two identical copies of the theme (one clean change
#          per apply, no intermediate theme). "bounce": go via Adwaita and back.
GTK_RELOAD_MODE = "bounce"
GTK_THEME_ALIAS = GTK_THEME_NAME + "-b"

THUMB_W, THUMB_H = 208, 117

# Which matugen colours appear as swatches, in order. Any matugen colour
# role works (primary, on_primary, primary_container, secondary, tertiary,
# error, background, surface, surface_variant, outline, inverse_primary,
# surface_container_high, ...). About 10-12 fit across the window.
PALETTE_MODE = "light"   # "light", "dark" or "default"

PALETTE_ROLES = [
    "primary",
    "on_primary",
    "primary_container",
    "secondary",
    "secondary_container",
    "tertiary",
    "tertiary_container",
    "surface",
    "surface_container",
    "outline",
]

HEX_RE = re.compile(r"#[0-9a-fA-F]{6}\b")

for directory in (APP_DIR, THUMB_DIR, PALETTE_DIR):
    directory.mkdir(parents=True, exist_ok=True)


# --------------------------------------------------
# HELPERS
# --------------------------------------------------

def run_command(args, timeout=90):
    try:
        return subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"Command error: {exc}")
        return None


def make_provider(css):
    provider = Gtk.CssProvider()

    if hasattr(provider, "load_from_string"):   # GTK >= 4.12
        provider.load_from_string(css)
    else:
        provider.load_from_data(css.encode())

    return provider


def find_role(data, role, mode):
    """Looks up one colour role in matugen's JSON, tolerating both
    colors[mode][role] and colors[role][mode] layouts."""

    colors = data.get("colors", data) if isinstance(data, dict) else {}

    lookups = (
        lambda: colors[mode][role],
        lambda: colors[role][mode],
        lambda: colors[role]["default"],
        lambda: colors[role],
    )

    for lookup in lookups:
        try:
            value = lookup()
        except (KeyError, TypeError):
            continue

        match = HEX_RE.search(json.dumps(value))

        if match:
            return match.group(0)

    return None


def extract_palette(data):
    """Returns [(role, hex), ...] for PALETTE_ROLES. If none of the roles
    are found (unexpected JSON layout), falls back to the first unique hex
    values in the file."""

    palette = []

    for role in PALETTE_ROLES:
        color = find_role(data, role, PALETTE_MODE)

        if color:
            palette.append((role, color))

    if palette:
        return palette

    raw = HEX_RE.findall(json.dumps(data))
    unique = list(dict.fromkeys(raw))[:len(PALETTE_ROLES)]

    return [(f"color {i + 1}", c) for i, c in enumerate(unique)]


def list_wallpapers():
    if not WALLPAPER_DIR.exists():
        return []

    return sorted(
        p for p in WALLPAPER_DIR.iterdir()
        if p.is_file() and p.suffix.lower() in SUPPORTED
    )


def is_fresh(cache, source):
    return cache.exists() and cache.stat().st_mtime >= source.stat().st_mtime


# --------------------------------------------------
# WALLPAPER MANAGEMENT
# --------------------------------------------------

def set_wallpaper(path):
    path = str(Path(path).expanduser().resolve())

    success = True

    for monitor in MONITORS:
        result = run_command([
            "hyprctl",
            "hyprpaper",
            "wallpaper",
            f"{monitor}, {path}"
        ])

        if result is None or result.returncode != 0:
            print(f"Failed to set wallpaper on {monitor}")
            success = False

    return success


def get_active_wallpaper(monitor):
    result = run_command(["hyprctl", "hyprpaper", "listactive"])

    if not result or result.returncode != 0:
        return None

    for line in result.stdout.splitlines():
        match = re.match(r"\s*(\S+?)\s*[:=]\s*(.+?)\s*$", line)

        if match and match.group(1) == monitor:
            return match.group(2)

    return None


# --------------------------------------------------
# MATUGEN
# --------------------------------------------------

def ensure_theme_alias():
    """Creates ~/.local/share/themes/<theme>-b as a symlink to the real theme
    so GTK sees two different theme names with identical contents."""

    themes_dir = HOME / ".local/share/themes"
    alias = themes_dir / GTK_THEME_ALIAS

    if alias.exists() or alias.is_symlink():
        return True

    for base in (Path("/usr/share/themes"), themes_dir, HOME / ".themes"):
        source = base / GTK_THEME_NAME

        if source.is_dir():
            try:
                themes_dir.mkdir(parents=True, exist_ok=True)
                alias.symlink_to(source)
                return True
            except OSError as exc:
                print(f"Could not create theme alias: {exc}")
                return False

    print(f"Theme '{GTK_THEME_NAME}' not found; cannot create alias")
    return False


def reload_gtk_apps():
    """Nudges running GTK apps / Brave to re-read the generated colours."""

    if not RELOAD_GTK_ON_APPLY:
        return

    schema = ["gsettings", "set", "org.gnome.desktop.interface", "gtk-theme"]

    if GTK_RELOAD_MODE == "alias" and ensure_theme_alias():

        current = run_command(
            ["gsettings", "get", "org.gnome.desktop.interface", "gtk-theme"],
            timeout=10
        )

        name = ""

        if current and current.returncode == 0:
            name = current.stdout.strip().strip("'")

        target = GTK_THEME_NAME if name == GTK_THEME_ALIAS else GTK_THEME_ALIAS

        run_command(schema + [target], timeout=10)
        return

    run_command(schema + ["Adwaita"], timeout=10)
    time.sleep(GTK_RELOAD_DELAY)
    run_command(schema + [GTK_THEME_NAME], timeout=10)


def generate_theme(job):
    """job is (path, reload_apps). Hover previews pass reload_apps=False so
    open apps don't flicker; apply passes True."""

    path, reload_apps = job if isinstance(job, tuple) else (job, False)

    result = run_command([
        "matugen",
        "image",
        str(path),
        "--prefer",
        "saturation",
        "-m",
        "light"
    ], timeout=120)

    if result is None or result.returncode != 0:
        print(
            "Matugen failed:",
            result.stderr if result else "No result"
        )
        return False

    reload_result = run_command(["hyprctl", "reload"])

    if reload_result is None or reload_result.returncode != 0:
        print("Warning: Hyprland reload failed")

    if reload_apps:
        reload_gtk_apps()

    return True


# --------------------------------------------------
# PERSISTENCE / RESTORE AT LOGIN
# --------------------------------------------------

def save_state(path):
    try:
        STATE_FILE.write_text(
            json.dumps({"wallpaper": str(Path(path).resolve())})
        )
    except OSError as exc:
        print(f"Could not save state: {exc}")


def load_state():
    try:
        return json.loads(STATE_FILE.read_text()).get("wallpaper")
    except (OSError, json.JSONDecodeError, AttributeError):
        return None


def restore():
    """Headless mode for login: re-apply the last applied wallpaper and
    regenerate its matugen theme so both always match."""

    path = load_state()

    if not path or not Path(path).exists():
        print("No saved wallpaper to restore")
        return 1

    # hyprpaper may still be starting; retry for up to ~15 seconds
    for _ in range(30):
        if set_wallpaper(path):
            break
        time.sleep(0.5)

    generate_theme((path, True))

    return 0


# --------------------------------------------------
# BACKGROUND "LATEST WINS" WORKER
# --------------------------------------------------

class LatestWorker:
    """
    Runs func(arg) on a background thread. If several submissions arrive
    while busy, only the newest is kept. shutdown() lets the pending job
    finish, then stops the thread. Safe to call shutdown() more than once.
    The thread is non-daemon so a final job (e.g. reverting) can finish
    after the window closes.
    """

    def __init__(self, func):
        self.func = func
        self.cond = threading.Condition()
        self.pending = None
        self.stopping = False

        self.thread = threading.Thread(target=self.work, daemon=False)
        self.thread.start()

    def submit(self, arg):
        with self.cond:
            if self.stopping:
                return

            self.pending = arg
            self.cond.notify()

    def shutdown(self):
        with self.cond:
            self.stopping = True
            self.cond.notify()

    def work(self):
        while True:
            with self.cond:
                while self.pending is None and not self.stopping:
                    self.cond.wait()

                if self.pending is None:
                    return

                arg = self.pending
                self.pending = None

            try:
                self.func(arg)
            except Exception as exc:
                print(f"Worker error: {exc}")


# --------------------------------------------------
# MAIN APPLICATION
# --------------------------------------------------

class WallpaperStudio(Gtk.Application):

    def __init__(self):
        super().__init__(
            application_id="com.wallpaperstudio.app"
        )

        self.window = None

        self.original = None
        self.current = None
        self.applied = False

        self.hover_source = None

        self.wallpaper_worker = LatestWorker(set_wallpaper)
        self.theme_worker = LatestWorker(generate_theme)

        self.palette_provider = None

        self.ipc_socket = None
        self.ipc_thread = None

        self.closing = False

        # Keyboard navigation state
        self.cards = []          # (path, card, flowbox child)
        self.index = None        # highlighted card
        self.last_key_time = 0

    # --------------------------------------------------
    # WINDOW
    # --------------------------------------------------

    def do_activate(self):

        # A second launch just focuses the existing window
        if self.window is not None:
            self.window.present()
            return

        self.window = Gtk.ApplicationWindow(application=self)

        self.window.set_title("Wallpaper Studio")
        self.window.set_default_size(685, 490)
        self.window.set_resizable(False)
        self.window.set_decorated(False)

        self.window.connect("close-request", self.on_close)

        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self.on_key)
        self.window.add_controller(keys)

        self.add_styles()

        root = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=8
        )

        root.set_margin_top(15)
        root.set_margin_bottom(15)
        root.set_margin_start(15)
        root.set_margin_end(15)

        self.window.set_child(root)

        # STATUS

        self.status = Gtk.Label(
            label="Hover or use arrow keys to preview, Enter/click to apply, Esc to cancel"
        )

        self.status.add_css_class("dim-label")
        self.status.set_halign(Gtk.Align.START)

        root.append(self.status)

        # GALLERY

        self.scroll = scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        scroll.set_policy(
            Gtk.PolicyType.NEVER,
            Gtk.PolicyType.AUTOMATIC
        )

        self.gallery = Gtk.FlowBox()
        self.gallery.set_selection_mode(Gtk.SelectionMode.NONE)
        self.gallery.set_homogeneous(True)
        self.gallery.set_min_children_per_line(3)
        self.gallery.set_max_children_per_line(3)
        self.gallery.set_column_spacing(6)
        self.gallery.set_row_spacing(6)

        scroll.set_child(self.gallery)
        root.append(scroll)

        # PALETTE (created before the gallery so hover handlers can use it)

        self.palette_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=5
        )

        self.palette_box.set_homogeneous(True)
        self.palette_box.set_hexpand(True)

        root.append(self.palette_box)

        files, pending_thumbs = self.load_wallpapers()
        self.highlight_current()

        # IPC

        self.start_ipc()

        self.window.present()

        # BACKGROUND THUMBNAIL + PALETTE CACHE

        threading.Thread(
            target=self.build_caches,
            args=(files, pending_thumbs),
            daemon=True
        ).start()

    # --------------------------------------------------
    # GTK STYLING
    # --------------------------------------------------

    def add_styles(self):

        css = """
        window {
            background: #171b20;
            color: #e5e9f0;
        }

        .wallpaper-card {
            background: transparent;
            border-radius: 10px;
            padding: 0;
            border: 2px solid #343c47;
            overflow: hidden;
        }

        .wallpaper-card.selected {
            border-color: #82d3e1;
        }
        """

        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            make_provider(css),
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

    # --------------------------------------------------
    # LOAD WALLPAPERS
    # --------------------------------------------------

    def load_wallpapers(self):

        if not WALLPAPER_DIR.exists():
            self.status.set_label(
                f"Wallpaper folder not found: {WALLPAPER_DIR}"
            )
            return [], []

        files = list_wallpapers()

        if not files:
            self.status.set_label(
                f"No wallpapers found in {WALLPAPER_DIR}"
            )
            return [], []

        self.original = get_active_wallpaper(MONITORS[0])
        self.current = self.original

        pending_thumbs = []

        for path in files:
            pending = self.add_wallpaper(path)

            if pending:
                pending_thumbs.append(pending)

        return files, pending_thumbs

    # --------------------------------------------------
    # WALLPAPER CARDS
    # --------------------------------------------------

    def add_wallpaper(self, path):
        """Adds a card. Returns (path, thumb, picture) if the thumbnail
        still needs generating, otherwise None."""

        card = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=0
        )

        card.add_css_class("wallpaper-card")
        card.set_tooltip_text(path.name)

        thumb = THUMB_DIR / f"{path.name}.jpg"

        picture = Gtk.Picture()
        picture.set_size_request(THUMB_W, THUMB_H)
        picture.set_content_fit(Gtk.ContentFit.COVER)
        picture.set_can_shrink(True)

        pending = None

        if is_fresh(thumb, path):
            picture.set_filename(str(thumb))
        else:
            pending = (path, thumb, picture)

        card.append(picture)

        motion = Gtk.EventControllerMotion()
        motion.connect(
            "enter",
            lambda *_args, p=path: self.on_mouse_enter(p)
        )
        card.add_controller(motion)

        click = Gtk.GestureClick()
        click.connect(
            "released",
            lambda *_args, p=path: self.on_apply(p)
        )
        card.add_controller(click)

        child = Gtk.FlowBoxChild()
        child.set_child(card)
        child.set_focusable(False)

        self.gallery.append(child)
        self.cards.append((path, card, child))

        return pending

    # --------------------------------------------------
    # LIVE PREVIEW
    # --------------------------------------------------

    def on_mouse_enter(self, path):

        # Ignore pointer "enter" events caused by the grid scrolling under
        # a stationary mouse while navigating with the keyboard
        if GLib.get_monotonic_time() - self.last_key_time < 250_000:
            return

        self.on_hover(path)

    def index_of(self, path):

        target = Path(path)

        for i, (p, _card, _child) in enumerate(self.cards):
            if p == target:
                return i

        return None

    def set_highlight(self, i):

        if i is None or i == self.index:
            return

        if self.index is not None:
            self.cards[self.index][1].remove_css_class("selected")

        self.index = i
        self.cards[i][1].add_css_class("selected")

    def highlight_current(self):

        if not self.current:
            return

        i = self.index_of(self.current)

        if i is not None:
            self.set_highlight(i)
            GLib.timeout_add(150, self.scroll_selected)

    def scroll_selected(self):

        if self.index is not None:
            self.scroll_to(self.cards[self.index][2])

        return GLib.SOURCE_REMOVE

    def scroll_to(self, child):

        ok, rect = child.compute_bounds(self.gallery)

        if not ok:
            return

        adj = self.scroll.get_vadjustment()

        top = rect.get_y()
        bottom = top + rect.get_height()

        if top < adj.get_value():
            adj.set_value(top - 6)

        elif bottom > adj.get_value() + adj.get_page_size():
            adj.set_value(bottom - adj.get_page_size() + 6)

    def move_selection(self, keyval):

        count = len(self.cards)

        if count == 0:
            return

        columns = 3

        if self.index is None:
            new = 0

        else:
            i = self.index
            new = i

            if keyval == Gdk.KEY_Left:
                new = i - 1

            elif keyval == Gdk.KEY_Right:
                new = i + 1

            elif keyval == Gdk.KEY_Up:
                new = i - columns

            elif keyval == Gdk.KEY_Down:
                new = i + columns

                # Partial last row: jump to the final item
                if new >= count and i // columns < (count - 1) // columns:
                    new = count - 1

            elif keyval == Gdk.KEY_Home:
                new = 0

            elif keyval == Gdk.KEY_End:
                new = count - 1

            if new < 0 or new >= count:
                new = i

        if new == self.index:
            return

        self.on_hover(self.cards[new][0])
        self.scroll_to(self.cards[new][2])

    def on_hover(self, path):

        path_str = str(path)

        self.set_highlight(self.index_of(path))
        self.update_palette(path)

        self.status.set_label(f"Previewing: {path.name}")

        if path_str != self.current:
            self.current = path_str
            self.wallpaper_worker.submit(path_str)

        if self.hover_source is not None:
            GLib.source_remove(self.hover_source)

        self.hover_source = GLib.timeout_add(
            350,
            self.start_theme_preview,
            path_str
        )

    def start_theme_preview(self, path):

        self.hover_source = None

        self.theme_worker.submit((path, False))

        return GLib.SOURCE_REMOVE

    # --------------------------------------------------
    # BACKGROUND CACHES (thumbnails, then palettes)
    # --------------------------------------------------

    def build_caches(self, files, pending_thumbs):

        for path, thumb, picture in pending_thumbs:

            if self.closing:
                return

            run_command([
                MAGICK,
                str(path),
                "-thumbnail",
                f"{THUMB_W}x{THUMB_H}^",
                "-gravity",
                "center",
                "-extent",
                f"{THUMB_W}x{THUMB_H}",
                str(thumb)
            ], timeout=30)

            source = thumb if thumb.exists() else path

            GLib.idle_add(self.set_picture, picture, str(source))

        self.cache_palettes(files)

    def set_picture(self, picture, filename):
        picture.set_filename(filename)
        return GLib.SOURCE_REMOVE

    def cache_palettes(self, files):

        for path in files:

            if self.closing:
                return

            cache = PALETTE_DIR / f"{path.name}.json"

            if is_fresh(cache, path):
                continue

            result = run_command([
                "matugen",
                "image",
                str(path),
                "--prefer",
                "saturation",
                "-m",
                "light",
                "--dry-run",
                "--json",
                "hex",
                "--include-image-in-json",
                "false"
            ], timeout=120)

            if result and result.returncode == 0:

                try:
                    data = json.loads(result.stdout)

                    cache.write_text(json.dumps(data, indent=2))

                    # Refresh swatches if this is the wallpaper being previewed
                    if self.current == str(path):
                        GLib.idle_add(self.refresh_palette, str(path))

                except (json.JSONDecodeError, OSError) as exc:
                    print("Palette cache error:", exc)

    # --------------------------------------------------
    # DISPLAY MATUGEN PALETTE
    # --------------------------------------------------

    def refresh_palette(self, path):
        self.update_palette(path)
        return GLib.SOURCE_REMOVE

    def update_palette(self, path):

        # Clear existing swatches and their CSS
        while self.palette_box.get_first_child():
            self.palette_box.remove(self.palette_box.get_first_child())

        if self.palette_provider is not None:
            Gtk.StyleContext.remove_provider_for_display(
                Gdk.Display.get_default(),
                self.palette_provider
            )
            self.palette_provider = None

        cache = PALETTE_DIR / f"{Path(path).name}.json"

        if not cache.exists():
            return

        try:
            data = json.loads(cache.read_text())

            colors = extract_palette(data)

        except (json.JSONDecodeError, OSError) as exc:
            print("Palette display error:", exc)
            return

        css = ""

        for i, (role, color) in enumerate(colors):

            css += f"""
            button.palette-swatch-{i} {{
                background: {color};
                border: 1px solid alpha(white, 0.25);
                border-radius: 6px;
                padding: 0;
                min-height: 28px;
                min-width: 54px;
            }}

            button.palette-swatch-{i}:hover {{
                border: 2px solid white;
            }}
            """

            button = Gtk.Button()
            button.add_css_class(f"palette-swatch-{i}")
            button.set_size_request(54, 28)
            button.set_tooltip_text(f"{role}: {color} (click to copy)")

            button.connect(
                "clicked",
                lambda _b, value=color: self.copy_color(value)
            )

            self.palette_box.append(button)

        # One provider for all swatches
        self.palette_provider = make_provider(css)

        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            self.palette_provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

    def copy_color(self, value):
        Gdk.Display.get_default().get_clipboard().set(value)
        self.status.set_label(f"Copied {value}")

    # --------------------------------------------------
    # APPLY / CANCEL
    # --------------------------------------------------

    def on_apply(self, path=None):

        path = str(path) if path else self.current

        if not path:
            return

        self.applied = True
        self.current = path

        save_state(path)

        self.wallpaper_worker.submit(path)
        self.theme_worker.submit((path, True))

        self.window.close()

    def on_cancel(self, *_args):
        self.window.close()

    def on_key(self, _controller, keyval, _keycode, _state):

        if keyval == Gdk.KEY_Escape:
            self.on_cancel()
            return True

        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_space):

            if self.index is not None:
                self.on_apply(self.cards[self.index][0])
            else:
                self.on_apply()

            return True

        if keyval in (
            Gdk.KEY_Left, Gdk.KEY_Right, Gdk.KEY_Up, Gdk.KEY_Down,
            Gdk.KEY_Home, Gdk.KEY_End
        ):
            self.last_key_time = GLib.get_monotonic_time()
            self.move_selection(keyval)
            return True

        return False

    # --------------------------------------------------
    # IPC SERVER
    # --------------------------------------------------

    def start_ipc(self):

        try:

            SOCKET_PATH.unlink(missing_ok=True)

            self.ipc_socket = socket.socket(
                socket.AF_UNIX,
                socket.SOCK_STREAM
            )

            self.ipc_socket.bind(str(SOCKET_PATH))
            self.ipc_socket.listen(2)
            self.ipc_socket.settimeout(0.5)

            self.ipc_thread = threading.Thread(
                target=self.ipc_loop,
                daemon=True
            )

            self.ipc_thread.start()

        except OSError as exc:
            print(f"IPC startup error: {exc}")

    def ipc_loop(self):

        while self.ipc_socket:

            try:
                conn, _ = self.ipc_socket.accept()

            except socket.timeout:
                continue

            except OSError:
                break

            with conn:

                conn.settimeout(1.0)

                try:
                    command = conn.recv(32).decode().strip()
                except (OSError, UnicodeDecodeError):
                    continue

                if command == "CLOSE":
                    GLib.idle_add(self.close_window)

    def close_window(self):
        self.window.close()
        return GLib.SOURCE_REMOVE

    def stop_ipc(self):

        if self.ipc_socket:
            self.ipc_socket.close()
            self.ipc_socket = None

        SOCKET_PATH.unlink(missing_ok=True)

    # --------------------------------------------------
    # CLEANUP
    # --------------------------------------------------

    def on_close(self, *_args):

        if self.closing:
            return False

        self.closing = True

        if self.hover_source is not None:
            GLib.source_remove(self.hover_source)
            self.hover_source = None

        self.stop_ipc()

        # Revert unless the user applied something
        if not self.applied and self.original:
            self.wallpaper_worker.submit(self.original)
            self.theme_worker.submit((self.original, False))

        # Workers finish their last job, then exit
        self.wallpaper_worker.shutdown()
        self.theme_worker.shutdown()

        return False


# --------------------------------------------------
# APPLICATION ENTRY POINT
# --------------------------------------------------

if __name__ == "__main__":

    if "--restore" in sys.argv:
        sys.exit(restore())

    app = WallpaperStudio()
    app.run()