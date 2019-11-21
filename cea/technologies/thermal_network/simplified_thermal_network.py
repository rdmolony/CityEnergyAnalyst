from __future__ import division

import math

import geopandas as gpd
import numpy as np
import pandas as pd
import wntr

import time
import cea.config
import cea.inputlocator
import cea.technologies.substation as substation
from cea.constants import P_WATER_KGPERM3
from cea.optimization.preprocessing.preprocessing_main import get_building_names_with_load
from cea.optimization.constants import PUMP_ETA
from cea.technologies.constants import NETWORK_DEPTH
from cea.utilities.epwreader import epw_reader
from cea.resources import geothermal


__author__ = "Jimeno A. Fonseca"
__copyright__ = "Copyright 2019, Architecture and Building Systems - ETH Zurich"
__credits__ = ["Jimeno A. Fonseca"]
__license__ = "MIT"
__version__ = "0.1"
__maintainer__ = "Daren Thomas"
__email__ = "cea@arch.ethz.ch"
__status__ = "Production"

def calculate_ground_temperature(locator):
    """
    calculate ground temperatures.

    :param locator:
    :return: list of ground temperatures, one for each hour of the year
    :rtype: list[np.float64]
    """
    weather_file = locator.get_weather_file()
    T_ambient_C = epw_reader(weather_file)['drybulb_C']
    network_depth_m = NETWORK_DEPTH  # [m]
    T_ground_K = geothermal.calc_ground_temperature(locator, T_ambient_C.values, network_depth_m)
    return T_ground_K


def extract_network_from_shapefile(edge_shapefile_df, node_shapefile_df):
    """
    Extracts network data into DataFrames for pipes and nodes in the network

    :param edge_shapefile_df: DataFrame containing all data imported from the edge shapefile
    :param node_shapefile_df: DataFrame containing all data imported from the node shapefile
    :type edge_shapefile_df: DataFrame
    :type node_shapefile_df: DataFrame
    :return node_df: DataFrame containing all nodes and their corresponding coordinates
    :return edge_df: list of edges and their corresponding lengths and start and end nodes
    :rtype node_df: DataFrame
    :rtype edge_df: DataFrame

    """
    # set precision of coordinates
    decimals = 6
    # create node dictionary with plant and consumer nodes
    node_dict = {}
    node_shapefile_df.set_index("Name", inplace=True)
    node_shapefile_df = node_shapefile_df.astype('object')
    node_shapefile_df['coordinates'] = node_shapefile_df['geometry'].apply(lambda x: x.coords[0])
    # sort node_df by index number
    node_sorted_index = node_shapefile_df.index.to_series().str.split('NODE', expand=True)[1].apply(int).sort_values(
        ascending=True)
    node_shapefile_df = node_shapefile_df.reindex(index=node_sorted_index.index)


    for node, row in node_shapefile_df.iterrows():
        coord_node = row['geometry'].coords[0]
        coord_node_round = (round(coord_node[0], decimals), round(coord_node[1], decimals))
        node_dict[coord_node_round] = node

    # create edge dictionary with pipe lengths and start and end nodes
    # complete node dictionary with missing nodes (i.e., joints)
    edge_shapefile_df.set_index("Name", inplace=True)
    edge_shapefile_df = edge_shapefile_df.astype('object')
    edge_shapefile_df['coordinates'] = edge_shapefile_df['geometry'].apply(lambda x: x.coords[0])
    # sort edge_df by index number
    edge_sorted_index = edge_shapefile_df.index.to_series().str.split('PIPE', expand=True)[1].apply(int).sort_values(
        ascending=True)
    edge_shapefile_df = edge_shapefile_df.reindex(index=edge_sorted_index.index)
    # assign edge properties
    edge_shapefile_df['start node'] = ''
    edge_shapefile_df['end node'] = ''

    for pipe, row in edge_shapefile_df.iterrows():
        # get the length of the pipe and add to dataframe
        edge_coords = row['geometry'].coords
        edge_shapefile_df.loc[pipe, 'length_m'] = row['geometry'].length
        start_node = (round(edge_coords[0][0], decimals), round(edge_coords[0][1], decimals))
        end_node = (round(edge_coords[1][0], decimals), round(edge_coords[1][1], decimals))
        if start_node in node_dict.keys():
            edge_shapefile_df.loc[pipe, 'start node'] = node_dict[start_node]
        else:
            print('The start node of ', pipe, 'has no match in node_dict, check precision of the coordinates.')
        if end_node in node_dict.keys():
            edge_shapefile_df.loc[pipe, 'end node'] = node_dict[end_node]
        else:
            print('The end node of ', pipe, 'has no match in node_dict, check precision of the coordinates.')

    return node_shapefile_df, edge_shapefile_df


