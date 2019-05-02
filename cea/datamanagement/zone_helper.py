"""
This is a template script - an example of how a CEA script should be set up.
NOTE: ADD YOUR SCRIPT'S DOCUMENTATION HERE (what, why, include literature references)
"""
from __future__ import division
from __future__ import print_function

import math
from geopandas import GeoDataFrame as Gdf
from cea.utilities.standardize_coordinates import get_projected_coordinate_system, get_geographic_coordinate_system
from cea.datamanagement.databases_verification import COLUMNS_ZONE_AGE, COLUMNS_ZONE_OCCUPANCY
from cea.utilities.dbf import dataframe_to_dbf, dbf_to_dataframe
import osmnx as ox
import numpy as np

import os
import cea.config
import cea.inputlocator
from cea.demand import constants

__author__ = "Jimeno Fonseca"
__copyright__ = "Copyright 2019, Architecture and Building Systems - ETH Zurich"
__credits__ = ["Jimeno Fonseca", "Reynold Mok"]
__license__ = "MIT"
__version__ = "0.1"
__maintainer__ = "Daren Thomas"
__email__ = "cea@arch.ethz.ch"
__status__ = "Production"

def clean_attributes(shapefile, buildings_height, buildings_floors, buildings_height_below_ground, buildings_floors_below_ground, key):
    # local variables
    no_buildings = shapefile.shape[0]
    list_of_columns = shapefile.columns
    if buildings_height is None and buildings_floors is None:
        print('you have not indicated a height/number of floors above ground for the buildings, '
              'we are reverting to data stored in Open Street Maps (It might not be accurate at all, Warning!, '
              'if we do not find data in OSM we asume 3 floors and 1 roof in every building')

        # Check which attributes the OSM has, Sometimes it does not have any.
        if 'building:levels' not in list_of_columns:
            shapefile['building:levels'] = [3] * no_buildings
        if 'roof:levels' not in list_of_columns:
            shapefile['roof:levels'] = [1] * no_buildings

        data_osm_floors1 = shapefile['building:levels'].fillna(0)
        data_osm_floors2 = shapefile['roof:levels'].fillna(0)
        data_floors_sum = [x + y for x, y in
                           zip([float(x) for x in data_osm_floors1], [float(x) for x in data_osm_floors2])]
        data_floors_sum_with_nan = [np.nan if x <= 0.0 else x for x in data_floors_sum]
        data_osm_floors_joined = int(
            math.ceil(np.nanmedian(data_floors_sum_with_nan)))  # median so we get close to the worse case
        shapefile["floors_ag"] = [int(x) if x is not np.nan else data_osm_floors_joined for x in
                                  shapefile['building:levels'].values]
        shapefile["height_ag"] = shapefile["floors_ag"] * constants.H_F
    elif buildings_height is None and buildings_floors is not None:
        shapefile["floors_ag"] = [buildings_floors] * no_buildings
        shapefile["height_ag"] = shapefile["floors_ag"] * constants.H_F
    elif buildings_height is not None and buildings_floors is None:
        shapefile["height_ag"] = [buildings_height] * no_buildings
        shapefile["floors_ag"] = [int(math.floor(x)) for x in shapefile["height_ag"] / constants.H_F]
    else:  # both are not none
        shapefile["height_ag"] = [buildings_height] * no_buildings
        shapefile["floors_ag"] = [buildings_floors] * no_buildings

    #add fields for floorsa and height below ground
    shapefile["height_bg"] = [buildings_height_below_ground] * no_buildings
    shapefile["floors_bg"] = [buildings_floors_below_ground] * no_buildings

    #add description
    if "description" in list_of_columns:
        shapefile["description"] = shapefile['description']
    elif 'addr:housename' in list_of_columns:
        shapefile["description"] = shapefile['addr:housename']
    elif 'amenity' in list_of_columns:
        shapefile["description"] = shapefile['amenity']
    else:
        shapefile["description"] = [np.nan]*no_buildings

    shapefile["category"] = shapefile['building']
    shapefile["Name"] = [key + str(x + 1000) for x in
                         range(no_buildings)]  # start in a big number to avoid potential confusion\
    result = shapefile[
        ["Name", "height_ag", "floors_ag", "height_bg", "floors_bg", "description", "category", "geometry"]]

    return result


