bl_info = {
    "name": "ZB-Nav",
    "author": "supokede, Cursor",
    "version": (1, 27, 0),
    "blender": (4, 0, 0),
    "location": "3D View Header > ZBrush",
    "description": "在 Blender 雕刻模式中启用 ZBrush 风格的视图导航子模式",
    "category": "3D View",
}

import math
import os
import time

import blf
import bpy
import gpu
import mathutils
from bpy.props import EnumProperty, FloatProperty, FloatVectorProperty, StringProperty
from bpy_extras import view3d_utils
from gpu_extras.batch import batch_for_shader

ADDON_KEYMAPS = []
SCULPT_BRUSH_MODIFIERS = []
NAV_MODE_PROP = "zb_nav_mode"
ROTATE_METHOD_RESTORE_PROP = "_zb_nav_rotate_method_restore"
VIEW_SENSITIVITY_RESTORE_PROP = "_zb_nav_view_sensitivity_restore"
HEADER_REGISTERED_PROP = "_zb_nav_view3d_header_registered"
VIEW3D_DRAW_HANDLER = None
MOVE_MODE_HOVER = None
GIZMO_HOVER_MONITOR_RUNNING = False
# Vertices at or above this mask value are left alone by gizmo transforms.
MASK_SKIP_THRESHOLD = 0.999
# The move-tool lookup walks a private Blender API, so cache its answer briefly.
TOOL_CACHE_SECONDS = 0.15
_MOVE_TOOL_CACHE = {"time": -1.0, "active": False}
# Alt+left drag only counts as a drag past this distance, in pixels.
ALT_DRAG_THRESHOLD = 6.0
BRUSH_SIZE_MIN_FALLBACK = 1
BRUSH_SIZE_MAX_FALLBACK = 5000
# Keymaps the brush size trigger may have to take a key from.
TRIGGER_KEYMAP_NAMES = (
    "Window",
    "Frames",
    "Sculpt",
    "3D View",
    "3D View Generic",
)
TRIGGER_KEY_ITEMS = (
    ("S", "S 键", "按住 S 再按住左键左右拖动"),
    ("SPACE", "空格键", "按住空格再按住左键左右拖动"),
)
_TRIGGER_KEY_YIELDED = None
_TRIGGER_KEY_YIELDED_ITEMS = []
_MODE_SHORTCUT_YIELDED_ITEMS = []
_REPORTED_DRAW_ERRORS = set()
_CAPSLOCK_LAST_STATE = None
FLOATING_PANEL_STATES = {}
ISOLATION_STATES = {}

FLOATING_PANEL_WIDTH = 124.0
FLOATING_PANEL_TITLE_HEIGHT = 28.0
FLOATING_PANEL_BUTTON_WIDTH = 52.0
FLOATING_PANEL_BUTTON_HEIGHT = 40.0
FLOATING_PANEL_GAP = 6.0
FLOATING_PANEL_PADDING = 7.0
FLOATING_PANEL_HALF_HEIGHT = 20.0
FLOATING_PANEL_SENSITIVITY_HEIGHT = 34.0
SUBDIVISION_LEVEL_MIN = 0
SUBDIVISION_LEVEL_MAX = 6
SUBDIVISION_MODIFIER_NAME = "ZB Nav Subdivision"
VIEW_SENSITIVITY_MIN = 0.1
VIEW_SENSITIVITY_MAX = 3.0
MODE_SHORTCUT_KEYMAPS = (
    "Window",
    "Screen",
    "Frames",
    "Sculpt",
    "3D View",
    "3D View Generic",
    "Object Mode",
    "Mesh",
)
CTRL_NUMBER_TYPES = {
    "ZERO", "ONE", "TWO", "THREE", "FOUR",
    "FIVE", "SIX", "SEVEN", "EIGHT", "NINE",
    "NUMPAD_0", "NUMPAD_1", "NUMPAD_2", "NUMPAD_3", "NUMPAD_4",
    "NUMPAD_5", "NUMPAD_6", "NUMPAD_7", "NUMPAD_8", "NUMPAD_9",
}

MOVE_GIZMO_PIVOT = None
ZBRUSH_KEYMAP_ITEMS = [
    {
        "keymap": "3D View",
        "space_type": "VIEW_3D",
        "idname": "zb_nav.pan_or_zoom",
        "type": "MIDDLEMOUSE",
        "value": "PRESS",
        "alt": True,
        "properties": {},
    },
    {
        "keymap": "3D View",
        "space_type": "VIEW_3D",
        "idname": "zb_nav.alt_select_target",
        "type": "LEFTMOUSE",
        "value": "PRESS",
        "alt": True,
        "properties": {},
    },
    {
        "keymap": "Sculpt",
        "space_type": "EMPTY",
        "idname": "zb_nav.alt_select_target",
        "type": "LEFTMOUSE",
        "value": "PRESS",
        "alt": True,
        "properties": {},
    },
    {
        "keymap": "3D View",
        "space_type": "VIEW_3D",
        "idname": "zb_nav.tool_shortcut",
        "type": "F",
        "value": "PRESS",
        "ctrl": True,
        "properties": {"action": "subdiv:toggle"},
    },
    {
        "keymap": "3D View",
        "space_type": "VIEW_3D",
        "idname": "zb_nav.tool_shortcut",
        "type": "A",
        "value": "PRESS",
        "ctrl": True,
        "properties": {"action": "wire:toggle"},
    },
]

def get_preferences(context):
    addon = context.preferences.addons.get(__name__)
    if addon:
        return addon.preferences
    return None


def _pref(context, name, default=None):
    """Read one add-on preference, falling back to a literal default."""
    try:
        prefs = get_preferences(context)
    except Exception:
        prefs = None
    if prefs is None:
        return default
    value = getattr(prefs, name, default)
    return default if value is None else value


def _ui_scale(context):
    """Blender's interface scale, so drawn pixels follow high DPI screens."""
    try:
        return max(0.5, float(context.preferences.system.ui_scale))
    except (AttributeError, TypeError, ValueError):
        return 1.0


def _px(context, pixels):
    """A UI pixel constant converted to region pixels at the current scale."""
    return float(pixels) * _ui_scale(context)


def _point_in_rect(x, y, rect):
    return rect[0] <= x <= rect[0] + rect[2] and rect[1] <= y <= rect[1] + rect[3]


def _floating_panel_layout(context):
    """Return panel geometry and remember its position per 3D View area."""
    area = context.area
    region = context.region
    if area is None or region is None:
        return None

    key = area.as_pointer()
    state = FLOATING_PANEL_STATES.get(key)
    if state is None:
        state = {
            "x": None,
            "y": None,
            "mouse": None,
            "dragging": False,
            "slider_dragging": False,
            "rotation_slider_dragging": False,
            "zoom_slider_dragging": False,
            "drag_offset": (0.0, 0.0),
        }
        FLOATING_PANEL_STATES[key] = state

    padding = _px(context, FLOATING_PANEL_PADDING)
    gap = _px(context, FLOATING_PANEL_GAP)
    title_height = _px(context, FLOATING_PANEL_TITLE_HEIGHT)
    button_width = _px(context, FLOATING_PANEL_BUTTON_WIDTH)
    button_height = _px(context, FLOATING_PANEL_BUTTON_HEIGHT)
    half_height = _px(context, FLOATING_PANEL_HALF_HEIGHT)
    sensitivity_height = _px(context, FLOATING_PANEL_SENSITIVITY_HEIGHT)
    width = _px(context, FLOATING_PANEL_WIDTH)
    height = (
        title_height
        + padding
        + button_height
        + gap * 5
        + half_height * 3
        + sensitivity_height * 2
        + padding
    )

    if state["x"] is None or state["y"] is None:
        stored_position = _pref(context, "floating_panel_position", (0.05, 0.84))
        try:
            position_x = max(0.0, min(1.0, float(stored_position[0])))
            position_y = max(0.0, min(1.0, float(stored_position[1])))
        except (TypeError, ValueError, IndexError):
            position_x, position_y = 0.05, 0.84
        state["x"] = region.width * position_x
        state["y"] = region.height * position_y - height
    state["x"] = min(max(float(state["x"]), _px(context, 4)), max(_px(context, 4), region.width - width - _px(context, 4)))
    state["y"] = min(max(float(state["y"]), _px(context, 4)), max(_px(context, 4), region.height - height - _px(context, 4)))

    x, y = state["x"], state["y"]
    title_rect = (x, y + height - title_height, width, title_height)
    inner_width = width - 2.0 * padding
    cursor_y = y + padding
    zoom_sensitivity_rect = (x + padding, cursor_y, inner_width, sensitivity_height)
    cursor_y += sensitivity_height + gap
    rotation_sensitivity_rect = (x + padding, cursor_y, inner_width, sensitivity_height)
    cursor_y += sensitivity_height + gap
    clear_mask_rect = (x + padding, cursor_y, button_width, half_height)
    invert_mask_rect = (
        clear_mask_rect[0] + clear_mask_rect[2] + gap,
        cursor_y,
        button_width,
        half_height,
    )
    cursor_y += half_height + gap
    subdivision_rect = (x + padding, cursor_y, button_width, half_height)
    subdivision_slider_rect = (
        subdivision_rect[0] + subdivision_rect[2] + gap,
        cursor_y,
        button_width,
        half_height,
    )
    cursor_y += half_height + gap
    topology_rect = (x + padding, cursor_y, inner_width, half_height)
    cursor_y += half_height + gap
    isolate_rect = (
        x + padding,
        cursor_y,
        button_width,
        button_height,
    )
    wire_rect = (
        isolate_rect[0] + isolate_rect[2] + gap,
        isolate_rect[1],
        button_width,
        button_height,
    )
    controls = {
        "isolate": isolate_rect,
        "wire": wire_rect,
        "topology": topology_rect,
        "subdivision": subdivision_rect,
        "subdivision_slider": subdivision_slider_rect,
        "clear_mask": clear_mask_rect,
        "invert_mask": invert_mask_rect,
        "rotation_sensitivity": rotation_sensitivity_rect,
        "zoom_sensitivity": zoom_sensitivity_rect,
    }
    return state, (x, y, width, height), title_rect, controls


def _remember_panel_position(context, state):
    prefs = get_preferences(context)
    region = context.region
    if prefs is None or region is None or region.width <= 0 or region.height <= 0:
        return
    prefs.floating_panel_position = (
        max(0.0, min(1.0, state["x"] / region.width)),
        max(0.0, min(1.0, state["y"] / region.height)),
    )


def _floating_panel_hit(context, mouse_x, mouse_y):
    layout = _floating_panel_layout(context)
    if layout is None:
        return None, None
    _state, _panel, title_rect, controls = layout
    if _point_in_rect(mouse_x, mouse_y, title_rect):
        return "title", None
    if _point_in_rect(mouse_x, mouse_y, controls["isolate"]):
        return "action", "isolate:toggle"
    if _point_in_rect(mouse_x, mouse_y, controls["wire"]):
        return "action", "wire:toggle"
    if _point_in_rect(mouse_x, mouse_y, controls["topology"]):
        return "action", "topology:toggle"
    if _point_in_rect(mouse_x, mouse_y, controls["subdivision"]):
        return "action", "subdiv:toggle"
    if _point_in_rect(mouse_x, mouse_y, controls["subdivision_slider"]):
        return "slider", "subdiv:level"
    if _point_in_rect(mouse_x, mouse_y, controls["clear_mask"]):
        return "action", "mask:clear"
    if _point_in_rect(mouse_x, mouse_y, controls["invert_mask"]):
        return "action", "mask:invert"
    if _point_in_rect(mouse_x, mouse_y, controls["rotation_sensitivity"]):
        return "slider", "rotation:sensitivity"
    if _point_in_rect(mouse_x, mouse_y, controls["zoom_sensitivity"]):
        return "slider", "zoom:sensitivity"
    return None, None


def _floating_panel_object(context):
    obj = _active_sculpt_object(context)
    return obj if obj is not None and obj.type == "MESH" else None


def _isolation_active(context):
    view_layer = getattr(context, "view_layer", None)
    if view_layer is None:
        return False
    return bool(ISOLATION_STATES.get(view_layer.name))


def _wire_active(context):
    objects = list(getattr(context.view_layer, "objects", ()))
    return any(getattr(obj, "show_wire", False) for obj in objects)


def _toggle_wire(context):
    objects = [
        obj for obj in getattr(context.view_layer, "objects", ())
        if hasattr(obj, "show_wire")
    ]
    if not objects:
        return False
    enable = not any(obj.show_wire for obj in objects)
    for obj in objects:
        obj.show_wire = enable
    return True


def _current_sculpt_brush(context):
    brush = getattr(getattr(context.tool_settings, "sculpt", None), "brush", None)
    return brush if brush is not None and getattr(brush, "use_paint_sculpt", False) else None


def _topology_active(context):
    brush = _current_sculpt_brush(context)
    return bool(brush is not None and brush.use_automasking_topology)


def _toggle_topology(context):
    brush = _current_sculpt_brush(context)
    if brush is None:
        return False
    brush.use_automasking_topology = not brush.use_automasking_topology
    return True


def _clear_mask(context):
    obj = _floating_panel_object(context)
    layer = obj.data.attributes.get(".sculpt_mask") if obj is not None else None
    if layer is None:
        return True
    for value in layer.data:
        value.value = 0.0
    obj.data.update()
    obj.update_tag(refresh={"DATA"})
    if context.area:
        context.area.tag_redraw()
    return True


def _mask_layer(context, create=False):
    obj = _floating_panel_object(context)
    if obj is None:
        return None
    layer = obj.data.attributes.get(".sculpt_mask")
    if layer is None and create:
        try:
            layer = obj.data.attributes.new(name=".sculpt_mask", type="FLOAT", domain="POINT")
        except (RuntimeError, TypeError):
            return None
    return layer


def _invert_mask(context):
    layer = _mask_layer(context, create=True)
    if layer is None:
        return False
    for value in layer.data:
        value.value = 1.0 - value.value
    obj = _floating_panel_object(context)
    obj.data.update()
    obj.update_tag(refresh={"DATA"})
    if context.area:
        context.area.tag_redraw()
    return True


def _rotation_sensitivity(context):
    try:
        return max(VIEW_SENSITIVITY_MIN, min(VIEW_SENSITIVITY_MAX, float(_pref(context, "rotation_sensitivity", 1.0))))
    except (TypeError, ValueError):
        return 1.0


def _zoom_sensitivity(context):
    try:
        return max(VIEW_SENSITIVITY_MIN, min(VIEW_SENSITIVITY_MAX, float(_pref(context, "zoom_sensitivity", 1.0))))
    except (TypeError, ValueError):
        return 1.0


def _set_sensitivity_preference(context, name, factor):
    prefs = get_preferences(context)
    if prefs is None:
        return False
    setattr(prefs, name, max(VIEW_SENSITIVITY_MIN, min(VIEW_SENSITIVITY_MAX, float(factor))))
    return True


def _subdivision_modifier(context, create=False):
    obj = _floating_panel_object(context)
    if obj is None:
        return None
    modifier = obj.modifiers.get(SUBDIVISION_MODIFIER_NAME)
    if modifier is None:
        modifier = next((item for item in obj.modifiers if item.type == "SUBSURF"), None)
    if modifier is not None and modifier.type != "SUBSURF":
        if not create:
            return None
        obj.modifiers.remove(modifier)
        modifier = None
    if modifier is None and create:
        modifier = obj.modifiers.new(SUBDIVISION_MODIFIER_NAME, "SUBSURF")
        modifier.levels = 2
        modifier.render_levels = 2
    return modifier


def _subdivision_active(context):
    modifier = _subdivision_modifier(context)
    return bool(modifier is not None and modifier.show_viewport)


def _subdivision_level(context):
    modifier = _subdivision_modifier(context)
    if modifier is None:
        return 2
    return max(SUBDIVISION_LEVEL_MIN, min(SUBDIVISION_LEVEL_MAX, int(modifier.levels)))


def _set_subdivision_level(context, level):
    modifier = _subdivision_modifier(context, create=True)
    if modifier is None:
        return False
    level = max(SUBDIVISION_LEVEL_MIN, min(SUBDIVISION_LEVEL_MAX, int(round(level))))
    modifier.levels = level
    modifier.render_levels = level
    if not modifier.show_viewport:
        modifier.show_viewport = True
    if not modifier.show_render:
        modifier.show_render = True
    return True


def _toggle_subdivision(context):
    modifier = _subdivision_modifier(context, create=False)
    if modifier is not None and modifier.show_viewport and modifier.show_render:
        modifier.show_viewport = False
        modifier.show_render = False
        return True
    modifier = _subdivision_modifier(context, create=True)
    if modifier is None:
        return False
    modifier.show_viewport = True
    modifier.show_render = True
    return True


def _toggle_isolation(context):
    obj = _floating_panel_object(context)
    view_layer = getattr(context, "view_layer", None)
    if obj is None or view_layer is None:
        return False
    hidden = list(ISOLATION_STATES.get(view_layer.name, ()))
    if hidden:
        for object_name in hidden:
            other = bpy.data.objects.get(object_name)
            if other is not None:
                try:
                    other.hide_set(False)
                except (RuntimeError, ReferenceError):
                    pass
        ISOLATION_STATES.pop(view_layer.name, None)
        return True

    hidden = []
    for other in view_layer.objects:
        if other == obj or other.hide_get():
            continue
        try:
            other.hide_set(True)
            hidden.append(other.name)
        except (RuntimeError, ReferenceError):
            continue
    ISOLATION_STATES[view_layer.name] = hidden
    try:
        obj.hide_set(False)
    except (RuntimeError, ReferenceError):
        pass
    return True


