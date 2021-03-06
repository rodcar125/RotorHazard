# ClusterNodeSet:  Manages a set of slave nodes

import logging
import gevent
import json
import socketio
from monotonic import monotonic
import RHUtils
from RHRace import RaceStatus
from Language import __
import Database
from util.RunningMedian import RunningMedian
from util.Averager import Averager

logger = logging.getLogger(__name__)


class SlaveNode:

    SPLIT_MODE = 'split'
    MIRROR_MODE = 'mirror'
    
    LATENCY_AVG_SIZE = 30
    TIMEDIFF_MEDIAN_SIZE = 30
    TIMEDIFF_CORRECTION_THRESH_MS = 250  # correct split times if slave clock more off than this

    def __init__(self, idVal, info, RACE, DB, getCurrentProfile, \
                 emit_split_pass_info, monotonic_to_epoch_millis, \
                 emit_cluster_connect_change, server_release_version):
        self.id = idVal
        self.info = info
        self.RACE = RACE
        self.DB = DB
        self.getCurrentProfile = getCurrentProfile
        self.emit_split_pass_info = emit_split_pass_info
        self.monotonic_to_epoch_millis = monotonic_to_epoch_millis
        self.emit_cluster_connect_change = emit_cluster_connect_change
        self.server_release_version = server_release_version
        addr = info['address']
        if not '://' in addr:
            addr = 'http://' + addr
        self.address = addr
        self.isMirrorMode = (str(info.get('mode', SlaveNode.SPLIT_MODE)) == SlaveNode.MIRROR_MODE)
        self.slaveModeStr = SlaveNode.MIRROR_MODE if self.isMirrorMode else SlaveNode.SPLIT_MODE
        self.recEventsFlag = info.get('recEventsFlag', self.isMirrorMode)
        self.queryInterval = info['queryInterval'] if 'queryInterval' in info else 0
        if self.queryInterval <= 0:
            self.queryInterval = 10
        self.firstQueryInterval = 3 if self.queryInterval >= 3 else 1
        self.startConnectTime = 0
        self.lastContactTime = -1
        self.firstContactTime = 0
        self.lastCheckQueryTime = 0
        self.secsSinceDisconnect = 0
        self.freqsSentFlag = False
        self.numDisconnects = 0
        self.numDisconnsDuringRace = 0
        self.numContacts = 0
        self.latencyAveragerObj = Averager(self.LATENCY_AVG_SIZE)
        self.totalUpTimeSecs = 0
        self.totalDownTimeSecs = 0
        self.timeDiffMedianObj = RunningMedian(self.TIMEDIFF_MEDIAN_SIZE)
        self.timeDiffMedianMs = 0
        self.timeCorrectionMs = 0
        self.progStartEpoch = 0
        self.sio = socketio.Client(reconnection=False, request_timeout=1)
        self.sio.on('connect', self.on_connect)
        self.sio.on('disconnect', self.on_disconnect)
        self.sio.on('pass_record', self.on_pass_record)
        self.sio.on('check_slave_response', self.on_check_slave_response)
        self.sio.on('join_cluster_response', self.join_cluster_response)
        gevent.spawn(self.slave_worker_thread)

    def slave_worker_thread(self):
        self.startConnectTime = monotonic()
        gevent.sleep(0.1)
        while True:
            try:
                gevent.sleep(1)
                if self.lastContactTime <= 0:
                    oldSecsSinceDis = self.secsSinceDisconnect
                    self.secsSinceDisconnect = monotonic() - self.startConnectTime
                    if self.secsSinceDisconnect >= 1.0:  # if disconnect just happened then wait a second before reconnect
                        # if never connected then only retry if race not in progress
                        if self.numDisconnects > 0 or (self.RACE.race_status != RaceStatus.STAGING and \
                                                        self.RACE.race_status != RaceStatus.RACING):
                            # if first-ever attempt or was previously connected then show log msg
                            if oldSecsSinceDis == 0 or self.numDisconnects > 0:
                                logger.log((logging.INFO if self.secsSinceDisconnect <= self.info['timeout'] else logging.DEBUG), \
                                           "Attempting to connect to slave {0} at {1}...".format(self.id+1, self.address))
                            try:
                                self.sio.connect(self.address)
                            except socketio.exceptions.ConnectionError as ex:
                                if self.lastContactTime > 0:  # if current status is connected
                                    logger.info("Error connecting to slave {0} at {1}: {2}".format(self.id+1, self.address, ex))
                                    if not self.sio.connected:  # if not connected then
                                        self.on_disconnect();   # invoke disconnect function to update status
                                else:
                                    err_msg = "Unable to connect to slave {0} at {1}: {2}".format(self.id+1, self.address, ex)
                                    if monotonic() <= self.startConnectTime + self.info['timeout']:
                                        if self.numDisconnects > 0:  # if previously connected then always log failure
                                            logger.info(err_msg)
                                        elif oldSecsSinceDis == 0:   # if not previously connected then only log once
                                            err_msg += " (will continue attempts until timeout)"
                                            logger.info(err_msg)
                                    else:  # if beyond timeout period
                                        if self.numDisconnects > 0:  # if was previously connected then keep trying
                                            logger.debug(err_msg)    #  log at debug level and
                                            gevent.sleep(29)         #  increase delay between attempts
                                        else:
                                            logger.warn(err_msg)     # if never connected then give up
                                            logger.warn("Reached timeout; no longer trying to connect to slave {0} at {1}".\
                                                        format(self.id+1, self.address))
                                            if self.emit_cluster_connect_change:
                                                self.emit_cluster_connect_change(False)  # play one disconnect tone
                                            return  # exit worker thread
                else:
                    now_time = monotonic()
                    if not self.freqsSentFlag:
                        try:
                            self.freqsSentFlag = True
                            if (not self.isMirrorMode) and self.getCurrentProfile:
                                logger.info("Sending node frequencies to slave {0} at {1}".format(self.id+1, self.address))
                                for idx, freq in enumerate(json.loads(self.getCurrentProfile().frequencies)["f"]):
                                    data = { 'node':idx, 'frequency':freq }
                                    self.emit('set_frequency', data)
                                    gevent.sleep(0.001)
                        except (KeyboardInterrupt, SystemExit):
                            raise
                        except Exception as ex:
                            logger.error("Error sending node frequencies to slave {0} at {1}: {2}".format(self.id+1, self.address, ex))
                    else:
                        try:
                            if self.sio.connected:
                                # send heartbeat-query every 'queryInterval' seconds, or that long since last contact
                                if (now_time > self.lastContactTime + self.queryInterval and \
                                            now_time > self.lastCheckQueryTime + self.queryInterval) or \
                                            (self.lastCheckQueryTime == 0 and \
                                             now_time > self.lastContactTime + self.firstQueryInterval):  # if first query do it sooner
                                    self.lastCheckQueryTime = now_time
                                    # timestamp not actually used by slave, but send to make query and response symmetrical
                                    payload = {
                                        'timestamp': self.monotonic_to_epoch_millis(now_time) \
                                                         if self.monotonic_to_epoch_millis else 0
                                    }
                                    # don't update 'lastContactTime' value until response received
                                    self.sio.emit('check_slave_query', payload)
                                # if there was no response to last query then disconnect (and reconnect next loop)
                                elif self.lastCheckQueryTime > self.lastContactTime:
                                    if self.lastCheckQueryTime - self.lastContactTime > 3.9:
                                        logger.warn("Disconnecting after no response for 'check_slave_query'" \
                                                    " received for slave {0} at {1}".format(self.id+1, self.address))
                                        # calling 'disconnect()' will usually invoke 'on_disconnect()', but
                                        #  'disconnect()' can be slow to return, so we update status now
                                        self.on_disconnect()
                                        self.sio.disconnect()
                                    else:
                                        logger.debug("No response for 'check_slave_query' received "\
                                                     "after {0:.1f} secs for slave {1} at {2}".\
                                                     format(self.lastCheckQueryTime - self.lastContactTime, \
                                                            self.id+1, self.address))
                            else:
                                logger.info("Invoking 'on_disconnect()' fn for slave {0} at {1}".\
                                            format(self.id+1, self.address))
                                self.on_disconnect()
                        except (KeyboardInterrupt, SystemExit):
                            raise
                        except Exception as ex:
                            logger.error("Error sending check-query to slave {0} at {1}: {2}".format(self.id+1, self.address, ex))
            except KeyboardInterrupt:
                logger.info("SlaveNode worker thread terminated by keyboard interrupt")
                raise
            except SystemExit:
                raise
            except Exception:
                logger.exception("Exception in SlaveNode worker thread for slave {0}".format(self.id+1))
                gevent.sleep(9)

    def emit(self, event, data = None):
        try:
            if self.lastContactTime > 0:
                self.sio.emit(event, data)
                self.lastContactTime = monotonic()
                self.numContacts += 1
            elif self.numDisconnects > 0:  # only warn if previously connected
                logger.warn("Unable to emit to disconnected slave {0} at {1}, event='{2}'".\
                            format(self.id+1, self.address, event))
        except Exception:
            logger.exception("Error emitting to slave {0} at {1}, event='{2}'".\
                            format(self.id+1, self.address, event))
            if self.sio.connected:
                logger.warn("Disconnecting after error emitting to slave {0} at {1}".\
                            format(self.id+1, self.address))
                self.sio.disconnect()

    def on_connect(self):
        try:
            if self.lastContactTime <= 0:
                self.lastContactTime = monotonic()
                self.firstContactTime = self.lastContactTime
                if self.numDisconnects <= 0:
                    logger.info("Connected to slave {0} at {1} (mode: {2})".format(\
                                        self.id+1, self.address, self.slaveModeStr))
                else:
                    downSecs = int(round(self.lastContactTime - self.startConnectTime)) if self.startConnectTime > 0 else 0
                    logger.info("Reconnected to " + self.get_log_str(downSecs, False));
                    self.totalDownTimeSecs += downSecs
                payload = {
                    'mode': self.slaveModeStr
                }
                self.emit('join_cluster_ex', payload)
                if (not self.isMirrorMode) and \
                        (self.RACE.race_status == RaceStatus.STAGING or self.RACE.race_status == RaceStatus.RACING):
                    self.emit('stage_race')  # if race in progress then make sure running on slave
                if self.emit_cluster_connect_change:
                    self.emit_cluster_connect_change(True)
            else:
                self.lastContactTime = monotonic()
                logger.debug("Received extra 'on_connect' event for slave {0} at {1}".format(self.id+1, self.address))
        except Exception:
            logger.exception("Error handling Cluster 'on_connect' for slave {0} at {1}".\
                             format(self.id+1, self.address))

    def on_disconnect(self):
        try:
            if self.lastContactTime > 0:
                self.startConnectTime = monotonic()
                self.lastContactTime = -1
                self.numDisconnects += 1
                self.numDisconnsDuringRace += 1
                upSecs = int(round(self.startConnectTime - self.firstContactTime)) if self.firstContactTime > 0 else 0
                logger.warn("Disconnected from " + self.get_log_str(upSecs));
                self.totalUpTimeSecs += upSecs
                if self.emit_cluster_connect_change:
                    self.emit_cluster_connect_change(False)
            else:
                logger.debug("Received extra 'on_disconnect' event for slave {0} at {1}".format(self.id+1, self.address))
        except Exception:
            logger.exception("Error handling Cluster 'on_disconnect' for slave {0} at {1}".\
                             format(self.id+1, self.address))

    def get_log_str(self, timeSecs=None, upTimeFlag=True, stoppedRaceFlag=False):
        if timeSecs is None:
            timeSecs = int(round(monotonic() - self.firstContactTime)) if self.lastContactTime > 0 else 0
        totUpSecs = self.totalUpTimeSecs
        totDownSecs = self.totalDownTimeSecs
        if upTimeFlag:
            totUpSecs += timeSecs
            upDownStr = "upTime"
        else:
            totDownSecs += timeSecs
            upDownStr = "downTime"
        upDownTotal = totUpSecs + totDownSecs
        return "slave {0} at {1} (latency: min={2} avg={3} max={4} last={5} ms, disconns={6}, contacts={7}, " \
               "timeDiff={8}ms, {9}={10}, totalUp={11}, totalDown={12}, avail={13:.1%}{14})".\
                    format(self.id+1, self.address, self.latencyAveragerObj.minVal, \
                           self.latencyAveragerObj.getIntAvgVal(), self.latencyAveragerObj.maxVal, \
                           self.latencyAveragerObj.lastVal, self.numDisconnects, self.numContacts, \
                           self.timeDiffMedianMs, upDownStr, timeSecs, totUpSecs, totDownSecs, \
                           (float(totUpSecs)/upDownTotal if upDownTotal > 0 else 0),
                           ((", numDisconnsDuringRace=" + str(self.numDisconnsDuringRace)) if \
                                    (self.numDisconnsDuringRace > 0 and \
                                     (stoppedRaceFlag or self.RACE.race_status == RaceStatus.RACING)) else ""))

    def on_pass_record(self, data):
        try:
            self.lastContactTime = monotonic()
            self.numContacts += 1
            node_index = data['node']

            if self.RACE.race_status is RaceStatus.RACING:

                pilot_id = Database.HeatNode.query.filter_by( \
                    heat_id=self.RACE.current_heat, node_index=node_index).one_or_none().pilot_id
        
                if pilot_id != Database.PILOT_ID_NONE:
        
                    # convert split timestamp (epoch ms sine 1970-01-01) to equivalent local 'monotonic' time value
                    split_ts = data['timestamp'] - self.RACE.start_time_epoch_ms
        
                    act_laps_list = self.RACE.get_active_laps()[node_index]
                    lap_count = max(0, len(act_laps_list) - 1)
                    split_id = self.id
        
                    # get timestamp for last lap pass (including lap 0)
                    if len(act_laps_list) > 0:
                        last_lap_ts = act_laps_list[-1]['lap_time_stamp']
                        last_split_id = self.DB.session.query(self.DB.func.max(Database.LapSplit.split_id)).filter_by(node_index=node_index, lap_id=lap_count).scalar()
                        if last_split_id is None: # first split for this lap
                            if split_id > 0:
                                logger.info('Ignoring missing splits before {0} for node {1}'.format(split_id+1, node_index+1))
                            last_split_ts = last_lap_ts
                        else:
                            if split_id > last_split_id:
                                if split_id > last_split_id + 1:
                                    logger.info('Ignoring missing splits between {0} and {1} for node {2}'.format(last_split_id+1, split_id+1, node_index+1))
                                last_split_ts = Database.LapSplit.query.filter_by(node_index=node_index, lap_id=lap_count, split_id=last_split_id).one().split_time_stamp
                            else:
                                logger.info('Ignoring out-of-order split {0} for node {1}'.format(split_id+1, node_index+1))
                                last_split_ts = None
                    else:
                        logger.info('Ignoring split {0} before zero lap for node {1}'.format(split_id+1, node_index+1))
                        last_split_ts = None
        
                    if last_split_ts is not None:
        
                        # if slave-timer clock was detected as not synchronized then apply correction
                        if self.timeCorrectionMs != 0:
                            split_ts -= self.timeCorrectionMs
                            
                        split_time = split_ts - last_split_ts
                        split_speed = float(self.info['distance'])*1000.0/float(split_time) if 'distance' in self.info else None
                        split_time_str = RHUtils.time_format(split_time)
                        logger.debug('Split pass record: Node {0}, lap {1}, split {2}, time={3}, speed={4}' \
                            .format(node_index+1, lap_count+1, split_id+1, split_time_str, \
                            ('{0:.2f}'.format(split_speed) if split_speed is not None else 'None')))
        
                        self.DB.session.add(Database.LapSplit(node_index=node_index, pilot_id=pilot_id, lap_id=lap_count, \
                                split_id=split_id, split_time_stamp=split_ts, split_time=split_time, \
                                split_time_formatted=split_time_str, split_speed=split_speed))
                        self.DB.session.commit()
                        self.emit_split_pass_info(pilot_id, split_id, split_time)

                else:
                    logger.info('Split pass record dismissed: Node: {0}, no pilot on node'.format(node_index+1))

            else:
                logger.info('Ignoring split {0} for node {1} because race not running'.format(self.id+1, node_index+1))

        except Exception:
            logger.exception("Error processing pass record from slave {0} at {1}".format(self.id+1, self.address))

        try:
            # send message-ack back to slave (but don't update 'lastContactTime' value)
            payload = {
                'messageType': 'pass_record',
                'messagePayload': data
            }
            self.sio.emit('cluster_message_ack', payload)
        except Exception:
            logger.exception("Error sending pass-record message acknowledgement to slave {0} at {1}".\
                             format(self.id+1, self.address))

    def on_check_slave_response(self, data):
        try:
            if self.lastContactTime > 0:
                nowTime = monotonic()
                self.lastContactTime = nowTime
                self.numContacts += 1
                transitTime = nowTime - self.lastCheckQueryTime if self.lastCheckQueryTime > 0 else 0
                if transitTime > 0:
                    self.latencyAveragerObj.addItem(int(round(transitTime * 1000)))
                    if data:
                        slaveTimestamp = data.get('timestamp', 0)
                        if slaveTimestamp:
                            # calculate local-time value midway between before and after network query
                            localTimestamp = self.monotonic_to_epoch_millis(\
                                             self.lastCheckQueryTime + transitTime/2) \
                                             if self.monotonic_to_epoch_millis else 0
                            # calculate clock-time difference in ms and add to running median
                            self.timeDiffMedianObj.insert(int(round(slaveTimestamp - localTimestamp)))
                            self.timeDiffMedianMs = self.timeDiffMedianObj.median()
                            return
                    logger.debug("Received check_slave_response with no timestamp from slave {0} at {1}".\
                                 format(self.id+1, self.address))
            else:
                logger.debug("Received check_slave_response while disconnected from slave {0} at {1}".\
                             format(self.id+1, self.address))
        except Exception:
            logger.exception("Error processing check-response from slave {0} at {1}".\
                             format(self.id+1, self.address))

    def join_cluster_response(self, data):
        try:
            infoStr = data.get('server_info')
            logger.debug("Server info from slave {0} at {1}:  {2}".\
                         format(self.id+1, self.address, infoStr))
            infoDict = json.loads(infoStr)
            prgStrtEpchStr = infoDict.get('prog_start_epoch')
            newPrgStrtEpch = False
            try:
                prgStrtEpch = int(float(prgStrtEpchStr))
                if self.progStartEpoch == 0:
                    self.progStartEpoch = prgStrtEpch
                    newPrgStrtEpch = True
                    logger.debug("Initial 'prog_start_epoch' value for slave {0}: {1}".\
                                format(self.id+1, prgStrtEpch))
                elif prgStrtEpch != self.progStartEpoch:
                    self.progStartEpoch = prgStrtEpch
                    newPrgStrtEpch = True
                    logger.info("New 'prog_start_epoch' value for slave {0}: {1}; resetting 'timeDiff' median".\
                                format(self.id+1, prgStrtEpch))
                    self.timeDiffMedianObj = RunningMedian(self.TIMEDIFF_MEDIAN_SIZE)
            except ValueError as ex:
                logger.warn("Error parsing 'prog_start_epoch' value from slave {0}: {1}".\
                            format(self.id+1, ex))
            # if first time connecting (or possible slave restart) then check/warn about program version
            if newPrgStrtEpch or self.numDisconnects == 0:
                slaveVerStr = infoDict.get('release_version')
                if slaveVerStr:
                    if slaveVerStr != self.server_release_version:
                        logger.warn("Different program version ('{0}') running on slave {1} at {2}".\
                                    format(slaveVerStr, self.id+1, self.address))
                else:
                    logger.warn("Unable to parse 'release_version' from slave {0} at {1}".\
                                format(self.id+1, self.address))
        except Exception:
            logger.exception("Error processing join-cluster response from slave {0} at {1}".\
                             format(self.id+1, self.address))
        try:
            # send message-ack back to slave (but don't update 'lastContactTime' value)
            #  this tells slave timer to expect future message-acks in response to 'pass_record' emits
            payload = { 'messageType': 'join_cluster_response' }
            self.sio.emit('cluster_message_ack', payload)
        except Exception:
            logger.exception("Error sending join-cluster message acknowledgement to slave {0} at {1}".\
                             format(self.id+1, self.address))

