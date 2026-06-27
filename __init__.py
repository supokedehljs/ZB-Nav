bl_info = {
    "name": "ZB-Nav",
    "author": "supokede, Cursor",
    "version": (1, 2, 0),
    "blender": (4, 0, 0),
    "location": "Top Bar > Blender / ZBrush",
    "description": "在顶部状态栏快速切换 Blender / ZBrush 视图导航方式",
    "category": "3D View",
}

import math

import bpy
from bpy.props import EnumProperty, FloatProperty
from bpy_extras import view3d_utils

ADDON_KEYMAPS = []
NAV_MODE_PROP = "zb_nav_mode"
HEADER_REGISTERED_PROP = "_zb_nav_view3d_header_registered"

ZBRUSH_KEYMAP_ITEMS = [
    {
        "idname": "zb_nav.pan_or_zoom",
        "type": "MIDDLEMOUSE",
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


def set_nav_mode(context, mode):
    context.window_manager[NAV_MODE_PROP] = mode


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


def add_zbrush_keymaps():
    remove_zbrush_keymaps()

    wm = bpy.context.window_manager
    kc = wm.keyconfigs.addon
    if not kc:
        return

    km = kc.keymaps.new(name="3D View", space_type="VIEW_3D")
    for item in ZBRUSH_KEYMAP_ITEMS:
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
        add_zbrush_keymaps()
    else:
        remove_zbrush_keymaps()
    set_nav_mode(context, mode)


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

    _last_mouse_x = 0
    _last_mouse_y = 0
    _region = None
    _region_3d = None
    _last_depth_location = None
    _pivot_location = None

    @classmethod
    def poll(cls, context):
        return context.area and context.area.type == "VIEW_3D" and context.region_data

    def invoke(self, context, event):
        self._region = context.region
        self._region_3d = context.region_data
        self._last_mouse_x = event.mouse_region_x
        self._last_mouse_y = event.mouse_region_y
        self._last_depth_location = find_depth_location(
            context,
            self._region,
            self._region_3d,
            event.mouse_region_x,
            event.mouse_region_y,
        )
        self._pivot_location = self._last_depth_location
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
                    self._zoom_view(context, current_x, current_y, dx, dy)

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

    def _zoom_view(self, context, current_x, current_y, dx, dy):
        focus_location = find_depth_location(context, self._region, self._region_3d, current_x, current_y)
        if focus_location is None:
            focus_location = self._pivot_location or self._last_depth_location or self._region_3d.view_location

        prefs = get_preferences(context)
        sensitivity = prefs.zoom_sensitivity if prefs else 1.0
        previous_distance = max(self._region_3d.view_distance, 0.0001)
        delta = dx + dy
        zoom_factor = math.exp(-delta * 0.01 * sensitivity)
        new_distance = max(previous_distance * zoom_factor, 0.0001)
        ratio = new_distance / previous_distance

        self._region_3d.view_location = focus_location + (self._region_3d.view_location - focus_location) * ratio
        self._region_3d.view_distance = new_distance
        self._pivot_location = focus_location
        self._last_depth_location = focus_location


class ZBNAV_OT_set_navigation_mode(bpy.types.Operator):
    bl_idname = "zb_nav.set_navigation_mode"
    bl_label = "Set Navigation Mode"
    bl_description = "切换 3D 视图导航方式"
    bl_options = {"REGISTER"}

    mode: EnumProperty(
        name="Navigation Mode",
        items=(
            ("BLENDER", "Blender", "使用 Blender 默认视图导航快捷键"),
            ("ZBRUSH", "ZBrush", "使用接近 ZBrush 的 Alt 鼠标视图导航快捷键"),
        ),
        default="BLENDER",
    )

    def execute(self, context):
        update_navigation_mode(context, self.mode)
        return {"FINISHED"}


def draw_view3d_header_buttons(self, context):
    layout = self.layout
    mode = get_nav_mode(context)
    row = layout.row(align=True)
    row.separator()

    blender = row.operator(
        ZBNAV_OT_set_navigation_mode.bl_idname,
        text="Blender",
        depress=mode == "BLENDER",
    )
    blender.mode = "BLENDER"

    zbrush = row.operator(
        ZBNAV_OT_set_navigation_mode.bl_idname,
        text="ZBrush",
        depress=mode == "ZBRUSH",
    )
    zbrush.mode = "ZBRUSH"


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
    ZBNAV_OT_set_navigation_mode,
)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    register_view3d_header_buttons()
    set_nav_mode(bpy.context, "BLENDER")


def unregister():
    remove_zbrush_keymaps()
    if hasattr(bpy.context, "window_manager"):
        bpy.context.window_manager.pop(NAV_MODE_PROP, None)
    unregister_view3d_header_buttons()
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
