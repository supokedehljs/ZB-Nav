bl_info = {
    "name": "ZB-Nav",
    "author": "supokede, Cursor",
    "version": (1, 14, 0),
    "blender": (4, 0, 0),
    "location": "3D View Header > ZBrush",
    "description": "在 Blender 雕刻模式中启用 ZBrush 风格的视图导航子模式",
    "category": "3D View",
}

import math

import blf
import bpy
import gpu
import mathutils
from bpy.props import EnumProperty, FloatProperty, PointerProperty
from bpy_extras import view3d_utils
from gpu_extras.batch import batch_for_shader

ADDON_KEYMAPS = []
SCULPT_BRUSH_MODIFIERS = []
SCULPT_ALT_LEFT_CONFLICTS = []
VIEW_ROTATE_SNAP_KEYMAPS = []
SPACE_KEYMAP_CONFLICTS = []
NAV_MODE_PROP = "zb_nav_mode"
HEADER_REGISTERED_PROP = "_zb_nav_view3d_header_registered"
VIEW3D_DRAW_HANDLER = None
BRUSH_SIZE_OVERLAY_HANDLER = None
CTRL_HIT_STATUS_HANDLER = None
CTRL_DIAGNOSTIC_RUNNING = False
BRUSH_SIZE_OVERLAY_ACTIVE = False
CTRL_HIT_STATUS = "等待 Ctrl + 左键"
CTRL_HIT_STATUS_X = 0
CTRL_HIT_STATUS_Y = 0
CTRL_LASSO_POINTS = []
MOVE_MODE_HOVER = None
MAX_BRUSH_SIZE = 5000

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
        "idname": "zb_nav.alt_select_or_invert",
        "type": "MIDDLEMOUSE",
        "value": "PRESS",
        "ctrl": True,
        "shift": True,
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
        "idname": "zb_nav.space_brush_size",
        "type": "SPACE",
        "value": "PRESS",
        "properties": {},
    },
    {
        "keymap": "Sculpt",
        "space_type": "EMPTY",
        "idname": "zb_nav.space_brush_size",
        "type": "SPACE",
        "value": "PRESS",
        "properties": {},
    },
]

def get_preferences(context):
    addon = context.preferences.addons.get(__name__)
    if addon:
        return addon.preferences
    return None


def get_nav_mode(context):
    return context.window_manager.get(NAV_MODE_PROP, "BLENDER")


def get_brush_size_owner(context):
    sculpt = context.tool_settings.sculpt
    brush = sculpt.brush if sculpt else None
    if not brush:
        return None, None

    unified = getattr(sculpt, "unified_paint_settings", None)
    if unified is None:
        unified = getattr(context.tool_settings, "unified_paint_settings", None)
    if unified is not None and getattr(unified, "use_unified_size", False):
        if hasattr(unified, "size"):
            return unified, "size"
    if hasattr(brush, "size"):
        return brush, "size"
    return None, None


def set_nav_mode(context, mode):
    context.window_manager[NAV_MODE_PROP] = mode


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


def find_depth_location(context, region, region_3d, mouse_x, mouse_y):
    if not region or not region_3d:
        return None

    coord = (mouse_x, mouse_y)
    origin = view3d_utils.region_2d_to_origin_3d(region, region_3d, coord)
    direction = view3d_utils.region_2d_to_vector_3d(region, region_3d, coord)
    hit, location, _normal, _index, _object, _matrix = context.scene.ray_cast(
        context.evaluated_depsgraph_get(),
        origin,
        direction,
    )
    if hit:
        return location
    return None


def location_on_depth(region, region_3d, mouse_x, mouse_y, depth_location):
    return view3d_utils.region_2d_to_location_3d(
        region,
        region_3d,
        (mouse_x, mouse_y),
        depth_location,
    )


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
    SCULPT_ALT_LEFT_CONFLICTS.clear()
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
        if keymap_item.ctrl == keymap_item.alt:
            continue

        SCULPT_BRUSH_MODIFIERS.append((keymap_item, keymap_item.ctrl, keymap_item.alt))
        keymap_item.ctrl, keymap_item.alt = keymap_item.alt, keymap_item.ctrl
        if keymap_item.alt and not keymap_item.ctrl:
            SCULPT_ALT_LEFT_CONFLICTS.append(keymap_item)
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
    SCULPT_ALT_LEFT_CONFLICTS.clear()


def suspend_plain_space_keymaps():
    restore_plain_space_keymaps()
    wm = bpy.context.window_manager
    keyconfig = wm.keyconfigs.user
    if not keyconfig:
        return

    for keymap in keyconfig.keymaps:
        for keymap_item in keymap.keymap_items:
            if keymap_item.type != "SPACE" or not keymap_item.active:
                continue
            if keymap_item.ctrl or keymap_item.shift or keymap_item.alt or keymap_item.oskey:
                continue
            if keymap_item.key_modifier != "NONE":
                continue
            try:
                SPACE_KEYMAP_CONFLICTS.append((keymap_item, keymap_item.active))
                keymap_item.active = False
            except (AttributeError, ReferenceError, RuntimeError):
                continue


def restore_plain_space_keymaps():
    saved_keymaps = list(SPACE_KEYMAP_CONFLICTS)
    SPACE_KEYMAP_CONFLICTS.clear()
    for keymap_item, active in saved_keymaps:
        try:
            keymap_item.active = active
        except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
            pass


def remap_view_rotate_axis_snap():
    restore_view_rotate_axis_snap()
    wm = bpy.context.window_manager

    for keyconfig in (wm.keyconfigs.user, wm.keyconfigs.default):
        if not keyconfig:
            continue
        modal_keymap = keyconfig.keymaps.get("View3D Rotate Modal")
        if not modal_keymap:
            continue

        for keymap_item in modal_keymap.keymap_items:
            if not keymap_item.propvalue.startswith("AXIS_SNAP"):
                continue
            original_state = (
                keymap_item,
                keymap_item.type,
                keymap_item.value,
                keymap_item.alt,
                keymap_item.shift,
                keymap_item.ctrl,
            )
            try:
                keymap_item.type = "LEFT_SHIFT"
                keymap_item.alt = False
                keymap_item.shift = False
                keymap_item.ctrl = False
            except (AttributeError, ReferenceError, RuntimeError):
                continue
            VIEW_ROTATE_SNAP_KEYMAPS.append(original_state)


