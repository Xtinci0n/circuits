# Package:  manager
# Date:     11th April 2010
# Author:   James Mills, prologic at shortcircuit dot net dot au

"""Manager

This module defines the Manager class subclasses by component.BaseComponent
"""

import atexit
from time import sleep
from itertools import chain
from collections import deque
from traceback import format_tb
from sys import exc_info as _exc_info
from signal import signal, SIGINT, SIGTERM
from inspect import getmembers, isfunction
from types import MethodType, GeneratorType
from threading import current_thread, Thread
from multiprocessing import current_process, Process

from .values import Value
from .handlers import handler
from .events import Done, Success, Failure
from .events import Error, Started, Stopped, Signal


TIMEOUT = 0.01  # 10ms timeout when no tick functions to process


def _sortkey(handler):
    return (handler.priority, handler.filter)


class Manager(object):
    """Manager

    This is the base Manager of the BaseComponent which manages an Event Queue,
    a set of Event Handlers, Channels, Tick Functions, Registered and Hidden
    Components, a Task and the Running State.
    """

    def __init__(self, *args, **kwargs):
        "initializes x; see x.__class__.__doc__ for signature"

        self._tasks = set()
        self._ticks = set()
        self._cache = dict()
        self._queue = deque()
        self._globals = set()
        self._handlers = dict()

        self._task = None
        self._running = False

        self.root = self.parent = self
        self.components = set()

    def __repr__(self):
        "x.__repr__() <==> repr(x)"

        name = self.__class__.__name__

        if hasattr(self, "channel") and self.channel is not None:
            channel = "/%s" % self.channel
        else:
            channel = ""

        q = len(self._queue)
        state = "R" if self.running else "S"

        pid = current_process().pid

        if pid:
            id = "%s:%s" % (pid, current_thread().getName())
        else:
            id = current_thread().getName()

        format = "<%s%s %s (queued=%d) [%s]>"
        return format % (name, channel, id, q, state)

    def __contains__(self, y):
        """x.__contains__(y) <==> y in x

        Return True if the Component y is registered.
        """

        components = self.components.copy()
        return y in components or y in [c.__class__ for c in components]

    def __len__(self):
        """x.__len__() <==> len(x)

        Returns the number of events in the Event Queue.
        """

        return len(self._queue)

    def __add__(self, y):
        """x.__add__(y) <==> x+y

        (Optional) Convenience operator to register y with x
        Equivalent to: y.register(x)

        @return: x
        @rtype Component or Manager
        """

        y.register(self)
        return self

    def __iadd__(self, y):
        """x.__iadd__(y) <==> x += y

        (Optional) Convenience operator to register y with x
        Equivalent to: y.register(x)

        @return: x
        @rtype Component or Manager
        """

        y.register(self)
        return self

    def __sub__(self, y):
        """x.__sub__(y) <==> x-y

        (Optional) Convenience operator to unregister y from x.manager
        Equivalent to: y.unregister()

        @return: x
        @rtype Component or Manager
        """

        if y.manager is not y:
            y.unregister()
        return self

    def __isub__(self, y):
        """x.__sub__(y) <==> x -= y

        (Optional) Convenience operator to unregister y from x
        Equivalent to: y.unregister()

        @return: x
        @rtype Component or Manager
        """

        if y.manager is not y:
            y.unregister()
        return self

    @property
    def name(self):
        """Return the name of this Component/Manager"""

        return self.__class__.__name__

    @property
    def running(self):
        """Return the running state of this Component/Manager"""

        return self._running

    def getHandlers(self, event, channel):
        channel_is_instance = isinstance(channel, Manager)
        if channel_is_instance and channel != self:
            return channel.getHandlers(event, channel)

        name = event.name
        handlers = set()

        handlers_chain = [self._handlers.get("*", set())]

        if name in self._handlers:
            handlers_chain.append(self._handlers[name])

        for handler in chain(*handlers_chain):
            if handler.channel:
                handler_channel = handler.channel
            elif hasattr(handler, "__self__"):
                handler_channel = getattr(handler.__self__, "channel", None)
            else:
                handler_channel = None

            if channel == "*" or handler_channel in ("*", channel,) \
                    or channel_is_instance:
                handlers.add(handler)

        handlers.update(self._globals)

        if not channel_is_instance:
            for c in self.components.copy():
                handlers.update(c.getHandlers(event, channel))

        return handlers

    def addHandler(self, f):
        if isfunction(f):
            method = MethodType(f, self, self.__class__)
        else:
            method = f

        setattr(self, method.__name__, method)

        if not method.names and method.channel == "*":
            self._globals.add(method)
        elif not method.names:
            self._handlers.setdefault("*", set()).add(method)
        else:
            for name in method.names:
                self._handlers.setdefault(name, set()).add(method)

        self.root._cache.clear()

    def removeHandler(self, f, event=None):
        if isfunction(f):
            method = MethodType(f, self, self.__class__)
        else:
            method = f

        if event is None:
            names = method.names
        else:
            names = [event]

        for name in names:
            self._handlers[name].remove(method)
            if not self._handlers[name]:
                del self._handlers[name]
                try:
                    delattr(self, method.__name__)
                except AttributeError:
                    # Handler was never part of self
                    pass

        self.root._cache.clear()

    def registerChild(self, component):
        self.components.add(component)
        self.root._queue.extend(list(component._queue))
        component._queue.clear()
        self.root._cache.clear()
        self.root._ticks = self.root.getTicks()

    def unregisterChild(self, component):
        self.components.remove(component)
        self.root._cache.clear()
        self.root._ticks = self.root.getTicks()

    def _fire(self, event, channel):
        self._queue.append((event, channel))

    def fireEvent(self, event, *channels):
        """Fire an event into the system.

        :param event: The event that is to be fired.
        :param channels: The channels that this event is delivered on.
           If no channels are specified, the event is delivered to the
           channels found in the event's :attr:`channel` attribute.
           If this attribute is not set, the event is delivered to
           the firing component's channel. And eventually,
           when set neither, the event is delivered on all
           channels ("*").
        """

        if not channels:
            channels = event.channels \
                    or (getattr(self, "channel", "*"),) \
                    or ("*",)

        event.channels = channels

        event.value = Value(event, self, getattr(event, 'notify', False))
        self.root._fire(event, channels)

        return event.value

    fire = fireEvent

    def registerTask(self, g):
        self._tasks.add(g)

    def unregisterTask(self, g):
        if g in self._tasks:
            self._tasks.remove(g)

    def waitEvent(self, event, channel=None):
        state = {
            'run': False,
            'flag': False,
            'event': None,
        }
        _event = event

        @handler(event, channel=channel)
        def _on_event(self, event, *args, **kwargs):
            if not state['run']:
                self.removeHandler(_on_event, _event)
                event.alert_done = True
                state['run'] = True
                state['event'] = event

        @handler("%s_done" % event, channel=channel)
        def _on_done(self, event, source, *args, **kwargs):
            if state['event'] == source:
                state['flag'] = True

        self.addHandler(_on_event)
        self.addHandler(_on_done)

        while not state['flag']:
            yield None

        self.removeHandler(_on_done, "%s_done" % event)

    wait = waitEvent

    def callEvent(self, event, *channels):
        value = self.fire(event, *channels)
        for r in self.waitEvent(event.name):
            yield r
        yield value

    call = callEvent

    def _flush(self):
        q = self._queue
        self._queue = deque()

        for event, channels in q:
            self._dispatcher(event, channels)

    def flushEvents(self):
        """Flush all Events in the Event Queue"""

        self.root._flush()

    flush = flushEvents

    def _dispatcher(self, event, channels):
        eargs = event.args
        ekwargs = event.kwargs

        if (event.name, channels) in self._cache:
            handlers = self._cache[(event.name, channels)]
        else:
            h = (self.getHandlers(event, channel) for channel in channels)
            handlers = sorted(chain(*h), key=_sortkey, reverse=True)
            self._cache[(event.name, channels)] = handlers

        value = None
        error = None

        for handler in handlers:
            event.handler = handler
            try:
                if handler.event:
                    value = handler(event, *eargs, **ekwargs)
                else:
                    value = handler(*eargs, **ekwargs)
            except (KeyboardInterrupt, SystemExit):
                raise
            except:
                etype, evalue, etraceback = _exc_info()
                traceback = format_tb(etraceback)
                error = (etype, evalue, traceback)

                event.value.errors = True

                value = error

                if event.failure:
                    self.fire(Failure.create("%sFailure" %
                        event.__class__.__name__, event, error),
                        *event.channels)

                self.fire(Error(etype, evalue, traceback, handler))

            if type(value) is GeneratorType:
                event.waitingHandlers += 1
                event.value.promise = True
                self.registerTask((event, value))
            elif value is not None:
                event.value.value = value

            if value and handler.filter:
                break

        self._eventDone(event, error)

    def _eventDone(self, event, error=None):
        if event.waitingHandlers:
            return

        if event.alert_done:
            self.fire(Done.create("%sDone" %
                event.__class__.__name__, event, event.value),
                *event.channels)

        if error is None and event.success:
            self.fire(Success.create("%sSuccess" %
                event.__class__.__name__, event, event.value), *event.channels)

    def _signalHandler(self, signal, stack):
        self.fire(Signal(signal, stack))
        if signal in [SIGINT, SIGTERM]:
            self.stop()

    def start(self, process=False):
        """
        Start a new thread or process that invokes this manager's
        ``run()`` method. The invocation of this method returns
        immediately after the task or process has been started.
        """
        Task = Process if process else Thread

        self._task = Task(target=self.run, name=self.name)

        self._task.daemon = True
        self._task.start()

    def stop(self):
        """
        Stop this manager. Invoking this method either causes
        an invocation of ``run()`` to return or terminates the
        thread or process associated with the manager.
        """
        if not self.running:
            return

        self._running = False
        self.fire(Stopped(self))

        for _ in range(3):
            self.tick()

        self._task = None

    def getTicks(self):
        ticks = set()

        p = lambda f: callable(f) and getattr(f, 'tick', False) is True
        for k, v in getmembers(self, p):
            ticks.add(v)

        # Kept for backward compatibility
        if getattr(self, '__tick__', False):
            ticks.add(self.__tick__)

        for c in self.components.copy():
            ticks.update(c.getTicks())

        return ticks


    def processTask(self, event, task, parent=None):
        value = None
        try:
            value = task.next()
            if type(value) is GeneratorType:
                event.waitingHandlers += 1
                self.registerTask((event, value, task))
                self.unregisterTask((event, task))
                # We want to process all the tasks because
                # we bind handlers in there
                self.processTask(event, value)
            elif value is not None:
                event.value.value = value
        except StopIteration:
            self.unregisterTask((event, task))
            if parent:
                self.registerTask((event, parent))
            event.waitingHandlers -= 1
            if event.waitingHandlers == 0:
                event.value.inform(True)
                self._eventDone(event)
        except:
            self.unregisterTask((event, task))

            etype, evalue, etraceback = _exc_info()
            traceback = format_tb(etraceback)
            error = (etype, evalue, traceback)

            event.value.value = value
            event.value.errors = True
            event.value.inform(True)

            if event.failure:
                self.fire(Failure.create("%sFailure" %
                    event.__class__.__name__, event, error),
                    *event.channels)

            self.fire(Error(etype, evalue, traceback, handler))

    def tick(self):
        for f in self._ticks.copy():
            try:
                f()
            except (KeyboardInterrupt, SystemExit):
                raise
            except:
                etype, evalue, etraceback = _exc_info()
                self.fire(Error(etype, evalue, format_tb(etraceback)))

        for task in self._tasks.copy():
            self.processTask(*task)

        if self:
            self.flush()
        else:
            sleep(TIMEOUT)

    def run(self):
        """
        Run this manager. The method continuously checks for events
        on the event queue of the component hierarchy, and invoke
        associated handlers. It also invokes the component's
        "tick"-handlers at regular intervals.

        The method returns when the manager's ``stop()`` method is invoked.

        If invoked by a programs main thread, a signal handler for
        the ``INT`` and ``TERM`` signals is installed. This handler
        fires the corresponding :class:`circuits.core.events.Signal`
        events and then calls ``stop()`` for the manager.
        """
        atexit.register(self.stop)

        if current_thread().getName() == "MainThread":
            try:
                signal(SIGINT, self._signalHandler)
                signal(SIGTERM, self._signalHandler)
            except ValueError:
                # Ignore if we can't install signal handlers
                pass

        self._running = True

        self.fire(Started(self))

        try:
            while self or self.running:
                self.tick()
        finally:
            self.tick()
