# Copyright (c) 2021 Gregory J. Miller. All rights reserved.
# Licensed under the Apache License, Version 2.0, which is in the LICENSE file.

"""
This takes data from an input excel file and formats into individual csv files for inputs
"""

import ast
import pandas as pd 
import numpy as np
from pathlib import Path
import os
import shutil
from datetime import datetime
import pytz

# Import the PySAM modules for simulating solar, CSP, and wind power generation
import PySAM.ResourceTools as tools
import PySAM.Pvwattsv7 as pv
import PySAM.TcsmoltenSalt as csp_tower
import PySAM.Windpower as wind


def generate_inputs(model_workspace, timezone):

    tz_offset = np.round(datetime(year=2020,month=1,day=1,tzinfo=pytz.timezone(timezone)).utcoffset().total_seconds()/3600)

    model_inputs = model_workspace / 'model_inputs.xlsx'

    print('Copying CBC solver to model run directory...')
    #copy the cbc solver to the model workspace
    shutil.copy('cbc.exe', model_workspace)
    shutil.copy('coin-license.txt', model_workspace)
    
    
    print('Writing options.txt...')
    # Scenarios
    xl_scenarios = pd.read_excel(io=model_inputs, sheet_name='scenarios')

    scenario_list = list(xl_scenarios.iloc[:, 3:].columns)

    #write options.txt
    options_txt = open(model_workspace / 'options.txt', 'w+')
    options_txt.write('--verbose\n')
    options_txt.write('--sorted-output\n')
    options_txt.write('--solver cbc\n')
    options_txt.close()

    print('Creating input and output folders for each scenario...')
    # create the scenario folders in the input and output directories
    try:
        os.mkdir(model_workspace / 'inputs')
        os.mkdir(model_workspace / 'outputs')
    except FileExistsError:
        pass

    for scenario in scenario_list:
        try:
            os.mkdir(model_workspace / f'inputs/{scenario}')
        except FileExistsError:
            pass
        try:
            os.mkdir(model_workspace / f'outputs/{scenario}')
        except FileExistsError:
            pass

    print('Loading data from excel spreadsheet...')
    # Load all of the data from the excel file

    xl_general = pd.read_excel(io=model_inputs, sheet_name='general')

    year = int(xl_general.loc[xl_general['Parameter'] == 'Model Year', 'Input'].item())
    timezone = xl_general.loc[xl_general['Parameter'] == 'Timezone', 'Input'].item()
    nrel_api_key = xl_general.loc[xl_general['Parameter'] == 'NREL API key', 'Input'].item()
    nrel_api_email = xl_general.loc[xl_general['Parameter'] == 'NREL API email', 'Input'].item()

    # periods.csv
    df_periods = pd.DataFrame(columns=['INVESTMENT_PERIOD','period_start','period_end'], data=[[year,year,year]])


    # timeseries.csv
    df_timeseries = pd.DataFrame(
        data={'TIMESERIES': [f'{year}_timeseries'],
            'ts_period': [year],
            'ts_duration_of_tp': [1], #duration (hour) of each timepoint
            'ts_num_tps': [8760], #number of timepoints in the timeseries
            'ts_scale_to_period': [1]} #number of timeseries in period
            )

    #financials
    df_financials = pd.DataFrame(
        data={'base_financial_year': [int(xl_general.loc[xl_general['Parameter'] == 'Base Financial Year', 'Input'].item())],
            'discount_rate': [xl_general.loc[xl_general['Parameter'] == 'Discount Rate', 'Input'].item()],
            'interest_rate': [xl_general.loc[xl_general['Parameter'] == 'Interest Rate', 'Input'].item()]}
    )

    # Read data from the excel file

    xl_gen = pd.read_excel(io=model_inputs, sheet_name='generation')
    if xl_gen.isnull().values.any():
        raise ValueError("The generation tab contains a missing value. Please fix")

    xl_load = pd.read_excel(io=model_inputs, sheet_name='load', header=[0,1], index_col=0)

    # ra_requirement.csv
    xl_ra_req = pd.read_excel(io=model_inputs, sheet_name='RA_requirements')
    ra_requirement = xl_ra_req[xl_ra_req['RA_RESOURCE'] != 'flexible_RA']
    ra_requirement['period'] = year
    ra_requirement = ra_requirement[['period','RA_RESOURCE','tp_month','ra_requirement','ra_cost','ra_resell_value']]

    # flexible_ra_requirement.csv
    flexible_ra_requirement = xl_ra_req[xl_ra_req['RA_RESOURCE'] == 'flexible_RA']
    flexible_ra_requirement['period'] = year
    flexible_ra_requirement = flexible_ra_requirement.drop(columns=['RA_RESOURCE'])
    flexible_ra_requirement = flexible_ra_requirement.rename(columns={'ra_requirement':'flexible_ra_requirement','ra_cost':'flexible_ra_cost', 'ra_resell_value':'flexible_ra_resell_value'})
    flexible_ra_requirement = flexible_ra_requirement[['period','tp_month','flexible_ra_requirement','flexible_ra_cost','flexible_ra_resell_value']]

    # ra_capacity_value.csv
    ra_capacity_value = pd.read_excel(io=model_inputs, sheet_name='RA_capacity_value')
    ra_capacity_value['period'] = year
    ra_capacity_value = ra_capacity_value[['period','gen_energy_source','tp_month','gen_capacity_value']]

    # ra_requirement_areas.csv
    ra_requirement_areas = pd.read_excel(io=model_inputs, sheet_name='RA_areas')

    # ra_requirement_categories.csv
    ra_requirement_categories = ra_requirement[['RA_RESOURCE']].drop_duplicates(ignore_index=True).rename(columns={'RA_RESOURCE':'RA_REQUIREMENT'})

    xl_nodal_prices = pd.read_excel(io=model_inputs, sheet_name='nodal_prices', index_col='Datetime', skiprows=1)

    xl_shift = pd.read_excel(io=model_inputs, sheet_name='load_shift', header=[0,1], index_col=0)


    # create a dataframe that contains the unique combinations of resource years and generator sets, and the scenarios associated with each
    vcf_sets = xl_scenarios[xl_scenarios['Input Type'].isin(['Resource year(s)', 'Generator Set'])].drop(columns=['Input Type','Parameter','Description']).transpose().reset_index()
    vcf_sets.columns = ['scenario','years','gen_set']
    vcf_sets = vcf_sets.groupby(['years','gen_set'])['scenario'].apply(list).reset_index()

    #for each of these unique combinations, get the variable capacity factor data
    for index,row in vcf_sets.iterrows():
        gen_set = row['gen_set']
        resource_years = ast.literal_eval(row['years'])
        set_scenario_list = row['scenario']

        print(f'Generating capacity factor timeseries for {gen_set}')

        #get the gen set data
        # subset the generation data for the set of generators that are part of the active set
        set_gens = xl_gen[xl_gen[gen_set] == 1]
        set_gen_list = list(set_gens['GENERATION_PROJECT'])

        # variable_capacity_factors.csv
        vcf_inputs = set_gens[['GENERATION_PROJECT','capacity_factor_input','SAM_template','latitude','longitude']]

        vcf_input_types = list(vcf_inputs.capacity_factor_input.unique())

        #create a blank dataframe with a datetimeindex
        df_vcf = pd.DataFrame(data=range(1,8761), columns=['timepoint']).set_index('timepoint')


        if 'manual' in vcf_input_types:
            manual_vcf = pd.read_excel(io=model_inputs, sheet_name='manual_capacity_factors', index_col='Datetime').reset_index(drop=True)
            if manual_vcf.isnull().values.any():
                raise ValueError("The manual_capacity_factor tab contains a missing value. Please fix")
            #only keep columns for the current scenario
            manual_vcf = manual_vcf.loc[:, manual_vcf.columns.isin(set_gen_list)]
            manual_vcf['timepoint'] = manual_vcf.index + 1
            manual_vcf = manual_vcf.set_index('timepoint')
            

            # merge manual vcf into df
            df_vcf = df_vcf.merge(manual_vcf, how='left', left_index=True, right_index=True)


        if 'SAM' in vcf_input_types:
            #get SAM template data
            sam_templates = pd.read_excel(io=model_inputs, sheet_name='SAM_templates')
            
            #get the information for the relevant generators
            sam_inputs = vcf_inputs[vcf_inputs['capacity_factor_input'] == 'SAM']
            
            #get list of templates
            template_list = list(sam_inputs.SAM_template.unique())

            #For each template, get the list of generators and simulate
            for template in template_list:
                #get the list of generators that use the current template
                gen_inputs = vcf_inputs[vcf_inputs['SAM_template'] == template]

                #get lat/long coordinates of all resources using this template
                gen_inputs['long/lat'] = gen_inputs.apply(lambda row: f"({row['longitude']},{row['latitude']})", axis=1)
                gen_inputs['long/lat'] = gen_inputs['long/lat'].apply(ast.literal_eval)
                resource_dict = dict(zip(gen_inputs['long/lat'], gen_inputs['GENERATION_PROJECT']))

                #get the parameter info for this template
                resource_template = sam_templates[sam_templates['Template_Name'] == template]

                #create a dictionary for the parameter values
                config_dict = {}
                for category in resource_template['Category'].unique():
                    #create a dict of parameters for this category
                    parameters = resource_template.loc[resource_template['Category'] == category, ['Parameter','Value']]
                    parameter_dict = {}
                    for index, row in parameters.iterrows():
                        try:
                            parameter_dict[row.Parameter] = ast.literal_eval(row.Value)
                        except ValueError:
                            parameter_dict[row.Parameter] = row.Value
                    #dict(zip(parameters.Parameter, ast.literal_eval(parameters.Value)))

                    config_dict[category] = parameter_dict

                #get the name of the PySAM function
                sam_function = resource_template.iloc[0,0]

                pysam_dir = model_workspace / gen_set

                if sam_function == 'pv':
                    #run PySAM to simulate the solar outputs
                    solar_vcf = simulate_solar_generation(nrel_api_key, nrel_api_email, resource_dict, config_dict, resource_years, pysam_dir, tz_offset)
                    
                    #add the data to the dataframe
                    df_vcf = df_vcf.merge(solar_vcf, how='left', left_index=True, right_index=True)
            
                elif sam_function == 'csp_tower':
                    #run PySAM to simulate the solar outputs
                    csp_vcf = simulate_csp_generation(nrel_api_key, nrel_api_email, resource_dict, config_dict, resource_years, pysam_dir, tz_offset)
                    
                    #add the data to the dataframe
                    df_vcf = df_vcf.merge(csp_vcf, how='left', left_index=True, right_index=True)

                elif sam_function == 'wind':
                    #run PySAM to simulate the solar outputs
                    wind_vcf = simulate_wind_generation(nrel_api_key, nrel_api_email, resource_dict, config_dict, resource_years, pysam_dir, tz_offset)
                    
                    #add the data to the dataframe
                    df_vcf = df_vcf.merge(wind_vcf, how='left', left_index=True, right_index=True)

            
        #replace all negative capacity factors with 0
        df_vcf[df_vcf < 0] = 0

        df_vcf = df_vcf.reset_index()
                    
        #iterate for each scenario and save outputs to csv files
        for scenario in set_scenario_list:

            print(f'Writing inputs for {scenario} scenario...')

            input_dir = model_workspace / f'inputs/{scenario}'
            output_dir = model_workspace / f'outputs/{scenario}'

            # modules.txt
            module_list = list(xl_scenarios.loc[(xl_scenarios['Input Type'] == 'Module') & (xl_scenarios[scenario] == 1), 'Parameter'])
            modules = open(input_dir / 'modules.txt', 'w+')
            for module in module_list:
                modules.write(module)
                modules.write('\n')
            modules.close()

            #switch_inputs_version.txt
            inputs_version = open(input_dir / 'switch_inputs_version.txt', 'w+')
            inputs_version.write('2.0.5')
            inputs_version.close()

            # renewable_target.csv
            renewable_target_value = xl_scenarios.loc[(xl_scenarios['Parameter'] == 'renewable_target'), scenario].item()
            renewable_target_type = xl_scenarios.loc[(xl_scenarios['Parameter'] == 'goal_type'), scenario].item()
            renewable_target = pd.DataFrame(data={'period':[year], 'renewable_target':[renewable_target_value]})
            renewable_target.to_csv(input_dir / 'renewable_target.csv', index=False)

            # summary_report.ipynb
            shutil.copy('reporting/summary_report.ipynb', input_dir)

            df_periods.to_csv(input_dir / 'periods.csv', index=False)
            df_timeseries.to_csv(input_dir / 'timeseries.csv', index=False)

            #get configuration options
            option_list = list(xl_scenarios.loc[(xl_scenarios['Input Type'] == 'Options') & (xl_scenarios[scenario] == 1), 'Parameter'])

            # scenarios.txt
            scenarios = open(model_workspace / 'scenarios.txt', 'a+')
            if 'select_variants' in option_list:
                variant_option = ' --select_variants select'
            else:
                variant_option = ''
            if renewable_target_type == 'annual':
                target_option = ' --goal_type annual'
            else:
                target_option = ''
            scenarios.write(f'--scenario-name {scenario} --outputs-dir outputs/{scenario} --inputs-dir inputs/{scenario}{variant_option}{target_option}')
            scenarios.write('\n')
            scenarios.close()

            # subset days
            subset_days = ast.literal_eval(xl_scenarios.loc[(xl_scenarios['Parameter'] == 'subset_days'), scenario].item())

            # timepoints.csv
            df_timepoints = pd.DataFrame(index=pd.date_range(start=f'01/01/{year} 00:00', end=f'12/31/{year} 23:00', freq='1H'))
            df_timepoints['timeseries'] = f'{year}_timeseries'
            df_timepoints['timestamp'] = df_timepoints.index.strftime('%m/%d/%Y %H:%M')
            df_timepoints['tp_month'] = df_timepoints.index.month
            df_timepoints['tp_day'] = df_timepoints.index.dayofyear
            df_timepoints['tp_in_subset'] = 0
            df_timepoints.loc[df_timepoints['tp_day'].isin(subset_days),'tp_in_subset'] = 1
            df_timepoints = df_timepoints.reset_index(drop=True)
            df_timepoints['timepoint_id'] = df_timepoints.index + 1
            df_timepoints[['timepoint_id','timestamp','timeseries']].to_csv(input_dir / 'timepoints.csv', index=False)

            # months.csv
            df_timepoints[['timepoint_id','tp_month']].to_csv(input_dir / 'months.csv', index=False)

            # days.csv
            df_timepoints[['timepoint_id','tp_day','tp_in_subset']].to_csv(input_dir / 'days.csv', index=False)

            df_financials.to_csv(input_dir / 'financials.csv', index=False)

            
            
            # gen_build_years.csv
            gen_build_years = set_gens[['GENERATION_PROJECT']]
            gen_build_years['build_year'] = year
            gen_build_years.to_csv(input_dir / 'gen_build_years.csv', index=False)

            # gen_build_predetermined.csv
            gen_build_predetermined = set_gens[['GENERATION_PROJECT','gen_predetermined_cap']]
            gen_build_predetermined = gen_build_predetermined[gen_build_predetermined['gen_predetermined_cap'] != '.']
            gen_build_predetermined['build_year'] = year
            if 'ignores_existing_contracts' in option_list:
                gen_build_predetermined = gen_build_predetermined[0:0]
            gen_build_predetermined = gen_build_predetermined[['GENERATION_PROJECT','build_year','gen_predetermined_cap']]
            gen_build_predetermined.to_csv(input_dir / 'gen_build_predetermined.csv', index=False)

            # generation_projects_info.csv
            gpi_columns = ['GENERATION_PROJECT',	
                        'gen_tech',	
                        'gen_energy_source',	
                        'gen_load_zone',	
                        'gen_reliability_area',	
                        'gen_variant_group',
                        'gen_is_variable',	
                        'gen_is_baseload',
                        'gen_is_storage',	
                        'gen_capacity_limit_mw',	
                        'gen_full_load_heat_rate',	
                        'gen_scheduled_outage_rate',	
                        'gen_forced_outage_rate',	
                        'storage_roundtrip_efficiency',	
                        'storage_charge_to_discharge_ratio',	
                        'storage_energy_to_power_ratio',	
                        'storage_max_annual_cycles',	
                        'storage_leakage_loss',	
                        'storage_hybrid_generation_project',	
                        'storage_hybrid_capacity_ratio',
                        'gen_pricing_node',
                        'ppa_energy_cost',	
                        'ppa_capacity_cost',	
                        'gen_excess_max']

            generation_projects_info = set_gens[gpi_columns]

            if 'is_price_agnostic' in option_list:
                generation_projects_info['ppa_energy_cost'] = 10
            
            if 'includes_cap_on_excess_generation' not in option_list:
                generation_projects_info = generation_projects_info.drop(columns=['gen_excess_max'])

            if 'ignores_capacity_limit' in option_list:
                generation_projects_info['gen_capacity_limit_mw'] = '.'

            if 'select_variants' not in option_list:
                generation_projects_info = generation_projects_info.drop(columns=['gen_variant_group'])

            

            generation_projects_info.to_csv(input_dir / 'generation_projects_info.csv', index=False)

            # non_fuel_energy_sources.csv
            non_fuel_energy_sources = set_gens[['gen_energy_source']].drop_duplicates(ignore_index=True).rename(columns={'gen_energy_source':'energy_source'})
            non_fuel_energy_sources.to_csv(input_dir / 'non_fuel_energy_sources.csv', index=False)

            # fuels.csv
            fuels = pd.DataFrame(columns=['fuel','co2_intensity','upstream_co2_intensity'])
            fuels.to_csv(input_dir / 'fuels.csv', index=False)

            # fuel_cost.csv
            fuel_cost = pd.DataFrame(columns=['load_zone','fuel','period','fuel_cost'])
            fuel_cost.to_csv(input_dir / 'fuel_cost.csv', index=False)

            # LOAD DATA #

            # load_zones.csv
            load_list = list(set_gens.gen_load_zone.unique())
            load_zones = pd.DataFrame(data={'LOAD_ZONE':load_list})
            load_zones.to_csv(input_dir / 'load_zones.csv', index=False)  
                
            #get the load type that should be used
            load_scenario = xl_scenarios.loc[(xl_scenarios['Parameter'] == 'load_scenario'), scenario].item()

            loads = xl_load.iloc[:, xl_load.columns.get_level_values(0) == load_scenario]
            loads.columns = loads.columns.droplevel()

            loads = loads.reset_index(drop=True)
            loads['TIMEPOINT'] = loads.index + 1
            loads = loads.melt(id_vars=['TIMEPOINT'], var_name='LOAD_ZONE', value_name='zone_demand_mw')
            loads = loads[['LOAD_ZONE','TIMEPOINT','zone_demand_mw']]
            loads.to_csv(input_dir / 'loads.csv', index=False)

            # RA data
            if 'switch_model.generators.extensions.resource_adequacy' in module_list:
                # local_reliability_areas.csv
                local_reliability_areas = set_gens[['gen_reliability_area']].drop_duplicates(ignore_index=True).rename(columns={'gen_reliability_area':'LOCAL_RELIABILITY_AREA'})
                local_reliability_areas.to_csv(input_dir / 'local_reliability_areas.csv', index=False)

                ra_requirement.to_csv(input_dir / 'ra_requirement.csv', index=False)
                flexible_ra_requirement.to_csv(input_dir / 'flexible_ra_requirement.csv', index=False)
                energy_source_list = list(generation_projects_info['gen_energy_source'].unique())
                ra_capacity_value = ra_capacity_value[ra_capacity_value['gen_energy_source'].isin(energy_source_list)]
                ra_capacity_value.to_csv(input_dir / 'ra_capacity_value.csv', index=False)

                lra_list = list(local_reliability_areas['LOCAL_RELIABILITY_AREA'])
                ra_requirement_areas = ra_requirement_areas[ra_requirement_areas['LOCAL_RELIABILITY_AREA'].isin(lra_list)]
                ra_requirement_areas.to_csv(input_dir / 'ra_requirement_areas.csv', index=False)
                ra_requirement_categories.to_csv(input_dir / 'ra_requirement_categories.csv', index=False)
            
            # system_power_cost.csv
            system_power_cost = xl_nodal_prices.reset_index(drop=True)
            system_power_cost = system_power_cost[load_list]
            system_power_cost['timepoint'] = system_power_cost.index + 1
            system_power_cost = system_power_cost.melt(id_vars=['timepoint'], var_name='load_zone', value_name='system_power_cost')
            system_power_cost = system_power_cost[['load_zone','timepoint','system_power_cost']]
            system_power_cost.to_csv(input_dir / 'system_power_cost.csv', index=False)

            # pricing_nodes.csv
            node_list = list(set_gens.gen_pricing_node.unique())
            node_list = [i for i in node_list if i not in ['.',np.nan]]
            pricing_nodes = pd.DataFrame(data={'PRICING_NODE':node_list})
            pricing_nodes.to_csv(input_dir / 'pricing_nodes.csv', index=False)  

            #nodal_prices.csv
            nodal_prices = xl_nodal_prices.reset_index(drop=True)
            nodal_prices = nodal_prices[node_list]
            nodal_prices['timepoint'] = nodal_prices.index + 1
            nodal_prices = nodal_prices.melt(id_vars=['timepoint'], var_name='pricing_node', value_name='nodal_price')
            nodal_prices = nodal_prices[['pricing_node','timepoint','nodal_price']]
            #add system power / demand node prices to df
            nodal_prices = pd.concat([nodal_prices, system_power_cost.rename(columns={'load_zone':'pricing_node','system_power_cost':'nodal_price'})], axis=0, ignore_index=True)
            nodal_prices.to_csv(input_dir / 'nodal_prices.csv', index=False)

            # dr_data.csv
            if scenario in list(xl_shift.columns.levels[0]):
                i = 0
                #iterate for each load zone
                for load in load_list:
                    if i == 0:
                        dr_data = xl_shift.iloc[:, xl_shift.columns.get_level_values(0) == scenario]
                        dr_data.columns = dr_data.columns.droplevel()
                        dr_data['LOAD_ZONE'] = load
                        dr_data = dr_data.reset_index(drop=True)
                        dr_data['TIMEPOINT'] = dr_data.index + 1
                        i += 1
                    elif i > 0:
                        dr_data_temp = xl_shift.iloc[:, xl_shift.columns.get_level_values(0) == scenario]
                        dr_data_temp.columns = dr_data_temp.columns.droplevel()
                        dr_data_temp['LOAD_ZONE'] = load
                        dr_data_temp = dr_data_temp.reset_index(drop=True)
                        dr_data_temp['TIMEPOINT'] = dr_data_temp.index + 1
                        dr_data = dr_data.append(dr_data_temp, ignore_index=True)
                #re-order columns
                dr_data = dr_data[['LOAD_ZONE','TIMEPOINT','dr_shift_down_limit','dr_shift_up_limit']]        
                dr_data.to_csv(input_dir / 'dr_data.csv', index=False)

            #variable_capacity_factors.csv
            df_vcf_scenario = df_vcf.copy()

            #melt the data and save as csv
            df_vcf_scenario = df_vcf_scenario.melt(id_vars="timepoint", var_name="GENERATION_PROJECT", value_name="gen_max_capacity_factor")

            #reorder the columns
            df_vcf_scenario = df_vcf_scenario[['GENERATION_PROJECT','timepoint','gen_max_capacity_factor']]
            df_vcf_scenario.to_csv(input_dir / 'variable_capacity_factors.csv', index=False)



