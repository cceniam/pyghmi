# Copyright 2025 Lenovo Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import pyghmi.redfish.oem.generic as generic
import pyghmi.constants as pygconst

healthlookup = {
    'ok': pygconst.Health.Ok,
    'critical': pygconst.Health.Critical
}

class OEMHandler(generic.OEMHandler):
    def get_health(self, fishclient, verbose=True):
        rsp = self._do_web_request('/redfish/v1/Chassis/chassis1')
        health = rsp.get('Status', {}).get('Health', 'Unknown').lower()
        health = healthlookup.get(health, pygconst.Health.Critical)
        return {'health': health}

    def get_description(self, fishclient):
        return {'height': 13, 'slot': 0, 'slots': [8, 2]}