def get_thermal_network_from_shapefile(locator, network_type, network_name):
    """
    This function reads the existing node and pipe network from a shapefile and produces an edge-node incidence matrix
    (as defined by Oppelt et al., 2016) as well as the edge properties (length, start node, and end node) and node
    coordinates.
    """

    # import shapefiles containing the network's edges and nodes
    network_edges_df = gpd.read_file(locator.get_network_layout_edges_shapefile(network_type, network_name))
    network_nodes_df = gpd.read_file(locator.get_network_layout_nodes_shapefile(network_type, network_name))

    # check duplicated NODE/PIPE IDs
    duplicated_nodes = network_nodes_df[network_nodes_df.Name.duplicated(keep=False)]
    duplicated_edges = network_edges_df[network_edges_df.Name.duplicated(keep=False)]
    if duplicated_nodes.size > 0:
        raise ValueError('There are duplicated NODE IDs:', duplicated_nodes)
    if duplicated_edges.size > 0:
        raise ValueError('There are duplicated PIPE IDs:', duplicated_nodes)

    # get node and pipe information
    node_df, edge_df = extract_network_from_shapefile(network_edges_df, network_nodes_df)

    return edge_df, node_df


def calc_max_diameter(volume_flow_m3s, pipe_catalog, velocity_ms, peak_load_percentage):
    volume_flow_m3s_corrected_to_design = volume_flow_m3s * peak_load_percentage / 100
    diameter_m = math.sqrt((volume_flow_m3s_corrected_to_design / velocity_ms) * (4 / math.pi))
    slection_of_catalog = pipe_catalog.ix[(pipe_catalog['D_int_m'] - diameter_m).abs().argsort()[:1]]
    D_int_m = slection_of_catalog['D_int_m'].values[0]
    Pipe_DN = slection_of_catalog['Pipe_DN'].values[0]
    D_ext_m = slection_of_catalog['D_ext_m'].values[0]
    D_ins_m = slection_of_catalog['D_ins_m'].values[0]

    return Pipe_DN, D_ext_m, D_int_m, D_ins_m


def calc_head_loss_m(diamter_m, max_volume_flow_rates_m3s, coefficient_friction, length_m):
    hf_L = (10.67 / (coefficient_friction ** 1.85)) * (max_volume_flow_rates_m3s ** 1.852) / (diamter_m ** 4.8704)
    head_loss_m = hf_L * length_m
    return head_loss_m


def calc_linear_thermal_loss_coefficient(diamter_ext_m, diamter_int_m, diameter_insulation_m):
    r_out_m = diamter_ext_m / 2
    r_in_m = diamter_int_m / 2
    r_s_m = diameter_insulation_m / 2
    k_pipe_WmK = 58.7  # steel pipe
    k_ins_WmK = 0.059  # scalcium silicate insulation
    resistance_KmperW = ((math.log(r_out_m / r_in_m) / k_pipe_WmK) + (math.log(r_s_m / r_out_m) / k_ins_WmK))
    K_WperKm = 2 * math.pi / resistance_KmperW
    return K_WperKm

