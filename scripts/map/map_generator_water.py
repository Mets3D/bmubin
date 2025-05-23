import math
import os
import struct
import time
import bpy
import bmesh
from pathlib import Path
try:
    from tqdm import tqdm
except:
    # installs progress bar in bpython
    from pip._internal import main
    main(['install', 'tqdm'])
from tqdm import tqdm
import sys


bm: bmesh.types.BMesh = bmesh.new()


water_height_mult = 0x300 / 0xffff
water_material_table = {}
color_layer = bm.loops.layers.float_color.new("water_data")
blocks = []
bvert_location_cache = {}

# block
# {
#   right: [bverts]
#   bottom: [bverts]
# }

# Store the internal edges of all LODs by location, for use in combining LODs
# lod_inside_edge_by_location
lod_borders = [None, {}, {}, {}, {}, {}, {}, {}, {}, None]
lod_current = -1
lod_current_verts = []


def blocks_add_entry(entry):
    blocks[-1].append(entry)


def blocks_new_row():
    # print('blocks_new_row')
    # print(len(blocks))
    if len(blocks) > 1:
        merge_blocks()
    blocks.append([])


def vert_dist(lod):
    return float(scale_multiplier_water[lod])


def connect_lod_borders(lod):
    border_1 = lod_borders[lod]
    border_2 = lod_borders[lod+1]
    border_3 = None
    if lod+2 < 9:
        if len(lod_borders[lod+2]) > 0:
            border_3 = lod_borders[lod+2]
    if not border_2 or not border_1:
        print(f"connect_lod_borders {lod} input sanitization did not pass")
        return
    dist_1 = vert_dist(lod)
    # dist_2 = vert_dist(lod+1)
    new_faces = []
    for bvert in border_1.values():
        bverts = get_face_verts_2_to_3(dist_1, bvert, border_1, border_2)
        if len(bverts) < 5 and border_3:
            # print(f'2 to 5, lod {lod}')
            bverts = get_face_verts_2_to_5(dist_1, bvert, border_1, border_3)

        faces = None
        if len(bverts) == 5:
            faces = pair_face_verts_2_to_3(bverts)
        if len(bverts) == 7:
            faces = pair_face_verts_2_to_5(bverts)

        if not faces:
            continue
        for face_verts in faces:
            if None in face_verts:
                continue
            try:
                new_face = bm.faces.new(face_verts)
                new_faces.append(new_face)
            except:
                pass
                # print('failed to make face')

    # apply stored data
    for new_face in new_faces:
        for loop in new_face.loops:
            # rgba
            make_color = [
                water_material_table[loop.vert][0],
                water_material_table[loop.vert][1],
                water_material_table[loop.vert][2],
                1
            ]
            loop[color_layer] = make_color


def cache_border_verts():
    global lod_current
    global lod_borders
    global lod_current_verts
    for bvert in lod_current_verts:
        if len(bvert.link_edges) > 3:
            continue
        bvert: bmesh.types.BMVert = bvert
        bv_location = str(bvert.co.x) + str(bvert.co.y)
        lod_borders[lod_current][bv_location] = bvert