def restore_view_rotate_axis_snap():
    saved_keymaps = list(VIEW_ROTATE_SNAP_KEYMAPS)
    VIEW_ROTATE_SNAP_KEYMAPS.clear()
    for keymap_item, event_type, value, alt, shift, ctrl in saved_keymaps:
        try:
            keymap_item.type = event_type
            keymap_item.value = value
            keymap_item.alt = alt
            keymap_item.shift = shift
            keymap_item.ctrl = ctrl
        except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
            # KeyMapItems can become invalid when Blender rebuilds a keymap.
            pass


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


def sculpt_target_poll(self, obj):
    return is_valid_sculpt_target(bpy.context, obj)


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

    context.window_manager.zb_nav_sculpt_target = target_object
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


def _point_in_polygon(x, y, points):
    inside = False
    n = len(points)
    j = n - 1
    for i in range(n):
        xi, yi = points[i]
        xj, yj = points[j]
        if (yi > y) != (yj > y):
            x_intersect = (xj - xi) * (y - yi) / (yj - yi) + xi
            if x < x_intersect:
                inside = not inside
        j = i
    return inside


def _is_lasso_click(points):
    if not points:
        return True
    x0, y0 = points[0]
    for x, y in points:
        if abs(x - x0) > 6 or abs(y - y0) > 6:
            return False
    return True


def _lasso_covers_object(context, points):
    obj = context.active_object
    if not obj or obj.type != "MESH":
        return False
    region = context.region
    region_3d = context.region_data
    if not region or not region_3d:
        return False

    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)

    deps = context.evaluated_depsgraph_get()
    eval_obj = deps.objects.get(obj.name, obj)
    mesh = eval_obj.data
    verts = mesh.vertices
    count = len(verts)
    if count == 0:
        return False

    stride = max(1, count // 20000)
    matrix = eval_obj.matrix_world
    for i in range(0, count, stride):
        world = matrix @ verts[i].co
        screen = view3d_utils.location_3d_to_region_2d(region, region_3d, world)
        if screen is None:
            continue
        sx, sy = screen.x, screen.y
        if min_x <= sx <= max_x and min_y <= sy <= max_y:
            if _point_in_polygon(sx, sy, points):
                return True
    return False


MOVE_AXIS_COLORS = ((1.0, 0.15, 0.15), (0.15, 1.0, 0.2), (0.2, 0.5, 1.0))

ZBNAV_MOVE_GIZMO_STYLES = (
    ("standard", "标准（箭头 + 方块 + 圆环）", "三轴带移动箭头、缩放方块和旋转圆环"),
    ("arrows", "极简箭头（仅移动）", "简洁的三轴箭头，只用于移动"),
    ("zbrush", "ZBrush 加粗箭头", "粗壮的填充箭头 + 方块 + 圆环"),
    ("rings", "圆环（侧重旋转）", "粗圆环旋转 + 细线移动"),
    ("double", "双头箭头", "双向箭头，两个方向都能拖动移动"),
    ("dots", "圆点轴", "细线轴 + 圆点把手 + 圆环"),
)

GIZMO_STYLE_CONFIG = {
    "standard": {
        "line": 2.5, "arrow": "v", "scale": "box", "rotate": True,
        "double": False, "center": "dot", "ring_radius": 0.7, "scale_pos": 0.6,
    },
    "arrows": {
        "line": 2.0, "arrow": "v", "scale": None, "rotate": False,
        "double": False, "center": None, "ring_radius": 0.7, "scale_pos": 0.6,
    },
    "zbrush": {
        "line": 3.5, "arrow": "tri", "scale": "box", "rotate": True,
        "double": False, "center": "cube", "ring_radius": 0.7, "scale_pos": 0.55,
    },
    "rings": {
        "line": 1.5, "arrow": None, "scale": None, "rotate": True,
        "double": False, "center": None, "ring_radius": 0.95,
        "scale_pos": 0.6, "line_move": True,
    },
    "double": {
        "line": 2.5, "arrow": "v", "scale": "box", "rotate": True,
        "double": True, "center": None, "ring_radius": 0.7, "scale_pos": 0.5,
    },
    "dots": {
        "line": 1.5, "arrow": "dot", "scale": "dot_box", "rotate": True,
        "double": False, "center": "dot", "ring_radius": 0.7, "scale_pos": 0.6,
    },
}


def _move_gizmo_style(context):
    return getattr(
        context.window_manager,
        "zb_nav_move_gizmo_style",
        "standard",
    )


def _gizmo_world_axes(context):
    obj = context.active_object
    if not obj:
        return None, None
    matrix = obj.matrix_world
    origin = matrix.translation.copy()
    axes = []
    for i in range(3):
        unit = mathutils.Vector((1, 0, 0) if i == 0 else (0, 1, 0) if i == 1 else (0, 0, 1))
        axes.append((matrix.to_3x3() @ unit).normalized())
    return origin, axes


def _gizmo_length(context):
    obj = context.active_object
    corners = [obj.matrix_world @ mathutils.Vector(c) for c in obj.bound_box]
    size = max(
        (max(c[i] for c in corners) - min(c[i] for c in corners))
        for i in range(3)
    )
    return max(0.25, min(10.0, size * 0.8))


def _to_screen(context, world_pos):
    region = context.region
    region_3d = context.region_data
    if not region or not region_3d:
        return None
    return view3d_utils.location_3d_to_region_2d(region, region_3d, world_pos)


def _dist_point_to_segment(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    length_sq = dx * dx + dy * dy
    if length_sq == 0.0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length_sq))
    cx, cy = ax + t * dx, ay + t * dy
    return math.hypot(px - cx, py - cy)


