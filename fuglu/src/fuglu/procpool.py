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
from __future__ import print_function
import multiprocessing
import multiprocessing.queues

from fuglu.scansession import SessionHandler
import fuglu.core
import logging
import traceback
import importlib
import pickle
from fuglu.stats import Statskeeper, StatDelta
import threading

class ProcManager(object):
    def __init__(self, numprocs = None, queuesize=100, config = None):
        self._child_id_counter=0
        self.manager = multiprocessing.Manager()
        self.shared_state = self._init_shared_state()
        self.config = config
        self.numprocs = numprocs
        self.workers = []
        self.queuesize = queuesize
        self.tasks = multiprocessing.queues.Queue(queuesize)
        self.child_to_server_messages = multiprocessing.queues.Queue()

        self.logger = logging.getLogger('%s.procpool' % __package__)
        self._stayalive = True
        self.name = 'ProcessPool'
        self.message_listener = MessageListener(self.child_to_server_messages)
        self.start()

    def _init_shared_state(self):
        shared_state = self.manager.dict()
        return shared_state

    @property
    def stayalive(self):
        return self._stayalive

    @stayalive.setter
    def stayalive(self, value):
        # procpool is shut down -> send poison pill to workers
        if self._stayalive and not value:
            self._stayalive = False
            self._send_poison_pills()
        self._stayalive = value

    def _send_poison_pills(self):
        """flood the queue with poison pills to tell all workers to shut down"""
        for _ in range(len(self.workers)):
            self.tasks.put_nowait(None)

    def add_task(self, session):
        if self._stayalive:
            self.tasks.put(session)

    def _create_worker(self):
        self._child_id_counter +=1
        worker_name = "Worker-%s"%self._child_id_counter
        worker = multiprocessing.Process(target=fuglu_process_worker, name=worker_name, args=(self.tasks, self.config, self.shared_state, self.child_to_server_messages))
        return worker

    def start(self):
        for i in range(self.numprocs):
            worker = self._create_worker()
            worker.start()
            self.workers.append(worker)

        # Start the child-to-parent message listener
        self.message_listener.start()

    def shutdown(self):
        self.stayalive = False
        self.message_listener.stayalive = False

class MessageListener(threading.Thread):
    def __init__(self, message_queue):
        threading.Thread.__init__(self)
        self.name = "Process Message Listener"
        self.message_queue = message_queue
        self.stayalive = True
        self.statskeeper = Statskeeper()
        self.daemon = True


    def run(self):
        while self.stayalive:
            message = self.message_queue.get()
            event_type = message['event_type']
            if event_type == 'statsdelta': # increase statistics counters
                try:
                    delta = StatDelta(**message)
                    self.statskeeper.increase_counter_values(delta)
                except:
                    print(traceback.format_exc())


def fuglu_process_worker(queue, config, shared_state,child_to_server_messages):
    logging.basicConfig(level=logging.DEBUG)
    workerstate = WorkerStateWrapper(shared_state,'loading configuration')
    logger = logging.getLogger('fuglu.process')

    # load config and plugins
    controller = fuglu.core.MainController(config)
    controller.load_extensions()
    controller.load_plugins()

    prependers = controller.prependers
    plugins = controller.plugins
    appenders = controller.appenders

    # forward statistics counters to parent process
    stats = Statskeeper()
    stats.stat_listener_callback.append(lambda event: child_to_server_messages.put(event.as_message()))

    try:
        while True:
            workerstate.workerstate = 'waiting for task'
            task = queue.get()
            if task is None: # poison pill
                logger.debug("Child process received poison pill - shut down")
                workerstate.workerstate = 'ended'
                return
            workerstate.workerstate = 'starting scan session'
            pickled_socket, handler_modulename, handler_classname = task
            sock = pickle.loads(pickled_socket)
            handler_class = getattr(importlib.import_module(handler_modulename), handler_classname)
            handler_instance = handler_class(sock, config)
            handler = SessionHandler(handler_instance, config,prependers, plugins, appenders)
            handler.handlesession(workerstate)
    except KeyboardInterrupt:
        workerstate.workerstate = 'ended'
    except:
        trb = traceback.format_exc()
        logger.error("Exception in child process: %s"%trb)
        print(trb)
        workerstate.workerstate = 'crashed'


class WorkerStateWrapper(object):
    def __init__(self, shared_state_dict, initial_state='created', process=None):
        self._state = initial_state
        self.shared_state_dict = shared_state_dict
        self.process = process
        if not process:
            self.process = multiprocessing.current_process()

        self._publish_state()

    def _publish_state(self):
        try:
            self.shared_state_dict[self.process.name] = self._state
        except EOFError:
            pass

    @property
    def workerstate(self):
        return self._state

    @workerstate.setter
    def workerstate(self, value):
        self._state = value
        self._publish_state()
