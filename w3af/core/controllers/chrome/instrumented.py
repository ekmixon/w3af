"""
instrumented.py

Copyright 2018 Andres Riancho

This file is part of w3af, http://w3af.org/ .

w3af is free software; you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation version 2 of the License.

w3af is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with w3af; if not, write to the Free Software
Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA  02110-1301  USA

"""
import os
import re
import time
import logging

from requests import ConnectionError

import w3af.core.controllers.output_manager as om

from w3af import ROOT_PATH
from w3af.core.controllers.tests.running_tests import is_running_tests
from w3af.core.controllers.profiling.utils.ps_mem import get_memory_usage
from w3af.core.controllers.chrome.devtools import DebugChromeInterface
from w3af.core.controllers.chrome.process import ChromeProcess
from w3af.core.controllers.chrome.proxy import LoggingProxy
from w3af.core.data.fuzzer.utils import rand_alnum


class InstrumentedChrome(object):
    """
    1. Start a proxy server
    2. Start a chrome process that navigates via the proxy
    3. Load a page in Chrome (via the proxy)
    4. Receive Chrome events which indicate when the page load finished
    5. Close the browser

    More features to be implemented later.
    """

    PROXY_HOST = '127.0.0.1'
    CHROME_HOST = '127.0.0.1'
    PAGE_LOAD_TIMEOUT = 10
    PAGE_WILL_CHANGE_TIMEOUT = 1

    PAGE_STATE_NONE = 0
    PAGE_STATE_LOADING = 1
    PAGE_STATE_LOADED = 2

    PAGINATION_PAGE_COUNT = 20

    JS_ONERROR_HANDLER = os.path.join(ROOT_PATH, 'core/controllers/chrome/js/onerror.js')
    JS_DOM_ANALYZER = os.path.join(ROOT_PATH, 'core/controllers/chrome/js/dom_analyzer.js')
    JS_SELECTOR_GENERATOR = os.path.join(ROOT_PATH, 'core/controllers/chrome/js/css-selector-generator.js')

    EVENT_TYPE_RE = re.compile('[a-zA-Z.]+')

    def __init__(self, uri_opener, http_traffic_queue):
        # These keep the internal state of the page based on received events
        self._frame_stopped_loading_event = None
        self._frame_scheduled_navigation = None
        self._network_almost_idle_event = None
        self._execution_context_created = None
        self._page_state = self.PAGE_STATE_NONE

        self.uri_opener = uri_opener
        self.http_traffic_queue = http_traffic_queue

        self.id = rand_alnum(8)
        self.debugging_id = None

        self.proxy = self.start_proxy()
        self.chrome_process = self.start_chrome_process()
        self.chrome_conn = self.connect_to_chrome()
        self.set_chrome_settings()

    def start_proxy(self):
        proxy = LoggingProxy(self.PROXY_HOST,
                             0,
                             self.uri_opener,
                             name='ChromeProxy',
                             queue=self.http_traffic_queue)

        proxy.set_debugging_id(self.debugging_id)

        proxy.start()
        proxy.wait_for_start()

        return proxy

    def get_proxy_address(self):
        return self.PROXY_HOST, self.proxy.get_bind_port()

    def get_first_response(self):
        return self.proxy.get_first_response()

    def get_first_request(self):
        return self.proxy.get_first_request()

    def start_chrome_process(self):
        chrome_process = ChromeProcess()

        proxy_host, proxy_port = self.get_proxy_address()
        chrome_process.set_proxy(proxy_host, proxy_port)

        chrome_process.start()
        chrome_process.wait_for_start()

        return chrome_process

    def connect_to_chrome(self):
        port = self.chrome_process.get_devtools_port()

        # The timeout we specify here is the websocket timeout, which is used
        # for send() and recv() calls.
        try:
            chrome_conn = DebugChromeInterface(host=self.CHROME_HOST,
                                               port=port,
                                               timeout=0.001,
                                               debugging_id=self.debugging_id)
        except ConnectionError:
            msg = 'Failed to connect to Chrome on port %s'
            raise InstrumentedChromeException(msg % port)

        chrome_conn.name = 'DebugChromeInterface'
        chrome_conn.daemon = True
        chrome_conn.start()

        # Set the handler that will maintain the page state
        chrome_conn.set_event_handler(self._page_state_handler)

        return chrome_conn

    def set_dialog_handler(self, dialog_handler):
        self.chrome_conn.set_dialog_handler(dialog_handler)

    def dialog_handler(self, _type, message):
        """
        This is the default dialog handler, it will just print to the log file
        the contents of the alert / prompt message and continue.

        It is possible to override the default handler by calling
        set_dialog_handler() right after creating an InstrumentedChrome instance
        and before loading any page.

        Handles Page.javascriptDialogOpening event [0] which freezes the browser
        until it is dismissed.

        [0] https://chromedevtools.github.io/devtools-protocol/tot/Page#event-javascriptDialogOpening

        :param _type: One of alert, prompt, etc.
        :param message: The message shown in the aler / prompt
        :return: A tuple containing:
                    * True if we want to dismiss the alert / prompt or False if we
                      want to cancel it.

                    * The message to enter in the prompt (if this is a prompt).
        """
        msg = 'Chrome handled an %s dialog generated by the page. The message was: "%s"'
        args = (_type, message)
        om.out.debug(msg % args)

        return True, 'Bye!'

    def set_debugging_id(self, debugging_id):
        self.debugging_id = debugging_id
        self.chrome_conn.set_debugging_id(debugging_id)
        self.proxy.set_debugging_id(debugging_id)

    def set_chrome_settings(self):
        """
        Set any configuration settings required for Chrome
        :return: None
        """
        # Disable certificate validation
        self.chrome_conn.Security.setIgnoreCertificateErrors(ignore=True)

        # Disable CSP
        self.chrome_conn.Page.setBypassCSP(enabled=False)

        # Disable downloads
        self.chrome_conn.Page.setDownloadBehavior(behavior='deny')

        # Add JavaScript to be evaluated on every frame load
        self.chrome_conn.Page.addScriptToEvaluateOnNewDocument(source=self.get_dom_analyzer_source())

        # Handle alert and prompts
        self.set_dialog_handler(self.dialog_handler)

        # Enable events
        self.chrome_conn.Page.enable()
        self.chrome_conn.Page.setLifecycleEventsEnabled(enabled=True)

        # Enable console log events
        # https://chromedevtools.github.io/devtools-protocol/tot/Runtime#event-consoleAPICalled
        self.chrome_conn.Runtime.enable()

    def get_dom_analyzer_source(self):
        """
        :return: The JS source code for the DOM analyzer. This is a helper script
                 that runs on the browser side and extracts information for us.

                 According to the `addScriptToEvaluateOnNewDocument` docs:

                    Evaluates given script in every frame upon creation
                    (before loading frame's scripts).

                So we'll be able to override the addEventListener to analyze all
                on* handlers.
        """
        source = []

        if is_running_tests():
            js_onerror_source = file(self.JS_ONERROR_HANDLER).read()
            source.append(js_onerror_source)

        js_selector_generator_source = file(self.JS_SELECTOR_GENERATOR).read()
        source.append(js_selector_generator_source)

        js_dom_analyzer_source = file(self.JS_DOM_ANALYZER).read()
        source.append(js_dom_analyzer_source)

        return '\n\n'.join(source)

    def _page_state_handler(self, message):
        """
        This handler defines the current page state:
            * Null: Nothing has been loaded yet
            * Loading: Chrome is loading the page
            * Done: Chrome has completed loading the page

        :param message: The message as received from chrome
        :return: None
        """
        self._navigator_started_handler(message)
        self._load_url_finished_handler(message)

    def _navigator_started_handler(self, message):
        """
        The handler identifies events which are related to a new page being
        loaded in the browser (Page.Navigate or clicking on an element).

        :param message: The message from chrome
        :return: None
        """
        if 'method' not in message:
            return

        if message['method'] == 'Page.frameScheduledNavigation':
            self._frame_scheduled_navigation = True

            self._frame_stopped_loading_event = False
            self._network_almost_idle_event = False
            self._execution_context_created = False

            self._page_state = self.PAGE_STATE_LOADING
            return

    def _load_url_finished_handler(self, message):
        """
        Knowing when a page has completed loading is difficult

        This handler will wait for two chrome events:
            * Page.frameStoppedLoading
            * Page.lifecycleEvent with name networkIdle

        And set the corresponding flags so that wait_for_load() can return.

        :param message: The message from chrome
        :return: True when the two events were received
                 False when one or none of the events were received
        """
        if 'method' not in message:
            return

        elif message['method'] == 'Page.frameStoppedLoading':
            self._frame_scheduled_navigation = False
            self._frame_stopped_loading_event = True

        elif message['method'] == 'Page.lifecycleEvent':
            if 'params' not in message:
                return

            if 'name' not in message['params']:
                return

            if message['params']['name'] == 'networkAlmostIdle':
                self._frame_scheduled_navigation = False
                self._network_almost_idle_event = True

        elif message['method'] == 'Runtime.executionContextCreated':
            self._frame_scheduled_navigation = False
            self._execution_context_created = True

        if self._network_almost_idle_event and self._frame_stopped_loading_event and self._execution_context_created:
            self._page_state = self.PAGE_STATE_LOADED

    def load_url(self, url):
        """
        Load an URL into the browser, start listening for events.

        :param url: The URL to load
        :return: This method returns immediately, even if the browser is not
                 able to load the URL and an error was raised.
        """
        self._page_state = self.PAGE_STATE_LOADING

        self.chrome_conn.Page.navigate(url=str(url),
                                       timeout=self.PAGE_LOAD_TIMEOUT)

    def load_about_blank(self):
        self.load_url('about:blank')

    def navigation_started(self, timeout=None):
        """
        When an event is dispatched to the browser it is impossible to know
        before-hand if the JS code will trigger a page navigation to a
        new URL.

        Use this method after dispatching an event to know if the browser
        will go to a different URL.

        The method will wait `timeout` seconds for the browser event. If the
        event does not appear in `timeout` seconds then False is returned.

        :param timeout: How many seconds to wait for the event
        :return: True if the page state is PAGE_STATE_LOADING
        """
        timeout = timeout or self.PAGE_WILL_CHANGE_TIMEOUT
        start = time.time()

        while True:
            if self._page_state == self.PAGE_STATE_LOADING:
                return True

            if time.time() - start > timeout:
                return False

            time.sleep(0.1)

    def wait_for_load(self, timeout=None):
        """
        Knowing when a page has completed loading is difficult

        This method works together with _page_state_handler() that
        reads all events and sets the corresponding page state

        If the state is not reached within PAGE_LOAD_TIMEOUT or `timeout`
        the method will exit returning False

        :param timeout: Seconds to wait for the page state
        :return: True when the page state has reached PAGE_STATE_LOADED
                 False when not
        """
        timeout = timeout or self.PAGE_LOAD_TIMEOUT
        start = time.time()

        while True:
            if self._page_state == self.PAGE_STATE_LOADED:
                return True

            if time.time() - start > timeout:
                return False

            time.sleep(0.1)

    def stop(self):
        """
        Stop loading any page and close.

        :return:
        """
        self._page_state = self.PAGE_STATE_LOADED
        self.chrome_conn.Page.stopLoading()

    def get_url(self):
        result = self.chrome_conn.Runtime.evaluate(expression='document.location.href')
        return result['result']['result']['value']

    def get_dom(self):
        result = self.chrome_conn.Runtime.evaluate(expression='document.documentElement.outerHTML')

        # This is a rare case where the DOM is not present
        if result is None:
            return None

        exception_details = result.get('result', {}).get('exceptionDetails', {})
        if exception_details:
            return None

        return result['result']['result']['value']

    def get_navigation_history(self):
        """
        :return: The browser's navigation history, which looks like:
            {
              "currentIndex": 2,
              "entries": [
                {
                  "id": 1,
                  "url": "about:blank",
                  "userTypedURL": "about:blank",
                  "title": "",
                  "transitionType": "typed"
                },
                {
                  "id": 3,
                  "url": "http://127.0.0.1:45571/",
                  "userTypedURL": "http://127.0.0.1:45571/",
                  "title": "",
                  "transitionType": "typed"
                },
                {
                  "id": 5,
                  "url": "http://127.0.0.1:45571/a",
                  "userTypedURL": "http://127.0.0.1:45571/a",
                  "title": "",
                  "transitionType": "link"
                }
              ]
            }
        """
        result = self.chrome_conn.Page.getNavigationHistory()
        return result['result']

    def get_navigation_history_index(self):
        navigation_history = self.get_navigation_history()
        entries = navigation_history['entries']
        index = navigation_history['currentIndex']
        return entries[index]['id']

    def navigate_to_history_index(self, index):
        self._page_state = self.PAGE_STATE_LOADING
        self.chrome_conn.Page.navigateToHistoryEntry(entryId=index)

    def _js_runtime_evaluate(self, expression, timeout=5):
        """
        A wrapper around Runtime.evaluate that provides error handling and
        timeouts.

        :param expression: The expression to evaluate

        :param timeout: The time to wait until the expression is run (in seconds)

        :return: The result of evaluating the expression, None if exceptions
                 were raised in JS during the expression execution.
        """
        result = self.chrome_conn.Runtime.evaluate(expression=expression,
                                                   returnByValue=True,
                                                   generatePreview=True,
                                                   awaitPromise=True,
                                                   timeout=timeout * 1000)

        # This is a rare case where the DOM is not present
        if result is None:
            return None

        if 'result' not in result:
            return None

        if 'result' not in result['result']:
            return None

        if 'value' not in result['result']['result']:
            return None

        return result['result']['result']['value']

    def get_js_variable_value(self, variable_name):
        """
        Read the value of a JS variable and return it. The value will be
        deserialized into a Python object.

        :param variable_name: The variable name. See the unittest at
                              test_load_page_read_js_variable to understand
                              how to reference a variable, it might be counter-
                              intuitive.

        :return: The variable value as a python object
        """
        return self._js_runtime_evaluate(variable_name)

    def get_js_errors(self):
        """
        This method should only be used during unit-testing, since it depends
        on onerror.js being loaded in get_dom_analyzer_source()

        :return: A list with all errors that appeared during the execution of
                 the JS code in the Chrome browser
        """
        return self.get_js_variable_value('window.errors')

    def get_js_set_timeouts(self):
        return list(self.get_js_set_timeouts_iter())

    def get_js_set_timeouts_iter(self):
        """
        :return: An iterator that can be used to read all the set interval handlers
        """
        for event in self._paginate(self.get_js_set_timeouts_paginated):
            yield event

    def get_js_set_timeouts_paginated(self,
                                      start=0,
                                      count=PAGINATION_PAGE_COUNT):
        """
        :param start: The index where to start the current batch at. This is
                      used for pagination purposes. The initial value is zero.

        :param count: The number of events to return in the result. The default
                      value is low, it is preferred to keep this number low in
                      order to avoid large websocket messages flowing from
                      chrome to the python code.

        :return: The event listeners
        """
        start = int(start)
        count = int(count)

        cmd = 'window._DOMAnalyzer.getSetTimeouts(%i, %i)'
        args = (start, count)

        return self.get_js_variable_value(cmd % args)

    def get_js_set_intervals(self):
        return list(self.get_js_set_intervals_iter())

    def get_js_set_intervals_iter(self):
        """
        :return: An iterator that can be used to read all the set interval handlers
        """
        for event in self._paginate(self.get_js_set_intervals_paginated):
            yield event

    def get_js_set_intervals_paginated(self,
                                       start=0,
                                       count=PAGINATION_PAGE_COUNT):
        """
        :param start: The index where to start the current batch at. This is
                      used for pagination purposes. The initial value is zero.

        :param count: The number of events to return in the result. The default
                      value is low, it is preferred to keep this number low in
                      order to avoid large websocket messages flowing from
                      chrome to the python code.

        :return: The event listeners
        """
        start = int(start)
        count = int(count)

        cmd = 'window._DOMAnalyzer.getSetIntervals(%i, %i)'
        args = (start, count)

        return self.get_js_variable_value(cmd % args)

    def get_js_event_listeners(self,
                               event_filter=None,
                               tag_name_filter=None):
        """
        get_js_event_listeners_iter() should be used in most scenarios to prevent
        huge json blobs from being sent from the browser to w3af

        :return: A list of event listeners
        """
        return list(self.get_js_event_listeners_iter(event_filter=event_filter,
                                                     tag_name_filter=tag_name_filter))

    def get_js_event_listeners_iter(self,
                                    event_filter=None,
                                    tag_name_filter=None):
        """
        :return: An iterator that can be used to read all the event listeners
        """
        for event in self._paginate(self.get_js_event_listeners_paginated,
                                    event_filter=event_filter,
                                    tag_name_filter=tag_name_filter):
            yield event

    def get_js_event_listeners_paginated(self,
                                         start=0,
                                         count=PAGINATION_PAGE_COUNT,
                                         event_filter=None,
                                         tag_name_filter=None):
        """
        :param start: The index where to start the current batch at. This is
                      used for pagination purposes. The initial value is zero.

        :param count: The number of events to return in the result. The default
                      value is low, it is preferred to keep this number low in
                      order to avoid large websocket messages flowing from
                      chrome to the python code.

        :return: The event listeners
        """
        start = int(start)
        count = int(count)

        event_filter = event_filter or []
        event_filter = list(event_filter)
        event_filter = repr(event_filter)

        tag_name_filter = tag_name_filter or []
        tag_name_filter = list(tag_name_filter)
        tag_name_filter = repr(tag_name_filter)

        cmd = 'window._DOMAnalyzer.getEventListeners(%s, %s, %i, %i)'
        args = (event_filter, tag_name_filter, start, count)

        event_listeners = self.get_js_variable_value(cmd % args)

        if event_listeners is None:
            # Something happen here... potentially a bug in the instrumented
            # chrome or the dom_analyzer.js code
            return

        for event_listener in event_listeners:
            yield EventListener(event_listener)

    def get_html_event_listeners_iter(self,
                                      event_filter=None,
                                      tag_name_filter=None):
        """
        :param event_filter: A list containing the events to filter by.
                     For example if only the "click" events are
                     required, the value of event_filter should be
                     ['click']. Use an empty filter to return all DOM
                     events.

        :param tag_name_filter: A list containing the tag names to filter by.
                                For example if only the "div" tags should be
                                returned, the value of tag_name_filter should be
                                ['div']. Use an empty filter to return events for
                                all DOM tags.

        :return: The DOM events that match the filters
        """
        for event in self._paginate(self.get_html_event_listeners_paginated,
                                    event_filter=event_filter,
                                    tag_name_filter=tag_name_filter):
            yield event

    def _paginate(self, functor, *args, **kwargs):
        start = 0

        while True:
            page_items = 0

            for result in functor(start, self.PAGINATION_PAGE_COUNT, *args, **kwargs):
                page_items += 1
                yield result

            if page_items < self.PAGINATION_PAGE_COUNT:
                break

            start += self.PAGINATION_PAGE_COUNT

    def get_html_event_listeners(self,
                                 event_filter=None,
                                 tag_name_filter=None):
        """
        :param event_filter: A list containing the events to filter by.
                     For example if only the "click" events are
                     required, the value of event_filter should be
                     ['click']. Use an empty filter to return all DOM
                     events.

        :param tag_name_filter: A list containing the tag names to filter by.
                                For example if only the "div" tags should be
                                returned, the value of tag_name_filter should be
                                ['div']. Use an empty filter to return events for
                                all DOM tags.

        :return: The DOM events that match the filters
        """
        return list(self.get_html_event_listeners_iter(event_filter=event_filter,
                                                       tag_name_filter=tag_name_filter))

    def get_html_event_listeners_paginated(self,
                                           start=0,
                                           count=PAGINATION_PAGE_COUNT,
                                           event_filter=None,
                                           tag_name_filter=None):
        """
        :param start: The index where to start the current batch at. This is
                      used for pagination purposes. The initial value is zero.

        :param count: The number of events to return in the result. The default
                      value is low, it is preferred to keep this number low in
                      order to avoid large websocket messages flowing from
                      chrome to the python code.

        :param event_filter: A list containing the events to filter by.
                             For example if only the "click" events are
                             required, the value of event_filter should be
                             ['click']. Use an empty filter to return all DOM
                             events.

        :param tag_name_filter: A list containing the tag names to filter by.
                                For example if only the "div" tags should be
                                returned, the value of tag_name_filter should be
                                ['div']. Use an empty filter to return events for
                                all DOM tags.

        :return: The DOM events that match the filters
        """
        start = int(start)
        count = int(count)

        event_filter = event_filter or []
        event_filter = list(event_filter)
        event_filter = repr(event_filter)

        tag_name_filter = tag_name_filter or []
        tag_name_filter = list(tag_name_filter)
        tag_name_filter = repr(tag_name_filter)

        cmd = 'window._DOMAnalyzer.getElementsWithEventHandlers(%s, %s, %i, %i)'
        args = (event_filter, tag_name_filter, start, count)

        event_listeners = self.get_js_variable_value(cmd % args)

        if event_listeners is None:
            # Something happen here... potentially a bug in the instrumented
            # chrome or the dom_analyzer.js code
            return

        for event_listener in event_listeners:
            yield EventListener(event_listener)

    def get_all_event_listeners(self,
                                event_filter=None,
                                tag_name_filter=None):

        for event_listener in self.get_js_event_listeners_iter(event_filter=event_filter,
                                                               tag_name_filter=tag_name_filter):
            yield event_listener

        for event_listener in self.get_html_event_listeners_iter(event_filter=event_filter,
                                                                 tag_name_filter=tag_name_filter):
            yield event_listener

    def _is_valid_event_type(self, event_type):
        """
        Validation function to make sure that a specially crafted page can not
        inject JS into dispatch_js_event() and other functions that generate code
        that is then eval'ed

        :param event_type: an event type (eg. click)
        :return: True if valid
        """
        return bool(self.EVENT_TYPE_RE.match(event_type))

    def _escape_js_string(self, text):
        """
        Escapes any double quotes that exist in text. Prevents specially crafted
        pages from injecting JS into functions like dispatch_js_event() that
        generate code that is then eval'ed.

        :param text: The javascript double quoted string to escape
        :return: The string with any double quotes escaped with \
        """
        return text.replace('"', '\\"')

    def capture_screenshot(self):
        """
        :return: The base64 encoded bytes of the captured image
        """
        response = self.chrome_conn.Page.captureScreenshot()

        return response['result']['data']

    def dispatch_js_event(self, selector, event_type):
        """
        Dispatch a new event in the browser
        :param selector: CSS selector for the element where the event is dispatched
        :param event_type: click, hover, etc.
        :return: Exceptions are raised on timeout and unknown events.
                 True is returned on success
        """
        # Perform input validation
        assert self._is_valid_event_type(event_type)
        selector = self._escape_js_string(selector)

        # Dispatch the event that will potentially trigger _navigator_started_handler()
        cmd = 'window._DOMAnalyzer.dispatchCustomEvent("%s", "%s")'
        args = (selector, event_type)

        result = self._js_runtime_evaluate(cmd % args)

        if result is None:
            raise EventTimeout('The event execution timed out')

        elif result is False:
            # This happens when the element associated with the event is not in
            # the DOM anymore
            raise EventException('The event was not run')

        return True

    def get_console_messages(self):
        console_message = self.chrome_conn.read_console_message()

        while console_message is not None:
            yield console_message
            console_message = self.chrome_conn.read_console_message()

    def terminate(self):
        om.out.debug('Terminating %s (did: %s)' % (self, self.debugging_id))

        try:
            self.proxy.stop()
        except Exception, e:
            msg = 'Failed to stop proxy server, exception: "%s" (did: %s)'
            args = (e, self.debugging_id)
            om.out.debug(msg % args)

        try:
            with AllLoggingDisabled():
                self.chrome_conn.close()
        except Exception, e:
            msg = 'Failed to close chrome connection, exception: "%s" (did: %s)'
            args = (e, self.debugging_id)
            om.out.debug(msg % args)

        try:
            self.chrome_process.terminate()
        except Exception, e:
            msg = 'Failed to terminate chrome process, exception: "%s" (did: %s)'
            args = (e, self.debugging_id)
            om.out.debug(msg % args)

        self.proxy = None
        self.chrome_process = None
        self.chrome_conn = None
        self._page_state = self.PAGE_STATE_NONE

    def get_pid(self):
        return self.chrome_process.get_parent_pid() if self.chrome_process is not None else None

    def get_memory_usage(self):
        """
        :return: The memory usage for the chrome process (parent) and all its
                 children (chrome uses various processes for rendering HTML)
        """
        parent = self.chrome_process.get_parent_pid()
        children = self.chrome_process.get_children_pids()

        if parent is None:
            return None, None

        _all = [parent]
        _all.extend(children)

        private, shared, count, total = get_memory_usage(_all, True)

        private = sum(p[1] for p in private)
        private = int(private)

        shared = sum(s[1] for s in shared.items())
        shared = int(shared)

        return private, shared

    def __str__(self):
        proxy_port = None
        devtools_port = None

        if self.proxy is not None:
            proxy_port = self.get_proxy_address()[1]

        if self.chrome_process is not None:
            devtools_port = self.chrome_process.get_devtools_port()

        pid = self.get_pid()

        args = (self.id, proxy_port, pid, devtools_port)
        msg = '<InstrumentedChrome (id:%s, proxy:%s, process_id: %s, devtools:%s)>'
        return msg % args


