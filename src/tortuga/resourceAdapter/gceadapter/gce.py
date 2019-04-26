# Copyright 2008-2018 Univa Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import json
import os.path
import random
import re
import shlex
import subprocess
import time
import urllib.parse
from typing import Any, Dict, List, NoReturn, Optional, Tuple

import apiclient
import gevent
import googleapiclient.discovery
from gevent.queue import JoinableQueue
from google.auth import compute_engine
from google.oauth2 import service_account
from sqlalchemy.orm.session import Session

from tortuga.db.models.hardwareProfile import HardwareProfile
from tortuga.db.models.instanceMapping import InstanceMapping
from tortuga.db.models.instanceMetadata import InstanceMetadata
from tortuga.db.models.nic import Nic
from tortuga.db.models.node import Node
from tortuga.db.models.softwareProfile import SoftwareProfile
from tortuga.db.nodesDbHandler import NodesDbHandler
from tortuga.exceptions.commandFailed import CommandFailed
from tortuga.exceptions.configurationError import ConfigurationError
from tortuga.exceptions.nodeNotFound import NodeNotFound
from tortuga.exceptions.operationFailed import OperationFailed
from tortuga.exceptions.unsupportedOperation import UnsupportedOperation
from tortuga.node import state
from tortuga.resourceAdapter.resourceAdapter \
    import (DEFAULT_CONFIGURATION_PROFILE_NAME, ResourceAdapter)
from tortuga.resourceAdapterConfiguration import settings
from tortuga.utility.cloudinit import get_cloud_init_path


API_VERSION = 'v1'

GCE_URL = 'https://www.googleapis.com/compute/%s/projects/' % (API_VERSION)

EXTERNAL_NETWORK_ACCESS_CONFIG = [
    {
        'type': 'ONE_TO_ONE_NAT',
        'name': 'External NAT',
    },
]


def get_instance_name_from_host_name(hostname):
    return hostname.split('.', 1)[0]


def get_disk_volume_name(instance_name, diskNumber):
    """Return persistent volume name based on instance name and disk number
    """

    return '%s-disk-%02d' % (instance_name, diskNumber)