def _capslock_state():
    if os.name != "nt":
        return None
    try:
        import ctypes
        # Low-order bit of GetKeyState is the Caps Lock toggle state.
        state = ctypes.windll.user32.GetKeyState(0x14)
        return bool(state & 1)
    except (AttributeError, OSError):
        return None


def _blender_is_foreground():
    if os.name != "nt":
        return True
    try:
        import ctypes
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        process_id = ctypes.c_ulong()
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
        return process_id.value == os.getpid()
    except (AttributeError, OSError):
        return True


def _trigger_capslock_isolation(context):
    global _CAPSLOCK_LAST_STATE
    _CAPSLOCK_LAST_STATE = _capslock_state()
    changed = _toggle_isolation(context)
    if changed and context.area:
        context.area.tag_redraw()
    return changed


def _capslock_watch_timer():
    global _CAPSLOCK_LAST_STATE
    context = bpy.context
    if not is_zbrush_sculpt_mode(context):
        _CAPSLOCK_LAST_STATE = None
        return 0.12
    current = _capslock_state()
    if current is None:
        return None
    if not _blender_is_foreground():
        _CAPSLOCK_LAST_STATE = current
        return 0.08
    if _CAPSLOCK_LAST_STATE is None:
        _CAPSLOCK_LAST_STATE = current
    elif current != _CAPSLOCK_LAST_STATE:
        _CAPSLOCK_LAST_STATE = current
        _toggle_isolation(context)
        if context.area:
            context.area.tag_redraw()
    return 0.08


def _start_capslock_watch():
    global _CAPSLOCK_LAST_STATE
    _CAPSLOCK_LAST_STATE = _capslock_state()
    if os.name != "nt":
        return
    try:
        if not bpy.app.timers.is_registered(_capslock_watch_timer):
            bpy.app.timers.register(_capslock_watch_timer, first_interval=0.08)
    except (AttributeError, ValueError):
        pass


def _restore_all_isolation():
    for hidden in ISOLATION_STATES.values():
        for object_name in hidden:
            other = bpy.data.objects.get(object_name)
            if other is not None:
                try:
                    other.hide_set(False)
                except (RuntimeError, ReferenceError):
                    pass
    ISOLATION_STATES.clear()


def _set_subdivision_from_slider(context, mouse_x, slider_rect):
    if slider_rect[2] <= 0.0:
        return False
    factor = max(0.0, min(1.0, (mouse_x - slider_rect[0]) / slider_rect[2]))
    level = round(SUBDIVISION_LEVEL_MIN + factor * (SUBDIVISION_LEVEL_MAX - SUBDIVISION_LEVEL_MIN))
    return _set_subdivision_level(context, level)


def _set_sensitivity_from_slider(context, name, mouse_x, slider_rect):
    if slider_rect[2] <= 0.0:
        return False
    factor = max(0.0, min(1.0, (mouse_x - slider_rect[0]) / slider_rect[2]))
    value = VIEW_SENSITIVITY_MIN + factor * (VIEW_SENSITIVITY_MAX - VIEW_SENSITIVITY_MIN)
    return _set_sensitivity_preference(context, name, value)


def _activate_floating_panel_action(context, action):
    if action == "isolate:toggle":
        changed = _toggle_isolation(context)
    elif action == "subdiv:toggle":
        changed = _toggle_subdivision(context)
    elif action == "wire:toggle":
        changed = _toggle_wire(context)
    elif action == "topology:toggle":
        changed = _toggle_topology(context)
    elif action == "mask:clear":
        changed = _clear_mask(context)
    elif action == "mask:invert":
        changed = _invert_mask(context)
    else:
        changed = False
    if changed and context.area:
        context.area.tag_redraw()
    return changed


def _draw_error(context, where, exc):
    """Report a swallowed drawing error once, on the console."""
    message = f"{where}: {exc!r}"
    if message in _REPORTED_DRAW_ERRORS:
        return
    if len(_REPORTED_DRAW_ERRORS) < 32:
        _REPORTED_DRAW_ERRORS.add(message)
    print(f"[ZB-Nav] {message}")


def get_nav_mode(context):
    return context.window_manager.get(NAV_MODE_PROP, "BLENDER")


def is_navigation_enabled(context):
    return context.window_manager.get(NAV_MODE_PROP, "BLENDER") == "ZBRUSH"


def set_nav_mode(context, mode):
    context.window_manager[NAV_MODE_PROP] = mode


def _apply_trackball_mode(context):
    """Force trackball while preserving the user's previous rotation style."""
    wm = context.window_manager
    inputs = context.preferences.inputs
    if ROTATE_METHOD_RESTORE_PROP not in wm:
        wm[ROTATE_METHOD_RESTORE_PROP] = str(inputs.view_rotate_method)
    if VIEW_SENSITIVITY_RESTORE_PROP not in wm:
        wm[VIEW_SENSITIVITY_RESTORE_PROP] = [
            float(inputs.view_rotate_sensitivity_trackball),
            float(inputs.view_rotate_sensitivity_turntable),
        ]
    factor = _rotation_sensitivity(context)
    saved = wm[VIEW_SENSITIVITY_RESTORE_PROP]
    inputs.view_rotate_method = "TRACKBALL"
    inputs.view_rotate_sensitivity_trackball = max(0.01, float(saved[0]) * factor)
    inputs.view_rotate_sensitivity_turntable = max(0.01, float(saved[1]) * factor)


def _restore_rotate_method(context):
    wm = context.window_manager
    previous = wm.pop(ROTATE_METHOD_RESTORE_PROP, None)
    previous_sensitivity = wm.pop(VIEW_SENSITIVITY_RESTORE_PROP, None)
    if previous is None:
        if previous_sensitivity is None:
            return
    try:
        inputs = context.preferences.inputs
        if previous is not None:
            inputs.view_rotate_method = previous
        if previous_sensitivity is not None:
            inputs.view_rotate_sensitivity_trackball = float(previous_sensitivity[0])
            inputs.view_rotate_sensitivity_turntable = float(previous_sensitivity[1])
    except (AttributeError, TypeError, ValueError, IndexError):
        pass


def _on_rotation_sensitivity_changed(self, context):
    if is_navigation_enabled(context):
        _apply_trackball_mode(context)


def is_zbrush_sculpt_mode(context):
    return get_nav_mode(context) == "ZBRUSH" and context.mode == "SCULPT"


def tag_all_view3d_areas_for_redraw():
    window_manager = getattr(bpy.context, "window_manager", None)
    if not window_manager:
        return
    for window in window_manager.windows:
        screen = window.screen
        if not screen:
            continue
        for area in screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()


def find_depth_hit(context, region, region_3d, mouse_x, mouse_y):
    """Return (hit, location, object) for the scene under the mouse."""
    if not region or not region_3d:
        return False, None, None

    coord = (mouse_x, mouse_y)
    origin = view3d_utils.region_2d_to_origin_3d(region, region_3d, coord)
    direction = view3d_utils.region_2d_to_vector_3d(region, region_3d, coord)
    hit, location, _normal, _index, obj, _matrix = context.scene.ray_cast(
        context.evaluated_depsgraph_get(),
        origin,
        direction,
    )
    return hit, location if hit else None, obj if hit else None


def find_depth_location(context, region, region_3d, mouse_x, mouse_y):
    hit, location, _obj = find_depth_hit(context, region, region_3d, mouse_x, mouse_y)
    return location if hit else None


def location_on_depth(region, region_3d, mouse_x, mouse_y, depth_location):
    return view3d_utils.region_2d_to_location_3d(
        region,
        region_3d,
        (mouse_x, mouse_y),
        depth_location,
    )


def _brush_size_owner(context):
    """Return (owner, property) for the brush size, honouring unified size."""
    tool_settings = getattr(context, "tool_settings", None)
    sculpt = getattr(tool_settings, "sculpt", None) if tool_settings else None
    if sculpt is None:
        return None, None
    for unified in (
        getattr(sculpt, "unified_paint_settings", None),
        getattr(tool_settings, "unified_paint_settings", None),
    ):
        if unified is not None and getattr(unified, "use_unified_size", False):
            return unified, "size"
    brush = getattr(sculpt, "brush", None)
    if brush is not None:
        return brush, "size"
    return None, None


def _brush_size_limits(owner):
    """Hard limits of the size property, so the drag can never reach zero."""
    try:
        prop = owner.bl_rna.properties["size"]
        return int(prop.hard_min), int(prop.hard_max)
    except (AttributeError, KeyError, TypeError, ValueError):
        return BRUSH_SIZE_MIN_FALLBACK, BRUSH_SIZE_MAX_FALLBACK


def _brush_size_value(context):
    owner, name = _brush_size_owner(context)
    if owner is None:
        return None
    try:
        return int(getattr(owner, name))
    except (AttributeError, TypeError, ValueError):
        return None


def _set_brush_size(context, value):
    owner, name = _brush_size_owner(context)
    if owner is None:
        return False
    low, high = _brush_size_limits(owner)
    target = max(low, min(high, int(round(value))))
    try:
        setattr(owner, name, target)
    except (TypeError, ValueError):
        return False
    return True


def _brush_size_sensitivity(context):
    try:
        return max(0.01, float(_pref(context, "brush_size_sensitivity", 0.5)))
    except (TypeError, ValueError):
        return 0.5


def _brush_size_from_drag(context, start_size, delta_x):
    """Use a stable brush-size change independent of the starting size."""
    return start_size + delta_x * _brush_size_sensitivity(context)


def _trigger_key_type(context):
    return "SPACE" if str(_pref(context, "trigger_key", "S")) == "SPACE" else "S"


def _trigger_keymap_items(context):
    """Keymap items for the brush size trigger, following the preference."""
    trigger = _trigger_key_type(context)
    return [
        {
            "keymap": "3D View",
            "space_type": "VIEW_3D",
            "idname": "zb_nav.brush_size_drag",
            "type": trigger,
            "value": "PRESS",
            "properties": {},
        },
        {
            "keymap": "Sculpt",
            "space_type": "EMPTY",
            "idname": "zb_nav.brush_size_drag",
            "type": trigger,
            "value": "PRESS",
            "properties": {},
        },
    ]


def _restore_trigger_key():
    """Put every yielded native keymap item back exactly as it was."""
    global _TRIGGER_KEY_YIELDED
    saved = list(_TRIGGER_KEY_YIELDED_ITEMS)
    _TRIGGER_KEY_YIELDED_ITEMS.clear()
    _TRIGGER_KEY_YIELDED = None
    for item, was_active in saved:
        try:
            item.active = was_active
        except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
            pass


def _yield_trigger_key(key_type):
    """Free up one key so the brush size trigger can use it.

    Only the keymaps this add-on can collide with are touched, only items
    without modifiers are taken, and every change is remembered for restore.
    """
    global _TRIGGER_KEY_YIELDED
    _restore_trigger_key()
    if not key_type:
        return
    keyconfigs = getattr(bpy.context.window_manager, "keyconfigs", None)
    keyconfig = getattr(keyconfigs, "user", None) if keyconfigs else None
    if keyconfig is None:
        return
    for keymap in keyconfig.keymaps:
        if keymap.name not in TRIGGER_KEYMAP_NAMES:
            continue
        for item in keymap.keymap_items:
            if item.type != key_type or not item.active:
                continue
            if item.ctrl or item.alt or item.shift or item.oskey:
                continue
            if item.key_modifier != "NONE":
                continue
            if item.idname.startswith("zb_nav."):
                continue
            try:
                _TRIGGER_KEY_YIELDED_ITEMS.append((item, item.active))
                item.active = False
            except (AttributeError, ReferenceError, RuntimeError):
                continue
    _TRIGGER_KEY_YIELDED = key_type


def _restore_mode_shortcuts():
    saved = list(_MODE_SHORTCUT_YIELDED_ITEMS)
    _MODE_SHORTCUT_YIELDED_ITEMS.clear()
    keyconfigs = bpy.context.window_manager.keyconfigs
    for keyconfig_name, keymap_name, item_index, idname, was_active in saved:
        try:
            keyconfig = keyconfigs.get(keyconfig_name)
            keymap = keyconfig.keymaps.get(keymap_name) if keyconfig else None
            if keymap is None or item_index >= len(keymap.keymap_items):
                continue
            item = keymap.keymap_items[item_index]
            if item.idname == idname:
                item.active = was_active
        except (AttributeError, KeyError, ReferenceError, RuntimeError, TypeError, ValueError):
            pass


def _yield_mode_shortcuts():
    """Reserve Ctrl+F and Ctrl+A for the ZBrush floating panel."""
    _restore_mode_shortcuts()
    keyconfigs = bpy.context.window_manager.keyconfigs
    for keyconfig in keyconfigs:
        if keyconfig.name not in {"Blender", "Blender addon", "Blender user"}:
            continue
        for keymap in keyconfig.keymaps:
            if keymap.name not in MODE_SHORTCUT_KEYMAPS:
                continue
            for item_index, item in enumerate(keymap.keymap_items):
                if not item.active or item.idname.startswith("zb_nav."):
                    continue
                is_ctrl_f = item.type == "F" and item.ctrl and not item.alt and not item.shift and not item.oskey
                is_ctrl_a = item.type == "A" and item.ctrl and not item.alt and not item.shift and not item.oskey
                is_ctrl_number = (
                    item.type in CTRL_NUMBER_TYPES
                    and item.idname == "object.subdivision_set"
                    and item.ctrl
                    and not item.alt
                    and not item.shift
                    and not item.oskey
                )
                if not (is_ctrl_f or is_ctrl_a or is_ctrl_number) or item.key_modifier != "NONE":
                    continue
                try:
                    _MODE_SHORTCUT_YIELDED_ITEMS.append((
                        keyconfig.name,
                        keymap.name,
                        item_index,
                        item.idname,
                        item.active,
                    ))
                    item.active = False
                except (AttributeError, ReferenceError, RuntimeError):
                    continue


def _sync_trigger_key_state():
    """Only ever take the key while it can actually be used."""
    context = bpy.context
    if is_zbrush_sculpt_mode(context):
        _yield_trigger_key(_trigger_key_type(context))
        _yield_mode_shortcuts()
    else:
        _restore_trigger_key()
        _restore_mode_shortcuts()


def _trigger_key_sync_timer():
    try:
        _sync_trigger_key_state()
    except Exception as exc:
        _draw_error(bpy.context, "trigger key sync", exc)
    return None


def _schedule_trigger_key_sync():
    """Keymap edits run from a timer, never from a draw or keymap callback."""
    try:
        if not bpy.app.timers.is_registered(_trigger_key_sync_timer):
            bpy.app.timers.register(_trigger_key_sync_timer, first_interval=0.0)
    except (AttributeError, ValueError):
        pass


def remove_zbrush_keymaps():
    wm = bpy.context.window_manager
    kc = wm.keyconfigs.addon
    if not kc:
        ADDON_KEYMAPS.clear()
        return

    for km, kmi in ADDON_KEYMAPS:
        try:
            km.keymap_items.remove(kmi)
        except (ReferenceError, RuntimeError):
            pass
    ADDON_KEYMAPS.clear()


def _add_keymap_item(item):
    wm = bpy.context.window_manager
    kc = wm.keyconfigs.addon
    if not kc:
        return

    km = kc.keymaps.new(
        name=item.get("keymap", "3D View"),
        space_type=item.get("space_type", "EMPTY"),
    )
    kmi = km.keymap_items.new(
        item["idname"],
        type=item["type"],
        value=item["value"],
        alt=item.get("alt", False),
        ctrl=item.get("ctrl", False),
        shift=item.get("shift", False),
        oskey=item.get("oskey", False),
        key_modifier=item.get("key_modifier", "NONE"),
    )
    for prop_name, prop_value in item.get("properties", {}).items():
        try:
            setattr(kmi.properties, prop_name, prop_value)
        except (AttributeError, TypeError, ValueError):
            # Radial-control properties differ between Blender versions.
            pass
    ADDON_KEYMAPS.append((km, kmi))


def swap_sculpt_brush_modifiers():
    restore_sculpt_brush_modifiers()
    wm = bpy.context.window_manager
    keyconfig = wm.keyconfigs.user
    if not keyconfig:
        return

    sculpt_keymap = keyconfig.keymaps.get("Sculpt")
    if not sculpt_keymap:
        return

    for keymap_item in sculpt_keymap.keymap_items:
        if keymap_item.idname != "sculpt.brush_stroke" or keymap_item.type != "LEFTMOUSE":
            continue
        # Keep the native Ctrl+Left mask brush intact and active. Only
        # neutralize the Alt+Left stroke so Alt+Left is free for target
        # switching (the plugin handles Alt+Left via its own keymap).
        if keymap_item.alt and not keymap_item.ctrl:
            SCULPT_BRUSH_MODIFIERS.append((keymap_item, keymap_item.ctrl, keymap_item.alt))
            keymap_item.active = False


