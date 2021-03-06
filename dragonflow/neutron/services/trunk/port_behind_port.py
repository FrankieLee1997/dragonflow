# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import netaddr
from neutron_lib.api.definitions import allowedaddresspairs as aap
from neutron_lib.callbacks import events
from neutron_lib.callbacks import registry
from neutron_lib.callbacks import resources
from neutron_lib import constants as n_constants
from neutron_lib import context
from neutron_lib.plugins import directory
from oslo_log import log

from dragonflow.db.models import trunk as trunk_models
from dragonflow.neutron.services import mixins
from dragonflow.neutron.services.trunk import driver as trunk_driver


LOG = log.getLogger(__name__)


@registry.has_registry_receivers
class DfPortBehindPortDetector(mixins.LazyNbApiMixin):

    def _detect_port_behind_port(self, a_context,
                                 updated_port, orig_port=None):
        """
        A heuristic to detect nested ports (i.e. ports behind ports).
        For each allowed-address-pair modification, scan to see if there are
        ports with those IPs/MACs. If so, these are nested ports. Create the
        relevant NB objects, and update their status in Neutron.
        """
        # TODO(oanson) We assume that the AAP is removed before the port
        updated_port_aaps = updated_port.get(aap.ADDRESS_PAIRS, [])
        orig_port_aaps = ([] if not orig_port else
                          orig_port.get(aap.ADDRESS_PAIRS, []))
        new_aaps = [aap_ for aap_ in updated_port_aaps
                    if aap_ not in orig_port_aaps]
        removed_aaps = [aap_ for aap_ in orig_port_aaps
                        if aap_ not in updated_port_aaps]
        remaining_aaps = [aap_ for aap_ in updated_port_aaps
                          if aap_ not in new_aaps]
        core_plugin = directory.get_plugin()
        new_status = trunk_driver.get_child_port_status(updated_port)
        LOG.debug('_detect_port_behind_port: id: %s '
                  'updated_port_aaps: %s orig_port_aaps: %s',
                  updated_port['id'], updated_port_aaps, orig_port_aaps)

        for pair in new_aaps:
            nested_port, segmentation_type = self._find_nested_port_and_type(
                a_context, pair, updated_port)
            if not nested_port:
                LOG.debug('_detect_port_behind_port (new): '
                          'Could not find port with ip pair %s', pair)
                continue
            cps_id = trunk_models.get_child_port_segmentation_id(
                    updated_port['id'], nested_port['id'])
            model = trunk_models.ChildPortSegmentation(
                id=cps_id,
                topic=updated_port['project_id'],
                parent=updated_port['id'],
                port=nested_port['id'],
                segmentation_type=segmentation_type,
            )
            self.nb_api.create(model)
            core_plugin.update_port_status(context.get_admin_context(),
                                           nested_port['id'], new_status)

        for pair in removed_aaps:
            nested_port, _segmentation_type = self._find_nested_port_and_type(
                a_context, pair, updated_port)
            if not nested_port:
                LOG.debug('_detect_port_behind_port (removed): '
                          'Could not find port with ip pair %s', pair)
                continue
            cps_id = trunk_models.get_child_port_segmentation_id(
                    updated_port['id'], nested_port['id'])
            model = trunk_models.ChildPortSegmentation(
                id=cps_id,
                topic=updated_port['project_id'],
            )
            self.nb_api.delete(model)
            core_plugin.update_port_status(context.get_admin_context(),
                                           nested_port['id'],
                                           n_constants.PORT_STATUS_DOWN)

        if not orig_port or updated_port['status'] == orig_port['status']:
            return  # No status change

        for pair in remaining_aaps:
            nested_port, _segmentation_type = self._find_nested_port_and_type(
                a_context, pair, updated_port)
            if not nested_port:
                LOG.debug('_detect_port_behind_port (removed): '
                          'Could not find port with ip pair %s', pair)
                continue
            # We assume the CPS instance exists.
            core_plugin.update_port_status(context.get_admin_context(),
                                           nested_port['id'], new_status)

    def _find_nested_port_and_type(self, context, pair, port):
        try:
            ip = netaddr.IPAddress(pair['ip_address'])
        except ValueError:
            return None, None  # Skip. This is a network, not a host
        mac_address = pair.get('mac_address')
        if mac_address and mac_address == port['mac_address']:
            mac_address = None
        if ip is None and mac_address is None:
            return None, None
        filters = {}
        if ip:
            filters['fixed_ips'] = {'ip_address': [str(ip)]}
        if mac_address:
            filters['mac_address'] = [str(mac_address)]
        core_plugin = directory.get_plugin()
        ports = core_plugin.get_ports(context, filters=filters)
        if ports:
            segmentation_type = (trunk_models.TYPE_MACVLAN if mac_address
                                 else trunk_models.TYPE_IPVLAN)
            return ports[0], segmentation_type
        return None, None

    @registry.receives(resources.PORT,
                       [events.AFTER_CREATE, events.AFTER_UPDATE])
    def _update_port_aap_handler(self, *args, **kwargs):
        port = kwargs['port']
        orig_port = kwargs.get('original_port')
        self._detect_port_behind_port(kwargs['context'], port, orig_port)