def _move_mode_pick(context, mouse_x, mouse_y, style):
    """Return (kind, axis) under the cursor or None."""
    cfg = GIZMO_STYLE_CONFIG.get(style, GIZMO_STYLE_CONFIG["standard"])
    origin, axes = _gizmo_world_axes(context)
    if origin is None:
        return None
    length = _gizmo_length(context)
    candidates = []

    for axis in range(3):
        if cfg.get("line_move"):
            origin_screen = _to_screen(context, origin)
            tip_screen = _to_screen(context, origin + axes[axis] * length)
            if origin_screen and tip_screen:
                d = _dist_point_to_segment(
                    mouse_x, mouse_y,
                    origin_screen.x, origin_screen.y,
                    tip_screen.x, tip_screen.y,
                )
                candidates.append((d, "move", axis))

        if cfg["arrow"]:
            tips = [(1.0, 1.0)]
            if cfg["double"]:
                tips = [(1.0, 1.0), (-1.0, 1.0)]
            for sign, _sc in tips:
                tip = origin + axes[axis] * length * sign
                tip_screen = _to_screen(context, tip)
                if tip_screen:
                    d = math.hypot(mouse_x - tip_screen.x, mouse_y - tip_screen.y)
                    candidates.append((d, "move", axis))

        if cfg["scale"]:
            mid = origin + axes[axis] * length * cfg["scale_pos"]
            mid_screen = _to_screen(context, mid)
            if mid_screen:
                d = math.hypot(mouse_x - mid_screen.x, mouse_y - mid_screen.y)
                candidates.append((d, "scale", axis))

        if cfg["rotate"]:
            radius = length * cfg["ring_radius"]
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
                candidates.append((best, "rotate", axis))

    candidates.sort(key=lambda item: item[0])
    if candidates and candidates[0][0] < 14.0:
        return candidates[0][1], candidates[0][2]
    return None


def _is_move_tool_active(context):
    try:
        from bl_ui.space_toolsystem_common import ToolSelectPanelHelper
        cls = ToolSelectPanelHelper._tool_class_from_space_type("VIEW_3D")
        _item, tool_active, _icon = cls._tool_get_active(context, "VIEW_3D", "SCULPT")
        return tool_active is not None and tool_active.idname == ZBNAV_MOVE_TOOL.bl_idname
    except Exception:
        return False


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
    )

    def draw_settings(context, layout, tool):
        layout.label(text="拖动轴：移动 / 缩放 / 旋转")


class ZBNAV_OT_move_mode_drag(bpy.types.Operator):
    bl_idname = "zb_nav.move_mode_drag"
    bl_label = "Move Mode Drag"
    bl_description = "拖动变换轴控制物体移动/缩放/旋转"
    bl_options = {"REGISTER"}

    _drag_handle = None
    _last_x = 0
    _last_y = 0

    @classmethod
    def poll(cls, context):
        return (
            context.mode == "SCULPT"
            and context.active_object is not None
            and context.area
            and context.area.type == "VIEW_3D"
        )

    def invoke(self, context, event):
        global MOVE_MODE_HOVER
        obj = context.active_object
        if not obj or obj.type != "MESH":
            return {"CANCELLED"}
        handle = _move_mode_pick(
            context, event.mouse_region_x, event.mouse_region_y,
            _move_gizmo_style(context),
        )
        if handle is None:
            MOVE_MODE_HOVER = None
            return {"CANCELLED"}
        self._drag_handle = handle
        self._last_x = event.mouse_region_x
        self._last_y = event.mouse_region_y
        MOVE_MODE_HOVER = handle
        try:
            bpy.ops.ed.undo_push(message="Move Mode")
        except (RuntimeError, TypeError):
            pass
        context.window_manager.modal_handler_add(self)
        if context.area:
            context.area.tag_redraw()
        return {"RUNNING_MODAL"}

    def _finish(self, context):
        global MOVE_MODE_HOVER
        MOVE_MODE_HOVER = None
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
        axis_dir = axes[axis]

        if kind == "move":
            delta = self._world_delta(context, event)
            amount = delta.dot(axis_dir)
            matrix = obj.matrix_world.copy()
            matrix.translation += axis_dir * amount
            obj.matrix_world = matrix
        elif kind == "scale":
            delta = self._world_delta(context, event)
            amount = delta.dot(axis_dir)
            factor = 1.0 + amount / length
            scale = obj.scale.copy()
            scale[axis] = max(0.001, scale[axis] * factor)
            obj.scale = scale
        elif kind == "rotate":
            origin_screen = _to_screen(context, origin)
            if origin_screen:
                prev_angle = math.atan2(
                    self._last_y - origin_screen.y,
                    self._last_x - origin_screen.x,
                )
                cur_angle = math.atan2(
                    event.mouse_region_y - origin_screen.y,
                    event.mouse_region_x - origin_screen.x,
                )
                angle = cur_angle - prev_angle
                rotation = mathutils.Matrix.Rotation(angle, 4, axis_dir)
                obj.matrix_world = rotation @ obj.matrix_world
        self._last_x = event.mouse_region_x
        self._last_y = event.mouse_region_y
        if context.area:
            context.area.tag_redraw()

    def modal(self, context, event):
        global MOVE_MODE_HOVER
        if not context.active_object:
            return self._finish(context)

        if event.type in {"ESC", "RIGHTMOUSE"} and event.value == "PRESS":
            return self._finish(context)

        if event.type == "MOUSEMOVE":
            self._apply(context, event)
            return {"RUNNING_MODAL"}

        if event.type == "LEFTMOUSE" and event.value == "RELEASE":
            return self._finish(context)

        return {"RUNNING_MODAL"}


