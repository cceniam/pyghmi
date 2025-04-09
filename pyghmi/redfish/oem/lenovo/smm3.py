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

import copy
import os
import pyghmi.redfish.oem.generic as generic
import pyghmi.constants as pygconst
import pyghmi.util.webclient as webclient
import pyghmi.exceptions as exc
import time

healthlookup = {
    'ok': pygconst.Health.Ok,
    'critical': pygconst.Health.Critical
}

def _baytonumber(bay):
    try:
        return int(bay)
    except ValueError:
        if len(bay) == 2:
            # Treat a hexadecimal system as a leading decimal digit and letter compile
            # 1a == slot 1, 1b == slot 2, 2a == slot 1, etc..
            try:
                tmp = int(bay, 16)
                return (2 * (tmp >> 4) - 1) + ((tmp & 15) % 10)
            except ValueError:
                return None
    return None


def _baytolabel(bay):
    try:
        baynum =  int(bay)
        if baynum < 1:
            raise exc.UnsupportedFunctionality(
                    'Reseat not supported for whole chassis')
        # need to convert to 1a, 1b, etc...
        vertidx = ((baynum - 1) // 2 + 1) << 4
        horizidx = (baynum - 1) % 2 + 10
        bayid = vertidx | horizidx
        return '{:02x}'.format(bayid)
    except ValueError:
        return bay
    return None

class OEMHandler(generic.OEMHandler):
    def get_health(self, fishclient, verbose=True):
        rsp = self._do_web_request('/redfish/v1/Chassis/chassis1')
        health = rsp.get('Status', {}).get('Health', 'Unknown').lower()
        health = healthlookup.get(health, pygconst.Health.Critical)
        return {'health': health}

    def get_system_configuration(self, hideadvanced=True, fishclient=None):
        return {}

    def get_diagnostic_data(self, savefile, progress=None, autosuffix=False):
        tsk = self._do_web_request(
            '/redfish/v1/Managers/bmc/LogServices/Dump/Actions/LogService.CollectDiagnosticData',
            {"DiagnosticDataType": "Manager"})
        taskrunning = True
        taskurl = tsk.get('@odata.id', None)
        pct = 0 if taskurl else 100
        durl = None
        while pct < 100 and taskrunning:
            status = self._do_web_request(taskurl)
            durl = status.get('AdditionalDataURI', '')
            pct = status.get('PercentComplete', 0)
            taskrunning = status.get('TaskState', 'Complete') == 'Running'
            if progress:
                progress({'phase': 'initializing', 'progress': float(pct)})
            if taskrunning:
                time.sleep(3)
        if not durl:
            for hdr in status.get('Payload', {}).get('HttpHeaders', []):
                if hdr.startswith('Location: '):

                    enturl = hdr.replace('Location: ', '')
                    entryinfo = self._do_web_request(enturl)
                    durl = entryinfo.get('AdditionalDataURI', None)
                    break
        if not durl:
            raise Exception("Failed getting service data url")
        fname = os.path.basename(durl)
        if autosuffix and not savefile.endswith('.tar.xz'):
            savefile += time.strftime('-SMM3_%Y%m%d_%H%M%S.tar.xz')
        fd = webclient.FileDownloader(self.webclient, durl, savefile)
        fd.start()
        while fd.isAlive():
            fd.join(1)
            if progress and self.webclient.get_download_progress():
                progress({'phase': 'download',
                          'progress': 100 * self.webclient.get_download_progress()})
        if fd.exc:
            raise fd.exc
        if progress:
            progress({'phase': 'complete'})
        return savefile

    def _extract_fwinfo(self, inf):
        fwi, url = inf
        currinf = {}
        buildid = fwi.get('Oem', {}).get('Lenovo', {}).get('ExtendedVersion', None)
        if buildid:
            currinf['build'] = buildid
        return currinf


    def _get_node_info(self):
        nodeinfo = self._varsysinfo
        if not nodeinfo:
            overview = self._do_web_request('/redfish/v1/')
            chassismembs = overview.get('Chassis', {}).get('@odata.id', None)
            if not chassismembs:
                return nodeinfo
            chassislist = self._do_web_request(chassismembs)
            chassismembs = chassislist.get('Members', [])
            if len(chassismembs) == 1:
                chassisurl = chassismembs[0]['@odata.id']
                nodeinfo = self._do_web_request(chassisurl)
        newnodeinfo = copy.deepcopy(nodeinfo)
        newnodeinfo['SKU'] = nodeinfo['Model']
        newnodeinfo['Model'] = 'N1380 Enclosure'
        return newnodeinfo

    def reseat_bay(self, bay):
        bayid = _baytolabel(bay)
        url = '/redfish/v1/Chassis/chassis1/Oem/Lenovo/Nodes/{}/Actions/Node.Reseat'.format(bayid)
        rsp = self._do_web_request(url, method='POST')

    def get_event_log(self, clear=False, fishclient=None):
        return super().get_event_log(clear, fishclient, extraurls=[{'@odata.id':'/redfish/v1/Chassis/chassis1/LogServices/EventLog'}])

    def get_description(self, fishclient):
        return {'height': 13, 'slot': 0, 'slots': [8, 2]}