def restore_sculpt_brush_modifiers():
    for keymap_item, ctrl, alt in SCULPT_BRUSH_MODIFIERS:
        try:
            keymap_item.ctrl = ctrl
            keymap_item.alt = alt
            keymap_item.active = True
        except (ReferenceError, RuntimeError):
            pass
    SCULPT_BRUSH_MODIFIERS.clear()


def find_object_under_mouse(context, mouse_x, mouse_y):
    region = context.region
    region_3d = context.region_data
    if not region or not region_3d:
        return None

    coord = (mouse_x, mouse_y)
    origin = view3d_utils.region_2d_to_origin_3d(region, region_3d, coord)
    direction = view3d_utils.region_2d_to_vector_3d(region, region_3d, coord)
    hit, _location, _normal, _index, obj, _matrix = context.scene.ray_cast(
        context.evaluated_depsgraph_get(),
        origin,
        direction,
    )
    if hit:
        obj = getattr(obj, "original", obj)
        if obj and obj.type == "MESH" and obj.visible_get():
            return obj
    return None


def is_valid_sculpt_target(context, obj):
    if not obj or obj.type != "MESH":
        return False
    if obj.name not in context.view_layer.objects:
        return False
    if not obj.visible_get(view_layer=context.view_layer):
        return False
    if obj.hide_select or obj.library is not None:
        return False
    return True


def switch_sculpt_target(context, target_object):
    if not is_zbrush_sculpt_mode(context):
        return False, "请先在雕刻模式中启用 ZBrush 子模式"
    if not is_valid_sculpt_target(context, target_object):
        return False, "目标必须是当前视图层中可见、可选择的本地网格对象"
    if target_object == context.active_object:
        return True, "目标已经是当前雕刻对象"

    try:
        previous_selection = list(context.selected_objects)
        bpy.ops.object.mode_set(mode="OBJECT")
        for selected_object in previous_selection:
            if selected_object.name in context.view_layer.objects:
                selected_object.select_set(False)
        target_object.hide_set(False)
        target_object.select_set(True)
        context.view_layer.objects.active = target_object
        bpy.ops.object.mode_set(mode="SCULPT")
    except RuntimeError as exc:
        return False, f"无法切换到目标雕刻模式: {exc}"

    tag_all_view3d_areas_for_redraw()
    return True, f"已切换到 {target_object.name}"


def select_or_invert_sculpt_target(context, event):
    if not is_zbrush_sculpt_mode(context):
        return {"PASS_THROUGH"}

    hit_object = find_object_under_mouse(context, event.mouse_region_x, event.mouse_region_y)
    if not hit_object or hit_object == context.active_object:
        return {"PASS_THROUGH"}

    success, _message = switch_sculpt_target(context, hit_object)
    return {"FINISHED"} if success else {"CANCELLED"}


MOVE_AXIS_COLORS = ((0.42, 0.055, 0.055), (0.055, 0.42, 0.08), (0.07, 0.18, 0.48))


def _axis_color(context, axis):
    prefs = get_preferences(context)
    if prefs:
        prop = getattr(prefs, ("axis_color_x", "axis_color_y", "axis_color_z")[axis], None)
        if prop is not None:
            return (prop[0], prop[1], prop[2])
    return MOVE_AXIS_COLORS[axis]


def _vivid_color(color):
    """Boost a handle colour to full brightness without washing it to white."""
    rgb = tuple(max(0.0, min(1.0, float(channel))) for channel in color[:3])
    high = max(rgb)
    low = min(rgb)
    if high < 1e-6:
        return (1.0, 0.42, 0.04)
    # Neutral handles have no hue to preserve, so use a warm high-saturation
    # accent rather than normalising grey into pure white.
    if high - low < high * 0.12:
        return (1.0, 0.42, 0.04)
    return tuple(channel / high for channel in rgb)


def _center_scale_color(context):
    prefs = get_preferences(context)
    if prefs:
        prop = getattr(prefs, "center_scale_color", None)
        if prop is not None:
            return (prop[0], prop[1], prop[2])
    return (1.0, 0.68, 0.05)


GIZMO_PIXEL_SIZE = 115.0
GIZMO_SCALE_POS = 0.35
GIZMO_RING_RADIUS = 0.7
GIZMO_VIEW_RING_RADIUS = 0.82
GIZMO_MOVE_TRI_BASE = 0.8
GIZMO_MOVE_TRI_HALF_WID = 10.0
GIZMO_SCALE_RECT_LEN = 26.0
GIZMO_SCALE_RECT_WID = 16.0
GIZMO_RING_BAND_PX = 2.0
GIZMO_RING_TUBE_W = 3.0
GIZMO_RING_TUBE_H = 7.0
GIZMO_RING_SEGMENTS = 96
GIZMO_CENTER_SQUARE_HALF = 8.0
GIZMO_CORNER_RADIUS = 0.98
GIZMO_CORNER_TRI_HEIGHT = 22.0
GIZMO_CORNER_TRI_HALF_WID = 22.0
GIZMO_BUTTON_RADIUS = 10.0
GIZMO_BUTTON_SPACING = 26.0
GIZMO_BUTTON_Y_OFFSET = 1.15
GIZMO_HANDLE_COLOR = (0.52, 0.52, 0.52)
GIZMO_BUTTON_COLOR = (0.32, 0.32, 0.32)
GIZMO_BUTTON_TOOLTIPS = (
    ("重置轴向", "恢复世界方向，保留轴心位置"),
    ("回到物体原点", "轴心位置和方向匹配物体原点"),
    ("回到网格中心", "移动轴心到网格包围盒中心"),
)


def _pivot_to_list(matrix):
    return [float(matrix[i][j]) for i in range(4) for j in range(4)]


def _pivot_from_list(data):
    m = mathutils.Matrix()
    for i in range(4):
        for j in range(4):
            m[i][j] = data[i * 4 + j]
    return m


def _pivot_is_per_object(obj):
    """True when the mesh is owned by a single object.

    The pivot is stored on the mesh data so it reverts together with the mesh
    when a gizmo transform is undone. Mesh data shared by several objects must
    not carry a per-object pivot, so those fall back to the in-memory cache.
    """
    return bool(obj and obj.data and getattr(obj.data, "users", 1) <= 1)


def _get_gizmo_matrix(context):
    """Return the pivot matrix without writing to any ID data.

    This runs from the POST_PIXEL draw handler too, so it must stay read-only.
    """
    global MOVE_GIZMO_PIVOT
    obj = context.active_object
    if not obj:
        return None

    if _pivot_is_per_object(obj):
        stored = obj.data.get("zb_nav_pivot")
        if stored is not None:
            try:
                matrix = _pivot_from_list(stored)
            except (TypeError, ValueError, IndexError):
                matrix = None
            if matrix is not None:
                MOVE_GIZMO_PIVOT = (obj.name, matrix)
                return matrix

    if MOVE_GIZMO_PIVOT is not None and MOVE_GIZMO_PIVOT[0] == obj.name:
        return MOVE_GIZMO_PIVOT[1]

    matrix = obj.matrix_world.copy()
    MOVE_GIZMO_PIVOT = (obj.name, matrix)
    return matrix


def _set_gizmo_matrix(obj_name, matrix):
    global MOVE_GIZMO_PIVOT
    MOVE_GIZMO_PIVOT = (obj_name, matrix.copy())
    obj = bpy.data.objects.get(obj_name)
    if _pivot_is_per_object(obj):
        try:
            obj.data["zb_nav_pivot"] = _pivot_to_list(matrix)
        except (TypeError, ValueError, AttributeError):
            pass


def _scale_matrix_about_axis(origin, axis, factor):
    """Return world-space scaling along one current gizmo axis about its origin."""
    axis = axis.normalized()
    amount = factor - 1.0
    linear = mathutils.Matrix((
        (1.0 + amount * axis.x * axis.x, amount * axis.x * axis.y, amount * axis.x * axis.z),
        (amount * axis.y * axis.x, 1.0 + amount * axis.y * axis.y, amount * axis.y * axis.z),
        (amount * axis.z * axis.x, amount * axis.z * axis.y, 1.0 + amount * axis.z * axis.z),
    )).to_4x4()
    return (
        mathutils.Matrix.Translation(origin)
        @ linear
        @ mathutils.Matrix.Translation(-origin)
    )


def _sculpt_mask_values(obj, vertex_count):
    """Return the sculpt mask layer data, or None when there is no usable mask."""
    if not obj or not obj.data:
        return None
    mask_layer = obj.data.attributes.get(".sculpt_mask")
    if mask_layer is None or len(mask_layer.data) < vertex_count:
        return None
    return mask_layer.data


def _mesh_bounds_center(obj):
    """World-space center of the mesh bounding box."""
    if not obj or not obj.data:
        return mathutils.Vector((0.0, 0.0, 0.0))
    matrix = obj.matrix_world
    corners = obj.bound_box
    center = mathutils.Vector((0.0, 0.0, 0.0))
    for corner in corners:
        center += matrix @ mathutils.Vector(corner)
    return center / (len(corners) or 1)


def _transform_mesh_in_world(obj, world_transform):
    """Transform unmasked mesh vertices in world space.

    The mask layer is resolved once: looking it up per vertex costs an RNA
    property read for every vertex on every mouse move.
    """
    vertices = obj.data.vertices
    mask_data = _sculpt_mask_values(obj, len(vertices))
    object_to_world = obj.matrix_world.copy()
    local_transform = (
        object_to_world.inverted_safe()
        @ world_transform
        @ object_to_world
    )
    moved = False
    if mask_data is None:
        for vertex in vertices:
            vertex.co = local_transform @ vertex.co
        moved = len(vertices) > 0
    else:
        for vertex in vertices:
            if mask_data[vertex.index].value < MASK_SKIP_THRESHOLD:
                vertex.co = local_transform @ vertex.co
                moved = True
    if moved:
        obj.data.update()


def _gizmo_world_axes(context):
    matrix = _get_gizmo_matrix(context)
    if matrix is None:
        return None, None
    origin = matrix.translation.copy()
    axes = []
    for i in range(3):
        unit = mathutils.Vector((1, 0, 0) if i == 0 else (0, 1, 0) if i == 1 else (0, 0, 1))
        axes.append((matrix.to_3x3() @ unit).normalized())
    return origin, axes


def _axis_pixels(context):
    """On-screen length of the gizmo axes, in pixels, following the DPI scale."""
    return max(20.0, _px(context, GIZMO_PIXEL_SIZE))


def _gizmo_length(context):
    matrix = _get_gizmo_matrix(context)
    if matrix is None:
        return 1.0
    origin = matrix.translation
    region = context.region
    region_3d = context.region_data
    if not region or not region_3d:
        return 1.0

    target_pixels = _axis_pixels(context)

    right = region_3d.view_rotation @ mathutils.Vector((1, 0, 0))
    a = view3d_utils.location_3d_to_region_2d(region, region_3d, origin)
    b = view3d_utils.location_3d_to_region_2d(region, region_3d, origin + right)
    if not a or not b:
        return 1.0
    pixels_per_unit = (b - a).length
    if pixels_per_unit < 1e-6:
        return 1.0
    return target_pixels / pixels_per_unit


def _to_screen(context, world_pos):
    region = context.region
    region_3d = context.region_data
    if not region or not region_3d:
        return None
    return view3d_utils.location_3d_to_region_2d(region, region_3d, world_pos)


def _gizmo_screen_reach(context):
    """Screen radius covering every handle, including the pivot buttons.

    Used to reject far-away picks before doing the expensive projections.
    """
    offset = max(1.6, _button_offset(context))
    return _axis_pixels(context) * offset + _button_size(context) + _px(context, 24.0)


def _axis_drag_angle(context, origin, axis, previous, current):
    region = context.region
    region_3d = context.region_data
    if not region or not region_3d:
        return 0.0

    points = []
    for coord in (previous, current):
        ray_origin = view3d_utils.region_2d_to_origin_3d(region, region_3d, coord)
        ray_direction = view3d_utils.region_2d_to_vector_3d(region, region_3d, coord)
        denominator = axis.dot(ray_direction)
        if abs(denominator) < 1e-6:
            return 0.0
        distance = axis.dot(origin - ray_origin) / denominator
        points.append(ray_origin + ray_direction * distance)

    previous_vector = points[0] - origin
    current_vector = points[1] - origin
    if previous_vector.length < 1e-6 or current_vector.length < 1e-6:
        return 0.0
    previous_vector.normalize()
    current_vector.normalize()
    return math.atan2(
        axis.dot(previous_vector.cross(current_vector)),
        previous_vector.dot(current_vector),
    )


def _dist_point_to_segment(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    length_sq = dx * dx + dy * dy
    if length_sq == 0.0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length_sq))
    cx, cy = ax + t * dx, ay + t * dy
    return math.hypot(px - cx, py - cy)


def _move_mode_pick(context, mouse_x, mouse_y):
    """Return (kind, axis) under the cursor or None (standard gizmo)."""
    origin, axes = _gizmo_world_axes(context)
    if origin is None:
        return None
    origin_screen = _to_screen(context, origin)
    if origin_screen is None:
        return None

    # Cheap reject: nothing can be hit outside this radius, so skip the ~140
    # projections a full pick would otherwise run on every mouse move.
    if math.hypot(mouse_x - origin_screen.x, mouse_y - origin_screen.y) > _gizmo_screen_reach(context):
        return None

    length = _gizmo_length(context)
    candidates = []

    for axis in range(3):
        tip = origin + axes[axis] * length
        base = origin + axes[axis] * length * GIZMO_MOVE_TRI_BASE
        mid = origin + axes[axis] * length * GIZMO_SCALE_POS
        tip_screen = _to_screen(context, tip)
        base_screen = _to_screen(context, base)
        mid_screen = _to_screen(context, mid)
        if tip_screen and base_screen:
            d = _dist_point_to_segment(
                mouse_x, mouse_y,
                base_screen.x, base_screen.y,
                tip_screen.x, tip_screen.y,
            )
            candidates.append((d - _px(context, GIZMO_MOVE_TRI_HALF_WID), "move", axis))
        if mid_screen:
            d = math.hypot(mouse_x - mid_screen.x, mouse_y - mid_screen.y)
            candidates.append((d - _px(context, 12.0), "scale", axis))

        radius = length * GIZMO_RING_RADIUS
        steps = 24
        other1 = axes[(axis + 1) % 3]
        other2 = axes[(axis + 2) % 3]
        screen_points = []
        for t in range(steps + 1):
            ang = 2.0 * math.pi * t / steps
            world = origin + (other1 * math.cos(ang) + other2 * math.sin(ang)) * radius
            screen = _to_screen(context, world)
            if screen:
                screen_points.append((screen.x, screen.y))
        if len(screen_points) >= 3:
            best = min(
                _dist_point_to_segment(
                    mouse_x, mouse_y,
                    screen_points[i][0], screen_points[i][1],
                    screen_points[(i + 1) % len(screen_points)][0],
                    screen_points[(i + 1) % len(screen_points)][1],
                )
                for i in range(len(screen_points))
            )
            candidates.append((best - _px(context, 3.0), "rotate", axis))

    # outer view-rotation ring (circle facing the camera)
    region_3d = context.region_data
    if region_3d:
        view_right = region_3d.view_rotation @ mathutils.Vector((1, 0, 0))
        view_up = region_3d.view_rotation @ mathutils.Vector((0, 1, 0))
        outer_radius = length * GIZMO_VIEW_RING_RADIUS
        screen_points = []
        for t in range(49):
            ang = 2.0 * math.pi * t / 48
            world = origin + (view_right * math.cos(ang) + view_up * math.sin(ang)) * outer_radius
            screen = _to_screen(context, world)
            if screen:
                screen_points.append((screen.x, screen.y))
        if len(screen_points) >= 3:
            best = min(
                _dist_point_to_segment(
                    mouse_x, mouse_y,
                    screen_points[i][0], screen_points[i][1],
                    screen_points[(i + 1) % len(screen_points)][0],
                    screen_points[(i + 1) % len(screen_points)][1],
                )
                for i in range(len(screen_points))
            )
            candidates.append((best - _px(context, 3.0), "view_rotate", -1))

        # four corner triangles: translate in the view plane
        corner_angles = (45, 135, 225, 315)
        corner_radius = length * GIZMO_CORNER_RADIUS
        for corner in range(4):
            rad = math.radians(corner_angles[corner])
            world = origin + (view_right * math.cos(rad) + view_up * math.sin(rad)) * corner_radius
            screen = _to_screen(context, world)
            if screen:
                d = math.hypot(mouse_x - screen.x, mouse_y - screen.y)
                candidates.append((d - _px(context, 16.0), "view_move", corner))

    # center square: uniform scale
    if origin_screen:
        d = math.hypot(mouse_x - origin_screen.x, mouse_y - origin_screen.y)
        candidates.append((d - _px(context, GIZMO_CENTER_SQUARE_HALF + 4.0), "scale_all", -1))

        # three gizmo buttons above the pivot
        btn_size = _button_size(context)
        for btn in range(3):
            bx, by = _gizmo_button_positions(context, origin_screen)[btn]
            d = math.hypot(mouse_x - bx, mouse_y - by)
            candidates.append((d - btn_size, "gizmo_button", btn))

    candidates.sort(key=lambda item: item[0])
    if candidates and candidates[0][0] < _px(context, 6.0):
        return candidates[0][1], candidates[0][2]
    return None


