from abc import ABC, abstractmethod
import numpy as np
import networkx as nx
from graph_utils import LStringGraphDual
import curves
from collections import defaultdict

class PruningStrategy:

    @abstractmethod
    def apply_strategy(self, **kwargs):
        ...

    def viz(self):
        return []


class UFOPruningStrategy(PruningStrategy):
    def __init__(self, trunk_targets, wire_walls, **params):
        self.trunk_targets = trunk_targets
        self.wire_walls = wire_walls
        self.params = params
        self.tree = None

        # State variables
        self.next_trunk_target = 0
        self.leaders_assigned = False

    def set_tree(self, tree: LStringGraphDual):
        self.tree = tree

    def __getitem__(self, item):
        return self.params[item]

    def apply_strategy(self, **kwargs):
        self.tie_down_trunk()
        self.examine_leaders()

    def tie_down_trunk(self):
        if not self.next_trunk_target < len(self.trunk_targets):
            return

        target = self.trunk_targets[self.next_trunk_target]
        root_branch = self.tree.search_branches('generation', '=', 0, assert_unique=True)
        nodes = self.tree.branches[root_branch]['nodes']

        # Find the last internode that was tied down and return nodes for all subsequent internodes
        last_tie_idx = 0
        for idx, (n1, n2) in enumerate(zip(nodes[:-1], nodes[1:])):
            data = dict(self.tree.graph.edges[n1, n2].get('post_modules', []))
            if 1 in data.get('Flags', []):
                last_tie_idx = idx + 1
        print('Index of last tie: {}'.format(last_tie_idx))
        nodes = nodes[last_tie_idx:]
        pts = self.tree.branches[root_branch]['points'][last_tie_idx:]

        offsets = np.linalg.norm(pts[:-1] - pts[1:], axis=1)
        cumul_lens = np.cumsum(offsets)
        length = cumul_lens[-1]
        if length > 0:
            _, dist_to_target, _, target_pt = target.get_point_sequence_dist(pts)
            if length > dist_to_target + np.linalg.norm(target_pt - pts[0]):
                params, _, rez = curves.run_cubic_bezier_strain_opt([pts[0], target_pt],
                                                                    pts[2 if nodes[0] == 0 else 1] - pts[0], 1)
                if rez.success:
                    print('Tying to Target {}'.format(self.next_trunk_target))
                    curve_pts = curves.CubicBezier(*params[0]).eval(np.linspace(0, 1, 10))
                    print(curve_pts)
                    curve_len = np.sum(np.linalg.norm(curve_pts[:-1] - curve_pts[1:], axis=1))

                    tie_down_node_idx = (np.argmax(cumul_lens > curve_len) or len(nodes) - 2) + 1
                    self.tree.set_guide_on_nodes(nodes[:tie_down_node_idx + 1], curve_pts, 'GlobalGuide')

                    self.next_trunk_target += 1

    def examine_leaders(self):
        root_branch = self.tree.search_branches('generation', '=', 0, assert_unique=True)
        branch = self.tree.branches[root_branch]
        if branch['length'] < 5:
            self.rub_off_trunk_buds()

        self.assign_leaders()

    def rub_off_trunk_buds(self):
        pass

    def find_module(self, module, edge):
        pre_mods = self.tree.graph.edges[edge].get('pre_modules', [])
        flags = None
        for mod in pre_mods:
            if mod[0] == module:
                flags = mod[1]
                break
        return flags


    def assign_leaders(self):
        first_gen_branches = self.tree.search_branches('generation', '=', 1)
        # Determine the status of each branch - Unassigned and untied (0), assigned but untied (1), tied (2)
        tied_nodes = defaultdict(list)

        for branch_id in first_gen_branches:
            branch = self.tree.branches[branch_id]
            nodes = branch['nodes']

            # Iterate through edges in reverse order and see if there are any ties
            last_tie = None
            for edge in zip(nodes[-2:1:-1], nodes[-1:2:-1]):
                tie_info = self.find_module('Tie', edge)
                if tie_info is not None:
                    last_tie = edge
                    wall_id, wall_tie_idx = tie_info
                    tied_nodes[wall_id] = nodes[0]

                    self.manage_tied_branch(nodes[nodes.index(edge[1]):], wall_id, wall_tie_idx)

            if last_tie is not None:
                # Tie down the branches
                pass

            mark = self.find_module('Mark', (nodes[0], nodes[1]))
            if mark is not None:
                wall_id = mark[0]
                tied_nodes[wall_id].append(nodes[0])

        # Check to see if all leaders have been assigned to each wall
        leaders_per_wall = self.params.get('leaders_per_wall', 5)
        is_full = True
        for wall_id in range(self.wire_walls):
            trunk_nodes = tied_nodes[wall_id]
            if len(trunk_nodes) < leaders_per_wall:
                is_full = False


        # If there is room, search for new buds that can be added to each wall
        if not is_full:
            desired_spacing = self.params.get('leader_spacing', 0.9)
            root_branch_id = self.tree.search_branches('generation', '=', 0, assert_unique=True)
            root_branch = self.tree.branches[root_branch_id]
            root_nodes = root_branch['nodes']
            pts = root_branch['points']
            dists = pts[:-1] - pts[1:]
            cum_dists = np.cumsum(dists)

            # For each wall, convert the root nodes to spacings along the trunk
            wall_spacings = {}
            all_tied_nodes = set()
            for wall_id, trunk_nodes in tied_nodes.items():
                spacings = [cum_dists[root_nodes.index[n] - 1] for n in trunk_nodes]  # Inefficient
                wall_spacings[wall_id] = sorted(spacings)
                all_tied_nodes.update(trunk_nodes)

            # TODO: This iteration needs to be over all bud internodes that are connected to the trunk
            for node, cum_dist in zip(root_nodes[1:], cum_dists):
                if node in all_tied_nodes or cum_dist < self.params.get('trunk_bare_dist', 5):
                    continue

                best_spacing = None
                best_wall_id = None
                for wall_id, spacings in wall_spacings.items():
                    if len(spacings) >= leaders_per_wall:
                        continue
                    spacing = query_dist_from_line_points(cum_dist, spacings)
                    if spacing < desired_spacing:
                        continue

                    if best_spacing is None or spacing < best_spacing:
                        best_spacing, best_wall_id = spacing, wall_id

                if best_wall_id is not None:

                    # [TODO] Flag the desired bud internode  - self.tree.graph.edges[edge]['pre_modules'] = [('Mark', [wall_id])]
                    wall_spacings[best_wall_id].append(cum_dist)
                    wall_spacings[best_wall_id].sort()

    def manage_tied_branch(self, nodes, wall_id, wall_tie_idx):
        if wall_tie_idx == len(self.wire_walls[wall_id]) - 1:
            self.tree.stub_branch(nodes, self.params.get('leader_excess_stub', 2.5))
        else:
            base_node = nodes[0]
            next_target = self.wire_walls[wall_id][wall_tie_idx + 1]
            base_pt = self.graph.nodes[nodes[0]]['point']
            next_pt = self.graph.nodes[nodes[1]]['point']
            vec = next_pt - base_pt
            vec = np.linalg.norm(vec)
            # Hack
            _, _,  wire_target_pt = next_target.get_segment_dist(base_pt, base_pt + vec * 0.001)

            # Do tying


    def manage_untied_branch(self, nodes, wall_id):
        pass


def query_dist_from_line_points(pt, sorted_array):
    if not len(sorted_array):
        return np.inf

    if pt < sorted_array[0]:
        return sorted_array[0] - pt

    elif pt >= sorted_array[-1]:
        return pt - sorted_array[-1]

    else:
        for l, u in zip(sorted_array[:-1], sorted_array[1:]):
            if l <= pt < u:
                return min(pt-l, u-pt)