def merge_blocks():
    # print('merge_blocks')

    # function to connect the faces of adjacent blocks
    block_row_len = len(blocks[0])
    for block_index in range(block_row_len):
        # print(f'block_index {block_index}')
        block = blocks[0][block_index]
        if not block:
            continue
        new_faces = []

        block_below = None
        if len(blocks) > 1 and len(blocks[1]) == len(blocks[0]):
            block_below = blocks[1][block_index]
        # print(f'block_below {bool(block_below)}')
        if block_below:
            for bvert_index in range(len(block[0])-1):
                face_verts = [
                    block[-1][bvert_index],
                    block[-1][bvert_index+1],
                    block_below[0][bvert_index+1],
                    block_below[0][bvert_index],
                ]
                if None in face_verts:
                    continue
                new_face = bm.faces.new(face_verts)
                new_faces.append(new_face)

        block_right = None
        if block_index < block_row_len-1:
            block_right = blocks[0][block_index+1]
        # print(f'block_right {bool(block_right)}')
        if block_right:
            for bvert_index in range(len(block)-1):
                face_verts = [
                    block[bvert_index][-1],
                    block_right[bvert_index][0],
                    block_right[bvert_index+1][0],
                    block[bvert_index+1][-1],
                ]
                if None in face_verts:
                    continue
                new_face = bm.faces.new(face_verts)
                new_faces.append(new_face)

        # handle corner face
        block_diagonal = None
        if block_below and block_index < block_row_len-1:
            block_diagonal = blocks[1][block_index+1]
        if block_right and block_below and block_diagonal:
            face_verts = [
                block[-1][-1],
                block_right[-1][0],
                block_diagonal[0][0],
                block_below[0][-1],
            ]
            if None not in face_verts:
                new_face = bm.faces.new(face_verts)
                new_faces.append(new_face)

        # apply vertex color
        for new_face in new_faces:
            for loop in new_face.loops:
                # rgba
                make_color = [
                    water_material_table[loop.vert][0],
                    water_material_table[loop.vert][1],
                    water_material_table[loop.vert][2],
                    1
                ]
                loop[color_layer] = make_color
    blocks.pop(0)


def build_block(grid_tl, grid_xy, detail, mdb):
    name = '5' + str(detail)
    grid_z = moser_de_brujin(grid_xy, mdb)

    name += format(grid_z, '0>8X')
    file = None
    file_name_terrain = 'map_data/water/' + name + '.water.extm'
    if os.path.isfile(file_name_terrain):
        try:
            file = open(file_name_terrain, 'rb')
        except:
            print(f'open {file_name_terrain} failed')
            blocks_add_entry(None)
            return
    else:
        # print(f'{file_name} does not exist')
        blocks_add_entry(None)
        return

    # https://docs.python.org/3/library/struct.html
    # https://zeldamods.org/wiki/Water.extm
    h_data = struct.unpack('<HHHBB', file.read(8))
    heights = []
    materials = []
    x_axis_flow_rates = []
    z_axis_flow_rates = []
    while h_data:
        heights.append(h_data[0])
        x_axis_flow_rates.append(h_data[1])
        z_axis_flow_rates.append(h_data[2])
        materials.append(h_data[4])
        try:
            h_data = struct.unpack('<HHHBB', file.read(8))
        except:
            h_data = None

    # raise 'stop'

    grid_rel_x = 64*(grid_xy[0] - grid_tl[0])
    grid_rel_y = 64*(grid_xy[1] - grid_tl[1])

    vertex_x = 0
    vertex_y = 0
    # make verts
    rows = []
    row = []

    for index in range(len(heights)):
        height = heights[index]
        # is_edge = vertex_x == 0 or vertex_x == 63 or vertex_y == 0 or vertex_y == 63
        if vertex_x > 63:
            vertex_x = 0
            vertex_y += 1
            rows.append(row)
            row = []
        if vertex_y > 63:
            print("this shouldn't happen")
            vertex_y = 0

        mult_loc = float(scale_multiplier_water[detail])
        x_loc = mult_loc * (grid_rel_x + vertex_x)
        y_loc = mult_loc * (grid_rel_y + vertex_y)
        location_cache_key = str(x_loc) + str(y_loc)
        vertex_x += 1
        if location_cache_key in bvert_location_cache:
            if bvert_location_cache[location_cache_key] == False:
                bvert_location_cache[location_cache_key] = True
            else:
                row.append(None)
                continue
        else:
            bvert_location_cache[location_cache_key] = True

        bvert = bm.verts.new((
            x_loc,
            y_loc,
            height
        ))
        global lod_current_verts
        lod_current_verts.append(bvert)
        row.append(bvert)
        material = materials[index]
        x_axis_flow_rate = x_axis_flow_rates[index]
        z_axis_flow_rate = z_axis_flow_rates[index]
        water_material_table[bvert] = [
            x_axis_flow_rate/0xffff,
            z_axis_flow_rate/0xffff,
            material/10
        ]

    rows.append(row)
    blocks_add_entry(rows)

    previous_row = rows[0]
    for row in rows[1:]:
        for bvert_index in range(len(row)-1):
            face_verts = [
                previous_row[bvert_index],
                previous_row[bvert_index+1],
                row[bvert_index+1],
                row[bvert_index],
            ]
            if None in face_verts:
                continue
            new_face = bm.faces.new(face_verts)
            for loop in new_face.loops:
                # rgba
                make_color = [
                    water_material_table[loop.vert][0],
                    water_material_table[loop.vert][1],
                    water_material_table[loop.vert][2],
                    1
                ]
                loop[color_layer] = make_color
        previous_row = row

    file.close()