def _raycast_surface(context, mouse_x, mouse_y):
    region = context.region
    region_3d = context.region_data
    if not region or not region_3d:
        return None, None, None
    origin = view3d_utils.region_2d_to_origin_3d(region, region_3d, (mouse_x, mouse_y))
    direction = view3d_utils.region_2d_to_vector_3d(region, region_3d, (mouse_x, mouse_y))
    hit, location, normal, _index, _obj, _matrix = context.scene.ray_cast(
        context.evaluated_depsgraph_get(),
        origin,
        direction,
    )
    if hit:
        return location, normal, _obj
    return None, None, None


def _active_sculpt_object(context):
    """Return the object currently being sculpted, when it is mesh data."""
    obj = getattr(context, "active_object", None) or getattr(context, "object", None)
    if obj is not None and obj.type == "MESH":
        return obj
    return None


def find_navigation_pivot(context, region, region_3d, mouse_x, mouse_y):
    """Find the auto-depth pivot for orbiting in ZBrush mode.

    A direct hit on the active sculpt object uses the clicked surface. Any
    click outside that object uses its mesh-bounds centre.
    """
    if not region or not region_3d:
        return None

    hit, hit_location, hit_obj = find_depth_hit(
        context, region, region_3d, mouse_x, mouse_y
    )
    active_obj = _active_sculpt_object(context)
    if active_obj is None:
        return hit_location if hit else region_3d.view_location.copy()
    if hit and hit_obj == active_obj:
        return hit_location
    return _mesh_bounds_center(active_obj)


def _matrix_z_to_direction(matrix, world_direction):
    direction = world_direction.normalized()
    if direction.length < 1e-6:
        return matrix
    reference = matrix.to_3x3()
    old_x = reference @ mathutils.Vector((1, 0, 0))
    new_x = old_x - direction * old_x.dot(direction)
    if new_x.length < 1e-5:
        old_y = reference @ mathutils.Vector((0, 1, 0))
        new_x = old_y - direction * old_y.dot(direction)
    if new_x.length < 1e-5:
        fallback = mathutils.Vector((1, 0, 0)) if abs(direction.z) < 0.999 else mathutils.Vector((0, 1, 0))
        new_x = fallback - direction * fallback.dot(direction)
    new_x.normalize()
    new_y = direction.cross(new_x)
    new_rotation = mathutils.Matrix((
        (new_x[0], new_y[0], direction[0]),
        (new_x[1], new_y[1], direction[1]),
        (new_x[2], new_y[2], direction[2]),
    ))
    loc = matrix.translation.copy()
    scale = matrix.to_scale()
    try:
        return mathutils.Matrix.LocRotScale(loc, new_rotation, scale)
    except (ValueError, RuntimeError):
        return matrix


def _set_pivot_world_point(context, world_point, z_direction=None):
    obj = context.active_object
    if not obj:
        return
    matrix = _get_gizmo_matrix(context)
    if matrix is None:
        return
    matrix = matrix.copy()
    matrix[0][3] = world_point.x
    matrix[1][3] = world_point.y
    matrix[2][3] = world_point.z
    if z_direction is not None:
        matrix = _matrix_z_to_direction(matrix, z_direction)
    _set_gizmo_matrix(obj.name, matrix)


def _is_move_tool_active(context):
    """Is the ZB-Nav move tool the active sculpt tool?

    The lookup walks a private Blender API and runs from the draw handler and
    from every hover event, so the answer is cached for a moment.
    """
    now = time.monotonic()
    if now - _MOVE_TOOL_CACHE["time"] < TOOL_CACHE_SECONDS:
        return _MOVE_TOOL_CACHE["active"]

    active = False
    try:
        from bl_ui.space_toolsystem_common import ToolSelectPanelHelper
        cls = ToolSelectPanelHelper._tool_class_from_space_type("VIEW_3D")
        _item, tool_active, _icon = cls._tool_get_active(context, "VIEW_3D", "SCULPT")
        active = tool_active is not None and tool_active.idname == ZBNAV_MOVE_TOOL.bl_idname
    except Exception as exc:
        _draw_error(context, "move tool lookup", exc)
        active = False

    _MOVE_TOOL_CACHE["time"] = now
    _MOVE_TOOL_CACHE["active"] = active
    return active


class ZBNAV_MOVE_TOOL(bpy.types.WorkSpaceTool):
    bl_space_type = "VIEW_3D"
    bl_context_mode = "SCULPT"
    bl_idname = "zb_nav.move_tool"
    bl_label = "移动"
    bl_description = "在物体原点显示移动/缩放/旋转轴，拖动轴控制物体变换"
    bl_icon = "ops.transform.translate"
    bl_widget = None
    bl_keymap = (
        ("zb_nav.move_mode_drag", {"type": "LEFTMOUSE", "value": "PRESS"}, None),
        ("zb_nav.move_mode_drag", {"type": "LEFTMOUSE", "value": "PRESS", "alt": True}, None),
    )

    def draw_settings(context, layout, tool):
        layout.label(text="拖轴：移动 / 缩放 / 旋转")
        layout.label(text="外圈：视图旋转 · 四角：视图平移")
        layout.label(text="中心方块：整体缩放")
        layout.label(text="Alt + 左键：重设轴心 / 只变换轴")


class ZBNAV_OT_move_mode_drag(bpy.types.Operator):
    bl_idname = "zb_nav.move_mode_drag"
    bl_label = "Move Mode Drag"
    bl_description = "拖动变换轴控制物体移动/缩放/旋转"
    # "UNDO" is required: the vertex edits this operator makes are otherwise
    # invisible to Blender's undo system in sculpt mode, so Ctrl+Z did nothing.
    # The operator additionally pushes a step at drag start (see invoke) so the
    # first undo returns to the pre-drag state.
    bl_options = {"REGISTER", "UNDO"}

    _drag_handle = None
    _last_x = 0
    _last_y = 0
    _reposition = False
    _pivot_drag = False
    _press_point = None
    _initial_matrix = None
    _initial_pivot = None
    _undo_pushed = False

    @classmethod
    def poll(cls, context):
        return (
            is_navigation_enabled(context)
            and context.mode == "SCULPT"
            and context.active_object is not None
            and context.area
            and context.area.type == "VIEW_3D"
        )

    def _start_reposition(self, context, event):
        global MOVE_MODE_HOVER
        location, normal, _obj = _raycast_surface(
            context, event.mouse_region_x, event.mouse_region_y
        )
        obj = context.active_object
        if not location or not obj or obj.type != "MESH":
            return {"CANCELLED"}
        self._reposition = True
        self._press_point = location.copy()
        self._last_x = event.mouse_region_x
        self._last_y = event.mouse_region_y
        MOVE_MODE_HOVER = None
        _set_pivot_world_point(context, location, normal)
        context.window_manager.modal_handler_add(self)
        if context.area:
            context.area.tag_redraw()
        return {"RUNNING_MODAL"}

    def _start_pivot_transform(self, context, event, handle):
        global MOVE_MODE_HOVER
        obj = context.active_object
        if not obj or obj.type != "MESH":
            return {"CANCELLED"}
        self._pivot_drag = True
        self._drag_handle = handle
        self._last_x = event.mouse_region_x
        self._last_y = event.mouse_region_y
        MOVE_MODE_HOVER = handle
        context.window_manager.modal_handler_add(self)
        if context.area:
            context.area.tag_redraw()
        return {"RUNNING_MODAL"}

    def _do_button(self, context, button_index):
        obj = context.active_object
        if not obj:
            return {"CANCELLED"}
        matrix = _get_gizmo_matrix(context)
        if matrix is None:
            return {"CANCELLED"}
        try:
            bpy.ops.ed.undo_push(message="Gizmo Button")
        except (RuntimeError, AttributeError):
            pass
        if button_index == 0:
            # reset control axis rotation to zero, keep position
            loc, _rot, scale = matrix.decompose()
            matrix = mathutils.Matrix.LocRotScale(loc, mathutils.Quaternion(), scale)
            _set_gizmo_matrix(obj.name, matrix)
        elif button_index == 1:
            # move to object origin, rotation aligned to origin
            _set_gizmo_matrix(obj.name, obj.matrix_world.copy())
        else:
            # move to the center of the mesh bounding box
            center = _mesh_bounds_center(obj)
            loc, rot, scale = matrix.decompose()
            matrix = mathutils.Matrix.LocRotScale(center, rot, scale)
            _set_gizmo_matrix(obj.name, matrix)
        if context.area:
            context.area.tag_redraw()
        return {"CANCELLED"}

    def invoke(self, context, event):
        handle = _move_mode_pick(
            context, event.mouse_region_x, event.mouse_region_y,
        )
        if handle is not None and handle[0] == "gizmo_button":
            return self._do_button(context, handle[1])
        if event.alt:
            if handle is not None:
                return self._start_pivot_transform(context, event, handle)
            return self._start_reposition(context, event)
        global MOVE_MODE_HOVER
        obj = context.active_object
        if not obj or obj.type != "MESH":
            return {"CANCELLED"}
        if handle is None:
            MOVE_MODE_HOVER = None
            return {"CANCELLED"}
        self._drag_handle = handle
        self._last_x = event.mouse_region_x
        self._last_y = event.mouse_region_y
        self._initial_matrix = obj.matrix_world.copy()
        pivot = _get_gizmo_matrix(context)
        self._initial_pivot = pivot.copy() if pivot is not None else None
        # Store the pre-drag state. Calling undo_push on release is too late:
        # it stores the already transformed state and makes the first undo a
        # no-op. This push is intentionally done only for real object drags.
        try:
            bpy.ops.ed.undo_push(message="ZB-Nav Transform")
            self._undo_pushed = True
        except (RuntimeError, AttributeError):
            self._undo_pushed = False
        MOVE_MODE_HOVER = handle
        context.window_manager.modal_handler_add(self)
        if context.area:
            context.area.tag_redraw()
        return {"RUNNING_MODAL"}

    def _finish(self, context):
        global MOVE_MODE_HOVER
        MOVE_MODE_HOVER = None
        self._pivot_drag = False
        self._reposition = False
        self._drag_handle = None
        if context.area:
            context.area.tag_redraw()
        return {"FINISHED"}

    def _world_delta(self, context, event):
        origin, _axes = _gizmo_world_axes(context)
        region = context.region
        region_3d = context.region_data
        if not origin or not region or not region_3d:
            return mathutils.Vector((0, 0, 0))
        prev = view3d_utils.region_2d_to_location_3d(
            region, region_3d, (self._last_x, self._last_y), origin
        )
        cur = view3d_utils.region_2d_to_location_3d(
            region, region_3d, (event.mouse_region_x, event.mouse_region_y), origin
        )
        return cur - prev

    def _apply(self, context, event):
        if not self._drag_handle:
            return
        kind, axis = self._drag_handle
        obj = context.active_object
        if not obj:
            return
        origin, axes = _gizmo_world_axes(context)
        if origin is None:
            return
        length = _gizmo_length(context)

        if kind == "view_rotate":
            origin_screen = _to_screen(context, origin)
            region_3d = context.region_data
            if origin_screen and region_3d:
                prev_angle = math.atan2(
                    self._last_y - origin_screen.y,
                    self._last_x - origin_screen.x,
                )
                cur_angle = math.atan2(
                    event.mouse_region_y - origin_screen.y,
                    event.mouse_region_x - origin_screen.x,
                )
                angle = -(cur_angle - prev_angle)
                view_axis = region_3d.view_rotation @ mathutils.Vector((0, 0, -1))
                rotation = mathutils.Matrix.Rotation(angle, 4, view_axis)
                translation = mathutils.Matrix.Translation(origin)
                transform = translation @ rotation @ translation.inverted()
                _transform_mesh_in_world(obj, transform)
                pivot = _get_gizmo_matrix(context)
                if pivot is not None:
                    _set_gizmo_matrix(obj.name, transform @ pivot)
        elif kind == "view_move":
            delta = self._world_delta(context, event)
            region_3d = context.region_data
            if region_3d:
                view_right = region_3d.view_rotation @ mathutils.Vector((1, 0, 0))
                view_up = region_3d.view_rotation @ mathutils.Vector((0, 1, 0))
                move = view_right * delta.dot(view_right) + view_up * delta.dot(view_up)
                transform = mathutils.Matrix.Translation(move)
                _transform_mesh_in_world(obj, transform)
                pivot = _get_gizmo_matrix(context)
                if pivot is not None:
                    _set_gizmo_matrix(obj.name, transform @ pivot)
        elif kind == "scale_all":
            origin_screen = _to_screen(context, origin)
            if origin_screen:
                normal = mathutils.Vector((1, 1)).normalized()
                mouse_vec = mathutils.Vector((
                    event.mouse_region_x - origin_screen.x,
                    event.mouse_region_y - origin_screen.y,
                ))
                prev_vec = mathutils.Vector((
                    self._last_x - origin_screen.x,
                    self._last_y - origin_screen.y,
                ))
                signed_delta = mouse_vec.dot(normal) - prev_vec.dot(normal)
                factor = max(0.001, 1.0 + signed_delta / _axis_pixels(context))
                transform = mathutils.Matrix.Translation(origin)
                transform = transform @ mathutils.Matrix.Scale(factor, 4) @ transform.inverted()
                _transform_mesh_in_world(obj, transform)
        else:
            axis_dir = axes[axis]

            if kind == "move":
                delta = self._world_delta(context, event)
                amount = delta.dot(axis_dir)
                transform = mathutils.Matrix.Translation(axis_dir * amount)
                _transform_mesh_in_world(obj, transform)
                pivot = _get_gizmo_matrix(context)
                if pivot is not None:
                    _set_gizmo_matrix(obj.name, transform @ pivot)
            elif kind == "scale":
                delta = self._world_delta(context, event)
                amount = delta.dot(axis_dir)
                factor = max(0.001, 1.0 + amount / length)
                # Axis and center both come from the current control gizmo.
                transform = _scale_matrix_about_axis(origin, axis_dir, factor)
                _transform_mesh_in_world(obj, transform)
            elif kind == "rotate":
                origin_screen = _to_screen(context, origin)
                if origin_screen:
                    angle = _axis_drag_angle(
                        context,
                        origin,
                        axis_dir,
                        (self._last_x, self._last_y),
                        (event.mouse_region_x, event.mouse_region_y),
                    )
                    rotation = mathutils.Matrix.Rotation(angle, 4, axis_dir)
                    translation = mathutils.Matrix.Translation(origin)
                    transform = translation @ rotation @ translation.inverted()
                    _transform_mesh_in_world(obj, transform)
                    pivot = _get_gizmo_matrix(context)
                    if pivot is not None:
                        _set_gizmo_matrix(obj.name, transform @ pivot)
        self._last_x = event.mouse_region_x
        self._last_y = event.mouse_region_y
        if context.area:
            context.area.tag_redraw()

    def _apply_pivot(self, context, event):
        if not self._drag_handle:
            return
        kind, axis = self._drag_handle
        obj = context.active_object
        if not obj:
            return
        origin, axes = _gizmo_world_axes(context)
        if origin is None:
            return
        matrix = _get_gizmo_matrix(context)
        if matrix is None:
            return

        if kind == "view_rotate":
            origin_screen = _to_screen(context, origin)
            region_3d = context.region_data
            if origin_screen and region_3d:
                prev_angle = math.atan2(
                    self._last_y - origin_screen.y,
                    self._last_x - origin_screen.x,
                )
                cur_angle = math.atan2(
                    event.mouse_region_y - origin_screen.y,
                    event.mouse_region_x - origin_screen.x,
                )
                angle = -(cur_angle - prev_angle)
                view_axis = region_3d.view_rotation @ mathutils.Vector((0, 0, -1))
                rotation = mathutils.Matrix.Rotation(angle, 4, view_axis)
                translation = mathutils.Matrix.Translation(origin)
                matrix = matrix.copy()
                matrix = translation @ rotation @ translation.inverted() @ matrix
                _set_gizmo_matrix(obj.name, matrix)
        elif kind == "view_move":
            delta = self._world_delta(context, event)
            region_3d = context.region_data
            if region_3d:
                view_right = region_3d.view_rotation @ mathutils.Vector((1, 0, 0))
                view_up = region_3d.view_rotation @ mathutils.Vector((0, 1, 0))
                move = view_right * delta.dot(view_right) + view_up * delta.dot(view_up)
                matrix = matrix.copy()
                matrix.translation += move
                _set_gizmo_matrix(obj.name, matrix)
        else:
            axis_dir = axes[axis]

            if kind == "move":
                delta = self._world_delta(context, event)
                amount = delta.dot(axis_dir)
                matrix = matrix.copy()
                matrix.translation += axis_dir * amount
                _set_gizmo_matrix(obj.name, matrix)
            elif kind == "rotate":
                origin_screen = _to_screen(context, origin)
                if origin_screen:
                    angle = _axis_drag_angle(
                        context,
                        origin,
                        axis_dir,
                        (self._last_x, self._last_y),
                        (event.mouse_region_x, event.mouse_region_y),
                    )
                    rotation = mathutils.Matrix.Rotation(angle, 4, axis_dir)
                    translation = mathutils.Matrix.Translation(origin)
                    matrix = matrix.copy()
                    matrix = translation @ rotation @ translation.inverted() @ matrix
                    _set_gizmo_matrix(obj.name, matrix)
            # scale handle under Alt does nothing (only the pivot moves/rotates)
        self._last_x = event.mouse_region_x
        self._last_y = event.mouse_region_y
        if context.area:
            context.area.tag_redraw()

    def modal(self, context, event):
        global MOVE_MODE_HOVER
        if not context.active_object:
            return self._finish(context)

        if event.type in {"ESC", "RIGHTMOUSE"} and event.value == "PRESS":
            if not (self._pivot_drag or self._reposition):
                if self._undo_pushed:
                    try:
                        bpy.ops.ed.undo()
                    except (RuntimeError, AttributeError):
                        if self._initial_matrix is not None and context.active_object:
                            context.active_object.matrix_world = self._initial_matrix
                elif self._initial_matrix is not None and context.active_object:
                    context.active_object.matrix_world = self._initial_matrix
            if self._initial_pivot is not None and context.active_object:
                _set_gizmo_matrix(context.active_object.name, self._initial_pivot)
            return self._finish(context)

        if self._pivot_drag:
            if event.type == "MOUSEMOVE":
                self._apply_pivot(context, event)
                return {"RUNNING_MODAL"}
            if event.type == "LEFTMOUSE" and event.value == "RELEASE":
                self._pivot_drag = False
                self._drag_handle = None
                return self._finish(context)
            return {"RUNNING_MODAL"}

        if self._reposition:
            if event.type == "MOUSEMOVE":
                location, _normal, _obj = _raycast_surface(
                    context, event.mouse_region_x, event.mouse_region_y
                )
                obj = context.active_object
                if location and obj:
                    drag = location - self._press_point
                    if drag.length > 1e-4:
                        matrix = _get_gizmo_matrix(context)
                        if matrix is not None:
                            _set_gizmo_matrix(
                                obj.name, _matrix_z_to_direction(matrix, drag)
                            )
                    if context.area:
                        context.area.tag_redraw()
                return {"RUNNING_MODAL"}
            if event.type == "LEFTMOUSE" and event.value == "RELEASE":
                self._reposition = False
                return self._finish(context)
            return {"RUNNING_MODAL"}

        if event.type == "MOUSEMOVE":
            self._apply(context, event)
            return {"RUNNING_MODAL"}

        if event.type == "LEFTMOUSE" and event.value == "RELEASE":
            return self._finish(context)

        return {"RUNNING_MODAL"}


