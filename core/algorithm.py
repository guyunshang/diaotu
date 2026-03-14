import time
import random
import traceback
import numpy as np
import osmnx as ox
import networkx as nx
import osmnx.distance as ox_dist
from shapely.geometry import Point, LineString
from shapely.ops import substring
from PyQt6.QtCore import QThread, pyqtSignal


class MD_CVRP_Worker(QThread):
    progress_output = pyqtSignal(str)
    iteration_finished = pyqtSignal(int, float)
    calculation_done = pyqtSignal(dict)

    def __init__(self, G, active_depots, active_customers, params):
        super().__init__()
        self.G = G
        self.depots_pos = [d['pos'] for d in active_depots]
        self.customers_pos = [c['pos'] for c in active_customers]
        self.coords = self.depots_pos + self.customers_pos
        self.num_depots = len(self.depots_pos)
        self.demand = [0.0] * self.num_depots + [c['demand'] for c in active_customers]

        self.CAPACITY = params.get('CAPACITY', 8)
        self.DISTANCE = params.get('DISTANCE', 30000)
        self.C0 = params.get('C0', 100)
        self.C1 = params.get('C1', 1)

        # 纯算法参数：从前端配置读取
        self.birdNum = int(params.get('birdNum', 50))
        self.iterMax = int(params.get('iterMax', 120))
        self.mutation_rate = float(params.get('mutation_rate', 0.12))
        self.local_search_prob = float(params.get('local_search_prob', 0.3))
        self.w_initial = float(params.get('w_initial', 0.9))
        self.w_final = float(params.get('w_final', 0.5))
        self.c1 = float(params.get('c1', 1.5))
        self.c2 = float(params.get('c2', 1.5))

    def run(self):
        try:
            start_time = time.time()
            n = len(self.coords)
            if n < 2 or self.num_depots < 1:
                self.progress_output.emit("❌ 寻优失败：参与点位不足。")
                return

            self.routing_nodes = []
            for i, c in enumerate(self.coords):
                lon, lat = c[1], c[0]
                v_node = f"vnode_{lon:.6f}_{lat:.6f}"

                if v_node not in self.G.nodes:
                    try:
                        u, v, key = ox_dist.nearest_edges(self.G, lon, lat)
                        edge_data = self.G.get_edge_data(u, v, key)

                        if 'geometry' in edge_data:
                            line = edge_data['geometry']
                        else:
                            line = LineString([(self.G.nodes[u]['x'], self.G.nodes[u]['y']),
                                               (self.G.nodes[v]['x'], self.G.nodes[v]['y'])])

                        p = Point(lon, lat)
                        proj_dist = line.project(p)
                        proj_p = line.interpolate(proj_dist)

                        self.G.add_node(v_node, x=proj_p.x, y=proj_p.y)

                        geom_start_to_vnode = substring(line, 0.0, proj_dist)
                        geom_vnode_to_end = substring(line, proj_dist, line.length)

                        start_pt = Point(line.coords[0])
                        dist_u_to_start = start_pt.distance(Point(self.G.nodes[u]['x'], self.G.nodes[u]['y']))
                        dist_v_to_start = start_pt.distance(Point(self.G.nodes[v]['x'], self.G.nodes[v]['y']))

                        if dist_u_to_start < dist_v_to_start:
                            n_start, n_end = u, v
                        else:
                            n_start, n_end = v, u

                        d_start_vnode = ox_dist.great_circle(self.G.nodes[n_start]['y'], self.G.nodes[n_start]['x'],
                                                             proj_p.y, proj_p.x)
                        d_vnode_end = ox_dist.great_circle(self.G.nodes[n_end]['y'], self.G.nodes[n_end]['x'], proj_p.y,
                                                           proj_p.x)

                        self.G.add_edge(n_start, v_node, weight=d_start_vnode, length=d_start_vnode,
                                        geometry=geom_start_to_vnode)
                        self.G.add_edge(v_node, n_start, weight=d_start_vnode, length=d_start_vnode,
                                        geometry=geom_start_to_vnode)
                        self.G.add_edge(v_node, n_end, weight=d_vnode_end, length=d_vnode_end,
                                        geometry=geom_vnode_to_end)
                        self.G.add_edge(n_end, v_node, weight=d_vnode_end, length=d_vnode_end,
                                        geometry=geom_vnode_to_end)
                    except Exception as e:
                        u = ox_dist.nearest_nodes(self.G, X=lon, Y=lat)
                        v_node = u
                self.routing_nodes.append(v_node)

            dis_matrix = np.zeros((n, n))
            path_matrix = {}
            self.progress_output.emit(f"🚀 正在构建 A* 精确路网距离矩阵...")

            def heuristic(u, v):
                return ox_dist.great_circle(self.G.nodes[u]['y'], self.G.nodes[u]['x'],
                                            self.G.nodes[v]['y'], self.G.nodes[v]['x'])

            for i in range(n):
                for j in range(n):
                    if i == j: continue
                    try:
                        path = nx.astar_path(self.G, self.routing_nodes[i], self.routing_nodes[j],
                                             weight='length', heuristic=heuristic)
                        dist = nx.astar_path_length(self.G, self.routing_nodes[i], self.routing_nodes[j],
                                                    weight='length', heuristic=heuristic)
                        dis_matrix[i, j] = dist
                        path_matrix[f"{i}_{j}"] = path
                    except:
                        dis_matrix[i, j] = 1e10
                        path_matrix[f"{i}_{j}"] = []

            birdPop = []
            for _ in range(self.birdNum):
                c_seq = self.greedy_logic(dis_matrix)
                d_assign = [random.randint(0, self.num_depots - 1) for _ in range(len(c_seq))]
                birdPop.append((c_seq, d_assign))

            birdPop_car, fits = self.extended_calFitness(birdPop, self.demand, dis_matrix)
            pBest_fits = fits.copy()
            pLine = birdPop.copy()

            gBest = min(fits)
            gBest_idx = fits.index(gBest)
            gLine = birdPop[gBest_idx]
            gLine_car = birdPop_car[gBest_idx]

            for iterI in range(1, self.iterMax + 1):
                w_current = self.w_initial - (self.w_initial - self.w_final) * (iterI / self.iterMax)

                for i in range(self.birdNum):
                    birdPop[i] = self.extended_crossover(birdPop[i], pLine[i], gLine, w_current)
                    birdPop[i] = self.extended_mutation(birdPop[i])
                    if random.random() < self.local_search_prob:
                        birdPop[i] = self.two_opt_optimization(birdPop[i], self.demand, dis_matrix)

                birdPop_car, fits = self.extended_calFitness(birdPop, self.demand, dis_matrix)

                for i in range(self.birdNum):
                    if fits[i] < pBest_fits[i]:
                        pBest_fits[i] = fits[i]
                        pLine[i] = birdPop[i]

                current_min = min(fits)
                if current_min < gBest:
                    gBest = current_min
                    gBest_idx = fits.index(current_min)
                    gLine = birdPop[gBest_idx]
                    gLine_car = birdPop_car[gBest_idx]

                if iterI % 20 == 0 or iterI == 1:
                    self.progress_output.emit(f"📈 迭代 {iterI}/{self.iterMax}, 当前最优成本: {gBest:.1f}")
                self.iteration_finished.emit(iterI, gBest)

            self.calculation_done.emit({
                "gLine_car": gLine_car,
                "gBest": gBest,
                "path_matrix": path_matrix,
                "time": time.time() - start_time
            })
        except Exception as e:
            error_msg = traceback.format_exc()
            self.progress_output.emit(f"❌ 严重错误！后台计算线程崩溃：\n{error_msg}")

    def greedy_logic(self, dis_matrix):
        dm = dis_matrix.copy().astype('float64')
        n = len(self.coords)
        for i in range(n): dm[i, i] = 1e10
        for i in range(self.num_depots): dm[:, i] = 1e10
        line = []
        rem = list(range(self.num_depots, n))
        now = random.choice(rem)
        line.append(now)
        rem.remove(now)
        dm[:, now] = 1e10
        while rem:
            nxt = min(rem, key=lambda x: dm[now, x])
            line.append(nxt)
            rem.remove(nxt)
            dm[:, nxt] = 1e10
            now = nxt
        return line

    def extended_calFitness(self, birdPop, Demand, dis_matrix):
        birdPop_car, fits = [], []
        for customer_sequence, depot_assignments in birdPop:
            lines, cur_route = [], []
            cur_depot, cur_load, cur_dist, last_point = None, 0, 0, None
            depot_idx, assigned_customers = 0, set()
            i = 0
            while i < len(customer_sequence):
                customer = customer_sequence[i]
                if not cur_route:
                    cur_depot = depot_assignments[depot_idx] if depot_idx < len(depot_assignments) else 0
                    depot_idx += 1

                    d_go = dis_matrix[cur_depot, customer]
                    d_back = dis_matrix[customer, cur_depot]

                    if Demand[customer] <= self.CAPACITY and d_go + d_back <= self.DISTANCE:
                        cur_route = [cur_depot, customer]
                        cur_load, cur_dist, last_point = Demand[customer], d_go, customer
                        assigned_customers.add(customer)
                    i += 1
                else:
                    d_next = dis_matrix[last_point, customer]
                    d_back = dis_matrix[customer, cur_depot]
                    if cur_load + Demand[customer] <= self.CAPACITY and cur_dist + d_next + d_back <= self.DISTANCE:
                        cur_route.append(customer)
                        cur_load += Demand[customer]
                        cur_dist += d_next
                        last_point = customer
                        assigned_customers.add(customer)
                        i += 1
                    else:
                        cur_route.append(cur_depot)
                        lines.append(cur_route)
                        cur_route, cur_load, cur_dist, last_point = [], 0, 0, None
            if cur_route:
                if cur_route[-1] != cur_depot: cur_route.append(cur_depot)
                lines.append(cur_route)

            penalty = len(set(range(self.num_depots, len(Demand))) - assigned_customers) * 10000
            total_d = sum(dis_matrix[r[j], r[j + 1]] for r in lines for j in range(len(r) - 1))
            fits.append(round(self.C0 * len(lines) + self.C1 * total_d + penalty, 1))
            birdPop_car.append(lines)
        return birdPop_car, fits

    def extended_crossover(self, bird, pLine, gLine, w):
        c_seq, d_assign = bird
        p_c, p_d = pLine
        g_c, g_d = gLine
        r = random.uniform(0, w + self.c1 + self.c2)
        p2_c, p2_d = (c_seq[::-1], d_assign[::-1]) if r <= w else (p_c, p_d) if r <= w + self.c1 else (g_c, g_d)

        croC = [None] * len(c_seq)
        s, e = sorted([random.randint(0, len(c_seq) - 1) for _ in range(2)])
        croC[s:e + 1] = c_seq[s:e + 1]
        rem_p2 = [x for x in p2_c if x not in croC]
        ptr = 0
        for k in range(len(croC)):
            if croC[k] is None: croC[k] = rem_p2[ptr]; ptr += 1
        croD = [d1 if random.random() < 0.5 else d2 for d1, d2 in zip(d_assign, p2_d)]
        return (croC, croD)

    def extended_mutation(self, bird):
        c, d = list(bird[0]), list(bird[1])
        if random.random() < self.mutation_rate:
            i1, i2 = random.sample(range(len(c)), 2)
            c[i1], c[i2] = c[i2], c[i1]
        if random.random() < self.mutation_rate:
            d[random.randint(0, len(d) - 1)] = random.randint(0, self.num_depots - 1)
        return (c, d)

    def two_opt_optimization(self, bird, Demand, dis_matrix):
        best_c, best_d = list(bird[0]), list(bird[1])
        _, f = self.extended_calFitness([(best_c, best_d)], Demand, dis_matrix)
        best_f = f[0]
        improved = True
        for _ in range(50):
            if not improved: break
            improved = False
            for i in range(len(best_c)):
                for j in range(i + 2, len(best_c)):
                    new_c = best_c[:i] + best_c[i:j + 1][::-1] + best_c[j + 1:]
                    _, nf = self.extended_calFitness([(new_c, best_d)], Demand, dis_matrix)
                    if nf[0] < best_f:
                        best_c, best_f = new_c, nf[0]
                        improved = True
                        break
                if improved: break
        return (best_c, best_d)