def build_blocks_in_range(tl, br, detail):
    """tl - top left, br - bottom right"""
    grid_size = 2**detail
    mdb = generate_mdb(grid_size)
    grid_tl = tuple([int(x*grid_size) for x in tl])
    grid_br = tuple([math.ceil(x*grid_size) - 1 for x in br])
    tqdm_args = {
        'leave': False,
        'ascii': True,
        'dynamic_ncols': True,
        'colour': 'green',
        'desc': 'vertices'
    }
    # length_y = grid_br[1] + 1 - grid_tl[1]
    # length_x = grid_br[0] + 1 - grid_tl[0]
    # print(f'length_y {length_y}')
    # print(f'length_x {length_x}')
    for y in tqdm(range(grid_tl[1], grid_br[1] + 1), **tqdm_args):
        blocks_new_row()
        for x in range(grid_tl[0], grid_br[0] + 1):
            build_block(grid_tl, (x, y), detail, mdb)
    blocks_new_row()
    blocks_new_row()


def reset_globals():
    global bm, water_material_table, color_layer, blocks, lod_current_verts
    # bm = bmesh.new()
    # water_material_table = {}
    # color_layer = bm.loops.layers.float_color.new("water_data")
    blocks = []
    lod_current_verts = []


def build_water_map():
    bpy.ops.object.select_all(action='SELECT')
    bpy.ops.object.delete()

    for area in bpy.data.screens["Layout"].areas:
        if area.type == 'VIEW_3D':
            for space in area.spaces:
                if space.type == 'VIEW_3D':
                    space.shading.color_type = 'TEXTURE'
                    space.clip_end = 100000
        if area.type == 'OUTLINER':
            space = area.spaces[0]
            space.show_restrict_column_viewport = True

    # for detail in range(4, 5):
    for detail in range(3, 0, -1):
        global lod_current
        lod_current = detail
        build_blocks_in_range((0, 0), (1, 1), detail)
        cache_border_verts()
        reset_globals()
        connect_lod_borders(detail)

    print('\n')


def apply_water_mat(object: bpy.types.Object):
    water_mat_name = 'BotW_Liquids'
    water_mat = bpy.data.materials.get(water_mat_name)
    if not water_mat:
        # import the actor terrain material from water_mat.blend
        append_directory = Path(f"linked_resources\\linked.blend").absolute()
        append_directory = f'{str(append_directory)}\\Material\\'
        files = [{'name': water_mat_name}]
        bpy.ops.wm.append(directory=append_directory, files=files, link=True, instance_collections=True)
        water_mat = bpy.data.materials.get(water_mat_name)
    object.active_material = water_mat


def main():
    if not os.path.isdir('map_data'):
        print('No map_data found')
        return
    build_water_map()
    map_object = add_map_to_scene('water_map', bm)
    apply_water_mat(map_object)
    save_path = Path(f"linked_resources\\water_map.blend").absolute()
    bpy.ops.wm.save_as_mainfile(filepath=str(save_path))


if __name__ == "__main__":
    # print(f"{__file__} is being run directly")
    sys.path.append(os.path.abspath("."))
    from scripts.map.map_generator_shared import *
    main()
else:
    # print(f"{__file__} is being imported")
    sys.path.append(os.path.abspath("."))
    from scripts.map.map_generator_shared import *