def zone_helper(locator, config):
    """
    This script gets a polygon and calculates the zone.shp and the occupancy.dbf and age.dbf inputs files for CEA
    :param locator:
    :param config:
    :return:
    """
    # local variables:
    poly = Gdf.from_file(locator.get_site_polygon())
    buildings_height = config.zone_helper.height_ag
    buildings_floors = config.zone_helper.floors_ag
    buildings_height_below_ground = config.zone_helper.height_bg
    buildings_floors_below_ground = config.zone_helper.floors_bg
    occupancy_type = config.zone_helper.occupancy_type
    year_construction = config.zone_helper.year_construction
    zone_output_path = locator.get_zone_geometry()
    occupancy_output_path = locator.get_building_occupancy()
    age_output_path = locator.get_building_age()

    # get zone.shp file
    zone_df = polygon_to_zone(buildings_floors, buildings_floors_below_ground, buildings_height, buildings_height_below_ground,
                    poly, zone_output_path)

    # use zone.shp file contents to get the contents of occupancy.dbf and age.dbf
    calculate_occupancy_file(zone_df.copy(), occupancy_type, occupancy_output_path)
    calculate_age_file(zone_df.copy(), year_construction, age_output_path)

def calculate_occupancy_file(zone_df, occupancy_type , occupancy_output_path):
    """
    This script fills in the occupancy.dbf file with one occupancy type
    :param zone_df:
    :param occupancy_type:
    :param occupancy_output_path:
    :return:
    """
    occupancy_df = zone_df[["Name"]].copy()
    for occupancy in COLUMNS_ZONE_OCCUPANCY:
        if occupancy_type == occupancy:
            occupancy_df.loc[:,occupancy] = 1.0
        else:
            occupancy_df.loc[:,occupancy] = 0.0

    dataframe_to_dbf(occupancy_df, occupancy_output_path)


def calculate_age_file(zone_df, year_construction, age_output_path):
    """
    This script fills in the age.dbf file with one year of construction
    :param zone_df:
    :param year_construction:
    :param age_output_path:
    :return:
    """
    age_df = zone_df[["Name"]].copy()
    for column in COLUMNS_ZONE_AGE:
        if column == 'built':
            age_df.loc[:, column] = year_construction
        else:
            age_df.loc[:, column] = 0
    dataframe_to_dbf(age_df, age_output_path)



def polygon_to_zone(buildings_floors, buildings_floors_below_ground, buildings_height, buildings_height_below_ground,
                    poly, shapefile_out_path):
    poly = poly.to_crs(get_geographic_coordinate_system())
    lon = poly.geometry[0].centroid.coords.xy[0][0]
    lat = poly.geometry[0].centroid.coords.xy[1][0]
    # get footprints of all the district
    poly = ox.footprints.create_footprints_gdf(polygon=poly['geometry'].values[0])
    # clean attributes of height, name and number of floors
    result = clean_attributes(poly, buildings_height, buildings_floors, buildings_height_below_ground,
                              buildings_floors_below_ground, key="B")
    result = result.to_crs(get_projected_coordinate_system(float(lat), float(lon)))
    # save to shapefile
    result.to_file(shapefile_out_path)

    return result


def main(config):
    """
    This script gets a polygon and calculates the zone.shp and the occupancy.dbf and age.dbf inputs files for CEA
    """
    assert os.path.exists(config.scenario), 'Scenario not found: %s' % config.scenario
    locator = cea.inputlocator.InputLocator(config.scenario)

    zone_helper(locator, config)


if __name__ == '__main__':
    main(cea.config.Configuration())