class InstrumentedChromeException(Exception):
    pass


class EventTimeout(Exception):
    pass


class EventException(Exception):
    pass


class AllLoggingDisabled(object):
    """
    A context manager that will prevent any logging messages
    triggered during the body from being processed.
    """
    def __init__(self):
        self.previous_level = logging.INFO

    def __enter__(self, highest_level=logging.CRITICAL):
        """
        :param highest_level: The maximum logging level in use.
                              This would only need to be changed if a custom level
                              greater than CRITICAL is defined.
        :return: None
        """
        # two kind-of hacks here:
        #    * can't get the highest logging level in effect => delegate to the user
        #    * can't get the current module-level override => use an undocumented
        #       (but non-private!) interface
        self.previous_level = logging.root.manager.disable
        logging.disable(highest_level)

    def __exit__(self, exc_type, exc_val, exc_tb):
        logging.disable(self.previous_level)

        # If we returned True here, any exception would be suppressed!
        return False


class EventListener(object):
    def __init__(self, event_as_dict):
        self._event_as_dict = event_as_dict

    def get_type_selector(self):
        return (self._event_as_dict['event_type'],
                self._event_as_dict['selector'],)

    def __getitem__(self, item):
        return self._event_as_dict[item]

    def get(self, item):
        return self._event_as_dict.get(item)

    def __setitem__(self, key, value):
        self._event_as_dict[key] = value

    def __eq__(self, other):
        if len(other) != len(self._event_as_dict):
            return False

        for key, value in self._event_as_dict.iteritems():
            other_value = other.get(key)

            if value != other_value:
                return False

        return True

    def __len__(self):
        return len(self._event_as_dict)

    def __repr__(self):
        return 'EventListener(%r)' % self._event_as_dict