class ZBNAV_OT_ctrl_diagnostic_monitor(bpy.types.Operator):
    bl_idname = "zb_nav.ctrl_diagnostic_monitor"
    bl_label = "Ctrl Mask Helper"
    bl_options = {"INTERNAL"}

    _lasso_active = False
    _lasso_points = []

    @classmethod
    def poll(cls, context):
        return context.area and context.area.type == "VIEW_3D"

    def invoke(self, context, event):
        global CTRL_DIAGNOSTIC_RUNNING, CTRL_LASSO_POINTS
        CTRL_DIAGNOSTIC_RUNNING = True
        self._lasso_active = False
        self._lasso_points = []
        CTRL_LASSO_POINTS = []
        context.window_manager.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def _finish(self, context):
        global CTRL_DIAGNOSTIC_RUNNING, CTRL_LASSO_POINTS
        CTRL_DIAGNOSTIC_RUNNING = False
        CTRL_LASSO_POINTS = []
        if context.area:
            context.area.tag_redraw()
        return {"CANCELLED"}

    def _handle_lasso_release(self, context, points):
        global CTRL_HIT_STATUS
        try:
            if _is_lasso_click(points):
                bpy.ops.paint.mask_flood_fill(mode="VALUE", value=1.0)
                CTRL_HIT_STATUS = "空白点击：已填充全部遮罩"
            elif not _lasso_covers_object(context, points):
                bpy.ops.paint.mask_flood_fill(mode="VALUE", value=0.0)
                CTRL_HIT_STATUS = "空白套索未遮罩到物体：已清除全部遮罩"
            else:
                bpy.ops.paint.mask_lasso_gesture(
                    path=[
                        {
                            "name": str(index),
                            "loc": (px, py),
                            "time": float(index),
                        }
                        for index, (px, py) in enumerate(points)
                    ],
                    value=1.0,
                )
                CTRL_HIT_STATUS = "空白套索：已对物体应用遮罩"
        except Exception as exc:
            CTRL_HIT_STATUS = f"遮罩操作失败: {exc}"
        if context.area:
            context.area.tag_redraw()

    def modal(self, context, event):
        global CTRL_HIT_STATUS, CTRL_HIT_STATUS_X, CTRL_HIT_STATUS_Y
        global CTRL_LASSO_POINTS
        if not is_zbrush_sculpt_mode(context):
            return self._finish(context)
        if _is_move_tool_active(context):
            self._lasso_active = False
            self._lasso_points = []
            CTRL_LASSO_POINTS = []
            return {"PASS_THROUGH"}

        if event.type == "ESC" and event.value == "PRESS":
            self._lasso_active = False
            self._lasso_points = []
            CTRL_LASSO_POINTS = []
            CTRL_HIT_STATUS = "套索已取消"
            if context.area:
                context.area.tag_redraw()
            return {"RUNNING_MODAL"}

        if event.ctrl and event.type in {"MOUSEMOVE", "LEFTMOUSE"}:
            CTRL_HIT_STATUS_X = event.mouse_region_x
            CTRL_HIT_STATUS_Y = event.mouse_region_y
            hit_object = find_object_under_mouse(
                context, event.mouse_region_x, event.mouse_region_y
            )
            CTRL_HIT_STATUS = (
                f"Ctrl 命中模型: {hit_object.name}"
                if hit_object is not None
                else "Ctrl 命中空白区域"
            )
            if context.area:
                context.area.tag_redraw()

        if self._lasso_active:
            if event.type == "MOUSEMOVE":
                self._lasso_points.append((event.mouse_region_x, event.mouse_region_y))
                CTRL_LASSO_POINTS = list(self._lasso_points)
                if context.area:
                    context.area.tag_redraw()
                return {"RUNNING_MODAL"}
            if event.type == "LEFTMOUSE" and event.value == "RELEASE":
                points = list(self._lasso_points)
                self._lasso_active = False
                self._lasso_points = []
                CTRL_LASSO_POINTS = []
                self._handle_lasso_release(context, points)
                return {"RUNNING_MODAL"}

        if (
            event.ctrl
            and event.type == "LEFTMOUSE"
            and event.value == "PRESS"
        ):
            hit_object = find_object_under_mouse(
                context, event.mouse_region_x, event.mouse_region_y
            )
            if hit_object is None:
                self._lasso_active = True
                self._lasso_points = [(event.mouse_region_x, event.mouse_region_y)]
                CTRL_LASSO_POINTS = list(self._lasso_points)
                CTRL_HIT_STATUS = "空白命中：拖动画套索，或直接点击填充全部遮罩"
                if context.area:
                    context.area.tag_redraw()
                return {"RUNNING_MODAL"}

        # Ctrl + 左键点击模型表面交给 Blender 原生遮罩笔刷。
        return {"PASS_THROUGH"}



def add_zbrush_keymaps():
    remove_zbrush_keymaps()
    swap_sculpt_brush_modifiers()
    remap_view_rotate_axis_snap()
    suspend_plain_space_keymaps()

    for item in ZBRUSH_KEYMAP_ITEMS:
        _add_keymap_item(item)


def update_navigation_mode(context, mode):
    global BRUSH_SIZE_OVERLAY_ACTIVE
    if mode == "ZBRUSH":
        if context.mode != "SCULPT":
            return False
        add_zbrush_keymaps()
        if not CTRL_DIAGNOSTIC_RUNNING:
            try:
                bpy.ops.zb_nav.ctrl_diagnostic_monitor("INVOKE_DEFAULT")
            except (RuntimeError, TypeError):
                pass
    else:
        BRUSH_SIZE_OVERLAY_ACTIVE = False
        remove_zbrush_keymaps()
        restore_sculpt_brush_modifiers()
        restore_view_rotate_axis_snap()
        restore_plain_space_keymaps()
    set_nav_mode(context, mode)
    tag_all_view3d_areas_for_redraw()
    return True


_AUTO_ZBRUSH_LAST_SCULPT = None


def _auto_zbrush_handler(scene, depsgraph):
    """每次进入雕刻模式时自动开启 ZBrush 子模式。"""
    global _AUTO_ZBRUSH_LAST_SCULPT
    context = bpy.context
    is_sculpt = context.mode == "SCULPT"
    if is_sculpt == _AUTO_ZBRUSH_LAST_SCULPT:
        return
    _AUTO_ZBRUSH_LAST_SCULPT = is_sculpt
    if (
        is_sculpt
        and context.active_object
        and context.active_object.type == "MESH"
        and get_nav_mode(context) != "ZBRUSH"
    ):
        try:
            update_navigation_mode(context, "ZBRUSH")
        except Exception:
            pass


