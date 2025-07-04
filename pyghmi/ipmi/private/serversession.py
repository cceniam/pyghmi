# Copyright 2015 Lenovo
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

"""This represents the server side of a session object
Split into a separate file to avoid overly manipulating the as-yet
client-centered session object
"""

import collections
import hashlib
import hmac
import os
import socket
import struct
import uuid

import pyghmi.ipmi.private.constants as constants
import pyghmi.ipmi.private.session as ipmisession


class ServerSession(ipmisession.Session):
    def __new__(cls, authdata, kg, clientaddr, netsocket, request, uuid,
                bmc):
        # Need to do default new type behavior.  The normal session
        # takes measures to assure the caller shares even when they
        # didn't try.  We don't have that operational mode to contend
        # with in the server case (one file descriptor per bmc)
        return object.__new__(cls)

    def create_open_session_response(self, request):
        clienttag = request[0]
        # role = request[1]
        self.clientsessionid = request[4:8]
        # TODO(jbjohnso): intelligently handle integrity/auth/conf
        # for now, forcibly do cipher suite 3
        self.managedsessionid = os.urandom(4)
        # table 13-17, 1 for now (hmac-sha1), 3 should also be supported
        # table 13-18, integrity, 1 for now is hmac-sha1-96, 4 is sha256
        # confidentiality: 1 is aes-cbc-128, the only one
        self.privlevel = 4
        response = (bytearray([clienttag, 0, self.privlevel, 0])
                    + self.clientsessionid + self.managedsessionid
                    + bytearray([
                        0, 0, 0, 8, 1, 0, 0, 0,  # auth
                        1, 0, 0, 8, 1, 0, 0, 0,  # integrity
                        2, 0, 0, 8, 1, 0, 0, 0,  # privacy
                    ]))
        return response

    def __init__(self, authdata, kg, clientaddr, netsocket, request, uuid,
                 bmc):
        # begin conversation per RMCP+ open session request
        self.uuid = uuid
        self.currhashlib = hashlib.sha1
        self.currhashlen = 12
        self.rqaddr = constants.IPMI_BMC_ADDRESS
        self.authdata = authdata
        self.servermode = True
        self.ipmiversion = 2.0
        self.sequencenumber = 0
        self.sessionid = 0
        self.bmc = bmc
        self.lastpayload = None
        self.rqlun = None  # This will be provided by the client
        self.broken = False
        self.authtype = 6
        self.integrityalgo = 0
        self.confalgo = 0
        self.kg = kg
        self.socket = netsocket
        self.sockaddr = clientaddr
        self.pendingpayloads = collections.deque([])
        self.pktqueue = collections.deque([])
        if clientaddr not in ipmisession.Session.bmc_handlers:
            ipmisession.Session.bmc_handlers[clientaddr] = {bmc.port: self}
        else:
            ipmisession.Session.bmc_handlers[clientaddr][bmc.port] = self
        response = self.create_open_session_response(bytearray(request))
        self.send_payload(response,
                          constants.payload_types['rmcpplusopenresponse'],
                          retry=False)

    def _got_rmcp_openrequest(self, data):
        response = self.create_open_session_response(
            struct.pack('B' * len(data), *data))
        self.send_payload(response,
                          constants.payload_types['rmcpplusopenresponse'],
                          retry=False)

    def _got_rakp1(self, data):
        clienttag = data[0]
        self.Rm = data[8:24]
        self.rolem = data[24]
        self.maxpriv = self.rolem & 0b111
        namepresent = data[27]
        if namepresent == 0:
            # ignore null username for now
            return
        self.username = bytes(data[28:])
        password = self.authdata.get(self.username.decode('utf-8'))
        if password is None:
            # don't think about invalid usernames for now
            return
        uuidbytes = self.uuid.bytes
        self.uuiddata = uuidbytes
        self.Rc = os.urandom(16)
        hmacdata = (self.clientsessionid + self.managedsessionid
                    + self.Rm + self.Rc + uuidbytes
                    + bytearray([self.rolem, len(self.username)]))
        hmacdata += self.username
        self.kuid = password.encode('utf-8')
        if self.kg is None:
            self.kg = self.kuid
        authcode = hmac.new(
            self.kuid, bytes(hmacdata), hashlib.sha1).digest()
        # regretably, ipmi mandates the server send out an hmac first
        # akin to a leak of /etc/shadow, not too worrisome if the secret
        # is complex, but terrible for most likely passwords selected by
        # a human
        newmessage = (bytearray([clienttag, 0, 0, 0]) + self.clientsessionid
                      + self.Rc + uuidbytes + authcode)
        self.send_payload(newmessage, constants.payload_types['rakp2'],
                          retry=False)

    def _got_rakp2(self, data):
        # stub, server should not think about rakp2
        pass

    def _got_rakp3(self, data):
        # for now drop rakp3 with bad authcode
        # respond correctly a TODO(jjohnson2), since Kg being used
        # yet incorrect is a scenario why rakp3 could be bad
        # even if rakp2 was good
        RmRc = self.Rm + self.Rc
        self.sik = hmac.new(self.kg,
                            bytes(RmRc)
                            + struct.pack("2B", self.rolem, len(self.username))
                            + self.username, hashlib.sha1).digest()
        self.k1 = hmac.new(self.sik, b'\x01' * 20, hashlib.sha1).digest()
        self.k2 = hmac.new(self.sik, b'\x02' * 20, hashlib.sha1).digest()
        self.aeskey = self.k2[0:16]
        hmacdata = (self.Rc + self.clientsessionid
                    + struct.pack("2B", self.rolem, len(self.username))
                    + self.username)
        expectedauthcode = hmac.new(self.kuid, bytes(hmacdata), hashlib.sha1
                                    ).digest()
        authcode = struct.pack("%dB" % len(data[8:]), *data[8:])
        if expectedauthcode != authcode:
            # TODO(jjohnson2): RMCP error back at invalid rakp3
            return
        clienttag = data[0]
        if data[1] != 0:
            # client did not like our response, so ignore the rakp3
            return
        self.localsid = struct.unpack('<I', self.managedsessionid)[0]
        self.ipmicallback = self.handle_client_request
        self._send_rakp4(clienttag, 0)

    def handle_client_request(self, request):
        if request['netfn'] == 6 and request['command'] == 0x3b:
            pendingpriv = request['data'][0]
            returncode = 0
            if pendingpriv > 1:
                if pendingpriv > self.maxpriv:
                    returncode = 0x81
                else:
                    self.clientpriv = request['data'][0]
            self._send_ipmi_net_payload(code=returncode,
                                        data=[self.clientpriv])
        elif request['netfn'] == 6 and request['command'] == 0x3c:
            self.send_ipmi_response()
            self.close_server_session()
        else:
            self.bmc.handle_raw_request(request, self)

    def close_server_session(self):
        pass

    def _send_rakp4(self, tagvalue, statuscode):
        payload = bytearray(
            [tagvalue, statuscode, 0, 0]) + self.clientsessionid
        hmacdata = self.Rm + self.managedsessionid + self.uuiddata
        hmacdata = struct.pack('%dB' % len(hmacdata), *hmacdata)
        authdata = hmac.new(self.sik, hmacdata, hashlib.sha1).digest()[:12]
        payload += authdata
        self.send_payload(payload, constants.payload_types['rakp4'],
                          retry=False)
        self.confalgo = 'aes'
        self.integrityalgo = 'sha1'
        self.sequencenumber = 1
        self.sessionid = struct.unpack(
            '<I', struct.pack('4B', *self.clientsessionid))[0]

    def _got_rakp4(self, data):
        # stub, server should not think about rakp4
        pass

    def _timedout(self):
        """Expire a client session after a period of inactivity

        After the session inactivity timeout, this invalidate the client
        session.
        """
        # for now, we will have a non-configurable 60 second timeout
        pass

    def _handle_channel_auth_cap(self, request):
        """Handle incoming channel authentication capabilities request

        This is used when serving as an IPMI target to service client
        requests for client authentication capabilities
        """
        pass

    def send_ipmi_response(self, data=[], code=0):
        self._send_ipmi_net_payload(data=data, code=code)

    def logout(self):
        pass