def thermal_network_simplified(locator, config, network_name):


    #local variables
    network_type = config.thermal_network.network_type
    thermal_transfer_unit_design_head_m = config.thermal_network.min_head_susbstation  * 9.8
    coefficient_friction_hanzen_williams = config.thermal_network.hw_friction_coefficient
    velocity_ms = config.thermal_network.peak_load_velocity
    fraction_equivalent_length = config.thermal_network.equivalent_length_factor
    peak_load_percentage = config.thermal_network.peak_load_percentage

    # GET INFORMATION ABOUT THE NETWORK
    edge_df, node_df = get_thermal_network_from_shapefile(locator, network_type, network_name)

    # GET INFORMATION ABOUT THE DEMAND OF BUILDINGS AND CONNECT TO THE NODE INFO
    # calculate substations for all buildings
    # local variables
    total_demand = pd.read_csv(locator.get_total_demand())
    volume_flow_m3pers_building = pd.DataFrame()
    T_sup_K_building = pd.DataFrame()
    T_re_K_building = pd.DataFrame()
    Q_demand_kWh_building = pd.DataFrame()
    if network_type == "DH":
        buildings_name_with_heating = get_building_names_with_load(total_demand, load_name='QH_sys_MWhyr')
        buildings_name_with_space_heating = get_building_names_with_load(total_demand, load_name='Qhs_sys_MWhyr')
        DHN_barcode = "111111thermalnetwork"
        if (buildings_name_with_heating != [] and buildings_name_with_space_heating != []):
            building_names = buildings_name_with_heating
            substation.substation_main_heating(locator, total_demand, building_names, DHN_barcode=DHN_barcode)
        else:
            raise Exception('problem here')

        for building_name in building_names:
            substation_results = pd.read_csv(
                locator.get_optimization_substations_results_file(building_name, "DH", DHN_barcode))
            volume_flow_m3pers_building[building_name] = substation_results["mdot_DH_result_kgpers"] / P_WATER_KGPERM3
            T_sup_K_building[building_name] = substation_results["T_supply_DH_result_K"]
            T_re_K_building[building_name] = substation_results["T_return_DH_result_K"]
            Q_demand_kWh_building[building_name] = (substation_results["Q_heating_W"] + substation_results[
                "Q_dhw_W"]) / 1000

    if network_type == "DC":
        buildings_name_with_cooling = get_building_names_with_load(total_demand, load_name='QC_sys_MWhyr')
        DCN_barcode = "111111thermalnetwork"
        if buildings_name_with_cooling != []:
            building_names = buildings_name_with_cooling
            substation.substation_main_cooling(locator, total_demand, building_names, DCN_barcode=DCN_barcode)
        else:
            raise Exception('problem here')

        for building_name in building_names:
            substation_results = pd.read_csv(
                locator.get_optimization_substations_results_file(building_name, "DC", DCN_barcode))
            volume_flow_m3pers_building[building_name] = substation_results[
                                                             "mdot_space_cooling_data_center_and_refrigeration_result_kgpers"] / P_WATER_KGPERM3
            T_sup_K_building[building_name] = substation_results[
                "T_return_DC_space_cooling_data_center_and_refrigeration_result_K"]
            T_re_K_building[building_name] = substation_results[
                "T_return_DC_space_cooling_data_center_and_refrigeration_result_K"]
            Q_demand_kWh_building[building_name] = substation_results[
                                                       "Q_space_cooling_data_center_and_refrigeration_W"] / 1000

    # Create a water network model
    import os
    os.chdir(locator.get_thermal_network_folder())
    wn = wntr.network.WaterNetworkModel()

    # add loads
    building_base_demand_m3s = {}
    for building in volume_flow_m3pers_building.keys():
        building_base_demand_m3s[building] = volume_flow_m3pers_building[building].max()
        pattern_demand = (volume_flow_m3pers_building[building].values / building_base_demand_m3s[building]).tolist()
        wn.add_pattern(building, pattern_demand)

    # add nodes
    consumer_nodes = []
    building_nodes_pairs = {}
    building_nodes_pairs_inversed = {}
    for node in node_df.iterrows():
        if node[1]["Type"] == "CONSUMER":
            demand_pattern = node[1]['Building']
            base_demand_m3s = building_base_demand_m3s[demand_pattern]
            consumer_nodes.append(node[0])
            building_nodes_pairs[node[0]] = demand_pattern
            building_nodes_pairs_inversed[demand_pattern] = node[0]
            wn.add_junction(node[0],
                            base_demand=base_demand_m3s,
                            demand_pattern=demand_pattern,
                            elevation=thermal_transfer_unit_design_head_m,
                            coordinates=node[1]["coordinates"])
        elif node[1]["Type"] == "PLANT":
            base_head = 1
            start_node = node[0]
            name_node_plant = start_node
            wn.add_reservoir(start_node,
                             base_head=base_head,
                             coordinates=node[1]["coordinates"])
        else:
            wn.add_junction(node[0],
                            elevation=0,
                            coordinates=node[1]["coordinates"])

    # add pipes
    for edge in edge_df.iterrows():
        length = edge[1]["length_m"]
        edge_name = edge[0]
        wn.add_pipe(edge_name, edge[1]["start node"],
                    edge[1]["end node"],
                    length=length * (1+fraction_equivalent_length),
                    roughness=coefficient_friction_hanzen_williams,
                    minor_loss=0.0,
                    status='OPEN')

    # add options
    wn.options.time.duration = 8759 * 3600
    wn.options.time.hydraulic_timestep = 60 * 60
    wn.options.time.pattern_timestep = 60 * 60

    # 1st ITERATION GET MASS FLOWS AND CALCULATE DIAMETER
    sim = wntr.sim.EpanetSimulator(wn)
    results = sim.run_sim()
    max_volume_flow_rates_m3s = results.link['flowrate'].abs().max()
    pipe_names = max_volume_flow_rates_m3s.index.values
    pipe_catalog = pd.read_excel(locator.get_database_supply_systems(), sheet_name='PIPING')
    Pipe_DN, D_ext_m, D_int_m, D_ins_m = zip(
        *[calc_max_diameter(flow, pipe_catalog, velocity_ms=velocity_ms, peak_load_percentage=peak_load_percentage) for flow in max_volume_flow_rates_m3s])
    pipe_dn = pd.Series(Pipe_DN, pipe_names)
    diameter_int_m = pd.Series(D_int_m, pipe_names)
    diameter_ext_m = pd.Series(D_ext_m, pipe_names)
    diameter_ins_m = pd.Series(D_ins_m, pipe_names)

    # 2nd ITERATION GET PRESSURE POINTS AND MASSFLOWS FOR SIZING PUMPING NEEDS - this could be for all the year
    # modify diameter and run simualtions
    edge_df['Pipe_DN'] = pipe_dn
    for edge in edge_df.iterrows():
        edge_name = edge[0]
        pipe = wn.get_link(edge_name)
        pipe.diameter = diameter_int_m[edge_name]
    sim = wntr.sim.EpanetSimulator(wn)
    results = sim.run_sim()

    # 3d ITERATION GET FINAL UTILIZATION OF THE GRID (SUPPLY SIDE)
    # get accumulated heat loss per hour
    head_loss_substations_ft = results.node['head'][consumer_nodes].abs()
    head_loss_substations_m = head_loss_substations_ft * 0.30487
    unitary_head_ftperkft = results.link['headloss'].abs()
    unitary_head_mperm = unitary_head_ftperkft * 0.30487 / 304.87
    head_loss_m = unitary_head_mperm.copy()
    for column in head_loss_m.columns.values:
        length = edge_df.loc[column]['length_m']
        head_loss_m[column] = head_loss_m[column] * length
    reservoir_head_loss_m = head_loss_m.sum(axis=1) + head_loss_substations_m.sum(axis=1)

    # apply this pattern to the reservoir and get results
    base_head = reservoir_head_loss_m.max()
    pattern_head_m = (reservoir_head_loss_m.values / base_head).tolist()
    wn.add_pattern('reservoir', pattern_head_m)
    reservoir = wn.get_node(name_node_plant)
    reservoir.head_timeseries.base_value = int(base_head)
    reservoir.head_timeseries._pattern = 'reservoir'
    sim = wntr.sim.EpanetSimulator(wn)
    results = sim.run_sim()

    # $ POSPROCESSING - MASSFLOWRATES PER PIPE PER HOUR OF THE YEAR
    flow_rate_supply_m3s = results.link['flowrate'].abs()
    massflow_supply_kgs = flow_rate_supply_m3s * P_WATER_KGPERM3

    # $ POSPROCESSING - MASSFLOWRATES PER NODE PER HOUR OF THE YEAR
    flow_rate_substations_m3s = results.node['demand'][consumer_nodes].abs()
    massflow_substations_kgs = flow_rate_substations_m3s * P_WATER_KGPERM3

    # $ POSPROCESSING - PRESSURE/HEAD LOSSES PER PIPE PER HOUR OF THE YEAR
    # at the pipes
    unitary_head_loss_supply_network_ftperkft = results.link['headloss'].abs()
    unitary_head_loss_supply_network_Paperm = unitary_head_loss_supply_network_ftperkft * 2989.0669 / 304.87
    head_loss_supply_network_Pa = unitary_head_loss_supply_network_Paperm.copy()
    for column in head_loss_supply_network_Pa.columns.values:
        length = edge_df.loc[column]['length_m']
        head_loss_supply_network_Pa[column] = head_loss_supply_network_Pa[column] * length

    head_loss_return_network_Pa = head_loss_supply_network_Pa.copy(0)
    # at the substations
    head_loss_substations_ft = results.node['head'][consumer_nodes].abs()
    head_loss_substations_Pa = head_loss_substations_ft * (2989.0669)

    # $ POSPROCESSING - PRESSURE LOSSES ACCUMUALTED PER HOUR OF THE YEAR (TIMES 2 to account for return)
    accumulated_head_loss_supply_Pa = head_loss_supply_network_Pa.sum(axis=1)
    accumulated_head_loss_return_Pa = head_loss_return_network_Pa.sum(axis=1)
    accumulated_head_loss_substations_Pa = head_loss_substations_Pa.sum(axis=1)
    accumulated_head_loss_total_Pa = accumulated_head_loss_supply_Pa + accumulated_head_loss_return_Pa + accumulated_head_loss_substations_Pa

    # $ POSPROCESSING - PUMPING NEEDS PER HOUR OF THE YEAR (TIMES 2 to account for return)
    head_loss_supply_kWperm = (unitary_head_loss_supply_network_Paperm * (flow_rate_supply_m3s * 3600)) / (
                3.6E6 * PUMP_ETA)
    head_loss_return_kWperm = head_loss_supply_kWperm.copy()
    head_loss_supply_kW = (head_loss_supply_network_Pa * (flow_rate_supply_m3s * 3600)) / (3.6E6 * PUMP_ETA)
    head_loss_return_kW = head_loss_supply_kW.copy()
    head_loss_substations_kW = (head_loss_substations_Pa * (flow_rate_substations_m3s * 3600)) / (3.6E6 * PUMP_ETA)
    accumulated_head_loss_supply_kW = head_loss_supply_kW.sum(axis=1)
    accumulated_head_loss_return_kW = head_loss_return_kW.sum(axis=1)
    accumulated_head_loss_substations_kW = head_loss_substations_kW.sum(axis=1)
    accumulated_head_loss_total_kW = accumulated_head_loss_supply_kW + accumulated_head_loss_return_kW + accumulated_head_loss_substations_kW

    # $ POSPROCESSING - THERMAL LOSSES PER PIPE PER HOUR OF THE YEAR (SUPPLY)
    # calculate the thermal characteristics of the grid
    temperature_of_the_ground_K = calculate_ground_temperature(locator)
    thermal_coeffcient_WperKm = pd.Series(
        np.vectorize(calc_linear_thermal_loss_coefficient)(diameter_ext_m, diameter_int_m, diameter_ins_m), pipe_names)
    temperature_supply = T_sup_K_building.max(axis=1)
    delta_T_in_out_K = temperature_supply - temperature_of_the_ground_K

    thermal_losses_supply_kWh = results.link['headloss'].copy()
    thermal_losses_supply_kWh.reset_index(inplace=True, drop=True)
    for pipe in pipe_names:
        length = edge_df.loc[pipe]['length_m']
        k_WperKm_pipe = thermal_coeffcient_WperKm[pipe]
        thermal_losses_supply_kWh[pipe] = delta_T_in_out_K * k_WperKm_pipe * length / 1000

    # retutn pipes
    average_temperature_return = T_re_K_building.mean(axis=1)
    delta_T_in_out_K = average_temperature_return - temperature_of_the_ground_K

    thermal_losses_return_kWh = results.link['headloss'].copy()
    thermal_losses_return_kWh.reset_index(inplace=True, drop=True)
    for pipe in pipe_names:
        length = edge_df.loc[pipe]['length_m']
        k_WperKm_pipe = thermal_coeffcient_WperKm[pipe]
        thermal_losses_return_kWh[pipe] = delta_T_in_out_K * k_WperKm_pipe * length / 1000

    # total
    thermal_losses_kWh = thermal_losses_supply_kWh + thermal_losses_return_kWh

    # $ POSPROCESSING - PLANT HEAT REQUIREMENT
    if network_type == "DH":
        Plant_load_kWh = thermal_losses_kWh.sum(axis=1) + Q_demand_kWh_building.sum(
            axis=1) - accumulated_head_loss_total_kW.values
    elif network_type == "DC":
        Plant_load_kWh = thermal_losses_kWh.sum(axis=1) + Q_demand_kWh_building.sum(
            axis=1) + accumulated_head_loss_total_kW.values
    Plant_load_kWh.to_csv(locator.get_thermal_network_plant_heat_requirement_file(network_type, network_name),
                          header=['NONE'], index=False)

    # WRITE TO DISK

    # thermal demand per building (no losses in the network or substations)
    Q_demand_Wh_building = Q_demand_kWh_building * 1000
    Q_demand_Wh_building.to_csv(locator.get_thermal_demand_csv_file(network_type, network_name), index=False)

    # pressure losses total
    head_loss_system_Pa = pd.DataFrame({"pressure_loss_supply_Pa": accumulated_head_loss_supply_Pa,
                                        "pressure_loss_return_Pa": accumulated_head_loss_return_Pa,
                                        "pressure_loss_substations_Pa": accumulated_head_loss_substations_Pa,
                                        "pressure_loss_total_Pa": accumulated_head_loss_total_Pa})
    head_loss_system_Pa.to_csv(locator.get_thermal_network_layout_pressure_drop_file(network_type, network_name),
                               index=False)

    # pressure losses per piping system
    head_loss_system_per_edge_kWh = head_loss_supply_kW + head_loss_return_kW
    head_loss_system_per_edge_kWh.to_csv(
        locator.get_thermal_network_layout_ploss_system_edges_file(network_type, network_name), index=False)

    # unitary pressure losses per piping system
    head_loss_system_per_edge_kWhperm = head_loss_supply_kWperm + head_loss_return_kWperm
    head_loss_system_per_edge_kWhperm.to_csv(
        locator.get_thermal_network_layout_unitary_ploss_system_edges_file(network_type, network_name), index=False)

    # pressure losses per substation
    head_loss_substations_kW = head_loss_substations_kW.rename(columns=building_nodes_pairs)
    head_loss_substations_kW.to_csv(locator.get_thermal_network_substation_ploss_file(network_type, network_name),
                                    index=False)

    # pumping needs losses total
    pumping_energy_system_kWh = pd.DataFrame({"pressure_loss_supply_kW": accumulated_head_loss_supply_kW,
                                              "pressure_loss_return_kW": accumulated_head_loss_return_kW,
                                              "pressure_loss_substations_kW": accumulated_head_loss_substations_kW,
                                              "pressure_loss_total_kW": accumulated_head_loss_total_kW})
    pumping_energy_system_kWh.to_csv(
        locator.get_thermal_network_layout_pressure_drop_kw_file(network_type, network_name), index=False)

    # unitary pressure losses
    unitary_head_loss_supply_network_Paperm.to_csv(
        locator.get_thermal_network_layout_linear_pressure_drop_file(network_type, network_name), index=False)

    # mass flow rates
    massflow_supply_kgs.to_csv(locator.get_thermal_network_layout_massflow_file(network_type, network_name),
                               index=False)

    # thermal losses
    thermal_losses_kWh.to_csv(locator.get_thermal_network_qloss_system_file(network_type, network_name), index=False)

    # return average temperature of supply at the substations
    T_sup_K_nodes = T_sup_K_building.rename(columns=building_nodes_pairs_inversed)
    average_year = T_sup_K_nodes.mean(axis=1)
    for node in node_df.index.values:
        T_sup_K_nodes[node] = average_year
    T_sup_K_nodes.to_csv(locator.get_thermal_network_layout_supply_temperature_file(network_type, network_name),
                         index=False)

    # summary of edges used for the calculation
    fields_edges = ['length_m', 'Pipe_DN', 'Type_mat']
    edge_df[fields_edges].to_csv(locator.get_thermal_network_edge_list_file(network_type, network_name), index=False)
    fields_nodes = ['Building', 'Type']
    node_df[fields_nodes].to_csv(locator.get_thermal_network_node_types_csv_file(network_type, network_name),
                                 index=False)

    # correct diamter of network and save to the shapefile
    from cea.utilities.dbf import dataframe_to_dbf, dbf_to_dataframe
    fields = ['length_m', 'Pipe_DN', 'Type_mat']
    edge_df = edge_df[fields]
    edge_df['name'] = edge_df.index.values
    network_edges_df = dbf_to_dataframe(
        locator.get_network_layout_edges_shapefile(network_type, network_name).split('.shp')[0] + '.dbf')
    network_edges_df = network_edges_df.merge(edge_df, left_on='Name', right_on='name', suffixes=('_x', ''))
    network_edges_df = network_edges_df.drop(['Pipe_DN_x', 'Type_mat_x', 'name', 'length_m_x'], axis=1)
    dataframe_to_dbf(network_edges_df,
                     locator.get_network_layout_edges_shapefile(network_type, network_name).split('.shp')[0] + '.dbf')

def main(config):
    """
    run the whole network summary routine
    """
    start = time.time()
    locator = cea.inputlocator.InputLocator(scenario=config.scenario)

    network_names = config.thermal_network.network_names

    if len(network_names) == 0:
        network_names = ['']

    for network_name in network_names:
        thermal_network_simplified(locator, config, network_name)

    print('done.')
    print('total time: ', time.time() - start)


if __name__ == '__main__':
    main(cea.config.Configuration())