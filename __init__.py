bl_info = {
    "name": "ZB-Nav",
    "author": "supokede, Cursor",
    "version": (1, 5, 1),
    "blender": (4, 0, 0),
    "location": "3D View Header > ZBrush",
    "description": "在 Blender 雕刻模式中启用 ZBrush 风格的视图导航子模式",
    "category": "3D View",
}

import math

import bpy
import gpu
from bpy.props import FloatProperty, PointerProperty
from bpy_extras import view3d_utils
from gpu_extras.batch import batch_for_shader

ADDON_KEYMAPS = []
SCULPT_BRUSH_MODIFIERS = []
SCULPT_ALT_LEFT_CONFLICTS = []
NAV_MODE_PROP = "zb_nav_mode"
HEADER_REGISTERED_PROP = "_zb_nav_view3d_header_registered"
VIEW3D_DRAW_HANDLER = None

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
        "idname": "zb_nav.block_space",
        "type": "SPACE",
        "value": "PRESS",
        "properties": {},
    },
    {
        "keymap": "Sculpt",
        "space_type": "EMPTY",
        "idname": "zb_nav.block_space",
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


def add_zbrush_keymaps():
    remove_zbrush_keymaps()
    swap_sculpt_brush_modifiers()

    wm = bpy.context.window_manager
    kc = wm.keyconfigs.addon
    if not kc:
        return

    for item in ZBRUSH_KEYMAP_ITEMS:
        km = kc.keymaps.new(
            name=item.get("keymap", "3D View"),
            space_type=item.get("space_type", "EMPTY"),
        )
        props = item.get("properties", {})
        kmi = km.keymap_items.new(
            item["idname"],
            type=item["type"],
            value=item["value"],
            alt=item.get("alt", False),
            ctrl=item.get("ctrl", False),
            shift=item.get("shift", False),
            oskey=item.get("oskey", False),
        )
        for prop_name, prop_value in props.items():
            setattr(kmi.properties, prop_name, prop_value)
        ADDON_KEYMAPS.append((km, kmi))


def update_navigation_mode(context, mode):
    if mode == "ZBRUSH":
        if context.mode != "SCULPT":
            return False
        add_zbrush_keymaps()
    else:
        remove_zbrush_keymaps()
        restore_sculpt_brush_modifiers()
    set_nav_mode(context, mode)
    tag_all_view3d_areas_for_redraw()
    return True


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

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "pan_sensitivity")
        layout.prop(self, "zoom_sensitivity")


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
        enabled = is_zbrush_sculpt_mode(context)

        status = layout.row()
        status.label(
            text="ZBrush 子模式已开启" if enabled else "请先点击顶部 ZBrush",
            icon="SCULPTMODE_HLT" if enabled else "INFO",
        )

        column = layout.column(align=True)
        column.enabled = enabled
        column.prop(
            context.window_manager,
            "zb_nav_sculpt_target",
            text="雕刻目标",
            icon="OBJECT_DATA",
        )
        column.operator(
            ZBNAV_OT_switch_sculpt_target.bl_idname,
            text="切换到目标雕刻",
            icon="SCULPTMODE_HLT",
        )

        layout.separator()
        layout.label(text="快捷方式：Alt + 左键点击模型")
        layout.label(text="若快捷键冲突，请使用上方选择框", icon="INFO")


class ZBNAV_OT_block_space(bpy.types.Operator):
    bl_idname = "zb_nav.block_space"
    bl_label = "Block Spacebar"
    bl_description = "在 ZBrush 雕刻子模式中拦截空格键"
    bl_options = {"INTERNAL"}

    @classmethod
    def poll(cls, context):
        return is_zbrush_sculpt_mode(context)

    def execute(self, context):
        return {"FINISHED"}


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
        if not (event.alt and event.ctrl and event.shift and event.type == "MIDDLEMOUSE"):
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
    if not is_zbrush_sculpt_mode(context):
        return

    region = context.region
    if not region or region.type != "WINDOW":
        return

    inset = 2.0
    width = max(float(region.width) - inset, inset)
    height = max(float(region.height) - inset, inset)
    vertices = (
        (inset, inset), (width, inset),
        (width, inset), (width, height),
        (width, height), (inset, height),
        (inset, height), (inset, inset),
    )
    shader = gpu.shader.from_builtin("UNIFORM_COLOR")
    batch = batch_for_shader(shader, "LINES", {"pos": vertices})

    gpu.state.blend_set("ALPHA")
    gpu.state.line_width_set(4.0)
    shader.bind()
    shader.uniform_float("color", (1.0, 0.03, 0.03, 1.0))
    batch.draw(shader)
    gpu.state.line_width_set(1.0)
    gpu.state.blend_set("NONE")


def register_view3d_draw_handler():
    global VIEW3D_DRAW_HANDLER
    if VIEW3D_DRAW_HANDLER is None:
        VIEW3D_DRAW_HANDLER = bpy.types.SpaceView3D.draw_handler_add(
            draw_zbrush_mode_border,
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
    ZBNAV_OT_block_space,
    ZBNAV_OT_alt_select_target,
    ZBNAV_OT_alt_select_or_invert,
    ZBNAV_OT_set_navigation_mode,
)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.WindowManager.zb_nav_sculpt_target = PointerProperty(
        name="雕刻目标",
        description="选择要直接切换到雕刻模式的网格对象",
        type=bpy.types.Object,
        poll=sculpt_target_poll,
    )
    register_view3d_header_buttons()
    register_view3d_draw_handler()
    set_nav_mode(bpy.context, "BLENDER")
    tag_all_view3d_areas_for_redraw()


def unregister():
    remove_zbrush_keymaps()
    restore_sculpt_brush_modifiers()
    unregister_view3d_draw_handler()
    if hasattr(bpy.context, "window_manager"):
        bpy.context.window_manager.pop(NAV_MODE_PROP, None)
    unregister_view3d_header_buttons()
    tag_all_view3d_areas_for_redraw()
    if hasattr(bpy.types.WindowManager, "zb_nav_sculpt_target"):
        del bpy.types.WindowManager.zb_nav_sculpt_target
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