class IpmiServer(object):
    # auth capabilities for now is a static payload
    # for now always completion code 0, otherwise ignore
    # authentication type fixed to ipmi2, ipmi1 forbidden
    # 0b10000000

    def __init__(self, authdata, port=623, bmcuuid=None, address='::'):
        """Create a new ipmi bmc instance.

        :param authdata: A dict or object with .get() to provide password
                        lookup by username.  This does not support the full
                        complexity of what IPMI can support, only a
                        reasonable subset.
        :param port: The default port number to bind to.  Defaults to the
                     standard 623
        :param address: The IP address to bind to. Defaults to '::' (all
                        zeroes)
        """
        self.revision = 0
        self.deviceid = 0
        self.firmwaremajor = 1
        self.firmwareminor = 0
        self.ipmiversion = 2
        self.additionaldevices = 0
        self.mfgid = 0
        self.prodid = 0
        self.pktqueue = collections.deque([])
        if bmcuuid is None:
            self.uuid = uuid.uuid4()
        else:
            self.uuid = bmcuuid
        lanchannel = 1
        authtype = 0b10000000  # ipmi2 only
        authstatus = 0b00000100  # change based on authdata/kg
        chancap = 0b00000010  # ipmi2 only
        oemdata = (0, 0, 0, 0)
        self.authdata = authdata
        self.authcap = struct.pack('BBBBBBBBB', 0, lanchannel, authtype,
                                   authstatus, chancap, *oemdata)
        self.kg = None
        self.timeout = 60
        self.port = port
        addrinfo = socket.getaddrinfo(address, port, 0,
                                      socket.SOCK_DGRAM)[0]
        self.serversocket = ipmisession.Session._assignsocket(addrinfo)
        ipmisession.Session.bmc_handlers[self.serversocket] = {0: self}

    def send_auth_cap(self, myaddr, mylun, clientaddr, clientlun, clientseq,
                      sockaddr):
        header = bytearray(
            b'\x06\x00\xff\x07\x00\x00\x00\x00\x00\x00\x00\x00\x00\x10')
        headerdata = [clientaddr, clientlun | (7 << 2)]
        headersum = ipmisession._checksum(*headerdata)
        header += bytearray(headerdata + [headersum, myaddr,
                                          mylun | (clientseq << 2), 0x38])
        header += self.authcap
        bodydata = struct.unpack('B' * len(header[17:]), bytes(header[17:]))
        header.append(ipmisession._checksum(*bodydata))
        ipmisession._io_sendto(self.serversocket, header, sockaddr)

    def process_pktqueue(self):
        while self.pktqueue:
            pkt = self.pktqueue.popleft()
            self.sessionless_data(pkt[0], pkt[1])

    def send_cipher_suites(self, myaddr, mylun, clientaddr, clientlun,
                           clientseq, data, sockaddr):
        # the last two bytes is length of message, fixed at 14 for now
        # the rest is boilerplate ipmi, follow along in ipmi spec
        # 'example ipmi over lan' if desired
        header = bytearray(
            b'\x06\x00\xff\x07\x06\x00\x00\x00\x00'
            b'\x00\x00\x00\x00\x00\x0e\x00')
        # now the generic inner ipmi packet, per figure-13-4,
        # ipmi lan message formats
        ipmihdr = bytearray([clientaddr, clientlun | (7 << 2)])
        hdrsum = ipmisession._checksum(*ipmihdr)
        ipmihdr.append(hdrsum)
        rq = bytearray([myaddr, mylun | clientseq << 2, 0x54])
        # for now, hard code a cipher suite 3 only response
        rq.extend(bytearray(b'\x00\x01\xc0\x03\x01\x41\x81'))
        hdrsum = ipmisession._checksum(*rq)
        rq.append(hdrsum)
        pkt = header + ipmihdr + rq
        ipmisession._io_sendto(self.serversocket, pkt, sockaddr)

    def sessionless_data(self, data, sockaddr):
        """Examines unsolocited packet and decides appropriate action.

        For a listening IpmiServer, a packet without an active session
        comes here for examination.  If it is something that is utterly
        sessionless (e.g. get channel authentication), send the appropriate
        response.  If it is a get session challenge or open rmcp+ request,
        spawn a session to handle the context.
        """
        if len(data) < 22:
            return
        data = bytearray(data)
        if not (data[0] == 6 and data[2:4] == b'\xff\x07'):  # not ipmi
            return
        if data[4] == 6:  # ipmi 2 payload...
            payloadtype = data[5]
            if payloadtype not in (0, 16):
                return
            if payloadtype == 16:  # new session to handle conversation
                ServerSession(self.authdata, self.kg, sockaddr,
                              self.serversocket, data[16:], self.uuid,
                              bmc=self)
                return
            # ditch two byte, because ipmi2 header is two
            # bytes longer than ipmi1 (payload type added, payload length 2).
            data = data[2:]
        myaddr, netfnlun = struct.unpack('2B', bytes(data[14:16]))
        netfn = (netfnlun & 0b11111100) >> 2
        mylun = netfnlun & 0b11
        if netfn == 6:  # application request
            if data[19] == 0x38:  # cmd = get channel auth capabilities
                verchannel, level = struct.unpack('2B', bytes(data[20:22]))
                version = verchannel & 0b10000000
                if version != 0b10000000:
                    return
                channel = verchannel & 0b1111
                if channel != 0xe:
                    return
                (clientaddr, clientlun) = struct.unpack(
                    'BB', bytes(data[17:19]))
                clientseq = clientlun >> 2
                clientlun &= 0b11  # Lun is only the least significant bits
                level &= 0b1111
                self.send_auth_cap(myaddr, mylun, clientaddr, clientlun,
                                   clientseq, sockaddr)
            elif data[19] == 0x54:
                clientaddr, clientlun = data[17:19]
                clientseq = clientlun >> 2
                clientlun &= 0b11
                self.send_cipher_suites(myaddr, mylun, clientaddr, clientlun,
                                        clientseq, data, sockaddr)

    def set_kg(self, kg):
        """Sets the Kg for the BMC to use

        In RAKP, Kg is a BMC-specific integrity key that can be set.  If not
        set, Kuid is used for the integrity key
        """
        try:
            self.kg = kg.encode('utf-8')
        except AttributeError:
            self.kg = kg

    def send_device_id(self, session):
        response = [self.deviceid, self.revision, self.firmwaremajor,
                    self.firmwareminor, self.ipmiversion,
                    self.additionaldevices]
        response += struct.unpack('4B', struct.pack('<I', self.mfgid))
        response += struct.unpack('4B', struct.pack('<I', self.prodid))
        session.send_ipmi_response(data=response)

    def handle_raw_request(self, request, session):
        # per table 5-2, completion code 0xc1 is 'unrecognized'
        session.send_ipmi_response(code=0xc1)

    def logout(self):
        pass
