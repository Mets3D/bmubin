from collections import Counter
import math
import os
import struct
import bpy
import bmesh
from pathlib import Path
from tqdm import tqdm
import sys
import json

with open("scripts\\map\\index_mapping.json", "r") as f:
    index_mapping = json.load(f)
    index_mapping = index_mapping['index_mapping']
    f.close()


def vert_dist(lod_level):
    num_chunks = 2**lod_level
    chunk_world_size = 16000/num_chunks
    grid_unit_size = chunk_world_size/256
    return grid_unit_size


x_dist = float(1/83)
vadd_viewport = [
    (0, 0),
    (0, 1),
    (x_dist, 1),
    (x_dist, 0),
]
vadd = [
    (0, 0),
    (0, 1),
    (0, 1),
    (0, 0),
]

class TerrainBuilder:
    def __init__(self, map_section="A-1"):
        # TODO: It actually doesn't seem to work on detail level 8, at least sometimes?
        self.map_section = map_section

        self.bm: bmesh.types.BMesh = bmesh.new()
        self.color_layer = self.bm.loops.layers.float_color.new("material_data")
        self.uv_lay0 = self.bm.loops.layers.uv.new("material0")
        self.uv_lay1 = self.bm.loops.layers.uv.new("material1")

        self.blocks = []
        self.bvert_location_cache = {}
        self.bvert_material_table = {}

    def build(self, lod_level):
        assert lod_level < 9
        
        # Store the internal edges of all LODs by location, for use in combining LODs.
        lod_borders = [None, {}, {}, {}, {}, {}, {}, {}, {}, None]
        for lod_current in range(lod_level, 0, -1):
            # We use the bvert_location_cache
            # to ignore vertices that have already been accounted for by lower LOD meshes, as we add 
            # more vertices in-between from the higher LODs.
            # This assumes that the data in lower LOD meshes for a given vertex is the same as the data for the same vertex in a lower LOD mesh, which is hopefully and probably true.
            lod_verts = self.build_blocks_lod(lod_current)
            lod_borders[lod_current] = {
                str(v.co.x)+str(v.co.y): v 
                for v in lod_verts
                if len(v.link_edges) < 4
            }
            self.connect_lod_borders(lod_current, lod_borders)

    def build_blocks_lod(self, lod_current) -> list[bmesh.types.BMVert]:
        # TODO: Refactor this such that it actually takes topleft/bottomright coordinates and just loops over those, from highest to lowest LOD, skipping missing files.
        """tl - top left, br - bottom right"""
        tl, br = (0, 0), (1, 1)
        grid_size = 2**lod_current
        grid_tl = tuple([int(x*grid_size) for x in tl])
        grid_br = tuple([math.ceil(x*grid_size) - 1 for x in br])
        tqdm_args = {
            'leave': False,
            'ascii': True,
            'dynamic_ncols': True,
            'colour': 'green',
            'desc': 'blocks'
        }
        lod_verts = []
        for grid_y in tqdm(range(grid_tl[1], grid_br[1] + 1), **tqdm_args):
            self.blocks_new_row()
            for grid_x in range(grid_tl[0], grid_br[0] + 1):
                # handle LOD focus, ignore if far away
                if not terrain_is_within_map_section(self.map_section, lod_current, (grid_x, grid_y)):
                    self.blocks_add_entry(None)
                    continue
                block_name = '5' + str(lod_current) + format(moser_de_brujin(grid_x, grid_y), '0>8X')
                new_verts = self.build_block(block_name, lod_current, (grid_x, grid_y))
                lod_verts += new_verts
                # if new_verts:
                #     print(f"Added ({grid_x}, {grid_y}) at level {lod_current} from {block_name}")
                # else:
                #     print(f"No data for {grid_x}, {grid_y} at level {lod_current} (so no {block_name})")
        self.blocks_new_row()
        self.blocks_new_row()

        return lod_verts

    def build_block(self, block_name, lod_current, grid_xy) -> list[bmesh.types.BMVert]:
        # https://zeldamods.org/wiki/Water.extm
        # https://zeldamods.org/wiki/MATE
        byte_structure_mate = '<BBBB'
        byte_entry_size_mate = 4
        file_name_mate = 'map_data/mate/' + block_name + '.mate'
        # https://zeldamods.org/wiki/HGHT
        byte_structure_hght = '<H'
        byte_entry_size_hght = 2
        file_name_hght = 'map_data/terrain/' + block_name + '.hght'
        # https://zeldamods.org/wiki/Grass.extm

        if os.path.isfile(file_name_hght):
            try:
                hfile = open(file_name_hght, 'rb')
                mfile = open(file_name_mate, 'rb')
            except:
                print(f'open {file_name_hght} failed')
                self.blocks_add_entry(None)
                return []
        else:
            # print(f'{file_name_hght} does not exist')
            self.blocks_add_entry(None)
            return []

        # https://docs.python.org/3/library/struct.html
        h_data = struct.unpack(byte_structure_hght, hfile.read(byte_entry_size_hght))
        m_data = struct.unpack(byte_structure_mate, mfile.read(byte_entry_size_mate))
        heights = []
        material0 = []
        material1 = []
        blend_weight = []
        while h_data:
            heights.append(h_data[0])
            material0.append(m_data[0])
            material1.append(m_data[1])
            blend_weight.append(m_data[2])
            try:
                h_data = struct.unpack(byte_structure_hght, hfile.read(byte_entry_size_hght))
                m_data = struct.unpack(byte_structure_mate, mfile.read(byte_entry_size_mate))
            except:
                h_data = None
                m_data = None

        if len(heights) != 65536:
            print('not enough heights?')
            return []

        if len(heights) != len(material0):
            print('height and material mismatch')
            print(len(heights))
            print(len(material0))
            return []

        vertex_x = 0
        vertex_y = 0
        # make verts
        rows = []
        row = []

        block_verts = []
        for index in range(len(heights)):
            height = heights[index]
            if vertex_x > 255:
                vertex_x = 0
                vertex_y += 1
                rows.append(row)
                row = []
            if vertex_y > 255:
                print("this shouldn't happen")
                vertex_y = 0

            world_xy = calc_vert_world_pos(lod_current, grid_xy, (vertex_x, vertex_y))
            location_cache_key = str(world_xy)
            vertex_x += 1
            if location_cache_key in self.bvert_location_cache:
                if self.bvert_location_cache[location_cache_key] == False:
                    self.bvert_location_cache[location_cache_key] = True
                else:
                    row.append(None)
                    continue
            else:
                self.bvert_location_cache[location_cache_key] = True

            bvert = self.bm.verts.new((
                world_xy[0],
                world_xy[1],
                height * HEIGHT_SCALE
            ))
            block_verts.append(bvert)
            row.append(bvert)

            mat0 = index_mapping[material0[index]]
            mat1 = index_mapping[material1[index]]
            blen = blend_weight[index]
            self.bvert_material_table[bvert] = [
                float(mat0)/83,
                float(mat1)/83,
                blen
            ]

        rows.append(row)
        self.blocks_add_entry(rows)

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
                self.make_a_face(face_verts)

            previous_row = row

        hfile.close()
        mfile.close()

        return block_verts

    def merge_blocks(self):
        """Connect the faces of adjacent blocks."""
        blocks = self.blocks
        block_row_len = len(blocks[0])
        for block_index in range(block_row_len):
            # print(f'block_index {block_index}')
            block = blocks[0][block_index]
            if not block:
                continue

            block_below = None
            if len(blocks) > 1 and len(blocks[1]) == len(blocks[0]):
                block_below = blocks[1][block_index]
            # print(f'block_below {bool(block_below)}')
            if block_below:
                # print(len(block[0]))
                # print(len(block[-1]))
                max_range = min(len(block[-1])-1, len(block_below[0])-1)
                for bvert_index in range(max_range):
                    face_verts = [
                        block[-1][bvert_index],
                        block[-1][bvert_index+1],
                        block_below[0][bvert_index+1],
                        block_below[0][bvert_index],
                    ]
                    if None in face_verts:
                        continue
                    self.make_a_face(face_verts)

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
                    self.make_a_face(face_verts)

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
                    self.make_a_face(face_verts)

        blocks.pop(0)

    def blocks_add_entry(self, entry):
        self.blocks[-1].append(entry)

    def blocks_new_row(self):
        # print('blocks_new_row')
        # print(len(blocks))
        if len(self.blocks) > 1:
            self.merge_blocks()
        self.blocks.append([])

    def make_a_face(self, face_verts):
        bm = self.bm
        if None in face_verts:
            return None
        new_face = None
        try:
            new_face = bm.faces.new(face_verts)
        except:
            pass
        if not new_face:
            return None

        face_mats0 = []
        face_mats1 = []
        for loop in new_face.loops:
            face_mats0.append(self.bvert_material_table[loop.vert][0])
            face_mats1.append(self.bvert_material_table[loop.vert][1])

        def get_most_common(lst: list):
            return Counter(lst).most_common(1)[0][0]

        face_mat0 = get_most_common(face_mats0)
        face_mat1 = get_most_common(face_mats1)

        for index, loop in enumerate(new_face.loops):
            # rgba
            make_color = [
                self.bvert_material_table[loop.vert][0],
                self.bvert_material_table[loop.vert][1],
                self.bvert_material_table[loop.vert][2],
                1
            ]
            loop[self.color_layer] = make_color
            m0uv = (face_mat0 + vadd[index][0], vadd[index][1])
            m1uv = (face_mat1 + vadd[index][0], vadd[index][1])
            loop[self.uv_lay0].uv = m0uv
            loop[self.uv_lay1].uv = m1uv

    def connect_lod_borders(self, lod, lod_borders):
        border_1 = lod_borders[lod]
        border_2 = lod_borders[lod+1]
        border_3 = None
        if lod < 7:
            if len(lod_borders[lod+2]) > 0:
                border_3 = lod_borders[lod+2]
        if not border_2 or not border_1:
            print(f"connect_lod_borders {lod} input sanitization did not pass")
            return
        dist_1 = vert_dist(lod)
        # dist_2 = vert_dist(lod+1)
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
                self.make_a_face(face_verts)


