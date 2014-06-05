#
#  Copyright 2007-2014 Charles du Jeu - Abstrium SAS <team (at) pyd.io>
#  This file is part of Pydio.
#
#  Pydio is free software: you can redistribute it and/or modify
#  it under the terms of the GNU Affero General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  Pydio is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU Affero General Public License for more details.
#
#  You should have received a copy of the GNU Affero General Public License
#  along with Pydio.  If not, see <http://www.gnu.org/licenses/>.
#
#  The latest code can be found at <http://pyd.io/>.
#

import time
import os
import threading
import pickle
import logging

from requests.exceptions import ConnectionError
from collections import deque
from pydio.job.localdb import LocalDbHandler
from pydio.job.local_watcher import LocalWatcher
from pydio.sdk.exceptions import ProcessException
from pydio.sdk.remote import PydioSdk
from pydio.sdk.local import SystemSdk
from pydio.job.EventLogger import EventLogger

from pydispatch import dispatcher
from pydio import PUBLISH_SIGNAL, TRANSFER_RATE_SIGNAL
# -*- coding: utf-8 -*-


class ContinuousDiffMerger(threading.Thread):
    """Main Thread grabbing changes from both sides, computing the necessary changes to apply, and applying them"""

    def __init__(self, job_config, job_data_path):
        """
        Initialize thread internals
        :param job_config: JobConfig instance
        :param job_data_path: Filesystem path where the job data are stored
        :return:
        """
        threading.Thread.__init__(self)
        self.data_base = job_data_path
        self.job_config = job_config
        self.init_global_progress()

        self.basepath = job_config.directory
        self.ws_id = job_config.workspace
        self.sdk = PydioSdk(
            job_config.server,
            ws_id=self.ws_id,
            remote_folder=job_config.remote_folder,
            user_id=job_config.user_id
        )
        self.system = SystemSdk(job_config.directory)
        self.remote_seq = 0
        self.local_seq = 0
        self.local_target_seq = 0
        self.remote_target_seq = 0
        self.local_seqs = []
        self.remote_seqs = []
        self.db_handler = LocalDbHandler(self.data_base, job_config.directory)
        self.interrupt = False
        self.event_timer = 2
        self.online_timer = 10
        self.offline_timer = 60
        self.online_status = True
        self.job_status_running = True
        self.direction = job_config.direction
        self.event_logger = EventLogger(self.data_base)
        dispatcher.send(signal=PUBLISH_SIGNAL, sender=self, channel='status', message='START')

        if os.path.exists(self.data_base + "/sequences"):
            try:
                sequences = pickle.load(open(self.data_base + "/sequences", "rb"))
                self.remote_seq = sequences['remote']
                self.local_seq = sequences['local']
            except Exception:
                # Wrong content, remove sequences file.
                os.unlink(self.data_base + "/sequences")

        if job_config.direction != 'down':
            self.watcher = LocalWatcher(job_config.directory,
                                        job_config.filters['includes'],
                                        job_config.filters['excludes'],
                                        job_data_path)
        dispatcher.connect( self.handle_transfer_rate_event, signal=TRANSFER_RATE_SIGNAL, sender=dispatcher.Any )

    def init_global_progress(self):
        """
        Initialize the internal progress data
        :return:None
        """
        self.global_progress = {
            'queue_length'      :0,
            'queue_done'        :0,
            'queue_bytesize'    :0,
            'last_transfer_rate':-1,
            'queue_start_time'  :time.clock(),
            'total_time'        :0
        }


    def handle_transfer_rate_event(self, sender, transfer_rate):
        """
        Handler for TRANSFER_SIGNAL to update the transfer rate internally. It's averaged with previous value.
        :param sender:Any
        :param transfer_rate:float
        :return:
        """
        if self.global_progress['last_transfer_rate'] > 0:
            self.global_progress['last_transfer_rate'] = (float(transfer_rate) + self.global_progress['last_transfer_rate']) / 2.0
        else:
            self.global_progress['last_transfer_rate'] = float(transfer_rate)

    def is_running(self):
        """
        Whether the job is in Running state or not.
        :return:bool
        """
        return self.job_status_running

    def get_global_progress(self):
        """
        Compute a dict representation with many indications about the current state of the queue
        :return: dict
        """
        self.global_progress['total_time'] = time.clock() - self.global_progress['queue_start_time']
        self.global_progress["queue_bytesize"] = self.compute_queue_bytesize()
        # compute an eta
        eta = -1
        if self.global_progress['last_transfer_rate'] > -1 and self.global_progress['queue_bytesize'] > 0 :
            eta = self.global_progress['queue_bytesize'] / self.global_progress['last_transfer_rate']
        elif self.global_progress['queue_done']:
            remaining_operations = self.global_progress['queue_length'] - self.global_progress['queue_done']
            eta = remaining_operations * self.global_progress['total_time'] / self.global_progress['queue_done']

        self.global_progress['eta'] = eta

        logging.debug(self.global_progress)
        return self.global_progress

    def get_current_tasks(self):
        """
        Get a list of the current tasks
        :return: list()
        """
        if not self.tasks:
            return []
        return list(self.tasks)

    def start_now(self):
        """
        Resume task (set it in running mode) and make sure the cycle starts now
        :return:
        """
        self.resume()
        self.last_run = 0

    def pause(self):
        """
        Set the task in pause. The thread is still running, but the cycle does nothing.
        :return:None
        """
        self.job_status_running = False
        self.info('Job Paused', toUser='PAUSE', channel='status')

    def resume(self):
        """
        Set the task out of pause mode.
        :return:
        """
        self.job_status_running = True
        self.info('Job Started', toUser='START', channel='status')

    def stop(self):
        """
        Set the thread in "interrupt" mode : will try to stop cleanly, and then the thread will stop.
        :return:
        """
        if hasattr(self, 'watcher'):
            logging.debug("Stopping watcher: %s" % self.watcher)
            self.watcher.stop()
        self.info('Job stopping', toUser='PAUSE', channel='status')
        self.interrupt = True

    def sleep_offline(self):
        """
        Sleep the thread for a "long" time (offline time)
        :return:
        """
        self.online_status = False
        self.last_run = time.time()
        time.sleep(self.event_timer)

    def sleep_online(self):
        """
        Sleep the thread for a "short" time (online time)
        :return:
        """
        self.online_status = True
        self.last_run = time.time()
        time.sleep(self.event_timer)

    def compute_queue_bytesize(self):
        """
        Sum all the bytesize of the nodes that are planned to be uploaded/downloaded in the queue.
        :return:float
        """
        total = 0
        for task in self.tasks:
            if 'remaining_bytes' in task:
                total += float(task['remaining_bytes'])
            elif "node" in task and task["node"]["md5"] != "directory" and task["node"]["bytesize"]:
                total += float(task["node"]["bytesize"])
        return float(total)

    def run(self):
        """
        Start the thread
        """
        if hasattr(self, 'watcher'):
            self.watcher.start()

        self.last_run = 0

        while not self.interrupt:

            try:
                self.tasks = []
                self.init_global_progress()
                interval = int(time.time() - self.last_run)
                if (self.online_status and interval < self.online_timer) or (not self.online_status and interval < self.offline_timer):
                    time.sleep(self.event_timer)
                    continue

                if not self.job_status_running:
                    logging.debug("self.online_timer: %s" % self.online_timer)
                    self.sleep_offline()
                    continue

                if not self.system.check_basepath():
                    logging.info('Cannot find local folder! Did you disconnect a volume? Waiting %s seconds before retry' % self.offline_timer)
                    self.sleep_offline()
                    continue

                # Load local and/or remote changes, depending on the direction
                local_changes = dict(data=dict(), path_to_seqs=dict())
                remote_changes = dict(data=dict(), path_to_seqs=dict())
                try:
                    if self.job_config.direction != 'up':
                        logging.info('Loading remote changes with sequence ' + str(self.remote_seq))
                        self.remote_target_seq = self.get_remote_changes(self.remote_seq, remote_changes)
                    else:
                        self.remote_target_seq = 1
                        self.ping_remote()
                except ConnectionError as ce:
                    logging.info('No connection detected, waiting %s seconds to retry' % self.offline_timer)
                    self.sleep_offline()
                    continue
                except Exception as e:
                    logging.info('Error while connecting to remote server (%s), waiting for %i seconds before retempting ' % (e.message, self.offline_timer))
                    self.sleep_offline()
                    continue
                self.online_status = True
                if not self.job_config.server_configs:
                    self.job_config.server_configs = self.sdk.load_server_configs()
                self.sdk.set_server_configs(self.job_config.server_configs)

                if self.job_config.direction != 'down':
                    logging.info('Loading local changes with sequence ' + str(self.local_seq))
                    self.local_target_seq = self.db_handler.get_local_changes(self.local_seq, local_changes)
                else:
                    self.local_target_seq = 1

                self.local_seqs = local_changes['data'].keys() #map(lambda x:x['seq'], local_changes)
                self.remote_seqs = remote_changes['data'].keys() #map(lambda x:x['seq'], remote_changes)
                logging.info('Reducing changes')
                conflicts = []
                changes = self.reduce_changes(local_changes, remote_changes, conflicts)
                if len(conflicts):
                    logging.info('Conflicts detected, cannot continue!')
                    self.store_conflicts(conflicts)
                    self.sleep_offline()
                    continue

                if len(changes):
                    self.global_progress['queue_length'] = len(changes)
                    self.tasks = deque(changes)
                    logging.info('Processing %i changes' % len(changes))
                    i = 1
                    while len(self.tasks):
                        change = self.tasks[0]
                        try:
                            self.process_change(change)
                            self.remove_seq(change['seq'], change['location'])
                        except ProcessException as pe:
                            logging.error(pe.message)
                        except OSError as e:
                            logging.error(e.message)
                        self.tasks.popleft()
                        progress_percent = (float(i)/len(changes) * 100)
                        self.global_progress['queue_done'] = i
                        i += 1
                        if self.interrupt or not self.job_status_running:
                            break
                        time.sleep(0.05)
                else:
                    logging.info('No changes detected')
            except OSError as e:
                logging.error('Type Error! ')
            logging.info('Finished this cycle, waiting for %i seconds' % self.online_timer)
            self.init_global_progress()
            self.sleep_online()

    def remove_seq(self, seq_id, location):
        """
        Remove a sequence
        :param seq_id:
        :param location:
        :return:
        """
        if location == 'local':
            self.local_seqs.remove(seq_id)
            if len(self.local_seqs):
                self.local_seq = min(min(self.local_seqs), self.local_target_seq)
            else:
                self.local_seq = self.local_target_seq
        else:
            self.remote_seqs.remove(seq_id)
            if len(self.remote_seqs):
                self.remote_seq = min(min(self.remote_seqs), self.remote_target_seq)
            else:
                self.remote_seq = self.remote_target_seq
        pickle.dump(dict(
            local=self.local_seq,
            remote=self.remote_seq
        ), open(self.data_base + '/sequences', 'wb'))

    def stat_path(self, path, location, stats=None, with_hash=False):
        """
        Stat a path, calling the correct SDK depending on the location passed.
        :param path:Node path
        :param location:"remote" or "local"
        :param stats: if they were already previously bulk_loaded, will just look for the path in that dict()
        :param with_hash:bool ask for content hash or not
        :return:
        """
        try:
            if stats:
                return stats[path]
        except KeyError:
            pass

        if location == 'remote':
            return self.sdk.stat(path, with_hash)
        else:
            return self.system.stat(path, with_hash=True)

    def ping_remote(self):
        """
        Simple stat of the remote server root, to know if it's reachable.
        :return:bool
        """
        test = self.sdk.stat('/')
        return (test != False)

    def filter_change(self, item, my_stat=None, other_stats=None):
        """
        Try to detect if a change can be ignored, depending on the state of the "target". For example, if a delete
        is registered and the file already cannot be found, we can just ignore it.
        :param item:change item
        :param my_stat:stats of the files on the same side as source
        :param other_stats:stats of the files of the other side
        :return:
        """

        location = item['location']
        opposite = 'local' if item['location'] == 'remote' else 'remote'
        res = False
        if item['type'] == 'create' or item['type'] == 'content':
            # If it does not exist on remote side, skip
            test_stat = self.stat_path(item['node']['node_path'], location=opposite, stats=other_stats, with_hash=True)
            if not test_stat:
                return False
            # If it exists but is a directory, it won't change
            if item['node']['md5'] == 'directory':
                res = True
            # If it exists and has same size, ok
            elif test_stat['size'] == item['node']['bytesize'] and 'hash' in test_stat and test_stat['hash'] == item['node']['md5']:
                res = True
        elif item['type'] == 'delete':
            # Shall we really delete it?
            loc_stat = self.stat_path(item['source'], location=location, stats=my_stat)
            if loc_stat:
                res = True
            # Shall we delete if already absent? no!
            test_stat = self.stat_path(item['source'], location=opposite, stats=other_stats)
            if not test_stat:
                res = True
        else:#MOVE
            source_stat = self.stat_path(item['source'], location=opposite, stats=other_stats)
            target_stat = self.stat_path(item['target'], location=opposite, stats=other_stats, with_hash=True)
            if not target_stat or source_stat:
                return False
            elif item['node']['md5'] == 'directory':
                res = True
            elif target_stat['size'] == item['node']['bytesize'] and 'hash' in target_stat and target_stat['hash'] == item['node']['md5']:
                res = True

        if res:
            if item['type'] != 'delete':
                logging.debug('['+location+'] Filtering out ' + item['type'] + ': ' + item['node']['node_path'])
            else:
                logging.debug('['+location+'] Filtering out ' + item['type'] + ' ' + item['source'])
            self.remove_seq(item['seq'], location)
            return True

        return False

    def changes_sorter(self, i1, i2):
        """
        Make sure to have the directories appearing on top
        :param i1:
        :param i2:
        :return:
        """
        if not i1['node']:
            return -1
        if not i2['node']:
            return 1

        # directory
        if i1['node']['md5'] == 'directory' and i2['node']['md5'] == 'directory':
            return cmp(i1['node']['node_path'], i2['node']['node_path'])

        if i1['node']['md5'] == 'directory':
            return -1
        if i2['node']['md5'] == 'directory':
            return 1

        # sort on path otherwise
        return cmp(i1['node']['node_path'], i2['node']['node_path'])

    def info(self, message, toUser=False, channel='sync'):
        logging.info(message)
        if toUser:
            dispatcher.send(signal=PUBLISH_SIGNAL, sender=self, channel=channel, message=message)

    def process_localMKDIR(self, path):
        message = path + ' <============ MKDIR'
        os.makedirs(self.basepath + path)
        self.info(message, 'New folder created at '+ path )
        self.event_logger.log(type='local', action='mkdir', status='undefined', target=path, message=message)

    def process_remoteMKDIR(self, path):
        message = 'MKDIR ============> ' + path
        self.info(message, toUser=False)
        self.sdk.mkdir(path)
        self.event_logger.log(type='remote', action='mkdir', status='undefined', target=path, message=message)

    def process_localDELETE(self, path):
        if os.path.isdir(self.basepath + path):
            self.system.rmdir(path)
            message = path + ' <============ DELETE'
            self.info(message, 'Deleted folder ' + path)
            self.event_logger.log(type='local', action='delete_folder', status='undefined', target=path, message=message)
        elif os.path.isfile(self.basepath + path):
            os.unlink(self.basepath + path)
            message = path + ' <============ DELETE'
            self.info(message, 'Deleted file ' + path)
            self.event_logger.log(type='local', action='delete_file', status='undefined', target=path, message=message)

    def process_remoteDELETE(self, path):
        self.sdk.delete(path)
        message = 'DELETE ============> ' + path
        self.info(message, False)
        self.event_logger.log(type='remote', action='delete', status='undefined', target=path, message=message)

    def process_localMOVE(self, source, target):
        if os.path.exists(self.basepath + source):
            if not os.path.exists(self.basepath + os.path.dirname(target)):
                os.makedirs(self.basepath + os.path.dirname(target))
            os.rename(self.basepath + source, self.basepath + target)
            message = source + ' to ' + target + ' <============ MOVE'
            self.info(message, 'Moved ' + source + ' to ' + target)
            self.event_logger.log(type='local', action='move', status='undefined', target=target, source=source, message=message)
            return True
        return False

    def process_remoteMOVE(self, source, target):
        message = 'MOVE ============> ' + source + ' to ' + target
        self.info(message, toUser=False)
        self.event_logger.log(type='remote', action='move', status='undefined', target=target, source=source, message=message)
        self.sdk.rename(source, target)

    def process_DOWNLOAD(self, path, callback_dict=None):
        self.db_handler.update_node_status(path, 'DOWN')
        self.sdk.download(path, self.basepath + path, callback_dict)
        self.db_handler.update_node_status(path, 'IDLE')
        message = path + ' <=============== ' + path
        self.info(message, 'File ' + path + ' downloaded from server')
        self.event_logger.log(type='local', action='download', status='undefined', target=path, message=message)

    def process_UPLOAD(self, path, callback_dict=None):
        self.db_handler.update_node_status(path, 'UP')
        max_upload_size = -1
        if self.job_config.server_configs and 'UPLOAD_MAX_SIZE' in self.job_config.server_configs:
            max_upload_size = int( self.job_config.server_configs['UPLOAD_MAX_SIZE'] )
        self.sdk.upload(self.basepath+path, self.system.stat(path), path, callback_dict, max_upload_size=max_upload_size)
        self.db_handler.update_node_status(path, 'IDLE')
        message = path + ' ===============> ' + path
        self.info(message, 'File ' + path + ' uploaded to server')
        self.event_logger.log(type='remote', action='upload', status='undefined', target=path, message=message)


    def process_change(self, item):
        """
        Now really process a "change"
        :param item:change item
        :return:
        """
        location = item['location']
        item['progress'] = 0
        if self.direction == 'up' and location == 'remote':
            return
        if self.direction == 'down' and location == 'local':
            return

        if item['type'] == 'create' or item['type'] == 'content':
            if item['node']['md5'] == 'directory':
                if item['node']['node_path']:
                    logging.info('[' + location + '] Create folder ' + item['node']['node_path'])
                    if location == 'remote':
                        self.process_localMKDIR(item['node']['node_path'])
                        self.db_handler.buffer_real_operation(item['type'], 'NULL', item['node']['node_path'])
                    else:
                        self.process_remoteMKDIR(item['node']['node_path'])
            else:
                if item['node']['node_path']:
                    if location == 'remote':
                        self.process_DOWNLOAD(item['node']['node_path'], callback_dict=item )
                        if item['type'] == 'create':
                            self.db_handler.buffer_real_operation(item['type'], 'NULL', item['node']['node_path'])
                        else:
                            self.db_handler.buffer_real_operation(item['type'], item['node']['node_path'], item['node']['node_path'])
                    else:
                        self.process_UPLOAD(item['node']['node_path'], item)

        elif item['type'] == 'delete':
            logging.info('[' + location + '] Should delete ' + item['source'])
            if location == 'remote':
                self.process_localDELETE(item['source'])
                self.db_handler.buffer_real_operation('delete', item['source'], 'NULL')
            else:
                self.process_remoteDELETE(item['source'])

        else:
            logging.info('[' + location + '] Should move ' + item['source'] + ' to ' + item['target'])
            if location == 'remote':
                if os.path.exists(self.basepath + item['source']):
                    if self.process_localMOVE(item['source'], item['target']):
                        self.db_handler.buffer_real_operation(item['type'], item['source'], item['target'])
                else:
                    if item["node"]["md5"] == "directory":
                        logging.debug('Cannot find folder to move, switching to creation')
                        self.process_localMKDIR(item['target'])
                    else:
                        logging.debug('Cannot find source, switching to DOWNLOAD')
                        self.process_DOWNLOAD(item['target'], callback_dict=item)
                    self.db_handler.buffer_real_operation('create', 'NULL', item['target'])
            else:
                if self.sdk.stat(item['source']):
                    self.process_remoteMOVE(item['source'], item['target'])
                elif item['node']['md5'] != 'directory':
                    logging.debug('Cannot find source, switching to UPLOAD')
                    self.process_UPLOAD(item['target'], item)

    def reduce_changes(self, local_changes=dict(), remote_changes=dict(), conflicts=[]):
        """
        Merge local changes and remote changes by trying to detect when they are similar (can be ignored), or
        where they conflict. This is the core of the algorithm.
        :param local_changes:dict() all local changes since last sync
        :param remote_changes: dict() all remote changes since last sync
        :param conflicts: An accumulator
        :return:
        """
        rchanges = remote_changes['data'].values()
        lchanges = local_changes['data'].values()

        for seq, item in local_changes['data'].items():
            logging.debug("seq, item: %s %s" % (seq, item))
            pathes = []
            if item['source'] != 'NULL':
                pathes.append(item['source'])
            if item['target'] != 'NULL':
                pathes.append(item['target'])
            # search these pathes in remote_changes
            remote_sequences = []
            for x in pathes:
                remote_sequences = remote_sequences + remote_changes['path_to_seqs'].setdefault(x, [])
            for seq_id in remote_sequences:
                otheritem = remote_changes['data'][seq_id]
                try:
                    if not (item['type'] == otheritem['type']):
                        continue
                    if not item['node'] and not otheritem['node'] and (item['source'] == otheritem['source']):
                        logging.debug('Reconciliation sequence for change (source)'+item['source'])
                        lchanges.remove(item)
                        rchanges.remove(otheritem)
                        self.remove_seq(item['seq'], 'local')
                        self.remove_seq(otheritem['seq'], 'remote')
                        break

                    if not (os.path.normpath(item['node']['node_path']) == os.path.normpath(otheritem['node']['node_path'])):
                        continue
                    if item['node']['bytesize'] == otheritem['node']['bytesize'] and item['node']['md5'] == otheritem['node']['md5']:
                        logging.debug('Reconciliation sequence for change (node)'+item['node']['node_path'])
                        lchanges.remove(item)
                        rchanges.remove(otheritem)
                        self.remove_seq(item['seq'], 'local')
                        self.remove_seq(otheritem['seq'], 'remote')
                        break
                except Exception as e:
                    pass

        test_stats = list(set(map(lambda it: it['source'] if it['source'] != 'NULL' else it['target'], lchanges)))
        remote_stats = None
        if len(test_stats):
            remote_stats = self.sdk.bulk_stat(test_stats, with_hash=True)

        rchanges = filter(lambda it: not self.filter_change(it, remote_stats, None), rchanges)
        lchanges = filter(lambda it: not self.filter_change(it, None, remote_stats), lchanges)

        last_ops = self.db_handler.get_last_operations()

        new_rchanges = []

        for item in lchanges:
            logging.debug("item: %s" % item)
            ignore = False
            for last in last_ops:
                if last['type'] == item['type'] and last['source'] == item['source'] and last['target'] == item['target']:
                    logging.info('IGNORING, RECENT MOVE FROM SERVER', last)
                    ignore = True
                    break
            if ignore:
                continue
            conflict = False
            for rItem in rchanges:
                if (not item['node'] and not rItem['node'] and rItem['source'] == rItem['source']) or (item['node'] and rItem['node'] and item['node']['node_path'] and rItem['node']['node_path'] and os.path.normpath(item['node']['node_path']) == os.path.normpath(rItem['node']['node_path'])):
                    # Seems there is a conflict - check
                    c_path = item['source']
                    if item['node']:
                        c_path = item['node']['node_path']
                    status = self.db_handler.get_node_status(c_path)
                    if status == 'SOLVED:KEEPLOCAL':
                        rchanges.remove(rItem)
                    elif status == 'SOLVED:KEEPREMOTE':
                        conflict = True
                    else:
                        conflict = True
                        rchanges.remove(rItem)
                        conflicts.append({'local':item,'remote':rItem})
                    break
            if conflict:
                continue
            new_rchanges.append(item)

        self.db_handler.clear_operations_buffer()

        # Sort to make sure directory operations are applied first
        rchanges = sorted(rchanges + new_rchanges, cmp=self.changes_sorter)

        # Prune changes : for DELETE and MOVE of Dir, remove all childrens
        toremove = []
        for i in range(len(rchanges)):
            ch = rchanges[i]
            if ch['type'] == 'path' and not ch['source'] == 'NULL' and not ch['target'] == 'NULL' and ch['node']['md5'] == 'directory':
                if i < len(rchanges)-1:
                    for j in range(i+1,len(rchanges)):
                        if rchanges[j]['source'] and rchanges[j]['type'] == 'path' and rchanges[j]['source'].startswith(ch['source']+'/'):
                            toremove.append(rchanges[j])

        if len(toremove):
            for r in toremove:
                if r in rchanges: rchanges.remove(r)

        logging.debug("rchanges: %s" % rchanges)
        return rchanges

    def store_conflicts(self, conflicts):
        """
        Store conflicts in the DB.
        :param conflicts:list()
        :return:
        """
        for conflict in conflicts:
            local = conflict["local"]
            remote = conflict["remote"]
            if local["node"]:
                path = local["node"]["node_path"]
            elif local["source"]:
                path = local["source"]
            else:
                path = local["target"]
            self.db_handler.update_node_status(path, 'CONFLICT', pickle.dumps(remote))

    def get_remote_changes(self, seq_id, changes=dict()):
        """
        Load changes from remote server using the API.
        :param seq_id:
        :param changes:
        :return:
        """
        # Test for loading changes as stream. Drastically reduces memory for huge
        # number of changes
        #
        # from change_stores import SqliteChangeStore
        # store = SqliteChangeStore(self.data_base + '/changes.sqlite')
        # store.open()
        # last_seq = self.sdk.changes_stream(seq_id, store.store)
        # store.sync()
        # store.close()
        # return last_seq

        logging.debug('Remote sequence ' + str(seq_id))
        data = self.sdk.changes(seq_id)
        for (i, item) in enumerate(data['changes']):
            item['location'] = 'remote'
            key = item['source'] if item['source'] != 'NULL' else item['target']
            if not key in changes['path_to_seqs']:
                changes['path_to_seqs'][key] = []
            changes['path_to_seqs'][key].append(item['seq'])
            changes['data'][item['seq']] = item

        return data['last_seq']
