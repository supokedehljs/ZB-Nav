bl_info = {
    "name": "ZB-Nav",
    "author": "supokede, Cursor",
    "version": (1, 16, 0),
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
from bpy.props import FloatProperty, FloatVectorProperty
from bpy_extras import view3d_utils
from gpu_extras.batch import batch_for_shader

ADDON_KEYMAPS = []
SCULPT_BRUSH_MODIFIERS = []
NAV_MODE_PROP = "zb_nav_mode"
HEADER_REGISTERED_PROP = "_zb_nav_view3d_header_registered"
VIEW3D_DRAW_HANDLER = None
MOVE_MODE_HOVER = None
GIZMO_HOVER_MONITOR_RUNNING = False

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
]

def get_preferences(context):
    addon = context.preferences.addons.get(__name__)
    if addon:
        return addon.preferences
    return None


def get_nav_mode(context):
    return context.window_manager.get(NAV_MODE_PROP, "BLENDER")


def is_navigation_enabled(context):
    return context.window_manager.get(NAV_MODE_PROP, "BLENDER") == "ZBRUSH"


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


def _get_gizmo_matrix(context):
    global MOVE_GIZMO_PIVOT
    obj = context.active_object
    if not obj:
        return None
    # The pivot lives on the mesh data so it reverts together with the mesh
    # when the user undoes a gizmo transform.
    if obj.data and "zb_nav_pivot" in obj.data:
        matrix = _pivot_from_list(obj.data["zb_nav_pivot"])
        MOVE_GIZMO_PIVOT = (obj.name, matrix)
        return matrix
    if MOVE_GIZMO_PIVOT is not None and MOVE_GIZMO_PIVOT[0] == obj.name:
        matrix = MOVE_GIZMO_PIVOT[1]
        try:
            obj.data["zb_nav_pivot"] = _pivot_to_list(matrix)
        except Exception:
            pass
        return matrix
    matrix = obj.matrix_world.copy()
    MOVE_GIZMO_PIVOT = (obj.name, matrix)
    try:
        obj.data["zb_nav_pivot"] = _pivot_to_list(matrix)
    except Exception:
        pass
    return matrix


def _set_gizmo_matrix(obj_name, matrix):
    global MOVE_GIZMO_PIVOT
    MOVE_GIZMO_PIVOT = (obj_name, matrix.copy())
    obj = bpy.data.objects.get(obj_name)
    if obj and obj.data:
        try:
            obj.data["zb_nav_pivot"] = _pivot_to_list(matrix)
        except Exception:
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


def _mesh_vertex_mask(obj, vertex_index):
    """Return Blender's sculpt mask value for one mesh vertex."""
    if not obj or not obj.data:
        return 0.0
    mask_layer = obj.data.attributes.get(".sculpt_mask")
    if mask_layer and vertex_index < len(mask_layer.data):
        try:
            return max(0.0, min(1.0, float(mask_layer.data[vertex_index].value)))
        except (AttributeError, TypeError, ValueError):
            pass
    return 0.0


def _unmasked_world_center(obj):
    """Return the center of vertices that are not sculpt-masked."""
    points = [
        obj.matrix_world @ vertex.co
        for vertex in obj.data.vertices
        if _mesh_vertex_mask(obj, vertex.index) < 0.999
    ]
    if not points:
        return obj.matrix_world.translation.copy()
    center = mathutils.Vector((0.0, 0.0, 0.0))
    for point in points:
        center += point
    return center / len(points)


def _transform_mesh_in_world(obj, world_transform):
    """Transform only unmasked mesh vertices in world space."""
    object_to_world = obj.matrix_world.copy()
    local_transform = (
        object_to_world.inverted_safe()
        @ world_transform
        @ object_to_world
    )
    for vertex in obj.data.vertices:
        if _mesh_vertex_mask(obj, vertex.index) < 0.999:
            vertex.co = local_transform @ vertex.co
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