def simulate_solar_generation(nrel_api_key, nrel_api_email, resource_dict, config_dict, resource_years, input_dir, tz_offset):
    
    #initiate the default PV setup
    system_model_PV = pv.default('PVWattsSingleOwner')

    # specify non-default system design factors
    systemDesign = config_dict['SystemDesign']

    #assign the non-default system design specs to the model
    system_model_PV.SystemDesign.assign(systemDesign)

    lon_lats = list(resource_dict.keys())

    #this is the df that will hold all of the data for all years
    df_resource = pd.DataFrame(data=range(1,8761), columns=['timepoint']).set_index('timepoint')
    df_index = df_resource.index

    for year in resource_years:

        #download resource files
        #https://github.com/NREL/pysam/blob/master/Examples/FetchResourceFileExample.py
        #https://nrel-pysam.readthedocs.io/en/master/Tools.html?highlight=download#files.ResourceTools.FetchResourceFiles

        #TODO: allow to fetch single resource year
        nsrdbfetcher = tools.FetchResourceFiles(
                        tech='solar',
                        workers=1,  # thread workers if fetching multiple files
                        nrel_api_key=nrel_api_key,
                        resource_type='psm3',
                        resource_year=str(year),
                        nrel_api_email=nrel_api_email,
                        resource_dir=(input_dir / 'PySAM Downloaded Weather Files/PV'))

        #fetch resource data from the dictionary
        nsrdbfetcher.fetch(lon_lats)

        #get a dictionary of all of the filepaths
        nsrdb_path_dict = nsrdbfetcher.resource_file_paths_dict

        for filename in nsrdb_path_dict:
            solarResource = tools.SAM_CSV_to_solar_data(nsrdb_path_dict[filename])
            
            #assign the solar resource input file to the model
            system_model_PV.SolarResource.solar_resource_data = solarResource

            #execute the system model
            system_model_PV.execute()

            #access sytem power generated output
            output = system_model_PV.Outputs.gen
            df_output = pd.DataFrame(output)

            #roll the data to get into pacific time
            roll = int(tz_offset  - system_model_PV.Outputs.tz)
            df_output = pd.DataFrame(np.roll(df_output, roll))

            #calculate capacity factor
            df_output = df_output / systemDesign['system_capacity']

            #name the column based on resource name
            df_output = df_output.rename(columns={0:f'{resource_dict[filename]}-{year}'})

            #merge into the resource
            df_output.index = df_index
            df_resource = df_resource.merge(df_output, how='left', left_index=True, right_index=True)

    #remove year from column name
    df_resource.columns = [i.split('-')[0] for i in df_resource.columns]

    #groupby column name
    df_resource = df_resource.groupby(df_resource.columns, axis=1).mean()

    return df_resource