class ZBNAV_OT_gizmo_hover_monitor(bpy.types.Operator):
    """Passive monitor for gizmo hover and native middle-mouse pivot setup."""

    bl_idname = "zb_nav.gizmo_hover_monitor"
    bl_label = "Gizmo Hover Monitor"
    bl_options = {"INTERNAL"}
    _saved_navigation_options = None
    _panel_click_consumed = False

    @classmethod
    def poll(cls, context):
        return context.area and context.area.type == "VIEW_3D"

    def invoke(self, context, event):
        global GIZMO_HOVER_MONITOR_RUNNING
        if GIZMO_HOVER_MONITOR_RUNNING:
            return {"CANCELLED"}
        GIZMO_HOVER_MONITOR_RUNNING = True
        context.window_manager.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def _finish(self, context):
        global GIZMO_HOVER_MONITOR_RUNNING, MOVE_MODE_HOVER
        GIZMO_HOVER_MONITOR_RUNNING = False
        MOVE_MODE_HOVER = None
        self._panel_click_consumed = False
        self._restore_navigation_options(context)
        _restore_all_isolation()
        if context.area:
            context.area.tag_redraw()
        return {"CANCELLED"}

    def _restore_navigation_options(self, context):
        if self._saved_navigation_options is None:
            return
        try:
            inputs = context.preferences.inputs
            inputs.use_mouse_depth_navigate = self._saved_navigation_options[0]
            inputs.use_rotate_around_active = self._saved_navigation_options[1]
        except (AttributeError, TypeError, ValueError):
            pass
        self._saved_navigation_options = None

    def _prepare_native_middle_orbit(self, context, event):
        obj = _active_sculpt_object(context)
        region = context.region
        region_3d = context.region_data
        if obj is None or region is None or region_3d is None:
            return

        hit, location, hit_obj = find_depth_hit(
            context,
            region,
            region_3d,
            event.mouse_region_x,
            event.mouse_region_y,
        )
        inputs = context.preferences.inputs
        self._saved_navigation_options = (
            bool(inputs.use_mouse_depth_navigate),
            bool(inputs.use_rotate_around_active),
        )
        if hit and hit_obj == obj:
            # Native behavior: orbit around the surface point under the cursor.
            inputs.use_mouse_depth_navigate = True
            inputs.use_rotate_around_active = False
        else:
            # Native dynamic offset around the selected object, without moving
            # the viewport centre or reconstructing the rotation formula.
            inputs.use_mouse_depth_navigate = False
            inputs.use_rotate_around_active = True

    def _zoom_with_mode_sensitivity(self, context, event):
        """Apply ZBrush-mode sensitivity with surface or object-centre pivot."""
        obj = _active_sculpt_object(context)
        region = context.region
        region_3d = context.region_data
        if obj is None or region is None or region_3d is None:
            return False

        hit, _location, hit_obj = find_depth_hit(
            context,
            region,
            region_3d,
            event.mouse_region_x,
            event.mouse_region_y,
        )
        pivot = location if hit and hit_obj == obj else _mesh_bounds_center(obj)

        zoom_in = event.type in {"WHEELUPMOUSE", "WHEELINMOUSE"}
        inputs = context.preferences.inputs
        if inputs.invert_zoom_wheel or inputs.invert_mouse_zoom:
            zoom_in = not zoom_in
        # Scale Blender's native 1.2 wheel step by the ZBrush-mode slider.
        sensitivity = _zoom_sensitivity(context)
        factor = (1.0 / 1.2) ** sensitivity if zoom_in else 1.2 ** sensitivity
        previous_distance = max(region_3d.view_distance, 0.0001)
        new_distance = max(previous_distance * factor, 0.0001)
        ratio = new_distance / previous_distance
        region_3d.view_location = pivot + (region_3d.view_location - pivot) * ratio
        region_3d.view_distance = new_distance
        if context.area:
            context.area.tag_redraw()
        return True

    def _handle_floating_panel_event(self, context, event):
        layout = _floating_panel_layout(context)
        if layout is None:
            return False
        state, panel_rect, title_rect, controls = layout

        if event.type == "MOUSEMOVE":
            state["mouse"] = (event.mouse_region_x, event.mouse_region_y)
            if state["dragging"]:
                offset_x, offset_y = state["drag_offset"]
                state["x"] = event.mouse_region_x - offset_x
                state["y"] = event.mouse_region_y - offset_y
                _remember_panel_position(context, state)
                if context.area:
                    context.area.tag_redraw()
                return True
            if state["slider_dragging"]:
                _set_subdivision_from_slider(
                    context, event.mouse_region_x, controls["subdivision_slider"]
                )
                if context.area:
                    context.area.tag_redraw()
                return True
            if state["rotation_slider_dragging"]:
                _set_sensitivity_from_slider(
                    context, "rotation_sensitivity", event.mouse_region_x,
                    controls["rotation_sensitivity"],
                )
                if context.area:
                    context.area.tag_redraw()
                return True
            if state["zoom_slider_dragging"]:
                _set_sensitivity_from_slider(
                    context, "zoom_sensitivity", event.mouse_region_x,
                    controls["zoom_sensitivity"],
                )
                if context.area:
                    context.area.tag_redraw()
                return True

        if event.type == "LEFTMOUSE" and event.value == "PRESS":
            mouse_x = event.mouse_region_x
            mouse_y = event.mouse_region_y
            if _point_in_rect(mouse_x, mouse_y, title_rect):
                state["dragging"] = True
                state["drag_offset"] = (mouse_x - state["x"], mouse_y - state["y"])
                return True
            kind, action = _floating_panel_hit(context, mouse_x, mouse_y)
            if kind == "action" and action is not None:
                _activate_floating_panel_action(context, action)
            elif kind == "slider":
                if action == "subdiv:level":
                    state["slider_dragging"] = True
                    _set_subdivision_from_slider(context, mouse_x, controls["subdivision_slider"])
                elif action == "rotation:sensitivity":
                    state["rotation_slider_dragging"] = True
                    _set_sensitivity_from_slider(
                        context, "rotation_sensitivity", mouse_x,
                        controls["rotation_sensitivity"],
                    )
                elif action == "zoom:sensitivity":
                    state["zoom_slider_dragging"] = True
                    _set_sensitivity_from_slider(
                        context, "zoom_sensitivity", mouse_x,
                        controls["zoom_sensitivity"],
                    )
            if _point_in_rect(mouse_x, mouse_y, panel_rect):
                self._panel_click_consumed = True
                return True

        if event.type == "LEFTMOUSE" and event.value == "RELEASE":
            state["dragging"] = False
            state["slider_dragging"] = False
            state["rotation_slider_dragging"] = False
            state["zoom_slider_dragging"] = False
            if self._panel_click_consumed:
                self._panel_click_consumed = False
                return True
        return False

    def modal(self, context, event):
        global MOVE_MODE_HOVER
        if context.area is None or context.area.type != "VIEW_3D":
            self._restore_navigation_options(context)
            return {"PASS_THROUGH"}
        if not is_navigation_enabled(context):
            MOVE_MODE_HOVER = None
            self._restore_navigation_options(context)
            return {"PASS_THROUGH"}
        if context.mode != "SCULPT":
            MOVE_MODE_HOVER = None
            self._restore_navigation_options(context)
            return {"PASS_THROUGH"}

        if event.type in {"CAPSLOCK", "CAPS_LOCK", "CAPSLOCKKEY"} and event.value == "PRESS":
            _trigger_capslock_isolation(context)
            return {"RUNNING_MODAL"}

        if self._handle_floating_panel_event(context, event):
            return {"RUNNING_MODAL"}

        if event.type == "MIDDLEMOUSE" and event.value == "PRESS":
            if not (event.alt or event.ctrl or event.shift or event.oskey):
                try:
                    self._prepare_native_middle_orbit(context, event)
                except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
                    _draw_error(context, "native middle orbit prepare", exc)
            return {"PASS_THROUGH"}

        if event.type == "MIDDLEMOUSE" and event.value == "RELEASE":
            self._restore_navigation_options(context)
            return {"PASS_THROUGH"}

        if event.type in {"ESC", "RIGHTMOUSE"} and event.value == "PRESS":
            self._restore_navigation_options(context)

        if event.type in {"WHEELUPMOUSE", "WHEELDOWNMOUSE", "WHEELINMOUSE", "WHEELOUTMOUSE"}:
            try:
                if self._zoom_with_mode_sensitivity(context, event):
                    return {"RUNNING_MODAL"}
            except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
                _draw_error(context, "object-centred zoom", exc)

        if event.type == "MOUSEMOVE":
            hover = None
            if _is_move_tool_active(context):
                hover = _move_mode_pick(
                    context, event.mouse_region_x, event.mouse_region_y
                )
            if hover != MOVE_MODE_HOVER:
                MOVE_MODE_HOVER = hover
                if context.area:
                    context.area.tag_redraw()

        return {"PASS_THROUGH"}


class ZBNAV_OT_brush_size_drag(bpy.types.Operator):
    """按住触发键 + 左键左右拖动调整笔刷大小。"""

    bl_idname = "zb_nav.brush_size_drag"
    bl_label = "Brush Size Drag"
    bl_description = "按住触发键再按住左键左右拖动，调整笔刷大小"
    bl_options = {"INTERNAL"}

    _start_mouse_x = 0
    _start_size = 0
    _dragging = False

    @classmethod
    def poll(cls, context):
        return is_zbrush_sculpt_mode(context) and _brush_size_owner(context)[0] is not None

    def invoke(self, context, event):
        if event.type != _trigger_key_type(context) or event.value != "PRESS":
            return {"PASS_THROUGH"}
        size = _brush_size_value(context)
        if size is None:
            return {"PASS_THROUGH"}

        self._start_size = size
        self._start_mouse_x = event.mouse_region_x
        self._dragging = False
        context.window_manager.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def _finish(self, context):
        self._dragging = False
        return {"FINISHED"}

    def _cancel(self, context):
        if self._dragging:
            _set_brush_size(context, self._start_size)
        return self._finish(context)

    def modal(self, context, event):
        trigger = _trigger_key_type(context)

        if event.type == "ESC" and event.value == "PRESS":
            return self._cancel(context)
        if event.type == trigger and event.value == "RELEASE":
            return self._finish(context)
        if not is_zbrush_sculpt_mode(context):
            return self._cancel(context)

        if event.type == "LEFTMOUSE" and event.value == "PRESS":
            self._start_mouse_x = event.mouse_region_x
            self._dragging = True
            return {"RUNNING_MODAL"}

        if event.type == "LEFTMOUSE" and event.value == "RELEASE":
            if self._dragging:
                return self._finish(context)
            return {"RUNNING_MODAL"}

        if event.type == "MOUSEMOVE" and self._dragging:
            delta_x = event.mouse_region_x - self._start_mouse_x
            target = _brush_size_from_drag(context, self._start_size, delta_x)
            if _set_brush_size(context, target) and context.area:
                context.area.tag_redraw()
            return {"RUNNING_MODAL"}

        return {"RUNNING_MODAL"}


def add_zbrush_keymaps(context=None):
    context = context or bpy.context
    remove_zbrush_keymaps()
    swap_sculpt_brush_modifiers()
    for item in ZBRUSH_KEYMAP_ITEMS:
        _add_keymap_item(item)
    for item in _trigger_keymap_items(context):
        _add_keymap_item(item)


def update_navigation_mode(context, mode):
    """切换子模式及其悬停监测。"""
    global MOVE_MODE_HOVER, _CAPSLOCK_LAST_STATE
    was_enabled = is_navigation_enabled(context)
    if mode == "ZBRUSH":
        _apply_trackball_mode(context)
    elif was_enabled:
        _restore_rotate_method(context)
        _restore_all_isolation()
    if (
        mode == "ZBRUSH"
        and context.mode == "SCULPT"
        and not GIZMO_HOVER_MONITOR_RUNNING
    ):
        try:
            bpy.ops.zb_nav.gizmo_hover_monitor("INVOKE_DEFAULT")
        except (RuntimeError, TypeError):
            pass
    else:
        MOVE_MODE_HOVER = None
    set_nav_mode(context, mode)
    if mode == "ZBRUSH":
        _start_capslock_watch()
    else:
        _CAPSLOCK_LAST_STATE = None
    tag_all_view3d_areas_for_redraw()
    _schedule_trigger_key_sync()
    return True


_SCULPT_MODE_LAST_STATE = None


def _sculpt_mode_handler(scene, depsgraph):
    """Keep the sub-mode, its hover monitor and the trigger key in sync."""
    global _SCULPT_MODE_LAST_STATE
    context = bpy.context
    is_sculpt = context.mode == "SCULPT"
    if is_sculpt == _SCULPT_MODE_LAST_STATE:
        return
    _SCULPT_MODE_LAST_STATE = is_sculpt

    if is_sculpt and is_navigation_enabled(context):
        try:
            update_navigation_mode(context, "ZBRUSH")
        except Exception as exc:
            _draw_error(context, "sculpt mode handler", exc)
    _schedule_trigger_key_sync()


def _on_trigger_key_changed(self, context):
    """Re-register the trigger keymap and free up the newly chosen key."""
    try:
        add_zbrush_keymaps(context)
    except Exception as exc:
        _draw_error(context, "trigger key update", exc)
    _schedule_trigger_key_sync()