def _gizmo_length(context):
    matrix = _get_gizmo_matrix(context)
    if matrix is None:
        return 1.0
    origin = matrix.translation
    region = context.region
    region_3d = context.region_data
    if not region or not region_3d:
        return 1.0

    target_pixels = GIZMO_PIXEL_SIZE

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
            candidates.append((d - GIZMO_MOVE_TRI_HALF_WID, "move", axis))
        if mid_screen:
            d = math.hypot(mouse_x - mid_screen.x, mouse_y - mid_screen.y)
            candidates.append((d - 12.0, "scale", axis))

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
            candidates.append((best - 3.0, "rotate", axis))

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
            candidates.append((best - 3.0, "view_rotate", -1))

        # four corner triangles: translate in the view plane
        corner_angles = (45, 135, 225, 315)
        corner_radius = length * GIZMO_CORNER_RADIUS
        for corner in range(4):
            rad = math.radians(corner_angles[corner])
            world = origin + (view_right * math.cos(rad) + view_up * math.sin(rad)) * corner_radius
            screen = _to_screen(context, world)
            if screen:
                d = math.hypot(mouse_x - screen.x, mouse_y - screen.y)
                candidates.append((d - 16.0, "view_move", corner))

    # center square: uniform scale
    origin_screen = _to_screen(context, origin)
    if origin_screen:
        d = math.hypot(mouse_x - origin_screen.x, mouse_y - origin_screen.y)
        candidates.append((d - GIZMO_CENTER_SQUARE_HALF - 4.0, "scale_all", -1))

        # three gizmo buttons above the pivot
        btn_size = _button_size(context)
        for btn in range(3):
            bx, by = _gizmo_button_positions(context, origin_screen)[btn]
            d = math.hypot(mouse_x - bx, mouse_y - by)
            candidates.append((d - btn_size, "gizmo_button", btn))

    candidates.sort(key=lambda item: item[0])
    if candidates and candidates[0][0] < 6.0:
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
            context.mode == "SCULPT"
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
            # move to the center of the visible/unmasked mesh
            center = _unmasked_world_center(obj)
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
                factor = max(0.001, 1.0 + signed_delta / GIZMO_PIXEL_SIZE)
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
    """轻量监听：只为移动工具的手柄提供悬停高亮。"""

    bl_idname = "zb_nav.gizmo_hover_monitor"
    bl_label = "Gizmo Hover Monitor"
    bl_options = {"INTERNAL"}

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
        if context.area:
            context.area.tag_redraw()
        return {"CANCELLED"}

    def modal(self, context, event):
        global MOVE_MODE_HOVER
        if not is_zbrush_sculpt_mode(context):
            return self._finish(context)

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


def add_zbrush_keymaps():
    remove_zbrush_keymaps()
    swap_sculpt_brush_modifiers()
    for item in ZBRUSH_KEYMAP_ITEMS:
        _add_keymap_item(item)


def update_navigation_mode(context, mode):
    """切换子模式；开启时启动手柄悬停监测。"""
    if (
        mode == "ZBRUSH"
        and context.mode == "SCULPT"
        and not GIZMO_HOVER_MONITOR_RUNNING
    ):
        try:
            bpy.ops.zb_nav.gizmo_hover_monitor("INVOKE_DEFAULT")
        except (RuntimeError, TypeError):
            pass
    set_nav_mode(context, mode)
    tag_all_view3d_areas_for_redraw()
    return True


_HOVER_MONITOR_LAST_SCULPT = None


def _hover_monitor_handler(scene, depsgraph):
    """进入雕刻模式时补启动悬停监测（不会自动打开 ZBrush 开关）。"""
    global _HOVER_MONITOR_LAST_SCULPT
    context = bpy.context
    is_sculpt = context.mode == "SCULPT"
    if is_sculpt == _HOVER_MONITOR_LAST_SCULPT:
        return
    _HOVER_MONITOR_LAST_SCULPT = is_sculpt
    if is_sculpt and is_navigation_enabled(context):
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

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "pan_sensitivity")
        layout.prop(self, "zoom_sensitivity")
        layout.label(text="控制轴颜色")
        color_row = layout.row(align=True)
        color_row.prop(self, "axis_color_x", text="")
        color_row.prop(self, "axis_color_y", text="")
        color_row.prop(self, "axis_color_z", text="")


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