def simulate_wind_generation(nrel_api_key, nrel_api_email, resource_dict, config_dict, resource_years, input_dir, tz_offset):
    
    #initiate the default wind power setup
    system_model_wind = wind.default('WindPowerSingleOwner')

    # specify non-default system design factors
    turbine = config_dict['Turbine']
    farm = config_dict['Farm']
    resource = config_dict['Resource']

    #assign the non-default system design specs to the model
    system_model_wind.Turbine.assign(turbine)
    system_model_wind.Farm.assign(farm)

    lon_lats = list(resource_dict.keys())

    #this is the df that will hold all of the data for all years
    df_resource = pd.DataFrame(data=range(1,8761), columns=['timepoint']).set_index('timepoint')
    df_index = df_resource.index

    for year in resource_years:

        #specify wind data input
        wtkfetcher = tools.FetchResourceFiles(
                        tech='wind',
                        workers=1,  # thread workers if fetching multiple files
                        nrel_api_key=nrel_api_key,
                        nrel_api_email=nrel_api_email,
                        resource_year=str(year),
                        resource_height=resource['resource_height'],
                        resource_dir=(input_dir / 'PySAM Downloaded Weather Files/Wind'))

        #fetch resource data from the dictionary
        wtkfetcher.fetch(lon_lats)

        #get a dictionary of all of the filepaths
        wtk_path_dict = wtkfetcher.resource_file_paths_dict

        for filename in wtk_path_dict:

            windResource = tools.SRW_to_wind_data(wtk_path_dict[filename])
            
            #assign the wind resource input data to the model
            system_model_wind.Resource.wind_resource_data = windResource

            #execute the system model
            system_model_wind.execute()

            #access sytem power generated output
            output = system_model_wind.Outputs.gen

            df_output = pd.DataFrame(output)

            #calculate capacity factor
            df_output = df_output / farm['system_capacity']

            #name the column based on resource name
            df_output = df_output.rename(columns={0:f'{resource_dict[filename]}-{year}'})

            #merge into the resource
            df_output.index = df_index
            df_resource = df_resource.merge(df_output, how='left', left_index=True, right_index=True)

    #remove year from column name
    df_resource.columns = [i.split('-')[0] for i in df_resource.columns]

    #groupby column name
    df_resource = df_resource.groupby(df_resource.columns, axis=1).mean()

    return df_resource