class Gce(ResourceAdapter): \
        # pylint: disable=too-many-public-methods

    __adaptername__ = 'gce'

    # Time (seconds) between attempts to update instance status to
    # avoid thrashing
    DEFAULT_SLEEP_TIME = 5

    settings = {
        'zone': settings.StringSetting(
            required=True,
            description='Zone in which compute resources are created'
        ),
        'json_keyfile': settings.FileSetting(
            description='Filename/path of service account credentials file as '
                        'provided by Google Compute Platform',
            base_path='/opt/tortuga/config/'
        ),
        'type': settings.StringSetting(
            required=True,
            description='Virtual machine type; ror example, "n1-standard-1"'
        ),
        'network': settings.StringSetting(
            list=True,
            required=False,
            description='Network where virtual machines will be created',
            mutually_exclusive=['networks'],
            overrides=['networks'],
        ),
        'networks': settings.StringSetting(
            list=True,
            required=False,
            description='Networks associated with virtual machines',
            mutually_exclusive=['network'],
            overrides=['network'],
        ),
        'project': settings.StringSetting(
            required=True,
            description='Name of Google Compute Engine project'
        ),
        'image': settings.StringSetting(
            required=True,
            description='Name of image used when creating compute nodes',
            mutually_exclusive=['image_url', 'image_family'],
            overrides=['image_url', 'image_family'],
        ),
        'image_url': settings.StringSetting(
            required=True,
            description='URL of image used when creating compute nodes',
            mutually_exclusive=['image', 'image_family'],
            overrides=['image', 'image_family'],
        ),
        'image_family': settings.StringSetting(
            required=True,
            description='Family of image used when creating compute nodes',
            mutually_exclusive=['image', 'image_url'],
            overrides=['image', 'image_url'],
        ),
        'startup_script_template': settings.FileSetting(
            required=True,
            description='Filename of "bootstrap" script used by Tortuga to '
                        'bootstrap compute nodes',
            default='startup_script.py',
            base_path='/opt/tortuga/config/'
        ),
        'default_ssh_user': settings.StringSetting(
            required=True,
            description='Username of default user on created VMs. "centos" '
                        'is an appropriate value for CentOS-based VMs.'
        ),
        'tags': settings.StringSetting(
            description='Keywords (separated by spaces)'
        ),
        'vcpus': settings.IntegerSetting(
            description='Number of virtual CPUs for specified virtual '
                        'machine type'
        ),
        'disksize': settings.IntegerSetting(
            description='Size of boot disk for virtual machine (in GB)',
            default='10'
        ),
        'sleeptime': settings.IntegerSetting(
            advanced=True,
            default=str(DEFAULT_SLEEP_TIME)
        ),
        'accelerators': settings.StringSetting(
            description='List of accelerators to include in the instance '
            'Format: "<accelerator-type>:<accelerator-count>,..."'
        ),
        'default_scopes': settings.StringSetting(
            required=True,
            list=True,
            list_separator='\n',
            default='https://www.googleapis.com/auth/devstorage.full_control\n'
                    'https://www.googleapis.com/auth/compute',
        ),
        'override_dns_domain': settings.BooleanSetting(default='False'),
        'dns_domain': settings.StringSetting(requires='override_dns_domain'),
        'dns_options': settings.StringSetting(),
        'dns_nameservers': settings.StringSetting(
            default='',
            list=True,
            list_separator=' '
        ),
        'createtimeout': settings.IntegerSetting(
            advanced=True,
            default='600'
        ),
        'ssd': settings.BooleanSetting(
            description='Use SSD backed virtual machines',
            default='True',
        ),
        'randomize_hostname': settings.BooleanSetting(
            description='Append random string to generated host names'
            'to prevent name collisions in highly dynamic environments',
            default='True',
        ),
    }

    def __init__(self, addHostSession: Optional[str] = None):
        super().__init__(addHostSession=addHostSession)

        self.__running_on_gce: Optional[bool] = None

    @property
    def is_running_on_gce(self) -> bool:
        if self.__running_on_gce is None:
            self.__running_on_gce = is_running_on_gce()

        return self.__running_on_gce

    def start(self, addNodesRequest: dict, dbSession: Session,
              dbHardwareProfile: HardwareProfile,
              dbSoftwareProfile: Optional[SoftwareProfile] = None) \
        -> List[Node]: \
            # pylint: disable=unused-argument
        """
        :raises: HardwareProfileNotFound
        :raises: SoftwareProfileNotFound
        :raises: InvalidArgument
        """

        gce_session = self.get_gce_session(
            addNodesRequest.get('resource_adapter_configuration'))

        # Add regular instance-backed (active) nodes
        nodes = self.__addActiveNodes(
            gce_session,
            dbSession,
            addNodesRequest,
            dbHardwareProfile,
            dbSoftwareProfile
        )

        # This is a necessary evil for the time being, until there's
        # a proper context manager implemented.
        self.addHostApi.clear_session_nodes(nodes)

        return nodes

    def validate_start_arguments(self, addNodesRequest: dict,
                                 dbHardwareProfile: HardwareProfile,
                                 dbSoftwareProfile: SoftwareProfile) -> None:
        """
        Validate arguments to start() API

        addNodesRequest['resource_adapter_configuration'] is updated with
        the cfg name that is actually used. If not initially provided,
        'default' is always the default.

        :raises UnsupportedOperation: Attempt to add node(s) without
                                      software profile specified
        """

        super().validate_start_arguments(
            addNodesRequest, dbHardwareProfile, dbSoftwareProfile
        )

        if not dbSoftwareProfile:
            raise UnsupportedOperation(
                'Software profile must be specified for GCE ndoes'
            )

    def deleteNode(self, nodes: List[Node]) -> None:
        """
        Raises:
            CommandFailed
        """

        # Iterate over list of Node database objects
        for node in nodes:
            self._logger.debug('deleteNode(): node=[%s]', node.name)

            if not node.instance or \
                    not node.instance.resource_adapter_configuration:
                # this node does not have an associated VM
                self._logger.debug(
                    'Node [%s] does not have an associated VM',
                    node.name
                )
                continue

            gce_session = self.get_gce_session(
                node.instance.resource_adapter_configuration.name)

            self.__deleteInstance(gce_session, node)

            self.__node_cleanup(node)

            # Update SAN API
            self.__process_deleted_disk_changes(node)

    def __get_project_and_zone_metadata(self, node: Node) \
            -> Tuple[Optional[str], Optional[str]]:
        """Get project and/or zone from instance metadata
        """

        project = None
        zone = None

        # iterate over instance metadata
        for md in node.instance.instance_metadata:
            if md.key == 'project':
                project = md.value
            elif md.key == 'zone':
                zone = md.value

        return project, zone

    def __process_deleted_disk_changes(self, node: Node) -> None:
        """Remove persistent disks from SAN API 'catalog'.

        Note: this does *NOT* remove the persistent disk from Google Compute
        Engine
        """

        # Get disk changes for node being deleted
        diskChanges = self.sanApi.discoverStorageChanges(node, True)

        for removedDiskNumber, disk in \
            [(int(disk_index), diskChanges['removed'][disk_index])
             for disk_index in diskChanges['removed'].keys()]:
            storageAdapter = disk['adapter']

            if storageAdapter != 'default':
                # Ignore requests for non-default storage adapter
                continue

            volume_name = get_disk_volume_name(
                get_instance_name_from_host_name(node.name),
                removedDiskNumber)

            self._logger.debug(
                'Removing persistent disk [%s]' % (volume_name))

            self.sanApi.deleteDrive(node, removedDiskNumber)

    def __node_cleanup(self, node: Node) -> None:
        self._logger.debug('__node_cleanup(): node=[%s]', node.name)

        self.addHostApi.clear_session_node(node)

        # Update SAN API
        self.__process_deleted_disk_changes(node)

    def __get_gce_session_for_node(self, node: Node) -> dict:
        """Returns GCE session object with project and/or zone properly
        defined based on existing node metadata.
        """

        gce_session = self.get_gce_session(
            node.instance.resource_adapter_configuration.name
        )

        project, zone = self.__get_project_and_zone_metadata(node)

        if zone and zone != gce_session['config']['zone']:
            gce_session['config']['zone'] = zone

        if project and project != gce_session['config']['project']:
            gce_session['config']['project'] = project

        return gce_session

    def shutdownNode(self, nodes: List[Node],
                     bSoftReset: bool = False) -> None:
        """Shutdown (stop) VMs

        TODO: implement this as an async operation to ensure shutdown
        operation succeeds. Currently, the async request is made to the GCE
        backend and control is returned to the caller.
        """

        self._logger.debug(
            'shutdownNode(): nodes=[%s], bSoftReset=%s',
            format_node_list(nodes),
            bSoftReset
        )

        for node in nodes:
            vm_name = get_instance_name_from_host_name(node.name)

            gce_session = self.__get_gce_session_for_node(node)

            self._logger.debug('Stopping node [%s]...', node.name)

            # issue async shutdown request
            self.__gce_stop_vm(
                gce_session['connection'].svc,
                vm_name,
                gce_session['config']['project'],
                gce_session['config']['zone']
            )

    def __gce_stop_vm(self, svc: googleapiclient.discovery.Resource,
                      vm_name: str, project: str, zone: str): \
            # pylint: disable=no-self-use
        svc.instances().stop(
            instance=vm_name,
            project=project,
            zone=zone
        ).execute()

    def startupNode(self, nodes: List[Node],
                    remainingNodeList: Optional[str] = None,
                    tmpBootMethod: str = 'n'): \
            # pylint: disable=unused-argument
        """Start stopped VMs
        """

        self._logger.debug(
            'startupNode(): nodes=[%s], remainingNodeList=[%s],'
            ' tmpBootMethod=[%s]',
            format_node_list(nodes),
            remainingNodeList,
            tmpBootMethod,
        )

        for node in nodes:
            vm_name = get_instance_name_from_host_name(node.name)

            gce_session = self.__get_gce_session_for_node(node)

            self._logger.debug('Starting node [%s]...', node.name)

            # issue async start request
            self.__gce_start_vm(
                gce_session['connection'].svc,
                vm_name,
                gce_session['config']['project'],
                gce_session['config']['zone']
            )

    def __gce_start_vm(self, svc, vm_name, project, zone): \
            # pylint: disable=no-self-use
        svc.instances().start(
            instance=vm_name,
            project=project,
            zone=zone,
        ).execute()

    def __validate_default_scopes(self, default_scopes: List[str]) -> None:
        """
        Raises:
            ConfigurationError
        """

        # Iterate over specified 'default_scopes' and ensure they are
        # properly formatted URLs.
        for url in default_scopes:
            urlResult = urllib.parse.urlparse(url)

            if not urlResult.scheme.lower() in ['http', 'https']:
                self._logger.error(
                    'Invalid URL specified in default_scopes:'
                    ' \"%s\" must be a properly formatted URL', url)

                raise ConfigurationError(
                    'Invalid URL [%s] specified in default_scopes' % (url))

    def process_config(self, config: Dict[str, Any]) -> None:
        #
        # Sanity check default scopes
        #
        self.__validate_default_scopes(config['default_scopes'])

        #
        # Parse tags
        #
        config['tags'] = self._parse_custom_tags(config)

        #
        # DNS settings
        #
        config['dns_domain'] = config['dns_domain'] \
            if 'dns_domain' in config else self.private_dns_zone

        if not config['dns_nameservers']:
            config['dns_nameservers'].append(self.installer_public_ipaddress)

        # extract 'region' from 'zone'; partially validate zone as side-effect
        try:
            config['region'], _ = config['zone'].rsplit('-', 1)
        except ValueError:
            raise ConfigurationError(
                'Invalid format for \'region\' setting: {}'.format(
                    config['region'])
            )

        if 'network' in config:
            network_defs = config['network']

            del config['network']
        elif 'networks' in config:
            network_defs = config['networks']

            del config['networks']
        else:
            # if 'network' or 'networks' is not defined, fall back to legacy
            network_defs = ['default']

        # convert networks definition into list of tuples
        config['networks'] = self.__parse_network_adapter_config(network_defs)

    def __parse_network_adapter_config(self, network_defs: List[str]) \
            -> List[Tuple[str, Optional[str], Optional[str]]]:
        return [split_three_item_value(network) for network in network_defs]

    def _parse_custom_tags(self, _configDict: dict) -> list:
        """
        Raises:
            ConfigurationError
        """

        # Create common regex for validating tags and custom metadata keys
        regex = re.compile(r'[a-zA-Z0-9-_]{1,128}')

        # Parse custom tags
        tags = shlex.split(_configDict['tags']) \
            if 'tags' in _configDict and _configDict['tags'] else []

        # Validate custom tags
        for tag in tags:
            result = regex.match(tag)
            if result is None or result.group(0) != tag:
                errmsg = ('Tag [%s] does not match regex'
                          '\'[a-zA-Z0-9-_]{1,128}\'' % (tag))

                self._logger.error(errmsg)

                raise ConfigurationError(errmsg)

        return tags

    def get_gce_session(
            self,
            section_name: Optional[str]) -> dict:
        """Initialize GCE session

        :raises ConfigurationError:
        :raises ResourceNotFound:
        """

        adapter_cfg = self.getResourceAdapterConfig(
            section_name or DEFAULT_CONFIGURATION_PROFILE_NAME
        )

        return {
            'config': adapter_cfg,
            'connection': gceAuthorize_from_json(
                adapter_cfg.get('json_keyfile')
            ),
        }

    def __getStartupScript(self, configDict: dict) -> Optional[str]:
        """
        Build a node/instance-specific startup script that will initialize
        VPN, install Puppet, and the bootstrap the instance.
        """

        self._logger.debug('__getStartupScript()')

        if not os.path.exists(configDict['startup_script_template']):
            self._logger.warning(
                'User data script template [%s] does not'
                ' exist. Compute Engine instances will be started without'
                ' user data', configDict['startup_script_template'])

            return None

        templateFileName = configDict['startup_script_template']

        installerIp = self.installer_public_ipaddress

        config = {
            'installerHostName': self.installer_public_hostname,
            'installerIp': installerIp,
            'adminport': str(self._cm.getAdminPort()),
            'scheme': self._cm.getAdminScheme(),
            'cfmuser': self._cm.getCfmUser(),
            'cfmpassword': self._cm.getCfmPassword(),
            'override_dns_domain': str(configDict['override_dns_domain']),
            'dns_domain': quoted_val(configDict['dns_domain']),
            'dns_options': quoted_val(configDict['dns_options'])
            if configDict.get('dns_options') else None,
            'dns_nameservers': _get_encoded_list(
                configDict['dns_nameservers']),
        }

        with open(templateFileName) as fp:
            result = ''

            for inp in fp.readlines():
                if inp.startswith('### SETTINGS'):
                    result += '''\
installerHostName = '%(installerHostName)s'
installerIpAddress = '%(installerIp)s'
port = %(adminport)s
cfmUser = '%(cfmuser)s'
cfmPassword = '%(cfmpassword)s'

# DNS settings
override_dns_domain = %(override_dns_domain)s
dns_options = %(dns_options)s
dns_search = %(dns_domain)s
dns_nameservers = %(dns_nameservers)s
''' % (config)
                else:
                    result += inp

        return result

    def __init_new_node(self, session: dict,
                        name: str,
                        hardwareprofile: HardwareProfile,
                        softwareprofile: SoftwareProfile,
                        *,
                        metadata: Optional[dict] = None) -> Node: \
            # pylint: disable=no-self-use
        # Initialize Node object for insertion into database

        return Node(
            name=name,
            state=state.NODE_STATE_LAUNCHING,
            hardwareprofile=hardwareprofile,
            softwareprofile=softwareprofile,
            vcpus=metadata.get('vcpus') if metadata else None,
            addHostSession=self.addHostSession,
        )

    def __createNodes(self, session: dict, dbSession: Session,
                      dbHardwareProfile: HardwareProfile,
                      dbSoftwareProfile: SoftwareProfile, *,
                      count: int = 1) -> List[Node]: \
            # pylint: disable=unused-argument
        """
        Raises:
            ConfigurationError
            NetworkNotFound
        """

        self._logger.debug('__createNodes()')

        # use resource adapter 'vcpus' override, otherwise fallback to
        # vm type-based lookup
        vcpus = session['config'].get('vcpus')
        if vcpus is None:
            vcpus = self.get_instance_size_mapping(session['config']['type'])

        # return list of newly initialized nodes
        return [
            self.__init_new_node(
                session,
                self.__generate_node_name(
                    session, dbSession, dbHardwareProfile
                ),
                dbHardwareProfile,
                dbSoftwareProfile,
                metadata={
                    'vcpus': vcpus,
                },
            ) for _ in range(count)
        ]

    def __generate_node_name(self, session: dict, dbSession: Session,
                             hardwareprofile: HardwareProfile) -> str:
        fqdn = self.addHostApi.generate_node_name(
            dbSession, hardwareprofile.nameFormat,
            randomize=session['config']['randomize_hostname'],
            dns_zone=self.private_dns_zone)

        hostname, _ = fqdn.split('.', 1)
        node_name = hostname

        if session['config']['override_dns_domain']:
            if session['config']['dns_domain'] == self.private_dns_zone:
                node_name = fqdn

            else:
                node_name = '{}.{}'.format(hostname, self.private_dns_zone)

        elif '.' in self.installer_public_hostname:
            node_name = '{}.{}'.format(
                hostname, self.installer_public_hostname.split('.', 1)[1])

        return node_name

    def __process_error_response(self, instance_name: str, result: dict):
        """
        Raises:
            CommandFailed
        """

        logmsg = ', '.join(
            '%s (%s)' % (error['message'], error['code'])
            for error in result['error']['errors'])

        excmsg = ', '.join(
            '%s' % (error['message'])
            for error in result['error']['errors'])

        self._logger.error(
            'Error launching instance [%s]: %s', instance_name, logmsg)

        raise CommandFailed(
            'Google Compute Engine reported error: \"%s\"' % (excmsg))

    def __build_node_request_queue(self, nodes: List[Node]) -> List[dict]: \
            # pylint: disable=no-self-use
        return [dict(node=node, status='pending') for node in nodes]

    def __write_user_data(self, node: Node, user_data_yaml: str) -> None:
        dstdir = get_cloud_init_path(node.name.split('.', 1)[0])

        if not os.path.exists(dstdir):
            self._logger.debug('Creating cloud-init directory [%s]', dstdir)

            os.makedirs(dstdir)

        with open(os.path.join(dstdir, 'user-data'), 'w') as fp:
            fp.write(user_data_yaml)

    def __get_instance_metadata(self, session: dict, pending_node: dict) \
            -> List[Tuple[str, Any]]:
        node = pending_node['node']

        metadata = self.__get_metadata(session)

        # Default to using startup-script
        if 'startup_script_template' in session['config']:
            startup_script = self.__getStartupScript(session['config'])

            if startup_script:
                metadata.append(('startup-script', startup_script))

            # Uncomment this to create local copy of startup script
            # tmpfn = '/tmp/startup_script.py.%s' % (dbNode.name)
            # with open(tmpfn, 'w') as fp:
            #     fp.write(startup_script + '\n')
        else:
            self._logger.warning(
                'Startup script template not defined for hardware'
                ' profile [%s]', node.hardwareprofile.name)

        if session['config']['override_dns_domain']:
            metadata.append(('hostname', node.name))

        return metadata

    def __launch_instances(self, session: dict, dbSession: Session,
                           node_requests: List[dict],
                           addNodesRequest: dict):
        """Launch Google Compute Engine instance for each node request
        """

        self._logger.debug('__launch_instances()')

        # 'extra_args' is a dict passed from addNodeRequest containing the
        # arguments passed through 'add-nodes ... --extra-arg <key:key=value>'
        common_launch_args = self.__get_common_launch_args(
            session,
            extra_args=addNodesRequest.get('extra_args')
        )

        self._logger.debug(
            'Preemptible flag {} enabled'.format(
                '*is*'
                if common_launch_args['preemptible'] else 'is not'
            )
        )

        for node_request in node_requests:
            node_request['instance_name'] = get_instance_name_from_host_name(
                node_request['node'].name)

            try:
                metadata = self.__get_instance_metadata(session, node_request)
            except Exception:
                self._logger.exception(
                    'Error getting metadata for instance [%s] (%s)',
                    node_request['instance_name'],
                    node_request['node'].name
                    )

                raise

            # Start the Compute Engine instance here

            #
            # Persistent disks must be created before the instances
            #
            persistent_disks = self.__process_added_disk_changes(
                session, node_request)

            #
            # 'disksize' setting is ignored if disks/partitions are defined
            # in the software profile.
            #
            if not persistent_disks:
                persistent_disks.append({
                    'sizeGb': session['config']['disksize'],
                })

            #
            # Now create the instances...
            #
            try:
                node_request['response'] = self.__launch_instance(
                    session,
                    node_request['instance_name'],
                    metadata,
                    common_launch_args,
                    persistent_disks=persistent_disks
                )

            except Exception:
                self._logger.error(
                    'Error launching instance [%s]',
                    node_request['instance_name']
                    )

                raise

            instance_metadata = [
                InstanceMetadata(
                    key='zone',
                    value=node_request['response']['zone'].split('/')[-1]
                ),
            ]

            if common_launch_args.get('preemptible', False):
                # store metadata indicating vm was launched as preemptible
                instance_metadata.append(
                    InstanceMetadata(
                        key='gce:scheduling',
                        value='preemptible'
                    )
                )

            adapter_cfg = self.load_resource_adapter_config(
                dbSession,
                addNodesRequest.get('resource_adapter_configuration')
            )

            # Update persistent mapping of node -> instance
            node_request['node'].instance = InstanceMapping(
                instance=node_request['instance_name'],
                instance_metadata=instance_metadata,
                resource_adapter_configuration=adapter_cfg
            )

        # Wait for instances to launch
        self.__wait_for_instances(session, node_requests)

    def __get_common_launch_args(
            self, session: dict, *,
            extra_args: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Return dict containing vm launch arguments aggregated from
        resource adapter configuration and extra_args

        :raises OperationFailed:
        """

        common_launch_args: Dict[str, Any] = {}

        if 'image' in session['config']:
            if '/' in session['config']['image']:
                image_project, image_name = \
                    session['config']['image'].split('/', 1)
            else:
                image_project = None
                image_name = session['config']['image']

            common_launch_args['image_url'] = \
                self.__gce_get_image_by_name(
                    session['connection'].svc, image_project, image_name
                )
        elif 'image_family' in session['config']:
            if '/' in session['config']['image_family']:
                image_family_project, image_family = \
                    session['config']['image_family'].split('/', 1)
            else:
                image_family_project = None
                image_family = session['config']['image_family']

            common_launch_args['image_url'] = \
                self.__gce_get_image_family_url(
                    session['connection'].svc, image_family_project,
                    image_family,
                )
        else:
            common_launch_args['image_url'] = session['config']['image_url']

        common_launch_args['network_interfaces'] = \
            self.__get_network_interface_definitions(
                session['config']['project'],
                session['config']['region'],
                session['config']['networks'],
            )

        common_launch_args['preemptible'] = \
            'preemptible' in extra_args if extra_args else False

        if 'accelerators' in session['config']:
            common_launch_args['accelerators'] = \
                    _parse_accelerator(session['config']['accelerators'])

        return common_launch_args

    def __process_added_disk_changes(self, session: dict,
                                     node_request: dict) -> List[dict]:
        persistent_disks = []

        node = node_request['node']

        # Apply any disk changes to VM before attempting to start
        diskChanges = self.sanApi.discoverStorageChanges(node)

        # Iterate over added disks in order
        for addedDiskNumber, disk in \
            [(int(disk_index), diskChanges['added'][disk_index])
             for disk_index in sorted(diskChanges['added'].keys())]:

            storageAdapter = disk['adapter']
            sizeMb = disk['size']
            sanVolume = disk['sanVolume']

            sizeGb = sizeMb / 1000

            if storageAdapter != 'default':
                # Ignore any non-default storage resources
                continue

            # Do physical disk create
            volName = get_disk_volume_name(
                node_request['instance_name'], addedDiskNumber)

            # Instance boot disk is created automatically when instance
            # is launched, so do not create one...
            if addedDiskNumber > 1:
                # Create persistent disk
                self._logger.debug(
                    'Creating data disk: (%s, %s, %s Gb)',
                    node.name, volName, sizeGb
                    )

                self.__create_persistent_disk(session, volName, sizeGb)

                # TODO: check result
                result = _blocking_call(
                    session['connection'].svc,
                    session['config']['project'],
                    None,
                    polling_interval=session['config']['sleeptime'])

                persistent_disks.append({
                    'name': volName,
                    'sizeGb': sizeGb,
                    'link': result['targetLink']
                })
            else:
                persistent_disks.append({
                    'name': volName,
                    'sizeGb': sizeGb
                })

            # Add placeholder to the storage subsystem so that drives
            # managed by GCE are tracked
            self.sanApi.addDrive(
                node, storageAdapter, addedDiskNumber, sizeMb, sanVolume)

        return persistent_disks

    def __mark_node_request_failed(self, node_request: dict,
                                   status: str = 'error',
                                   message: Optional[str] = None) -> None:
        node_request['status'] = 'error'
        if message:
            node_request['message'] = message

    def __wait_for_instance(self, session: dict, pending_node_request: dict):
        try:
            if gevent_wait_for_instance(session, pending_node_request):
                # VM launched successfully
                try:
                    self.__instance_post_launch(session, pending_node_request)
                except Exception as exc:  # noqa pylint: disable=broad-except
                    msg = 'Internal error: post-launch action for VM [%s]' % (
                        pending_node_request['instance_name']
                    )

                    # instance post-launch action raised an exception
                    self._logger.error(msg)

                    self.__mark_node_request_failed(
                        pending_node_request,
                        message='{} (exception {}: {})'.format(
                            msg, exc.__class__.__name__, exc
                        )
                    )

                    # delete vm
                    self.__deleteInstance(
                        session,
                        pending_node_request['node']
                    )
            else:
                # vm failed to launch successfully
                result = pending_node_request['result']

                logmsg = ', '.join(
                    '%s (%s)' % (error['message'], error['code'])
                    for error in result['error']['errors'])

                errmsg = 'Google Compute Engine error: \"%s\"' % (logmsg)

                self._logger.error(errmsg)

                self.__mark_node_request_failed(
                    pending_node_request,
                    message=logmsg
                )
        except Exception as exc:  # noqa pylint: disable=broad-except
            self._logger.exception(
                '_blocking_call() failed on instance [%s]',
                pending_node_request['instance_name']
            )

            self.__mark_node_request_failed(
                pending_node_request,
                message=str(exc)
            )

    def wait_worker(self, session: dict, queue: JoinableQueue) -> NoReturn:
        # greenlet to wait on queue and process VM launches
        while True:
            pending_node_request = queue.get()

            try:
                self.__wait_for_instance(session, pending_node_request)
            finally:
                queue.task_done()

    def __wait_for_instances(self, session: dict,
                             node_request_queue: List[dict]) -> None:
        """
        Raises:
            CommandFailed
        """
        self._logger.debug('__wait_for_instances()')

        queue = JoinableQueue()

        launch_requests = len(node_request_queue)
        worker_thread_count = 10 if launch_requests > 10 else launch_requests

        # Create greenlets
        for _ in range(worker_thread_count):
            gevent.spawn(self.wait_worker, session, queue)

        for node_request in node_request_queue:
            if 'response' not in node_request:
                # Ignore failed launch
                continue

            queue.put(node_request)

        queue.join()

        # Raise exception if any instances failed
        for node_request in node_request_queue:
            if node_request['status'] == 'error':
                self._logger.error('Message: %s', node_request['message'])

                raise CommandFailed(
                    'Fatal error launching one or more instances')

    def __instance_post_launch(self, session: dict, node_request: dict) \
            -> None:
        """Called after VM has been successfully launched and is in running
        state.
        """

        instance_name = node_request['instance_name']
        node = node_request['node']

        vm_inst = self.gce_get_vm(session, instance_name)
        if vm_inst is None:
            self._logger.error(
                'VM [%s] went away after launching; nothing to do',
                instance_name
            )

            return

        # Create nics for instance
        node.state = state.NODE_STATE_INSTALLED

        internal_ip = self.__get_instance_internal_ip(vm_inst)
        if internal_ip is None:
            self._logger.error(
                'VM [%s] does not have an IP address (???)', vm_inst
            )

            return

        node.nics.append(Nic(ip=internal_ip, boot=True))

        # Call pre-add-host to set up DNS record
        self._pre_add_host(
            node.name,
            node.hardwareprofile.name,
            node.softwareprofile.name,
            internal_ip,
        )

    def __get_instance_internal_ip(self, instance: dict) -> Optional[str]: \
            # pylint: disable=no-self-use
        for network_interface in instance['networkInterfaces']:
            return network_interface['networkIP']

        return None

    def __addActiveNodes(self, session: dict, dbSession: Session,
                         addNodesRequest: dict,
                         dbHardwareProfile: HardwareProfile,
                         dbSoftwareProfile: SoftwareProfile) -> List[Node]:
        """
        Create active nodes
        """

        self._logger.debug('__addActiveNodes()')

        count = addNodesRequest.get('count', 1)

        self._logger.info(
            'Creating %d node(s) for mapping to Compute Engine instance(s)',
            count
        )

        # Create node entries in the database
        nodes = self.__createNodes(
            session, dbSession, dbHardwareProfile, dbSoftwareProfile,
            count=count
        )

        dbSession.add_all(nodes)
        dbSession.commit()

        self._logger.debug('Initialized node(s): %s', format_node_list(nodes))

        try:
            node_request_queue = self.__build_node_request_queue(nodes)
        except Exception:
            self._logger.exception('Error building node request map')

            for node in nodes:
                dbSession.delete(node)

                self.__node_cleanup(node)

            dbSession.commit()

            raise

        # Launch instances
        try:
            self.__launch_instances(
                session, dbSession, node_request_queue, addNodesRequest)
        except Exception:
            self.__post_launch_action(dbSession, session, node_request_queue)

            raise

        return self.__post_launch_action(
            dbSession, session, node_request_queue)

    def __post_launch_action(self, dbSession: Session, session: dict,
                             node_request_queue: List[dict]):
        count = len(node_request_queue)

        result = []
        completed = 0

        # Find all instances that failed to launch and clean them up

        for node_request in node_request_queue:
            if node_request['status'] != 'success':
                if 'instance_name' in node_request:
                    self._logger.error(
                        'Cleaning up failed instance [%s]'
                        ' (node [%s])',
                        node_request['instance_name'],
                        node_request['node'].name
                    )
                else:
                    self._logger.error(
                        'Cleaning up node [%s]', node_request['node'])

                self.__node_cleanup(node_request['node'])

                dbSession.delete(node_request['node'])
            else:
                result.append(node_request['node'])

                # Mark node as 'Provisioned' after being successfully launched
                node_request['node'].state = state.NODE_STATE_PROVISIONED
                self.fire_provisioned_event(node_request['node'])

                completed += 1

        dbSession.commit()

        if completed and completed < count:
            warnmsg = ('only %d of %d requested instances launched'
                       ' successfully' % (completed, count))

            self._logger.warning(warnmsg)

        return result

    def __get_metadata(self, session: dict) -> List[Tuple[str, Any]]:
        metadata = []

        default_ssh_user = session['config']['default_ssh_user'] \
            if 'default_ssh_user' in session['config'] else 'centos'

        fn = '/root/.ssh/id_rsa.pub'

        if os.path.exists(fn):
            with open(fn) as fp:
                metadata.append(
                    ('sshKeys', '%s:' % (default_ssh_user) + fp.read()))
        else:
            self._logger.info('Public SSH key (%s) not found', fn)

        metadata.append(('tortuga_installer_public_hostname',
                         self.installer_public_hostname))

        metadata.append(('tortuga_installer_public_ipaddress',
                         self.installer_public_ipaddress))

        return metadata

    def __get_disk_type_resource_url(self, project: str, zone: str, ssd: bool):
        project_url = '%s%s' % (GCE_URL, project)

        disk_type = 'pd-ssd' if ssd else 'pd-standard'

        return '%s/zones/%s/diskTypes/%s' % (project_url, zone, disk_type)

    def __create_persistent_disk(self, session: dict, volume_name: str,
                                 size_in_Gb: int) -> None:
        self._logger.debug(
            'Creating persistent disk [%s] (size [%s])',
            volume_name,
            size_in_Gb
        )

        # Create the instance
        session['connection'].svc.disks().insert(
            project=session['config']['project'],
            body={
                'kind': 'compute#disk',
                'name': volume_name,
                'sizeGb': size_in_Gb,
            },
            zone=session['config']['zone']
        ).execute()

    def __launch_instance(self, session: dict, instance_name: str,
                          metadata: List[Tuple[str, Any]],
                          common_launch_args, *,
                          persistent_disks: List[dict]) -> dict:
        # This is the lowest level interface to Google Compute Engine
        # API to launch an instance.  It depends on 'session' (dict) to
        # contain settings, but this could easily be mocked.

        self._logger.debug(
            '__launch_instance(): instance_name=[%s]', instance_name)

        connection = session['connection']

        config = session['config']

        # Construct URLs
        project_url = '%s%s' % (GCE_URL, config['project'])

        machine_type_url = '%s/zones/%s/machineTypes/%s' % (
            project_url, config['zone'], config['type'])

        instance = {
            'name': instance_name,
            'tags': {
                'items': ['tortuga'] + config['tags'],
            },
            'machineType': machine_type_url,
            'disks': [
                {
                    'type': 'PERSISTENT',
                    'boot': 'true',
                    'mode': 'READ_WRITE',
                    # 'deviceName': instance_name,
                    'autoDelete': True,
                    'initializeParams': {
                        'sourceImage': common_launch_args['image_url'],
                        'diskSizeGb': persistent_disks[0]['sizeGb'],
                        'diskType': self.__get_disk_type_resource_url(
                            config['project'],
                            config['zone'],
                            config['ssd']
                        ),
                    }
                },
            ],
            'networkInterfaces': common_launch_args['network_interfaces'],
        }

        # only add 'preemptible' flag if enabled
        if common_launch_args.get('preemptible', False):
            instance['scheduling'] = {
                'preemptible': common_launch_args['preemptible']
            }

        if common_launch_args.get('accelerators', False):
            guest_accelerators = []
            for accelerator in common_launch_args['accelerators']:
                full_type = "/projects/%s/zones/%s/acceleratorTypes/%s" % \
                        (config['project'], config["zone"], accelerator["acceleratorType"])
                full_accelerator = {"acceleratorType": full_type,
                        "acceleratorCount": accelerator["acceleratorCount"]}
                guest_accelerators.append(full_accelerator)
            instance['guestAccelerators'] = guest_accelerators
            # Also need to disable migration and restart policy for GPU nodes
            scheduling = instance.get('scheduling',{})
            scheduling["onHostMaintenance"] = "TERMINATE"
            instance['scheduling'] = scheduling
        # Add any persistent (data) disks to the instance; ignore the first
        # disk in the disk because it's automatically created when the
        # instance is launched.

        # TODO: should the 'autoDelete' be exposed as a configurable?
        for persistent_disk in persistent_disks[1:] or []:
            instance['disks'].append({
                'type': 'PERSISTENT',
                'autoDelete': True,
                'source': persistent_disk['link'],
            })

        instance['metadata'] = {
            'kind': 'compute#metadata',
            'items': [dict(key=key, value=value) for key, value in metadata],
        }

        # Create the instance
        return connection.svc.instances().insert(
            project=config['project'],
            body=instance,
            zone=config['zone']
        ).execute()

    def __get_network_interface_definitions(self, project: str, region: str,
                                            networks: List[str]) -> list:
        """
        Parse network(s) from config, return list of dicts containing
        network interface spec

        :raises ConfigurationError:
        """

        network_interfaces = []

        primary_intfc = None

        for network in networks:
            network_interface, network_flags = \
                self.__get_network_interface(project, region, network)

            # ensure only one interface is marked as primary
            primary_value = network_flags.get('primary')
            if primary_value is not None and primary_value:
                if primary_intfc is not None:
                    raise ConfigurationError(
                        'Only one interface may be primary: {} is already'
                        ' marked as primary'.format(network[0])
                    )

                primary_intfc = network

            # honor 'ext[ernal]' network configuration flag
            if is_network_flag_set(network_flags, flag='external'):
                set_external_network_access(network_interface)

            network_interfaces.append(network_interface)

        # maintain backwards compatibility by ensuring the default interface
        # has external access. The semantics of this might need to change
        # in more advanced network configurations.
        enable_external_network_access(networks, network_interfaces)

        return network_interfaces

    def __get_network_interface(self, default_project: str,
                                default_region: str, network: str) \
            -> Tuple[dict, dict]:
        """
        Returns properly formed dict containing network interface
        configuration.
        """

        network_def, subnet_def, network_args = network

        # pay particular attention to the ordering reversal of input vs. output
        project, network = \
            split_forward_slash_value(network_def, default_project)

        network_interface = {
            'network': '%s%s/global/networks/%s' % (GCE_URL, project, network),
        }

        if subnet_def:
            # pay particular attention to the ordering reversal of input vs.
            # output
            region, subnetwork = \
                split_forward_slash_value(subnet_def, default_region)

            network_interface['subnetwork'] = \
                '%s%s/regions/%s/subnetworks/%s' % (
                    GCE_URL, project, region, subnetwork
                )

        return network_interface, get_network_flags(network_args)

    def gce_get_vm(self, gce_session: dict, instance_name: str) \
            -> Optional[dict]:
        """Call GCE to retrieve vm
        """

        connection = gce_session['connection']

        try:
            return connection.svc.instances().get(
                project=gce_session['config']['project'],
                zone=gce_session['config']['zone'],
                instance=instance_name
            ).execute()
        except apiclient.errors.HttpError as ex:
            # We can safely ignore a simple 404 error indicating the instance
            # does not exist.
            if ex.resp.status != 404:
                # Process JSON response content
                try:
                    error_resp = json.loads(ex.content)

                    self._logger.error(
                        'Unable to get Compute Engine instance %s'
                        ' (code: %s, message: %s)',
                        instance_name,
                        error_resp['error']['code'],
                        error_resp['error']['message']
                    )
                except ValueError:
                    # Malformed JSON in response
                    self._logger.error(
                        'Unable to get Compute Engine instance %s'
                        ' (JSON parsing error)', instance_name
                    )

            # If an exception was raised while attempting to get the instance,
            # return None to inform the caller that it is not available.
            response = None

        return response

    def __deleteInstance(self, session: dict, node: Node) -> None:
        """
        Raises:
            CommandFailed
        """

        instance_name = get_instance_name_from_host_name(node.name)

        self._logger.debug(
            '__deleteInstance(): instance_name=[%s]',
            instance_name
        )

        project, zone = self.__get_project_and_zone_metadata(node)

        project_arg = project \
            if project is not None else session['config']['project']

        zone_arg = zone if zone is not None else session['config']['zone']

        try:
            initial_response = \
                session['connection'].svc.instances().delete(
                    project=project_arg,
                    zone=zone_arg,
                    instance=instance_name
                ).execute()

            self._logger.debug(
                '__deleteInstance(): initial_response=[%s]',
                initial_response
            )

            # Wait for instance to be deleted
            # _blocking_call(
            #     session['connection'].svc,
            #     session['config']['project'], initial_response,
            #     polling_interval=session['config']['sleeptime'])
        except apiclient.errors.HttpError as ex:
            if ex.resp['status'] == '404':
                # Specified instance not found; nothing we can do there...
                self._logger.warning('Instance [%s] not found', instance_name)
            else:
                self._logger.debug(
                    '__deleteInstance(): ex.resp=[%s],'
                    ' ex.content=[%s]', ex.resp, ex.content)

                raise CommandFailed(
                    'Error deleting Compute Engine instance [%s]' % (
                        instance_name))

    def rebootNode(self, nodes: List[Node],
                   bSoftReset: bool = False) -> None: \
            # pylint: disable=unused-argument
        """Reboot the given node

        TODO: this should be an async task
        """

        for node in nodes:
            self._logger.debug('rebootNode(): node=[%s]', node.name)

            gce_session = self.get_gce_session(
                node.instance.resource_adapter_configuration.name
            )

            instance_name = get_instance_name_from_host_name(node.name)

            project, zone = self.__get_project_and_zone_metadata(
                node
            )

            project_arg = project \
                if project is not None else \
                gce_session['config']['project']

            zone_arg = zone if zone is not None else \
                gce_session['config']['zone']

            try:
                initial_response = \
                    gce_session['connection'].svc.instances().reset(
                        project=project_arg, zone=zone_arg,
                        instance=instance_name
                    ).execute()

                self._logger.debug(
                    'rebootNode(): initial_response=[%s]',
                    initial_response
                )

                # Wait for instance to be rebooted
                _blocking_call(
                    gce_session['connection'].svc,
                    gce_session['config']['project'],
                    initial_response,
                    polling_interval=gce_session['config']['sleeptime']
                )

                self._logger.debug(f'Instance [%s] rebooted', node.name)
            except apiclient.errors.HttpError as ex:
                if ex.resp['status'] == '404':
                    # Specified instance not found; nothing we can do
                    # there...
                    self._logger.warning(
                        'Instance [%s] not found', instance_name)
                else:
                    self._logger.debug(
                        'rebootNode(): ex.resp=[%s],'
                        ' ex.content=[%s]', ex.resp, ex.content)

                    raise CommandFailed(
                        'Error rebooting Compute Engine instance [%s]' % (
                            instance_name))

    def get_node_vcpus(self, name: str) -> int:
        """
        Return number of vcpus for node. Value of 'vcpus' configured
        in resource adapter configuration takes precedence over file
        lookup.

        Raises:
            ResourceNotFound

        :param name: node name
        :return: number of vcpus
        :returntype: int

        """
        #
        # Default to zero, because if for some reason the node can't be found
        # (i.e. it was deleted in the background), then it will not be using
        # any cpus
        #
        vcpus = 0

        try:
            configDict = self.get_node_resource_adapter_config(
                NodesDbHandler().getNode(self.session, name)
            )

            vcpus = configDict.get('vcpus', 0)
            if not vcpus:
                vcpus = self.get_instance_size_mapping(configDict['type'])

        except NodeNotFound:
            pass

        return vcpus

    def __gce_get_image_by_name(self, svc, image_project: str,
                                image_name: str) -> str:
        """
        :raises OperationFailed: unable to find image
        """

        try:
            result = svc.images().get(
                project=image_project, image=image_name).execute()

            return result['selfLink']
        except googleapiclient.errors.HttpError as exc:

            errors = self.__process_exception(exc.content)

            if errors:
                self._logger.error(
                    'The following error(s) were reported:'
                )

                for err_message, err_reason, err_domain in errors:
                    self._logger.error(
                        '%s (reason: %s, domain: %s)',
                        err_message,
                        err_reason,
                        err_domain
                    )

        raise OperationFailed('Error reported by Google Compute Engine')

    def __gce_get_image_family_url(self, svc, image_family_project: str,
                                   image_family: str) -> str:
        """
        :raises OperationFailed:
        """

        try:
            result = svc.images().getFromFamily(
                project=image_family_project,
                family=image_family,
            ).execute()

            return result['selfLink']
        except googleapiclient.errors.HttpError as exc:

            errors = self.__process_exception(exc.content)

            if errors:
                self._logger.error(
                    'The following error(s) were reported:'
                )

                for err_message, err_reason, err_domain in errors:
                    self._logger.error(
                        '%s (reason: %s, domain: %s)',
                        err_message,
                        err_reason,
                        err_domain
                    )

        raise OperationFailed('Error reported by Google Compute Engine')


class GoogleComputeEngine:
    def __init__(self, svc=None):
        self._svc = None
        self.svc = svc

    @property
    def svc(self):
        return self._svc

    @svc.setter
    def svc(self, value):
        self._svc = value


def gceAuthorize_from_json(json_filename: Optional[str] = None) \
        -> GoogleComputeEngine:
    """Returns GCE session object
    """

    url = 'https://www.googleapis.com/auth/compute'

    # Only try and load the file if it exists
    if json_filename and os.path.isfile(json_filename):
        credentials = service_account.Credentials.from_service_account_file(
            json_filename, scopes=[url])
    else:
        # Fallback to machine credentials
        credentials = compute_engine.Credentials()

    svc = googleapiclient.discovery.build(
        'compute', API_VERSION, credentials=credentials)

    return GoogleComputeEngine(svc=svc)


def _blocking_call(gce_service, project_id, response,
                   polling_interval=Gce.DEFAULT_SLEEP_TIME):
    status = response['status']

    while status != 'DONE' and response:
        operation_id = response['name']

        # Identify if this is a per-zone resource
        if 'zone' in response:
            zone_name = response['zone'].split('/')[-1]

            response = gce_service.zoneOperations().get(
                project=project_id,
                operation=operation_id,
                zone=zone_name
            ).execute()
        else:
            response = gce_service.globalOperations().get(
                project=project_id, operation=operation_id
            ).execute()

        if response:
            status = response['status']

            if status != 'DONE':
                time.sleep(polling_interval)

    return response


def _gevent_blocking_call(gce_service, project_id, response,
                          polling_interval: int = Gce.DEFAULT_SLEEP_TIME):
    """
    polling_interval is seconds
    """

    status = response['status']

    attempt = 0

    max_sleep_time = 5000

    while status != 'DONE' and response:
        operation_id = response['name']

        # Identify if this is a per-zone resource
        if 'zone' in response:
            zone_name = response['zone'].split('/')[-1]

            response = gce_service.zoneOperations().get(
                project=project_id,
                operation=operation_id,
                zone=zone_name
            ).execute()
        else:
            response = gce_service.globalOperations().get(
                project=project_id, operation=operation_id
            ).execute()

        if response:
            status = response['status']

            if status != 'DONE':
                if attempt > 0:
                    temp = min(max_sleep_time,
                               (polling_interval * 1000) * 2 ** attempt)

                    sleeptime = \
                        (temp / 2 + random.randint(0, temp / 2)) / 1000.0
                else:
                    # Set sleep time after launch to 10s
                    sleeptime = 10

                gevent.sleep(sleeptime)

        attempt += 1

    return response


def gevent_wait_for_instance(session, pending_node_request):
    result = _gevent_blocking_call(
        session['connection'].svc,
        session['config']['project'],
        pending_node_request['response'],
        polling_interval=session['config']['sleeptime']
    )

    pending_node_request['status'] = 'error' \
        if 'error' in result else 'success'

    pending_node_request['result'] = result

    return pending_node_request['status'] == 'success'


def is_running_on_gce() -> bool:
    p = subprocess.Popen('dmidecode -s bios-vendor', shell=True,
                         stdout=subprocess.PIPE)
    stdout, _ = p.communicate()

    return stdout.rstrip() == 'Google'


def _get_encoded_list(items):
    """Return Python list encoded in a string"""
    return '[' + ', '.join(['\'%s\'' % (item) for item in items]) + ']' \
        if items else '[]'


def quoted_val(value):
    return '\'{0}\''.format(value)


def split_forward_slash_value(value, default):
    """
    Returns '<default>/<value>' if value does not contain '/', otherwise
    '<token1>/<token2>'
    """

    if '/' in value:
        return tuple(value.split('/', 1))

    return default, value


def split_three_item_value(value) -> Tuple[str, Optional[str], Optional[str]]:
    token_count = value.count(':')

    if token_count == 2:
        return value.split(':', token_count)

    if token_count == 1:
        token1, token2 = value.split(':', token_count)

        return token1, token2, None

    return value, None, None


def get_network_flags(network_args: str) -> Dict[str, Any]:
    """Parse network flags (options) from network configuration.

    Flags are processed in order... last one takes prescedence

    :raises ConfigurationError:
    """

    if network_args is None:
        return {}

    result = {}

    for network_arg in network_args.split(';'):
        if not network_arg:
            # handle empty network args
            continue

        if network_arg.lower().startswith('ext'):
            result['external'] = True
        elif network_arg.lower().startswith('noext'):
            result['external'] = False
        elif network_arg.lower().startswith('pri'):
            result['primary'] = True
        else:
            raise ConfigurationError(
                'Invalid network flag: [{}]'.format(network_arg)
            )

    return result


def set_external_network_access(network_interface):
    network_interface['accessConfigs'] = EXTERNAL_NETWORK_ACCESS_CONFIG


def is_network_flag_set(network_flags, *, flag: str, default: bool = False):
    return network_flags.get(flag, default)


def enable_external_network_access(networks, network_interfaces):
    """Enable 'external' access (public IP) if only one network interface is
    defined and if network is 'default' or not external access is not
    explicitly set.
    """

    if len(networks) != 1 or not network_interfaces:
        return

    network_def, _, network_args = networks[0]

    if network_def == 'default' or \
            is_network_flag_set(
                get_network_flags(network_args),
                flag='external',
                default=True
            ):
        set_external_network_access(network_interfaces[0])


def format_node_list(nodes: List[Node]) -> str:
    """Format list of Node objects suitable for user output or logging
    """
    if len(nodes) > 3:
        return '{}..{}'.format(nodes[0].name, nodes[-1].name)

    return ' '.join([node.name for node in nodes])

def _parse_accelerator(accelerator_string: str) -> List[dict]:
    accel = []
    for s in accelerator_string.split(","):
        parts = s.strip().split(":")
        if len(parts) != 2:
            raise ConfigurationError("Invalid Accelerator Configuration")
        accelerator, count = parts
        accel.append({"acceleratorType":accelerator, "acceleratorCount": int(count)})
    return accel