def _gizmo_button_positions(_context, origin_screen):
    # GIZMO_BUTTON_Y_OFFSET 是轴在屏幕上的像素长度的倍数。
    pixel_offset = GIZMO_PIXEL_SIZE * GIZMO_BUTTON_Y_OFFSET
    spacing = GIZMO_BUTTON_SPACING
    base_y = origin_screen.y + pixel_offset
    return [
        (origin_screen.x - spacing, base_y),
        (origin_screen.x, base_y),
        (origin_screen.x + spacing, base_y),
    ]


def _button_size(context):
    prefs = get_preferences(context)
    return GIZMO_BUTTON_RADIUS


def _button_color(_context):
    return (0.32, 0.32, 0.32)


def _draw_button_icon(shader, cx, cy, kind, hover, size, color):
    """Draw one simple borderless icon with smooth line rendering."""
    s = size / 10.0
    icon = (1.0, 1.0, 1.0, 1.0) if hover else (color[0], color[1], color[2], 1.0)
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
    x = min(max(bx + button_size + 8.0, 8.0), max(region.width - 180.0, 8.0))
    y = by + 5.0
    blf.size(font_id, 12)
    blf.color(font_id, 1.0, 1.0, 1.0, 0.95)
    blf.position(font_id, x, y, 0)
    blf.draw(font_id, title)
    blf.size(font_id, 10)
    blf.color(font_id, 0.72, 0.76, 0.82, 0.9)
    blf.position(font_id, x, y - 15.0, 0)
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
    segments = 128

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

    shader = gpu.shader.from_builtin("UNIFORM_COLOR")
    gpu.state.blend_set("ALPHA")
    gpu.state.line_width_set(2.5)

    origin_screen = _to_screen(context, origin)
    if not origin_screen:
        return

    hover = MOVE_MODE_HOVER

    def accent(base, kind):
        if hover and hover[1] == axis and hover[0] == kind:
            return (1.0, 1.0, 1.0, 1.0)
        return (base[0], base[1], base[2], 1.0)

    for axis in range(3):
        try:
            color = _axis_color(context, axis)
            axis_dir = axes[axis]

            tip_screen = _to_screen(context, origin + axis_dir * length)
            base_screen = _to_screen(context, origin + axis_dir * length * GIZMO_MOVE_TRI_BASE)
            mid_screen = _to_screen(context, origin + axis_dir * length * GIZMO_SCALE_POS)

            if tip_screen and origin_screen:
                _draw_line_2d(shader, (origin_screen.x, origin_screen.y), (tip_screen.x, tip_screen.y), accent(color, "move"))

            if tip_screen and base_screen:
                _draw_tri_along_axis(
                    shader,
                    (tip_screen.x, tip_screen.y),
                    (base_screen.x, base_screen.y),
                    accent(color, "move"),
                    GIZMO_MOVE_TRI_HALF_WID,
                )

            if mid_screen and tip_screen and origin_screen:
                _draw_rect_along_axis(
                    shader,
                    (mid_screen.x, mid_screen.y),
                    (tip_screen.x - origin_screen.x, tip_screen.y - origin_screen.y),
                    accent(color, "scale"),
                    GIZMO_SCALE_RECT_LEN,
                    GIZMO_SCALE_RECT_WID,
                )

            radius = length * GIZMO_RING_RADIUS
            other1 = axes[(axis + 1) % 3]
            other2 = axes[(axis + 2) % 3]
            view_dir = region_3d.view_rotation @ mathutils.Vector((0, 0, -1))
            ring_color = accent(color, "rotate")
            ring_color = (ring_color[0], ring_color[1], ring_color[2], 1.0)
            _draw_tube_band_2d(
                shader,
                context,
                origin,
                other1,
                other2,
                radius,
                length * (GIZMO_RING_TUBE_W / GIZMO_PIXEL_SIZE),
                length * (GIZMO_RING_TUBE_H / GIZMO_PIXEL_SIZE),
                view_dir,
                ring_color,
            )
        except Exception:
            pass

    # outer view-rotation ring (circle facing the camera)
    try:
        view_right = region_3d.view_rotation @ mathutils.Vector((1, 0, 0))
        view_up = region_3d.view_rotation @ mathutils.Vector((0, 1, 0))
        outer_radius = length * GIZMO_VIEW_RING_RADIUS
        band_world = length * (GIZMO_RING_BAND_PX / GIZMO_PIXEL_SIZE)
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
            outer_color = (0.82, 0.82, 0.82, 1.0) if hover and hover[0] == "view_rotate" else (0.52, 0.52, 0.52, 1.0)
            _draw_ring_band_2d(shader, inner_points, outer_points, outer_color)

        # four corner triangles: translate in the view plane (squat, farther out)
        corner_angles = (45, 135, 225, 315)
        corner_radius = length * GIZMO_CORNER_RADIUS
        tri_height = GIZMO_CORNER_TRI_HEIGHT
        tri_half_wid = GIZMO_CORNER_TRI_HALF_WID
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
            dx, dy = rx / rnorm, ry / rnorm
            px, py = -dy, dx
            corner_color = (0.82, 0.82, 0.82, 1.0) if hover and hover[0] == "view_move" and hover[1] == corner else (0.52, 0.52, 0.52, 1.0)
            if hover and hover[0] == "view_move" and hover[1] == corner:
                corner_color = (1.0, 1.0, 1.0, 1.0)
            apex = (center[0] + dx * tri_height, center[1] + dy * tri_height)
            left = (center[0] + px * tri_half_wid, center[1] + py * tri_half_wid)
            right = (center[0] - px * tri_half_wid, center[1] - py * tri_half_wid)
            _draw_tri_2d(shader, apex, left, right, corner_color)
    except Exception:
        pass

    # redraw the three translation arrows last so the view ring never covers them
    for axis in range(3):
        try:
            color = _axis_color(context, axis)
            axis_dir = axes[axis]
            tip_screen = _to_screen(context, origin + axis_dir * length)
            base_screen = _to_screen(context, origin + axis_dir * length * GIZMO_MOVE_TRI_BASE)
            if tip_screen and base_screen:
                arrow_color = accent(color, "move")
                _draw_line_2d(shader, (origin_screen.x, origin_screen.y), (tip_screen.x, tip_screen.y), arrow_color)
                _draw_tri_along_axis(shader, (tip_screen.x, tip_screen.y), (base_screen.x, base_screen.y), arrow_color, GIZMO_MOVE_TRI_HALF_WID)
        except Exception:
            pass

    # center square: uniform scale (opaque)
    try:
        center_color = (1.0, 1.0, 1.0, 1.0)
        half = GIZMO_CENTER_SQUARE_HALF
        cx, cy = origin_screen.x, origin_screen.y
        verts = [
            (cx - half, cy - half), (cx + half, cy - half), (cx + half, cy + half),
            (cx - half, cy - half), (cx + half, cy + half), (cx - half, cy + half),
        ]
        batch = batch_for_shader(shader, "TRIS", {"pos": verts})
        shader.bind()
        shader.uniform_float("color", center_color)
        batch.draw(shader)
    except Exception:
        pass

    # three gizmo buttons above the pivot
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
    except Exception:
        pass

    gpu.state.line_width_set(1.0)
    gpu.state.blend_set("NONE")


def register_view3d_draw_handler():
    global VIEW3D_DRAW_HANDLER
    if VIEW3D_DRAW_HANDLER is None:
        VIEW3D_DRAW_HANDLER = bpy.types.SpaceView3D.draw_handler_add(
            draw_move_mode_gizmo,
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
    ZBNAV_OT_set_navigation_mode,
    ZBNAV_OT_gizmo_hover_monitor,
    ZBNAV_OT_move_mode_drag,
)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.utils.register_tool(ZBNAV_MOVE_TOOL, separator=True)
    register_view3d_header_buttons()
    register_view3d_draw_handler()
    add_zbrush_keymaps()
    if not bpy.app.handlers.depsgraph_update_post.count(_hover_monitor_handler):
        bpy.app.handlers.depsgraph_update_post.append(_hover_monitor_handler)
    tag_all_view3d_areas_for_redraw()


def unregister():
    global GIZMO_HOVER_MONITOR_RUNNING
    GIZMO_HOVER_MONITOR_RUNNING = False
    if bpy.app.handlers.depsgraph_update_post.count(_hover_monitor_handler):
        bpy.app.handlers.depsgraph_update_post.remove(_hover_monitor_handler)
    remove_zbrush_keymaps()
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