class ZBNAV_AddonPreferences(bpy.types.AddonPreferences):
    bl_idname = __name__

    trigger_key: EnumProperty(
        name="触发键",
        description="按住它再按住左键左右拖动，即可调整笔刷大小",
        items=TRIGGER_KEY_ITEMS,
        default="S",
        update=_on_trigger_key_changed,
    )

    brush_size_sensitivity: FloatProperty(
        name="笔刷灵敏度",
        description="每像素拖动改变多少笔刷大小（相对当前大小）",
        default=0.5,
        min=0.05,
        max=5.0,
        soft_min=0.1,
        soft_max=2.0,
        step=1,
        precision=2,
    )

    rotation_sensitivity: FloatProperty(
        name="旋转灵敏度",
        description="ZBrush 模式下的视图旋转灵敏度倍率",
        default=1.0,
        min=VIEW_SENSITIVITY_MIN,
        max=VIEW_SENSITIVITY_MAX,
        soft_min=0.25,
        soft_max=2.0,
        step=10,
        precision=2,
        update=_on_rotation_sensitivity_changed,
    )

    floating_panel_position: FloatVectorProperty(
        name="浮动面板位置",
        description="浮动工具面板在 3D 视口中的相对位置",
        default=(0.05, 0.84),
        min=0.0,
        max=1.0,
        size=2,
    )

    pan_sensitivity: FloatProperty(
        name="Pan Sensitivity",
        description="ZBrush 模式下 Alt + 鼠标中键平移视图的敏感度",
        default=1.0,
        min=0.1,
        max=5.0,
        soft_min=0.25,
        soft_max=3.0,
        step=10,
    )

    zoom_sensitivity: FloatProperty(
        name="Zoom Sensitivity",
        description="ZBrush 模式下松开 Alt 后缩放视图的敏感度",
        default=1.0,
        min=0.1,
        max=5.0,
        soft_min=0.25,
        soft_max=3.0,
        step=10,
    )

    axis_color_x: FloatVectorProperty(
        name="X 轴颜色",
        description="控制轴 X 轴颜色",
        default=(0.42, 0.055, 0.055),
        min=0.0, max=1.0, size=3, subtype="COLOR",
    )
    axis_color_y: FloatVectorProperty(
        name="Y 轴颜色",
        description="控制轴 Y 轴颜色",
        default=(0.055, 0.42, 0.08),
        min=0.0, max=1.0, size=3, subtype="COLOR",
    )
    axis_color_z: FloatVectorProperty(
        name="Z 轴颜色",
        description="控制轴 Z 轴颜色",
        default=(0.07, 0.18, 0.48),
        min=0.0, max=1.0, size=3, subtype="COLOR",
    )
    center_scale_color: FloatVectorProperty(
        name="中心整体缩放颜色",
        description="控制轴中心整体缩放方块的颜色",
        default=(1.0, 0.68, 0.05),
        min=0.0, max=1.0, size=3, subtype="COLOR",
    )

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = False

        column = layout.column(align=True)
        row = column.row(align=True)
        row.prop(self, "trigger_key", text="触发键")
        row.prop(self, "brush_size_sensitivity", text="笔刷", slider=True)
        row = column.row(align=True)
        row.prop(self, "rotation_sensitivity", text="旋转灵敏度", slider=True)
        row.prop(self, "zoom_sensitivity", text="缩放灵敏度", slider=True)

        row = layout.row(align=True)
        row.label(text="轴色")
        row.prop(self, "axis_color_x", text="X")
        row.prop(self, "axis_color_y", text="Y")
        row.prop(self, "axis_color_z", text="Z")
        row = layout.row(align=True)
        row.label(text="整体缩放")
        row.prop(self, "center_scale_color", text="中心颜色")

class ZBNAV_OT_pan_or_zoom(bpy.types.Operator):
    bl_idname = "zb_nav.pan_or_zoom"
    bl_label = "ZBrush Pan / Zoom"
    bl_description = "按住 Alt + 鼠标中键拖动平移，拖动中松开 Alt 切换为缩放"
    bl_options = {"REGISTER", "BLOCKING", "GRAB_CURSOR"}

    _start_mouse_x = 0
    _start_mouse_y = 0
    _last_mouse_x = 0
    _last_mouse_y = 0
    _region = None
    _region_3d = None
    _start_view_location = None
    _start_view_distance = 0.0
    _last_depth_location = None
    _pivot_location = None
    _was_alt_pressed = True
    _converted_pan_to_zoom = False

    @classmethod
    def poll(cls, context):
        return (
            context.area
            and context.area.type == "VIEW_3D"
            and context.region_data
            and is_navigation_enabled(context)
        )

    def invoke(self, context, event):
        self._region = context.region
        self._region_3d = context.region_data
        self._start_mouse_x = event.mouse_region_x
        self._start_mouse_y = event.mouse_region_y
        self._last_mouse_x = event.mouse_region_x
        self._last_mouse_y = event.mouse_region_y
        self._start_view_location = self._region_3d.view_location.copy()
        self._start_view_distance = self._region_3d.view_distance
        self._last_depth_location = find_depth_location(
            context,
            self._region,
            self._region_3d,
            event.mouse_region_x,
            event.mouse_region_y,
        )
        if self._last_depth_location is None:
            obj = _active_sculpt_object(context)
            if obj is not None:
                self._last_depth_location = _mesh_bounds_center(obj)
        self._pivot_location = self._last_depth_location or self._start_view_location.copy()
        self._was_alt_pressed = event.alt
        self._converted_pan_to_zoom = False
        context.window_manager.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        try:
            if event.type in {"MIDDLEMOUSE", "ESC"} and event.value == "RELEASE":
                return {"FINISHED"}

            if event.type == "MOUSEMOVE":
                current_x = event.mouse_region_x
                current_y = event.mouse_region_y
                dx = current_x - self._last_mouse_x
                dy = current_y - self._last_mouse_y

                if event.alt:
                    self._pan_view(context, current_x, current_y)
                else:
                    if self._was_alt_pressed and not self._converted_pan_to_zoom:
                        self._convert_pan_preview_to_zoom(context, current_x, current_y)
                    else:
                        self._zoom_view(context, current_x, current_y, dx, dy)

                self._was_alt_pressed = event.alt
                self._last_mouse_x = current_x
                self._last_mouse_y = current_y
                context.area.tag_redraw()
                return {"RUNNING_MODAL"}

            return {"RUNNING_MODAL"}
        except Exception as exc:
            self.report({"WARNING"}, f"ZB-Nav pan/zoom stopped: {exc}")
            return {"CANCELLED"}

    def _pan_view(self, context, current_x, current_y):
        prefs = get_preferences(context)
        sensitivity = prefs.pan_sensitivity if prefs else 1.0
        depth_location = self._pivot_location or self._last_depth_location or self._region_3d.view_location
        previous_location = location_on_depth(
            self._region,
            self._region_3d,
            self._last_mouse_x,
            self._last_mouse_y,
            depth_location,
        )
        current_location = location_on_depth(
            self._region,
            self._region_3d,
            current_x,
            current_y,
            depth_location,
        )
        self._region_3d.view_location += (previous_location - current_location) * sensitivity

    def _convert_pan_preview_to_zoom(self, context, current_x, current_y):
        self._region_3d.view_location = self._start_view_location.copy()
        self._region_3d.view_distance = self._start_view_distance
        self._zoom_view(
            context,
            current_x,
            current_y,
            current_x - self._start_mouse_x,
            current_y - self._start_mouse_y,
        )
        self._converted_pan_to_zoom = True

    def _zoom_view(self, context, current_x, current_y, dx, dy):
        # Keep the pivot captured when Alt+MMB was pressed. Re-sampling depth
        # here makes the centre jump as the model grows under the mouse.
        focus_location = self._pivot_location or self._last_depth_location or self._start_view_location

        sensitivity = _zoom_sensitivity(context)
        previous_distance = max(self._region_3d.view_distance, 0.0001)
        # Blender's region Y coordinate increases upward; invert it so screen-down
        # contributes in the same direction as screen-right, matching ZBrush.
        delta = dx - dy
        zoom_factor = math.exp(-delta * 0.01 * sensitivity)
        new_distance = max(previous_distance * zoom_factor, 0.0001)
        ratio = new_distance / previous_distance

        self._region_3d.view_location = focus_location + (self._region_3d.view_location - focus_location) * ratio
        self._region_3d.view_distance = new_distance


def _blender_trackball_vector(region, delta_x, delta_y):
    """Match Blender's ``calctrackballvec`` mapping for a drag delta."""
    size = max(1.0, float(min(region.width, region.height)))
    x = 2.0 * delta_x / size
    y = 2.0 * delta_y / size
    length_sq = x * x + y * y
    distance = math.sqrt(length_sq)
    # Blender uses a sphere inside radius 1/sqrt(2), then a hyperbola outside.
    # This avoids the direction and speed distortion of plain sphere mapping.
    if distance < 1.0 / math.sqrt(2.0):
        z = math.sqrt(max(0.0, 1.0 - length_sq))
    else:
        z = 0.5 / max(distance, 1e-8)
    return mathutils.Vector((x, y, z))


def _trackball_rotation(context, region, base_rotation, start_x, start_y, current_x, current_y):
    """Return Blender-native trackball rotation for this drag."""
    # Native MMB orbit initializes from the viewport center. Subtracting the
    # press position here is equivalent to its internal event_xy_offset.
    start = _blender_trackball_vector(region, 0.0, 0.0)
    current = _blender_trackball_vector(
        region,
        current_x - start_x,
        current_y - start_y,
    )
    sensitivity = context.preferences.inputs.view_rotate_sensitivity_trackball
    angle = (current - start).length * math.pi * 0.5 * sensitivity
    angle = (angle + math.pi) % (2.0 * math.pi) - math.pi
    axis = start.cross(current)
    if axis.length < 1e-8:
        return base_rotation.copy()
    rotation = mathutils.Quaternion(axis.normalized(), angle) @ base_rotation
    rotation.normalize()
    return rotation


def _orbit_view_about_pivot(
    region_3d,
    start_rotation,
    start_location,
    pivot,
    new_rotation,
):
    """Match Blender's ``view3d_orbit_apply_dyn_ofs`` around a pivot."""
    ofs_old = -start_location
    dyn_ofs = -pivot
    delta = start_rotation.inverted() @ new_rotation
    delta.invert()
    ofs_new = dyn_ofs + delta @ (ofs_old - dyn_ofs)
    region_3d.view_rotation = new_rotation
    region_3d.view_location = -ofs_new


class ZBNAV_OT_alt_select_target(bpy.types.Operator):
    bl_idname = "zb_nav.alt_select_target"
    bl_label = "Switch Sculpt Target"
    bl_description = "Alt + 左键单击切换雕刻对象，拖动则旋转视图"
    bl_options = {"INTERNAL"}

    _press_x = 0
    _press_y = 0
    _orbiting = False
    _start_rotation = None
    _start_view_location = None
    _start_view_distance = 0.0
    _orbit_pivot = None

    @classmethod
    def poll(cls, context):
        return is_zbrush_sculpt_mode(context)

    def invoke(self, context, event):
        if not (event.alt and event.type == "LEFTMOUSE" and event.value == "PRESS"):
            return {"PASS_THROUGH"}
        self._press_x = event.mouse_region_x
        self._press_y = event.mouse_region_y
        self._orbiting = False
        region_3d = context.region_data
        self._start_rotation = region_3d.view_rotation.copy()
        self._start_view_location = region_3d.view_location.copy()
        self._start_view_distance = region_3d.view_distance
        self._orbit_pivot = None
        context.window_manager.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def _orbit_view(self, context, event):
        """Trackball-orbit the camera around the active sculpt object."""
        region_3d = context.region_data
        region = context.region
        if region_3d is None or region is None:
            return
        rotation = _trackball_rotation(
            context,
            region,
            self._start_rotation,
            self._press_x,
            self._press_y,
            event.mouse_region_x,
            event.mouse_region_y,
        )
        if self._start_view_location is not None and self._orbit_pivot is not None:
            _orbit_view_about_pivot(
                region_3d,
                self._start_rotation,
                self._start_view_location,
                self._orbit_pivot,
                rotation,
            )
        else:
            region_3d.view_rotation = rotation
        if context.area:
            context.area.tag_redraw()

    def modal(self, context, event):
        if event.type == "MOUSEMOVE":
            if not self._orbiting:
                if (
                    abs(event.mouse_region_x - self._press_x) > ALT_DRAG_THRESHOLD
                    or abs(event.mouse_region_y - self._press_y) > ALT_DRAG_THRESHOLD
                ):
                    self._orbit_pivot = find_navigation_pivot(
                        context,
                        context.region,
                        context.region_data,
                        self._press_x,
                        self._press_y,
                    ) or self._start_view_location.copy()
                    self._orbiting = True
            if self._orbiting:
                self._orbit_view(context, event)
            return {"RUNNING_MODAL"}

        if event.type == "LEFTMOUSE" and event.value == "RELEASE":
            if not self._orbiting:
                select_or_invert_sculpt_target(context, event)
            return {"FINISHED"}

        if event.type in {"ESC", "RIGHTMOUSE"} and event.value == "PRESS":
            return {"FINISHED"}

        return {"RUNNING_MODAL"}


class ZBNAV_OT_set_navigation_mode(bpy.types.Operator):
    bl_idname = "zb_nav.set_navigation_mode"
    bl_label = "Toggle ZBrush Sculpt Mode"
    bl_description = "在当前雕刻模式中启用或退出 ZBrush 导航子模式"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context):
        return context.area and context.area.type == "VIEW_3D"

    def execute(self, context):
        next_mode = "BLENDER" if is_navigation_enabled(context) else "ZBRUSH"
        update_navigation_mode(context, next_mode)
        return {"FINISHED"}


class ZBNAV_OT_tool_shortcut(bpy.types.Operator):
    bl_idname = "zb_nav.tool_shortcut"
    bl_label = "ZB-Nav Tool Shortcut"
    bl_options = {"INTERNAL"}

    action: StringProperty(default="")

    @classmethod
    def poll(cls, context):
        return is_zbrush_sculpt_mode(context)

    def execute(self, context):
        if _activate_floating_panel_action(context, self.action):
            return {"FINISHED"}
        return {"CANCELLED"}


def _draw_line_2d(shader, a, b, color):
    batch = batch_for_shader(shader, "LINES", {"pos": [a, b]})
    shader.bind()
    shader.uniform_float("color", color)
    batch.draw(shader)


def _draw_polyline_2d(shader, points, color, close=False):
    if len(points) >= 2:
        draw_points = points + [points[0]] if close else points
        batch = batch_for_shader(shader, "LINE_STRIP", {"pos": draw_points})
        shader.bind()
        shader.uniform_float("color", color)
        batch.draw(shader)


def _draw_tri_2d(shader, a, b, c, color):
    batch = batch_for_shader(shader, "TRIS", {"pos": [a, b, c]})
    shader.bind()
    shader.uniform_float("color", color)
    batch.draw(shader)


def _draw_rect_2d(shader, rect, color):
    x, y, width, height = rect
    verts = [
        (x, y), (x + width, y), (x + width, y + height),
        (x, y), (x + width, y + height), (x, y + height),
    ]
    batch = batch_for_shader(shader, "TRIS", {"pos": verts})
    shader.bind()
    shader.uniform_float("color", color)
    batch.draw(shader)


def _rounded_rect_points(rect, radius, segments=6):
    x, y, width, height = rect
    radius = min(radius, width * 0.5, height * 0.5)
    points = []
    corners = (
        (x + width - radius, y + radius, -math.pi * 0.5, 0.0),
        (x + width - radius, y + height - radius, 0.0, math.pi * 0.5),
        (x + radius, y + height - radius, math.pi * 0.5, math.pi),
        (x + radius, y + radius, math.pi, math.pi * 1.5),
    )
    for cx, cy, start, end in corners:
        for step in range(segments + 1):
            angle = start + (end - start) * step / segments
            points.append((cx + math.cos(angle) * radius, cy + math.sin(angle) * radius))
    return points


def _draw_rounded_rect_2d(shader, rect, radius, color):
    points = _rounded_rect_points(rect, radius)
    x, y, width, height = rect
    center = (x + width * 0.5, y + height * 0.5)
    verts = []
    for index, point in enumerate(points):
        verts.extend((center, point, points[(index + 1) % len(points)]))
    batch = batch_for_shader(shader, "TRIS", {"pos": verts})
    shader.bind()
    shader.uniform_float("color", color)
    batch.draw(shader)


def _draw_text_2d(x, y, text, size, color):
    font_id = 0
    blf.size(font_id, max(8, int(size)))
    blf.color(font_id, color[0], color[1], color[2], color[3])
    blf.position(font_id, x, y, 0)
    blf.draw(font_id, text)


def _draw_centered_text_2d(x, y, width, height, text, size, color):
    font_id = 0
    blf.size(font_id, max(8, int(size)))
    text_width, text_height = blf.dimensions(font_id, text)
    _draw_text_2d(
        x + max(0.0, (width - text_width) * 0.5),
        y + max(0.0, (height - text_height) * 0.5) + size * 0.12,
        text,
        size,
        color,
    )


def _draw_tool_button_icon(shader, context, action, rect, color):
    """Draw the two icons used by the compact tool panel."""
    x, y, width, height = rect
    cx, cy = x + width * 0.5, y + height * 0.5
    s = min(width, height)
    line_width = max(1.0, _px(context, 1.6))
    gpu.state.line_width_set(line_width)

    if action == "isolate:toggle":
        half = s * 0.23
        arm = s * 0.09
        for xs, ys in ((-1, -1), (1, -1), (-1, 1), (1, 1)):
            px, py = cx + xs * half, cy + ys * half
            _draw_line_2d(shader, (px, py), (px - xs * arm, py), color)
            _draw_line_2d(shader, (px, py), (px, py - ys * arm), color)
        _draw_circle_outline_2d(shader, (cx, cy), s * 0.10, color, line_width, 18)
        _draw_filled_circle_2d(shader, (cx, cy), s * 0.035, color, segments=10)
    elif action == "wire:toggle":
        half = s * 0.23
        _draw_polyline_2d(
            shader,
            [
                (cx - half, cy - half), (cx + half, cy - half),
                (cx + half, cy + half), (cx - half, cy + half),
                (cx - half, cy - half),
            ],
            color,
        )
        for offset in (-half / 3.0, half / 3.0):
            _draw_line_2d(shader, (cx - half, cy + offset), (cx + half, cy + offset), color)
            _draw_line_2d(shader, (cx + offset, cy - half), (cx + offset, cy + half), color)
    elif action == "subdiv:toggle":
        half = s * 0.24
        _draw_polyline_2d(
            shader,
            [
                (cx - half, cy - half), (cx, cy), (cx + half, cy - half),
                (cx + half, cy + half), (cx, cy), (cx - half, cy + half),
                (cx - half, cy - half),
            ],
            color,
        )
        _draw_line_2d(shader, (cx - half, cy), (cx + half, cy), color)
        _draw_line_2d(shader, (cx, cy - half), (cx, cy + half), color)
    gpu.state.line_width_set(1.0)


