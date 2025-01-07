import copy
import json
import pyghmi.redfish.oem.generic as generic
import pyghmi.exceptions as pygexc
import pyghmi.util.webclient as webclient
import zipfile
import os.path

class OEMHandler(generic.OEMHandler):

    def supports_expand(self, url):
        return True

    def get_system_power_watts(self, fishclient):
        powerinfo = fishclient._do_web_request('/redfish/v1/Chassis/1/Sensors/power_Sys_Power')
        return powerinfo['Reading']

    def _get_cpu_temps(self, fishclient):
        cputemps = []
        for reading in super()._get_cpu_temps(fishclient):
            if 'Margin' in reading['Name']:
                continue
            cputemps.append(reading)
        return cputemps

    def get_system_configuration(self, hideadvanced=True, fishclient=None):
        stgs = self._getsyscfg(fishclient)[0]
        outstgs = {}
        for stg in stgs:
            outstgs[f'UEFI.{stg}'] = stgs[stg]
        return outstgs

    def set_system_configuration(self, changeset, fishclient):
        bmchangeset = {}
        vpdchangeset = {}
        for stg in list(changeset):
            if stg.startswith('BMC.'):
                bmchangeset[stg.replace('BMC.', '')] = changeset[stg]
                del changeset[stg]
            if stg.startswith('UEFI.'):
                changeset[stg.replace('UEFI.', '')] = changeset[stg]
                del changeset[stg]
            if stg.startswith('VPD.'):
                vpdchangeset[stg.replace('VPD.', '')] = changeset[stg]
                del changeset[stg]
        if changeset:
            super().set_system_configuration(changeset, fishclient)
        if bmchangeset:
            self._set_xcc3_settings(bmchangeset, fishclient)
        if vpdchangeset:
            self._set_xcc3_vpd(vpdchangeset, fishclient)

    def _set_xcc3_vpd(self, changeset, fishclient):
        newvpd = {'Attributes': changeset}
        fishclient._do_web_request(
            '/redfish/v1/Chassis/1/Oem/Lenovo/SysvpdSettings/Actions/LenovoSysVpdSettings.SetVpdSettings',
            newvpd)


    def _set_xcc3_settings(self, changeset, fishclient):
        currsettings, reginfo = self._get_lnv_bmcstgs(fishclient)
        rawsettings = fishclient._do_web_request('/redfish/v1/Managers/1/Oem/Lenovo/BMCSettings',
                                                 cache=False)
        rawsettings = rawsettings.get('Attributes', {})
        pendingsettings = {}
        ret = self._set_redfish_settings(
            changeset, fishclient, currsettings, rawsettings,
            pendingsettings, self.lenovobmcattrdeps, reginfo,
            '/redfish/v1/Managers/1/Oem/Lenovo/BMCSettings')
        fishclient._do_web_request('/redfish/v1/Managers/1/Oem/Lenovo/BMCSettings', cache=False)
        return ret

    oemacctmap = {
        'password_reuse_count': 'MinimumPasswordReuseCycle',
        'password_change_interval':  'MinimumPasswordChangeIntervalHours',
        'password_expiration': 'PasswordExpirationPeriodDays',
        'password_complexity': 'ComplexPassword',
        }

    acctmap = {
        'password_login_failures': 'AccountLockoutThreshold',
        'password_min_length': 'MinPasswordLength',
        'password_lockout_period': 'AccountLockoutDuration',
        }

    def update_firmware(self, filename, data=None, progress=None, bank=None, otherfields=()):
        if not otherfields and bank == 'backup':
            uxzcount = 0
            otherfields = {'UpdateParameters': {"Targets": ["/redfish/v1/UpdateService/FirmwareInventory/BMC-Backup"]}}
            needseek = False
            if data and hasattr(data, 'read'):
                if zipfile.is_zipfile(data):
                    needseek = True
                    z = zipfile.ZipFile(data)
                else:
                    data.seek(0)
            elif data is None and zipfile.is_zipfile(filename):
                z = zipfile.ZipFile(filename)
            if z:
                for tmpname in z.namelist():
                    if tmpname.startswith('payloads/'):
                        uxzcount += 1
                        if tmpname.endswith('.uxz'):
                            wrappedfilename = tmpname
            if uxzcount == 1 and wrappedfilename:
                filename = os.path.basename(wrappedfilename)
                data = z.open(wrappedfilename)
            elif needseek:
                data.seek(0)
        super().update_firmware(filename, data=data, progress=progress, bank=bank, otherfields=otherfields)

    def get_bmc_configuration(self):
        settings = {}
        acctsrv = self._do_web_request('/redfish/v1/AccountService')
        for oemstg in self.oemacctmap:
            settings[oemstg] = {
                'value': acctsrv['Oem']['Lenovo'][self.oemacctmap[oemstg]]}
        for stg in self.acctmap:
            settings[stg] = {
                'value': acctsrv[self.acctmap[stg]]}
        bmcstgs = self._do_web_request('/redfish/v1/Managers/1/Oem/Lenovo/BMCSettings')
        usbeth = 'Enable' if bmcstgs['Attributes']['NetMgrUsb0Enabled'] == 'True' else 'Disable'
        settings['usb_ethernet'] = {
            'value': usbeth
        }
        fwd = 'Enable' if bmcstgs['Attributes']['NetMgrUsb0PortForwardingEnabled'] == 'True' else 'Disable'
        settings['usb_ethernet_port_forwarding'] = fwd
        mappings = []
        for idx in range(1, 11):
            if bmcstgs['Attributes']['NetMgrUsb0PortForwardingPortMapping.{}'.format(idx)] == '0,0':
                continue
            src, dst = bmcstgs['Attributes']['NetMgrUsb0PortForwardingPortMapping.{}'.format(idx)].split(',')
            mappings.append('{}:{}'.format(src,dst))
        settings['usb_forwarded_ports'] = {'value': ','.join(mappings)}
        return settings

    def set_bmc_configuration(self, changeset):
        acctattribs = {}
        usbsettings = {}
        for key in changeset:
            if isinstance(changeset[key], str):
                changeset[key] = {'value': changeset[key]}
            currval = changeset[key].get('value', None)
            if key == 'password_complexity':
                if currval.lower() in ("false", 0):
                    currval = False
                elif currval.lower() in ('true', 1):
                    currval = True
            elif key.lower().startswith('usb_'):
                if 'forwarded_ports' not in key.lower():
                    currval = currval.lower()
                    if currval and 'disabled'.startswith(currval):
                        currval = 'False'
                    elif currval and 'enabled'.startswith(currval):
                        currval = 'True'
            else:
                currval = int(currval)
            if key.lower() in self.oemacctmap:
                if 'Oem' not in acctattribs:
                    acctattribs['Oem'] = {'Lenovo': {}}
                acctattribs['Oem']['Lenovo'][
                    self.oemacctmap[key.lower()]] = currval
                if key.lower() == 'password_expiration':
                    warntime = str(int(int(currval) * 0.08))
                    acctattribs['Oem']['Lenovo'][
                        'PasswordExpirationWarningPeriod'] = warntime
            elif key.lower() in self.acctmap:
                acctattribs[self.acctmap[key.lower()]] = currval
            elif key.lower() in (
                    'usb_ethernet', 'usb_ethernet_port_forwarding',
                    'usb_forwarded_ports'):
                usbsettings[key] = currval
            else:
                raise pygexc.InvalidParameterValue(
                    '{0} not a known setting'.format(key))
        if acctattribs:
            self._do_web_request(
                '/redfish/v1/AccountService', acctattribs, method='PATCH')
            self._do_web_request('/redfish/v1/AccountService', cache=False)
        if usbsettings:
            self.apply_usb_configuration(usbsettings)

    def apply_usb_configuration(self, usbsettings):
        bmcattribs = {}
        if 'usb_forwarded_ports' in usbsettings:
            pairs = usbsettings['usb_forwarded_ports'].split(',')
            idx = 1
            for pair in pairs:
                pair = pair.replace(':', ',')
                bmcattribs[
                    'NetMgrUsb0PortForwardingPortMapping.{}'.format(
                        idx)] = pair
                idx += 1
            while idx < 11:
                bmcattribs[
                    'NetMgrUsb0PortForwardingPortMapping.{}'.format(
                        idx)] = '0,0'
                idx += 1
        if 'usb_ethernet' in usbsettings:
            bmcattribs['NetMgrUsb0Enabled'] = usbsettings['usb_ethernet']
        if 'usb_ethernet_port_forwarding' in usbsettings:
            bmcattribs[
                'NetMgrUsb0PortForwardingEnabled'] = usbsettings[
                    'usb_ethernet_port_forwarding']
        self._do_web_request(
            '/redfish/v1/Managers/1/Oem/Lenovo/BMCSettings',
            {'Attributes': bmcattribs}, method='PATCH')
        self._do_web_request(
            '/redfish/v1/Managers/1/Oem/Lenovo/BMCSettings', cache=False)

    def get_extended_bmc_configuration(self, fishclient, hideadvanced=True):
        cfgin = self._get_lnv_bmcstgs(fishclient)[0]
        cfgout = {}
        for stgname in cfgin:
            cfgout[f'BMC.{stgname}'] = cfgin[stgname]
        vpdin = self._get_lnv_vpd(fishclient)[0]
        for stgname in vpdin:
            cfgout[f'VPD.{stgname}'] = vpdin[stgname]
        return cfgout

    def _get_lnv_vpd(self, fishclient):
        currsettings, reginfo = self._get_lnv_stgs(
            fishclient, '/redfish/v1/Chassis/1/Oem/Lenovo/SysvpdSettings')
        self.lenovobmcattrdeps = reginfo[3]
        return currsettings, reginfo

    def _get_lnv_bmcstgs(self, fishclient):
        currsettings, reginfo = self._get_lnv_stgs(
            fishclient, '/redfish/v1/Managers/1/Oem/Lenovo/BMCSettings')
        self.lenovobmcattrdeps = reginfo[3]
        return currsettings, reginfo

    def _get_lnv_stgs(self, fishclient, url):
        bmcstgs = fishclient._do_web_request(url)
        bmcreg = bmcstgs.get('AttributeRegistry', None)
        extrainfo = {}
        valtodisplay = {}
        currsettings = {}
        reginfo = {}, {}, {}, {}
        if bmcreg:
            reginfo = self._get_attrib_registry(fishclient, bmcreg)
            if reginfo:
                extrainfo, valtodisplay, _, _ = reginfo
        for setting in bmcstgs.get('Attributes', {}):
            val = bmcstgs['Attributes'][setting]
            currval = val
            val = valtodisplay.get(setting, {}).get(val, val)
            val = {'value': val}
            val.update(**extrainfo.get(setting, {}))
            currsettings[setting] = val
        return currsettings, reginfo

    def upload_media(self, filename, progress=None, data=None):
        wc = self.webclient
        uploadthread = webclient.FileUploader(
            wc, '/rdoc_upload', filename, data,
            formname='file',
            formwrap=True)
        uploadthread.start()
        while uploadthread.isAlive():
            uploadthread.join(3)
            if progress:
                progress({'phase': 'upload',
                          'progress': 100 * wc.get_upload_progress()})
        rsp = json.loads(uploadthread.rsp)
        if rsp['return'] != 0:
            raise Exception('Issue uploading file')
        remfilename = rsp['upload_filename']
        if progress:
            progress({'phase': 'upload',
                      'progress': 100.0})
        self._do_web_request(
            '/redfish/v1/Systems/1/VirtualMedia/RDOC1',
            {'Image':'file:///gpx/rdocupload/' + remfilename,
             'WriteProtected': False}, method='PATCH')
        if progress:
            progress({'phase': 'complete'})

    def get_firmware_inventory(self, components, fishclient):
        fwlist = fishclient._do_web_request(fishclient._fwinventory + '?$expand=.')
        fwlist = copy.deepcopy(fwlist.get('Members', []))
        self._fwnamemap = {}
        for redres in fwlist:
            fwurl = redres['@odata.id']
            res = (redres, fwurl)
            if fwurl.startswith('/redfish/v1/UpdateService/FirmwareInventory/Bundle.'):
                continue  # skip Bundle information for now
            if redres.get('Name', '').startswith('Firmware:'):
                redres['Name'] = redres['Name'].replace('Firmware:', '')
            if redres['Name'].startswith('Firmware-PSoC') and 'Drive_Backplane' in redres["@odata.id"]:
                redres['Name'] = 'Drive Backplane'
            if redres['Name'].startswith('DEVICE-'):
                redres['Name'] = redres['Name'].replace('DEVICE-', '')
            if redres['Name'].startswith('POWER-PSU'):
                redres['Name'] = redres['Name'].replace('POWER-', '')
            swid = redres.get('SoftwareId', '')
            buildid = ''
            version = redres.get('Version', None)
            if swid.startswith('FPGA-') or swid.startswith('UEFI-') or swid.startswith('BMC-'):
                buildid = swid.split('-')[1] + version.split('-')[0]
                version = '-'.join(version.split('-')[1:])
            if version:
                redres['Version'] = version
            cres = fishclient._extract_fwinfo(res)
            if cres[0] is None:
                continue
            if buildid:
                cres[1]['build'] = buildid
            yield cres
        raise pygexc.BypassGenericBehavior()