def build_map(map_section, lod_level) -> bmesh.types.BMesh:
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

    builder = TerrainBuilder(map_section)
    builder.build(lod_level)

    print('\n\n')

    return builder.bm


def apply_terrain_mat(object: bpy.types.Object):
    terrain_mat_name = 'BotW_Terrain_Map'
    terrain_mat = bpy.data.materials.get(terrain_mat_name)
    if not terrain_mat:
        # import the terrain map material from linked.blend
        append_directory = Path(f"linked_resources\\linked.blend").absolute()
        append_directory = f'{str(append_directory)}\\Material\\'
        files = [{'name': terrain_mat_name}]
        bpy.ops.wm.append(directory=append_directory, files=files, link=True, instance_collections=True)
        terrain_mat = bpy.data.materials.get(terrain_mat_name)
    object.active_material = terrain_mat


def main():
    if not os.path.isdir('map_data'):
        print('No map_data found')
        return

    print("Enter map section (A-1 through J-8): ")
    map_section = input().upper()
    print("Enter detail level (1-8, recommended: 4, 5, or 6): ")
    lod_level = int(input())
    bmesh = build_map(map_section, lod_level)
    map_name = f'terrain_map {map_section}'
    map_object = add_map_to_scene(map_name, bmesh)
    apply_terrain_mat(map_object)
    save_path = Path(f"asset_library\\{map_name}.blend").absolute()
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