class ZBNAV_AddonPreferences(bpy.types.AddonPreferences):
    bl_idname = __name__

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

    brush_size_sensitivity: FloatProperty(
        name="Brush Size Sensitivity",
        description="空格拖拽调整笔刷大小时的灵敏度倍数（越大拖得越快）",
        default=2.0,
        min=0.1,
        max=10.0,
        soft_min=0.5,
        soft_max=5.0,
        step=10,
    )

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "pan_sensitivity")
        layout.prop(self, "zoom_sensitivity")
        layout.prop(self, "brush_size_sensitivity")


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
            and is_zbrush_sculpt_mode(context)
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
        depth_location = self._last_depth_location or self._region_3d.view_location
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
        new_depth = find_depth_location(context, self._region, self._region_3d, current_x, current_y)
        self._last_depth_location = new_depth or depth_location
        self._pivot_location = self._last_depth_location

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
        focus_location = find_depth_location(context, self._region, self._region_3d, current_x, current_y)
        if focus_location is None:
            focus_location = self._pivot_location or self._last_depth_location or self._start_view_location

        prefs = get_preferences(context)
        sensitivity = prefs.zoom_sensitivity if prefs else 1.0
        previous_distance = max(self._region_3d.view_distance, 0.0001)
        # Blender's region Y coordinate increases upward; invert it so screen-down
        # contributes in the same direction as screen-right, matching ZBrush.
        delta = dx - dy
        zoom_factor = math.exp(-delta * 0.01 * sensitivity)
        new_distance = max(previous_distance * zoom_factor, 0.0001)
        ratio = new_distance / previous_distance

        self._region_3d.view_location = focus_location + (self._region_3d.view_location - focus_location) * ratio
        self._region_3d.view_distance = new_distance
        self._pivot_location = focus_location
        self._last_depth_location = focus_location


class ZBNAV_OT_switch_sculpt_target(bpy.types.Operator):
    bl_idname = "zb_nav.switch_sculpt_target"
    bl_label = "切换到目标雕刻"
    bl_description = "无需手动退出雕刻模式，直接切换到选择框中的网格对象"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        target = getattr(context.window_manager, "zb_nav_sculpt_target", None)
        return is_zbrush_sculpt_mode(context) and is_valid_sculpt_target(context, target)

    def execute(self, context):
        target = context.window_manager.zb_nav_sculpt_target
        success, message = switch_sculpt_target(context, target)
        if not success:
            self.report({"WARNING"}, message)
            return {"CANCELLED"}
        self.report({"INFO"}, message)
        return {"FINISHED"}


class ZBNAV_PT_sculpt_target(bpy.types.Panel):
    bl_label = "ZB-Nav"
    bl_idname = "ZBNAV_PT_sculpt_target"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "ZB-Nav"

    @classmethod
    def poll(cls, context):
        return context.mode == "SCULPT"

    def draw(self, context):
        layout = self.layout
        wm = context.window_manager

        move_box = layout.box()
        move_box.label(text="移动模式（选择左侧移动工具开启）")
        move_box.prop(wm, "zb_nav_move_gizmo_style", text="轴样式")

        layout.separator()
        box = layout.box()
        box.label(text="Alt + 鼠标中键  平移 / 缩放视图", icon="MOUSE_MOVE")
        box.label(text="Ctrl + 左键：模型笔刷 / 空白套索", icon="BRUSH_MASK")
        box.label(text="Ctrl 点击空白 = 填充全部遮罩", icon="RESTRICT_SELECT_OFF")
        box.label(text="Ctrl 空白套索未遮到物体 = 清除遮罩", icon="BRUSH_DATA")
        box.label(text="Ctrl + 鼠标中键  遮罩修正", icon="BRUSH_MASK")
        box.label(text="Alt + 鼠标左键  切换雕刻目标", icon="OBJECT_DATA")
        box.label(text="空格 + 鼠标左键  调整笔刷大小", icon="BRUSH_DATA")


class ZBNAV_BrushSizeMixin:
    _last_mouse_x = 0
    _start_mouse_x = 0
    _start_mouse_y = 0
    _left_mouse_down = False
    _moved = False
    _adjusted = False
    _size_accumulator = 0.0

    def _apply_brush_size(self, context, event):
        brush_owner, size_property = get_brush_size_owner(context)
        if brush_owner is None:
            return True

        raw_delta = event.mouse_x - self._last_mouse_x
        self._last_mouse_x = event.mouse_x

        prefs = get_preferences(context)
        sensitivity = prefs.brush_size_sensitivity if prefs else 2.0
        self._size_accumulator += raw_delta * sensitivity
        step = int(self._size_accumulator)
        if step == 0:
            return True
        self._size_accumulator -= step

        current_size = int(getattr(brush_owner, size_property))
        new_size = max(1, min(current_size + step, MAX_BRUSH_SIZE))
        try:
            setattr(brush_owner, size_property, new_size)
        except (TypeError, ValueError):
            return False
        if context.area:
            context.area.tag_redraw()
        return True


class ZBNAV_OT_space_brush_size(ZBNAV_BrushSizeMixin, bpy.types.Operator):
    bl_idname = "zb_nav.space_brush_size"
    bl_label = "Adjust Sculpt Brush Size"
    bl_description = "点按空格进入，然后按住左键水平拖动调整雕刻笔刷大小（类似 F）"
    bl_options = {"INTERNAL", "BLOCKING"}

    @classmethod
    def poll(cls, context):
        return is_zbrush_sculpt_mode(context)

    def invoke(self, context, event):
        global BRUSH_SIZE_OVERLAY_ACTIVE
        self._last_mouse_x = event.mouse_x
        self._start_mouse_x = event.mouse_x
        self._start_mouse_y = event.mouse_y
        self._left_mouse_down = False
        self._moved = False
        self._adjusted = False
        self._size_accumulator = 0.0
        self._space_released = False
        BRUSH_SIZE_OVERLAY_ACTIVE = True
        if context.area:
            context.area.tag_redraw()
        context.window_manager.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def _finish(self, context, cancel=False):
        global BRUSH_SIZE_OVERLAY_ACTIVE
        BRUSH_SIZE_OVERLAY_ACTIVE = False
        if context.area:
            context.area.tag_redraw()
        return {"CANCELLED"} if cancel else {"FINISHED"}

    def modal(self, context, event):
        if not is_zbrush_sculpt_mode(context):
            return self._finish(context, cancel=True)

        if event.type == "ESC" and event.value == "PRESS":
            return self._finish(context)

        if event.type == "SPACE":
            if event.value == "RELEASE":
                if self._adjusted:
                    return self._finish(context)
                self._space_released = True
            elif event.value == "PRESS" and self._space_released and not self._left_mouse_down:
                return self._finish(context)
            return {"RUNNING_MODAL"}

        if event.type == "LEFTMOUSE":
            if event.value == "PRESS":
                self._left_mouse_down = True
                self._start_mouse_x = event.mouse_x
                self._start_mouse_y = event.mouse_y
                self._moved = False
                self._last_mouse_x = event.mouse_x
                self._size_accumulator = 0.0
                return {"RUNNING_MODAL"}
            if event.value == "RELEASE":
                was_down = self._left_mouse_down
                self._left_mouse_down = False
                if was_down and not self._moved and self._adjusted:
                    return self._finish(context)
                self._last_mouse_x = event.mouse_x
                return {"RUNNING_MODAL"}

        if event.type == "MOUSEMOVE":
            if self._left_mouse_down:
                if (
                    abs(event.mouse_x - self._start_mouse_x)
                    + abs(event.mouse_y - self._start_mouse_y)
                ) > 3:
                    self._moved = True
                if self._moved:
                    if not self._apply_brush_size(context, event):
                        self.report({"WARNING"}, "当前笔刷大小属性不支持调整")
                        return self._finish(context, cancel=True)
                    self._adjusted = True
                self._last_mouse_x = event.mouse_x
            return {"RUNNING_MODAL"}

        return {"RUNNING_MODAL"}


