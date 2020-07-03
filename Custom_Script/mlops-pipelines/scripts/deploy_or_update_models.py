# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License.

import pathlib
import argparse
import joblib
import warnings
from azureml.core import Workspace, Model, Environment, Webservice
from azureml.core.conda_dependencies import CondaDependencies
from azureml.core.model import InferenceConfig
from azureml.core.compute import AksCompute
from azureml.core.webservice import AciWebservice, AksWebservice
from azureml.exceptions import WebserviceException


DEPLOYMENT_TYPES = ['aci', 'aks']


def main(ws, deployment_type, routing_model_name, grouping_tags=[], sorting_tags=[], 
         aks_target=None, service_prefix='manymodels-', container_size=250):

    if deployment_type not in DEPLOYMENT_TYPES:
        raise ValueError('Wrong deployment type. Expected: {}'.format(', '.join(DEPLOYMENT_TYPES)))

    
    # Get deployed models
    models_deployed = get_deployed_models(routing_model_name)

    # Get groups to deploy or update
    all_groups = get_models_in_groups(grouping_tags=grouping_tags, sorting_tags=sorting_tags,
                                      exclude_names=[routing_model_name], container_size=container_size)
    groups_new, groups_updated = split_groups_new_updated(all_groups, models_deployed)

    # Deployment configuration
    deployment_config = get_deployment_config(deployment_type, aks_target)


    deployments = []

    # Launch webservice deployments
    for group_name, group_models in groups_new.items():
        service = deploy_model_group(ws, group_name, group_models, deployment_config, name_prefix=service_prefix)
        deployments.append({ 'service': service, 'group': group_name, 'models': group_models })
    
    # Launch webservice updates
    for group_name, group_models in groups_updated.items():
        service = deploy_model_group(ws, group_name, group_models, deployment_config, name_prefix=service_prefix, update=True)
        deployments.append({ 'service': service, 'group': group_name, 'models': group_models })
    

    # Wait for deployments to finish
    for deployment in deployments:
        
        service = deployment['service']
        print(f'Waiting for deployment of {service.name} to finish...')
        try:
            service.wait_for_deployment(show_output=True)
        except WebserviceException as e:
            warnings.warn(f'DEPLOYMENT FAILED FOR SERVICE {service.name}:\n{e}', RuntimeWarning)
    
        service_info = {
            'webservice': service.name,
            'state': service.state,
            'endpoint': service.scoring_uri if service.state == 'Healthy' else None,
            'key': service.get_keys()[0] if service.auth_enabled and service.state == 'Healthy' else None
        }

        # Store/update deployment info for each deployed model
        for m in deployment['models']:
            models_deployed[m.name] = {
                'version': m.version,
                'group': deployment['group'],
                **service_info
            }

    return models_deployed


def get_deployed_models(routing_model_name):

    # Load deployed models info
    routing_model = Model.list(ws, name=routing_model_name, latest=True)
    deployed_models = joblib.load(routing_model[0].download()) if routing_model else {}

    # Make sure webservices are still deployed
    services = {}
    for service_name in set(v['webservice'] for v in deployed_models.values()):
        try:
            service = Webservice(ws, service_name)
            services[service_name] = service
        except WebserviceException:
            print(f'Webservice {service_name} not found')

    # Exclude models associated to deleted werbservices
    deployed_models = { model_name : {**model_info, 'service': services[model_info['webservice']]}
                        for model_name, model_info in deployed_models.items()
                        if model_info['webservice'] in services.keys() }

    return deployed_models


def get_models_in_groups(grouping_tags=[], sorting_tags=[], exclude_names=[], exclude_tags=[], 
                         container_size=250, page_count=100):
    
    # Get all models registered in the workspace
    all_models = Model.list(ws, latest=True, expand=False, page_count=page_count)
    print(f'Found {len(all_models)} models registered.')

    # Sort models by sorting tags
    if sorting_tags:
        all_models = sorted(all_models, key=lambda m: combine_tags(m, sorting_tags))

    # Group models by tags
    grouped_models = {}
    for m in all_models:

        # Exclude models with names or kvtags specified
        if m.name in exclude_names or any(m.tags.get(t) == v for t,v in exclude_tags):
            continue

        if any(t not in m.tags.keys() for t in grouping_tags):
            print(f'Model "{m.name}" does not contain grouping tags. Skipping.')
            continue

        # Group models in subgroups up to container_size inside groups splitted by grouping tags
        group_name = combine_tags(m, grouping_tags) if grouping_tags else 'modelgroup'
        subgroups = grouped_models.setdefault(group_name, [[]])
        if len(subgroups[-1]) == container_size:
            subgroups.append([])
        subgroups[-1].append(m)

    grouped_models = {'{}-{}'.format(g, i+1):sg[i] for g,sg in grouped_models.items() for i in range(len(sg))}
    print(f'Grouped models in {len(grouped_models)} groups.')
    
    return grouped_models