def simulate_csp_generation(nrel_api_key, nrel_api_email, resource_dict, config_dict, resource_years, input_dir, tz_offset):
    
    #initiate the default PV setup
    system_model_MSPT = csp_tower.default('MSPTSingleOwner')

    # specify non-default system design factors
    systemDesign = config_dict['SystemDesign']
    timeOfDeliveryFactors = config_dict['TimeOfDeliveryFactors']
    systemControl = config_dict['SystemControl']

    #assign the non-default system design specs to the model
    system_model_MSPT.SystemControl.assign(systemControl)
    system_model_MSPT.TimeOfDeliveryFactors.assign(timeOfDeliveryFactors)
    system_model_MSPT.SystemDesign.assign(systemDesign)

    lon_lats = list(resource_dict.keys())

    #this is the df that will hold all of the data for all years
    df_resource = pd.DataFrame(data=range(1,8761), columns=['timepoint']).set_index('timepoint')
    df_index = df_resource.index

    for year in resource_years:

        #download resource files
        #https://github.com/NREL/pysam/blob/master/Examples/FetchResourceFileExample.py
        #https://nrel-pysam.readthedocs.io/en/master/Tools.html?highlight=download#files.ResourceTools.FetchResourceFiles

        nsrdbfetcher = tools.FetchResourceFiles(
                        tech='solar',
                        workers=1,  # thread workers if fetching multiple files
                        nrel_api_key=nrel_api_key,
                        resource_type='psm3',
                        resource_year=str(year),
                        nrel_api_email=nrel_api_email,
                        resource_dir=(input_dir / 'PySAM Downloaded Weather Files/CSP'))

        #fetch resource data from the dictionary
        nsrdbfetcher.fetch(lon_lats)

        #get a dictionary of all of the filepaths
        nsrdb_path_dict = nsrdbfetcher.resource_file_paths_dict

        for filename in nsrdb_path_dict:
            #convert TMY data to be used in SAM
            #solarResource = tools.SAM_CSV_to_solar_data(nsrdb_path_dict[filename])

            #assign the solar resource input file to the model
            #system_model_MSPT.SolarResource.solar_resource_data = solarResource
            system_model_MSPT.SolarResource.solar_resource_file = nsrdb_path_dict[filename]
            
            #execute the system model
            system_model_MSPT.execute()

            #access sytem power generated output
            output = system_model_MSPT.Outputs.gen

            #roll the data to get into pacific time
            df_output = pd.DataFrame(output)

            #calculate capacity factor
            df_output = df_output / (systemDesign['P_ref'] * 1000)

            #name the column based on resource name
            df_output = df_output.rename(columns={0:f'{resource_dict[filename]}-{year}'})

            #merge into the resource
            df_output.index = df_index
            df_resource = df_resource.merge(df_output, how='left', left_index=True, right_index=True)

    #remove year from column name
    df_resource.columns = [i.split('-')[0] for i in df_resource.columns]

    #groupby column name
    df_resource = df_resource.groupby(df_resource.columns, axis=1).mean()

    return df_resource