class ZBNAV_OT_alt_select_target(bpy.types.Operator):
    bl_idname = "zb_nav.alt_select_target"
    bl_label = "Switch Sculpt Target"
    bl_description = "Alt + 左键单击切换到目标模型的雕刻模式"
    bl_options = {"INTERNAL"}

    @classmethod
    def poll(cls, context):
        return is_zbrush_sculpt_mode(context)

    def invoke(self, context, event):
        if event.alt and event.type == "LEFTMOUSE" and event.value == "PRESS":
            return select_or_invert_sculpt_target(context, event)
        return {"PASS_THROUGH"}


class ZBNAV_OT_alt_select_or_invert(bpy.types.Operator):
    bl_idname = "zb_nav.alt_select_or_invert"
    bl_label = "ZBrush Sculpt Target Switch"
    bl_description = "ZBrush 模式下 Alt + 鼠标中键点击切换模型，拖动平移视图"
    bl_options = {"REGISTER", "BLOCKING", "GRAB_CURSOR"}

    _start_mouse_x = 0
    _start_mouse_y = 0
    _last_mouse_x = 0
    _last_mouse_y = 0
    _moved = False

    @classmethod
    def poll(cls, context):
        return is_zbrush_sculpt_mode(context)

    def invoke(self, context, event):
        if not (event.ctrl and event.shift and event.type == "MIDDLEMOUSE"):
            return {"PASS_THROUGH"}
        self._start_mouse_x = event.mouse_region_x
        self._start_mouse_y = event.mouse_region_y
        self._last_mouse_x = event.mouse_region_x
        self._last_mouse_y = event.mouse_region_y
        self._moved = False
        context.window_manager.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        if event.type == "MOUSEMOVE":
            current_x = event.mouse_region_x
            current_y = event.mouse_region_y
            if abs(current_x - self._start_mouse_x) > 3 or abs(current_y - self._start_mouse_y) > 3:
                self._moved = True
            if self._moved:
                self._pan_view(context, current_x, current_y)
            self._last_mouse_x = current_x
            self._last_mouse_y = current_y
            context.area.tag_redraw()
            return {"RUNNING_MODAL"}

        if event.type in {"MIDDLEMOUSE", "ESC"} and event.value == "RELEASE":
            if not self._moved and event.type == "MIDDLEMOUSE":
                select_or_invert_sculpt_target(context, event)
            return {"FINISHED"}

        return {"RUNNING_MODAL"}

    def _pan_view(self, context, current_x, current_y):
        region = context.region
        region_3d = context.region_data
        previous = location_on_depth(
            region,
            region_3d,
            self._last_mouse_x,
            self._last_mouse_y,
            region_3d.view_location,
        )
        current = location_on_depth(
            region,
            region_3d,
            current_x,
            current_y,
            region_3d.view_location,
        )
        region_3d.view_location += previous - current


class ZBNAV_OT_set_navigation_mode(bpy.types.Operator):
    bl_idname = "zb_nav.set_navigation_mode"
    bl_label = "Toggle ZBrush Sculpt Mode"
    bl_description = "在当前雕刻模式中启用或退出 ZBrush 导航子模式"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context):
        return context.mode == "SCULPT"

    def execute(self, context):
        next_mode = "BLENDER" if get_nav_mode(context) == "ZBRUSH" else "ZBRUSH"
        if not update_navigation_mode(context, next_mode):
            self.report({"WARNING"}, "ZBrush 子模式只能在雕刻模式中启用")
            return {"CANCELLED"}
        return {"FINISHED"}


def draw_zbrush_mode_border():
    context = bpy.context
    draw_move_mode_gizmo()
    if is_zbrush_sculpt_mode(context):
        draw_ctrl_hit_status()


def _draw_line_2d(shader, a, b, color):
    batch = batch_for_shader(shader, "LINES", {"pos": [a, b]})
    shader.bind()
    shader.uniform_float("color", color)
    batch.draw(shader)


def _draw_polyline_2d(shader, points, color):
    if len(points) >= 2:
        batch = batch_for_shader(shader, "LINE_STRIP", {"pos": points})
        shader.bind()
        shader.uniform_float("color", color)
        batch.draw(shader)


def _draw_tri_2d(shader, a, b, c, color):
    batch = batch_for_shader(shader, "TRIS", {"pos": [a, b, c]})
    shader.bind()
    shader.uniform_float("color", color)
    batch.draw(shader)


def _draw_filled_circle_2d(shader, center, radius, color, segments=12):
    cx, cy = center
    verts = []
    for i in range(segments):
        a1 = 2.0 * math.pi * i / segments
        a2 = 2.0 * math.pi * (i + 1) / segments
        verts.append((cx, cy))
        verts.append((cx + math.cos(a1) * radius, cy + math.sin(a1) * radius))
        verts.append((cx + math.cos(a2) * radius, cy + math.sin(a2) * radius))
    _draw_tri_2d(shader, verts[0], verts[1], verts[2], color)
    batch = batch_for_shader(shader, "TRIS", {"pos": verts})
    shader.bind()
    shader.uniform_float("color", color)
    batch.draw(shader)