def _draw_tri_along_axis(shader, apex, base, color, half_width):
    dx = base[0] - apex[0]
    dy = base[1] - apex[1]
    norm = math.hypot(dx, dy) or 1.0
    ux, uy = dx / norm, dy / norm
    px, py = -uy, ux
    left = (base[0] + px * half_width, base[1] + py * half_width)
    right = (base[0] - px * half_width, base[1] - py * half_width)
    _draw_tri_2d(shader, (apex[0], apex[1]), left, right, color)


def _draw_filled_circle_2d(shader, center, radius, color, segments=20):
    cx, cy = center
    verts = []
    for i in range(segments):
        a1 = 2.0 * math.pi * i / segments
        a2 = 2.0 * math.pi * (i + 1) / segments
        verts.append((cx, cy))
        verts.append((cx + math.cos(a1) * radius, cy + math.sin(a1) * radius))
        verts.append((cx + math.cos(a2) * radius, cy + math.sin(a2) * radius))
    batch = batch_for_shader(shader, "TRIS", {"pos": verts})
    shader.bind()
    shader.uniform_float("color", color)
    batch.draw(shader)


def _draw_circle_outline_2d(shader, center, radius, color, width=1.0, segments=32):
    points = []
    for step in range(segments + 1):
        angle = 2.0 * math.pi * step / segments
        points.append((center[0] + math.cos(angle) * radius, center[1] + math.sin(angle) * radius))
    gpu.state.line_width_set(width)
    _draw_polyline_2d(shader, points, color)


def _button_offset(_context):
    """Pivot button height as a multiple of the gizmo's on-screen pixel length."""
    return GIZMO_BUTTON_Y_OFFSET


def _button_spacing(context):
    return _px(context, GIZMO_BUTTON_SPACING)


def _gizmo_button_positions(context, origin_screen):
    pixel_offset = _axis_pixels(context) * _button_offset(context)
    spacing = _button_spacing(context)
    base_y = origin_screen.y + pixel_offset
    return [
        (origin_screen.x - spacing, base_y),
        (origin_screen.x, base_y),
        (origin_screen.x + spacing, base_y),
    ]


def _button_size(context):
    return _px(context, GIZMO_BUTTON_RADIUS)


def _button_color(_context):
    return GIZMO_BUTTON_COLOR


def _draw_button_icon(shader, cx, cy, kind, hover, size, color):
    """Draw one simple borderless icon with smooth line rendering."""
    s = size / 10.0
    icon_rgb = _vivid_color(color) if hover else (color[0], color[1], color[2])
    icon = (icon_rgb[0], icon_rgb[1], icon_rgb[2], 1.0)
    try:
        gpu.state.line_smooth_set(True)
    except (AttributeError, RuntimeError):
        pass

    if kind == 0:
        # Reset axis: one simple circular arrow.
        points = []
        for step in range(20):
            angle = math.radians(45.0) + math.radians(275.0) * step / 19.0
            points.append((cx + math.cos(angle) * 6.0 * s, cy + math.sin(angle) * 6.0 * s))
        gpu.state.line_width_set(1.8 * s)
        _draw_polyline_2d(shader, points, icon)
        ex, ey = points[-1]
        _draw_tri_2d(shader, (ex, ey), (ex - 4.0 * s, ey + 0.5 * s), (ex - 0.8 * s, ey - 3.0 * s), icon)
    elif kind == 1:
        # Object origin: minimal crosshair.
        gpu.state.line_width_set(1.8 * s)
        _draw_line_2d(shader, (cx - 7.0 * s, cy), (cx + 7.0 * s, cy), icon)
        _draw_line_2d(shader, (cx, cy - 7.0 * s), (cx, cy + 7.0 * s), icon)
        _draw_filled_circle_2d(shader, (cx, cy), 1.5 * s, icon, segments=12)
    else:
        # Mesh center: inverted address/location pin.
        radius = 5.5 * s
        curve = []
        for step in range(49):
            angle = math.pi - math.pi * step / 48.0
            curve.append((cx + math.cos(angle) * radius, cy + 1.0 * s + math.sin(angle) * radius))
        outline = curve + [(cx, cy - 7.0 * s)]
        gpu.state.line_width_set(1.8 * s)
        _draw_polyline_2d(shader, outline + [outline[0]], icon)
        _draw_circle_outline_2d(shader, (cx, cy + 1.0 * s), 1.8 * s, icon, 1.5 * s, 16)

    gpu.state.line_width_set(1.0)
    try:
        gpu.state.line_smooth_set(False)
    except (AttributeError, RuntimeError):
        pass
def _draw_gizmo_button_tooltip(shader, context, button_index, bx, by, button_size):
    """Draw a small, plain text-only tooltip."""
    region = context.region
    if not region or not (0 <= button_index < len(GIZMO_BUTTON_TOOLTIPS)):
        return
    title, detail = GIZMO_BUTTON_TOOLTIPS[button_index]
    font_id = 0
    ui = _ui_scale(context)
    x = min(
        max(bx + button_size + 8.0 * ui, 8.0 * ui),
        max(region.width - 180.0 * ui, 8.0 * ui),
    )
    y = by + 5.0 * ui
    blf.size(font_id, int(12 * ui))
    blf.color(font_id, 1.0, 1.0, 1.0, 0.95)
    blf.position(font_id, x, y, 0)
    blf.draw(font_id, title)
    blf.size(font_id, int(10 * ui))
    blf.color(font_id, 0.72, 0.76, 0.82, 0.9)
    blf.position(font_id, x, y - 15.0 * ui, 0)
    blf.draw(font_id, detail)


def _draw_rect_along_axis(shader, center, axis_dir_screen, color, along, across):
    dx, dy = axis_dir_screen
    norm = math.hypot(dx, dy) or 1.0
    ux, uy = dx / norm, dy / norm
    px, py = -uy, ux
    half_a = along / 2.0
    half_c = across / 2.0
    corners = [
        (center[0] - ux * half_a + px * half_c, center[1] - uy * half_a + py * half_c),
        (center[0] + ux * half_a + px * half_c, center[1] + uy * half_a + py * half_c),
        (center[0] + ux * half_a - px * half_c, center[1] + uy * half_a - py * half_c),
        (center[0] - ux * half_a - px * half_c, center[1] - uy * half_a - py * half_c),
    ]
    _draw_tri_2d(shader, corners[0], corners[1], corners[2], color)
    _draw_tri_2d(shader, corners[0], corners[2], corners[3], color)


def _draw_ring_band_2d(shader, inner_points, outer_points, color):
    n = min(len(inner_points), len(outer_points))
    if n < 3:
        return
    verts = []
    for i in range(n):
        j = (i + 1) % n
        verts.append(inner_points[i])
        verts.append(outer_points[i])
        verts.append(outer_points[j])
        verts.append(inner_points[i])
        verts.append(outer_points[j])
        verts.append(inner_points[j])
    batch = batch_for_shader(shader, "TRIS", {"pos": verts})
    shader.bind()
    shader.uniform_float("color", color)
    batch.draw(shader)


def _draw_tube_band_2d(shader, context, origin, e1, e2, radius, tube_w, tube_h, view_dir, color):
    normal = e1.cross(e2)
    half_w = tube_w / 2.0
    half_h = tube_h / 2.0
    segments = GIZMO_RING_SEGMENTS

    # Sample one contiguous arc instead of filtering 0..2pi, so the visible
    # half never spans the sampling boundary and no chord is created.
    view_proj = view_dir - normal * view_dir.dot(normal)
    if view_proj.length < 1e-6:
        start_angle = 0.0
        arc = 2.0 * math.pi
    else:
        center_dir = (-view_proj).normalized()
        center_angle = math.atan2(center_dir.dot(e2), center_dir.dot(e1))
        start_angle = center_angle - math.pi / 2.0
        arc = math.pi

    strip = []
    for i in range(segments + 1):
        ang = start_angle + arc * i / segments
        e = e1 * math.cos(ang) + e2 * math.sin(ang)
        c = origin + e * radius
        a = c + e * half_w + normal * half_h
        b = c + e * half_w - normal * half_h
        sa = _to_screen(context, a)
        sb = _to_screen(context, b)
        if sa and sb:
            strip.append(((sa.x, sa.y), (sb.x, sb.y)))
    if len(strip) < 2:
        return
    verts = []
    for i in range(len(strip) - 1):
        a1, a2 = strip[i]
        b1, b2 = strip[i + 1]
        verts.append(a1)
        verts.append(a2)
        verts.append(b2)
        verts.append(a1)
        verts.append(b2)
        verts.append(b1)
    batch = batch_for_shader(shader, "TRIS", {"pos": verts})
    shader.bind()
    shader.uniform_float("color", color)
    batch.draw(shader)


def draw_floating_tool_panel():
    """Draw the movable ZBrush tool palette over the viewport."""
    context = bpy.context
    if not is_zbrush_sculpt_mode(context):
        return
    layout = _floating_panel_layout(context)
    if layout is None:
        return
    state, (x, y, width, height), title_rect, controls = layout
    isolate_rect = controls["isolate"]
    wire_rect = controls["wire"]
    topology_rect = controls["topology"]
    subdiv_rect = controls["subdivision"]
    slider_rect = controls["subdivision_slider"]
    clear_mask_rect = controls["clear_mask"]
    invert_mask_rect = controls["invert_mask"]
    rotation_sensitivity_rect = controls["rotation_sensitivity"]
    zoom_sensitivity_rect = controls["zoom_sensitivity"]
    ui = _ui_scale(context)
    shader = gpu.shader.from_builtin("UNIFORM_COLOR")
    gpu.state.blend_set("ALPHA")

    try:
        panel_radius = _px(context, 7)
        button_radius = _px(context, 5)
        _draw_rounded_rect_2d(
            shader,
            (x + _px(context, 3), y - _px(context, 3), width, height),
            panel_radius,
            (0.0, 0.0, 0.0, 0.35),
        )
        _draw_rounded_rect_2d(shader, (x, y, width, height), panel_radius, (0.105, 0.11, 0.115, 0.97))
        _draw_rounded_rect_2d(shader, title_rect, panel_radius, (0.19, 0.195, 0.20, 0.99))
        panel_points = _rounded_rect_points((x, y, width, height), panel_radius)
        outline = panel_points + [panel_points[0]]
        _draw_polyline_2d(shader, outline, (0.38, 0.40, 0.44, 0.95))

        mouse = state.get("mouse")
        hovered_tip = None

        def draw_button(rect, active, hovered):
            if active:
                fill = (1.0, 0.48, 0.06, 0.99)
            elif hovered:
                fill = (0.245, 0.25, 0.27, 0.99)
            else:
                fill = (0.185, 0.19, 0.20, 0.98)
            _draw_rounded_rect_2d(shader, rect, button_radius, fill)
            points = _rounded_rect_points(rect, button_radius)
            _draw_polyline_2d(shader, points + [points[0]], (0.34, 0.35, 0.37, 0.85))
            return (0.10, 0.075, 0.025, 1.0) if active else (0.94, 0.95, 0.97, 1.0)

        isolate_hovered = mouse is not None and _point_in_rect(mouse[0], mouse[1], isolate_rect)
        isolate_active = _isolation_active(context)
        isolate_color = draw_button(isolate_rect, isolate_active, isolate_hovered)
        if isolate_hovered:
            hovered_tip = "孤立当前物体"
        _draw_tool_button_icon(
            shader,
            context,
            "isolate:toggle",
            (isolate_rect[0] + _px(context, 9), isolate_rect[1] + _px(context, 10),
             isolate_rect[2] - _px(context, 18), isolate_rect[3] - _px(context, 18)),
            isolate_color,
        )
        _draw_centered_text_2d(
            isolate_rect[0], isolate_rect[1] + _px(context, 2),
            isolate_rect[2], _px(context, 11), "孤立", 8.5 * ui, isolate_color,
        )

        wire_hovered = mouse is not None and _point_in_rect(mouse[0], mouse[1], wire_rect)
        wire_active = _wire_active(context)
        wire_color = draw_button(wire_rect, wire_active, wire_hovered)
        if wire_hovered:
            hovered_tip = "显示面边线线框"
        _draw_tool_button_icon(
            shader,
            context,
            "wire:toggle",
            (wire_rect[0] + _px(context, 9), wire_rect[1] + _px(context, 10),
             wire_rect[2] - _px(context, 18), wire_rect[3] - _px(context, 18)),
            wire_color,
        )
        _draw_centered_text_2d(
            wire_rect[0], wire_rect[1] + _px(context, 2),
            wire_rect[2], _px(context, 11), "线框", 8.5 * ui, wire_color,
        )

        topology_hovered = mouse is not None and _point_in_rect(mouse[0], mouse[1], topology_rect)
        topology_active = _topology_active(context)
        topology_color = draw_button(topology_rect, topology_active, topology_hovered)
        if topology_hovered:
            hovered_tip = "切换当前笔刷的自动遮罩拓扑"
        _draw_centered_text_2d(
            topology_rect[0], topology_rect[1], topology_rect[2], topology_rect[3],
            "自动遮罩拓扑", 9.0 * ui, topology_color,
        )

        subdiv_hovered = mouse is not None and _point_in_rect(mouse[0], mouse[1], subdiv_rect)
        subdiv_active = _subdivision_active(context)
        subdiv_color = draw_button(subdiv_rect, subdiv_active, subdiv_hovered)
        if subdiv_hovered:
            hovered_tip = "细分显示开关"
        _draw_centered_text_2d(
            subdiv_rect[0], subdiv_rect[1], subdiv_rect[2], subdiv_rect[3],
            "细分", 9.0 * ui, subdiv_color,
        )

        level = _subdivision_level(context)
        has_subdivision = level is not None
        slider_hovered = mouse is not None and _point_in_rect(mouse[0], mouse[1], slider_rect)
        if slider_hovered:
            hovered_tip = "细分级别"
        _draw_rounded_rect_2d(
            shader, slider_rect, button_radius,
            (0.13, 0.135, 0.14, 0.96)
            if not has_subdivision
            else ((0.19, 0.195, 0.205, 0.99) if slider_hovered else (0.145, 0.15, 0.16, 0.98)),
        )
        track_left = slider_rect[0] + _px(context, 6)
        track_right = slider_rect[0] + slider_rect[2] - _px(context, 6)
        track_y = slider_rect[1] + _px(context, 5)
        _draw_line_2d(shader, (track_left, track_y), (track_right, track_y), (0.38, 0.40, 0.43, 1.0))
        factor = (
            (level - SUBDIVISION_LEVEL_MIN)
            / max(1, SUBDIVISION_LEVEL_MAX - SUBDIVISION_LEVEL_MIN)
            if has_subdivision else 0.0
        )
        knob_x = track_left + (track_right - track_left) * factor
        _draw_filled_circle_2d(
            shader, (knob_x, track_y), _px(context, 3.0),
            (1.0, 0.48, 0.06, 1.0) if has_subdivision else (0.38, 0.39, 0.41, 1.0),
            segments=12,
        )
        _draw_centered_text_2d(
            slider_rect[0], slider_rect[1] + _px(context, 8),
            slider_rect[2], slider_rect[3] - _px(context, 8),
            str(level) if has_subdivision else "—",
            8.5 * ui,
            (0.86, 0.88, 0.92, 1.0) if has_subdivision else (0.42, 0.44, 0.48, 1.0),
        )

        clear_hovered = mouse is not None and _point_in_rect(mouse[0], mouse[1], clear_mask_rect)
        clear_color = draw_button(clear_mask_rect, False, clear_hovered)
        if clear_hovered:
            hovered_tip = "清除全部遮罩"
        _draw_centered_text_2d(
            clear_mask_rect[0], clear_mask_rect[1], clear_mask_rect[2], clear_mask_rect[3],
            "取消遮罩", 8.5 * ui, clear_color,
        )

        invert_hovered = mouse is not None and _point_in_rect(mouse[0], mouse[1], invert_mask_rect)
        invert_color = draw_button(invert_mask_rect, False, invert_hovered)
        if invert_hovered:
            hovered_tip = "反转全部遮罩"
        _draw_centered_text_2d(
            invert_mask_rect[0], invert_mask_rect[1], invert_mask_rect[2], invert_mask_rect[3],
            "反转遮罩", 8.5 * ui, invert_color,
        )

        def draw_sensitivity_slider(rect, label, value):
            hovered = mouse is not None and _point_in_rect(mouse[0], mouse[1], rect)
            _draw_rounded_rect_2d(
                shader, rect, button_radius,
                (0.19, 0.195, 0.205, 0.99) if hovered else (0.145, 0.15, 0.16, 0.98),
            )
            track_left = rect[0] + _px(context, 8)
            track_right = rect[0] + rect[2] - _px(context, 8)
            track_y = rect[1] + _px(context, 7)
            _draw_text_2d(
                rect[0] + _px(context, 8),
                rect[1] + rect[3] - _px(context, 14),
                f"{label}  {value:.2f}",
                9.0 * ui,
                (0.86, 0.88, 0.92, 1.0),
            )
            _draw_line_2d(
                shader, (track_left, track_y), (track_right, track_y),
                (0.38, 0.40, 0.43, 1.0),
            )
            factor = (
                (value - VIEW_SENSITIVITY_MIN)
                / max(1e-6, VIEW_SENSITIVITY_MAX - VIEW_SENSITIVITY_MIN)
            )
            knob_x = track_left + (track_right - track_left) * factor
            _draw_filled_circle_2d(
                shader, (knob_x, track_y), _px(context, 3.2),
                (1.0, 0.48, 0.06, 1.0), segments=12,
            )
            return hovered

        if draw_sensitivity_slider(
            rotation_sensitivity_rect, "旋转敏感度", _rotation_sensitivity(context)
        ):
            hovered_tip = "ZBrush 模式下的视图旋转敏感度"
        if draw_sensitivity_slider(
            zoom_sensitivity_rect, "缩放敏感度", _zoom_sensitivity(context)
        ):
            hovered_tip = "ZBrush 模式下的视图缩放敏感度"

        font_id = 0
        title_size = 9.0 * ui
        blf.size(font_id, max(8, int(title_size)))
        _title_width, title_text_height = blf.dimensions(font_id, "zbrush")
        _draw_text_2d(
            title_rect[0] + _px(context, 8),
            title_rect[1] + (title_rect[3] - title_text_height) * 0.5,
            "zbrush",
            title_size,
            (0.92, 0.94, 0.98, 1.0),
        )
        grip_x = title_rect[0] + title_rect[2] - _px(context, 12)
        grip_y = title_rect[1] + title_rect[3] * 0.5 - _px(context, 5)
        for row in range(3):
            for col in range(2):
                _draw_filled_circle_2d(
                    shader,
                    (grip_x + col * _px(context, 5), grip_y + row * _px(context, 5)),
                    _px(context, 1.25),
                    (0.58, 0.62, 0.68, 0.95),
                    segments=8,
                )

        if hovered_tip:
            blf.size(font_id, max(8, int(10.5 * ui)))
            text_width, text_height = blf.dimensions(font_id, hovered_tip)
            tip_width = text_width + _px(context, 12)
            tip_height = text_height + _px(context, 8)
            tip_x = x + width + _px(context, 7)
            if tip_x + tip_width > context.region.width - _px(context, 4):
                tip_x = x - tip_width - _px(context, 7)
            tip_y = title_rect[1] + title_rect[3] - tip_height
            _draw_rect_2d(shader, (tip_x, tip_y, tip_width, tip_height), (0.04, 0.045, 0.05, 0.96))
            _draw_polyline_2d(
                shader,
                [
                    (tip_x, tip_y), (tip_x + tip_width, tip_y),
                    (tip_x + tip_width, tip_y + tip_height),
                    (tip_x, tip_y + tip_height), (tip_x, tip_y),
                ],
                (0.34, 0.37, 0.42, 0.95),
            )
            _draw_text_2d(
                tip_x + _px(context, 6),
                tip_y + _px(context, 4),
                hovered_tip,
                10.5 * ui,
                (0.92, 0.94, 0.98, 1.0),
            )
    except Exception as exc:
        _draw_error(context, "floating tool panel", exc)
    finally:
        gpu.state.blend_set("NONE")


