"""
Create a list of samples in a specified folder as input for the demand sensitivity analysis.
"""
import os

import numpy as np
import pandas as pd
from SALib.sample.saltelli import sample as sampler_sobol
from SALib.sample.morris import sample as sampler_morris
from cea.inputlocator import InputLocator


def create_demand_samples(method='morris', num_samples=1000, variable_groups=('THERMAL'), sampler_params={}):
    """

    :param sampler_params: additional, sampler-specific parameters. For `method='morris'` these are: [grid_jump,
                           num_levels], for `method='sobol'` these are: [calc_second_order]
    :param output_folder: Folder to place the output file 'samples.npy' (FIXME: should this be part of the
                          InputLocator?)
    :param method: The method to use. Valid values are 'morris' (default) and 'sobol'.
    :param num_samples: The number of samples `N` to make
    :param variable_groups: list of names of groups of variables to analyse. Possible values are:
        'THERMAL', 'ARCHITECTURE', 'INDOOR_COMFORT', 'INTERNAL_LOADS'. This list links to the probability density
        functions of the variables contained in locator.get_uncertainty_db() and refers to the Excel worksheet name.
    :return: (samples, problem) - samples is a list of configurations for each simulation to run, a configuration being
        a list of values for each variable in the problem. The problem is a dictionary with the keys 'num_vars',
        'names' and 'bounds' and describes the variables being sampled: 'names' is list of variable names of length
        'num_vars' and 'bounds' is a list of tuples(lower-bound, upper-bound) for each of these variables.
    """
    locator = InputLocator(None)

    pdf = pd.concat([pd.read_excel(locator.get_uncertainty_db(), group, axis=1) for group in variable_groups])
    num_vars = pdf.name.count()  # integer with number of variables
    names = pdf.name.values  # [,,] with names of each variable
    bounds = []  # a list of two-tuples containing the lower-bound and upper-bound of each variable
    for var in range(num_vars):
        limits = [pdf.loc[var, 'min'], pdf.loc[var, 'max']]
        bounds.append(limits)

    # define the problem
    problem = {'num_vars': num_vars, 'names': names, 'bounds': bounds, 'groups': None}

    # create samples (combinations of variables)
    def sampler(**kwargs):
        if method is 'sobol':
            return sampler_sobol(problem, N=num_samples, **kwargs)
        else:
            return sampler_morris(problem, N=num_samples, **kwargs)

    return sampler(), problem


if __name__ == '__main__':
    import argparse
    import pickle

    parser = argparse.ArgumentParser()
    parser.add_argument('-m', '--method', help='Method to use {morris, sobol}', default='morris')
    parser.add_argument('-n', '--num-samples', help='number of samples (generally 1000 or until it converges',
                        default=1000)
    parser.add_argument('--calc-second-order', help='(sobol) calc_second_order parameter',
                        default=False)
    parser.add_argument('--grid-jump', help='(morris) grid_jump parameter',
                        default=2)
    parser.add_argument('--num-levels', help='(morris) num_levels parameter',
                        default=4)
    parser.add_argument('-o', '--output-folder', default='.',
                        help='folder to place the output files (samples.npy, problem.pickle) in')
    args = parser.parse_args()

    sampler_params = {}
    if args.method == 'morris':
        sampler_params['grid_jump'] = args.grid_jump
        sampler_params['num_levels'] = args.num_levels
    elif args.method == 'sobol':
        sampler_params['calc_second_order'] = args.calc_second_order

    samples, problem = create_demand_samples(method=args.method, num_samples=args.num_samples)

    # save out to disk
    np.save(os.path.join(args.output_folder, 'samples.npy'), samples)
    pickle.dump(os.path.join(args.output_folder, 'problem.pickle', problem))