def _draw_box_2d(shader, center, color, filled=False):
    cx, cy = center
    half = 5.0
    if filled:
        verts = [
            (cx - half, cy - half), (cx + half, cy - half), (cx + half, cy + half),
            (cx - half, cy - half), (cx + half, cy + half), (cx - half, cy + half),
        ]
        batch = batch_for_shader(shader, "TRIS", {"pos": verts})
        shader.bind()
        shader.uniform_float("color", color)
        batch.draw(shader)
    else:
        verts = [
            (cx - half, cy - half), (cx + half, cy - half),
            (cx + half, cy - half), (cx + half, cy + half),
            (cx + half, cy + half), (cx - half, cy + half),
            (cx - half, cy + half), (cx - half, cy - half),
        ]
        batch = batch_for_shader(shader, "LINES", {"pos": verts})
        shader.bind()
        shader.uniform_float("color", color)
        batch.draw(shader)


def _draw_arrow_2d(shader, tip, back_dir, color, kind):
    dx, dy = back_dir
    norm = math.hypot(dx, dy) or 1.0
    ux, uy = dx / norm, dy / norm
    px, py = -uy, ux
    if kind == "v":
        size = 12
        _draw_polyline_2d(shader, [
            (tip[0], tip[1]),
            (tip[0] - ux * size + px * 7, tip[1] - uy * size + py * 7),
            (tip[0] - ux * size - px * 7, tip[1] - uy * size - py * 7),
            (tip[0], tip[1]),
        ], color)
    elif kind == "tri":
        size = 14
        _draw_tri_2d(shader,
            (tip[0], tip[1]),
            (tip[0] - ux * size + px * 8, tip[1] - uy * size + py * 8),
            (tip[0] - ux * size - px * 8, tip[1] - uy * size - py * 8),
            color,
        )
    elif kind == "dot":
        _draw_filled_circle_2d(shader, tip, 6.0, color)


def draw_move_mode_gizmo():
    context = bpy.context
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
    style = _move_gizmo_style(context)
    cfg = GIZMO_STYLE_CONFIG.get(style, GIZMO_STYLE_CONFIG["standard"])

    shader = gpu.shader.from_builtin("UNIFORM_COLOR")
    gpu.state.blend_set("ALPHA")
    gpu.state.line_width_set(cfg["line"])

    origin_screen = _to_screen(context, origin)
    if not origin_screen:
        return

    hover = MOVE_MODE_HOVER

    def accent(base, kind):
        if hover and hover[1] == axis and hover[0] == kind:
            return (1.0, 1.0, 1.0, 1.0)
        return (base[0], base[1], base[2], 1.0)

    for axis in range(3):
        color = MOVE_AXIS_COLORS[axis]
        axis_dir = axes[axis]

        tip_screen = _to_screen(context, origin + axis_dir * length)
        neg_screen = _to_screen(context, origin - axis_dir * length)

        if cfg["double"]:
            if neg_screen and tip_screen:
                _draw_line_2d(shader, (neg_screen.x, neg_screen.y), (tip_screen.x, tip_screen.y), accent(color, "move"))
        elif tip_screen:
            _draw_line_2d(shader, (origin_screen.x, origin_screen.y), (tip_screen.x, tip_screen.y), accent(color, "move"))

        if cfg["arrow"]:
            if tip_screen:
                _draw_arrow_2d(
                    shader,
                    (tip_screen.x, tip_screen.y),
                    (tip_screen.x - origin_screen.x, tip_screen.y - origin_screen.y),
                    accent(color, "move"),
                    cfg["arrow"],
                )
            if cfg["double"] and neg_screen:
                _draw_arrow_2d(
                    shader,
                    (neg_screen.x, neg_screen.y),
                    (neg_screen.x - origin_screen.x, neg_screen.y - origin_screen.y),
                    accent(color, "move"),
                    cfg["arrow"],
                )

        if cfg["scale"]:
            mid = _to_screen(context, origin + axis_dir * length * cfg["scale_pos"])
            if mid:
                _draw_box_2d(
                    shader,
                    (mid.x, mid.y),
                    accent(color, "scale"),
                    filled=(cfg["scale"] == "dot_box"),
                )

        if cfg["rotate"]:
            radius = length * cfg["ring_radius"]
            other1 = axes[(axis + 1) % 3]
            other2 = axes[(axis + 2) % 3]
            ring_points = []
            for t in range(33):
                ang = 2.0 * math.pi * t / 32
                world = origin + (other1 * math.cos(ang) + other2 * math.sin(ang)) * radius
                screen = _to_screen(context, world)
                if screen:
                    ring_points.append((screen.x, screen.y))
            if len(ring_points) >= 3:
                ring_color = accent(color, "rotate")
                ring_color = (ring_color[0], ring_color[1], ring_color[2], 0.6)
                if hover and hover[1] == axis and hover[0] == "rotate":
                    ring_color = (1.0, 1.0, 1.0, 1.0)
                _draw_polyline_2d(shader, ring_points, ring_color)

        if cfg["center"]:
            radius = 4.0 if cfg["center"] == "dot" else 6.0
            _draw_filled_circle_2d(shader, (origin_screen.x, origin_screen.y), radius, (1.0, 1.0, 1.0, 0.9))

    gpu.state.line_width_set(1.0)
    gpu.state.blend_set("NONE")


def draw_ctrl_hit_status():
    if not is_zbrush_sculpt_mode(bpy.context):
        return
    context = bpy.context
    region = context.region
    if not region or region.type != "WINDOW":
        return

    if CTRL_LASSO_POINTS:
        shader = gpu.shader.from_builtin("UNIFORM_COLOR")
        points = [(float(px), float(py)) for px, py in CTRL_LASSO_POINTS]
        if len(points) >= 2:
            vertices = points + [points[0]]
            batch = batch_for_shader(shader, "LINE_STRIP", {"pos": vertices})
            gpu.state.blend_set("ALPHA")
            gpu.state.line_width_set(2.0)
            shader.bind()
            shader.uniform_float("color", (1.0, 1.0, 0.0, 0.9))
            batch.draw(shader)
            gpu.state.line_width_set(1.0)
            gpu.state.blend_set("NONE")

    x = min(max(float(CTRL_HIT_STATUS_X) + 18.0, 8.0), max(region.width - 260.0, 8.0))
    y = min(max(float(CTRL_HIT_STATUS_Y) + 18.0, 24.0), max(region.height - 24.0, 24.0))
    font_id = 0
    blf.size(font_id, 15)
    blf.color(font_id, 0.2, 1.0, 0.35, 1.0)
    blf.position(font_id, 24, region.height - 70, 0)
    blf.draw(font_id, "ZB-Nav Ctrl 遮罩辅助已启动")
    blf.color(font_id, 1.0, 0.8, 0.1, 1.0)
    blf.position(font_id, x, y, 0)
    blf.draw(font_id, CTRL_HIT_STATUS)