def draw_move_mode_gizmo():
    context = bpy.context
    # The header toggle is the master switch: nothing is drawn while it is off.
    if not is_navigation_enabled(context):
        return
    if context.mode != "SCULPT":
        return
    if not _is_move_tool_active(context):
        return
    region = context.region
    region_3d = context.region_data
    if not region or region.type != "WINDOW" or not region_3d:
        return

    origin, axes = _gizmo_world_axes(context)
    if origin is None:
        return
    length = _gizmo_length(context)
    axis_pixels = _axis_pixels(context)

    shader = gpu.shader.from_builtin("UNIFORM_COLOR")
    gpu.state.blend_set("ALPHA")
    gpu.state.line_width_set(2.5)

    origin_screen = _to_screen(context, origin)
    if not origin_screen:
        return

    hover = MOVE_MODE_HOVER
    # Every handle is drawn: the visibility preferences were dropped when the
    # panel was trimmed to the brush size settings.
    show_arrows = True
    show_axis_rings = True
    show_view_ring = True
    show_corner_handles = True
    show_center_handle = True
    show_pivot_buttons = True
    handle_color = (GIZMO_HANDLE_COLOR[0], GIZMO_HANDLE_COLOR[1], GIZMO_HANDLE_COLOR[2], 1.0)
    corner_base_color = (
        GIZMO_HANDLE_COLOR[0], GIZMO_HANDLE_COLOR[1], GIZMO_HANDLE_COLOR[2], 1.0
    )

    def accent(base, kind):
        if hover and hover[1] == axis and hover[0] == kind:
            vivid = _vivid_color(base)
            return (vivid[0], vivid[1], vivid[2], 1.0)
        return (base[0], base[1], base[2], 1.0)

    for axis in range(3):
        try:
            color = _axis_color(context, axis)
            axis_dir = axes[axis]

            if show_arrows:
                tip_screen = _to_screen(context, origin + axis_dir * length)
                base_screen = _to_screen(context, origin + axis_dir * length * GIZMO_MOVE_TRI_BASE)
                mid_screen = _to_screen(context, origin + axis_dir * length * GIZMO_SCALE_POS)

                if tip_screen:
                    _draw_line_2d(shader, (origin_screen.x, origin_screen.y), (tip_screen.x, tip_screen.y), accent(color, "move"))

                if tip_screen and base_screen:
                    _draw_tri_along_axis(
                        shader,
                        (tip_screen.x, tip_screen.y),
                        (base_screen.x, base_screen.y),
                        accent(color, "move"),
                        _px(context, GIZMO_MOVE_TRI_HALF_WID),
                    )

                if mid_screen and tip_screen:
                    _draw_rect_along_axis(
                        shader,
                        (mid_screen.x, mid_screen.y),
                        (tip_screen.x - origin_screen.x, tip_screen.y - origin_screen.y),
                        accent(color, "scale"),
                        _px(context, GIZMO_SCALE_RECT_LEN),
                        _px(context, GIZMO_SCALE_RECT_WID),
                    )

            if show_axis_rings:
                radius = length * GIZMO_RING_RADIUS
                other1 = axes[(axis + 1) % 3]
                other2 = axes[(axis + 2) % 3]
                view_dir = region_3d.view_rotation @ mathutils.Vector((0, 0, -1))
                axis_ring_color = accent(color, "rotate")
                axis_ring_color = (
                    axis_ring_color[0], axis_ring_color[1], axis_ring_color[2], 1.0
                )
                _draw_tube_band_2d(
                    shader,
                    context,
                    origin,
                    other1,
                    other2,
                    radius,
                    length * (_px(context, GIZMO_RING_TUBE_W) / axis_pixels),
                    length * (_px(context, GIZMO_RING_TUBE_H) / axis_pixels),
                    view_dir,
                    axis_ring_color,
                )
        except Exception as exc:
            _draw_error(context, "gizmo axis", exc)

    # outer view-rotation ring (circle facing the camera) and the corner handles
    if show_view_ring or show_corner_handles:
        try:
            view_right = region_3d.view_rotation @ mathutils.Vector((1, 0, 0))
            view_up = region_3d.view_rotation @ mathutils.Vector((0, 1, 0))
            outer_radius = length * GIZMO_VIEW_RING_RADIUS
            band_world = length * (_px(context, GIZMO_RING_BAND_PX) / axis_pixels)

            if show_view_ring:
                inner_points = []
                outer_points = []
                for t in range(GIZMO_RING_SEGMENTS + 1):
                    ang = 2.0 * math.pi * t / GIZMO_RING_SEGMENTS
                    dir_vec = view_right * math.cos(ang) + view_up * math.sin(ang)
                    si = _to_screen(context, origin + dir_vec * (outer_radius - band_world))
                    so = _to_screen(context, origin + dir_vec * (outer_radius + band_world))
                    if si and so:
                        inner_points.append((si.x, si.y))
                        outer_points.append((so.x, so.y))
                if len(inner_points) >= 3:
                    ring_hover = bool(hover and hover[0] == "view_rotate")
                    if ring_hover:
                        vivid = _vivid_color(handle_color)
                        outer_color = (vivid[0], vivid[1], vivid[2], 1.0)
                    else:
                        outer_color = handle_color
                    _draw_ring_band_2d(shader, inner_points, outer_points, outer_color)

            if show_corner_handles:
                # four squat triangles farther out: translate in the view plane
                corner_angles = (45, 135, 225, 315)
                corner_radius = length * GIZMO_CORNER_RADIUS
                tri_height = _px(context, GIZMO_CORNER_TRI_HEIGHT)
                tri_half_wid = _px(context, GIZMO_CORNER_TRI_HALF_WID)
                for corner in range(4):
                    rad = math.radians(corner_angles[corner])
                    world = origin + (view_right * math.cos(rad) + view_up * math.sin(rad)) * corner_radius
                    screen = _to_screen(context, world)
                    if not screen:
                        continue
                    center = (screen.x, screen.y)
                    rx = screen.x - origin_screen.x
                    ry = screen.y - origin_screen.y
                    rnorm = math.hypot(rx, ry) or 1.0
                    dir_x, dir_y = rx / rnorm, ry / rnorm
                    perp_x, perp_y = -dir_y, dir_x
                    corner_hover = bool(
                        hover and hover[0] == "view_move" and hover[1] == corner
                    )
                    if corner_hover:
                        vivid = _vivid_color(corner_base_color)
                        corner_color = (vivid[0], vivid[1], vivid[2], 1.0)
                    else:
                        corner_color = corner_base_color
                    apex = (center[0] + dir_x * tri_height, center[1] + dir_y * tri_height)
                    left = (center[0] + perp_x * tri_half_wid, center[1] + perp_y * tri_half_wid)
                    right = (center[0] - perp_x * tri_half_wid, center[1] - perp_y * tri_half_wid)
                    _draw_tri_2d(shader, apex, left, right, corner_color)
        except Exception as exc:
            _draw_error(context, "gizmo view ring", exc)

    # redraw the three translation arrows last so the view ring never covers them
    if show_arrows:
        for axis in range(3):
            try:
                color = _axis_color(context, axis)
                axis_dir = axes[axis]
                tip_screen = _to_screen(context, origin + axis_dir * length)
                base_screen = _to_screen(context, origin + axis_dir * length * GIZMO_MOVE_TRI_BASE)
                if tip_screen and base_screen:
                    arrow_color = accent(color, "move")
                    _draw_line_2d(shader, (origin_screen.x, origin_screen.y), (tip_screen.x, tip_screen.y), arrow_color)
                    _draw_tri_along_axis(
                        shader,
                        (tip_screen.x, tip_screen.y),
                        (base_screen.x, base_screen.y),
                        arrow_color,
                        _px(context, GIZMO_MOVE_TRI_HALF_WID),
                    )
            except Exception as exc:
                _draw_error(context, "gizmo arrows", exc)

    # center square: uniform scale (opaque)
    if show_center_handle:
        try:
            center_base = _center_scale_color(context)
            if hover and hover[0] == "scale_all":
                vivid = _vivid_color(center_base)
                center_color = (vivid[0], vivid[1], vivid[2], 1.0)
            else:
                center_color = (center_base[0], center_base[1], center_base[2], 1.0)
            half = _px(context, GIZMO_CENTER_SQUARE_HALF)
            cx, cy = origin_screen.x, origin_screen.y
            verts = [
                (cx - half, cy - half), (cx + half, cy - half), (cx + half, cy + half),
                (cx - half, cy - half), (cx + half, cy + half), (cx - half, cy + half),
            ]
            batch = batch_for_shader(shader, "TRIS", {"pos": verts})
            shader.bind()
            shader.uniform_float("color", center_color)
            batch.draw(shader)
        except Exception as exc:
            _draw_error(context, "gizmo center handle", exc)

    # three gizmo buttons above the pivot
    if show_pivot_buttons:
        try:
            btn_size = _button_size(context)
            btn_color = _button_color(context)
            button_positions = _gizmo_button_positions(context, origin_screen)
            hovered_button = None
            for btn in range(3):
                bx, by = button_positions[btn]
                btn_hover = hover is not None and hover[0] == "gizmo_button" and hover[1] == btn
                if btn_hover:
                    hovered_button = (btn, bx, by)
                _draw_button_icon(shader, bx, by, btn, btn_hover, btn_size, btn_color)
            if hovered_button is not None:
                btn, bx, by = hovered_button
                _draw_gizmo_button_tooltip(shader, context, btn, bx, by, btn_size)
        except Exception as exc:
            _draw_error(context, "gizmo pivot buttons", exc)

    gpu.state.line_width_set(1.0)
    gpu.state.blend_set("NONE")


def draw_zb_overlays():
    """The single POST_PIXEL handler for the floating panel and move gizmo."""
    draw_floating_tool_panel()
    draw_move_mode_gizmo()


def register_view3d_draw_handler():
    global VIEW3D_DRAW_HANDLER
    if VIEW3D_DRAW_HANDLER is None:
        VIEW3D_DRAW_HANDLER = bpy.types.SpaceView3D.draw_handler_add(
            draw_zb_overlays,
            (),
            "WINDOW",
            "POST_PIXEL",
        )



def unregister_view3d_draw_handler():
    global VIEW3D_DRAW_HANDLER
    if VIEW3D_DRAW_HANDLER is not None:
        try:
            bpy.types.SpaceView3D.draw_handler_remove(VIEW3D_DRAW_HANDLER, "WINDOW")
        except (ReferenceError, RuntimeError, ValueError):
            pass
        VIEW3D_DRAW_HANDLER = None


def draw_view3d_header_buttons(self, context):
    layout = self.layout
    row = layout.row(align=True)
    row.separator()
    row.enabled = True
    row.operator(
        ZBNAV_OT_set_navigation_mode.bl_idname,
        text="ZBrush",
        depress=is_navigation_enabled(context),
    )


def register_view3d_header_buttons():
    header = bpy.types.VIEW3D_HT_header
    if getattr(header, HEADER_REGISTERED_PROP, False):
        return
    header.append(draw_view3d_header_buttons)
    setattr(header, HEADER_REGISTERED_PROP, True)


def unregister_view3d_header_buttons():
    header = bpy.types.VIEW3D_HT_header
    try:
        header.remove(draw_view3d_header_buttons)
    except (ReferenceError, RuntimeError, ValueError):
        pass
    setattr(header, HEADER_REGISTERED_PROP, False)


CLASSES = (
    ZBNAV_AddonPreferences,
    ZBNAV_OT_pan_or_zoom,
    ZBNAV_OT_alt_select_target,
    ZBNAV_OT_brush_size_drag,
    ZBNAV_OT_set_navigation_mode,
    ZBNAV_OT_tool_shortcut,
    ZBNAV_OT_gizmo_hover_monitor,
    ZBNAV_OT_move_mode_drag,
)


def register():
    FLOATING_PANEL_STATES.clear()
    ISOLATION_STATES.clear()
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.utils.register_tool(ZBNAV_MOVE_TOOL, separator=True)
    register_view3d_header_buttons()
    register_view3d_draw_handler()
    add_zbrush_keymaps()
    if not bpy.app.handlers.depsgraph_update_post.count(_sculpt_mode_handler):
        bpy.app.handlers.depsgraph_update_post.append(_sculpt_mode_handler)
    _start_capslock_watch()
    tag_all_view3d_areas_for_redraw()


def unregister():
    global GIZMO_HOVER_MONITOR_RUNNING
    GIZMO_HOVER_MONITOR_RUNNING = False
    _restore_all_isolation()
    FLOATING_PANEL_STATES.clear()
    _restore_rotate_method(bpy.context)
    if bpy.app.handlers.depsgraph_update_post.count(_sculpt_mode_handler):
        bpy.app.handlers.depsgraph_update_post.remove(_sculpt_mode_handler)
    try:
        if bpy.app.timers.is_registered(_trigger_key_sync_timer):
            bpy.app.timers.unregister(_trigger_key_sync_timer)
        if bpy.app.timers.is_registered(_capslock_watch_timer):
            bpy.app.timers.unregister(_capslock_watch_timer)
    except (AttributeError, ValueError):
        pass
    remove_zbrush_keymaps()
    _restore_trigger_key()
    _restore_mode_shortcuts()
    restore_sculpt_brush_modifiers()
    unregister_view3d_draw_handler()
    if hasattr(bpy.context, "window_manager"):
        bpy.context.window_manager.pop(NAV_MODE_PROP, None)
    unregister_view3d_header_buttons()
    tag_all_view3d_areas_for_redraw()
    try:
        bpy.utils.unregister_tool(ZBNAV_MOVE_TOOL)
    except Exception:
        pass
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
