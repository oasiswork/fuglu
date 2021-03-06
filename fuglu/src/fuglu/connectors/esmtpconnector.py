#   Copyright 2009-2018 Oli Schacher
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
#
#
import smtplib
import logging
import string
import tempfile
import os
from fuglu.protocolbase import ProtocolHandler, BasicTCPServer
from fuglu.shared import Suspect, apply_template

from email.header import Header
import re


def buildmsgsource(suspect):
    """Build the message source with fuglu headers prepended"""
    # we must prepend headers manually as we can't set a header order in email
    # objects
    origmsgtxt = suspect.get_source()
    newheaders = ""

    for key in suspect.addheaders:
        # is ignore the right thing to do here?
        val = suspect.addheaders[key]
        val.encode('UTF-8', 'ignore')
        #self.logger.debug('Adding header %s : %s'%(key,val))
        hdr = Header(val, header_name=key, continuation_ws=' ')
        newheaders += "%s: %s\n" % (key, hdr.encode())

    modifiedtext = newheaders + origmsgtxt
    return modifiedtext


class ESMTPHandler(ProtocolHandler):
    protoname = 'ESMTP (before queue)'

    def __init__(self, socket, config):
        ProtocolHandler.__init__(self, socket, config)
        self.sess = ESMTPPassthroughSession(socket, config)

    def re_inject(self, suspect):
        """Send message back to postfix"""
        if suspect.get_tag('noreinject'):
            # in esmtp sessions we don't want to provide info to the connecting
            # client
            return 250, 'OK'
        if suspect.get_tag('reinjectoriginal'):
            self.logger.info(
                'Injecting original message source without modifications')
            msgcontent = suspect.get_original_source()
        else:
            msgcontent = buildmsgsource(suspect)

        (code, answer) = self.sess.forwardconn.data(msgcontent)
        return code, answer

    def get_suspect(self):
        success = self.sess.getincomingmail()
        if not success:
            self.logger.error('incoming esmtp transfer did not finish')
            return None

        sess = self.sess
        fromaddr = sess.from_address
        tempfilename = sess.tempfilename
        
        try:
            suspect = Suspect(fromaddr, sess.recipients, tempfilename)
        except ValueError as e:
            if len(sess.recipients)>0:
                toaddr = sess.recipients[0]
            else:
                toaddr = ''
            self.logger.error('failed to initialise suspect with from=<%s> to=<%s> : %s' % (fromaddr, toaddr, str(e)))
            raise

        if sess.xforward_helo is not None and sess.xforward_addr is not None and sess.xforward_rdns is not None:
            suspect.clientinfo = sess.xforward_helo, sess.xforward_addr, sess.xforward_rdns

        return suspect

    def commitback(self, suspect):
        injectcode, injectanswer = self.re_inject(suspect)
        suspect.set_tag("injectanswer", injectanswer)

        values = dict(injectanswer=injectanswer)
        message = apply_template(
            self.config.get('esmtpconnector', 'queuetemplate'), suspect, values)

        if injectcode >= 200 and injectcode < 300:
            self.sess.endsession(250, message)
        else:
            self.sess.endsession(injectcode, injectanswer)
        self.sess = None

    def defer(self, reason):
        self.sess.endsession(451, reason)
        self.sess.finish_outgoing_connection()

    def discard(self, reason):
        self.sess.endsession(250, reason)
        self.sess.finish_outgoing_connection()

    def reject(self, reason):
        self.sess.endsession(550, reason)
        self.sess.finish_outgoing_connection()


class ESMTPServer(BasicTCPServer):

    def __init__(self, controller, port=10125, address="127.0.0.1"):
        BasicTCPServer.__init__(self, controller, port, address, ESMTPHandler)