def draw_brush_size_overlay():
    if not BRUSH_SIZE_OVERLAY_ACTIVE:
        return
    context = bpy.context
    if not is_zbrush_sculpt_mode(context):
        return

    region = context.region
    if not region or region.type != "WINDOW":
        return

    brush_owner, size_property = get_brush_size_owner(context)
    if brush_owner is None:
        return

    value = getattr(brush_owner, size_property)
    font_id = 0
    blf.size(font_id, 16)
    blf.color(font_id, 0.0, 0.95, 0.6, 1.0)
    blf.position(font_id, 24, region.height - 28, 0)
    blf.draw(font_id, f"Brush Size: {value}")
    blf.position(font_id, 24, region.height - 46, 0)
    blf.color(font_id, 1.0, 1.0, 1.0, 0.9)
    blf.draw(font_id, "拖动调整 · 点击 / 空格 / Esc 退出")


def register_view3d_draw_handler():
    global VIEW3D_DRAW_HANDLER, BRUSH_SIZE_OVERLAY_HANDLER, CTRL_HIT_STATUS_HANDLER
    if VIEW3D_DRAW_HANDLER is None:
        VIEW3D_DRAW_HANDLER = bpy.types.SpaceView3D.draw_handler_add(
            draw_zbrush_mode_border,
            (),
            "WINDOW",
            "POST_PIXEL",
        )
    if BRUSH_SIZE_OVERLAY_HANDLER is None:
        BRUSH_SIZE_OVERLAY_HANDLER = bpy.types.SpaceView3D.draw_handler_add(
            draw_brush_size_overlay,
            (),
            "WINDOW",
            "POST_PIXEL",
        )



def unregister_view3d_draw_handler():
    global VIEW3D_DRAW_HANDLER, BRUSH_SIZE_OVERLAY_HANDLER, CTRL_HIT_STATUS_HANDLER
    if VIEW3D_DRAW_HANDLER is not None:
        try:
            bpy.types.SpaceView3D.draw_handler_remove(VIEW3D_DRAW_HANDLER, "WINDOW")
        except (ReferenceError, RuntimeError, ValueError):
            pass
        VIEW3D_DRAW_HANDLER = None
    if BRUSH_SIZE_OVERLAY_HANDLER is not None:
        try:
            bpy.types.SpaceView3D.draw_handler_remove(BRUSH_SIZE_OVERLAY_HANDLER, "WINDOW")
        except (ReferenceError, RuntimeError, ValueError):
            pass
        BRUSH_SIZE_OVERLAY_HANDLER = None
    if CTRL_HIT_STATUS_HANDLER is not None:
        try:
            bpy.types.SpaceView3D.draw_handler_remove(
                CTRL_HIT_STATUS_HANDLER, "WINDOW"
            )
        except (ReferenceError, RuntimeError, ValueError):
            pass
        CTRL_HIT_STATUS_HANDLER = None


def draw_view3d_header_buttons(self, context):
    layout = self.layout
    row = layout.row(align=True)
    row.separator()
    row.enabled = context.mode == "SCULPT"
    row.operator(
        ZBNAV_OT_set_navigation_mode.bl_idname,
        text="ZBrush",
        depress=is_zbrush_sculpt_mode(context),
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
    ZBNAV_OT_switch_sculpt_target,
    ZBNAV_PT_sculpt_target,
    ZBNAV_OT_pan_or_zoom,
    ZBNAV_OT_space_brush_size,
    ZBNAV_OT_alt_select_target,
    ZBNAV_OT_alt_select_or_invert,
    ZBNAV_OT_ctrl_diagnostic_monitor,
    ZBNAV_OT_move_mode_drag,
    ZBNAV_OT_set_navigation_mode,
)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.WindowManager.zb_nav_move_gizmo_style = EnumProperty(
        name="变换轴样式",
        description="选择移动模式中变换轴的外观",
        items=ZBNAV_MOVE_GIZMO_STYLES,
        default="standard",
    )
    bpy.types.WindowManager.zb_nav_sculpt_target = PointerProperty(
        name="雕刻目标",
        description="选择要直接切换到雕刻模式的网格对象",
        type=bpy.types.Object,
        poll=sculpt_target_poll,
    )
    bpy.utils.register_tool(ZBNAV_MOVE_TOOL, separator=True)
    register_view3d_header_buttons()
    register_view3d_draw_handler()
    if not bpy.app.handlers.depsgraph_update_post.count(_auto_zbrush_handler):
        bpy.app.handlers.depsgraph_update_post.append(_auto_zbrush_handler)
    tag_all_view3d_areas_for_redraw()


def unregister():
    global CTRL_DIAGNOSTIC_RUNNING
    CTRL_DIAGNOSTIC_RUNNING = False
    if bpy.app.handlers.depsgraph_update_post.count(_auto_zbrush_handler):
        bpy.app.handlers.depsgraph_update_post.remove(_auto_zbrush_handler)
    remove_zbrush_keymaps()
    restore_sculpt_brush_modifiers()
    restore_view_rotate_axis_snap()
    restore_plain_space_keymaps()
    unregister_view3d_draw_handler()
    if hasattr(bpy.context, "window_manager"):
        bpy.context.window_manager.pop(NAV_MODE_PROP, None)
    unregister_view3d_header_buttons()
    tag_all_view3d_areas_for_redraw()
    try:
        bpy.utils.unregister_tool(ZBNAV_MOVE_TOOL)
    except Exception:
        pass
    if hasattr(bpy.types.WindowManager, "zb_nav_sculpt_target"):
        del bpy.types.WindowManager.zb_nav_sculpt_target
    if hasattr(bpy.types.WindowManager, "zb_nav_move_gizmo_style"):
        del bpy.types.WindowManager.zb_nav_move_gizmo_style
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