def combine_tags(model, tags):
    return '-'.join([model.tags.get(t, '') for t in tags])


def split_groups_new_updated(model_groups, deployed_models):
    
    deployed_groups = set(m['group'] for m in deployed_models.values())

    groups_new, groups_updated = {}, {}
    for group_name, group_models in model_groups.items():
        group_exists = group_name in deployed_groups
        models_changed = any(m.version > deployed_models.get(m.name, {}).get('version', 0) for m in group_models)
        if not group_exists:
            groups_new[group_name] = group_models
        elif models_changed:
            groups_updated[group_name] = group_models
        else:
            pass

    print(f'{len(groups_new)} groups to be deployed, {len(groups_updated)} groups to be updated.')
    
    return groups_new, groups_updated


def get_deployment_config(deployment_type, aks_target=None, cores=1, memory=1):
    
    if deployment_type == 'aks' and aks_target is None:
        raise ValueError('AKS target name needs to be set in AKS deployments')

    # Define inference environment
    forecast_env = Environment.from_conda_specification(
        name='many_models_environment',
        file_path='Custom_Script/scripts/forecast_webservice.conda.yml'
    )
    
    # Define inference configuration
    inference_config = InferenceConfig(
        source_directory='Custom_Script/scripts/',
        entry_script='forecast_webservice.py',
        environment=forecast_env
    )

    # Define deploy configuration
    if deployment_type == 'aci':
        deployment_config = AciWebservice.deploy_configuration(cpu_cores=cores, memory_gb=memory)
        deployment_target = None
    elif deployment_type == 'aks':
        deployment_config = AksWebservice.deploy_configuration(cpu_cores=cores, memory_gb=memory)
        deployment_target = AksCompute(ws, aks_target)
    
    config = {
        'inference_config': inference_config,
        'deployment_config': deployment_config,
        'deployment_target': deployment_target
    }
    
    return config


def deploy_model_group(ws, group_name, group_models, deployment_config, name_prefix='manymodels-', update=False):
    
    service_name = '{prefix}{group}'.format(
        prefix=name_prefix,
        group=group_name
    ).lower()

    if update:
        print(f'Launching updating of {service_name}...')
        service = Webservice(ws, service_name)
        service.update(
            models=group_models,
            inference_config=deployment_config['inference_config']
        )
        print(f'Updating of {service_name} started')
    else:
        print(f'Launching deployment of {service_name}...')
        service = Model.deploy(
            workspace=ws,
            name=service_name,
            models=group_models,
            **deployment_config,
            overwrite=True
        )
        print('Deployment of {} started'.format(service_name))

    return service


def parse_args(args=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--subscription-id', required=True, type=str)
    parser.add_argument('--resource-group', required=True, type=str)
    parser.add_argument('--workspace-name', required=True, type=str)
    parser.add_argument("--grouping-tags", default='', type=lambda str: [t for t in str.split(',') if t])
    parser.add_argument("--sorting-tags", default='', type=lambda str: [t for t in str.split(',') if t])
    parser.add_argument("--routing-model-name", type=str, default='deployed_models_info')
    parser.add_argument("--output", type=str, default='models_deployed.pkl')
    parser.add_argument("--aks-target", type=str)
    parser.add_argument("--service-prefix", type=str)
    parser.add_argument("--container-size", type=int, default=250)
    args_parsed = parser.parse_args(args)

    if args_parsed.aks_target == '':
        args_parsed.aks_target = None

    if args_parsed.service_prefix is None:
        args_parsed.service_prefix = 'test-manymodels-' if not args_parsed.aks_target else 'manymodels-'
    
    return args_parsed


if __name__ == "__main__":
    args = parse_args()

    # Connect to workspace
    ws = Workspace.get(
        name=args.workspace_name,
        subscription_id=args.subscription_id,
        resource_group=args.resource_group
    )

    models_deployed = main(
        ws, 
        deployment_type='aks' if args.aks_target else 'aci',
        routing_model_name=args.routing_model_name,
        grouping_tags=args.grouping_tags,
        sorting_tags=args.sorting_tags,
        aks_target=args.aks_target,
        service_prefix=args.service_prefix,
        container_size=args.container_size
    )
    
    joblib.dump(models_deployed, args.output)