class ESMTPPassthroughSession(object):
    ST_INIT = 0
    ST_HELO = 1
    ST_MAIL = 2
    ST_RCPT = 3
    ST_DATA = 4
    ST_QUIT = 5

    def __init__(self, socket, config):
        self.config = config
        self.from_address = None
        self.recipients = []
        self.helo = None
        self.dataAccum = None

        self.socket = socket
        self.state = ESMTPPassthroughSession.ST_INIT
        self.logger = logging.getLogger("fuglu.smtpsession")
        self.tempfilename = None
        self.tempfile = None
        self.forwardconn = None

        self.xforward_helo = None
        self.xforward_addr = None
        self.xforward_rdns = None

    def endsession(self, code, message):
        """End session with incoming postfix"""
        self.socket.send("%s %s\r\n" % (code, message))
        data = ''
        completeLine = 0
        while not completeLine:
            lump = self.socket.recv(1024)
            if len(lump):
                data += lump
                if (len(data) >= 2) and data[-2:] == '\r\n':
                    completeLine = 1
                    cmd = data[0:4]
                    cmd = string.upper(cmd)
                    keep = 1
                    rv = None
                    if cmd == "QUIT":
                        self.socket.send("%s %s\r\n" % (220, "BYE"))
                        self.closeconn()
                        return
                    self.socket.send(
                        "%s %s\r\n" % (421, "Cannot accept further commands"))
                    self.closeconn()
                    return
            else:
                self.closeconn()
                return
        return

    def closeconn(self):
        """clocke socket to incoming postfix"""
        self.socket.close()
        
    def _close_tempfile(self):
        if self.tempfile and not self.tempfile.closed:
            self.tempfile.close()

    def getincomingmail(self):
        """return true if mail got in, false on error Session will be kept open"""
        self.socket.send("220 fuglu scanner ready \r\n")
        while True:
            data = ''
            completeLine = 0
            while not completeLine:
                lump = self.socket.recv(1024)
                if len(lump):
                    data += lump
                    if (len(data) >= 2) and data[-2:] == '\r\n':
                        completeLine = 1
                        if self.state != ESMTPPassthroughSession.ST_DATA:
                            rsp, keep = self.doCommand(data)
                        else:
                            try:
                                rsp = self.doData(data)
                            except IOError:
                                self.endsession(
                                    421, "Could not write to temp file")
                                self._close_tempfile()
                                return False

                            if rsp is None:
                                continue
                            else:
                                # data finished.. keep connection open though
                                self.logger.debug('incoming message finished')
                                return True

                        self.socket.send(rsp + "\r\n")
                        if keep == 0:
                            self.socket.close()
                            return False
                else:
                    # EOF
                    return False
        return False

    def forwardCommand(self, command):
        """forward a esmtp command to outgoing postfix instance"""
        command = command.strip()
        if self.forwardconn is None:
            targethost = self.config.get('main', 'outgoinghost')
            if targethost == '${injecthost}':
                targethost = self.socket.getpeername()[0]
            self.forwardconn = smtplib.SMTP(
                targethost, self.config.getint('main', 'outgoingport'))
        self.logger.debug("""SEND: "%s" """ % command)
        code, ans = self.forwardconn.docmd(command)
        ret = "%s %s" % (code, ans)
        if ret.find('\n'):
            temprv = []
            parts = ret.split('\n')
            code = ret[:3]
            parts[0] = parts[0][3:]
            line = ''
            for line in parts:
                line = line.strip()
                temprv.append('%s-%s' % (code, line))
            # replace - with space on last line
            temprv[-1] = '%s %s' % (code, line)

            ret = '\r\n'.join(temprv)
        self.logger.debug("""RECEIVE: "%s" """ % ret)
        return ret.strip()

    def finish_outgoing_connection(self):
        """Try to gracefully end the connection to the outgoing postfix"""
        try:
            self.forwardconn.quit()
        except:
            self.logger.info("Quit failed")
        self.forwardconn = None

    def doCommand(self, data):
        """Process a single SMTP Command"""
        cmd = data[0:4]
        cmd = string.upper(cmd)
        keep = 1
        rv = None

        if cmd in["EHLO", 'HELO']:
            self.state = ESMTPPassthroughSession.ST_HELO
        elif cmd == "RSET":
            self.from_address = None
            self.recipients = []
            self.helo = None
            self.dataAccum = ""
            self.state = ESMTPPassthroughSession.ST_INIT
        elif cmd == "NOOP":
            pass
        elif cmd == "QUIT":
            keep = 0
            return "221 bye", keep

        elif cmd == "MAIL":
            if self.state != ESMTPPassthroughSession.ST_HELO:
                return "503 Bad command sequence", 1
            try:
                self.from_address = self.stripAddress(data)
            except Exception:
                return "501 invalid address syntax", 1
            self.state = ESMTPPassthroughSession.ST_MAIL

        elif cmd == "RCPT":
            if (self.state != ESMTPPassthroughSession.ST_MAIL) and (self.state != ESMTPPassthroughSession.ST_RCPT):
                return "503 Bad command sequence", 1
            try:
                rec = self.stripAddress(data)
                self.recipients.append(rec)
            except Exception:
                return "501 invalid address syntax", 1

            # feature for spam trap setups: only deliver the message for the first recipient to the MTA
            # so we only get one copy of the mail in the catch all box
            if len(self.recipients) > 1 and self.config.getboolean('esmtpconnector', 'ignore_multiple_recipients'):
                return "250 OK #%s" % (len(self.recipients)), 1

            self.state = ESMTPPassthroughSession.ST_RCPT

        elif cmd == "DATA":
            if self.state != ESMTPPassthroughSession.ST_RCPT:
                return "503 Bad command sequence", 1
            self.state = ESMTPPassthroughSession.ST_DATA
            self.dataAccum = ""
            try:
                (handle, tempfilename) = tempfile.mkstemp(
                    prefix='fuglu', dir=self.config.get('main', 'tempdir'))
                self.tempfilename = tempfilename
                self.tempfile = os.fdopen(handle, 'w+b')
            except Exception as e:
                self.endsession(421, "could not create file: %s" % str(e))

            return "354 OK, Enter data, terminated with a \\r\\n.\\r\\n", 1

        if data[0:8].upper() == 'XFORWARD':
            self.store_xforward(data)

        rv = self.forwardCommand(data)

        return rv, keep

    def store_xforward(self, data):
        parts = data.split()[1:]
        for part in parts:
            try:
                key, value = part.split('=', 1)
                key = key.upper()
                if key == 'NAME':  # rdns
                    if value.upper() == '[UNAVAILABLE]':
                        self.xforward_rdns = 'unknown'
                    else:
                        self.xforward_rdns = value

                if key == 'ADDR':  # IP
                    if value.upper() == '[UNAVAILABLE]':
                        continue
                    if value.upper().startswith('IPV6:'):
                        self.xforward_addr = value[5:]
                    else:
                        self.xforward_addr = value

                if key == 'HELO':  # SMTP HELO
                    if value.upper() == '[UNAVAILABLE]':
                        continue
                    self.xforward_helo = value
            except:
                continue

    def doData(self, data):
        data = self.unquoteData(data)
        # store the last few bytes in memory to keep track when the msg is
        # finished
        self.dataAccum = self.dataAccum + data

        if len(self.dataAccum) > 4:
            self.dataAccum = self.dataAccum[-5:]

        if len(self.dataAccum) > 4 and self.dataAccum[-5:] == '\r\n.\r\n':
            # check if there is more data to write to the file
            if len(data) > 4:
                self.tempfile.write(data[0:-5])

            self._close_tempfile()

            self.state = ESMTPPassthroughSession.ST_HELO
            return "250 OK - Data and terminator. found"
        else:
            self.tempfile.write(data)
            return None

    def unquoteData(self, data):
        """two leading dots at the beginning of a line must be unquoted to a single dot"""
        return re.sub(r'(?m)^\.\.', '.', data)

    def stripAddress(self, address):
        """
        Strip the leading & trailing <> from an address.  Handy for
        getting FROM: addresses.
        """
        start = address.find('<') + 1
        if start < 1:
            start = address.find(':') + 1
        if start < 1:
            raise ValueError("Could not parse address %s" % address)
        end = string.find(address, '>')
        if end < 0:
            end = len(address)
        retaddr = address[start:end]
        retaddr = retaddr.strip()
        if retaddr != '' and re.match("^[^@]+@[^@]+\.[^@]+$", retaddr) is None:
            raise ValueError("Could not parse address %s" % address)
        return retaddr