class ClusterNodeSet:
    def __init__(self):
        self.slaves = []
        self.splitSlaves = []
        self.recEventsSlaves = []

    def addSlave(self, slave):
        self.slaves.append(slave)
        if not slave.isMirrorMode:
            self.splitSlaves.append(slave)
        if slave.recEventsFlag:
            self.recEventsSlaves.append(slave)

    def hasSlaves(self):
        return (len(self.slaves) > 0)

    def hasRecEventsSlaves(self):
        return (len(self.recEventsSlaves) > 0)
    
    # return True if slave is 'split' mode and is or has been connected
    def isSplitSlaveAvailable(self, slave_index):
        return (slave_index < len(self.slaves)) and \
               (not self.slaves[slave_index].isMirrorMode) and \
                    (self.slaves[slave_index].lastContactTime > 0 or \
                     self.slaves[slave_index].numDisconnects > 0)

    def emit(self, event, data = None):
        for slave in self.slaves:
            gevent.spawn(slave.emit, event, data)

    def emitToSplits(self, event, data = None):
        for slave in self.splitSlaves:
            gevent.spawn(slave.emit, event, data)

    def emitEventTrigger(self, data = None):
        for slave in self.recEventsSlaves:
            gevent.spawn(slave.emit, 'cluster_event_trigger', data)

    def getClusterStatusInfo(self):
        nowTime = monotonic()
        payload = []
        for slave in self.slaves:
            upTimeSecs = int(round(nowTime - slave.firstContactTime)) if slave.lastContactTime > 0 else 0
            downTimeSecs = int(round(slave.secsSinceDisconnect)) if slave.lastContactTime <= 0 else 0
            totalUpSecs = slave.totalUpTimeSecs + upTimeSecs
            totalDownSecs = slave.totalDownTimeSecs + downTimeSecs
            payload.append(
                {'address': slave.address, \
                 'modeIndicator': ('M' if slave.isMirrorMode else 'S'), \
                 'minLatencyMs':  slave.latencyAveragerObj.minVal, \
                 'avgLatencyMs': slave.latencyAveragerObj.getIntAvgVal(), \
                 'maxLatencyMs': slave.latencyAveragerObj.maxVal, \
                 'lastLatencyMs': slave.latencyAveragerObj.lastVal, \
                 'numDisconnects': slave.numDisconnects, \
                 'numContacts': slave.numContacts, \
                 'timeDiffMs': slave.timeDiffMedianMs, \
                 'upTimeSecs': upTimeSecs, \
                 'downTimeSecs': downTimeSecs, \
                 'availability': round((100.0*totalUpSecs/(totalUpSecs+totalDownSecs) \
                                       if totalUpSecs+totalDownSecs > 0 else 0), 1), \
                 'last_contact': int(nowTime-slave.lastContactTime) if slave.lastContactTime >= 0 else \
                                 (__("connection lost") if slave.numDisconnects > 0 else __("never connected"))
                 })
        return {'slaves': payload}

    def doClusterRaceStart(self):
        for slave in self.slaves:
            slave.numDisconnsDuringRace = 0
            if slave.lastContactTime > 0:
                logger.info("Connected at race start to " + slave.get_log_str());
                if abs(slave.timeDiffMedianMs) > SlaveNode.TIMEDIFF_CORRECTION_THRESH_MS:
                    slave.timeCorrectionMs = slave.timeDiffMedianMs
                    logger.info("Slave {0} clock not synchronized with master, timeDiff={1}ms".\
                                format(slave.id+1, slave.timeDiffMedianMs))
                else:
                    slave.timeCorrectionMs = 0
                    logger.debug("Slave {0} clock synchronized OK with master, timeDiff={1}ms".\
                                 format(slave.id+1, slave.timeDiffMedianMs))
            elif slave.numDisconnects > 0:
                logger.warn("Slave {0} not connected at race start".format(slave.id+1))

    def doClusterRaceStop(self):
        for slave in self.slaves:
            if slave.lastContactTime > 0:
                logger.info("Connected at race stop to " + slave.get_log_str(stoppedRaceFlag=True));
            elif slave.numDisconnects > 0:
                logger.warn("Not connected at race stop to " + slave.get_log_str(stoppedRaceFlag=True));
